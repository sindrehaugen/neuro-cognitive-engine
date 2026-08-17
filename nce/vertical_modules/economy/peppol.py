"""
nce/vertical_modules/economy/peppol.py
========================================
KID (Norwegian payment reference) generation + outbound EHF/PEPPOL, gated
behind ``NCE_ECONOMY_PEPPOL_ENABLED`` (off by default). Batch 128 / M8.W13 —
the final wave of Module 8, per ``docs/vertical_engines/08-economy-engine.md``
build phase B5 ("KID + outbound EHF behind the PEPPOL provider flag") and its
review round-2 hardening #6 ("EHF mandatory Jan 2027 = hard regulatory clock
... PEPPOL-provider decision as the gating dependency").

Discipline 1 — KID (MOD10 implemented; MOD11 pending)
-------------------------------------------------------
A Norwegian KID (kundeidentifikasjon) is a numeric payment reference ending
in a checksum digit. The spec (``docs/vertical_engines/08-economy-engine.md``)
names two check-digit variants, MOD10 and MOD11. **This module implements
MOD10** (the Luhn algorithm) today; **MOD11 is specified but not yet built.**
Which variant a given creditor requires is a per-agreement property of their
bank arrangement, not a global constant — that decision belongs to whoever
owns the PEPPOL provider selection (see Discipline 2) and has not been made
yet, so this module does not guess at it. :func:`do_generate_kid` and
:func:`do_validate_kid` take an explicit ``variant`` parameter (default
``"MOD10"``) so a caller can express the choice once it exists: passing
``"MOD11"`` raises ``NotImplementedError`` naming it as pending that
decision, and any other unrecognised value is rejected outright rather than
silently falling back to MOD10 — the same "fail toward refusal" rule as the
rest of this module (see :func:`_resolve_kid_variant`).

:func:`_mod10_check_digit` / :func:`_mod10_is_valid` are pinned against a
third-party reference vector (Wikipedia's canonical Luhn worked example,
"79927398713") in ``tests/test_economy_peppol_close.py`` — not just
self-consistency.

Discipline 2 — outbound EHF is a safety interlock, not a feature toggle
-------------------------------------------------------------------------
This is the ONLY path in the Economy module that could hand a real document
to a real external counterparty (roadmap 08's External blockers 🔴: "PEPPOL
prod in/out pending provider (Tickstar/Pagero) — sandbox-only today"). The
shape is deliberately copied from ``procurement/transports.py``'s
``PoTransport`` ABC + ``NetsetPoTransport`` 🔴 stub (Batch 54): a real-money
surface can ship safely when the transport is a stub and the ceiling is
zero. Concretely:

  * :func:`do_generate_ehf` ALWAYS builds the outbound XML — that part is
    pure formatting, never a network call, and is safe regardless of the
    flag.
  * With ``NCE_ECONOMY_PEPPOL_ENABLED`` false (the default), the function
    returns immediately after building the XML. It never calls
    :func:`_creds` (so ``NCE_ECONOMY_PEPPOL_API_KEY`` /
    ``NCE_ECONOMY_PEPPOL_BASE_URL`` are never resolved) and never
    constructs a :class:`PeppolTransport`. This is the exact property
    ``tests/test_economy_peppol_close.py::test_ehf_disabled_never_touches_transport``
    is written to catch: delete the flag check below and that test fails,
    because its patched ``_creds``/transport stand-ins raise the instant
    they are touched.
  * With the flag true, :class:`StubPeppolTransport` — mirroring
    ``NetsetPoTransport`` — always raises ``NotImplementedError``: the
    PEPPOL provider is not yet selected, so even an operator who flips the
    flag on cannot cause a real send. Callers that need to exercise the
    "flag on, send attempted" path in tests inject a fake ``transport=``
    (same dependency-injection seam ``procurement/po.py``'s
    ``do_submit_po`` already uses).

Credentials — env-only, resolved at call time (System Design's Lucid
precedent, Batch 65)
------------------------------------------------------------------------
:func:`_creds` mirrors ``system_design/lucid.py``'s ``_creds()`` exactly:
``resolve_secret()`` is called directly (bypassing ``nce.config.cfg``) so
monkeypatched env vars take effect without a module reload, and neither key
is ever logged. These two secrets are deliberately NOT added to
``nce/config.py`` / ``nce/settings_registry.py`` — same reasoning as Lucid's
``NCE_SYSTEM_DESIGN_LUCID_API_KEY``. Only the non-secret master gate
(``NCE_ECONOMY_PEPPOL_ENABLED``) and network selector
(``NCE_ECONOMY_PEPPOL_MODE``) are registered there, mirroring the
D365/NetBox vertical convention.

Money — ``Decimal``, never ``float`` (invariant 5)
------------------------------------------------------
:func:`_money_str` coerces the payable amount to ``Decimal`` and quantises
it to 2 dp exactly once, at the boundary — the same discipline
``ngaap.py``'s ``_quantise`` documents, reimplemented locally here (a
four-line helper) rather than imported: dependencies point inward, and this
is the same reimplementation choice ``cascade.py``/``graph.py`` already make
for the identical reason (see those modules' docstrings).

XML construction only — this module never parses XML
---------------------------------------------------------
``do_generate_ehf`` only ever WRITES a UBL document (``xml.etree.ElementTree``
build + ``tostring``); it never reads/parses one, so the ``defusedxml``
requirement in this wave's brief — which governs the PARSE path — does not
apply here. (The parse path already lives in ``ingestion.py``'s
``_parse_ehf_document``, already on ``defusedxml``.)
"""

from __future__ import annotations

import abc
import logging
from decimal import ROUND_HALF_UP, Decimal, DecimalException
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

from nce.config import cfg, resolve_secret

log = logging.getLogger("nce.vertical_modules.economy.peppol")

# ---------------------------------------------------------------------------
# Discipline 1 — KID (MOD10 / Luhn). See module docstring for the variant choice.
# ---------------------------------------------------------------------------

_KID_MIN_BASE_LEN = 1
# Norwegian KID numbers are at most 25 digits total (including the check
# digit); 24 leaves room for exactly one check digit within that ceiling.
_KID_MAX_BASE_LEN = 24
_KID_VARIANT = "MOD10"
_KID_VARIANT_MOD11 = "MOD11"  # specified, not yet implemented -- see _resolve_kid_variant


def _digits_only(value: Any, where: str) -> str:
    """Require *value* to be a ``str`` of ASCII digits; refuse everything else.

    ``str`` only — never ``int`` — because a KID base number commonly carries
    meaningful leading zeros (e.g. ``"00012345"``); accepting an ``int``
    would silently strip them and generate a check digit for the wrong
    number. ``bool`` is rejected too (``isinstance(True, int)`` is ``True``
    in Python, but a KID is never a boolean).
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{where}: must be a str of digits, got {type(value).__name__} {value!r}")
    stripped = value.strip()
    if not stripped or not stripped.isascii() or not stripped.isdigit():
        raise ValueError(f"{where}: must be a non-empty string of ASCII digits, got {value!r}")
    return stripped


def _resolve_kid_variant(variant: Any, where: str) -> str:
    """Validate a requested KID check-digit *variant*; return it unchanged if OK.

    Three outcomes, deliberately distinct:

    * ``"MOD10"`` (the default) -- the only implemented variant. Returned as-is.
    * ``"MOD11"`` -- a real, spec-named variant
      (``docs/vertical_engines/08-economy-engine.md``) that is honestly
      unavailable: raises ``NotImplementedError`` naming it as pending a
      bank-arrangement decision (which variant a creditor requires is a
      per-agreement property of their bank arrangement, not something this
      module can decide or guess). This is the same "declare the gap, don't
      hide it" shape as :class:`StubPeppolTransport` below.
    * anything else (a typo, ``""``, ``None``, a non-``str``) -- rejected
      with ``ValueError``. This module never falls back toward MOD10 for an
      unrecognised request; falling back toward the permissive default is
      exactly the "fails toward looseness" pattern this module avoids
      elsewhere (see :func:`_digits_only`, :func:`_money_str`).
    """
    if not isinstance(variant, str) or not variant:
        raise ValueError(
            f"{where}: variant must be a non-empty str, got {type(variant).__name__} {variant!r}"
        )
    if variant == _KID_VARIANT_MOD11:
        raise NotImplementedError(
            f"{where}: variant='MOD11' is specified "
            "(docs/vertical_engines/08-economy-engine.md) but not yet implemented -- "
            "which check-digit variant a creditor requires is a per-agreement "
            "bank-arrangement decision that has not been made yet. Only 'MOD10' "
            "ships today."
        )
    if variant != _KID_VARIANT:
        raise ValueError(
            f"{where}: unknown variant {variant!r} -- only 'MOD10' is implemented "
            "('MOD11' is specified but pending: it raises NotImplementedError, not this)"
        )
    return variant


def _mod10_check_digit(base: str) -> str:
    """Compute the MOD10 (Luhn) check digit for *base* (digits only, no check digit).

    Weights alternate 2, 1, 2, 1, ... starting at the RIGHTMOST digit of
    *base* (which becomes the second-from-right digit once the check digit
    is appended). A product >= 10 is reduced by subtracting 9 (equivalent to
    summing its two digits). The check digit is ``(10 - (total % 10)) % 10``.
    """
    total = 0
    for i, ch in enumerate(reversed(base)):
        digit = int(ch)
        weight = 2 if i % 2 == 0 else 1
        product = digit * weight
        if product >= 10:
            product -= 9
        total += product
    return str((10 - (total % 10)) % 10)


def _mod10_is_valid(full: str) -> bool:
    """Validate a complete MOD10 KID (base digits + trailing check digit).

    Weights alternate 1, 2, 1, 2, ... starting at the RIGHTMOST digit (the
    check digit itself gets weight 1) — the standard Luhn validation pass.
    A number is valid iff the weighted sum is a multiple of 10.
    """
    total = 0
    for i, ch in enumerate(reversed(full)):
        digit = int(ch)
        weight = 1 if i % 2 == 0 else 2
        product = digit * weight
        if product >= 10:
            product -= 9
        total += product
    return total % 10 == 0


def do_generate_kid(base_number: str, variant: str = _KID_VARIANT) -> dict[str, Any]:
    """Generate a Norwegian KID from *base_number* under the requested *variant*.

    Parameters
    ----------
    base_number:
        A ``str`` of 1-24 ASCII digits (e.g. an invoice or customer
        reference). Leading zeros are preserved.
    variant:
        Check-digit scheme, default ``"MOD10"`` (the only variant this
        module implements). ``"MOD11"`` is specified but not yet built --
        see :func:`_resolve_kid_variant` and the module docstring. Any other
        value is rejected.

    Returns
    -------
    dict with ``base_number``, ``check_digit``, ``kid`` (base + check digit),
    and ``variant`` (echoes the resolved variant -- always ``"MOD10"`` today,
    since every other value either raises or is rejected).

    Raises
    ------
    NotImplementedError
        *variant* is ``"MOD11"`` -- specified, pending a bank-arrangement
        decision (see module docstring).
    ValueError
        *variant* is not a recognised scheme (anything other than
        ``"MOD10"``/``"MOD11"``), or *base_number* is not a ``str``, is
        empty, contains a non-digit character, or exceeds the 24-digit
        ceiling (25 digits total with the check digit) — refused rather than
        silently truncated, coerced, or defaulted.
    """
    resolved_variant = _resolve_kid_variant(variant, "do_generate_kid: variant")
    base = _digits_only(base_number, "do_generate_kid: base_number")
    if not (_KID_MIN_BASE_LEN <= len(base) <= _KID_MAX_BASE_LEN):
        raise ValueError(
            f"do_generate_kid: base_number must be {_KID_MIN_BASE_LEN}-{_KID_MAX_BASE_LEN} "
            f"digits, got {len(base)} digits ({base_number!r})"
        )
    check_digit = _mod10_check_digit(base)
    return {
        "base_number": base,
        "check_digit": check_digit,
        "kid": base + check_digit,
        "variant": resolved_variant,
    }


def do_validate_kid(kid: str, variant: str = _KID_VARIANT) -> dict[str, Any]:
    """Validate a complete KID (base + trailing check digit) under *variant*.

    Parameters
    ----------
    kid:
        A ``str`` of at least 2 ASCII digits (1+ base digits plus the check
        digit).
    variant:
        Check-digit scheme, default ``"MOD10"`` (the only variant this
        module implements). ``"MOD11"`` is specified but not yet built --
        see :func:`_resolve_kid_variant` and the module docstring. Any other
        value is rejected.

    Returns
    -------
    dict with ``kid``, ``valid`` (bool), and ``variant`` (echoes the resolved
    variant -- always ``"MOD10"`` today).

    Raises
    ------
    NotImplementedError
        *variant* is ``"MOD11"`` -- specified, pending a bank-arrangement
        decision (see module docstring).
    ValueError
        *variant* is not a recognised scheme, or *kid* is an ambiguous input
        (wrong type, empty, non-digit, too short) -- raised rather than
        reporting ``valid=False`` for a shape it never actually checked, or
        silently defaulting an unrecognised variant — the same "fail toward
        refusal" rule as the rest of this module.
    """
    resolved_variant = _resolve_kid_variant(variant, "do_validate_kid: variant")
    full = _digits_only(kid, "do_validate_kid: kid")
    if len(full) < 2:
        raise ValueError(
            f"do_validate_kid: kid must carry at least one base digit plus the check "
            f"digit (>= 2 digits total), got {len(full)} digits ({kid!r})"
        )
    return {"kid": full, "valid": _mod10_is_valid(full), "variant": resolved_variant}


# ---------------------------------------------------------------------------
# Discipline 2 — outbound EHF, flag-gated. See module docstring.
# ---------------------------------------------------------------------------

_PEPPOL_DEFAULT_BASE_URL = "https://api.peppol-provider.example"

# UBL namespaces — mirrors ingestion.py's _UBL_NS (the read side) so a
# generated document's tag names match what this repo's own EHF reader expects.
_UBL_NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
_UBL_NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
_UBL_NS_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"

_MONEY_QUANT = Decimal("0.01")


def _money_str(value: Any, where: str) -> str:
    """Coerce a payable amount to an exact ``Decimal``, quantised to 2 dp, as ``str``.

    Mirrors ``ngaap.py``'s ``_quantise`` boundary discipline (invariant 5):
    ``float`` is routed through ``Decimal(str(value))`` — never
    ``Decimal(value)`` — so the binary-float representation error is never
    imported; ``bool`` is rejected first (``isinstance(True, int)`` is
    ``True``); the result is serialised with ``str()``, never passed through
    ``float()`` again.
    """
    if isinstance(value, bool):
        raise ValueError(f"{where}: must be a number, got bool {value!r}")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        candidate = Decimal(str(value))
    elif isinstance(value, str):
        try:
            candidate = Decimal(value)
        except DecimalException as exc:
            raise ValueError(f"{where}: not a usable number: {value!r}") from exc
    else:
        raise ValueError(f"{where}: must be int/float/str/Decimal, got {type(value).__name__}")
    if not candidate.is_finite():
        raise ValueError(f"{where}: must be finite, got {value!r}")
    try:
        return str(candidate.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP))
    except DecimalException as exc:
        raise ValueError(f"{where}: too large to express to 2 dp: {candidate!r}") from exc


def _require_str(invoice: dict[str, Any], key: str) -> str:
    value = invoice.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"do_generate_ehf: invoice[{key!r}] must be a non-empty string")
    return value.strip()


def _build_ehf_xml(invoice: dict[str, Any]) -> bytes:
    """Build a minimal UBL Invoice (EHF) document from *invoice*.

    Required keys: ``invoice_id``, ``issue_date`` (``YYYY-MM-DD``),
    ``currency_code``, ``payable_amount`` (int/float/str/Decimal — never
    accepted as a pre-formatted string that skips :func:`_money_str`'s
    coercion), ``supplier_peppol_id``, ``buyer_peppol_id``.
    Optional: ``kid`` (Norwegian payment reference; embedded as the
    PaymentMeans PaymentID when present, so a document generated by this
    module always carries a correctly-checksummed reference — see
    :func:`do_generate_kid`).

    Writes exactly the four header fields ``ingestion.py``'s
    ``_parse_ehf_document`` reads back out (``cbc:ID`` / ``cbc:IssueDate`` /
    ``cbc:DocumentCurrencyCode`` / ``cac:LegalMonetaryTotal/cbc:PayableAmount``)
    so a document this module generates round-trips through this repo's own
    EHF reader. This function only ever WRITES XML — it never parses
    external input, so ``defusedxml`` does not apply here (see module
    docstring).
    """
    invoice_id = _require_str(invoice, "invoice_id")
    issue_date = _require_str(invoice, "issue_date")
    currency_code = _require_str(invoice, "currency_code")
    supplier_id = _require_str(invoice, "supplier_peppol_id")
    buyer_id = _require_str(invoice, "buyer_peppol_id")
    payable_amount = _money_str(invoice.get("payable_amount"), "do_generate_ehf: payable_amount")

    root = Element(
        "Invoice",
        {"xmlns": _UBL_NS_INVOICE, "xmlns:cbc": _UBL_NS_CBC, "xmlns:cac": _UBL_NS_CAC},
    )
    SubElement(root, "cbc:ID").text = invoice_id
    SubElement(root, "cbc:IssueDate").text = issue_date
    SubElement(root, "cbc:DocumentCurrencyCode").text = currency_code

    supplier_party = SubElement(root, "cac:AccountingSupplierParty")
    SubElement(supplier_party, "cbc:EndpointID").text = supplier_id
    buyer_party = SubElement(root, "cac:AccountingCustomerParty")
    SubElement(buyer_party, "cbc:EndpointID").text = buyer_id

    kid = invoice.get("kid")
    if kid is not None:
        payment_means = SubElement(root, "cac:PaymentMeans")
        SubElement(payment_means, "cbc:PaymentID").text = _digits_only(
            kid, "do_generate_ehf: invoice['kid']"
        )

    monetary_total = SubElement(root, "cac:LegalMonetaryTotal")
    payable = SubElement(monetary_total, "cbc:PayableAmount")
    payable.set("currencyID", currency_code)
    payable.text = payable_amount

    return tostring(root, encoding="utf-8")


class PeppolTransport(abc.ABC):
    """Abstract PEPPOL send adapter. Mirrors ``procurement/transports.py``'s
    ``PoTransport`` exactly (see module docstring)."""

    @abc.abstractmethod
    async def send_document(
        self,
        *,
        xml_bytes: bytes,
        sender_peppol_id: str,
        receiver_peppol_id: str,
        namespace_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Send *xml_bytes* to *receiver_peppol_id* over the PEPPOL network.

        Raises ``NotImplementedError`` in every shipped implementation today
        (see :class:`StubPeppolTransport`) until a real PEPPOL access-point
        provider (Tickstar/Pagero) is selected.
        """


class StubPeppolTransport(PeppolTransport):
    """🔴 Stub — PEPPOL access-point provider not yet selected (roadmap 08
    External blockers). Always raises ``NotImplementedError``, mirroring
    ``procurement/transports.py``'s ``NetsetPoTransport`` — the ceiling on
    what this module can actually cause to happen externally is zero,
    regardless of the flag, until this class is replaced."""

    async def send_document(
        self,
        *,
        xml_bytes: bytes,
        sender_peppol_id: str,
        receiver_peppol_id: str,
        namespace_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        raise NotImplementedError(
            "PEPPOL access-point provider not yet selected (Tickstar/Pagero) — "
            "StubPeppolTransport.send_document is a stub until a provider ships. "
            f"sender={sender_peppol_id!r} receiver={receiver_peppol_id!r} "
            "Track blocker: docs/vertical_engines/08-economy-engine.md External blockers 🔴"
        )


def _creds() -> tuple[str, str] | None:
    """Return ``(api_key, base_url)`` or ``None`` when unconfigured.

    Mirrors ``system_design/lucid.py``'s ``_creds()`` exactly: resolved at
    call time (via ``resolve_secret``, never ``nce.config.cfg``) so
    ``monkeypatch.setenv`` in tests takes effect without a module reload.
    Values are never logged. Called ONLY when
    ``NCE_ECONOMY_PEPPOL_ENABLED`` is true — see :func:`do_generate_ehf`.
    """
    api_key = resolve_secret("NCE_ECONOMY_PEPPOL_API_KEY")
    if not api_key:
        return None
    base_url = resolve_secret("NCE_ECONOMY_PEPPOL_BASE_URL") or _PEPPOL_DEFAULT_BASE_URL
    return api_key, base_url


async def do_generate_ehf(
    invoice: dict[str, Any],
    *,
    namespace_id: str,
    idempotency_key: str,
    transport: PeppolTransport | None = None,
) -> dict[str, Any]:
    """Generate an outbound EHF document; send it only when the PEPPOL flag is on.

    Parameters
    ----------
    invoice:
        See :func:`_build_ehf_xml` for required/optional keys.
    namespace_id:
        Tenant namespace UUID string — forwarded to the transport for audit
        correlation only (this function makes no DB call itself).
    idempotency_key:
        Stable key forwarded to the transport when sending is attempted.
    transport:
        Test/DI seam; defaults to :class:`StubPeppolTransport` — mirrors
        ``procurement/po.py``'s ``do_submit_po(transport=...)`` parameter.

    Returns
    -------
    dict with ``ehf_xml`` (str), ``sent`` (bool), ``peppol_enabled`` (bool),
    ``mode`` (``NCE_ECONOMY_PEPPOL_MODE``), and either ``reason`` (why
    nothing was sent) or ``transport_result`` (the adapter's response).

    Safety interlock (see module docstring): with
    ``NCE_ECONOMY_PEPPOL_ENABLED`` false (the default), this function
    returns immediately after building the XML — :func:`_creds` is never
    called and no :class:`PeppolTransport` is ever constructed.
    """
    xml_bytes = _build_ehf_xml(invoice)
    xml_str = xml_bytes.decode("utf-8")

    # --- THE GATE ---------------------------------------------------------
    # This is the entire safety interlock. Removing this check is exactly
    # the regression `test_ehf_disabled_never_touches_transport` is written
    # to catch: with the flag left at its default (false), nothing below
    # this line may execute.
    if not cfg.NCE_ECONOMY_PEPPOL_ENABLED:
        return {
            "ehf_xml": xml_str,
            "sent": False,
            "peppol_enabled": False,
            "mode": cfg.NCE_ECONOMY_PEPPOL_MODE,
            "reason": "NCE_ECONOMY_PEPPOL_ENABLED is false (default) — outbound EHF is disabled",
        }
    # ------------------------------------------------------------------

    creds = _creds()
    if creds is None:
        return {
            "ehf_xml": xml_str,
            "sent": False,
            "peppol_enabled": True,
            "mode": cfg.NCE_ECONOMY_PEPPOL_MODE,
            "reason": "NCE_ECONOMY_PEPPOL_API_KEY is not configured",
        }

    _transport: PeppolTransport = transport if transport is not None else StubPeppolTransport()
    send_result = await _transport.send_document(
        xml_bytes=xml_bytes,
        sender_peppol_id=invoice.get("supplier_peppol_id", ""),
        receiver_peppol_id=invoice.get("buyer_peppol_id", ""),
        namespace_id=str(namespace_id),
        idempotency_key=idempotency_key,
    )
    log.info(
        "[peppol] EHF sent invoice=%s ns=%s transport=%s",
        invoice.get("invoice_id"),
        str(namespace_id)[:8],
        type(_transport).__name__,
    )
    return {
        "ehf_xml": xml_str,
        "sent": True,
        "peppol_enabled": True,
        "mode": cfg.NCE_ECONOMY_PEPPOL_MODE,
        "transport_result": send_result,
    }
