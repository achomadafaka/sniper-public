"""Post-buy-pump instrumentation: forward-capture each large-buy sighting's price path.

Pure horizon logic + a capture pass that batch-polls DexScreener for outcomes whose marks
are due. ref_price = the trader's buy price (their_price); pump = (peak_mark/ref − 1)·100.
Marks are sampled at fixed horizons, so max_gain is a lower bound on the true peak — this is
a per-trader RANKING signal ("do their buys move price"), not a net copy-edge estimate.
"""
from __future__ import annotations

from datetime import datetime

from sniper.persistence import db_memecoin as m
from sniper.execution.solana_quote import dexscreener_stats_batch_keyed, quote_for, quote_key
from sniper.settings import memecoin as _cfg


def _epoch(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def due_marks(outcome, now_ts, horizons):
    """Horizons whose due time (buy_ts + h) has passed and which are not yet filled."""
    bt = _epoch(outcome.get("buy_ts"))
    if bt is None or now_ts is None:
        return []
    marks = outcome.get("marks") or {}
    return [h for h in horizons if str(h) not in marks and (now_ts - bt) >= h]


# unit-mismatch guard: fomo their_price is USD for Solana (verified via the copy chase guard)
# but the feed is MULTICHAIN and discovery buys don't pass that guard, so a mis-scaled price
# on some chain would produce a garbage ratio. A real memecoin move is well inside [1e-4, 1e4]
# (−99.99% .. +1,000,000%); a mark whose price/ref lands outside is treated as a bad quote.
# RATIO_MAX 1e3 (was 1e4): a malformed HYPE mark at 4,916x ref slipped under the old bound
# and polluted the large-cap pump stats as a +491,553%% 'gain' (Codex audit 2026-07-11).
# Within a <=1h capture window, >1000x is a unit-mismatch print, not a market move.
_RATIO_MIN, _RATIO_MAX = 1e-4, 1e3


def recompute(ref_price, marks):
    """From filled marks -> (max_gain_pct, peak_horizon_s, last_price, n_marks).
    max_gain is the best (price/ref−1) over SANE sampled marks; last_price is the newest mark."""
    filled = {int(k): v for k, v in (marks or {}).items() if v and v.get("price")}
    n = len(filled)
    if not filled or not ref_price or ref_price <= 0:
        last = filled[max(filled)]["price"] if filled else None
        return (None, None, last, n)
    # HIGH-side outliers (price/ref > 1e4) are unit mismatches (a real +1,000,000% pump is
    # implausible) -> drop. LOW-side (a rugged token, >99.99% loss) is REAL -> clamp the
    # effective price to the ref*1e-4 floor and COUNT it as a ~−99.99% loss, don't drop it
    # (dropping the worst outcomes made the profile systematically optimistic).
    usable = {}
    for h, v in filled.items():
        if (v["price"] / ref_price) > _RATIO_MAX:
            continue
        usable[h] = max(v["price"], ref_price * _RATIO_MIN)
    last_price = filled[max(filled)]["price"]
    if not usable:
        return (None, None, last_price, n)   # only absurd-high (unit-mismatch) marks -> no gain
    peak_h = max(usable, key=lambda h: usable[h])
    max_gain = (usable[peak_h] / ref_price - 1.0) * 100.0
    return (round(max_gain, 4), peak_h, last_price, n)


def is_matured(outcome, now_ts, horizons, abandon_s):
    """Matured once every horizon is filled, OR the final horizon is overdue by abandon_s
    (a dust/dead token whose quotes keep failing must not stay open forever)."""
    marks = outcome.get("marks") or {}
    if all(str(h) in marks for h in horizons):
        return True
    bt = _epoch(outcome.get("buy_ts"))
    if bt is None or now_ts is None:
        return False
    return (now_ts - bt) >= (max(horizons) + abandon_s)


def pump_capture_pass(conn, now_iso, stats_batch_fn=None, *, horizons=None, abandon_s=None):
    """Fill due horizon marks for all open outcomes in one batched price poll. Fail-closed:
    a missing quote leaves the mark null to retry next pass. Returns {polled, filled, matured}."""
    horizons = horizons or _cfg.MEMECOIN_PUMP_HORIZONS_S
    abandon_s = _cfg.MEMECOIN_PUMP_ABANDON_S if abandon_s is None else abandon_s
    stats_batch_fn = stats_batch_fn or dexscreener_stats_batch_keyed
    now_ts = _epoch(now_iso)
    open_rows = m.list_open_outcomes(conn)

    # collect mints that have at least one due mark, batch-poll once
    need = {}   # (network_id, normalized mint) -> raw key
    per_row_due = {}
    for o in open_rows:
        d = due_marks(o, now_ts, horizons)
        if d:
            per_row_due[o["fomo_event_id"]] = d
            key = quote_key(o.get("network_id"), o["mint"])
            need[key] = (o.get("network_id") or key[0], o["mint"])
    if not need:
        price_map = {}
    elif stats_batch_fn is dexscreener_stats_batch_keyed:
        price_map = stats_batch_fn(list(need.values()))
    else:
        # Backward-compatible injected fixture functions still consume raw mints.
        price_map = stats_batch_fn([mint for _network, mint in need.values()])
    if price_map:
        try:   # dense quote ledger: signal-coin paths at minute resolution (v1 exit-search
            from sniper.persistence.db_memecoin import record_quote_observations  # prereq)
            record_quote_observations(conn, price_map, now_iso)
        except Exception:
            pass   # ledger is best-effort; the capture pass must never break on it

    filled = matured = 0
    # PHASE 1 — READ AND RECOMPUTE ONLY. Nothing below writes. The applies are collected and handed
    # to `save_outcomes` in one short transaction after the loop, so the write lock covers the
    # applies rather than the whole pass. See `db_memecoin.save_outcomes` for the measurement that
    # motivated this; the short version is that a per-row commit copied the same
    # `idx_mc_outcome_status` leaf pages into the WAL ~100 times per pass.
    pending = []
    for o in open_rows:
        eid = o["fomo_event_id"]
        marks = dict(o.get("marks") or {})
        bt = _epoch(o.get("buy_ts"))
        for h in per_row_due.get(eid, []):
            st = quote_for(price_map, o["mint"], o.get("network_id"))
            price = st.get("price") if st else None
            if price:
                marks[str(h)] = {"price": price, "age_s": round(now_ts - bt) if (now_ts and bt) else None}
                filled += 1
        mg, ph, last, n = recompute(o.get("ref_price"), marks)
        # re-evaluate maturity against the UPDATED marks
        status = "matured" if is_matured({"buy_ts": o["buy_ts"], "marks": marks},
                                         now_ts, horizons, abandon_s) else "open"
        if status == "matured":
            matured += 1
        # only write if something changed (new marks) or it just matured — unchanged guard,
        # it now decides membership of the batch rather than whether to fire a commit
        if per_row_due.get(eid) or status != o.get("status"):
            pending.append({"fomo_event_id": eid,
                            "marks": {int(k): v for k, v in marks.items()},
                            "max_gain_pct": mg, "peak_horizon_s": ph, "last_price": last,
                            "n_marks": n, "status": status})
    # PHASE 2 — one short write-only transaction
    written = m.save_outcomes(conn, pending, now_iso=now_iso)
    return {"polled": len(need), "filled": filled, "matured": matured, "written": written}
