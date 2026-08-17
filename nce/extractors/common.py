"""Shared helpers: encryption sniff, table rendering, garbled text heuristics."""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

# --- ZIP-based Office (OOXML) quick encryption check ---
OOXML_ENCRYPTED_MARKERS = (
    "EncryptionInfo",
    "encryptedPackage",
    "EncryptedPackage",
)


def is_zip_encrypted_ooxml(blob: bytes) -> bool:
    """Best-effort: detect encrypted OOXML before spending CPU on full parse."""
    if len(blob) < 4 or blob[:2] != b"PK":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        return False
    for marker in OOXML_ENCRYPTED_MARKERS:
        if any(marker.lower() in n.lower() for n in names):
            return True
    return False


def is_pdf_encrypted_blob(blob: bytes) -> bool:
    """Detect /Encrypt in PDF trailer (Appendix J.18)."""
    if b"/Encrypt" in blob[: min(len(blob), 8192)]:
        return True
    # trailer often not at start; scan bounded window from end too
    tail = blob[-16384:] if len(blob) > 16384 else blob
    return b"/Encrypt" in tail


def trim_trailing_empty(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    if not rows:
        return rows
    # trim empty rows from bottom
    r = list(rows)
    while r and all(v is None or (isinstance(v, str) and not v.strip()) for v in r[-1]):
        r.pop()
    if not r:
        return r
    # trim empty cols from right
    width = max(len(row) for row in r)
    while width > 0:
        if all(
            len(row) < width or row[width - 1] is None or str(row[width - 1]).strip() == ""
            for row in r
        ):
            width -= 1
        else:
            break
    if width <= 0:
        return []
    return [tuple((row[i] if i < len(row) else None) for i in range(width)) for row in r]


def cell_to_str(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            pass
    s = str(v)
    return s


def rows_to_markdown(rows: list[tuple[Any, ...]]) -> str:
    if not rows:
        return ""
    lines = []
    for row in rows:
        cells = [cell_to_str(c).replace("|", "\\|") for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


_NON_PRINT_RATIO_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def looks_garbled(text: str, threshold: float = 0.12) -> bool:
    if not text.strip():
        return True
    bad = len(_NON_PRINT_RATIO_RE.findall(text))
    ctl_ratio = bad / max(len(text), 1)
    high_xor = sum(1 for c in text[:2000] if ord(c) > 0xFF) / max(min(len(text), 2000), 1)
    return ctl_ratio > threshold or high_xor > 0.25


def split_tables_by_blank_rows(
    rows: list[tuple[Any, ...]],
    *,
    min_rows_for_split: int = 50,
) -> list[list[tuple[Any, ...]]]:
    """When a sheet is large, split on fully blank separator rows (J.5 edge case)."""
    if len(rows) <= min_rows_for_split:
        return [rows]
    tables: list[list[tuple[Any, ...]]] = []
    cur: list[tuple[Any, ...]] = []
    for row in rows:
        empty = all(v is None or (isinstance(v, str) and not str(v).strip()) for v in row)
        if empty and cur:
            tables.append(cur)
            cur = []
        elif not empty:
            cur.append(row)
    if cur:
        tables.append(cur)
    return tables if tables else [rows]


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for sel in ("script", "style", "nav", "header", "footer"):
            for n in tree.css(sel):
                try:
                    n.decompose()
                except AttributeError:
                    n.remove()
        t = tree.text(separator="\n", strip=True)
        if t:
            return t
    except Exception:
        pass
    return re.sub(r"<[^>]+>", " ", html)
