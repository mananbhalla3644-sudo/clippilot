from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Config, load_config
from ..errors import ClipPilotError
from ..handoff.recipes import list_recipes
from ..utils.imaging import encode_jpeg
from ..utils.logging import get_logger

log = get_logger("gui.server")

WEB_DIR = Path(__file__).resolve().parent / "web"


# ------------------------------------------------------------------ models
class TaskRequest(BaseModel):
    task: str = Field(min_length=1)
    dry_run: bool = False
    require_confirmation: bool = True
    vlm_verify: bool = False
    max_steps: int = 40
    monitor: str = "virtual"
    handoff_app: str = ""


class EditRequest(BaseModel):
    inputs: list[str] = Field(default_factory=list)
    output: str = ""
    plan: dict[str, Any] | None = None
    mode: str = "highlights"
    resolution: str = "1080x1920"
    fps: int = 30
    crf: int = 20
    transitions: str = "cut"
    music: str = ""
    music_gain_db: float = -18.0
    duck: bool = True
    captions: bool = False
    burn_captions: bool = True
    normalize: str = "loudnorm"
    top_n: int = 5
    max_seconds: float = 0.0
    overwrite: bool = True
    also_handoff: bool = False
    handoff_app: str = "capcut"


class HandoffRequest(BaseModel):
    app: str
    project: str
    notes: str = ""


class ConfirmRequest(BaseModel):
    approved: bool


class AskRequest(BaseModel):
    answer: str


class SettingsRequest(BaseModel):
    dry_run: bool | None = None
    require_confirmation: bool | None = None
    vlm_verify: bool | None = None
    max_steps: int | None = None
    monitor: str | None = None
    default_app: str | None = None
    ocr_backend: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None


# ----------------------------------------------------------------- session
@dataclass
class Session:
    """One long-lived agent plus the event stream the UI renders."""

    id: str
    config: Config
    agent: Any = None
    engine: Any = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list["queue.Queue[dict[str, Any]]"] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    busy: bool = False
    stop_flag: threading.Event = field(default_factory=threading.Event)
    pending_confirm: dict[str, Any] | None = None
    confirm_event: threading.Event = field(default_factory=threading.Event)
    confirm_result: bool = False
    pending_ask: dict[str, Any] | None = None
    ask_event: threading.Event = field(default_factory=threading.Event)
    ask_result: str = ""
    last_elements: list[dict[str, Any]] = field(default_factory=list)
    last_observation: dict[str, Any] = field(default_factory=dict)
    run_dir: str = ""
    last_render: dict[str, Any] = field(default_factory=dict)
    log_lines: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        event = {"kind": kind, "at": time.time(), **payload}
        self.events.append(event)
        if len(self.events) > 5000:
            del self.events[:2000]
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def log(self, text: str, level: str = "info") -> None:
        line = {"at": time.time(), "level": level, "text": text}
        self.log_lines.append(line)
        if len(self.log_lines) > 800:
            del self.log_lines[:400]
        self.emit("log", line)

    # ---- human-in-the-loop bridges (the agent thread blocks on these) ----
    def approver(self, action: dict[str, Any], reason: str) -> bool:
        self.pending_confirm = {
            "action": {k: v for k, v in action.items() if k != "plan"},
            "reason": reason,
        }
        self.confirm_result = False
        self.confirm_event.clear()
        self.emit("confirm", self.pending_confirm)
        while not self.confirm_event.wait(timeout=0.4):
            if self.stop_flag.is_set():
                self.pending_confirm = None
                return False
        self.pending_confirm = None
        return self.confirm_result

    def asker(self, question: str, options: list[str], why: str) -> str:
        self.pending_ask = {"question": question, "options": options, "why": why}
        self.ask_result = ""
        self.ask_event.clear()
        self.emit("ask", self.pending_ask)
        while not self.ask_event.wait(timeout=0.4):
            if self.stop_flag.is_set():
                self.pending_ask = None
                return ""
        self.pending_ask = None
        return self.ask_result


class AppState:
    def __init__(self, config_path: str | None = None) -> None:
        self.config = load_config(config_path)
        self.session = Session(id=uuid.uuid4().hex[:8], config=self.config)

    def reset_session(self) -> Session:
        old = self.session
        old.stop_flag.set()
        if old.agent is not None:
            try:
                old.agent.abort()
            except Exception:  # noqa: BLE001
                pass
        if old.engine is not None:
            try:
                old.engine.close()
            except Exception:  # noqa: BLE001
                pass
        old.confirm_event.set()
        old.ask_event.set()
        self.session = Session(id=uuid.uuid4().hex[:8], config=self.config)
        return self.session


# ------------------------------------------------------------------- agent
def build_agent(state: AppState, s: Session) -> Any:
    from ..agent.loop import Agent

    agent = Agent(
        state.config,
        on_event=lambda kind, payload: _agent_event(s, kind, payload),
        approver=s.approver,
        asker=s.asker,
    )
    s.agent = agent
    s.engine = agent.perception
    return agent


def _agent_event(s: Session, kind: str, payload: dict[str, Any]) -> None:
    if kind == "run_start":
        s.run_dir = str(payload.get("run_dir", ""))
    elif kind == "observation":
        s.last_observation = {k: v for k, v in payload.items() if k != "kind"}
        s.last_elements = payload.get("elements", [])
    elif kind == "render_done":
        s.last_render = payload.get("outputs", {})
    elif kind == "error":
        s.log(str(payload.get("message", "")), "error")
    s.emit(kind, payload)


def _monitors() -> list[dict[str, Any]]:
    try:
        from ..perception.screen import ScreenCapture

        cap = ScreenCapture()
        out = [{"name": n, "box": b.as_list()} for n, b in cap.monitors()]
        cap.close()
        return out
    except Exception:  # noqa: BLE001
        return []


def _parse_region(region: str) -> Any:
    if not region:
        return None
    try:
        from ..utils.geometry import Box

        return Box(*[int(v) for v in region.split(",")[:4]])
    except Exception:  # noqa: BLE001
        return None


def plan_from_request(state: AppState, s: Session, req: EditRequest) -> dict[str, Any]:
    """Turn the simple GUI form into a full plan (analysing media when asked)."""
    if not req.inputs:
        raise HTTPException(400, "no input file")
    from ..agent.executor import VideoExecutor
    from ..control.actuator import Actuator
    from ..handoff.manager import Handoff
    from ..video.probe import probe

    if s.engine is None:
        from ..observation import PerceptionEngine

        s.engine = PerceptionEngine(state.config)
    ex = VideoExecutor(state.config, Handoff(state.config, s.engine, Actuator(state.config, s.engine)))

    source = str(Path(req.inputs[0]).expanduser().resolve())
    info = probe(source)

    if req.mode in ("highlights", "keep"):
        res = ex.analyse(req.mode, source, top_n=req.top_n, max_seconds=req.max_seconds or None)
        if not res.ok:
            raise HTTPException(400, res.message)
        segments = list(res.data.get("segments", []))
    else:
        segments = [{"source": source, "start": 0.0, "end": max(0.1, info.duration)}]
    if not segments:
        raise HTTPException(400, "no usable ranges found in the input")

    output = req.output or str(Path(source).with_name(Path(source).stem + "_clippilot.mp4"))
    plan: dict[str, Any] = {
        "segments": segments,
        "output": output,
        "resolution": req.resolution,
        "fps": req.fps,
        "crf": req.crf,
        "aspect": "fill",
        "pad_blur": True,
        "overwrite": req.overwrite,
        "audio": {"normalize": req.normalize},
    }
    if len(segments) > 1 and req.transitions and req.transitions != "cut":
        types = [t.strip() for t in req.transitions.split(",") if t.strip()]
        plan["transitions"] = [{"type": t, "duration": 0.5} for t in types]
        while len(plan["transitions"]) < len(segments) - 1:
            plan["transitions"].append({"type": "cut"})
    if req.music:
        plan["audio"].update({"music": req.music, "music_gain_db": req.music_gain_db, "duck": req.duck})
    if req.captions:
        plan["captions"] = {"enabled": True, "burn": req.burn_captions, "position": "bottom"}
    return plan


# --------------------------------------------------------------------- app
def create_app(config_path: str | None = None) -> FastAPI:
    state = AppState(config_path)
    api = FastAPI(title="ClipPilot", version=__version__, docs_url="/api/docs")

    def s() -> Session:
        return state.session

    def ensure_engine(sess: Session) -> Any:
        if sess.engine is None:
            from ..observation import PerceptionEngine

            sess.engine = PerceptionEngine(state.config)
        return sess.engine

    # ------------------------------------------------------------ static UI
    @api.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))

    @api.get("/app.js")
    def appjs() -> FileResponse:
        return FileResponse(WEB_DIR / "app.js", media_type="application/javascript")

    @api.get("/style.css")
    def stylecss() -> FileResponse:
        return FileResponse(WEB_DIR / "style.css", media_type="text/css")

    @api.get("/api/ping")
    def ping() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    # ------------------------------------------------------------- settings
    @api.get("/api/settings")
    def get_settings() -> JSONResponse:
        cfg = state.config
        return JSONResponse(
            {
                "version": __version__,
                "config_path": str(cfg.path) if cfg.path else None,
                "llm": {
                    "base_url": cfg.llm.get("base_url"),
                    "model": cfg.llm.get("model"),
                    "vision_model": cfg.vision_model,
                    "api_key_set": bool(cfg.llm.get("api_key")),
                    "native_tools": cfg.llm.get("native_tools"),
                },
                "perception": cfg.perception,
                "control": {"dry_run": cfg.control.get("dry_run"), "humanize": cfg.control.get("humanize")},
                "safety": {"require_confirmation": cfg.safety.get("require_confirmation")},
                "agent": {"max_steps": cfg.agent.get("max_steps"), "vlm_verify": cfg.agent.get("vlm_verify")},
                "video_defaults": cfg.video.get("default", {}),
                "handoff": {"default_app": cfg.handoff.get("default_app")},
                "apps": list_recipes(cfg.resolve_path(cfg.handoff.get("recipes_dir", "recipes"))),
                "monitors": _monitors(),
                "has_api_key": cfg.has_api_key,
            }
        )

    @api.post("/api/settings")
    def set_settings(req: SettingsRequest) -> dict[str, Any]:
        mapping = {
            "dry_run": "control.dry_run",
            "require_confirmation": "safety.require_confirmation",
            "vlm_verify": "agent.vlm_verify",
            "max_steps": "agent.max_steps",
            "monitor": "perception.capture",
            "default_app": "handoff.default_app",
            "ocr_backend": "perception.ocr_backend",
            "llm_model": "llm.model",
            "llm_base_url": "llm.base_url",
            "llm_api_key": "llm.api_key",
        }
        applied: dict[str, Any] = {}
        for name, dotted in mapping.items():
            value = getattr(req, name, None)
            if value is not None:
                state.config.set(dotted, value)
                applied[dotted] = value
        if applied:
            s().log(f"settings updated: {json.dumps(applied)[:200]}")
            state.reset_session()
        return {"ok": True, "applied": applied}

    @api.post("/api/settings/save")
    def save_settings() -> dict[str, Any]:
        import yaml

        dest = Path(state.config.path) if state.config.path else Path.cwd() / "config.yaml"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            yaml.safe_dump(state.config.data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        state.config.path = dest
        return {"ok": True, "path": str(dest)}

    @api.post("/api/test/llm")
    def test_llm() -> dict[str, Any]:
        from ..agent.llm import LlmClient

        return LlmClient(state.config).ping()

    @api.get("/api/doctor")
    def doctor() -> dict[str, Any]:
        from ..perception.ocr import describe_backends
        from ..utils.binaries import BinaryKind, find_binary, ffmpeg_features, ffmpeg_version
        from ..video.captions import whisper_available

        checks: list[dict[str, Any]] = []
        for kind in (BinaryKind.FFMPEG, BinaryKind.FFPROBE):
            try:
                exe = find_binary(kind, str(state.config.video.get(kind.value, "") or ""))
                checks.append({"name": kind.value, "ok": True, "detail": ffmpeg_version(exe)})
                if kind is BinaryKind.FFMPEG:
                    feats = ffmpeg_features(exe)
                    checks.append(
                        {
                            "name": "libass (caption burn-in)",
                            "ok": "libass" in feats,
                            "detail": ", ".join(sorted(feats)) or "no optional features",
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                checks.append({"name": kind.value, "ok": False, "detail": str(exc).splitlines()[0]})
        have, which = whisper_available()
        checks.append({"name": "speech-to-text", "ok": have, "detail": which})
        try:
            from ..control.fallback import get_backend

            checks.append({"name": "input backend", "ok": True, "detail": get_backend("auto").name})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "input backend", "ok": False, "detail": str(exc)})
        try:
            from ..perception.screen import ScreenCapture

            cap = ScreenCapture()
            shot = cap.grab()
            checks.append(
                {"name": "screen capture", "ok": True, "detail": f"{shot.box} {shot.image.width}x{shot.image.height}"}
            )
            cap.close()
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "screen capture", "ok": False, "detail": str(exc)})
        checks.append(
            {
                "name": "ocr",
                "ok": state.config.perception.get("ocr_backend") != "none",
                "detail": describe_backends().replace("\n", " | "),
            }
        )
        return {"checks": checks, "ok": all(c["ok"] for c in checks)}

    # ------------------------------------------------------------ streaming
    @api.get("/api/events")
    async def events() -> StreamingResponse:
        sess = s()
        q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=2000)
        sess.subscribers.append(q)

        async def gen() -> AsyncIterator[bytes]:
            try:
                hello = json.dumps({"kind": "hello", "at": time.time(), "session": sess.id, "busy": sess.busy})
                yield f"data: {hello}\n\n".encode()
                while True:
                    try:
                        event = q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.15)
                        yield b": ping\n\n"
                        continue
                    yield f"data: {json.dumps(event)}\n\n".encode()
            except asyncio.CancelledError:
                raise
            finally:
                if q in sess.subscribers:
                    sess.subscribers.remove(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @api.get("/api/state")
    def get_state() -> dict[str, Any]:
        sess = s()
        return {
            "session": sess.id,
            "busy": sess.busy,
            "run_dir": sess.run_dir,
            "history": sess.history[-100:],
            "logs": sess.log_lines[-200:],
            "pending_confirm": sess.pending_confirm,
            "pending_ask": sess.pending_ask,
            "last_render": sess.last_render,
            "elements": sess.last_elements,
            "observation": sess.last_observation,
        }

    @api.get("/api/screenshot")
    def screenshot(quality: int = 60, w: int = 1000, region: str = "") -> Response:
        try:
            engine = ensure_engine(s())
            obs = engine.observe(region=_parse_region(region), with_ocr=False, with_templates=False)
            data = encode_jpeg(obs.image, quality=quality, max_side=w)
            s().last_observation = {
                "screen": obs.screen.as_list(),
                "window": obs.active_window,
                "cursor": obs.cursor,
                "at": obs.taken_at,
            }
        except Exception as exc:  # noqa: BLE001
            return Response(content=b"", media_type="image/jpeg", headers={"X-Error": str(exc)[:200]})
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @api.get("/api/inspect")
    def inspect(region: str = "", monitor: str = "") -> dict[str, Any]:
        engine = ensure_engine(s())
        obs = engine.observe(region=_parse_region(region), which=monitor or None)
        sess = s()
        sess.last_elements = [e.to_dict() for e in obs.elements]
        sess.last_observation = obs.describe()
        return {
            "screen": obs.screen.as_list(),
            "window": obs.active_window,
            "cursor": obs.cursor,
            "ocr": obs.ocr_backend,
            "elements": sess.last_elements,
        }

    # ------------------------------------------------------------ agent run
    @api.post("/api/run")
    def run(req: TaskRequest) -> dict[str, Any]:
        if s().busy:
            raise HTTPException(409, "a task is already running")
        state.config.set("control.dry_run", req.dry_run)
        state.config.set("safety.require_confirmation", req.require_confirmation)
        state.config.set("agent.vlm_verify", req.vlm_verify)
        state.config.set("agent.max_steps", req.max_steps)
        if req.monitor:
            state.config.set("perception.capture", req.monitor)
        if req.handoff_app:
            state.config.set("handoff.default_app", req.handoff_app)

        sess = state.reset_session()
        sess.log(f"task: {req.task}")
        sess.history.append({"role": "user", "text": req.task, "at": time.time()})
        try:
            agent = build_agent(state, sess)
        except Exception as exc:  # noqa: BLE001
            sess.log(f"could not start the agent: {exc}", "error")
            raise HTTPException(500, f"could not start the agent: {exc}") from exc

        sess.busy = True

        def worker() -> None:
            try:
                result = agent.run(req.task)
                sess.history.append(
                    {"role": "assistant", "text": result.summary or "done", "success": result.success, "at": time.time()}
                )
                sess.emit(
                    "run_finished",
                    {"success": result.success, "summary": result.summary, "steps": len(result.steps), "run_dir": str(result.transcript_path or "")},
                )
            except ClipPilotError as exc:
                sess.log(f"error: {exc}", "error")
                sess.emit("run_finished", {"success": False, "summary": str(exc), "steps": 0})
            except Exception as exc:  # noqa: BLE001
                sess.log(f"crash: {type(exc).__name__}: {exc}", "error")
                sess.emit("run_finished", {"success": False, "summary": str(exc), "steps": 0})
            finally:
                sess.busy = False
                sess.emit("idle", {})

        threading.Thread(target=worker, name="clippilot-agent", daemon=True).start()
        return {"ok": True, "session": sess.id}

    @api.post("/api/stop")
    def stop() -> dict[str, Any]:
        sess = s()
        sess.stop_flag.set()
        if sess.agent is not None:
            sess.agent.abort()
        sess.log("stop requested", "warn")
        return {"ok": True}

    @api.post("/api/confirm")
    def confirm(req: ConfirmRequest) -> dict[str, Any]:
        sess = s()
        sess.confirm_result = req.approved
        if sess.pending_confirm:
            sess.history.append(
                {
                    "role": "assistant",
                    "text": ("approved: " if req.approved else "declined: ")
                    + str(sess.pending_confirm.get("reason", "action")),
                    "at": time.time(),
                }
            )
        sess.confirm_event.set()
        return {"ok": True}

    @api.post("/api/answer")
    def answer(req: AskRequest) -> dict[str, Any]:
        sess = s()
        sess.ask_result = req.answer
        if sess.pending_ask:
            sess.history.append({"role": "user", "text": req.answer, "at": time.time()})
        sess.ask_event.set()
        return {"ok": True}

    # ---------------------------------------------------------------- edits
    @api.post("/api/analyse")
    def analyse(req: EditRequest) -> dict[str, Any]:
        sess = s()
        if sess.busy:
            raise HTTPException(409, "something is already running")
        if not req.inputs:
            raise HTTPException(400, "no input file")
        sess.busy = True

        def worker() -> None:
            try:
                engine = ensure_engine(sess)
                from ..agent.executor import VideoExecutor
                from ..control.actuator import Actuator
                from ..handoff.manager import Handoff

                ex = VideoExecutor(state.config, Handoff(state.config, engine, Actuator(state.config, engine)))
                res = ex.analyse(req.mode, str(req.inputs[0]), top_n=req.top_n, max_seconds=req.max_seconds or None)
                sess.emit("analysis", res.to_dict() | {"ok": res.ok, "message": res.message})
                if res.ok:
                    sess.log(res.message)
            except Exception as exc:  # noqa: BLE001
                sess.log(f"analysis failed: {exc}", "error")
                sess.emit("analysis", {"ok": False, "message": str(exc)})
            finally:
                sess.busy = False
                sess.emit("idle", {})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True}

    @api.post("/api/edit")
    def edit(req: EditRequest) -> dict[str, Any]:
        sess = s()
        if sess.busy:
            raise HTTPException(409, "something is already running")
        sess.busy = True
        payload: dict[str, Any] = {}

        def worker() -> None:
            try:
                engine = ensure_engine(sess)
                from ..agent.executor import VideoExecutor
                from ..control.actuator import Actuator
                from ..handoff.manager import Handoff

                ex = VideoExecutor(
                    state.config,
                    Handoff(state.config, engine, Actuator(state.config, engine)),
                    on_event=lambda k, p: sess.emit(k, p),
                    on_progress=lambda pct, msg: sess.emit("progress", {"pct": round(pct, 3), "message": msg}),
                )
                plan = req.plan or plan_from_request(state, sess, req)
                res = ex.render(plan, dry_run=bool(state.config.control.get("dry_run")))
                payload.update(res.to_dict())
                sess.last_render = {k: v for k, v in res.data.items() if k != "notes"}
                sess.emit("render_done", {"outputs": res.data, "message": res.message})
                if req.also_handoff and res.ok:
                    project = str(res.data.get("video", ""))
                    hres = ex.handoff_manager_run(req.handoff_app, project)
                    sess.emit("handoff_result", {"ok": hres.ok, "summary": hres.message})
                    payload["handoff"] = hres.message
            except ClipPilotError as exc:
                sess.log(str(exc), "error")
                sess.emit("render_error", {"message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                sess.log(f"{type(exc).__name__}: {exc}", "error")
                sess.emit("render_error", {"message": str(exc)})
            finally:
                sess.busy = False
                sess.emit("idle", {})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True}

    @api.post("/api/handoff")
    def handoff(req: HandoffRequest) -> dict[str, Any]:
        sess = s()
        if sess.busy:
            raise HTTPException(409, "something is already running")
        sess.busy = True

        def worker() -> None:
            try:
                engine = ensure_engine(sess)
                from ..control.actuator import Actuator
                from ..handoff.manager import Handoff

                h = Handoff(
                    state.config,
                    engine,
                    Actuator(state.config, engine),
                    on_event=lambda k, p: sess.emit(k, p),
                )
                res = h.run(req.app, req.project, notes=req.notes)
                sess.emit("handoff_result", {"ok": res.ok, "summary": res.message, "steps": res.data.get("steps", [])})
            except Exception as exc:  # noqa: BLE001
                sess.log(str(exc), "error")
                sess.emit("handoff_result", {"ok": False, "summary": str(exc)})
            finally:
                sess.busy = False
                sess.emit("idle", {})

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True, "started": True}

    @api.get("/api/browse")
    def browse(kind: str = "media", start: str = "") -> dict[str, Any]:
        """Native folder/file picker, run out-of-process so the UI never stalls."""
        try:
            import subprocess
            import sys

            if kind == "folder":
                script = (
                    "import tkinter as tk\nfrom tkinter import filedialog\n"
                    "r=tk.Tk();r.withdraw()\n"
                    f"print(filedialog.askdirectory(initialdir={(start or str(Path.home()))!r}))\nr.destroy()\n"
                )
            else:
                script = (
                    "import tkinter as tk\nfrom tkinter import filedialog\n"
                    "r=tk.Tk();r.withdraw()\n"
                    f"print(filedialog.askopenfilename(initialdir={(start or str(Path.home()))!r},"
                    "filetypes=[('Media','*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mp3 *.wav *.aac'),"
                    "('All files','*.*')]))\nr.destroy()\n"
                )
            exe = "pythonw" if (os.name == "nt" and hasattr(sys, "executable")) else sys.executable
            args = [exe, "-c", script] if os.name == "nt" else [sys.executable, "-c", script]
            out = subprocess.run(args, capture_output=True, text=True, timeout=300)
            picked = (out.stdout or "").strip().splitlines()
            picked = picked[-1].strip() if picked else ""
            return {"ok": bool(picked), "path": picked or None}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @api.get("/api/paths")
    def paths() -> dict[str, Any]:
        return {
            "desktop": str(Path.home() / "Desktop"),
            "videos": str(Path.home() / "Videos"),
            "downloads": str(Path.home() / "Downloads"),
            "home": str(Path.home()),
            "cwd": str(Path.cwd()),
        }

    @api.get("/api/probe")
    def probe(path: str) -> dict[str, Any]:
        from ..video.probe import probe as do_probe

        try:
            return {"ok": True, **do_probe(path).to_dict()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    return api
