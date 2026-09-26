from __future__ import annotations

import platform
import sys
from typing import Any

from ..config import Config
from .schema import plain_prompt_schema

APP_HANDBOOK = """\
Windows apps you are likely to be asked to drive:

- File Explorer ("File Explorer" in the title bar). Ctrl+L focuses the address bar,
  Ctrl+A select all, type a full path, Enter opens. Ctrl+C copies, Ctrl+V pastes.
- CapCut desktop ("CapCut"). New project -> Import (or drag files in). Timeline at the
  bottom, preview top-left, Export in the top-right corner. Ctrl+Z undo, Ctrl+S save.
- DaVinci Resolve ("DaVinci Resolve"). Media Pool bottom-left, Edit page icon (film strip)
  at the bottom, Fusion for effects, Deliver (top-right) for export.
- Adobe Premiere Pro ("Adobe Premiere Pro"). Media panel left, Program monitor centre,
  Timeline bottom, Ctrl+I import, Ctrl+E export, Ctrl+M new sequence from footage.
- Shotcut ("Shotcut"). Open Files (Ctrl+O), timeline bottom, Export panel on the left.
- Windows Video Editor / Clipchamp ("Video Editor", "Clipchamp"). Import -> Add media.
- Photos ("Photos"): select all (Ctrl+A), Video Editor -> Trim / Save as copy.
- Media Player ("Media Player"): Space play/pause, F11 fullscreen.
- OBS ("OBS"): Ctrl+, preferences.
- Any dialog: Enter confirms, Escape cancels, Tab cycles focus, Alt+letter is a menu
  accelerator. Prefer keyboard over hunting for a button when the accelerator is known.
- Setup pages: "ms-settings:display", "ms-settings:sound", "ms-settings:bluetooth",
  "ms-settings:privacy", "ms-settings:defaultapps".
"""

SYSTEM_PROMPT = """\
You are ClipPilot, an operator for a Windows desktop. You complete tasks that mix
real screen interaction with a built-in video engine.

Two capabilities, and you must choose correctly:
1. VIDEO ENGINE (edit_video, plan_video) - exact, fast, non-interactive. Use it for
   every actual cut, trim, concat, speed change, music mix, caption burn and format
   conversion. Never try to click a timeline to cut a clip when you can build a plan.
2. SCREEN CONTROL (click/type/key/scroll/...) - for launching apps, importing your
   rendered files into an editor, filling in export dialogs, and anything the engine
   cannot do.

Operating rules:
- Take exactly ONE action per turn. Then you will see the result and the new screen.
- Target elements by element_id (e12) or by their visible text. Only use raw x/y when
  neither resolves. Never invent an element id that was not in the list.
- After any action that changes state (click, key, type, drag), the next thing you see
  is the fresh screen. Read it before deciding the next step.
- If something did not work, do not repeat the identical action. Change your approach:
  different element, keyboard shortcut, or ask the user.
- Never type passwords, payment data, 2FA codes, or API keys. Use ask_human.
- Prefer keyboard shortcuts over clicking when a reliable accelerator exists.
- Before typing a file path, focus the field (click it, or use Ctrl+L / Ctrl+O), then
  type the FULL absolute path and press Enter.
- Verify before declaring success: use assert, or re-observe and look. Never claim a
  render or import worked without evidence in a tool result.
- When the task is complete, call task_done with a concrete summary and the file paths.
- If you are blocked after 3 genuine attempts, call give_up with what you tried.

Response format: {response_format}
"""

GUIDELINES = """\
Good behaviour:
- "cut the first 12 seconds off C:\\footage\\raw.mp4 and export a vertical version"
  -> plan_video(mode=info) or probe, then edit_video with one segment, then task_done.
- "make a 30s highlight reel from this podcast"
  -> plan_video(mode=highlights), then edit_video with those segments plus transitions,
     then optionally handoff to the user's editor.
- "open the result in CapCut"
  -> handoff(app=capcut) which launches, imports, and reports back.
- "export with captions"
  -> edit_video with captions.enabled=true, burn=true.

Failure modes to avoid:
- Clicking around a timeline and hoping. That is slower and wrong.
- Inventing file paths. Only use paths the user gave, or that appeared in a tool result.
- Retrying a failed click unchanged. Read the error, then pick a different route.
- Leaving a modal dialog open and clicking blindly behind it. Handle the dialog first.
- Reporting success you have not verified with a tool result.
"""


def build_system_prompt(config: Config, native_tools: bool, dry_run: bool = False) -> str:
    env = describe_environment()
    fmt = (
        "Reply with EXACTLY ONE tool call: a single JSON object "
        '{"name": "<action>", "arguments": {...}}. No prose, no markdown fences.'
        if not native_tools
        else "Reply with EXACTLY ONE tool call (function call). No prose, no markdown fences."
    )
    parts = [
        SYSTEM_PROMPT.format(response_format=fmt),
        "Environment: " + env,
    ]
    if dry_run:
        parts.append(
            "NOTE: this session is a DRY RUN. Actions are logged, not executed. "
            "Behave normally, but be extra careful not to claim things happened."
        )
    parts.append(GUIDELINES)
    parts.append(APP_HANDBOOK)
    parts.append("Available actions:\n" + (plain_prompt_schema() if not native_tools else _compact_tool_list()))
    return "\n\n".join(parts)


def _compact_tool_list() -> str:
    from .schema import FUNCTIONS

    return "\n".join(f"- {f['name']}: {f['description'].splitlines()[0]}" for f in FUNCTIONS)


def describe_environment() -> str:
    from ..perception.ocr import describe_backends
    from ..video.ffmpeg import installed as ffmpeg_installed

    ff = ffmpeg_installed()
    ocr = describe_backends().replace("\n", "; ")
    return (
        f"{platform.system()} {platform.release()} ({platform.machine()}), "
        f"python {sys.version_info.major}.{sys.version_info.minor}, "
        f"ffmpeg={'ready' if ff.get('available') else 'MISSING'}, "
        f"ocr=[{ocr}]"
    )


def build_task_prompt(task: str, context: dict[str, Any] | None = None) -> str:
    lines = [f"TASK: {task}"]
    if context:
        for key, value in context.items():
            if value in (None, "", [], {}):
                continue
            lines.append(f"{key.upper()}: {value}")
    lines.append("")
    lines.append("Work step by step. First look at the screen, then act.")
    return "\n".join(lines)


def observation_message(
    text: str,
    screenshot_uri: str | None = None,
    step: int = 0,
) -> list[dict[str, Any]]:
    if screenshot_uri:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": screenshot_uri, "detail": "high"}},
                ],
            }
        ]
    return [{"role": "user", "content": text}]


def result_message(action: dict[str, Any], ok: bool, message: str, extra: str = "") -> str:
    name = action.get("type", "?")
    head = "OK" if ok else "FAILED"
    body = f"[{head}] {name}: {message}"
    if extra:
        body += f"\n{extra}"
    return body


def failure_coach(action: dict[str, Any], message: str) -> str:
    n = int(action.get("_attempts", 0))
    tips = {
        "click": "The target was not found or the click missed. Re-observe, look at the element list, and click a different element (or use a keyboard shortcut).",
        "type": "The text may not have reached the field. Click the field first, or use clear_first=true, or try a keyboard shortcut instead.",
        "key": "The keystroke may not have registered. Make sure the right window is focused (focus_window) and retry once.",
        "drag": "Drags often need a slower motion and a pause before release. Increase duration_ms to 1200-1500 and retry.",
    }
    tip = tips.get(str(action.get("type")), "Change approach: different element, different method, or ask the user.")
    return (
        f"Previous attempt {n} failed: {message}\n"
        f"Coach: {tip}\n"
        f"Do not repeat the same action twice in a row. Try a different approach or ask_human."
    )


def blocked_coach(action: dict[str, Any], tries: int) -> str:
    return (
        f"You have tried to {action.get('type')} the same way {tries} times and it keeps failing. "
        "Stop repeating. Options: (a) ask_human for a hint or for the human to do that step, "
        "(b) accomplish the same thing through the video engine instead, "
        "(c) give_up with a clear explanation."
    )
