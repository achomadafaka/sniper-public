"""Stage 1 — Cheap-reject gates per protocol v2 §3.

Run AFTER data fetch (§4) and BEFORE any statistical analysis. The first
failing gate is the REJECT reason; subsequent gates are not evaluated.

Gates are "cheap" in the sense that they skip bootstrap/BH-FDR/CUSUM, NOT
that they skip API calls. All four data fetches must complete first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sniper.analysis.wallet_protocol.types import GateResult, WalletData


# Locked thresholds (protocol §3.1, §3.2). Mirror v2 spec text exactly.
N_CLOSED_POSITIONS_MIN = 30
DORMANT_DAYS_THRESHOLD = 90
N_OPEN_DEAD_F1_MIN = 50
F1_OPEN_DEAD_RATIO_THRESHOLD = 0.50
F2_TOP3_NET_PNL_FRACTION = 0.70
F3_RECENT_90D_FRACTION = 0.70

_HEDGE_LAYER1_LABELS = {
    "paired_outcome_arb",
    "two_sided_inventory_trader",
}


@dataclass
class GateEvaluation:
    """Output of evaluate_stage1_gates(). Surfaced into ProtocolReport.

    v3 amendment B: when F2 fires, `f2_top3_indices` holds indices into
    `data.closed_positions` for the three positions by absolute realized
    P&L, and `f2_top3_share` is the concentration (>0.7 when fired).
    Both None when F2 doesn't fire. The existing F2 string in
    `caution_flags` is preserved for backcompat.
    """

    result: GateResult
    reason: str | None
    caution_flags: list[str]
    n_open_dead: int = 0
    f2_top3_indices: list[int] | None = None
    f2_top3_share: float | None = None


def _last_activity_ts(data: WalletData) -> int:
    """Most recent timestamp across closed + activity data. 0 if none."""
    candidates: list[int] = []
    for row in data.closed_positions:
        ts = int(row.get("timestamp") or 0)
        if ts:
            candidates.append(ts)
    for row in data.activity_rows:
        ts = int(row.get("timestamp") or 0)
        if ts:
            candidates.append(ts)
    return max(candidates) if candidates else 0


def _count_open_dead(data: WalletData, threshold: float = 0.01) -> int:
    """Open positions with curPrice <= threshold (effectively-resolved-against).

    Under the held-token-price convention (ADR-003 + R2 empirical
    resolution, cba241e), `curPrice` is the held-token price uniformly
    regardless of outcome label (YES/NO/categorical). A held token whose
    price collapsed to ~0 has resolved against the wallet — the wallet
    holds a worthless loser. The threshold therefore catches losers
    universally and no outcome-label dispatch is required.

    Spot-check on swissmiss /positions confirmed the same convention for
    open rows (paired YES `curPrice=0.0165`, NO `curPrice=0.9835` on the
    same condition — outcome-native held-token prices).

    R3 ticket (`docs/superpowers/specs/2026-05-06-stage1-f1-open-dead-split.md`)
    closed as no-fix-needed under R2 Outcome 2.
    """
    n = 0
    for p in data.open_positions:
        cp = p.get("curPrice")
        if cp is None:
            continue
        if float(cp) <= threshold:
            n += 1
    return n


def _top3_pnl_concentration(
    data: WalletData,
) -> tuple[float | None, list[int] | None]:
    """Returns (concentration_share, indices_into_closed_positions).

    Concentration = fraction of net realized P&L (by absolute value) from
    the top-3 closed positions. Returns (None, None) when total net P&L is
    zero or non-positive — concentration metric is not meaningful.

    Indices are positions in `data.closed_positions` (the order passed
    in), so callers can use them to slice for the H1_sub_top3 diagnostic.
    """
    pnls = [float(p.get("realizedPnl") or 0.0) for p in data.closed_positions]
    if not pnls:
        return None, None
    # Index-aware sort by abs(P&L) descending
    indexed = sorted(
        enumerate(pnls), key=lambda t: abs(t[1]), reverse=True,
    )
    top3 = indexed[:3]
    top3_abs = sum(abs(x) for _, x in top3)
    total_abs = sum(abs(x) for x in pnls)
    if total_abs <= 0:
        return None, None
    indices = [i for i, _ in top3]
    return top3_abs / total_abs, indices


def _as_of_ts(data: WalletData) -> int:
    """The instant this wallet's data represents: when it was fetched.

    Every time-relative gate must measure against this, never wall clock. For a live run they
    are the same moment (fetched_at is set seconds earlier), so nothing changes. For a fixture
    replay they diverge by however long the fixture has existed, which silently turned a
    deterministic calibration baseline into a time bomb: 0xb3b50ef6 flipped
    edge_detected -> data_insufficient 81 days after capture with no code or data change.
    """
    return int(getattr(data, "fetched_at", 0) or 0) or int(time.time())


def _recent_90d_fraction(data: WalletData) -> float | None:
    """Fraction of closed positions whose timestamp is within the 90 days before capture.

    As-of capture, not wall clock — same reason as the R3 dormancy gate below: a frozen
    fixture must not drift across a time threshold just because the calendar moved.
    """
    if not data.closed_positions:
        return None
    cutoff = _as_of_ts(data) - 90 * 86400
    n_recent = sum(
        1 for p in data.closed_positions
        if int(p.get("timestamp") or 0) >= cutoff
    )
    return n_recent / len(data.closed_positions)


def evaluate_stage1_gates(data: WalletData) -> GateEvaluation:
    """Run the Stage 1 gates in order. First failing gate wins.

    Order matters per spec §3:
    R1 hedge archetype → R2 insufficient sample → R3 dormant
    Then caution flags F1-F4 (do not block; collected for the report).
    """
    caution: list[str] = []

    # R1 — Hedge archetype
    if (
        data.archetype_layer1 is not None
        and data.archetype_layer1 in _HEDGE_LAYER1_LABELS
    ):
        return GateEvaluation(
            result="reject_hedge_archetype",
            reason=(
                f"Layer 1 archetype = {data.archetype_layer1!r}. "
                "Structurally non-copyable; tag-level inference invalid because "
                "winning and losing legs are correlated by construction."
            ),
            caution_flags=caution,
        )

    # R2 — Insufficient sample
    n_closed = len(data.closed_positions)
    if n_closed < N_CLOSED_POSITIONS_MIN:
        return GateEvaluation(
            result="reject_insufficient_sample",
            reason=(
                f"n_closed_positions = {n_closed} < {N_CLOSED_POSITIONS_MIN}. "
                "All Stage 2 hypotheses would be underpowered."
            ),
            caution_flags=caution,
        )

    # R3 — Dormant, measured as of capture (see _as_of_ts).
    last_ts = _last_activity_ts(data)
    if last_ts > 0:
        days_since = (_as_of_ts(data) - last_ts) / 86400
        if days_since > DORMANT_DAYS_THRESHOLD:
            return GateEvaluation(
                result="reject_dormant",
                reason=(
                    f"last_activity = {days_since:.0f} days ago "
                    f"(threshold = {DORMANT_DAYS_THRESHOLD}). "
                    "Idle wallet; historical analysis non-projectable."
                ),
                caution_flags=caution,
            )

    # Caution flags (do not reject; only annotate)

    # F1 — Sloppy operator: many dead-but-unredeemed open positions
    n_open_dead = _count_open_dead(data)
    n_open = len(data.open_positions)
    if n_open_dead > N_OPEN_DEAD_F1_MIN and n_open > 0:
        ratio = n_open_dead / n_open
        if ratio > F1_OPEN_DEAD_RATIO_THRESHOLD:
            caution.append(
                f"F1_sloppy_operator (n_open_dead={n_open_dead}, ratio={ratio:.2f})"
            )

    # F2 — Single-bet concentration
    top3_share, top3_indices = _top3_pnl_concentration(data)
    f2_top3_indices: list[int] | None = None
    f2_top3_share: float | None = None
    if top3_share is not None and top3_share > F2_TOP3_NET_PNL_FRACTION:
        caution.append(
            f"F2_single_bet_concentration (top3_share={top3_share:.2f})"
        )
        f2_top3_indices = top3_indices
        f2_top3_share = top3_share

    # F3 — Recent-regime concentration
    recent = _recent_90d_fraction(data)
    if recent is not None and recent > F3_RECENT_90D_FRACTION:
        caution.append(
            f"F3_recent_regime (last_90d_share={recent:.2f})"
        )

    # F4 — Heavy CTF: depends on execution_profile content. Simple heuristic:
    # if execution_profile reports >50% of "fills" classified as redemption-like.
    # The exact field name varies; check several common shapes.
    if isinstance(data.execution_profile, dict):
        ctf_share = data.execution_profile.get("ctf_redemption_share")
        if ctf_share is None:
            ctf_share = data.execution_profile.get("redemption_fraction")
        if isinstance(ctf_share, (int, float)) and ctf_share > 0.50:
            caution.append(f"F4_heavy_ctf (ctf_share={ctf_share:.2f})")

    return GateEvaluation(
        result="pass",
        reason=None,
        caution_flags=caution,
        n_open_dead=n_open_dead,
        f2_top3_indices=f2_top3_indices,
        f2_top3_share=f2_top3_share,
    )


__all__ = [
    "GateEvaluation",
    "evaluate_stage1_gates",
    "N_CLOSED_POSITIONS_MIN",
    "DORMANT_DAYS_THRESHOLD",
]
