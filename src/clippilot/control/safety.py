from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import SafetyViolation
from ..types import Observation
from ..utils.geometry import Box
from ..utils.logging import get_logger

log = get_logger("control.safety")

#: Keys that must never be synthesised by the agent.
HARD_DENY_KEYS = {"ctrl+alt+delete", "ctrl+alt+del", "win+l", "cmd+l"}

#: Field hints that mean "credentials" - we refuse to type into them.
SECRET_HINTS = re.compile(
    r"(password|passcode|pin\b|2fa|otp|security code|cvv|credit ?card|card number|ssn|social security|seed phrase|recovery phrase|private key)",
    re.I,
)

DESTRUCTIVE_TEXT = re.compile(
    r"\b(delete|deleting|remove|discard|erase|wipe|format|reset|overwrite|uninstall|empty trash|permanently)\b",
    re.I,
)

SENSITIVE_APPS = re.compile(
    r"(bank|paypal|wallet|bitcoin|crypto|password manager|keepass|1password|lastpass|bitwarden|login|signin|sign-in)",
    re.I,
)


@dataclass
class RateLimiter:
    """Keeps the agent from machine-gunning a UI (and from looking like one)."""

    clicks_per_minute: int = 240
    keys_per_minute: int = 600
    _clicks: list[float] = field(default_factory=list)
    _keys: list[float] = field(default_factory=list)

    def _trim(self, bucket: list[float]) -> None:
        cutoff = time.time() - 60.0
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)

    def click(self) -> None:
        self._trim(self._clicks)
        if len(self._clicks) >= self.clicks_per_minute:
            time.sleep(1.0)
            self._trim(self._clicks)
        self._clicks.append(time.time())

    def key(self) -> None:
        self._trim(self._keys)
        if len(self._keys) >= self.keys_per_minute:
            time.sleep(0.5)
            self._trim(self._keys)
        self._keys.append(time.time())


@dataclass
class CheckResult:
    ok: bool
    reason: str = ""
    severity: str = "info"  # info | warn | block | confirm


class SafetyPolicy:
    """Everything the agent is not allowed to do without a human."""

    def __init__(self, config: Config) -> None:
        s = config.safety
        self.require_confirmation = bool(s.get("require_confirmation", True))
        self.confirm_on = set(s.get("confirm_on", []))
        self.allow_run_command = bool(s.get("allow_run_command", False))
        self.allow_clipboard_write = bool(s.get("allow_clipboard_write", True))
        self.blocked_rects = [Box(*b) for b in (s.get("blocked_rects") or [])]
        self.refuse_password_fields = bool(s.get("refuse_password_fields", True))
        self.denylist_keys = {str(k).lower().replace(" ", "") for k in (s.get("denylist_keys") or [])}
        self.confirm_texts = [str(t).lower() for t in (s.get("confirm_texts") or [])]
        self.limiter = RateLimiter(
            clicks_per_minute=int(s.get("max_clicks_per_minute", 240)),
            keys_per_minute=int(s.get("max_keys_per_minute", 600)),
        )

    # ------------------------------------------------------------------ checks
    def check(self, action: dict[str, Any], obs: Observation | None = None) -> CheckResult:
        t = str(action.get("type", "")).lower()

        if t == "key":
            combo = str(action.get("keys", "")).lower().replace(" ", "")
            if combo in HARD_DENY_KEYS or combo in self.denylist_keys:
                return CheckResult(False, f"key {combo!r} is on the hard deny list", "block")

        if t == "run_command" and not self.allow_run_command:
            return CheckResult(False, "running shell commands is disabled (safety.allow_run_command=false)", "block")

        if t == "type":
            text = str(action.get("text", ""))
            if SECRET_HINTS.search(text):
                return CheckResult(False, "refusing to type credential-looking text", "block")
            if obs is not None and self.refuse_password_fields and self._near_secret_field(action, obs):
                return CheckResult(False, "the focused field looks like a password field; type it yourself", "block")

        if t == "click" and obs is not None:
            pt = self._point(action, obs)
            if pt is not None:
                for rect in self.blocked_rects:
                    if rect.contains(*pt):
                        return CheckResult(False, f"that region is off limits (blocked_rects: {rect})", "block")
                if obs.screen.contains(*pt) is False:
                    return CheckResult(False, f"point {pt} is outside the captured screen area {obs.screen}", "block")

        if t == "launch_app":
            app = str(action.get("app", ""))
            if SENSITIVE_APPS.search(app):
                return CheckResult(False, f"refusing to automate {app!r}", "block")

        return CheckResult(True)

    def needs_confirmation(self, action: dict[str, Any]) -> tuple[bool, str]:
        if not self.require_confirmation:
            return False, ""
        t = str(action.get("type", "")).lower()

        if t == "run_command":
            return True, f"run shell command: {action.get('command')}"
        if t in ("type",) and len(str(action.get("text", ""))) > 240:
            return True, "type a long block of text"
        if t == "key":
            combo = str(action.get("keys", "")).lower().replace(" ", "")
            if "alt+f4" in combo or "ctrl+w" in combo or "ctrl+q" in combo:
                return True, f"close a window/quit with {combo}"
            if combo in ("ctrl+shift+delete",):
                return True, "clear browser data"
        if t == "click":
            label = str(action.get("text") or action.get("element") or action.get("target_text") or "")
            hay = label.lower()
            if any(tok in hay for tok in self.confirm_texts) or DESTRUCTIVE_TEXT.search(hay):
                return True, f"click something destructive: {label!r}"
        if t == "edit_video":
            out = str(action.get("output", ""))
            if out and Path(out).exists():
                return True, f"overwrite existing file {out}"
        if t == "handoff" and action.get("overwrite"):
            return True, "overwrite the project in the editor"
        if t in self.confirm_on:
            return True, f"action type {t!r} is marked confirm-first"
        return False, ""

    # ------------------------------------------------------------------ helpers
    def _point(self, action: dict[str, Any], obs: Observation) -> tuple[int, int] | None:
        if action.get("x") is not None and action.get("y") is not None:
            return (int(action["x"]), int(action["y"]))
        el = obs.find(
            text=action.get("text") or action.get("element"),
            element_id=action.get("element_id") or action.get("id"),
            kind=action.get("kind"),
            index=int(action.get("index", 0) or 0),
        )
        return el.center if el else None

    def _near_secret_field(self, action: dict[str, Any], obs: Observation) -> bool:
        for el in obs.elements:
            if SECRET_HINTS.search(el.text or ""):
                return True
        return False

    def enforce(self, action: dict[str, Any], obs: Observation | None = None) -> None:
        result = self.check(action, obs)
        if not result.ok:
            raise SafetyViolation(result.reason)


@dataclass
class AuditEntry:
    at: float
    action: dict[str, Any]
    approved: bool
    reason: str = ""


class AuditLog:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: list[AuditEntry] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, action: dict[str, Any], approved: bool, reason: str = "") -> None:
        e = AuditEntry(time.time(), action, approved, reason)
        self.entries.append(e)
        if not self.path:
            return
        import json

        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": e.at, "action": e.action, "approved": e.approved, "reason": e.reason}) + "\n")
