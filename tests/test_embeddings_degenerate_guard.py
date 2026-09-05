"""A degenerate embedding must never be stored as if it were real.

Why this file exists
--------------------
The bundled cognitive sidecar stub (``deploy/cognitive-stub/stub_server.py``)
answers ``POST /v1/embeddings`` with a 768-dimensional **zero vector**. Until the
guard these tests pin, ``_validate_batch`` let that through with
``degraded=False``:

* ``_l2_normalize`` documents itself as a "No-op when norm ≈ 0", so the zero
  vector was returned unchanged rather than raising or being replaced, and
* nothing else inspected the magnitude.

The consequence is worse than a wrong number. A zero vector is **equidistant
from every other vector** and cosine similarity against it is ``0/0``, so recall
does not fail -- it returns arbitrary ordering that looks like a result set. The
failure mode is silence, which is exactly why it survived: a stack full of green
health checks was writing un-recallable memories.

These tests are deliberately written against the *observable contract*
(``degraded`` and the returned vectors), not against the guard's internals, so
they keep their meaning if the implementation moves.
"""

from __future__ import annotations

import pytest

from nce.embeddings import (
    VECTOR_DIM,
    _deterministic_hash_embedding,
    _l2_normalize,
    _validate_batch,
)


def _norm_sq(vec: list[float]) -> float:
    return sum(x * x for x in vec)


class TestDegenerateVectorsAreRefused:
    """The guard proper: a near-zero vector must be replaced and flagged."""

    def test_all_zero_vector_is_refused_and_flagged_degraded(self) -> None:
        """This is the exact shape the cognitive stub returns."""
        texts = ["hello world"]
        vectors = [[0.0] * VECTOR_DIM]

        out, degraded = _validate_batch(texts, vectors, backend_name="StubProbe")

        assert degraded is True, "a zero vector must mark the batch degraded"
        assert _norm_sq(out[0]) > 0.0, "the replacement must not itself be degenerate"

    def test_the_replacement_is_the_deterministic_fallback(self) -> None:
        """Replacement is not arbitrary noise -- it is the documented fallback."""
        texts = ["def add(a, b): return a + b"]
        out, degraded = _validate_batch(texts, [[0.0] * VECTOR_DIM], backend_name="StubProbe")

        assert degraded is True
        assert out[0] == pytest.approx(_deterministic_hash_embedding(texts[0]))

    def test_replacement_discriminates_between_different_texts(self) -> None:
        """The property a zero vector destroys: different inputs must differ.

        With zeros, every text embeds identically, so similarity ranking is
        arbitrary. After the guard, distinct texts must produce distinct vectors.
        """
        texts = ["SELECT * FROM users", "def add(a, b): return a + b"]
        out, degraded = _validate_batch(
            texts, [[0.0] * VECTOR_DIM, [0.0] * VECTOR_DIM], backend_name="StubProbe"
        )

        assert degraded is True
        assert out[0] != out[1], "distinct texts must not share an embedding"

    def test_one_bad_vector_condemns_the_whole_batch(self) -> None:
        """Partial acceptance would leave a hole no caller can see."""
        texts = ["good", "bad"]
        good = _l2_normalize([0.5] * VECTOR_DIM)
        out, degraded = _validate_batch(texts, [good, [0.0] * VECTOR_DIM], backend_name="StubProbe")

        assert degraded is True
        assert all(_norm_sq(v) > 0.0 for v in out)

    def test_tiny_but_real_vector_is_refused_too(self) -> None:
        """Denormal-scale values are degenerate in effect, not just at exactly 0."""
        texts = ["x"]
        out, degraded = _validate_batch(texts, [[1e-18] * VECTOR_DIM], backend_name="StubProbe")

        assert degraded is True
        assert _norm_sq(out[0]) > 0.0


class TestHealthyVectorsStillPass:
    """Guard-the-guard: the positive control.

    Without these, every assertion above would still pass if ``_validate_batch``
    simply flagged *everything* degraded -- which would be a different defect
    wearing the same green tick.
    """

    def test_a_normal_vector_is_accepted_and_not_flagged(self) -> None:
        texts = ["hello world"]
        healthy = _deterministic_hash_embedding("hello world")

        out, degraded = _validate_batch(texts, [healthy], backend_name="StubProbe")

        assert degraded is False, "a healthy vector must NOT be reported degraded"
        assert out[0] == pytest.approx(healthy)

    def test_accepted_vectors_come_back_l2_normalised(self) -> None:
        texts = ["hello world"]
        unnormalised = [3.0] + [0.0] * (VECTOR_DIM - 1)

        out, degraded = _validate_batch(texts, [unnormalised], backend_name="StubProbe")

        assert degraded is False
        assert _norm_sq(out[0]) == pytest.approx(1.0)

    def test_a_small_but_legitimate_vector_is_not_refused(self) -> None:
        """The floor must not swallow vectors that are merely modest."""
        texts = ["x"]
        small = [1e-3] * VECTOR_DIM  # |v|^2 ~ 7.7e-4, far above the 1e-24 floor

        out, degraded = _validate_batch(texts, [small], backend_name="StubProbe")

        assert degraded is False
        assert _norm_sq(out[0]) == pytest.approx(1.0)


class TestDimensionGuardStillWorks:
    """The pre-existing behaviour this change must not disturb."""

    def test_wrong_dimension_is_still_refused(self) -> None:
        texts = ["x"]
        out, degraded = _validate_batch(texts, [[0.1, 0.2]], backend_name="StubProbe")

        assert degraded is True
        assert len(out[0]) == VECTOR_DIM

    def test_count_mismatch_is_still_refused(self) -> None:
        texts = ["a", "b"]
        out, degraded = _validate_batch(
            texts, [_deterministic_hash_embedding("a")], backend_name="StubProbe"
        )

        assert degraded is True
        assert len(out) == len(texts)
