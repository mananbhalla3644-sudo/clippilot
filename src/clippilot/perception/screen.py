from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from ..errors import PerceptionError
from ..utils.geometry import Box
from ..utils.logging import get_logger

log = get_logger("perception.screen")

try:  # mss is the fast path
    import mss

    _HAS_MSS = True
except Exception:  # pragma: no cover
    mss = None  # type: ignore[assignment]
    _HAS_MSS = False

try:
    import pyautogui  # only used as a capture fallback

    _HAS_PYAUTOGUI = True
except Exception:
    _HAS_PYAUTOGUI = False

DPI_AWARE = False


def enable_dpi_awareness() -> bool:
    """Per-monitor v2 DPI awareness: makes screen coords == physical pixels."""
    global DPI_AWARE
    if DPI_AWARE:
        return True
    if os.name != "nt":
        DPI_AWARE = True
        return True
    try:
        user32 = ctypes.windll.user32
        try:  # Windows 10 1703+
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                DPI_AWARE = True
                return True
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            DPI_AWARE = True
            return True
        except Exception:
            pass
        try:
            user32.SetProcessDPIAware()
            DPI_AWARE = True
        except Exception:
            pass
    except Exception:
        pass
    return DPI_AWARE


@dataclass
class Capture:
    image: Image.Image
    box: Box  # virtual-screen coordinates of the captured region
    monitor: str
    taken_at: float

    def crop(self, region: Box) -> tuple[Image.Image, Box]:
        """Crop in *screen* coordinates, returning image + its screen box."""
        region = region.intersect(self.box)
        if region is None:
            return self.image.copy(), self.box
        local = region.translate(-self.box.x1, -self.box.y1)
        return self.image.crop(local.as_xywh()), region


class ScreenCapture:
    """Screen grabber. Supports the whole virtual desktop (multi-monitor)."""

    def __init__(self, mode: str = "virtual") -> None:
        enable_dpi_awareness()
        self.mode = mode
        self._sct: Any = None
        self._lock = threading.Lock()
        self._virtual = Box(0, 0, 1920, 1080)
        self._primary = Box(0, 0, 1920, 1080)
        if not _HAS_MSS:
            log.warning("mss unavailable; falling back to pillow/pyautogui capture")
        self._refresh()

    # ------------------------------------------------------------- monitors
    def _refresh(self) -> None:
        with self._lock:
            if _HAS_MSS:
                if self._sct is None:
                    self._sct = mss.mss()
                monitors = self._sct.monitors
                if monitors:
                    v = monitors[0]
                    self._virtual = Box(v["left"], v["top"], v["left"] + v["width"], v["top"] + v["height"])
                if len(monitors) > 1:
                    p = monitors[1]
                    self._primary = Box(p["left"], p["top"], p["left"] + p["width"], p["top"] + p["height"])
            else:
                self._virtual = Box(0, 0, 1920, 1080)

    @property
    def virtual_screen(self) -> Box:
        return self._virtual

    @property
    def primary(self) -> Box:
        return self._primary

    def monitors(self) -> list[tuple[str, Box]]:
        out = [("primary", self._primary), ("virtual", self._virtual)]
        if _HAS_MSS and self._sct is not None:
            for i, m in enumerate(self._sct.monitors[1:], start=1):
                out.append((f"monitor_{i}", Box(m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])))
        return out

    def select(self, which: str) -> Box:
        if which in ("virtual", "all", "desktop"):
            return self._virtual
        if which in ("primary", "main", "1"):
            return self._primary
        for name, box in self.monitors():
            if name == which:
                return box
        return self._virtual

    # --------------------------------------------------------------- capture
    def grab(self, region: Box | None = None, which: str | None = None, retries: int = 3) -> Capture:
        import time as _t

        self._refresh()
        box = region or self.select(which or self.mode)
        last_exc: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                if _HAS_MSS:
                    with self._lock:
                        if self._sct is None:
                            self._sct = mss.mss()
                        raw = self._sct.grab(
                            {"left": box.x1, "top": box.y1, "width": box.width, "height": box.height}
                        )
                    img = Image.frombytes("RGB", (raw.width, raw.height), raw.bgra, "raw", "BGRX")
                    return Capture(img, box, which or self.mode, _t.time())
                if _HAS_PYAUTOGUI:
                    shot = pyautogui.screenshot(region=box.as_xywh())
                    return Capture(shot, box, which or self.mode, _t.time())
                raise RuntimeError("no screen capture backend available (pip install mss)")
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                # mss gets into a bad state after some grabs (BitBlt failures).
                # Throw the handle away and build a fresh one before retrying.
                log.warning("capture attempt %d/%d failed: %s", attempt + 1, retries, exc)
                self._recycle()
                _t.sleep(0.25 * (attempt + 1))
        raise PerceptionError(
            f"could not capture the screen ({last_exc}). If you are on a lock screen, "
            "a secure desktop, or a remote session without a desktop, screen control is unavailable."
        )

    def _recycle(self) -> None:
        with self._lock:
            if self._sct is not None:
                try:
                    self._sct.close()
                except Exception:  # noqa: BLE001
                    pass
                self._sct = None
        time.sleep(0.05)

    def cursor_position(self) -> tuple[int, int] | None:
        if os.name == "nt":
            try:
                class _P(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

                pt = _P()
                if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                    return (int(pt.x), int(pt.y))
                return None
            except Exception:
                return None
        try:
            import pyautogui

            return tuple(int(v) for v in pyautogui.position())  # type: ignore[return-value]
        except Exception:
            return None

    def close(self) -> None:
        with self._lock:
            if self._sct is not None:
                try:
                    self._sct.close()
                except Exception:
                    pass
                self._sct = None

    def __enter__(self) -> "ScreenCapture":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
