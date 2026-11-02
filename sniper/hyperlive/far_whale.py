"""Far-Whale Liquidation Magnet — candidate detection (paper study fwlm-v0).

Spec: docs/superpowers/specs/2026-07-04-hl-far-whale-liquidation-magnet-paper-study.md
Canonical side semantics per spec §2. NEVER reuse cluster_map's `side` field — it labels
the magnet direction, the INVERSE of the liquidating trader's side. Here:
  position_side          = the liquidating trader's side (long if szi > 0)
  direction_to_liquidation = up if liquidation_price > mid else down (paper entry direction)
  invariant: long_liquidation <=> direction down; short_liquidation <=> direction up.
Violations are dropped + counted upstream — never coerced.
"""
from __future__ import annotations

from sniper.hyperlive.models import ROUND_TRIP_FEE_BPS

# --- frozen v0 pre-registration (spec §16) — constants, NOT env knobs. Changing any of
# these starts a new study_version; they are the contract, so they live in code.
STUDY_VERSION = "fwlm-v0"
DIST_MIN_BPS = 50.0
DIST_MAX_BPS = 300.0
MAX_SPREAD_BPS = 5.0
MAX_STALENESS_MS = 5000
SWEEP_FLOOR_NOTIONAL = 50_000.0        # capture floor for the §4 sweep pool, NOT the study gate
EPISODE_LIQ_TOL_BPS = 25.0             # §3.1 episode identity tolerance
EPISODE_GAP_MS = 3_600_000             # §3.1: 60 min out of gate -> new episode
TARGET_FRAC = 0.75
ADVERSE_FRAC = 0.50
HORIZON_MS = 30 * 60_000               # primary; diagnostics below
# §6 diagnostic horizons — pre-registered for a future multi-horizon readout; v0 wires
# only the primary HORIZON_MS. Kept so the constant set matches the spec, not yet computed.
DIAG_HORIZONS_MS = (10 * 60_000, 20 * 60_000, 60 * 60_000)
PAPER_NOTIONAL = 1_000.0
MIGRATE_MIN_FRAC = 0.25                # directional-progress label; looser than TARGET_FRAC by design
MAX_EVENT_SHARE = 0.40                 # Control 5 — applied to positive pnl AND candidate counts
CAMPAIGN_CAP_DAYS = 60                 # §13 sparsity kill
FEE_BPS = ROUND_TRIP_FEE_BPS           # ~9 bps round-trip taker


def whale_rows_from_clearinghouse(state: dict, address: str) -> list[dict]:
    """One clearinghouseState -> per-position rows keeping szi SIGN (position_side source).
    cluster_map.positions_from_clearinghouse drops the sign — do not reuse it here."""
    out = []
    for ap in (state or {}).get("assetPositions", []) or []:
        pos = ap.get("position") or {}
        coin = pos.get("coin")
        liq = _f(pos.get("liquidationPx"))
        szi = _f(pos.get("szi"))
        entry = _f(pos.get("entryPx"))
        if not coin or liq is None or liq <= 0 or szi is None or szi == 0:
            continue
        out.append({
            "address": address,
            "asset": str(coin).upper(),
            "liquidation_price": liq,
            "szi": szi,
            "position_side": "long" if szi > 0 else "short",
            "entry_px": entry,
            "position_notional": abs(szi) * (entry if entry else liq),
        })
    return out


def evaluate_candidate(row: dict, mid: float, *, spread_bps: float | None,
                       staleness_ms: int, threshold_notional: float
                       ) -> tuple[dict | None, str | None]:
    """§3 gate + §2 invariants. Returns (candidate, None) or (None, drop_reason).
    Order matters: anomalies are detected before the band check so they are COUNTED
    even when the level is also out of band (spec §2: log dropped anomalies)."""
    if not mid or mid <= 0:
        return None, "no_mid"
    liq = float(row["liquidation_price"])
    direction = "up" if liq > mid else "down"
    # §2 invariant: long liquidates below mid, short above. Mismatch = source anomaly.
    expected = "down" if row["position_side"] == "long" else "up"
    if direction != expected:
        return None, "anomaly_side_invariant"
    if float(row["position_notional"]) < threshold_notional:
        return None, "below_threshold"
    distance_bps = abs(liq - mid) / mid * 1e4
    if not (DIST_MIN_BPS <= distance_bps <= DIST_MAX_BPS):
        return None, "out_of_band"
    if spread_bps is None:
        return None, "spread_missing"        # unknown spread -> fail closed
    if spread_bps > MAX_SPREAD_BPS:
        return None, "spread_wide"
    if staleness_ms > MAX_STALENESS_MS:
        return None, "stale"
    return {
        "asset": row["asset"],
        "whale_address": row["address"],
        "liquidation_price": liq,
        "mid_price": float(mid),
        "distance_bps": distance_bps,
        "direction_to_liquidation": direction,
        "position_side": row["position_side"],
        "liquidation_type": ("long_liquidation" if row["position_side"] == "long"
                             else "short_liquidation"),
        "position_notional": float(row["position_notional"]),
        "position_size": abs(float(row["szi"])),
        "spread_bps": float(spread_bps),
    }, None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
