"""Supply-weighted funder-cluster admission policy for funded Solana entries."""
from __future__ import annotations

import inspect
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable

from sniper.util.clock import real_utc_now_iso


POLICY_ID = "solana-funded-cluster-admission-v3"
# Exact integer ratios: clustered >= 25/100 of attempted, top funder >= 50/100.
CLUSTERED_LIMIT_NUM, CLUSTERED_LIMIT_DEN = 25, 100
TOP_FUNDER_LIMIT_NUM, TOP_FUNDER_LIMIT_DEN = 50, 100
CLUSTERED_LIMIT = CLUSTERED_LIMIT_NUM / CLUSTERED_LIMIT_DEN      # reporting only
TOP_FUNDER_LIMIT = TOP_FUNDER_LIMIT_NUM / TOP_FUNDER_LIMIT_DEN   # reporting only
MIN_ELIGIBLE_OWNERS = 4


class ClusterQuotaError(RuntimeError):
    """The research RPC quota blocked a funder trace."""


@dataclass(frozen=True)
class ClusterDecision:
    decision_id: str
    allowed: bool
    reason: str
    policy_id: str
    snapshot_at: str
    completed_at: str
    metrics: dict[str, int | float | None]


def decide_cluster(
    owners_by_supply: list[tuple[str, int]],
    cached: dict[str, str],
    *,
    trace_fn: Callable[[str], tuple[str | None, str]],
    persist_fn: Callable[[str, str | None, str], None] | None = None,
    workers: int = 8,
    deadline_s: float = 12.0,
    clock: Callable[[], float] = time.monotonic,
    now_fn: Callable[[], str] = real_utc_now_iso,
    snapshot_at: str | None = None,
    thin_owner_pass: bool = False,
    early_allow: bool = False,
    non_clusterable: frozenset = frozenset(),
) -> ClusterDecision:
    """Block on final cluster evidence and require full resolution for an allow.

    `early_allow` (desk only, operator 2026-10-22 'fix the cluster budget'): also allow as soon as NO
    assignment of the still-unresolved supply could reach a limit: every unresolved owner joining the
    largest funder group (top) or turning the largest singleton into a cluster (clustered). Exact
    integer bound, so an early allow is the same verdict full resolution could at worst give. Its
    reasons are allowed_bound / pass_thin_bound, distinct from a fully resolved allow."""
    snapshot_at = snapshot_at or now_fn()
    balances: dict[str, int] = {}
    for owner, raw_balance in owners_by_supply:
        balance = int(raw_balance)
        if balance > 0:
            balances[owner] = balances.get(owner, 0) + balance
    attempted = sum(balances.values())
    funders = {owner: cached[owner] for owner in balances if cached.get(owner)}

    def clusterable() -> dict:
        """Resolved owners whose funder can form a group. A funder in `non_clusterable` (desk only: a frozen list of shared
        infrastructure such as exchange hot wallets) resolves its owner but is NOT a same-entity signal, so it joins no group and
        counts toward neither top_funder nor clustered. Empty list = every resolved owner (the funded gate's behaviour)."""
        return {o: f for o, f in funders.items() if f not in non_clusterable} if non_clusterable else funders

    def metrics() -> dict[str, int | float | None]:
        groups: dict[str, list[str]] = {}
        for owner, funder in clusterable().items():
            groups.setdefault(funder, []).append(owner)
        if thin:
            # THIN BOOK (desk only, operator 2026-10-21 option b): a lone owner is trivially its
            # own top funder, so only a funder SHARED by two or more owners counts as a cluster.
            groups = {f: o for f, o in groups.items() if len(o) >= 2}
        clustered = sum(
            balances[owner]
            for grouped_owners in groups.values()
            if len(grouped_owners) >= 2
            for owner in grouped_owners
        )
        top = max(
            (
                sum(balances[owner] for owner in grouped_owners)
                for grouped_owners in groups.values()
            ),
            default=0,
        )
        resolved = sum(balances[owner] for owner in funders)
        excluded = [o for o, f in funders.items() if f in non_clusterable] if non_clusterable else []
        extra = ({"excluded_known_funders": len(excluded),
                  "excluded_known_funders_of_attempted": sum(balances[o] for o in excluded) / attempted if attempted else 0.0}
                 if non_clusterable else {})
        return {
            **extra,
            "attempted_supply": attempted,
            "resolved_supply": resolved,
            "clustered_supply": clustered,
            "largest_funder_supply": top,
            "coverage": resolved / attempted if attempted else 0.0,
            "clustered_of_attempted": clustered / attempted if attempted else 0.0,
            "top_funder_of_attempted": top / attempted if attempted else 0.0,
            "eligible": len(balances),
            "resolved": len(funders),
        }

    def final(reason: str, allowed: bool = False) -> ClusterDecision:
        return ClusterDecision(
            decision_id=uuid.uuid4().hex,
            allowed=allowed,
            reason=reason,
            policy_id=POLICY_ID,
            snapshot_at=snapshot_at,
            completed_at=now_fn(),
            metrics=metrics(),
        )

    thin = False
    if not balances:
        return final("cluster_no_holders")
    if len(balances) < MIN_ELIGIBLE_OWNERS:
        if not thin_owner_pass:
            return final("cluster_too_few_owners")
        thin = True                     # trace the owners that exist; block only on a shared funder

    # Thresholds are compared as EXACT INTEGER RATIOS, never floats. Atomic balances reach
    # 1e15+, where float division loses precision and can move an exactly-25% or exactly-50%
    # book across the limit in either direction — and those exact boundaries are the ones
    # with dedicated regression tests. `clustered * 100 >= attempted * 25` is exact for any
    # Python int. The float ratios in metrics() stay for reporting only.
    def _at_or_over(part: int, whole: int, numerator: int, denominator: int) -> bool:
        return part * denominator >= whole * numerator

    def final_block() -> ClusterDecision | None:
        current = metrics()
        if _at_or_over(int(current["clustered_supply"]), attempted,
                       CLUSTERED_LIMIT_NUM, CLUSTERED_LIMIT_DEN):
            return final("cluster_share_high")
        if _at_or_over(int(current["largest_funder_supply"]), attempted,
                       TOP_FUNDER_LIMIT_NUM, TOP_FUNDER_LIMIT_DEN):
            return final("cluster_top_funder_high")
        return None

    def bound_allows() -> bool:
        """True when the unresolved supply cannot push either limit, whatever its funders are.
        Worst case: every unresolved owner shares the largest group's funder (top), and each open
        owner joins a different singleton (clustered). An unresolved owner can only form a cluster with SOMEONE: a lone unresolved
        owner with nothing resolved cannot (thin rule: the lone owner is no shared cluster)."""
        groups: dict[str, int] = {}
        sizes: dict[str, int] = {}
        for owner, funder in clusterable().items():
            groups[funder] = groups.get(funder, 0) + balances[owner]
            sizes[funder] = sizes.get(funder, 0) + 1
        open_owners = [o for o in balances if o not in funders]
        unresolved = sum(balances[o] for o in open_owners)
        joinable = unresolved if (len(open_owners) >= 2 or clusterable()) else 0
        clustered = sum(v for f, v in groups.items() if sizes[f] >= 2)
        singles = sorted((v for f, v in groups.items() if sizes[f] == 1), reverse=True)
        singleton = singles[0] if singles else 0
        # each open owner may join a DIFFERENT singleton's funder, turning each into a cluster
        # (Gamma, 7a0610431 BLOCK): the worst case adds the top-k singletons, k = open owners
        turned = sum(singles[:len(open_owners)])
        shared_top = max((v for f, v in groups.items() if sizes[f] >= 2), default=0)
        any_top = max(groups.values(), default=0)
        top_worst = max(shared_top if thin else any_top, singleton) + joinable
        clustered_worst = clustered + joinable + (turned if joinable else 0)
        return (clustered_worst * CLUSTERED_LIMIT_DEN < attempted * CLUSTERED_LIMIT_NUM
                and top_worst * TOP_FUNDER_LIMIT_DEN < attempted * TOP_FUNDER_LIMIT_NUM)

    def early() -> ClusterDecision | None:
        if early_allow and len(funders) < len(balances) and bound_allows():
            return final("pass_thin_bound" if thin else "allowed_bound", allowed=True)
        return None

    blocked = final_block() or early()
    if blocked is not None:
        return blocked

    todo = sorted(
        (owner for owner in balances if owner not in funders),
        key=balances.get,
        reverse=True,
    )
    started = clock()
    # STOPPING A TRACE NEEDS THE TRACE'S COOPERATION. Nothing outside a running trace can end it:
    # `future.cancel()` only drops futures that never started and `shutdown(wait=False)` does not
    # wait, so every deadline used to return while its traces kept reading up to 20 paged RPC calls.
    # The leak is ACROSS calls, not within one — the loop returns on the first deadline, so a single
    # call never built a second pool with live threads. Measured on five back-to-back deadlined
    # calls at workers=3: 15 traces still running, 16 threads alive, each holding an RPC connection
    # and spending the research quota. A pool hoist does not touch that; only the flag below does.
    #
    # `should_stop` is passed only to a trace_fn that accepts it, so single-argument callables
    # (tests, the cache worker) keep working unchanged. The event is set on EVERY exit path.
    stop = threading.Event()
    try:
        _accepts_stop = "should_stop" in inspect.signature(trace_fn).parameters
    except (TypeError, ValueError):
        _accepts_stop = False
    submit_fn = ((lambda owner: trace_fn(owner, should_stop=stop.is_set)) if _accepts_stop
                 else trace_fn)
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        while todo:
            remaining = deadline_s - (clock() - started)
            if remaining <= 0:
                return final("cluster_unknown_deadline")
            batch, todo = todo[: max(1, workers)], todo[max(1, workers) :]
            futures = {pool.submit(submit_fn, owner): owner for owner in batch}
            done, pending = wait(futures, timeout=remaining)
            if pending:
                for future in pending:
                    future.cancel()
                return final("cluster_unknown_deadline")
            for future in done:
                owner = futures[future]
                try:
                    funder, status = future.result()
                except ClusterQuotaError:
                    return final("cluster_unknown_quota")
                except Exception:
                    return final("cluster_unknown_trace_error")
                if persist_fn is not None:
                    persist_fn(owner, funder, status)
                if status == "resolved" and funder:
                    funders[owner] = funder
            blocked = final_block() or early()
            if blocked is not None:
                return blocked
    finally:
        stop.set()                     # every exit path stops the in-flight traces
        pool.shutdown(wait=False, cancel_futures=True)
    if len(funders) != len(balances):
        return final("cluster_unknown_unresolved")
    return final("pass_thin_owners" if thin else "allowed", allowed=True)
