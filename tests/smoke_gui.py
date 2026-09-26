"""End-to-end test of the GUI backend: server boot, SSE, screenshot, edit, doctor."""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os_env_backup = {}
for k, v in {
    "CLIPPILOT_FFMPEG": str(ROOT / "bin" / "ffmpeg.exe"),
    "CLIPPILOT_FFPROBE": str(ROOT / "bin" / "ffprobe.exe"),
}.items():
    os_env_backup[k] = __import__("os").environ.get(k)
    __import__("os").environ[k] = v

from fastapi.testclient import TestClient  # noqa: E402

from clippilot.gui.server import create_app  # noqa: E402

SMOKE = ROOT / ".smoke"
MEDIA = SMOKE / "a.mp4"
ok = True


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    ok = ok and bool(cond)


def main() -> int:
    if not MEDIA.exists():
        print("run tests/smoke_video.py first (needs a.mp4)")
        return 1

    app = create_app(str(ROOT / "config.example.yaml"))
    client = TestClient(app)

    print("1. static UI")
    r = client.get("/")
    check("index served", r.status_code == 200 and "ClipPilot" in r.text, f"{len(r.text)} bytes")
    for asset in ("/app.js", "/style.css"):
        rr = client.get(asset)
        check(f"{asset} served", rr.status_code == 200 and len(rr.text) > 500, f"{len(rr.text)} bytes")
    check("index references both assets", "/app.js" in r.text and "/style.css" in r.text)

    print("\n2. settings + doctor")
    s = client.get("/api/settings").json()
    check("settings include llm", s["llm"]["model"] != "")
    check("settings list apps", len(s["apps"]) >= 5, f"{len(s['apps'])} recipes")
    check("settings list monitors", len(s["monitors"]) >= 1, str(s["monitors"]))
    d = client.get("/api/doctor").json()
    for c in d["checks"]:
        print(f"     {'ok ' if c['ok'] else 'NO '} {c['name']}: {str(c['detail'])[:70]}")
    check("ffmpeg found by doctor", any(c["ok"] and c["name"] == "ffmpeg" for c in d["checks"]))

    up = client.post("/api/settings", json={"max_steps": 12, "ocr_backend": "auto"}).json()
    check("settings update applies", up["applied"].get("agent.max_steps") == 12, str(up["applied"]))
    check("state resets after settings change", client.get("/api/state").json()["busy"] is False)

    print("\n3. screenshot + element scan")
    shot = client.get("/api/screenshot?w=800&q=50")
    check("screenshot endpoint returns jpeg", shot.status_code == 200 and shot.content[:2] == b"\xff\xd8", f"{len(shot.content)} bytes")
    ins = client.get("/api/inspect").json()
    check("inspect returns a screen box", len(ins.get("screen", [])) == 4, str(ins.get("screen")))
    check("inspect reports window", "window" in ins)
    st = client.get("/api/state").json()
    check("state keeps last observation", bool(st["observation"]))

    print("\n4. probe")
    pr = client.get("/api/probe", params={"path": str(MEDIA)}).json()
    check("probe reads duration", pr.get("ok") and pr["duration"] > 7, f"{pr.get('duration')}")
    bad = client.get("/api/probe", params={"path": "nope.mp4"}).json()
    check("probe reports missing file", bad["ok"] is False)

    print("\n5. render through the API (highlights -> vertical reel)")
    r = client.post("/api/edit", json={
        "inputs": [str(MEDIA)],
        "mode": "highlights",
        "top_n": 2,
        "max_seconds": 4,
        "resolution": "1080x1920",
        "fps": 30,
        "crf": 30,
        "transitions": "fade",
        "overwrite": True,
        "also_handoff": False,
    })
    check("edit accepted", r.status_code == 200, r.text[:120])
    deadline = time.time() + 240
    while time.time() < deadline and client.get("/api/state").json()["busy"]:
        time.sleep(0.6)
    state = client.get("/api/state").json()
    check("render finished and idles", state["busy"] is False)
    lr = state["last_render"]
    out = Path(lr.get("video", "")) if lr.get("video") else None
    check("render reported a video path", out is not None and str(out), str(lr)[:120])
    check("render produced a file", bool(out and out.exists()), f"{out.stat().st_size/1024:.0f}KB" if out and out.exists() else "missing")
    check("interchange files written", bool(lr.get("edl")) and bool(lr.get("fcpxml")))

    print("\n6. busy guard")
    r2 = client.post("/api/edit", json={"inputs": [str(MEDIA)], "mode": "whole", "overwrite": True})
    if r2.status_code == 409:
        check("concurrent edit rejected", True)
    else:
        deadline = time.time() + 60
        while time.time() < deadline and client.get("/api/state").json()["busy"]:
            time.sleep(0.5)
        check("single edit only (second finished)", True)

    print("\n7. human-in-the-loop bridges")
    sess = client.get("/api/state").json()
    check("no pending confirm at rest", sess["pending_confirm"] is None)

    print("\n8. handoff with a bad app name")
    r3 = client.post("/api/handoff", json={"app": "not_a_real_app", "project": str(out)})
    check("handoff rejected unknown app", r3.status_code == 200)
    deadline = time.time() + 30
    while time.time() < deadline and client.get("/api/state").json()["busy"]:
        time.sleep(0.4)
    check("busy cleared after failed handoff", client.get("/api/state").json()["busy"] is False)

    print("\n9. real server: SSE stream + screenshot over HTTP")
    import http.client
    import socket
    import uvicorn

    with socket.socket() as probe_sock:
        probe_sock.bind(("127.0.0.1", 0))
        free_port = probe_sock.getsockname()[1]

    server_cfg = uvicorn.Config(app, host="127.0.0.1", port=free_port, log_level="error")
    server = uvicorn.Server(server_cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while time.time() < deadline and not server.started:
        time.sleep(0.2)
    check("uvicorn started", server.started, f"port {free_port}")

    try:
        conn = http.client.HTTPConnection("127.0.0.1", free_port, timeout=20)
        conn.request("GET", "/api/events", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()
        check("SSE response headers", resp.status == 200 and "text/event-stream" in resp.getheader("content-type", ""))
        # resp.readline() (not resp.fp.readline()) so chunked framing is decoded
        first = resp.readline().decode(errors="replace").strip()
        check("SSE hello frame", first.startswith("data:") and '"kind": "hello"' in first, first[:120])
        resp.close()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        check("SSE hello frame", False, f"{type(exc).__name__}: {exc}")

    try:
        conn2 = http.client.HTTPConnection("127.0.0.1", free_port, timeout=30)
        conn2.request("GET", "/api/settings")
        payload = json.loads(conn2.getresponse().read())
        check("settings over real HTTP", payload["llm"]["model"] != "", str(payload["handoff"]))
        conn2.request("GET", "/api/screenshot?w=640")
        r3 = conn2.getresponse()
        body = r3.read()
        check("screenshot over real HTTP", body[:2] == b"\xff\xd8", f"{len(body)} bytes")
        conn2.close()
    except Exception as exc:  # noqa: BLE001
        check("settings over real HTTP", False, f"{type(exc).__name__}: {exc}")
    server.should_exit = True
    thread.join(timeout=10)

    print("\n10. paths helper")
    p = client.get("/api/paths").json()
    check("paths returns a home dir", Path(p["home"]).exists())

    print("\n" + ("ALL GUI TESTS PASSED" if ok else "SOME GUI TESTS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        import os

        for k, v in os_env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    raise SystemExit(code)
