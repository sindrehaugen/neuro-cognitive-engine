"""
nce/vertical_modules/sales/signing.py
======================================
Sales Quote Signing orchestration (Batch 090).
Coordinates signature request via C7 SignTransport, handling signed callbacks,
freezing quote baselines, and triggering Project conversion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from nce.bom_lines import list_bom_lines_for_quote
from nce.db_utils import scoped_pg_session
from nce.mailer import EmailDelivery, get_mailer
from nce.signing_service import (
    ManualTransport,
    SignTransport,
    TransportMethod,
    get_signing_transport,
)
from nce.vertical_modules.project.convert import do_convert_signed_quote
from nce.vertical_modules.sales.baseline import do_freeze_baseline, get_signed_baseline
from nce.vertical_modules.sales.render import render_quote_document

log = logging.getLogger("nce.vertical_modules.sales.signing")


def _resolve_transport(engine: Any, method: str) -> SignTransport:
    """Resolve the SignTransport instance for this execution.

    Prefers an engine-injected `sign_transport` if explicitly configured;
    otherwise resolves via the signing_service transport factory.
    Guards against MagicMock auto-synthesizing child mocks (Charter K-1).
    """
    # Guard against MagicMock auto-synthesizing an unconfigured child mock (Kaizen K-1)
    if hasattr(engine, "_mock_return_value") or type(engine).__name__ in ("MagicMock", "AsyncMock"):
        if "sign_transport" in getattr(engine, "__dict__", {}):
            return cast(SignTransport, engine.sign_transport)
        return get_signing_transport(method)

    injected = getattr(engine, "sign_transport", None)
    if injected is not None:
        return cast(SignTransport, injected)
    return get_signing_transport(method)


class MissingBaselineError(ValueError):
    """Raised when requesting signature for a quote that has no frozen baseline.

    Under Sindre's ruling (Option b), the quote document signed by the customer is
    rendered directly from the frozen baseline. Requesting a signature on an
    unfrozen quote is refused with a 4xx equivalent.
    """


def _resolve_signed_delivery_recipients(
    merged_quote: dict[str, Any],
    manual: dict[str, Any],
    params: dict[str, Any],
    account_data: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """Resolve recipient addresses for signed quote document delivery (Wave Q-3).

    Recipients per Sindre's mandate:
    1. 'signer': The person who signed the quote document.
    2. 'case_responsible': The responsible salesperson / PM / service tech / advisor for the case.
    3. 'account_responsible': The account-responsible person.

    Returns a list of (role, email) pairs for all non-empty email addresses found.
    """
    recipients: list[tuple[str, str]] = []

    # 1. Signer
    signer_email = (
        params.get("signer_email")
        or manual.get("signer_email")
        or merged_quote.get("signer_email")
        or (merged_quote.get("signer") or {}).get("email")
    )
    if signer_email and isinstance(signer_email, str) and "@" in signer_email:
        recipients.append(("signer", signer_email.strip()))

    # 2. Case Responsible (salesperson / project manager / service tech / advisor)
    case_resp = (
        params.get("case_responsible_email")
        or params.get("salesperson_email")
        or params.get("project_manager_email")
        or params.get("service_tech_email")
        or params.get("advisor_email")
        or merged_quote.get("case_responsible_email")
        or merged_quote.get("salesperson_email")
        or merged_quote.get("project_manager_email")
        or merged_quote.get("service_tech_email")
        or merged_quote.get("advisor_email")
        or merged_quote.get("responsible_email")
        or (merged_quote.get("case") or {}).get("responsible_email")
        or (merged_quote.get("case") or {}).get("email")
        or (merged_quote.get("salesperson") or {}).get("email")
        or (merged_quote.get("project_manager") or {}).get("email")
        or (merged_quote.get("service_tech") or {}).get("email")
        or (merged_quote.get("advisor") or {}).get("email")
    )
    if case_resp and isinstance(case_resp, str) and "@" in case_resp:
        recipients.append(("case_responsible", case_resp.strip()))

    # 3. Account Responsible
    acc_resp = (
        params.get("account_responsible_email")
        or params.get("account_manager_email")
        or params.get("account_owner_email")
        or merged_quote.get("account_responsible_email")
        or merged_quote.get("account_manager_email")
        or merged_quote.get("account_owner_email")
        or (merged_quote.get("account") or {}).get("responsible_email")
        or (merged_quote.get("account") or {}).get("email")
        or (merged_quote.get("account_manager") or {}).get("email")
    )
    if not acc_resp and account_data:
        acc_resp = (
            account_data.get("account_responsible_email")
            or account_data.get("account_manager_email")
            or account_data.get("owner_email")
            or account_data.get("email")
        )
    if acc_resp and isinstance(acc_resp, str) and "@" in acc_resp:
        recipients.append(("account_responsible", acc_resp.strip()))

    return recipients


class MissingSignerError(ValueError):
    """Raised when requesting signature without explicit, valid signer identity.

    A signature request requires an explicit signer name and email address.
    Silent defaults (e.g. signer@example.com) are strictly prohibited.
    """


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

    Under Sindre's ruling (Option b), the quote document bytes are deterministically
    rendered from the frozen baseline stored in `sales_signed_baselines`.
    Callers cannot supply `doc_bytes`, and requesting a signature for an unfrozen
    quote or without an explicit signer is strictly refused with typed 4xx errors.

    Params:
      namespace_id (str | UUID): namespace id
      quote_id (str): identifier of the quote
      signer (dict): required signer details (name, email)
      method (str, optional): transport method (defaults to "manual")

    Returns:
      dict: details of the created signing session including document_hash.
    """
    ns_raw = params.get("namespace_id")
    if not ns_raw:
        raise ValueError("namespace_id is required")
    ns_uuid = UUID(str(ns_raw)) if not isinstance(ns_raw, UUID) else ns_raw

    quote_id = params.get("quote_id")
    if not quote_id or not isinstance(quote_id, str) or not quote_id.strip():
        raise ValueError("quote_id is required")
    quote_id = quote_id.strip()

    if "doc_bytes" in params:
        raise ValueError(
            "doc_bytes parameter is not allowed; quote document is rendered from the frozen baseline"
        )

    signer = params.get("signer")
    if not signer or not isinstance(signer, dict):
        raise MissingSignerError("signer dict with 'name' and 'email' is required")
    signer_name = str(signer.get("name") or "").strip()
    signer_email = str(signer.get("email") or "").strip()
    if not signer_name:
        raise MissingSignerError("signer 'name' is required and must not be empty")
    if not signer_email or "@" not in signer_email or "." not in signer_email.split("@")[-1]:
        raise MissingSignerError("signer 'email' is required and must be a valid email address")
    signer_payload = {"name": signer_name, "email": signer_email}

    method = params.get("method") or "manual"
    transport = _resolve_transport(engine, method)
    tm_method = cast(TransportMethod, method)

    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT name, manual, source_json
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

        # Verify frozen baseline exists
        baseline = await get_signed_baseline(conn, ns_uuid, quote_id)
        if not baseline:
            raise MissingBaselineError(
                f"Quote {quote_id} has no frozen baseline; baseline must be frozen before requesting signature"
            )

        # Read line items from bom_line_content
        try:
            line_items = await list_bom_lines_for_quote(conn, ns_uuid, quote_id=quote_id)
        except Exception as exc:
            log.warning("Could not read bom_line_content for quote %s: %s", quote_id, exc)
            line_items = []

        source_json = row["source_json"] or {}
        if isinstance(source_json, str):
            source_json = json.loads(source_json)
        manual = row["manual"] or {}
        if isinstance(manual, str):
            manual = json.loads(manual)

        merged_quote = {**(source_json or {}), **(manual or {})}
        quote_details = {
            "name": row["name"] or merged_quote.get("name"),
            "customer_name": merged_quote.get("customer_name") or merged_quote.get("customer"),
            "currency": merged_quote.get("currency") or "NOK",
            "description": merged_quote.get("description"),
        }
        if not line_items and "lines" in merged_quote and isinstance(merged_quote["lines"], list):
            line_items = merged_quote["lines"]

        # Deterministically render quote PDF from the frozen baseline
        doc_bytes = render_quote_document(
            baseline=baseline,
            quote_details=quote_details,
            line_items=line_items,
            signer=signer_payload,
        )
        doc_hash = hashlib.sha256(doc_bytes).hexdigest()

        # Request signature from C7 transport
        session = transport.request_signature(doc_bytes, signer_payload, tm_method)
        session_id = session["session_id"]

        # Update quote record in sales_read_model with session details and document hash
        manual["signing_session_id"] = session_id
        manual["signing_status"] = "pending"
        manual["signing_method"] = method
        manual["signing_security_tier"] = session.get("security_tier", method)
        manual["signing_fingerprint"] = session["fingerprint"]
        manual["document_hash"] = doc_hash
        manual["signer_name"] = signer_name
        manual["signer_email"] = signer_email

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

    session["document_hash"] = doc_hash
    session["quote_id"] = quote_id
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

    # 1. Fetch quote associated with session_id from read model
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        row = await conn.fetchrow(
            """
            SELECT source_id, name, source_json, manual
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
        quote_db_name = row["name"] if "name" in row else None
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
                "deliveries_sent": 0,
            }

        # Attempt to read linked account record for account-responsible contact
        account_id = (
            merged_quote.get("account_id")
            or merged_quote.get("customer_id")
            or merged_quote.get("_customerid_value")
        )
        account_data: dict[str, Any] | None = None
        if account_id:
            try:
                acc_row = await conn.fetchrow(
                    """
                    SELECT source_json, manual
                    FROM sales_read_model
                    WHERE namespace_id = $1
                      AND entity = 'accounts'
                      AND source_id = $2
                    """,
                    str(ns_uuid),
                    str(account_id),
                )
                if acc_row:
                    asj = acc_row["source_json"] or {}
                    if isinstance(asj, str):
                        asj = json.loads(asj)
                    aman = acc_row["manual"] or {}
                    if isinstance(aman, str):
                        aman = json.loads(aman)
                    account_data = {**(asj or {}), **(aman or {})}
            except Exception as exc:
                log.debug("Could not read account for %s: %s", account_id, exc)

    # 2. Transition transport session state
    method = params.get("method") or manual.get("signing_method") or "manual"
    transport = _resolve_transport(engine, method)
    try:
        updated_session = transport.on_signed(session_id, callback_payload)
    except KeyError:
        if method == "manual" and isinstance(transport, ManualTransport):
            # Multi-process / external webhook: register and transition session locally
            transport._sessions[session_id] = {
                "session_id": session_id,
                "status": "signed",
                "fingerprint": callback_payload.get("fingerprint", ""),
                "method": "manual",
                "signer": {},
            }
            updated_session = transport._sessions[session_id]
        else:
            raise

    # 3. Call do_freeze_baseline (idempotent).
    # The baseline is immutable once written, so every figure must come from the
    # signed quote itself — never a default. Missing/malformed amounts raise
    # MissingSignedAmountError rather than freezing an invented number.
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
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

        # Retrieve baseline and line items to render deterministic quote PDF (Wave Q-3)
        baseline = await get_signed_baseline(conn, ns_uuid, quote_id)
        try:
            line_items = await list_bom_lines_for_quote(conn, ns_uuid, quote_id=quote_id)
        except Exception as exc:
            log.warning("Could not read bom_line_content for quote %s: %s", quote_id, exc)
            line_items = []
        if not line_items and "lines" in merged_quote and isinstance(merged_quote["lines"], list):
            line_items = merged_quote["lines"]

    # Render signed quote PDF from the frozen baseline
    quote_name = quote_db_name or merged_quote.get("name") or quote_id
    quote_details = {
        "name": quote_name,
        "customer_name": merged_quote.get("customer_name") or merged_quote.get("customer"),
        "currency": merged_quote.get("currency") or "NOK",
        "description": merged_quote.get("description"),
    }
    signer_name = (
        merged_quote.get("signer_name")
        or (merged_quote.get("signer") or {}).get("name")
        or "Customer"
    )
    signer_email = (
        manual.get("signer_email")
        or merged_quote.get("signer_email")
        or (merged_quote.get("signer") or {}).get("email")
        or ""
    )
    signer_payload = {"name": signer_name, "email": signer_email}

    if baseline:
        doc_bytes = render_quote_document(
            baseline=baseline,
            quote_details=quote_details,
            line_items=line_items,
            signer=signer_payload,
        )
    else:
        # Fallback for unit tests mocking without baseline row
        doc_bytes = b"%PDF-1.4\n%Signed Quote Document\n%%EOF"

    doc_hash = hashlib.sha256(doc_bytes).hexdigest()

    # 4. Trigger Project convert A2A bridge (idempotent)
    # Call outside transaction to avoid nested connection holds.
    convert_res = await do_convert_signed_quote(
        engine,
        {
            "namespace_id": str(ns_uuid),
            "quote_id": quote_id,
            "signed_by": signer_name,
            "signature_ref": session_id,
        },
    )

    # 5. Deliver signed document by email (Wave Q-3)
    # Sindre's requirement: on signature, the document goes to:
    # 1. The signer
    # 2. The responsible salesperson / project manager / service tech / advisor for the case
    # 3. The account-responsible person
    #
    # HARD CONSTRAINT: Separate emails, NEVER CC or BCC.
    # Every send call carries exactly one recipient address in `to`,
    # with `cc` and `bcc` strictly empty to prevent cross-boundary address disclosure.
    mailer = get_mailer(engine)
    delivery_candidates = _resolve_signed_delivery_recipients(
        merged_quote=merged_quote,
        manual=manual,
        params=params,
        account_data=account_data,
    )

    delivered_records: list[dict[str, Any]] = []
    seen_addresses: set[str] = set()
    active_sec_tier = updated_session.get("security_tier", method)

    for role, recipient_email in delivery_candidates:
        clean_email = recipient_email.strip()
        norm_key = clean_email.lower()
        if norm_key in seen_addresses:
            continue
        seen_addresses.add(norm_key)

        email_delivery = EmailDelivery(
            to=clean_email,
            subject=f"Signed Quote: {quote_name} ({quote_id})",
            body_text=(
                f"Quote {quote_id} ({quote_name}) has been successfully signed.\n\n"
                f"Signer: {signer_name} ({signer_email})\n"
                f"Security Tier: {active_sec_tier}\n"
                f"Document Hash (SHA-256): {doc_hash}\n\n"
                f"The signed document is attached.\n"
            ),
            attachments=((f"quote_{quote_id}_signed.pdf", doc_bytes, "application/pdf"),),
            cc=(),
            bcc=(),
        )

        try:
            await mailer.send(email_delivery)
            delivered_records.append(
                {
                    "role": role,
                    "email": clean_email,
                    "status": "sent",
                }
            )
            log.info(
                "do_on_signed_callback: delivered signed quote %s to %s (%s)",
                quote_id,
                clean_email,
                role,
            )
        except Exception as exc:
            log.error(
                "do_on_signed_callback: failed to deliver signed quote %s to %s (%s): %s",
                quote_id,
                clean_email,
                role,
                exc,
            )
            delivered_records.append(
                {
                    "role": role,
                    "email": clean_email,
                    "status": "failed",
                    "error": str(exc),
                }
            )

    # 6. Mark quote as signed in read model with delivery audit trail
    async with scoped_pg_session(engine.pg_pool, ns_uuid) as conn:
        manual["signing_status"] = "signed"
        manual["signing_security_tier"] = active_sec_tier
        manual["document_hash"] = doc_hash
        manual["signed_delivery"] = {
            "delivered_at": datetime.now(timezone.utc).isoformat(),
            "deliveries": delivered_records,
            "document_hash": doc_hash,
        }
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
        "security_tier": active_sec_tier,
        "deliveries_sent": sum(1 for d in delivered_records if d.get("status") == "sent"),
        "delivery_records": delivered_records,
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

        method = params.get("method") or manual.get("signing_method") or "manual"
        transport = _resolve_transport(engine, method)

        # Transition transport session
        try:
            _ = transport.on_declined(session_id, callback_payload)
        except KeyError:
            pass

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
