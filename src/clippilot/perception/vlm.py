from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from PIL import Image

from ..config import Config
from ..errors import LLMError
from ..utils.imaging import data_uri
from ..utils.logging import get_logger

log = get_logger("perception.vlm")

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.S)
_OBJECT = re.compile(r"(\{.*\}|\[.*\])", re.S)


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model response."""
    if not text:
        raise LLMError("empty response while parsing JSON")
    text = text.strip()
    if text[0] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            text = m.group(1)
    m = _OBJECT.search(text)
    if m:
        candidate = m.group(1)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # last resort: trim trailing commas
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError as exc:
                raise LLMError(f"could not parse JSON from response: {exc}") from exc
    raise LLMError(f"no JSON found in response: {text[:200]!r}")


@dataclass
class VlmVerdict:
    passed: bool
    reason: str
    confidence: float = 0.0


class VisionModel:
    """Image-grounded questions: 'where is the Export button?', 'did it work?'"""

    def __init__(self, llm_client: Any, config: Config) -> None:
        self.llm = llm_client
        self.cfg = config
        self.max_side = int(config.vlm.get("max_side", 1450))
        self.quality = int(config.vlm.get("jpeg_quality", 80))

    def _ask(
        self,
        image: Image.Image,
        prompt: str,
        system: str,
        max_tokens: int = 700,
        temperature: float = 0.0,
    ) -> str:
        uri = data_uri(image, quality=self.quality, max_side=self.max_side)
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": uri, "detail": "high"}},
                ],
            },
        ]
        return self.llm.complete(
            messages,
            model=self.cfg.vision_model,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=False,
        )

    # ------------------------------------------------------------- grounding
    def locate(
        self,
        image: Image.Image,
        description: str,
        origin: tuple[int, int] = (0, 0),
        already_has_candidates: bool = False,
    ) -> tuple[int, int] | None:
        hint = (
            "OCR already proposed some candidates. Prefer clicking one of them if it matches; "
            "only invent a new point when none match."
            if already_has_candidates
            else "OCR found nothing usable, so you must estimate the point yourself."
        )
        system = (
            "You are a precise GUI grounding model. You look at one screenshot and reply with a single "
            "JSON object and nothing else: "
            '{"point": [x, y], "confidence": 0.0-1.0, "label": "what you clicked"}. '
            "Coordinates are in the SCREENSHOT's pixel space (origin top-left, units = image pixels)."
        )
        prompt = (
            f"Task: find the on-screen element that matches this description.\n"
            f"Description: {description}\n{hint}\n"
            f"Image size: {image.width}x{image.height}px.\n"
            "Reply with JSON only."
        )
        try:
            raw = self._ask(image, prompt, system, max_tokens=200)
            data = extract_json(raw)
            x, y = int(data["point"][0]), int(data["point"][1])
            x = min(max(x, 0), image.width - 1)
            y = min(max(y, 0), image.height - 1)
            conf = float(data.get("confidence", 0.5))
            log.info("vlm located %r at (%d,%d) conf=%.2f", description, x, y, conf)
            return (x + origin[0], y + origin[1])
        except Exception as exc:  # noqa: BLE001
            log.warning("vlm locate failed: %s", exc)
            return None

    # --------------------------------------------------------- verification
    def verify(self, image: Image.Image, statement: str) -> VlmVerdict:
        system = (
            "You verify whether a described change actually happened on a screenshot. "
            'Reply with JSON only: {"ok": true|false, "reason": "one short sentence", "confidence": 0.0-1.0}. '
            "Be strict: if you cannot clearly see the evidence, answer false."
        )
        prompt = (
            f"Statement to check: {statement}\n"
            "Look at the screenshot and decide whether it is now true. Reply with JSON only."
        )
        try:
            raw = self._ask(image, prompt, system, max_tokens=250)
            data = extract_json(raw)
            return VlmVerdict(
                passed=bool(data.get("ok", False)),
                reason=str(data.get("reason", ""))[:300],
                confidence=float(data.get("confidence", 0.0)),
            )
        except Exception as exc:  # noqa: BLE001
            return VlmVerdict(False, f"verification error: {exc}")

    # ------------------------------------------------------------ describing
    def describe(self, image: Image.Image, hint: str = "") -> str:
        system = (
            "You describe a Windows application screenshot for another AI agent. "
            "Be concrete and terse: name the app if visible, list the visible controls and dialogs "
            "with their approximate location (top-left, center, right sidebar...). "
            "If a modal dialog or error is open, say so first. No preamble, no JSON."
        )
        prompt = f"Describe this screen. {hint}".strip()
        try:
            return self._ask(image, prompt, system, max_tokens=420).strip()
        except Exception as exc:  # noqa: BLE001
            return f"<vision description unavailable: {exc}>"
