"""
Intel NPU — static-shape OpenVINO export for jinaai/jina-embeddings-v2-base-code (§8.3).

Call this from the installer or a one-shot admin script after the Hugging Face snapshot
is available locally. This module does not download weights by itself unless you call
`export_jina_to_openvino_npu` with `local_files_only=False` and allow hub access.

Export output is loaded at runtime by `OpenVINONPUBackend` via `NCE_OPENVINO_MODEL_DIR`.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger("nce-openvino-export")

DEFAULT_MODEL_ID = "jinaai/jina-embeddings-v2-base-code"

# Model revision pin for trust_remote_code safety (FIX-053)
OPENVINO_MODEL_REVISION = os.environ.get("NCE_OPENVINO_MODEL_REVISION", "")

_BATCH_SIZE_MIN = 1
_BATCH_SIZE_MAX = 32
_SEQ_LEN_MIN = 1
_SEQ_LEN_MAX = 8192


def export_jina_to_openvino_npu(
    output_dir: str | Path,
    *,
    model_id_or_path: str = DEFAULT_MODEL_ID,
    batch_size: int = 1,
    sequence_length: int = 512,
    compile_for_npu: bool = False,
    local_files_only: bool = False,
) -> Path:
    """
    Export the code-embedding model to OpenVINO IR with fixed batch × sequence shapes
    required by Intel NPU (static graphs only).

    Mirrors §8.3:
      1. ``OVModelForFeatureExtraction.from_pretrained(..., export=True, compile=False)``
      2. ``model.reshape(batch_size=batch_size, sequence_length=sequence_length)``
      3. Optional ``compile()`` then ``save_pretrained(output_dir)``

    Parameters
    ----------
    output_dir:
        Directory to write IR + tokenizer artifacts (e.g. ./jina-openvino-npu).
    model_id_or_path:
        Hugging Face hub id or local snapshot path.
    batch_size:
        Static batch dimension (installer typically uses 1).
    sequence_length:
        Fixed token length; inference truncates/pads to this bound.
    compile_for_npu:
        When True, runs OpenVINO ``compile()`` after reshape.
    local_files_only:
        When True, ``from_pretrained`` never hits the hub (offline / air-gapped).

    Returns
    -------
    Path
        Resolved output directory.
    """
    model_id_or_path = str(model_id_or_path).strip()
    if not model_id_or_path:
        raise ValueError("model_id_or_path must not be empty")

    if not (_BATCH_SIZE_MIN <= batch_size <= _BATCH_SIZE_MAX):
        raise ValueError(
            f"batch_size must be between {_BATCH_SIZE_MIN} and {_BATCH_SIZE_MAX}, got {batch_size}"
        )
    if not (_SEQ_LEN_MIN <= sequence_length <= _SEQ_LEN_MAX):
        raise ValueError(
            f"sequence_length must be between {_SEQ_LEN_MIN} and {_SEQ_LEN_MAX}, got {sequence_length}"
        )

    output_dir = Path(output_dir).resolve()

    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(
            f"output_dir {output_dir} already contains files. "
            "Pass an empty or non-existent directory to avoid overwriting artifacts."
        )

    try:
        from optimum.intel import OVModelForFeatureExtraction
    except ImportError as e:
        raise RuntimeError(
            "openvino_npu_export requires optimum with Intel OpenVINO support. "
            "Install e.g. pip install 'optimum[openvino-intel]' openvino"
        ) from e

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="nce_ov_export_", dir=output_dir.parent))

    try:
        log.info(
            "OpenVINO NPU export: model=%r out=... batch=%s seq=%s local_only=%s",
            model_id_or_path[:80] + ("..." if len(model_id_or_path) > 80 else ""),
            batch_size,
            sequence_length,
            local_files_only,
        )

        model = OVModelForFeatureExtraction.from_pretrained(
            model_id_or_path,
            export=True,
            compile=False,
            local_files_only=local_files_only,
            revision=OPENVINO_MODEL_REVISION or None,
        )

        model.reshape(batch_size=batch_size, sequence_length=sequence_length)

        if compile_for_npu:
            model.compile()

        model.save_pretrained(tmp_dir)

        if not OPENVINO_MODEL_REVISION:
            raise RuntimeError(
                "NCE_OPENVINO_MODEL_REVISION must be set to a commit SHA before "
                "exporting models that require trust_remote_code. "
                "Unset: set NCE_OPENVINO_MODEL_REVISION=<sha> and re-run."
            )

        try:
            from transformers import AutoTokenizer

            tok = AutoTokenizer.from_pretrained(
                model_id_or_path,
                local_files_only=local_files_only,
                trust_remote_code=True,
                revision=OPENVINO_MODEL_REVISION,
            )
            tok.save_pretrained(tmp_dir)
        except (OSError, ImportError) as e:
            log.warning(
                "Tokenizer save failed (%s); export IR may still be loadable "
                "if tokenizer is provided separately: %s",
                type(e).__name__,
                e,
            )
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        import json as _json

        try:
            import transformers as _tf

            _tf_version = _tf.__version__
        except ImportError:
            _tf_version = "unavailable"

        try:
            import openvino as _ov

            _ov_version = getattr(_ov, "__version__", "unknown")
        except ImportError:
            _ov_version = "unavailable"

        try:
            import optimum as _opt

            _opt_version = _opt.__version__
        except ImportError:
            _opt_version = "unavailable"

        manifest_data = {
            "model_source": model_id_or_path,
            "model_revision": OPENVINO_MODEL_REVISION or None,
            "batch_size": batch_size,
            "sequence_length": sequence_length,
            "compile_for_npu": compile_for_npu,
            "local_files_only": local_files_only,
            "exporter": "nce.openvino_npu_export.export_jina_to_openvino_npu",
            "dependency_versions": {
                "transformers": _tf_version,
                "openvino": _ov_version,
                "optimum": _opt_version,
            },
            "note": (
                f"Inputs longer than sequence_length={sequence_length} tokens "
                "will be truncated at inference time."
            ),
        }

        manifest = tmp_dir / "nce_openvino_export_manifest.json"
        manifest.write_text(_json.dumps(manifest_data, indent=2), encoding="utf-8")

        tmp_dir.rename(output_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    log.info("OpenVINO export finished: %s", output_dir)
    return output_dir
