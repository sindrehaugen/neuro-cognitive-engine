-- 082_schema_sql_only_rls_baseline.sql
--
-- Q-4 step 1b: give the ledger a path to the RLS that only ``schema.sql`` provides.
--
-- WHY THIS EXISTS, AND WHY IT IS SEPARATE FROM 081
-- -----------------------------------------------
-- 081 covered the ten columns that live only in ``schema.sql``. That was not enough, and
-- the gap was in the checker's own scope rather than in the data. Measured on ``f2dba69``:
--
--     CREATE POLICY   schema.sql=50  migrations=65  only in schema.sql=4
--     ENABLE RLS      schema.sql=49  migrations=64  only in schema.sql=4
--
-- The four are ``outbox_events``, ``replay_runs``, ``saga_execution_log`` and
-- ``topology_graph``. Their tenant-isolation policy AND their row-level-security
-- enablement exist in ``schema.sql`` and in no migration at all.
--
-- That matters more than the columns did. If Q-4 step 2 lands and ``schema.sql`` stops
-- running on an existing database, a missing column eventually raises an error at query
-- time. A missing policy raises nothing: it is one tenant reading another tenant's rows,
-- silently, with every test still green. The column-only checker reported such a database
-- as clean, which is why ``check_schema_drift.py`` now compares RLS too.
--
-- This migration is a no-op on the live database -- verified 2026-09-17, all four tables
-- had ``relrowsecurity = true`` and exactly one policy each. It is a baseline, not a
-- change. **Step 2 must not land before this has.**
--
-- ON REPRODUCING RATHER THAN IMPROVING
-- ------------------------------------
-- ``topology_graph``'s policy is visibly weaker than the other three: no ``TO nce_app``,
-- no ``WITH CHECK``, and no ``IS NOT NULL`` guard. Without ``WITH CHECK``, the policy
-- constrains reads but not writes -- a caller can INSERT a row carrying another
-- namespace's id.
--
-- This migration reproduces it **exactly as schema.sql has it, weakness included.** A
-- baseline that silently tightens a security predicate is a behaviour change wearing a
-- no-op's clothing, and it would land without anyone deciding it. The gap is filed as its
-- own row so it gets argued on its merits.
--
-- Note also that three of the four grant ``TO nce_app`` -- a role nothing currently
-- connects as. The deployed role is ``mcp_user``, which is ``rolsuper``/``rolbypassrls``,
-- so these policies do not constrain the running system today either way. That is the
-- standing RLS-posture finding, not something this migration changes.

BEGIN;

-- --------------------------------------------------------------------------
-- outbox_events
-- --------------------------------------------------------------------------
ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'outbox_events' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON outbox_events
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- --------------------------------------------------------------------------
-- replay_runs  (scopes on source_namespace_id, not namespace_id)
-- --------------------------------------------------------------------------
ALTER TABLE replay_runs ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'replay_runs' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON replay_runs
        FOR ALL TO nce_app
        USING (
            source_namespace_id IS NOT NULL
            AND source_namespace_id = get_nce_namespace()
        )
        WITH CHECK (
            source_namespace_id IS NOT NULL
            AND source_namespace_id = get_nce_namespace()
        );
    END IF;
END $$;

-- --------------------------------------------------------------------------
-- saga_execution_log
-- --------------------------------------------------------------------------
ALTER TABLE saga_execution_log ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'saga_execution_log' AND p.polname = 'tenant_isolation_policy'
    ) THEN
        CREATE POLICY tenant_isolation_policy ON saga_execution_log
        FOR ALL TO nce_app
        USING (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
        WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());
    END IF;
END $$;

-- --------------------------------------------------------------------------
-- topology_graph
--
-- Reproduced verbatim from schema.sql:2288, including the missing WITH CHECK. See the
-- header: tightening it here would be an undeclared security change.
-- --------------------------------------------------------------------------
ALTER TABLE topology_graph ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        WHERE c.relname = 'topology_graph' AND p.polname = 'topology_graph_tenant_isolation'
    ) THEN
        CREATE POLICY topology_graph_tenant_isolation ON topology_graph
        FOR ALL
        USING (namespace_id = get_nce_namespace());
    END IF;
END $$;

COMMIT;
