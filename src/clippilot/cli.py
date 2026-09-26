from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, load_config, write_example_config
from .errors import ClipPilotError
from .utils.logging import get_logger, human_duration, setup_logging

log = get_logger("cli")

BANNER = r"""
   ___ _      _  ___            _     _       _
  / __| |    | |/ __|_ __ _  _ | |_ _| |_  _ (_)__
 | (__| |_ _ | | (__| '_/ _` || |  _| ' \| || / /
  \___|\__,_|_|\___|_| \__,_||_|_| |_||_\_,_||_\_\
       a video+desktop agent for Windows
"""


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def info(msg: str) -> None:
    print(msg)


def ok(msg: str) -> None:
    print(f"  {_c('OK', '38;5;42')}   {msg}")


def warn(msg: str) -> None:
    print(f"  {_c('WARN', '38;5;214')} {msg}")


def bad(msg: str) -> None:
    print(f"  {_c('FAIL', '38;5;203')} {msg}")


def _load(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {}
    if getattr(args, "config", None):
        overrides["__path__"] = args.config
    cfg = load_config(getattr(args, "config", None))
    if getattr(args, "dry_run", False):
        cfg.set("control.dry_run", True)
    if getattr(args, "verbose", False):
        setup_logging("debug")
    if getattr(args, "no_ocr", False):
        cfg.set("perception.ocr_backend", "none")
    if getattr(args, "max_steps", None):
        cfg.set("agent.max_steps", int(args.max_steps))
    if getattr(args, "yes", False):
        cfg.set("safety.require_confirmation", False)
    if getattr(args, "no_confirm", False):
        cfg.set("safety.require_confirmation", False)
    if getattr(args, "monitor", None):
        cfg.set("perception.capture", args.monitor)
    return cfg


# --------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load(args)
    info(BANNER)
    info("")
    info(f"ClipPilot {__version__}")
    info(f"config      : {cfg.path or '<defaults>'}")
    info(f"root        : {cfg.root}")
    info("")

    problems = 0
    info("python")
    ok(f"{sys.version.split()[0]} on {sys.platform}")
    if sys.version_info < (3, 10):
        bad("Python 3.10+ required")
        problems += 1

    info("core packages")
    for mod, pkg in (
        ("mss", "mss"),
        ("PIL", "pillow"),
        ("numpy", "numpy"),
        ("cv2", "opencv-python"),
        ("requests", "requests"),
        ("yaml", "pyyaml"),
    ):
        try:
            m = __import__(mod)
            ok(f"{pkg} {getattr(m, '__version__', '')}".strip())
        except ImportError:
            bad(f"{pkg} missing  ->  pip install {pkg}")
            problems += 1

    info("ffmpeg")
    from .utils.binaries import BinaryKind, find_binary, ffmpeg_features, ffmpeg_version

    try:
        exe = find_binary(BinaryKind.FFMPEG, str(cfg.video.get("ffmpeg", "")))
        ok(f"{ffmpeg_version(exe)}")
        feats = ffmpeg_features(exe)
        if "libass" in feats:
            ok("libass (caption burn-in available)")
        else:
            warn("no libass: burned-in captions unavailable, use soft subtitle tracks")
    except Exception as exc:  # noqa: BLE001
        bad(str(exc).splitlines()[0])
        warn("winget install --id Gyan.FFmpeg -e")
        problems += 1

    info("ocr")
    from .perception.ocr import describe_backends

    for line in describe_backends().splitlines():
        if "ready" in line:
            ok(line.strip())
        else:
            warn(line.strip())
    info("  (the agent still works from the vision model alone, but element targeting is weaker)")

    info("speech to text")
    from .video.captions import whisper_available

    have, which = whisper_available()
    (ok if have else warn)(which)

    info("llm")
    from .agent.llm import LlmClient

    llm = LlmClient(cfg)
    info(f"  endpoint {llm.base_url}")
    info(f"  model    {llm.model}")
    if not cfg.has_api_key:
        warn("no api_key set (fine for Ollama/LM Studio; required for hosted providers)")
    else:
        ping = llm.ping()
        if ping.get("ok"):
            ok(f"reachable, replied {ping.get('reply')!r}")
        else:
            bad(f"unreachable: {ping.get('error')}")
            problems += 1

    info("screen control")
    from .control.fallback import get_backend

    try:
        backend = get_backend(cfg.control.get("backend", "auto"))
        ok(f"input backend: {backend.name}")
    except Exception as exc:  # noqa: BLE001
        bad(str(exc))
        problems += 1
    try:
        from .perception.screen import ScreenCapture

        cap = ScreenCapture()
        shot = cap.grab()
        ok(f"capture ok: {shot.box} ({shot.image.width}x{shot.image.height})")
        cap.close()
    except Exception as exc:  # noqa: BLE001
        bad(f"screen capture failed: {exc}")
        problems += 1

    info("apps")
    from .handoff.recipes import list_recipes

    rd = cfg.resolve_path(cfg.handoff.get("recipes_dir", "recipes"))
    for r in list_recipes(rd):
        info(f"  - {r['key']:<12} {r['name']}")

    info("")
    if problems:
        bad(f"{problems} blocking issue(s). Fix the FAIL lines above.")
        return 1
    ok("all good - you can start the app with:  clippilot-gui")
    return 0


# ------------------------------------------------------------------- snapshot
def cmd_shot(args: argparse.Namespace) -> int:
    from .observation import PerceptionEngine
    from .perception.vlm import VisionModel
    from .utils.imaging import encode_png, save
    from .agent.llm import LlmClient

    cfg = _load(args)
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    vision = VisionModel(LlmClient(cfg), cfg) if cfg.vlm.get("enabled", True) and not args.no_vision else None
    engine = PerceptionEngine(cfg, vision)
    obs = engine.observe(which=args.monitor, save=False)
    if args.overlay:
        from .perception.elements import relabel
        from .utils.geometry import Box
        from .utils.imaging import draw_overlay

        boxes = [(e.box, f"{e.id} {e.text[:24]}", (80, 200, 255)) for e in obs.elements]
        image = draw_overlay(obs.image, boxes, origin=(obs.screen.x1, obs.screen.y1))
    else:
        image = obs.image
    path = save(image, out_dir / f"screen_{int(time.time())}.png")
    print(f"screenshot: {path}")
    print(f"screen     : {obs.screen}  window={obs.active_window!r}")
    print(f"elements   : {len(obs.elements)} (ocr={obs.ocr_backend})")
    if args.json:
        print(json.dumps(obs.describe(), indent=2)[:4000])
    else:
        for e in obs.elements[: int(args.limit)]:
            print(f"  {e.id:<5} {e.kind:<7} {str(e.box.center):<14} {e.confidence:.2f}  {e.text!r}")
    if args.describe and vision and obs.image is not None:
        print("\nvision:", vision.describe(obs.image))
    engine.close()
    return 0


# ----------------------------------------------------------------------- run
def cmd_run(args: argparse.Namespace) -> int:
    from .agent.loop import Agent

    cfg = _load(args)
    task = " ".join(args.task).strip()
    if not task:
        bad("give me a task, e.g. clippilot run \"trim the intro off D:\\clips\\raw.mp4\"")
        return 2

    def on_event(kind: str, payload: dict[str, Any]) -> None:
        if kind == "run_start":
            info(f"\nrun dir: {payload['run_dir']}")
            if payload.get("dry_run"):
                warn("DRY RUN - no real clicks, no real typing")
        elif kind == "observation":
            if args.verbose:
                info(f"  [screen] {payload.get('window')!r} elements={payload.get('element_count')}")
        elif kind == "step":
            a = payload["action"]
            plan = a.get("plan")
            detail = f"plan({len(plan.get('segments', []))} seg -> {plan.get('output')})" if plan else _brief(a)
            print(f"\n{_c('step ' + str(payload.get('index', '?')), '1;36')}  {detail}")
            if payload.get("thought"):
                print(f"   {_c('thought:', '2m')} {payload['thought'][:300]}")
        elif kind == "step_result":
            mark = _c("ok", "38;5;42") if payload.get("ok") else _c("FAILED", "38;5;203")
            print(f"   {mark} {payload.get('message', '')[:400]}")
        elif kind == "progress":
            bar = "#" * int(float(payload.get("pct", 0)) * 30)
            sys.stdout.write(f"\r   [{bar:<30}] {payload.get('pct', 0):.0%} {payload.get('message', '')[:40]}")
            sys.stdout.flush()
        elif kind == "render_done":
            print(f"\n   {_c('render', '1;36')} {payload.get('outputs', {}).get('video')} ({payload.get('elapsed')}s)")
        elif kind == "ask":
            print(f"\n   {_c('AGENT ASKS', '1;33')} {payload.get('question')}")
        elif kind == "error":
            bad(payload.get("message", ""))

    if args.plan:
        from .agent.prompts import build_system_prompt

        cfg.set("agent.vlm_verify", False)
        text = build_system_prompt(cfg, native_tools=True, dry_run=cfg.control.get("dry_run", False))
        (Path(args.plan)).write_text(text, encoding="utf-8")
        ok(f"system prompt written to {args.plan}")
        return 0

    agent = Agent(cfg, on_event=on_event)
    result = agent.run(task)
    print()
    info("=" * 66)
    if result.success:
        ok(f"done in {result.duration:.1f}s")
    else:
        warn(f"incomplete after {result.duration:.1f}s")
    if result.summary:
        info(result.summary)
    info(f"steps: {len(result.steps)}   llm: {json.dumps(result.usage)}")
    if result.transcript_path:
        info(f"transcript: {result.transcript_path}")
    return 0 if result.success else 1


def _brief(a: dict[str, Any]) -> str:
    t = a.get("type")
    keys = {
        "click": lambda: f"click {a.get('text') or a.get('element_id') or (str(a.get('x')) + ',' + str(a.get('y')))}",
        "type": lambda: f"type {str(a.get('text'))[:60]!r}",
        "key": lambda: f"key {a.get('keys')}",
        "wait": lambda: f"wait {a.get('seconds')}s",
        "scroll": lambda: f"scroll {a.get('amount')}",
        "drag": lambda: f"drag -> {a.get('to')}",
        "move": lambda: f"move {a.get('x')},{a.get('y')}",
        "launch_app": lambda: f"launch {a.get('app')}",
        "focus_window": lambda: f"focus {a.get('title_contains')}",
        "list_windows": lambda: "list windows",
        "assert": lambda: f"assert {a.get('text_present') or a.get('statement') or ''}",
        "plan_video": lambda: f"plan {a.get('mode')} {a.get('input')}",
        "handoff": lambda: f"handoff -> {a.get('app')} ({a.get('project')})",
        "ask_human": lambda: f"ask: {str(a.get('question'))[:60]}",
        "task_done": lambda: "finish",
        "give_up": lambda: "give up",
        "observe": lambda: "observe",
        "run_command": lambda: f"run {str(a.get('command'))[:60]}",
    }
    fn = keys.get(str(t))
    return fn() if fn else str(t)


# ----------------------------------------------------------------------- edit
def cmd_edit(args: argparse.Namespace) -> int:
    from .agent.executor import VideoExecutor
    from .handoff.manager import Handoff
    from .video.plan import VideoPlan, build_default_plan, plan_from_json_file

    cfg = _load(args)
    if args.plan:
        plan = plan_from_json_file(args.plan, dict(cfg.video.get("default", {})))
    elif args.highlights:
        plan = None
    else:
        inputs = list(args.input or [])
        if not inputs:
            bad("give me -i input.mp4 (repeatable) or --plan plan.json or --highlights")
            return 2
        keep = None
        if args.trim:
            parts = str(args.trim).split(",")
            if len(parts) == 2:
                keep = [(float(parts[0]), float(parts[1]))]
        plan = build_default_plan(
            inputs,
            args.output or "out.mp4",
            keep=keep,
            resolution=args.resolution,
            fps=args.fps,
            crf=args.crf,
        )
    if plan is None:
        return 2

    if args.output:
        plan.output = args.output
    for flag, value in (("resolution", args.resolution), ("fps", args.fps), ("crf", args.crf)):
        if value:
            setattr(plan, flag, value)
    if args.music:
        plan.audio.music = args.music
        plan.audio.music_gain_db = args.music_gain
        plan.audio.duck = not args.no_duck
    if args.captions:
        plan.captions.enabled = True
        plan.captions.burn = not args.soft_captions
    if args.no_normalize:
        plan.audio.normalize = "none"
    if args.vertical:
        plan.resolution = "1080x1920"
    elif args.horizontal:
        plan.resolution = "1920x1080"
    elif args.square:
        plan.resolution = "1080x1080"
    plan.overwrite = bool(args.force)
    plan.resolve_paths(cfg.root)

    info("plan:")
    for line in plan.describe().splitlines():
        info("  " + line)
    if getattr(args, "dry_run", False) or cfg.control.get("dry_run"):
        from .video.ffmpeg import Ffmpeg

        res = Ffmpeg(str(cfg.video.get("ffmpeg", "")), str(cfg.video.get("ffprobe", ""))).render(plan, dry_run=True)
        info("\ncommand:")
        info("  " + res.command_str)
        return 0

    handoff = Handoff(cfg, None, None)  # type: ignore[arg-type]
    ex = VideoExecutor(
        cfg,
        handoff,
        on_event=lambda k, p: print(f"  [{k}] {json.dumps(p)[:200]}") if args.verbose else None,
        on_progress=lambda pct, msg: sys.stdout.write(f"\r  [{pct*100:5.1f}%] {msg[:50]}") or sys.stdout.flush(),
    )
    try:
        res = ex.render(plan.to_dict())
    except ClipPilotError as exc:
        bad(str(exc))
        return 1
    if not res.ok:
        bad(res.message)
        return 1
    ok(res.message)
    for path in (res.data.get("edl"), res.data.get("fcpxml"), res.data.get("srt")):
        if path:
            info(f"  {path}")
    return 0


# ------------------------------------------------------------------ highlights
def cmd_highlights(args: argparse.Namespace) -> int:
    from .video.ffmpeg import Ffmpeg
    from .video.highlights import analyze
    from .video.probe import probe

    cfg = _load(args)
    path = Path(args.input).expanduser()
    info = probe(path)
    print(info.summary())
    ff = Ffmpeg(str(cfg.video.get("ffmpeg", "")), str(cfg.video.get("ffprobe", "")))
    h = cfg.video.get("highlights", {})
    an = analyze(
        ff.exe, path, info.duration,
        window=float(args.window or h.get("window", 2.0)),
        hop=float(h.get("hop", 0.5)),
        silence_db=float(h.get("silence_db", -32.0)),
        min_silence=float(h.get("min_silence", 0.35)),
        pad=float(h.get("pad", 0.25)),
        top_n=int(args.top or h.get("top_n", 5)),
        max_seconds=args.max_seconds,
    )
    print()
    print(an.summary())
    if args.json:
        print(json.dumps(an.to_dict(), indent=2))
    if args.emit_plan:
        segs = [r.as_segment(str(path.resolve())) for r in (an.highlights if args.mode == "highlights" else an.keep_ranges)]
        plan = {"segments": segs, "output": str(Path(args.emit_plan))}
        Path(args.emit_plan).write_text(json.dumps(plan, indent=2), encoding="utf-8")
        print(f"\nplan written to {args.emit_plan}")
    return 0


# --------------------------------------------------------------------- probe
def cmd_probe(args: argparse.Namespace) -> int:
    from .video.probe import describe_media, probe

    if args.json:
        print(json.dumps([probe(p).to_dict() for p in args.input], indent=2))
    else:
        print(describe_media(args.input))
    return 0


# ------------------------------------------------------------------ captions
def cmd_captions(args: argparse.Namespace) -> int:
    from .video.captions import read_srt, transcribe, write_srt

    if args.input.lower().endswith(".srt"):
        cues = read_srt(args.input)
    else:
        cues = transcribe(args.input, model=args.model, language=args.language)
    out = args.output or str(Path(args.input).with_suffix(".srt"))
    write_srt(cues, out)
    ok(f"{len(cues)} cues -> {out}")
    return 0


# ------------------------------------------------------------------- handoff
def cmd_handoff(args: argparse.Namespace) -> int:
    from .agent.loop import Agent

    cfg = _load(args)
    agent = Agent(cfg, on_event=lambda k, p: None)
    res = agent.handoff.run(args.app, args.project, notes=args.notes or "")
    (ok if res.ok else bad)(res.message)
    for step in res.data.get("steps", []):
        mark = _c("ok", "38;5;42") if step["ok"] else _c("--", "38;5;240")
        print(f"   {mark} {step['action'].get('type')}: {step['message'][:100]}")
    return 0 if res.ok else 1


# --------------------------------------------------------------------- apps
def cmd_apps(args: argparse.Namespace) -> int:
    from .handoff.recipes import TEMPLATE, get_recipe, list_recipes
    from .control import win32

    cfg = _load(args)
    rd = cfg.resolve_path(cfg.handoff.get("recipes_dir", "recipes"))
    if args.template:
        p = rd / "_template.yaml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(TEMPLATE, encoding="utf-8")
        ok(f"wrote {p}")
        return 0
    if args.check:
        for key in args.check:
            r = get_recipe(key, rd)
            if not r:
                bad(f"no recipe for {key}")
                continue
            print(f"{r.name}:")
            try:
                path = win32.resolve_app_path(r.app)
                ok(f"executable found: {path}")
            except Exception as exc:  # noqa: BLE001
                warn(f"executable not found ({exc}) - install the app or edit the recipe")
        return 0
    for r in list_recipes(rd):
        print(f"  {r['key']:<12} {r['name']}")
        print(f"               {r['description']}")
        if args.verbose:
            for s in r["steps"]:
                flag = " (optional)" if s.get("_optional") else ""
                print(f"                 - {s.get('type')}{flag}")
    return 0


# -------------------------------------------------------------------- replay
def cmd_replay(args: argparse.Namespace) -> int:
    from .control.actuator import Actuator
    from .observation import PerceptionEngine
    from .perception.vlm import VisionModel
    from .agent.llm import LlmClient
    from .recorder import Recorder

    cfg = _load(args)
    if args.dry_run:
        cfg.set("control.dry_run", True)
    episode = Path(args.episode)
    if episode.is_dir():
        episode = episode / "episode.jsonl"
    rec = Recorder(episode)
    entries = rec.read()
    if not entries:
        bad(f"no recorded actions in {episode}")
        return 1
    engine = PerceptionEngine(cfg, VisionModel(LlmClient(cfg), cfg) if cfg.vlm.get("enabled") else None)
    act = Actuator(cfg, engine)

    def execute(action: dict[str, Any]):
        if args.also_render and action.get("type") == "edit_video":
            from .agent.executor import VideoExecutor
            from .handoff.manager import Handoff

            ex = VideoExecutor(cfg, Handoff(cfg, engine, act))
            return ex.render(action.get("plan") or {})
        return act.execute(action)

    count = 0
    for r in rec.replay(execute, only=args.only, skip_failed=not args.include_failed):
        count += 1
        mark = _c("ok", "38;5;42") if r["replay_ok"] else _c("FAILED", "38;5;203")
        print(f"  step {r['step']:<4} {r['action']:<12} {mark} {r['replay_message'][:90]}")
    ok(f"replayed {count} action(s)")
    return 0


# ---------------------------------------------------------------------- init
def cmd_init(args: argparse.Namespace) -> int:
    p = write_example_config(Path(args.path).expanduser(), overwrite=args.force)
    ok(f"wrote {p}")
    info("set llm.api_key (or the OPENAI_API_KEY env var), then run: clippilot doctor")
    return 0


# ----------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="clippilot",
        description="ClipPilot - edit video with ffmpeg, drive Windows apps by screen control.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  clippilot doctor\n"
            "  clippilot-gui\n"
            '  clippilot run "cut the first 12s off D:\\raw.mp4, add captions, make it vertical"\n'
            "  clippilot edit -i D:\\raw.mp4 --trim 12,95 --vertical --captions -o D:\\out.mp4\n"
            "  clippilot highlights D:\\podcast.mp4 --top 5 --emit-plan plan.json\n"
            "  clippilot handoff --app capcut --project D:\\out.mp4\n"
        ),
    )
    ap.add_argument("--version", action="version", version=f"ClipPilot {__version__}")
    ap.add_argument("-c", "--config", help="path to config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="log actions without touching anything")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check the environment and dependencies")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("shot", help="screenshot + OCR element dump (debugging)")
    s.add_argument("-o", "--out", default="shots")
    s.add_argument("--monitor", help="virtual | primary | monitor_2")
    s.add_argument("--overlay", action="store_true", help="draw element boxes on the image")
    s.add_argument("--describe", action="store_true", help="ask the vision model to narrate the screen")
    s.add_argument("--no-vision", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(func=cmd_shot)

    r = sub.add_parser("run", help="let the agent do a task (video + screen control)")
    r.add_argument("task", nargs="+")
    r.add_argument("--max-steps", type=int)
    r.add_argument("--monitor")
    r.add_argument("--no-ocr", action="store_true")
    r.add_argument("--yes", action="store_true", help="skip approval prompts (NOT recommended)")
    r.add_argument("--no-confirm", action="store_true", help="do not require confirmation for risky actions")
    r.add_argument("--plan", help="write the system prompt to a file and exit")
    r.set_defaults(func=cmd_run)

    e = sub.add_parser("edit", help="render a video from a plan (or straight flags)")
    e.add_argument("-i", "--input", action="append", help="input file (repeatable)")
    e.add_argument("-p", "--plan", help="plan json file")
    e.add_argument("-o", "--output")
    e.add_argument("--trim", help="keep range for the first input, e.g. 12,95")
    e.add_argument("--resolution", help="e.g. 1080x1920")
    e.add_argument("--fps", type=int)
    e.add_argument("--crf", type=int, help="quality: lower = better (18 = good, 23 = default)")
    e.add_argument("--vertical", action="store_true", help="1080x1920")
    e.add_argument("--horizontal", action="store_true", help="1920x1080")
    e.add_argument("--square", action="store_true", help="1080x1080")
    e.add_argument("--music", help="background music file")
    e.add_argument("--music-gain", type=float, default=-18.0, help="dB")
    e.add_argument("--no-duck", action="store_true", help="do not auto-duck music under speech")
    e.add_argument("--no-normalize", action="store_true")
    e.add_argument("--captions", action="store_true", help="auto-transcribe and burn in")
    e.add_argument("--soft-captions", action="store_true", help="add as a subtitle track instead of burning in")
    e.add_argument("-f", "--force", action="store_true", help="overwrite the output")
    e.add_argument("--highlights", action="store_true", help="auto-pick highlights (needs -i)")
    e.add_argument("--dry-run", action="store_true", help="print the ffmpeg command instead of running it")
    e.set_defaults(func=cmd_edit)

    hl = sub.add_parser("highlights", help="find the good bits / the dead air")
    hl.add_argument("input")
    hl.add_argument("--top", type=int, help="how many highlight windows")
    hl.add_argument("--window", type=float, help="analysis window seconds")
    hl.add_argument("--max-seconds", type=float)
    hl.add_argument("--mode", choices=["highlights", "keep"], default="highlights")
    hl.add_argument("--emit-plan", help="write a plan json to this path")
    hl.add_argument("--json", action="store_true")
    hl.set_defaults(func=cmd_highlights)

    p = sub.add_parser("probe", help="show media info")
    p.add_argument("input", nargs="+")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_probe)

    c = sub.add_parser("captions", help="transcribe to .srt")
    c.add_argument("input")
    c.add_argument("-o", "--output")
    c.add_argument("--model", default="base", help="tiny | base | small | medium | large-v3")
    c.add_argument("--language", default="en")
    c.set_defaults(func=cmd_captions)

    h = sub.add_parser("handoff", help="open a file in a real editor and import it")
    h.add_argument("--app", required=True)
    h.add_argument("--project", required=True)
    h.add_argument("--notes")
    h.set_defaults(func=cmd_handoff)

    a = sub.add_parser("apps", help="list handoff recipes")
    a.add_argument("--template", action="store_true", help="write a template recipe file")
    a.add_argument("--check", action="append", help="verify the executable is installed")
    a.set_defaults(func=cmd_apps)

    rp = sub.add_parser("replay", help="re-run a recorded episode")
    rp.add_argument("episode", help="run dir or episode.jsonl")
    rp.add_argument("--only", help="replay only this action type")
    rp.add_argument("--include-failed", action="store_true")
    rp.add_argument("--also-render", action="store_true", help="also re-run edit_video actions")
    rp.set_defaults(func=cmd_replay)

    i = sub.add_parser("init", help="write a config.yaml")
    i.add_argument("path", nargs="?", default="config.yaml")
    i.add_argument("-f", "--force", action="store_true")
    i.set_defaults(func=cmd_init)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    setup_logging("debug" if args.verbose else "info")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        warn("interrupted")
        return 130
    except ClipPilotError as exc:
        bad(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("unhandled error")
        bad(f"{type(exc).__name__}: {exc}")
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
