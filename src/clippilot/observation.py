from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .errors import PerceptionError
from .perception.elements import dedupe, relabel, template_elements, words_to_elements
from .perception.ocr import OcrEngine
from .perception.screen import Capture, ScreenCapture
from .perception.templates import TemplateMatcher
from .perception.vlm import VisionModel
from .types import Element, Observation
from .utils.geometry import Box
from .utils.imaging import encode_png
from .utils.logging import get_logger

log = get_logger("observation")

Event = Callable[[str, dict[str, Any]], None]


class PerceptionEngine:
    """Turns pixels into something an LLM can act on."""

    def __init__(self, config: Config, vision: VisionModel | None = None) -> None:
        self.cfg = config
        p = config.perception
        self.capture = ScreenCapture(mode=p.get("capture", "virtual"))
        self.ocr = OcrEngine(
            backend=p.get("ocr_backend", "auto"),
            min_confidence=float(p.get("min_confidence", 0.35)),
        )
        self.templates = TemplateMatcher(config.resolve_path(p.get("templates_dir", "assets/templates")))
        self.vision = vision
        self.last: Observation | None = None
        self.last_capture: Capture | None = None
        self._shot_dir: Path | None = None
        self._event: Event | None = None

    # ------------------------------------------------------------------ wiring
    def set_event_hook(self, hook: Event | None) -> None:
        self._event = hook

    def set_shot_dir(self, path: str | Path | None) -> None:
        self._shot_dir = Path(path) if path else None

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._event:
            try:
                self._event(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def monitors(self) -> list[tuple[str, Box]]:
        return self.capture.monitors()

    # ---------------------------------------------------------------- observe
    def observe(
        self,
        region: Box | None = None,
        which: str | None = None,
        with_templates: bool = True,
        with_ocr: bool = True,
        save: bool = False,
        describe: bool = False,
    ) -> Observation:
        t0 = time.perf_counter()
        cap = self.capture.grab(region=region, which=which)
        image = cap.image
        self.last_capture = cap

        els: list[Element] = []
        if with_ocr:
            try:
                words = self.ocr.read(image, origin=(cap.box.x1, cap.box.y1))
                els = words_to_elements(
                    words,
                    keep_words=bool(self.cfg.perception.get("keep_words", False)),
                    min_confidence=float(self.cfg.perception.get("min_confidence", 0.35)),
                    merge_gap=int(self.cfg.perception.get("merge_words_gap", 34)),
                )
            except PerceptionError as exc:
                log.warning("OCR skipped: %s", exc)

        if with_templates:
            try:
                hits = self.templates.find(image, origin=(cap.box.x1, cap.box.y1))
                els.extend(template_elements(hits, els))
            except Exception as exc:  # noqa: BLE001
                log.debug("template matching failed: %s", exc)

        els = relabel(dedupe(els))

        title, active = self._window_titles()
        obs = Observation(
            screen=cap.box,
            image=image,
            elements=els,
            window_title=title,
            active_window=active,
            cursor=self.capture.cursor_position(),
            ocr_backend=self.ocr.name,
            scale=1.0,
        )
        if save and self._shot_dir:
            name = f"obs_{int(time.time() * 1000)}.png"
            path = self._shot_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encode_png(image))
            obs.image_path = path

        if describe and self.vision and not els:
            obs.note = self.vision.describe(image, hint="The OCR pass found nothing clickable.")

        obs.taken_at = time.time()
        self.last = obs
        log.info(
            "observed %s %dx%d, %d elements (ocr=%s) in %dms",
            cap.box,
            image.width,
            image.height,
            len(els),
            obs.ocr_backend,
            (time.perf_counter() - t0) * 1000,
        )
        return obs

    def observe_after(self, previous: Observation, settle: float = 0.35) -> Observation:
        time.sleep(max(0.0, settle))
        return self.observe(region=previous.screen)

    def _window_titles(self) -> tuple[str | None, str | None]:
        try:
            from .control.win32 import active_window_title, top_window_titles

            titles = top_window_titles(6)
            return (titles[0] if titles else None), active_window_title()
        except Exception:
            return None, None

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        self.capture.close()
