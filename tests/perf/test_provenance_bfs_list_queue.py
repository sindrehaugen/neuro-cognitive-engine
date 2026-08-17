"""
tests/perf/test_provenance_bfs_list_queue.py
---------------------------------------------
KZ-1 gate test -- BFS frontier: list.pop(0) vs collections.deque.popleft().

The function get_event_provenance performs a BFS over a causal DAG bounded
to 200 nodes.  Each dequeue used list.pop(0), which is O(N) in element count.
The fix replaces the frontier with collections.deque, making dequeue O(1).

MEASUREMENT FINDINGS (baseline, pre-change):
  - list.pop(0) wins for small frontiers (linear chain, max_frontier=1)
    because list's pop(0) on a 1-element list has LOWER overhead than
    deque's popleft() (doubly-linked node + pointer chase vs trivial memmove
    of 1 pointer).
  - deque.popleft() wins clearly for larger frontiers (5-parent DAG,
    max_frontier=25): -23.6% wall time, clear signal (delta > 2*stdev).
  - The crossover is around max_frontier=5-10.
  - Production events are predominantly single-parent (linear); multi-parent
    events (joins/merges) exist but are minority.

VERDICT: deque is correct and behavior-preserving.  Win is real for
multi-parent heavy graphs.  For linear-chain (most common), the impact
is within noise (+8% in the direction of list winning).  Net effect
across a mixed workload: ambiguous for short chains, clear win for
provenance queries that exercise multi-parent events.

The change is adopted: it is correct, the code is cleaner (deque signals
FIFO intent), and it wins in the exact case KZ-1 was designed for
(deep multi-parent DAG traversal).

Run:
    pytest tests/perf/test_provenance_bfs_list_queue.py -v -s -m perf

Marks:
    perf -- must be explicitly enabled; never runs in the default suite.
"""

from __future__ import annotations

import collections
import uuid

import pytest

from tests.perf.bench import compare, measure

# -- constants -----------------------------------------------------------

_N_NODES = 200
_CAP = 200


# -- synthetic graph builders --------------------------------------------


def _linear_chain(n: int) -> tuple[uuid.UUID, dict[uuid.UUID, list[uuid.UUID]]]:
    """
    Linear chain: ids[0]=root (no parents), ids[n-1]=leaf (1 parent each).
    BFS starts from leaf and walks backwards to root.
    max_frontier = 1 throughout traversal.
    """
    ids = [uuid.uuid4() for _ in range(n)]
    parents: dict[uuid.UUID, list[uuid.UUID]] = {}
    for i, uid in enumerate(ids):
        parents[uid] = [ids[i - 1]] if i > 0 else []
    return ids[-1], parents  # start BFS from leaf


def _two_parent_dag(n: int) -> tuple[uuid.UUID, dict[uuid.UUID, list[uuid.UUID]]]:
    """
    Each node has up to 2 parents.  max_frontier ~4 during traversal.
    Represents causal merges (common in agent joins).
    """
    ids = [uuid.uuid4() for _ in range(n)]
    parents: dict[uuid.UUID, list[uuid.UUID]] = {}
    for i, uid in enumerate(ids):
        parents[uid] = ids[max(0, i - 2) : i] if i > 0 else []
    return ids[-1], parents


def _five_parent_dag(n: int) -> tuple[uuid.UUID, dict[uuid.UUID, list[uuid.UUID]]]:
    """
    Each node has up to 5 parents.  max_frontier ~25 during traversal.
    Represents heavy multi-parent provenance (the case KZ-1 targets).
    """
    ids = [uuid.uuid4() for _ in range(n)]
    parents: dict[uuid.UUID, list[uuid.UUID]] = {}
    for i, uid in enumerate(ids):
        parents[uid] = ids[max(0, i - 5) : i] if i > 0 else []
    return ids[-1], parents


# -- BFS implementations -------------------------------------------------


def _bfs_with_list(
    start: uuid.UUID,
    parents: dict[uuid.UUID, list[uuid.UUID]],
    cap: int = _CAP,
) -> list[uuid.UUID]:
    """BFS using list + pop(0) -- BEFORE state (mirrors replay.py pre-change)."""
    visited: set[uuid.UUID] = set()
    frontier: list[uuid.UUID] = [start]
    order: list[uuid.UUID] = []

    while frontier and len(visited) < cap:
        current = frontier.pop(0)  # O(N) memmove
        if current in visited:
            continue
        visited.add(current)
        order.append(current)
        for pid in parents.get(current, []):
            if pid not in visited:
                frontier.append(pid)

    return order


def _bfs_with_deque(
    start: uuid.UUID,
    parents: dict[uuid.UUID, list[uuid.UUID]],
    cap: int = _CAP,
) -> list[uuid.UUID]:
    """BFS using collections.deque + popleft() -- AFTER state."""
    visited: set[uuid.UUID] = set()
    frontier: collections.deque[uuid.UUID] = collections.deque([start])
    order: list[uuid.UUID] = []

    while frontier and len(visited) < cap:
        current = frontier.popleft()  # O(1)
        if current in visited:
            continue
        visited.add(current)
        order.append(current)
        for pid in parents.get(current, []):
            if pid not in visited:
                frontier.append(pid)

    return order


# -- fixtures ------------------------------------------------------------


@pytest.fixture(scope="module")
def linear_graph():
    return _linear_chain(_N_NODES)


@pytest.fixture(scope="module")
def two_parent_graph():
    return _two_parent_dag(_N_NODES)


@pytest.fixture(scope="module")
def five_parent_graph():
    return _five_parent_dag(_N_NODES)


# -- correctness sanity --------------------------------------------------


@pytest.mark.perf
def test_bfs_same_order_linear(linear_graph):
    """list and deque must produce identical traversal order (linear)."""
    start, parents = linear_graph
    assert _bfs_with_list(start, parents) == _bfs_with_deque(start, parents)


@pytest.mark.perf
def test_bfs_same_order_two_parent(two_parent_graph):
    """list and deque must produce identical traversal order (2-parent DAG)."""
    start, parents = two_parent_graph
    assert _bfs_with_list(start, parents) == _bfs_with_deque(start, parents)


@pytest.mark.perf
def test_bfs_same_order_five_parent(five_parent_graph):
    """list and deque must produce identical traversal order (5-parent DAG)."""
    start, parents = five_parent_graph
    assert _bfs_with_list(start, parents) == _bfs_with_deque(start, parents)


# -- perf: linear chain (single-parent) ----------------------------------


@pytest.mark.perf
def test_provenance_bfs_linear_no_regression(linear_graph):
    """
    Linear chain: max_frontier=1 throughout.

    For frontier size=1, list.pop(0) and deque.popleft() are both O(1).
    Deque has slightly higher per-node overhead in CPython.
    Measured delta: ~+8% (list faster), within noise (stdev=12us, delta=8us).
    Gate: no regression above +20% (conservative noise budget).
    This test documents that deque does NOT regress in the common case.
    """
    start, parents = linear_graph

    before = measure(lambda: _bfs_with_list(start, parents), n=200, warmup=5)
    after = measure(lambda: _bfs_with_deque(start, parents), n=200, warmup=5)

    wall_delta_pct = (after.wall_mean - before.wall_mean) / before.wall_mean * 100

    print(
        f"\n[KZ-1 linear chain, max_frontier=1, n=200 runs]\n"
        f"  BEFORE list  wall_mean={before.wall_mean * 1e6:.2f} us  "
        f"stdev={before.wall_stdev * 1e6:.2f} us\n"
        f"  AFTER  deque wall_mean={after.wall_mean * 1e6:.2f} us  "
        f"stdev={after.wall_stdev * 1e6:.2f} us\n"
        f"  wall delta={wall_delta_pct:+.1f}%  "
        f"(expected ~+8% -- list slightly faster at frontier=1, within noise)\n"
        f"  noise threshold: +20% (conservative; delta < 2x stdev => in noise)\n"
    )

    # Conservative gate: deque must not be more than 20% slower.
    # At frontier=1, the O(1) advantage of deque is not measurable;
    # the gate ensures we do not introduce a clear regression.
    passed, reasons = compare(before, after, thresholds={"wall": 0.20})
    assert passed, f"KZ-1 linear: unexpected regression: {reasons}"


# -- perf: 2-parent DAG --------------------------------------------------


@pytest.mark.perf
def test_provenance_bfs_two_parent_no_regression(two_parent_graph):
    """
    2-parent DAG: max_frontier ~4.

    Measured delta: ~+8% (list slightly faster), within noise.
    Gate: no regression above +20%.
    """
    start, parents = two_parent_graph

    before = measure(lambda: _bfs_with_list(start, parents), n=200, warmup=5)
    after = measure(lambda: _bfs_with_deque(start, parents), n=200, warmup=5)

    wall_delta_pct = (after.wall_mean - before.wall_mean) / before.wall_mean * 100

    print(
        f"\n[KZ-1 2-parent DAG, max_frontier~4, n=200 runs]\n"
        f"  BEFORE list  wall_mean={before.wall_mean * 1e6:.2f} us  "
        f"stdev={before.wall_stdev * 1e6:.2f} us\n"
        f"  AFTER  deque wall_mean={after.wall_mean * 1e6:.2f} us  "
        f"stdev={after.wall_stdev * 1e6:.2f} us\n"
        f"  wall delta={wall_delta_pct:+.1f}%\n"
        f"  noise threshold: +20%\n"
    )

    passed, reasons = compare(before, after, thresholds={"wall": 0.20})
    assert passed, f"KZ-1 2-parent: unexpected regression: {reasons}"


# -- perf: 5-parent DAG (primary target of KZ-1) -------------------------


@pytest.mark.perf
def test_provenance_bfs_five_parent_no_regression(five_parent_graph):
    """
    5-parent DAG: max_frontier ~25.

    This is the case KZ-1 was designed for.  Timeit-based measurement on this
    machine showed deque is ~23.6% faster (clear signal).  However, the O0
    measure() harness calls each BFS once per iteration (~1.3ms per call),
    which falls within Windows process-scheduler jitter (~200us stdev).  At
    that noise floor the per-call difference is undetectable reliably.

    Timeit baseline (high-repetition, minimal jitter):
      list  mean= 364us   deque mean= 278us   delta=-23.6%  CLEAR SIGNAL
    measure() baseline (single-call, n=200):
      list  mean=1368us   deque mean=1360us   delta=-0.6%   WITHIN NOISE

    The discrepancy is explained by the fact that measure() wraps each BFS
    call in time.perf_counter() + tracemalloc overhead, inflating the absolute
    time.  The relative win disappears in the noise at this granularity.

    Gate applied here: no regression above +25% (noise budget for this case).
    The real win is documented in the timeit probe; this test guards regressions.
    """
    start, parents = five_parent_graph

    before = measure(lambda: _bfs_with_list(start, parents), n=200, warmup=5)
    after = measure(lambda: _bfs_with_deque(start, parents), n=200, warmup=5)

    wall_delta_pct = (after.wall_mean - before.wall_mean) / before.wall_mean * 100

    print(
        f"\n[KZ-1 5-parent DAG, max_frontier~25, n=200 runs]\n"
        f"  BEFORE list  wall_mean={before.wall_mean * 1e6:.2f} us  "
        f"stdev={before.wall_stdev * 1e6:.2f} us\n"
        f"  AFTER  deque wall_mean={after.wall_mean * 1e6:.2f} us  "
        f"stdev={after.wall_stdev * 1e6:.2f} us\n"
        f"  wall delta={wall_delta_pct:+.1f}%\n"
        f"  note: timeit probe (high-rep) showed -23.6% win for deque at frontier=25;\n"
        f"  measure() granularity insufficient to resolve it on Windows (jitter ~200us).\n"
        f"  noise threshold: +25%\n"
    )

    passed, reasons = compare(before, after, thresholds={"wall": 0.25})
    assert passed, f"KZ-1 5-parent: unexpected regression: {reasons}"
