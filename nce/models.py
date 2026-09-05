"""
NCE Phase 0.1 — Pydantic V2 data models.

This is the single source of truth for all request/response shapes and internal
record types for Phase 0.1 (Multi-Tenant Namespacing + Postgres RLS).

Decisions encoded:
  [D4]  agent_id: free-text, stripped whitespace, max 128 chars, safe-ID pattern.
        Default 'default'. No FK, no agents table in v1.
  [D5]  temporal_retention_days: int | None per namespace (90 day default; None = infinite).
  [D6]  llm_payload_retention_days: int | None (None = inherits D5; 0 = no caching).
  [D8]  valid_from: ALWAYS server-assigned (now()). Any user-supplied past timestamp is
        rejected at the boundary. This model enforces that: valid_from is not an input field.
  [D9]  assertion_type: Literal['fact','opinion','preference','observation'].
        Contradiction detection fires only on fact vs fact.

Do NOT add raw DB connection or pool logic here. This module is import-only.
"""

from __future__ import annotations

import re
import sys
import uuid
from datetime import datetime, timezone

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from strenum import StrEnum  # type: ignore[import-untyped]
from typing import Any, Literal, TypedDict

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from nce.mcp_args import SafeMetadataDict

# ── Constants ─────────────────────────────────────────────────────────────────

# Namespace slugs: lowercase alphanumeric + hyphens, 2–64 chars,
# must start and end with alphanumeric.
_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$")

# agent_id / legacy user_id / session_id: alphanumeric, hyphens, underscores, 1–128 chars.
_SAFE_ID_RE = re.compile(r"^[\w\-]{1,128}$")

_MAX_SUMMARY_LEN: int = 8_192
_MAX_PAYLOAD_LEN: int = 10 * 1024 * 1024  # 10 MB hard cap [GLOBAL CONSTRAINT]
_MAX_TOP_K: int = 100
MAX_GRAPH_DEPTH: int = 3
# Subgraph / GraphRAG — keep max_edges_per_node default aligned with ``nce.graph_query.MAX_EDGES_PER_NODE``.
_MAX_GRAPH_EDGES_PER_NODE: int = 2048
MAX_GRAPH_EDGE_PAGE: int = 1000
_MAX_CONTENT_LEN: int = 1_000_000  # 1 MB of text for embedding
_MAX_QUERY_LEN: int = 10_000  # search and graph query strings


# ── Shared helpers ────────────────────────────────────────────────────────────


def _validate_agent_id(v: str) -> str:
    """[D4] Strip whitespace, enforce max 128 chars and safe-ID charset."""
    v = v.strip()
    if not v:
        raise ValueError("agent_id must not be empty or whitespace-only")
    if len(v) > 128:
        raise ValueError("agent_id must be ≤ 128 characters")
    if not _SAFE_ID_RE.match(v):
        raise ValueError("agent_id may only contain alphanumerics, hyphens, and underscores")
    return v


# ── Enumerations ──────────────────────────────────────────────────────────────


class AssertionType(StrEnum):
    """[D9] Fact-typing used for contradiction detection and memory classification."""

    fact = "fact"
    opinion = "opinion"
    preference = "preference"
    observation = "observation"


class MemoryType(StrEnum):
    """Classification of memory entries stored in the memories table."""

    episodic = "episodic"
    consolidated = "consolidated"
    decision = "decision"
    code_chunk = "code_chunk"


class PIIPolicy(StrEnum):
    """[Phase 0.3] Per-namespace PII handling policy."""

    redact = "redact"
    pseudonymise = "pseudonymise"
    reject = "reject"
    flag = "flag"


class SigningKeyStatus(StrEnum):
    """[Phase 0.2] Signing key lifecycle. Retired keys retained for historical verify."""

    active = "active"
    retired = "retired"


class ChangeOrigin(StrEnum):
    """[Batch C0] Origin of changes on memories/KG entities."""

    sync = "sync"
    webhook = "webhook"
    agent = "agent"
    operator = "operator"
    consolidation = "consolidation"
    replay = "replay"
    unknown = "unknown"


class ApprovalStatus(StrEnum):
    """[Batch C0] Status of approval queue actions."""

    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    executed = "executed"
    expired = "expired"


class ActorKind(StrEnum):
    """[Batch C0] Kind of actors participating in the trust model."""

    agent = "agent"
    operator = "operator"


class ManageNamespaceCommand(StrEnum):
    """Commands accepted by the manage_namespace MCP admin tool."""

    create = "create"
    list = "list"
    grant = "grant"
    revoke = "revoke"
    update_metadata = "update_metadata"
    delete = "delete"


class ManageQuotasCommand(StrEnum):
    """Commands accepted by the manage_quotas MCP admin tool (Phase 3.2)."""

    set = "set"
    list = "list"
    delete = "delete"
    reset = "reset"


class ManageQuotasRequest(BaseModel):
    """Input for the manage_quotas MCP admin tool (Phase 3.2)."""

    model_config = ConfigDict(extra="forbid")

    command: ManageQuotasCommand
    namespace_id: UUID4
    agent_id: str | None = None
    resource_type: str | None = None
    limit: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _command_constraints(self) -> ManageQuotasRequest:
        cmd = self.command
        if cmd == ManageQuotasCommand.set:
            if self.resource_type is None or self.limit is None:
                raise ValueError("resource_type and limit are required for command='set'")
        if cmd == ManageQuotasCommand.delete or cmd == ManageQuotasCommand.reset:
            if self.resource_type is None:
                raise ValueError("resource_type is required for command='delete'/'reset'")
        return self


# ── Namespace sub-models ──────────────────────────────────────────────────────


class NamespaceCognitiveConfig(BaseModel):
    """
    Cognitive settings nested inside NamespaceMetadata.
    Consumed in Phase 1.1 (Ebbinghaus Decay + Salience).
    """

    model_config = ConfigDict(extra="forbid")

    half_life_days: float = Field(default=30.0, gt=0.0)
    reinforcement_delta: float = Field(default=0.05, gt=0.0, le=1.0)
    alpha: float = Field(default=0.7, ge=0.0, le=1.0)


class NamespacePIIConfig(BaseModel):
    """
    PII policy block nested inside NamespaceMetadata.
    Consumed in Phase 0.3 (PII Detection and Auto-Redaction).
    Declared here so namespace creation can pre-configure it.
    """

    model_config = ConfigDict(extra="forbid")

    entity_types: list[str] = Field(
        default_factory=list,
        description=(
            "Presidio entity types to detect (e.g. PERSON, EMAIL, PHONE). "
            "Empty means every type the installation recognises."
        ),
    )
    policy: PIIPolicy = Field(
        default=PIIPolicy.redact,
        description="Action when PII is found",
    )
    reversible: bool = Field(
        default=False,
        description="If True and policy=pseudonymise, store encrypted original",
    )
    allowlist: list[str] = Field(
        default_factory=list,
        description="Entity strings explicitly exempted from redaction",
    )
    pseudonym_hmac_key: str | None = Field(
        default=None,
        description="Per-namespace secret for HMAC-SHA256 pseudonym tokens (≥8 UTF-8 bytes when set). "
        "If omitted, ``NCE_MASTER_KEY`` (UTF-8, ≥32 chars) is used as the HMAC key.",
    )
    namespace_id: str = Field(
        default="",
        description="Namespace identifier; scopes master-key pseudonym derivation per tenant.",
    )
    locale: str = Field(
        default="en",
        description="Localization locale for PII scanning (e.g. 'no' for Norwegian)",
    )

    @field_validator("pseudonym_hmac_key")
    @classmethod
    def _validate_hmac_key(cls, v: str | None) -> str | None:
        if v is not None and len(v.encode("utf-8")) < 8:
            raise ValueError("pseudonym_hmac_key must be at least 8 UTF-8 bytes when set")
        return v


class PIIEntity(BaseModel):
    """[Phase 0.3] Detected PII entity.

    Lifecycle: raw ``value`` is consumed during redaction and immediately
    cleared via ``clear_raw_value()``.  After that point only the redacted
    ``token`` and entity metadata remain reachable.
    """

    model_config = ConfigDict(extra="forbid")

    start: int
    end: int
    entity_type: str
    value: str
    score: float = 1.0
    token: str = ""

    def clear_raw_value(self) -> None:
        """Overwrite ``value`` with a non-sensitive placeholder.

        Call this as soon as the raw PII text has been consumed (token
        generation, vault encryption).  After the call the object is safe
        to appear in logs, debug dumps, and exception tracebacks.

        Idempotent: safe to call multiple times or on entities whose
        ``value`` is already ``[REDACTED]`` or ``None`` (e.g. after an
        early rollback or partial construction).  Any ``AttributeError``
        or ``TypeError`` during the setattr is silently suppressed — the
        entity is already in a sanitised (or destroyed) state.
        """
        import logging

        _log = logging.getLogger("nce-pii")
        try:
            current = object.__getattribute__(self, "value")
            if current in ("[REDACTED]", None):
                return  # already sanitised or never materialised
            object.__setattr__(self, "value", "[REDACTED]")
        except (AttributeError, TypeError) as exc:
            # AttributeError: value field missing (partially constructed / GC'd)
            # TypeError: unexpected type preventing setattr
            _log.debug(
                "clear_raw_value suppressed on entity %r: %s",
                getattr(self, "entity_type", "?"),
                exc,
            )

    def __repr__(self) -> str:
        token_part = f" token={self.token!r}" if self.token else ""
        return (
            f"PIIEntity(start={self.start}, end={self.end}, "
            f"entity_type={self.entity_type!r}, "
            f"value={'[REDACTED]' if self.value == '[REDACTED]' else '<present>'}, "
            f"score={self.score:.2f}{token_part})"
        )


class PIIProcessResult(BaseModel):
    """[Phase 0.3] Result of the PII redaction pipeline."""

    model_config = ConfigDict(extra="forbid")

    sanitized_text: str
    redacted: bool
    entities_found: list[str]
    vault_entries: list[dict] = Field(default_factory=list)


class NamespaceProductConfig(BaseModel):
    """
    Product vertical opt-in settings nested inside NamespaceMetadata.
    A namespace must set ``enabled=True`` to access Product MCP tools and REST routes.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class NamespaceConsolidationConfig(BaseModel):
    """
    Cognitive consolidation settings nested inside NamespaceMetadata.
    Consumed in Phase 1 (Cognitive Layer). Declared here to allow pre-configuration.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    llm_provider: str | None = None
    llm_model: str | None = None
    # Credential reference: 'ref:env/NCE_NS_<slug>_<PROVIDER>_KEY' or 'ref:vault/...' [D3]
    llm_credentials: str | None = None
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    decay_sources: bool = Field(
        default=False,
        description="Mark source memories as lower-salience after consolidation",
    )


class NamespaceForkConfig(BaseModel):
    """
    Configuration for a forked namespace (Phase 2.3).
    """

    model_config = ConfigDict(extra="forbid")

    forked_from_as_of: datetime = Field(
        description="The exact timezone.utc timestamp at which the parent namespace was forked"
    )


class NamespaceEconomyConfig(BaseModel):
    """
    Economy vertical opt-in settings nested inside NamespaceMetadata (Module 8).

    A namespace must set ``enabled=True`` for the recurring-recognition and
    contract-renewal-watcher cron ticks (``nce/cron.py``) to scan it — see
    ``docs/vertical_engines/08-economy-engine.md``'s "Config keys" section.

    Contract records themselves live in the ``economy_contracts`` table
    (migration 049, M8.Wave 10) — NOT in this JSONB blob. Before this field
    existed, ``metadata->'economy'->'recurring_contracts'`` was read via raw
    SQL as a temporary Wave-9 substitute for that table; because
    ``NamespaceMetadata`` is ``extra="forbid"`` with no ``economy`` field,
    any raw-SQL write that ever populated an ``economy`` key would have made
    every subsequent ``manage_namespace(update_metadata)`` call for that
    namespace fail (the whole merged dict is re-validated on every patch,
    ``nce/orchestrators/namespace.py``). Adding this typed field (mirrors
    ``NamespaceProductConfig``) closes that trap for metadata written from
    this point forward.

    That is narrower than the real problem, though: a namespace's ALREADY
    STORED metadata may still carry the full Wave-9 shim shape —
    ``{"enabled": true, "recurring_contracts": [...]}`` — written before this
    field existed. Re-validating that stored shape verbatim (as
    ``_update_namespace_metadata``'s merge-and-revalidate does on every call)
    would still raise, because ``recurring_contracts`` is not a field here.
    The ``_drop_legacy_wave9_shim_keys`` validator below strips exactly that
    one historical sub-key before validation — and ONLY that key — so a
    namespace carrying it can still complete a subsequent, unrelated
    ``update_metadata`` call. ``extra="forbid"`` stays in force for every
    other key: this is a narrow, named amnesty for one retired field, not a
    loosening of the unknown-key guard.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_wave9_shim_keys(cls, data: Any) -> Any:
        """Strip the retired Wave-9 ``recurring_contracts`` sub-key, if present,
        before field validation.

        Only this one named legacy key is dropped — an ``economy`` dict
        carrying any OTHER unrecognised key still raises via
        ``extra="forbid"``, exactly as before. This exists solely so that a
        namespace whose metadata predates the ``economy_contracts`` table
        (migration 049) does not permanently fail every future
        ``manage_namespace(update_metadata)`` call for that namespace.
        """
        if isinstance(data, dict) and "recurring_contracts" in data:
            data = {k: v for k, v in data.items() if k != "recurring_contracts"}
        return data


class NamespaceMetadata(BaseModel):
    """
    Typed representation of the namespaces.metadata JSONB column.

    extra='forbid' prevents unrecognised keys from silently entering the DB.
    Unknown future fields must be added here first (makes schema evolution explicit).
    """

    model_config = ConfigDict(extra="forbid")

    # [D5] Memory temporal retention
    temporal_retention_days: int | None = Field(
        default=90,
        ge=0,
        description="Days to retain memories. None = infinite. 0 = purge immediately.",
    )
    # [D6] LLM response payload retention (MinIO)
    llm_payload_retention_days: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Days to retain LLM response payloads in MinIO. "
            "None = inherit temporal_retention_days. 0 = no caching, re-execute only."
        ),
    )
    product: NamespaceProductConfig = Field(default_factory=NamespaceProductConfig)
    consolidation: NamespaceConsolidationConfig = Field(
        default_factory=NamespaceConsolidationConfig
    )
    pii: NamespacePIIConfig = Field(default_factory=NamespacePIIConfig)
    cognitive: NamespaceCognitiveConfig = Field(default_factory=NamespaceCognitiveConfig)
    fork_config: NamespaceForkConfig | None = Field(
        default=None,
        description="[Phase 2.3] Configuration if this namespace is a fork of another.",
    )
    economy: NamespaceEconomyConfig = Field(default_factory=NamespaceEconomyConfig)
    disabled: bool = Field(
        default=False,
        description=(
            "Soft-delete flag: tenant is logically retired without physical row purge. "
            "When toggled True via metadata patch emits a ``namespace_disabled`` audit event."
        ),
    )


class NamespaceMetadataPatch(BaseModel):
    """Strictly-typed partial update for namespace metadata (Phase 3).

    Mirrors :class:`NamespaceMetadata` fields but all are optional — suitable
    for PATCH semantics via ``manage_namespace(command='update_metadata')``.

    ``extra='forbid'`` rejects unrecognised keys (**schema pollution guard**):
    a typo in a metadata-patch field name is caught at the Pydantic boundary
    rather than silently inserted into the namespaces JSONB column.
    """

    model_config = ConfigDict(extra="forbid")

    temporal_retention_days: int | None = Field(
        default=None,
        ge=0,
        description="[D5] Days to retain memories. None = infinite. 0 = purge immediately.",
    )
    llm_payload_retention_days: int | None = Field(
        default=None,
        ge=0,
        description="[D6] Days to retain LLM payloads in MinIO.",
    )
    product: NamespaceProductConfig | None = None
    consolidation: NamespaceConsolidationConfig | None = None
    pii: NamespacePIIConfig | None = None
    cognitive: NamespaceCognitiveConfig | None = None
    fork_config: NamespaceForkConfig | None = None
    economy: NamespaceEconomyConfig | None = None
    disabled: bool | None = None


# ── Namespace CRUD models ─────────────────────────────────────────────────────


class NamespaceCreate(BaseModel):
    """Input payload for manage_namespace(command='create')."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(
        min_length=2,
        max_length=64,
        description=(
            "URL-safe lowercase identifier. 2–64 chars. "
            "Alphanumeric + hyphens. Must start and end with alphanumeric."
        ),
    )
    parent_id: UUID4 | None = Field(
        default=None,
        description="Parent namespace UUID for hierarchy. None = root namespace.",
    )
    metadata: NamespaceMetadata = Field(default_factory=NamespaceMetadata)

    @field_validator("slug")
    @classmethod
    def _slug_format(cls, v: str) -> str:
        v = v.lower().strip()
        if not _SAFE_SLUG_RE.match(v):
            raise ValueError(
                "slug must be 2–64 chars, lowercase alphanumeric and hyphens, "
                "starting and ending with an alphanumeric character"
            )
        return v


class NamespaceRecord(BaseModel):
    """
    Full namespace row as returned from the database.
    extra='ignore' tolerates any additional DB columns without breaking.
    """

    model_config = ConfigDict(extra="ignore")

    id: UUID4
    slug: str
    parent_id: UUID4 | None = None
    created_at: datetime
    metadata: NamespaceMetadata


# ── Memory models ─────────────────────────────────────────────────────────────


class StoreMemoryRequest(BaseModel):
    """
    Input for the store_memory MCP tool (Phase 0.1).

    Decisions enforced:
      [D4]  agent_id stripped, max 128 chars, defaults to 'default'.
      [D8]  valid_from is NOT an input field — always assigned server-side (now()).
            Any attempt to supply it is rejected by extra='forbid'.
      [D9]  assertion_type defaults to AssertionType.fact. Classifier may override
            during the ingest pipeline (nce/pii.py::infer_assertion_type).
    """

    model_config = ConfigDict(extra="forbid")

    namespace_id: UUID4 = Field(description="Target namespace for this memory")
    agent_id: str = Field(
        default="default",
        description="[D4] Agent identifier. Free-text, max 128 chars.",
    )

    # Content
    content: str = Field(
        min_length=1,
        description="Primary text to store, embed, and graph-extract",
    )
    summary: str = Field(
        default="",
        max_length=_MAX_SUMMARY_LEN,
        description=(
            "Short synopsis used for FTS index. "
            "Auto-derived from content (first 8192 chars) if left blank."
        ),
    )
    heavy_payload: str = Field(
        default="",
        description=(
            "Full raw content (transcript, document, …). "
            "Stored in MongoDB, not embedded. "
            "Defaults to content when not supplied."
        ),
    )
    content_type: Literal["chat", "code"] | None = Field(
        default=None,
        description="Type of content ('chat' or 'code').",
    )

    # Classification
    memory_type: MemoryType = Field(default=MemoryType.episodic)
    assertion_type: AssertionType = Field(
        default=AssertionType.fact,
        description="[D9] Classifier may override this during ingest.",
    )

    # Optional enrichment
    metadata: SafeMetadataDict | None = Field(
        default=None,
        description="Arbitrary caller metadata stored in MongoDB alongside the payload — flat JSON-safe keys only",
    )
    derived_from: list[UUID4] | None = Field(
        default=None,
        description="Source memory IDs — required when memory_type='consolidated'",
    )
    check_contradictions: bool = Field(
        default=False,
        description="Phase 1.3: If true, runs sync contradiction detection and returns result.",
    )

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return _validate_agent_id(v)

    @field_validator("heavy_payload")
    @classmethod
    def _payload_size(cls, v: str) -> str:
        if len(v.encode("utf-8")) > _MAX_PAYLOAD_LEN:
            raise ValueError(f"heavy_payload exceeds {_MAX_PAYLOAD_LEN // 1_048_576} MB limit")
        return v

    @field_validator("content")
    @classmethod
    def _content_size(cls, v: str) -> str:
        if len(v.encode("utf-8")) > _MAX_CONTENT_LEN:
            raise ValueError(f"content exceeds maximum size ({_MAX_CONTENT_LEN // 1_000} KB limit)")
        return v

    @model_validator(mode="after")
    def _fill_defaults(self) -> StoreMemoryRequest:
        """Derive summary and heavy_payload from content when not supplied."""
        if not self.summary:
            self.summary = self.content[:_MAX_SUMMARY_LEN]
        if not self.heavy_payload:
            self.heavy_payload = self.content
        return self

    @model_validator(mode="after")
    def _consolidated_requires_derived_from(self) -> StoreMemoryRequest:
        if self.memory_type == MemoryType.consolidated and not self.derived_from:
            raise ValueError(
                "derived_from must list source memory IDs when memory_type='consolidated'"
            )
        return self


class MemoryRecord(BaseModel):
    """
    In-memory representation of a single memories table row.

    This model is produced by the orchestrator after a successful write,
    NOT constructed from raw user input.
    The `valid_from` field is always server-assigned and present here for
    downstream read paths (verify, search results, etc.).
    """

    model_config = ConfigDict(extra="ignore")

    id: UUID4
    namespace_id: UUID4
    agent_id: str
    created_at: datetime
    memory_type: MemoryType
    assertion_type: AssertionType
    payload_ref: str = Field(description="MongoDB document _id (string form)")
    embedding_model_id: UUID4 | None = None
    derived_from: list[UUID4] | None = None
    valid_from: datetime = Field(description="[D8] Server-assigned; never user-supplied")
    valid_to: datetime | None = Field(
        default=None,
        description="NULL = current row (latest version)",
    )
    signature: bytes = Field(description="[Phase 0.2] HMAC-SHA256 over JCS payload")
    signature_key_id: str = Field(description="[Phase 0.2] ID of the signing key used")
    pii_redacted: bool = False
    change_origin: ChangeOrigin | None = None
    origin_event_id: UUID4 | None = None
    derivation_depth: int | None = None


class MemorySalienceRecord(BaseModel):
    """Represents a row in the memory_salience table."""

    model_config = ConfigDict(extra="ignore")

    memory_id: UUID4
    agent_id: str
    namespace_id: UUID4
    salience_score: float = Field(default=1.0, ge=0.0, le=1.0)
    access_count: int = Field(default=0, ge=0)
    updated_at: datetime
    created_at: datetime


# ── Knowledge Graph models ────────────────────────────────────────────────────


class KGNode(BaseModel):
    """A node (entity) in the knowledge graph."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    entity_type: str = Field(default="UNKNOWN")
    source_text: str
    payload_ref: str | None = None
    change_origin: ChangeOrigin | None = None
    origin_event_id: UUID4 | None = None
    derivation_depth: int | None = None

    @field_validator("label")
    @classmethod
    def _strip_label(cls, v: str) -> str:
        return v.strip()


class KGEdge(BaseModel):
    """An edge (relation) in the knowledge graph."""

    model_config = ConfigDict(extra="forbid")

    subject_label: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_label: str = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    payload_ref: str | None = None
    metadata: SafeMetadataDict = Field(default_factory=dict)
    change_origin: ChangeOrigin | None = None
    origin_event_id: UUID4 | None = None
    derivation_depth: int | None = None

    @field_validator("subject_label", "predicate", "object_label")
    @classmethod
    def _strip_labels(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _reject_self_referential(self) -> KGEdge:
        """Prevent edges where subject and object are the same node.

        Self-referential edges (A→A) create infinite loops in BFS graph
        traversal because the same node is both the current position and
        the next neighbor, causing the BFS to never make forward progress.
        """
        if self.subject_label == self.object_label:
            raise ValueError(
                f"Self-referential KGEdge: subject_label and object_label "
                f"must differ (both are {self.subject_label!r}). "
                f"Self-referential edges cause infinite BFS loops."
            )
        return self


class MediaPayload(BaseModel):
    """Input for the store_media MCP tool (Phase 1.2)."""

    model_config = ConfigDict(extra="forbid")

    namespace_id: UUID4
    user_id: str
    session_id: str
    media_type: Literal["audio", "video", "image"]
    file_path_on_disk: str
    summary: str = Field(max_length=_MAX_SUMMARY_LEN)

    @field_validator("user_id", "session_id")
    @classmethod
    def _validate_ids(cls, v: str) -> str:
        if not _SAFE_ID_RE.match(v):
            raise ValueError("user_id/session_id contains invalid characters")
        return v


# Backward-compat alias for MediaPayload (Phase 1.2 -> Phase 1.3 Transition)
ArtifactPayload = MediaPayload


class GetHealthResponse(BaseModel):
    """Output for the get_health MCP tool."""

    status: str
    timestamp: str
    security: dict[str, str]
    databases: dict[str, str]
    cognitive: dict[str, Any]


# ── Search / retrieval models ─────────────────────────────────────────────────


class SemanticSearchRequest(BaseModel):
    """Input for the semantic_search MCP tool."""

    model_config = ConfigDict(extra="forbid")

    namespace_id: UUID4
    agent_id: str | None = Field(
        default=None,
        description="Filter by agent. None = all agents in namespace.",
    )
    query: str = Field(min_length=1)
    limit: int = Field(
        default=5,
        ge=1,
        le=_MAX_TOP_K,
        description="Maximum hits to return after offset.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of ranked hits to skip (offset pagination).",
    )
    as_of: datetime | None = Field(
        default=None,
        description="Point-in-time recall: return memories valid at this timestamp",
    )

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str | None) -> str | None:
        return _validate_agent_id(v) if v is not None else None

    @field_validator("query")
    @classmethod
    def _query_size(cls, v: str) -> str:
        if len(v) > _MAX_QUERY_LEN:
            raise ValueError(f"query exceeds maximum length ({_MAX_QUERY_LEN} characters)")
        return v


class SemanticSearchResult(BaseModel):
    """A single hit returned from a semantic search."""

    model_config = ConfigDict(extra="ignore")

    memory_id: UUID4
    namespace_id: UUID4
    agent_id: str
    score: float = Field(ge=0.0)
    payload_ref: str
    assertion_type: AssertionType
    memory_type: MemoryType
    valid_from: datetime
    pii_redacted: bool = False
    content_preview: str | None = Field(
        default=None,
        description="Populated by the orchestrator from MongoDB; may be None if redacted",
    )
    metadata: SafeMetadataDict | None = Field(
        default=None,
        description="Optional memories.metadata JSON (compare_states / diagnostics) — flat JSON-safe keys only",
    )
    change_origin: ChangeOrigin | None = None
    origin_event_id: UUID4 | None = None
    derivation_depth: int | None = None


class GraphSearchRequest(BaseModel):
    """Input for the graph_search MCP tool."""

    model_config = ConfigDict(extra="forbid")

    namespace_id: UUID4
    agent_id: str | None = Field(
        default=None,
        description="Filter graph traversal by agent. None = all agents in namespace.",
    )
    query: str = Field(min_length=1)
    max_depth: int = Field(default=2, ge=1, le=MAX_GRAPH_DEPTH)
    anchor_top_k: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of semantic anchor nodes to seed BFS traversal.",
    )
    as_of: datetime | None = None
    max_edges_per_node: int = Field(
        default=512,
        ge=1,
        le=_MAX_GRAPH_EDGES_PER_NODE,
        description=(
            "Maximum incident edges loaded from the database per BFS expansion "
            "(ordered by decayed confidence). Caps hub-node fan-out to prevent OOM."
        ),
    )
    edge_limit: int | None = Field(
        default=None,
        ge=1,
        le=MAX_GRAPH_EDGE_PAGE,
        description="If set, return at most this many edges after deduplication (pagination).",
    )
    edge_offset: int = Field(
        default=0,
        ge=0,
        description="Offset into the deduplicated edge list before applying edge_limit.",
    )

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str | None) -> str | None:
        return _validate_agent_id(v) if v is not None else None

    @field_validator("query")
    @classmethod
    def _query_size(cls, v: str) -> str:
        if len(v) > _MAX_QUERY_LEN:
            raise ValueError(f"query exceeds maximum length ({_MAX_QUERY_LEN} characters)")
        return v


class BoostMemoryRequest(BaseModel):
    """Input for the boost_memory MCP tool (Phase 1.1)."""

    model_config = ConfigDict(extra="forbid")

    memory_id: UUID4
    agent_id: str
    namespace_id: UUID4
    factor: float = Field(default=0.2, ge=-1.0, le=1.0)

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return _validate_agent_id(v)


class ForgetMemoryRequest(BaseModel):
    """Input for the forget_memory MCP tool (Phase 1.1)."""

    model_config = ConfigDict(extra="forbid")

    memory_id: UUID4
    agent_id: str
    namespace_id: UUID4

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return _validate_agent_id(v)


class UnredactMemoryRequest(BaseModel):
    """Input for the unredact_memory MCP tool (ADMIN)."""

    model_config = ConfigDict(extra="forbid")

    memory_id: UUID4
    namespace_id: UUID4
    agent_id: str

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return _validate_agent_id(v)


class ShredMemoryRequest(BaseModel):
    """Input for the shred_memory MCP tool (Part II.4 — Provable Forgetting, ADMIN)."""

    model_config = ConfigDict(extra="forbid")

    memory_id: UUID4
    namespace_id: UUID4
    agent_id: str

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return _validate_agent_id(v)


class GetRecentContextRequest(BaseModel):
    """Input for the get_recent_context MCP tool."""

    model_config = ConfigDict(extra="forbid")

    namespace_id: UUID4
    agent_id: str | None = Field(
        default=None,
        description="Calling agent identity. None falls back to 'default' in the handler.",
    )
    user_id: str = Field(
        default="default",
        description="[DEPRECATED] Use agent_id instead.",
    )
    session_id: str = Field(default="default")
    limit: int = Field(default=10, ge=1, le=_MAX_TOP_K)
    offset: int = Field(
        default=0,
        ge=0,
        description="Rows to skip before returning `limit` recent memories.",
    )
    as_of: datetime | None = None

    @field_validator("agent_id")
    @classmethod
    def _validate_get_recent_agent_id(cls, v: str | None) -> str | None:
        return _validate_agent_id(v) if v is not None else None

    @field_validator("user_id", "session_id")
    @classmethod
    def _validate_ids(cls, v: str) -> str:
        if not _SAFE_ID_RE.match(v):
            raise ValueError("user_id/session_id contains invalid characters")
        return v

    @model_validator(mode="after")
    def _resolve_agent_id_from_user_id(self) -> GetRecentContextRequest:
        """Promote user_id to agent_id when agent_id is not explicitly set.

        user_id is a deprecated alias. Callers should migrate to agent_id.
        After this validator, handlers always use req.agent_id as the
        canonical identity — req.user_id is never passed to engine methods.
        """
        if self.agent_id is None and self.user_id != "default":
            self.agent_id = self.user_id
        return self


# ── Code Indexing ─────────────────────────────────────────────────────────────


class IndexCodeFileRequest(BaseModel):
    """Input for the index_code_file MCP tool (Phase 3.2)."""

    model_config = ConfigDict(extra="forbid")

    filepath: str
    raw_code: str
    language: Literal["python", "javascript", "typescript", "go", "rust"]
    namespace_id: UUID4 | None = None
    user_id: str | None = None
    private: bool = False

    @field_validator("user_id")
    @classmethod
    def _validate_user_id(cls, v: str | None) -> str | None:
        if v is not None and not _SAFE_ID_RE.match(v):
            raise ValueError("Invalid user_id format")
        return v

    @model_validator(mode="after")
    def _private_requires_user(self) -> IndexCodeFileRequest:
        if self.private and not self.user_id:
            raise ValueError("private indexing requires user_id")
        return self


# ── Replay Engine (Phase 2.3) ──────────────────────────────────────────────────


class ReplayLlmProvider(StrEnum):
    """Labels accepted by ``nce.providers.factory.get_provider`` for replay overrides."""

    LOCAL_COGNITIVE_MODEL = "local-cognitive-model"
    OPENAI = "openai"
    AZURE_OPENAI = "azure_openai"
    DEEPSEEK = "deepseek"
    MOONSHOT_KIMI = "moonshot_kimi"
    OPENAI_COMPATIBLE = "openai_compatible"
    GOOGLE_GEMINI = "google_gemini"
    ANTHROPIC = "anthropic"


class ReplayConfigOverrides(BaseModel):
    """
    Allowed keys for ``replay_fork`` / re-execute ``config_overrides``.

    Free-text prompt mutation (e.g. ``prompt_suffix``) is forbidden to prevent
    prompt injection into the consolidation replay path.

    ``frozen=True`` ensures that once validated at the API boundary, override
    values cannot be monkey-patched by any code path — Python reflection,
    ``setattr``, or ``object.__setattr__`` all raise ``ValidationError``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    llm_provider: ReplayLlmProvider | None = None
    llm_model: str | None = Field(default=None, max_length=256)
    llm_credentials: str | None = Field(default=None, max_length=2048)
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)


def normalize_replay_config_overrides(
    raw: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate replay override dict; return JSON-compatible dict or ``None``."""
    if raw is None:
        return None
    return ReplayConfigOverrides.model_validate(raw).model_dump(mode="json", exclude_none=True)


class FrozenForkConfig(BaseModel):
    """
    Immutable execution config for a forked replay run.

    Once instantiated, ALL fields are read-only.  Python reflection
    (``setattr``, ``object.__setattr__``) is blocked at the Pydantic level
    via ``frozen=True``.  This guarantees that no code path — handler,
    LLM resolver, or observer — can mutate the replay parameters *during
    flight*, which is a requirement for WORM-compliant replay integrity.

    The model also prohibits extra keys (``extra="forbid"``) so that
    accidental injection of unvalidated configuration is impossible.

    .. note::

       Pydantic v2 ``frozen=True`` hooks ``__setattr__`` but not
       ``object.__setattr__``.  This means a deliberate ``object.__setattr__``
       call can technically bypass the freeze.  This is a known limitation
       of the Pydantic v2 runtime model — it is NOT a practical attack vector
       because:
       - Type checkers (mypy/pyright) flag ``object.__setattr__`` as a type error
         on typed models.
       - Any code path that uses ``object.__setattr__`` to mutate a frozen config
         is trivially detectable in code review and CI (custom linter rule).
       - The ``overrides_dict`` property returns independent copies, so mutating
         the returned dict cannot affect the frozen config.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_namespace_id: UUID4
    target_namespace_id: UUID4
    fork_seq: int = Field(ge=1)
    start_seq: int = Field(default=1, ge=1)
    replay_mode: Literal["deterministic", "re-execute"] = "deterministic"
    config_overrides: ReplayConfigOverrides | None = None
    agent_id_filter: str | None = None

    # Private — not part of the JSON schema, survives ``frozen=True``
    _existing_run_id: UUID4 | None = PrivateAttr(default=None)

    def with_existing_run_id(self, run_id: uuid.UUID) -> FrozenForkConfig:
        """
        Return a **new** ``FrozenForkConfig`` with ``_existing_run_id`` set.

        This is the ONLY way to attach a pre-created run ID — the returned
        object is a fresh frozen instance, preserving immutability of the
        original.
        """
        new_instance = self.model_copy()
        new_instance._existing_run_id = run_id
        return new_instance

    @property
    def existing_run_id(self) -> uuid.UUID | None:
        """Private field accessor (not part of the JSON schema)."""
        return self._existing_run_id

    @property
    def overrides_dict(self) -> dict[str, Any] | None:
        """Read-only JSON-compatible dict of non-None override values."""
        if self.config_overrides is None:
            return None
        return self.config_overrides.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_request(
        cls,
        req: ReplayForkRequest,
        _existing_run_id: uuid.UUID | None = None,
    ) -> FrozenForkConfig:
        """Construct from the API-boundary ``ReplayForkRequest`` model.

        Validates the payload checksum BEFORE constructing the frozen config
        — this guarantees that no replay state manipulation begins on a
        tampered or corrupted payload (WORM requirement, Item 11).
        """
        cls._validate_payload_checksum(req)
        instance = cls(
            source_namespace_id=req.source_namespace_id,
            target_namespace_id=req.target_namespace_id,
            fork_seq=req.fork_seq,
            start_seq=req.start_seq,
            replay_mode=req.replay_mode,
            config_overrides=req.config_overrides,
            agent_id_filter=req.agent_id_filter,
        )
        instance._existing_run_id = _existing_run_id
        return instance

    @staticmethod
    def _validate_payload_checksum(req: ReplayForkRequest) -> None:
        """Recompute SHA-256 over all replay-determining fields and compare.

        The caller MUST attach ``expected_sha256`` computed over canonical
        JSON of every field *except* the hash itself.  This proves the
        payload was not tampered with between the client and the server.

        Raises:
            ReplayChecksumError: If the recomputed hash does not match.
        """
        import hashlib

        from nce.replay import ReplayChecksumError
        from nce.signing import canonical_json

        payload_for_hash: dict[str, Any] = {
            "source_namespace_id": str(req.source_namespace_id),
            "target_namespace_id": str(req.target_namespace_id),
            "fork_seq": req.fork_seq,
            "start_seq": req.start_seq,
            "replay_mode": req.replay_mode,
            "config_overrides": (
                req.config_overrides.model_dump(mode="json", exclude_none=True)
                if req.config_overrides
                else None
            ),
            "agent_id_filter": req.agent_id_filter,
        }
        computed = hashlib.sha256(canonical_json(payload_for_hash)).hexdigest()
        if computed != req.expected_sha256:
            import logging as _log

            _log.getLogger("nce.models").debug(
                "Replay checksum mismatch: expected=%s computed=%s",
                req.expected_sha256,
                computed,
            )
            raise ReplayChecksumError("Payload checksum mismatch")


class ReplayObserveRequest(BaseModel):
    """Input for the replay_observe MCP tool."""

    model_config = ConfigDict(extra="forbid")

    namespace_id: UUID4
    start_seq: int = Field(default=1, ge=1)
    end_seq: int | None = Field(default=None, ge=1)
    agent_id_filter: str | None = None
    max_events: int = Field(default=500, ge=1, le=5000)


class ReplayForkRequest(BaseModel):
    """Input for the replay_fork MCP tool.

    ``expected_sha256`` is a cryptographic payload checksum that the caller
    MUST compute over all other fields (canonical JSON, excluding the hash
    itself) and attach.  The server recomputes and compares before any
    replay state manipulation begins — satisfying the WORM requirement
    that the payload cannot have been tampered with in transit.
    """

    model_config = ConfigDict(extra="forbid")

    source_namespace_id: UUID4
    target_namespace_id: UUID4
    fork_seq: int = Field(ge=1)
    start_seq: int = Field(default=1, ge=1)
    replay_mode: Literal["deterministic", "re-execute"] = "deterministic"
    config_overrides: ReplayConfigOverrides | None = None
    agent_id_filter: str | None = None
    expected_sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="sha256(canonical_json(all_other_fields)).hexdigest()",
    )

    @field_validator("expected_sha256")
    @classmethod
    def _validate_hex(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("expected_sha256 must be exactly 64 lowercase hex characters")
        return v


# ── Embedding Migrations (Phase 2.1) ──────────────────────────────────────────


class MigrationStartRequest(BaseModel):
    """Input for start_migration admin tool."""

    model_config = ConfigDict(extra="forbid")
    target_model_id: UUID4


# ── A2A Protocol (Phase 3.1) ──────────────────────────────────────────────────


class A2AScope(BaseModel):
    """Defines the resource type and permissions granted in an A2A share."""

    model_config = ConfigDict(extra="forbid")
    resource_type: Literal["namespace", "memory", "kg_node", "subgraph"]
    resource_id: str
    permissions: list[Literal["read"]] = Field(default_factory=lambda: ["read"])  # type: ignore[arg-type]


class A2AGrantRequest(BaseModel):
    """Request payload to create a new A2A sharing grant."""

    model_config = ConfigDict(extra="forbid")
    target_namespace_id: UUID4 | None = None
    target_agent_id: str | None = None
    scopes: list[A2AScope] = Field(..., min_length=1)
    expires_in_seconds: int = Field(3600, ge=60, le=86400 * 30)
    can_delegate: bool = Field(default=False)

    @field_validator("target_agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str | None) -> str | None:
        return _validate_agent_id(v) if v is not None else None


class A2AGrantResponse(BaseModel):
    """Response payload containing the secure sharing token."""

    model_config = ConfigDict(extra="forbid")
    grant_id: UUID4
    sharing_token: str
    expires_at: datetime


class VerifiedGrant(BaseModel):
    """Result of a successful token verification."""

    model_config = ConfigDict(extra="forbid")
    grant_id: UUID4
    owner_namespace_id: UUID4
    owner_agent_id: str
    scopes: list[A2AScope]
    expires_at: datetime
    can_delegate: bool = False


class A2AQuerySharedRequest(BaseModel):
    """Input for a2a_query_shared MCP tool."""

    model_config = ConfigDict(extra="forbid")
    sharing_token: str
    consumer_namespace_id: UUID4
    consumer_agent_id: str = Field(default="default")
    query: str = Field(min_length=1)
    resource_type: Literal["namespace", "memory", "kg_node", "subgraph"] = "namespace"
    resource_id: str | None = None
    top_k: int = Field(default=5, ge=1, le=_MAX_TOP_K)


# ── Namespace management ──────────────────────────────────────────────────────


class ManageNamespaceRequest(BaseModel):
    """
    Input for the manage_namespace MCP admin tool.

    The command field acts as a discriminator. Required sub-fields are enforced
    by the cross-field validator below.
    """

    model_config = ConfigDict(extra="forbid")

    command: ManageNamespaceCommand

    # Contextual fields — not all are used by every command.
    namespace_id: UUID4 | None = Field(
        default=None,
        description="Target namespace for grant/revoke/update_metadata/delete",
    )
    create: NamespaceCreate | None = Field(
        default=None,
        description="Required when command='create'",
    )
    metadata_patch: NamespaceMetadataPatch | None = Field(
        default=None,
        description="Partial metadata keys to update; merged server-side — strict Pydantic model",
    )
    grantee_namespace_id: UUID4 | None = Field(
        default=None,
        description="Child namespace that gains/loses read access in grant/revoke",
    )
    allow_audit_destruction: bool = Field(
        default=False,
        description=(
            "Reserved for destructive admin flows only. Namespace delete stays blocked "
            "while event_log rows reference the tenant (WORM + FK); this flag carries "
            "no automated purge."
        ),
    )

    @model_validator(mode="after")
    def _command_constraints(self) -> ManageNamespaceRequest:
        cmd = self.command
        if cmd == ManageNamespaceCommand.create and self.create is None:
            raise ValueError("'create' payload is required when command='create'")
        if cmd == ManageNamespaceCommand.update_metadata:
            if self.namespace_id is None:
                raise ValueError("namespace_id required for command='update_metadata'")
            if self.metadata_patch is None:
                raise ValueError("metadata_patch required for command='update_metadata'")
        if cmd in (ManageNamespaceCommand.grant, ManageNamespaceCommand.revoke):
            if self.namespace_id is None or self.grantee_namespace_id is None:
                raise ValueError(
                    "Both namespace_id and grantee_namespace_id are required "
                    "for command='grant' / 'revoke'"
                )
        if cmd == ManageNamespaceCommand.delete and self.namespace_id is None:
            raise ValueError("namespace_id required for command='delete'")
        return self


ManageNamespaceRequest.model_rebuild()


# ── Signing key (schema in Phase 0.1, signing logic in Phase 0.2) ────────────


class SigningKeyRecord(BaseModel):
    """
    Row from the signing_keys table.
    The encrypted_key (BYTEA) column is intentionally absent — never surfaced
    to application-layer models.
    """

    model_config = ConfigDict(extra="ignore")

    id: UUID4
    key_id: str = Field(min_length=1)
    status: SigningKeyStatus
    created_at: datetime
    retired_at: datetime | None = Field(
        default=None,
        description="[D0.2] Set when key is retired. Never deleted.",
    )


# ── Phase 2.2: Time Travel Snapshots ─────────────────────────────────────────


class CreateSnapshotRequest(BaseModel):
    """Input for create_snapshot MCP tool."""

    model_config = ConfigDict(extra="forbid")

    namespace_id: UUID4
    agent_id: str = Field(default="default")
    name: str = Field(min_length=1, max_length=255)
    snapshot_at: datetime | None = Field(
        default=None,
        description="The point in time to snapshot. Defaults to now().",
    )
    metadata: SafeMetadataDict = Field(
        default_factory=dict,
        description="Arbitrary caller metadata — flat JSON-safe keys only",
    )

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        return _validate_agent_id(v)


class SnapshotRecord(BaseModel):
    """Full snapshot row from the database."""

    model_config = ConfigDict(extra="ignore")

    id: UUID4
    namespace_id: UUID4
    agent_id: str
    name: str
    snapshot_at: datetime
    created_at: datetime
    metadata: SafeMetadataDict


class CompareStatesRequest(BaseModel):
    """Input for compare_states MCP tool."""

    model_config = ConfigDict(extra="forbid")

    namespace_id: UUID4
    as_of_a: datetime = Field(description="First point in time (T_start)")
    as_of_b: datetime = Field(description="Second point in time (T_end)")
    query: str | None = Field(
        default=None,
        description="If provided, diffs semantic results. If None, diffs full namespace state.",
    )
    top_k: int = Field(default=10, ge=1, le=_MAX_TOP_K)


class DeleteSnapshotResult(BaseModel):
    """Result of a successful snapshot deletion."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    message: str


class SagaState(StrEnum):
    """Canonical states for the Saga Execution Log."""

    STARTED = "started"
    PG_COMMITTED = "pg_committed"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    DEFERRED = "deferred"


class SagaFailureContext(TypedDict, total=False):
    """Typed parameters passed to saga rollback / failure callbacks.

    Using ``total=False`` makes every field optional so callers can
    supply only the keys they have at the point of failure.
    """

    e: BaseException
    payload: StoreMemoryRequest
    collection: Any
    inserted_mongo_id: str | None
    inserted_result: Any
    memory_id: str | None
    pg_committed: bool
    saga_id: str | None


class StateDiffResult(BaseModel):
    """The result of diffing two temporal states."""

    as_of_a: datetime
    as_of_b: datetime
    added: list[SemanticSearchResult]
    removed: list[SemanticSearchResult]
    modified: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Version transitions: old and new metadata for same memory_id",
    )


class ActorTrustOut(BaseModel):
    """[Batch C0] Response model representing an actor trust row."""

    model_config = ConfigDict(extra="ignore")

    namespace_id: UUID4
    actor_id: str
    actor_kind: ActorKind
    confirmations: int
    rejections: int
    contradictions_sourced: int
    trust: float
    updated_at: datetime


class ApprovalQueueItemOut(BaseModel):
    """[Batch C0] Response model representing an approval queue item."""

    model_config = ConfigDict(extra="ignore")

    id: UUID4
    namespace_id: UUID4
    agent_id: str
    action_type: str
    target_system: str
    target_entity_id: str | None = None
    proposed_payload: dict[str, Any]
    status: ApprovalStatus
    dry_run_result: dict[str, Any] | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class ActionIdempotencyOut(BaseModel):
    """[Batch C0] Response model representing an idempotency key row."""

    model_config = ConfigDict(extra="ignore")

    idempotency_key: str
    namespace_id: UUID4
    action_type: str
    target_entity_id: str | None = None
    response_hash: bytes | None = None
    created_at: datetime


class EventParentOut(BaseModel):
    """[Batch C0] Response model representing an event parent lineage mapping."""

    model_config = ConfigDict(extra="ignore")

    event_id: UUID4
    parent_event_id: UUID4
    namespace_id: UUID4
    created_at: datetime


# ── Instantiation tests ───────────────────────────────────────────────────────
# Run directly:  python -m nce.models
# The block below is NOT a test suite replacement; it validates that every
# model can be instantiated with valid data and rejects known-bad inputs.

if __name__ == "__main__":
    import sys

    _ns_id = uuid.uuid4()
    _mem_id = uuid.uuid4()
    _now = datetime.now(tz=timezone.utc)

    passed: list[str] = []
    failed: list[tuple[str, Exception]] = []

    def _ok(name: str) -> None:
        passed.append(name)
        print(f"  PASS  {name}")

    def _fail(name: str, exc: Exception) -> None:
        failed.append((name, exc))
        print(f"  FAIL  {name}: {exc}")

    def _expect_error(name: str, fn) -> None:
        try:
            fn()
            _fail(name, AssertionError("Expected ValidationError but none was raised"))
        except Exception:
            _ok(name)

    print("\n-- Phase 0.1 model instantiation tests --\n")

    # ── NamespaceMetadata ──
    try:
        m = NamespaceMetadata(temporal_retention_days=30)
        assert m.temporal_retention_days == 30
        assert m.consolidation.enabled is False
        _ok("NamespaceMetadata: valid construction")
    except Exception as e:
        _fail("NamespaceMetadata: valid construction", e)

    _expect_error(
        "NamespaceMetadata: extra field rejected",
        lambda: NamespaceMetadata(unknown_field="x"),  # type: ignore[call-arg]
    )

    # ── NamespaceCreate ──
    try:
        nc = NamespaceCreate(slug="acme-corp", parent_id=None)
        assert nc.slug == "acme-corp"
        _ok("NamespaceCreate: valid slug")
    except Exception as e:
        _fail("NamespaceCreate: valid slug", e)

    try:
        nc_upper = NamespaceCreate(slug="ACME-Corp")
        assert nc_upper.slug == "acme-corp", f"Expected 'acme-corp', got {nc_upper.slug!r}"
        _ok("NamespaceCreate: uppercase slug normalised to lowercase")
    except Exception as e:
        _fail("NamespaceCreate: uppercase slug normalised to lowercase", e)
    _expect_error(
        "NamespaceCreate: slug starting with hyphen rejected",
        lambda: NamespaceCreate(slug="-bad"),
    )
    _expect_error(
        "NamespaceCreate: single-char slug rejected",
        lambda: NamespaceCreate(slug="a"),
    )

    # ── StoreMemoryRequest — happy path ──
    try:
        req = StoreMemoryRequest(
            namespace_id=_ns_id,
            content="User discussed multi-tenant architecture patterns.",
        )
        assert req.agent_id == "default"
        assert req.assertion_type == AssertionType.fact
        assert req.summary == req.content  # auto-derived
        assert req.heavy_payload == req.content  # auto-derived
        _ok("StoreMemoryRequest: defaults (agent_id, summary, heavy_payload)")
    except Exception as e:
        _fail("StoreMemoryRequest: defaults", e)

    try:
        req2 = StoreMemoryRequest(
            namespace_id=_ns_id,
            agent_id="  planner-bot  ",  # leading/trailing whitespace stripped
            content="Decision: use pgvector HNSW index.",
            memory_type=MemoryType.decision,
            assertion_type=AssertionType.fact,
        )
        assert req2.agent_id == "planner-bot"
        _ok("StoreMemoryRequest: agent_id whitespace stripped")
    except Exception as e:
        _fail("StoreMemoryRequest: agent_id whitespace stripped", e)

    # [D8] valid_from is not an input field
    _expect_error(
        "StoreMemoryRequest: [D8] valid_from rejected (extra='forbid')",
        lambda: StoreMemoryRequest(
            namespace_id=_ns_id,
            content="test",
            valid_from=_now,  # type: ignore[call-arg]  # must be rejected
        ),
    )

    _expect_error(
        "StoreMemoryRequest: empty content rejected",
        lambda: StoreMemoryRequest(namespace_id=_ns_id, content=""),
    )

    _expect_error(
        "StoreMemoryRequest: agent_id with invalid chars rejected",
        lambda: StoreMemoryRequest(
            namespace_id=_ns_id,
            content="x",
            agent_id="bad agent!",
        ),
    )

    _expect_error(
        "StoreMemoryRequest: consolidated without derived_from rejected",
        lambda: StoreMemoryRequest(
            namespace_id=_ns_id,
            content="summary of three memories",
            memory_type=MemoryType.consolidated,
        ),
    )

    try:
        req3 = StoreMemoryRequest(
            namespace_id=_ns_id,
            content="summary",
            memory_type=MemoryType.consolidated,
            derived_from=[uuid.uuid4(), uuid.uuid4()],
        )
        _ok("StoreMemoryRequest: consolidated with derived_from accepted")
    except Exception as e:
        _fail("StoreMemoryRequest: consolidated with derived_from accepted", e)

    # ── SemanticSearchRequest ──
    try:
        sr = SemanticSearchRequest(namespace_id=_ns_id, query="HNSW indexes")
        assert sr.limit == 5
        assert sr.offset == 0
        assert sr.agent_id is None
        _ok("SemanticSearchRequest: defaults")
    except Exception as e:
        _fail("SemanticSearchRequest: defaults", e)

    _expect_error(
        "SemanticSearchRequest: limit=0 rejected",
        lambda: SemanticSearchRequest(namespace_id=_ns_id, query="x", limit=0),
    )
    _expect_error(
        "SemanticSearchRequest: limit=101 rejected",
        lambda: SemanticSearchRequest(namespace_id=_ns_id, query="x", limit=101),
    )
    _expect_error(
        "SemanticSearchRequest: offset=-1 rejected",
        lambda: SemanticSearchRequest(namespace_id=_ns_id, query="x", offset=-1),
    )

    _expect_error(
        "GetRecentContextRequest: offset=-1 rejected",
        lambda: GetRecentContextRequest(namespace_id=_ns_id, offset=-1),
    )

    # ── GraphSearchRequest ──
    try:
        gr = GraphSearchRequest(namespace_id=_ns_id, query="architecture")
        assert gr.max_depth == 2
        assert gr.max_edges_per_node == 512
        assert gr.edge_limit is None
        assert gr.edge_offset == 0
        _ok("GraphSearchRequest: defaults")
    except Exception as e:
        _fail("GraphSearchRequest: defaults", e)

    _expect_error(
        "GraphSearchRequest: max_depth=4 rejected",
        lambda: GraphSearchRequest(namespace_id=_ns_id, query="x", max_depth=4),
    )

    # ── ManageNamespaceRequest ──
    try:
        mr = ManageNamespaceRequest(
            command=ManageNamespaceCommand.create,
            create=NamespaceCreate(slug="test-ns"),
        )
        _ok("ManageNamespaceRequest: create command accepted")
    except Exception as e:
        _fail("ManageNamespaceRequest: create command accepted", e)

    _expect_error(
        "ManageNamespaceRequest: create without payload rejected",
        lambda: ManageNamespaceRequest(command=ManageNamespaceCommand.create),
    )

    _expect_error(
        "ManageNamespaceRequest: update_metadata without namespace_id rejected",
        lambda: ManageNamespaceRequest(
            command=ManageNamespaceCommand.update_metadata,
            metadata_patch={"temporal_retention_days": 30},  # type: ignore[arg-type]
        ),
    )

    try:
        mr2 = ManageNamespaceRequest(
            command=ManageNamespaceCommand.update_metadata,
            namespace_id=_ns_id,
            metadata_patch={"temporal_retention_days": 60},  # type: ignore[arg-type]
        )
        _ok("ManageNamespaceRequest: update_metadata accepted")
    except Exception as e:
        _fail("ManageNamespaceRequest: update_metadata accepted", e)

    # ── MemorySalienceRecord ──
    try:
        sal = MemorySalienceRecord(
            memory_id=_mem_id,
            agent_id="default",
            namespace_id=_ns_id,
            salience_score=0.85,
            access_count=3,
            updated_at=_now,
            created_at=_now,
        )
        assert 0.0 <= sal.salience_score <= 1.0
        _ok("MemorySalienceRecord: valid")
    except Exception as e:
        _fail("MemorySalienceRecord: valid", e)

    _expect_error(
        "MemorySalienceRecord: salience_score > 1.0 rejected",
        lambda: MemorySalienceRecord(
            memory_id=_mem_id,
            agent_id="default",
            namespace_id=_ns_id,
            salience_score=1.5,
            updated_at=_now,
            created_at=_now,
        ),
    )

    # ── SigningKeyRecord ──
    try:
        skr = SigningKeyRecord(
            id=uuid.uuid4(),
            key_id="key-2026-05-01",
            status=SigningKeyStatus.active,
            created_at=_now,
        )
        _ok("SigningKeyRecord: active key")
    except Exception as e:
        _fail("SigningKeyRecord: active key", e)

    # ── Summary ──
    print(f"\n-- Results: {len(passed)} passed, {len(failed)} failed --\n")
    if failed:
        for name, exc in failed:
            print(f"  FAILED: {name}\n    {exc}")
        sys.exit(1)
