from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PIL import Image

from ..errors import BinaryKind, DependencyError, PerceptionError
from ..utils.binaries import find_binary, missing_binary_message
from ..utils.geometry import Box
from ..utils.imaging import prewarp
from ..utils.logging import get_logger

log = get_logger("perception.ocr")


@dataclass
class Word:
    text: str
    box: Box
    confidence: float
    line_id: int = -1
    block_id: int = -1


class OcrBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def read(self, image: Image.Image) -> list[Word]: ...


# ---------------------------------------------------------------- tesseract
class TesseractBackend:
    name = "tesseract"

    def __init__(self, min_confidence: float = 0.35, psm: int = 11) -> None:
        self.min_confidence = min_confidence
        self.psm = psm
        self._cmd: str | None = None
        self._api: object | None = None
        self._checked = False

    def _ensure(self) -> bool:
        if self._checked:
            return self._api is not None
        self._checked = True
        try:
            import pytesseract

            self._cmd = find_binary(BinaryKind.TESSERACT)
            pytesseract.pytesseract.tesseract_cmd = self._cmd
            ver = pytesseract.get_tesseract_version()
            log.info("tesseract backend ready (%s)", ver)
            self._api = pytesseract
            return True
        except (DependencyError, ImportError, Exception) as exc:  # noqa: BLE001
            log.info("tesseract unavailable: %s", exc)
            return False

    def available(self) -> bool:
        return self._ensure()

    def read(self, image: Image.Image) -> list[Word]:
        if not self._ensure() or self._api is None:
            raise PerceptionError("tesseract backend is not available")
        import pytesseract  # type: ignore[import-untyped]

        conf = "--oem 3 --psm %d -c preserve_interword_spaces=1" % self.psm
        data: dict = pytesseract.image_to_data(
            image, lang="eng+osd", config=conf, output_type=pytesseract.Output.DICT
        )
        words: list[Word] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                score = float(data["conf"][i])
            except (TypeError, ValueError):
                score = -1.0
            if score < self.min_confidence * 100:
                continue
            x, y = int(data["left"][i]), int(data["top"][i])
            w, h = int(data["width"][i]), int(data["height"][i])
            if w <= 1 or h <= 1:
                continue
            words.append(
                Word(
                    text=text,
                    box=Box.from_xywh(x, y, w, h),
                    confidence=score / 100.0,
                    line_id=int(data.get("line_num", [0] * n)[i] or 0),
                    block_id=int(data.get("block_num", [0] * n)[i] or 0),
                )
            )
        return words


# ----------------------------------------------------------- windows ocr
class WindowsOcrBackend:
    """Windows 10/11 built-in OCR - no binary download required."""

    name = "windows"

    def __init__(self, min_confidence: float = 0.35) -> None:
        self.min_confidence = min_confidence
        self._ready = False
        self._checked = False

    def _ensure(self) -> bool:
        if self._checked:
            return self._ready
        self._checked = True
        try:
            import winrt  # type: ignore  # noqa: F401
            from winrt.windows.globalization import Language
            from winrt.windows.graphics.imaging import BitmapDecoder
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.storage.streams import DataReader  # noqa: F401

            try:
                OcrEngine.try_create_from_language(Language("en-US"))
            except Exception:
                OcrEngine.try_create_from_user_profile_languages()
            self._ready = True
            log.info("Windows.Media.Ocr backend ready")
        except Exception as exc:
            log.info("Windows OCR unavailable (%s) - pip install winsdk", exc)
            self._ready = False
        return self._ready

    def available(self) -> bool:
        return self._ensure()

    def read(self, image: Image.Image) -> list[Word]:
        if not self._ensure():
            raise PerceptionError("Windows OCR backend is not available")
        import io

        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataReader, InMemoryRandomAccessStream

        import asyncio

        async def _run() -> list[Word]:
            engine = OcrEngine.try_create_from_language(Language("en-US")) or OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                raise PerceptionError("no OCR language pack installed (Settings > Time & language > Language > Optional features)")
            raw = io.BytesIO()
            image.convert("RGB").save(raw, format="PNG")
            stream = InMemoryRandomAccessStream()
            await stream.load_async(bytes(raw.getvalue()))
            decoder = await BitmapDecoder.create_async(stream)
            bmp = await decoder.get_software_bitmap_async()
            result = await engine.recognize_async(bmp)
            out: list[Word] = []
            for line in result.lines:
                for w in line.words:
                    r = w.bounding_rect
                    box = Box(int(r.x), int(r.y), int(r.x + r.width), int(r.y + r.height))
                    if box.width <= 1 or box.height <= 1:
                        continue
                    out.append(Word(w.text, box, 0.9))
            return out

        return asyncio.run(_run())


class NullBackend:
    name = "none"

    def available(self) -> bool:
        return True

    def read(self, image: Image.Image) -> list[Word]:
        return []


_BACKENDS: dict[str, type] = {
    "tesseract": TesseractBackend,
    "windows": WindowsOcrBackend,
    "none": NullBackend,
}

#: Preferred first. Tesseract is the accurate one; the WinRT SDK backend is a
#: zero-download fallback that only exists for Python <= 3.12.
_AUTO_ORDER = ["tesseract", "windows"]


class OcrEngine:
    """Chooses a backend, preprocesses, and returns words in *screen* coords."""

    def __init__(self, backend: str = "auto", min_confidence: float = 0.35, mode: str = "gray", upscale: float = 1.0) -> None:
        self.min_confidence = min_confidence
        self.mode = mode
        self.upscale = max(1.0, float(upscale))
        self.requested = backend
        self.backend: OcrBackend = NullBackend()
        self._resolve(backend)

    def _resolve(self, backend: str) -> None:
        order: list[str]
        if backend == "auto":
            order = list(_AUTO_ORDER)
        elif backend in _BACKENDS:
            order = [backend]
        else:
            raise PerceptionError(
                f"unknown ocr backend {backend!r} (use auto|tesseract|windows|none)"
            )
        for name in order:
            try:
                impl = _BACKENDS[name]()
            except Exception as exc:  # noqa: BLE001
                log.info("ocr backend %s failed to construct: %s", name, exc)
                continue
            if impl.available():
                self.backend = impl
                log.info("OCR backend: %s", impl.name)
                return
        if backend != "none":
            log.warning("no OCR backend available; the agent will work from the screenshot only")
        self.backend = NullBackend()

    @property
    def name(self) -> str:
        return self.backend.name

    def read(self, image: Image.Image, origin: tuple[int, int] = (0, 0)) -> list[Word]:
        if isinstance(self.backend, NullBackend):
            return []
        prepared = prewarp(image, self.mode)
        if self.upscale != 1.0:
            prepared = prepared.resize(
                (int(prepared.width * self.upscale), int(prepared.height * self.upscale)),
                Image.LANCZOS,
            )
        try:
            words = self.backend.read(prepared)
        except PerceptionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PerceptionError(f"OCR failed: {exc}") from exc
        out: list[Word] = []
        sx = 1.0 / self.upscale
        ox, oy = origin
        for w in words:
            if len(w.text.strip()) < 1:
                continue
            box = w.box.scale(sx).translate(ox, oy)
            out.append(Word(w.text.strip(), box, w.confidence, w.line_id, w.block_id))
        return out


def describe_backends() -> str:
    lines = []
    for name in ("tesseract", "windows"):
        impl = _BACKENDS[name]()
        ok = impl.available()
        lines.append(f"  {name:<10} {'ready' if ok else 'not available'}")
        if name == "tesseract" and not ok:
            last = missing_binary_message(BinaryKind.TESSERACT).splitlines()[-1].strip()
            lines.append(f"             {last}")
        if name == "windows" and not ok:
            lines.append("             needs: pip install winsdk (Python 3.12 or older)")
    lines.append("  auto order  : " + " -> ".join(_AUTO_ORDER) + " -> (screenshot only)")
    return "\n".join(lines)
