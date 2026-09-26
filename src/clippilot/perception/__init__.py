from __future__ import annotations

from . import elements as _el
from .elements import dedupe, relabel, template_elements, words_to_elements
from .ocr import OcrEngine, describe_backends
from .screen import Capture, ScreenCapture, enable_dpi_awareness
from .templates import TemplateMatcher, hamming, perceptual_hash
from .vlm import VisionModel, extract_json

__all__ = [
    "OcrEngine",
    "ScreenCapture",
    "Capture",
    "TemplateMatcher",
    "VisionModel",
    "extract_json",
    "words_to_elements",
    "template_elements",
    "dedupe",
    "relabel",
    "describe_backends",
    "enable_dpi_awareness",
    "hamming",
    "perceptual_hash",
]
