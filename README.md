# ClipPilot

An agent that edits video with ffmpeg, and drives Windows apps by real screen
control. It reads the screen, decides what to click, clicks it, then checks
whether that worked before doing the next thing.

Two halves, and you can use either on its own:

- **Video** — probe a file, find the highlights, re-cut them vertical or
  horizontal, burn in captions, normalise audio, export reel EDL / CSV /
  FCPXML.
- **Screen control** — launch an editor, focus its window, walk it through an
  import, and hand the rendered clip over. `recipes/` holds one YAML file per
  app.

## Requirements

- Windows 10/11
- Python 3.10+
- ffmpeg and ffprobe (auto-discovered, or `setup_windows.ps1` fetches them)

## Setup

```powershell
powershell -ExecutionPolicy Bypass -File setup_windows.ps1
```

Safe to re-run. It installs the Python dependencies, downloads ffmpeg and
tesseract into `bin/`, and writes a `config.yaml` you can then edit.

```powershell
powershell -ExecutionPolicy Bypass -File setup_windows.ps1 -SkipDownloads  # deps only
powershell -ExecutionPolicy Bypass -File setup_windows.ps1 -Exe            # also build dist\ClipPilot.exe
```

## Run

```bash
python launcher.py          # GUI
python -m clippilot         # CLI
```

## Configuration

`config.yaml` is git-ignored. Copy `config.example.yaml` and edit that instead —
it is commented throughout and is the better documentation of what is tunable.

Any `${ENV_VAR}` or `${ENV_VAR:default}` string is interpolated from the
environment, so keys do not have to live in the file at all.

```bash
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=http://localhost:11434/v1   # Ollama
export OPENAI_MODEL=qwen2.5-vl:7b
```

`llm.base_url` accepts any OpenAI-compatible endpoint: OpenAI, OpenRouter, Groq,
Ollama, LM Studio. `llm.native_tools: false` forces plain-JSON mode, which
helps with providers that advertise tool calling and then ignore it.

Optional extras, installed with `pip install -e ".[ocr]"` and friends:

| Extra | Adds |
| --- | --- |
| `ocr` | Tesseract OCR |
| `winsdk` | the Windows OCR engine, no Tesseract needed |
| `whisper` | `faster-whisper` for captions |
| `dev` | pytest |

## Safety

Screen control is the risky half, so the defaults lean conservative:

- `safety.require_confirmation: true`, and `confirm_on` covers `type_text`,
  `run_command`, `delete`, `export_overwrite`, `close_app`, `install`
- `control.dry_run: true` logs every action and touches nothing
- `control.failsafe: true` aborts instantly when the mouse is slammed into the
  top-left corner
- `safety.refuse_password_fields: true`
- `safety.max_clicks_per_minute` and `max_keys_per_minute` cap throughput
- `safety.blocked_rects` keeps the agent out of a screen region

Turn these down deliberately, one at a time, and read the diff.

## Handoff recipes

`recipes/_template.yaml` is the starting point. Copy it to
`recipes/<yourapp>.yaml`, set `key` and `app`, and the app appears in the
Handoff tab. Placeholders available in step fields: `{project}`, `{dir}`,
`{filename}`, `{stem}`.

## Layout

```
src/clippilot/
  agent/        the loop: plan, act, observe, verify
  perception/   screen capture, OCR, template matching, VLM
  control/      win32 + pyautogui actuators, safety gate
  video/        ffmpeg/ffprobe wrappers, highlights, captions, plan
  handoff/      recipe loading and running
  gui/          local web GUI
  utils/        binaries, geometry, imaging, logging
tests/          unit + smoke tests
```

## Tests

```bash
python -m pytest tests/
```

`tests/smoke_*.py` drive the real GUI and the real ffmpeg. They need a desktop
session and will move your mouse.

## Packaging

`clippilot.spec` is the PyInstaller spec. `setup_windows.ps1 -Exe` uses it to
produce `dist/ClipPilot.exe`.

## License

MIT
