"""Empty ``entity_types`` scans everything; overlapping spans resolve by score.

Ported from the steps-ai fork of NCE (backend/nce/pii.py, commit b6c78c66a,
2026-08-20). Two defects in ``nce/pii.py``:

1. ``_scan_sync`` returned ``[]`` when ``config.entity_types`` was empty, and every
   namespace is created with exactly that list next to ``policy: redact``. The
   namespace reported itself as redacting while fødselsnummer, phone, e-mail and
   card numbers were stored verbatim, and the early return skipped the Norwegian
   locale scan as well. No policy value means "do not scan" (redact, pseudonymise,
   reject and flag all presuppose a scan), so an empty list means "everything this
   installation recognises": Presidio receives ``entities=None`` and the regex
   fallback runs every pattern. An explicit list still narrows.

2. ``_merge_overlapping_entities`` promised highest-score-wins in its docstring but
   sorted by start position only. A Mod-11-verified fødselsnummer (score 0.99)
   sharing a span with the generic phone regex (0.8) was recorded as ``<PHONE>``:
   hidden, but booked under the wrong type, which is what pseudonym reversal and
   the audit trail go by.

Presidio is not a declared dependency (requirements*.txt), so CI exercises the
regex fallback; the Presidio contract is checked through an injected fake module,
following ``tests/test_pii_batch3.py``.
"""

from __future__ import annotations

import re
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

import nce.pii as pii
from nce.models import NamespacePIIConfig, PIIEntity, PIIPolicy
from nce.pii import (
    _FALLBACK_REGEXES,
    _fodselsnummer_check,
    _merge_overlapping_entities,
    _scan_sync,
    process,
)

# The fork's test vector. Both check digits verify (see the guard test below).
FNR = "01129900160"
FNR_TEXT = f"fnr {FNR}"

# One of each family the regex fallback and the Norwegian scan can recognise.
EMAIL = "ola@example.no"
PHONE = "555-123-4567"
CARD = "4532015112830366"  # Luhn-valid, see tests/test_pii_batch4.py
MIXED_TEXT = f"kontakt {EMAIL}, tlf {PHONE}, kort {CARD}, fnr {FNR}"
MIXED_RAW = (EMAIL, PHONE, CARD, FNR)
MIXED_TYPES = {"EMAIL", "PHONE", "CREDIT_CARD", "NO_FODSELSNUMMER"}


# --------------------------------------------------------------------------- #
# Fixture guards: the premises the tests below rest on.
# --------------------------------------------------------------------------- #


def test_fixture_fodselsnummer_passes_mod11():
    assert _fodselsnummer_check(FNR)


def test_fixture_generic_phone_regex_also_claims_the_fodselsnummer():
    """The two patterns compete for the same span; that is the merge's job."""
    m = re.search(_FALLBACK_REGEXES["PHONE"], FNR_TEXT)
    assert m is not None
    assert m.group(0) == FNR


def test_fixture_default_config_has_empty_entity_types():
    """The shape every namespace is created with."""
    assert NamespacePIIConfig().entity_types == []


# --------------------------------------------------------------------------- #
# _merge_overlapping_entities: highest score wins, as the docstring promises.
# --------------------------------------------------------------------------- #


class TestMergePrefersHigherScore:
    def test_same_span_higher_score_wins_regardless_of_input_order(self):
        phone = PIIEntity(start=4, end=15, entity_type="PHONE", value=FNR, score=0.8)
        fnr = PIIEntity(start=4, end=15, entity_type="NO_FODSELSNUMMER", value=FNR, score=0.99)
        for order in ([phone, fnr], [fnr, phone]):
            merged = _merge_overlapping_entities(list(order))
            assert [e.entity_type for e in merged] == ["NO_FODSELSNUMMER"], order

    def test_partial_overlap_higher_score_wins_even_when_it_starts_later(self):
        low = PIIEntity(start=0, end=8, entity_type="PHONE", value="x" * 8, score=0.8)
        high = PIIEntity(start=4, end=12, entity_type="EMAIL", value="y" * 8, score=0.95)
        merged = _merge_overlapping_entities([low, high])
        assert [e.entity_type for e in merged] == ["EMAIL"]

    def test_a_kept_span_blocks_every_later_overlapper_not_only_the_last(self):
        """Non-overlap must be checked against all kept spans, not a running end."""
        best = PIIEntity(start=10, end=20, entity_type="A", value="a" * 10, score=0.99)
        left = PIIEntity(start=0, end=12, entity_type="B", value="b" * 12, score=0.9)
        right = PIIEntity(start=18, end=30, entity_type="C", value="c" * 12, score=0.9)
        far = PIIEntity(start=40, end=44, entity_type="D", value="d" * 4, score=0.5)
        merged = _merge_overlapping_entities([left, best, right, far])
        assert sorted(e.entity_type for e in merged) == ["A", "D"]

    def test_equal_scores_keep_the_earlier_longer_span(self):
        """The pre-existing tie-break survives the reordering."""
        longer = PIIEntity(start=0, end=16, entity_type="EMAIL", value="a" * 16, score=0.9)
        short = PIIEntity(start=0, end=4, entity_type="PERSON", value="b" * 4, score=0.9)
        assert [e.entity_type for e in _merge_overlapping_entities([short, longer])] == ["EMAIL"]

    def test_output_is_sorted_by_start_descending_for_reverse_replacement(self):
        a = PIIEntity(start=0, end=2, entity_type="A", value="aa", score=0.5)
        b = PIIEntity(start=5, end=7, entity_type="B", value="bb", score=0.9)
        c = PIIEntity(start=10, end=12, entity_type="C", value="cc", score=0.7)
        assert [e.start for e in _merge_overlapping_entities([a, b, c])] == [10, 5, 0]


# --------------------------------------------------------------------------- #
# The fork's measured vector: "fnr 01129900160" -> <NO_FODSELSNUMMER>, not <PHONE>.
# --------------------------------------------------------------------------- #


class TestFodselsnummerBeatsPhone:
    def test_scan_records_the_fodselsnummer_type(self):
        cfg = NamespacePIIConfig(entity_types=["PHONE"], locale="no")
        entities = _scan_sync(FNR_TEXT, cfg, locale="no")
        assert [e.entity_type for e in entities] == ["NO_FODSELSNUMMER"]

    @pytest.mark.asyncio
    async def test_process_masks_as_fodselsnummer(self):
        cfg = NamespacePIIConfig(entity_types=["PHONE"], policy=PIIPolicy.redact, locale="no")
        out = await process(FNR_TEXT, cfg)
        assert out.sanitized_text == "fnr <NO_FODSELSNUMMER>"
        assert out.entities_found == ["NO_FODSELSNUMMER"]


# --------------------------------------------------------------------------- #
# Empty entity_types means "everything the installation recognises".
# --------------------------------------------------------------------------- #


class TestEmptyEntityTypesScansEverything:
    def test_regex_fallback_runs_every_pattern_and_the_locale_scan(self):
        cfg = NamespacePIIConfig(locale="no")
        types_found = {e.entity_type for e in _scan_sync(MIXED_TEXT, cfg, locale="no")}
        assert MIXED_TYPES <= types_found

    @pytest.mark.asyncio
    async def test_process_with_the_default_config_redacts_all_four(self):
        cfg = NamespacePIIConfig(policy=PIIPolicy.redact, locale="no")
        out = await process(MIXED_TEXT, cfg)
        assert out.redacted is True
        for raw in MIXED_RAW:
            assert raw not in out.sanitized_text, raw
        for token in (f"<{t}>" for t in MIXED_TYPES):
            assert token in out.sanitized_text, token

    @pytest.mark.asyncio
    async def test_reject_policy_with_the_default_config_actually_rejects(self):
        """A namespace that says reject must not store the payload untouched."""
        cfg = NamespacePIIConfig(policy=PIIPolicy.reject)
        with pytest.raises(ValueError, match="PII Policy Reject"):
            await process(f"kontakt {EMAIL}", cfg)

    def test_an_explicit_list_still_narrows(self):
        cfg = NamespacePIIConfig(entity_types=["EMAIL"])
        types_found = {e.entity_type for e in _scan_sync(MIXED_TEXT, cfg)}
        assert types_found == {"EMAIL"}

    def test_text_without_pii_still_yields_nothing(self):
        assert _scan_sync("ingen personopplysninger her", NamespacePIIConfig(), locale="no") == []


# --------------------------------------------------------------------------- #
# Presidio contract: None (= full recogniser set) for the empty list.
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_presidio(monkeypatch):
    """Inject a fake ``presidio_analyzer`` and reset the cached analyzer around it."""
    mod = types.ModuleType("presidio_analyzer")
    engine_cls = MagicMock()
    engine_cls.return_value.analyze.return_value = []
    mod.AnalyzerEngine = engine_cls  # type: ignore[attr-defined]
    monkeypatch.setattr(pii, "_ANALYZER", None)
    with patch.dict(sys.modules, {"presidio_analyzer": mod}):
        yield engine_cls.return_value
    monkeypatch.setattr(pii, "_ANALYZER", None)


class TestPresidioReceivesTheRightEntityFilter:
    def test_empty_list_becomes_none(self, fake_presidio):
        _scan_sync("anything", NamespacePIIConfig())
        fake_presidio.analyze.assert_called_once()
        assert fake_presidio.analyze.call_args.kwargs["entities"] is None

    def test_explicit_list_is_passed_through(self, fake_presidio):
        _scan_sync("anything", NamespacePIIConfig(entity_types=["EMAIL", "PERSON"]))
        fake_presidio.analyze.assert_called_once()
        assert fake_presidio.analyze.call_args.kwargs["entities"] == ["EMAIL", "PERSON"]
