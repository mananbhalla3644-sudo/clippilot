"""Agent loop test with a scripted LLM (no API key needed).

Exercises: tool-call parsing, dry-run execution, safety refusal, approval flow,
retry coaching, edit_video dispatch, failure recovery, and the transcript.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ["CLIPPILOT_FFMPEG"] = str(ROOT / "bin" / "ffmpeg.exe")
os.environ["CLIPPILOT_FFPROBE"] = str(ROOT / "bin" / "ffprobe.exe")

from clippilot.agent import llm as llm_mod  # noqa: E402
from clippilot.agent.loop import Agent  # noqa: E402
from clippilot.config import load_config  # noqa: E402

SMOKE = ROOT / ".smoke"
MEDIA = SMOKE / "a.mp4"
ok = True


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    ok = ok and bool(cond)


class ScriptedLlm:
    """Stands in for LlmClient: pops one scripted completion per call."""

    instances: list["ScriptedLlm"] = []

    def __init__(self, script: list[tuple[str, dict[str, Any]] | None], text: str = "") -> None:
        self.script = list(script)
        self.text = text
        self.i = 0
        self.calls: list[list[dict[str, Any]]] = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        self.native_tools = True
        self.model = "scripted"
        self.base_url = "scripted://"
        self.api_key = "none"
        ScriptedLlm.instances.append(self)

    def chat(self, messages, tools=None, model=None, temperature=None, max_tokens=None, json_mode=False):
        self.calls.append(list(messages))
        self.usage["calls"] += 1
        item = self.script[self.i] if self.i < len(self.script) else ("task_done", {"summary": "script exhausted"})
        self.i += 1
        name, args = item
        comp = llm_mod.Completion(model="scripted", text=self.text)
        comp.tool_calls.append(llm_mod.ToolCall(name, args, id=f"call_{self.i}"))
        return comp

    def complete(self, messages, **kw):
        return self.text or "ok"

    def ping(self):
        return {"ok": True, "model": "scripted"}


def make_agent(script, text="thinking...", approver=None, asker=None, cfg_over: dict | None = None):
    cfg = load_config(str(ROOT / "config.example.yaml"))
    cfg.set("control.dry_run", True)
    cfg.set("agent.max_steps", 14)
    cfg.set("perception.ocr_backend", "none")
    cfg.set("safety.allow_run_command", False)
    for k, v in (cfg_over or {}).items():
        cfg.set(k, v)
    fake = ScriptedLlm(script, text)
    orig_init = Agent.__init__

    def patched(self, config, **kwargs):
        orig_init(self, config, **kwargs)
        self.llm = fake

    Agent.__init__ = patched  # type: ignore[method-assign]
    agent = Agent(cfg, approver=approver, asker=asker, run_dir=ROOT / ".smoke" / "runs")
    Agent.__init__ = orig_init  # type: ignore[method-assign]
    return agent, fake


def main() -> int:
    if not MEDIA.exists():
        print("run tests/smoke_video.py first")
        return 1

    print("1. happy path: click -> assert -> done")
    agent, fake = make_agent([
        ("click", {"text": "File", "index": 0}),
        ("key", {"keys": "ctrl+s"}),
        ("assert", {"text_present": "definitely-not-on-screen"}),
        ("task_done", {"summary": "saved the project", "artifacts": [str(MEDIA)]}),
    ])
    res = agent.run("do a small thing")
    check("run succeeded", res.success, res.summary)
    check("4 steps recorded", len(res.steps) == 4, f"{len(res.steps)}")
    check("summary carried through", "saved the project" in res.summary)
    check("transcript written", res.transcript_path and res.transcript_path.exists())
    if res.transcript_path:
        text = res.transcript_path.read_text(encoding="utf-8")
        check("transcript has the task", "do a small thing" in text)
        check("transcript lists step actions", "click" in text and "task_done" in text)
    check("usage counted", res.usage.get("calls", 0) >= 4, str(res.usage))
    check("observations sent to the llm", all(len(c) > 1 for c in fake.calls))

    print("\n2. dry run: nothing real happens")
    agent, _ = make_agent([
        ("click", {"x": 400, "y": 300}),
        ("type", {"text": "hello world"}),
        ("task_done", {"summary": "ok"}),
    ])
    res = agent.run("dry run please")
    msgs = [m for c in [_ for _ in []] for m in c]
    actions = [s.action for s in res.steps]
    check("click executed (dry)", actions[0]["type"] == "click")
    check("typing executed (dry)", actions[1]["type"] == "type")
    check("system prompt warns about dry run", "DRY RUN" in agent.memory.messages[0]["content"])

    print("\n3. safety: run_command is blocked")
    agent, _ = make_agent([
        ("run_command", {"command": "echo pwned"}),
        ("task_done", {"summary": "gave up"}),
    ])
    res = agent.run("try a command")
    refused = [s for s in res.steps if s.action["type"] == "run_command"]
    check("run_command attempted", len(refused) == 1)
    check("run_command result is a refusal", refused[0].result is not None and not refused[0].result.ok)
    check("refusal explains the policy", "allow_run_command" in (refused[0].result.message or ""))

    print("\n4. safety: denylisted hotkey")
    agent, _ = make_agent([
        ("key", {"keys": "ctrl+alt+delete"}),
        ("task_done", {"summary": "ok"}),
    ])
    res = agent.run("lock the machine please")
    k = res.steps[0]
    check("denylisted key refused", k.result is not None and not k.result.ok, k.result.message if k.result else "")

    print("\n5. safety: approval flow, declined")
    seen: list[tuple[str, str]] = []

    def deny(action, reason):
        seen.append((action.get("type", ""), reason))
        return False

    agent, _ = make_agent([
        ("key", {"keys": "alt+f4"}),
        ("task_done", {"summary": "ok"}),
    ], approver=deny)
    res = agent.run("close everything")
    check("approver was consulted", len(seen) == 1, str(seen))
    check("alt+f4 flagged for approval", "close" in seen[0][1].lower() if seen else False, str(seen))
    declined = [s for s in res.steps if s.action["type"] == "key"]
    check("declined action did not run", declined and declined[0].result is not None and not declined[0].result.ok)

    print("\n6. safety: approval flow, approved")
    calls = []

    def yes(action, reason):
        calls.append(reason)
        return True

    agent, _ = make_agent([
        ("key", {"keys": "alt+f4"}),
        ("task_done", {"summary": "closed it"}),
    ], approver=yes)
    res = agent.run("close everything")
    check("approved action ran", res.steps[0].result is not None and res.steps[0].result.ok, res.steps[0].result.message)
    check("audit log written", (agent.run_dir / "audit.jsonl").exists())
    if (agent.run_dir / "audit.jsonl").exists():
        entries = [json.loads(l) for l in (agent.run_dir / "audit.jsonl").read_text().splitlines() if l.strip()]
        check("audit records the approval", any(e.get("approved") for e in entries), f"{len(entries)} entries")

    print("\n7. safety: never types credentials")
    agent, _ = make_agent([
        ("type", {"text": "my password is hunter2"}),
        ("task_done", {"summary": "ok"}),
    ])
    res = agent.run("log in for me")
    t = res.steps[0]
    check("credential-ish typing refused", t.result is not None and not t.result.ok, t.result.message if t.result else "")

    print("\n8. ask_human round trip")
    asked: list[str] = []

    def answer(question, options, why):
        asked.append(question)
        return "use the second one"

    agent, _ = make_agent([
        ("ask_human", {"question": "which take should I use?", "options": ["take 1", "take 2"]}),
        ("task_done", {"summary": "used take 2"}),
    ], asker=answer)
    res = agent.run("pick the best take")
    check("asker was called", len(asked) == 1, str(asked))
    check("answer reached the agent", "take 2" in res.summary or res.success)

    print("\n9. failure recovery: coaching after a bad target")
    agent, _ = make_agent([
        ("click", {"text": "Zzz-not-a-real-button"}),
        ("click", {"text": "Also not real"}),
        ("task_done", {"summary": "recovered"}),
    ])
    res = agent.run("click something")
    first, second = res.steps[0], res.steps[1]
    check("bad target failed", first.result is not None and not first.result.ok)
    check("failure message lists visible labels", "Visible labels" in (first.result.message or ""))
    last_user_msgs = [str(m.get("content")) for m in agent.memory.messages if m.get("role") == "user"]
    check("agent was coached", any("Coach:" in c for c in last_user_msgs),
          next((c[:90] for c in last_user_msgs if "Coach:" in c), "no coach message found"))
    check("coach suggests another approach", any("different element" in c or "keyboard shortcut" in c
                                                 for c in last_user_msgs if "Coach:" in c))

    print("\n10. blocked after repeated identical failures")
    agent, _ = make_agent([("click", {"text": "Nope"})] * 4 + [("give_up", {"reason": "cannot find it"})])
    res = agent.run("find the thing")
    coach_msgs = [str(m.get("content")) for m in agent.memory.messages if m.get("role") == "user"]
    check("blocked-coach sent eventually", any("tries and it keeps failing" in c for c in coach_msgs) or
          any("Do not repeat" in c for c in coach_msgs))
    check("gave up terminates the run", res.steps[-1].action["type"] == "give_up")
    check("give_up is not a success", res.success is False)

    print("\n11. real edit_video through the loop (live render)")
    out = SMOKE / "agent_render.mp4"
    if out.exists():
        out.unlink()
    agent, _ = make_agent([
        ("edit_video", {"plan": {
            "segments": [{"source": str(MEDIA), "start": 1.0, "end": 4.0}],
            "output": str(out),
            "resolution": "1080x1920",
            "crf": 30,
            "preset": "ultrafast",
            "overwrite": True,
        }}),
        ("task_done", {"summary": "rendered"}),
    ], cfg_over={"control.dry_run": False})
    res = agent.run("trim and go vertical")
    ed = res.steps[0]
    check("edit_video ran", ed.result is not None and ed.result.ok, (ed.result.message if ed.result else "")[:120])
    check("file exists after render", out.exists(), f"{out.stat().st_size/1024:.0f}KB" if out.exists() else "missing")
    check("vertical output", ed.result.data.get("info", {}).get("height") == 1920 if ed.result else False,
          str(ed.result.data.get("info"))[:80] if ed.result else "")
    check("edl handed back", bool(ed.result.data.get("edl")) if ed.result else False)

    print("\n12. render refusal when the output exists")
    agent, _ = make_agent([
        ("edit_video", {"plan": {
            "segments": [{"source": str(MEDIA), "start": 0, "end": 2}],
            "output": str(out),
        }}),
        ("task_done", {"summary": "done"}),
    ], cfg_over={"control.dry_run": False})
    res = agent.run("overwrite it")
    check("existing output refused", res.steps[0].result is not None and not res.steps[0].result.ok,
          (res.steps[0].result.message if res.steps[0].result else "")[:100])

    print("\n13. invalid plan is reported, not crashed")
    agent, _ = make_agent([
        ("edit_video", {"plan": {"segments": [{"source": str(MEDIA), "start": 5, "end": 1}], "output": "x.mp4"}}),
        ("task_done", {"summary": "gave up"}),
    ], cfg_over={"control.dry_run": False})
    res = agent.run("make an impossible edit")
    check("bad plan reported", res.steps[0].result is not None and not res.steps[0].result.ok)
    check("plan error is readable", "plan" in (res.steps[0].result.message or "").lower())

    print("\n14. handoff to an unknown app")
    agent, _ = make_agent([
        ("handoff", {"app": "not_an_app", "project": str(out)}),
        ("task_done", {"summary": "ok"}),
    ], cfg_over={"control.dry_run": False})
    res = agent.run("open it")
    h = res.steps[0]
    check("unknown app reported", h.result is not None and not h.result.ok, (h.result.message if h.result else "")[:90])
    check("agent was told which apps exist", "capcut" in (h.result.message if h.result else ""))

    print("\n15. list_windows / focus_window are handled by the loop")
    agent, _ = make_agent([
        ("list_windows", {}),
        ("focus_window", {"title_contains": "no-such-window-xyz"}),
        ("task_done", {"summary": "ok"}),
    ])
    res = agent.run("what is open?")
    lw = res.steps[0]
    check("list_windows returned a list", lw.result is not None and lw.result.ok and "windows" in lw.result.message)
    fw = res.steps[1]
    check("missing window reported as failure", fw.result is not None and not fw.result.ok,
          (fw.result.message if fw.result else "")[:60])

    print("\n16. max_steps is respected")
    agent, _ = make_agent([("observe", {})] * 40, cfg_over={"agent.max_steps": 5})
    res = agent.run("loop forever")
    check("stopped at max_steps", len(res.steps) == 5, f"{len(res.steps)} steps")

    print("\n17. plan_video analysis through the loop")
    agent, _ = make_agent([
        ("plan_video", {"mode": "highlights", "input": str(MEDIA), "top_n": 2}),
        ("task_done", {"summary": "found them"}),
    ], cfg_over={"control.dry_run": False})
    res = agent.run("find the good bits")
    pv = res.steps[0]
    check("plan_video ran", pv.result is not None and pv.result.ok, (pv.result.message if pv.result else "")[:100])
    check("segments returned", bool(pv.result.data.get("segments")) if pv.result else False,
          str(pv.result.data.get("segments"))[:100] if pv.result else "")

    print("\n18. memory facts persist")
    agent, _ = make_agent([("task_done", {"summary": "ok"})])
    agent.memory.remember("user_prefers", "vertical 1080x1920")
    facts_file = ROOT / ".clippilot" / "state.json"
    check("fact persisted to disk", facts_file.exists(), str(facts_file))
    if facts_file.exists():
        check("fact readable back", agent.memory.recall("user_prefers") == "vertical 1080x1920")

    print("\n19. event stream fires the UI needs")
    events: list[str] = []
    cfg = load_config(str(ROOT / "config.example.yaml"))
    cfg.set("control.dry_run", True)
    cfg.set("perception.ocr_backend", "none")
    cfg.set("agent.max_steps", 4)
    fake = ScriptedLlm([("click", {"x": 10, "y": 10}), ("task_done", {"summary": "ok"})], text="thought")
    orig = Agent.__init__

    def patched2(self, config, **kwargs):
        orig(self, config, **kwargs)
        self.llm = fake

    Agent.__init__ = patched2  # type: ignore[method-assign]
    a2 = Agent(cfg, on_event=lambda k, p: events.append(k), approver=lambda *a: True, asker=lambda *a: "")
    Agent.__init__ = orig  # type: ignore[method-assign]
    a2.run("hi")
    for kind in ("run_start", "observation", "step", "step_result", "run_end"):
        check(f"emitted {kind}", kind in events, f"{len(events)} events total")

    print("\n" + ("ALL AGENT TESTS PASSED" if ok else "SOME AGENT TESTS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
