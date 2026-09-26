from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import BinaryKind, RenderError
from ..utils.binaries import ffmpeg_features, ffmpeg_version, find_binary
from ..utils.logging import get_logger
from .plan import VideoPlan

log = get_logger("video.ffmpeg")

Progress = Callable[[float, str], None]

_TIME_RE = re.compile(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def escape_filter_path(path: str | Path) -> str:
    """Make a path safe to sit inside an ffmpeg filter option value."""
    s = str(path).replace("\\", "/")
    s = s.replace("'", r"\'").replace(":", r"\:").replace("[", r"\[").replace("]", r"\]")
    return s


def atempo_chain(speed: float) -> list[float]:
    """atempo only accepts 0.5..2.0, so chain factors for extremes."""
    factors: list[float] = []
    remaining = float(speed)
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5 - 1e-9
    factors.append(round(remaining, 4))
    return factors


@dataclass
class RenderResult:
    output: Path
    ok: bool
    command: list[str] = field(default_factory=list)
    returncode: int = 0
    elapsed: float = 0.0
    stderr_tail: str = ""
    notes: list[str] = field(default_factory=list)
    progress: list[float] = field(default_factory=list)

    @property
    def command_str(self) -> str:
        def q(part: str) -> str:
            return f'"{part}"' if (" " in part or "\t" in part) else part
        return " ".join(q(p) for p in self.command)


class Ffmpeg:
    """Thin, honest wrapper: builds one filter_complex and runs it."""

    def __init__(self, ffmpeg: str = "", ffprobe: str = "") -> None:
        self.exe = find_binary(BinaryKind.FFMPEG, ffmpeg)
        self.ffprobe = find_binary(BinaryKind.FFPROBE, ffprobe)
        self.version = ffmpeg_version(self.exe)
        self.features = ffmpeg_features(self.exe)
        self._supports_stats = "stats_period" in self.version or "ffmpeg version 5" in self.version or "ffmpeg version 6" in self.version or "ffmpeg version 7" in self.version

    # ------------------------------------------------------------------ build
    def build(self, plan: VideoPlan) -> tuple[list[str], list[str]]:
        """Return (pre_input_args, filter_complex) for a plan."""
        if not plan.segments:
            raise RenderError("nothing to render: plan has no segments")
        for seg in plan.segments:
            if seg.path is None:
                raise RenderError(f"source not resolved: {seg.source}")

        W, H, FPS = plan.width, plan.height, plan.fps
        pre: list[str] = []
        graph: list[str] = []
        notes: list[str] = []

        seg_video: list[str] = []
        seg_audio: list[str] = []
        input_index = 0

        for i, seg in enumerate(plan.segments):
            raw_len = max(0.05, seg.end - seg.start)
            pre += ["-ss", f"{seg.start:.3f}", "-t", f"{raw_len:.3f}", "-i", str(seg.path)]
            v_idx = input_index
            input_index += 1

            # ---------------- video chain
            chain = [f"[{v_idx}:v]setpts=PTS-STARTPTS"]
            if seg.speed != 1.0:
                chain[0] = f"[{v_idx}:v]setpts=(PTS-STARTPTS)/{seg.speed:.5f}"
            if plan.aspect == "stretch":
                chain.append(f"scale={W}:{H}")
            elif plan.aspect == "fit":
                chain.append(f"scale={W}:{H}:force_original_aspect_ratio=decrease")
                chain.append(f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black")
            elif plan.pad_blur:
                chain.append(f"scale={W}:{H}:force_original_aspect_ratio=increase")
                chain.append(f"crop={W}:{H}")
                chain.append(f"split=2[bg{i}][fg{i}]")
                graph.append(",".join(chain))
                graph.append(f"[bg{i}]gblur=sigma=22:steps=2[bgb{i}]")
                graph.append(f"[fg{i}]scale={W}:{H}:force_original_aspect_ratio=decrease[fgs{i}]")
                graph.append(
                    f"[bgb{i}][fgs{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={FPS},format=yuv420p[v{i}]"
                )
                notes.append(f"segment {i}: blurred-pad letterbox to {W}x{H}")
                chain = []
            else:
                chain.append(f"scale={W}:{H}:force_original_aspect_ratio=increase")
                chain.append(f"crop={W}:{H}")
            if chain:
                chain += [f"setsar=1", f"fps={FPS}", "format=yuv420p", f"[v{i}]"]
                graph.append(",".join(chain))
            seg_video.append(f"[v{i}]")

            # ---------------- audio chain
            if seg.has_audio:
                a_filters = [
                    "aresample=48000",
                    "asetpts=N/SR/TB",
                    "aformat=sample_fmts=fltp:channel_layouts=stereo",
                ]
                if seg.speed != 1.0:
                    a_filters += [f"atempo={f}" for f in atempo_chain(seg.speed)]
                graph.append(f"[{v_idx}:a]{','.join(a_filters)}[a{i}]")
                seg_audio.append(f"[a{i}]")
            else:
                pre += [
                    "-f", "lavfi",
                    "-t", f"{raw_len / max(0.01, seg.speed):.3f}",
                    "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                ]
                s_idx = input_index
                input_index += 1
                a_filters = [
                    "aresample=48000",
                    "asetpts=N/SR/TB",
                    "aformat=sample_fmts=fltp:channel_layouts=stereo",
                ]
                if seg.speed != 1.0:
                    a_filters += [f"atempo={f}" for f in atempo_chain(seg.speed)]
                graph.append(f"[{s_idx}:a]{','.join(a_filters)}[a{i}]")
                seg_audio.append(f"[a{i}]")
                notes.append(f"segment {i}: source has no audio, inserted silence")

        total = plan.total_duration
        xfades = plan.transition_durations

        # ---------------- concatenate
        if len(seg_video) == 1:
            graph.append(f"{seg_video[0]}copy[vcat]")
            graph.append(f"{seg_audio[0]}anull[acat]" if seg_audio else "anullsrc=r=48000:cl=stereo[acat]")
        elif not plan.has_xfades:
            pairs = "".join(f"{v}{a}" for v, a in zip(seg_video, seg_audio))
            graph.append(f"{pairs}concat=n={len(seg_video)}:v=1:a=1[vcat][acat]")
        else:
            cur_v, cur_a = "v0", "a0"
            acc = plan.segments[0].length
            for i in range(1, len(seg_video)):
                d = xfades[i - 1]
                tr = plan.transitions[i - 1].type if i - 1 < len(plan.transitions) else "fade"
                if tr == "cut":
                    tr = "fade"
                offset = max(0.0, acc - d)
                nxt_v, nxt_a = f"vx{i}", f"ax{i}"
                graph.append(
                    f"[{cur_v}][v{i}]xfade=transition={tr}:duration={d:.3f}:offset={offset:.3f}[{nxt_v}]"
                )
                graph.append(
                    f"[{cur_a}][a{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{nxt_a}]"
                )
                acc = acc + plan.segments[i].length - d
                cur_v, cur_a = nxt_v, nxt_a
            graph.append(f"[{cur_v}]copy[vcat]")
            graph.append(f"[{cur_a}]anull[acat]")

        audio_label = "[acat]"

        # ---------------- music bed
        if plan.audio.music and plan.audio.music_path:
            if plan.loop_music:
                pre += ["-stream_loop", "-1", "-i", str(plan.audio.music_path)]
            else:
                pre += ["-i", str(plan.audio.music_path)]
            m_idx = input_index
            input_index += 1
            m = plan.audio
            m_filters = [
                f"atrim=0:{max(0.1, total):.3f}" if total > 0 else "anull",
                "asetpts=N/SR/TB",
                "aresample=48000",
                f"volume={m.music_gain_db}dB",
                "aformat=sample_fmts=fltp:channel_layouts=stereo",
                "afade=t=in:st=0:d=0.8",
            ]
            if total > 1.5:
                m_filters.append(f"afade=t=out:st={max(0.0, total - 1.4):.3f}:d=1.4")
            graph.append(f"[{m_idx}:a]{','.join(m_filters)}[music0]")
            if m.duck:
                graph.append("[music0]asplit=2[msc][mmus]")
                graph.append(
                    f"{audio_label}[msc]sidechaincompress=threshold=0.02:ratio=14:attack=8:release=400[ducked]"
                )
                graph.append(
                    "[ducked][mmus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[amixed]"
                )
                notes.append("music ducked under speech with sidechain compression")
            else:
                graph.append(
                    f"{audio_label}[music0]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[amixed]"
                )
            audio_label = "[amixed]"

        # ---------------- final loudness
        norm = plan.audio.normalize
        if norm == "loudnorm":
            graph.append(
                f"{audio_label}loudnorm=I={plan.audio.target_lufs:.1f}:TP=-1.5:LRA=11[afinal]"
            )
            notes.append(f"loudness normalised to {plan.audio.target_lufs:.1f} LUFS")
        elif norm == "peak":
            graph.append(f"{audio_label}alimiter=level_in=1:level_out=1:limit=0.891:attack=5:release=60[afinal]")
        else:
            graph.append(f"{audio_label}anull[afinal]")

        # ---------------- captions
        video_label = "[vcat]"
        if plan.captions.enabled and plan.captions.srt_path and plan.captions.burn:
            srt = escape_filter_path(plan.captions.srt_path)
            style = plan.captions.style or ""
            if plan.captions.position != "bottom":
                style = _force_alignment(style, plan.captions.position)
            force = f":force_style='{style}'" if style else ""
            graph.append(f"{video_label}subtitles=filename='{srt}'{force},format=yuv420p[vfinal]")
            notes.append(f"captions burned in from {plan.captions.srt_path.name}")
        else:
            graph.append(f"{video_label}format=yuv420p[vfinal]")

        return pre, graph

    # ------------------------------------------------------------------ render
    def render(
        self,
        plan: VideoPlan,
        progress: Progress | None = None,
        dry_run: bool = False,
        extra_output_args: list[str] | None = None,
    ) -> RenderResult:
        pre, graph = self.build(plan)
        out = plan.output_path or Path(plan.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)

        soft_subs: list[str] = []
        if plan.captions.enabled and plan.captions.srt_path and not plan.captions.burn:
            pre += ["-i", str(plan.captions.srt_path)]
            sub_idx = sum(1 for a in pre if a == "-i")
            soft_subs = [f"-map", f"{sub_idx}:0", "-c:s", "mov_text" if plan.format == "mp4" else "subrip"]

        args: list[str] = ["-hide_banner", "-y" if plan.overwrite else "-n", "-loglevel", "warning"]
        if self._supports_stats:
            args += ["-stats", "-stats_period", "0.3"]
        args += pre
        if graph:
            args += ["-filter_complex", ";".join(graph)]
        args += ["-map", "[vfinal]", "-map", "[afinal]"]
        args += soft_subs
        args += [
            "-c:v", "libx264",
            "-preset", plan.preset,
            "-crf", str(plan.crf),
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-c:a", "aac",
            "-b:a", plan.audio_bitrate,
            "-ar", "48000",
            "-movflags", "+faststart",
        ]
        if plan.total_duration > 0:
            args += ["-t", f"{plan.total_duration + 0.5:.3f}"]
        args += extra_output_args or []
        args.append(str(out))

        result = RenderResult(output=out, ok=False, command=[self.exe, *args])
        if dry_run:
            result.ok = True
            return result

        total = plan.total_duration or 0.0
        t0 = time.perf_counter()
        tail: list[str] = []
        proc = subprocess.Popen(
            [self.exe, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
        )
        lock = threading.Lock()

        def pump() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.rstrip()
                tail.append(line)
                del tail[:-60]
                if progress:
                    m = _TIME_RE.search(line)
                    if m and total > 0:
                        done = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                        pct = max(0.0, min(1.0, done / total))
                        with lock:
                            result.progress.append(pct)
                        progress(pct, f"encoding {done:.1f}s / {total:.1f}s")

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            thread.join(timeout=2)
            raise
        thread.join(timeout=3)
        result.elapsed = time.perf_counter() - t0
        result.returncode = proc.returncode or 0
        result.stderr_tail = "\n".join(tail)
        result.ok = result.returncode == 0 and out.exists()
        if not result.ok:
            raise RenderError(
                f"ffmpeg failed (exit {result.returncode}) for {out.name}\n{result.stderr_tail[-1500:]}"
            )
        if not out.exists() or out.stat().st_size == 0:
            raise RenderError(f"ffmpeg produced no output for {out.name}")
        return result

    # ------------------------------------------------------------------ extras
    def extract_frame(self, src: str | Path, at: float, dest: str | Path, width: int = 1280) -> Path:
        dest_p = Path(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [self.exe, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0.0, at):.3f}",
             "-i", str(src), "-frames:v", "1", "-vf", f"scale={width}:-2", str(dest_p)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return dest_p

    def concat_demuxer(self, files: list[str | Path], dest: str | Path) -> Path:
        """Stream-copy join (no re-encode) - fast, but needs matching codecs."""
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        listfile = out.with_suffix(".txt")
        listfile.write_text(
            "\n".join(f"file '{Path(f).resolve().as_posix()}'" for f in files) + "\n",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [self.exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
             "-i", str(listfile), "-c", "copy", str(out)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if proc.returncode != 0:
            raise RenderError(f"concat demuxer failed: {proc.stderr[-800:]}")
        return out

    def waveform_png(self, src: str | Path, dest: str | Path, width: int = 1200, color: str = "0x4cc9f0") -> Path:
        dest_p = Path(dest)
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [self.exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-filter_complex", f"showwavespic=s={width}x220:colors={color}", "-frames:v", "1", str(dest_p)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        return dest_p


def _force_alignment(style: str, position: str) -> str:
    align = {"top": "8", "center": "5", "bottom": "2"}[position]
    margin = {"top": "MarginV=140", "center": "", "bottom": "MarginV=90"}[position]
    out = style
    if "Alignment=" in out:
        out = re.sub(r"Alignment=\d+", f"Alignment={align}", out)
    else:
        out = (out + "," if out else "") + f"Alignment={align}"
    if margin:
        if "MarginV=" in out:
            out = re.sub(r"MarginV=\d+", margin, out)
        else:
            out += "," + margin
    return out


def installed() -> dict[str, Any]:
    try:
        exe = find_binary(BinaryKind.FFMPEG)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)}
    return {
        "available": True,
        "path": exe,
        "version": ffmpeg_version(exe),
        "features": sorted(ffmpeg_features(exe)),
    }
