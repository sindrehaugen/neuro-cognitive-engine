"""Backward-compatible facade; implementation lives in ``nce.admin_handlers``."""

from nce.admin_handlers import *  # noqa: F403
from nce.admin_handlers._shared import (  # noqa: F401 — patch targets for tests
    cfg,
    fetch_fleet_overview_page,
    fetch_pg_rls_snapshot,
    logger,
    update_dotenv,
)
