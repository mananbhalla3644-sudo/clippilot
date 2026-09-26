from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..control.actuator import Actuator
from ..control.safety import AuditLog, SafetyPolicy
from ..errors import (
    ActionError,
    ClipPilotError,
    ConfirmationRequired,
    FailsafeTriggered,
    LLMError,
    SafetyViolation,
)
from ..handoff.manager import Handoff
from ..observation import PerceptionEngine
from ..perception.vlm import VisionModel, extract_json
from ..recorder import Recorder
from ..types import ActionResult, Observation, RunResult, Step
from ..utils.imaging import data_uri
from ..utils.logging import get_logger
from . import prompts, schema
from .executor import VideoExecutor
from .llm import Completion, LlmClient
from .memory import Memory

log = get_logger("agent.loop")

Event = Callable[[str, dict[str, Any]], None]
Approver = Callable[[dict[str, Any], str], bool]
Asker = Callable[[str, list[str], str], str]


def _default_approver(action: dict[str, Any], reason: str) -> bool:
    print()
    print(f"  [approval needed] {reason}")
    print(f"  action: {json.dumps({k: v for k, v in action.items() if k != 'plan'})[:300]}")
    try:
        ans = input("  allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes", "always", "a")


def _default_asker(question: str, options: list[str], why: str) -> str:
    print()
    if why:
        print(f"  (agent asked: {why})")
    print(f"  {question}")
    if options:
        print("  options: " + " | ".join(options))
    try:
        return input("  your answer: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


class Agent:
    """observe -> think -> act -> verify, with a human in the loop."""

    def __init__(
        self,
        config: Config,
        on_event: Event | None = None,
        approver: Approver | None = None,
        asker: Asker | None = None,
        run_dir: str | Path | None = None,
        memory: Memory | None = None,
    ) -> None:
        self.cfg = config
        self.on_event = on_event
        self.approver = approver or _default_approver
        self.asker = asker or _default_asker
        self.interactive = bool(config.agent.get("interactive", True))
        self.max_steps = int(config.agent.get("max_steps", 40))
        self.max_retries = int(config.agent.get("max_retries_per_action", 3))
        self.vlm_verify = bool(config.agent.get("vlm_verify", False))
        self.dry_run = bool(config.control.get("dry_run", False))

        self.llm = LlmClient(config)
        self.vision = VisionModel(self.llm, config) if config.vlm.get("enabled", True) else None
        self.perception = PerceptionEngine(config, self.vision)
        self.actuator = Actuator(config, self.perception, on_event=on_event)
        self.safety = SafetyPolicy(config)
        self.handoff = Handoff(config, self.perception, self.actuator, on_event=on_event)
        self.video = VideoExecutor(
            config,
            self.handoff,
            on_event=on_event,
            on_progress=lambda pct, msg: self._emit("progress", {"pct": round(pct, 3), "message": msg}),
        )
        self.run_dir = Path(run_dir) if run_dir else self.cfg.resolve_path(self.cfg.agent.get("run_dir", "runs")) / time.strftime("%Y%m%d-%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.perception.set_shot_dir(self.run_dir)
        self.memory = memory or Memory(
            state_path=self.cfg.root / ".clippilot" / "state.json",
            history_limit=int(config.agent.get("history_limit", 24)),
        )
        self.recorder = Recorder(self.run_dir / "episode.jsonl")
        self.audit = AuditLog(self.run_dir / "audit.jsonl")
        self.aborted = False
        self._last_obs: Observation | None = None

    # ------------------------------------------------------------------ events
    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def abort(self) -> None:
        self.aborted = True
        self._emit("abort", {})

    # ----------------------------------------------------------------- capture
    def _screenshot_uri(self, obs: Observation) -> str | None:
        if not self.cfg.perception.get("llm_image", True) or obs.image is None:
            return None
        try:
            return data_uri(obs.image, quality=int(self.cfg.vlm.get("jpeg_quality", 80)), max_side=int(self.cfg.vlm.get("max_side", 1450)))
        except Exception as exc:  # noqa: BLE001
            log.debug("screenshot encode failed: %s", exc)
            return None

    def _observation_text(self, obs: Observation, step: int) -> str:
        from ..types import summarize_for_log

        head = f"STEP {step} - current screen"
        body = summarize_for_log(obs, max_chars=1800)
        extra = []
        if obs.note:
            extra.append(obs.note)
        if self.dry_run:
            extra.append("(DRY RUN: nothing has actually happened yet on the real machine)")
        if extra:
            body += "\n" + "\n".join(extra)
        facts = self.memory.facts_for_prompt()
        if facts:
            body += "\nremembered facts:\n" + facts
        return f"{head}\n{body}"

    # --------------------------------------------------------------------- run
    def run(self, task: str) -> RunResult:
        result = RunResult(task=task, transcript_path=self.run_dir / "transcript.md")
        self._emit("run_start", {"task": task, "run_dir": str(self.run_dir), "dry_run": self.dry_run})
        self.perception.set_event_hook(self.on_event)

        system = prompts.build_system_prompt(self.cfg, native_tools=self.llm.native_tools, dry_run=self.dry_run)
        self.memory.set_system(system)
        self.memory.add("user", prompts.build_task_prompt(task, {"run_dir": str(self.run_dir)}))
        self._save_system(system)

        history = self.memory.messages  # same list object, so memory always mirrors the conversation
        attempt_sig: dict[str, int] = {}
        last_obs: Observation | None = None

        for step in range(1, self.max_steps + 1):
            if self.aborted:
                result.summary = "aborted by the operator"
                break

            # ---- observe
            region = last_obs.screen if last_obs else None
            obs = self.perception.observe(region=region, save=True)
            last_obs = obs
            self._emit("observation", {"step": step, **obs.describe(), "screenshot": str(obs.image_path) if obs.image_path else None})

            # ---- think
            history.append({"role": "user", "content": prompts.observation_message(
                self._observation_text(obs, step), self._screenshot_uri(obs), step
            )[0]["content"]})
            if self.memory.needs_compaction():
                self.memory.compact(self.llm)
                history = self.memory.messages

            try:
                comp = self.llm.chat(history, tools=schema.TOOLS)
            except LLMError as exc:
                result.summary = f"LLM failure: {exc}"
                self._emit("error", {"message": result.summary})
                break

            try:
                action = self._extract_action(comp)
            except ActionError as exc:
                history.append({"role": "user", "content": f"Invalid action: {exc}. Reply with one valid tool call."})
                continue

            thought = comp.text.strip()[:600] if comp.text else ""
            st = Step(index=step, action=action, thought=thought, observation=obs)
            result.steps.append(st)
            self._emit("step", st.to_dict())
            history.append({"role": "assistant", "content": comp.text or f"(chose {action.get('type')})"})

            # ---- safety
            try:
                self.safety.enforce(action, obs)
            except SafetyViolation as exc:
                st.result = ActionResult(ok=False, message=f"refused by safety policy: {exc}")
                self._emit("step_result", {"step": step, **st.result.to_dict()})
                history.append({"role": "user", "content": prompts.result_message(action, False, str(exc),
                                                          "The policy blocks this. Do not try a sneaky variant; find a legitimate route or ask_human.")})
                continue

            need, reason = self.safety.needs_confirmation(action)
            if need:
                ok = self.approver(action, reason) if self.interactive else False
                self.audit.add(action, ok, reason)
                if not ok:
                    st.result = ActionResult(ok=False, message="denied by the human operator")
                    self._emit("step_result", {"step": step, **st.result.to_dict()})
                    history.append({"role": "user", "content": prompts.result_message(
                        action, False, "the human declined this step. Continue without it, or ask_human what they prefer.")})
                    continue

            # ---- act
            try:
                action_result, terminal, obs_after = self._perform(action, obs, history, step)
            except FailsafeTriggered as exc:
                st.result = ActionResult(ok=False, message=str(exc))
                self._emit("step_result", {"step": step, **st.result.to_dict()})
                result.summary = "stopped: failsafe triggered (pointer in the abort corner)"
                break
            except ClipPilotError as exc:
                action_result = ActionResult(ok=False, message=str(exc))
                terminal = None
                obs_after = obs
            except Exception as exc:  # noqa: BLE001
                log.exception("step %d crashed", step)
                action_result = ActionResult(ok=False, message=f"{type(exc).__name__}: {exc}")
                terminal = None
                obs_after = obs

            st.result = action_result
            self.recorder.record(step, action, action_result, obs)
            self._emit("step_result", {"step": step, "action": action.get("type"), **action_result.to_dict()})
            if obs_after is not None:
                last_obs = obs_after

            # ---- feedback
            if action_result.ok:
                sig = _signature(action)
                attempt_sig.pop(sig, None)
                extra = self._verification_note(action, action_result, obs_after)
                history.append({"role": "user", "content": prompts.result_message(action, True, action_result.message, extra)})
            else:
                sig = _signature(action)
                attempt_sig[sig] = attempt_sig.get(sig, 0) + 1
                if action.get("type") == "ask_human" and action_result.ok:
                    pass
                note = prompts.failure_coach({**action, "_attempts": attempt_sig[sig]}, action_result.message)
                if attempt_sig[sig] >= self.max_retries:
                    note += "\n" + prompts.blocked_coach(action, attempt_sig[sig])
                history.append({"role": "user", "content": prompts.result_message(action, False, action_result.message, note)})

            if terminal == "done":
                result.success = True
                result.summary = action_result.message
                break
            if terminal == "give_up":
                result.summary = action_result.message
                break

        result.finished = time.time()
        result.usage = dict(self.llm.usage)
        self._emit("run_end", result.to_dict())
        if self.cfg.agent.get("save_transcript", True):
            self._write_transcript(result, system)
        self.perception.set_event_hook(None)
        return result

    # ------------------------------------------------------------------ actions
    def _extract_action(self, comp: Completion) -> dict[str, Any]:
        if comp.tool_calls:
            tc = comp.tool_calls[0]
            if not tc.name:
                raise ActionError("tool call had no function name")
            return schema.validate(schema.parse_tool_call(tc.name, tc.arguments))
        if comp.text.strip():
            data = extract_json(comp.text)
            if isinstance(data, dict):
                name = str(data.get("name") or data.get("action") or data.get("type") or "")
                args = data.get("arguments") or data.get("args") or data.get("parameters") or {}
                if not args and name in data:
                    args = {k: v for k, v in data.items() if k not in ("name", "action", "type")}
                if not name:
                    raise ActionError(f"action object missing a name: {json.dumps(data)[:160]}")
                return schema.validate(schema.parse_tool_call(name, args))
        raise ActionError("the model did not return an action")

    def _perform(
        self,
        action: dict[str, Any],
        obs: Observation,
        history: list[dict[str, Any]],
        step: int,
    ) -> tuple[ActionResult, str | None, Observation | None]:
        t = str(action.get("type"))

        if t == "observe":
            region = None
            if action.get("region"):
                from ..utils.geometry import Box

                try:
                    region = Box(*[int(v) for v in action["region"]][:4])
                except (TypeError, ValueError):
                    region = None
            new_obs = self.perception.observe(
                region=region,
                with_ocr=bool(action.get("with_ocr", True)),
                describe=bool(action.get("describe", False)),
                save=True,
            )
            return ActionResult(ok=True, message=f"observed {new_obs.screen}, {len(new_obs.elements)} elements"), None, new_obs

        if t == "list_windows":
            from ..control import win32

            wins = win32.list_windows()[:12]
            lines = [f"- {w.title} [{w.process or '?'}] {w.rect}" for w in wins]
            msg = "visible windows:\n" + ("\n".join(lines) or "  (none)")
            return ActionResult(ok=True, message=msg, data={"windows": [w.to_dict() for w in wins]}), None, obs

        if t == "focus_window":
            from ..control import win32

            w = win32.focus_window(str(action.get("title_contains", "")), maximize=bool(action.get("maximize")))
            time.sleep(0.5)
            new_obs = self.perception.observe(region=obs.screen, save=True)
            msg = f"focused {w.title}" if w else f"no window matching {action.get('title_contains')!r}"
            return ActionResult(ok=w is not None, message=msg), None, new_obs

        if t == "launch_app":
            from ..control import win32

            app = str(action.get("app", ""))
            try:
                path, pid = win32.launch(app, action.get("args"), wait=float(action.get("wait", 6.0)))
            except (FileNotFoundError, OSError) as exc:
                return ActionResult(ok=False, message=str(exc)), None, obs
            if self.dry_run:
                return ActionResult(ok=True, message=f"[dry-run] would launch {app}"), None, obs
            new_obs = self.perception.observe(region=obs.screen, save=True)
            return ActionResult(ok=True, message=f"launched {path}", data={"pid": pid}), None, new_obs

        if t == "assert":
            return self._assert(action, obs), None, obs

        if t == "edit_video":
            if self.dry_run:
                plan = action.get("plan", {})
                return ActionResult(ok=True, message=f"[dry-run] would render {len(plan.get('segments', []))} segment(s) to {plan.get('output')}"), None, obs
            res = self.video.render(action.get("plan") or {}, dry_run=False)
            return res, None, obs

        if t == "plan_video":
            res = self.video.analyse(
                str(action.get("mode", "highlights")),
                str(action.get("input", "")),
                top_n=action.get("top_n"),
                max_seconds=action.get("max_seconds"),
                min_silence=action.get("min_silence"),
            )
            return res, None, obs

        if t == "handoff":
            app = str(action.get("app", ""))
            project = str(action.get("project", ""))
            if not project and self.video.last_render:
                project = str(self.video.last_render.get("video", ""))
            if not project:
                return ActionResult(ok=False, message="handoff needs a project path, or run edit_video first"), None, obs
            if self.dry_run:
                return ActionResult(ok=True, message=f"[dry-run] would hand {project} to {app}"), None, obs
            res = self.handoff.run(app, project, notes=str(action.get("notes", "")), overwrite=bool(action.get("overwrite")))
            new_obs = self.perception.observe(region=obs.screen, save=True) if res.ok else obs
            return res, None, new_obs

        if t == "ask_human":
            if not self.interactive:
                return ActionResult(ok=False, message="this session is non-interactive; cannot ask the user"), None, obs
            question = str(action.get("question", ""))
            answer = self.asker(question, [str(o) for o in (action.get("options") or [])], str(action.get("why", "")))
            self.memory.remember(f"answer_to_{question[:40]}", answer)
            return ActionResult(ok=True, message=f"the human said: {answer!r}"), None, obs

        if t == "task_done":
            artifacts = [str(a) for a in (action.get("artifacts") or [])]
            msg = str(action.get("summary", "done"))
            if artifacts:
                msg += "\nartifacts:\n" + "\n".join(f"  {a}" for a in artifacts)
            return ActionResult(ok=True, message=msg, data={"artifacts": artifacts}), "done", obs

        if t == "give_up":
            tried = action.get("tried") or []
            msg = str(action.get("reason", "gave up"))
            if tried:
                msg += "\ntried:\n" + "\n".join(f"  - {x}" for x in tried)
            return ActionResult(ok=True, message=msg), "give_up", obs

        if t == "run_command":
            return self.actuator.execute(action, obs), None, obs

        # everything else is raw screen control
        res = self.actuator.execute(action, obs)
        new_obs = obs
        if res.ok and t in ("click", "key", "type", "drag", "scroll"):
            time.sleep(self.actuator.settle())
            new_obs = self.perception.observe(region=obs.screen, save=True)
        return res, None, new_obs

    def _assert(self, action: dict[str, Any], obs: Observation) -> ActionResult:
        checks: list[tuple[str, bool]] = []
        if action.get("text_present"):
            checks.append((f"text {action['text_present']!r} present", obs.has_text(str(action["text_present"]))))
        if action.get("text_absent"):
            checks.append((f"text {action['text_absent']!r} absent", not obs.has_text(str(action["text_absent"]))))
        if action.get("window_title_contains"):
            from ..control import win32

            checks.append((f"window {action['window_title_contains']!r} open", win32.find_window(str(action["window_title_contains"])) is not None))
        if action.get("statement") and self.vision and obs.image is not None:
            verdict = self.vision.verify(obs.image, str(action["statement"]))
            checks.append((f"vision says {str(action['statement'])!r} is true", verdict.passed))
            if not verdict.passed and verdict.reason:
                return ActionResult(ok=False, message=f"vision check failed: {verdict.reason}")
        if not checks:
            return ActionResult(ok=False, message="assert needs at least one condition")
        failed = [name for name, ok in checks if not ok]
        detail = "; ".join(f"{'PASS' if ok else 'FAIL'} {name}" for name, ok in checks)
        return ActionResult(ok=not failed, message=detail)

    def _verification_note(self, action: dict[str, Any], result: ActionResult, obs: Observation | None) -> str:
        notes: list[str] = []
        if result.diff:
            verdict = "screen changed" if result.diff > 0.001 else "screen did NOT change"
            notes.append(f"post-action {verdict} (diff={result.diff:.3f}) - if nothing changed, the click probably missed")
        if self.vlm_verify and obs is not None and obs.image is not None and str(action.get("type")) in ("click", "key", "type"):
            verdict = self.vision.verify(obs.image, str(action.get("expect") or f"the action {action.get('type')} had the intended effect"))
            notes.append(f"vision verification: {'ok' if verdict.passed else 'NOT ok'} - {verdict.reason}")
        return "\n".join(notes)

    # -------------------------------------------------------------- transcripts
    def _save_system(self, system: str) -> None:
        (self.run_dir / "system_prompt.txt").write_text(system, encoding="utf-8")

    def _write_transcript(self, result: RunResult, system: str) -> None:
        lines = [
            f"# ClipPilot run - {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"**Task:** {result.task}",
            f"**Result:** {'SUCCESS' if result.success else 'INCOMPLETE'}",
            f"**Duration:** {result.duration:.1f}s",
            f"**Steps:** {len(result.steps)}",
            f"**Dry run:** {self.dry_run}",
            f"**Usage:** {json.dumps(result.usage)}",
            "",
        ]
        if result.summary:
            lines += [f"**Summary:** {result.summary}", ""]
        for st in result.steps:
            action = {k: v for k, v in st.action.items() if k != "plan"}
            plan = st.action.get("plan")
            lines.append(f"## Step {st.index} - `{action.get('type')}`")
            if st.thought:
                lines += ["", f"> {st.thought}"]
            lines += ["", "```json", json.dumps(action, indent=2)[:1200], "```"]
            if plan:
                lines += ["", f"plan: {len(plan.get('segments', []))} segment(s) -> `{plan.get('output')}`"]
            if st.result:
                mark = "ok" if st.result.ok else "FAILED"
                lines += ["", f"- result: **{mark}** - {st.result.message}"]
            if st.observation and st.observation.image_path:
                lines += ["", f"![screen]({st.observation.image_path.name})"]
            lines.append("")
        try:
            (self.run_dir / "transcript.md").write_text("\n".join(lines), encoding="utf-8")
            result.transcript_path = self.run_dir / "transcript.md"
        except OSError as exc:
            log.warning("could not write transcript: %s", exc)


def _signature(action: dict[str, Any]) -> str:
    keys = [str(action.get("type"))]
    for k in ("text", "element_id", "keys", "command", "app", "title_contains", "mode", "input"):
        if k in action:
            keys.append(f"{k}={str(action[k])[:80]}")
    return "|".join(keys)
