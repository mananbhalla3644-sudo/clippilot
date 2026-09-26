from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..errors import BinaryKind, DependencyError
from ..utils.binaries import find_binary, ffmpeg_version
from ..utils.logging import get_logger

log = get_logger("video.probe")


@dataclass
class VideoStream:
    index: int
    codec: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    bitrate: int = 0
    rotation: int = 0
    pix_fmt: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def orientation(self) -> str:
        w, h = self.width, self.height
        return "portrait" if h > w else ("landscape" if w > h else "square")


@dataclass
class AudioStream:
    index: int
    codec: str
    channels: int = 2
    sample_rate: int = 48000
    duration: float = 0.0
    bitrate: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaInfo:
    path: Path
    duration: float = 0.0
    size: int = 0
    format: str = ""
    bitrate: int = 0
    video: VideoStream | None = None
    audio: AudioStream | None = None
    extra_streams: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        return self.audio is not None

    @property
    def width(self) -> int:
        return self.video.width if self.video else 0

    @property
    def height(self) -> int:
        return self.video.height if self.video else 0

    @property
    def fps(self) -> float:
        return self.video.fps if self.video else 0.0

    @property
    def orientation(self) -> str:
        return self.video.orientation if self.video else "audio-only"

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.width and self.height else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "duration": round(self.duration, 3),
            "size": self.size,
            "container": self.format,
            "bitrate": self.bitrate,
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "orientation": self.orientation,
            "has_audio": self.has_audio,
            "audio_codec": self.audio.codec if self.audio else None,
            "audio_channels": self.audio.channels if self.audio else None,
            "sample_rate": self.audio.sample_rate if self.audio else None,
            "extra_streams": self.extra_streams,
        }

    def summary(self) -> str:
        if not self.has_video:
            return f"{self.path.name}: audio only ({self.duration:.1f}s, {self.audio.codec if self.audio else '?'})"
        return (
            f"{self.path.name}: {self.duration:.1f}s {self.width}x{self.height} "
            f"@{self.fps:.2f}fps {self.orientation} {self.format} "
            f"audio={'yes (' + self.audio.codec + ')' if self.has_audio else 'none'}"
        )


def _parse_fps(text: str) -> float:
    text = str(text or "0/0")
    if "/" in text:
        num, den = text.split("/", 1)
        try:
            d = float(den)
            return float(num) / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _rotation_of(stream: dict[str, Any]) -> int:
    tags = stream.get("tags") or {}
    for key in ("rotate", "Rotate"):
        if key in tags:
            try:
                return int(float(tags[key])) % 360
            except (TypeError, ValueError):
                pass
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(float(sd["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    return 0


def probe(path: str | Path, ffprobe: str = "") -> MediaInfo:
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such media file: {p}")
    exe = find_binary(BinaryKind.FFPROBE, ffprobe)
    proc = subprocess.run(
        [
            exe, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(p),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {p.name}: {proc.stderr.strip()[:400]}")
    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format", {})
    info = MediaInfo(
        path=p,
        duration=float(fmt.get("duration") or 0.0),
        size=int(fmt.get("size") or p.stat().st_size),
        format=str(fmt.get("format_long_name") or fmt.get("format_name") or ""),
        bitrate=int(fmt.get("bit_rate") or 0),
        raw=data,
    )
    others = 0
    for st in data.get("streams", []):
        kind = st.get("codec_type")
        if kind == "video" and info.video is None and st.get("disposition", {}).get("attached_pic") is not True:
            info.video = VideoStream(
                index=int(st.get("index", 0)),
                codec=str(st.get("codec_name", "")),
                width=int(st.get("width") or 0),
                height=int(st.get("height") or 0),
                fps=_parse_fps(st.get("avg_frame_rate") or st.get("r_frame_rate")),
                duration=float(st.get("duration") or 0.0),
                bitrate=int(st.get("bit_rate") or 0),
                rotation=_rotation_of(st),
                pix_fmt=str(st.get("pix_fmt") or ""),
                raw=st,
            )
        elif kind == "audio" and info.audio is None:
            info.audio = AudioStream(
                index=int(st.get("index", 0)),
                codec=str(st.get("codec_name", "")),
                channels=int(st.get("channels") or 2),
                sample_rate=int(st.get("sample_rate") or 48000),
                duration=float(st.get("duration") or 0.0),
                bitrate=int(st.get("bit_rate") or 0),
                raw=st,
            )
        else:
            others += 1
    info.extra_streams = others
    if not info.duration and info.video:
        info.duration = info.video.duration
    if not info.duration and info.audio:
        info.duration = info.audio.duration
    return info


def ffprobe_version(ffprobe: str = "") -> str:
    try:
        exe = find_binary(BinaryKind.FFPROBE, ffprobe)
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).splitlines()[0].strip()
    except DependencyError as exc:
        return str(exc)


def duration_of(path: str | Path) -> float:
    return probe(path).duration


@lru_cache(maxsize=64)
def probe_cached(path: str, ffprobe: str = "") -> MediaInfo:
    return probe(path, ffprobe)


def tool_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    for kind in (BinaryKind.FFMPEG, BinaryKind.FFPROBE):
        try:
            exe = find_binary(kind)
            proc = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=20)
            out[kind.value] = (proc.stdout or proc.stderr).splitlines()[0].strip()
        except Exception as exc:  # noqa: BLE001
            out[kind.value] = f"missing ({exc})"
    return out


def describe_media(paths: list[str | Path]) -> str:
    lines = []
    for p in paths:
        try:
            lines.append(probe(p).summary())
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{Path(p).name}: ERROR {exc}")
    return "\n".join(lines)


__all__ = [
    "MediaInfo",
    "VideoStream",
    "AudioStream",
    "probe",
    "probe_cached",
    "duration_of",
    "tool_versions",
    "ffmpeg_version",
    "ffprobe_version",
    "describe_media",
]
