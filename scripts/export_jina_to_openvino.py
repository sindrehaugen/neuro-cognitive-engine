"""Export jinaai/jina-embeddings-v2-base-code to a static-shape OpenVINO IR.

Why this exists (design finding D42, measured 2026-08-31)
---------------------------------------------------------
NCE defaults ``NCE_EMBEDDING_MODEL_ID`` to ``jinaai/jina-embeddings-v2-base-code``.
That model ships custom ``modeling_bert.py`` code which imports
``find_pruneable_heads_and_indices`` — removed in transformers 5.x — so the model
cannot load under NCE's runtime pin (``transformers>=5.14.1``). Do NOT pin
transformers back. Instead, this script exports the model once to an OpenVINO IR,
which can then be loaded without ever importing Jina's custom modeling code.

HOW TO LOAD THE IR AT RUNTIME (measured 2026-08-31 — read this before wiring it up)
-----------------------------------------------------------------------------------
Use the **raw** ``openvino`` runtime plus a standard ``transformers.AutoTokenizer``.
Do **not** use ``optimum.intel.OVModelForFeatureExtraction``: the newest
``optimum-intel`` (2.1.0) pins ``transformers<5.6``, while NCE pins
``transformers>=5.14.1``, so the two cannot coexist. Worse, pip does not error on
that conflict — it silently backtracks to ``optimum-intel==1.15.0`` (2024), which
cannot detect OpenVINO at all and fails with a misleading
"requires the openvino library but it was not found".

Verified working on this IR: ``openvino.Core().compile_model(<xml>, "NPU")`` with
static inputs ``[1, 512]`` and output ``[1, 512, 768]``.

You cannot export a model you cannot load, so the export itself must run inside a
THROWAWAY venv with ``transformers<5``. The repo never depends on that venv.

Throwaway export-venv setup (PowerShell; adapt paths / use bin/ on Linux)::

    python -m venv $env:TEMP\\ov-export
    & $env:TEMP\\ov-export\\Scripts\\python -m pip install --upgrade pip
    & $env:TEMP\\ov-export\\Scripts\\pip install "transformers>=4.41,<5" `
        "optimum[openvino]" torch --extra-index-url https://download.pytorch.org/whl/cpu
    & $env:TEMP\\ov-export\\Scripts\\python scripts\\export_jina_to_openvino.py

The exported IR is reshaped to STATIC shapes (batch 1 x --seq-len tokens) because
NPU compilation requires static shapes. The IR blob is host-local and must NEVER
be committed to the repo.

This is a standalone tool: it must not import anything from ``nce/``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_MODEL_ID = "jinaai/jina-embeddings-v2-base-code"
DEFAULT_OUTPUT_DIR = Path.home() / ".nce" / "models" / "jina-embeddings-v2-base-code-ov"
DEFAULT_SEQ_LEN = 512  # matches the NCE_OPENVINO_SEQ_LEN default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export a Jina embedding model to a static-shape OpenVINO IR "
            "(feature-extraction task). Run inside a throwaway transformers<5 venv; "
            "see the module docstring for setup commands."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to write the IR and tokenizer to (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--seq-len",
        type=int,
        default=DEFAULT_SEQ_LEN,
        help=f"Static sequence length to reshape the IR to (default: {DEFAULT_SEQ_LEN})",
    )
    parser.add_argument(
        "--model-id",
        default=DEFAULT_MODEL_ID,
        help=f"Hugging Face model id to export (default: {DEFAULT_MODEL_ID})",
    )
    return parser.parse_args(argv)


def export(model_id: str, output_dir: Path, seq_len: int) -> int:
    """Export ``model_id`` to a static-shape OpenVINO IR under ``output_dir``.

    Returns the embedding dimension (hidden size) of the exported model.
    """
    try:
        from optimum.intel import OVModelForFeatureExtraction
        from transformers import AutoTokenizer
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "Missing export dependencies. Run this script inside the throwaway "
            "transformers<5 export venv described in the module docstring "
            f"(import failed: {exc})"
        ) from exc

    # trust_remote_code is acceptable HERE ONLY: this runs in the throwaway export
    # venv, where transformers<5 can still execute Jina's custom modeling code.
    # The exported IR is later loaded WITHOUT trust_remote_code.
    model = OVModelForFeatureExtraction.from_pretrained(
        model_id,
        export=True,
        trust_remote_code=True,
    )

    # NPU compilation needs static shapes: batch 1 x seq_len tokens.
    model.reshape(batch_size=1, sequence_length=seq_len)

    output_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(output_dir)

    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.save_pretrained(output_dir)

    return int(model.config.hidden_size)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir.expanduser().resolve()

    embedding_dim = export(args.model_id, output_dir, args.seq_len)

    print(f"output_dir: {output_dir}")
    print(f"embedding_dim: {embedding_dim}")
    print(f"seq_len: {args.seq_len} (static shapes: batch 1 x {args.seq_len})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
