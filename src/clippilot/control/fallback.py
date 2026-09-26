from __future__ import annotations

import time
from typing import Any, Iterable

from . import win32

try:  # cross-platform / dependency-free fallback
    import pyautogui

    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.0
    _HAS_PYAUTOGUI = True
except Exception:  # pragma: no cover
    _HAS_PYAUTOGUI = False

from ..utils.geometry import Box
from ..utils.logging import get_logger

log = get_logger("control.fallback")


class InputBackend:
    name = "base"

    def move(self, x: int, y: int, duration_ms: int = 0, humanize: bool = True, jitter: int = 0) -> None:
        raise NotImplementedError

    def button(self, kind: str, x: int, y: int, clicks: int = 1) -> None:
        raise NotImplementedError

    def scroll(self, amount: int, horizontal: bool = False) -> None:
        raise NotImplementedError

    def key(self, name: str) -> None:
        raise NotImplementedError

    def hotkey(self, combo: Iterable[str]) -> None:
        raise NotImplementedError

    def type_text(self, text: str) -> None:
        raise NotImplementedError

    def position(self) -> tuple[int, int]:
        raise NotImplementedError

    def screen(self) -> Box:
        raise NotImplementedError


class Win32Backend(InputBackend):
    """Native Win32 SendInput: multi-monitor safe, no extra dependencies."""

    name = "win32"

    def __init__(self) -> None:
        if not win32.IS_WINDOWS:
            raise RuntimeError("win32 backend requires Windows")

    def move(self, x: int, y: int, duration_ms: int = 0, humanize: bool = True, jitter: int = 0) -> None:
        win32.move_cursor(x, y, duration_ms=duration_ms, humanize=humanize, jitter=jitter)

    def button(self, kind: str, x: int, y: int, clicks: int = 1) -> None:
        self.move(x, y, duration_ms=0)
        time.sleep(0.03)
        for i in range(clicks):
            if kind == "right":
                win32.mouse_down("right")
                time.sleep(0.03)
                win32.mouse_up("right")
            elif kind == "middle":
                win32.mouse_down("middle")
                time.sleep(0.03)
                win32.mouse_up("middle")
            else:
                win32.mouse_down("left")
                time.sleep(0.035)
                win32.mouse_up("left")
            if i < clicks - 1:
                time.sleep(0.055)

    def scroll(self, amount: int, horizontal: bool = False) -> None:
        steps = max(1, min(12, abs(int(amount)) // 2 or 1))
        per = amount / steps
        for _ in range(steps):
            win32.scroll(int(per), horizontal=horizontal)
            time.sleep(0.02)

    def key(self, name: str) -> None:
        win32.press_key(name)

    def hotkey(self, combo: Iterable[str]) -> None:
        win32.hotkey(combo)

    def type_text(self, text: str) -> None:
        win32.type_text(text)

    def position(self) -> tuple[int, int]:
        return win32.cursor_position()

    def screen(self) -> Box:
        return win32.virtual_screen()


class PyAutoGuiBackend(InputBackend):
    name = "pyautogui"

    def __init__(self) -> None:
        if not _HAS_PYAUTOGUI:
            raise RuntimeError("pyautogui is not installed")

    def move(self, x: int, y: int, duration_ms: int = 0, humanize: bool = True, jitter: int = 0) -> None:
        pyautogui.moveTo(x, y, duration=duration_ms / 1000.0)

    def button(self, kind: str, x: int, y: int, clicks: int = 1) -> None:
        pyautogui.click(x=x, y=y, clicks=clicks, button=kind, interval=0.05)

    def scroll(self, amount: int, horizontal: bool = False) -> None:
        if horizontal:
            pyautogui.hscroll(amount / 5)
        else:
            pyautogui.scroll(amount / 5)

    def key(self, name: str) -> None:
        pyautogui.press(name)

    def hotkey(self, combo: Iterable[str]) -> None:
        pyautogui.hotkey(*combo)

    def type_text(self, text: str) -> None:
        pyautogui.typewrite(text, interval=0.01)

    def position(self) -> tuple[int, int]:
        return tuple(int(v) for v in pyautogui.position())  # type: ignore[return-value]

    def screen(self) -> Box:
        w, h = pyautogui.size()
        return Box(0, 0, int(w), int(h))


def get_backend(name: str = "auto") -> InputBackend:
    if name in ("auto", ""):
        if win32.IS_WINDOWS:
            try:
                return Win32Backend()
            except Exception as exc:  # noqa: BLE001
                log.warning("win32 backend failed (%s); trying pyautogui", exc)
        if _HAS_PYAUTOGUI:
            return PyAutoGuiBackend()
        raise RuntimeError("no input backend available")
    if name == "win32":
        return Win32Backend()
    if name == "pyautogui":
        return PyAutoGuiBackend()
    raise ValueError(f"unknown input backend {name!r}")
