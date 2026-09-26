from __future__ import annotations

import base64
import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .geometry import Box


def to_pil(image: Image.Image | np.ndarray) -> Image.Image:
    if isinstance(image, Image.Image):
        return image
    return Image.fromarray(image)


def encode_jpeg(image: Image.Image, quality: int = 80, max_side: int | None = None) -> bytes:
    img = image.convert("RGB")
    if max_side:
        w, h = img.size
        longest = max(w, h)
        if longest > max_side:
            s = max_side / float(longest)
            img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def encode_png(image: Image.Image, max_side: int | None = None) -> bytes:
    img = image.convert("RGB")
    if max_side:
        w, h = img.size
        longest = max(w, h)
        if longest > max_side:
            s = max_side / float(longest)
            img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def data_uri(image: Image.Image, quality: int = 80, max_side: int | None = None) -> str:
    raw = encode_jpeg(image, quality=quality, max_side=max_side)
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def diff_ratio(a: Image.Image | np.ndarray, b: Image.Image | np.ndarray) -> float:
    """Fraction of pixels that changed between two same-size images."""
    ia, ib = to_pil(a).convert("RGB"), to_pil(b).convert("RGB")
    if ia.size != ib.size:
        ib = ib.resize(ia.size, Image.BILINEAR)
    delta = ImageChops.difference(ia, ib).convert("L")
    arr = np.asarray(delta, dtype=np.uint8)
    return float((arr > 12).mean())


def draw_overlay(
    image: Image.Image,
    boxes: list[tuple[Box, str, tuple[int, int, int]]],
    origin: tuple[int, int] = (0, 0),
    width: int = 2,
) -> Image.Image:
    """Draw labelled boxes (screen coords -> image coords via origin)."""
    out = image.convert("RGB").copy()
    d = ImageDraw.Draw(out, "RGBA")
    ox, oy = origin
    for box, label, color in boxes:
        x1, y1 = box.x1 - ox, box.y1 - oy
        x2, y2 = box.x2 - ox, box.y2 - oy
        d.rectangle([x1, y1, x2, y2], outline=color + (255,), width=width)
        if label:
            tw = d.textlength(label)
            d.rectangle([x1, max(0, y1 - 15), x1 + tw + 8, y1], fill=color + (215,))
            d.text((x1 + 4, max(0, y1 - 14)), label, fill=(0, 0, 0, 255))
    return out


def annotate_cursor(image: Image.Image, point: tuple[int, int] | None, origin: tuple[int, int] = (0, 0)) -> Image.Image:
    if point is None:
        return image
    out = image.convert("RGB").copy()
    d = ImageDraw.Draw(out, "RGBA")
    x, y = point[0] - origin[0], point[1] - origin[1]
    r = 9
    d.ellipse([x - r, y - r, x + r, y + r], outline=(255, 80, 80, 255), width=2)
    d.line([x - r - 5, y, x - r + 2, y], fill=(255, 80, 80, 255), width=2)
    d.line([x + r - 2, y, x + r + 5, y], fill=(255, 80, 80, 255), width=2)
    d.line([x, y - r - 5, x, y - r + 2], fill=(255, 80, 80, 255), width=2)
    d.line([x, y + r - 2, x, y + r + 5], fill=(255, 80, 80, 255), width=2)
    return out


def prewarp(image: Image.Image, mode: str) -> Image.Image:
    """Cheap preprocessing that measurably helps tesseract on UI screenshots."""
    g = image.convert("L")
    if mode == "gray":
        return g
    if mode == "boost":
        from PIL import ImageOps

        g = ImageOps.autocontrast(g, cutoff=1)
        g = g.filter(ImageFilter.SHARPEN)
        return g
    if mode == "binarize":
        from PIL import ImageOps

        g = ImageOps.autocontrast(g, cutoff=2)
        # Local mean threshold (cheap Sauvola-ish) keeps thin UI text alive.
        arr = np.asarray(g, dtype=np.float32)
        k = 25
        pad = k // 2
        padded = np.pad(arr, pad, mode="edge")
        integral = padded.cumsum(0).cumsum(1)
        integral = np.pad(integral, ((1, 0), (1, 0)), mode="constant")
        size = k * k
        total = (
            integral[k:, k:] - integral[:-k, k:] - integral[k:, :-k] + integral[:-k, :-k]
        )
        local_mean = total / size
        out = np.where(arr > (local_mean - 6), 255, 0).astype(np.uint8)
        return Image.fromarray(out)
    return image


def save(image: Image.Image, path: str | Path, quality: int = 88) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() in (".jpg", ".jpeg"):
        image.convert("RGB").save(p, quality=quality)
    else:
        image.save(p)
    return p
