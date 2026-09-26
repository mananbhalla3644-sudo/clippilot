"""Generate assets/clippilot.ico (used by the PyInstaller build)."""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def rounded(size: int, radius_ratio: float = 0.22) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * radius_ratio)
    # vertical gradient background
    top, bottom = (76, 201, 240), (247, 120, 186)
    for y in range(size):
        t = y / max(1, size - 1)
        d.line(
            [(0, y), (size, y)],
            fill=(
                int(top[0] + (bottom[0] - top[0]) * t),
                int(top[1] + (bottom[1] - top[1]) * t),
                int(top[2] + (bottom[2] - top[2]) * t),
                255,
            ),
        )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    img.putalpha(mask)

    # a stylised "play + cut" mark
    d = ImageDraw.Draw(img)
    inset = size * 0.26
    if size >= 32:
        bar_w = max(1, int(size * 0.055))
        cx = size * 0.36
        d.rectangle([cx - bar_w / 2, inset, cx + bar_w / 2, size - inset], fill=(6, 18, 26, 235))
        tri = [
            (size * 0.5, size * 0.34),
            (size * 0.5, size * 0.66),
            (size * 0.74, size * 0.5),
        ]
        d.polygon(tri, fill=(6, 18, 26, 235))
    else:
        d.polygon([(size * 0.34, size * 0.28), (size * 0.34, size * 0.72), (size * 0.72, size * 0.5)],
                  fill=(6, 18, 26, 240))
    return img


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    frames = [rounded(s) for s in SIZES]
    ico = OUT / "clippilot.ico"
    frames[0].save(ico, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[1:])
    png = OUT / "clippilot.png"
    frames[-1].save(png)
    print(f"wrote {ico} and {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
