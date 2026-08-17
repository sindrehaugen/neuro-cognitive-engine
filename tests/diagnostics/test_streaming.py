"""Batch 71 — Stream-and-Reduce core.

Unit tests for ``nce.vertical_modules.diagnostics.streaming``.

All tests are pure-unit: no Docker, no network, no database.  The "large"
inputs are synthetic temp archives generated on the fly; peak memory is
asserted with :mod:`tracemalloc` to prove the constant-memory invariant.
"""

from __future__ import annotations

import gzip
import io
import re
import tarfile
import tracemalloc
import zipfile
from pathlib import Path

import pytest

from nce.config import cfg
from nce.vertical_modules.diagnostics.profiles import LogProfile, get_profile
from nce.vertical_modules.diagnostics.streaming import (
    Anomaly,
    Digest,
    PoisonBundleError,
    WindowBucket,
    digest_stream,
    stream_entries,
)

# Generous per-test ceilings so the guard does not fire unless a test wants it.
_BIG = 1 << 40  # 1 TiB — effectively unbounded for the happy-path tests
_MANY = 1_000_000


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_line(i: int, *, anomalous: bool) -> str:
    """Build one synthetic log line, optionally carrying an ERROR keyword."""
    ts = f"2024-01-15 08:{(i // 60) % 60:02d}:{i % 60:02d}"
    if anomalous:
        return f"{ts} ERROR connection reset on link {i}"
    return f"{ts} INFO heartbeat ok seq {i}"


def _write_zip(path: Path, *, members: int, lines_each: int, anomalous_every: int) -> int:
    """Write a multi-member zip; return the total line count."""
    total = 0
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for m in range(members):
            buf = io.StringIO()
            for i in range(lines_each):
                buf.write(_make_line(i, anomalous=(i % anomalous_every == 0)))
                buf.write("\n")
                total += 1
            zf.writestr(f"logs/member_{m}.log", buf.getvalue())
    return total


# ── Format detection / round-trip ───────────────────────────────────────────────


def test_stream_plain_text(tmp_path: Path) -> None:
    p = tmp_path / "diag.log"
    p.write_text("\n".join(_make_line(i, anomalous=False) for i in range(100)) + "\n")
    pairs = list(stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=_MANY))
    assert len(pairs) == 100
    assert all(name == "diag.log" for name, _ in pairs)


def test_stream_gzip_single_member(tmp_path: Path) -> None:
    p = tmp_path / "diag.log.gz"
    payload = ("\n".join(_make_line(i, anomalous=False) for i in range(50)) + "\n").encode()
    with gzip.open(p, "wb") as gz:
        gz.write(payload)
    pairs = list(stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=_MANY))
    assert len(pairs) == 50
    assert all(name == "diag.log.gz" for name, _ in pairs)


def test_stream_tar_multi_member(tmp_path: Path) -> None:
    p = tmp_path / "diag.tar.gz"
    with tarfile.open(p, "w:gz") as tf:
        for m in range(3):
            data = ("\n".join(_make_line(i, anomalous=False) for i in range(20)) + "\n").encode()
            info = tarfile.TarInfo(name=f"member_{m}.log")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    pairs = list(stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=_MANY))
    assert len(pairs) == 60
    names = {name for name, _ in pairs}
    assert names == {"member_0.log", "member_1.log", "member_2.log"}


def test_stream_zip_multi_member(tmp_path: Path) -> None:
    p = tmp_path / "diag.zip"
    total = _write_zip(p, members=4, lines_each=25, anomalous_every=5)
    pairs = list(stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=_MANY))
    assert len(pairs) == total == 100


def test_zip_directory_entries_skipped(tmp_path: Path) -> None:
    p = tmp_path / "diag.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("logs/", "")  # explicit directory entry
        zf.writestr("logs/a.log", "2024-01-15 08:00:00 INFO ok\n")
    pairs = list(stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=_MANY))
    assert [name for name, _ in pairs] == ["logs/a.log"]


def test_crlf_line_endings_stripped(tmp_path: Path) -> None:
    p = tmp_path / "diag.log"
    p.write_bytes(b"2024-01-15 08:00:00 INFO one\r\n2024-01-15 08:00:01 INFO two\r\n")
    pairs = list(stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=_MANY))
    assert [line for _, line in pairs] == [
        "2024-01-15 08:00:00 INFO one",
        "2024-01-15 08:00:01 INFO two",
    ]


# ── Zip-bomb guard ──────────────────────────────────────────────────────────────


def test_guard_raises_on_uncompressed_byte_ceiling(tmp_path: Path) -> None:
    # A highly compressible payload: small on disk, large when inflated.
    p = tmp_path / "bomb.zip"
    bomb_payload = ("A" * 10_000 + "\n").encode() * 1000  # ~10 MB uncompressed
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.log", bomb_payload)
    with pytest.raises(PoisonBundleError, match="uncompressed size"):
        list(
            stream_entries(
                str(p),
                max_uncompressed_bytes=64 * 1024,  # 64 KiB ceiling
                max_entries=_MANY,
            )
        )


def test_guard_raises_on_entry_count_ceiling(tmp_path: Path) -> None:
    p = tmp_path / "many.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for m in range(20):
            zf.writestr(f"member_{m}.log", "2024-01-15 08:00:00 INFO ok\n")
    with pytest.raises(PoisonBundleError, match="entry count"):
        list(stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=5))


def test_guard_raises_on_plain_text_byte_ceiling(tmp_path: Path) -> None:
    p = tmp_path / "big.log"
    p.write_bytes(b"x" * (256 * 1024))  # 256 KiB, no newline
    with pytest.raises(PoisonBundleError, match="uncompressed size"):
        list(stream_entries(str(p), max_uncompressed_bytes=64 * 1024, max_entries=_MANY))


# ── digest_stream: anomaly detection ────────────────────────────────────────────


def test_digest_detects_anomalies(tmp_path: Path) -> None:
    p = tmp_path / "diag.zip"
    _write_zip(p, members=2, lines_each=50, anomalous_every=10)
    profile = get_profile("generic")
    digest = digest_stream(
        profile,
        stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=_MANY),
    )
    assert isinstance(digest, Digest)
    assert digest.processed_lines == 100
    assert len(digest.anomalies) == 1
    err = digest.anomalies[0]
    assert err.anomaly_type == "error"
    # 5 anomalous lines per member (indices 0,10,20,30,40) × 2 members.
    assert err.occurrences == 10
    assert "ERROR" in err.sample


def test_digest_clean_stream_has_no_anomalies() -> None:
    profile = get_profile("generic")
    lines = [("f.log", _make_line(i, anomalous=False)) for i in range(200)]
    digest = digest_stream(profile, lines)
    assert digest.processed_lines == 200
    assert digest.anomalies == []
    assert digest.windows == []


def test_digest_sample_truncated_to_200_chars() -> None:
    profile = get_profile("generic")
    long_line = "2024-01-15 08:00:00 ERROR " + ("y" * 5000)
    digest = digest_stream(profile, [("f.log", long_line)])
    assert len(digest.anomalies) == 1
    assert len(digest.anomalies[0].sample) <= 200


def test_digest_anomaly_list_capped_at_configured_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "NCE_DIAG_MAX_ANOMALIES", 3)
    # A profile with more distinct anomaly types than the cap allows.
    patterns = tuple((f"type_{n}", re.compile(rf"\bSIG{n}\b"), 3) for n in range(10))
    profile = LogProfile(name="manytypes", patterns=patterns)
    lines = [("f.log", f"2024-01-15 08:00:0{n % 10} SIG{n} boom") for n in range(10)]
    digest = digest_stream(profile, lines)
    assert len(digest.anomalies) == 3
    assert all(isinstance(a, Anomaly) for a in digest.anomalies)


def test_digest_dedupes_by_type_with_occurrence_counts() -> None:
    profile = get_profile("generic")
    lines = [("f.log", f"2024-01-15 08:00:00 ERROR fault {i}") for i in range(7)]
    digest = digest_stream(profile, lines)
    assert len(digest.anomalies) == 1
    assert digest.anomalies[0].occurrences == 7


def test_digest_keeps_highest_severity_sample() -> None:
    # Two types; verify ordering puts the lower-severity-number (more urgent) first.
    profile = get_profile("generic")
    lines = [
        ("f.log", "2024-01-15 08:00:00 ERROR low urgency"),
        ("f.log", "2024-01-15 08:00:01 FATAL high urgency"),
    ]
    digest = digest_stream(profile, lines)
    assert digest.anomalies[0].anomaly_type == "fatal_error"
    assert digest.anomalies[0].severity < digest.anomalies[1].severity


def test_digest_window_aggregation_5_minute_buckets() -> None:
    profile = get_profile("generic")
    # Two lines inside one 5-min window, one in the next.
    lines = [
        ("f.log", "2024-01-15 08:00:00 ERROR a"),
        ("f.log", "2024-01-15 08:04:59 ERROR b"),
        ("f.log", "2024-01-15 08:05:00 ERROR c"),
    ]
    digest = digest_stream(profile, lines)
    assert all(isinstance(w, WindowBucket) for w in digest.windows)
    assert len(digest.windows) == 2
    counts = sorted(w.count for w in digest.windows)
    assert counts == [1, 2]
    # Buckets are floored to 300-second boundaries.
    assert all(w.window_start % 300 == 0 for w in digest.windows)


# ── Constant-memory invariant ───────────────────────────────────────────────────


def test_peak_memory_bounded_on_large_archive(tmp_path: Path) -> None:
    """Peak memory must stay well below the total uncompressed payload size."""
    p = tmp_path / "large.zip"
    # ~8 MB uncompressed across many members; if anything loaded it all, peak
    # memory would dwarf the few-hundred-KB ceiling we assert below.
    total = _write_zip(p, members=20, lines_each=8000, anomalous_every=50)
    assert total == 160_000

    profile = get_profile("generic")

    tracemalloc.start()
    try:
        digest = digest_stream(
            profile,
            stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=_MANY),
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert digest.processed_lines == total
    # The digest itself is tiny (1 anomaly type), so peak should reflect only
    # the sliding window + chunk buffers, not the full ~8 MB payload.
    assert peak < 2_000_000, f"peak memory {peak} bytes exceeded 2 MB bound"


def test_stream_entries_is_lazy_generator(tmp_path: Path) -> None:
    """stream_entries must not eagerly walk the archive — it returns a generator."""
    p = tmp_path / "diag.log"
    p.write_text("2024-01-15 08:00:00 INFO ok\n" * 10)
    gen = stream_entries(str(p), max_uncompressed_bytes=_BIG, max_entries=_MANY)
    # A generator yields on demand; pulling one item should not exhaust it.
    first = next(gen)
    assert first[0] == "diag.log"
    rest = list(gen)
    assert len(rest) == 9
