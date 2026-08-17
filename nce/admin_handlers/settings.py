from __future__ import annotations

import json
import os
from typing import Any

from starlette.responses import JSONResponse

from nce import admin_state
from nce.admin_handlers._shared import UTC, logger
from nce.config import cfg
from nce.settings_registry import (
    REGISTRY,
    SettingMetadata,
    validate_bool,
    validate_str_list,
)


def get_validation_metadata(metadata: SettingMetadata) -> dict[str, Any]:
    """Inspect registry validator closures to extract min and allow_empty rules."""
    validator = metadata.validator
    val_dict: dict[str, Any] = {}
    if validator == validate_bool:
        return val_dict
    if validator == validate_str_list:
        return val_dict

    closure = getattr(validator, "__closure__", None)
    code = getattr(validator, "__code__", None)
    if closure and code:
        co_freevars = getattr(code, "co_freevars", ())
        for var_name, cell in zip(co_freevars, closure):
            try:
                val = cell.cell_contents
                if var_name == "minimum" and val is not None:
                    val_dict["min"] = val
                elif var_name == "allow_empty":
                    val_dict["allow_empty"] = val
            except (ValueError, AttributeError):
                pass
    return val_dict


def get_effective_value(
    key: str,
    metadata: SettingMetadata,
    db_overrides: dict[str, Any],
) -> tuple[Any, str, bool]:
    """Determine a setting's effective value, its source (store, env, or default), and override status."""
    if key == "NCE_MASTER_KEY":
        source = "env" if "NCE_MASTER_KEY" in os.environ else "default"
        val = getattr(cfg, "NCE_MASTER_KEY", None)
        if val:
            val = "••••set"
        return val, source, False

    if key in db_overrides:
        row = db_overrides[key]
        is_secret = row["is_secret"]
        if is_secret:
            has_sec = row.get("has_secret")
            if has_sec is None:
                has_sec = bool(row.get("secret_enc"))
            val = "••••set" if has_sec else None
            return val, "store", True
        else:
            val = row["value"]
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except Exception:
                    pass
            return val, "store", True

    if key in os.environ:
        val = getattr(cfg, key, None)
        if metadata.is_secret:
            val = "••••set" if val else None
        return val, "env", False

    val = getattr(cfg, key, None)
    if metadata.is_secret:
        val = "••••set" if val else None
    return val, "default", False


async def api_admin_settings_list(request: Any) -> JSONResponse:
    """GET /api/admin/settings

    List registered configuration settings grouped by section.
    Supports query filters:
      - section: filter by exact section name match
      - q: case-insensitive query matching key or description
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    section_filter = request.query_params.get("section")
    q_filter = request.query_params.get("q")

    # Fetch all DB overrides to avoid N+1 queries
    db_overrides: dict[str, Any] = {}
    if admin_state.engine.pg_pool:
        try:
            async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
                rows = await conn.fetch(
                    "SELECT key, value, (secret_enc IS NOT NULL) AS has_secret, is_secret, updated_by, updated_at FROM settings"
                )
                for row in rows:
                    db_overrides[row["key"]] = row
        except Exception as e:
            logger.error("Failed to fetch settings overrides from Postgres: %s", e)

    sections_list: list[str] = []
    section_to_keys: dict[str, list[dict[str, Any]]] = {}

    for key, metadata in REGISTRY.items():
        if section_filter and metadata.section != section_filter:
            continue
        if q_filter:
            q_lower = q_filter.lower()
            if q_lower not in key.lower() and q_lower not in metadata.description.lower():
                continue

        if metadata.section not in section_to_keys:
            sections_list.append(metadata.section)
            section_to_keys[metadata.section] = []

        effective_value, source, store_value_set = get_effective_value(key, metadata, db_overrides)
        validation_dict = get_validation_metadata(metadata)

        key_detail = {
            "key": key,
            "type": metadata.type,
            "reload_class": metadata.reload_class,
            "is_secret": metadata.is_secret,
            "prod_locked": metadata.prod_locked,
            "effective_value": effective_value,
            "source": source,
            "store_value_set": store_value_set,
            "validation": validation_dict,
            "description": metadata.description,
            "updated_by": None,
            "updated_at": None,
        }

        if store_value_set and key in db_overrides:
            row = db_overrides[key]
            key_detail["updated_by"] = row["updated_by"]
            if row["updated_at"]:
                key_detail["updated_at"] = row["updated_at"].astimezone(UTC).isoformat()

        section_to_keys[metadata.section].append(key_detail)

    response_sections = []
    for sec_name in sections_list:
        keys = section_to_keys[sec_name]
        if keys:
            response_sections.append({"section": sec_name, "keys": keys})

    return JSONResponse({"sections": response_sections})


def _baseline_effective_value(key: str, metadata: SettingMetadata) -> Any:
    """Return the env/default effective value for *key*, ignoring any DB override.

    Secrets are masked to ``••••set``/``None`` by ``get_effective_value`` — the raw
    secret value is never produced here.
    """
    value, _, _ = get_effective_value(key, metadata, {})
    return value


def _coerce_event_params(raw: Any) -> dict[str, Any]:
    """Normalise an ``event_log.params`` column (jsonb may arrive as str) to a dict."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


async def _reconstruct_effective_as_of(conn: Any, as_of_dt: Any) -> dict[str, Any]:
    """Fold ordered ``config_changed``/``config_reset`` WORM events up to *as_of_dt*
    over the env/default baseline, returning the effective (secrets-masked) config.

    Only the redacted ``new_value`` recorded on each ``config_changed`` event is
    applied — secret *values* are never reconstructed from the log (they are stored
    only as ``••••set``/``••••unset`` lifecycle tokens). A ``config_reset`` event
    reverts the affected keys back to their env/default baseline.
    """
    effective: dict[str, Any] = {
        key: _baseline_effective_value(key, metadata) for key, metadata in REGISTRY.items()
    }

    rows = await conn.fetch(
        """
        SELECT event_type, params, event_seq, occurred_at
        FROM event_log
        WHERE event_type IN ('config_changed', 'config_reset')
          AND occurred_at <= $1
        ORDER BY occurred_at ASC, event_seq ASC
        """,
        as_of_dt,
    )

    for row in rows:
        params = _coerce_event_params(row["params"])
        if row["event_type"] == "config_changed":
            changes = params.get("changes")
            if isinstance(changes, dict):
                for key, change in changes.items():
                    if key in effective and isinstance(change, dict) and "new_value" in change:
                        effective[key] = change["new_value"]
        elif row["event_type"] == "config_reset":
            resets = params.get("resets")
            if isinstance(resets, dict):
                for key in resets:
                    if key in REGISTRY:
                        effective[key] = _baseline_effective_value(key, REGISTRY[key])

    return effective


async def api_admin_settings_effective(request: Any) -> JSONResponse:
    """GET /api/admin/settings/effective[?as_of=T]

    Returns a flat key-value object of all settings with effective values (secrets masked).

    When ``as_of`` (ISO 8601 timestamp) is supplied, the effective config is
    reconstructed by folding the ordered ``config_changed``/``config_reset`` WORM
    events up to ``T`` over the env/default baseline — the config analog of
    ``semantic_search(as_of=)``. Non-secret values reconstruct exactly; secret
    values are never recovered from the log (only the set/unset lifecycle token).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    as_of_raw = request.query_params.get("as_of")
    if as_of_raw is not None:
        from nce.temporal import parse_as_of

        try:
            as_of_dt = parse_as_of(as_of_raw)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=422)

        if not admin_state.engine.pg_pool:
            return JSONResponse({"error": "Database not available"}, status_code=503)

        try:
            async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
                effective_dict = await _reconstruct_effective_as_of(conn, as_of_dt)
        except Exception as e:
            logger.exception("Failed to reconstruct effective config as_of: %s", e)
            return JSONResponse({"error": f"Database query failed: {e}"}, status_code=500)

        return JSONResponse(
            {"as_of": as_of_dt.isoformat() if as_of_dt else None, "effective": effective_dict}
        )

    db_overrides: dict[str, Any] = {}
    if admin_state.engine.pg_pool:
        try:
            async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
                rows = await conn.fetch(
                    "SELECT key, value, (secret_enc IS NOT NULL) AS has_secret, is_secret FROM settings"
                )
                for row in rows:
                    db_overrides[row["key"]] = row
        except Exception as e:
            logger.error("Failed to fetch settings overrides for effective: %s", e)

    effective_dict = {}
    for key, metadata in REGISTRY.items():
        effective_value, _, _ = get_effective_value(key, metadata, db_overrides)
        effective_dict[key] = effective_value

    return JSONResponse(effective_dict)


async def handle_explain_config_change(engine: Any, arguments: dict[str, Any]) -> str:
    """[MCP] explain_config_change(key) — return a config key's full change history.

    Folds the ordered ``config_changed``/``config_reset`` WORM events that touched
    *key*, returning one entry per change (timestamp, actor, reason, old→new,
    reset-to-baseline) — the audit companion to ``effective?as_of=T``. Secret
    values are never returned: only the recorded ``set``/``unset``/``rotated``
    redaction tokens already stored on the event.
    """
    key = arguments.get("key")
    if not key or not isinstance(key, str):
        raise ValueError("explain_config_change requires a string 'key' argument")
    if key not in REGISTRY:
        return json.dumps({"key": key, "error": "Setting key not found in registry"})

    metadata = REGISTRY[key]
    pool = getattr(engine, "pg_pool", None)
    if pool is None:
        raise RuntimeError("Database pool not available")

    async with pool.acquire(timeout=10.0) as conn:
        rows = await conn.fetch(
            """
            SELECT event_id, event_type, agent_id, params, event_seq, occurred_at
            FROM event_log
            WHERE event_type IN ('config_changed', 'config_reset')
            ORDER BY occurred_at ASC, event_seq ASC
            """
        )

    history: list[dict[str, Any]] = []
    for row in rows:
        params = _coerce_event_params(row["params"])
        occurred_at = row["occurred_at"]
        occurred_iso = occurred_at.isoformat() if occurred_at is not None else None
        if row["event_type"] == "config_changed":
            changes = params.get("changes")
            if isinstance(changes, dict) and key in changes and isinstance(changes[key], dict):
                change = changes[key]
                history.append(
                    {
                        "event_id": str(row["event_id"]),
                        "event_seq": row["event_seq"],
                        "occurred_at": occurred_iso,
                        "event_type": "config_changed",
                        "actor": params.get("actor"),
                        "reason": params.get("reason"),
                        "old_value": change.get("old_value"),
                        "new_value": change.get("new_value"),
                    }
                )
        elif row["event_type"] == "config_reset":
            resets = params.get("resets")
            if isinstance(resets, dict) and key in resets:
                reset = resets[key] if isinstance(resets[key], dict) else {}
                history.append(
                    {
                        "event_id": str(row["event_id"]),
                        "event_seq": row["event_seq"],
                        "occurred_at": occurred_iso,
                        "event_type": "config_reset",
                        "actor": params.get("actor"),
                        "reverted_to_source": reset.get("source"),
                        "new_value": reset.get("new_value"),
                    }
                )

    return json.dumps(
        {
            "key": key,
            "is_secret": metadata.is_secret,
            "section": metadata.section,
            "change_count": len(history),
            "history": history,
        }
    )


class _PatchRequestProxy:
    """Minimal request shim that delegates to *base* but serves a synthesized JSON body.

    Used so a rollback can apply its inverse change-set through the *normal* PATCH
    path (``api_admin_settings_patch``) — preserving every guardrail (prod-locked
    skip, validation, optimistic-lock, COLD→pending_restart, signed config_changed
    event) rather than re-implementing the write.
    """

    def __init__(self, base: Any, body: dict[str, Any]) -> None:
        self._base = base
        self._body = body

    async def json(self) -> dict[str, Any]:
        return self._body

    @property
    def state(self) -> Any:
        return self._base.state

    @property
    def headers(self) -> Any:
        return self._base.headers


async def api_admin_settings_rollback(request: Any) -> JSONResponse:
    """POST /api/admin/settings/rollback { as_of, sections?, dry_run }

    Config time-travel rollback (V.6). Reconstructs the effective config at ``as_of``,
    diffs it against the current effective config, and computes the inverse change-set
    that would restore the past state.

    - ``dry_run`` (default True) returns the proposed diff for a confirm-modal.
    - On apply (``dry_run: false``), the inverse change-set is pushed through the
      normal PATCH path so every guardrail still holds; the rollback is itself
      recorded as a ``config_changed`` event (``reason: "rollback to T"``).

    Guardrails: prod-locked keys are never silently re-enabled (skipped); secrets
    cannot be auto-restored from the WORM log (their values are unrecoverable by
    design) — keys whose secret changed since ``as_of`` are *flagged* for manual
    re-entry rather than fabricated.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    if not isinstance(body, dict) or "as_of" not in body:
        return JSONResponse({"error": "Payload must contain 'as_of'"}, status_code=422)

    from nce.temporal import parse_as_of

    try:
        as_of_dt = parse_as_of(body["as_of"])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    if as_of_dt is None:
        return JSONResponse({"error": "'as_of' must be a timestamp"}, status_code=422)

    section_filter = body.get("sections")
    if section_filter is not None and (
        not isinstance(section_filter, list) or not all(isinstance(s, str) for s in section_filter)
    ):
        return JSONResponse({"error": "'sections' must be a list of strings"}, status_code=422)
    section_set = set(section_filter) if section_filter else None

    dry_run = body.get("dry_run", True)
    if not isinstance(dry_run, bool):
        return JSONResponse({"error": "'dry_run' must be a boolean"}, status_code=422)

    if not admin_state.engine.pg_pool:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    # Reconstruct past config + read current DB overrides for the live effective view.
    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            past = await _reconstruct_effective_as_of(conn, as_of_dt)
            db_overrides: dict[str, Any] = {}
            rows = await conn.fetch(
                "SELECT key, value, (secret_enc IS NOT NULL) AS has_secret, is_secret FROM settings"
            )
            for row in rows:
                db_overrides[row["key"]] = row
    except Exception as e:
        logger.exception("Failed to load config for rollback: %s", e)
        return JSONResponse({"error": f"Database query failed: {e}"}, status_code=500)

    current = {
        key: get_effective_value(key, metadata, db_overrides)[0]
        for key, metadata in REGISTRY.items()
    }

    applied_diff: dict[str, dict[str, Any]] = {}
    skipped: dict[str, dict[str, Any]] = {}
    flagged_secrets: dict[str, dict[str, Any]] = {}

    for key, metadata in REGISTRY.items():
        if section_set is not None and metadata.section not in section_set:
            continue
        target = past[key]
        cur = current[key]
        if target == cur:
            continue  # no inverse change required

        if metadata.prod_locked:
            skipped[key] = {
                "reason": "prod_locked",
                "current_value": cur,
                "would_restore_to": target,
            }
            continue

        if metadata.is_secret:
            # Secret values are unrecoverable from the WORM log; never fabricate.
            flagged_secrets[key] = {
                "reason": "secret_rotated_since_as_of",
                "current_value": cur,
                "target_token": target,
            }
            continue

        applied_diff[key] = {"old_value": cur, "new_value": target}

    proposal: dict[str, Any] = {
        "as_of": as_of_dt.isoformat(),
        "dry_run": dry_run,
        "diff": applied_diff,
        "skipped_prod_locked": skipped,
        "flagged_secrets": flagged_secrets,
    }

    if dry_run:
        return JSONResponse(proposal)

    if not applied_diff:
        return JSONResponse({**proposal, "settings": {}, "note": "No applicable changes"})

    # Apply the inverse (non-secret) change-set through the normal PATCH path so
    # all guardrails/validation/WORM logging still apply.
    patch_body = {
        "settings": {k: {"value": v["new_value"]} for k, v in applied_diff.items()},
        "reason": f"rollback to {as_of_dt.isoformat()}",
    }
    patch_resp = await api_admin_settings_patch(_PatchRequestProxy(request, patch_body))
    patch_payload = json.loads(bytes(patch_resp.body).decode("utf-8"))
    return JSONResponse(
        {**proposal, "settings": patch_payload.get("settings", patch_payload)},
        status_code=patch_resp.status_code,
    )


async def api_admin_settings_get(request: Any) -> JSONResponse:
    """GET /api/admin/settings/{key}

    Returns detailed schema, metadata, and status for a single configuration setting.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    key = request.path_params.get("key")
    if not key or key not in REGISTRY:
        return JSONResponse({"error": f"Setting key '{key}' not found"}, status_code=404)

    metadata = REGISTRY[key]

    db_row = None
    if admin_state.engine.pg_pool:
        try:
            async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
                db_row = await conn.fetchrow(
                    "SELECT key, value, (secret_enc IS NOT NULL) AS has_secret, is_secret, updated_by, updated_at FROM settings WHERE key = $1",
                    key,
                )
        except Exception as e:
            logger.error("Failed to fetch settings key '%s' from Postgres: %s", key, e)

    db_overrides = {key: db_row} if db_row else {}
    effective_value, source, store_value_set = get_effective_value(key, metadata, db_overrides)

    key_detail = {
        "key": key,
        "type": metadata.type,
        "reload_class": metadata.reload_class,
        "is_secret": metadata.is_secret,
        "prod_locked": metadata.prod_locked,
        "effective_value": effective_value,
        "source": source,
        "store_value_set": store_value_set,
        "validation": get_validation_metadata(metadata),
        "description": metadata.description,
        "updated_by": None,
        "updated_at": None,
    }

    if store_value_set and db_row:
        key_detail["updated_by"] = db_row["updated_by"]
        if db_row["updated_at"]:
            key_detail["updated_at"] = db_row["updated_at"].astimezone(UTC).isoformat()

    return JSONResponse(key_detail)


async def api_admin_settings_patch(request: Any) -> JSONResponse:
    """PATCH /api/admin/settings

    Batch apply settings updates with per-key status.
    Enforces optimistic-concurrency guard expected_updated_at (409),
    production-lock guard prod_locked (403),
    and server-side metadata validation (422).
    Appends a signed config_changed WORM event (secrets redacted to set/unset).
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    # Accept both {"settings": { ... }} and direct flat dictionary updates
    if isinstance(body, dict) and "settings" in body:
        updates = body["settings"]
        reason = body.get("reason", "")
    elif isinstance(body, dict):
        updates = {k: v for k, v in body.items() if k != "reason"}
        reason = body.get("reason", "")
    else:
        return JSONResponse({"error": "Invalid settings update payload format"}, status_code=422)

    if not isinstance(updates, dict):
        return JSONResponse({"error": "Settings must be a dictionary"}, status_code=422)

    # Extract actor (agent_id)
    agent_id = "admin"
    ns_ctx = getattr(request.state, "namespace_ctx", None)
    if ns_ctx and ns_ctx.agent_id:
        agent_id = ns_ctx.agent_id
    else:
        agent_id = request.headers.get("x-nce-agent-id") or "admin"

    # Pre-fetch all current DB overrides for the keys in the batch in a single query
    db_overrides: dict[str, Any] = {}
    if admin_state.engine.pg_pool:
        try:
            async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
                rows = await conn.fetch(
                    "SELECT key, value, (secret_enc IS NOT NULL) AS has_secret, is_secret, updated_by, updated_at FROM settings WHERE key = ANY($1)",
                    list(updates.keys()),
                )
                for row in rows:
                    db_overrides[row["key"]] = row
        except Exception as e:
            logger.error("Failed to pre-fetch settings overrides: %s", e)

    results: dict[str, dict[str, Any]] = {}
    has_rejections = False
    valid_updates: dict[str, dict[str, Any]] = {}

    import uuid
    from datetime import datetime, timezone

    # First pass: Validate all keys in the batch
    for key, update_info in updates.items():
        if key not in REGISTRY:
            results[key] = {
                "status": "rejected",
                "error": f"Setting key '{key}' not found in registry",
                "status_code": 422,
            }
            has_rejections = True
            continue

        metadata = REGISTRY[key]

        # Extract value and expected_updated_at
        if isinstance(update_info, dict) and (
            "value" in update_info or "expected_updated_at" in update_info
        ):
            value = update_info.get("value")
            expected_updated_at = update_info.get("expected_updated_at")
        else:
            value = update_info
            expected_updated_at = None

        # 1. Guardrail check: prod_locked -> 403
        if metadata.prod_locked:
            results[key] = {
                "status": "rejected",
                "error": f"Setting '{key}' is production locked and cannot be updated dynamically",
                "status_code": 403,
            }
            has_rejections = True
            continue

        # 2. Optimistic-concurrency guard: stale expected_updated_at -> 409
        db_row = db_overrides.get(key)
        db_updated_at = db_row["updated_at"] if db_row else None

        # If expected_updated_at is provided (either as string or None), check it
        if isinstance(update_info, dict) and "expected_updated_at" in update_info:
            is_stale = False
            if expected_updated_at is not None:
                try:
                    expected_dt = datetime.fromisoformat(
                        expected_updated_at.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    if db_updated_at is None:
                        is_stale = True
                    else:
                        db_dt = db_updated_at.astimezone(timezone.utc)
                        if expected_dt != db_dt:
                            is_stale = True
                except Exception as e:
                    results[key] = {
                        "status": "rejected",
                        "error": f"Invalid expected_updated_at format: {e}",
                        "status_code": 422,
                    }
                    has_rejections = True
                    continue
            else:
                if db_updated_at is not None:
                    is_stale = True

            if is_stale:
                results[key] = {
                    "status": "rejected",
                    "error": f"Optimistic lock conflict: Setting '{key}' has been updated since last read",
                    "status_code": 409,
                }
                has_rejections = True
                continue

        # 3. Secret no-op check: if is_secret is True and value is "••••set", skip validation/update
        if metadata.is_secret and value == "••••set":
            valid_updates[key] = {
                "value": value,
                "noop": True,
                "metadata": metadata,
            }
            continue

        # 4. Validation check -> 422
        if not metadata.validator(value):
            results[key] = {
                "status": "rejected",
                "error": f"Validation failed for setting '{key}'",
                "status_code": 422,
            }
            has_rejections = True
            continue

        valid_updates[key] = {
            "value": value,
            "noop": False,
            "metadata": metadata,
        }

    # If any key failed validation/concurrency/lock, reject the entire batch
    if has_rejections:
        for key in updates.keys():
            if key not in results:
                results[key] = {
                    "status": "rejected",
                    "error": "Batch aborted due to rejections on other keys",
                    "status_code": 422,
                }
        return JSONResponse({"settings": results}, status_code=207)

    # Second pass: Apply changes transactionally
    from nce import settings_store
    from nce.event_log import append_event

    updated_keys: list[str] = []
    event_changes: dict[str, dict[str, Any]] = {}

    def redact_value(val: Any, is_secret: bool) -> Any:
        if not is_secret:
            return val
        if val is not None and val != "":
            return "••••set"
        return "••••unset"

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                for key, info in valid_updates.items():
                    if info.get("noop"):
                        metadata = info["metadata"]
                        if metadata.reload_class == "HOT":
                            status = "applied"
                        elif metadata.reload_class == "WARM":
                            status = "pending_reload"
                        else:
                            status = "pending_restart"
                        results[key] = {"status": status}
                        continue

                    metadata = info["metadata"]
                    value = info["value"]

                    db_row = db_overrides.get(key)
                    if db_row:
                        old_raw = db_row["value"]
                        if db_row["is_secret"]:
                            old_val = "••••set" if db_row["has_secret"] else None
                        else:
                            if isinstance(old_raw, str):
                                try:
                                    old_val = json.loads(old_raw)
                                except Exception:
                                    old_val = old_raw
                            else:
                                old_val = old_raw
                    else:
                        old_val = getattr(cfg, key, None)

                    await settings_store.set(
                        key,
                        value,
                        is_secret=metadata.is_secret,
                        section=metadata.section,
                        updated_by=agent_id,
                        conn=conn,
                    )
                    updated_keys.append(key)

                    old_redacted = redact_value(old_val, metadata.is_secret)
                    new_redacted = redact_value(value, metadata.is_secret)

                    event_changes[key] = {
                        "old_value": old_redacted,
                        "new_value": new_redacted,
                    }

                    if metadata.reload_class == "HOT":
                        status = "applied"
                    elif metadata.reload_class == "WARM":
                        status = "pending_reload"
                    else:
                        status = "pending_restart"
                    results[key] = {"status": status}

                if updated_keys:
                    ns_id = None
                    if ns_ctx and ns_ctx.namespace_id:
                        ns_id = ns_ctx.namespace_id

                    if not ns_id:
                        ns_id_raw = await conn.fetchval(
                            "SELECT id FROM namespaces WHERE slug = '_global_legacy'"
                        )
                        if not ns_id_raw:
                            ns_id_raw = await conn.fetchval(
                                "SELECT id FROM namespaces ORDER BY created_at ASC LIMIT 1"
                            )
                        if ns_id_raw:
                            ns_id = (
                                uuid.UUID(str(ns_id_raw))
                                if not isinstance(ns_id_raw, uuid.UUID)
                                else ns_id_raw
                            )

                    if not ns_id:
                        raise RuntimeError("No namespace found to log config_changed event")

                    await append_event(
                        conn=conn,
                        namespace_id=ns_id,
                        agent_id=agent_id,
                        event_type="config_changed",
                        params={
                            "actor": agent_id,
                            "reason": reason,
                            "changes": event_changes,
                        },
                    )

    except Exception as exc:
        logger.exception("Failed to apply settings update transactionally: %s", exc)
        return JSONResponse({"error": f"Database transaction failed: {exc}"}, status_code=500)

    # Post-commit: Invalidate and populate Redis + local cache
    active_redis = admin_state.engine.redis_client
    if active_redis and updated_keys:
        try:
            for key in updated_keys:
                await active_redis.hdel("nce:settings:overrides", key)
                await active_redis.publish("nce:settings:invalidate", key)
        except Exception as e:
            logger.warning("Failed to invalidate Redis cache post-commit: %s", e)

    for key in updated_keys:
        settings_store._local_cache.pop(key, None)

    return JSONResponse({"settings": results}, status_code=207)


async def api_admin_settings_reset(request: Any) -> JSONResponse:
    """POST /api/admin/settings/reset

    Reverts specified configuration settings to their environment or registry defaults by
    deleting database overrides. Logs signed config_reset events.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    keys: list[str] = []
    if isinstance(body, dict):
        if "key" in body:
            if isinstance(body["key"], str):
                keys = [body["key"]]
            else:
                return JSONResponse({"error": "Field 'key' must be a string"}, status_code=422)
        elif "keys" in body:
            if isinstance(body["keys"], list) and all(isinstance(k, str) for k in body["keys"]):
                keys = list(body["keys"])
            else:
                return JSONResponse(
                    {"error": "Field 'keys' must be a list of strings"}, status_code=422
                )
        else:
            return JSONResponse(
                {"error": "Payload must contain either 'key' or 'keys'"}, status_code=422
            )
    else:
        return JSONResponse({"error": "Payload must be a dictionary"}, status_code=422)

    for key in keys:
        if key not in REGISTRY:
            return JSONResponse(
                {"error": f"Setting key '{key}' not found in registry"}, status_code=404
            )
        metadata = REGISTRY[key]
        if metadata.prod_locked:
            return JSONResponse(
                {"error": f"Setting '{key}' is production locked and cannot be reset"},
                status_code=403,
            )

    # Extract actor (agent_id)
    agent_id = "admin"
    ns_ctx = getattr(request.state, "namespace_ctx", None)
    if ns_ctx and ns_ctx.agent_id:
        agent_id = ns_ctx.agent_id
    else:
        agent_id = request.headers.get("x-nce-agent-id") or "admin"

    # For each key, resolve what value it will revert to (env or registry default)
    resets: dict[str, Any] = {}
    for key in keys:
        metadata = REGISTRY[key]
        if key in os.environ:
            new_val = getattr(cfg, key, None)
            source = "env"
        else:
            new_val = getattr(cfg, key, None)
            source = "default"

        redacted_val: Any = None
        if metadata.is_secret:
            redacted_val = "••••set" if new_val is not None else "••••unset"
        else:
            redacted_val = new_val

        resets[key] = {
            "source": source,
            "new_value": redacted_val,
        }

    import uuid

    from nce import settings_store

    try:
        async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
            async with conn.transaction():
                # Perform DELETE
                await conn.execute("DELETE FROM settings WHERE key = ANY($1)", keys)

                # Fetch ns_id
                ns_id = None
                if ns_ctx and ns_ctx.namespace_id:
                    ns_id = ns_ctx.namespace_id

                if not ns_id:
                    ns_id_raw = await conn.fetchval(
                        "SELECT id FROM namespaces WHERE slug = '_global_legacy'"
                    )
                    if not ns_id_raw:
                        ns_id_raw = await conn.fetchval(
                            "SELECT id FROM namespaces ORDER BY created_at ASC LIMIT 1"
                        )
                    if ns_id_raw:
                        ns_id = (
                            uuid.UUID(str(ns_id_raw))
                            if not isinstance(ns_id_raw, uuid.UUID)
                            else ns_id_raw
                        )

                if not ns_id:
                    raise RuntimeError("No namespace found to log config_reset event")

                from nce.event_log import append_event

                await append_event(
                    conn=conn,
                    namespace_id=ns_id,
                    agent_id=agent_id,
                    event_type="config_reset",
                    params={
                        "actor": agent_id,
                        "resets": resets,
                    },
                )
    except Exception as exc:
        logger.exception("Failed to reset settings transactionally: %s", exc)
        return JSONResponse({"error": f"Database transaction failed: {exc}"}, status_code=500)

    # Post-commit cache invalidation
    active_redis = admin_state.engine.redis_client
    if active_redis:
        try:
            for key in keys:
                await active_redis.hdel("nce:settings:overrides", key)
                await active_redis.publish("nce:settings:invalidate", key)
        except Exception as e:
            logger.warning("Failed to invalidate Redis cache post-commit: %s", e)

    for key in keys:
        settings_store._local_cache.pop(key, None)

    return JSONResponse({"status": "reset", "keys": keys})


async def api_admin_settings_pending(request: Any) -> JSONResponse:
    """GET /api/admin/settings/pending

    Returns a list of overridden settings keys that have reload_class="COLD",
    meaning they require a system restart to take effect.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    db_keys: list[str] = []
    if admin_state.engine.pg_pool:
        try:
            async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
                rows = await conn.fetch("SELECT key FROM settings")
                db_keys = [r["key"] for r in rows]
        except Exception as e:
            logger.error("Failed to fetch settings overrides for pending: %s", e)
            return JSONResponse({"error": f"Database query failed: {e}"}, status_code=500)

    cold_keys = []
    for key in db_keys:
        if key in REGISTRY:
            metadata = REGISTRY[key]
            if metadata.reload_class == "COLD":
                cold_keys.append(key)

    return JSONResponse({"keys": cold_keys})


async def api_admin_settings_reload(request: Any) -> JSONResponse:
    """POST /api/admin/settings/reload

    Triggers WARM reloads for selected domains (cron, llm, observability, a2a) and logs
    signed config_reload events.
    """
    if not admin_state.engine:
        return JSONResponse({"error": "Engine not connected"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    if not isinstance(body, dict) or "domains" not in body:
        return JSONResponse({"error": "Payload must contain a 'domains' list"}, status_code=422)

    domains = body["domains"]
    if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains):
        return JSONResponse({"error": "Field 'domains' must be a list of strings"}, status_code=422)

    VALID_DOMAINS = {"cron", "llm", "observability", "a2a"}
    for d in domains:
        if d not in VALID_DOMAINS:
            return JSONResponse(
                {
                    "error": f"Invalid domain '{d}'. Valid domains are: cron, llm, observability, a2a"
                },
                status_code=422,
            )

    from nce import settings_store

    settings_store._local_cache.clear()

    outcomes: dict[str, dict[str, Any]] = {}
    successful_domains: list[str] = []

    for domain in domains:
        if domain == "cron":
            try:
                from nce.cron import reschedule_jobs

                msg = await reschedule_jobs()
                outcomes["cron"] = {"status": "success", "message": msg}
                successful_domains.append("cron")
            except Exception as e:
                logger.exception("Failed to reload domain 'cron'")
                outcomes["cron"] = {"status": "error", "message": str(e)}

        elif domain == "llm":
            try:
                from nce.providers.factory import rebuild_provider_cache

                msg = rebuild_provider_cache()
                outcomes["llm"] = {"status": "success", "message": msg}
                successful_domains.append("llm")
            except Exception as e:
                logger.exception("Failed to reload domain 'llm'")
                outcomes["llm"] = {"status": "error", "message": str(e)}

        elif domain == "observability":
            try:
                import nce.observability

                nce.observability._tracer_initialized = False
                nce.observability.init_observability()
                outcomes["observability"] = {
                    "status": "success",
                    "message": "observability initialized",
                }
                successful_domains.append("observability")
            except Exception as e:
                logger.exception("Failed to reload domain 'observability'")
                outcomes["observability"] = {"status": "error", "message": str(e)}

        elif domain == "a2a":
            try:
                from nce.jwt_auth import _load_public_key

                _load_public_key.cache_clear()
                outcomes["a2a"] = {"status": "success", "message": "jwt public key cache cleared"}
                successful_domains.append("a2a")
            except Exception as e:
                logger.exception("Failed to reload domain 'a2a'")
                outcomes["a2a"] = {"status": "error", "message": str(e)}

    last_event_id = None
    if successful_domains:
        # Extract actor (agent_id)
        agent_id = "admin"
        ns_ctx = getattr(request.state, "namespace_ctx", None)
        if ns_ctx and ns_ctx.agent_id:
            agent_id = ns_ctx.agent_id
        else:
            agent_id = request.headers.get("x-nce-agent-id") or "admin"

        import uuid

        try:
            async with admin_state.engine.pg_pool.acquire(timeout=10.0) as conn:
                async with conn.transaction():
                    # Fetch ns_id
                    ns_id = None
                    if ns_ctx and ns_ctx.namespace_id:
                        ns_id = ns_ctx.namespace_id

                    if not ns_id:
                        ns_id_raw = await conn.fetchval(
                            "SELECT id FROM namespaces WHERE slug = '_global_legacy'"
                        )
                        if not ns_id_raw:
                            ns_id_raw = await conn.fetchval(
                                "SELECT id FROM namespaces ORDER BY created_at ASC LIMIT 1"
                            )
                        if ns_id_raw:
                            ns_id = (
                                uuid.UUID(str(ns_id_raw))
                                if not isinstance(ns_id_raw, uuid.UUID)
                                else ns_id_raw
                            )

                    if not ns_id:
                        raise RuntimeError("No namespace found to log config_reload event")

                    from nce.event_log import append_event

                    for dom in successful_domains:
                        res = await append_event(
                            conn=conn,
                            namespace_id=ns_id,
                            agent_id=agent_id,
                            event_type="config_reload",
                            params={
                                "actor": agent_id,
                                "domain": dom,
                                "message": outcomes[dom]["message"],
                            },
                        )
                        if res and getattr(res, "event_id", None):
                            last_event_id = str(res.event_id)
        except Exception as exc:
            logger.exception("Failed to write config_reload events transactionally: %s", exc)
            return JSONResponse({"error": f"Database transaction failed: {exc}"}, status_code=500)

    response_payload: dict[str, Any] = {
        "status": "success",
        "outcomes": outcomes,
    }
    if last_event_id:
        response_payload["last_event_id"] = last_event_id

    return JSONResponse(response_payload)
