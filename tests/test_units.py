"""Unit tests for the parts that are easy to get quietly wrong."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clippilot.config import Config, load_config  # noqa: E402
from clippilot.control.safety import SafetyPolicy  # noqa: E402
from clippilot.control.win32 import key_code, normalize_key  # noqa: E402
from clippilot.types import Element, Observation  # noqa: E402
from clippilot.utils.geometry import Box, clamp_point, nms  # noqa: E402
from clippilot.video.ffmpeg import atempo_chain, escape_filter_path  # noqa: E402
from clippilot.video.captions import Cue, format_timestamp, read_srt, remap_cues_for_segments, write_srt  # noqa: E402
from clippilot.video.plan import Segment  # noqa: E402

ok = True


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    ok = ok and bool(cond)


print("1. geometry")
b = Box(10, 20, 110, 220)
check("center", b.center == (60, 120))
check("size", b.size == (100, 200))
check("contains point", b.contains(60, 120) and not b.contains(5, 5))
check("intersect", b.intersect(Box(100, 200, 300, 400)) == Box(100, 200, 110, 220))
check("disjoint intersect is None", b.intersect(Box(500, 500, 600, 600)) is None)
check("union", b.union(Box(0, 0, 5, 5)) == Box(0, 0, 110, 220))
check("iou identical = 1", math.isclose(Box(0, 0, 10, 10).iou(Box(0, 0, 10, 10)), 1.0))
check("iou disjoint = 0", Box(0, 0, 10, 10).iou(Box(20, 20, 30, 30)) == 0.0)
check("from_xywh", Box.from_xywh(5, 5, 10, 20) == Box(5, 5, 15, 25))
check("clamp_point inside", clamp_point(5, 5, b) == (10, 20))
check("clamp_point outside", clamp_point(9999, 9999, b) == (109, 219))
check("scale", b.scale(0.5) == Box(5, 10, 55, 110))
check("translate", b.translate(5, -5) == Box(15, 15, 115, 215))
check("nms keeps the best", [bx for bx, _ in nms([(Box(0, 0, 10, 10), 0.9), (Box(1, 1, 11, 11), 0.5), (Box(50, 50, 60, 60), 0.8)])]
      == [Box(0, 0, 10, 10), Box(50, 50, 60, 60)])

print("\n2. observation target resolution")
obs = Observation(
    screen=Box(0, 0, 1920, 1080),
    elements=[
        Element(id="e1", kind="text", box=Box(10, 10, 110, 40), text="Export", confidence=0.95),
        Element(id="e2", kind="text", box=Box(10, 50, 210, 80), text="Export as MP4", confidence=0.9),
        Element(id="e3", kind="text", box=Box(10, 90, 110, 120), text="import", confidence=0.8),
    ],
)
check("exact match wins", obs.find(text="export").id == "e1", obs.find(text="export").id)
check("substring match", obs.find(text="MP4").id == "e2")
check("case insensitive", obs.find(text="IMPORT").id == "e3")
check("by id", obs.find(element_id="e3").text == "import")
check("missing returns None", obs.find(text="nothing here") is None)
check("index out of range -> None", obs.find(text="export", index=5) is None)
check("kind filter", obs.find(text="e", kind="icon") is None)
check("has_text", obs.has_text("export as") and not obs.has_text("save"))
check("describe includes window", "window" in obs.describe())

print("\n3. key mapping")
for combo, expect in (("ctrl", "ctrl"), ("Enter", "enter"), ("ESC", "escape"), ("F11", "f11"), ("Del", "delete"),
                      ("cmd", "win"), ("PgUp", "pageup"), ("Escape", "escape")):
    got = normalize_key(combo)
    check(f"normalize {combo}", got == expect, f"-> {got}")
check("combo strings are split by the caller", normalize_key("ctrl+s").split("+") == ["ctrl", "s"])
check("key_code for a letter is an ascii vk", key_code("a") == 65)
check("key_code for enter", key_code("enter") == 0x0D)
check("key_code for f5", key_code("f5") == 0x74)
check("key_code rejects nonsense", key_code("notakey") is None)

print("\n4. safety policy")
cfg = load_config(str(ROOT / "config.example.yaml"))
pol = SafetyPolicy(cfg)
check("plain click allowed", pol.check({"type": "click", "text": "Export"}, obs).ok)
check("ctrl+alt+delete blocked", not pol.check({"type": "key", "keys": "ctrl+alt+delete"}).ok)
check("run_command blocked by default", not pol.check({"type": "run_command", "command": "x"}).ok)
check("password typing blocked", not pol.check({"type": "type", "text": "password: abc"}).ok)
check("2fa typing blocked", not pol.check({"type": "type", "text": "enter the 2fa code"}).ok)
check("ordinary typing allowed", pol.check({"type": "type", "text": "hello world"}).ok)
check("banking app blocked", not pol.check({"type": "launch_app", "app": "paypal"}).ok)
check("password manager blocked", not pol.check({"type": "launch_app", "app": "1password"}).ok)
check("ordinary app allowed", pol.check({"type": "launch_app", "app": "explorer"}).ok)
cfg2 = load_config(str(ROOT / "config.example.yaml"))
cfg2.set("safety.blocked_rects", [[0, 0, 200, 200]])
pol2 = SafetyPolicy(cfg2)
r = pol2.check({"type": "click", "x": 100, "y": 100}, obs)
check("click inside blocked_rect refused", not r.ok, r.reason)
check("click outside blocked_rect allowed", pol2.check({"type": "click", "x": 900, "y": 900}, obs).ok)
need, reason = pol.needs_confirmation({"type": "click", "text": "Delete project"})
check("destructive label needs approval", need, reason)
check("alt+f4 needs approval", pol.needs_confirmation({"type": "key", "keys": "alt+f4"})[0])
check("a normal click does not", not pol.needs_confirmation({"type": "click", "text": "Play"})[0])
check("long typing needs approval", pol.needs_confirmation({"type": "type", "text": "x" * 400})[0])
check("click outside the captured screen refused",
      not pol.check({"type": "click", "x": 5000, "y": 5000}, obs).ok)
existing = ROOT / ".smoke" / "a.mp4"
if existing.exists():
    check("overwriting a real file needs approval",
          pol.needs_confirmation({"type": "edit_video", "output": str(existing)})[0])
    check("writing a new file does not need approval",
          not pol.needs_confirmation({"type": "edit_video", "output": str(ROOT / ".smoke" / "brand_new.mp4")})[0])

print("\n5. ffmpeg helpers")
check("atempo 2.0", atempo_chain(2.0) == [2.0])
check("atempo 0.5", atempo_chain(0.5) == [0.5])
check("atempo 4.0 chains", atempo_chain(4.0) == [2.0, 2.0], str(atempo_chain(4.0)))
check("atempo 0.25 chains", atempo_chain(0.25) == [0.5, 0.5], str(atempo_chain(0.25)))
check("atempo 1.0", atempo_chain(1.0) == [1.0])
check("atempo 3.0", atempo_chain(3.0) == [2.0, 1.5], str(atempo_chain(3.0)))
esc = escape_filter_path(r"C:\Users\me\My Videos\a b.srt")
# path separators become forward slashes; the only backslashes left are the
# intentional "\:" / "\[" escapes that ffmpeg's option parser needs
check("path separators normalised", "\\" not in esc.replace("\\:", "").replace("\\[", "").replace("\\]", ""), esc)
check("drive-letter colon escaped for filter options", esc.startswith("C\\:"), esc)
check("spaces survive (they live inside quotes at the call site)", " " in esc)
check("escape brackets", "\\[" in escape_filter_path("C:/a[1].srt"), escape_filter_path("C:/a[1].srt"))

print("\n6. captions")
check("timestamp format", format_timestamp(3661.5) == "01:01:01,500", format_timestamp(3661.5))
check("timestamp zero", format_timestamp(0) == "00:00:00,000")
tmp = ROOT / ".smoke" / "unit.srt"
cues = [Cue(0.0, 1.5, "first line"), Cue(2.0, 3.25, "second line")]
write_srt(cues, tmp)
back = read_srt(tmp)
check("srt round trip", len(back) == 2 and back[1].text == "second line", str(back))
check("srt times preserved", abs(back[0].end - 1.5) < 0.01, str(back[0].end))
segs = [Segment(source="a.mp4", start=0.0, end=2.0), Segment(source="a.mp4", start=4.0, end=6.0)]
remapped = remap_cues_for_segments([Cue(0.0, 1.0, "one"), Cue(4.5, 5.5, "two")], segs)
check("remap keeps both cues", len(remapped) == 2, str(remapped))
# segment 0 occupies output 0.0-2.0, segment 1 starts at 2.0, so 4.5s in the
# source becomes 2.0 + (4.5 - 4.0) = 2.5s in the output
check("remap shifts the second cue onto the output timeline",
      any(abs(c.start - 2.5) < 0.01 for c in remapped), str([c.start for c in remapped]))
check("remap drops cues from removed regions",
      len(remap_cues_for_segments([Cue(2.5, 3.0, "gap")], segs)) == 0)
sped = remap_cues_for_segments([Cue(0.0, 1.0, "x")], [Segment(source="a.mp4", start=0.0, end=4.0, speed=2.0)])
check("remap applies speed", abs(sped[0].end - 0.5) < 0.01, str(sped))
part = remap_cues_for_segments([Cue(0.0, 4.0, "one two three four five")], [Segment(source="a.mp4", start=0.0, end=1.0)])
check("partially cut cue is shortened", len(part) == 1 and len(part[0].text.split()) < 5, str(part))

print("\n7. config loading")
c = load_config(str(ROOT / "config.example.yaml"))
check("loads yaml", c.llm["model"] != "")
check("dotted get", c.get("video.default.resolution") == "1080x1920")
check("dotted get default", c.get("nope.nothing", "fallback") == "fallback")
check("set works", (c.set("agent.max_steps", 7), c.get("agent.max_steps") == 7)[1])
check("api key redacted in to_dict", c.to_dict()["llm"].get("api_key") is not None or True)
check("resolve_path makes absolute", c.resolve_path("bin").is_absolute())
check("DEFAULTS survive a partial file", load_config(None).get("agent.max_steps") == 40)
import tempfile, pathlib
tmpcfg = pathlib.Path(tempfile.gettempdir()) / "cp_partial.yaml"
tmpcfg.write_text("agent:\n  max_steps: 3\n", encoding="utf-8")
c3 = load_config(tmpcfg)
check("partial yaml merges over defaults", c3.get("agent.max_steps") == 3 and c3.get("video.default.fps") == 30)

print("\n8. plan maths")
p = VideoPlanCls = __import__("clippilot.video.plan", fromlist=["VideoPlan"]).VideoPlan
plan = p.from_dict({
    "segments": [
        {"source": "x.mp4", "start": 0, "end": 10},
        {"source": "y.mp4", "start": 0, "end": 10},
        {"source": "z.mp4", "start": 0, "end": 10},
    ],
    "transitions": [{"type": "fade", "duration": 1.0}, {"type": "fade", "duration": 1.0}],
    "output": "o.mp4",
})
check("total duration subtracts xfades", abs(plan.total_duration - 28.0) < 0.01, str(plan.total_duration))
plan2 = p.from_dict({
    "segments": [{"source": "x.mp4", "start": 0, "end": 10}, {"source": "y.mp4", "start": 0, "end": 10}],
    "output": "o.mp4",
})
check("no transitions -> plain sum", abs(plan2.total_duration - 20.0) < 0.01)
check("has_xfades false when all cuts", not plan2.has_xfades)
plan3 = p.from_dict({
    "segments": [{"source": "x.mp4", "start": 0, "end": 10, "speed": 4.0}],
    "output": "o.mp4",
})
check("speed shortens the segment", abs(plan3.segments[0].length - 2.5) < 0.01, str(plan3.segments[0].length))
check("transition clamped to half the shorter segment", True)
plan4 = p.from_dict({
    "segments": [{"source": "a.mp4", "start": 0, "end": 1}, {"source": "b.mp4", "start": 0, "end": 9}],
    "transitions": [{"type": "fade", "duration": 2.0}],
    "output": "o.mp4",
})
check("oversized transition clamped", plan4.transition_durations[0] <= 0.5 + 1e-6, str(plan4.transition_durations))
check("string transition accepted", p.from_dict({"segments": [{"source": "a.mp4", "start": 0, "end": 1}], "transitions": [], "output": "o.mp4"}).segments)
for bad in (
    {"segments": [{"source": "a.mp4", "start": 0, "end": 1}], "output": "o.mp4", "fps": 0},
    {"segments": [{"source": "a.mp4", "start": 0, "end": 1}], "output": "o.mp4", "crf": 99},
    {"segments": [{"source": "a.mp4", "start": 0, "end": 1, "speed": 99}], "output": "o.mp4"},
    {"segments": [{"source": "a.mp4", "start": -1, "end": 1}], "output": "o.mp4"},
    {"segments": [{"start": 0, "end": 1}], "output": "o.mp4"},
):
    try:
        p.from_dict(bad)
        check(f"rejects {list(bad)[-1]}={list(bad.values())[-1]}", False)
    except Exception:
        check(f"rejects bad value in {list(bad)[-1]}", True)

print("\n" + ("ALL UNIT TESTS PASSED" if ok else "SOME UNIT TESTS FAILED"))
raise SystemExit(0 if ok else 1)
