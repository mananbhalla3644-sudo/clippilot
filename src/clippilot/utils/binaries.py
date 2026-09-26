from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from ..errors import BinaryKind, DependencyError

CANDIDATE_DIRS = (
    "./bin",
    "./tools/ffmpeg/bin",
    "./vendor/ffmpeg/bin",
    str(Path.home() / "scoop/shims"),
    str(Path.home() / "scoop/apps/ffmpeg/current/bin"),
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ffmpeg\bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",
)

WIN_PACKAGES = {
    BinaryKind.FFMPEG: "Gyan.FFmpeg",
    BinaryKind.FFPROBE: "Gyan.FFmpeg",
    BinaryKind.TESSERACT: "UB-Mannheim.TesseractOCR",
}


def _env_override(kind: BinaryKind) -> str:
    keys = {
        BinaryKind.FFMPEG: ("CLIPPILOT_FFMPEG",),
        BinaryKind.FFPROBE: ("CLIPPILOT_FFPROBE",),
        BinaryKind.TESSERACT: ("CLIPPILOT_TESSERACT", "TESSERACT_CMD"),
    }
    for key in keys[kind]:
        val = os.environ.get(key)
        if val and Path(val).exists():
            return val
    return ""


@lru_cache(maxsize=8)
def find_binary(kind: BinaryKind, extra: str = "") -> str:
    """Locate an external tool, or raise DependencyError with install hints."""
    name = kind.value
    override = extra or _env_override(kind)
    if override:
        p = Path(override)
        if p.is_file():
            return str(p)
        if p.is_dir():
            found = p / (name + ".exe" if os.name == "nt" else name)
            if found.is_file():
                return str(found)
        raise DependencyError(f"{name}: configured path does not exist: {override}")

    found = shutil.which(name)
    if found:
        return found

    for d in CANDIDATE_DIRS:
        cand = Path(d) / (name + (".exe" if os.name == "nt" else ""))
        if cand.is_file():
            return str(cand)

    raise DependencyError(missing_binary_message(kind))


def missing_binary_message(kind: BinaryKind) -> str:
    name = kind.value
    lines = [f"{name} not found on PATH."]
    if os.name == "nt":
        pkg = WIN_PACKAGES[kind]
        lines += [
            "",
            "Fastest fix (one command, no admin needed):",
            f"  winget install --id {pkg} -e",
            "",
            "Or run the bootstrap script:",
            r"  powershell -ExecutionPolicy Bypass -File setup_windows.ps1",
            "",
            "Or drop the binaries in .\\bin  (clip\\bin\\ffmpeg.exe) and re-run.",
        ]
    else:
        lines += [f"  brew install {name}", f"  sudo apt install {name}"]
    return "\n".join(lines)


def have(kind: BinaryKind, extra: str = "") -> bool:
    try:
        find_binary(kind, extra)
        return True
    except DependencyError:
        return False


@lru_cache(maxsize=4)
def ffmpeg_version(ffmpeg: str) -> str:
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return (out.stdout or out.stderr).splitlines()[0].strip()
    except Exception as exc:  # pragma: no cover
        return f"<version unavailable: {exc}>"


def ffmpeg_features(ffmpeg: str) -> set[str]:
    """Detect optional encoders/filters we can use (libass, libx264, nvenc...)."""
    feats: set[str] = set()
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True, timeout=20)
        blob = (out.stdout or "") + (out.stderr or "")
    except Exception:
        return feats
    for needle, feature in (
        ("--enable-libass", "libass"),
        ("--enable-libx264", "libx264"),
        ("libx264", "libx264"),
        ("--enable-libfdk-aac", "libfdk-aac"),
    ):
        if needle in blob:
            feats.add(feature)
    try:
        enc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        for line in (enc.stdout or "").splitlines():
            low = line.lower()
            if " nvenc" in low:
                feats.add("h264_nvenc")
            if " hevc_nvenc" in low:
                feats.add("hevc_nvenc")
            if " h264_amf" in low:
                feats.add("h264_amf")
            if " h264_qsv" in low:
                feats.add("h264_qsv")
            if "libmp3lame" in low:
                feats.add("libmp3lame")
    except Exception:
        pass
    return feats
