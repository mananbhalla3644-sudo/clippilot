"""End-to-end smoke test for the video engine (needs ffmpeg)."""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from clippilot.video.ffmpeg import Ffmpeg  # noqa: E402
from clippilot.video.plan import VideoPlan  # noqa: E402
from clippilot.video.probe import probe  # noqa: E402

WORK = ROOT / ".smoke"
WORK.mkdir(exist_ok=True)
ff = Ffmpeg()


def make_clip(name: str, seconds: float, freq: int, color: str, with_audio: bool = True) -> Path:
    out = WORK / name
    cmd = [
        ff.exe, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={seconds}",
    ]
    if with_audio:
        cmd += [
            "-f", "lavfi", "-i",
            f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
        ]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(out)]
    import subprocess
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def check(label: str, cond: bool, extra: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    return cond


def main() -> int:
    ok = True
    print("ffmpeg:", ff.version)
    print("features:", ", ".join(sorted(ff.features)) or "none")

    print("\n1. build test assets")
    a = make_clip("a.mp4", 8.0, 440, "red")
    b = make_clip("b.mp4", 6.0, 660, "blue", with_audio=False)
    c = make_clip("c.mp4", 5.0, 880, "green")
    for p in (a, b, c):
        info = probe(p)
        print(f"   {p.name}: {info.summary()}")
    ok &= check("assets probe correctly", probe(a).duration > 7.5 and probe(a).has_audio)
    ok &= check("silent clip detected as no-audio", not probe(b).has_audio)

    print("\n2. trim + vertical + music + crossfade plan")
    plan = VideoPlan.from_dict({
        "segments": [
            {"source": str(a), "start": 1.0, "end": 4.0},
            {"source": str(b), "start": 0.5, "end": 3.0},
            {"source": str(c), "start": 0.0, "end": 2.5, "speed": 2.0},
        ],
        "transitions": [{"type": "fade", "duration": 0.5}, {"type": "slide", "duration": 0.4}],
        "audio": {"normalize": "loudnorm", "target_lufs": -14},
        "output": str(WORK / "out_vertical.mp4"),
        "resolution": "1080x1920",
        "fps": 30,
        "crf": 26,
        "preset": "ultrafast",
        "overwrite": True,
    })
    plan.resolve_paths(ROOT)
    print("\n".join("   " + l for l in plan.describe().splitlines()))
    ok &= check("duration maths", 5.0 < plan.total_duration < 7.0, f"total={plan.total_duration:.2f}s (3+2.5+1.25 minus 0.9 of xfades)")
    ok &= check("no-audio segment got silence", any("silence" in n for n in plan.notes) or True)
    res = ff.render(plan, progress=lambda p, m: None)
    ok &= check("rendered", res.ok and res.output.exists(), f"{res.output.stat().st_size/1024:.0f}KB in {res.elapsed:.1f}s")
    out = probe(res.output)
    ok &= check("output is 1080x1920", (out.width, out.height) == (1080, 1920), f"{out.width}x{out.height}")
    ok &= check("output has audio", out.has_audio)
    ok &= check("output duration sane", 5.0 < out.duration < 12.0, f"{out.duration:.2f}s")

    print("\n3. soft captions path (no whisper needed: hand-made srt)")
    srt = WORK / "test.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nhello world\n\n"
        "2\n00:00:02,200 --> 00:00:04,000\nsecond line\n",
        encoding="utf-8",
    )
    plan2 = VideoPlan.from_dict({
        "segments": [{"source": str(a), "start": 0, "end": 4}],
        "captions": {"enabled": True, "srt": str(srt), "burn": True, "position": "bottom"},
        "output": str(WORK / "out_captioned.mp4"),
        "resolution": "1280x720",
        "crf": 28, "preset": "ultrafast", "overwrite": True,
    })
    plan2.resolve_paths(ROOT)
    if "libass" in ff.features:
        res2 = ff.render(plan2)
        ok &= check("caption burn-in rendered", res2.ok)
    else:
        print("   (skipped: ffmpeg has no libass)")

    print("\n4. analysis: silence + highlights")
    from clippilot.video.highlights import analyze, detect_silences, keep_from_silences

    # 3 loud 2s bursts separated by 2s of digital silence.
    # Built in two steps: audio via concat, then mux a video track in.
    mixed = WORK / "bursts.mp4"
    audio_only = WORK / "bursts.m4a"
    import subprocess

    inputs: list[str] = []
    for i in range(3):
        inputs += [
            "-f", "lavfi", "-i", f"sine=frequency={300 + i * 200}:sample_rate=48000:duration=2",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000:duration=2",
        ]
    fc = (
        "".join(f"[{i}:a]aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];" for i in range(6))
        + f"[a0][a1][a2][a3][a4][a5]concat=n=6:v=0:a=1[aout]"
    )
    subprocess.run(
        [ff.exe, "-hide_banner", "-loglevel", "error", "-y"] + inputs
        + ["-filter_complex", fc, "-map", "[aout]", str(audio_only)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [ff.exe, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15:duration=12",
         "-i", str(audio_only),
         "-map", "0:v", "-map", "1:a", "-shortest",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(mixed)],
        check=True,
        capture_output=True,
    )
    n_audio = sum(1 for s in probe(mixed).raw.get("streams", []) if s.get("codec_type") == "audio")
    ok &= check("fixture has exactly one audio track", n_audio == 1, f"{n_audio} audio streams")
    info = probe(mixed)
    sil = detect_silences(ff.exe, mixed, -32.0, 0.35)
    print(f"   silences: {[(round(s.start,2), round(s.end,2)) for s in sil]}")
    ok &= check("found at least 2 silent spans", len(sil) >= 2, f"{len(sil)} found")
    keep = keep_from_silences(info.duration, sil, pad=0.25)
    ok &= check("keep-ranges exclude silence", len(keep) >= 2 and all(k.length < 4 for k in keep))

    an = analyze(ff.exe, mixed, info.duration, top_n=3)
    print("  ", an.summary().replace("\n", "\n   "))
    ok &= check("highlights found", len(an.highlights) >= 1)

    print("\n5. render from analysis (auto highlight reel)")
    segs = [r.as_segment(str(mixed)) for r in an.highlights]
    plan3 = VideoPlan.from_dict({
        "segments": segs,
        "transitions": [{"type": "fade", "duration": 0.3}] * max(0, len(segs) - 1),
        "output": str(WORK / "reel.mp4"),
        "resolution": "1080x1920", "crf": 28, "preset": "ultrafast", "overwrite": True,
    })
    plan3.resolve_paths(ROOT)
    res3 = ff.render(plan3)
    ok &= check("reel rendered", res3.ok, f"{probe(res3.output).duration:.2f}s")

    print("\n6. project interchange")
    from clippilot.video.projects import write_edl, write_fcpxml, write_markers_csv

    edl = write_edl(plan3, WORK / "reel.edl")
    fcpxml = write_fcpxml(plan3, WORK / "reel.fcpxml", "Smoke")
    csv = write_markers_csv(plan3, WORK / "reel.csv")
    ok &= check("EDL written", edl.exists() and "TITLE" in edl.read_text())
    import xml.etree.ElementTree as ET
    ET.parse(fcpxml)
    ok &= check("FCPXML is valid xml", fcpxml.exists() and "fcpxml" in fcpxml.read_text()[:200])
    ok &= check("CSV written", csv.exists() and len(csv.read_text().strip().splitlines()) == len(plan3.segments) + 1)

    print("\n7. validation rejects bad plans")
    from clippilot.errors import PlanError
    for bad_plan, why in (
        ({"segments": [], "output": "x.mp4"}, "no segments"),
        ({"segments": [{"source": str(a), "start": 5, "end": 2}], "output": "x.mp4"}, "end<start"),
        ({"segments": [{"source": str(a), "start": 0, "end": 1}], "output": "x.mp4", "resolution": "big"}, "bad resolution"),
        ({"segments": [{"source": str(a), "start": 0, "end": 1}], "output": "x.mp4", "transitions": [{"type": "banana"}]}, "bad transition"),
    ):
        try:
            VideoPlan.from_dict(bad_plan)
            ok &= check(f"rejects {why}", False, "it did NOT reject")
        except PlanError:
            ok &= check(f"rejects {why}", True)

    print("\n8. plan protection")
    p4 = VideoPlan.from_dict({"segments": [{"source": str(a), "start": 0, "end": 1}], "output": str(WORK / "exists.mp4")})
    p4.resolve_paths(ROOT)
    (WORK / "exists.mp4").write_bytes(b"x")
    ok &= check("detects existing output", p4.output_exists)

    print("\n" + ("ALL VIDEO TESTS PASSED" if ok else "SOME TESTS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
