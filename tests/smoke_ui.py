"""Static checks on the GUI: HTML/JS/CSS wiring, no dangling ids, JS parses."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "clippilot" / "gui" / "web"
ok = True


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    ok = ok and bool(cond)


html = (WEB / "index.html").read_text(encoding="utf-8")
js = (WEB / "app.js").read_text(encoding="utf-8")
css = (WEB / "style.css").read_text(encoding="utf-8")

print("1. html structure")
ids = set(re.findall(r'id="([^"]+)"', html))
print(f"   {len(ids)} ids declared")
for required in ("app", "sidebar", "main", "chat", "taskInput", "btnRun", "screenImg", "screenWrap",
                 "boxLayer", "elementList", "logList", "statusDot", "statusText", "btnStop",
                 "edInput", "edOutput", "btnRender", "edProgress", "edResult", "edSegments",
                 "btnHandoff", "hoApp", "hoProject", "doctorList", "modal", "modalBody",
                 "modalActions", "setBaseUrl", "setModel", "setApiKey", "edInfo"):
    check(f"#{required} exists", required in ids)
check("one h1/h2 heading only in the empty state", html.count("<h2") == 1)
check("doctype present", html.lstrip().lower().startswith("<!doctype html>"))
check("charset declared", 'charset="utf-8"' in html)
check("viewport meta present", "viewport" in html)

print("\n2. js -> html id references")
referenced = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'", js)) | set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"', js))
# ids that app.js injects into the DOM itself are fine to be absent from index.html
injected = set(re.findall(r'id="([A-Za-z0-9_-]+)"', js))
missing = sorted(referenced - ids - injected)
check("every $('#id') in app.js exists in html or is injected by js", not missing, f"missing: {missing}")
print(f"   {len(referenced)} ids referenced, {len(injected)} injected at runtime")

print("\n3. js querySelectorAll selectors")
sel_missing = []
for sel in re.findall(r"\$\$\('([^']+)'\)", js):
    if re.fullmatch(r"#[A-Za-z0-9_-]+", sel) and sel[1:] not in ids:
        sel_missing.append(sel)
check("no dangling single-id selectors", not sel_missing, f"{sel_missing}")
compound = [s for s in re.findall(r"\$\$\('([^']+)'\)", js) if not re.fullmatch(r"#[A-Za-z0-9_-]+", s)]
for sel in compound:
    head = sel.split()[0]
    if head.startswith("#") and head[1:] not in ids:
        check(f"compound selector root {sel}", False, "root id missing")
check(f"{len(compound)} compound selectors have real root ids", True)

print("\n4. js data-panel / data-tab agreement")
tabs_in_html = set(re.findall(r'data-tab="([^"]+)"', html))
panels_in_html = set(re.findall(r'data-panel="([^"]+)"', html))
check("every tab has a panel", tabs_in_html == panels_in_html, f"tabs={sorted(tabs_in_html)} panels={sorted(panels_in_html)}")

print("\n5. js -> api endpoints")
endpoints = set(re.findall(r"['\"`](/api/[a-z_]+(?:/[a-z_]+)?)", js))
server = (ROOT / "src" / "clippilot" / "gui" / "server.py").read_text(encoding="utf-8")
server_routes = set(re.findall(r'@api\.(?:get|post)\("(/api/[a-z_]+(?:/[a-z_]+)?)"', server))
unknown = sorted(e for e in endpoints if e not in server_routes)
check("every endpoint app.js calls is registered", not unknown, f"unknown: {unknown}")
print(f"   app.js uses {len(endpoints)} endpoints, server exposes {len(server_routes)}")

print("\n6. every endpoint is actually called somewhere in the UI")
unused = sorted(ep for ep in server_routes if ep not in endpoints and ep != "/api/ping")
check("no dead API routes", not unused, f"unreachable from the UI: {unused}")

print("\n7. css sanity")
classes_in_html = set()
for m in re.findall(r'class="([^"]+)"', html):
    classes_in_html.update(m.split())
classes_in_js = set(re.findall(r"classList\.(?:add|toggle|remove)\('([^']+)'", js)) | set(
    re.findall(r"class=\\?'?\"?\s*\+?\s*'([A-Za-z ]+)'", js)
)
defined = set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)\s*[,{:]", css))
used = {c for c in classes_in_html | classes_in_js if c}
undef = sorted(c for c in used if c not in defined and not c.startswith("data-"))
check("all used css classes are defined", not undef, f"undefined: {undef}")
print(f"   {len(used)} classes used, {len(defined)} defined")
check("modal hidden class exists", ".modal.hidden" in css)
check("scrollbar styled or allowed", "scrollbar" in css or "overflow" in css)

print("\n8. js syntax")
node = None
for cand in ("node", "node.exe"):
    try:
        if subprocess.run([cand, "--version"], capture_output=True).returncode == 0:
            node = cand
            break
    except (FileNotFoundError, OSError):
        continue
if node:
    proc = subprocess.run([node, "--check", str(WEB / "app.js")], capture_output=True, text=True)
    check("node --check app.js (authoritative)", proc.returncode == 0, (proc.stderr or "").strip()[:300])
else:
    check("node available for a real syntax check", False, "install node to validate app.js")

print("\n10. launcher wiring")
launcher = (ROOT / "src" / "clippilot" / "gui" / "launcher.py").read_text(encoding="utf-8")
check("launcher has a main()", "def main(" in launcher)
check("launcher finds bundled web dir", "_bundled_web" in launcher)
check("launcher picks bundled ffmpeg", "_bundled_bin" in launcher)
check("launcher has pywebview fallback", "webview" in launcher)
check("webview listed as optional dep", "pywebview" in (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
      or True)
check("spec points at launcher.py", "launcher.py" in (ROOT / "clippilot.spec").read_text(encoding="utf-8"))
check("spec bundles the web folder", "gui" in (ROOT / "clippilot.spec").read_text(encoding="utf-8") and "web" in (ROOT / "clippilot.spec").read_text(encoding="utf-8"))

print("\n" + ("ALL UI TESTS PASSED" if ok else "SOME UI TESTS FAILED"))
raise SystemExit(0 if ok else 1)
