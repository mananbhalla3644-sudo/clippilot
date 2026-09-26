"""Live perception test: capture the real screen and read it with OCR."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from clippilot.config import load_config  # noqa: E402
from clippilot.observation import PerceptionEngine  # noqa: E402
from clippilot.utils.imaging import save  # noqa: E402

out_dir = Path(__file__).resolve().parents[1] / ".smoke" / "perception"
out_dir.mkdir(parents=True, exist_ok=True)

# A synthetic "app window" so the test does not depend on what is on screen.
fake = Image.new("RGB", (900, 420), (28, 30, 38))
d = ImageDraw.Draw(fake)
d.rectangle([0, 0, 900, 44], fill=(18, 20, 26))
d.text((16, 15), "Media Editor  -  project_01.mp4", fill=(225, 230, 240))
d.rectangle([16, 60, 300, 300], fill=(45, 48, 58))
d.text((30, 70), "Media Pool", fill=(210, 215, 225))
for i, name in enumerate(["clip_01.mp4", "clip_02.mp4", "interview_take3.mov", "music_bed.wav"]):
    d.rectangle([28, 96 + i * 40, 288, 126 + i * 40], outline=(90, 95, 110))
    d.text((38, 105 + i * 40), name, fill=(200, 205, 215))
d.rectangle([320, 60, 700, 300], fill=(20, 22, 28))
d.text((420, 170), "PREVIEW", fill=(120, 200, 240))
d.rectangle([320, 320, 700, 380], fill=(60, 63, 74))
for i in range(9):
    d.rectangle([330 + i * 40, 330, 366 + i * 40, 372], fill=(80, 150, 200) if i % 2 else (70, 80, 95))
d.rectangle([716, 60, 884, 300], fill=(45, 48, 58))
for i, label in enumerate(["Export", "Trim", "Speed", "Captions", "Audio Mix"]):
    d.rectangle([730, 80 + i * 42, 870, 112 + i * 42], fill=(60, 63, 74))
    d.text((745, 90 + i * 42), label, fill=(220, 225, 235))
fake_path = save(fake, out_dir / "fake_app.png")
print(f"synthetic window: {fake_path}")

cfg = load_config("config.example.yaml")
print("OCR backend selected:", end=" ")
engine = PerceptionEngine(cfg)
print(engine.ocr.name)

t0 = time.perf_counter()
obs = engine.observe(region=None, with_ocr=False)
print(f"(capture ok: {obs.screen})")

# OCR the synthetic window through the same pipeline
from clippilot.perception.elements import dedupe, relabel, words_to_elements  # noqa: E402
from clippilot.utils.imaging import draw_overlay  # noqa: E402

words = engine.ocr.read(fake, origin=(0, 0))
took = time.perf_counter() - t0
els = relabel(dedupe(words_to_elements(words, min_confidence=0.0)))
print(f"\nOCR read {len(words)} word(s) -> {len(els)} label(s) in {took:.2f}s\n")
for e in els:
    print(f"  {e.id:<4} {str(e.box.center):<12} {e.text!r}")

expected = ["Media Editor", "Media Pool", "clip_01.mp4", "Export", "Captions", "Audio Mix", "PREVIEW"]
found_blob = " | ".join(e.text for e in els).lower()
missing = [w for w in expected if w.lower() not in found_blob]
print()
if missing:
    print("  MISSING:", missing)
else:
    print("  all expected labels found")

overlayed = draw_overlay(
    fake,
    [(e.box, f"{e.id} {e.text[:20]}", (76, 201, 240)) for e in els],
    origin=(0, 0),
)
print("  overlay:", save(overlayed, out_dir / "ocr_overlay.png"))
engine.close()
