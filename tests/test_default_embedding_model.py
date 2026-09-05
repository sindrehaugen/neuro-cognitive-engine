"""The default embedding model must actually load (D42).

Strategy — NO importlib.reload
-------------------------------
Follows ``tests/test_config_diag.py``: ``nce/config.py`` installs the shared
secrets-provider seam at import time, so reloading it pollutes global state for
other tests sharing an xdist worker. Default-value assertions hit the
already-imported ``cfg`` directly.

These assertions are deliberately configuration-only — no model is downloaded.
The load behaviour they stand for was measured out of band against this repo's
pins (transformers 5.16.1, sentence-transformers 6.0.1):

    jinaai/jina-embeddings-v2-base-code, trust_remote_code=False
        ValueError: Specified `attn_implementation="torch"` is not supported
    jinaai/jina-embeddings-v2-base-code, trust_remote_code=True
        ImportError: cannot import name 'find_pruneable_heads_and_indices'
    sentence-transformers/all-mpnet-base-v2, trust_remote_code=False
        OK  dim=768  norm=1.0000
"""

from __future__ import annotations

from nce.config import cfg

# The model the default must be. 768 dims, matching VECTOR_DIM and the
# halfvec(768) columns, so changing to it needs no schema migration.
EXPECTED_DEFAULT = "sentence-transformers/all-mpnet-base-v2"

# The default that could not load, in either configuration. Named so a revert
# is caught here rather than rediscovered at runtime.
BROKEN_DEFAULT = "jinaai/jina-embeddings-v2-base-code"


def test_default_embedding_model_id() -> None:
    assert cfg.NCE_EMBEDDING_MODEL_ID == EXPECTED_DEFAULT


def test_default_embedding_model_is_not_the_unloadable_one() -> None:
    """D42 regression.

    The Jina code model requires trust_remote_code, and its custom modeling
    code imports find_pruneable_heads_and_indices, removed in transformers 5.x.
    It is still reachable by setting NCE_EMBEDDING_MODEL_ID explicitly, and it
    remains the model behind the OpenVINO IR path — it just cannot be the
    in-process default.
    """
    assert cfg.NCE_EMBEDDING_MODEL_ID != BROKEN_DEFAULT


def test_default_model_needs_no_remote_code() -> None:
    """The shipped defaults must be internally consistent.

    NCE_EMBEDDING_TRUST_REMOTE_CODE defaults to False, so a default model that
    requires remote code can never load. That pairing *was* the defect: the two
    settings shipped contradicting each other. Asserting both together is what
    makes this discriminating — either half alone would have passed before.
    """
    assert cfg.NCE_EMBEDDING_TRUST_REMOTE_CODE is False
    assert cfg.NCE_EMBEDDING_MODEL_ID != BROKEN_DEFAULT
