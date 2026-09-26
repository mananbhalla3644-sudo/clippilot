from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..utils.logging import get_logger
from .plan import Segment, VideoPlan

log = get_logger("video.captions")

_SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


@dataclass
class Cue:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}

    def shift(self, delta: float) -> "Cue":
        return Cue(self.start + delta, self.end + delta, self.text)

    def scale(self, factor: float, offset: float = 0.0) -> "Cue":
        return Cue(offset + self.start * factor, offset + self.end * factor, self.text)


def format_timestamp(seconds: float, comma: bool = True) -> str:
    seconds = max(0.0, float(seconds))
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def write_srt(cues: Sequence[Cue], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for i, c in enumerate(cues, start=1):
        lines.append(str(i))
        lines.append(f"{format_timestamp(c.start)} --> {format_timestamp(c.end)}")
        lines.append(c.text.strip())
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote %d cues to %s", len(cues), p.name)
    return p


def read_srt(path: str | Path) -> list[Cue]:
    p = Path(path)
    if not p.exists():
        return []
    raw = p.read_text(encoding="utf-8", errors="replace")
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", raw.strip())
    for block in blocks:
        m = _SRT_TIME.search(block)
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        text = block[m.end():].strip()
        text = re.sub(r"<[^>]+>", "", text)
        text = " ".join(text.split())
        if text:
            cues.append(Cue(start, end, text))
    return cues


# --------------------------------------------------------------- whisper
def whisper_available() -> tuple[bool, str]:
    try:
        import faster_whisper  # type: ignore  # noqa: F401

        return True, "faster-whisper"
    except ImportError:
        pass
    try:
        import whisper  # type: ignore  # noqa: F401

        return True, "openai-whisper"
    except ImportError:
        return False, "not installed (pip install faster-whisper)"


def _group_words(
    words: Iterable[tuple[float, float, str]],
    max_chars: int = 34,
    max_lines: int = 2,
    max_gap: float = 0.6,
    min_duration: float = 0.7,
    max_duration: float = 6.0,
) -> list[Cue]:
    """Group word timings into readable short-form caption cues."""
    words = [w for w in words if w[2].strip()]
    if not words:
        return []
    cues: list[Cue] = []
    cur: list[tuple[float, float, str]] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        if not cur:
            return
        start = cur[0][0]
        end = cur[-1][1]
        if end - start < min_duration:
            end = start + min_duration
        text = " ".join(w[2].strip() for w in cur)
        # wrap into at most max_lines
        if len(text) > max_chars * max_lines:
            words_ = text.split()
            half = len(words_) // 2
            text = " ".join(words_[:half]) + "\n" + " ".join(words_[half:])
        cues.append(Cue(start, end, text))
        cur = []
        cur_len = 0

    prev_end: float | None = None
    for start, end, word in words:
        gap = (start - prev_end) if prev_end is not None else 0.0
        would_be = cur_len + 1 + len(word.strip())
        if cur and (gap > max_gap or would_be > max_chars * max_lines or (end - cur[0][0]) > max_duration):
            flush()
        cur.append((start, end, word))
        cur_len = would_be
        prev_end = end
    flush()
    return cues


def transcribe(
    media: str | Path,
    model: str = "base",
    language: str | None = "en",
    beam_size: int = 5,
    vad: bool = True,
) -> list[Cue]:
    """Transcribe media to cues. Needs faster-whisper (or openai-whisper)."""
    ok, which = whisper_available()
    if not ok:
        raise RuntimeError(
            "speech-to-text is not installed. Run:  pip install faster-whisper\n"
            "(first run downloads the model, ~150MB for 'base')"
        )
    log.info("transcribing %s with %s (%s)", Path(media).name, which, model)

    if which == "faster-whisper":
        from faster_whisper import WhisperModel  # type: ignore

        wm = WhisperModel(model, device="auto", compute_type="int8")
        segments, _info = wm.transcribe(
            str(media),
            language=language or None,
            beam_size=beam_size,
            vad_filter=vad,
            word_timestamps=True,
        )
        words: list[tuple[float, float, str]] = []
        fallback: list[Cue] = []
        for seg in segments:
            if getattr(seg, "words", None):
                for w in seg.words:
                    token = (w.word or "").strip()
                    if token:
                        words.append((float(w.start), float(w.end), token))
            text = (seg.text or "").strip()
            if text:
                fallback.append(Cue(float(seg.start), float(seg.end), text))
        cues = _group_words(words) if words else fallback
    else:
        import whisper  # type: ignore

        wm = whisper.load_model(model)
        res = wm.transcribe(str(media), language=language, word_timestamps=True, verbose=False)
        words = [
            (float(w["start"]), float(w["end"]), w["word"].strip())
            for w in (res.get("segments") and [x for s in res["segments"] for x in (s.get("words") or [])] or [])
            if w.get("word", "").strip()
        ]
        if words:
            cues = _group_words(words)
        else:
            cues = [Cue(float(s["start"]), float(s["end"]), s["text"].strip()) for s in res.get("segments", [])]
    log.info("transcribed %d cues", len(cues))
    return cues


# ----------------------------------------------------------------- remapping
def remap_cues_for_segments(cues: Sequence[Cue], segments: Sequence[Segment]) -> list[Cue]:
    """
    Re-time captions for an edit.

    Every cue lives inside exactly one source timeline; we walk the edit
    segments in order, and for each, map [seg.start, seg.end] onto the output
    timeline, shifting/scaling/splitting cues that fall inside it.
    """
    if not segments:
        return []
    out: list[Cue] = []
    cursor = 0.0
    for i, seg in enumerate(segments):
        seg_len = seg.end - seg.start
        if seg_len <= 0:
            continue
        factor = 1.0 / max(0.01, seg.speed)
        seg_out_start = cursor
        seg_out_end = cursor + seg_len * factor
        for cue in cues:
            overlap_start = max(cue.start, seg.start)
            overlap_end = min(cue.end, seg.end)
            if overlap_end - overlap_start < 0.12:
                continue
            new_start = seg_out_start + (overlap_start - seg.start) * factor
            new_end = seg_out_start + (overlap_end - seg.start) * factor
            text = cue.text
            if cue.start < seg.start - 0.05 or cue.end > seg.end + 0.05:
                # partially cut cue: keep the words that survived, proportionally
                ratio = (overlap_end - overlap_start) / max(0.12, cue.duration)
                words = text.split()
                keep = max(1, int(round(len(words) * ratio)))
                text = " ".join(words[:keep])
            if not text.strip():
                continue
            out.append(Cue(new_start, max(new_start + 0.25, new_end), text))
        # crossfade overlaps the next segment by `duration`
        if i < len(segments) - 1:
            cursor = seg_out_end
        else:
            cursor = seg_out_end
    out.sort(key=lambda c: c.start)
    # de-overlap: clamp each cue's start to the previous end
    for i in range(1, len(out)):
        if out[i].start < out[i - 1].end:
            out[i].start = min(out[i - 1].end + 0.02, out[i].end - 0.2)
    return [c for c in out if c.duration > 0.15]


def captions_for_plan(plan: VideoPlan, work_dir: str | Path, transcribe_final: bool = True) -> Path:
    """
    Produce the .srt the render will burn or mux.

    If the plan already has an srt, use it. Otherwise: if there is a single
    segment covering a whole file, transcribe that source; if the edit is more
    complex, transcribe the first source and re-time the cues onto the edit.
    """
    if plan.captions.srt_path and plan.captions.srt_path.exists():
        return plan.captions.srt_path
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    if not plan.segments:
        raise RuntimeError("no segments to caption")

    first = plan.segments[0]
    source = first.path or Path(first.source)
    simple = len(plan.segments) == 1 and abs(first.start) < 0.3

    srt_path = work / (source.stem + (".srt" if simple else ".edited.srt"))
    if srt_path.exists():
        log.info("reusing existing captions %s", srt_path.name)
        return srt_path

    cues = transcribe(source, model=plan.captions.whisper_model, language=plan.captions.language)
    if not simple:
        cues = remap_cues_for_segments(cues, plan.segments)
    if not cues:
        raise RuntimeError("no speech detected - nothing to caption")
    return write_srt(cues, srt_path)
