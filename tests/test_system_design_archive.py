"""
tests/test_system_design_archive.py
====================================
Module 6 Wave 17b (B067h2) — the retired-archive sweep.

The filename matches the ``tests/test_system_design_*.py`` CI glob B067a wired
into ``.github/workflows/ci.yml``, so this file runs in CI with no workflow
edit.  Any other filename would need a ``ci.yml`` change, which is a scope
change.

WHAT THESE TESTS GATE
----------------------
1. **THE ORDERING.**  ``serialise -> store -> VERIFY READABLE BACK -> drop``.
   Two independent tests, because the two failure modes are different:

   * :func:`test_the_order_of_the_four_steps_is_pinned` observes the actual
     call sequence (a spy on the drop, a recording MinIO client) and pins it
     positionally.  Moving the drop above the read-back reorders that list.
   * :class:`TestADropNeverTrustsAnUnverifiedWrite` makes the read-back *fail*
     — first by returning different bytes, then by raising — and asserts the
     rows are all still there.  A drop that had already run cannot pass this,
     and unlike the positional test it stays meaningful even if the
     implementation stops calling ``_delete_permanently`` by that name.

   Each of those has a positive control: an honest client in the same class
   drops the node, so "nothing was deleted" is never merely "the fixture never
   had anything to delete".

2. **The flag defaults OFF.**  Both halves: :func:`sweep_enabled` is False with
   the variable unset, and a disabled sweep does not so much as acquire a
   connection.  The positive control turns it on and shows the same call now
   reaches the pool.

3. **NULL / absent status is not a candidate.**  The unit gate
   (:func:`_is_candidate`) and the DB gate (the query predicate) are tested
   separately, because they are two independent doors and a single test over
   "an undeclared node survives" would pass with either one removed.

4. **Interrupt-idempotence.**  A run interrupted between the store and the drop,
   then re-run, leaves exactly ONE object.  The negative assertion ("no second
   copy") carries a positive control that proves the counting helper can see a
   second copy when one genuinely exists.

5. **Tenant isolation (§6.4).**  ``nce_app`` serves no request here; every
   statement runs on an owner pool that ``FORCE ROW LEVEL SECURITY`` does not
   constrain, so the explicit ``namespace_id`` predicate is the boundary.  The
   two tenants below collide on **every identifier** — same design id, same
   device ref, therefore byte-identical node labels — and differ **only in
   content**.  A fixture that gave them different labels could not detect a
   predicate that filters by label.

6. **No new ``EventType``**, and the archive key is durably recorded.

The per-predicate mutation table is in the wave report.  Every row was produced
by mutating a single predicate in a scratchpad COPY of the tree — never in the
tree itself — and asserting the edit landed before running.

All DB-dependent tests are ``@pytest.mark.integration``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from nce.event_types import VALID_EVENT_TYPES
from nce.vertical_modules.system_design import archive as archive_mod
from nce.vertical_modules.system_design.archive import (
    ARCHIVE_EVENT_TYPE,
    ARCHIVE_OP,
    ARCHIVE_PREFIX,
    DEFAULT_RETENTION_DAYS,
    SWEEP_ENABLED_ENV,
    SWEEP_STATUS,
    ArchiveNotReadableError,
    archive_key,
    run_design_archive_sweep,
    sweep_enabled,
)
from nce.vertical_modules.system_design.devices import device_label

_BUCKET = "test-b067h2-archive"

# ---------------------------------------------------------------------------
# Fixture data.  EVERY identifier is shared by both tenants; only content
# differs.  See point 5 of the module docstring for why that is not stylistic.
# ---------------------------------------------------------------------------

_DESIGN_ID = "DESIGN-W17B-ARCHIVE-001"
_RETIRED_REF = "SW-RETIRED"
_UNDECLARED_REF = "SW-UNDECLARED"
_FRESH_REF = "SW-FRESH"

_RETIRED_LABEL = device_label(_DESIGN_ID, _RETIRED_REF)
_UNDECLARED_LABEL = device_label(_DESIGN_ID, _UNDECLARED_REF)
_FRESH_LABEL = device_label(_DESIGN_ID, _FRESH_REF)

#: Per-tenant CONTENT.  None of these is an identifier, so a namespace
#: predicate that went missing shows up as the wrong VALUE under the right key.
_TENANT_CONTENT: dict[str, dict[str, Any]] = {
    "ALPHA": {"revision": "ALPHA-REV-7", "manufacturer": "ALPHA-MAKER"},
    "BETA": {"revision": "BETA-REV-91", "manufacturer": "BETA-MAKER"},
}


# ---------------------------------------------------------------------------
# MinIO doubles.
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, blob: bytes) -> None:
        self._blob = blob
        self.closed = False

    def read(self) -> bytes:
        return self._blob

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        return None


class _FakeMinio:
    """An honest object store: what you put is what you get."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str]] = []

    def put_object(
        self,
        bucket: str,
        key: str,
        data: Any,
        length: int,
        content_type: str | None = None,
    ) -> None:
        self.calls.append(("put", key))
        self.objects[(bucket, key)] = data.read()

    def get_object(self, bucket: str, key: str) -> _Response:
        self.calls.append(("get", key))
        return _Response(self._fetch(bucket, key))

    def _fetch(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]

    def keys_under(self, prefix: str) -> list[str]:
        """Count of stored objects whose key starts with *prefix*.

        Used for the "exactly one copy" assertion.  Its own positive control is
        :func:`test_the_copy_counter_can_see_a_second_copy`.
        """
        return sorted(key for (_bucket, key) in self.objects if key.startswith(prefix))


class _CorruptingMinio(_FakeMinio):
    """Accepts the PUT, returns different bytes on the GET.

    This is the silent-corruption case: nothing raises anywhere, and the only
    thing standing between it and a permanent data loss is the byte comparison
    in ``_read_back``.
    """

    def _fetch(self, bucket: str, key: str) -> bytes:
        return b'{"archive_version": 0}'


class _VanishingMinio(_FakeMinio):
    """Accepts the PUT; the object is not there afterwards (the GET raises)."""

    def _fetch(self, bucket: str, key: str) -> bytes:
        raise RuntimeError("NoSuchKey: the object is not in the bucket")


class _CrashesOnceAfterPut(_FakeMinio):
    """Stores the object, then fails the FIRST read-back only.

    Models an interruption in the window between "stored" and "dropped": the
    object exists, the transaction never ran.  The second run must heal that
    without leaving a second copy behind.
    """

    def __init__(self) -> None:
        super().__init__()
        self.read_backs = 0

    def _fetch(self, bucket: str, key: str) -> bytes:
        self.read_backs += 1
        if self.read_backs == 1:
            raise RuntimeError("interrupted before the drop")
        return self.objects[(bucket, key)]


class _BoomPool:
    """A pool that refuses to be used.  Proves the disabled sweep touches nothing."""

    def acquire(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the sweep acquired a connection when it should not have")


# ---------------------------------------------------------------------------
# Seeding helpers.
# ---------------------------------------------------------------------------


async def _seed_node(
    pg_pool: Any,
    ns_id: uuid.UUID,
    label: str,
    *,
    tag: str,
    status: str | None,
    age_days: int,
) -> None:
    """One DEVICE with a state row, a geometry row and a capability row.

    Seeded by SQL rather than through the authoring tool on purpose: the sweep
    reads these tables directly, and a fixture that went through the tool would
    make these tests depend on the authoring surface's own guards.
    """
    from nce.db_utils import scoped_pg_session

    content = _TENANT_CONTENT[tag]
    ns = str(ns_id)
    stamp = datetime.now(timezone.utc) - timedelta(days=age_days)
    async with scoped_pg_session(pg_pool, ns_id) as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO kg_nodes (label, entity_type, namespace_id, change_origin)
                VALUES ($1, 'DEVICE', $2::uuid, 'operator')
                ON CONFLICT (label, namespace_id) DO NOTHING
                """,
                label,
                ns,
            )
            await conn.execute(
                """
                INSERT INTO system_design_node_state
                       (namespace_id, node_label, node_type, status, revision, updated_at)
                VALUES ($1::uuid, $2, 'DEVICE', $3, $4, $5)
                ON CONFLICT (namespace_id, node_label) DO UPDATE
                   SET status = EXCLUDED.status,
                       revision = EXCLUDED.revision,
                       updated_at = EXCLUDED.updated_at
                """,
                ns,
                label,
                status,
                content["revision"],
                stamp,
            )
            await conn.execute(
                """
                INSERT INTO system_design_geometry (namespace_id, node_label, x, y)
                VALUES ($1::uuid, $2, 10, 20)
                ON CONFLICT (namespace_id, node_label) DO NOTHING
                """,
                ns,
                label,
            )
            await conn.execute(
                """
                INSERT INTO system_design_device_capabilities
                       (namespace_id, node_label, manufacturer)
                VALUES ($1::uuid, $2, $3)
                ON CONFLICT (namespace_id, node_label) DO NOTHING
                """,
                ns,
                label,
                content["manufacturer"],
            )


async def _surviving(pg_pool: Any, ns_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """``{node_label: {...}}`` — the TEST's own read of what is left.

    The namespace predicate here is the test's own; the predicates under test
    are the ones inside ``archive.py``.
    """
    from nce.db_utils import scoped_pg_session

    async with scoped_pg_session(pg_pool, ns_id) as conn:
        rows = await conn.fetch(
            """
            SELECT s.node_label,
                   s.status,
                   s.revision,
                   (SELECT count(*) FROM kg_nodes n
                     WHERE n.namespace_id = s.namespace_id AND n.label = s.node_label) AS nodes,
                   (SELECT count(*) FROM system_design_geometry g
                     WHERE g.namespace_id = s.namespace_id
                       AND g.node_label = s.node_label) AS geometry,
                   (SELECT count(*) FROM system_design_device_capabilities c
                     WHERE c.namespace_id = s.namespace_id
                       AND c.node_label = s.node_label) AS capabilities
              FROM system_design_node_state s
             WHERE s.namespace_id = $1::uuid
            """,
            str(ns_id),
        )
    return {r["node_label"]: dict(r) for r in rows}


@pytest.fixture
def _sweep_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWEEP_ENABLED_ENV, "true")


# ---------------------------------------------------------------------------
# 2. The flag defaults OFF.
# ---------------------------------------------------------------------------


class TestTheFlagDefaultsOff:
    def test_sweep_enabled_is_false_when_the_variable_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SWEEP_ENABLED_ENV, raising=False)
        assert sweep_enabled() is False

    @pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "maybe"])
    def test_sweep_enabled_is_false_for_anything_that_is_not_truthy(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(SWEEP_ENABLED_ENV, raw)
        assert sweep_enabled() is False

    @pytest.mark.asyncio
    async def test_a_disabled_sweep_does_not_even_acquire_a_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(SWEEP_ENABLED_ENV, raising=False)
        minio = _FakeMinio()
        result = await run_design_archive_sweep(_BoomPool(), minio, bucket=_BUCKET)  # type: ignore[arg-type]
        assert result["enabled"] is False
        assert result["archived"] == 0
        assert minio.calls == []

    @pytest.mark.asyncio
    async def test_positive_control_an_enabled_sweep_does_reach_the_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without this, the test above passes for a sweep that can never run at all."""
        monkeypatch.setenv(SWEEP_ENABLED_ENV, "true")
        with pytest.raises(AssertionError, match="acquired a connection"):
            await run_design_archive_sweep(_BoomPool(), _FakeMinio(), bucket=_BUCKET)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3a. The unit gate on an undeclared lifecycle.
# ---------------------------------------------------------------------------


class TestAbsenceIsNotACandidate:
    def test_null_status_is_refused(self) -> None:
        assert archive_mod._is_candidate(None) is False

    @pytest.mark.parametrize("status", ["planned", "active", "offline", "inventory", ""])
    def test_any_other_status_is_refused(self, status: str) -> None:
        assert archive_mod._is_candidate(status) is False

    def test_positive_control_the_retired_status_is_accepted(self) -> None:
        assert archive_mod._is_candidate(SWEEP_STATUS) is True


# ---------------------------------------------------------------------------
# 6. No new EventType; deterministic key.
# ---------------------------------------------------------------------------


def test_the_sweep_adds_no_new_event_type() -> None:
    """``event_types.py`` and ``replay.py`` are a pair; this wave touches neither."""
    assert ARCHIVE_EVENT_TYPE in VALID_EVENT_TYPES
    assert ARCHIVE_EVENT_TYPE == "system_design_authored"


def test_the_archive_key_is_a_pure_function_of_tenant_and_label() -> None:
    """Interrupt-idempotence rests on this: no clock, no uuid, no counter."""
    ns = uuid.uuid4()
    first = archive_key(ns, _RETIRED_LABEL)
    second = archive_key(ns, _RETIRED_LABEL)
    assert first == second
    assert first.startswith(f"{ARCHIVE_PREFIX}/{ns}/")
    assert archive_key(uuid.uuid4(), _RETIRED_LABEL) != first


def test_the_default_retention_window_is_not_zero() -> None:
    """A zero window would make every retired node eligible the instant it is retired."""
    assert DEFAULT_RETENTION_DAYS >= 30
    cutoff = archive_mod.retention_cutoff(datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert cutoff < datetime(2026, 5, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. THE ORDERING — the whole wave.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestADropNeverTrustsAnUnverifiedWrite:
    """The read-back fails; the rows must survive."""

    @pytest.mark.asyncio
    async def test_bytes_that_come_back_different_block_the_drop(
        self, pg_pool: Any, namespace_id: uuid.UUID, _sweep_on: None
    ) -> None:
        await _seed_node(
            pg_pool, namespace_id, _RETIRED_LABEL, tag="ALPHA", status=SWEEP_STATUS, age_days=400
        )
        minio = _CorruptingMinio()
        result = await run_design_archive_sweep(
            pg_pool, minio, namespaces=[namespace_id], bucket=_BUCKET
        )

        assert result["archived"] == 0
        assert result["skipped_unreadable"] == 1
        left = await _surviving(pg_pool, namespace_id)
        assert _RETIRED_LABEL in left
        assert left[_RETIRED_LABEL]["nodes"] == 1
        assert left[_RETIRED_LABEL]["geometry"] == 1
        assert left[_RETIRED_LABEL]["capabilities"] == 1

    @pytest.mark.asyncio
    async def test_a_read_back_that_raises_blocks_the_drop(
        self, pg_pool: Any, namespace_id: uuid.UUID, _sweep_on: None
    ) -> None:
        await _seed_node(
            pg_pool, namespace_id, _RETIRED_LABEL, tag="ALPHA", status=SWEEP_STATUS, age_days=400
        )
        minio = _VanishingMinio()
        result = await run_design_archive_sweep(
            pg_pool, minio, namespaces=[namespace_id], bucket=_BUCKET
        )

        assert result["archived"] == 0
        assert result["skipped_unreadable"] == 1
        assert _RETIRED_LABEL in await _surviving(pg_pool, namespace_id)

    @pytest.mark.asyncio
    async def test_positive_control_an_honest_store_does_drop_the_node(
        self, pg_pool: Any, namespace_id: uuid.UUID, _sweep_on: None
    ) -> None:
        """Otherwise the two tests above pass for a sweep that never drops anything."""
        await _seed_node(
            pg_pool, namespace_id, _RETIRED_LABEL, tag="ALPHA", status=SWEEP_STATUS, age_days=400
        )
        minio = _FakeMinio()
        result = await run_design_archive_sweep(
            pg_pool, minio, namespaces=[namespace_id], bucket=_BUCKET
        )

        assert result["archived"] == 1
        assert _RETIRED_LABEL not in await _surviving(pg_pool, namespace_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_order_of_the_four_steps_is_pinned(
    pg_pool: Any, namespace_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch, _sweep_on: None
) -> None:
    """serialise -> store -> read back -> drop, observed rather than assumed.

    The drop is spied on where ``archive.py`` calls it, so the recorded list is
    the real sequence.  Moving the drop above the read-back — the defect this
    wave exists to prevent — reorders it and this fails.
    """
    await _seed_node(
        pg_pool, namespace_id, _RETIRED_LABEL, tag="ALPHA", status=SWEEP_STATUS, age_days=400
    )
    minio = _FakeMinio()
    real_delete = archive_mod._delete_permanently

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        minio.calls.append(("drop", _RETIRED_LABEL))
        return await real_delete(*args, **kwargs)

    monkeypatch.setattr(archive_mod, "_delete_permanently", _spy)

    result = await run_design_archive_sweep(
        pg_pool, minio, namespaces=[namespace_id], bucket=_BUCKET
    )

    assert result["archived"] == 1
    key = archive_key(namespace_id, _RETIRED_LABEL)
    assert minio.calls == [("put", key), ("get", key), ("drop", _RETIRED_LABEL)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_archive_key_reaches_event_log_and_the_object_holds_the_rows(
    pg_pool: Any, namespace_id: uuid.UUID, _sweep_on: None
) -> None:
    """ "Recoverable by reference" is two facts: the key is durable, the bytes are complete."""
    from nce.db_utils import scoped_pg_session

    await _seed_node(
        pg_pool, namespace_id, _RETIRED_LABEL, tag="ALPHA", status=SWEEP_STATUS, age_days=400
    )
    minio = _FakeMinio()
    await run_design_archive_sweep(pg_pool, minio, namespaces=[namespace_id], bucket=_BUCKET)

    key = archive_key(namespace_id, _RETIRED_LABEL)
    async with scoped_pg_session(pg_pool, namespace_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT params FROM event_log
             WHERE namespace_id = $1::uuid
               AND event_type   = $2
               AND params->>'op' = $3
            """,
            str(namespace_id),
            ARCHIVE_EVENT_TYPE,
            ARCHIVE_OP,
        )
    assert row is not None, "no archive record in event_log — the object has no name"
    import json

    params = row["params"]
    params = json.loads(params) if isinstance(params, str) else params
    assert params["archive_key"] == key

    blob = minio.objects[(_BUCKET, key)]
    document = json.loads(blob.decode("utf-8"))
    assert [r["node_label"] for r in document["system_design_node_state"]] == [_RETIRED_LABEL]
    assert [r["label"] for r in document["kg_nodes"]] == [_RETIRED_LABEL]
    assert document["system_design_geometry"], "geometry was dropped but not archived"
    assert document["system_design_device_capabilities"], "capability was dropped but not archived"


# ---------------------------------------------------------------------------
# 3b. The DB gate on an undeclared lifecycle, and the window.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_undeclared_node_is_never_swept_and_a_retired_one_is(
    pg_pool: Any, namespace_id: uuid.UUID, _sweep_on: None
) -> None:
    """NULL status is not a candidate — with its positive control in the same run.

    Both nodes are equally old and sit in the same sweep, so "the undeclared one
    survived" cannot be explained by the sweep having done nothing.
    """
    await _seed_node(
        pg_pool, namespace_id, _UNDECLARED_LABEL, tag="ALPHA", status=None, age_days=400
    )
    await _seed_node(
        pg_pool, namespace_id, _RETIRED_LABEL, tag="ALPHA", status=SWEEP_STATUS, age_days=400
    )

    result = await run_design_archive_sweep(
        pg_pool, _FakeMinio(), namespaces=[namespace_id], bucket=_BUCKET
    )
    left = await _surviving(pg_pool, namespace_id)

    assert result["archived"] == 1
    assert _UNDECLARED_LABEL in left
    assert left[_UNDECLARED_LABEL]["status"] is None
    assert _RETIRED_LABEL not in left
    # Gate ONE, on its own.  Without this the query predicate is not gated at
    # all: widen it to ``OR status IS NULL`` and the undeclared node still
    # survives — because ``_is_candidate`` catches it — and every assertion
    # above still passes.  The counter is the only thing that can tell "the
    # query never returned it" from "the query returned it and the second gate
    # threw it away", and only the first of those is the contract.
    assert result["skipped_not_candidate"] == 0, (
        "the query returned a row that is not a candidate — the SQL gate is open"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_node_inside_the_retention_window_is_kept(
    pg_pool: Any, namespace_id: uuid.UUID, _sweep_on: None
) -> None:
    await _seed_node(
        pg_pool, namespace_id, _FRESH_LABEL, tag="ALPHA", status=SWEEP_STATUS, age_days=0
    )
    await _seed_node(
        pg_pool, namespace_id, _RETIRED_LABEL, tag="ALPHA", status=SWEEP_STATUS, age_days=400
    )

    result = await run_design_archive_sweep(
        pg_pool, _FakeMinio(), namespaces=[namespace_id], bucket=_BUCKET
    )
    left = await _surviving(pg_pool, namespace_id)

    assert result["archived"] == 1
    assert _FRESH_LABEL in left
    assert _RETIRED_LABEL not in left


# ---------------------------------------------------------------------------
# 4. Interrupt-idempotence.
# ---------------------------------------------------------------------------


def test_the_copy_counter_can_see_a_second_copy() -> None:
    """Positive control for the negative assertion below."""
    minio = _FakeMinio()
    prefix = f"{ARCHIVE_PREFIX}/"
    minio.objects[(_BUCKET, prefix + "a.json")] = b"{}"
    assert len(minio.keys_under(prefix)) == 1
    minio.objects[(_BUCKET, prefix + "b.json")] = b"{}"
    assert len(minio.keys_under(prefix)) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_run_interrupted_before_the_drop_leaves_one_copy_after_the_rerun(
    pg_pool: Any, namespace_id: uuid.UUID, _sweep_on: None
) -> None:
    """Run, fail between store and drop, re-run.  Exactly one object, and it is dropped."""
    await _seed_node(
        pg_pool, namespace_id, _RETIRED_LABEL, tag="ALPHA", status=SWEEP_STATUS, age_days=400
    )
    minio = _CrashesOnceAfterPut()

    first = await run_design_archive_sweep(
        pg_pool, minio, namespaces=[namespace_id], bucket=_BUCKET
    )
    assert first["archived"] == 0 and first["skipped_unreadable"] == 1
    assert _RETIRED_LABEL in await _surviving(pg_pool, namespace_id)
    assert len(minio.keys_under(f"{ARCHIVE_PREFIX}/")) == 1

    second = await run_design_archive_sweep(
        pg_pool, minio, namespaces=[namespace_id], bucket=_BUCKET
    )
    assert second["archived"] == 1
    assert _RETIRED_LABEL not in await _surviving(pg_pool, namespace_id)
    assert minio.keys_under(f"{ARCHIVE_PREFIX}/") == [archive_key(namespace_id, _RETIRED_LABEL)]


# ---------------------------------------------------------------------------
# 5. Tenant isolation.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_sweep_does_not_cross_the_tenant_boundary(
    pg_pool: Any, make_namespace: Any, _sweep_on: None
) -> None:
    """Byte-identical labels in two tenants; only one tenant is swept.

    Owner pools bypass FORCE RLS, so the explicit ``namespace_id`` predicate is
    what is under test.  BETA's node is equally old and equally retired — the
    ONLY thing keeping it is that the sweep was not asked for that namespace.
    """
    alpha = await make_namespace()
    example = await make_namespace()

    for ns, tag in ((alpha, "ALPHA"), (example, "BETA")):
        await _seed_node(pg_pool, ns, _RETIRED_LABEL, tag=tag, status=SWEEP_STATUS, age_days=400)

    minio = _FakeMinio()
    result = await run_design_archive_sweep(pg_pool, minio, namespaces=[alpha], bucket=_BUCKET)

    assert result["archived"] == 1
    assert _RETIRED_LABEL not in await _surviving(pg_pool, alpha)

    left = await _surviving(pg_pool, example)
    assert _RETIRED_LABEL in left
    # Content, not identity: a predicate that filtered by label would have taken
    # this row too and the label assertion alone could not tell.
    assert left[_RETIRED_LABEL]["revision"] == _TENANT_CONTENT["BETA"]["revision"]
    assert left[_RETIRED_LABEL]["nodes"] == 1

    assert minio.keys_under(f"{ARCHIVE_PREFIX}/") == [archive_key(alpha, _RETIRED_LABEL)]


# ---------------------------------------------------------------------------
# The error type is part of the contract: callers distinguish "could not verify"
# from "the node genuinely failed".
# ---------------------------------------------------------------------------


def test_archive_not_readable_is_its_own_error_type() -> None:
    assert issubclass(ArchiveNotReadableError, RuntimeError)
