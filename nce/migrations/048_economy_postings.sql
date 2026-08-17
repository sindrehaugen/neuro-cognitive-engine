-- 048_economy_postings.sql
-- ============================================================================
-- Economy engine (Module 8, Wave 6 — graph-postings): the balanced-ledger
-- table behind the POSTING kg_node, plus per-vertical economy_source_id
-- provenance columns on kg_nodes/kg_edges (INVOICE/POSTING/PERIOD/MARGIN
-- upserts — see nce/vertical_modules/economy/graph.py).
--
-- Every write to this table goes through
-- nce.vertical_modules.economy.graph.persist_financial_event(), which takes
-- the NORMALISED event nce.vertical_modules.economy.events.do_emit_financial_event
-- already validated (postings sum to zero within epsilon, amounts are exact
-- Decimal). That Python-level guard is necessary but the wave explicitly asks
-- for a STORAGE-level backstop too (docs/vertical_engines/08-economy-engine.md
-- B2: "economy_postings table (RLS, sum=0 guard)") -- a future direct-SQL
-- write, or a bug that bypasses do_emit_financial_event, must not be able to
-- leave an unbalanced ledger undetected. trg_economy_postings_assert_balanced
-- below is that backstop: an AFTER INSERT STATEMENT-level trigger using a
-- transition table (REFERENCING NEW TABLE) so a single multi-row INSERT for
-- one event (all its posting lines, inserted together as one statement — see
-- persist_financial_event) is checked ONCE, against the FULL set of rows
-- already stored for that (namespace_id, event_id) -- not just the newly
-- inserted rows -- so a partial insert history would still be caught
-- correctly. A full replay (every line already present, ON CONFLICT DO
-- NOTHING skips all of them) leaves the transition table empty and the
-- trigger correctly does nothing -- idempotency and the balance guard do not
-- fight each other.
--
-- Tolerance: the SAME 0.01 (oere) epsilon as
-- nce.vertical_modules.economy.events.do_emit_financial_event's documented
-- default / cascade.py's _BALANCE_EPSILON_DEFAULT -- NOT bit-exact zero.
-- Quantising each leg to oere independently (persist_financial_event does
-- this because Postgres NUMERIC(18,2) would otherwise silently round an
-- unquantised amount on write -- the exact bug class migration 047 already
-- fixed once) is a real transformation that can in principle move an
-- already-epsilon-approved raw sum by a fraction of an oere per leg; matching
-- the application epsilon here (rather than demanding a stricter exact-zero)
-- means this backstop catches a REAL break without rejecting a legitimately
-- balanced, already-quantised ledger entry.
--
-- Field ownership: economy_postings is Economy-owned exclusively. `amount` is
-- a SINGLE signed column, never a separate debit/credit column pair -- this
-- engine's own convention (ngaap.py's do_compute_bucket_targets docstring: "a
-- leg's debit/credit direction follows the SIGN of its amount") already
-- established that a debit/credit column split invites exactly the sign-swap
-- class of bug this wave's brief calls out by name.
--
-- Idempotent DDL -- there is no migration ledger in this repo; schema.sql and
-- every migrations/*.sql file re-run on every boot under an advisory lock.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- economy_source_id provenance column (per-vertical source-id pattern; see
-- migrations 037/038/042/046 for the identical precedent on other engines).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_nodes' AND column_name = 'economy_source_id'
    ) THEN
        ALTER TABLE kg_nodes ADD COLUMN economy_source_id TEXT;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'kg_edges' AND column_name = 'economy_source_id'
    ) THEN
        ALTER TABLE kg_edges ADD COLUMN economy_source_id TEXT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_kg_nodes_economy_source
    ON kg_nodes (namespace_id, economy_source_id)
    WHERE economy_source_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_kg_edges_economy_source
    ON kg_edges (namespace_id, economy_source_id)
    WHERE economy_source_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- economy_postings -- one row per posting LINE within a balanced financial
-- event. `event_id` is the event's own deterministic content hash (from
-- do_emit_financial_event) -- the SAME id addresses the corresponding
-- POSTING kg_node (label `POSTING:{event_id}`; see upsert_posting_node), so
-- the graph node and its ledger detail share one identity. Natural key
-- (namespace_id, event_id, line_no) -- ONE ROW PER (event, line position);
-- INSERT ... ON CONFLICT DO NOTHING, never DO UPDATE -- same
-- idempotent-by-constraint discipline as economy_bom_actual_costs (migration
-- 047): a replay of the identical event is a no-op, not a silent overwrite.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS economy_postings (
    id                 UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    event_id           TEXT          NOT NULL,
    event_type         TEXT          NOT NULL,
    line_no            INTEGER       NOT NULL,
    account            TEXT          NOT NULL,
    -- NOK, 2-decimal (oere) precision -- mirrors economy_bom_actual_costs.actual_cost
    -- (migration 047). Signed: debit/credit direction is the SIGN of this
    -- value, never a separate column (see header comment).
    amount             NUMERIC(18,2) NOT NULL,
    period_id          TEXT,
    economy_source_id  TEXT,
    change_origin      TEXT          NOT NULL DEFAULT 'agent'
                                      CHECK (change_origin IN
                                          ('sync','webhook','agent','operator','consolidation','replay','unknown')),
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

ALTER TABLE economy_postings DROP CONSTRAINT IF EXISTS economy_postings_natural_key;
ALTER TABLE economy_postings
    ADD CONSTRAINT economy_postings_natural_key
    UNIQUE (namespace_id, event_id, line_no);

-- Non-empty `account` CHECK (storage-level backstop, round-3 fix): an
-- account-less posting can be arithmetically perfect (sums to zero) and
-- still be financially meaningless -- graph.py's persist_financial_event
-- already refuses this at the Python level, but Batch 118's lesson is that
-- balancing to zero is necessary, never sufficient, so the storage layer
-- must not depend solely on the application to enforce it. Mirrors
-- migration 033's `owner_engine <> ''` precedent; TRIM() also catches
-- whitespace-only accounts, which a bare `<> ''` would miss. Idempotent via
-- the pg_constraint existence guard (safe to re-run).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'ck_economy_postings_account_nonempty'
          AND  conrelid = 'economy_postings'::regclass
    ) THEN
        ALTER TABLE economy_postings
            ADD CONSTRAINT ck_economy_postings_account_nonempty
            CHECK (TRIM(account) <> '');
    END IF;
END $$;

-- Non-unique: supports the per-event lookup the sum=0 trigger and the
-- application-level read path both use (WHERE namespace_id=... AND event_id=...).
CREATE INDEX IF NOT EXISTS idx_economy_postings_namespace_event
    ON economy_postings (namespace_id, event_id);

ALTER TABLE economy_postings ENABLE ROW LEVEL SECURITY;
ALTER TABLE economy_postings FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON economy_postings;
CREATE POLICY tenant_isolation_policy ON economy_postings
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE economy_postings FROM nce_app;
        -- Append-only ledger (WORM), round-3 fix: withhold UPDATE/DELETE
        -- from nce_app at the grant level, following this repo's own
        -- precedent -- event_log grants nce_app only INSERT, SELECT
        -- (schema.sql), deliberately withholding UPDATE/DELETE. No shipped
        -- code path issues UPDATE/DELETE against economy_postings (graph.py's
        -- persist_financial_event is the sole writer and is INSERT-only),
        -- but nce_app is the application's general role, so any future bug,
        -- admin raw-SQL tool, or correction script would otherwise hit an
        -- unguarded table. Corrections must instead go through compensating
        -- reversal postings -- standard ledger practice, now enforced
        -- structurally rather than left to convention. Idempotent: REVOKE
        -- ALL first means a re-run always converges on exactly SELECT,
        -- INSERT regardless of what an earlier version of this migration
        -- granted.
        GRANT SELECT, INSERT ON TABLE economy_postings TO nce_app;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Storage-level sum=0 backstop (see header comment for the full rationale).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION economy_postings_assert_balanced() RETURNS TRIGGER AS $BODY$
DECLARE
    bad RECORD;
BEGIN
    FOR bad IN
        SELECT ep.namespace_id AS ns, ep.event_id AS eid, SUM(ep.amount) AS total
        FROM economy_postings ep
        JOIN (SELECT DISTINCT namespace_id, event_id FROM new_postings) np
          ON np.namespace_id = ep.namespace_id AND np.event_id = ep.event_id
        GROUP BY ep.namespace_id, ep.event_id
        HAVING ABS(SUM(ep.amount)) > 0.01
    LOOP
        RAISE EXCEPTION
            'economy_postings: event % (namespace %) does not balance to zero (sum=%, tolerance=+/-0.01)',
            bad.eid, bad.ns, bad.total;
    END LOOP;
    RETURN NULL;
END;
$BODY$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_economy_postings_assert_balanced ON economy_postings;
CREATE TRIGGER trg_economy_postings_assert_balanced
    AFTER INSERT ON economy_postings
    REFERENCING NEW TABLE AS new_postings
    FOR EACH STATEMENT
    EXECUTE FUNCTION economy_postings_assert_balanced();

COMMENT ON TABLE economy_postings IS
'Economy-owned balanced-ledger table (Module 8, Wave 6). One row per posting
LINE within a balanced financial event (do_emit_financial_event validates the
whole event balances to zero within epsilon BEFORE any write reaches here --
see nce/vertical_modules/economy/events.py). event_id is the event''s own
deterministic content hash, shared with the corresponding POSTING kg_node
label (POSTING:{event_id}). Natural-keyed (namespace_id, event_id, line_no) --
ONE ROW PER (event, line); INSERT ... ON CONFLICT DO NOTHING, so a replay of
the identical event is a no-op by construction, never a silent overwrite
(mirrors economy_bom_actual_costs, migration 047). `amount` is signed --
direction follows the sign, never a separate debit/credit column pair.
trg_economy_postings_assert_balanced is a STORAGE-level backstop (not a
replacement for the application-level guard in events.py): it re-checks
SUM(amount)=0 within +/-0.01 NOK per (namespace_id, event_id) after every
INSERT, using a transition table so a multi-row insert for one event is
checked once against the full stored set for that event. FORCE RLS isolates
per tenant (mirrors procurement_bid_prices / economy_bom_actual_costs).
Round-3 fixes: nce_app is granted only SELECT, INSERT (append-only/WORM,
mirrors event_log) -- corrections must be compensating reversal postings,
never an UPDATE/DELETE; and ck_economy_postings_account_nonempty rejects an
empty or whitespace-only `account` at the DB level, independently of
graph.py''s own guard (the same non-empty-after-TRIM CHECK pattern as
migration 033''s owner_engine constraint).';
