"""§3 + §11 — Top-level orchestrator: analyze_wallet(wallet) → ProtocolReport.

Stage 0 (discovery): caller's responsibility — this entrypoint takes a wallet
address and assumes the wallet is already a candidate.

Stage 1 (cheap-reject gates): R1 hedge archetype, R2 insufficient sample,
R3 dormant. F1–F4 caution flags surfaced regardless.

Stage 2 (deep analysis): mode router → held-analyst and/or flipper branch(es).

Stage 3 (decision): apply §11 mixed-mode matrix or single-branch shortcut to
populate `overall_verdict` + `primary_edge_type` + `copyability_note`.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import requests

from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
from sniper.analysis.wallet_protocol.flip_reconstruction import reconstruct_flips
from sniper.analysis.wallet_protocol.flipper import (
    FlipRoiBaselineFn,
    PeerBaselineFn,
    PriceHistoryFn,
    run_flipper_branch,
)
from sniper.analysis.wallet_protocol.wallet_fixture_hash import (
    compute_wallet_fixture_hash_from_data,
)
from sniper.analysis.wallet_protocol.held_analyst import (
    compute_dw_ew_roi,
    filter_held_to_resolution,
    run_held_branch,
)
from sniper.analysis.wallet_protocol.mode_router import classify_mode
from sniper.analysis.wallet_protocol.stage1_gates import evaluate_stage1_gates
from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    OverallVerdict,
    ProtocolReport,
    VerdictLabel,
    WalletData,
)


# ---------------------------------------------------------------------------
# Issue #24 — Negative-PnL edge-detected honesty floor (Pestle).
#
# A report-level guard that runs AFTER overall_verdict has been assembled in
# `analyze_wallet`. If a wallet was rendered `edge_detected` despite having
# non-positive net_pnl AND no non-trivial explanatory-surface coverage
# (attribution / timing / position-building), the verdict is downgraded to
# `underpowered` and a `NEGATIVE_PNL_EDGE_UNEXPLAINED` flag is appended.
#
# Display-only: the floor never feeds gate_result or primary_edge_type. It
# only mutates `overall_verdict`, `copyability_note`, and `data_quality_flags`
# when the doctrine violation is detected. Pure dict reads — no I/O.
# ---------------------------------------------------------------------------

NEGATIVE_PNL_EDGE_UNEXPLAINED = "NEGATIVE_PNL_EDGE_UNEXPLAINED"

# Phase A split #2 — fetch-rebuild design 2026-05-12.
# Emitted when realized_pnl <= 0 but combined_pnl > 0 (i.e. edge depends on
# unrealized open-position value rather than closed-position history).
# Verdict stays edge_detected; only a data_quality_flag is appended.
EDGE_DEPENDS_ON_OPEN_POSITIONS = "EDGE_DEPENDS_ON_OPEN_POSITIONS"

VALID_TAG_LOOKUP_STATES = frozenset({"ok", "empty", "unavailable", "skipped"})

# ---------------------------------------------------------------------------
# Tag-grain rescue honesty floor (spec: 2026-05-12-tag-grain-rescue-honesty-floor).
#
# A branch-level caveat emitted AFTER `_apply_negative_pnl_honesty_floor` when
# a branch was promoted to "supported" solely on tag-grain evidence with no
# primary hypothesis surviving out-of-sample holdout. Caveat-only: does NOT
# mutate overall_verdict, gate_result, primary_edge_type, or copyability_note.
# ---------------------------------------------------------------------------

BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY = "BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY"


def _branch_promoted_on_tag_grain_only(branch: BranchReport) -> bool:
    """Return True iff the branch was promoted on tag-grain evidence alone.

    All four conditions must hold:
    1. branch.is_active is True
    2. _branch_verdict(branch) == "supported"
    3. At least one tag grain with verdict == "supported" exists
    4. No primary hypothesis is both "supported" AND did not fail OOS
       ("did not fail OOS" means time_split entry is absent OR holdout_status != "failed")
    """
    if not branch.is_active:
        return False

    if _branch_verdict(branch) != "supported":
        return False

    if not any(g.verdict == "supported" for g in branch.tag_grains):
        return False

    # Build primary set, mirroring the ADR-002 conditional in _branch_verdict.
    base_primary = (
        HELD_PRIMARY_TESTS if branch.branch == "held_analyst" else FLIPPER_PRIMARY_TESTS
    )
    primary: list[str] = list(base_primary)
    if branch.branch == "held_analyst":
        h2 = next((h for h in branch.hypotheses if h.name == "H2"), None)
        h2_sub = next((h for h in branch.hypotheses if h.name == "H2_sub080"), None)
        if (
            h2 is not None
            and h2.verdict == "refuted"
            and h2_sub is not None
            and h2_sub.verdict not in ("underpowered", "n/a")
        ):
            primary.append("H2_sub080")

    # Check whether any primary hypothesis survived OOS (i.e., did NOT fail).
    for h in branch.hypotheses:
        if h.name not in primary:
            continue
        if h.verdict != "supported":
            continue
        ts = branch.time_split.get(h.name)
        # "did not fail OOS" = no time_split entry OR holdout_status is not "failed"
        if ts is None or ts.holdout_status != "failed":
            return False  # at least one primary survived OOS — not a tag-grain rescue

    return True


def _apply_tag_grain_rescue_caveat(report: "ProtocolReport") -> "ProtocolReport":
    """Emit a branch-level caveat when a branch was promoted on tag-grain evidence alone.

    Iterates held_analyst_branch and flipper_branch. When the predicate fires,
    appends BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY to data_quality_flags and a
    human-readable entry to applicable_limitations.

    Does NOT mutate overall_verdict, primary_edge_type, copyability_note,
    gate_result, hypothesis verdicts, time-split outcomes, or tag-grain verdicts.
    """
    for branch in (report.held_analyst_branch, report.flipper_branch):
        if branch is None:
            continue
        if not _branch_promoted_on_tag_grain_only(branch):
            continue

        # Deterministic top grain: sort supported grains by (grain, tag) tuple.
        supported_grains = sorted(
            (g for g in branch.tag_grains if g.verdict == "supported"),
            key=lambda g: (g.grain, g.tag),
        )
        top_grain = supported_grains[0]

        # Failed primaries: supported + time-split holdout_status == "failed".
        base_primary = (
            HELD_PRIMARY_TESTS
            if branch.branch == "held_analyst"
            else FLIPPER_PRIMARY_TESTS
        )
        primary: list[str] = list(base_primary)
        if branch.branch == "held_analyst":
            h2 = next((h for h in branch.hypotheses if h.name == "H2"), None)
            h2_sub = next((h for h in branch.hypotheses if h.name == "H2_sub080"), None)
            if (
                h2 is not None
                and h2.verdict == "refuted"
                and h2_sub is not None
                and h2_sub.verdict not in ("underpowered", "n/a")
            ):
                primary.append("H2_sub080")

        sorted_failed_names = sorted(
            h.name
            for h in branch.hypotheses
            if h.name in primary
            and h.verdict == "supported"
            and branch.time_split.get(h.name) is not None
            and branch.time_split[h.name].holdout_status == "failed"
        )

        flag_string = (
            f"{BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY}: {branch.branch} "
            f"(grain={top_grain.grain}/{top_grain.tag}, "
            f"primaries_failed_oos={sorted_failed_names})"
        )
        limitation_line = (
            f"Branch '{branch.branch}' promoted to supported on tag-local "
            f"evidence ({top_grain.grain}/{top_grain.tag}) only; no primary "
            f"hypothesis survived out-of-sample holdout. Edge claim is "
            f"category-local, not validated by wallet-level OOS testing."
        )
        report.data_quality_flags.append(flag_string)
        report.applicable_limitations.append(limitation_line)

    return report


def attribution_coverage_nontrivial(data_completeness: dict | None) -> bool:
    """True iff `attribution_coverage` shows >0 resolved-attribution rows over
    a non-empty total. None input → False (no coverage knowable)."""
    if data_completeness is None:
        return False
    ac = data_completeness.get("attribution_coverage") or {}
    try:
        n_with_resolved = int(ac.get("n_with_resolved_attribution") or 0)
        n_total = int(ac.get("n_total") or 0)
    except (TypeError, ValueError):
        return False
    return n_with_resolved > 0 and n_total > 0


def timing_coverage_nontrivial(timing_surface: dict | None) -> bool:
    """True iff timing-surface `confidence_band` is one of {partial, good, high}.
    None input or missing band → False."""
    if timing_surface is None:
        return False
    band = timing_surface.get("confidence_band")
    return band in {"partial", "good", "high"}


def position_coverage_nontrivial(position_building_surface: dict | None) -> bool:
    """True iff `wallet_summary.observation_window_coverage` is a number
    strictly greater than 0.30. None input or non-numeric coverage → False."""
    if position_building_surface is None:
        return False
    ws = position_building_surface.get("wallet_summary") or {}
    cov = ws.get("observation_window_coverage")
    # Strict numeric check; booleans are not accepted as coverage values.
    if isinstance(cov, bool) or not isinstance(cov, (int, float)):
        return False
    return cov > 0.30


def _apply_negative_pnl_honesty_floor(report: ProtocolReport) -> ProtocolReport:
    """Apply the Issue #24 honesty floor to ``report``.

    No-op unless ``report.overall_verdict == "edge_detected"``.

    PnL gate uses BOTH realized and combined signals (Phase A split #2):
    - pnl_floor_satisfied = (realized_pnl > 0) OR (combined_pnl > 0)

    When realized <= 0 AND combined <= 0 AND no coverage → downgrade to
    ``"underpowered"`` + ``NEGATIVE_PNL_EDGE_UNEXPLAINED`` flag.

    When realized <= 0 but combined > 0 AND no coverage → verdict stays
    ``"edge_detected"`` but ``EDGE_DEPENDS_ON_OPEN_POSITIONS`` flag is appended
    (informational; mutually exclusive with NEGATIVE_PNL_EDGE_UNEXPLAINED).

    When coverage is non-trivial, neither flag fires regardless of PnL sign.
    """
    if report.overall_verdict != "edge_detected":
        return report

    # New: positive PnL gate uses both signals.
    # net_pnl is included as a backward-compat fallback: old callers that
    # construct ProtocolReport(net_pnl=X) without setting realized_pnl/combined_pnl
    # still satisfy the gate when net_pnl > 0 (net_pnl == realized_pnl invariant).
    pnl_floor_satisfied = (
        (report.realized_pnl > 0)
        or (report.combined_pnl > 0)
        or (report.net_pnl > 0)
    )
    coverage_floor_satisfied = (
        attribution_coverage_nontrivial(report.data_completeness)
        or timing_coverage_nontrivial(report.timing_surface)
        or position_coverage_nontrivial(report.position_building_surface)
    )

    if pnl_floor_satisfied or coverage_floor_satisfied:
        # Special caveat: realized is non-positive but combined survived on unrealized.
        if (
            report.realized_pnl <= 0
            and report.combined_pnl > 0
            and not coverage_floor_satisfied
        ):
            report.data_quality_flags.append(
                f"{EDGE_DEPENDS_ON_OPEN_POSITIONS}: "
                f"realized={report.realized_pnl:.0f}, "
                f"unrealized={report.unrealized_pnl:.0f}"
            )
        return report

    # Both signals non-positive AND no coverage — existing downgrade.
    report.data_quality_flags.append(NEGATIVE_PNL_EDGE_UNEXPLAINED)
    report.overall_verdict = "underpowered"
    report.copyability_note = (
        "Honesty floor: edge_detected was inconsistent with net_pnl <= 0 "
        "and no non-trivial explanatory-surface coverage. Verdict downgraded; "
        "see NEGATIVE_PNL_EDGE_UNEXPLAINED."
    )
    return report


HELD_PRIMARY_TESTS = ("H2", "H3", "H5")
FLIPPER_PRIMARY_TESTS = ("FH1", "FH2", "FH4")

_ACCOUNTING_TIE_THRESHOLD = 0.02  # 2%


def _compute_open_position_breakdown(
    open_positions: list[dict],
    now_ts: int,
) -> "dict | None":
    """Compute open-position health breakdown for ProtocolReport.open_position_breakdown.

    Returns None when open_positions is empty.
    Otherwise returns a dict with shape documented in types.ProtocolReport.

    Categorisation:
    - dead:       curPrice <= 0.01
    - in_money:   curPrice > avgPrice  AND curPrice > 0.01
    - underwater: curPrice <= avgPrice AND curPrice > 0.01
    """
    if not open_positions:
        return None

    import statistics as _stats

    n_dead = 0
    n_in_money = 0
    n_underwater = 0
    dead_capital = 0.0
    in_money_capital = 0.0
    underwater_capital = 0.0
    in_money_unrealized = 0.0
    underwater_unrealized = 0.0
    hold_days: list[float] = []

    for p in open_positions:
        cur = float(p.get("curPrice") or 0.0)
        avg = float(p.get("avgPrice") or 0.0)
        size = float(p.get("size") or 0.0)
        capital = avg * size
        unrealized = (cur - avg) * size

        # time held
        start_raw = p.get("startDate")
        if start_raw is not None:
            try:
                from datetime import datetime, timezone as _tz
                if isinstance(start_raw, (int, float)):
                    start_ts = float(start_raw)
                else:
                    dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
                    start_ts = dt.timestamp()
                days = (now_ts - start_ts) / 86_400.0
                if days >= 0:
                    hold_days.append(days)
            except Exception:
                pass

        if cur <= 0.01:
            n_dead += 1
            dead_capital += capital
        elif cur > avg:
            n_in_money += 1
            in_money_capital += capital
            in_money_unrealized += unrealized
        else:
            n_underwater += 1
            underwater_capital += capital
            underwater_unrealized += unrealized

    # time_in_position quantiles
    if hold_days:
        sorted_days = sorted(hold_days)
        n = len(sorted_days)

        def _percentile(data: list[float], pct: float) -> float:
            idx = pct / 100.0 * (len(data) - 1)
            lo = int(idx)
            hi = lo + 1
            if hi >= len(data):
                return data[-1]
            frac = idx - lo
            return data[lo] + frac * (data[hi] - data[lo])

        median_days: "float | None" = _percentile(sorted_days, 50)
        p25_days: "float | None" = _percentile(sorted_days, 25)
        p75_days: "float | None" = _percentile(sorted_days, 75)
        p95_days: "float | None" = _percentile(sorted_days, 95)
        n_stale_90d = sum(1 for d in sorted_days if d > 90)
        n_stale_365d = sum(1 for d in sorted_days if d > 365)
    else:
        median_days = None
        p25_days = None
        p75_days = None
        p95_days = None
        n_stale_90d = 0
        n_stale_365d = 0

    return {
        "n_open_total": len(open_positions),
        "n_open_dead": n_dead,
        "n_open_in_money": n_in_money,
        "n_open_underwater": n_underwater,
        "dead_capital_usdc": dead_capital,
        "in_money_capital_usdc": in_money_capital,
        "underwater_capital_usdc": underwater_capital,
        "in_money_unrealized_pnl": in_money_unrealized,
        "underwater_unrealized_pnl": underwater_unrealized,
        "time_in_position": {
            "median_days": median_days,
            "p25_days": p25_days,
            "p75_days": p75_days,
            "p95_days": p95_days,
            "n_stale_90d": n_stale_90d,
            "n_stale_365d": n_stale_365d,
        },
    }


def _compute_accounting_tie(
    capital_ledger: "CapitalLedger | None",
    realized_pnl: float,
    open_positions: list[dict],
    portfolio_value_usdc: float | None,
    portfolio_value_observed_at: int | None = None,
) -> list[str]:
    """Emit portfolio-value diagnostics.

    `/value` is current wallet value. It is not lifetime equity, so it must
    not be treated as proof that closed-position P&L reconciles to the
    capital ledger. The useful check here is current open mark vs current
    wallet value; the lifetime tie remains unsupported until we have the
    wallet's external USDC transfer ledger.

    Caveats (documented so readers understand expected failures):
    - net_capital_funded covers USDC traded through CTF (SPLIT−MERGE) only.
      It is not external wallet funding.
    - realized_pnl from /closed-positions includes settlement via redemptions.
    - curPrice may be stale, so open-mark parity is diagnostic, not authority.
    """
    if capital_ledger is None or portfolio_value_usdc is None:
        return []

    open_value = sum(
        float(p.get("curPrice") or 0.0) * float(p.get("size") or 0.0)
        for p in open_positions
    )
    unrealized_pnl = sum(
        (float(p.get("curPrice") or 0.0) - float(p.get("avgPrice") or 0.0))
        * float(p.get("size") or 0.0)
        for p in open_positions
    )
    implied_idle = portfolio_value_usdc - open_value
    open_delta_pct = abs(implied_idle) / max(abs(portfolio_value_usdc), 1.0)

    flags: list[str] = []
    prefix = (
        "PORTFOLIO_VALUE_OPEN_MARK_OK"
        if implied_idle >= 0 or open_delta_pct <= _ACCOUNTING_TIE_THRESHOLD
        else "PORTFOLIO_VALUE_OPEN_MARK_FAILED"
    )
    observed = (
        f" observed_at={portfolio_value_observed_at}"
        if portfolio_value_observed_at is not None
        else ""
    )
    flags.append(
        f"{prefix}: portfolio_value=${portfolio_value_usdc:,.2f} "
        f"open_mark=${open_value:,.2f} implied_idle_usdc=${implied_idle:,.2f} "
        f"delta_pct={open_delta_pct*100:.2f}%{observed}"
    )

    lifecycle_estimate = (
        capital_ledger.net_capital_funded_usdc + realized_pnl + unrealized_pnl
    )
    lifecycle_delta = lifecycle_estimate - portfolio_value_usdc
    lifecycle_pct = abs(lifecycle_delta) / max(abs(portfolio_value_usdc), 1.0)
    flags.append(
        "ACCOUNTING_TIE_UNSUPPORTED: "
        f"lifecycle_estimate=${lifecycle_estimate:,.2f} "
        f"portfolio_value=${portfolio_value_usdc:,.2f} "
        f"delta=${lifecycle_delta:,.2f} delta_pct={lifecycle_pct*100:.2f}% "
        f"(unrealized=${unrealized_pnl:,.2f} "
        f"net_ctf_flow=${capital_ledger.net_capital_funded_usdc:,.2f}; "
        "portfolio_value is current holdings, not lifetime equity)"
    )
    return flags

_EXECUTION_QUALITY_FIELDS = (
    "skew_vs_preceding_bar",
    "skew_vs_tape_5min",
    "drift_1h",
    "drift_6h",
    "drift_24h",
)


def _outcome_bucket(raw: Any) -> str:
    """Bucket Polymarket outcome label into 'yes' / 'no' / 'other'."""
    label = (str(raw or "")).strip().lower()
    if label in ("yes", "long"):
        return "yes"
    if label in ("no", "short"):
        return "no"
    return "other"


def _summarize_bucket(rows: Sequence[Mapping]) -> dict:
    """Compute (n, win_rate, roi, total_pnl) for a list of closed positions.

    Win definition: realizedPnl > 0. See `_compute_outcome_side_breakdown`
    docstring for rationale.
    """
    from sniper.analysis.wallet_protocol.held_analyst import (
        _entry_notional, _realized_pnl,
    )
    pnls = [_realized_pnl(p) for p in rows]
    notionals = [_entry_notional(p) for p in rows]
    rois = [
        (p / n) if n > 0 else 0.0
        for p, n in zip(pnls, notionals)
    ]
    wins = [p > 0 for p in pnls]
    return {
        "n": len(rows),
        "win_rate": sum(1 for w in wins if w) / len(wins) if rows else None,
        "roi": sum(rois) / len(rois) if rois else None,
        "total_pnl": float(sum(pnls)),
    }


def _top_markets_by(
    rows: Sequence[Mapping], key_fn, *, k: int = 5,
) -> list[dict]:
    """Return the top-k closed positions by the given key (descending by abs)."""
    from sniper.analysis.wallet_protocol.held_analyst import _realized_pnl
    if not rows:
        return []
    indexed = sorted(rows, key=lambda p: abs(key_fn(p) or 0.0), reverse=True)
    out: list[dict] = []
    for p in indexed[:k]:
        out.append({
            "condition_id": p.get("conditionId"),
            "title": p.get("title"),
            "outcome": p.get("outcome"),
            "pnl": _realized_pnl(p),
            "total_bought": float(p.get("totalBought") or 0.0),
            "event_slug": p.get("eventSlug"),
        })
    return out


def _other_bucket_detail(
    rows: Sequence[Mapping],
    leafs_by_cid: Mapping[str, Sequence[str]] | None,
    nav_lookup: Mapping[str, str] | None,
) -> dict:
    """Build the OTHER-bucket breakdown.

    OTHER is defined as closed positions whose `outcome` label is not in
    {yes, no, long, short}. These are categorical / multi-outcome markets:
    candidate names ("Trump", "Kamala"), team names ("England"),
    descriptive binary labels ("Over"/"Under"), esports rosters, etc.
    """
    from collections import Counter
    from sniper.analysis.wallet_protocol.held_analyst import _realized_pnl

    detail: dict = {}

    # Outcome labels — top 5 by count, with per-label total PnL.
    label_counts: Counter = Counter()
    label_pnl: dict[str, float] = {}
    for p in rows:
        label = (str(p.get("outcome") or "")).strip()
        if not label:
            continue
        label_counts[label] += 1
        label_pnl[label] = label_pnl.get(label, 0.0) + _realized_pnl(p)
    detail["outcome_labels"] = [
        {"label": lab, "n": cnt, "total_pnl": float(label_pnl.get(lab, 0.0))}
        for lab, cnt in label_counts.most_common(5)
    ]

    # Top markets by realized PnL (absolute) and by total bought (volume).
    detail["top_markets_by_pnl"] = _top_markets_by(
        rows, lambda p: _realized_pnl(p), k=5,
    )
    detail["top_markets_by_volume"] = _top_markets_by(
        rows, lambda p: float(p.get("totalBought") or 0.0), k=5,
    )

    # Tag coverage. `leafs_by_cid` may be None when the run did not capture
    # tag lookups (e.g. fixture-replay without DB). When None, with_leaf_tags
    # is reported as "unknown" rather than 0 to avoid implying coverage data
    # was queried and came back empty.
    if leafs_by_cid is None:
        detail["tag_coverage"] = {
            "total": len(rows),
            "with_leaf_tags": None,
            "without_leaf_tags": None,
            "with_nav_mapping": None,
            "without_nav_mapping": None,
            "note": "tag lookup not available on this run",
        }
    else:
        with_leafs = 0
        without_leafs = 0
        with_nav = 0
        without_nav = 0
        nav_counts: Counter = Counter()
        for p in rows:
            cid = p.get("conditionId") or ""
            leafs = leafs_by_cid.get(cid) or []
            if leafs:
                with_leafs += 1
                if nav_lookup is not None:
                    mapped_navs = [
                        nav_lookup[t] for t in leafs if t in nav_lookup
                    ]
                    if mapped_navs:
                        with_nav += 1
                        for nav in set(mapped_navs):
                            nav_counts[nav] += 1
                    else:
                        without_nav += 1
            else:
                without_leafs += 1
        detail["tag_coverage"] = {
            "total": len(rows),
            "with_leaf_tags": with_leafs,
            "without_leaf_tags": without_leafs,
            "with_nav_mapping": with_nav if nav_lookup is not None else None,
            "without_nav_mapping": without_nav if nav_lookup is not None else None,
        }
        if nav_lookup is not None:
            detail["top_nav_categories"] = [
                {"category": cat, "n": cnt}
                for cat, cnt in nav_counts.most_common(5)
            ]
    return detail


# OTHER share above this fraction triggers a caveat line in the report.
_OTHER_BUCKET_CAVEAT_THRESHOLD: float = 0.25


def _compute_outcome_side_breakdown(
    closed_positions: Sequence[Mapping],
    *,
    leafs_by_cid: Mapping[str, Sequence[str]] | None = None,
    nav_lookup: Mapping[str, str] | None = None,
    activity_rows: Sequence[Mapping] | None = None,
    activity_cap_hit: bool = False,
    subgraph_fallback_used: bool = False,
    walk_full_history: bool = False,
) -> dict | None:
    """Edge Surface v1 — bucket closed positions by outcome side and
    aggregate descriptive financials.

    Display-only; NEVER feeds verdict logic. Bucket = 'yes' / 'no' / 'other'.
    `other` bucket is populated only when at least one position has a
    non-yes/no outcome. Returns None when no closed positions are present.

    Win definition: `realizedPnl > 0`. We deliberately do NOT reuse
    `_is_win` here because `_is_win` is curPrice-at-resolution and
    /closed-positions can include pre-resolution exits whose curPrice is
    mid-band. P&L-based win is the universally-meaningful binary at the
    closed-positions aggregate level.

    OTHER-bucket detail:
        outcome_labels: top 5 labels by count, each with n + total_pnl.
        top_markets_by_pnl / top_markets_by_volume: top 5 markets within OTHER.
        tag_coverage: with/without leaf tags + with/without nav mapping.
        top_nav_categories: top 5 nav categories among OTHER positions.
    Per-bucket `top_markets_by_pnl` is also attached to YES and NO so
    operators can spot single-market concentration.

    `caveat` is set on the breakdown when OTHER share exceeds
    `_OTHER_BUCKET_CAVEAT_THRESHOLD`; render layer surfaces it as a clear
    "edge surface is incomplete" warning.
    """
    if not closed_positions:
        return None
    from sniper.analysis.wallet_protocol.exit_attribution import (
        attribute_subset,
    )
    n_total = len(closed_positions)
    buckets: dict[str, list] = {"yes": [], "no": [], "other": []}
    for pos in closed_positions:
        bucket = _outcome_bucket(pos.get("outcome"))
        buckets[bucket].append(pos)
    out: dict[str, dict] = {}
    for name, rows in buckets.items():
        if not rows:
            if name == "other":
                continue
            out[name] = {
                "n": 0, "win_rate": None, "roi": None, "total_pnl": 0.0,
                "share_of_closed": 0.0,
            }
            continue
        summary = _summarize_bucket(rows)
        summary["share_of_closed"] = len(rows) / n_total
        summary["top_markets_by_pnl"] = _top_markets_by_pnl_legacy(rows, k=5)
        # Per-bucket exit attribution slice. Empty when activity_rows is
        # None — render layer surfaces this as "attribution unavailable".
        if activity_rows is not None:
            summary["by_attribution"] = attribute_subset(
                rows, activity_rows,
                activity_cap_hit=activity_cap_hit,
                subgraph_fallback_used=subgraph_fallback_used,
                walk_full_history=walk_full_history,
            )
        if name == "other":
            summary.update(_other_bucket_detail(rows, leafs_by_cid, nav_lookup))
        out[name] = summary

    # Caveat: OTHER share dominates → edge surface under-represents this wallet.
    other_share = (out.get("other", {}).get("share_of_closed") or 0.0)
    if other_share > _OTHER_BUCKET_CAVEAT_THRESHOLD:
        out["caveat"] = (
            f"OTHER bucket is {other_share*100:.1f}% of closed activity "
            f"(n={out['other']['n']}/{n_total}). The YES/NO outcome surface "
            "under-represents this wallet's behavior; see OTHER-bucket detail."
        )
    # G6 — cross-bucket category_clusters aggregate (audit 2026-05-10).
    # Strategy Pattern Audit's category_specialist_v1 rule consumes this
    # exact shape. Emit only when leafs_by_cid AND nav_lookup are both
    # available AND at least one closed position maps to a nav category.
    #
    # Multi-tag semantics: a position with multiple nav-mapped tags counts
    # in every cluster (cluster_n += 1 per nav, cluster_pnl += pnl per
    # nav). total_pnl is wallet-wide sum(|pnl|) added ONCE per position,
    # so per-cluster shares (cluster.pnl / cluster.total_pnl) sum to >1
    # for heavily multi-tagged wallets. The rule reads only the primary
    # (sort-first) cluster so this doesn't produce double-matches, but
    # consumers iterating all clusters should not assume shares sum to 1.
    #
    # total_pnl == 0 edge case: when every closed position has
    # realizedPnl=0, total_pnl is 0.0. The rule's _category_concentration
    # divides by `max(total_pnl, 1.0)` so this is safe at the canonical
    # consumer. New consumers MUST apply the same guard before dividing.
    if leafs_by_cid is not None and nav_lookup is not None:
        from collections import Counter as _Counter, defaultdict as _defaultdict
        _cluster_n: _Counter = _Counter()
        _cluster_pnl: _defaultdict = _defaultdict(float)
        _total_abs_pnl = 0.0
        for _p in closed_positions:
            _pnl = float(_p.get("realizedPnl") or 0.0)
            _total_abs_pnl += abs(_pnl)
            _cid = _p.get("conditionId") or ""
            _leafs = leafs_by_cid.get(_cid) or []
            _nav_set = {
                nav_lookup[_t] for _t in _leafs if _t in nav_lookup
            }
            for _nav in _nav_set:
                _cluster_n[_nav] += 1
                _cluster_pnl[_nav] += _pnl
        if _cluster_n:
            out["category_clusters"] = sorted(
                [
                    {
                        "category": _cat,
                        "n": int(_cluster_n[_cat]),
                        "pnl": float(_cluster_pnl[_cat]),
                        "total_pnl": float(_total_abs_pnl),
                    }
                    for _cat in _cluster_n
                ],
                key=lambda c: (-c["n"], -abs(c["pnl"]), c["category"]),
            )
    return out


def _top_markets_by_pnl_legacy(rows: Sequence[Mapping], *, k: int = 5) -> list[dict]:
    """Thin wrapper: top markets by absolute realized P&L, used for YES/NO."""
    from sniper.analysis.wallet_protocol.held_analyst import _realized_pnl
    return _top_markets_by(rows, lambda p: _realized_pnl(p), k=k)


def _extract_execution_quality_summary(
    execution_profile: "Mapping | None",
) -> dict | None:
    """Pull display-only execution-quality summary fields from the profile.

    Returns None when the profile is missing, unpopulated, or carries no
    relevant fields. Pure passthrough — no derivations, no verdict logic.
    """
    if not isinstance(execution_profile, Mapping):
        return None
    if execution_profile.get("status") != "populated":
        return None
    summary: dict[str, dict] = {}
    for key in _EXECUTION_QUALITY_FIELDS:
        val = execution_profile.get(key)
        if isinstance(val, Mapping):
            summary[key] = dict(val)
    return summary or None


# Capital-ledger collateral mix detection — task #55 part (d).
# When the per-event resolver attributes events to both USDC.e and pUSD
# (because the wallet spans the 2026-04-28 CLOB V2 cutover), the lifetime
# totals sum two tokens under a single header. The 1:1 USD assumption
# between them is documented as enforced onchain but is auditable, not
# free, so consumers must downweight headline `total_deposits_usdc` /
# `total_withdrawals_usdc` when both tokens are present.
_COLLATERAL_KNOWN_TOKENS = ("USDC.e", "pUSD")


def _compute_capital_ledger_mixed_flag(
    capital_ledger: "CapitalLedger | None",  # forward ref; defined in capital_ledger.py
) -> str | None:
    """Return a CAPITAL_LEDGER_COLLATERAL_MIXED flag string when the
    ledger's collateral_summary attributes events to both USDC.e and
    pUSD (each with non-zero n_events).

    Returns None when:
        * capital_ledger surface is unavailable (Option B fixtures)
        * collateral_summary is empty
        * Only one known token has activity
        * The only attributed bucket is `"unknown"` (resolver gap; not
          a multi-token mix)
    """
    if capital_ledger is None:
        return None
    summary = getattr(capital_ledger, "collateral_summary", None) or {}
    if not isinstance(summary, dict) or not summary:
        return None
    # Collect tokens with at least one event in either deposits, withdrawals,
    # or redemptions. n_events=0 means a degenerate bucket (e.g. from a
    # hand-edited fixture); don't count it as real activity.
    active_known_tokens: list[tuple[str, dict[str, float]]] = []
    for token in _COLLATERAL_KNOWN_TOKENS:
        bucket = summary.get(token)
        if not isinstance(bucket, dict):
            continue
        n_events = float(bucket.get("n_events") or 0.0)
        if n_events > 0:
            active_known_tokens.append((token, bucket))
    if len(active_known_tokens) < 2:
        return None
    # Build a render-friendly summary: per-token (deposits + withdrawals +
    # redemptions) volume so the reviewer can see how the lifetime number
    # splits across the cutover.
    parts: list[str] = []
    for token, bucket in active_known_tokens:
        vol = (
            float(bucket.get("deposits_usdc") or 0.0)
            + float(bucket.get("withdrawals_usdc") or 0.0)
            + float(bucket.get("redemption_payout_usdc") or 0.0)
        )
        parts.append(
            f"{token}={int(bucket.get('n_events') or 0)}ev/${vol:,.2f}"
        )
    # Wording separates two distinct facts to avoid contradicting
    # docs/research/scopes/2026-05-14-pusd-peg-verification.md:
    #   * Per-event collateral attribution is INFERRED (timestamp + address
    #     via ledger/collateral.py cutoff resolver); the underlying
    #     activity-subgraph 0.0.4 has no native collateralToken field.
    #   * pUSD/USDC peg was snapshot-verified at-peg within ±0.15% on
    #     2026-05-14 from two independent sources; the 1:1 USD assumption
    #     used to sum totals is empirically grounded at that moment.
    return (
        "CAPITAL_LEDGER_COLLATERAL_MIXED: " + ", ".join(parts)
        + " (both tokens active; per-event token attribution is inferred "
        + "via ledger/collateral.py cutoff resolver, not from a native "
        + "subgraph field. pUSD/USDC peg snapshot-verified at-peg within "
        + "±0.15% on 2026-05-14; see "
        + "docs/research/scopes/2026-05-14-pusd-peg-verification.md)"
    )


# I1 — DW vs EW ROI divergence detection thresholds.
# Ratio threshold: |DW/EW| above this OR below 1/this OR sign-flip → divergent.
# EW magnitude floor: when |EW| is tiny, the ratio explodes for trivial reasons
# (a 0.5%-vs-2% delta isn't a structural finding). Floor at 1% absolute ROI.
DW_EW_DIVERGENCE_RATIO = 2.0
DW_EW_EW_MAGNITUDE_FLOOR = 0.01


def _dw_ew_divergence_flag(
    dw_point: float | None, ew_point: float | None,
) -> str | None:
    """Return a DW_EW_DIVERGENCE flag string when DW and EW disagree materially.

    Conditions for firing:
        * Both metrics computed (not None).
        * Signs differ (sign flip is structural regardless of magnitude), OR
          |EW| > magnitude floor AND |DW/EW| > 2.0 or < 0.5.

    Magnitude floor (|EW| ≤ 1%) only suppresses the flag when DW and EW
    share a sign — in that regime tiny |EW| makes the ratio meaningless.
    Sign flip is its own signal: dollar-weighted profit + equal-weight
    loss (or vice versa) is exactly the concentration-vs-strike-rate
    asymmetry the flag exists to surface. Verified 2026-05-14: exec-bot
    0x6a72f6 v3 has DW=+4.50% / EW=-0.34% and was incorrectly silent
    under the old magnitude-floor-first logic.
    """
    if dw_point is None or ew_point is None:
        return None
    sign_flip = (dw_point * ew_point) < 0
    # Magnitude floor only applies when signs match. Sign of zero is
    # undefined, so EW == 0 short-circuits to None (no divergence signal
    # we can express as a ratio).
    if ew_point == 0:
        return None
    if not sign_flip and abs(ew_point) <= DW_EW_EW_MAGNITUDE_FLOOR:
        return None
    ratio = dw_point / ew_point
    big_gap = abs(ratio) > DW_EW_DIVERGENCE_RATIO or abs(ratio) < (
        1.0 / DW_EW_DIVERGENCE_RATIO
    )
    if not (sign_flip or big_gap):
        return None
    # Render-friendly ratio string: |DW|/|EW| as the "X-fold" multiplier
    fold = abs(ratio) if abs(ratio) >= 1.0 else (1.0 / abs(ratio)) if ratio != 0 else 0.0
    return (
        f"DW_EW_DIVERGENCE: dw_roi={dw_point*100:.2f}% vs ew_roi={ew_point*100:.2f}% "
        f"(|ratio|={fold:.1f}x{'; sign_flip' if sign_flip else ''})"
    )


def _branch_verdict(branch: BranchReport) -> VerdictLabel:
    """Derive a single branch verdict from its hypotheses + tag grains.

    Spec §11 "supported" = BH-adjusted p < 0.10 for at least one primary test
    AND survived time-split (when applicable). "Refuted" = H1/FH1 CI excludes
    positive performance OR all primary tests refute.

    v3 amendment (held branch only): when full-sample H2 is REFUTED (the
    certainty-buyer signature) and H2_sub080 is present in `hypotheses`,
    H2_sub080 joins the primary consultation set for THIS call only. The
    module-level `HELD_PRIMARY_TESTS` tuple is never mutated. BH pool
    membership (always) and primary-consultation membership (conditional)
    are distinct concepts for H2_sub080.
    """
    base_primary = HELD_PRIMARY_TESTS if branch.branch == "held_analyst" else FLIPPER_PRIMARY_TESTS
    primary = list(base_primary)
    gate_name = "H1" if branch.branch == "held_analyst" else "FH1"

    # Conditional H2_sub080 promotion (held branch + ADR-002 path)
    if branch.branch == "held_analyst":
        h2 = next((h for h in branch.hypotheses if h.name == "H2"), None)
        h2_sub = next((h for h in branch.hypotheses if h.name == "H2_sub080"), None)
        if (
            h2 is not None and h2.verdict == "refuted"
            and h2_sub is not None
            and h2_sub.verdict not in ("underpowered", "n/a")
        ):
            primary.append("H2_sub080")

    gate = next((h for h in branch.hypotheses if h.name == gate_name), None)
    primary_results: list[HypothesisResult] = [
        h for h in branch.hypotheses if h.name in primary
    ]
    # Treat per-grain tag results as part of the primary pool too
    grain_supported = any(g.verdict == "supported" for g in branch.tag_grains)
    grain_refuted = any(g.verdict == "refuted" for g in branch.tag_grains)

    any_supported = (
        any(h.verdict == "supported" for h in primary_results)
        or grain_supported
    )
    all_refuted_or_underpowered = all(
        h.verdict in ("refuted", "underpowered", "n/a")
        for h in primary_results
    ) and not grain_supported

    # Time-split survival: if a primary test is "supported" but its time-split
    # holdout_status is "failed", downgrade to "inconclusive."
    if any_supported:
        for h in primary_results:
            if h.verdict != "supported":
                continue
            ts = branch.time_split.get(h.name)
            if ts is not None and ts.holdout_status == "failed":
                # This primary test failed OOS. Don't promote on its strength alone.
                # If no OTHER primary test is supported, demote to inconclusive.
                others = [
                    o for o in primary_results
                    if o.name != h.name and o.verdict == "supported"
                ]
                if not others and not grain_supported:
                    any_supported = False
                    break

    if any_supported:
        return "supported"
    if gate is not None and gate.verdict == "refuted" and all_refuted_or_underpowered:
        return "refuted"
    if grain_refuted and all_refuted_or_underpowered:
        return "refuted"
    # No supported, no clear refutation
    if all(h.verdict == "underpowered" for h in primary_results):
        return "underpowered"
    return "inconclusive"


# Mixed-mode matrix per §11
_MIXED_MODE_MATRIX: dict[tuple[VerdictLabel, VerdictLabel], tuple[OverallVerdict, str | None, str]] = {
    ("supported", "supported"):     ("edge_detected", "both",     "edge_detected (both modes)"),
    ("supported", "refuted"):       ("operator_review", None,     "conflict — operator_review"),
    ("supported", "inconclusive"):  ("edge_detected", "held",     "edge_detected (held)"),
    ("refuted", "supported"):       ("edge_detected", "flipper",  "edge_detected (flipper)"),
    ("refuted", "refuted"):         ("no_edge", None,             "no_edge (both refuted)"),
    ("refuted", "inconclusive"):    ("no_edge", None,             "no_edge (held refutes)"),
    ("inconclusive", "supported"):  ("edge_detected", "flipper",  "edge_detected (flipper)"),
    ("inconclusive", "refuted"):    ("no_edge", None,             "no_edge (flipper refutes)"),
    ("inconclusive", "inconclusive"): ("underpowered", None,      "underpowered"),
}


def _compute_v3_flags(
    held_branch: BranchReport | None,
) -> list[str]:
    """Emit v3 diagnostic flags from a completed held branch.

    Returns flags appended to ProtocolReport.data_quality_flags:
    - CERTAINTY_BUYER_DETECTED: H2 refuted AND H2_sub080 ran (n >= H2_MIN_N).
      Indicates the wallet's headline WR comes from buying near resolution;
      ADR-002 sub-0.80 sample is the meaningful one.
    - EDGE_CONDITIONAL_ON_TOP3_EXCLUSION: H1 (winsorized) inconclusive AND
      H1.details.sub_top3.ci_lower > 0. The book has edge ONLY after
      excluding the top-3 dominant positions — the rest of the book is
      profitable but the H1 CI is blown out by tail concentration.
    """
    flags: list[str] = []
    if held_branch is None or not held_branch.is_active:
        return flags

    h2 = next((h for h in held_branch.hypotheses if h.name == "H2"), None)
    h2_sub = next((h for h in held_branch.hypotheses if h.name == "H2_sub080"), None)
    if (
        h2 is not None and h2.verdict == "refuted"
        and h2_sub is not None and h2_sub.verdict != "underpowered"
    ):
        flags.append("CERTAINTY_BUYER_DETECTED")

    h1 = next((h for h in held_branch.hypotheses if h.name == "H1"), None)
    if h1 is not None and h1.verdict == "inconclusive":
        sub = (h1.details or {}).get("sub_top3")
        if isinstance(sub, dict):
            ci_lo = sub.get("ci_lower")
            if isinstance(ci_lo, (int, float)) and ci_lo > 0:
                flags.append("EDGE_CONDITIONAL_ON_TOP3_EXCLUSION")
    return flags


def _resolve_mixed_mode(
    held: VerdictLabel, flipper: VerdictLabel,
) -> tuple[OverallVerdict, str | None, str]:
    """Apply the §11 matrix; underpowered/n/a treated as inconclusive."""
    def _norm(v: VerdictLabel) -> VerdictLabel:
        if v in ("underpowered", "n/a", "inference_not_run", "descriptive_only"):
            return "inconclusive"
        return v
    held_n = _norm(held)
    flipper_n = _norm(flipper)
    return _MIXED_MODE_MATRIX.get(
        (held_n, flipper_n),
        ("underpowered", None, "underpowered (default)"),
    )


def _resolve_single_branch(
    branch: VerdictLabel,
) -> tuple[OverallVerdict, str | None, str]:
    if branch == "supported":
        return ("edge_detected", "single_branch", "edge_detected (single branch)")
    if branch == "refuted":
        return ("no_edge", None, "no_edge")
    if branch == "underpowered":
        return ("underpowered", None, "underpowered")
    return ("underpowered", None, "inconclusive (treated as underpowered)")


def _primary_edge_type(branch: BranchReport) -> str | None:
    """Plain-language primary edge type for the report header."""
    primary = HELD_PRIMARY_TESTS if branch.branch == "held_analyst" else FLIPPER_PRIMARY_TESTS
    for h in branch.hypotheses:
        if h.name in primary and h.verdict == "supported":
            if branch.branch == "held_analyst":
                return {
                    "H2": "entry_timing",
                    "H3": "size_conviction",
                    "H5": "tag_specialist",
                }.get(h.name)
            else:
                return {
                    "FH1": "flipper_skill",
                    "FH2": "size_conviction",
                    "FH4": "tag_specialist",
                }.get(h.name)
    if any(g.verdict == "supported" for g in branch.tag_grains):
        return "tag_specialist"
    return None


def _compute_time_coverage(data: "WalletData") -> "dict | None":
    """Compute temporal extent of wallet activity from closed_positions + activity_rows.

    Returns None when closed_positions is empty. All timestamps are unix epoch
    seconds. display-only; NEVER feeds verdict / gate / primary_edge_type.
    Returns None for n_positions_per_month when duration_days == 0 (single-day
    wallet); preferred over 0.0 to distinguish 'no temporal extent' from
    'no positions'.
    """
    if not data.closed_positions:
        return None
    cp_timestamps = [int(p.get("timestamp") or 0) for p in data.closed_positions]
    cp_timestamps = [t for t in cp_timestamps if t > 0]
    if not cp_timestamps:
        return None
    first_ts = min(cp_timestamps)
    last_ts = max(cp_timestamps)
    duration_days = max(0, (last_ts - first_ts) // 86_400)
    n_positions_per_month: float | None
    if duration_days > 0:
        n_positions_per_month = len(cp_timestamps) / (duration_days / 30.0)
    else:
        n_positions_per_month = None
    act_timestamps = [int(r.get("timestamp") or 0) for r in (data.activity_rows or [])]
    act_timestamps = [t for t in act_timestamps if t > 0]
    return {
        "first_position_ts": first_ts,
        "last_position_ts": last_ts,
        "duration_days": int(duration_days),
        "n_positions_per_month": n_positions_per_month,
        "first_fill_ts": min(act_timestamps) if act_timestamps else None,
        "last_fill_ts": max(act_timestamps) if act_timestamps else None,
        "fetch_termination": data.fetch_termination or {},
    }


def analyze_wallet(
    wallet: str = "",
    *,
    session: requests.Session | None = None,
    archetype_extractor: Any = None,
    peer_baseline_fn: PeerBaselineFn | None = None,
    flip_roi_baseline_fn: FlipRoiBaselineFn | None = None,
    price_history_fn: PriceHistoryFn | None = None,
    leafs_by_cid: Mapping[str, Sequence[str]] | None = None,
    nav_lookup: Mapping[str, str] | None = None,
    n_iter: int = 10_000,
    seed: int = 42,
    pre_fetched_data: WalletData | None = None,
    walk_full_history: bool = False,
    tag_lookup_state: str | None = None,
    triage_context: dict | None = None,
) -> ProtocolReport:
    """Top-level entrypoint. Returns a fully-populated ProtocolReport.

    Args:
        wallet: 0x-prefixed address (lowercase preferred). When omitted and
            pre_fetched_data is supplied, derived from pre_fetched_data.wallet.
        session: optional requests.Session for HTTP fetches (re-use across
            wallets for connection pooling).
        archetype_extractor: optional callable used by fetch_wallet_data to
            populate `archetype_layer1` and `execution_profile`.
        peer_baseline_fn: H5 / FH4 per-grain baseline lookup.
        price_history_fn: H6 / FH6 prices-history lookup.
        leafs_by_cid / nav_lookup: tag-grain lookups.
        n_iter / seed: pass-through to bootstrap / permutation tests.
        pre_fetched_data: skip Stage 0 fetch (used by tests + replay).
    """
    # Stage 0: data fetch
    if pre_fetched_data is not None:
        data = pre_fetched_data
        # When wallet is not passed explicitly, derive it from pre_fetched_data.
        if not wallet:
            wallet = data.wallet
    else:
        sess = session or requests.Session()
        data = fetch_wallet_data(
            wallet, sess, archetype_extractor,
            walk_full_history=walk_full_history,
        )

    # PnL composition (Phase A split #2).
    # Codex 2026-05-27 audit: Polymarket /closed-positions returns size=0
    # (no current holdings) and totalBought as the gross share count over
    # the position's lifetime. The dossier builder verified empirically
    # that (curPrice - avgPrice) * totalBought ≈ realizedPnl. Using
    # `size` here would zero out realized_roi for normal closed-only
    # wallets. /positions (open) returns the real share count in `size`
    # so the open-side math is unchanged.
    _realized_pnl_val = sum(
        float(p.get("realizedPnl") or 0.0) for p in data.closed_positions
    )
    _unrealized_pnl_val = sum(
        (float(p.get("curPrice") or 0.0) - float(p.get("avgPrice") or 0.0))
        * float(p.get("size") or 0.0)
        for p in data.open_positions
    )
    _combined_pnl_val = _realized_pnl_val + _unrealized_pnl_val

    _closed_cost_basis = sum(
        float(p.get("avgPrice") or 0.0) * float(p.get("totalBought") or 0.0)
        for p in data.closed_positions
    )
    _open_cost_basis = sum(
        float(p.get("avgPrice") or 0.0) * float(p.get("size") or 0.0)
        for p in data.open_positions
    )
    _total_cost_basis = _closed_cost_basis + _open_cost_basis

    _realized_roi = _realized_pnl_val / _closed_cost_basis if _closed_cost_basis > 0 else None
    _unrealized_roi = _unrealized_pnl_val / _open_cost_basis if _open_cost_basis > 0 else None
    _combined_roi = _combined_pnl_val / _total_cost_basis if _total_cost_basis > 0 else None

    _now_ts = int(time.time())
    _open_breakdown = _compute_open_position_breakdown(data.open_positions, _now_ts)

    report = ProtocolReport(
        wallet=wallet,
        analyzed_at=_now_ts,
        n_closed_positions=len(data.closed_positions),
        n_held_to_resolution=len(filter_held_to_resolution(data)),
        n_open_positions=len(data.open_positions),
        net_pnl=_realized_pnl_val,
        realized_pnl=_realized_pnl_val,
        unrealized_pnl=_unrealized_pnl_val,
        combined_pnl=_combined_pnl_val,
        realized_roi=_realized_roi,
        unrealized_roi=_unrealized_roi,
        combined_roi=_combined_roi,
        open_position_breakdown=_open_breakdown,
        activity_rows_fetched=len(data.activity_rows),
        activity_cap_hit=data.activity_cap_hit,
        subgraph_fallback_used=data.subgraph_fallback_used,
        archetype_layer1=data.archetype_layer1,
        archetype_layer2=data.archetype_layer2,
        walk_full_history=data.walk_full_history,
        n_unmapped_fills=data.n_unmapped_fills,
    )

    # Stamp the supersession key from raw WalletData. The
    # flipper-grain writer reads this when no fixture_path is given.
    # Wrapped: a degenerate WalletData (no rows at all) is rare in
    # production but possible for synthetic test data; in that case
    # leave the field None and let the writer skip-or-raise on its own.
    try:
        report.wallet_fixture_hash = compute_wallet_fixture_hash_from_data(data)
    except ValueError:
        report.wallet_fixture_hash = None

    # G3 (2026-05-17) — operator-supplied triage context. Verbatim
    # passthrough; analyzer never reads or interprets it. Deep-copy so
    # caller mutations don't leak into the stamped report. Done here
    # (right after construction) so every return path — including the
    # data_insufficient early return — carries the stamp.
    if triage_context is not None:
        import copy as _copy
        report.triage_context = _copy.deepcopy(triage_context)

    report.execution_quality_summary = _extract_execution_quality_summary(
        data.execution_profile,
    )
    # Edge Surface v1: descriptive surfaces. Display-only, never feed verdict.
    # Pull tag lookups from the analyze_wallet kwargs OR from `data` (replay
    # surfaces); kwargs win when both are present (live runs typically pass
    # them in directly while record_fetch_snapshot stamps them on data).
    _leafs = leafs_by_cid if leafs_by_cid is not None else data.leafs_by_cid
    _nav = nav_lookup if nav_lookup is not None else data.nav_lookup
    report.outcome_side_breakdown = _compute_outcome_side_breakdown(
        data.closed_positions,
        leafs_by_cid=_leafs,
        nav_lookup=_nav,
        activity_rows=data.activity_rows,
        activity_cap_hit=data.activity_cap_hit,
        subgraph_fallback_used=data.subgraph_fallback_used,
        walk_full_history=data.walk_full_history,
    )
    # Exit Attribution v1: top-level summary across ALL closed positions.
    # Display-only. Per-bucket slices are inside outcome_side_breakdown.
    from sniper.analysis.wallet_protocol.exit_attribution import (
        compute_attribution_summary,
    )
    report.exit_attribution = compute_attribution_summary(
        data.closed_positions,
        data.activity_rows,
        activity_cap_hit=data.activity_cap_hit,
        subgraph_fallback_used=data.subgraph_fallback_used,
        walk_full_history=data.walk_full_history,
    )
    # Timing Surface v1 — captured-travel-only timing classification.
    # Display-only. Re-uses exit_attribution categorization so timing
    # cannot drift. NEVER feeds verdict / gate / primary_edge_type.
    # See docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
    # (commit d598cf2, r2).
    from sniper.analysis.wallet_protocol.timing import compute_timing_surface

    ea = report.exit_attribution or {}
    ea_summary = ea.get("summary") or {}
    ea_coverage = ea.get("coverage") or {}
    # R10 Option A: Timing Surface's `attribution_coverage_low` kwarg uses the
    # sample-size-floor definition (n_attributed = n_total - insufficient_data).
    # This preserves Timing Surface r2 §5 design intent: a wallet dominated
    # by unresolved_or_unknown lands in `unclear`, not `underpowered`. The
    # stricter "reliable attribution coverage" signal (n_with_resolved /
    # n_total) is computed downstream in the Completeness Surface caveat
    # only — see report.py:_data_completeness_block. W2 will consume
    # `coverage["n_with_resolved_attribution"]` directly, not this kwarg.
    _n_total = int(ea_coverage.get("n_total", 0) or 0)
    _n_insufficient = int((ea_summary.get("insufficient_data") or {}).get("n", 0) or 0)
    _n_attributed_for_coverage = max(_n_total - _n_insufficient, 0)
    _attribution_coverage_low = (
        _n_total > 0 and (_n_attributed_for_coverage / _n_total) < 0.50
    )
    report.timing_surface = compute_timing_surface(
        data.closed_positions,
        data.activity_rows,
        activity_cap_hit=data.activity_cap_hit,
        subgraph_fallback_used=data.subgraph_fallback_used,
        walk_full_history=data.walk_full_history,
        n_unmapped_fills=data.n_unmapped_fills,
        attribution_coverage_low=_attribution_coverage_low,
    )
    report.extractor_features_summary = (
        dict(data.extractor_features_summary)
        if isinstance(data.extractor_features_summary, dict)
        else None
    )

    # Completeness Surface v1 — typed JSON exposure of the same numbers
    # rendered in the `## Data completeness` Markdown block. Display-only;
    # NEVER feeds verdict / gate / primary_edge_type / copyability_note.
    # Sourced from already-computed exit_attribution coverage + capture-state
    # flags so it cannot drift from the rendered block.
    _n_with_resolved = int(ea_coverage.get("n_with_resolved_attribution", 0) or 0)
    _history_full = bool(ea_coverage.get("history_assumed_full", True))
    _activity_truncated = (
        bool(report.activity_cap_hit)
        and not bool(report.walk_full_history)
        and not bool(report.subgraph_fallback_used)
    )
    _subgraph_unmapped = int(report.n_unmapped_fills or 0) > 0
    _coverage_low = (
        _n_total > 0 and (_n_with_resolved / _n_total) < 0.50
    )
    _limitation_present = _n_total > 0
    # G2 — closed-position fetch cap signal (audit 2026-05-10).
    # fetch_termination["closed_positions"] ∈ {"empty_page","partial_page","api_refused"}.
    # api_refused fires on any abnormal fetch termination — 4xx (real cap),
    # 5xx (transient service degradation), or network error (timeout/connect).
    # In all three cases the closed-position history is incomplete, so the
    # boolean correctly reflects "data may be truncated" rather than the
    # narrower "we hit a hard offset cap". The conservative default (absence
    # → False) is preserved: missing fetch_termination is read as no cap.
    _fetch_term = getattr(data, "fetch_termination", None) or {}
    _closed_cap_hit = _fetch_term.get("closed_positions") == "api_refused"

    # G4 — tag-lookup availability surface (audit 2026-05-10).
    # Caller may pass explicit state; otherwise infer from leafs_by_cid shape.
    # Conservative default: when no explicit state and leafs_by_cid is None,
    # treat as "unavailable" rather than silently None — absence of signal
    # is read as "we couldn't look up tags for this wallet".
    if tag_lookup_state is not None:
        if tag_lookup_state not in VALID_TAG_LOOKUP_STATES:
            raise ValueError(
                "tag_lookup_state must be one of "
                f"{sorted(VALID_TAG_LOOKUP_STATES)}, got {tag_lookup_state!r}"
            )
        _tag_lookup_state = tag_lookup_state
    elif leafs_by_cid is None:
        _tag_lookup_state = "unavailable"
    elif len(leafs_by_cid) == 0:
        _tag_lookup_state = "empty"
    else:
        _tag_lookup_state = "ok"
    report.data_completeness = {
        "activity_capture": {
            "activity_rows_fetched": int(report.activity_rows_fetched or 0),
            "activity_cap_hit": bool(report.activity_cap_hit),
            "walk_full_history": bool(report.walk_full_history),
            "subgraph_fallback_used": bool(report.subgraph_fallback_used),
            "n_unmapped_fills": int(report.n_unmapped_fills or 0),
        },
        "attribution_coverage": {
            "n_total": _n_total,
            "n_outside_window": int(ea_coverage.get("n_outside_window", 0) or 0),
            "n_insufficient_data": _n_insufficient,
            "n_attributed": _n_attributed_for_coverage,
            "n_with_resolved_attribution": _n_with_resolved,
            "history_assumed_full": _history_full,
        },
        "closed_positions_capture": {
            "closed_positions_fetched": int(report.n_closed_positions or 0),
            "closed_positions_cap_hit": bool(_closed_cap_hit),
        },
        "tag_lookup_state": _tag_lookup_state,
        "caveats": {
            "activity_truncated": _activity_truncated,
            "subgraph_unmapped_fills": _subgraph_unmapped,
            "attribution_coverage_low": _coverage_low,
            "limitation_present": _limitation_present,
            "closed_positions_truncated": bool(_closed_cap_hit),
        },
    }
    # G4 — append TAG_LOOKUP_UNAVAILABLE flag (idempotently) when the
    # lookup couldn't run. Other states (ok/empty/skipped) do NOT append a
    # flag — only the "unavailable" state is load-bearing for downstream
    # consumers that already inspect data_quality_flags.
    if (
        _tag_lookup_state == "unavailable"
        and "TAG_LOOKUP_UNAVAILABLE" not in report.data_quality_flags
    ):
        report.data_quality_flags.append("TAG_LOOKUP_UNAVAILABLE")

    # Position Building Surface v1 (W2 Phase 3) — pure-compute reconstruction
    # of how the wallet accumulated, reduced, and re-accumulated positions
    # from normalized fill rows. Display-only; NEVER feeds verdict / gate /
    # primary_edge_type / copyability_note. No I/O, no simulator, no trigger
    # logic, no copy fields, no price-history calls.
    # See docs/superpowers/specs/2026-05-10-wallet-position-builder-design.md.
    # `compute_position_building_surface` returns None when neither
    # activity_rows nor closed_positions has any rows; we leave the field
    # at None in that case (preserving the documented default).
    from sniper.analysis.wallet_protocol.position_building import (
        compute_position_building_surface,
    )
    report.position_building_surface = compute_position_building_surface(
        data.activity_rows,
        data.closed_positions,
        wallet=wallet,
        activity_cap_hit=data.activity_cap_hit,
        subgraph_fallback_used=data.subgraph_fallback_used,
        walk_full_history=data.walk_full_history,
    )

    # Liquidity Surface v1 — proxy-only execution-friction context.
    # Display-only; NEVER feeds verdict / gate / primary_edge_type /
    # copyability_note and never appends top-level data_quality_flags.
    from sniper.analysis.wallet_protocol.liquidity import compute_liquidity_surface

    report.liquidity_surface = compute_liquidity_surface(
        data.closed_positions,
        data.activity_rows,
        data.price_history_lookups,
    )

    # Capital & Risk surface — peak concurrent deployment (capital to play),
    # max drawdown (capital to survive), recycle factor, return-on-base, PnL
    # concentration. Display-only; NEVER feeds verdict / gate /
    # primary_edge_type / copyability_note and never appends top-level
    # data_quality_flags. The display-only guarantee is the hard wall — no
    # verdict/gate code reads `capital_risk_surface` (grep-verified + the
    # mutation-proof scope test) — NOT the call's position in this function.
    from sniper.analysis.wallet_protocol.capital_risk import (
        compute_capital_risk_surface,
    )

    report.capital_risk_surface = compute_capital_risk_surface(
        data.closed_positions,
        data.activity_rows,
        data.capital_ledger,
        report.net_pnl,
        n_unmapped_fills=getattr(data, "n_unmapped_fills", 0) or 0,
    )

    # Sizing & Outcome surface — does entry sizing associate with outcome
    # quality? Display-only; NEVER feeds verdict / gate / primary_edge_type /
    # copyability_note and never appends top-level data_quality_flags. The
    # display-only guarantee is the hard wall — no verdict/gate code reads
    # `sizing_outcome_surface` (grep-verified + the mutation-proof scope
    # test) — NOT the call's position in this function.
    from sniper.analysis.wallet_protocol.sizing_outcome import (
        compute_sizing_outcome_surface,
    )

    report.sizing_outcome_surface = compute_sizing_outcome_surface(
        data.closed_positions,
        data.activity_rows,
        _leafs,
        _nav,
    )

    # Null-Strategy Baseline surface — did the wallet's side selection beat
    # the chalk? Display-only; NEVER feeds verdict / gate / primary_edge_type
    # / copyability_note and never appends top-level data_quality_flags. The
    # display-only guarantee is the hard wall — no verdict/gate code reads
    # `null_baseline_surface` (grep-verified + the mutation-proof scope
    # test) — NOT the call's position in this function.
    from sniper.analysis.wallet_protocol.null_baseline import (
        compute_null_baseline_surface,
    )

    report.null_baseline_surface = compute_null_baseline_surface(
        data.closed_positions,
    )

    # Survivorship surface — did discovery itself bias the sample? Display-
    # only; NEVER feeds verdict / gate / primary_edge_type / copyability_note
    # and never appends top-level data_quality_flags. The display-only
    # guarantee is the hard wall — no verdict/gate code reads
    # `survivorship_surface` (grep-verified + the mutation-proof scope test).
    from sniper.analysis.wallet_protocol.survivorship import (
        compute_survivorship_surface,
    )

    report.survivorship_surface = compute_survivorship_surface(triage_context)

    # Copy-Latency Decay surface — how much entry edge survives to a copier
    # entering minutes late. Display-only; NEVER feeds verdict / gate /
    # primary_edge_type / copyability_note and never appends top-level
    # data_quality_flags. The display-only guarantee is the hard wall — no
    # verdict/gate code reads `copy_latency_surface` (grep-verified + the
    # mutation-proof scope test). Dormant until a grid-enabled capture runs.
    from sniper.analysis.wallet_protocol.copy_latency import (
        compute_copy_latency_surface,
    )

    report.copy_latency_surface = compute_copy_latency_surface(
        data.closed_positions,
        data.activity_rows,
        data.copy_latency_prices,
    )

    # Position Reconciliation surface — reconciles /closed-positions against
    # CTF Redemption events to detect API undercounting. Display-only; NEVER
    # feeds overall_verdict, gate_result, primary_edge_type, copyability_note,
    # or any other surface's numbers. `downstream_lower_bound` is informational.
    # The display-only guarantee is the hard wall — no verdict/gate code reads
    # `position_reconciliation_surface` (grep-verified + mutation-proof scope test).
    from sniper.analysis.wallet_protocol.position_reconciliation import (
        compute_position_reconciliation_surface,
    )

    report.position_reconciliation_surface = compute_position_reconciliation_surface(
        data.closed_positions,
        data.activity_rows,
        data.redemptions,
        data.splits,
        data.merges,
        data.activity_cap_hit,
        data.n_unmapped_fills,
    )

    # Time Coverage v1 (Phase A split #3) — temporal extent of wallet activity.
    # Display-only; NEVER feeds verdict / gate / primary_edge_type /
    # copyability_note. None when no closed positions are present.
    report.time_coverage = _compute_time_coverage(data)

    # I1 — Capital efficiency: dollar-weighted vs equal-weighted ROI.
    # Computed regardless of branch routing or gate result so flipper-only
    # wallets and Stage-1-rejected wallets still surface the structural
    # finding (the metric is universe-agnostic). When no closed position has
    # a valid cost basis, fields stay None and report.py omits the section.
    dw_ew = compute_dw_ew_roi(
        data.closed_positions, n_iter=n_iter, seed=seed,
    )
    if dw_ew is not None:
        dw_point, dw_lo, dw_hi, ew_point, _n = dw_ew
        report.dw_roi_point = dw_point
        report.dw_roi_ci_lower = dw_lo
        report.dw_roi_ci_upper = dw_hi
        report.ew_roi_point = ew_point
        flag = _dw_ew_divergence_flag(dw_point, ew_point)
        if flag is not None:
            report.data_quality_flags.append(flag)

    # C1 — Capital ledger passthrough.
    if data.capital_ledger is not None:
        report.capital_ledger = data.capital_ledger

    # Task #55(d): emit CAPITAL_LEDGER_COLLATERAL_MIXED when both USDC.e
    # and pUSD have non-zero activity in the inferred per-event resolver.
    # Independent of the accounting tie below (that one is lifecycle parity;
    # this one is currency-mix transparency).
    mixed_flag = _compute_capital_ledger_mixed_flag(data.capital_ledger)
    if mixed_flag is not None:
        report.data_quality_flags.append(mixed_flag)

    # Follow-up 4: ±2% accounting tie.
    tie_flags = _compute_accounting_tie(
        data.capital_ledger,
        report.net_pnl,
        data.open_positions,
        data.portfolio_value_usdc,
        portfolio_value_observed_at=data.fetched_at,
    )
    report.data_quality_flags.extend(tie_flags)

    # Merge fetch-layer quality flags (subgraph fallback, unmapped tokens, etc.)
    report.data_quality_flags.extend(data.fetch_quality_flags)

    # Stage 1: gates + caution flags
    gate_eval = evaluate_stage1_gates(data)
    report.gate_result = gate_eval.result
    report.gate_reason = gate_eval.reason
    report.n_open_dead = gate_eval.n_open_dead
    report.data_quality_flags.extend(gate_eval.caution_flags)

    if gate_eval.result != "pass":
        report.overall_verdict = "data_insufficient"
        return report

    # Stage 2a: flip reconstruction + mode routing
    flip_recon = reconstruct_flips(data)
    routing = classify_mode(flip_recon)
    report.flip_rate = flip_recon.flip_rate
    report.trading_mode = routing.mode

    if routing.flipper_truncated:
        report.data_quality_flags.append(
            f"FLIPPER_ANALYSIS_TRUNCATED: flip_rate={flip_recon.flip_rate:.2f}, activity_cap_hit"
        )

    # Stage 2b: branches
    held_branch: BranchReport | None = None
    flipper_branch: BranchReport | None = None

    if routing.run_held_branch:
        held_branch = run_held_branch(
            data,
            peer_baseline_fn=peer_baseline_fn,
            price_history_fn=price_history_fn,
            leafs_by_cid=leafs_by_cid,
            nav_lookup=nav_lookup,
            n_iter=n_iter,
            seed=seed,
            f2_top3_indices=gate_eval.f2_top3_indices,
        )
        report.held_analyst_branch = held_branch

    if routing.run_flipper_branch:
        flipper_branch = run_flipper_branch(
            flip_recon.flips,
            wallet=data.wallet,
            execution_profile=data.execution_profile,
            peer_baseline_fn=peer_baseline_fn,
            flip_roi_baseline_fn=flip_roi_baseline_fn,
            price_history_fn=price_history_fn,
            leafs_by_cid=leafs_by_cid,
            nav_lookup=nav_lookup,
            n_iter=n_iter,
            seed=seed,
            materialized_meta=data.replay_surface_meta,
        )
        report.flipper_branch = flipper_branch
    else:
        report.flipper_branch = BranchReport(
            branch="flipper",
            is_active=False,
            skip_reason=routing.rationale,
        )

    if not routing.run_held_branch:
        report.held_analyst_branch = BranchReport(
            branch="held_analyst",
            is_active=False,
            skip_reason=routing.rationale,
        )

    # Stage 3: overall verdict + primary edge type
    if routing.mode == "mixed_mode":
        held_v = _branch_verdict(held_branch) if held_branch else "underpowered"
        flipper_v = _branch_verdict(flipper_branch) if flipper_branch else "underpowered"
        verdict, primary, note = _resolve_mixed_mode(held_v, flipper_v)
        report.overall_verdict = verdict
        report.primary_edge_type = primary
        report.copyability_note = note
        # Pick edge-type from whichever branch supported (prefer held when both)
        if primary == "held" and held_branch:
            report.primary_edge_type = _primary_edge_type(held_branch)
        elif primary == "flipper" and flipper_branch:
            report.primary_edge_type = _primary_edge_type(flipper_branch)
        elif primary == "both" and held_branch:
            report.primary_edge_type = (
                _primary_edge_type(held_branch)
                or (_primary_edge_type(flipper_branch) if flipper_branch else None)
            )
    else:
        active = held_branch if routing.run_held_branch else flipper_branch
        if active is None:
            report.overall_verdict = "underpowered"
        else:
            v = _branch_verdict(active)
            verdict, _, note = _resolve_single_branch(v)
            report.overall_verdict = verdict
            report.copyability_note = note
            report.primary_edge_type = _primary_edge_type(active)

    # Stage 3.5: report-level honesty floor (Issue #24 — Pestle).
    # Runs AFTER verdict assembly so it can see the final overall_verdict.
    # Display-only: never feeds gate/primary_edge_type; only mutates verdict +
    # copyability_note + data_quality_flags when the doctrine violation is
    # detected.
    _apply_negative_pnl_honesty_floor(report)

    # Stage 3.6: branch-level tag-grain rescue honesty caveat.
    # Runs after the negative-PnL floor (orthogonal observation). Emits
    # BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY flag + applicable_limitations entry
    # when a branch was promoted to "supported" solely on tag-grain evidence
    # with no primary hypothesis surviving OOS holdout. Caveat-only: never
    # mutates overall_verdict, gate_result, primary_edge_type, or copyability_note.
    _apply_tag_grain_rescue_caveat(report)

    # v3 diagnostic flags (CERTAINTY_BUYER_DETECTED, EDGE_CONDITIONAL_ON_TOP3_EXCLUSION)
    report.data_quality_flags.extend(_compute_v3_flags(held_branch))

    # Mandatory limitations to surface on every report (§13 spec items 1, 14, 17, 18)
    limitations = [
        "B1 population baseline biased by speculator-bot-heavy discovery pipeline",
        "Mode-router thresholds (0.30/0.70) are judgment calls, not empirical",
    ]
    if not report.subgraph_fallback_used and not walk_full_history:
        limitations.append(
            "Activity API capped at 3000 rows; high-volume flippers may have truncated history"
        )
    if leafs_by_cid is None or nav_lookup is None:
        limitations.append(
            "Cross-event news-cycle correlations not captured without tag-nav lookup"
        )
    report.applicable_limitations.extend(limitations)

    return report


__all__ = [
    "analyze_wallet",
    "HELD_PRIMARY_TESTS",
    "FLIPPER_PRIMARY_TESTS",
    "NEGATIVE_PNL_EDGE_UNEXPLAINED",
    "EDGE_DEPENDS_ON_OPEN_POSITIONS",
    "BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY",
    "attribution_coverage_nontrivial",
    "timing_coverage_nontrivial",
    "position_coverage_nontrivial",
    "_branch_promoted_on_tag_grain_only",
    "_apply_tag_grain_rescue_caveat",
    "_apply_negative_pnl_honesty_floor",
    "_compute_open_position_breakdown",
]
