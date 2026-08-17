"""J.14 Diagrams: .vsdx, .drawio, .mermaid — text + relationships; never crash worker."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
from pathlib import Path

from defusedxml import ElementTree as ET

from nce.extractors.core import ExtractionResult, Section, empty_skipped

log = logging.getLogger(__name__)

_MXCELL_VALUE_RE = re.compile(rb'<mxCell[^>]*\bvalue="([^"]*)"', re.I)
_DRAWIO_DECODE = re.compile(
    r"<diagram[^>]*>([A-Za-z0-9+/=\s]+)</diagram>",
    re.I | re.DOTALL,
)


def _safe_et_parse(xml_bytes: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(xml_bytes)
    except (ET.ParseError, ET.EntitiesForbidden):
        return None


def _decode_drawio_diagram_blob(inner: str) -> str:
    inner = inner.strip()
    try:
        raw = base64.b64decode(inner, validate=False)
        import zlib

        return zlib.decompress(raw, -zlib.MAX_WBITS).decode("utf-8", errors="replace")
    except Exception:
        return inner


async def extract_vsdx(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []

    def _run() -> ExtractionResult:
        try:
            from vsdx import VisioFile
        except ImportError as e:
            return empty_skipped("vsdx", "dependency_missing", warnings=[f"vsdx_unavailable:{e}"])

        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".vsdx", delete=False) as tf:
                tf.write(blob)
                path = Path(tf.name)
            with VisioFile(str(path)) as vis:
                sections: list[Section] = []
                order = 0
                try:
                    pages_iter = list(getattr(vis, "pages", None) or [])
                except Exception as e:
                    warnings.append(f"vsdx_pages_list:{e}")
                    pages_iter = []
                for pi, page in enumerate(pages_iter):
                    if page is None:
                        continue
                    label = getattr(page, "name", None) or f"Page {pi + 1}"
                    text_parts: list[str] = []
                    connects = []
                    try:
                        connects = list(getattr(page, "connects", []) or [])
                    except Exception as e:
                        warnings.append(f"vsdx_connects:{e}")
                    try:
                        for shape in page.child_shapes:

                            def _walk(s, depth: int = 0):
                                if depth > 50:
                                    return
                                try:
                                    t = (getattr(s, "text", None) or "").strip()
                                    if t:
                                        text_parts.append(t)
                                except Exception as e:
                                    warnings.append(f"vsdx_shape_text:{e}")
                                try:
                                    for ch in getattr(s, "child_shapes", []) or []:
                                        _walk(ch, depth + 1)
                                except Exception as e:
                                    warnings.append(f"vsdx_child_shapes:{e}")

                            _walk(shape)
                    except Exception as e:
                        warnings.append(f"vsdx_walk_shapes:{e}")
                    rel_lines: list[str] = []
                    try:
                        for c in connects:
                            try:
                                frm = getattr(c, "from_id", None) or getattr(c, "from_shape", None)
                                to = getattr(c, "to_id", None) or getattr(c, "to_shape", None)
                                if frm is not None or to is not None:
                                    rel_lines.append(f"connect: {frm} -> {to}")
                            except Exception as e:
                                warnings.append(f"vsdx_connect_repr:{e}")
                    except Exception as e:
                        warnings.append(f"vsdx_connects_iterate:{e}")
                    body = "\n".join(text_parts).strip()
                    if rel_lines:
                        body = (body + "\n\n### Connections\n" + "\n".join(rel_lines)).strip()
                    sections.append(
                        Section(
                            text=body or "(no text shapes)",
                            structure_path=f"Visio: {label}",
                            section_type="diagram",
                            order=order,
                        )
                    )
                    order += 1
                full = "\n\n".join(s.text for s in sections)
                return ExtractionResult(
                    method="vsdx",
                    text=full,
                    sections=sections,
                    metadata={"page_count": len(sections)},
                    warnings=warnings,
                )
        except Exception as e:
            log.warning("vsdx_extract_failed: %s", e, exc_info=True)
            return empty_skipped("vsdx", "extraction_failed", warnings=[str(e)])
        finally:
            if path and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass

    return await asyncio.to_thread(_run)


def _drawio_text_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception as e:
        return empty_skipped("drawio", "decode_failed", warnings=[str(e)])

    values: list[str] = []
    for m in _MXCELL_VALUE_RE.finditer(blob):
        try:
            v = m.group(1).decode("utf-8", errors="replace").strip()
        except Exception:
            continue
        if v:
            values.append(v)

    for dm in _DRAWIO_DECODE.finditer(text):
        inner_xml = _decode_drawio_diagram_blob(dm.group(1))
        for m in _MXCELL_VALUE_RE.finditer(inner_xml.encode("utf-8", errors="replace")):
            try:
                v = m.group(1).decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if v:
                values.append(v)

    root = _safe_et_parse(blob)
    if root is not None:
        try:
            for el in root.iter():
                v = el.attrib.get("value")
                if v and v.strip():
                    values.append(v.strip())
        except Exception as e:
            warnings.append(f"drawio_xml_walk:{e}")

    seen: set[str] = set()
    uniq = []
    for v in values:
        if v not in seen:
            seen.add(v)
            uniq.append(v)

    body = "\n".join(uniq).strip()
    if not body:
        warnings.append("drawio_no_mxcell_values")
    sec = Section(
        text=body or "(no text extracted)",
        structure_path="draw.io",
        section_type="diagram",
        order=0,
    )
    return ExtractionResult(
        method="drawio",
        text=sec.text,
        sections=[sec],
        metadata={"value_nodes": len(uniq)},
        warnings=warnings,
    )


async def extract_drawio(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_drawio_text_sync, blob)


def _mermaid_sections_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception as e:
        return empty_skipped("mermaid", "decode_failed", warnings=[str(e)])

    parts = re.split(r"(?m)^---\s*$", text)
    sections: list[Section] = []
    order = 0
    if len(parts) <= 1:
        sections.append(
            Section(
                text=text.strip(),
                structure_path="Mermaid",
                section_type="diagram",
                order=order,
            )
        )
    else:
        for i, chunk in enumerate(parts):
            c = chunk.strip()
            if not c:
                continue
            sections.append(
                Section(
                    text=c,
                    structure_path=f"Mermaid block {i + 1}",
                    section_type="diagram",
                    order=order,
                )
            )
            order += 1
    full = "\n\n".join(s.text for s in sections)
    return ExtractionResult(
        method="mermaid",
        text=full,
        sections=sections,
        metadata={"blocks": len(sections)},
        warnings=warnings,
    )


async def extract_mermaid(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_mermaid_sections_sync, blob)
