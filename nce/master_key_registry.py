"""Every at-rest blob wrapped under ``NCE_MASTER_KEY``, declared in one place.

WHY THIS EXISTS
---------------
Changing ``NCE_MASTER_KEY`` requires re-wrapping every ciphertext wrapped under the old
one. Before this module there was **no way to enumerate that set**: several modules called
``nce.signing.encrypt_signing_key`` independently, and the only way to find them was to
grep -- which over-counted, because it also matched ``require_master_key`` READERS that
wrap nothing. The AST ratchet below puts the real number at **8 call sites**: six modules
that own a registered column, plus two that wrap without persisting. A rotation therefore meant "grep, hope, restart", and any consumer added later would
silently not be re-wrapped -- its rows becoming permanently undecryptable the moment the
old key was retired.

So the set is declared here, and ``tests/test_master_key_registry.py`` enforces two
invariants that make the declaration trustworthy rather than aspirational:

1. **Every ``bytea`` column in the schema is classified** -- either as wrapped material or
   as a hash/signature with a stated reason. A new ``bytea`` column fails the ratchet until
   someone classifies it. That is what stops the set going stale again.
2. **Every module that calls ``encrypt_signing_key`` is accounted for** -- either it owns a
   registered column, or it is listed as wrapping-without-persisting with a reason.

DERIVED FROM THE LIVE SCHEMA, NOT FROM GREP
-------------------------------------------
The classification below was produced by listing every ``bytea`` column in
``information_schema`` on the deployed database (14 non-partition columns), then checking
each one's writer. Partitions are excluded deliberately: they inherit their parent's
columns, so ``event_log_2026_09.signature`` is the same column as ``event_log.signature``
and re-wrapping it twice would be wrong.

WHAT A ROTATION DOES **NOT** TOUCH
----------------------------------
The six hash/signature columns are derived values, not ciphertext. Verified: none of them
appears on a line with ``encrypt_signing_key``. Critically, ``rewrap_signing_key`` changes
only the *wrapping* of a signing key -- the signing key material itself is unchanged -- so
**every existing signature and Merkle chain_hash stays valid across a master-key rotation**.
A rotation is not a re-signing event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final


@dataclass(frozen=True)
class WrappedColumn:
    """A column holding ciphertext wrapped under ``NCE_MASTER_KEY``."""

    table: str
    column: str
    owner_module: str
    note: str
    # Columns used ONLY to identify a row in the sweep's output -- never in its WHERE clause.
    #
    # F6, 2026-09-10 rebuild: the sweep reported its two unopenable rows as
    # `id=UUID('3975871b-...')`, while the runbook and the rebuild handoff both name the
    # expected pair by `key_id` (`sk-cccdd24038f54c32`, `sk-8f3adec2e7e34abb`). So an
    # operator mid-rotation had to hand-join UUID to key_id to answer the one question that
    # matters -- "are these the two failures I was told to expect, or two new ones?" --
    # which is exactly the check the runbook exists to make cheap. Empty means "identify
    # rows by key_columns", which is right for every column whose PK is already meaningful.
    label_columns: tuple[str, ...] = ()
    # Primary-key column(s) used to address a row when re-wrapping it.
    #
    # These MUST match the table's real PRIMARY KEY. The default ("id",) was wrong for four
    # of the eight columns, and the rewrap sweep died on `column "id" does not exist` the
    # first time it ran against the live database: `settings` is keyed by "key", and
    # `memories` / `pii_redactions` are RANGE partitioned so their PK is composite
    # ("id", "created_at") -- Postgres requires the partition key in the PK. A wrong key
    # column here means an UPDATE that matches nothing, or one that matches more than
    # intended, so the sweep pre-flights these against information_schema before writing.
    key_columns: tuple[str, ...] = field(default=("id",))


@dataclass(frozen=True)
class UnwrappedByteaColumn:
    """A ``bytea`` column that is NOT master-key ciphertext, with the reason."""

    table: str
    column: str
    reason: str


# ---------------------------------------------------------------------------
# Wrapped under NCE_MASTER_KEY -- a rotation MUST re-wrap all of these.
# ---------------------------------------------------------------------------
WRAPPED_COLUMNS: tuple[WrappedColumn, ...] = (
    WrappedColumn(
        table="signing_keys",
        column="encrypted_key",
        label_columns=("key_id", "status"),
        owner_module="nce.signing",
        note=(
            "The signing key itself. `rewrap_signing_key` (nce/signing.py:796) already "
            "implements the re-wrap for this one -- and had ZERO callers before the sweep, "
            "so the capability existed but was never wired up."
        ),
        key_columns=("id",),
    ),
    WrappedColumn(
        table="memories",
        column="wrapped_dek",
        owner_module="nce.envelope",
        note="Per-memory data encryption key. Envelope encryption; the DEK unwraps memory content.",
        key_columns=("id", "created_at"),
    ),
    WrappedColumn(
        table="pii_redactions",
        column="encrypted_value",
        owner_module="nce.pii",
        note="Redacted PII retained for lawful re-identification.",
        key_columns=("id", "created_at"),
    ),
    WrappedColumn(
        table="settings",
        column="secret_enc",
        owner_module="nce.settings_store",
        note="Operator-entered secrets from the admin UI.",
        key_columns=("key",),
    ),
    WrappedColumn(
        table="signing_credentials",
        column="encrypted_secret",
        owner_module="nce.signing_credentials",
        note="Broker / national-eID signing provider client secrets (Wave Q-4).",
    ),
    WrappedColumn(
        table="bridge_subscriptions",
        column="oauth_access_token_enc",
        owner_module="nce.bridge_repo",
        note="OAuth access tokens for outbound bridges; also renewed by nce.bridge_renewal.",
    ),
    WrappedColumn(
        table="d365_integrations",
        column="token_enc",
        owner_module="nce.bridge_repo",
        note="Dynamics 365 integration token.",
    ),
    WrappedColumn(
        table="d365_integrations",
        column="webhook_secret_enc",
        owner_module="nce.bridge_repo",
        note=(
            "D365 webhook shared secret. NB: no Python writer was found for this column "
            "when the registry was built -- it is either written through a path the search "
            "missed or it is dead. Registered anyway: a rotation that skipped a live column "
            "would be unrecoverable, whereas re-wrapping a dead one is harmless."
        ),
    ),
)


# ---------------------------------------------------------------------------
# bytea, but NOT master-key ciphertext. A rotation must leave these alone.
# ---------------------------------------------------------------------------
UNWRAPPED_BYTEA_COLUMNS: tuple[UnwrappedByteaColumn, ...] = (
    UnwrappedByteaColumn(
        table="event_log",
        column="signature",
        reason=(
            "Ed25519/HMAC signature produced WITH the signing key, not wrapped under the "
            "master key. Re-wrapping a signing key does not change its material, so every "
            "signature stays valid across a rotation."
        ),
    ),
    UnwrappedByteaColumn(
        table="event_log",
        column="chain_hash",
        reason="Merkle chain hash. Derived, not encrypted. Unaffected by rotation.",
    ),
    UnwrappedByteaColumn(
        table="event_log",
        column="llm_payload_hash",
        reason="Content hash of an LLM payload. Derived, not encrypted.",
    ),
    UnwrappedByteaColumn(
        table="memories",
        column="signature",
        reason="Signature over the memory row. Same reasoning as event_log.signature.",
    ),
    UnwrappedByteaColumn(
        table="a2a_grants",
        column="token_hash",
        reason="One-way hash of an A2A grant token. Not reversible, nothing to re-wrap.",
    ),
    UnwrappedByteaColumn(
        table="action_idempotency",
        column="response_hash",
        reason="Idempotency response hash. Derived, not encrypted.",
    ),
)


# ---------------------------------------------------------------------------
# Modules that call encrypt_signing_key WITHOUT persisting a bytea column.
# Listed so the ratchet can tell "accounted for" from "forgotten".
# ---------------------------------------------------------------------------
WRAPS_WITHOUT_PERSISTING: dict[str, str] = {
    "nce.master_key_ring": (
        "Re-wraps a blob and hands it back; the caller writes it. Persists nothing itself. "
        "Added by the rewrap sweep -- and this ratchet caught it on the very next PR after "
        "the registry landed, which is the behaviour it exists for."
    ),
    "nce.analytics.stress": (
        "Returns the blob to its caller rather than storing it (stress/benchmark helper). "
        "No table to re-wrap."
    ),
    "nce.vertical_modules.netbox.contacts": (
        "Encrypts at contacts.py:207 and decrypts the same value at :210, then INSERTs into "
        "on_call_routing -- which has NO bytea column. The encryption there appears to be a "
        "no-op round trip and is worth a look on its own; either way there is nothing "
        "persisted for a rotation to re-wrap."
    ),
}


def wrapped_column_count() -> int:
    """Number of columns a master-key rotation must re-wrap."""
    return len(WRAPPED_COLUMNS)


def all_classified_bytea() -> set[tuple[str, str]]:
    """Every ``(table, column)`` this registry classifies, wrapped or not."""
    return {(c.table, c.column) for c in WRAPPED_COLUMNS} | {
        (c.table, c.column) for c in UNWRAPPED_BYTEA_COLUMNS
    }


# ---------------------------------------------------------------------------
# RLS visibility guard -- F10 / F11, 2026-09-10
# ---------------------------------------------------------------------------

# Registered tables carrying FORCE ROW LEVEL SECURITY. Verified against
# pg_class.relforcerowsecurity on the live database 2026-09-10; `signing_keys` and `settings`
# are the only two registered tables that do NOT force RLS.
FORCE_RLS_WRAPPED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "memories",
        "pii_redactions",
        "signing_credentials",
        "bridge_subscriptions",
        "d365_integrations",
    }
)


async def rls_visibility_problem(conn: Any) -> str | None:
    """Return a refusal message if *conn*'s role cannot see FORCE-RLS rows, else ``None``.

    **Every master-key tool must call this, and call it before reading any wrapped column.**

    These tools read wrapped columns with a bare ``SELECT ... WHERE <col> IS NOT NULL`` and set
    **no namespace context**. Five of the eight registered columns are on FORCE-RLS tables, so a
    role without ``BYPASSRLS`` sees zero rows in them -- no error, no warning. Measured on a
    restore of the live database, same data and same moment, differing only by role: a
    NOBYPASSRLS role saw **0** of ``pii_redactions``' 4 blobs and reported **3** off-primary
    where the truth was **7**.

    This is the one failure mode in this area that lies in the **reassuring** direction, so it
    is a refusal rather than a warning. An ``information_schema`` pre-flight cannot substitute:
    that catalogue is not RLS-filtered, so a schema check passes cleanly for a role that can
    see none of the data.

    Fails **closed** -- an unresolvable role (``NULL``) refuses too, because that is exactly
    when you least want to guess.
    """

    can_bypass = await conn.fetchval(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if can_bypass:
        return None
    role = await conn.fetchval("SELECT current_user")
    return (
        f"role {role!r} cannot bypass row-level security (needs rolsuper or rolbypassrls). "
        f"REFUSING TO RUN: {len(FORCE_RLS_WRAPPED_TABLES)} of the {len(WRAPPED_COLUMNS)} "
        "master-key-wrapped columns are on FORCE ROW LEVEL SECURITY tables and this tool sets "
        "no namespace context, so this role would see ZERO rows in them. Counts would be "
        "silently under-reported and the run would report success while blobs stayed wrapped "
        "under the old key. Re-run as a BYPASSRLS role (mcp_user)."
    )
