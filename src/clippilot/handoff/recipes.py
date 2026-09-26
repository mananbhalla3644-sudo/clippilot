from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.logging import get_logger

log = get_logger("handoff.recipes")


@dataclass
class Step:
    action: dict[str, Any]
    optional: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.action)
        if self.optional:
            d["_optional"] = True
        if self.note:
            d["_note"] = self.note
        return d


@dataclass
class Recipe:
    key: str
    name: str
    app: str
    window_title: str = ""
    description: str = ""
    launch_wait: float = 7.0
    steps: list[Step] = field(default_factory=list)
    tips: list[str] = field(default_factory=list)
    source: str = "built-in"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "app": self.app,
            "window_title": self.window_title,
            "description": self.description,
            "launch_wait": self.launch_wait,
            "steps": [s.to_dict() for s in self.steps],
            "tips": self.tips,
            "source": self.source,
        }


def _s(action: dict[str, Any], optional: bool = False, note: str = "") -> Step:
    return Step(action=action, optional=optional, note=note)


#: Built-in recipes. Keyboard-first, because accelerators survive UI redesigns.
BUILTIN: list[Recipe] = [
    Recipe(
        key="capcut",
        name="CapCut (desktop)",
        app="capcut",
        window_title="CapCut",
        description="Launch CapCut and import the rendered clip onto a new timeline.",
        launch_wait=9.0,
        steps=[
            _s({"type": "launch_app", "app": "capcut", "wait": 9}, note="CapCut cold start is slow"),
            _s({"type": "focus_window", "title_contains": "CapCut", "maximize": True}, optional=True),
            _s({"type": "wait", "seconds": 3.0}),
            _s({"type": "click", "text": "New project"}, optional=True, note="start screen entry point"),
            _s({"type": "wait", "seconds": 2.5}),
            _s({"type": "click", "text": "Import"}, optional=True),
            _s({"type": "wait", "seconds": 2.0}),
            _s({"type": "key", "keys": "ctrl+o"}, optional=True, note="file dialog fallback"),
            _s({"type": "wait", "seconds": 2.0}),
            _s({"type": "type", "text": "{project}", "clear_first": True}),
            _s({"type": "key", "keys": "enter"}),
            _s({"type": "wait", "seconds": 6.0}),
        ],
        tips=[
            "If a project picker is already open, skip straight to typing the path.",
            "CapCut may ask about proxy formats; the agent will report the dialog instead of guessing.",
        ],
    ),
    Recipe(
        key="resolve",
        name="DaVinci Resolve",
        app="resolve",
        window_title="DaVinci Resolve",
        description="Launch Resolve, switch to the Edit page, import the media into the Media Pool.",
        launch_wait=12.0,
        steps=[
            _s({"type": "launch_app", "app": "resolve", "wait": 12}, note="Resolve takes a while"),
            _s({"type": "focus_window", "title_contains": "DaVinci Resolve", "maximize": True}, optional=True),
            _s({"type": "wait", "seconds": 4.0}),
            _s({"type": "click", "text": "Edit"}, optional=True, note="bottom page selector"),
            _s({"type": "wait", "seconds": 3.0}),
            _s({"type": "key", "keys": "ctrl+i"}, optional=True),
            _s({"type": "wait", "seconds": 2.5}),
            _s({"type": "type", "text": "{project}", "clear_first": True}),
            _s({"type": "key", "keys": "enter"}),
            _s({"type": "wait", "seconds": 5.0}),
        ],
        tips=["Resolve keeps the Media Pool bottom-left; imported clips appear there."],
    ),
    Recipe(
        key="premiere",
        name="Adobe Premiere Pro",
        app="premiere",
        window_title="Premiere Pro",
        description="Launch Premiere, import the media, create a sequence from the clip.",
        launch_wait=15.0,
        steps=[
            _s({"type": "launch_app", "app": "premiere", "wait": 15}),
            _s({"type": "focus_window", "title_contains": "Premiere Pro", "maximize": True}, optional=True),
            _s({"type": "wait", "seconds": 5.0}),
            _s({"type": "key", "keys": "ctrl+i"}),
            _s({"type": "wait", "seconds": 3.0}),
            _s({"type": "type", "text": "{project}", "clear_first": True}),
            _s({"type": "key", "keys": "enter"}),
            _s({"type": "wait", "seconds": 5.0}),
            _s({"type": "click", "text": "{filename}"}, optional=True, note="select the clip in the panel"),
            _s({"type": "key", "keys": "ctrl+m"}, optional=True, note="new sequence from clip"),
            _s({"type": "wait", "seconds": 4.0}),
        ],
        tips=["Import the .edl/.fcpxml from the render folder to bring the whole cut, not just one file."],
    ),
    Recipe(
        key="shotcut",
        name="Shotcut",
        app="shotcut",
        window_title="Shotcut",
        description="Open the rendered file (and optionally the EDL) in Shotcut.",
        launch_wait=6.0,
        steps=[
            _s({"type": "launch_app", "app": "shotcut", "wait": 6}),
            _s({"type": "focus_window", "title_contains": "Shotcut", "maximize": True}, optional=True),
            _s({"type": "wait", "seconds": 2.0}),
            _s({"type": "key", "keys": "ctrl+o"}),
            _s({"type": "wait", "seconds": 2.5}),
            _s({"type": "type", "text": "{project}", "clear_first": True}),
            _s({"type": "key", "keys": "enter"}),
            _s({"type": "wait", "seconds": 4.0}),
        ],
        tips=["Shotcut reads CMX3600 EDL via File > Open."],
    ),
    Recipe(
        key="clipchamp",
        name="Windows Video Editor / Clipchamp",
        app="clipchamp",
        window_title="Clipchamp",
        description="Import into Clipchamp (Microsoft's free editor).",
        launch_wait=8.0,
        steps=[
            _s({"type": "launch_app", "app": "clipchamp", "wait": 8}),
            _s({"type": "focus_window", "title_contains": "Clipchamp", "maximize": True}, optional=True),
            _s({"type": "wait", "seconds": 3.0}),
            _s({"type": "click", "text": "Import"}, optional=True),
            _s({"type": "wait", "seconds": 2.5}),
            _s({"type": "type", "text": "{project}", "clear_first": True}),
            _s({"type": "key", "keys": "enter"}),
            _s({"type": "wait", "seconds": 5.0}),
        ],
        tips=["Windows Video Editor (the successor) is installed from the Store; the same steps apply."],
    ),
    Recipe(
        key="photos",
        name="Windows Photos",
        app="photos",
        window_title="Photos",
        description="Open the clip in Photos and jump to its Trim tool.",
        launch_wait=6.0,
        steps=[
            _s({"type": "launch_app", "app": "photos", "wait": 6}),
            _s({"type": "focus_window", "title_contains": "Photos", "maximize": True}, optional=True),
            _s({"type": "wait", "seconds": 2.0}),
            _s({"type": "key", "keys": "ctrl+o"}, optional=True),
            _s({"type": "wait", "seconds": 2.0}),
            _s({"type": "type", "text": "{project}", "clear_first": True}),
            _s({"type": "key", "keys": "enter"}),
            _s({"type": "wait", "seconds": 3.0}),
            _s({"type": "key", "keys": "ctrl+e"}, optional=True, note="Photos shortcuts: playback + edit"),
        ],
        tips=["Photos' Trim tool is limited; for real edits prefer the ffmpeg engine or Resolve."],
    ),
    Recipe(
        key="media_player",
        name="Media Player",
        app="vlc",
        window_title="Media Player",
        description="Just open the result so the user can watch it immediately.",
        launch_wait=4.0,
        steps=[
            _s({"type": "launch_app", "app": "vlc", "wait": 4}),
            _s({"type": "wait", "seconds": 1.5}),
            _s({"type": "key", "keys": "ctrl+o"}, optional=True),
            _s({"type": "wait", "seconds": 2.0}),
            _s({"type": "type", "text": "{project}", "clear_first": True}),
            _s({"type": "key", "keys": "enter"}),
        ],
        tips=[],
    ),
    Recipe(
        key="explorer",
        name="File Explorer",
        app="explorer",
        window_title="File Explorer",
        description="Reveal the output in Explorer so the user can see the files.",
        launch_wait=4.0,
        steps=[
            _s({"type": "launch_app", "app": "explorer", "wait": 4}),
            _s({"type": "wait", "seconds": 1.5}),
            _s({"type": "key", "keys": "ctrl+l"}, optional=True),
            _s({"type": "type", "text": "{dir}", "clear_first": True}),
            _s({"type": "key", "keys": "enter"}),
        ],
        tips=[],
    ),
]

BY_KEY = {r.key: r for r in BUILTIN}


def load_extra(recipes_dir: str | Path) -> list[Recipe]:
    """User-authored recipes from recipes/*.yaml."""
    out: list[Recipe] = []
    d = Path(recipes_dir)
    if not d.exists():
        return out
    try:
        import yaml
    except ImportError:
        log.warning("pyyaml missing; ignoring %s", d)
        return out
    for path in sorted(d.glob("*.y*ml")):
        if path.name.startswith("_") or path.name.startswith("."):
            continue  # _template.yaml and friends are documentation, not recipes
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("bad recipe %s: %s", path.name, exc)
            continue
        if not isinstance(data, dict):
            continue
        key = str(data.get("key") or path.stem).lower()
        steps_raw = data.get("steps") or []
        steps = [
            _s(
                {k: v for k, v in dict(s).items() if not k.startswith("_")},
                optional=bool(dict(s).get("_optional", False)),
                note=str(dict(s).get("_note", "")),
            )
            for s in steps_raw
            if isinstance(s, dict)
        ]
        out.append(
            Recipe(
                key=key,
                name=str(data.get("name", key)),
                app=str(data.get("app", key)),
                window_title=str(data.get("window_title", "")),
                description=str(data.get("description", "")),
                launch_wait=float(data.get("launch_wait", 7.0)),
                steps=steps,
                tips=[str(t) for t in (data.get("tips") or [])],
                source=str(path),
            )
        )
        log.info("loaded recipe %s from %s", key, path.name)
    return out


def all_recipes(recipes_dir: str | Path = "recipes") -> list[Recipe]:
    extra = {r.key: r for r in load_extra(recipes_dir)}
    merged = list(BUILTIN)
    for key, r in extra.items():
        if key in BY_KEY:
            merged = [r if m.key == key else m for m in merged]
        else:
            merged.append(r)
    return merged


def get_recipe(key: str, recipes_dir: str | Path = "recipes") -> Recipe | None:
    key = (key or "").strip().lower()
    for r in all_recipes(recipes_dir):
        if r.key == key or r.app == key or r.name.lower() == key:
            return r
    aliases = {
        "vlc": "media_player",
        "media player": "media_player",
        "davinci": "resolve",
        "davinci resolve": "resolve",
        "pr": "premiere",
        "premierepro": "premiere",
        "adobe premiere": "premiere",
        "photos": "photos",
        "capcut desktop": "capcut",
        "windows video editor": "clipchamp",
    }
    if key in aliases:
        return get_recipe(aliases[key], recipes_dir)
    return None


def list_recipes(recipes_dir: str | Path = "recipes") -> list[dict[str, Any]]:
    return [r.to_dict() for r in all_recipes(recipes_dir)]


TEMPLATE = """\
# Custom handoff recipe for ClipPilot
key: myeditor
name: My Editor
app: myeditor            # exe name, Start Menu entry, or ms-settings: page
window_title: My Editor  # used to focus the window
description: Import the rendered clip.
launch_wait: 8.0
tips:
  - Anything worth telling the agent about this app.
steps:
  - type: launch_app
    app: myeditor
    wait: 8
  - type: focus_window
    title_contains: My Editor
    maximize: true
  - type: key
    keys: ctrl+o
  - type: type
    text: "{project}"      # {project} = output file, {dir} = its folder, {filename} = name
    clear_first: true
  - type: key
    keys: enter
  - type: click
    text: Import
    _optional: true        # keep going if this step fails
"""
