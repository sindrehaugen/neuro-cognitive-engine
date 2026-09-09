"""The master-key registry must stay exhaustive, or a rotation loses data.

`nce/master_key_registry.py` declares every at-rest blob wrapped under `NCE_MASTER_KEY`.
A rotation re-wraps exactly that set, so a column missing from it becomes permanently
undecryptable the moment the old key is retired. The declaration is only worth trusting if
something forces it to stay complete -- otherwise it is a comment.

Two ratchets here do that:

* every `bytea` column in the declared schema is classified (wrapped, or not-wrapped with
  a reason), so a NEW bytea column fails until someone classifies it;
* every module calling `encrypt_signing_key` either owns a registered column or is listed
  as wrapping-without-persisting.

The schema is parsed from `nce/migrations/*.sql` + `schema.sql` rather than queried from a
live database, so these run in the unit tier with no services.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from nce.master_key_registry import (
    UNWRAPPED_BYTEA_COLUMNS,
    WRAPPED_COLUMNS,
    WRAPS_WITHOUT_PERSISTING,
    all_classified_bytea,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NCE_DIR = REPO_ROOT / "nce"
MIGRATIONS_DIR = NCE_DIR / "migrations"

# Partitions inherit their parent's columns; classifying both would double-count and a
# rotation would re-wrap the same physical row twice.
_PARTITION_SUFFIX = re.compile(r"(_\d+|_default|_20\d\d_\d\d)$")


def _schema_text() -> str:
    parts = [
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(MIGRATIONS_DIR.glob("*.sql"))
    ]
    schema = NCE_DIR / "schema.sql"
    if schema.exists():
        parts.append(schema.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _declared_bytea_columns() -> set[tuple[str, str]]:
    """``(table, column)`` for every BYTEA column declared in CREATE TABLE / ADD COLUMN."""

    text = _schema_text()
    found: set[tuple[str, str]] = set()

    for match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\n\s*\)\s*(?:PARTITION\s+BY[^;]*)?;",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        table = match.group(1).lower()
        if _PARTITION_SUFFIX.search(table):
            continue
        for line in match.group(2).split("\n"):
            stripped = line.split("--", 1)[0].strip()
            tokens = stripped.split()
            if len(tokens) >= 2 and tokens[1].upper().startswith("BYTEA"):
                found.add((table, tokens[0].strip('"').lower()))

    for match in re.finditer(
        r"ALTER\s+TABLE\s+(?:public\.)?([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+BYTEA",
        text,
        re.IGNORECASE,
    ):
        table = match.group(1).lower()
        if not _PARTITION_SUFFIX.search(table):
            found.add((table, match.group(2).lower()))

    return found


def test_the_schema_parser_finds_a_realistic_number_of_bytea_columns() -> None:
    """Floor on the parser itself, so the two ratchets below cannot pass vacuously.

    If a reworded CREATE TABLE or a changed formatting convention silences the pattern,
    `_declared_bytea_columns` returns an empty set and BOTH assertions below become
    trivially true. This estate has shipped that exact failure more than once, so the
    instrument gets its own floor.
    """

    found = _declared_bytea_columns()
    assert len(found) >= 10, (
        f"the BYTEA column parser found only {len(found)} columns, which means the pattern "
        "stopped matching rather than that the schema shrank -- the classification "
        "ratchets below would pass vacuously. Fix the parser before trusting them."
    )
    # Anchor on a column that must always exist: the signing key itself.
    assert ("signing_keys", "encrypted_key") in found, (
        "the parser cannot see signing_keys.encrypted_key, so it is not reading the schema"
    )


def test_every_bytea_column_is_classified() -> None:
    """A new BYTEA column must be classified before it can merge.

    Unclassified means a rotation does not know whether to re-wrap it. Guessing wrong in
    one direction corrupts the column; in the other it leaves it undecryptable.
    """

    declared = _declared_bytea_columns()
    classified = all_classified_bytea()
    unclassified = sorted(declared - classified)

    detail = "".join(f"\n  {t}.{c}" for t, c in unclassified)
    assert not unclassified, (
        "These BYTEA columns are not classified in nce/master_key_registry.py. Add each to "
        "WRAPPED_COLUMNS (if it holds master-key ciphertext) or to UNWRAPPED_BYTEA_COLUMNS "
        "with a reason (if it is a hash or signature). A master-key rotation re-wraps only "
        "the registered set, so an unclassified ciphertext column becomes permanently "
        f"undecryptable when the old key is retired:{detail}"
    )


def test_registry_does_not_claim_columns_the_schema_does_not_have() -> None:
    """The registry must not drift ahead of the schema either.

    A stale entry makes the sweep touch a column that no longer exists, which fails the
    rotation halfway through -- the worst possible time.
    """

    declared = _declared_bytea_columns()
    classified = all_classified_bytea()
    phantom = sorted(classified - declared)

    detail = "".join(f"\n  {t}.{c}" for t, c in phantom)
    assert not phantom, (
        "nce/master_key_registry.py classifies columns the schema does not declare as "
        f"BYTEA. Remove them or fix the name:{detail}"
    )


def _modules_calling_encrypt() -> set[str]:
    """Dotted module names under ``nce/`` that call ``encrypt_signing_key``."""

    callers: set[str] = set()
    for path in sorted(NCE_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "encrypt_signing_key" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "encrypt_signing_key":
                rel = path.relative_to(REPO_ROOT).with_suffix("")
                callers.add(".".join(rel.parts))
                break
    return callers


def test_every_wrapping_module_is_accounted_for() -> None:
    """Each module that wraps material either owns a registered column or is excused.

    This is the half that catches a NEW consumer. A module can add
    ``encrypt_signing_key`` and a new table in one PR; without this, the rotation sweep
    would simply not know about it, and the failure would surface only when the old key
    was retired -- long after the merge.
    """

    owners = {c.owner_module for c in WRAPPED_COLUMNS}
    excused = set(WRAPS_WITHOUT_PERSISTING)
    callers = _modules_calling_encrypt()

    # nce.signing defines the primitive itself.
    unaccounted = sorted(callers - owners - excused - {"nce.signing"})

    detail = "".join(f"\n  {m}" for m in unaccounted)
    assert not unaccounted, (
        "These modules call encrypt_signing_key but are not accounted for in "
        "nce/master_key_registry.py. Either register the column they persist to in "
        "WRAPPED_COLUMNS, or add them to WRAPS_WITHOUT_PERSISTING with a reason. A "
        "master-key rotation cannot re-wrap what nothing declares:" + detail
    )


def test_wrapped_and_unwrapped_sets_are_disjoint() -> None:
    """No column may be both wrapped and not-wrapped."""

    wrapped = {(c.table, c.column) for c in WRAPPED_COLUMNS}
    unwrapped = {(c.table, c.column) for c in UNWRAPPED_BYTEA_COLUMNS}
    overlap = sorted(wrapped & unwrapped)
    assert not overlap, f"classified as BOTH wrapped and unwrapped: {overlap}"


def test_every_entry_carries_a_reason() -> None:
    """Reasons are the point: a bare list rots into a list nobody trusts."""

    missing = [f"{c.table}.{c.column}" for c in WRAPPED_COLUMNS if not c.note.strip()]
    missing += [f"{c.table}.{c.column}" for c in UNWRAPPED_BYTEA_COLUMNS if not c.reason.strip()]
    missing += [m for m, why in WRAPS_WITHOUT_PERSISTING.items() if not why.strip()]
    assert not missing, f"registry entries without a stated reason: {missing}"
