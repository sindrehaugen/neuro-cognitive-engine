"""
Phase 2.2 — Memory Time Travel (graph_search as_of).

Seeds a synthetic event_log timeline with fixed timezone.utc timestamps, drives
``GraphRAGTraverser.search`` through a fake asyncpg pool whose ``fetch`` implements
the same *reconstruction rules* as ``nce/graph_query.py`` (latest event per
memory at cutoff, ``store_memory`` vs ``forget_memory``).

External I/O mocked: Mongo hydrate returns []. Embeddings are deterministic scalars
derived from the query string so anchor choice is stable across runs.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from nce.graph_query import GraphRAGTraverser

# ---------------------------------------------------------------------------
# Deterministic timeline (timezone.utc)
# ---------------------------------------------------------------------------

TZ_UTC = timezone.utc
T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=TZ_UTC)
T1 = datetime(2026, 1, 1, 10, 15, 0, tzinfo=TZ_UTC)
T2 = datetime(2026, 1, 1, 10, 30, 0, tzinfo=TZ_UTC)
T3 = datetime(2026, 1, 1, 10, 45, 0, tzinfo=TZ_UTC)
T4 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=TZ_UTC)

NS_ID = UUID("00000000-0000-4000-8000-000000000001")
MEM_A = UUID("00000000-0000-4000-8000-0000000000a1")
MEM_B = UUID("00000000-0000-4000-8000-0000000000b1")


def _label_distance(label: str, query: str) -> float:
    """Deterministic pseudo-distance monotonic in label (for ordering)."""
    q = sum(ord(c) for c in query) % 251
    ell = sum(ord(c) for c in label) % 251
    return ((ell + 0.001 * q) % 100) / 100.0


@dataclass
class SeededEvent:
    event_seq: int
    occurred_at: datetime
    event_type: str
    memory_id: UUID
    entities: list[dict[str, str]]
    triplets: list[dict[str, Any]]
    namespace_id: UUID = NS_ID


def _active_store_rows(
    events: list[SeededEvent], namespace_id: UUID, as_of: datetime
) -> dict[UUID, SeededEvent]:
    """Mirror event_log CTE: last event per memory_id with occurred_at <= as_of."""
    filt = [
        e
        for e in events
        if e.namespace_id == namespace_id
        and e.occurred_at <= as_of
        and e.event_type in ("store_memory", "forget_memory")
    ]
    by_mem: dict[UUID, list[SeededEvent]] = defaultdict(list)
    for e in filt:
        by_mem[e.memory_id].append(e)
    active: dict[UUID, SeededEvent] = {}
    for mid, rows in by_mem.items():
        latest = max(rows, key=lambda r: r.event_seq)
        if latest.event_type == "store_memory":
            active[mid] = latest
    return active


class _Acquire:
    def __init__(self, conn: TemporalGraphFakeConn) -> None:
        self._c = conn

    async def __aenter__(self) -> TemporalGraphFakeConn:
        return self._c

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> None:
        return None


class FakePool:
    def __init__(self, conn: TemporalGraphFakeConn) -> None:
        self._conn = conn

    def acquire(self, *, timeout: float | None = None) -> _Acquire:
        return _Acquire(self._conn)


class TemporalGraphFakeConn:
    """
    Handles the three ``fetch`` shapes used when ``as_of`` and ``namespace_id``
    are set in ``GraphRAGTraverser``.
    """

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    def __init__(
        self,
        events: list[SeededEvent],
        memories_payload_ref: dict[UUID, str],
        last_query: list[str],
        *,
        embed_probe: dict[str, str],
    ) -> None:
        self._events = events
        self._payload_ref = memories_payload_ref
        self._last_query = last_query
        self._embed_probe = embed_probe

    async def execute(self, query: str, *args: Any) -> str:
        """No-op stub for set_namespace_context()."""
        return "SET 1"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self._last_query.append(query)
        q = query.lower()

        # ---- Extract as_of from args based on query shape ----
        # Anchor search: (vector_json, top_k, ns_id, as_of)
        # Recursive CTE time-travel BFS: (label, max_depth, ns_id, as_of, max_edges, max_nodes)
        # Batch edge fetch time-travel: (labels[], ns_id, as_of)
        # Node hydration time-travel: (labels[], ns_id, as_of)
        # Current-state BFS CTE: (label, max_depth, max_nodes)
        # Current-state batch edges: (labels[])
        if "historical_nodes" in q and "<=>" in q and len(args) >= 4:
            as_of = args[3]
        elif "recursive traversal" in q or "WITH RECURSIVE" in q or "with recursive" in q:
            # Recursive CTE — param order depends on time-travel vs current-state
            if len(args) >= 4 and isinstance(args[3], datetime):
                as_of = args[3]  # time-travel: (label, depth, ns_id, as_of, ...)
            elif self._events:
                as_of = self._events[-1].occurred_at  # current-state: (label, depth, max_nodes)
            else:
                as_of = T2
        elif "historical_edges" in q and any("$3" in query for _ in [1]):
            # Time-travel batch edge fetch: (labels[], ns_id, as_of)
            as_of = args[-1]
        elif "historical_nodes" in q and "any($1::text[])" in q.lower():
            # Node hydration in time-travel
            as_of = args[-1]
        else:
            as_of = args[-1] if args else T2

        assert isinstance(as_of, datetime), f"as_of must be datetime, got {type(as_of)}: {as_of}"

        active = _active_store_rows(self._events, NS_ID, as_of)

        # Anchor search (entities + vector distance)
        if "historical_nodes" in q and "<=>" in q:
            vector_json, top_k, ns_arg, _cutoff = args
            assert UUID(str(ns_arg)) == NS_ID
            _ = vector_json
            # Build distinct labels (stable: sort by label)
            label_to_row: dict[str, tuple[str, UUID]] = {}
            for ev in sorted(active.values(), key=lambda e: (e.memory_id, e.event_seq)):
                for ent in ev.entities:
                    lab = ent["label"]
                    if lab not in label_to_row:
                        label_to_row[lab] = (ent["entity_type"], ev.memory_id)

            probe = self._embed_probe.get("q", "")
            scored: list[tuple[float, str, str, str, UUID]] = []
            for lab, (etype, mid) in sorted(label_to_row.items()):
                dist = _label_distance(lab, probe)
                pref = self._payload_ref.get(mid, "")
                scored.append((dist, lab, etype, pref, mid))
            scored.sort(key=lambda t: t[0])
            rows: list[dict[str, Any]] = []
            for dist, lab, etype, pref, _mid in scored[: int(top_k)]:
                rows.append(
                    {
                        "label": lab,
                        "entity_type": etype,
                        "payload_ref": pref,
                        "distance": dist,
                    }
                )
            return rows

        # --- Recursive CTE label discovery (new batch BFS) ---
        # Detected by "recursive traversal" in query + only requesting labels.
        if "recursive traversal" in q or ("with recursive" in q and "traversal" in q):
            # Return all unique labels from active events (simulates BFS reachability)
            start_label = str(args[0])
            seen: set[str] = set()
            rows = []
            for ev in active.values():
                for tr in ev.triplets:
                    if tr["subject_label"] == start_label or tr["object_label"] == start_label:
                        for lab in (tr["subject_label"], tr["object_label"]):
                            if lab not in seen:
                                seen.add(lab)
                                rows.append({"label": lab, "depth": 0})
            if not rows:
                # At minimum return the start label
                rows.append({"label": start_label, "depth": 0})
            return rows

        # BFS edge scan (supports both old per-hop and new batch query shapes)
        if "params->'triplets'" in q and "historical_edges" in q:
            if "any($1::text[])" in q.lower():
                # New: batch edge fetch — args[0] is list of visited labels
                label_list, *_rest = args
                rows = []
                for ev in active.values():
                    for tr in ev.triplets:
                        subj = tr["subject_label"]
                        obj = tr["object_label"]
                        if subj not in label_list and obj not in label_list:
                            continue
                        mid = ev.memory_id
                        rows.append(
                            {
                                "subject_label": subj,
                                "predicate": tr["predicate"],
                                "object_label": obj,
                                "payload_ref": self._payload_ref.get(mid, ""),
                                "decayed_confidence": float(tr.get("confidence", 1.0)),
                            }
                        )
                return rows
            else:
                # Legacy: per-hop fetch — one label at a time
                current_label, ns_arg, _cutoff = args
                assert UUID(str(ns_arg)) == NS_ID
                rows = []
                for ev in active.values():
                    for tr in ev.triplets:
                        subj = tr["subject_label"]
                        obj = tr["object_label"]
                        if subj != current_label and obj != current_label:
                            continue
                        mid = ev.memory_id
                        rows.append(
                            {
                                "subject_label": subj,
                                "predicate": tr["predicate"],
                                "object_label": obj,
                                "payload_ref": self._payload_ref.get(mid, ""),
                                "decayed_confidence": float(tr.get("confidence", 1.0)),
                            }
                        )
                return rows

        # Visited node hydration
        if "params->'entities'" in q and "any($1::text[])" in q.lower():
            labels_any, ns_arg, _cutoff = args
            assert UUID(str(ns_arg)) == NS_ID
            want = set(labels_any)
            seen_label: dict[str, tuple[str, UUID]] = {}
            for ev in sorted(active.values(), key=lambda e: (e.memory_id, e.event_seq)):
                for ent in ev.entities:
                    lab = ent["label"]
                    if lab in want and lab not in seen_label:
                        seen_label[lab] = (ent["entity_type"], ev.memory_id)

            out = []
            for lab in sorted(seen_label.keys()):
                etype, mid = seen_label[lab]
                out.append(
                    {
                        "label": lab,
                        "entity_type": etype,
                        "payload_ref": self._payload_ref.get(mid, ""),
                    }
                )
            return out

        raise AssertionError(f"Unexpected temporal fetch query: {query!r} args={args!r}")


async def _noop_hydrate(*_a: object, **_k: object) -> list:
    return []


@pytest.fixture
def time_travel_traverser(monkeypatch: pytest.MonkeyPatch):
    events: list[SeededEvent] = [
        SeededEvent(
            1,
            T1,
            "store_memory",
            MEM_A,
            entities=[
                {"label": "Alpha", "entity_type": "CONCEPT"},
                {"label": "Beta", "entity_type": "CONCEPT"},
            ],
            triplets=[
                {
                    "subject_label": "Alpha",
                    "predicate": "relates_to",
                    "object_label": "Beta",
                    "confidence": 0.95,
                }
            ],
        ),
        SeededEvent(
            2,
            T3,
            "store_memory",
            MEM_A,
            entities=[
                {"label": "Gamma", "entity_type": "CONCEPT"},
                {"label": "Delta", "entity_type": "CONCEPT"},
            ],
            triplets=[
                {
                    "subject_label": "Gamma",
                    "predicate": "mentions",
                    "object_label": "Delta",
                    "confidence": 0.8,
                }
            ],
        ),
    ]
    payloads = {MEM_A: "payload_mem_a", MEM_B: "payload_mem_b"}

    last_sql: list[str] = []
    probe_holder: dict[str, str] = {"q": ""}
    conn = TemporalGraphFakeConn(events, payloads, last_sql, embed_probe=probe_holder)
    pool = FakePool(conn)

    async def fake_embed(text: str) -> list[float]:
        probe_holder["q"] = text
        return [0.0, 0.0, float(len(text) % 7)]

    t = GraphRAGTraverser(pg_pool=pool, mongo_client=MagicClient(), embedding_fn=fake_embed)

    monkeypatch.setattr(t, "_hydrate_sources", _noop_hydrate)
    return t, conn, events


class MagicClient:
    """Minimal motor stand-in (never hit)."""

    memory_archive = None


@pytest.mark.asyncio
async def test_as_of_before_update_sees_original_graph(
    time_travel_traverser: tuple,
) -> None:
    traverser, _conn, _events = time_travel_traverser
    sg = await traverser.search(
        "timeline",
        namespace_id=str(NS_ID),
        max_depth=2,
        anchor_top_k=1,
        as_of=T2,
    )
    labels = {n.label for n in sg.nodes}
    assert labels == {"Alpha", "Beta"}
    preds = {(e.subject, e.predicate, e.obj) for e in sg.edges}
    assert preds == {("Alpha", "relates_to", "Beta")}
    assert sg.anchor in {"Alpha", "Beta"}


@pytest.mark.asyncio
async def test_as_of_after_update_sees_rewritten_memory(
    time_travel_traverser: tuple,
) -> None:
    traverser, _conn, _events = time_travel_traverser
    sg = await traverser.search(
        "timeline",
        namespace_id=str(NS_ID),
        max_depth=2,
        anchor_top_k=1,
        as_of=T4,
    )
    labels = {n.label for n in sg.nodes}
    assert "Alpha" not in labels and "Beta" not in labels
    assert "Gamma" in labels and "Delta" in labels


@pytest.mark.asyncio
async def test_pure_reconstruction_matches_known_historical_snapshots() -> None:
    """
    Cross-check: golden expected node/edge sets from the seed file directly,
    independent of traverser BFS walk (order-insensitive graph identity).
    """

    events: list[SeededEvent] = [
        SeededEvent(
            1,
            T1,
            "store_memory",
            MEM_A,
            [{"label": "Alpha", "entity_type": "CONCEPT"}],
            [
                {
                    "subject_label": "Alpha",
                    "predicate": "p",
                    "object_label": "Beta",
                    "confidence": 1.0,
                }
            ],
        ),
        SeededEvent(2, T2, "forget_memory", MEM_A, [], []),
        SeededEvent(
            3,
            T3,
            "store_memory",
            MEM_B,
            [{"label": "Zeta", "entity_type": "CONCEPT"}],
            [],
        ),
    ]

    snap_t1_5 = _active_store_rows(events, NS_ID, datetime(2026, 1, 1, 10, 20, 0, tzinfo=TZ_UTC))
    assert MEM_A in snap_t1_5 and MEM_B not in snap_t1_5

    snap_after_forget = _active_store_rows(events, NS_ID, T3)
    assert MEM_A not in snap_after_forget and MEM_B in snap_after_forget

    labs_t1_5 = {e["label"] for ev in snap_t1_5.values() for e in ev.entities}
    assert labs_t1_5 == {"Alpha"}

    labs_t4 = {e["label"] for ev in snap_after_forget.values() for e in ev.entities}
    assert labs_t4 == {"Zeta"}


@pytest.mark.asyncio
async def test_as_of_after_forget_yields_no_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the latest event for every memory is forget_memory, anchors are empty."""
    events: list[SeededEvent] = [
        SeededEvent(
            1,
            T1,
            "store_memory",
            MEM_A,
            [{"label": "X", "entity_type": "CONCEPT"}],
            [],
        ),
        SeededEvent(2, T3, "forget_memory", MEM_A, [], []),
    ]
    payloads = {MEM_A: "ref"}
    last_sql: list[str] = []
    probe_holder: dict[str, str] = {"q": ""}
    conn = TemporalGraphFakeConn(events, payloads, last_sql, embed_probe=probe_holder)
    pool = FakePool(conn)

    async def fake_embed(_text: str) -> list[float]:
        return [0.0]

    t = GraphRAGTraverser(pg_pool=pool, mongo_client=MagicClient(), embedding_fn=fake_embed)
    monkeypatch.setattr(t, "_hydrate_sources", _noop_hydrate)

    sg = await t.search(
        "q",
        namespace_id=str(NS_ID),
        max_depth=2,
        anchor_top_k=1,
        as_of=T4,
    )
    assert sg.anchor == "<none>"
    assert sg.nodes == []
    assert sg.edges == []


# --- parse_as_of (nce.temporal — MCP / REST boundary) ---


@pytest.fixture
def _patch_temporal_wall_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Pinned wall clock so 'future' timestamps are controllable."""
    import nce.temporal as temporal_mod

    fixed = datetime(2026, 5, 5, 12, 0, 0, tzinfo=TZ_UTC)

    class _DT(datetime):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None or tz == TZ_UTC:
                return fixed
            return fixed.astimezone(tz)

    monkeypatch.setattr(temporal_mod, "datetime", _DT)
    return fixed


def test_parse_as_of_none_returns_none() -> None:
    from nce.temporal import parse_as_of

    assert parse_as_of(None) is None


def test_parse_as_of_accepts_naive_iso_as_utc(
    _patch_temporal_wall_clock: datetime,
) -> None:
    from nce.temporal import parse_as_of

    dt = parse_as_of("2026-03-01T10:15:30")
    assert dt == datetime(2026, 3, 1, 10, 15, 30, tzinfo=TZ_UTC)


def test_parse_as_of_accepts_z_suffix(_patch_temporal_wall_clock: datetime) -> None:
    from nce.temporal import parse_as_of

    dt = parse_as_of("2026-03-01T10:15:30Z")
    assert dt.tzinfo is not None


def test_parse_as_of_rejects_future(_patch_temporal_wall_clock: datetime) -> None:
    from nce.temporal import parse_as_of

    with pytest.raises(ValueError, match="future"):
        parse_as_of("2026-06-01T00:00:00Z")


def test_parse_as_of_rejects_bad_format(_patch_temporal_wall_clock: datetime) -> None:
    from nce.temporal import parse_as_of

    with pytest.raises(ValueError, match="ISO 8601"):
        parse_as_of("not-a-timestamp")


# --- Temporal lookback boundary tests ---


@pytest.fixture
def _patch_lookback_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the default 90-day lookback to a narrow 30-day window."""
    import nce.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod.cfg,
        "NCE_MAX_TEMPORAL_LOOKBACK_DAYS",
        30,
    )


@pytest.fixture
def _disable_lookback_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set lookback to 0 (unlimited) to test the boundary-gate bypass."""
    import nce.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod.cfg,
        "NCE_MAX_TEMPORAL_LOOKBACK_DAYS",
        0,
    )


def test_enforce_lookback_boundary_accepts_recent_timestamp(
    _patch_temporal_wall_clock: datetime,
    _patch_lookback_config: None,
) -> None:
    """A timestamp within the 30-day window must be accepted."""
    from nce.temporal import parse_as_of

    # Wall clock is pinned to 2026-05-05T12:00:00Z, 30-day window → earliest 2026-04-05T12:00:00Z.
    # 2026-04-10 is within the window.
    dt = parse_as_of("2026-04-10T00:00:00Z")
    assert dt == datetime(2026, 4, 10, 0, 0, 0, tzinfo=TZ_UTC)


def test_enforce_lookback_boundary_rejects_old_timestamp(
    _patch_temporal_wall_clock: datetime,
    _patch_lookback_config: None,
) -> None:
    """A timestamp older than the 30-day window must be rejected."""
    from nce.temporal import parse_as_of

    # 2026-03-01 is 65 days before the pinned wall clock (2026-05-05) → exceeds 30-day limit.
    with pytest.raises(ValueError, match="outside the allowed lookback window"):
        parse_as_of("2026-03-01T00:00:00Z")


def test_enforce_lookback_boundary_exact_cutoff_allowed(
    _patch_temporal_wall_clock: datetime,
    _patch_lookback_config: None,
) -> None:
    """A timestamp exactly at the cutoff (now - 30 days) must be accepted."""
    from nce.temporal import parse_as_of

    # 2026-05-05 minus 30 days = 2026-04-05T12:00:00Z
    dt = parse_as_of("2026-04-05T12:00:00Z")
    assert dt == datetime(2026, 4, 5, 12, 0, 0, tzinfo=TZ_UTC)


def test_enforce_lookback_boundary_one_second_before_cutoff_rejected(
    _patch_temporal_wall_clock: datetime,
    _patch_lookback_config: None,
) -> None:
    """A timestamp one second before the cutoff must be rejected (boundary precision)."""
    from nce.temporal import parse_as_of

    # 2026-04-05T11:59:59Z is 1 second before cutoff → should be rejected.
    with pytest.raises(ValueError, match="outside the allowed lookback window"):
        parse_as_of("2026-04-05T11:59:59Z")


def test_enforce_lookback_boundary_disabled_with_zero(
    _patch_temporal_wall_clock: datetime,
    _disable_lookback_config: None,
) -> None:
    """Setting NCE_MAX_TEMPORAL_LOOKBACK_DAYS=0 must disable the boundary."""
    from nce.temporal import parse_as_of

    # 2026-03-01 is well beyond the default 90-day window but with 0 (unlimited) it must pass.
    dt = parse_as_of("2026-03-01T00:00:00Z")
    assert dt == datetime(2026, 3, 1, 0, 0, 0, tzinfo=TZ_UTC)


def test_enforce_lookback_boundary_default_90_days(
    _patch_temporal_wall_clock: datetime,
) -> None:
    """With default config (90-day lookback), a timestamp 89 days ago must be accepted."""
    from nce.temporal import parse_as_of

    # 2026-05-05 minus 89 days = 2026-02-05.  2026-02-06 is within 90 days.
    dt = parse_as_of("2026-02-06T00:00:00Z")
    assert dt == datetime(2026, 2, 6, 0, 0, 0, tzinfo=TZ_UTC)


def test_enforce_lookback_boundary_default_rejects_excessive(
    _patch_temporal_wall_clock: datetime,
) -> None:
    """With default config (90-day lookback), a timestamp 100 days ago must be rejected."""
    from nce.temporal import parse_as_of

    # 2026-01-26 is 99 days before pinned clock (2026-05-05) → exceeds 90-day limit.
    with pytest.raises(ValueError, match="outside the allowed lookback window"):
        parse_as_of("2026-01-26T00:00:00Z")
