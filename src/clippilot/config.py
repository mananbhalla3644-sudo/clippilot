from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")

DEFAULTS: dict[str, Any] = {
    "llm": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1",
        "api_key": "",
        "vision_model": "",
        "temperature": 0.1,
        "max_tokens": 1200,
        "timeout": 90,
        "max_retries": 4,
        "native_tools": True,
    },
    "perception": {
        "ocr_backend": "auto",
        "capture": "virtual",
        "scale": 1.0,
        "llm_image": True,
        "max_elements": 120,
        "keep_words": False,
        "min_confidence": 0.35,
        "merge_words_gap": 34,
        "templates_dir": "assets/templates",
        "cache_dir": ".cache/perception",
    },
    "control": {
        "backend": "win32",
        "humanize": True,
        "move_duration": [0.28, 0.65],
        "type_delay": [0.028, 0.095],
        "clipboard_paste_threshold": 40,
        "settle": [0.25, 0.6],
        "dry_run": False,
        "failsafe": True,
        "default_settle_ms": 400,
    },
    "safety": {
        "require_confirmation": True,
        "confirm_on": ["type_text", "run_command", "delete", "export_overwrite", "close_app", "install"],
        "allow_run_command": False,
        "allow_clipboard_write": True,
        "blocked_rects": [],
        "max_clicks_per_minute": 240,
        "max_keys_per_minute": 600,
        "refuse_password_fields": True,
        "denylist_keys": ["ctrl+alt+delete"],
        "confirm_texts": ["delete", "remove", "discard", "erase", "format", "reset", "overwrite", "unsubscribe"],
    },
    "agent": {
        "max_steps": 40,
        "max_retries_per_action": 3,
        "history_limit": 24,
        "verify_after_actions": ["click", "double_click", "key", "type"],
        "vlm_verify": False,
        "interactive": True,
        "retry_prompt": True,
        "run_dir": "runs",
        "save_transcript": True,
    },
    "video": {
        "ffmpeg": "",
        "ffprobe": "",
        "work_dir": ".work",
        "default": {
            "resolution": "1080x1920",
            "fps": 30,
            "crf": 20,
            "preset": "veryfast",
            "format": "mp4",
            "audio_bitrate": "192k",
            "normalize": "loudnorm",
            "target_lufs": -14.0,
            "aspect": "fill",
            "pad_blur": True,
        },
        "highlights": {
            "window": 2.0,
            "hop": 0.5,
            "silence_db": -32.0,
            "min_silence": 0.35,
            "pad": 0.25,
            "top_n": 5,
        },
        "captions": {
            "whisper_model": "base",
            "language": "en",
            "max_chars_per_line": 34,
            "max_lines": 2,
            "min_duration": 0.7,
            "style": "FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=90",
        },
    },
    "handoff": {
        "default_app": "capcut",
        "launch_wait": 6.0,
        "window_wait": 12.0,
        "recipes_dir": "recipes",
    },
    "vlm": {
        "enabled": True,
        "max_side": 1450,
        "jpeg_quality": 80,
    },
}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _interp(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) or "")
        return _ENV_RE.sub(sub, value)
    if isinstance(value, list):
        return [_interp(v) for v in value]
    if isinstance(value, dict):
        return {k: _interp(v) for k, v in value.items()}
    return value


def _expand(path: Path) -> Path:
    path = Path(path).expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass
    return path


def search_config_path(explicit: str | os.PathLike | None = None) -> Path | None:
    if explicit:
        p = _expand(Path(explicit))
        return p if p.exists() else None
    env = os.environ.get("CLIPPILOT_CONFIG")
    if env and Path(env).exists():
        return _expand(Path(env))
    here = Path.cwd()
    for base in (here, *here.parents):
        for name in ("config.yaml", "clippilot.yaml"):
            cand = base / name
            if cand.exists():
                return cand
    root = Path(__file__).resolve().parents[2]
    for name in ("config.yaml", "config.example.yaml"):
        cand = root / name
        if cand.exists():
            return cand
    return None


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Path | None = None
    root: Path = field(default_factory=Path.cwd)

    # ------------------------------------------------------------- accessors
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    @property
    def llm(self) -> dict[str, Any]:
        return self.data["llm"]

    @property
    def perception(self) -> dict[str, Any]:
        return self.data["perception"]

    @property
    def control(self) -> dict[str, Any]:
        return self.data["control"]

    @property
    def safety(self) -> dict[str, Any]:
        return self.data["safety"]

    @property
    def agent(self) -> dict[str, Any]:
        return self.data["agent"]

    @property
    def video(self) -> dict[str, Any]:
        return self.data["video"]

    @property
    def handoff(self) -> dict[str, Any]:
        return self.data["handoff"]

    @property
    def vlm(self) -> dict[str, Any]:
        return self.data["vlm"]

    @property
    def vision_model(self) -> str:
        return self.llm.get("vision_model") or self.llm.get("model", "")

    @property
    def has_api_key(self) -> bool:
        return bool(self.llm.get("api_key")) or "localhost" in str(self.llm.get("base_url", ""))

    def resolve_path(self, value: str | os.PathLike) -> Path:
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = (self.root / p).resolve()
        return p

    def as_env(self) -> dict[str, str]:
        """Config in the shape the LLM prompt builder wants."""
        return {
            "base_url": self.llm.get("base_url", ""),
            "model": self.llm.get("model", ""),
            "vision_model": self.vision_model,
        }

    def to_dict(self) -> dict[str, Any]:
        redacted = copy.deepcopy(self.data)
        key = redacted.get("llm", {}).get("api_key")
        if key:
            redacted["llm"]["api_key"] = f"***{str(key)[-4:] if len(str(key)) >= 4 else ''}"
        return redacted


def load_config(path: str | os.PathLike | None = None, overrides: dict[str, Any] | None = None) -> Config:
    found = search_config_path(path)
    data = copy.deepcopy(DEFAULTS)
    used: Path | None = None
    if found:
        try:
            raw = yaml.safe_load(found.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{found}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{found}: top level must be a mapping")
        data = _deep_merge(data, raw)
        used = found
    data = _interp(data)
    if overrides:
        for dotted, value in overrides.items():
            if value is None:
                continue
            parts = dotted.split(".")
            node = data
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = _interp(value)
    root = used.parent if used else Path.cwd()
    return Config(data=data, path=used, root=root)


def write_example_config(dest: str | os.PathLike = "config.yaml", overwrite: bool = False) -> Path:
    src = Path(__file__).resolve().parents[2] / "config.example.yaml"
    if not src.exists():
        raise ConfigError("config.example.yaml is missing next to the package")
    dst = Path(dest).expanduser()
    if dst.exists() and not overwrite:
        raise ConfigError(f"{dst} already exists (pass overwrite=True to replace)")
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst
