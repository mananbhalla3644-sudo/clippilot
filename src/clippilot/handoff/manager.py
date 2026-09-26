from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..control.actuator import Actuator
from ..errors import ActionError
from ..observation import PerceptionEngine
from ..types import ActionResult, Observation
from ..utils.logging import get_logger
from . import recipes as R

log = get_logger("handoff")

Event = Callable[[str, dict[str, Any]], None]


class Handoff:
    """Runs a recipe: launch the editor, import the render, report what happened."""

    def __init__(
        self,
        config: Config,
        perception: PerceptionEngine,
        actuator: Actuator,
        on_event: Event | None = None,
    ) -> None:
        self.cfg = config
        self.perception = perception
        self.actuator = actuator
        self.on_event = on_event
        self.recipes_dir = config.resolve_path(config.handoff.get("recipes_dir", "recipes"))

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def available(self) -> list[dict[str, Any]]:
        return R.list_recipes(self.recipes_dir)

    def run(
        self,
        app: str,
        project: str | Path,
        notes: str = "",
        overwrite: bool = False,
        launch_only: bool = False,
    ) -> ActionResult:
        recipe = R.get_recipe(app, self.recipes_dir)
        if recipe is None:
            keys = ", ".join(sorted(R.BY_KEY))
            return ActionResult(
                ok=False,
                message=f"no recipe named {app!r}. Known apps: {keys}. Write one in {self.recipes_dir}/.",
            )
        p = Path(project).expanduser().resolve()
        if not p.exists():
            return ActionResult(ok=False, message=f"nothing to import: {p} does not exist")
        if recipe.key == "explorer" or (app.lower() in ("explorer", "none") and not p.exists()):
            pass

        self._emit("handoff", {"app": recipe.name, "project": str(p), "notes": notes})
        tokens = {
            "project": str(p),
            "dir": str(p.parent),
            "filename": p.name,
            "stem": p.stem,
        }
        steps_log: list[dict[str, Any]] = []
        obs: Observation = self.perception.observe()
        failures: list[str] = []
        executed = 0

        for step in recipe.steps:
            action = self._substitute(dict(step.action), tokens)
            optional = step.optional
            result = self._perform(action, obs)
            obs = result["obs"]
            steps_log.append(
                {
                    "action": {k: v for k, v in action.items() if k != "plan"},
                    "ok": result["result"].ok,
                    "message": result["result"].message,
                    "optional": optional,
                }
            )
            if result["result"].ok:
                executed += 1
            else:
                failures.append(f"{action.get('type')}: {result['result'].message}")
                if not optional:
                    if not self._recoverable(action, result["result"]):
                        break
            if launch_only and action.get("type") == "launch_app":
                break

        ok = executed > 0 and len(failures) <= max(1, len(recipe.steps) // 3)
        summary = (
            f"{recipe.name}: ran {executed}/{len(recipe.steps)} steps"
            + (f"; problems: {'; '.join(failures[:3])}" if failures else "")
        )
        if notes:
            summary += f"\nnotes: {notes}"
        self._emit(
            "handoff_result",
            {"ok": ok, "summary": summary, "steps": steps_log, "recipe": recipe.to_dict()},
        )
        return ActionResult(
            ok=ok,
            message=summary,
            data={"recipe": recipe.key, "steps": steps_log, "project": str(p)},
        )

    def _recoverable(self, action: dict[str, Any], result: ActionResult) -> bool:
        """Keep going after the boring failures (window focus, unknown hotkey)."""
        msg = result.message.lower()
        return any(
            token in msg
            for token in ("not find", "cannot find", "no such", "focus", "active window")
        )

    def _substitute(self, action: dict[str, Any], tokens: dict[str, str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in action.items():
            if isinstance(v, str):
                for tk, tv in tokens.items():
                    v = v.replace("{" + tk + "}", tv)
            out[k] = v
        return out

    def _perform(self, action: dict[str, Any], obs: Observation) -> dict[str, Any]:
        t = action.get("type")
        if t == "focus_window":
            from ..control import win32

            w = win32.focus_window(str(action.get("title_contains", "")), maximize=bool(action.get("maximize")))
            time.sleep(0.6)
            obs = self.perception.observe(region=obs.screen)
            return {
                "obs": obs,
                "result": ActionResult(ok=w is not None, message=f"focused {w.title}" if w else "window not found"),
            }
        if t == "launch_app":
            from ..control import win32

            try:
                wait = float(action.get("wait", self.cfg.handoff.get("launch_wait", 7.0)))
                path, pid = win32.launch(str(action.get("app", "")), action.get("args"), wait=wait)
                obs = self.perception.observe(region=obs.screen)
                return {"obs": obs, "result": ActionResult(ok=True, message=f"launched {path}", data={"pid": pid})}
            except (FileNotFoundError, OSError) as exc:
                return {"obs": obs, "result": ActionResult(ok=False, message=str(exc))}
        if t == "assert":
            return {"obs": obs, "result": self._assert(action, obs)}

        result = self.actuator.execute(action, obs)
        time.sleep(self.actuator.settle())
        obs = self.perception.observe(region=obs.screen)
        return {"obs": obs, "result": result}

    def _assert(self, action: dict[str, Any], obs: Observation) -> ActionResult:
        checks: list[tuple[str, bool]] = []
        if action.get("text_present"):
            checks.append((f"text {action['text_present']!r} present", obs.has_text(str(action["text_present"]))))
        if action.get("text_absent"):
            checks.append((f"text {action['text_absent']!r} absent", not obs.has_text(str(action["text_absent"]))))
        if action.get("window_title_contains"):
            from ..control import win32

            w = win32.find_window(str(action["window_title_contains"]))
            checks.append((f"window {action['window_title_contains']!r} open", w is not None))
        if not checks:
            return ActionResult(ok=True, message="assert with no conditions - nothing checked")
        failed = [name for name, ok in checks if not ok]
        detail = "; ".join(f"{name}={ok}" for name, ok in checks)
        return ActionResult(ok=not failed, message=detail)


def default_app(config: Config) -> str:
    return str(config.handoff.get("default_app", "capcut"))
