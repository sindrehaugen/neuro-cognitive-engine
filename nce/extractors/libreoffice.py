"""LibreOffice headless conversion (J.4, J.6, J.8, J.22 local mode)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from nce.net_safety import _verify_binary_safety

log = logging.getLogger(__name__)

_SAFE_EXT = re.compile(r"^\.[a-zA-Z0-9]{1,8}$")


def _safe_source_ext(source_ext: str) -> str:
    """Return a safe extension for temp filenames (no path segments)."""
    ext = source_ext if source_ext.startswith(".") else f".{source_ext}"
    if not _SAFE_EXT.match(ext):
        raise ValueError(f"invalid source_ext: {source_ext!r}")
    return ext


def _resolve_soffice() -> str:
    exe = os.environ.get("NCE_SOFFICE", "soffice")
    if shutil.which(exe):
        return exe
    if os.name == "nt":
        for candidate in (
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ):
            if os.path.isfile(candidate):
                return candidate
    return exe


def libreoffice_convert(
    blob: bytes,
    source_ext: str,
    target_ext: str,
    *,
    timeout: int = 180,
) -> bytes | None:
    """
    Convert document bytes via `soffice --headless --convert-to`.
    Returns None on failure (partial extraction elsewhere should log and continue).
    """
    try:
        ext = _safe_source_ext(source_ext)
    except ValueError as e:
        log.warning("libreoffice_invalid_ext: %s", e)
        return None
    target = target_ext.lstrip(".")
    soffice = _resolve_soffice()
    expected_hash = os.environ.get("NCE_SOFFICE_HASH", "").strip()
    if not expected_hash:
        log.warning("libreoffice_convert: NCE_SOFFICE_HASH environment variable is not set")
        return None
    verified_soffice = _verify_binary_safety(soffice, expected_hash)
    if not verified_soffice:
        log.warning("libreoffice_convert: binary safety check failed for %s", soffice)
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="nce_lo_") as d:
            td = Path(d)
            src = td / f"source{ext}"
            src.write_bytes(blob)
            cmd = [
                verified_soffice,
                "--headless",
                "--norestore",
                "--nolockcheck",
                "--nodefault",
                "--nofirststartwizard",
                "--convert-to",
                target,
                "--outdir",
                str(td),
                str(src),
            ]
            from nce.subprocess_registry import make_confinement_preexec, tracked_process

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                preexec_fn=make_confinement_preexec(),
            )
            with tracked_process(proc):
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    log.warning("libreoffice_timeout")
                    return None
                except Exception as e:
                    proc.kill()
                    proc.communicate()
                    raise e

            if proc.returncode != 0:
                log.warning(
                    "libreoffice_failed rc=%s stderr=%s",
                    proc.returncode,
                    (stderr or b"")[:500],
                )
                return None
            # LO names output: source.docx from source.doc
            out = td / f"source.{target}"
            if not out.is_file():
                for p in td.iterdir():
                    if p.suffix.lower() == f".{target}" and p.name != src.name:
                        out = p
                        break
            if not out.is_file():
                log.warning("libreoffice_no_output %s", list(td.iterdir()))
                return None
            return out.read_bytes()
    except FileNotFoundError:
        log.warning("libreoffice_not_found: %s", soffice)
        return None
    except subprocess.TimeoutExpired:
        log.warning("libreoffice_timeout")
        return None
    except Exception as e:
        log.warning("libreoffice_error: %s", e)
        return None
