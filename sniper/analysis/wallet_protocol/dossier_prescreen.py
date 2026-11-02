"""Cheap pre-screen — decide whether a wallet is worth a FULL dossier.

The full wallet-protocol dossier is expensive (live fetch + flipper permutation
at n_iter=2000 + capital ledger + peer baselines). Running it on wallets that
are obvious net losers wastes compute. This module is the cheap gate that runs
BEFORE any fetch, using data we already have locally in `user_analysis`.

Tier 1 (this module): zero-API, pure-function screen on the local snapshot —
Gate 0 lifetime-PnL floor (ADR-006), net-PnL positivity, market-maker exclusion
(ADR-006: market_maker scores 0), and the existing wallet_quality eligibility
gate (ADR-007). Skips clear losers; conservative (does not over-filter a wallet
with unknown lifetime PnL but positive computed PnL).

Tier 2 (NOT here — operator-gated, needs live API / score_history time-series):
the "steadily losing over the last month" recency-trend check. The local
snapshot has no per-wallet PnL time-series, so a 30-day trend needs either a
fresh Polymarket profile pull or the score_history feed. Specced as a follow-up.

Doctrine: read-only, decision-support. Does NOT mutate verdicts or write DB.

SCOPE EXCEPTION — reading legacy `user_analysis` (explicitly scoped per
AGENTS.md §"Legacy / Historical Areas", which permits the frozen tables
"unless explicitly scoped otherwise"). This module reads `user_analysis`
because it is the only CHEAP, zero-API local source of lifetime PnL; the whole
point of the pre-screen is to skip clear losers BEFORE the expensive live
fetch, so pulling a live Polymarket profile here would defeat the design.
Why this is safe despite the freeze:
  • Read-only triage. Never mutates verdict / gate / primary_edge_type /
    copyability_note, and never writes the DB.
  • It only ORDERS / drops compute candidates — the full dossier re-fetches
    LIVE data, so a stale snapshot at most mis-orders or wastes one dossier;
    it can never produce a wrong verdict.
  • It leans on `lb_pnl` (Polymarket's own profile PnL — external truth, not a
    discredited internal score) and `wallet_quality_score` (the rebuilt
    P&L-first ADR-007 formula), not the inverted legacy `insider_score`.
STALENESS CAVEAT — the legacy analyzer that populates `user_analysis` is
frozen, so these snapshots do not refresh. A wallet that turned profitable (or
busted) AFTER its last snapshot will be mis-screened. Treat the partition as a
compute-ordering hint, not ground truth. Re-screen against a current source if
the snapshot age matters for the decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# Gate 0 default: Polymarket lifetime PnL floor (USDC). A wallet at or below the
# floor with KNOWN lifetime PnL is a net loser — skip. Default 0 = "must be
# lifetime-profitable". ADR-006 added Gate 0 separately; this mirrors its intent.
DEFAULT_LB_PNL_FLOOR = 0.0

# Classifications that are structurally not worth a directional-copy dossier.
# ADR-006: market_maker returns followability 0.0 (two-sided, rebate-driven).
_EXCLUDED_CLASSES = frozenset({"market_maker"})


@dataclass(frozen=True)
class PrescreenResult:
    verdict: str          # "analyze" | "skip"
    reason: str           # human-readable justification
    rank_score: float     # within-tier value (raw): wq_score | lb_pnl | pnl
    rank_tier: int = 0     # signal-quality tier: 3=wq_score, 2=lb_pnl, 1=pnl, 0=skip
    # Ordering sorts (rank_tier, rank_score) desc so a 0-100 wallet_quality_score
    # is NEVER compared directly against raw-dollar lb_pnl/pnl across wallets.


def screen_wallet_row(
    row: Mapping[str, Any],
    *,
    lb_pnl_floor: float = DEFAULT_LB_PNL_FLOOR,
) -> PrescreenResult:
    """Decide analyze vs skip for one `user_analysis` row. Pure, no I/O.

    Conservative skip rules (only skip clear losers):
      1. classification in _EXCLUDED_CLASSES (market_maker) → skip
      2. KNOWN lifetime PnL (lb_pnl not None) at/below floor → skip (Gate 0)
      3. lb_pnl unknown AND computed pnl < 0 → skip (no lifetime data + we
         measured a loss; a flat $0 is not a clear loser → analyze)
      4. wallet_quality_eligible explicitly 0 with a hard fail reason → skip
      Otherwise → analyze.

    Ordering of the analyze set is TIERED by signal quality, never by raw
    cross-unit value: wallets with a wallet_quality_score (0-100) rank above
    lb_pnl-only wallets, which rank above computed-pnl-only wallets. Within a
    tier, higher rank_score first. This prevents a 0-100 quality score from
    being compared directly against raw-dollar lb_pnl (a wq_score=90 wallet
    must not sort below an unscored lb_pnl=$10k wallet). Losers never rank.
    """
    classification = (row.get("classification") or "").strip().lower()
    lb_pnl = row.get("lb_pnl")
    pnl = _as_float(row.get("pnl"))
    wq_eligible = row.get("wallet_quality_eligible")
    wq_score = row.get("wallet_quality_score")
    wq_fail = (row.get("wallet_quality_fail_reason") or "").strip()

    # Rule 1 — structural class exclusion
    if classification in _EXCLUDED_CLASSES:
        return PrescreenResult("skip", f"excluded_class:{classification}", rank_score=float("-inf"))

    # Rule 2 — Gate 0: known lifetime PnL at/below floor
    if lb_pnl is not None:
        lb = _as_float(lb_pnl)
        if lb <= lb_pnl_floor:
            return PrescreenResult(
                "skip", f"gate0_lb_pnl<=floor:lb_pnl={lb:.0f}<=({lb_pnl_floor:.0f})",
                rank_score=float("-inf"),
            )

    # Rule 3 — no lifetime data AND computed PnL is a strict loss. A flat $0
    # (genuinely flat or never-measured) is not a "clear loser" → analyze.
    if lb_pnl is None and pnl < 0:
        return PrescreenResult(
            "skip", f"no_lb_pnl_and_computed_pnl<0:pnl={pnl:.0f}",
            rank_score=float("-inf"),
        )

    # Rule 4 — explicit wallet_quality hard-fail
    if wq_eligible == 0 and wq_fail:
        return PrescreenResult(
            "skip", f"wallet_quality_ineligible:{wq_fail}", rank_score=float("-inf"),
        )

    # ANALYZE — rank by best available quality signal, tiered so units never mix
    if wq_score is not None:
        rank, tier, basis = _as_float(wq_score), 3, "wallet_quality_score"
    elif lb_pnl is not None:
        rank, tier, basis = _as_float(lb_pnl), 2, "lb_pnl"
    else:
        rank, tier, basis = pnl, 1, "computed_pnl"
    return PrescreenResult(
        "analyze", f"passed:rank_by={basis}", rank_score=rank, rank_tier=tier,
    )


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def partition_wallets(
    rows: list[Mapping[str, Any]],
    *,
    lb_pnl_floor: float = DEFAULT_LB_PNL_FLOOR,
) -> dict[str, list[dict]]:
    """Split rows into analyze (rank-sorted, best first) and skip lists.

    Returns {"analyze": [...], "skip": [...]} where each entry is the row dict
    augmented with `_prescreen_verdict`, `_prescreen_reason`, `_prescreen_rank`.
    """
    analyze: list[dict] = []
    skip: list[dict] = []
    for row in rows:
        res = screen_wallet_row(row, lb_pnl_floor=lb_pnl_floor)
        enriched = dict(row)
        enriched["_prescreen_verdict"] = res.verdict
        enriched["_prescreen_reason"] = res.reason
        enriched["_prescreen_rank"] = res.rank_score
        enriched["_prescreen_tier"] = res.rank_tier
        (analyze if res.verdict == "analyze" else skip).append(enriched)
    # Tier first (signal-quality class), then within-tier value — never compare
    # a 0-100 wallet_quality_score against raw-dollar lb_pnl across wallets.
    analyze.sort(key=lambda r: (r["_prescreen_tier"], r["_prescreen_rank"]), reverse=True)
    return {"analyze": analyze, "skip": skip}
