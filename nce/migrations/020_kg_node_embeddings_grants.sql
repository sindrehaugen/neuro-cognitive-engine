-- Migration: Grant SELECT, INSERT, UPDATE, DELETE on kg_node_embeddings to nce_app and DELETE on pii_redactions
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nce_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings TO nce_app;
        IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'kg_node_embeddings_0') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings_0 TO nce_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings_1 TO nce_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings_2 TO nce_app;
            GRANT SELECT, INSERT, UPDATE, DELETE ON kg_node_embeddings_3 TO nce_app;
        END IF;
        
        GRANT DELETE ON pii_redactions TO nce_app;
        IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'pii_redactions_default') THEN
            GRANT DELETE ON pii_redactions_default TO nce_app;
        END IF;
    END IF;
END $$;
