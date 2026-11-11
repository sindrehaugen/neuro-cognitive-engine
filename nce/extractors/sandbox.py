"""
nce/extractors/sandbox.py
=========================
Subprocess sanitization, process confinement, and extraction budget defenses.
"""

from __future__ import annotations

import io
import logging
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

from nce.subprocess_registry import make_confinement_preexec, tracked_process

log = logging.getLogger("nce.extractors.sandbox")

_SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"


def build_sanitized_subprocess_env(
    work_dir: Path | str | None = None,
    extra_safe_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a minimal environment stripped of all os.environ secrets.

    Contains only safe system variables:
      - PATH: /usr/local/bin:/usr/bin:/bin
      - HOME: <work_dir> (or temp directory)
      - TMPDIR: <work_dir> (or temp directory)
      - LANG: C.UTF-8
      - LC_ALL: C.UTF-8
    Completely stripped of NCE_MASTER_KEY, PG_DSN, DATABASE_URL, MONGO_URI,
    REDIS_URL, MINIO_SECRET_KEY, OPENAI_API_KEY, AWS_*, AZURE_*, etc.
    """
    tmp_path = str(work_dir) if work_dir is not None else tempfile.gettempdir()
    env: dict[str, str] = {
        "PATH": _SAFE_PATH,
        "HOME": tmp_path,
        "TMPDIR": tmp_path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if extra_safe_env:
        env.update(extra_safe_env)
    return env


def enforce_extraction_budget(
    data: bytes,
    *,
    max_bytes: int = 50 * 1024 * 1024,
    max_expansion_ratio: float = 100.0,
) -> None:
    """Enforce raw byte size and archive decompression ratio (zip-bomb defense).

    Raises ValueError if raw data exceeds max_bytes or if zip decompression
    exceeds max_expansion_ratio.
    """
    if len(data) > max_bytes:
        raise ValueError(
            f"Extraction budget exceeded: input size {len(data)} bytes exceeds limit of {max_bytes} bytes."
        )

    # Check for zip bomb if payload has zip magic bytes (PK\x03\x04)
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                total_uncompressed = sum(info.file_size for info in zf.infolist())
                compressed_size = max(len(data), 1)
                expansion_ratio = total_uncompressed / compressed_size
                if expansion_ratio > max_expansion_ratio:
                    raise ValueError(
                        f"Zip-bomb detected: expansion ratio {expansion_ratio:.1f}x exceeds budget {max_expansion_ratio:.1f}x "
                        f"(uncompressed {total_uncompressed} bytes vs compressed {compressed_size} bytes)."
                    )
        except zipfile.BadZipFile:
            pass


def run_sandboxed_extractor(
    cmd: list[str],
    *,
    timeout_s: float = 30.0,
    max_output_bytes: int = 10_485_760,
    work_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    """Execute subprocess inside a confined directory with a sanitized environment.

    Runs with:
      - Sanitized environment stripped of all host secrets
      - CPU & address-space resource limits (make_confinement_preexec)
      - Enforced timeout
      - Output truncated / validated against max_output_bytes limit
    """
    effective_dir = work_dir or Path(tempfile.gettempdir())
    sanitized_env = build_sanitized_subprocess_env(work_dir=effective_dir)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(effective_dir),
        env=sanitized_env,
        preexec_fn=make_confinement_preexec(),
    )

    with tracked_process(proc):
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout_s, output=b"", stderr=b"") from exc
        except Exception:
            proc.kill()
            proc.communicate()
            raise

    stdout = stdout or b""
    stderr = stderr or b""

    if len(stdout) > max_output_bytes:
        log.warning(
            "Extractor output truncated from %d bytes to %d bytes",
            len(stdout),
            max_output_bytes,
        )
        stdout = stdout[:max_output_bytes]

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )
