"""When is it safe to drop ``NCE_MASTER_KEY_PREVIOUS``? (F3)

This file exists because #125 got the answer wrong in a way no test could see. Both the
"safe to drop" log line and the sweep's exit code were gated on there being **zero unopenable
rows** -- and two rows in the live database are permanently unopenable by design, wrapped under
``tests/conftest.py``'s ``"x" * 32`` by the accidental rotations of 2026-09-07. The runbook
correctly says never to "fix" them. So the sweep exited 1 forever, the line never printed, and
runbook step 5 -- "drop PREVIOUS when the sweep reports zero off-primary blobs", described there
as *"the step that used to be a guess"* -- would have waited forever.

The rule, stated so it cannot be re-broken: **an unopenable blob is not openable by PREVIOUS
either.** No key on the ring opens it, PREVIOUS included. So it can never be a reason to keep
PREVIOUS. Only blobs that are off the primary key AND openable by some ring key can be.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rewrap_master_key import drop_previous_decision  # noqa: E402


def test_the_f3_regression_permanently_unopenable_rows_do_not_block_the_drop() -> None:
    """The exact live shape after a completed sweep: 2 off-primary, both unopenable.

    This is the case that was broken. If it ever returns ``(False, 1)`` again, the completion
    signal is unreachable and the rotation runbook has no terminating condition.
    """

    safe, code = drop_previous_decision(off_primary_total=2, unopenable_total=2)
    assert safe is True, (
        "2 off-primary rows, both permanently unopenable, must NOT block dropping PREVIOUS -- "
        "no ring key opens them and PREVIOUS does not either"
    )
    assert code == 0


def test_a_blob_a_ring_key_can_open_does_block_the_drop() -> None:
    """The other direction, which must keep working: real work left means do not drop."""

    safe, code = drop_previous_decision(off_primary_total=7, unopenable_total=2)
    assert safe is False, "5 actionable blobs remain; dropping PREVIOUS would orphan them"
    assert code == 1


def test_a_fully_swept_database_is_safe_and_clean() -> None:
    safe, code = drop_previous_decision(off_primary_total=0, unopenable_total=0)
    assert (safe, code) == (True, 0)


def test_one_actionable_blob_is_enough_to_hold_previous() -> None:
    """Guard against an off-by-one making a single straggler invisible."""

    safe, code = drop_previous_decision(off_primary_total=1, unopenable_total=0)
    assert (safe, code) == (False, 1)


@pytest.mark.parametrize("unopenable", [1, 2, 5, 99])
def test_any_number_of_unopenable_rows_alone_is_still_safe(unopenable: int) -> None:
    """The count must not matter. It was the *presence* of any that broke F3."""

    safe, code = drop_previous_decision(off_primary_total=unopenable, unopenable_total=unopenable)
    assert (safe, code) == (True, 0)


def test_unopenable_exceeding_off_primary_does_not_invert_the_decision() -> None:
    """Defensive: a miscount must fail SAFE (do not drop is the cautious answer)...

    ...except that here the cautious answer is the opposite. If ``unopenable`` somehow
    exceeded ``off_primary`` -- which would mean the two counts were gathered inconsistently --
    ``actionable`` goes negative. Negative must not read as "work remains" forever, which is
    the F3 failure mode all over again, so ``<= 0`` is deliberate rather than ``== 0``.
    """

    safe, code = drop_previous_decision(off_primary_total=2, unopenable_total=3)
    assert (safe, code) == (True, 0)
