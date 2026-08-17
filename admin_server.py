# ruff: noqa: F401, E402, I001
from __future__ import annotations

import logging

from nce import admin_state
from nce.admin_app import app
from nce.admin_http_support import (
    admin_error_response as _admin_error_response,  # noqa: F401 — re-export for tests
    update_dotenv,  # noqa: F401 — re-export for tests
)

logging.basicConfig(level=logging.INFO)

# Backward compatibility for tests patching admin_server.engine
engine = admin_state.engine


if __name__ == "__main__":
    import uvicorn
    from nce.config import assert_admin_override_not_in_production

    assert_admin_override_not_in_production()
    uvicorn.run(app, host="0.0.0.0", port=8003)

# Re-export handlers for existing tests
from nce.admin_http_handlers import (
    api_admin_salience_map,
    api_admin_llm_payload,
    api_admin_fleet_overview,
    api_admin_bridge_renew,
    api_admin_memory_boost,
    api_admin_contradictions_recent,
    api_admin_namespace_bridges,
    api_admin_events,
    api_admin_datastores_status,
    api_admin_datastores_save,
    api_admin_db_postgres_status,
    api_admin_db_mongo_status,
    api_admin_db_redis_status,
    api_admin_db_minio_status,
    api_admin_connectors_status,
    api_admin_namespaces_list,
    api_admin_namespaces_get,
    api_admin_namespaces_update_metadata,
    api_admin_dlq_list,
    api_admin_quotas,
    api_admin_signing_status,
    api_admin_pii_redactions_list,
    api_admin_security_event_seq_gaps,
    api_admin_security_verify_memory_sample,
    api_admin_security_test_rls_isolation,
    api_admin_verify_chain,
    trigger_gc,
    api_search,
)
