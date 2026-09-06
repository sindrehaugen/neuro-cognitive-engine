"""Agreements Engine vertical module (Module 3).

Exports public core functions for agreement lifecycle authoring, extraction,
review, coverage/gap matrix, kickback reconciliation, compliance audit,
signing orchestration, and SLA coverage.
"""

from __future__ import annotations

from nce.vertical_modules.agreements._guard import (
    AgreementsDisabledError,
    require_agreements_enabled,
)
from nce.vertical_modules.agreements.authoring import (
    do_add_comment,
    do_create_agreement,
    do_suggest_revision,
)
from nce.vertical_modules.agreements.compliance import (
    do_run_compliance_audit,
    do_suggest_terms,
)
from nce.vertical_modules.agreements.coverage import do_coverage_matrix
from nce.vertical_modules.agreements.extract import do_extract_agreement
from nce.vertical_modules.agreements.graph import do_upsert_agreement
from nce.vertical_modules.agreements.kickback import do_reconcile_kickback
from nce.vertical_modules.agreements.review import do_review_extraction
from nce.vertical_modules.agreements.signing import (
    do_record_signature,
    do_request_signature,
)
from nce.vertical_modules.agreements.sla import do_set_sla_coverage

__all__ = [
    "AgreementsDisabledError",
    "do_add_comment",
    "do_coverage_matrix",
    "do_create_agreement",
    "do_extract_agreement",
    "do_reconcile_kickback",
    "do_record_signature",
    "do_request_signature",
    "do_review_extraction",
    "do_run_compliance_audit",
    "do_set_sla_coverage",
    "do_suggest_revision",
    "do_suggest_terms",
    "do_upsert_agreement",
    "require_agreements_enabled",
]
