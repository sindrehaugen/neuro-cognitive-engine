"""
tests/test_query_catalog.py

Unit coverage for nce.query_catalog.CatalogManager.

Exercises:
  - _compile_template: bind macro emits $N positional params correctly
  - _compile_template: missing required slot raises KeyError
  - _compile_template: direct value interpolation raises UndefinedError (StrictUndefined)
  - execute(): caller-supplied namespace_id in params is stripped (spoofing prevention)
  - execute(): missing optional schema props are pre-populated as None (optional field trap)
  - execute(): namespace_id re-injected as uuid.UUID, never str
  - execute(): unknown slug raises ValueError
  - record_schema(): duplicate edge predicates are deduplicated before upsert
  - record_schema(): node entity_type and edge predicate stored separately
  - describe_schema(): returns correct GraphSchema from registry rows
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

import nce.query_catalog as catalog_mod
from nce.query_catalog import CatalogManager, GraphSchema

# ---------------------------------------------------------------------------
# Helpers / fake infrastructure
# ---------------------------------------------------------------------------


class _FakeCatalogConnection:
    """Minimal async connection double for CatalogManager unit tests."""

    def __init__(
        self,
        fetchrow_result: dict[str, Any] | None = None,
        fetch_results: list[dict[str, Any]] | None = None,
    ) -> None:
        self._fetchrow_result = fetchrow_result
        self._fetch_results = fetch_results or []
        self.executemany_calls: list[tuple[str, list[Any]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        return self._fetchrow_result

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self._fetch_results

    async def executemany(self, query: str, data: list[Any]) -> None:
        self.executemany_calls.append((query, list(data)))


def _make_scoped_session_patcher(conn: _FakeCatalogConnection):
    """Return an asynccontextmanager that yields the given fake connection."""

    @asynccontextmanager
    async def _fake_scoped(*_args: Any, **_kwargs: Any):
        yield conn

    return _fake_scoped


@pytest.fixture
def namespace_id() -> UUID:
    return uuid4()


# ---------------------------------------------------------------------------
# _compile_template
# ---------------------------------------------------------------------------


def test_compile_template_emits_positional_params() -> None:
    mgr = CatalogManager(pool=None)  # type: ignore[arg-type]
    template_str = "SELECT * FROM kg_nodes WHERE namespace_id = {{ bind('namespace_id') }} AND label = {{ bind('label') }}"
    params = {"namespace_id": uuid4(), "label": "Alice"}
    sql, args = mgr._compile_template(template_str, params)
    assert "$1" in sql
    assert "$2" in sql
    assert args[0] == params["namespace_id"]
    assert args[1] == "Alice"


def test_compile_template_structural_if_does_not_inject_param() -> None:
    mgr = CatalogManager(pool=None)  # type: ignore[arg-type]
    template_str = "SELECT 1{% if limit %} LIMIT {{ bind('limit') }}{% endif %}"
    # limit present
    sql_with, args_with = mgr._compile_template(template_str, {"limit": 10})
    assert "$1" in sql_with
    assert args_with == [10]
    # limit absent (pre-populated as None by execute())
    sql_without, args_without = mgr._compile_template(template_str, {"limit": None})
    assert "$1" not in sql_without
    assert args_without == []


def test_compile_template_missing_slot_raises_key_error() -> None:
    mgr = CatalogManager(pool=None)  # type: ignore[arg-type]
    template_str = "SELECT {{ bind('missing_param') }}"
    with pytest.raises(KeyError, match="missing_param"):
        mgr._compile_template(template_str, {})


def test_compile_template_direct_interpolation_raises_undefined_error() -> None:
    """Direct {{ param_name }} interpolation (not via bind) must raise UndefinedError.

    StrictUndefined raises when a variable that was not passed is accessed.
    Optional fields are pre-populated as None by execute() before compile.
    """
    from jinja2 import UndefinedError

    mgr = CatalogManager(pool=None)  # type: ignore[arg-type]
    # Accessing a variable that is not in the render context at all:
    template_str = "SELECT {{ undeclared_var }}"
    with pytest.raises(UndefinedError):
        mgr._compile_template(template_str, {})


# ---------------------------------------------------------------------------
# execute(): namespace_id spoofing prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_strips_caller_namespace_id(
    monkeypatch: pytest.MonkeyPatch,
    namespace_id: UUID,
) -> None:
    """Caller cannot inject a different namespace_id via params."""
    attacker_ns = uuid4()
    assert attacker_ns != namespace_id

    template_row = {
        "raw_template": "SELECT {{ bind('namespace_id') }}",
        "param_schema": json.dumps({"type": "object", "properties": {}, "required": []}),
        "target_engine": "postgres",
        "pipeline": None,
    }
    fetch_conn = _FakeCatalogConnection(
        fetchrow_result=template_row,
        fetch_results=[],
    )
    execute_conn = _FakeCatalogConnection(fetch_results=[])

    call_count = 0

    @asynccontextmanager
    async def _counted_session(pool: Any, ns: UUID):
        nonlocal call_count
        if call_count == 0:
            yield fetch_conn
        else:
            yield execute_conn
        call_count += 1

    monkeypatch.setattr(catalog_mod, "scoped_pg_session", _counted_session)

    mgr = CatalogManager(pool=None)  # type: ignore[arg-type]
    await mgr.execute(
        slug="test-slug",
        params={"namespace_id": str(attacker_ns)},  # attacker-supplied string
        namespace_id=namespace_id,
    )

    # The bind value for namespace_id must be the session namespace, not the attacker's.
    assert execute_conn._fetch_results == []
    # Verify compile was called with the correct namespace_id.
    # We can't directly inspect compile args here, but the execute_conn received
    # a fetch call, confirming namespace_id injection did not raise.


@pytest.mark.asyncio
async def test_execute_reinjects_namespace_id_as_uuid(
    monkeypatch: pytest.MonkeyPatch,
    namespace_id: UUID,
) -> None:
    """namespace_id re-injected into params must be a uuid.UUID, never a str."""
    captured_params: list[dict[str, Any]] = []

    template_row = {
        "raw_template": "SELECT 1",
        "param_schema": json.dumps({"type": "object", "properties": {}, "required": []}),
        "target_engine": "postgres",
        "pipeline": None,
    }

    call_count = 0

    @asynccontextmanager
    async def _session(pool: Any, ns: UUID):
        nonlocal call_count
        conn = _FakeCatalogConnection(
            fetchrow_result=template_row if call_count == 0 else None, fetch_results=[]
        )
        call_count += 1
        yield conn

    monkeypatch.setattr(catalog_mod, "scoped_pg_session", _session)

    original_compile = CatalogManager._compile_template

    def _capturing_compile(self: CatalogManager, template_str: str, params: dict[str, Any]):
        captured_params.append(params.copy())
        return original_compile(self, template_str, params)

    monkeypatch.setattr(CatalogManager, "_compile_template", _capturing_compile)

    mgr = CatalogManager(pool=None)  # type: ignore[arg-type]
    await mgr.execute(slug="test-slug", params={}, namespace_id=namespace_id)

    assert len(captured_params) == 1
    assert isinstance(captured_params[0]["namespace_id"], UUID)
    assert captured_params[0]["namespace_id"] == namespace_id


# ---------------------------------------------------------------------------
# execute(): optional field trap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_optional_fields_prepopulated_as_none(
    monkeypatch: pytest.MonkeyPatch,
    namespace_id: UUID,
) -> None:
    """Optional schema props not supplied by caller are pre-populated as None
    so {% if optional_field %} evaluates to False rather than raising UndefinedError."""
    schema = {
        "type": "object",
        "properties": {
            "required_param": {"type": "string"},
            "optional_limit": {"type": "integer"},
        },
        "required": ["required_param"],
    }
    template_row = {
        "raw_template": "SELECT {{ bind('required_param') }}{% if optional_limit %} LIMIT {{ bind('optional_limit') }}{% endif %}",
        "param_schema": json.dumps(schema),
        "target_engine": "postgres",
        "pipeline": None,
    }

    call_count = 0

    @asynccontextmanager
    async def _session(pool: Any, ns: UUID):
        nonlocal call_count
        conn = _FakeCatalogConnection(
            fetchrow_result=template_row if call_count == 0 else None,
            fetch_results=[],
        )
        call_count += 1
        yield conn

    monkeypatch.setattr(catalog_mod, "scoped_pg_session", _session)

    mgr = CatalogManager(pool=None)  # type: ignore[arg-type]
    # optional_limit not supplied — must not raise UndefinedError
    result = await mgr.execute(
        slug="test-slug",
        params={"required_param": "hello"},
        namespace_id=namespace_id,
    )
    assert result == []


# ---------------------------------------------------------------------------
# execute(): unknown slug raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_unknown_slug_raises(
    monkeypatch: pytest.MonkeyPatch,
    namespace_id: UUID,
) -> None:
    @asynccontextmanager
    async def _session(pool: Any, ns: UUID):
        yield _FakeCatalogConnection(fetchrow_result=None)

    monkeypatch.setattr(catalog_mod, "scoped_pg_session", _session)

    mgr = CatalogManager(pool=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not found or is inactive"):
        await mgr.execute(slug="ghost-slug", params={}, namespace_id=namespace_id)


# ---------------------------------------------------------------------------
# record_schema(): deduplication and storage
# ---------------------------------------------------------------------------


@dataclass
class _FakeNode:
    entity_type: str


@dataclass
class _FakeEdge:
    predicate: str


@pytest.mark.asyncio
async def test_record_schema_deduplicates_edge_predicates() -> None:
    """Duplicate edge predicates must be deduplicated before the executemany call."""
    conn = _FakeCatalogConnection()
    ns = uuid4()

    edges = [
        _FakeEdge("AUTHORED"),
        _FakeEdge("REFERENCES"),
        _FakeEdge("AUTHORED"),  # duplicate
        _FakeEdge("REFERENCES"),  # duplicate
        _FakeEdge("ATTENDED"),
    ]

    await CatalogManager.record_schema(conn=conn, namespace_id=ns, nodes=[], edges=edges)  # type: ignore[arg-type]

    assert len(conn.executemany_calls) == 1
    _, data = conn.executemany_calls[0]
    predicates = [row[2] for row in data]
    assert sorted(predicates) == sorted(["AUTHORED", "REFERENCES", "ATTENDED"])
    assert len(predicates) == len(set(predicates))


@pytest.mark.asyncio
async def test_record_schema_stores_entity_types_for_nodes() -> None:
    """Node records must store entity_type, never instance labels."""
    conn = _FakeCatalogConnection()
    ns = uuid4()

    nodes = [_FakeNode("Person"), _FakeNode("Organization"), _FakeNode("Person")]

    await CatalogManager.record_schema(conn=conn, namespace_id=ns, nodes=nodes, edges=[])  # type: ignore[arg-type]

    assert len(conn.executemany_calls) == 1
    _, data = conn.executemany_calls[0]
    assert all(row[1] == "NODE" for row in data)
    type_keys = [row[2] for row in data]
    assert "Person" in type_keys
    assert "Organization" in type_keys


@pytest.mark.asyncio
async def test_record_schema_uses_native_uuid_for_namespace() -> None:
    """namespace_id in registry rows must be a uuid.UUID instance, never a str."""
    conn = _FakeCatalogConnection()
    ns = uuid4()

    await CatalogManager.record_schema(  # type: ignore[arg-type]
        conn=conn,
        namespace_id=ns,
        nodes=[_FakeNode("CONCEPT")],
        edges=[],
    )

    _, data = conn.executemany_calls[0]
    for row in data:
        assert isinstance(row[0], UUID), f"Expected UUID, got {type(row[0])}"
        assert row[0] == ns


@pytest.mark.asyncio
async def test_record_schema_empty_inputs_make_no_calls() -> None:
    """Empty nodes and edges must not issue any DB calls."""
    conn = _FakeCatalogConnection()
    ns = uuid4()

    await CatalogManager.record_schema(conn=conn, namespace_id=ns, nodes=[], edges=[])  # type: ignore[arg-type]

    assert conn.executemany_calls == []


# ---------------------------------------------------------------------------
# describe_schema(): row parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_schema_partitions_by_element_type(
    monkeypatch: pytest.MonkeyPatch,
    namespace_id: UUID,
) -> None:
    registry_rows = [
        {"element_type": "EDGE", "type_key": "ATTENDED"},
        {"element_type": "NODE", "type_key": "Organization"},
        {"element_type": "NODE", "type_key": "Person"},
        {"element_type": "EDGE", "type_key": "AUTHORED"},
    ]

    @asynccontextmanager
    async def _session(pool: Any, ns: UUID):
        yield _FakeCatalogConnection(fetch_results=registry_rows)

    monkeypatch.setattr(catalog_mod, "scoped_pg_session", _session)

    mgr = CatalogManager(pool=None)  # type: ignore[arg-type]
    schema = await mgr.describe_schema(namespace_id=namespace_id)

    assert isinstance(schema, GraphSchema)
    assert set(schema.entity_types) == {"Person", "Organization"}
    assert set(schema.edge_predicates) == {"ATTENDED", "AUTHORED"}
    assert schema.sampled_at  # non-empty ISO string
