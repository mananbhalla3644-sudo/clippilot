from __future__ import annotations

import json
from typing import Any

from ..errors import ActionError
from ..utils.logging import get_logger

log = get_logger("agent.schema")

_TARGET = {
    "type": "object",
    "description": "Where to act: give element_id (from the element list) or text (label on screen), or raw x/y screen pixels.",
    "properties": {
        "element_id": {"type": "string", "description": "e.g. e12 - id from the current element list"},
        "text": {"type": "string", "description": "visible label, matched exactly first then by substring"},
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "index": {"type": "integer", "minimum": 0, "description": "which match to use when several share a label"},
    },
}

_SEGMENT = {
    "type": "object",
    "required": ["source", "start", "end"],
    "properties": {
        "source": {"type": "string", "description": "path to the media file"},
        "start": {"type": "number", "minimum": 0, "description": "seconds"},
        "end": {"type": "number", "minimum": 0, "description": "seconds"},
        "speed": {"type": "number", "minimum": 0.1, "maximum": 8, "description": "1 = normal, 2 = double speed"},
    },
}

FUNCTIONS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click something. Prefer element_id or text from the element list over raw coordinates.",
        "parameters": {
            "type": "object",
            "properties": {
                **_TARGET["properties"],
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer", "minimum": 1, "maximum": 3},
                "note": {"type": "string"},
            },
        },
    },
    {
        "name": "move",
        "description": "Move the pointer without clicking (hover menus, drag previews).",
        "parameters": {
            "type": "object",
            "required": ["x", "y"],
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "duration_ms": {"type": "integer"}},
        },
    },
    {
        "name": "drag",
        "description": "Press at one point, move, release at another (sliders, timeline trims, canvas).",
        "parameters": {
            "type": "object",
            "required": ["to"],
            "properties": {
                "from": _TARGET,
                "to": {
                    "type": "object",
                    "required": ["x", "y"],
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                },
                "duration_ms": {"type": "integer", "description": "default 900 - slower drags are read as real drags"},
            },
        },
    },
    {
        "name": "scroll",
        "description": "Scroll the wheel. Positive amount = up, negative = down.",
        "parameters": {
            "type": "object",
            "required": ["amount"],
            "properties": {
                "amount": {"type": "integer", "description": "-10 (fast down) .. 10 (fast up)"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
        },
    },
    {
        "name": "type",
        "description": "Type literal text into the focused field. Long text is pasted automatically.",
        "parameters": {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "clear_first": {"type": "boolean", "description": "select-all + delete first"},
                "interval_ms": {"type": "integer"},
            },
        },
    },
    {
        "name": "key",
        "description": "Press keys/chords: 'enter', 'escape', 'ctrl+s', 'ctrl+shift+e'.",
        "parameters": {
            "type": "object",
            "required": ["keys"],
            "properties": {"keys": {"type": "string"}},
        },
    },
    {
        "name": "wait",
        "description": "Pause (loading screens, render progress). Max 20s per call.",
        "parameters": {
            "type": "object",
            "required": ["seconds"],
            "properties": {"seconds": {"type": "number"}, "reason": {"type": "string"}},
        },
    },
    {
        "name": "observe",
        "description": "Re-look at the screen, optionally zoomed into a region (box in screen px).",
        "parameters": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "[x1,y1,x2,y2] screen pixels",
                },
                "with_ocr": {"type": "boolean"},
                "describe": {"type": "boolean", "description": "ask the vision model to narrate unknown screens"},
            },
        },
    },
    {
        "name": "focus_window",
        "description": "Raise an open window by (partial) title. Also maximizes it.",
        "parameters": {
            "type": "object",
            "required": ["title_contains"],
            "properties": {"title_contains": {"type": "string"}, "maximize": {"type": "boolean"}},
        },
    },
    {
        "name": "list_windows",
        "description": "List visible window titles to find out what is open.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "launch_app",
        "description": "Start an application (e.g. 'explorer', 'photos', 'ms-settings:display').",
        "parameters": {
            "type": "object",
            "required": ["app"],
            "properties": {
                "app": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "wait": {"type": "number"},
            },
        },
    },
    {
        "name": "assert",
        "description": "Check the screen before continuing. Fails loudly so you can correct course.",
        "parameters": {
            "type": "object",
            "properties": {
                "text_present": {"type": "string"},
                "text_absent": {"type": "string"},
                "window_title_contains": {"type": "string"},
                "statement": {"type": "string", "description": "checked by the vision model"},
            },
        },
    },
    {
        "name": "edit_video",
        "description": "Render a video with the built-in ffmpeg engine (fast, exact, non-interactive). "
        "Prefer this over clicking a timeline: build the plan, render, then hand off to an editor if the user wants a project.",
        "parameters": {
            "type": "object",
            "required": ["plan"],
            "properties": {
                "plan": {
                    "type": "object",
                    "required": ["output"],
                    "properties": {
                        "segments": {"type": "array", "items": _SEGMENT},
                        "transitions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "description": "cut | fade | fadeblack | wipe | slide | dissolve | circleopen",
                                    },
                                    "duration": {"type": "number", "description": "0.4-1.5s"},
                                },
                            },
                        },
                        "audio": {
                            "type": "object",
                            "properties": {
                                "music": {"type": "string"},
                                "music_gain_db": {"type": "number"},
                                "duck": {"type": "boolean", "description": "auto-duck music under speech"},
                                "normalize": {"type": "string", "enum": ["none", "peak", "loudnorm"]},
                                "target_lufs": {"type": "number"},
                            },
                        },
                        "captions": {
                            "type": "object",
                            "properties": {
                                "enabled": {"type": "boolean"},
                                "srt": {"type": "string", "description": "existing subtitle file, else auto-transcribed"},
                                "burn": {"type": "boolean", "description": "true = draw into the picture, false = soft track"},
                                "position": {"type": "string", "enum": ["top", "center", "bottom"]},
                            },
                        },
                        "output": {"type": "string"},
                        "resolution": {"type": "string", "description": "e.g. 1080x1920 for vertical, 1920x1080 for landscape"},
                        "fps": {"type": "integer"},
                        "aspect": {"type": "string", "enum": ["fill", "fit", "stretch"]},
                        "crf": {"type": "integer"},
                        "preset": {"type": "string"},
                        "loop_music": {"type": "boolean"},
                    },
                }
            },
        },
    },
    {
        "name": "plan_video",
        "description": "Analyse media and propose segments without rendering: highlights, silence removal, "
        "or a rough cut. Use this when the user says 'find the best bits' or 'remove the dead air'.",
        "parameters": {
            "type": "object",
            "required": ["mode", "input"],
            "properties": {
                "mode": {"type": "string", "enum": ["highlights", "remove_silence", "auto_cut", "info"]},
                "input": {"type": "string"},
                "top_n": {"type": "integer"},
                "max_seconds": {"type": "number"},
                "min_silence": {"type": "number"},
            },
        },
    },
    {
        "name": "handoff",
        "description": "Open a rendered file in a real editor and drive it (import + arrange). "
        "Use after edit_video when the user wants the project in CapCut / DaVinci / Premiere / Shotcut / Clipchamp.",
        "parameters": {
            "type": "object",
            "required": ["app"],
            "properties": {
                "app": {"type": "string", "enum": ["capcut", "resolve", "premiere", "shotcut", "clipchamp", "photos", "none"]},
                "project": {"type": "string", "description": "video or EDL/FCPXML project file to import"},
                "notes": {"type": "string"},
            },
        },
    },
    {
        "name": "ask_human",
        "description": "Stop and ask the user. Use for ambiguity, credentials, destructive steps, or anything you cannot verify.",
        "parameters": {
            "type": "object",
            "required": ["question"],
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
                "why": {"type": "string"},
            },
        },
    },
    {
        "name": "task_done",
        "description": "Declare success. Include the artifacts you produced.",
        "parameters": {
            "type": "object",
            "required": ["summary"],
            "properties": {
                "summary": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "give_up",
        "description": "Stop and report you cannot finish. Explain what you tried and what is blocking.",
        "parameters": {
            "type": "object",
            "required": ["reason"],
            "properties": {"reason": {"type": "string"}, "tried": {"type": "array", "items": {"type": "string"}}},
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command. Disabled unless safety.allow_run_command is true.",
        "parameters": {
            "type": "object",
            "required": ["command"],
            "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}},
        },
    },
]

TOOLS: list[dict[str, Any]] = [{"type": "function", "function": f} for f in FUNCTIONS]

#: A compact reference for providers without native tool calling.
def plain_prompt_schema() -> str:
    lines = []
    for f in FUNCTIONS:
        params = f["parameters"]
        props = params.get("properties", {})
        req = set(params.get("required", []))
        bits = []
        for name, spec in props.items():
            enum = spec.get("enum")
            t = spec.get("type", "any")
            desc = spec.get("description", "")
            opt = "" if name in req else "?"
            if enum:
                bits.append(f"{name}{opt}: {'/'.join(enum)}")
            elif t == "array":
                bits.append(f"{name}{opt}: array")
            elif t == "object":
                bits.append(f"{name}{opt}: object")
            else:
                bits.append(f"{name}{opt}: {t}")
        lines.append(f"- {f['name']}({', '.join(bits)}) - {f['description']}")
    return "\n".join(lines)


def normalize_keys(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "+".join(str(v) for v in value)
    return str(value)


def parse_tool_call(name: str, raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError as exc:
            raise ActionError(f"bad JSON in arguments for {name}: {exc}") from exc
    elif isinstance(raw_args, dict):
        args = dict(raw_args)
    else:
        raise ActionError(f"unsupported argument payload for {name}: {type(raw_args).__name__}")

    if name in ("key",):
        if "keys" in args:
            args["keys"] = normalize_keys(args["keys"])
        elif "key" in args:
            args["keys"] = normalize_keys(args.pop("key"))
    if name in ("click", "move", "drag") and "target" in args:
        target = args.pop("target") or {}
        for k, v in target.items():
            args.setdefault(k, v)
    if name == "scroll" and "direction" in args:
        dirs = {"up": 6, "down": -6, "left": -6, "right": 6}
        d = str(args.pop("direction")).lower()
        base = int(args.get("amount", 6))
        args["amount"] = dirs.get(d, 6) if base in (0, 6) else base * (1 if d == "up" else -1)
    if name == "type" and "value" in args:
        args["text"] = args.pop("value")
    args["type"] = name
    return args


def validate(action: dict[str, Any]) -> dict[str, Any]:
    """Light validation; the actuator reports anything that cannot be resolved."""
    t = str(action.get("type", ""))
    if not t:
        raise ActionError("action is missing a 'type'")
    if t not in {f["name"] for f in FUNCTIONS}:
        raise ActionError(f"unknown action type {t!r}")

    if t in ("click", "move", "drag"):
        has_xy = action.get("x") is not None and action.get("y") is not None
        has_ref = bool(action.get("element_id") or action.get("text"))
        if t != "drag" and not (has_xy or has_ref):
            raise ActionError("click/move needs element_id, text, or x+y")
    if t == "type" and "text" not in action:
        raise ActionError("type needs 'text'")
    if t == "key" and not action.get("keys"):
        raise ActionError("key needs 'keys' (e.g. 'ctrl+s')")
    if t == "edit_video":
        plan = action.get("plan") or {}
        if not plan.get("output"):
            raise ActionError("edit_video needs plan.output")
    return action
