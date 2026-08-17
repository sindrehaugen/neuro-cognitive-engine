"""
Tests for nce/openvino_npu_export.py — BATCH 1.

OpenVINO / optimum / transformers imports are mocked; no live hub or NPU required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import pytest

from nce.openvino_npu_export import export_jina_to_openvino_npu

pytestmark = pytest.mark.heavy

_REVISION = "abc123def4567890abcdef1234567890abcdef12"


def _install_mock_hub():
    """Inject fake optimum.intel + transformers into sys.modules."""
    mock_model = MagicMock(name="ov_model_instance")
    mock_ov_cls = MagicMock(name="OVModelForFeatureExtraction")
    mock_ov_cls.from_pretrained.return_value = mock_model

    mock_intel = MagicMock()
    mock_intel.OVModelForFeatureExtraction = mock_ov_cls

    mock_optimum = MagicMock()
    mock_optimum.__version__ = "0.0-mock"
    mock_optimum.intel = mock_intel

    mock_tok = MagicMock(name="tokenizer_instance")
    mock_tok_cls = MagicMock(name="AutoTokenizer")
    mock_tok_cls.from_pretrained.return_value = mock_tok

    mock_transformers = MagicMock()
    mock_transformers.__version__ = "0.0-mock"
    mock_transformers.AutoTokenizer = mock_tok_cls

    patcher = patch.dict(
        sys.modules,
        {
            "optimum": mock_optimum,
            "optimum.intel": mock_intel,
            "transformers": mock_transformers,
        },
    )
    return patcher, mock_ov_cls, mock_model, mock_tok_cls, mock_tok


# --------------------------------------------------------------------------- #
# BATCH 1: revision guard + happy-path export
# --------------------------------------------------------------------------- #


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", "")
def test_export_raises_when_revision_unset():
    """Unset NCE_OPENVINO_MODEL_REVISION → RuntimeError before tokenizer load."""
    patcher, mock_ov_cls, mock_model, _mock_tok_cls, _mock_tok = _install_mock_hub()
    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "export-out"
            with pytest.raises(
                RuntimeError,
                match="NCE_OPENVINO_MODEL_REVISION must be set",
            ):
                export_jina_to_openvino_npu(out, local_files_only=True)
            assert not out.exists()

    mock_ov_cls.from_pretrained.assert_called_once()
    mock_model.reshape.assert_called_once()
    mock_model.save_pretrained.assert_called_once()


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_export_succeeds_when_revision_set():
    """Export completes when revision is pinned and hub classes are mocked."""
    patcher, mock_ov_cls, mock_model, mock_tok_cls, mock_tok = _install_mock_hub()
    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "export-out"
            result = export_jina_to_openvino_npu(
                out,
                model_id_or_path="jinaai/jina-embeddings-v2-base-code",
                batch_size=1,
                sequence_length=512,
                local_files_only=True,
            )

            assert result == out.resolve()
            assert result.is_dir()
            assert (result / "nce_openvino_export_manifest.json").is_file()

            mock_ov_cls.from_pretrained.assert_called_once()
            mock_model.reshape.assert_called_once_with(batch_size=1, sequence_length=512)
            mock_model.save_pretrained.assert_called_once()
            assert mock_model.save_pretrained.call_args[0][0].name.startswith("nce_ov_export_")

            mock_tok_cls.from_pretrained.assert_called_once()
            mock_tok.save_pretrained.assert_called_once()


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_revision_forwarded_to_from_pretrained():
    """revision kwarg reaches both OV model and AutoTokenizer from_pretrained."""
    patcher, mock_ov_cls, _mock_model, mock_tok_cls, _mock_tok = _install_mock_hub()
    model_id = "jinaai/jina-embeddings-v2-base-code"

    with patcher:
        with TemporaryDirectory() as tmp:
            export_jina_to_openvino_npu(
                Path(tmp) / "out",
                model_id_or_path=model_id,
                local_files_only=True,
            )

    mock_ov_cls.from_pretrained.assert_called_once_with(
        model_id,
        export=True,
        compile=False,
        local_files_only=True,
        revision=_REVISION,
    )
    mock_tok_cls.from_pretrained.assert_called_once_with(
        model_id,
        local_files_only=True,
        trust_remote_code=True,
        revision=_REVISION,
    )


# --------------------------------------------------------------------------- #
# BATCH 2: input validation
# --------------------------------------------------------------------------- #


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
@pytest.mark.parametrize("batch_size", [0, 33])
def test_batch_size_out_of_range_raises(batch_size):
    with pytest.raises(ValueError, match="batch_size must be between"):
        export_jina_to_openvino_npu("/tmp/out", batch_size=batch_size)


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
@pytest.mark.parametrize("sequence_length", [0, 9000])
def test_sequence_length_out_of_range_raises(sequence_length):
    with pytest.raises(ValueError, match="sequence_length must be between"):
        export_jina_to_openvino_npu("/tmp/out", sequence_length=sequence_length)


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_empty_model_id_raises():
    with pytest.raises(ValueError, match="model_id_or_path must not be empty"):
        export_jina_to_openvino_npu("/tmp/out", model_id_or_path="   ")


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_non_empty_output_dir_raises():
    with TemporaryDirectory() as tmp:
        out = Path(tmp) / "occupied"
        out.mkdir()
        (out / "existing.bin").write_bytes(b"x")
        with pytest.raises(RuntimeError, match="already contains files"):
            export_jina_to_openvino_npu(out)


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_empty_output_dir_passes_validation():
    patcher, *_ = _install_mock_hub()
    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "fresh-out"
            export_jina_to_openvino_npu(out, local_files_only=True)
            assert out.is_dir()


# --------------------------------------------------------------------------- #
# BATCH 3: atomic export + tokenizer exception narrowing
# --------------------------------------------------------------------------- #


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_model_save_failure_leaves_no_output_dir():
    patcher, _mock_ov_cls, mock_model, _mock_tok_cls, _mock_tok = _install_mock_hub()
    mock_model.save_pretrained.side_effect = OSError("disk full")

    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "failed-export"
            with pytest.raises(OSError, match="disk full"):
                export_jina_to_openvino_npu(out, local_files_only=True)
            assert not out.exists()
            assert not any(p.name.startswith("nce_ov_export_") for p in Path(tmp).iterdir())


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_tokenizer_oserror_continues_export():
    patcher, _mock_ov_cls, mock_model, mock_tok_cls, mock_tok = _install_mock_hub()
    mock_tok.save_pretrained.side_effect = OSError("read-only fs")

    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "partial-export"
            result = export_jina_to_openvino_npu(out, local_files_only=True)

            assert result == out.resolve()
            assert (result / "nce_openvino_export_manifest.json").is_file()
            mock_model.save_pretrained.assert_called_once()


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_tokenizer_unexpected_exception_cleans_tmp():
    patcher, _mock_ov_cls, mock_model, mock_tok_cls, mock_tok = _install_mock_hub()
    mock_tok.save_pretrained.side_effect = ValueError("unexpected")

    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "bad-tokenizer-export"
            with pytest.raises(ValueError, match="unexpected"):
                export_jina_to_openvino_npu(out, local_files_only=True)
            assert not out.exists()


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_successful_export_writes_expected_artifacts():
    patcher, _mock_ov_cls, mock_model, mock_tok_cls, mock_tok = _install_mock_hub()

    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "complete-export"
            result = export_jina_to_openvino_npu(out, local_files_only=True)

            assert result.is_dir()
            assert (result / "nce_openvino_export_manifest.json").is_file()
            mock_model.save_pretrained.assert_called_once()
            mock_tok_cls.from_pretrained.assert_called_once()
            mock_tok.save_pretrained.assert_called_once()


# --------------------------------------------------------------------------- #
# BATCH 4: JSON manifest hardening
# --------------------------------------------------------------------------- #


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_manifest_is_valid_json():
    patcher, *_ = _install_mock_hub()
    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "json-manifest"
            export_jina_to_openvino_npu(out, local_files_only=True)
            raw = (out / "nce_openvino_export_manifest.json").read_text(encoding="utf-8")
            data = json.loads(raw)
            assert data["model_source"] == "jinaai/jina-embeddings-v2-base-code"


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_manifest_dependency_versions_structure():
    patcher, *_ = _install_mock_hub()
    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "deps-manifest"
            export_jina_to_openvino_npu(out, sequence_length=256, local_files_only=True)
            data = json.loads(
                (out / "nce_openvino_export_manifest.json").read_text(encoding="utf-8")
            )
            deps = data["dependency_versions"]
            assert set(deps) == {"transformers", "openvino", "optimum"}
            assert all(isinstance(v, str) for v in deps.values())


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", "")
def test_manifest_model_revision_none_when_env_unset():
    import nce.openvino_npu_export as ov_export

    assert (ov_export.OPENVINO_MODEL_REVISION or None) is None


@patch("nce.openvino_npu_export.OPENVINO_MODEL_REVISION", _REVISION)
def test_manifest_contains_truncation_note():
    patcher, *_ = _install_mock_hub()
    with patcher:
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "note-manifest"
            export_jina_to_openvino_npu(out, sequence_length=128, local_files_only=True)
            data = json.loads(
                (out / "nce_openvino_export_manifest.json").read_text(encoding="utf-8")
            )
            assert data["model_revision"] == _REVISION
            assert "sequence_length=128" in data["note"]
            assert "truncated" in data["note"]
