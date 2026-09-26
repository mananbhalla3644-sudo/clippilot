from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .types import ActionResult, Observation
from .utils.logging import get_logger

log = get_logger("recorder")


@dataclass
class Episode:
    task: str = ""
    started: float = 0.0
    entries: list[dict[str, Any]] = None  # type: ignore[assignment]
    run_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.entries is None:
            self.entries = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "started": self.started,
            "entries": self.entries,
            "run_dir": str(self.run_dir) if self.run_dir else None,
        }


class Recorder:
    """Append-only action log: replayable, diffable, greppable."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        step: int,
        action: dict[str, Any],
        result: ActionResult,
        obs: Observation | None = None,
    ) -> None:
        entry = {
            "step": step,
            "at": time.time(),
            "action": {k: v for k, v in action.items() if k != "plan"},
            "plan": action.get("plan"),
            "ok": result.ok,
            "message": result.message,
            "diff": round(result.diff, 4),
            "elements": [e.to_dict() for e in (obs.elements[:30] if obs else [])],
        }
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.debug("recorded step %d (%s)", step, action.get("type"))

    def read(self) -> list[dict[str, Any]]:
        if not self.path or not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def replay(
        self,
        execute: Any,
        only: str | None = None,
        skip_failed: bool = True,
        dry_run: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """
        Re-run a recorded episode through `execute(action)`.

        `only` filters by action type. `dry_run` is passed through to the caller.
        """
        for entry in self.read():
            t = str(entry.get("action", {}).get("type", ""))
            if only and t != only:
                continue
            if skip_failed and not entry.get("ok", True):
                log.info("skipping failed step %s (%s)", entry.get("step"), t)
                continue
            action = entry.get("action", {})
            log.info("replay step %s: %s", entry.get("step"), t)
            result = execute(action)
            yield {
                "step": entry.get("step"),
                "action": t,
                "previous": entry.get("message", ""),
                "replay_ok": bool(getattr(result, "ok", True)),
                "replay_message": getattr(result, "message", ""),
            }


def to_markdown(episode: Episode) -> str:
    lines = [f"# Episode: {episode.task}", ""]
    for e in episode.entries:
        lines.append(f"- **step {e.get('step')}** `{e.get('action', {}).get('type')}` -> {e.get('message', '')}")
    return "\n".join(lines) + "\n"
