> **Status:** shipped · **Verified-against:** 4125327 (main) · **Last-audited:** 2026-09-21

# ADR-0008: Append-only enforcement for support_ticket_actions

## Status

Shipped

## Context

`support_ticket_actions` logs interventions (*tiltak*) and outcomes
(*utfall*) against a support ticket — a record of what was tried and what
happened, per ticket. Six sites in this codebase already described it as
append-only: migration `091_support_ticket_actions.sql`'s own header, this
table's `ResourceSpec` description (`support/resources.py`), two comments
in `support/tickets.py`, and two documentation pages
(`docs/database_architecture.md`, `docs/engines/support-user.md`) — all
citing "ADR 0042" as the source of that decision.

No ADR 0042 exists. `docs/adr/` is numbered sequentially from 0000; the
highest number before this one was 0007. Six independent citations
recorded a real intent with no enforcement behind it and no real document
to point to.

The database backed none of it: migration 091 granted `nce_app`
`SELECT, INSERT, UPDATE, DELETE` on this table — full CRUD, no WORM
trigger, no restricted grant. The generic C12 resource-surface route
(`TICKET_ACTION_SPEC`, `support/resources.py`) declared no
`excluded_verbs` for `upsert` either, so a real, registered MCP tool
(`support_upsert_ticket_actions`) could `PATCH` an existing row — and the
test suite's own generic control (`test_generated_patch[support:ticket-actions]`)
passed, treating that mutation as correct, working behavior.

Measured before deciding (see
`_internal/work-docs/mlv16-orchestration/TICKET_ACTION_APPEND_ONLY.md`):
`do_log_ticket_action` (`support/tickets.py`) is the only hand-written
writer of this table and only ever `INSERT`s. No hand-written `UPDATE` or
`DELETE` against `support_ticket_actions` exists anywhere in the tree,
and nothing calls `support_upsert_ticket_actions` outside the generic
route's own generated-controls test. Tightening the grant breaks no real
caller.

Alternatives considered:
- **Strike the claim** (treat "append-only" as aspirational prose,
  documented nowhere real, and remove the six citations without
  enforcing anything). Rejected: six independent citations recording the
  same intent, across a migration, a spec, application code, and two doc
  pages, is evidence of a real decision that was made and never wired
  up — not evidence the decision was never wanted. This estate already
  knows how to build the enforced version (`event_log`, ADR-0001) and
  simply never did it here.
- **Trigger-based enforcement**, matching `event_log`'s
  `prevent_mutation()` (ADR-0001) — enforced at the storage layer for
  every role and connection, including a bypass of `FORCE ROW LEVEL
  SECURITY`. Rejected for this table specifically: `event_log`'s trigger
  protects the Merkle chain-hash integrity check, where a single silently
  mutated row breaks retroactive verification across the whole chain.
  `support_ticket_actions` carries no chain-hash dependency and no
  cryptographic integrity guarantee downstream depends on — a grant
  revocation closes the actual gap (an unauthenticated generic write
  route) without adding trigger machinery this table's risk profile
  doesn't call for. If a future need for superuser-bypass-proof
  immutability emerges here, this ADR should be superseded.
- **Governed writer via `excluded_verbs` reason (3)** — no governed actor
  exists for the `UPDATE`/`DELETE` half of this table (checked: reason
  (3) requires naming a real writer by `file:line`; none exists to name).
  Does not apply.

## Decision

`nce_app`'s grant on `support_ticket_actions` is narrowed to
`SELECT, INSERT` only — the same shape as `event_log`'s grant
(`schema.sql:838`), not its trigger. `TICKET_ACTION_SPEC` excludes
`upsert` from its generated C12 surface, citing
`ResourceSpec.excluded_verbs` reason (2) (storage itself permanently
forbids the verb, cited by the exact grant line that makes it
impossible — not a policy choice).

All six phantom "ADR 0042" citations are replaced with this ADR's real
number.

**Source citations** (verified via `git show main:<path>` before
writing, and again after landing):
- `nce/migrations/108_support_ticket_actions_append_only.sql` —
  `REVOKE UPDATE, DELETE ON TABLE support_ticket_actions FROM nce_app` —
  the enforcement
- `nce/schema.sql` — mirror block immediately after the original 091
  grant, same statement, cumulative current state
- `nce/vertical_modules/support/resources.py` —
  `TICKET_ACTION_SPEC`'s `excluded_verbs=frozenset({"archive", "upsert"})`
  (archive was already excluded by the separate archive-column wave;
  this ADR adds `upsert`)
- `nce/vertical_modules/support/tickets.py` — `do_log_ticket_action`, the
  sole, INSERT-only writer, unaffected by this change

## Consequences

### Positive

- The database now enforces what six sites already claimed: `nce_app`
  cannot `UPDATE` or `DELETE` a `support_ticket_actions` row under any
  application code path, regardless of future changes to
  `TICKET_ACTION_SPEC` or `support/tickets.py`.
- The generic C12 resource-surface route can no longer offer a `PATCH`
  that the storage layer would refuse anyway — the previous shape
  (`test_generated_patch[support:ticket-actions]` passing against a table
  whose own spec called itself append-only) does not recur.
- Closes a real, if narrow, gap the `PO_LINE_SPEC`/`SIGNED_BASELINE_SPEC`
  archive-column and grant-mismatch findings already established a
  pattern for tonight: an advertised verb the storage never actually
  supported.

### Negative / Trade-offs

- A ticket action logged in error cannot be corrected in place; a
  compensating action entry must be appended instead (matches
  `event_log`'s own accepted trade-off, `ADR-0001`).
- Unlike `event_log`, this enforcement is grant-level, not
  trigger-level: a superuser connection or a future re-grant could still
  bypass it. Accepted as proportionate to this table's actual risk
  profile (no chain-hash dependency), not as a statement that grant-level
  enforcement is always sufficient — `event_log`'s stronger guarantee
  remains the right choice where cryptographic integrity is at stake.
