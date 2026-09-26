"""PyInstaller entry point: launch the ClipPilot desktop app."""
from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()
    from clippilot.gui.launcher import main as gui_main

    argv = sys.argv[1:]
    if argv and argv[0] in ("cli", "--cli"):
        from clippilot.cli import main as cli_main

        return cli_main(argv[1:])
    return gui_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
