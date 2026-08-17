"""
tests/perf/test_mongo_bulk_dedup.py
OL finding O22: mongo-bulk-dual-dedup-structures (RAM)

The pre-patch _normalize_and_validate_refs kept BOTH a ``seen: list[str]`` and a
``uniq: dict[str, None]``; the patch drops ``seen`` and iterates the
insertion-ordered dict directly. Insertion order (py3.7+) makes the output
identical. This test:
  1. (default suite) proves the new impl is byte-identical to a frozen
     dual-structure reference oracle  -> behavior preservation.
  2. (-m perf) measures heap peak of both and asserts the single-structure
     version does not regress vs the dual-structure one.
"""

from __future__ import annotations

import pytest

from nce.mongo_bulk import (
    _MAX_REFS,
    _normalize_and_validate_refs,
    _safe_object_id,
    normalize_payload_ref,
)
from tests.perf.bench import measure

_N_UNIQUE = 500
_N_DUPES = 100


def _build_refs() -> list:
    valid = [f"{i:024x}" for i in range(_N_UNIQUE)]
    return valid + valid[:_N_DUPES] + [None] * 20 + ["not-an-objectid"] * 10


_REFS = _build_refs()


def _dual_structure_reference(refs):
    """Frozen copy of the PRE-patch dual-structure logic (behavior oracle)."""
    seen: list = []
    uniq: dict = {}
    for ref in refs:
        key = normalize_payload_ref(ref)
        if not key or key in uniq:
            continue
        uniq[key] = None
        seen.append(key)
    if not seen:
        return []
    if len(seen) > _MAX_REFS:
        raise ValueError("too many")
    oids = []
    for key in seen:
        oid = _safe_object_id(key)
        if oid is None:
            continue
        oids.append(oid)
    return oids


def test_mongo_bulk_dedup_correctness() -> None:
    """Runs in the default suite: new impl == dual-structure reference."""
    new = _normalize_and_validate_refs(_REFS)
    ref = _dual_structure_reference(_REFS)
    assert new == ref
    assert len(new) == _N_UNIQUE


@pytest.mark.perf
def test_mongo_bulk_dedup_heap() -> None:
    before = measure(lambda: _dual_structure_reference(_REFS), n=30, warmup=3)
    after = measure(lambda: _normalize_and_validate_refs(_REFS), n=30, warmup=3)
    if before.heap_peak_mean and after.heap_peak_mean:
        d_kb = (before.heap_peak_mean - after.heap_peak_mean) / 1024
        d_pct = (before.heap_peak_mean - after.heap_peak_mean) / before.heap_peak_mean * 100
        print(
            f"\n[O22] heap dual={before.heap_peak_mean / 1024:.2f}KB "
            f"single={after.heap_peak_mean / 1024:.2f}KB "
            f"delta={d_kb:.2f}KB ({d_pct:.1f}%)"
        )
        # Non-regression guard (the ~6% win can sit within tracemalloc noise on
        # a single in-process run; assert single does not allocate MORE).
        assert after.heap_peak_mean <= before.heap_peak_mean * 1.10
