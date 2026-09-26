from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import PlanError
from ..utils.logging import get_logger

log = get_logger("video.plan")

#: xfade transition names ffmpeg actually accepts.
XFADE_TRANSITIONS = {
    "cut", "fade", "fadeblack", "fadewhite", "fadegrays", "distance",
    "wipeleft", "wiperight", "wipeup", "wipedown", "wipeupleft", "wipeupright",
    "wipedownleft", "wipedownright", "slideleft", "slideright", "slideup", "slidedown",
    "circlecrop", "rectcrop", "circleclose", "circleopen", "horzclose", "horzopen",
    "vertclose", "vertopen", "diagbl", "diagbr", "diagtl", "diagtr",
    "hlslice", "hrslice", "vuslice", "vdslice", "dissolve", "pixelize",
    "radial", "hlwind", "hrwind", "vuswind", "vdwind", "squeezeh", "squeezev",
}

ASPECTS = ("fill", "fit", "stretch")
NORMALIZERS = ("none", "peak", "loudnorm")
CAPTION_POSITIONS = ("top", "center", "bottom")

#: Friendly names people actually type -> real xfade names.
XFADE_ALIASES = {
    "slide": "slideleft",
    "slideside": "slideleft",
    "wipe": "wipeleft",
    "circle": "circleopen",
    "black": "fadeblack",
    "white": "fadewhite",
    "gray": "fadegrays",
    "grey": "fadegrays",
    "zoom": "circlecrop",
    "spin": "radial",
    "squeeze": "squeezeh",
    "pixel": "pixelize",
    "hard": "cut",
    "none": "cut",
}

_RES_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")


@dataclass
class Segment:
    source: str
    start: float = 0.0
    end: float = 0.0
    speed: float = 1.0
    # Filled during validation:
    path: Path | None = None
    has_audio: bool = True
    duration: float = 0.0

    @property
    def length(self) -> float:
        base = max(0.0, self.end - self.start)
        return base / max(0.01, self.speed)

    @property
    def length_at_speed(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        d = {"source": self.source, "start": round(self.start, 3), "end": round(self.end, 3)}
        if self.speed != 1.0:
            d["speed"] = self.speed
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        if not isinstance(data, dict):
            raise PlanError(f"segment must be an object, got {type(data).__name__}")
        src = str(data.get("source") or data.get("path") or "").strip()
        if not src:
            raise PlanError("segment is missing 'source'")
        try:
            start = float(data.get("start", 0.0))
            end = float(data.get("end", data.get("duration", 0.0)))
        except (TypeError, ValueError) as exc:
            raise PlanError(f"segment start/end must be numbers: {exc}") from exc
        if start < 0:
            raise PlanError("segment start must be >= 0")
        if end <= start:
            raise PlanError(f"segment end ({end}) must be greater than start ({start})")
        speed = float(data.get("speed", 1.0) or 1.0)
        if not 0.05 <= speed <= 12:
            raise PlanError(f"segment speed {speed} is out of range (0.05..12)")
        return cls(source=src, start=start, end=end, speed=speed)


@dataclass
class Transition:
    type: str = "cut"
    duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type}
        if self.duration:
            d["duration"] = self.duration
        return d

    @classmethod
    def from_dict(cls, data: Any) -> "Transition":
        if data is None:
            return cls("cut", 0.0)
        if isinstance(data, str):
            name = XFADE_ALIASES.get(data.strip().lower(), data.strip().lower())
            if name not in XFADE_TRANSITIONS:
                raise PlanError(f"unknown transition {data!r}")
            return cls(name, 0.0)
        if not isinstance(data, dict):
            raise PlanError(f"transition must be a string or object, got {type(data).__name__}")
        name = str(data.get("type", "cut")).strip().lower()
        name = XFADE_ALIASES.get(name, name)
        if name == "cut":
            return cls("cut", 0.0)
        if name not in XFADE_TRANSITIONS:
            raise PlanError(f"unknown transition {name!r} (see XFADE_TRANSITIONS)")
        dur = float(data.get("duration", 0.0) or 0.0)
        if name != "cut" and dur <= 0:
            dur = 0.5
        if dur and not 0.1 <= dur <= 3.0:
            raise PlanError(f"transition duration {dur}s out of range (0.1..3.0)")
        return cls(name, dur)


@dataclass
class AudioPlan:
    music: str | None = None
    music_gain_db: float = -18.0
    duck: bool = True
    normalize: str = "loudnorm"
    target_lufs: float = -14.0
    music_path: Path | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "AudioPlan":
        data = data or {}
        if not isinstance(data, dict):
            raise PlanError("audio must be an object")
        norm = str(data.get("normalize", "loudnorm")).lower()
        if norm not in NORMALIZERS:
            raise PlanError(f"unknown normalize mode {norm!r} (use {NORMALIZERS})")
        return cls(
            music=(str(data["music"]) if data.get("music") else None),
            music_gain_db=float(data.get("music_gain_db", -18.0)),
            duck=bool(data.get("duck", True)),
            normalize=norm,
            target_lufs=float(data.get("target_lufs", -14.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"normalize": self.normalize, "target_lufs": self.target_lufs}
        if self.music:
            d["music"] = self.music
            d["music_gain_db"] = self.music_gain_db
            d["duck"] = self.duck
        return d


@dataclass
class CaptionsPlan:
    enabled: bool = False
    srt: str | None = None
    burn: bool = True
    position: str = "bottom"
    style: str = ""
    language: str = "en"
    whisper_model: str = "base"
    srt_path: Path | None = None

    @classmethod
    def from_dict(cls, data: Any, defaults: dict[str, Any] | None = None) -> "CaptionsPlan":
        data = data or {}
        if isinstance(data, bool):
            data = {"enabled": data}
        if not isinstance(data, dict):
            raise PlanError("captions must be an object or boolean")
        defaults = defaults or {}
        pos = str(data.get("position", "bottom")).lower()
        if pos not in CAPTION_POSITIONS:
            raise PlanError(f"caption position must be one of {CAPTION_POSITIONS}")
        return cls(
            enabled=bool(data.get("enabled", False)),
            srt=(str(data["srt"]) if data.get("srt") else None),
            burn=bool(data.get("burn", True)),
            position=pos,
            style=str(data.get("style", defaults.get("style", "")) or ""),
            language=str(data.get("language", defaults.get("language", "en"))),
            whisper_model=str(data.get("whisper_model", defaults.get("whisper_model", "base"))),
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"enabled": self.enabled, "burn": self.burn, "position": self.position}
        if self.srt:
            d["srt"] = self.srt
        return d


@dataclass
class VideoPlan:
    segments: list[Segment] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    audio: AudioPlan = field(default_factory=AudioPlan)
    captions: CaptionsPlan = field(default_factory=CaptionsPlan)
    output: str = "out.mp4"
    resolution: str = "1080x1920"
    fps: int = 30
    aspect: str = "fill"
    pad_blur: bool = True
    crf: int = 20
    preset: str = "veryfast"
    format: str = "mp4"
    audio_bitrate: str = "192k"
    loop_music: bool = True
    overwrite: bool = False
    output_path: Path | None = None
    notes: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- geometry
    @property
    def width(self) -> int:
        m = _RES_RE.match(str(self.resolution))
        return int(m.group(1)) if m else 1080

    @property
    def height(self) -> int:
        m = _RES_RE.match(str(self.resolution))
        return int(m.group(2)) if m else 1920

    @property
    def transition_durations(self) -> list[float]:
        out: list[float] = []
        for i in range(len(self.segments) - 1):
            tr = self.transitions[i] if i < len(self.transitions) else Transition()
            d = 0.0 if tr.type == "cut" else tr.duration
            limit = min(self.segments[i].length, self.segments[i + 1].length)
            out.append(min(d, limit / 2.0))
        return out

    @property
    def has_xfades(self) -> bool:
        return any(d > 0 for d in self.transition_durations)

    @property
    def total_duration(self) -> float:
        xf = sum(self.transition_durations)
        return max(0.0, sum(s.length for s in self.segments) - xf)

    @property
    def output_exists(self) -> bool:
        return bool(self.output_path and self.output_path.exists())

    # ------------------------------------------------------------------ build
    @classmethod
    def from_dict(cls, data: dict[str, Any], defaults: dict[str, Any] | None = None) -> "VideoPlan":
        if not isinstance(data, dict):
            raise PlanError("plan must be an object")
        d = defaults or {}

        segs_raw = data.get("segments") or data.get("clips") or []
        if not segs_raw:
            inputs = data.get("inputs")
            if inputs:
                segs_raw = [{"source": i} for i in inputs]
        if not segs_raw:
            raise PlanError("plan has no segments - list the source files and time ranges to keep")
        segments = [Segment.from_dict(s) for s in segs_raw]
        if len(segments) > 64:
            raise PlanError("too many segments (64 max) - that is almost certainly a mistake")

        tr_raw = data.get("transitions") or []
        if isinstance(tr_raw, str):
            tr_raw = [tr_raw]
        transitions = [Transition.from_dict(t) for t in tr_raw]
        while len(transitions) < len(segments) - 1:
            transitions.append(Transition("cut", 0.0))

        res = str(data.get("resolution", d.get("resolution", "1080x1920")))
        if not _RES_RE.match(res):
            raise PlanError(f"resolution must look like 1080x1920, got {res!r}")
        aspect = str(data.get("aspect", d.get("aspect", "fill"))).lower()
        if aspect not in ASPECTS:
            raise PlanError(f"aspect must be one of {ASPECTS}")

        out = str(data.get("output") or d.get("output") or "out.mp4")
        plan = cls(
            segments=segments,
            transitions=transitions,
            audio=AudioPlan.from_dict(data.get("audio")),
            captions=CaptionsPlan.from_dict(data.get("captions"), d.get("captions")),
            output=out,
            resolution=res,
            fps=int(data.get("fps", d.get("fps", 30))),
            aspect=aspect,
            pad_blur=bool(data.get("pad_blur", d.get("pad_blur", True))),
            crf=int(data.get("crf", d.get("crf", 20))),
            preset=str(data.get("preset", d.get("preset", "veryfast"))),
            format=str(data.get("format", d.get("format", "mp4"))).lower().lstrip("."),
            audio_bitrate=str(data.get("audio_bitrate", d.get("audio_bitrate", "192k"))),
            loop_music=bool(data.get("loop_music", d.get("loop_music", True))),
            overwrite=bool(data.get("overwrite", False)),
            notes=list(data.get("notes") or []),
        )
        if not 1 <= plan.fps <= 120:
            raise PlanError(f"fps {plan.fps} is out of range")
        if not 0 <= plan.crf <= 51:
            raise PlanError(f"crf {plan.crf} is out of range (0..51)")
        return plan

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "transitions": [t.to_dict() for t in self.transitions],
            "audio": self.audio.to_dict(),
            "captions": self.captions.to_dict(),
            "output": self.output,
            "resolution": self.resolution,
            "fps": self.fps,
            "aspect": self.aspect,
            "crf": self.crf,
            "preset": self.preset,
        }

    def resolve_paths(self, base: Path | None = None) -> None:
        """Attach absolute Paths; expand ~ and env vars; fill source durations."""
        from .probe import probe

        base = base or Path.cwd()
        for seg in self.segments:
            p = Path(seg.source).expanduser()
            if not p.is_absolute():
                p = (base / p)
            p = p.resolve()
            if not p.exists():
                guess = _autocorrect(p, base)
                if guess:
                    log.info("resolved %s -> %s", p, guess)
                    p = guess
            if not p.exists():
                raise PlanError(f"source file not found: {seg.source}")
            seg.path = p
            info = probe(p)
            seg.has_audio = info.has_audio
            seg.duration = info.duration
            if seg.end > info.duration + 0.35:
                seg.notes_clamp = True  # type: ignore[attr-defined]
                seg.end = max(seg.start + 0.05, info.duration)
                log.warning("clamped segment end to media duration (%.2fs)", info.duration)
            if seg.start >= info.duration:
                raise PlanError(f"segment starts at {seg.start}s but {p.name} is only {info.duration:.2f}s long")
        if self.audio.music:
            m = Path(self.audio.music).expanduser()
            if not m.is_absolute():
                m = base / m
            m = m.resolve()
            if not m.exists():
                raise PlanError(f"music file not found: {self.audio.music}")
            self.audio.music_path = m
        if self.captions.srt:
            s = Path(self.captions.srt).expanduser()
            if not s.is_absolute():
                s = base / s
            if not s.exists():
                raise PlanError(f"srt file not found: {self.captions.srt}")
            self.captions.srt_path = s.resolve()
        out = Path(self.output).expanduser()
        if not out.is_absolute():
            out = base / out
        self.output_path = out.resolve()

    def describe(self) -> str:
        lines = [
            f"output      : {self.output_path or self.output}",
            f"timeline    : {len(self.segments)} segment(s), {self.total_duration:.2f}s after transitions",
            f"format      : {self.resolution} @ {self.fps}fps, aspect={self.aspect}, crf={self.crf} ({self.preset})",
        ]
        for i, seg in enumerate(self.segments):
            name = seg.path.name if seg.path else seg.source
            extra = f" speed={seg.speed}x" if seg.speed != 1.0 else ""
            lines.append(f"  [{i}] {name} {seg.start:.2f}-{seg.end:.2f}{extra}")
        if self.transitions:
            lines.append(
                "transitions : "
                + ", ".join(f"{t.type}({t.duration:.2f}s)" for t in self.transitions[: len(self.segments) - 1])
            )
        if self.audio.music:
            lines.append(
                f"audio       : music={self.audio.music} {self.audio.music_gain_db}dB "
                f"duck={self.audio.duck} normalize={self.audio.normalize}({self.audio.target_lufs}LUFS)"
            )
        if self.captions.enabled:
            mode = "burned in" if self.captions.burn else "soft track"
            lines.append(f"captions    : {mode}, position={self.captions.position}")
        return "\n".join(lines)


def _autocorrect(path: Path, base: Path) -> Path | None:
    """Try to be helpful: same name in common folders, case-insensitive match."""
    target = path.name.lower()
    search_dirs = [
        base,
        base / "Desktop",
        base / "Downloads",
        base / "Videos",
        base / "Documents",
        Path.home() / "Desktop",
        Path.home() / "Videos",
        Path.home() / "Downloads",
        Path.home() / "Movies",
    ]
    for d in search_dirs:
        if not d or not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                if entry.is_file() and entry.name.lower() == target:
                    return entry.resolve()
        except (OSError, PermissionError):
            continue
    return None


def plan_from_json_file(path: str | Path, defaults: dict[str, Any] | None = None) -> VideoPlan:
    import json

    p = Path(path).expanduser()
    if not p.exists():
        raise PlanError(f"plan file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlanError(f"{p.name} is not valid JSON: {exc}") from exc
    if isinstance(data, list):
        data = {"segments": data}
    return VideoPlan.from_dict(data, defaults)


def build_default_plan(
    inputs: list[str | Path],
    output: str | Path,
    keep: list[tuple[float, float]] | None = None,
    **overrides: Any,
) -> VideoPlan:
    """Convenience: whole files, or explicit keep-ranges of the first file."""
    from .probe import probe

    segs: list[dict[str, Any]] = []
    for src in inputs:
        if keep and Path(str(src)) == Path(str(inputs[0])):
            for start, end in keep:
                segs.append({"source": str(src), "start": start, "end": end})
        else:
            dur = probe(src).duration if Path(src).exists() else 0.0
            segs.append({"source": str(src), "start": 0.0, "end": max(dur, 0.1)})
    data = {"segments": segs, "output": str(output)}
    data.update({k: v for k, v in overrides.items() if v is not None})
    return VideoPlan.from_dict(data)
