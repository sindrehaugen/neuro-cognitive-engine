"""Pure-unit tests for the diag_ingest RQ lane (Batch 68).

Verifies:
* ``DIAG_INGEST_QUEUE`` constant value.
* ``get_diag_queue`` returns a Queue named ``"diag_ingest"``.
* ``"diag_ingest"`` appears in ``start_worker.QUEUE_NAMES``.

No Docker / Redis required: RQ ``Queue`` is instantiated with
``is_async=False`` in test environments, which does not connect to Redis
on construction.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from start_worker import QUEUE_NAMES

from nce.extractors.dispatch import DIAG_INGEST_QUEUE, get_diag_queue


def test_diag_ingest_queue_constant() -> None:
    """DIAG_INGEST_QUEUE must equal the canonical lane name."""
    assert DIAG_INGEST_QUEUE == "diag_ingest"


def test_get_diag_queue_returns_queue_named_diag_ingest() -> None:
    """get_diag_queue returns an RQ Queue whose name is ``diag_ingest``."""
    fake_conn = MagicMock()
    q = get_diag_queue(fake_conn)
    assert q.name == "diag_ingest"


def test_diag_ingest_in_queue_names() -> None:
    """``diag_ingest`` must be present in start_worker.QUEUE_NAMES."""
    assert "diag_ingest" in QUEUE_NAMES
