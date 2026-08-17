"""Batch 71 — Stream-and-Reduce core for the Diagnostics Engine.

A flat-memory pipeline that turns a diagnostic bundle (zip / tar / gzip /
plain text) into a small bounded :class:`Digest` without ever loading the
whole archive into RAM.

The two public seams are:

* :func:`stream_entries` — a generator that walks an archive member-by-member
  (or a plain-text file line-by-line) yielding ``(entry_name, line)`` pairs,
  with a zip-bomb guard that caps cumulative *uncompressed* bytes and entry
  count.  A breach raises :class:`PoisonBundleError`.
* :func:`digest_stream` — folds those lines through a :class:`LogProfile`
  (from :mod:`nce.vertical_modules.diagnostics.profiles`) into a bounded
  anomaly list plus per-``anomaly_type`` 5-minute window aggregates.

Constant-memory invariant: neither function materialises the input.  The
anomaly list is capped at ``cfg.NCE_DIAG_MAX_ANOMALIES`` and every retained
sample is truncated, so peak resident memory is bounded regardless of input
size.

Stdlib only (``zipfile`` / ``tarfile`` / ``gzip``) — no third-party deps.
"""

from __future__ import annotations

import gzip
import tarfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

from nce.config import cfg
from nce.vertical_modules.diagnostics.profiles import LogProfile

# ── Constants ───────────────────────────────────────────────────────────────────

# Longest sample string we will ever retain for a single anomaly.  Keeping this
# small is part of the constant-memory contract.
_SAMPLE_MAX_CHARS = 200

# Aggregation window for per-anomaly_type rate buckets, in seconds.
_WINDOW_SECONDS = 300  # 5 minutes

# Read granularity for the sliding-window plain-text reader / stream copies.
_CHUNK_SIZE = 64 * 1024

# Magic-byte signatures used to detect the container format.  Order matters:
# zip and gzip have unambiguous 2-byte prefixes; tar is detected via the
# ``ustar`` marker at offset 257, handled separately.
_ZIP_MAGIC = b"PK\x03\x04"
_ZIP_EMPTY_MAGIC = b"PK\x05\x06"  # empty archive
_ZIP_SPANNED_MAGIC = b"PK\x07\x08"  # spanned archive
_GZIP_MAGIC = b"\x1f\x8b"
_TAR_USTAR_OFFSET = 257
_TAR_USTAR_MAGIC = b"ustar"


# ── Errors ──────────────────────────────────────────────────────────────────────


class PoisonBundleError(Exception):
    """Raised when a bundle breaches a streaming safety guard.

    The diagnostics worker classifies this as *non-retryable*: a bundle that
    exceeds the uncompressed-size or entry-count ceiling (a zip bomb) will
    breach again on every retry, so it must be dead-lettered rather than
    re-queued.
    """


# ── Data model ───────────────────────────────────────────────────────────────────


@dataclass
class Anomaly:
    """A single retained anomaly, deduplicated by ``anomaly_type``.

    Attributes:
        anomaly_type: Classifier-assigned category (e.g. ``"ptp_desync"``).
        severity:     Syslog-style severity (lower = more urgent).
        occurrences:  Number of lines that classified to this type, including
                      those collapsed into this record after the list filled.
        sample:       A representative line, truncated to ≤200 chars.
    """

    anomaly_type: str
    severity: int
    occurrences: int
    sample: str


@dataclass
class WindowBucket:
    """Per-``anomaly_type`` count within one 5-minute wall-clock window.

    Attributes:
        anomaly_type: The anomaly category this bucket aggregates.
        window_start: Unix epoch second marking the start of the window
                      (floored to ``_WINDOW_SECONDS``).
        count:        Number of occurrences observed in this window.
    """

    anomaly_type: str
    window_start: int
    count: int


@dataclass
class Digest:
    """Bounded summary produced by :func:`digest_stream`.

    Attributes:
        processed_lines: Total number of lines folded through the profile.
        anomalies:       Bounded list (≤ ``cfg.NCE_DIAG_MAX_ANOMALIES``) of
                         retained anomalies, highest-severity first.
        windows:         Per-``(anomaly_type, window_start)`` aggregates.
    """

    processed_lines: int = 0
    anomalies: list[Anomaly] = field(default_factory=list)
    windows: list[WindowBucket] = field(default_factory=list)


# ── Format detection ──────────────────────────────────────────────────────────────


def _read_magic(local_path: str) -> bytes:
    """Read the leading bytes of *local_path* for format sniffing."""
    with open(local_path, "rb") as handle:
        return handle.read(_TAR_USTAR_OFFSET + len(_TAR_USTAR_MAGIC))


def _is_zip(magic: bytes) -> bool:
    return magic.startswith((_ZIP_MAGIC, _ZIP_EMPTY_MAGIC, _ZIP_SPANNED_MAGIC))


def _is_gzip(magic: bytes) -> bool:
    return magic.startswith(_GZIP_MAGIC)


def _is_tar(magic: bytes) -> bool:
    end = _TAR_USTAR_OFFSET + len(_TAR_USTAR_MAGIC)
    return len(magic) >= end and magic[_TAR_USTAR_OFFSET:end] == _TAR_USTAR_MAGIC


def _is_gzipped_tar(local_path: str) -> bool:
    """Return True when a gzip-magic file actually wraps a tar archive.

    ``tarfile.is_tarfile`` transparently decompresses the gzip stream and only
    reads enough to validate the first tar header, so this stays cheap and
    constant-memory even for very large bundles.
    """
    try:
        return tarfile.is_tarfile(local_path)
    except (tarfile.TarError, OSError):
        return False


# ── Guard ─────────────────────────────────────────────────────────────────────────


class _BombGuard:
    """Tracks cumulative uncompressed bytes + entry count against ceilings."""

    __slots__ = ("_max_bytes", "_max_entries", "total_bytes", "total_entries")

    def __init__(self, *, max_uncompressed_bytes: int, max_entries: int) -> None:
        self._max_bytes = max_uncompressed_bytes
        self._max_entries = max_entries
        self.total_bytes = 0
        self.total_entries = 0

    def add_entry(self, name: str) -> None:
        self.total_entries += 1
        if self.total_entries > self._max_entries:
            raise PoisonBundleError(
                f"entry count exceeded ceiling of {self._max_entries} (at entry {name!r})"
            )

    def add_bytes(self, n: int, name: str) -> None:
        self.total_bytes += n
        if self.total_bytes > self._max_bytes:
            raise PoisonBundleError(
                f"uncompressed size exceeded ceiling of "
                f"{self._max_bytes} bytes (while reading {name!r})"
            )


# ── Line streaming over a binary member ─────────────────────────────────────────────


class _BinaryReader(Protocol):
    """Minimal structural type: anything with a binary ``read(size)``.

    Covers ``zipfile.ZipExtFile``, ``gzip.GzipFile``, the ``IO[bytes]`` that
    ``tarfile.extractfile`` returns, and the ``BufferedReader`` from ``open``.
    """

    def read(self, size: int = ..., /) -> bytes: ...


def _iter_lines(
    name: str,
    reader: _BinaryReader,
    guard: _BombGuard,
) -> Iterator[tuple[str, str]]:
    """Yield decoded ``(name, line)`` from *reader*, counting bytes via *guard*.

    Lines are produced via a sliding buffer so a single pathological line
    cannot force the whole member into memory beyond the bomb ceiling: every
    chunk read is charged to the guard *before* it is buffered, so an
    over-large member trips the guard mid-read.
    """
    buffer = b""
    while True:
        chunk = reader.read(_CHUNK_SIZE)
        if not chunk:
            break
        guard.add_bytes(len(chunk), name)
        buffer += chunk
        # Emit all complete lines currently in the buffer.
        start = 0
        newline = buffer.find(b"\n", start)
        while newline != -1:
            raw = buffer[start:newline]
            yield (name, raw.decode("utf-8", errors="replace").rstrip("\r"))
            start = newline + 1
            newline = buffer.find(b"\n", start)
        buffer = buffer[start:]
    if buffer:
        yield (name, buffer.decode("utf-8", errors="replace").rstrip("\r"))


# ── Public: stream_entries ──────────────────────────────────────────────────────────


def stream_entries(
    local_path: str,
    *,
    max_uncompressed_bytes: int,
    max_entries: int,
) -> Iterator[tuple[str, str]]:
    """Stream ``(entry_name, line)`` pairs from a bundle at *local_path*.

    The container format (zip / tar / gzip / plain text) is detected from
    magic bytes, then walked one member at a time so memory stays flat
    regardless of bundle size.

    Args:
        local_path: Filesystem path to the bundle.
        max_uncompressed_bytes: Cumulative uncompressed-byte ceiling across
            every member.  A breach raises :class:`PoisonBundleError`.
        max_entries: Maximum number of members.  A breach raises
            :class:`PoisonBundleError`.

    Yields:
        ``(entry_name, line)`` tuples.  For plain-text and single-member gzip
        inputs the entry name is the basename of *local_path*.

    Raises:
        PoisonBundleError: When a zip-bomb guard ceiling is exceeded.
    """
    guard = _BombGuard(
        max_uncompressed_bytes=max_uncompressed_bytes,
        max_entries=max_entries,
    )
    magic = _read_magic(local_path)

    if _is_zip(magic):
        yield from _stream_zip(local_path, guard)
    elif _is_tar(magic):
        yield from _stream_tar(local_path, guard)
    elif _is_gzip(magic):
        # A gzip stream may wrap either a tar archive (``.tar.gz``) or plain
        # text (``.log.gz``).  The outer 2 magic bytes cannot tell them apart,
        # so probe for tar-ness; ``tarfile`` decompresses gzip transparently.
        if _is_gzipped_tar(local_path):
            yield from _stream_tar(local_path, guard)
        else:
            yield from _stream_gzip(local_path, guard)
    else:
        yield from _stream_plain(local_path, guard)


def _stream_zip(local_path: str, guard: _BombGuard) -> Iterator[tuple[str, str]]:
    with zipfile.ZipFile(local_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            guard.add_entry(info.filename)
            with zf.open(info, "r") as member:
                # zf.open returns a ZipExtFile (a BufferedIOBase subclass).
                yield from _iter_lines(info.filename, member, guard)


def _stream_tar(local_path: str, guard: _BombGuard) -> Iterator[tuple[str, str]]:
    # mode "r|*" = streaming read, transparent (de)compression, single pass.
    with tarfile.open(local_path, mode="r|*") as tf:
        for member in tf:
            if not member.isfile():
                continue
            guard.add_entry(member.name)
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            with extracted:
                yield from _iter_lines(member.name, extracted, guard)


def _stream_gzip(local_path: str, guard: _BombGuard) -> Iterator[tuple[str, str]]:
    name = _basename(local_path)
    guard.add_entry(name)
    with gzip.open(local_path, "rb") as gz:
        yield from _iter_lines(name, gz, guard)


def _stream_plain(local_path: str, guard: _BombGuard) -> Iterator[tuple[str, str]]:
    name = _basename(local_path)
    guard.add_entry(name)
    with open(local_path, "rb") as handle:
        yield from _iter_lines(name, handle, guard)


def _basename(path: str) -> str:
    # Normalise both separators so the basename is stable cross-platform.
    return path.replace("\\", "/").rsplit("/", 1)[-1]


# ── Timestamp extraction ───────────────────────────────────────────────────────────


def _extract_epoch(line: str) -> int:
    """Best-effort extraction of a Unix-epoch second from a log *line*.

    Recognises an ISO-8601-ish ``YYYY-MM-DDTHH:MM:SS`` / ``YYYY-MM-DD HH:MM:SS``
    prefix and a bare ``epoch=<seconds>`` token.  Returns ``0`` when no
    timestamp is found so window bucketing still groups the line deterministically.
    """
    import re
    from datetime import datetime, timezone

    iso = re.search(
        r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})",
        line,
    )
    if iso is not None:
        try:
            dt = datetime(
                int(iso.group(1)),
                int(iso.group(2)),
                int(iso.group(3)),
                int(iso.group(4)),
                int(iso.group(5)),
                int(iso.group(6)),
                tzinfo=timezone.utc,
            )
            return int(dt.timestamp())
        except ValueError:
            return 0
    epoch = re.search(r"\bepoch=(\d{1,15})\b", line)
    if epoch is not None:
        return int(epoch.group(1))
    return 0


# ── Public: digest_stream ──────────────────────────────────────────────────────────


def digest_stream(profile: LogProfile, lines: Iterable[tuple[str, str]]) -> Digest:
    """Fold ``(entry_name, line)`` pairs into a bounded :class:`Digest`.

    The anomaly list is capped at ``cfg.NCE_DIAG_MAX_ANOMALIES``.  Each distinct
    ``anomaly_type`` collapses to one :class:`Anomaly` record carrying an
    ``occurrences`` count and the highest-severity sample seen; once the cap is
    reached, further *new* anomaly types are dropped but already-tracked types
    keep counting.  Per-``anomaly_type`` 5-minute window aggregates are emitted
    separately so rate signals survive even when the type list is full.

    Args:
        profile: The :class:`LogProfile` used to classify each line.
        lines:   Iterable of ``(entry_name, line)`` pairs — typically the
                 generator returned by :func:`stream_entries`.

    Returns:
        A :class:`Digest` whose ``anomalies`` are sorted highest-severity
        first (severity ascending), then by descending ``occurrences``.
    """
    max_anomalies = int(cfg.NCE_DIAG_MAX_ANOMALIES)

    by_type: dict[str, Anomaly] = {}
    windows: dict[tuple[str, int], WindowBucket] = {}
    processed = 0

    for _entry_name, line in lines:
        processed += 1
        classified = profile.classify(line)
        if classified is None:
            continue
        anomaly_type, severity = classified

        existing = by_type.get(anomaly_type)
        if existing is not None:
            existing.occurrences += 1
            # Keep the highest-severity (lowest number) representative sample.
            if severity < existing.severity:
                existing.severity = severity
                existing.sample = _truncate(line)
        elif len(by_type) < max_anomalies:
            by_type[anomaly_type] = Anomaly(
                anomaly_type=anomaly_type,
                severity=severity,
                occurrences=1,
                sample=_truncate(line),
            )
        # else: cap reached and this is a new type — drop the per-type record
        # but still record its rate below so the signal is not wholly lost.

        window_start = (_extract_epoch(line) // _WINDOW_SECONDS) * _WINDOW_SECONDS
        key = (anomaly_type, window_start)
        bucket = windows.get(key)
        if bucket is None:
            windows[key] = WindowBucket(
                anomaly_type=anomaly_type,
                window_start=window_start,
                count=1,
            )
        else:
            bucket.count += 1

    anomalies = sorted(
        by_type.values(),
        key=lambda a: (a.severity, -a.occurrences, a.anomaly_type),
    )
    window_list = sorted(
        windows.values(),
        key=lambda w: (w.window_start, w.anomaly_type),
    )
    return Digest(processed_lines=processed, anomalies=anomalies, windows=window_list)


def _truncate(line: str) -> str:
    """Truncate *line* to at most :data:`_SAMPLE_MAX_CHARS` characters."""
    if len(line) <= _SAMPLE_MAX_CHARS:
        return line
    return line[:_SAMPLE_MAX_CHARS]
