from __future__ import annotations

from enum import Enum


class ClipPilotError(Exception):
    """Base error."""


class ConfigError(ClipPilotError):
    pass


class DependencyError(ClipPilotError):
    """A required external dependency is missing (ffmpeg, tesseract, ...)."""


class PerceptionError(ClipPilotError):
    pass


class ActionError(ClipPilotError):
    """An action could not be executed."""


class SafetyViolation(ClipPilotError):
    """Action refused by the safety policy."""


class FailsafeTriggered(ClipPilotError):
    """User slammed the pointer into the abort corner."""


class ConfirmationRequired(ClipPilotError):
    """Action needs human approval before it runs."""

    def __init__(self, message: str, action: dict | None = None) -> None:
        super().__init__(message)
        self.action = action or {}


class LLMError(ClipPilotError):
    pass


class PlanError(ClipPilotError):
    """Video plan is invalid."""


class RenderError(ClipPilotError):
    pass


class BinaryKind(str, Enum):
    FFMPEG = "ffmpeg"
    FFPROBE = "ffprobe"
    TESSERACT = "tesseract"
