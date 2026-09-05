-- Migration 071: Module 15 Staff & Resources Engine - Allocations, Travel Legs, Stays, Per Diems
-- Enforces concurrency conflict exclusion via PostgreSQL btree_gist extension.

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- 1. allocations table
CREATE TABLE IF NOT EXISTS allocations (
    id                     UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id           UUID             NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    resource_id            UUID             NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    demand_kind            VARCHAR(64)      NOT NULL,
    demand_id              UUID,
    functional_location_id UUID,
    starts_at              TIMESTAMPTZ      NOT NULL,
    ends_at                TIMESTAMPTZ      NOT NULL,
    status                 VARCHAR(32)      NOT NULL DEFAULT 'reserved',
    confidence             DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    attrs                  JSONB            NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT check_allocation_dates CHECK (ends_at > starts_at),
    CONSTRAINT exclude_resource_double_booking EXCLUDE USING gist (
        resource_id WITH =,
        tstzrange(starts_at, ends_at) WITH &&
    ) WHERE (status <> 'released')
);

CREATE INDEX IF NOT EXISTS idx_allocations_tenant_res ON allocations (namespace_id, resource_id, starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_allocations_tenant_demand ON allocations (namespace_id, demand_kind, demand_id);

ALTER TABLE allocations ENABLE ROW LEVEL SECURITY;
ALTER TABLE allocations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON allocations;
CREATE POLICY tenant_isolation_policy ON allocations
    FOR ALL
    USING (namespace_id = get_nce_namespace());

GRANT SELECT, INSERT, UPDATE, DELETE ON allocations TO nce_app;

-- 2. travel_legs table
CREATE TABLE IF NOT EXISTS travel_legs (
    id             UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id   UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    allocation_id  UUID          NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
    origin         VARCHAR(255)  NOT NULL,
    destination    VARCHAR(255)  NOT NULL,
    departure_at   TIMESTAMPTZ   NOT NULL,
    arrival_at     TIMESTAMPTZ,
    mode           VARCHAR(64)   NOT NULL DEFAULT 'flight',
    cost_nok       NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    booking_ref    VARCHAR(128),
    status         VARCHAR(32)   NOT NULL DEFAULT 'planned',
    attrs          JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_travel_legs_tenant_alloc ON travel_legs (namespace_id, allocation_id);

ALTER TABLE travel_legs ENABLE ROW LEVEL SECURITY;
ALTER TABLE travel_legs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON travel_legs;
CREATE POLICY tenant_isolation_policy ON travel_legs
    FOR ALL
    USING (namespace_id = get_nce_namespace());

GRANT SELECT, INSERT, UPDATE, DELETE ON travel_legs TO nce_app;

-- 3. stays table
CREATE TABLE IF NOT EXISTS stays (
    id             UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id   UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    allocation_id  UUID          NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
    location       VARCHAR(255)  NOT NULL,
    check_in       TIMESTAMPTZ   NOT NULL,
    check_out      TIMESTAMPTZ   NOT NULL,
    cost_nok       NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    booking_ref    VARCHAR(128),
    status         VARCHAR(32)   NOT NULL DEFAULT 'planned',
    attrs          JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT check_stay_dates CHECK (check_out > check_in)
);

CREATE INDEX IF NOT EXISTS idx_stays_tenant_alloc ON stays (namespace_id, allocation_id);

ALTER TABLE stays ENABLE ROW LEVEL SECURITY;
ALTER TABLE stays FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON stays;
CREATE POLICY tenant_isolation_policy ON stays
    FOR ALL
    USING (namespace_id = get_nce_namespace());

GRANT SELECT, INSERT, UPDATE, DELETE ON stays TO nce_app;

-- 4. per_diems table
CREATE TABLE IF NOT EXISTS per_diems (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace_id    UUID          NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,
    allocation_id   UUID          NOT NULL REFERENCES allocations(id) ON DELETE CASCADE,
    date            DATE          NOT NULL,
    rate_nok        NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    diet_type       VARCHAR(64)   NOT NULL DEFAULT 'statutory_overnight',
    meals_provided  JSONB         NOT NULL DEFAULT '{"breakfast": false, "lunch": false, "dinner": false}'::jsonb,
    attrs           JSONB         NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_per_diems_tenant_alloc ON per_diems (namespace_id, allocation_id);

ALTER TABLE per_diems ENABLE ROW LEVEL SECURITY;
ALTER TABLE per_diems FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON per_diems;
CREATE POLICY tenant_isolation_policy ON per_diems
    FOR ALL
    USING (namespace_id = get_nce_namespace());

GRANT SELECT, INSERT, UPDATE, DELETE ON per_diems TO nce_app;
