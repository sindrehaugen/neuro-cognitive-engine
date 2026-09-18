"""nce.vertical_modules.legal_entities — C15 Legal-Entity Register module.

Phase A Wave A-5:
Provides the shared-core legal entity register, organization number normalization,
company roles, and group structures.
"""

from __future__ import annotations

from nce.vertical_modules.legal_entities.models import LegalEntityRecord
from nce.vertical_modules.legal_entities.resources import LEGAL_ENTITY_SPEC
from nce.vertical_modules.legal_entities.service import (
    add_role_to_legal_entity,
    archive_legal_entity,
    get_legal_entity,
    get_legal_entity_by_org_nr,
    list_legal_entities,
    register_legal_entity,
    update_legal_entity,
)

__all__ = [
    "LEGAL_ENTITY_SPEC",
    "LegalEntityRecord",
    "register_legal_entity",
    "get_legal_entity",
    "get_legal_entity_by_org_nr",
    "list_legal_entities",
    "update_legal_entity",
    "archive_legal_entity",
    "add_role_to_legal_entity",
]
