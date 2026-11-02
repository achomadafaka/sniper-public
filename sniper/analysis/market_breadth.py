"""WO-4 — regime context: honest reuse of the failed cross-pool factor.

The robustness gate (docs/research/2026-07-24-memecoin-tape-predictive-robustness.md) proved a strong
SHARED market factor that fails as per-pool timing but is real cross-sectionally. This operationalizes
it as a market-BREADTH signal: what fraction of active pools have positive net flow right now. When
most of the market is offered (breadth low = "regime off"), a held position's weakness is more likely
real; when most is bid ("regime on"), a dip is more likely market-wide noise.

CONTEXT ONLY, shadow. It is logged on the decision row (regime_json); NO rule consumes it without its
own new prereg — see docs/research/2026-07-25-regime-context-shadow-prereg.md. This complements
memecoin_market_state._regime (which measures the ENTRY feed's buy consensus) with an exit-side tape
signal; it deliberately does not re-implement the consensus regime.

Data-relative "now" (the latest observed tick), never the host wall clock, because this host runs
~+7h fast and pool_ticks.observed_at is ingest time.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

NEUTRAL_BREADTH = 0.5     # [inherited] half the active market bid = the neutral midpoint
MIN_ACTIVE_POOLS = 5      # [bound] below this the breadth is too thin to label -> "unknown"


def _iso_minus(iso, seconds):
    return (datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            - timedelta(seconds=seconds)).isoformat()


def tape_market_breadth(conn, chain_id, *, window_s=300, min_active_pools=MIN_ACTIVE_POOLS,
                        neutral=NEUTRAL_BREADTH, as_of=None):
    """Fraction of active pools (>=1 swap in the last `window_s`) whose net quote flow is positive.

    Returns {regime, breadth, active_pools, positive_pools, window_s}. regime is:
      'off'     breadth < neutral  (market mostly offered — a dip is more likely real)
      'on'      breadth >= neutral (market mostly bid — a dip is more likely market noise)
      'unknown' fewer than min_active_pools active pools (too thin to label)
    net flow sign per pool = sign of the summed signed QUOTE amount (amount{quote_side}); positive
    quote into the pool = a buy of the coin (exit_features.pool_flow convention)."""
    unknown = {"regime": "unknown", "breadth": None, "active_pools": 0,
               "positive_pools": 0, "window_s": window_s}
    if as_of is None:
        row = conn.execute("SELECT MAX(observed_at) FROM pool_ticks WHERE chain_id=?",
                           (str(chain_id),)).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(observed_at) FROM pool_ticks WHERE chain_id=? AND observed_at<=?",
            (str(chain_id), str(as_of))).fetchone()
    latest = row[0] if row else None
    if not latest:
        return unknown
    start = _iso_minus(latest, window_s)
    rows = conn.execute(
        "SELECT p.pool, m.quote_side, p.amount0, p.amount1 FROM pool_ticks p "
        "JOIN pool_meta m ON m.chain_id=p.chain_id AND m.pool=p.pool "
        "WHERE p.chain_id=? AND m.quote_side IS NOT NULL "
        "AND p.kind IN ('v2_swap','v3_swap','sol_swap','sol_curve_swap') "
        "AND p.observed_at>=? AND p.observed_at<=?",
        (str(chain_id), start, latest)).fetchall()
    net = defaultdict(int)
    for pool, quote_side, amount0, amount1 in rows:
        amt = amount1 if int(quote_side) == 1 else amount0
        try:
            net[pool] += int(amt)
        except (TypeError, ValueError):
            continue
    active = len(net)
    positive = sum(1 for v in net.values() if v > 0)
    if active < min_active_pools:
        return {**unknown, "active_pools": active, "positive_pools": positive}
    breadth = positive / active
    return {"regime": "on" if breadth >= neutral else "off", "breadth": breadth,
            "active_pools": active, "positive_pools": positive, "window_s": window_s}
