from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..utils.geometry import Box
from ..utils.logging import get_logger

log = get_logger("control.win32")

IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------- input
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x01000
MOUSEEVENTF_ABSOLUTE_VIRT = 0x4000
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x0002, 0x0004, 0x0008

SM_CXSCREEN, SM_CYSCREEN = 0, 1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

VK: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "select": 0x29, "print": 0x2A, "execute": 0x2B, "printscreen": 0x2C, "prtsc": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "help": 0x2F,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "f13": 0x7C, "f14": 0x7D, "f15": 0x7E, "f16": 0x7F, "f17": 0x80, "f18": 0x81,
    "f19": 0x82, "f20": 0x83, "f21": 0x84, "f22": 0x85, "f23": 0x86, "f24": 0x87,
}
for _i in range(1, 25):
    VK.setdefault(f"num{_i}", 0x60 + _i)
for _i in range(10):
    VK.setdefault(f"num{_i}pad", 0x60 + _i)

KEY_ALIASES = {
    "cmd": "win", "meta": "win", "super": "win", "option": "alt",
    "return": "enter", "del": "delete", "ins": "insert", "pgup": "pageup",
    "pgdn": "pagedown", "esc": "escape", "bksp": "backspace", "arrowleft": "left",
    "arrowright": "right", "arrowup": "up", "arrowdown": "down", "caps": "capslock",
    "printscreen": "printscreen",
}

# Modifier VKs we must hold while sending another key.
MODIFIERS = {"shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "win": 0x5B}


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    rect: Box
    visible: bool
    minimized: bool
    pid: int = 0
    process: str = ""

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "rect": self.rect.as_list(),
            "visible": self.visible,
            "minimized": self.minimized,
            "process": self.process,
        }


def _user32():
    return ctypes.windll.user32


# ------------------------------------------------------------------- screens
def virtual_screen() -> Box:
    if not IS_WINDOWS:
        return Box(0, 0, 1920, 1080)
    u = _user32()
    return Box(
        int(u.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(u.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        int(u.GetSystemMetrics(SM_XVIRTUALSCREEN) + u.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        int(u.GetSystemMetrics(SM_YVIRTUALSCREEN) + u.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


def primary_screen() -> Box:
    if not IS_WINDOWS:
        return Box(0, 0, 1920, 1080)
    u = _user32()
    return Box(0, 0, int(u.GetSystemMetrics(SM_CXSCREEN)), int(u.GetSystemMetrics(SM_CYSCREEN)))


# --------------------------------------------------------------------- mouse
def _send_mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
            ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
        ]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _U)]

    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = MOUSEINPUT(dx, dy, data, flags, 0, None)
    _user32().SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def cursor_position() -> tuple[int, int]:
    if not IS_WINDOWS:
        return (0, 0)

    class POINT(ctypes.Structure):
        _fields_ = [("x", wt.LONG), ("y", wt.LONG)]

    pt = POINT()
    _user32().GetCursorPos(ctypes.byref(pt))
    return (int(pt.x), int(pt.y))


def move_cursor(x: int, y: int, duration_ms: int = 0, humanize: bool = True, jitter: int = 0) -> None:
    """Absolute move across the whole virtual desktop (negative coords fine)."""
    vs = virtual_screen()
    x = int(min(max(x, vs.x1), vs.x2 - 1))
    y = int(min(max(y, vs.y1), vs.y2 - 1))
    if jitter:
        x += int((jitter * (hash((time.time_ns(),)) % 100) / 100) - jitter / 2)
        y += int((jitter * (hash((time.time_ns() + 7,)) % 100) / 100) - jitter / 2)
        x = int(min(max(x, vs.x1), vs.x2 - 1))
        y = int(min(max(y, vs.y1), vs.y2 - 1))
    if duration_ms <= 0:
        _send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_ABSOLUTE_VIRT, _abs(x), _abs(y))
        return
    steps = max(6, min(60, duration_ms // 8))
    sx, sy = cursor_position()
    for i in range(1, steps + 1):
        t = i / steps
        e = 3 * t * t - 2 * t * t * t if humanize else t
        nx = int(sx + (x - sx) * e)
        ny = int(sy + (y - sy) * e)
        _send_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_ABSOLUTE_VIRT, _abs(nx), _abs(ny))
        time.sleep(duration_ms / 1000.0 / steps)


def _abs(coord: int) -> int:
    vs = virtual_screen()
    span = max(1, vs.width - 1)
    return int((coord - vs.x1) * 65535 / span)


def mouse_down(button: str = "left") -> None:
    flag = {"left": MOUSEEVENTF_LEFTDOWN, "right": MOUSEEVENTF_RIGHTDOWN, "middle": MOUSEEVENTF_MIDDLEDOWN}[button]
    _send_mouse(flag)


def mouse_up(button: str = "left") -> None:
    flag = {"left": MOUSEEVENTF_LEFTUP, "right": MOUSEEVENTF_RIGHTUP, "middle": MOUSEEVENTF_MIDDLEUP}[button]
    _send_mouse(flag)


def scroll(amount: int, horizontal: bool = False) -> None:
    """Positive amount scrolls up (natural direction), like a real wheel."""
    _send_mouse(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, data=int(amount * 120))


# ------------------------------------------------------------------ keyboard
def _send_key(vk: int, up: bool = False) -> None:
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
            ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
        ]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _U)]

    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, None)
    _user32().SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _send_char(ch: str) -> None:
    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
            ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
        ]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _U)]

    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, None)
    _user32().SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def normalize_key(name: str) -> str:
    n = name.strip().lower().replace(" ", "")
    return KEY_ALIASES.get(n, n)


def key_code(name: str) -> int | None:
    n = normalize_key(name)
    if n in VK:
        return VK[n]
    if len(n) == 1:
        return ord(n.upper())
    return None


def press_key(name: str) -> None:
    vk = key_code(name)
    if vk is None:
        raise ValueError(f"unknown key {name!r}")
    if len(normalize_key(name)) == 1:
        _send_char(normalize_key(name))
        return
    _send_key(vk)
    time.sleep(0.012)
    _send_key(vk, up=True)


def hotkey(combo: Iterable[str], hold_ms: int = 18) -> None:
    keys = [normalize_key(k) for k in combo if k]
    codes: list[int] = []
    for k in keys:
        code = key_code(k)
        if code is None:
            raise ValueError(f"unknown key in combo: {k!r}")
        codes.append(code)
    for code in codes:
        _send_key(code)
    time.sleep(hold_ms / 1000.0)
    for code in reversed(codes):
        _send_key(code, up=True)


def type_text(text: str) -> None:
    for ch in text:
        if ch == "\n":
            press_key("enter")
        elif ch == "\t":
            press_key("tab")
        else:
            _send_char(ch)
        time.sleep(0.004)


# -------------------------------------------------------------------- window
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _window_rect(hwnd: int) -> Box:
    r = wt.RECT()
    if _user32().GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
        return Box(int(r.left), int(r.top), int(r.right), int(r.bottom))
    return Box(0, 0, 0, 0)


def list_windows(visible_only: bool = True, limit: int = 40) -> list[WindowInfo]:
    out: list[WindowInfo] = []

    def cb(hwnd, _lparam):
        if visible_only and not _user32().IsWindowVisible(hwnd):
            return True
        length = _user32().GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32().GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wt.DWORD()
        _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=title,
                rect=_window_rect(int(hwnd)),
                visible=bool(_user32().IsWindowVisible(hwnd)),
                minimized=bool(_user32().IsIconic(hwnd)),
                pid=int(pid.value),
                process=process_name(int(pid.value)),
            )
        )
        return len(out) < limit

    try:
        _user32().EnumWindows(EnumWindowsProc(cb), 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("EnumWindows failed: %s", exc)
    return out


def process_name(pid: int) -> str:
    try:
        import psutil  # type: ignore

        return psutil.Process(pid).name()
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=6,
        )
        for line in out.stdout.splitlines():
            parts = [p.strip('" ') for p in line.split('","')]
            if len(parts) > 1 and parts[1].isdigit() and int(parts[1]) == pid:
                return parts[0]
    except Exception:
        pass
    return ""


def active_window() -> WindowInfo | None:
    if not IS_WINDOWS:
        return None
    hwnd = _user32().GetForegroundWindow()
    if not hwnd:
        return None
    length = _user32().GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32().GetWindowTextW(hwnd, buf, length + 1)
    pid = wt.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(
        hwnd=int(hwnd),
        title=buf.value.strip(),
        rect=_window_rect(int(hwnd)),
        visible=True,
        minimized=False,
        pid=int(pid.value),
        process=process_name(int(pid.value)),
    )


def active_window_title() -> str | None:
    w = active_window()
    return w.title if w and w.title else None


def top_window_titles(limit: int = 6) -> list[str]:
    wins = [w for w in list_windows() if w.title and w.rect.width > 120 and w.rect.height > 80]
    active = active_window()
    if active:
        wins = [w for w in wins if w.hwnd != active.hwnd]
        wins.insert(0, active)
    return [w.title for w in wins[:limit]]


def find_window(title_contains: str, visible_only: bool = True) -> WindowInfo | None:
    needle = title_contains.strip().lower()
    if not needle:
        return None
    for w in list_windows(visible_only=visible_only):
        if needle in w.title.lower():
            return w
    return None


def _attach(hwnd: int) -> int:
    fg = _user32().GetForegroundWindow()
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    other = _user32().GetWindowThreadProcessId(fg, None) if fg else None
    if other and other != cur:
        _user32().AttachThreadInput(cur, other, True)
    return other or cur


def _detach(thread: int) -> None:
    _user32().AttachThreadInput(ctypes.windll.kernel32.GetCurrentThreadId(), thread, False)


def focus_window(title_contains: str, maximize: bool = False) -> WindowInfo | None:
    """Bring a window to the foreground (with the AttachThreadInput dance)."""
    w = find_window(title_contains)
    if w is None:
        return None
    hwnd = wt.HWND(w.hwnd)
    if w.minimized:
        _user32().ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.25)
    thread = _attach(w.hwnd)
    try:
        _user32().BringWindowToTop(hwnd)
        _user32().SetForegroundWindow(hwnd)
        time.sleep(0.12)
        if maximize:
            _user32().ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        _user32().SetActiveWindow(hwnd)
    finally:
        _detach(thread)
    time.sleep(0.2)
    return w


def minimize_all() -> None:
    """Win+D equivalent - get the desktop out of the way."""
    if IS_WINDOWS:
        hotkey(["win", "d"])
    else:
        hotkey(["ctrl", "alt", "f1"])


def close_window(title_contains: str) -> bool:
    w = find_window(title_contains)
    if not w:
        return False
    _user32().PostMessageW(wt.HWND(w.hwnd), 0x0010, 0, 0)  # WM_CLOSE
    return True


# ------------------------------------------------------------------- launch
APP_ALIASES: dict[str, list[str]] = {
    "capcut": ["CapCut.exe", r"C:\Program Files\CapCut\CapCut.exe", "CapCut"],
    "resolve": ["Resolve.exe", r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe", "Resolve"],
    "premiere": ["Adobe Premiere Pro.exe", r"C:\Program Files\Adobe\Adobe Premiere Pro 2025\Adobe Premiere Pro.exe", "Adobe Premiere Pro"],
    "shotcut": ["shotcut.exe", r"C:\Program Files\Shotcut\shotcut.exe", "Shotcut"],
    "clipchamp": ["Clipchamp.exe", "Microsoft Clipchamp"],
    "photos": ["Microsoft.Windows.Photos_8wekyb3d8bbwe!App", "Microsoft Photos", "Photos"],
    "explorer": ["explorer.exe", "explorer"],
    "vlc": ["vlc.exe", "VLC media player", "vlc"],
    "obs": ["obs64.exe", "obs32.exe", "OBS Studio"],
    "vscode": ["Code.exe", "Visual Studio Code", "code"],
    "chrome": ["chrome.exe", "Google Chrome", "chrome"],
    "edge": ["msedge.exe", "Microsoft Edge", "msedge"],
    "notepad": ["notepad.exe", "Notepad"],
}

START_MENU = [
    Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
]


def resolve_app_path(app: str) -> str:
    """Find an executable: direct hit, installed path, PATH, or Start Menu shortcut."""
    app_lower = app.strip().strip('"')
    if os.path.isfile(app_lower):
        return app_lower

    candidates: list[str] = list(APP_ALIASES.get(app_lower.lower(), []))
    if app_lower.endswith(".exe"):
        candidates.append(app_lower)
    else:
        candidates.append(app_lower + ".exe")
    if os.name == "nt" and app_lower.startswith("ms-settings"):
        return app_lower

    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    for cand in candidates:
        base = os.path.basename(cand).replace(".exe", "")
        found = shutil.which(base) or shutil.which(cand)
        if found:
            return found

    # brute-force scan of the usual install roots (fast enough, cached by caller)
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                if app_lower not in entry.lower():
                    continue
                for dirpath, _dirnames, filenames in os.walk(os.path.join(root, entry), topdown=True):
                    depth = dirpath.count(os.sep) - os.path.join(root, entry).count(os.sep)
                    if depth > 3:
                        _dirnames[:] = []
                        continue
                    for fn in filenames:
                        low = fn.lower()
                        if low == app_lower or (low.startswith(app_lower) and low.endswith(".exe")):
                            return os.path.join(dirpath, fn)
        except (OSError, PermissionError):
            continue

    if os.name == "nt":
        for base in START_MENU:
            if not base or not base.exists():
                continue
            for dirpath, _d, files in os.walk(base):
                for fn in files:
                    if app_lower in fn.lower():
                        full = os.path.join(dirpath, fn)
                        return full
    raise FileNotFoundError(f"could not locate application {app!r}")


def launch(app: str, args: list[str] | None = None, wait: float = 0.0) -> tuple[str, int | None]:
    """Start an app. Returns (command, pid)."""
    if os.name == "nt" and app.strip().lower().startswith("ms-settings"):
        os.startfile(app)  # type: ignore[attr-defined]
        time.sleep(max(1.0, wait))
        return app, None

    path = resolve_app_path(app)
    flags = 0
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        flags = 0x00000008

    log.info("launching %s %s", path, args or [])
    if path.lower().endswith(".lnk") or path.lower().endswith(".url"):
        os.startfile(path)  # type: ignore[attr-defined]
        return path, None
    proc = subprocess.Popen([path, *(args or [])], creationflags=creationflags)
    if wait:
        time.sleep(wait)
    return path, proc.pid


def launch_file(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        import subprocess as sp

        opener = "open" if sys_platform_darwin() else "xdg-open"
        sp.Popen([opener, path])


def sys_platform_darwin() -> bool:
    return sys.platform == "darwin"


def wait_for_window(title_contains: str, timeout: float = 15.0, poll: float = 0.4) -> WindowInfo | None:
    end = time.time() + timeout
    while time.time() < end:
        w = find_window(title_contains)
        if w:
            time.sleep(0.4)
            return w
        time.sleep(poll)
    return None
