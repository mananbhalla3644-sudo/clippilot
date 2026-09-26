from __future__ import annotations

import re
from typing import Sequence

from ..types import Element
from ..utils.geometry import Box
from ..utils.logging import get_logger
from .ocr import Word
from .templates import TemplateHit

log = get_logger("perception.elements")

_NOISE = re.compile(r"^[\W_]+$", re.UNICODE)

#: Words that are chrome, not controls. Kept short on purpose: a false positive
#: here means a wasted click, a false negative is recoverable.
CHROME_WORDS = {
    "ok", "cancel", "yes", "no", "x", "?", "...",
}


def _clean(text: str) -> str:
    text = text.replace("|", "I").replace("ﬁ", "fi").replace("ﬂ", "fl")
    return re.sub(r"\s+", " ", text).strip(" \t\r\n|:.,;")


def _is_noise(text: str) -> bool:
    if not text or len(text) < 1:
        return True
    if _NOISE.match(text):
        return True
    digits = sum(ch.isdigit() for ch in text)
    if digits and digits == len(text):
        return True
    return False


def group_lines(words: Sequence[Word], gap: int = 34) -> list[list[Word]]:
    """Cluster OCR words into visual lines (y-overlap + horizontal proximity)."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.box.y1, w.box.x1))
    lines: list[list[Word]] = [[ordered[0]]]
    for w in ordered[1:]:
        cur = lines[-1]
        ref = cur[-1]
        same_row = (
            w.box.y1 < ref.box.y2 - 2
            and w.box.y2 > ref.box.y1 + 2
            and abs((w.box.y1 + w.box.y2) / 2 - (ref.box.y1 + ref.box.y2) / 2) <= max(8, ref.box.height * 0.6)
        )
        near_x = w.box.x1 - ref.box.x2 <= gap
        if same_row and near_x:
            cur.append(w)
        else:
            lines.append([w])
    return [ln for ln in lines if ln]


def _line_text(line: Sequence[Word]) -> str:
    parts: list[str] = []
    prev: Word | None = None
    for w in sorted(line, key=lambda w: w.box.x1):
        if prev is not None:
            spacer = w.box.x1 - prev.box.x2
            ratio = spacer / max(1.0, max(prev.box.height, w.box.height))
            if spacer > 6 and ratio > 0.45:
                parts.append(" ")
            elif w.text[:1].isalnum() and prev.text[-1:].isalnum() and ratio < 0.18:
                parts.append("")
            else:
                parts.append(" ")
        parts.append(w.text)
        prev = w
    return _clean("".join(parts))


def words_to_elements(
    words: Sequence[Word],
    keep_words: bool = False,
    min_confidence: float = 0.35,
    merge_gap: int = 34,
) -> list[Element]:
    words = [w for w in words if not _is_noise(_clean(w.text)) and w.confidence >= min_confidence]
    lines = group_lines(words, gap=merge_gap)
    lines.sort(key=lambda ln: (min(w.box.y1 for w in ln), min(w.box.x1 for w in ln)))

    elements: list[Element] = []
    n = 0
    for line in lines:
        n += 1
        text = _line_text(line)
        if _is_noise(text):
            continue
        box = Box.union_all(w.box for w in line)
        conf = sum(w.confidence for w in line) / max(1, len(line))
        elements.append(
            Element(
                id=f"t{n}",
                kind="text",
                box=box,
                text=text,
                confidence=round(conf, 3),
                source="ocr",
                meta={"words": len(line)},
            )
        )

    if keep_words:
        for i, w in enumerate(words, start=1):
            if _is_noise(_clean(w.text)):
                continue
            elements.append(
                Element(
                    id=f"w{i}",
                    kind="word",
                    box=w.box,
                    text=_clean(w.text),
                    confidence=round(w.confidence, 3),
                    source="ocr",
                )
            )

    return elements


def template_elements(
    hits: Sequence[TemplateHit],
    existing: Sequence[Element],
    iou_threshold: float = 0.25,
) -> list[Element]:
    out: list[Element] = []
    for h in hits:
        if any(h.box.iou(e.box) > iou_threshold for e in existing) or any(
            h.box.iou(e.box) > iou_threshold for e in out
        ):
            continue
        out.append(
            Element(
                id=f"i{len(out) + 1}",
                kind="icon",
                box=h.box,
                text=h.name.replace("_", " "),
                confidence=round(h.score, 3),
                source="template",
            )
        )
    return out


def dedupe(elements: Sequence[Element], iou_threshold: float = 0.6) -> list[Element]:
    """Drop near-duplicate labels, keeping the highest confidence / largest box."""
    ranked = sorted(elements, key=lambda e: (-e.confidence, -e.box.area))
    kept: list[Element] = []
    for e in ranked:
        dup = None
        for k in kept:
            if k.text.strip().lower() == e.text.strip().lower() and k.box.iou(e.box) > 0.35:
                dup = k
                break
        if dup is None:
            kept.append(e)
        elif e.box.area > dup.box.area:
            dup.box = e.box
    return sorted(kept, key=lambda e: (e.box.y1, e.box.x1))


def relabel(elements: Sequence[Element]) -> list[Element]:
    """Stable, short ids: e1..eN in reading order (what the LLM actually cites)."""
    ordered = sorted(elements, key=lambda e: (e.box.y1, e.box.x1))
    for i, e in enumerate(ordered, start=1):
        e.id = f"e{i}"
    return ordered


def crop_priority(elements: Sequence[Element], limit: int = 40) -> list[Element]:
    """Prefer big, confident, interactive-looking elements when space is tight."""
    def score(e: Element) -> float:
        area_term = min(1.0, (e.box.area / 25000.0) ** 0.35)
        text_term = 1.0 if e.kind in ("text", "word") and len(e.text) > 1 else 0.7
        return area_term * text_term * (0.5 + 0.5 * e.confidence)

    return sorted(elements, key=score, reverse=True)[:limit]
