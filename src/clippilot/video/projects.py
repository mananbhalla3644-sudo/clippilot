from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from xml.dom import minidom

from ..utils.logging import get_logger
from .plan import Segment, VideoPlan

log = get_logger("video.projects")

FCP_NS = "http://www.apple.com/XML/fcpversion/1.9"


@dataclass
class RenderOutput:
    video: Path
    notes: list[str]
    edl: Path | None = None
    fcpxml: Path | None = None
    srt: Path | None = None
    command: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "video": str(self.video),
            "notes": self.notes,
            "edl": str(self.edl) if self.edl else None,
            "fcpxml": str(self.fcpxml) if self.fcpxml else None,
            "srt": str(self.srt) if self.srt else None,
            "command": self.command,
            "elapsed": round(self.elapsed, 2),
        }


# -------------------------------------------------------------------- EDL
def _edl_timecode(seconds: float, fps: float) -> str:
    fps = max(1.0, round(fps))
    total = int(round(max(0.0, seconds) * fps))
    f = total % fps
    s = (total // fps) % 60
    m = (total // fps // 60) % 60
    h = total // fps // 3600
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def write_edl(plan: VideoPlan, path: str | Path, title: str = "CLIPPILOT_EDIT") -> Path:
    """CMX3600 EDL - imports into Premiere, Resolve, Vegas, Shotcut, Media Composer."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fps = float(plan.fps)
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    rec = 0.0
    for i, seg in enumerate(plan.segments, start=1):
        src = seg.path or Path(seg.source)
        src_rate = fps
        dur = (seg.end - seg.start) / max(0.01, seg.speed)
        lines.append(
            f"{i:03d}  {src.stem.upper()[:12].ljust(12)} V     C        "
            f"{_edl_timecode(seg.start, src_rate)} {_edl_timecode(seg.end, src_rate)} "
            f"{_edl_timecode(rec, fps)} {_edl_timecode(rec + dur, fps)}"
        )
        if seg.has_audio:
            lines.append(
                f"{i:03d}  {src.stem.upper()[:12].ljust(12)} AA    C        "
                f"{_edl_timecode(seg.start, src_rate)} {_edl_timecode(seg.end, src_rate)} "
                f"{_edl_timecode(rec, fps)} {_edl_timecode(rec + dur, fps)}"
            )
        if i < len(plan.segments):
            tr = plan.transitions[i - 1] if i - 1 < len(plan.transitions) else None
            if tr and tr.type != "cut" and tr.duration > 0:
                lines.append(f"* FROM CLIP NAME: {src.name}")
                lines.append(f"* TO CLIP NAME: {tr.type.upper()} {tr.duration:.2f}s")
        rec += dur
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("wrote EDL %s", out.name)
    return out


# ------------------------------------------------------------------ FCPXML
def write_fcpxml(plan: VideoPlan, path: str | Path, name: str = "ClipPilot Project") -> Path:
    """
    FCPXML 1.9 - drops straight into Final Cut Pro, and CapCut/Premiere/Resolve
    via their File > Import path.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ET.register_namespace("fcpxml", FCP_NS)
    fcpxml = ET.Element(f"{{{FCP_NS}}}fcpxml", {"version": "1.9"})
    resources = ET.SubElement(fcpxml, f"{{{FCP_NS}}}resources")
    ET.SubElement(
        fcpxml,
        f"{{{FCP_NS}}}formatIdentifiers",
        {"Identifiers": "r0"},
    )
    for i, seg in enumerate(plan.segments):
        asset = ET.SubElement(
            resources,
            f"{{{FCP_NS}}}asset",
            {
                "name": (seg.path or Path(seg.source)).name,
                "ref": f"r{i}",
                "start": "0s",
                "duration": f"{(seg.end - seg.start):.4f}s",
                "hasVideo": "1",
                "format": "r1",
                "audioSources": "1",
                "audioChannels": "2",
                "audioRate": "48k",
            },
        )
        ET.SubElement(
            asset,
            f"{{{FCP_NS}}}asset-clip",
            {
                "ref": f"r{i}",
                "offset": f"{seg.start:.4f}s",
                "name": (seg.path or Path(seg.source)).name,
                "duration": f"{(seg.end - seg.start):.4f}s",
                "tcFormat": "NDF",
                "tcStart": "0s",
                "tcDuration": f"{(seg.end - seg.start):.4f}s",
            },
        )

    library = ET.SubElement(fcpxml, f"{{{FCP_NS}}}library")
    ET.SubElement(library, f"{{{FCP_NS}}}asset", {"name": "event-01"})
    event = ET.SubElement(library, f"{{{FCP_NS}}}asset", {"name": "event-01", "start": "0s"})
    project = ET.SubElement(
        event,
        f"{{{FCP_NS}}}project",
        {"name": name},
    )
    sequence = ET.SubElement(
        project,
        f"{{{FCP_NS}}}sequence",
        {
            "format": "r1",
            "duration": f"{plan.total_duration:.4f}s",
            "tcStart": "0s",
            "tcFormat": "NDF",
            "audioLayout": "stereo",
            "audioRate": "48k",
        },
    )
    ET.SubElement(sequence, f"{{{FCP_NS}}}spine")
    ET.SubElement(
        sequence,
        f"{{{FCP_NS}}}metadata",
    )
    for attr, val in (
        (f"{{{FCP_NS}}}name", name),
        (f"{{{FCP_NS}}}note", "generated by ClipPilot"),
    ):
        meta = ET.SubElement(sequence, f"{{{FCP_NS}}}metadata")
        ET.SubElement(meta, attr, {f"{{{FCP_NS}}}value": val})

    # Sequence needs a format definition to be importable.
    formats = fcpxml.find(f"{{{FCP_NS}}}formatIdentifiers")
    if formats is not None:
        fmt = ET.SubElement(
            formats,
            f"{{{FCP_NS}}}format",
            {
                "id": "r1",
                "name": f"FFVideoFormat{plan.height}p{plan.fps}",
                "frameDuration": f"{1 / max(1, plan.fps):.6f}s",
                "width": str(plan.width),
                "height": str(plan.height),
                "colorSpace": "1-1-1 (Rec. 709)",
            },
        )
        if fmt is not None:
            pass

    xml_str = minidom.parseString(ET.tostring(fcpxml, encoding="utf-8")).toprettyxml(indent="  ")
    out.write_text(xml_str, encoding="utf-8")
    log.info("wrote FCPXML %s", out.name)
    return out


def write_markers_csv(plan: VideoPlan, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = ["index,source,start,end,duration,speed,output_start,output_end"]
    cursor = 0.0
    for i, seg in enumerate(plan.segments, start=1):
        dur = (seg.end - seg.start) / max(0.01, seg.speed)
        rows.append(
            f"{i},{seg.path or seg.source},{seg.start:.3f},{seg.end:.3f},{dur:.3f},{seg.speed},"
            f"{cursor:.3f},{cursor + dur:.3f}"
        )
        cursor += dur
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return out


def write_otio_json(plan: VideoPlan, path: str | Path) -> Path | None:
    """OpenTimelineIO JSON when the module is installed (Premiere/Resolve/AE)."""
    try:
        import opentimelineio as otio  # type: ignore
    except ImportError:
        return None
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    timeline = otio.schema.Timeline(name="ClipPilot")
    track = otio.schema.Track(name="V1")
    for seg in plan.segments:
        clip = otio.schema.Clip(
            name=(seg.path or Path(seg.source)).name,
            media_reference=otio.schema.ExternalReference(
                target_url=f"file:///{str(seg.path or seg.source).replace(os.sep, '/')}".replace("///", "//"),
                available_range=otio.opentime.TimeRange(
                    otio.opentime.RationalTime(0, 1),
                    otio.opentime.RationalTime(int((seg.end - seg.start) * 48000), 48000),
                ),
            ),
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(int(seg.start * 48000), 48000),
                otio.opentime.RationalTime(int((seg.end - seg.start) * 48000), 48000),
            ),
        )
        track.append(clip)
    timeline.tracks.append(track)
    otio.adapters.write_to_file(timeline, str(out))
    return out


def export_all(plan: VideoPlan, work_dir: str | Path) -> dict[str, Path | None]:
    """Write every interchange format we can produce next to the output."""
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    stem = (plan.output_path or Path(plan.output)).stem
    result: dict[str, Path | None] = {
        "edl": write_edl(plan, work / f"{stem}.edl"),
        "fcpxml": write_fcpxml(plan, work / f"{stem}.fcpxml"),
        "csv": write_markers_csv(plan, work / f"{stem}.csv"),
    }
    try:
        result["otio"] = write_otio_json(plan, work / f"{stem}.otio")
    except Exception as exc:  # noqa: BLE001
        log.debug("otio export skipped: %s", exc)
        result["otio"] = None
    return result


def make_timeline(segments: Sequence[dict[str, Any]], output: str | Path, **overrides: Any) -> VideoPlan:
    from .plan import VideoPlan

    data: dict[str, Any] = {"segments": list(segments), "output": str(output)}
    data.update(overrides)
    return VideoPlan.from_dict(data)
