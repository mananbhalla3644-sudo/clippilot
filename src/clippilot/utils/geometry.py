from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Box:
    """Axis-aligned rectangle in virtual-screen (physical) pixels."""

    x1: int
    y1: int
    x2: int
    y2: int

    # ---------------------------------------------------------------- build
    @classmethod
    def from_xywh(cls, x: float, y: float, w: float, h: float) -> "Box":
        return cls(int(round(x)), int(round(y)), int(round(x + w)), int(round(y + h)))

    @classmethod
    def from_points(cls, pts: Sequence[Sequence[float]]) -> "Box":
        xs = [float(p[0]) for p in pts]
        ys = [float(p[1]) for p in pts]
        return cls(min(xs), min(ys), max(xs), max(ys))

    # ------------------------------------------------------------- geometry
    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[int, int]:
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)

    def as_list(self) -> list[int]:
        return [self.x1, self.y1, self.x2, self.y2]

    def as_xywh(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.width, self.height)

    def contains(self, x: int, y: int) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2

    def contains_box(self, other: "Box", tol: int = 0) -> bool:
        return (
            self.x1 - tol <= other.x1
            and self.y1 - tol <= other.y1
            and self.x2 + tol >= other.x2
            and self.y2 + tol >= other.y2
        )

    def intersect(self, other: "Box") -> "Box | None":
        x1, y1 = max(self.x1, other.x1), max(self.y1, other.y1)
        x2, y2 = min(self.x2, other.x2), min(self.y2, other.y2)
        if x2 <= x1 or y2 <= y1:
            return None
        return Box(x1, y1, x2, y2)

    def union(self, other: "Box") -> "Box":
        return Box(
            min(self.x1, other.x1),
            min(self.y1, other.y1),
            max(self.x2, other.x2),
            max(self.y2, other.y2),
        )

    def union_all(self, boxes: Iterable["Box"]) -> "Box":
        out: Box | None = None
        for b in boxes:
            out = b if out is None else out.union(b)
        return out or Box(0, 0, 0, 0)

    def iou(self, other: "Box") -> float:
        inter = self.intersect(other)
        if inter is None:
            return 0.0
        denom = self.area + other.area - inter.area
        return inter.area / denom if denom else 0.0

    # ------------------------------------------------------------ transform
    def scale(self, sx: float, sy: float | None = None) -> "Box":
        sy = sx if sy is None else sy
        return Box(
            int(round(self.x1 * sx)),
            int(round(self.y1 * sy)),
            int(round(self.x2 * sx)),
            int(round(self.y2 * sy)),
        )

    def translate(self, dx: int, dy: int) -> "Box":
        return Box(self.x1 + dx, self.y1 + dy, self.x2 + dx, self.y2 + dy)

    def clamp(self, bounds: "Box") -> "Box":
        return Box(
            min(max(self.x1, bounds.x1), bounds.x2),
            min(max(self.y1, bounds.y1), bounds.y2),
            min(max(self.x2, bounds.x1), bounds.x2),
            min(max(self.y2, bounds.y1), bounds.y2),
        )

    def inset(self, dx: int, dy: int | None = None) -> "Box":
        dy = dx if dy is None else dy
        return Box(self.x1 + dx, self.y1 + dy, max(self.x1 + dx, self.x2 - dx), max(self.y1 + dy, self.y2 - dy))

    def expand(self, px: int, py: int | None = None) -> "Box":
        py = px if py is None else py
        return Box(self.x1 - px, self.y1 - py, self.x2 + px, self.y2 + py)

    def padded(self, px: int, py: int | None = None) -> "Box":
        """Shrink by a percentage-ish padding (safer click targets)."""
        py = px if py is None else py
        return Box(
            self.x1 + px,
            self.y1 + py,
            max(self.x1 + px + 1, self.x2 - px),
            max(self.y1 + py + 1, self.y2 - py),
        )

    # --------------------------------------------------------------- output
    def to_dict(self) -> dict:
        return {
            "box": self.as_list(),
            "center": list(self.center),
            "w": self.width,
            "h": self.height,
        }

    def __str__(self) -> str:
        return f"({self.x1},{self.y1})-({self.x2},{self.y2})"


def clamp_point(x: int, y: int, bounds: Box) -> tuple[int, int]:
    return (
        min(max(int(x), bounds.x1), bounds.x2 - 1),
        min(max(int(y), bounds.y1), bounds.y2 - 1),
    )


def distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def nms(
    candidates: Sequence[tuple[Box, float]],
    threshold: float = 0.3,
) -> list[tuple[Box, float]]:
    """Greedy non-maximum suppression on (box, score) pairs."""
    ordered = sorted(candidates, key=lambda c: c[1], reverse=True)
    kept: list[tuple[Box, float]] = []
    for box, score in ordered:
        if all(box.iou(k[0]) < threshold for k in kept):
            kept.append((box, score))
    return kept


def humanize_point(
    x: int,
    y: int,
    region: Box,
    jitter: int = 1,
) -> tuple[int, int]:
    """Nudge a click point off the exact centre, like a human hand would."""
    if jitter <= 0:
        return clamp_point(x, y, region)
    cx, cy = region.center
    r = max(1, int(min(region.width, region.height) * 0.18))
    x += random.randint(-jitter, jitter) + random.randint(-r // 2, r // 2)
    y += random.randint(-jitter, jitter) + random.randint(-r // 2, r // 2)
    return clamp_point(x, y, region)


def ease_in_out(t: float) -> float:
    return 3 * t * t - 2 * t * t * t


def bezier(p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float], t: float) -> tuple[float, float]:
    u = 1 - t
    x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
    return x, y
