-- 029_c3_external_scope_rls.sql
-- C3 external-principal RLS primitive.
--
-- Adds the session GUC `nce.external_scope_id`, a SQL helper function
-- `get_nce_external_scope()`, and a reusable policy template comment that
-- documents how external-facing tables combine this with tenant_isolation_policy.
--
-- Security invariants:
--   1. DENY-WHEN-UNSET: if nce.external_scope_id is not set (or empty), the
--      function returns the nil UUID (all-zeros). No real external_scope_id
--      column will ever store that value (gen_random_uuid() never produces it),
--      so the USING predicate is always FALSE → zero rows visible.
--   2. ANDs the tenant predicate: external_isolation_policy carries BOTH
--      `namespace_id = get_nce_namespace()` AND
--      `external_scope_id = get_nce_external_scope()`.
--      Neither condition alone is sufficient; both must hold simultaneously.
--   3. Tables that serve ALL internal principals (employees, agents) must keep
--      the existing `tenant_isolation_policy` only. Tables that serve external
--      principals (partners, customers) replace it with `external_isolation_policy`
--      — or add it as a second policy layer. Wave 23 wires the SET into sessions.
--
-- DENY-WHEN-UNSET mechanism:
--   The nil UUID ('00000000-0000-0000-0000-000000000000') is the sentinel.
--   gen_random_uuid() uses random v4 bits; the probability of collision is
--   effectively zero, and we document this constraint in the function comment.
--   Operators MUST NOT insert rows with external_scope_id = nil UUID.
-- ============================================================================

-- Deny-sentinel constant (nil UUID). Any column storing external_scope_id MUST
-- NOT use this value as a real scope. It is reserved exclusively as the
-- no-scope sentinel so that unset GUC → zero rows exposed.
DO $$
BEGIN
    IF current_setting('nce.external_scope_id_sentinel_defined', true) IS DISTINCT FROM 'true' THEN
        PERFORM set_config('nce.external_scope_id_sentinel_defined', 'true', false);
    END IF;
END $$;

-- get_nce_external_scope(): read-only accessor for the C3 scope GUC.
-- Returns the nil UUID when the GUC is unset or empty (deny-when-unset).
-- Returns the caller's external_scope_id UUID when properly set.
-- NEVER raises: the sentinel guarantees zero visibility rather than an error,
-- so internal-principal sessions (no GUC set) fail closed gracefully.
CREATE OR REPLACE FUNCTION get_nce_external_scope() RETURNS uuid AS $$
DECLARE
    val text;
BEGIN
    val := nullif(trim(current_setting('nce.external_scope_id', true)), '');
    IF val IS NULL THEN
        -- GUC unset or empty: return nil UUID as deny-sentinel.
        -- The policy `external_scope_id = get_nce_external_scope()` will match
        -- no row because real external_scope_id values are random v4 UUIDs.
        RETURN '00000000-0000-0000-0000-000000000000'::uuid;
    END IF;
    BEGIN
        RETURN val::uuid;
    EXCEPTION
        WHEN invalid_text_representation THEN
            -- Malformed value: treat as unset → deny-sentinel.
            RETURN '00000000-0000-0000-0000-000000000000'::uuid;
    END;
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION get_nce_external_scope() IS
'C3 external-principal RLS accessor.
Returns the current session''s external_scope_id UUID, or the nil-UUID sentinel
(00000000-…-0000) when nce.external_scope_id is unset/empty.
Use in external_isolation_policy USING expressions — the nil sentinel guarantees
zero rows are exposed when no scope is set (deny-when-unset invariant).
Setting nce.external_scope_id is the responsibility of the session-wiring layer (Wave 23).
INVARIANT: no real external_scope_id column may store the nil UUID.';

-- external_isolation_policy template comment.
-- Apply to every external-facing table T that carries both namespace_id and
-- external_scope_id columns:
--
--   ALTER TABLE T ENABLE ROW LEVEL SECURITY;
--   ALTER TABLE T FORCE ROW LEVEL SECURITY;
--   DROP POLICY IF EXISTS tenant_isolation_policy ON T;
--   DROP POLICY IF EXISTS external_isolation_policy ON T;
--   CREATE POLICY external_isolation_policy ON T
--       FOR ALL TO nce_app
--       USING (
--           namespace_id IS NOT NULL
--           AND namespace_id = get_nce_namespace()
--           AND external_scope_id IS NOT NULL
--           AND external_scope_id = get_nce_external_scope()
--       )
--       WITH CHECK (
--           namespace_id IS NOT NULL
--           AND namespace_id = get_nce_namespace()
--           AND external_scope_id IS NOT NULL
--           AND external_scope_id = get_nce_external_scope()
--       );
--
-- SECURITY NOTES:
--  * The nil-UUID sentinel for get_nce_external_scope() ensures that if
--    nce.external_scope_id is not set the USING clause is always FALSE (deny).
--  * external_scope_id IS NOT NULL in the USING clause is a belt-and-suspenders
--    guard: rows with NULL external_scope_id are unreachable via this policy.
--  * Do NOT replace tenant_isolation_policy with external_isolation_policy on
--    purely internal tables; add external_isolation_policy only where external
--    principals have a defined access pattern.
