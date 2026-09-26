from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ..utils.geometry import Box, nms
from ..utils.logging import get_logger

log = get_logger("perception.templates")


@dataclass
class TemplateHit:
    name: str
    box: Box
    score: float


def _to_gray(img: Image.Image, scale: float = 1.0) -> np.ndarray:
    arr = np.asarray(img.convert("L"), dtype=np.float32)
    if scale != 1.0:
        arr = cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    return arr


class TemplateMatcher:
    """Finds known icons/buttons in the screenshot when OCR can't see them."""

    def __init__(self, directory: str | Path, threshold: float = 0.82) -> None:
        self.dir = Path(directory)
        self.threshold = threshold
        self.templates: list[tuple[str, np.ndarray, float]] = []
        self._mtimes: dict[str, float] = {}
        self.load()

    def load(self) -> int:
        if not self.dir.exists():
            return 0
        for png in sorted(self.dir.rglob("*.png")):
            mtime = png.stat().st_mtime
            if self._mtimes.get(str(png)) == mtime:
                continue
            self._mtimes[str(png)] = mtime
            arr = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
            if arr is None:
                log.warning("cannot read template %s", png)
                continue
            name = png.stem
            self.templates = [t for t in self.templates if t[0] != name]
            self.templates.append((name, arr, 0.0))
        return len(self.templates)

    def find(
        self,
        image: Image.Image,
        region: Box | None = None,
        origin: tuple[int, int] = (0, 0),
        threshold: float | None = None,
    ) -> list[TemplateHit]:
        if not self.templates:
            self.load()
        thr = threshold if threshold is not None else self.threshold
        hits: list[TemplateHit] = []
        hay = _to_gray(image)
        hy, hw = hay.shape[:2]
        for name, tpl, _ in self.templates:
            th, tw = tpl.shape[:2]
            if th > hy or tw > hw or tw < 4 or th < 4:
                continue
            res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
            _, maxv, _, maxl = cv2.minMaxLoc(res)
            if maxv >= thr:
                x, y = maxl
                hits.append(
                    TemplateHit(
                        name=name,
                        box=Box.from_xywh(x + origin[0], y + origin[1], tw, th),
                        score=float(maxv),
                    )
                )
        return hits

    def best(self, image: Image.Image, name: str) -> TemplateHit | None:
        hits = [h for h in self.find(image, threshold=0.5) if h.name == name]
        return max(hits, key=lambda h: h.score) if hits else None


def perceptual_hash(image: Image.Image) -> str:
    """Tiny change-detector for screenshots (cheap 'did the UI react?' signal)."""
    small = image.convert("L").resize((16, 16), Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float32)
    mean = arr.mean()
    bits = (arr > mean).flatten()
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return f"{value:064x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
