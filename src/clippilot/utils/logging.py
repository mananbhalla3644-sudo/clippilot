from __future__ import annotations

import logging
import os
import sys
import time
from typing import Callable

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_configured = False


class _Fmt(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[38;5;244m",
        logging.INFO: "\033[38;5;39m",
        logging.WARNING: "\033[38;5;214m",
        logging.ERROR: "\033[38;5;203m",
        logging.CRITICAL: "\033[48;5;203;38;5;231m",
    }
    RESET = "\033[0m"

    def __init__(self, color: bool) -> None:
        super().__init__("%(asctime)s %(levelname)-5s %(name)-22s %(message)s", "%H:%M:%S")
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.color:
            return text
        c = self.COLORS.get(record.levelno, "")
        return f"{c}{text}{self.RESET}" if c else text


def setup_logging(level: str = "info", log_file: str | os.PathLike | None = None) -> None:
    global _configured
    if _configured:
        logging.getLogger().setLevel(_LEVELS.get(str(level).lower(), logging.INFO))
        return
    root = logging.getLogger()
    root.setLevel(_LEVELS.get(str(level).lower(), logging.INFO))
    stream = logging.StreamHandler(sys.stderr)
    use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty() and os.name == "nt"
    stream.setFormatter(_Fmt(use_color))
    root.addHandler(stream)
    if log_file:
        os.makedirs(os.path.dirname(str(log_file)) or ".", exist_ok=True)
        fh = logging.FileHandler(str(log_file), encoding="utf-8")
        fh.setFormatter(_Fmt(False))
        root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.t0

    def __str__(self) -> str:
        return f"{self.elapsed:.2f}s"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def progress_line(done: int, total: int, prefix: str = "") -> tuple[Callable[[str], None], str]:
    """Build (emit, final_line) progress helpers."""
    total = max(1, total)
    last = [0.0]

    def emit(msg: str) -> None:
        now = time.perf_counter()
        pct = min(100.0, 100.0 * done / total)
        if now - last[0] > 0.15:
            last[0] = now
            sys.stderr.write(f"\r{prefix}[{pct:5.1f}%] {msg[:60]:<60}")
            sys.stderr.flush()

    def final(msg: str) -> str:
        sys.stderr.write("\r" + " " * 100 + "\r")
        return f"{prefix}100.0% {msg}"

    return emit, final
