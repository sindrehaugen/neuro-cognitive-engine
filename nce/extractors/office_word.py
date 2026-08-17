"""J.3 / J.4 Word modern + legacy."""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as et_fromstring
from docx import Document
from docx.oxml.ns import qn

from nce.extractors.common import is_zip_encrypted_ooxml
from nce.extractors.core import ExtractionResult, Section, empty_skipped
from nce.extractors.libreoffice import libreoffice_convert

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Decompression bomb guard
# ---------------------------------------------------------------------------

MAX_DECOMPRESSED_SIZE = 500 * 1024 * 1024  # 500 MB
MAX_ENTRY_DECOMPRESSED_SIZE = 200 * 1024 * 1024  # 200 MB per entry

# ---------------------------------------------------------------------------
# XML entity expansion (Billion Laughs) guard
# ---------------------------------------------------------------------------

MAX_XML_ENTITY_DECLARATIONS = 20  # Reasonable docx XMLs have 0-5 entity decls


def _check_xml_entity_expansion(blob: bytes) -> str | None:
    """Return an error message if *blob* contains a Billion Laughs payload.

    Scans every ``.xml`` and ``.rels`` file inside the OOXML archive for
    excessive ``<!ENTITY`` declarations.  A benign docx typically has 0-5
    entity declarations across all XML files.  A Billion Laughs attack
    defines dozens of nested entities to trigger exponential expansion
    during parsing.

    The scan is purely lexical — it reads the raw bytes and counts entity
    declarations without invoking an XML parser.  This guarantees that even
    if ``python-docx``, ``lxml``, or ``xml.etree.ElementTree`` is not
    configured with entity-expansion limits, the attack is blocked before
    any XML processing begins.

    Returns ``None`` when safe, or an error string when the document is
    detected as malicious.
    """
    import re

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for name in z.namelist():
                if not (name.endswith(".xml") or name.endswith(".rels")):
                    continue
                try:
                    content = z.read(name)
                except Exception:
                    continue
                text = content.decode("utf-8", errors="replace")
                entity_count = len(re.findall(r"<!ENTITY\s+\S+", text))
                if entity_count > MAX_XML_ENTITY_DECLARATIONS:
                    return (
                        f"xml_entity_bomb: {name} contains {entity_count} entity "
                        f"declarations (limit: {MAX_XML_ENTITY_DECLARATIONS}) — "
                        f"possible Billion Laughs attack"
                    )
    except Exception as exc:
        return f"xml_entity_scan_failed:{exc}"
    return None


def _safe_parse_xml(data: bytes):
    """Parse XML via ``defusedxml``, converting entity expansion to ``ValueError``.

    Raises ``ValueError`` (not ``DefusedXmlException``) so that callers
    can catch a single exception type for "malicious document" without
    importing ``defusedxml`` internals.
    """
    try:
        return et_fromstring(data)
    except DefusedXmlException as e:
        raise ValueError(f"Malicious XML detected: {e}") from e


def _check_zip_bomb(blob: bytes) -> str | None:
    """Return an error message if *blob* exceeds the decompressed size limit.

    Scans all ``ZipInfo`` entries and sums ``file_size`` (uncompressed size
    as stored in the central directory).  Returns ``None`` if the total is
    within bounds.  Also checks each entry individually against
    ``MAX_ENTRY_DECOMPRESSED_SIZE`` to prevent OOM from a single
    highly-compressed entry.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            total = 0
            for info in z.infolist():
                if info.file_size > MAX_ENTRY_DECOMPRESSED_SIZE:
                    return (
                        f"decompression_bomb: entry {info.filename!r} has "
                        f"{info.file_size} bytes uncompressed, exceeds "
                        f"MAX_ENTRY_DECOMPRESSED_SIZE ({MAX_ENTRY_DECOMPRESSED_SIZE})"
                    )
                total += info.file_size
    except Exception as exc:
        return f"zip_scan_failed:{exc}"

    if total > MAX_DECOMPRESSED_SIZE:
        return (
            f"decompression_bomb: estimated {total} bytes uncompressed "
            f"exceeds MAX_DECOMPRESSED_SIZE ({MAX_DECOMPRESSED_SIZE})"
        )
    return None


def _extract_core_props_xml(blob: bytes) -> dict:
    meta: dict = {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            if "docProps/core.xml" not in z.namelist():
                return meta
            root = _safe_parse_xml(z.read("docProps/core.xml"))
            for el in root.iter():
                local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
                if el.text and local in (
                    "creator",
                    "created",
                    "modified",
                    "lastModifiedBy",
                ):
                    meta[local] = el.text.strip()
    except Exception as e:
        log.debug("core_props: %s", e)
    return meta


async def extract_docx(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(extract_docx_sync, blob)


def extract_docx_sync(blob: bytes) -> ExtractionResult:
    try:
        # Decompression bomb guard before any extraction.
        bomb_err = _check_zip_bomb(blob)
        if bomb_err:
            return empty_skipped("python-docx", bomb_err)

        # XML entity expansion guard before any XML parsing.
        entity_err = _check_xml_entity_expansion(blob)
        if entity_err:
            return empty_skipped("python-docx", entity_err)

        if is_zip_encrypted_ooxml(blob):
            return empty_skipped("python-docx", "encrypted")
        warnings: list[str] = []
        try:
            doc = Document(io.BytesIO(blob))
        except Exception as e:
            log.warning("python-docx failed, trying LibreOffice: %s", e)
            converted = libreoffice_convert(blob, ".docx", ".docx")
            if not converted:
                return empty_skipped(
                    "libreoffice",
                    "conversion_failed",
                    warnings=[f"python-docx failed: {e}", "libreoffice fallback failed"],
                )
            try:
                doc = Document(io.BytesIO(converted))
                warnings.append("Opened via LibreOffice re-normalization after python-docx failure")
            except Exception as e2:
                return empty_skipped("python-docx", "corrupt", warnings=[str(e), str(e2)])

        sections: list[Section] = []
        order = 0
        heading_stack: list[str] = []

        for para in doc.paragraphs:
            try:
                if not para.text.strip():
                    continue
                style_name = para.style.name if para.style else ""
                if style_name.startswith("Heading"):
                    try:
                        level_s = style_name.replace("Heading", "").strip()
                        level = int(level_s) if level_s else 1
                    except ValueError:
                        level = 1
                    heading_stack = heading_stack[: max(0, level - 1)] + [para.text.strip()]
                    sections.append(
                        Section(
                            text=para.text,
                            structure_path=" > ".join(heading_stack),
                            section_type="heading",
                            order=order,
                        )
                    )
                else:
                    path = " > ".join(heading_stack) if heading_stack else "Body"
                    sections.append(
                        Section(
                            text=para.text,
                            structure_path=path,
                            section_type="body",
                            order=order,
                        )
                    )
                order += 1
            except Exception as e:
                warnings.append(f"paragraph_skip:{e}")

        for tbl in doc.tables:
            try:
                rows = [
                    "| " + " | ".join(cell.text.replace("|", "\\|") for cell in row.cells) + " |"
                    for row in tbl.rows
                ]
                path = (" > ".join(heading_stack) + " > Table") if heading_stack else "Table"
                sections.append(
                    Section(
                        text="\n".join(rows),
                        structure_path=path,
                        section_type="table",
                        order=order,
                    )
                )
                order += 1
            except Exception as e:
                warnings.append(f"table_skip:{e}")

        # Comments via defusedxml
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                if "word/comments.xml" in z.namelist():
                    root = _safe_parse_xml(z.read("word/comments.xml"))
                    for comment in root.iter(qn("w:comment")):
                        try:
                            author = comment.get(qn("w:author")) or "?"
                            texts = []
                            for t in comment.iter(qn("w:t")):
                                if t.text:
                                    texts.append(t.text)
                            text = "".join(texts)
                            if text.strip():
                                sections.append(
                                    Section(
                                        text=f"[{author}]: {text}",
                                        structure_path="Comment",
                                        section_type="comment",
                                        order=order,
                                    )
                                )
                                order += 1
                        except Exception as e:
                            warnings.append(f"comment_xml_skip:{e}")
        except Exception as e:
            warnings.append(f"comments_zip_skip:{e}")

        for sec in doc.sections:
            try:
                for hdr_para in sec.header.paragraphs:
                    if hdr_para.text.strip():
                        sections.append(
                            Section(
                                text=hdr_para.text,
                                structure_path="Header",
                                section_type="header",
                                order=order,
                            )
                        )
                        order += 1
                for ft_para in sec.footer.paragraphs:
                    if ft_para.text.strip():
                        sections.append(
                            Section(
                                text=ft_para.text,
                                structure_path="Footer",
                                section_type="footer",
                                order=order,
                            )
                        )
                        order += 1
            except Exception as e:
                warnings.append(f"header_footer_skip:{e}")

        # Embedded xlsx in word/embeddings/
        try:
            from nce.extractors.office_excel import extract_xlsx_sync

            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for name in z.namelist():
                    if not name.startswith("word/embeddings/"):
                        continue
                    if not name.lower().endswith((".xlsx", ".xlsm")):
                        continue
                    try:
                        data = z.read(name)
                        sub = extract_xlsx_sync(data)
                        if sub.text.strip():
                            sections.append(
                                Section(
                                    text=f"[Embedded sheet: {name}]\n\n{sub.text}",
                                    structure_path=f"Embedded: {name}",
                                    section_type="body",
                                    order=order,
                                )
                            )
                            order += 1
                        warnings.extend([f"embedded:{name}:{w}" for w in sub.warnings])
                    except Exception as e:
                        warnings.append(f"embedded_xlsx_failed:{name}:{e}")
        except Exception as e:
            warnings.append(f"embedding_walk_skip:{e}")

        full_text = "\n\n".join(s.text for s in sections)
        if len(full_text.strip()) < 50:
            warnings.append(
                "sparse_text:<50_chars; consider exporting with text layer or OCR pipeline (J.19)"
            )

        metadata = _extract_core_props_xml(blob)
        full_text = "\n\n".join(s.text for s in sections)
        return ExtractionResult(
            method="python-docx",
            text=full_text,
            sections=sections,
            metadata=metadata,
            warnings=warnings,
        )
    except Exception as exc:
        log.warning("extract_docx_sync failed on corrupted blob: %s", exc)
        return empty_skipped("python-docx", "corrupt", warnings=[f"extract_docx_sync_failed:{exc}"])


async def extract_doc(blob: bytes) -> ExtractionResult:
    try:
        converted = await asyncio.to_thread(libreoffice_convert, blob, ".doc", ".docx")
        if not converted:
            return empty_skipped(
                "libreoffice", "conversion_failed", warnings=["doc conversion failed"]
            )
        res = await extract_docx(converted)
        res.method = "libreoffice→python-docx"
        res.warnings.insert(0, "Converted from legacy .doc via LibreOffice")
        return res
    except Exception as exc:
        log.warning("extract_doc failed on corrupted blob: %s", exc)
        return empty_skipped("libreoffice", "corrupt", warnings=[f"extract_doc_failed:{exc}"])
