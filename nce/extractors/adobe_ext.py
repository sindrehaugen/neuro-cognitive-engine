"""J.15 — Adobe: .psd (psd-tools), .idml (ZIP Story XML), .indd skip; .ai → PDF pipeline."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile

from defusedxml import ElementTree as ET

from nce.extractors import pdf_ext
from nce.extractors.core import ExtractionResult, Section, empty_skipped

log = logging.getLogger(__name__)

_STORY_TEXT_RE = re.compile(r">([^<]{1,8000})<")


def _collect_psd_type_layers(psd, texts: list[str], warnings: list[str]) -> None:
    try:
        from psd_tools.api.layers import TypeLayer
    except ImportError:
        return
    try:
        desc = getattr(psd, "descendants", lambda: ())()
        for layer in desc:
            try:
                if isinstance(layer, TypeLayer):
                    t = getattr(layer, "text", None)
                    if isinstance(t, str) and t.strip():
                        texts.append(t.strip())
            except Exception as e:
                warnings.append(f"psd_typelayer:{e}")
    except Exception as e:
        warnings.append(f"psd_descendants:{e}")


def _extract_psd_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    try:
        from psd_tools import PSDImage
    except ImportError as e:
        return empty_skipped("psd", "dependency_missing", warnings=[f"psd_tools_unavailable:{e}"])

    try:
        psd = PSDImage.open(io.BytesIO(blob))
    except Exception as e:
        log.warning("psd_open_failed: %s", e)
        return empty_skipped("psd", "open_failed", warnings=[str(e)])

    texts: list[str] = []
    try:
        _collect_psd_type_layers(psd, texts, warnings)
    except Exception as e:
        warnings.append(f"psd_descendants_collect:{e}")

    seen: set[str] = set()
    body: list[str] = []
    for t in texts:
        if t not in seen:
            seen.add(t)
            body.append(t)

    full_txt = "\n".join(body).strip()
    sec = Section(
        text=full_txt or "(no TypeLayer text)",
        structure_path="PSD",
        section_type="diagram",
        order=0,
    )
    return ExtractionResult(
        method="psd",
        text=sec.text,
        sections=[sec],
        metadata={"type_layers": len(body)},
        warnings=warnings,
    )


async def extract_psd(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_extract_psd_sync, blob)


def _idml_story_texts(sync: bytes) -> list[str]:
    found: list[str] = []
    try:
        root = ET.fromstring(sync)
    except (ET.ParseError, ET.EntitiesForbidden):
        raw = sync.decode("utf-8", errors="replace")
        found.extend(m.group(1).strip() for m in _STORY_TEXT_RE.finditer(raw) if m.group(1).strip())
        return found
    for el in root.iter():
        tag = el.tag.split("}")[-1].lower() if el.tag else ""
        if tag == "content":
            if el.text and el.text.strip():
                found.append(el.text.strip())
            if el.tail and el.tail.strip():
                found.append(el.tail.strip())
        elif el.text and tag in ("pstyle", "cstyle", "path"):
            continue
        elif el.text and el.text.strip() and len(el) == 0:
            found.append(el.text.strip())
    return found


def _extract_idml_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    if len(blob) < 4 or blob[:2] != b"PK":
        return empty_skipped("idml", "not_zip", warnings=["IDML must be a ZIP archive"])
    texts: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = z.namelist()
            story_paths = [n for n in names if n.startswith("Stories/") and n.endswith(".xml")]
            for sp in story_paths:
                try:
                    raw = z.read(sp)
                except Exception as e:
                    warnings.append(f"idml_read:{sp}:{e}")
                    continue
                try:
                    for t in _idml_story_texts(raw):
                        if t:
                            texts.append(t)
                except Exception as e:
                    warnings.append(f"idml_story_parse:{sp}:{e}")
    except zipfile.BadZipFile as e:
        return empty_skipped("idml", "bad_zip", warnings=[str(e)])
    except Exception as e:
        log.warning("idml_extract_failed: %s", e, exc_info=True)
        return empty_skipped("idml", "extraction_failed", warnings=[str(e)])

    seen: set[str] = set()
    uniq: list[str] = []
    for t in texts:
        if t not in seen:
            seen.add(t)
            uniq.append(t)

    sections: list[Section] = []
    for i, block in enumerate(uniq):
        sections.append(
            Section(
                text=block,
                structure_path=f"IDML Story {i + 1}",
                section_type="body",
                order=i,
            )
        )
    full = "\n\n".join(uniq).strip()
    if not full:
        warnings.append("idml_no_story_text")
    return ExtractionResult(
        method="idml",
        text=full or "(no text)",
        sections=sections
        or [Section(text="(no text)", structure_path="IDML", section_type="body", order=0)],
        metadata={"story_blocks": len(uniq)},
        warnings=warnings,
    )


async def extract_idml(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_extract_idml_sync, blob)


async def extract_indd(blob: bytes) -> ExtractionResult:
    _ = blob
    return empty_skipped(
        "indd",
        "unsupported_binary",
        warnings=[
            "InDesign .indd is a proprietary binary package. Export to .idml for text extraction.",
        ],
    )


async def extract_ai(blob: bytes) -> ExtractionResult:
    """Illustrator files are often PDF-compatible; reuse PDF extractor (J.15)."""
    try:
        return await pdf_ext.extract_pdf(blob)
    except Exception as e:
        log.warning("ai_via_pdf_failed: %s", e, exc_info=True)
        return empty_skipped("ai", "extraction_failed", warnings=[str(e)])
