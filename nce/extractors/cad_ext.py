"""J.16 — CAD: .dxf/.dwg via ezdxf (+ odafc for DWG); .rvt / .skp metadata-only."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import tempfile
from pathlib import Path

from nce.extractors.core import ExtractionResult, Section, empty_skipped

log = logging.getLogger(__name__)

_MAX_CAD_TEXT_ENTITIES = int(os.environ.get("NCE_CAD_MAX_ENTITIES", "10000"))

_SKP_HEADER_RE = re.compile(rb"SketchUp Model", re.I)
_RVT_MAGIC = b"{ rvtml"  # sometimes present in RVT family


def _collect_entity_texts(msp, warnings: list[str]) -> list[str]:
    """Extract TEXT/MTEXT strings from modelspace with a hard entity cap."""
    lines: list[str] = []
    for i, entity in enumerate(msp):
        if i >= _MAX_CAD_TEXT_ENTITIES:
            warnings.append(f"cad_entity_limit: capped at {_MAX_CAD_TEXT_ENTITIES}")
            break
        t = _entity_text(entity)
        if t:
            lines.append(t)
    return lines


def _entity_text(entity) -> str | None:
    try:
        dxftype = entity.dxftype()
        if dxftype == "TEXT":
            return str(entity.dxf.text).strip() or None
        if dxftype == "MTEXT":
            return str(entity.text).strip() or None
        if dxftype == "ATTRIB":
            return str(entity.dxf.text).strip() or None
    except Exception as e:
        log.debug("cad_entity_text: %s", e)
    return None


def _extract_dxf_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    try:
        import ezdxf
    except ImportError as e:
        return empty_skipped("dxf", "dependency_missing", warnings=[f"ezdxf_unavailable:{e}"])

    try:
        doc = ezdxf.read(io.BytesIO(blob))  # type: ignore[arg-type]
    except Exception as e:
        log.warning("dxf_read_failed: %s", e)
        name = type(e).__name__
        reason = "malformed_dxf" if "DXF" in name or "Structure" in name else "read_failed"
        return empty_skipped("dxf", reason, warnings=[str(e)])

    lines: list[str] = []
    try:
        msp = doc.modelspace()
        lines = _collect_entity_texts(msp, warnings)
    except Exception as e:
        warnings.append(f"dxf_modelspace:{e}")

    seen: set[str] = set()
    uniq: list[str] = []
    for t in lines:
        if t not in seen:
            seen.add(t)
            uniq.append(t)

    body = "\n".join(uniq).strip()
    sec = Section(
        text=body or "(no TEXT/MTEXT)",
        structure_path="DXF modelspace",
        section_type="engineering",
        order=0,
    )
    return ExtractionResult(
        method="dxf",
        text=sec.text,
        sections=[sec],
        metadata={"entity_strings": len(uniq)},
        warnings=warnings,
    )


async def extract_dxf(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_extract_dxf_sync, blob)


def _extract_dwg_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    try:
        import ezdxf
    except ImportError as e:
        return empty_skipped("dwg", "dependency_missing", warnings=[f"ezdxf_unavailable:{e}"])

    path: Path | None = None
    try:
        try:
            doc = ezdxf.read(io.BytesIO(blob))  # type: ignore[arg-type]
        except Exception:
            doc = None
        if doc is None:
            try:
                from ezdxf.addons import odafc  # type: ignore
            except ImportError as e:
                return empty_skipped(
                    "dwg",
                    "odafc_unavailable",
                    warnings=[f"DWG requires ezdxf odafc + ODA File Converter: {e}"],
                )
            with tempfile.NamedTemporaryFile(suffix=".dwg", delete=False) as tf:
                tf.write(blob)
                path = Path(tf.name)
            try:
                doc = odafc.readfile(str(path))  # type: ignore[attr-defined]
            except Exception as e:
                warnings.append(f"dwg_odafc:{e}")
                return empty_skipped(
                    "dwg",
                    "odafc_failed",
                    warnings=warnings
                    + [
                        "Install ODA File Converter and ensure odafc can invoke it; "
                        "see https://ezdxf.readthedocs.io/en/stable/addons/odafc.html",
                    ],
                )

        msp = doc.modelspace()
        lines = _collect_entity_texts(msp, warnings)
        seen: set[str] = set()
        uniq = []
        for t in lines:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        body = "\n".join(uniq).strip()
        sec = Section(
            text=body or "(no TEXT/MTEXT)",
            structure_path="DWG modelspace",
            section_type="engineering",
            order=0,
        )
        return ExtractionResult(
            method="dwg",
            text=sec.text,
            sections=[sec],
            metadata={"entity_strings": len(uniq)},
            warnings=warnings,
        )
    finally:
        if path and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


async def extract_dwg(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_extract_dwg_sync, blob)


def _rvt_metadata_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = ["rvt_metadata_only_full_model_skipped"]
    size = len(blob)
    head = blob[: min(4096, size)]
    hints: list[str] = []
    if _RVT_MAGIC in head:
        hints.append("revit_family_magic_detected")
    # printable ASCII snippets (file paths / titles sometimes leak)
    try:
        ascii_run = re.findall(rb"[\x20-\x7e]{12,120}", head[:2048])
        for chunk in ascii_run[:8]:
            try:
                s = chunk.decode("ascii", errors="ignore").strip()
                if "autodesk" in s.lower() or "revit" in s.lower():
                    hints.append(s[:200])
            except Exception:
                pass
    except Exception as e:
        warnings.append(f"rvt_scan:{e}")

    meta_lines = [f"bytes: {size}", *hints]
    body = "Revit project (.rvt) — metadata only.\n" + "\n".join(meta_lines)
    sec = Section(text=body, structure_path="RVT metadata", section_type="metadata", order=0)
    return ExtractionResult(
        method="rvt",
        text=body,
        sections=[sec],
        metadata={"bytes": size, "hints": hints},
        warnings=warnings,
    )


async def extract_rvt(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_rvt_metadata_sync, blob)


def _skp_metadata_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = ["skp_metadata_only_geometry_skipped"]
    size = len(blob)
    is_skp = bool(_SKP_HEADER_RE.search(blob[: min(len(blob), 8192)]))
    ver = None
    try:
        m = re.search(rb"(\d{4}-\d{2})", blob[:4096])
        if m:
            ver = m.group(1).decode("ascii", errors="ignore")
    except Exception:
        pass
    lines = [
        f"SketchUp document (.skp) — metadata only (bytes={size}).",
        f"sketchup_header_hint: {is_skp}",
    ]
    if ver:
        lines.append(f"possible_version_marker: {ver}")
    body = "\n".join(lines)
    sec = Section(text=body, structure_path="SKP metadata", section_type="metadata", order=0)
    return ExtractionResult(
        method="skp",
        text=body,
        sections=[sec],
        metadata={"bytes": size, "header_like_skp": is_skp},
        warnings=warnings,
    )


async def extract_skp(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_skp_metadata_sync, blob)
