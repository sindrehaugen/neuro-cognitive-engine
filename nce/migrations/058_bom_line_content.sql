-- 058_bom_line_content.sql
-- ============================================================================
-- Module 0, Wave 31 (Batch 132a): the BOM_LINE content store.
--
-- BOM_LINE is referenced by five-or-more engines (sales/dealroom.py,
-- project/convert.py, project/tasks.py all SELECT it) but no wave in the
-- original 231 was ever assigned to WRITE it. This table is that write path's
-- home. It is shared, top-level foundation -- NOT engine-prefixed -- because
-- both system_design (manual design authoring) and sales (manual pick,
-- package expansion, external ingest) write it; naming it `sales_*` would
-- assert an ownership §9.1 denies, and system_design must not import sales.
--
-- Field ownership follows the roadmap's own "5-writer BOM_LINE" decomposition
-- (§9.1, worked example, and mirrored in migration 047's header): CONTENT
-- (this table's qty/unit_price/line_total/currency) is authored by whichever
-- flow created the line and is Sales-frozen at contract signature; STATUS
-- (this table's status column) advances independently through
-- Procurement -> Inventory -> Field Tech; actual_cost belongs to the Economy
-- cascade alone (economy_bom_actual_costs, migration 047) and is NOT this
-- table's concern.
--
-- Column notes:
--   quote_id / line_ref are their OWN columns, never parsed from the label at
--   query time. Every existing quote-scoped read in this repo has had to
--   defend against LIKE metacharacters in a caller-supplied quote_id
--   (dealroom.py:80-89, convert.py:288-309, cascade.py:443-471 all use
--   starts_with() against a LIKE prefix built from quote_id). Explicit
--   columns make every read here an equality filter, so that hazard cannot
--   recur against this table.
--
--   qty NUMERIC(14,4): AV lines mix discrete counts (2 displays) and
--   continuous lengths (12.5 m of cable) in the same BOM.
--
--   unit_price / line_total NUMERIC(18,2), currency CHAR(3) DEFAULT 'NOK', no
--   fx_rate / fx_as_of. ORCHESTRATOR DECISION (deviates from the build plan's
--   `unit_price_nok` / `line_total_nok` naming): the plan itself states the
--   cost of deferring FX is not the missing columns, it is that "every
--   historical row has an implicit currency you have to assert rather than
--   read" -- one column removes exactly that cost, and a `_nok` suffix would
--   contradict itself the day a second currency appears. FX conversion
--   machinery has no caller today and is not built here.
--
--   line_total is STORED, never `GENERATED AS (qty * unit_price)` --
--   discount logic is not necessarily multiplicative and must not be baked
--   into DDL.
--
--   origin_kind TEXT NOT NULL, deliberately NO CHECK: provenance is open by
--   construction, following sales_read_model.source, which has absorbed ten
--   entity kinds with no DDL change. This is NOT one column per source (the
--   kg_nodes.d365_source_id / procurement_source_id mistake this repo has
--   already made twice). origin_kind, origin_ref and writer_engine are a
--   TRUST BOUNDARY: they are set only by the calling flow's own code inside
--   nce/bom_lines.py, never accepted as caller-supplied tool arguments -- see
--   that module's docstring. A caller-controlled origin_kind would let a
--   manually entered line claim `origin_kind='design'`, and a later
--   reconciliation report against legally signed baselines would trust it.
--
--   status / status_changed_at are DELIBERATELY OUTSIDE the freeze trigger's
--   protected column set below -- content freezes, status keeps advancing
--   (ORDERED -> DELIVERED -> INSTALLED) after a line's content is frozen.
--   That is the entire point of the §9.1 field-ownership decomposition.
--
-- Natural key: UNIQUE (namespace_id, bom_line_label) -- namespace_id MUST be
-- in it, or one tenant's quote id would block another tenant's insert of the
-- same id.
--
-- has_status projection: the build plan (Appendix B, finding 5) designs a
-- `has_status` kg_edges projection from this column and no wave builds it.
-- That gap is NOT closed here -- this table's `status` column is the
-- authoritative source and the graph projection is UNBUILT. Declared, not
-- silent.
--
-- Idempotent DDL -- there is no migration ledger in this repo; schema.sql and
-- every migrations/*.sql file re-run on every boot under an advisory lock.
-- ============================================================================

CREATE TABLE IF NOT EXISTS bom_line_content (
    id                 UUID          NOT NULL DEFAULT gen_random_uuid(),
    namespace_id       UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    bom_line_label     TEXT          NOT NULL,
    quote_id           TEXT          NOT NULL,
    line_ref           TEXT          NOT NULL,
    qty                NUMERIC(14,4) NOT NULL,
    unit_price         NUMERIC(18,2) NOT NULL,
    line_total         NUMERIC(18,2) NOT NULL,
    currency           CHAR(3)       NOT NULL DEFAULT 'NOK',
    -- Open-by-construction provenance (no CHECK) -- see header comment.
    -- Trust boundary: set ONLY by nce/bom_lines.py's own flow-to-origin_kind
    -- mapping, never from a caller-supplied tool argument.
    origin_kind        TEXT          NOT NULL,
    origin_ref         TEXT,
    writer_engine      TEXT          NOT NULL,
    status             TEXT          NOT NULL DEFAULT 'DRAFT',
    status_changed_at  TIMESTAMPTZ,
    -- Immutable once set -- enforced by the trigger below, not by DDL alone
    -- (a CHECK cannot see OLD).
    frozen_at          TIMESTAMPTZ,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

-- Every CHECK/UNIQUE is explicitly named -- an anonymous column CHECK is
-- auto-named on one path (CREATE TABLE) and can diverge from the name a
-- second path (e.g. a later ALTER) would pick, which is exactly the
-- fresh-install-vs-migrated divergence that caused a prior rejection on this
-- table family (Batch 132's own history).
ALTER TABLE bom_line_content DROP CONSTRAINT IF EXISTS bom_line_content_natural_key;
ALTER TABLE bom_line_content
    ADD CONSTRAINT bom_line_content_natural_key UNIQUE (namespace_id, bom_line_label);

ALTER TABLE bom_line_content DROP CONSTRAINT IF EXISTS bom_line_content_qty_positive_chk;
ALTER TABLE bom_line_content
    ADD CONSTRAINT bom_line_content_qty_positive_chk CHECK (qty > 0);

ALTER TABLE bom_line_content DROP CONSTRAINT IF EXISTS bom_line_content_unit_price_nonneg_chk;
ALTER TABLE bom_line_content
    ADD CONSTRAINT bom_line_content_unit_price_nonneg_chk CHECK (unit_price >= 0);

ALTER TABLE bom_line_content DROP CONSTRAINT IF EXISTS bom_line_content_line_total_nonneg_chk;
ALTER TABLE bom_line_content
    ADD CONSTRAINT bom_line_content_line_total_nonneg_chk CHECK (line_total >= 0);

-- Non-unique: supports both the per-line lookup (namespace_id + bom_line_label,
-- covered by the natural key above) and the per-quote listing/read
-- (namespace_id + quote_id) that 132f and Batch 142 will use.
CREATE INDEX IF NOT EXISTS idx_bom_line_content_namespace_quote
    ON bom_line_content (namespace_id, quote_id);

-- ----------------------------------------------------------------------------
-- Freeze semantics: a BEFORE UPDATE trigger, not a GRANT and not the registry.
-- status / status_changed_at are deliberately OUTSIDE the protected set --
-- content freezes, status keeps advancing. frozen_at itself is immutable
-- once set, independent of the content freeze check.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION reject_frozen_bom_line_mutation() RETURNS TRIGGER AS $BODY$
BEGIN
    IF OLD.frozen_at IS NOT NULL AND NEW.frozen_at IS DISTINCT FROM OLD.frozen_at THEN
        RAISE EXCEPTION
            'bom_line_content.frozen_at is immutable once set (label=%)', OLD.bom_line_label;
    END IF;

    IF OLD.frozen_at IS NOT NULL THEN
        IF NEW.quote_id       IS DISTINCT FROM OLD.quote_id
        OR NEW.line_ref       IS DISTINCT FROM OLD.line_ref
        OR NEW.qty            IS DISTINCT FROM OLD.qty
        OR NEW.unit_price     IS DISTINCT FROM OLD.unit_price
        OR NEW.line_total     IS DISTINCT FROM OLD.line_total
        OR NEW.currency       IS DISTINCT FROM OLD.currency
        OR NEW.origin_kind    IS DISTINCT FROM OLD.origin_kind
        OR NEW.origin_ref     IS DISTINCT FROM OLD.origin_ref
        OR NEW.writer_engine  IS DISTINCT FROM OLD.writer_engine
        THEN
            RAISE EXCEPTION
                'bom_line_content: content is frozen and cannot be mutated (label=%)',
                OLD.bom_line_label;
        END IF;
    END IF;

    RETURN NEW;
END;
$BODY$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_reject_frozen_bom_line_mutation ON bom_line_content;
CREATE TRIGGER trg_reject_frozen_bom_line_mutation
    BEFORE UPDATE ON bom_line_content
    FOR EACH ROW EXECUTE FUNCTION reject_frozen_bom_line_mutation();

ALTER TABLE bom_line_content ENABLE ROW LEVEL SECURITY;
ALTER TABLE bom_line_content FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON bom_line_content;
CREATE POLICY tenant_isolation_policy ON bom_line_content
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE bom_line_content FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE bom_line_content TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE bom_line_content IS
'Shared, top-level BOM_LINE content store (Module 0, Wave 31 / Batch 132a).
Written by system_design (content:create:design, content:update:design) and
by sales (content:create:manual/package/external,
content:update:manual/package/external, content:freeze) -- guarded per-flow
by node_ownership_registry via nce/bom_lines.py, never engine-prefixed
because both engines write it. Natural-keyed (namespace_id, bom_line_label);
INSERT ... ON CONFLICT DO NOTHING makes a replay of the same
(namespace, label) a no-op by construction. Content (qty/unit_price/
line_total/currency/origin_*) freezes via trg_reject_frozen_bom_line_mutation
once frozen_at is set (from do_freeze_baseline, sales/baseline.py); status
stays mutable after freeze -- see the header comment for the full field-
ownership split. actual_cost is NOT this table''s column -- see
economy_bom_actual_costs (migration 047). FORCE RLS isolates per tenant;
every query must ALSO carry an explicit namespace_id predicate because the
owner/superuser pool used by background jobs bypasses FORCE RLS (bitten three
prior waves: B67, B120, B130).';
