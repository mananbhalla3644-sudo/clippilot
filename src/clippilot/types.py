from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .utils.geometry import Box


class ActionType(str, Enum):
    OBSERVE = "observe"
    CLICK = "click"
    MOVE = "move"
    DRAG = "drag"
    SCROLL = "scroll"
    TYPE = "type"
    KEY = "key"
    WAIT = "wait"
    FOCUS_WINDOW = "focus_window"
    LIST_WINDOWS = "list_windows"
    LAUNCH_APP = "launch_app"
    ASSERT = "assert"
    EDIT_VIDEO = "edit_video"
    HANDOFF = "handoff"
    PLAN_VIDEO = "plan_video"
    RUN_COMMAND = "run_command"
    ASK_HUMAN = "ask_human"
    TASK_DONE = "task_done"
    GIVE_UP = "give_up"


#: Actions that change application state and therefore deserve verification.
STATE_CHANGING = {
    ActionType.CLICK,
    ActionType.TYPE,
    ActionType.KEY,
    ActionType.DRAG,
    ActionType.LAUNCH_APP,
    ActionType.EDIT_VIDEO,
    ActionType.HANDOFF,
}


@dataclass
class Element:
    """A clickable thing the perception layer found on screen."""

    id: str
    kind: str  # text | word | icon | template | window | control
    box: Box
    text: str = ""
    confidence: float = 1.0
    source: str = ""  # ocr:tesseract | ocr:windows | template | vlm
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def center(self) -> tuple[int, int]:
        return self.box.center

    @property
    def label(self) -> str:
        return self.text or f"{self.kind}#{self.id}"

    def to_dict(self, with_box: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "kind": self.kind, "text": self.text[:120]}
        if self.kind in ("text", "word"):
            d["box"] = self.box.as_list()
            d["c"] = self.box.center
        elif with_box:
            d["box"] = self.box.as_list()
        d["conf"] = round(self.confidence, 2)
        return d


@dataclass
class Observation:
    """One look at the screen."""

    screen: Box
    image: Any = None  # PIL.Image (kept out of prompt text)
    elements: list[Element] = field(default_factory=list)
    window_title: str | None = None
    active_window: str | None = None
    cursor: tuple[int, int] | None = None
    ocr_backend: str = "none"
    scale: float = 1.0
    image_path: Path | None = None
    taken_at: float = field(default_factory=time.time)
    note: str = ""

    @property
    def width(self) -> int:
        return self.screen.width

    @property
    def height(self) -> int:
        return self.screen.height

    def describe(self, max_elements: int = 120) -> dict[str, Any]:
        els = self.elements[:max_elements]
        out: dict[str, Any] = {
            "screen": self.screen.as_list(),
            "window": self.active_window or self.window_title,
            "cursor": list(self.cursor) if self.cursor else None,
            "ocr": self.ocr_backend,
            "element_count": len(self.elements),
        }
        if self.elements and len(self.elements) > max_elements:
            out["truncated"] = len(self.elements) - max_elements
        if els:
            out["elements"] = [e.to_dict() for e in els]
        return out

    # ------------------------------------------------------------- targeting
    def find(
        self,
        text: str | None = None,
        element_id: str | None = None,
        kind: str | None = None,
        index: int = 0,
        exact: bool = False,
        min_confidence: float = 0.0,
        near: tuple[int, int] | None = None,
    ) -> Element | None:
        if element_id:
            for el in self.elements:
                if el.id == element_id:
                    return el
            return None

        cands = [e for e in self.elements if e.confidence >= min_confidence]
        if kind:
            cands = [e for e in cands if e.kind == kind or (kind == "text" and e.kind == "word")]
        if text:
            needle = text.strip().lower()
            exact_hits = [e for e in cands if e.text.strip().lower() == needle]
            loose = [e for e in cands if needle in e.text.strip().lower()]
            cands = exact_hits or loose if exact else (loose or exact_hits)
        if not cands:
            return None
        if near is not None:
            cands.sort(key=lambda e: (e.box.center[0] - near[0]) ** 2 + (e.box.center[1] - near[1]) ** 2)
        if not 0 <= index < len(cands):
            return None
        return cands[index]

    def has_text(self, needle: str) -> bool:
        n = needle.strip().lower()
        return any(n in e.text.lower() for e in self.elements)


@dataclass
class ActionResult:
    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    screenshot: Path | None = None
    diff: float = 0.0
    took: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "data": self.data,
            "screenshot": str(self.screenshot) if self.screenshot else None,
            "diff": round(self.diff, 4),
            "took": round(self.took, 3),
        }


@dataclass
class Step:
    index: int
    action: dict[str, Any]
    thought: str = ""
    result: ActionResult | None = None
    observation: Observation | None = None
    started: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "thought": self.thought,
            "result": self.result.to_dict() if self.result else None,
            "observation": self.observation.describe() if self.observation else None,
            "at": self.started,
        }


@dataclass
class RunResult:
    task: str
    steps: list[Step] = field(default_factory=list)
    success: bool = False
    summary: str = ""
    transcript_path: Path | None = None
    started: float = field(default_factory=time.time)
    finished: float | None = None
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return (self.finished or time.time()) - self.started

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "success": self.success,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
            "duration": round(self.duration, 2),
            "usage": self.usage,
            "transcript": str(self.transcript_path) if self.transcript_path else None,
        }


def summarize_for_log(obs: Observation, max_chars: int = 1400) -> str:
    """Compact, token-cheap textual rendering of an observation for the LLM."""
    d = obs.describe()
    lines = [
        f"screen={d['screen']} ({obs.width}x{obs.height}px)"
        + (f" window={d['window']!r}" if d.get("window") else ""),
    ]
    if d.get("cursor"):
        lines.append(f"cursor={tuple(d['cursor'])}")
    if d.get("elements"):
        for e in d["elements"][:40]:
            lines.append(
                f"  {e['id']:<5} {e['kind']:<8} {str(e.get('c') or e.get('box')):<22} conf={e.get('conf')} {e['text']!r}"
            )
        if d.get("truncated"):
            lines.append(f"  ... {d['truncated']} more elements (use observe(region=...) to zoom)")
    else:
        lines.append("  (no OCR elements - rely on the screenshot, or enable an OCR backend)")
    text = "\n".join(lines)
    return text[:max_chars]
