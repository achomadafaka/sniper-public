"""A bounded price path for every open PAPER position (2026-09-27, operator).

WHY. A shape table (MFE, MAE, value at +10/30/60/120/300 s) exists for funded positions through
memecoin_route_series. Paper had nothing per position: memecoin_position_marks is per MINT, gap
p50 33 s, 2 points in the first 60 s (7 days, 2,850 closed paper positions).

KEEPING, NOT FETCHING. The rows come from the price map the paper bracket already fetched this
pass (~15 s). No provider is called here and no funded thread writes here: the bracket runs on its
own worker thread with its own connection.

BASIS 'mid'. DexScreener lags ~25 s plus a 30 s CDN cache, so a mid path cannot show +10 s or
+30 s; a reader must censor those horizons for basis 'mid', never interpolate. Paper and funded
rows never share a table, and `basis` keeps a later executable source apart from this one.

BOUNDED. Every observation in the first 60 s, then one per 15 s to 300 s, then one per 60 s, plus
every new high and low, and never more than MAX_ROWS per position.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from sniper.persistence import db_memecoin as m

logger = logging.getLogger(__name__)

BASIS_MID = "mid"
BASIS_TICK = "tick"        # the v1 engine's own on-chain tick (paper step 1, spec 2026-10-08)
FIRST_WINDOW_S = 60.0
MID_WINDOW_S = 300.0
MID_STEP_S = 15.0
LATE_STEP_S = 60.0
MAX_ROWS = 400


def _ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def keep_kind(t_offset, state, price):
    """'sample', 'high', 'low', or None. state = (rows, last t_offset_s, high, low) or None."""
    if not state or not state[0]:
        return "sample"
    rows, last, high, low = state
    if rows >= MAX_ROWS:
        return None
    if price > high:
        return "high"
    if price < low:
        return "low"
    step = 0.0 if t_offset <= FIRST_WINDOW_S else MID_STEP_S if t_offset <= MID_WINDOW_S else LATE_STEP_S
    return "sample" if t_offset - last >= step else None


def record_paper_path(conn, positions, price_map, now_iso, *, key_fn=None, basis=BASIS_MID):
    """Append this pass's price for each open paper position, thinned. Returns rows written.
    `price_map` may key a price by position id (the v1 engine's own tick per position) as well as
    by coin; `basis` keeps a tick path apart from a mid path."""
    from sniper.settings import memecoin as cfg
    if not getattr(cfg, "MEMECOIN_PAPER_PATH_ENABLED", True):
        return 0
    key_fn = key_fn or m.coin_identity
    papers = [p for p in positions or ()
              if p.get("execution_mode") == "paper" and (p.get("opened_recorded_at") or p.get("opened_at"))]
    if not papers or not price_map:
        return 0
    state = m.paper_path_state(conn, [p["id"] for p in papers], basis)
    now = _ts(now_iso)
    rows = []
    for p in papers:
        st = (price_map.get(p["id"]) or price_map.get(key_fn(p.get("network_id"), p["mint"]))
              or price_map.get(p["mint"]) or {})
        try:
            price = float(st.get("price"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(price) and price > 0):
            continue
        t = now - _ts(p.get("opened_recorded_at") or p["opened_at"])
        if t < 0:
            continue
        kind = keep_kind(t, state.get(p["id"]), price)
        if kind is None:
            continue
        rows.append((p["id"], p.get("network_id"), p["mint"], basis,
                     st.get("source") or "dexscreener", round(t, 3), now_iso, price, kind))
    return m.insert_paper_path_rows(conn, rows)
