"""
nce/vertical_modules/sales/signing.py
======================================
Sales Quote Signing orchestration (Batch 090).
Coordinates signature request via C7 SignTransport, handling signed callbacks,
freezing quote baselines, and triggering Project conversion.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, cast
from uuid import UUID

from nce.db_utils import scoped_pg_session
from nce.signing_service import ManualTransport, TransportMethod
from nce.vertical_modules.project.convert import do_convert_signed_quote
from nce.vertical_modules.sales.baseline import do_freeze_baseline

log = logging.getLogger("nce.vertical_modules.sales.signing")

# Global in-memory transport instance for manual signing
_transport = ManualTransport()


class MissingSignedAmountError(ValueError):
    """A signed quote carries no usable margin/total, so no baseline can be frozen.

    Money never gets a fabricated default (§9.3): the frozen baseline is a
    one-time, immutable write that downstream commission, project and margin
    decisions treat as ground truth, so an invented figure would become
    permanent, unflagged "truth". Fail closed instead and let an operator
    correct the quote and re-fire the callback (do_freeze_baseline is
    idempotent, so a retry is safe).
    """


def _require_money_field(
    quote: dict[str, Any],
    keys: tuple[str, ...],
    *,
    label: str,
    quote_id: str,
    minimum: float,
    maximum: float | None = None,
) -> float:
    """Return the first PRESENT key in `keys` as a validated float.

    Presence is checked with `is not None`, never truthiness: a legitimate 0
    (a zero-margin or zero-sum signed quote) must not fall through to the next
    key or to a default. Non-numeric, NaN/Inf and out-of-range values fail
    closed rather than poisoning an immutable baseline.
    """
    for key in keys:
        raw = quote.get(key)
        if raw is None:
            continue
        if isinstance(raw, bool):  # bool is an int subclass; never a money value
            raise MissingSignedAmountError(
                f"{label} for quote {quote_id} is a boolean ({key}={raw!r}), not a number"
            )
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise MissingSignedAmountError(
                f"{label} for quote {quote_id} is not numeric ({key}={raw!r}): {exc}"
            ) from exc
        if not math.isfinite(value):
            raise MissingSignedAmountError(
                f"{label} for quote {quote_id} is not finite ({key}={raw!r})"
            )
        if value < minimum or (maximum is not None and value > maximum):
            bound = f"[{minimum}, {maximum}]" if maximum is not None else f">= {minimum}"
            raise MissingSignedAmountError(
                f"{label} for quote {quote_id} is out of range {bound} ({key}={raw!r})"
            )
        return value

    raise MissingSignedAmountError(
        f"{label} for quote {quote_id} is missing: none of {list(keys)} present on the "
        "signed quote. Refusing to freeze a baseline from a fabricated amount; correct "
        "the quote and re-fire the signed callback."
    )


async def do_request_signature(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Request a signature for a quote.

    Params:
      namespace_id (str | UUID): namespace id
      quote_id (str): identifier of the quote
      doc_bytes (bytes, optional): raw document bytes to sign (defaults to dummy bytes)
      signer (dict, optional): signer details (name, email)
      method (str, optional): transport method (defaults to "manual")

    Returns:
      dict: details of the created signing session.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    quote_id = params.get("quote_id")
    if not quote_id:
        raise ValueError("quote_id is required")

    doc_bytes = params.get("doc_bytes") or b"Dummy Quote Document"
    signer = params.get("signer") or {"name": "Test Signer", "email": "signer@example.com"}
    method = params.get("method") or "manual"
    if method not in ("oneflow", "criipto", "signicat", "manual"):
        raise ValueError(f"Invalid signing method: {method}")
    tm_method = cast(TransportMethod, method)

    # 1. Request signature from C7 transport
    session = _transport.request_signature(doc_bytes, signer, tm_method)
    session_id = session["session_id"]

    # 2. Update quote record in sales_read_model with session details
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT manual, source_json
            FROM sales_read_model
            WHERE namespace_id = $1
              AND entity = 'quotes'
              AND source_id = $2
            """,
            str(ns_uuid),
            quote_id,
        )

        if not row:
            raise ValueError(f"Quote {quote_id} not found in read model")

        manual = row["manual"] or {}
        if isinstance(manual, str):
            manual = json.loads(manual)

        manual["signing_session_id"] = session_id
        manual["signing_status"] = "pending"
        manual["signing_fingerprint"] = session["fingerprint"]
        manual["signer_name"] = signer.get("name", "Unknown")

        await conn.execute(
            """
            UPDATE sales_read_model
            SET manual = $1::jsonb,
                updated_at = now()
            WHERE namespace_id = $2
              AND entity = 'quotes'
              AND source_id = $3
            """,
            json.dumps(manual),
            str(ns_uuid),
            quote_id,
        )

    return session


async def do_on_signed_callback(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handle the signed callback from the signing service.

    This functions freezes the quote baseline (W8) and triggers project conversion (M7.W4).
    Idempotent: runs exactly once per quote_id/session_id.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    session_id = params.get("session_id")
    if not session_id:
        raise ValueError("session_id is required")

    callback_payload = params.get("callback_payload") or {}

    # 1. Transition transport session state
    _ = _transport.on_signed(session_id, callback_payload)

    # 2. Fetch quote associated with session_id
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT source_id, source_json, manual
            FROM sales_read_model
            WHERE namespace_id = $1
              AND entity = 'quotes'
              AND (manual->>'signing_session_id' = $2 OR source_json->>'signing_session_id' = $2)
            """,
            str(ns_uuid),
            session_id,
        )

        if not row:
            raise ValueError(f"Quote not found for session_id: {session_id}")

        quote_id = row["source_id"]
        source_json = row["source_json"] or {}
        if isinstance(source_json, str):
            source_json = json.loads(source_json)
        manual = row["manual"] or {}
        if isinstance(manual, str):
            manual = json.loads(manual)

        merged_quote = {**(source_json or {}), **(manual or {})}

        # Short-circuit if already signed & processed
        if manual.get("signing_status") == "signed":
            log.info("Quote %s already processed as signed", quote_id)
            # Find existing project label (idempotency check)
            project_lbl = f"PROJECT:{quote_id.upper()}"
            return {
                "ok": True,
                "quote_id": quote_id,
                "session_id": session_id,
                "baseline_frozen": True,
                "project_id": project_lbl,
                "already_processed": True,
            }

        # 3. Call do_freeze_baseline (idempotent).
        # The baseline is immutable once written, so every figure must come from the
        # signed quote itself — never a default. Missing/malformed amounts raise
        # MissingSignedAmountError rather than freezing an invented number.
        signed_margin_pct = _require_money_field(
            merged_quote,
            ("margin", "signed_margin_pct"),
            label="signed_margin_pct",
            quote_id=quote_id,
            minimum=0.0,
            maximum=1.0,
        )
        signed_total_nok = _require_money_field(
            merged_quote,
            ("total_price", "signed_total_nok", "unit_price"),
            label="signed_total_nok",
            quote_id=quote_id,
            minimum=0.0,
        )

        freeze_res = await do_freeze_baseline(
            conn,
            ns_uuid,
            quote_id=quote_id,
            signed_margin_pct=signed_margin_pct,
            signed_total_nok=signed_total_nok,
        )

    # 4. Trigger Project convert A2A bridge (idempotent)
    # Call outside transaction to avoid nested connection holds.
    convert_res = await do_convert_signed_quote(
        engine,
        {
            "namespace_id": str(ns_uuid),
            "quote_id": quote_id,
            "signed_by": merged_quote.get("signer_name") or "Customer",
            "signature_ref": session_id,
        },
    )

    # 5. Mark quote as signed in read model
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        manual["signing_status"] = "signed"
        await conn.execute(
            """
            UPDATE sales_read_model
            SET manual = $1::jsonb,
                updated_at = now()
            WHERE namespace_id = $2
              AND entity = 'quotes'
              AND source_id = $3
            """,
            json.dumps(manual),
            str(ns_uuid),
            quote_id,
        )

    return {
        "ok": True,
        "quote_id": quote_id,
        "session_id": session_id,
        "baseline_frozen": freeze_res.get("ok", False),
        "project_id": convert_res.get("project_id"),
        "already_processed": False,
    }


async def do_on_declined_callback(
    engine: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Handle a declined signature callback."""
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    session_id = params.get("session_id")
    if not session_id:
        raise ValueError("session_id is required")

    callback_payload = params.get("callback_payload") or {}

    # Transition transport session
    _ = _transport.on_declined(session_id, callback_payload)

    # Update quote status to declined
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT source_id, manual
            FROM sales_read_model
            WHERE namespace_id = $1
              AND entity = 'quotes'
              AND (manual->>'signing_session_id' = $2)
            """,
            str(ns_uuid),
            session_id,
        )

        if not row:
            raise ValueError(f"Quote not found for session_id: {session_id}")

        quote_id = row["source_id"]
        manual = row["manual"] or {}
        if isinstance(manual, str):
            manual = json.loads(manual)

        manual["signing_status"] = "declined"

        await conn.execute(
            """
            UPDATE sales_read_model
            SET manual = $1::jsonb,
                updated_at = now()
            WHERE namespace_id = $2
              AND entity = 'quotes'
              AND source_id = $3
            """,
            json.dumps(manual),
            str(ns_uuid),
            quote_id,
        )

    return {
        "ok": True,
        "quote_id": quote_id,
        "session_id": session_id,
        "status": "declined",
    }
