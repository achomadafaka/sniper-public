"""E14 on Solana, step 0: the coin's own price path from OUR marks already on the mark tape.

Spec docs/superpowers/specs/2026-10-09-memecoin-solana-presignal-feed-design.md, Gamma PASS 8da3c172c,
operator-approved step 0. SHADOW ONLY: the classifier records this beside the provider's momentum and
nothing decides on it.

The marks come from the paper vault stream (`paper_tick:sol_*`, since paper step 1) and the funded swap
marks (`fastlane_sol_*`). Both are written one per coin per 30 s (the mark floor), the provider series'
own cadence.

- ONE KIND PER SERIES. Spot-type marks (pool state, curve reserves) when they make the phase's 3 points,
  else swap-type marks (a trade's own execution price, which carries impact); never mixed. A STATE mark
  on DLMM/CLMM is not spot, and the funded corroboration band already keeps those off the tape.
- OUR OWN FILLS. `fastlane_sol_*` exists only on coins we hold funded, and after our fill the pool's
  path includes our buy's impact. A series with one of our funded fills inside its window says so
  (`includes_own_fill`), and the read reports it apart.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("memecoin")

# EXACT NAMES, NOT PREFIXES (Gamma M1 on 7271a137a). `paper_tick:sol_state_carried` prices the pool's
# LAST SWAP (an execution price) while the reserves sit still; a prefix match on `paper_tick:sol_state`
# called it spot, and 14 of 32 live 'spot' series mixed kinds or were all carried. It is swap-type.
SPOT_SOURCES = frozenset({"paper_tick:sol_state", "paper_tick:sol_curve", "fastlane_sol_state",
                          "fastlane_sol_curve"})
CARRIED_SOURCE = "paper_tick:sol_state_carried"
SWAP_SOURCES = frozenset({"paper_tick:sol_swap", "fastlane_sol_swap", CARRIED_SOURCE})
WINDOW_S = 900.0          # MEMECOIN_FUNDED_TAPE_WINDOW_S's default; read at call time below


def _epoch(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _includes_own_fill(conn, mint, lo, hi):
    """True when one of our FUNDED fills on this Solana coin falls in [lo, hi]."""
    lo_iso = datetime.fromtimestamp(lo, timezone.utc).isoformat()
    for (created,) in conn.execute(
            "SELECT f.created_at FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
            "WHERE p.execution_mode='real' AND (p.is_open=1 OR p.closed_at>=?) AND p.mint=? "
            "AND p.network_id='solana'", (lo_iso, mint)):
        at = _epoch(created)
        if at is not None and lo <= at <= hi:
            return True
    return False


def own_marks_series(conn, mint, at, *, window_s=WINDOW_S, min_points=3):
    """{reason, points [(epoch, usd)], protocol 'marks:spot'|'marks:swap', includes_own_fill, ...}."""
    out = {"reason": None, "points": [], "pool": None, "protocol": None, "quote_token": None,
           "quote_usd": None, "covered_s": float(window_s), "includes_own_fill": None, "carried_n": None}
    lo, hi = float(at) - float(window_s), float(at)
    # A text bound one minute wide on each side lets the (chain_id, mint, sweep_ts) index prune;
    # the parsed instants below are the authority (mixed 'Z' / '+00:00' spellings sort apart).
    coarse = (datetime.fromtimestamp(lo - 60, timezone.utc).isoformat(),
              datetime.fromtimestamp(hi + 60, timezone.utc).isoformat())
    spot, swap, carried = [], [], 0
    for ts, mid, source in conn.execute(
            "SELECT sweep_ts, mid, source FROM memecoin_position_marks WHERE chain_id='solana' "
            "AND mint=? AND sweep_ts>=? AND sweep_ts<=? AND quality='live'", (mint, *coarse)):
        when = _epoch(ts)
        if when is None or not lo <= when <= hi or mid is None or float(mid) <= 0:
            continue
        source = str(source or "")
        if source in SPOT_SOURCES:
            spot.append((when, float(mid)))
        elif source in SWAP_SOURCES:
            swap.append((when, float(mid)))
            carried += source == CARRIED_SOURCE
    if not spot and not swap:
        out["reason"] = "no_own_marks"
        return out
    kind, points = (("spot", spot) if len(spot) >= min_points or not swap else ("swap", swap))
    out.update(reason="ok", points=sorted(points), protocol="marks:%s" % kind,
               carried_n=carried if kind == "swap" else 0,
               includes_own_fill=_includes_own_fill(conn, mint, lo, hi))
    return out


def read_momentum(mint):
    """One read-only read of the main ledger's mark tape; never a network call, never raises."""
    from sniper.persistence.db_core import get_db_path, get_readonly_connection
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now
    started = time.monotonic()
    try:
        conn = get_readonly_connection(get_db_path())
        try:
            at = real_utc_now()
            out = own_marks_series(conn, mint, at, window_s=float(
                getattr(cfg, "MEMECOIN_FUNDED_TAPE_WINDOW_S", WINDOW_S)))
            out["read_at"] = datetime.fromtimestamp(at, timezone.utc).isoformat()
        finally:
            conn.close()
    except Exception as exc:
        import sqlite3
        if isinstance(exc, (sqlite3.Error, OSError)):
            out = {"reason": "ledger_unavailable", "points": []}
        else:
            logger.exception("solana own momentum read failed mint=%s", mint)
            out = {"reason": "read_failed", "points": []}
    out["read_ms"] = (time.monotonic() - started) * 1000.0
    return out
