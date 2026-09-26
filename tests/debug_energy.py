import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clippilot.video.highlights import energy_profile  # noqa: E402

exe = sys.argv[1] if len(sys.argv) > 1 else "bin/ffmpeg.exe"
media = sys.argv[2] if len(sys.argv) > 2 else ".smoke/bursts.mp4"
for d in energy_profile(exe, media, window=1.0, hop=0.5):
    bar = "#" * int(d["score"] * 40)
    print(f"{d['start']:5.1f}-{d['end']:5.1f} {d['score']:.3f} {bar}")
