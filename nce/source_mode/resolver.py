"""
nce.source_mode.resolver — C5 source-mode resolver.

``resolve(engine, function, namespace)`` looks up the active mode from the
``source_mode_config`` table (Wave 26) and returns one of three literals:

    "d365"  — reads and writes go to the external Dynamics 365 system.
    "both"  — reads NCE-primary with a parity check; writes go to both.
    "nce"   — reads and writes are native-only (migration complete).

Safe default: ``"d365"``.  When no row is configured for a given
``(engine, function)`` key the resolver returns ``"d365"`` — the
conservative external-source default — rather than raising or silently
falling back to native.  The caller should never need to handle a missing
mode; *not* having a row means "we have not migrated yet".

``read_through(mode, *, native_reader, external_reader, parity_check)``
and ``write_route(mode, *, native_writer, external_writer)`` dispatch by
the resolved mode using a table-driven approach — no per-mode branches
that drift.

Write-routing contract (§9.2):
  - While mode is ``"d365"`` or ``"both"``: write-through to the external
    system (and also to native when ``"both"``).
  - Once mode is ``"nce"``: native-only writes; external is skipped.

Namespace isolation: every query runs inside ``scoped_pg_session`` so the
RLS ``nce.namespace_id`` GUC is active for the duration.

Dependencies point inward: no web, HTTP, admin, or framework imports here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]

from nce.db_utils import scoped_pg_session

# ---------------------------------------------------------------------------
# Public type
# ---------------------------------------------------------------------------

SourceMode = Literal["d365", "both", "nce"]

# Conservative external-source fallback: no row → still on D365 (not yet migrated).
_DEFAULT_MODE: SourceMode = "d365"


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


async def resolve(
    pool: asyncpg.Pool,  # type: ignore[type-arg]
    *,
    engine: str,
    function: str,
    namespace_id: str | UUID,
) -> SourceMode:
    """Look up the active source mode for ``(engine, function)`` in ``namespace_id``.

    Returns the configured ``mode`` value from ``source_mode_config``, or
    ``"d365"`` when no row exists (conservative external-source default —
    means migration has not started yet for this function).

    Args:
        pool:         asyncpg pool; a scoped connection is acquired internally.
        engine:       Engine name key (e.g. ``"d365_sync"``).
        function:     Function name key (e.g. ``"read_contacts"``).
        namespace_id: Active namespace UUID (string or UUID).

    Returns:
        One of ``"d365"``, ``"both"``, or ``"nce"``.
    """
    async with scoped_pg_session(pool, namespace_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT mode
              FROM source_mode_config
             WHERE namespace_id = $1
               AND engine = $2
               AND function = $3
            """,
            UUID(str(namespace_id)) if not isinstance(namespace_id, UUID) else namespace_id,
            engine,
            function,
        )

    if row is None:
        return _DEFAULT_MODE

    mode: str = row["mode"]
    # Belt-and-braces: the CHECK constraint on the table enforces valid values,
    # but we guard here so mypy and callers get the narrowed type.
    if mode not in ("d365", "both", "nce"):  # pragma: no cover
        return _DEFAULT_MODE

    return mode  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# read_through
# ---------------------------------------------------------------------------

# Dispatch table: maps mode → (use_native, use_external, run_parity)
_READ_DISPATCH: dict[SourceMode, tuple[bool, bool, bool]] = {
    "d365": (False, True, False),
    "both": (True, False, True),  # NCE-primary; external used only for parity
    "nce": (True, False, False),
}


async def read_through(
    mode: SourceMode,
    *,
    native_reader: Callable[[], Awaitable[Any]],
    external_reader: Callable[[], Awaitable[Any]],
    parity_check: Callable[[Any, Any], Awaitable[None]],
) -> Any:
    """Dispatch a read by resolved ``mode``.

    Mode semantics:
        ``"d365"``  — call ``external_reader``; return its result.
        ``"both"``  — call ``native_reader`` (primary); fire ``parity_check``
                      with (native_result, external_result); return native_result.
        ``"nce"``   — call ``native_reader``; return its result.

    Args:
        mode:            Resolved source mode.
        native_reader:   Zero-arg async callable that reads from NCE.
        external_reader: Zero-arg async callable that reads from the external system.
        parity_check:    Two-arg async callable ``(native, external)`` invoked for
                         ``"both"`` mode; divergence logging is Wave 28's concern —
                         this callable is the hook point.

    Returns:
        The result of the primary reader for the resolved mode.
    """
    use_native, use_external, run_parity = _READ_DISPATCH[mode]

    native_result: Any = None
    external_result: Any = None

    if use_native or run_parity:
        native_result = await native_reader()
    if use_external or run_parity:
        external_result = await external_reader()

    if run_parity:
        await parity_check(native_result, external_result)

    return native_result if (use_native or run_parity) else external_result


# ---------------------------------------------------------------------------
# write_route
# ---------------------------------------------------------------------------

# Dispatch table: maps mode → (write_native, write_external)
# Write-through to external while "d365" or "both"; native-only once "nce".
_WRITE_DISPATCH: dict[SourceMode, tuple[bool, bool]] = {
    "d365": (False, True),
    "both": (True, True),
    "nce": (True, False),
}


async def write_route(
    mode: SourceMode,
    *,
    native_writer: Callable[[], Awaitable[Any]],
    external_writer: Callable[[], Awaitable[Any]],
) -> dict[str, Any]:
    """Route a write by resolved ``mode``.

    Write-through contract (§9.2):
        ``"d365"``  — external-only write.
        ``"both"``  — write-through: both native and external receive the write.
        ``"nce"``   — native-only write; external is skipped.

    Args:
        mode:            Resolved source mode.
        native_writer:   Zero-arg async callable that writes to NCE.
        external_writer: Zero-arg async callable that writes to the external system.

    Returns:
        ``{"native": result | None, "external": result | None}`` — callers
        may inspect individual results; ``None`` means that path was not
        executed for the given mode.
    """
    write_native, write_external = _WRITE_DISPATCH[mode]

    native_result: Any = None
    external_result: Any = None

    if write_native:
        native_result = await native_writer()
    if write_external:
        external_result = await external_writer()

    return {"native": native_result, "external": external_result}
