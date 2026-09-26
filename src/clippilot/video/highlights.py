from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import RenderError
from ..utils.logging import get_logger
from .plan import Segment

log = get_logger("video.highlights")

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass
class Range:
    start: float
    end: float

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, float]:
        return {"start": round(self.start, 3), "end": round(self.end, 3)}

    def as_segment(self, source: str) -> dict[str, Any]:
        return {"source": source, "start": round(self.start, 3), "end": round(self.end, 3)}


def detect_silences(
    ffmpeg_exe: str,
    path: str | Path,
    noise_db: float = -32.0,
    min_duration: float = 0.35,
) -> list[Range]:
    """ffmpeg silencedetect -> list of quiet ranges."""
    proc = subprocess.run(
        [
            ffmpeg_exe, "-hide_banner", "-nostats", "-i", str(path),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_duration}",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=1800,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    silences: list[Range] = []
    pending: float | None = None
    for line in text.splitlines():
        m = _SILENCE_START.search(line)
        if m:
            pending = max(0.0, float(m.group(1)))
            continue
        m = _SILENCE_END.search(line)
        if m and pending is not None:
            silences.append(Range(pending, max(pending, float(m.group(1)))))
            pending = None
    if pending is not None:
        silences.append(Range(pending, pending + 1.0))
    return silences


def energy_profile(
    ffmpeg_exe: str,
    path: str | Path,
    window: float = 2.0,
    hop: float = 0.5,
    rate: int = 16000,
) -> list[dict[str, Any]]:
    """RMS energy per window, scored 0..1 (speech + motion => high)."""
    proc = subprocess.Popen(
        [
            ffmpeg_exe, "-hide_banner", "-loglevel", "error", "-nostats", "-i", str(path),
            "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    chunks: list[bytes] = []
    while True:
        block = proc.stdout.read(1 << 20)
        if not block:
            break
        chunks.append(block)
    proc.wait()
    if not chunks:
        return []
    raw = np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0
    if raw.size == 0:
        return []
    win = max(1, int(rate * window))
    hop_n = max(1, int(rate * hop))
    n = 1 + max(0, (raw.size - win) // hop_n)
    rms = np.empty(n, dtype=np.float32)
    # strided windowing is plenty fast and avoids a huge stride view
    csum = np.concatenate(([0.0], np.cumsum(np.abs(raw))))
    csum2 = np.concatenate(([0.0], np.cumsum(raw * raw)))
    for i in range(n):
        a = i * hop_n
        b = a + win
        rms[i] = np.sqrt(max(1e-9, (csum2[b] - csum2[a]) / win))
    peak = float(np.percentile(rms, 95)) or 1.0
    floor = float(np.percentile(rms, 10))
    scaled = np.clip((rms - floor) / max(1e-6, peak - floor), 0.0, 1.0)
    scored: list[dict[str, Any]] = []
    for i in range(n):
        scored.append(
            {
                "start": round(i * hop, 3),
                "end": round(i * hop + window, 3),
                "score": round(float(scaled[i]), 4),
            }
        )
    return scored


def top_windows(
    scored: list[dict[str, Any]],
    count: int = 5,
    min_gap: float = 1.0,
    min_score: float = 0.28,
) -> list[Range]:
    """Greedy non-overlapping pick of the loudest/most active windows."""
    order = sorted(scored, key=lambda d: d["score"], reverse=True)
    chosen: list[Range] = []
    for d in order:
        if d["score"] < min_score:
            break
        r = Range(float(d["start"]), float(d["end"]))
        if any(not (r.end + min_gap <= c.start or r.start >= c.end + min_gap) for c in chosen):
            continue
        chosen.append(r)
        if len(chosen) >= count:
            break
    return sorted(chosen, key=lambda r: r.start)


def keep_from_silences(
    duration: float,
    silences: list[Range],
    pad: float = 0.25,
    min_keep: float = 0.6,
) -> list[Range]:
    """Complement of the silence map, with a little breathing room kept."""
    keep: list[Range] = []
    cursor = 0.0
    for s in sorted(silences, key=lambda r: r.start):
        if s.start - pad > cursor:
            keep.append(Range(cursor, max(cursor + min_keep, s.start - pad)))
        cursor = max(cursor, s.end + pad)
    if duration - cursor > min_keep:
        keep.append(Range(cursor, duration))
    merged: list[Range] = []
    for r in keep:
        if merged and r.start - merged[-1].end < 0.08:
            merged[-1].end = r.end
        else:
            merged.append(r)
    return [r for r in merged if r.length >= min_keep]


def clamp_ranges(ranges: list[Range], duration: float) -> list[Range]:
    out: list[Range] = []
    for r in ranges:
        start = max(0.0, min(r.start, duration))
        end = max(start + 0.05, min(r.end, duration))
        out.append(Range(start, end))
    return sorted(out, key=lambda r: r.start)


def limit_total(ranges: list[Range], max_seconds: float | None = None) -> list[Range]:
    if not max_seconds:
        return ranges
    out: list[Range] = []
    budget = max_seconds
    for r in ranges:
        if budget <= 0:
            break
        if r.length <= budget:
            out.append(r)
            budget -= r.length
        else:
            out.append(Range(r.start, r.start + budget))
            budget = 0
    return out


@dataclass
class Analysis:
    path: Path
    duration: float
    silences: list[Range] = field(default_factory=list)
    scored: list[dict[str, Any]] = field(default_factory=list)
    highlights: list[Range] = field(default_factory=list)
    keep_ranges: list[Range] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "duration": round(self.duration, 3),
            "silences": [r.to_dict() for r in self.silences],
            "highlights": [r.to_dict() for r in self.highlights],
            "keep_ranges": [r.to_dict() for r in self.keep_ranges],
            "scored": self.scored,
        }

    def summary(self) -> str:
        def fmt(rs: list[Range]) -> str:
            return ", ".join(f"{r.start:.1f}-{r.end:.1f}" for r in rs[:8]) or "none"
        return (
            f"{self.path.name} ({self.duration:.1f}s)\n"
            f"  quiet spans : {fmt(self.silences)}\n"
            f"  best moments: {fmt(self.highlights)}\n"
            f"  kept if trimming silence: {fmt(self.keep_ranges)}"
        )

    def as_segments(self, speed: float = 1.0) -> list[Segment]:
        segs = []
        for r in self.highlights:
            segs.append(Segment(source=str(self.path), start=r.start, end=r.end, speed=speed))
        return segs

    def keep_as_segments(self) -> list[Segment]:
        return [Segment(source=str(self.path), start=r.start, end=r.end) for r in self.keep_ranges]


def analyze(
    ffmpeg_exe: str,
    path: str | Path,
    duration: float,
    window: float = 2.0,
    hop: float = 0.5,
    silence_db: float = -32.0,
    min_silence: float = 0.35,
    pad: float = 0.25,
    top_n: int = 5,
    max_seconds: float | None = None,
) -> Analysis:
    p = Path(path)
    log.info("analysing %s (%.1fs)", p.name, duration)
    silences = detect_silences(ffmpeg_exe, p, silence_db, min_silence)
    scored = energy_profile(ffmpeg_exe, p, window=window, hop=hop)
    highlights = clamp_ranges(top_windows(scored, count=top_n, min_gap=max(0.5, window / 2)), duration)
    if max_seconds:
        highlights = limit_total(highlights, max_seconds)
    keep = clamp_ranges(keep_from_silences(duration, silences, pad=pad), duration)
    return Analysis(path=p, duration=duration, silences=silences, scored=scored, highlights=highlights, keep_ranges=keep)


def make_plan_segments(
    analysis: Analysis,
    mode: str = "highlights",
    transitions: list[dict[str, Any]] | None = None,
    **plan_overrides: Any,
) -> list[dict[str, Any]]:
    ranges = analysis.highlights if mode == "highlights" else analysis.keep_ranges
    segs = [r.as_segment(str(analysis.path)) for r in ranges]
    if not segs:
        raise RenderError(
            "nothing usable found - the audio may be silent throughout, or the thresholds are too strict"
        )
    out: dict[str, Any] = {"segments": segs}
    if transitions:
        out["transitions"] = transitions
    out.update({k: v for k, v in plan_overrides.items() if v is not None})
    return segs
