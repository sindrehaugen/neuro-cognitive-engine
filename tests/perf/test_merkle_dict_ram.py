"""
tests/perf/test_merkle_dict_ram.py
------------------------------------
OL RAM finding: merkle-anchor-dict-always-built

Measures heap-peak delta for verify_merkle_chain on a synthetic 50k-event
namespace, both without anchor (common/CI path) and with anchor.

The test is self-contained: it populates RecordingFakeConnection.event_inserts
directly using the internal hash functions (_compute_content_hash,
_compute_chain_hash), replicating the data shape that verify_merkle_chain
reads from the DB.  This avoids async append_event overhead per event and
makes chain building fast enough to run in a test.

Noise threshold rationale (no-anchor path):
  At 50k events, each dict entry is roughly int(28) + bytes(57) + dict-overhead
  ~ 120 bytes amortised = ~6 MB total.  The perf test heap budget includes
  the row list itself (~50k * row-dict), so the dict contribution is measured
  as the DELTA between the patched and unpatched runs.  A reduction > 5% of
  the baseline heap_peak (i.e. > noise) is a clear win.

Run with: pytest -m perf tests/perf/test_merkle_dict_ram.py -s -v
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from nce.event_log import (
    _GENESIS_SENTINEL,
    _build_signing_fields,
    _compute_chain_hash,
    _compute_content_hash,
    verify_merkle_chain,
)
from tests.fixtures.fake_asyncpg import RecordingFakeConnection
from tests.perf.bench import measure

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_N_EVENTS = 50_000
_ANCHOR_FRAC = 0.5  # anchor at midpoint for the anchored path

_DB_CLOCK = datetime(2026, 6, 23, 10, 0, 0, tzinfo=timezone.utc)
_OCCURRED_AT_ISO = _DB_CLOCK.isoformat()

# ---------------------------------------------------------------------------
# Synthetic chain builder (sync, no DB, no HMAC)
# ---------------------------------------------------------------------------


def _build_synthetic_chain(n: int) -> tuple[RecordingFakeConnection, object]:
    """
    Build a RecordingFakeConnection pre-populated with *n* chained event rows.

    Rows are constructed with _compute_content_hash + _compute_chain_hash so
    verify_merkle_chain sees a valid chain.  No asyncio.run per event, no HMAC
    signing -- just the hash chain and the minimal row fields the function reads.

    Returns (conn, namespace_id).
    """
    ns_id = uuid4()
    conn = RecordingFakeConnection(db_clock=_DB_CLOCK)

    previous_hash: bytes = _GENESIS_SENTINEL
    agent_id = "perf-agent"
    event_type = "store_memory"

    for seq in range(1, n + 1):
        event_id = uuid4()
        params: dict = {"idx": seq, "saga_id": str(uuid4()), "memory_id": str(uuid4())}

        signing_fields = _build_signing_fields(
            event_id=event_id,
            namespace_id=ns_id,
            agent_id=agent_id,
            event_type=event_type,
            event_seq=seq,
            occurred_at_iso=_OCCURRED_AT_ISO,
            params=params,
            parent_event_id=None,
            prev_chain_hash_hex=previous_hash.hex(),
        )
        content_hash = _compute_content_hash(signing_fields=signing_fields)
        chain_hash = _compute_chain_hash(
            content_hash=content_hash,
            previous_chain_hash=previous_hash,
        )
        previous_hash = chain_hash

        conn.event_inserts.append(
            {
                "id": event_id,
                "namespace_id": ns_id,
                "agent_id": agent_id,
                "event_type": event_type,
                "event_seq": seq,
                "occurred_at": _DB_CLOCK,
                "params": json.dumps(params, sort_keys=True),
                "parent_event_id": None,
                "chain_hash": chain_hash,
                "signature_version": 2,
            }
        )

    return conn, ns_id


# ---------------------------------------------------------------------------
# Workload callables (sync wrappers around async verify_merkle_chain)
# ---------------------------------------------------------------------------


def _make_no_anchor_workload(conn, ns_id):
    """Sync callable: verify full chain without anchor (common admin/CI path)."""

    def _run() -> None:
        asyncio.run(verify_merkle_chain(conn, namespace_id=ns_id))

    return _run


def _make_anchor_workload(conn, ns_id, anchor_seq: int, anchor_hash: bytes):
    """Sync callable: verify full chain with anchor at anchor_seq."""

    def _run() -> None:
        asyncio.run(
            verify_merkle_chain(
                conn,
                namespace_id=ns_id,
                anchor_chain_hash=anchor_hash,
                anchor_seq=anchor_seq,
            )
        )

    return _run


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.perf
def test_merkle_dict_ram_no_anchor() -> None:
    """
    Capture heap-peak for anchor-less verify_merkle_chain on 50k events.

    This is the COMMON path (admin/CI health check).  Before the optimisation
    the seq_to_recomputed_hash dict is always built and discarded; after it
    must not be built at all.

    Noise threshold: heap reduction > 5% of baseline is the win gate.
    Expected saving: ~6-8 MB (50k * ~120 bytes amortised dict overhead).
    """
    print(f"\n[merkle-dict-RAM] Building {_N_EVENTS}-event synthetic chain...")
    conn, ns_id = _build_synthetic_chain(_N_EVENTS)
    print(
        f"[merkle-dict-RAM] Chain built ({len(conn.event_inserts)} rows). Measuring (no anchor)..."
    )

    workload = _make_no_anchor_workload(conn, ns_id)
    result = measure(workload, n=3, warmup=1)

    heap_kb = result.heap_peak_mean / 1024 if result.heap_peak_mean else None
    wall_ms = result.wall_mean * 1000

    print(
        f"\n[OL merkle-dict-RAM | no-anchor | n_events={_N_EVENTS}]\n"
        f"  heap_peak_mean  = {heap_kb:.1f} KB\n"
        f"  wall_mean       = {wall_ms:.1f} ms\n"
        f"  cpu_mean        = {result.cpu_mean * 1000:.1f} ms\n"
        f"  n_runs          = {result.n}\n"
        f"  -- This is the BEFORE/AFTER reference line for the no-anchor path --\n"
    )

    assert result.n == 3
    assert result.wall_mean > 0
    # Sanity: chain hash-integrity is valid (use explicit end_seq to skip the
    # event_sequences counter check, which requires append_event's upsert path)
    check = asyncio.run(
        verify_merkle_chain(conn, namespace_id=ns_id, start_seq=1, end_seq=_N_EVENTS)
    )
    assert check["valid"] is True
    assert check["checked"] == _N_EVENTS


@pytest.mark.perf
def test_merkle_dict_ram_with_anchor() -> None:
    """
    Capture heap-peak for verify_merkle_chain WITH anchor (midpoint of 50k events).

    After the optimisation the dict is still built but stops accumulating past
    anchor_seq, so the saving is ~50% of the no-anchor case.
    """
    print(f"\n[merkle-dict-RAM] Building {_N_EVENTS}-event synthetic chain...")
    conn, ns_id = _build_synthetic_chain(_N_EVENTS)
    anchor_seq = int(_N_EVENTS * _ANCHOR_FRAC)
    anchor_hash = conn.event_inserts[anchor_seq - 1]["chain_hash"]
    print(f"[merkle-dict-RAM] Chain built. Measuring (anchor_seq={anchor_seq})...")

    workload = _make_anchor_workload(conn, ns_id, anchor_seq, anchor_hash)
    result = measure(workload, n=3, warmup=1)

    heap_kb = result.heap_peak_mean / 1024 if result.heap_peak_mean else None
    wall_ms = result.wall_mean * 1000

    print(
        f"\n[OL merkle-dict-RAM | with-anchor | n_events={_N_EVENTS} | anchor_seq={anchor_seq}]\n"
        f"  heap_peak_mean  = {heap_kb:.1f} KB\n"
        f"  wall_mean       = {wall_ms:.1f} ms\n"
        f"  cpu_mean        = {result.cpu_mean * 1000:.1f} ms\n"
        f"  n_runs          = {result.n}\n"
        f"  -- This is the BEFORE/AFTER reference line for the anchored path --\n"
    )

    assert result.n == 3
    assert result.wall_mean > 0
    # Sanity: anchor match (use explicit end_seq to skip event_sequences counter check)
    check = asyncio.run(
        verify_merkle_chain(
            conn,
            namespace_id=ns_id,
            start_seq=1,
            end_seq=_N_EVENTS,
            anchor_chain_hash=anchor_hash,
            anchor_seq=anchor_seq,
        )
    )
    assert check["valid"] is True
    assert check.get("anchor_match") is True
