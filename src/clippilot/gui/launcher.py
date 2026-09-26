from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .. import __version__
from ..utils.logging import get_logger, setup_logging

log = get_logger("gui.launcher")


def _free_port(preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 40)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ClipPilot"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "ClipPilot"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "clippilot"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _bundled_web() -> Path:
    """Works both from source and from a PyInstaller onefile bundle."""
    here = Path(__file__).resolve().parent
    candidates = [here / "web", Path(getattr(sys, "_MEIPASS", "")) / "clippilot" / "gui" / "web"]
    for c in candidates:
        if (c / "index.html").exists():
            return c
    return here / "web"


def _bundled_bin(name: str) -> str:
    """ffmpeg shipped next to the exe, if the packager included it."""
    meipass = Path(getattr(sys, "_MEIPASS", "") or ".")
    roots = [Path(sys.executable).parent, meipass, meipass / "bin", Path.cwd() / "bin"]
    for root in roots:
        cand = root / (name + ".exe" if os.name == "nt" else name)
        if cand.exists():
            return str(cand)
    return ""


def _configure_env(data_dir: Path) -> None:
    os.environ.setdefault("CLIPPILOT_DATA", str(data_dir))
    for name in ("ffmpeg", "ffprobe"):
        found = _bundled_bin(name)
        if found and not os.environ.get(f"CLIPPILOT_{name.upper()}"):
            os.environ[f"CLIPPILOT_{name.upper()}"] = found


def start_server(
    host: str = "127.0.0.1",
    port: int = 8756,
    config: str | None = None,
    open_browser: bool = True,
) -> tuple[Any, int]:
    import uvicorn

    from .server import create_app

    data_dir = _data_dir()
    _configure_env(data_dir)
    port = _free_port(port)
    app = create_app(config)
    log.info("ClipPilot %s UI on http://%s:%d  (data: %s)", __version__, host, port, data_dir)
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(cfg)

    thread = threading.Thread(target=server.run, name="clippilot-uvicorn", daemon=True)
    thread.start()

    url = f"http://{host}:{port}/"
    for _ in range(80):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    return server, port


def run_desktop(port: int = 8756, config: str | None = None) -> int:
    """Native window via pywebview, falling back to the default browser."""
    server, actual_port = start_server(port=port, config=config, open_browser=True)
    url = f"http://127.0.0.1:{actual_port}/"
    try:
        import webview  # type: ignore
    except ImportError:
        print(f"ClipPilot is running in your browser: {url}")
        print("Press Ctrl+C to quit.")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nbye")
        return 0

    try:
        window = webview.create_window(
            "ClipPilot",
            url,
            width=1440,
            height=940,
            min_size=(1040, 700),
            background_color="#0b0e14",
            text_select=True,
        )
        webview.start(debug=False)
    except Exception as exc:  # noqa: BLE001
        print(f"native window unavailable ({exc}); using the browser at {url}")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nbye")
    finally:
        server.should_exit = True
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="clippilot-gui",
        description="ClipPilot desktop app: chat with the agent, edit video, drive apps.",
    )
    ap.add_argument("--port", type=int, default=8756)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("-c", "--config", help="path to config.yaml")
    ap.add_argument("--browser", action="store_true", help="skip the native window, use the browser")
    ap.add_argument("--no-window", action="store_true", help="serve only (for remote use)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging("debug" if args.verbose else "info")

    if args.no_window:
        server, port = start_server(host=args.host, port=args.port, config=args.config, open_browser=False)
        print(f"ClipPilot serving on http://{args.host}:{port}/  (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            server.should_exit = True
        return 0
    if args.browser:
        server, port = start_server(host=args.host, port=args.port, config=args.config, open_browser=True)
        print(f"ClipPilot serving on http://{args.host}:{port}/  (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            server.should_exit = True
        return 0
    return run_desktop(port=args.port, config=args.config)


if __name__ == "__main__":
    raise SystemExit(main())
