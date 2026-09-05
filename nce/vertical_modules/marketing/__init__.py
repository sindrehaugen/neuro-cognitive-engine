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
from nce.vertical_modules.marketing.approval import do_approve_content
from nce.vertical_modules.marketing.candidates import do_find_case_study_candidates
from nce.vertical_modules.marketing.drafting import (
    do_draft_case_study,
    validate_draft_grounding,
)
from nce.vertical_modules.marketing.publish import (
    PublishTransport,
    do_publish_content,
)
from nce.vertical_modules.marketing.redaction import redact_for_marketing_draft
from nce.vertical_modules.marketing.taxonomy import DEFAULT_BRAND_VOICE
from nce.vertical_modules.marketing.testimonials import (
    do_capture_testimonial,
    do_request_testimonial,
    do_retract_testimonial,
)

__all__ = [
    "DEFAULT_BRAND_VOICE",
    "MarketingConsentMissingError",
    "MarketingDisabledError",
    "MarketingLowHealthTriggerError",
    "MarketingSensitiveDataLeakError",
    "MarketingUnapprovedPublishError",
    "MarketingUngroundedClaimError",
    "PublishTransport",
    "do_approve_content",
    "do_capture_testimonial",
    "do_draft_case_study",
    "do_find_case_study_candidates",
    "do_publish_content",
    "do_request_testimonial",
    "do_retract_testimonial",
    "redact_for_marketing_draft",
    "require_marketing_enabled",
    "validate_draft_grounding",
]
