# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for ClipPilot.

    python -m PyInstaller --noconfirm clippilot.spec              # one folder (fast start)
    set CLIPPILOT_ONEFILE=1
    python -m PyInstaller --noconfirm clippilot.spec               # single .exe

Output
    dist\\ClipPilot\\ClipPilot.exe        (default, onedir - starts instantly)
    dist\\ClipPilot.exe                   (CLIPPILOT_ONEFILE=1)

ffmpeg / ffprobe / tesseract found in .\\bin are bundled so the app works on a
machine that has nothing installed. Env switches:
    CLIPPILOT_ONEFILE=1     single-file executable
    CLIPPILOT_CONSOLE=1     keep the console window (for debugging)
    CLIPPILOT_NO_BINARIES=1 skip bundling ffmpeg/tesseract (small exe)
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve()
ONEFILE = os.environ.get("CLIPPILOT_ONEFILE", "0") == "1"
CONSOLE = os.environ.get("CLIPPILOT_CONSOLE", "0") == "1"
BUNDLE_BIN = os.environ.get("CLIPPILOT_NO_BINARIES", "0") != "1"

datas = [
    (str(ROOT / "src" / "clippilot" / "gui" / "web"), "clippilot/gui/web"),
    (str(ROOT / "config.example.yaml"), "."),
]
recipes_dir = ROOT / "recipes"
if recipes_dir.exists():
    datas.append((str(recipes_dir), "recipes"))
templates = ROOT / "assets" / "templates"
if templates.exists():
    datas.append((str(templates), os.path.join("assets", "templates")))

binaries = []
if BUNDLE_BIN:
    for name in ("ffmpeg.exe", "ffprobe.exe", "tesseract.exe"):
        cand = ROOT / "bin" / name
        if cand.exists():
            binaries.append((str(cand), "."))
        else:
            print(f"[spec] {name} not in .\\bin - not bundled")
    tesseract_data = ROOT / "bin" / "tessdata"
    if tesseract_data.exists():
        datas.append((str(tesseract_data), "tessdata"))

try:
    datas += collect_data_files("mss")
except Exception:
    pass

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "clippilot.gui.launcher",
    "clippilot.gui.server",
    "clippilot.cli",
]
for mod in ("mss", "cv2"):
    try:
        hiddenimports += collect_submodules(mod)
    except Exception:
        pass

icon_path = ROOT / "assets" / "clippilot.ico"
icon = str(icon_path) if icon_path.exists() else None

analysis = Analysis(  # noqa: F821
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter.test", "test", "unittest", "pydoc_data", "lib2to3",
        "numpy.f2py", "matplotlib", "pandas", "scipy", "IPython",
        "notebook", "sqlite3", "setuptools", "pip",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)  # noqa: F821

if ONEFILE:
    exe = EXE(  # noqa: F821
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name="ClipPilot",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=CONSOLE,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon,
    )
else:
    exe = EXE(  # noqa: F821
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name="ClipPilot",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=CONSOLE,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon,
    )
    coll = COLLECT(  # noqa: F821
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="ClipPilot",
    )
