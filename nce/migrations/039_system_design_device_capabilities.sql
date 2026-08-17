-- 039_system_design_device_capabilities.sql
-- Capability table for System Design Phase-2 device model.
--
-- Context / design decisions
-- ~~~~~~~~~~~~~~~~~~~~~~~~~~
-- Phase-2 adds DEVICE/PORT/SIGNAL_CHAIN/RACK/CABLE topology to the graph
-- (hung off the DESIGN node as kg_nodes/kg_edges).  kg_nodes has NO payload
-- column; typed capability attributes (HDMI version, PoE class, Dante channel
-- count, power draw, heat/BTU, redundancy role) cannot live there.  This table
-- is the queryable typed store for those attributes, keyed by (namespace_id,
-- node_label) which is the same natural key used by kg_nodes.
--
-- AVIXA AV Device Revit Parameter List (Phase 2.1) drives the column schema:
--   - signal_format / signal_version  → port signal type + standard version
--   - port_direction                  → input | output | bidirectional
--   - poe_class                       → IEEE 802.3 PoE class (0–8)
--   - poe_watts                       → max watts drawn/sourced
--   - dante_rx_channels               → Dante receive channel count
--   - dante_tx_channels               → Dante transmit channel count
--   - power_draw_watts                → total device power consumption
--   - heat_btu_hr                     → heat dissipation (BTU/hr)
--   - redundancy_role                 → primary | secondary | standalone
--   - device_category                 → AVIXA Communication Devices category
--   - manufacturer                    → device manufacturer
--   - model_number                    → manufacturer part / model number
--   - extra                           → JSONB escape hatch for future AVIXA params
--
-- RLS
-- ~~~
-- ENABLE + FORCE ROW LEVEL SECURITY with tenant_isolation_policy scoped by
-- get_nce_namespace() — exactly mirrors the procurement_bid_prices pattern.
--
-- Idempotent DDL throughout (IF NOT EXISTS / DO $$ … $$).
-- ============================================================================

CREATE TABLE IF NOT EXISTS system_design_device_capabilities (
    id              UUID        NOT NULL DEFAULT gen_random_uuid(),
    namespace_id    UUID        NOT NULL REFERENCES namespaces(id) ON DELETE CASCADE,

    -- Graph key — matches (label, namespace_id) in kg_nodes.
    node_label      TEXT        NOT NULL,

    -- AVIXA Revit Parameter List: port-level signal attributes.
    signal_format   TEXT,           -- e.g. 'HDMI', 'DP', 'Dante', 'SDI', 'AES67'
    signal_version  TEXT,           -- e.g. '2.1', '2.0', '1.4', '1.2'
    port_direction  TEXT            -- 'input' | 'output' | 'bidirectional'
        CHECK (port_direction IS NULL OR port_direction IN ('input', 'output', 'bidirectional')),

    -- AVIXA: Power over Ethernet parameters.
    poe_class       SMALLINT,       -- IEEE 802.3 PoE class 0–8; NULL = no PoE
    poe_watts       NUMERIC,        -- max watts drawn (consumer) or sourced (PSE)

    -- AVIXA: Dante/AES67 networked audio channel counts.
    dante_rx_channels  SMALLINT,    -- Dante receive channels; NULL = no Dante
    dante_tx_channels  SMALLINT,    -- Dante transmit channels; NULL = no Dante

    -- AVIXA: Device power and thermal budget.
    power_draw_watts NUMERIC,       -- total device power draw (watts)
    heat_btu_hr      NUMERIC,       -- heat dissipation (BTU/hr)

    -- AVIXA: Redundancy role for SPOF analysis.
    redundancy_role TEXT            -- 'primary' | 'secondary' | 'standalone'
        CHECK (redundancy_role IS NULL OR redundancy_role IN ('primary', 'secondary', 'standalone')),

    -- AVIXA: Device classification.
    device_category TEXT,           -- AVIXA 'Communication Devices' or sub-category
    manufacturer    TEXT,           -- manufacturer name
    model_number    TEXT,           -- manufacturer part / model number

    -- Escape hatch for future AVIXA parameter extensions.
    extra           JSONB           NOT NULL DEFAULT '{}'::jsonb,

    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

    PRIMARY KEY (id),
    UNIQUE (namespace_id, node_label)
);

-- Index: primary read path (node_label lookup within a namespace).
CREATE INDEX IF NOT EXISTS idx_sddc_namespace_node_label
    ON system_design_device_capabilities (namespace_id, node_label);

-- Index: signal_format queries (port/format compatibility check).
CREATE INDEX IF NOT EXISTS idx_sddc_namespace_signal_format
    ON system_design_device_capabilities (namespace_id, signal_format)
    WHERE signal_format IS NOT NULL;

-- Index: redundancy_role queries (SPOF check).
CREATE INDEX IF NOT EXISTS idx_sddc_namespace_redundancy_role
    ON system_design_device_capabilities (namespace_id, redundancy_role)
    WHERE redundancy_role IS NOT NULL;

-- Row-Level Security.
ALTER TABLE system_design_device_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_design_device_capabilities FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation_policy ON system_design_device_capabilities;
CREATE POLICY tenant_isolation_policy ON system_design_device_capabilities
    FOR ALL TO nce_app
    USING  (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace())
    WITH CHECK (namespace_id IS NOT NULL AND namespace_id = get_nce_namespace());

-- Application role grants.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        REVOKE ALL ON TABLE system_design_device_capabilities FROM nce_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE system_design_device_capabilities TO nce_app;
    END IF;
END $$;

COMMENT ON TABLE system_design_device_capabilities IS
'Phase-2 device capability attributes for the System Design engine.
Keyed by (namespace_id, node_label) — the node_label matches kg_nodes.label.
Column schema follows the AVIXA AV Device Revit Parameter List (Phase 2.1).
kg_nodes has no payload column; typed/queryable capability fields live here.
FORCE RLS isolates per tenant (mirrors procurement_bid_prices pattern).';
