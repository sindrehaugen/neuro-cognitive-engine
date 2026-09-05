"""
Module 14: The Marketing Engine.
Grounded case study generation, testimonial consent management, and AEO/GEO publication.
"""

from __future__ import annotations

from nce.vertical_modules.marketing._guard import (
    MarketingConsentMissingError,
    MarketingDisabledError,
    MarketingLowHealthTriggerError,
    MarketingSensitiveDataLeakError,
    MarketingUnapprovedPublishError,
    MarketingUngroundedClaimError,
    require_marketing_enabled,
)
from nce.vertical_modules.marketing.candidates import do_find_case_study_candidates
from nce.vertical_modules.marketing.drafting import (
    do_draft_case_study,
    validate_draft_grounding,
)
from nce.vertical_modules.marketing.redaction import redact_for_marketing_draft
from nce.vertical_modules.marketing.taxonomy import DEFAULT_BRAND_VOICE

__all__ = [
    "DEFAULT_BRAND_VOICE",
    "MarketingConsentMissingError",
    "MarketingDisabledError",
    "MarketingLowHealthTriggerError",
    "MarketingSensitiveDataLeakError",
    "MarketingUnapprovedPublishError",
    "MarketingUngroundedClaimError",
    "do_draft_case_study",
    "do_find_case_study_candidates",
    "redact_for_marketing_draft",
    "require_marketing_enabled",
    "validate_draft_grounding",
]
