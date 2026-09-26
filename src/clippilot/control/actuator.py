from __future__ import annotations

import random
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..errors import ActionError, SafetyViolation
from ..observation import PerceptionEngine
from ..types import ActionResult, Observation
from ..utils.geometry import Box, clamp_point
from ..utils.imaging import diff_ratio
from ..utils.logging import get_logger
from .fallback import InputBackend, get_backend

log = get_logger("control.actuator")

Event = Callable[[str, dict[str, Any]], None]


class Failsafe:
    """Slam the pointer into the top-left corner to abort instantly."""

    def __init__(self, enabled: bool = True, size: int = 6) -> None:
        self.enabled = enabled
        self.size = size
        self.triggered = False

    def check(self, pos: tuple[int, int] | None) -> None:
        if not self.enabled or pos is None:
            return
        if pos[0] <= self.size and pos[1] <= self.size:
            self.triggered = True
            from ..errors import FailsafeTriggered

            raise FailsafeTriggered("pointer hit the abort corner - agent stopped")


class Clipboard:
    """Clipboard paste for long/unicode text (typing 500 chars char-by-char is silly)."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self._restore: str | None = None

    def get(self) -> str:
        try:
            import subprocess as sp

            out = sp.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"], capture_output=True, text=True, timeout=8)
            return out.stdout.strip("\r\n")
        except Exception:
            return ""

    def set(self, text: str) -> bool:
        if not self.allowed:
            return False
        try:
            import base64

            b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            import subprocess as sp

            sp.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Set-Clipboard -Value ([System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64}')))",
                ],
                capture_output=True,
                timeout=10,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("clipboard write failed: %s", exc)
            return False

    def paste(self, text: str) -> bool:
        if not self.set(text):
            return False
        backend = get_backend("auto")
        backend.hotkey(["ctrl", "v"])
        return True


class Actuator:
    """Executes one action against the real desktop."""

    def __init__(
        self,
        config: Config,
        perception: PerceptionEngine,
        backend: InputBackend | None = None,
        on_event: Event | None = None,
    ) -> None:
        self.cfg = config
        self.perception = perception
        c = config.control
        self.backend = backend or get_backend(c.get("backend", "auto"))
        self.humanize = bool(c.get("humanize", True))
        self.dry_run = bool(c.get("dry_run", False))
        self.move_range = tuple(c.get("move_duration", [0.28, 0.65]))
        self.type_range = tuple(c.get("type_delay", [0.028, 0.095]))
        self.settle_range = tuple(c.get("settle", [0.25, 0.6]))
        self.paste_threshold = int(c.get("clipboard_paste_threshold", 40))
        self.default_settle = int(c.get("default_settle_ms", 400)) / 1000.0
        self.clipboard = Clipboard(bool(config.safety.get("allow_clipboard_write", True)))
        self.failsafe = Failsafe(bool(c.get("failsafe", True)))
        self.on_event = on_event
        self.history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ utils
    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def settle(self) -> float:
        lo, hi = self.settle_range
        return random.uniform(float(lo), float(hi))

    def _move_ms(self, override: int | None = None) -> int:
        if override is not None:
            return int(max(0, override))
        lo, hi = self.move_range
        return int(random.uniform(float(lo), float(hi)) * 1000)

    def screen_bounds(self) -> Box:
        return self.perception.capture.virtual_screen

    def _resolve_point(self, action: dict[str, Any], obs: Observation) -> tuple[int, int]:
        if action.get("x") is not None and action.get("y") is not None:
            return (int(action["x"]), int(action["y"]))
        needle = action.get("text") or action.get("element")
        element = obs.find(
            text=needle,
            element_id=action.get("element_id") or action.get("id"),
            kind=action.get("kind"),
            index=int(action.get("index", 0) or 0),
        )
        if element is None and needle:
            # OCR often mangles labels; try a token-wise match before giving up.
            tokens = [t for t in str(needle).lower().split() if len(t) > 2]
            for tok in tokens:
                element = obs.find(text=tok, index=int(action.get("index", 0) or 0))
                if element is not None:
                    log.info("fuzzy target %r matched via token %r", needle, tok)
                    break
        if element is None:
            raise ActionError(
                f"cannot find a target for {needle or action.get('element_id')!r}. "
                f"Visible labels: {[e.text for e in obs.elements[:25]]}"
            )
        return element.center

    # --------------------------------------------------------------- dispatch
    def execute(self, action: dict[str, Any], obs: Observation | None = None) -> ActionResult:
        t0 = time.perf_counter()
        t = str(action.get("type", ""))
        before: Any = None
        if obs is not None and obs.image is not None and t in ("click", "key", "type", "drag"):
            before = obs.image

        self._emit("action", {"type": t, "action": {k: v for k, v in action.items() if k != "plan"}})

        try:
            result = self._dispatch(t, action, obs)
        except (ActionError, SafetyViolation) as exc:
            result = ActionResult(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("action %s failed", t)
            result = ActionResult(ok=False, message=f"{type(exc).__name__}: {exc}")

        result.took = time.perf_counter() - t0
        if before is not None and result.ok and t in ("click", "key", "type", "drag"):
            time.sleep(self.settle())
            try:
                after_obs = self.perception.observe(region=obs.screen, with_templates=False, with_ocr=False)
                result.diff = diff_ratio(before, after_obs.image)
                result.screenshot = after_obs.image_path
            except Exception as exc:  # noqa: BLE001
                log.debug("post-action diff failed: %s", exc)
        self.history.append({"at": time.time(), "type": t, "action": action, "ok": result.ok, "message": result.message})
        self._emit("result", result.to_dict())
        return result

    def _dispatch(self, t: str, action: dict[str, Any], obs: Observation | None) -> ActionResult:
        if t == "click":
            return self._click(action, obs)
        if t == "move":
            return self._move(action, obs)
        if t == "drag":
            return self._drag(action, obs)
        if t == "scroll":
            return self._scroll(action, obs)
        if t == "type":
            return self._type(action)
        if t == "key":
            return self._key(action)
        if t == "wait":
            return self._wait(action)
        if t == "run_command":
            return self._run_command(action)
        raise ActionError(f"the actuator cannot perform {t!r} (handled by the agent loop)")

    # ---------------------------------------------------------------- actions
    def _click(self, action: dict[str, Any], obs: Observation | None) -> ActionResult:
        if obs is None:
            obs = self.perception.observe()
        x, y = self._resolve_point(action, obs)
        x, y = clamp_point(x, y, self.screen_bounds())
        button = str(action.get("button", "left"))
        clicks = int(action.get("clicks", 1) or 1)
        target = action.get("text") or action.get("element_id") or f"({x},{y})"
        if self.dry_run:
            return ActionResult(ok=True, message=f"[dry-run] click {button}x{clicks} {target} at ({x},{y})", data={"x": x, "y": y})
        self._guard_pointer()
        self.backend.move(x, y, duration_ms=self._move_ms(action.get("duration_ms")))
        time.sleep(random.uniform(0.04, 0.11))
        self.backend.button(button, x, y, clicks=clicks)
        time.sleep(0.05)
        label = action.get("text") or ""
        return ActionResult(ok=True, message=f"clicked {button} {'x'.join([''] * clicks)[1:] or ''}{label or f'({x},{y})'}".strip(), data={"x": x, "y": y})

    def _move(self, action: dict[str, Any], obs: Observation | None) -> ActionResult:
        if obs is None:
            obs = self.perception.observe()
        x, y = self._resolve_point(action, obs)
        x, y = clamp_point(x, y, self.screen_bounds())
        if self.dry_run:
            return ActionResult(ok=True, message=f"[dry-run] move to ({x},{y})")
        self.backend.move(x, y, duration_ms=self._move_ms(action.get("duration_ms")))
        return ActionResult(ok=True, message=f"moved to ({x},{y})", data={"x": x, "y": y})

    def _drag(self, action: dict[str, Any], obs: Observation | None) -> ActionResult:
        if obs is None:
            obs = self.perception.observe()
        to = action.get("to") or {}
        to_pt = (int(to.get("x", 0)), int(to.get("y", 0)))
        if action.get("from") or action.get("text") or action.get("element_id"):
            from_pt = self._resolve_point(action, obs)
        elif action.get("x") is not None and action.get("y") is not None:
            from_pt = (int(action["x"]), int(action["y"]))
        else:
            raise ActionError("drag needs a 'from' (element or x,y) and a 'to'")
        x0, y0 = clamp_point(*from_pt, self.screen_bounds())
        x1, y1 = clamp_point(*to_pt, self.screen_bounds())
        if self.dry_run:
            return ActionResult(ok=True, message=f"[dry-run] drag ({x0},{y0}) -> ({x1},{y1})")
        self.backend.move(x0, y0, duration_ms=self._move_ms(250))
        time.sleep(0.08)
        self.backend.button("left", x0, y0, clicks=1)
        # insert a real button hold: press down again after the initial click pair
        time.sleep(0.05)
        duration = int(action.get("duration_ms", 900))
        steps = max(12, min(80, duration // 12))
        for i in range(1, steps + 1):
            t = i / steps
            e = 3 * t * t - 2 * t * t * t
            self.backend.move(
                int(x0 + (x1 - x0) * e),
                int(y0 + (y1 - y0) * e),
                duration_ms=0,
            )
            time.sleep(duration / 1000.0 / steps)
        time.sleep(0.12)
        return ActionResult(ok=True, message=f"dragged ({x0},{y0}) -> ({x1},{y1})", data={"from": [x0, y0], "to": [x1, y1]})

    def _scroll(self, action: dict[str, Any], obs: Observation | None) -> ActionResult:
        amount = int(action.get("amount", 6) or 6)
        if action.get("x") is not None and action.get("y") is not None and not self.dry_run:
            self.backend.move(int(action["x"]), int(action["y"]), duration_ms=180)
            time.sleep(0.06)
        if self.dry_run:
            return ActionResult(ok=True, message=f"[dry-run] scroll {amount}")
        self.backend.scroll(amount, horizontal=str(action.get("horizontal", False)))
        time.sleep(0.18)
        return ActionResult(ok=True, message=f"scrolled {amount}")

    def _type(self, action: dict[str, Any]) -> ActionResult:
        text = str(action.get("text", ""))
        if action.get("clear_first"):
            self.backend.hotkey(["ctrl", "a"])
            time.sleep(0.05)
            self.backend.key("delete")
            time.sleep(0.08)
        if self.dry_run:
            preview = text if len(text) <= 80 else text[:77] + "..."
            return ActionResult(ok=True, message=f"[dry-run] type {preview!r}", data={"chars": len(text)})
        if len(text) >= self.paste_threshold and self.clipboard.paste(text):
            time.sleep(0.25)
            return ActionResult(ok=True, message=f"pasted {len(text)} chars via clipboard")
        lo, hi = self.type_range
        if self.humanize:
            for ch in text:
                self.backend.type_text(ch)
                delay = random.uniform(float(lo), float(hi))
                if ch in " .,!?;:-\n":
                    delay += random.uniform(0.02, 0.09)
                time.sleep(delay)
        else:
            self.backend.type_text(text)
        return ActionResult(ok=True, message=f"typed {len(text)} chars", data={"chars": len(text)})

    def _key(self, action: dict[str, Any]) -> ActionResult:
        raw = str(action.get("keys", "")).strip()
        if not raw:
            raise ActionError("no keys given")
        combos = [c.strip() for c in raw.split("|") if c.strip()]
        if self.dry_run:
            return ActionResult(ok=True, message=f"[dry-run] key {' then '.join(combos)}")
        for combo in combos:
            keys = [k for k in combo.replace("-", "+").split("+") if k]
            if len(keys) == 1:
                self.backend.key(keys[0])
            else:
                self.backend.hotkey(keys)
            time.sleep(random.uniform(0.06, 0.16))
        time.sleep(0.12)
        return ActionResult(ok=True, message=f"pressed {' then '.join(combos)}")

    def _wait(self, action: dict[str, Any]) -> ActionResult:
        seconds = min(20.0, max(0.0, float(action.get("seconds", 1.0))))
        if not self.dry_run:
            time.sleep(seconds)
        return ActionResult(ok=True, message=f"waited {seconds:.1f}s", data={"seconds": seconds})

    def _run_command(self, action: dict[str, Any]) -> ActionResult:
        cmd = str(action.get("command", "")).strip()
        if not cmd:
            raise ActionError("no command given")
        timeout = float(action.get("timeout", 60))
        if self.dry_run:
            return ActionResult(ok=True, message=f"[dry-run] would run: {cmd}")
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=action.get("cwd") or None,
        )
        out = (proc.stdout or "")[-4000:]
        err = (proc.stderr or "")[-2000:]
        return ActionResult(
            ok=proc.returncode == 0,
            message=f"exit {proc.returncode}\n{out.strip()}\n{err.strip()}".strip(),
            data={"returncode": proc.returncode, "stdout": out, "stderr": err},
        )

    # ------------------------------------------------------------------ safety
    def _guard_pointer(self) -> None:
        self.failsafe.check(self.perception.capture.cursor_position())

    def abort(self) -> None:
        """Stop everything and (optionally) tell the OS we're done."""
        self.failsafe.enabled = True
        log.warning("actuator aborted by operator")
        self._emit("abort", {})

    def undo(self, times: int = 1) -> None:
        for _ in range(times):
            self.backend.hotkey(["ctrl", "z"])
            time.sleep(0.2)
