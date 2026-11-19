"""Shared Pydantic V2 data contracts and exception types for Day-2 operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OpsDiskHeadroomError(RuntimeError):
    """Raised when free filesystem space cannot safely hold backup/restore staging."""


class OpsRestorePreflightError(RuntimeError):
    """Raised when archive integrity, key fingerprint, or schema preflight fails."""


class BackupManifestRecord(BaseModel):
    """Structured manifest record persisted in ``backup_manifests`` and ``manifest.json``."""

    model_config = ConfigDict(extra="forbid")

    backup_id: UUID
    t_cut: datetime
    t_blob_completed: datetime | None = None
    archive_path: str
    archive_sha256: str
    size_bytes: int = Field(ge=0)
    master_key_hmac_fp: str
    schema_heads: dict[str, Any] = Field(default_factory=dict)
    worm_heads: dict[str, Any] = Field(default_factory=dict)
    tombstone_count: int = Field(default=0, ge=0)
    status: str = "verified"
    last_drill_at: datetime | None = None
    last_drill_status: str | None = None
    last_drill_report: dict[str, Any] | None = None
    created_at: datetime | None = None
    file_digests: dict[str, str] = Field(default_factory=dict)
    manifest_hmac_sha256: str | None = None


class RestoreDrillReport(BaseModel):
    """Summary emitted by an isolated single-host ephemeral restore drill."""

    model_config = ConfigDict(extra="forbid")

    backup_id: UUID
    drill_id: str
    status: Literal["passed", "failed"]
    t_cut: datetime
    duration_seconds: float = Field(ge=0.0)
    reconciliation_ms: float = Field(default=0.0, ge=0.0)
    checked_pointers: int = Field(default=0, ge=0)
    missing_pre_cut_pointers: int = Field(default=0, ge=0)
    resurrected_dek_count: int = Field(default=0, ge=0)
    swept_post_cut_blobs: int = Field(default=0, ge=0)
    worm_chain_valid: bool = True
    cleanup_verified: bool = True
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ProbeResult(BaseModel):
    """Deterministic maintenance caddy probe reading persisted to ``ops_probe_runs``."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    probe_name: str
    status: Literal["ok", "warn", "critical"]
    metrics_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    recommended_playbook: str | None = None
    fingerprint: str | None = None
    title: str | None = None


class ELMDiagnosticBrief(BaseModel):
    """Strict schema validated against advisory ``ops-elm`` diagnostics."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=600)
    probable_root_cause: str = Field(min_length=1, max_length=600)
    cited_metric_keys: list[str] = Field(default_factory=list, max_length=24)
    recommended_playbook_id: str | None = None
    confidence: Literal["low", "medium", "high"] = "low"


class QuarantinedJobRecord(BaseModel):
    """Poison-pill or crashed RQ job tracked in ``ops_quarantine``."""

    model_config = ConfigDict(extra="forbid")

    quarantine_id: UUID
    job_fingerprint: str
    queue_name: str
    func_name: str
    namespace_id: UUID | None = None
    failure_count: int = Field(ge=1)
    exit_reason: str
    sanitized_traceback: str
    job_args_preview: dict[str, Any] = Field(default_factory=dict)
    status: Literal["quarantined", "requeued", "dismissed"] = "quarantined"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PlaybookPreviewResult(BaseModel):
    """Dry-run preview and HMAC confirmation token for ``@governed`` repair playbooks."""

    model_config = ConfigDict(extra="forbid")

    playbook_id: str
    dry_run: bool = True
    estimated_impact: dict[str, Any] = Field(default_factory=dict)
    affected_count: int = Field(default=0, ge=0)
    confirmation_token: str | None = None
    expires_at: datetime | None = None
    executed: bool = False
    result_json: dict[str, Any] = Field(default_factory=dict)
    audit_event_id: str | None = None
