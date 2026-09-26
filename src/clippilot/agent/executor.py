from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from ..config import Config
from ..errors import ActionError, PlanError, RenderError
from ..handoff.manager import Handoff
from ..types import ActionResult, Observation
from ..utils.logging import get_logger
from ..video.captions import captions_for_plan
from ..video.ffmpeg import Ffmpeg
from ..video.highlights import analyze
from ..video.plan import VideoPlan
from ..video.probe import probe
from ..video.projects import export_all

log = get_logger("agent.executor")

Event = Callable[[str, dict[str, Any]], None]
Progress = Callable[[float, str], None]


class VideoExecutor:
    """
    Handles the actions the GUI cannot do well: renders, analysis, handoff.

    Kept separate from the agent loop so the same engine can be used headless
    from the CLI, the API, or the desktop app.
    """

    def __init__(
        self,
        config: Config,
        handoff: Handoff,
        on_event: Event | None = None,
        on_progress: Progress | None = None,
    ) -> None:
        self.cfg = config
        self.handoff = handoff
        self.on_event = on_event
        self.on_progress = on_progress
        self._ffmpeg: Ffmpeg | None = None
        self.last_render: dict[str, Any] | None = None

    @property
    def ffmpeg(self) -> Ffmpeg:
        if self._ffmpeg is None:
            self._ffmpeg = Ffmpeg(
                ffmpeg=str(self.cfg.video.get("ffmpeg", "") or ""),
                ffprobe=str(self.cfg.video.get("ffprobe", "") or ""),
            )
        return self._ffmpeg

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def work_dir(self) -> Path:
        wd = self.cfg.resolve_path(self.cfg.video.get("work_dir", ".work"))
        wd.mkdir(parents=True, exist_ok=True)
        return wd

    # ------------------------------------------------------------------ render
    def render(
        self,
        plan_data: dict[str, Any],
        dry_run: bool = False,
        export_interchange: bool = True,
    ) -> ActionResult:
        defaults = dict(self.cfg.video.get("default", {}))
        defaults["captions"] = dict(self.cfg.video.get("captions", {}))
        try:
            plan = VideoPlan.from_dict(plan_data, defaults)
            plan.resolve_paths(self.cfg.root)
        except PlanError as exc:
            return ActionResult(ok=False, message=f"invalid edit plan: {exc}")

        if plan.output_exists and not plan.overwrite:
            return ActionResult(
                ok=False,
                message=(
                    f"{plan.output_path} already exists. Set overwrite=true (or ask the user) if replacing it is intended."
                ),
            )

        notes: list[str] = []
        try:
            if plan.captions.enabled and not plan.captions.srt_path:
                self._emit("progress", {"phase": "transcribing", "message": "generating captions"})
                srt = captions_for_plan(plan, self.work_dir())
                plan.captions.srt_path = srt
                notes.append(f"captions from {srt.name}")
        except Exception as exc:  # noqa: BLE001
            return ActionResult(ok=False, message=f"caption step failed: {exc}")

        self._emit(
            "render_start",
            {"plan": plan.to_dict(), "describe": plan.describe(), "output": str(plan.output_path)},
        )
        try:
            result = self.ffmpeg.render(plan, progress=self.on_progress, dry_run=dry_run)
        except RenderError as exc:
            self._emit("render_error", {"message": str(exc)})
            return ActionResult(ok=False, message=str(exc))

        notes.extend(result.notes)
        outputs: dict[str, Any] = {"video": str(result.output)}
        if not dry_run and export_interchange:
            try:
                extras = export_all(plan, self.work_dir())
                outputs.update({k: (str(v) if v else None) for k, v in extras.items()})
                notes.append("project files: " + ", ".join(p.name for p in extras.values() if p))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"interchange export skipped ({exc})")

        if plan.captions.srt_path:
            outputs["srt"] = str(plan.captions.srt_path)

        try:
            info = probe(result.output)
            outputs["info"] = info.to_dict()
            size_line = f"{info.width}x{info.height} @{info.fps:.2f}fps, {info.duration:.1f}s"
        except Exception as exc:  # noqa: BLE001
            size_line = f"(probe failed: {exc})"

        message = (
            f"rendered {result.output.name} in {result.elapsed:.1f}s - {size_line}"
            + (f"\nffmpeg: {result.command_str}" if dry_run else "")
        )
        self.last_render = outputs
        self._emit("render_done", {"outputs": outputs, "elapsed": result.elapsed, "notes": notes})
        return ActionResult(ok=True, message=message, data={**outputs, "notes": notes, "command": result.command_str})

    # ----------------------------------------------------------------- analyse
    def analyse(self, mode: str, path: str, top_n: int | None = None, max_seconds: float | None = None,
                min_silence: float | None = None) -> ActionResult:
        p = Path(path).expanduser()
        if not p.exists():
            return ActionResult(ok=False, message=f"file not found: {p}")
        try:
            info = probe(p)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(ok=False, message=f"cannot read {p.name}: {exc}")
        if mode == "info":
            return ActionResult(ok=True, message=info.summary(), data=info.to_dict())

        h = self.cfg.video.get("highlights", {})
        analysis = analyze(
            self.ffmpeg.exe,
            p,
            info.duration,
            window=float(h.get("window", 2.0)),
            hop=float(h.get("hop", 0.5)),
            silence_db=float(h.get("silence_db", -32.0)),
            min_silence=float(min_silence if min_silence is not None else h.get("min_silence", 0.35)),
            pad=float(h.get("pad", 0.25)),
            top_n=int(top_n or h.get("top_n", 5)),
            max_seconds=max_seconds,
        )
        ranges = analysis.highlights if mode == "highlights" else analysis.keep_ranges
        segs = [r.as_segment(str(p.resolve())) for r in ranges]
        self._emit("analysis", {"mode": mode, "data": analysis.to_dict()})
        if not segs:
            return ActionResult(
                ok=False,
                message="nothing detected - the audio looks silent throughout; try lowering silence_db",
            )
        message = (
            f"{p.name} ({info.duration:.1f}s) -> {len(segs)} usable range(s): "
            + ", ".join(f"{s['start']:.1f}-{s['end']:.1f}" for s in segs[:10])
            + "\nUse these as plan.segments to render."
        )
        return ActionResult(ok=True, message=message, data={"segments": segs, "analysis": analysis.to_dict()})

    # ----------------------------------------------------------------- handoff
    def handoff(self, app: str, project: str, notes: str = "", overwrite: bool = False) -> ActionResult:
        try:
            return self.handoff_manager_run(app, project, notes, overwrite)
        except ActionError as exc:
            return ActionResult(ok=False, message=str(exc))

    def handoff_manager_run(self, app: str, project: str, notes: str = "", overwrite: bool = False) -> ActionResult:
        return self.handoff.run(app, project, notes=notes, overwrite=overwrite)


def build_plan_from_segments(
    segments: list[dict[str, Any]],
    output: str,
    config: Config,
    **overrides: Any,
) -> dict[str, Any]:
    defaults = dict(config.video.get("default", {}))
    plan: dict[str, Any] = {
        "segments": segments,
        "output": output,
        "resolution": defaults.get("resolution", "1080x1920"),
        "fps": defaults.get("fps", 30),
    }
    plan.update({k: v for k, v in overrides.items() if v is not None})
    return plan


def guess_output(input_path: str, suffix: str = "_clippilot") -> str:
    p = Path(input_path).expanduser()
    return str(p.with_name(p.stem + suffix + p.suffix))


def elapsed_note(t0: float) -> str:
    return f"{time.perf_counter() - t0:.1f}s"


def obs_summary(obs: Observation) -> str:
    return f"{len(obs.elements)} elements on {obs.screen}"
