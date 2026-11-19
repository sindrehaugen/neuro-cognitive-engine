"""Governance primitives for NCE connectors: replay fence, WORM audit & JCS responses."""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import os
import uuid
from collections.abc import Callable
from typing import Any

from nce.connectors.guards import redact_c8_allowlist
from nce.crypto.envelope import canonicalize_jcs
from nce.event_log import AppendResult, append_event

log = logging.getLogger("nce.connectors.governance")


class ReplaySandboxFenceError(RuntimeError):
    """Raised when an outbound mutation is attempted within a replay sandbox context."""


def check_replay_sandbox_env(headers_or_kwargs: Any = None) -> bool:
    """Return True when the current execution context or environment is a replay sandbox."""
    if os.getenv("NCE_REPLAY_SANDBOX", "").strip().lower() in ("1", "true", "yes"):
        return True

    if headers_or_kwargs is None:
        return False

    if isinstance(headers_or_kwargs, dict):
        if headers_or_kwargs.get("is_replay_sandbox"):
            return True
        val = headers_or_kwargs.get("x-nce-replay-sandbox") or headers_or_kwargs.get(
            "X-NCE-Replay-Sandbox"
        )
        if val and str(val).strip().lower() in ("1", "true", "yes"):
            return True
    elif getattr(headers_or_kwargs, "is_replay_sandbox", False):
        return True

    return False


async def assert_not_replay_sandbox(
    context_or_conn: Any = None,
    *,
    namespace_id: Any = None,
    is_replay_sandbox: bool = False,
) -> None:
    """Assert that the current context is NOT a replay sandbox before mutating externals."""
    if is_replay_sandbox:
        raise ReplaySandboxFenceError("Outbound mutations are blocked: is_replay_sandbox is True")

    if check_replay_sandbox_env(context_or_conn):
        raise ReplaySandboxFenceError(
            "Outbound mutations are blocked: replay sandbox environment detected"
        )

    if context_or_conn is not None and namespace_id is not None:
        fetchval = getattr(context_or_conn, "fetchval", None)
        if callable(fetchval):
            try:
                has_col = await fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'namespaces'
                          AND column_name = 'is_replay_sandbox'
                    )
                    """
                )
                if has_col:
                    ns_uuid = (
                        namespace_id
                        if isinstance(namespace_id, uuid.UUID)
                        else uuid.UUID(str(namespace_id))
                    )
                    is_sandbox = await fetchval(
                        "SELECT is_replay_sandbox FROM namespaces WHERE id = $1",
                        ns_uuid,
                    )
                    if is_sandbox:
                        raise ReplaySandboxFenceError(
                            f"Outbound mutations are blocked: namespace {namespace_id} "
                            "is marked as replay sandbox"
                        )
            except ReplaySandboxFenceError:
                raise
            except Exception as exc:
                log.debug("Replay sandbox DB probe skipped: %s", exc)


def compute_jcs_sha256(payload: Any) -> str:
    """Return lowercase SHA-256 hex digest of RFC 8785 canonical JSON."""
    return hashlib.sha256(canonicalize_jcs(payload)).hexdigest()


def governed_action_response(
    action_type: str,
    confirm: bool,
    payload: Any,
    result_if_confirmed: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a C8-redacted governance response with RFC 8785 JCS integrity hash."""
    redacted_payload = redact_c8_allowlist(payload)
    digest = compute_jcs_sha256({"action_type": action_type, "payload": redacted_payload})

    if not confirm:
        out: dict[str, Any] = {
            "status": "pending_approval",
            "action_type": action_type,
            "confirm_required": True,
            "preview": redacted_payload,
            "jcs_sha256": digest,
        }
        if isinstance(redacted_payload, dict) and "visibility" in redacted_payload:
            out.setdefault("visibility", redacted_payload["visibility"])
        out.update(extra)
        return out

    if isinstance(result_if_confirmed, dict):
        out = dict(redact_c8_allowlist(result_if_confirmed))
        out.setdefault("status", "confirmed")
        out.setdefault("action_type", action_type)
        out.setdefault("jcs_sha256", digest)
        out.update(extra)
        return out

    return {
        "status": "confirmed",
        "action_type": action_type,
        "result": redact_c8_allowlist(result_if_confirmed),
        "jcs_sha256": digest,
        **extra,
    }


async def record_connector_worm_event(
    conn: Any,
    *,
    namespace_id: uuid.UUID | str,
    agent_id: str,
    connector_slug: str,
    action: str,
    params: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
) -> AppendResult:
    """Append a C8-redacted, JCS-hashed 'connector_action' entry to the WORM event_log."""
    ns_uuid = namespace_id if isinstance(namespace_id, uuid.UUID) else uuid.UUID(str(namespace_id))
    clean_params = redact_c8_allowlist(params or {})
    clean_summary = redact_c8_allowlist(result_summary or {})
    event_params = {
        "connector_slug": connector_slug,
        "action": action,
        "jcs_sha256": compute_jcs_sha256(clean_params),
        "params": clean_params,
    }
    return await append_event(
        conn=conn,
        namespace_id=ns_uuid,
        agent_id=agent_id,
        event_type="connector_action",
        params=event_params,
        result_summary=clean_summary,
    )


def _extract_kw_or_arg(
    sig: inspect.Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    name: str,
    default: Any = None,
) -> Any:
    if name in kwargs:
        return kwargs[name]
    try:
        bound = sig.bind_partial(*args, **kwargs)
        return bound.arguments.get(name, default)
    except Exception:
        return default


def governed_action(
    connector_slug: str,
    action: str | None = None,
    *,
    mutation: bool = True,
    require_approval: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator enforcing replay-sandbox fence, confirmation gate, C8 redaction & WORM signing."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        action_name = action or fn.__name__
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _extract_kw_or_arg(sig, args, kwargs, "ctx")
            conn = _extract_kw_or_arg(sig, args, kwargs, "conn")
            arguments = _extract_kw_or_arg(sig, args, kwargs, "arguments")
            if isinstance(arguments, dict):
                namespace_id = arguments.get("namespace_id")
                dry_run = bool(arguments.get("dry_run", False))
                confirm = bool(arguments.get("confirm", not require_approval))
                is_sandbox = bool(arguments.get("is_replay_sandbox", False))
            else:
                namespace_id = _extract_kw_or_arg(sig, args, kwargs, "namespace_id")
                dry_run = bool(_extract_kw_or_arg(sig, args, kwargs, "dry_run", False))
                confirm = bool(
                    _extract_kw_or_arg(sig, args, kwargs, "confirm", not require_approval)
                )
                is_sandbox = bool(_extract_kw_or_arg(sig, args, kwargs, "is_replay_sandbox", False))

            probe_ctx = ctx if ctx is not None else conn
            if probe_ctx is None and isinstance(arguments, dict):
                probe_ctx = arguments

            if mutation:
                await assert_not_replay_sandbox(
                    probe_ctx,
                    namespace_id=namespace_id,
                    is_replay_sandbox=is_sandbox,
                )

            audit_payload = (
                dict(arguments)
                if isinstance(arguments, dict)
                else {k: v for k, v in kwargs.items() if k not in ("conn", "ctx")}
            )

            if dry_run or (require_approval and not confirm):
                return governed_action_response(
                    action_type=f"{connector_slug}.{action_name}",
                    confirm=False,
                    payload=audit_payload,
                    dry_run=dry_run,
                )

            raw_result = fn(*args, **kwargs)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result

            clean_result = redact_c8_allowlist(raw_result)
            digest = compute_jcs_sha256(clean_result)
            audit_event_id: str | None = None

            if (
                conn is not None
                and namespace_id is not None
                and callable(getattr(conn, "is_in_transaction", None))
                and conn.is_in_transaction()
            ):
                agent_id = str(
                    kwargs.get("agent_id")
                    or (ctx.get("agent_id") if isinstance(ctx, dict) else None)
                    or f"connector:{connector_slug}"
                )
                appended = await record_connector_worm_event(
                    conn,
                    namespace_id=namespace_id,
                    agent_id=agent_id,
                    connector_slug=connector_slug,
                    action=action_name,
                    params=audit_payload,
                    result_summary=(
                        clean_result if isinstance(clean_result, dict) else {"value": clean_result}
                    ),
                )
                audit_event_id = str(appended.event_id)

            if isinstance(clean_result, dict):
                clean_result.setdefault("jcs_sha256", digest)
                if audit_event_id is not None:
                    clean_result.setdefault("audit_event_id", audit_event_id)
            return clean_result

        return wrapper

    return decorator


__all__ = [
    "ReplaySandboxFenceError",
    "assert_not_replay_sandbox",
    "check_replay_sandbox_env",
    "compute_jcs_sha256",
    "governed_action",
    "governed_action_response",
    "record_connector_worm_event",
]
