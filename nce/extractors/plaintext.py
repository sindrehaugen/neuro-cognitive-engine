"""J.12 Plain-text family extractors."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import random
import re
from collections import Counter
from collections.abc import Iterator
from numbers import Real
from typing import Any

import chardet

from nce.extractors.common import (
    cell_to_str,
    html_to_text,
    rows_to_markdown,
    trim_trailing_empty,
)
from nce.extractors.core import ExtractionResult, Section
from nce.extractors.office_excel import (
    SHEET_ROW_SAMPLE,
    SHEET_ROW_SMALL,
    _emit_sheet_sections,
)

log = logging.getLogger(__name__)


def _decode_blob(blob: bytes) -> tuple[str, list[str]]:
    warnings: list[str] = []
    det = chardet.detect(blob[: min(len(blob), 500_000)]) or {}
    enc = det.get("encoding") or "utf-8"
    conf = float(det.get("confidence") or 0.0)
    if conf < 0.55:
        warnings.append(f"chardet_low_confidence:{conf:.2f} encoding={enc}")
    try:
        return blob.decode(enc, errors="strict"), warnings
    except Exception:
        try:
            return blob.decode("utf-8", errors="replace"), warnings + ["decode_utf8_replace"]
        except Exception as e:
            return blob.decode("latin-1", errors="replace"), warnings + [
                f"decode_latin1_replace:{e}"
            ]


def _numeric_type(v: object) -> bool:
    return isinstance(v, Real) and not isinstance(v, bool)


def _summarize_delimited_stream(
    header: tuple[Any, ...],
    body_rows: Iterator[tuple[Any, ...]],
    *,
    label: str,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    ncol = len(header)
    row_count = 0
    types_n: list[Counter[str]] = [Counter() for _ in range(ncol)]
    numeric_min: list[float | None] = [None] * ncol
    numeric_max: list[float | None] = [None] * ncol
    str_top: list[Counter[str]] = [Counter() for _ in range(ncol)]

    def _norm_cell(raw: object) -> object:
        if raw is None:
            return None
        if isinstance(raw, str) and raw.strip():
            try:
                if "." in raw or "e" in raw.lower():
                    return float(raw)
                return int(raw)
            except Exception:
                return raw
        return raw

    for row in body_rows:
        row_count += 1
        for j in range(ncol):
            raw = row[j] if j < len(row) else None
            v = _norm_cell(raw)
            if v is None or (isinstance(v, str) and not str(v).strip()):
                types_n[j]["empty"] += 1
                continue
            if _numeric_type(v):
                types_n[j]["numeric"] += 1
                fv = float(v)  # type: ignore[arg-type]
                numeric_min[j] = (
                    fv if numeric_min[j] is None else min(numeric_min[j], fv)  # type: ignore[type-var]
                )
                numeric_max[j] = (
                    fv if numeric_max[j] is None else max(numeric_max[j], fv)  # type: ignore[type-var]
                )
            else:
                types_n[j]["text"] += 1
                s = cell_to_str(v)[:200]
                str_top[j][s] += 1
    lines = [
        f"# {label} (summary — {row_count + 1} rows including header)",
        f"Columns ({ncol}): " + " | ".join(cell_to_str(h) for h in header),
    ]
    for j in range(ncol):
        cn = header[j] if j < len(header) else f"col{j}"
        parts = [f"**{cell_to_str(cn)}**"]
        parts.append(f"types: {dict(types_n[j])}")
        if numeric_min[j] is not None:
            parts.append(f"min={numeric_min[j]}, max={numeric_max[j]}")
        top = str_top[j].most_common(5)
        if top:
            parts.append("top_values: " + "; ".join(f"{k!r} ({c})" for k, c in top))
        lines.append("- " + "; ".join(parts))
    warnings.append(f"{label}_summary_only: full content not indexed")
    return "\n".join(lines), warnings


def _extract_delimited_sync(blob: bytes, delimiter: str, label: str) -> ExtractionResult:
    text, enc_warnings = _decode_blob(blob)
    warnings = list(enc_warnings)
    sio = io.StringIO(text)
    reader = csv.reader(sio, delimiter=delimiter)
    try:
        rows = [tuple(row) for row in reader]
    except Exception as e:
        log.warning("%s parse failed: %s", label, e)
        return ExtractionResult(
            method=label,
            text="",
            sections=[],
            metadata={},
            warnings=warnings + [str(e)],
        )
    if not rows:
        return ExtractionResult(method=label, text="", sections=[], metadata={}, warnings=warnings)

    n_data = len(rows) - 1
    header = rows[0]

    if n_data <= SHEET_ROW_SMALL:
        body = trim_trailing_empty(rows)
        secs, _ = _emit_sheet_sections(label.upper(), body, 0, warnings)
        full = "\n\n".join(s.text for s in secs)
        return ExtractionResult(
            method=label,
            text=full,
            sections=secs,
            metadata={"rows": len(body)},
            warnings=warnings,
        )

    if n_data <= SHEET_ROW_SAMPLE:
        rnd = random.Random(42)
        data_rows = list(rows[1:])
        data_rows = trim_trailing_empty(data_rows)
        if len(data_rows) <= 200:
            sample = [header] + data_rows
        else:
            first = data_rows[:100]
            last = data_rows[-100:]
            mid_range = list(range(100, len(data_rows) - 100))
            pick = rnd.sample(mid_range, min(100, len(mid_range)))
            pick.sort()
            middle = [data_rows[i] for i in pick]
            sample = [header] + first + middle + last
        md = rows_to_markdown(trim_trailing_empty(sample))
        warnings.append(
            f"Large {label} sampled: {n_data + 1} rows; header + first 100 + 100 random middle + last 100"
        )
        sec = Section(text=md, structure_path=label.upper(), section_type="table", order=0)
        return ExtractionResult(
            method=label,
            text=md,
            sections=[sec],
            metadata={"rows": n_data + 1},
            warnings=warnings,
        )

    body_iter = (tuple(row) for row in rows[1:])
    summary_txt, sw = _summarize_delimited_stream(header, body_iter, label=label.upper())
    warnings.extend(sw)
    sec = Section(text=summary_txt, structure_path=label.upper(), section_type="table", order=0)
    return ExtractionResult(
        method=label,
        text=summary_txt,
        sections=[sec],
        metadata={"rows": n_data + 1},
        warnings=warnings,
    )


def _markdown_to_sections(src: str) -> list[Section]:
    """Heading-aware sections via markdown-it tokens (J.12)."""
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return [
            Section(
                text=src.strip(),
                structure_path="Document",
                section_type="body",
                order=0,
            ),
        ]

    md = MarkdownIt()
    tokens = md.parse(src)
    sections: list[Section] = []
    order = 0
    heading_stack: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.type == "heading_open":
            level = int(t.tag[1])
            i += 1
            title = ""
            if i < len(tokens) and tokens[i].type == "inline":
                title = tokens[i].content
                i += 1
            heading_stack = heading_stack[: level - 1] + [title]
            if i < len(tokens) and tokens[i].type == "heading_close":
                i += 1
            continue
        if t.type in ("paragraph_open", "blockquote_open"):
            close = "paragraph_close" if t.type == "paragraph_open" else "blockquote_close"
            i += 1
            chunk: list[str] = []
            while i < len(tokens) and tokens[i].type != close:
                ct = tokens[i]
                if ct.type == "inline":
                    chunk.append(ct.content)
                elif ct.type in ("fence", "code_block"):
                    info = getattr(ct, "info", "") or ""
                    chunk.append(f"\n```{info}\n{ct.content}\n```\n")
                i += 1
            body = "".join(chunk).strip()
            if body:
                path = " / ".join(heading_stack) if heading_stack else "Document"
                sections.append(
                    Section(text=body, structure_path=path, section_type="body", order=order)
                )
                order += 1
            if i < len(tokens) and tokens[i].type == close:
                i += 1
            continue
        if t.type in ("fence", "code_block"):
            info = getattr(t, "info", "") or ""
            body = f"```{info}\n{t.content}\n```"
            path = " / ".join(heading_stack) if heading_stack else "Document"
            sections.append(
                Section(
                    text=body,
                    structure_path=f"{path} > code",
                    section_type="code",
                    order=order,
                )
            )
            order += 1
            i += 1
            continue
        i += 1

    if not sections:
        sections.append(
            Section(
                text=src.strip(),
                structure_path="Document",
                section_type="body",
                order=0,
            )
        )
    return sections


def _ipynb_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    try:
        import nbformat
    except ImportError as e:
        return ExtractionResult(
            method="nbformat",
            text="",
            sections=[],
            metadata={},
            warnings=[f"nbformat_unavailable:{e}"],
        )
    try:
        nb = nbformat.read(io.BytesIO(blob), as_version=4)
    except Exception as e:
        log.warning("ipynb read failed: %s", e)
        return ExtractionResult(
            method="nbformat",
            text="",
            sections=[],
            metadata={},
            warnings=[str(e)],
        )
    sections: list[Section] = []
    order = 0
    for i, cell in enumerate(nb.cells):
        try:
            src = getattr(cell, "source", "") or ""
        except Exception as e:
            warnings.append(f"ipynb_cell_{i}:{e}")
            continue
        if cell.cell_type == "markdown":
            sections.append(
                Section(
                    text=src.strip(),
                    structure_path=f"Cell {i} (markdown)",
                    section_type="body",
                    order=order,
                )
            )
            order += 1
        elif cell.cell_type == "code":
            warnings.append(
                f"ipynb code cell {i}: consider index_code_file for structured code indexing"
            )
            sections.append(
                Section(
                    text=src.strip(),
                    structure_path=f"Cell {i} (code)",
                    section_type="code",
                    order=order,
                )
            )
            order += 1
        else:
            warnings.append(f"ipynb_skip_cell_type:{i}:{cell.cell_type}")

    full = "\n\n".join(s.text for s in sections)
    return ExtractionResult(
        method="nbformat",
        text=full,
        sections=sections,
        metadata={"cells": len(nb.cells)},
        warnings=warnings,
    )


async def extract_txt(blob: bytes) -> ExtractionResult:
    def _run() -> ExtractionResult:
        text, warnings = _decode_blob(blob)
        sec = Section(text=text.strip(), structure_path="Document", section_type="body", order=0)
        return ExtractionResult(
            method="chardet+txt",
            text=text,
            sections=[sec],
            metadata={},
            warnings=warnings,
        )

    return await asyncio.to_thread(_run)


async def extract_markdown(blob: bytes) -> ExtractionResult:
    def _run() -> ExtractionResult:
        text, warnings = _decode_blob(blob)
        sections = _markdown_to_sections(text)
        full = "\n\n".join(s.text for s in sections)
        return ExtractionResult(
            method="markdown-it",
            text=full,
            sections=sections,
            metadata={},
            warnings=warnings,
        )

    return await asyncio.to_thread(_run)


async def extract_csv(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_extract_delimited_sync, blob, ",", "csv")


async def extract_tsv(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_extract_delimited_sync, blob, "\t", "tsv")


def _html_sections_from_headings(html: str) -> list[Section]:
    """Split HTML into Sections by h1–h3 heading context using selectolax.

    Each section covers the content that follows its nearest heading ancestor.
    The structure_path encodes the heading hierarchy so downstream chunking
    can prepend context (e.g. "Introduction > Security > Token flow").
    Falls back to a single plain-text section if selectolax is unavailable.
    """
    try:
        from selectolax.parser import HTMLParser
    except ImportError:
        plain = html_to_text(html)
        return [Section(text=plain.strip(), structure_path="HTML", section_type="body", order=0)]

    tree = HTMLParser(html)
    for sel in ("script", "style", "nav", "footer"):
        for n in tree.css(sel):
            try:
                n.decompose()
            except AttributeError:
                n.remove()

    # heading stack: list of (tag_level 1-3, text)
    h_stack: list[tuple[int, str]] = []
    sections: list[Section] = []
    pending_lines: list[str] = []
    order = 0
    HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3}
    BLOCK_TAGS = {
        "p",
        "div",
        "li",
        "td",
        "th",
        "blockquote",
        "pre",
        "article",
        "section",
        "aside",
        "main",
        "dd",
        "dt",
    }

    def _flush(path: str) -> None:
        nonlocal order
        combined = "\n".join(line for line in pending_lines if line.strip())
        if combined.strip():
            sections.append(
                Section(
                    text=combined.strip(),
                    structure_path=path or "HTML",
                    section_type="body",
                    order=order,
                )
            )
            order += 1
        pending_lines.clear()

    def _path() -> str:
        return " / ".join(t for _, t in h_stack) if h_stack else "HTML"

    for node in tree.root.traverse(include_text=False):  # type: ignore[union-attr]
        tag = node.tag if node.tag else ""
        level = HEADING_TAGS.get(tag)
        if level is not None:
            _flush(_path())
            # Pop headings of same or deeper level
            while h_stack and h_stack[-1][0] >= level:
                h_stack.pop()
            h_stack.append((level, (node.text(strip=True) or "").strip()))
        elif tag in BLOCK_TAGS:
            t = node.text(deep=True, strip=True)
            if t:
                pending_lines.append(t)

    _flush(_path())

    if not sections:
        plain = tree.root.text(separator="\n", strip=True)  # type: ignore[union-attr]
        sections = [
            Section(text=plain.strip(), structure_path="HTML", section_type="body", order=0)
        ]

    return sections


async def extract_html(blob: bytes) -> ExtractionResult:
    def _run() -> ExtractionResult:
        text, warnings = _decode_blob(blob)
        sections = _html_sections_from_headings(text)
        full_text = "\n\n".join(s.text for s in sections)
        return ExtractionResult(
            method="selectolax",
            text=full_text,
            sections=sections,
            metadata={},
            warnings=warnings,
        )

    return await asyncio.to_thread(_run)


async def extract_rtf(blob: bytes) -> ExtractionResult:
    def _run() -> ExtractionResult:
        try:
            from striprtf.striprtf import rtf_to_text
        except ImportError as e:
            return ExtractionResult(
                method="striprtf",
                text="",
                sections=[],
                metadata={},
                warnings=[f"striprtf_unavailable:{e}"],
            )
        warnings: list[str] = []
        try:
            text = blob.decode("utf-8", errors="replace")
        except Exception:
            text = blob.decode("latin-1", errors="replace")
            warnings.append("rtf_decode_latin1")
        try:
            plain = rtf_to_text(text)
        except Exception as e:
            log.warning("striprtf failed: %s", e)
            return ExtractionResult(
                method="striprtf",
                text="",
                sections=[],
                metadata={},
                warnings=warnings + [str(e)],
            )
        sec = Section(text=plain.strip(), structure_path="RTF", section_type="body", order=0)
        return ExtractionResult(
            method="striprtf",
            text=plain,
            sections=[sec],
            metadata={},
            warnings=warnings,
        )

    return await asyncio.to_thread(_run)


async def extract_json(blob: bytes) -> ExtractionResult:
    def _run() -> ExtractionResult:
        warnings: list[str] = []
        text, enc_w = _decode_blob(blob)
        warnings.extend(enc_w)
        try:
            obj = json.loads(text)
            pretty = json.dumps(obj, indent=2, ensure_ascii=False)
        except Exception as e:
            pretty = text
            warnings.append(f"json_parse_failed_treating_as_text:{e}")
        sec = Section(text=pretty.strip(), structure_path="JSON", section_type="body", order=0)
        return ExtractionResult(
            method="json",
            text=pretty,
            sections=[sec],
            metadata={},
            warnings=warnings,
        )

    return await asyncio.to_thread(_run)


async def extract_xml(blob: bytes) -> ExtractionResult:
    def _run() -> ExtractionResult:
        warnings: list[str] = []
        try:
            from lxml import etree
        except ImportError as e:
            text, enc_w = _decode_blob(blob)
            plain = re.sub(r"<[^>]+>", " ", text)
            sec = Section(text=plain.strip(), structure_path="XML", section_type="body", order=0)
            return ExtractionResult(
                method="regex-fallback",
                text=plain,
                sections=[sec],
                metadata={},
                warnings=enc_w + [f"lxml_unavailable:{e}"],
            )
        try:
            parser = etree.XMLParser(
                resolve_entities=False,
                no_network=True,
                load_dtd=False,
            )
            root = etree.fromstring(blob, parser=parser)
            pretty = etree.tostring(root, pretty_print=True, encoding="unicode")
        except Exception as e:
            log.warning("xml parse failed: %s", e)
            text, enc_w = _decode_blob(blob)
            sec = Section(
                text=text[:500_000].strip(),
                structure_path="XML",
                section_type="body",
                order=0,
            )
            return ExtractionResult(
                method="xml",
                text=text[:500_000],
                sections=[sec],
                metadata={},
                warnings=enc_w + [str(e)],
            )
        sec = Section(text=pretty.strip(), structure_path="XML", section_type="body", order=0)
        return ExtractionResult(
            method="lxml", text=pretty, sections=[sec], metadata={}, warnings=warnings
        )

    return await asyncio.to_thread(_run)


async def extract_yaml(blob: bytes) -> ExtractionResult:
    def _run() -> ExtractionResult:
        warnings: list[str] = []
        try:
            import yaml
        except ImportError as e:
            return ExtractionResult(
                method="yaml",
                text="",
                sections=[],
                metadata={},
                warnings=[f"pyyaml_unavailable:{e}"],
            )
        text, enc_w = _decode_blob(blob)
        warnings.extend(enc_w)
        try:
            data = yaml.safe_load(text)
            dumped = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        except Exception as e:
            log.warning("yaml parse failed: %s", e)
            sec = Section(text=text.strip(), structure_path="YAML", section_type="body", order=0)
            return ExtractionResult(
                method="yaml",
                text=text,
                sections=[sec],
                metadata={},
                warnings=warnings + [str(e)],
            )
        sec = Section(text=dumped.strip(), structure_path="YAML", section_type="body", order=0)
        return ExtractionResult(
            method="pyyaml", text=dumped, sections=[sec], metadata={}, warnings=warnings
        )

    return await asyncio.to_thread(_run)


async def extract_ipynb(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_ipynb_sync, blob)
