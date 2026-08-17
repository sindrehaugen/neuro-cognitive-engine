"""J.17 — Project files: .mpp (optional MPXJ sidecar), .pub (LibreOffice → docx)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from nce.extractors import pdf_ext
from nce.extractors.core import ExtractionResult, Section, empty_skipped
from nce.extractors.libreoffice import libreoffice_convert
from nce.extractors.office_word import extract_docx
from nce.net_safety import _verify_binary_safety

log = logging.getLogger(__name__)

_FORBIDDEN_SHELL_CHARS = frozenset(";|&$`<>")
_DEFAULT_MPXJ_BINARIES = frozenset({"java", "python", "python3", "mpxj-cli"})


def _mpxj_allowed_binaries() -> frozenset[str]:
    raw = os.environ.get("NCE_MPXJ_ALLOWED_BINARIES", "").strip()
    if not raw:
        return _DEFAULT_MPXJ_BINARIES
    names = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return frozenset(names) if names else _DEFAULT_MPXJ_BINARIES


def _normalize_executable_name(path: str) -> str:
    name = Path(path).name.lower()
    if os.name == "nt" and name.endswith(".exe"):
        return name[:-4]
    return name


def _parse_mpxj_argv(cmd: str) -> list[str] | None:
    if any(ch in cmd for ch in _FORBIDDEN_SHELL_CHARS):
        return None
    try:
        argv = shlex.split(cmd, posix=os.name != "nt")
    except ValueError:
        return None
    if not argv:
        return None
    exe = _normalize_executable_name(argv[0])
    if exe not in _mpxj_allowed_binaries():
        return None
    return argv


def _extract_mpp_sync(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    cmd = os.environ.get("NCE_MPXJ_EXTRACTOR", "").strip()
    if not cmd:
        return empty_skipped(
            "mpp",
            "mpp_extractor_not_configured",
            warnings=[
                "MS Project .mpp requires a sidecar: set NCE_MPXJ_EXTRACTOR to a CLI "
                "that writes JSON to stdout. The input path is passed in env NCE_MPP_INPUT.",
            ],
        )
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mpp", delete=False) as tf:
            tf.write(blob)
            path = Path(tf.name)
        argv = _parse_mpxj_argv(cmd)
        if argv is None:
            return empty_skipped(
                "mpp",
                "mpp_bad_command",
                warnings=[
                    "NCE_MPXJ_EXTRACTOR is not on the allowlist or contains shell metacharacters"
                ],
            )
        expected_hash = os.environ.get("NCE_MPXJ_HASH", "").strip()
        if not expected_hash:
            return empty_skipped(
                "mpp",
                "mpp_binary_hash_not_configured",
                warnings=["NCE_MPXJ_HASH environment variable is not set"],
            )
        verified_bin = _verify_binary_safety(argv[0], expected_hash)
        if not verified_bin:
            return empty_skipped(
                "mpp",
                "mpp_binary_safety_failed",
                warnings=[f"MPXJ binary safety check failed for {argv[0]!r}"],
            )
        argv[0] = verified_bin

        from nce.subprocess_registry import make_confinement_preexec, tracked_process

        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "NCE_MPP_INPUT": str(path)},
            preexec_fn=make_confinement_preexec(),
        )
        with tracked_process(proc):
            try:
                stdout, stderr = proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return empty_skipped(
                    "mpp", "mpp_sidecar_timeout", warnings=["MPXJ extractor timed out"]
                )
            except Exception as e:
                proc.kill()
                proc.communicate()
                raise e

        if proc.returncode != 0:
            return empty_skipped(
                "mpp",
                "mpp_sidecar_failed",
                warnings=[(stderr or stdout or f"exit {proc.returncode}")[:500]],
            )
        raw = (stdout or "").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return empty_skipped("mpp", "mpp_json_invalid", warnings=[str(e), raw[:200]])

        sections: list[Section] = []
        order = 0
        tasks = data.get("tasks") if isinstance(data, dict) else None
        if isinstance(tasks, list):
            for t in tasks:
                if not isinstance(t, dict):
                    continue
                name = str(t.get("name") or t.get("Name") or "").strip()
                if not name:
                    continue
                lvl = t.get("outlineLevel", t.get("outline_level", 0))
                prefix = f"Task[{lvl}]: "
                sections.append(
                    Section(
                        text=f"{prefix}{name}",
                        structure_path=f"MPP task {order + 1}",
                        section_type="project",
                        order=order,
                    )
                )
                order += 1
        notes = data.get("notes") if isinstance(data, dict) else None
        if isinstance(notes, list):
            for n in notes:
                if isinstance(n, str) and n.strip():
                    sections.append(
                        Section(
                            text=n.strip(),
                            structure_path=f"MPP note {order + 1}",
                            section_type="note",
                            order=order,
                        )
                    )
                    order += 1

        full = "\n".join(s.text for s in sections)
        if not full.strip():
            warnings.append("mpp_sidecar_empty_tasks")
        return ExtractionResult(
            method="mpp",
            text=full,
            sections=sections
            or [
                Section(
                    text="(no tasks)",
                    structure_path="MPP",
                    section_type="project",
                    order=0,
                )
            ],
            metadata={"task_sections": len(sections)},
            warnings=warnings,
        )
    except subprocess.TimeoutExpired:
        return empty_skipped("mpp", "mpp_sidecar_timeout", warnings=["MPXJ extractor timed out"])
    except FileNotFoundError as e:
        return empty_skipped("mpp", "mpp_executable_missing", warnings=[str(e)])
    except Exception as e:
        log.warning("mpp_extract_failed: %s", e, exc_info=True)
        return empty_skipped("mpp", "extraction_failed", warnings=[str(e)])
    finally:
        if path and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


async def extract_mpp(blob: bytes) -> ExtractionResult:
    return await asyncio.to_thread(_extract_mpp_sync, blob)


async def extract_pub(blob: bytes) -> ExtractionResult:
    warnings: list[str] = []
    try:
        docx_bytes = await asyncio.to_thread(libreoffice_convert, blob, ".pub", "docx")
    except Exception as e:
        return empty_skipped("pub", "libreoffice_exception", warnings=[str(e)])

    if not docx_bytes:
        # Publisher sometimes converts better to PDF for text
        try:
            pdf_bytes = await asyncio.to_thread(libreoffice_convert, blob, ".pub", "pdf")
        except Exception as e:
            warnings.append(f"pub_pdf_fallback:{e}")
            pdf_bytes = None
        if pdf_bytes:
            try:
                return await pdf_ext.extract_pdf(pdf_bytes)
            except Exception as e:
                return empty_skipped("pub", "pdf_fallback_failed", warnings=[str(e)])
        return empty_skipped(
            "pub",
            "libreoffice_conversion_failed",
            warnings=warnings
            + [
                "Install LibreOffice and ensure soffice is on PATH (or set NCE_SOFFICE).",
            ],
        )

    try:
        inner = await extract_docx(docx_bytes)
    except Exception as e:
        return empty_skipped("pub", "docx_parse_failed", warnings=[str(e)])

    warnings.extend(inner.warnings)
    meta = dict(inner.metadata)
    meta["source_format"] = "pub_via_libreoffice_docx"
    return ExtractionResult(
        method="pub",
        text=inner.text,
        sections=inner.sections,
        metadata=meta,
        warnings=warnings,
    )
