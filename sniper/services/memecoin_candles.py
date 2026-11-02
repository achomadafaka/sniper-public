"""Phase 5 — OHLC candles over `memecoin_position_marks`, plus the context a position needs.

Sparklines cannot support an intra-position decision. The tape already holds everything a
real chart needs; what was missing is a shape that puts the price, the fills, and the frozen
contract's rail levels in ONE payload, because those three together are what explains an
exit.

Position 11984 is the canonical case and the phase gate: its tape shows a single +50.489%
mid tick, its rails saw a +48.296% executable peak, and the 2.2-point gap between those two
numbers is exactly why its +50% de-risk never fired and it exited at −66% instead. A chart
that draws only `mid` cannot tell that story, so the payload carries both peaks and the
rail levels as absolute prices.

Gaps stay gaps. 4663 mark gaps reach p99 380s; interpolating across them would invent a
tape that never existed.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 2000

# Sources whose `mid` is an EXECUTABLE price (whole-bag, route-aware) rather than a pool mid.
# They must never share a candle with mid rows: merging them and labelling the bar with the
# dominant source is precisely what hides the divergence this chart exists to show.
EXECUTABLE_SOURCES = ("fastlane_rpc",)
# OUR OWN marks (operator via Alpha, 2026-10-08, Douyin 72229): the exact route quote, the Solana swap
# and curve marks, and the 4663 post-close tape anchor. A FUNDED position's chart, series, card price and
# last-known price read only these; a provider mid (DexScreener, GeckoTerminal, the paper sweep's
# paper_dexscreener rows) never reaches them. fastlane_sol_state stays off the screen as before.
OWN_MARK_SOURCES = ("fastlane_rpc", "fastlane_sol_swap", "fastlane_sol_curve",
                    "onchain_tape_post_close_post_swap_anchor")


def _parse(ts):
    try:
        d = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def to_candles(rows, interval_s):
    """Bucket mark rows into OHLC. Pure; rows need `sweep_ts` and `mid`.

    Each candle also reports `n` (how many marks backed it — a 1-mark candle on a 60s
    interval is a cadence warning, not a price signal) and `src`, the dominant source in the
    bucket, so an old-cadence GT era and a new-cadence executable era stay visually distinct.
    """
    interval_s = max(1, int(interval_s or 60))
    buckets = {}
    for r in rows or []:
        ts = _parse(r.get("sweep_ts") if isinstance(r, dict) else r["sweep_ts"])
        mid = (r.get("mid") if isinstance(r, dict) else r["mid"])
        try:
            mid = float(mid)
        except (TypeError, ValueError):
            continue
        if ts is None or mid <= 0:
            continue
        epoch = int(ts.timestamp())
        key = epoch - (epoch % interval_s)
        b = buckets.get(key)
        src = (r.get("source") if isinstance(r, dict) else r["source"]) or "?"
        if b is None:
            buckets[key] = {"o": mid, "h": mid, "l": mid, "c": mid, "n": 1, "_src": {src: 1}}
        else:
            b["h"] = max(b["h"], mid)
            b["l"] = min(b["l"], mid)
            b["c"] = mid
            b["n"] += 1
            b["_src"][src] = b["_src"].get(src, 0) + 1
    out = []
    for key in sorted(buckets):
        b = buckets.pop(key)
        src = max(b.pop("_src").items(), key=lambda kv: kv[1])[0]
        out.append({"t": datetime.fromtimestamp(key, timezone.utc).isoformat(), **b, "src": src})
    return out


def _rail_levels(entry_price, policy):
    """Rail percentages as absolute prices, so the chart draws horizontal lines without
    re-deriving policy arithmetic in JavaScript (and getting it subtly different)."""
    levels = {}
    try:
        entry = float(entry_price or 0.0)
    except (TypeError, ValueError):
        return levels
    if entry <= 0:
        return levels
    for name, key, sign in (("stop", "stop_pct", -1), ("catastrophe", "catastrophe_pct", -1),
                            ("derisk", "derisk_pct", +1), ("tp", "tp_pct", +1)):
        try:
            pct = float((policy or {}).get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if pct > 0:
            levels[name] = entry * (1.0 + sign * pct / 100.0)
    return levels


def _shift(ts, seconds):
    d = _parse(ts)
    return (d + timedelta(seconds=seconds)).isoformat() if d else None


def candles_payload(conn, *, chain_id, mint, interval_s=60, since=None, position_id=None,
                    max_rows=DEFAULT_MAX_ROWS, pad_s=120):
    """Tape + fills + rails for one `(chain_id, mint)`, optionally in one position's context.

    Keyed on `(chain_id, mint)` throughout — never mint alone (07-15 keying finding: the same
    mint string on two chains is two different coins).
    """
    pos = None
    if position_id is not None:
        try:
            row = conn.execute("SELECT * FROM memecoin_positions WHERE id=?",
                               (int(position_id),)).fetchone()
            pos = dict(row) if row is not None else None
        except Exception:
            logger.exception("candles: position lookup failed id=%s", position_id)
        # Identity must AGREE. Loading the position by id while querying the tape by
        # caller-supplied (chain, mint) lets one coin's fills and rails be drawn over another
        # coin's candles — a chart that is confidently, silently wrong.
        if pos is not None and (str(pos.get("network_id") or "") != str(chain_id)
                                or str(pos.get("mint") or "") != str(mint)):
            raise ValueError(
                f"position {position_id} is {pos.get('network_id')}/{pos.get('mint')}, "
                f"not {chain_id}/{mint}")

    # In a position's context the DEFAULT window is that position's own life, padded. Drawing
    # the mint's whole tape makes rails it never reached look reached: on 11984 the full-life
    # tape highs +283.7% while the position only ever saw +48.3%, which would read as "the +50%
    # de-risk was hit" when it never was. An explicit `since` still widens the view on request.
    win_from, win_to = (since or None), None
    if pos is not None:
        if not since:
            win_from = _shift(pos.get("opened_at"), -pad_s)
        if pos.get("closed_at"):
            win_to = _shift(pos.get("closed_at"), +pad_s)

    params = [str(chain_id), str(mint)]
    where = "chain_id=? AND mint=? AND quality != 'quarantined_spike'"
    if win_from:
        where += " AND sweep_ts >= ?"
        params.append(str(win_from))
    if win_to:
        where += " AND sweep_ts <= ?"
        params.append(str(win_to))
    rows = [dict(r) for r in conn.execute(
        "SELECT sweep_ts, mid, executable_exit_value, liquidity, mcap, source "
        "FROM memecoin_position_marks WHERE " + where + " ORDER BY sweep_ts", params)]

    exec_rows = [r for r in rows if (r.get("source") or "") in EXECUTABLE_SOURCES]
    mid_rows = [r for r in rows if (r.get("source") or "") not in EXECUTABLE_SOURCES]
    if pos is not None and pos.get("execution_mode") == "real":
        mid_rows = [r for r in mid_rows if (r.get("source") or "") in OWN_MARK_SOURCES]

    def _cap(cs):
        return cs[-int(max_rows):] if (max_rows and len(cs) > int(max_rows)) else cs

    candles = _cap(to_candles(mid_rows, interval_s))
    exec_candles = _cap(to_candles(exec_rows, interval_s))

    fills, rails, levels, position = [], {}, {}, None
    override, override_levels = {}, {}
    if pos is not None:
        fills = [{"t": f["created_at"], "kind": f["kind"], "price": f["price"],
                  "qty": f["qty"], "notional": f["notional"]}
                 for f in conn.execute(
                     "SELECT created_at, kind, price, qty, notional FROM memecoin_fills "
                     "WHERE position_id=? ORDER BY created_at", (int(position_id),))]
        policy = {}
        try:
            policy = (json.loads(pos.get("funded_contract_json") or "{}") or {}).get(
                "policy") or {}
        except (TypeError, ValueError):
            logger.warning("candles: unparseable funded contract on position %s", position_id)
        rails = {
            "entry_price": pos.get("entry_price"),
            "peak_price": pos.get("peak_price"),
            "executable_peak_price": pos.get("executable_peak_price"),
            "policy_id": policy.get("policy_id"),
            "stop_pct": policy.get("stop_pct"),
            "catastrophe_pct": policy.get("catastrophe_pct"),
            "derisk_pct": policy.get("derisk_pct"),
            "trail_pct": policy.get("trail_pct"),
            "tp_pct": policy.get("tp_pct"),
        }
        levels = _rail_levels(pos.get("entry_price"), policy)
        # Operator overrides are a SEPARATE overlay, never a contract mutation — and they can
        # contradict the frozen rails outright. Position 9380 froze stop_pct=0 (the 4663 chain
        # default) yet carried an active operator sl_pct=-60 and exited b_stop; drawing only
        # the contract shows no stop line on a position that stopped out.
        try:
            ov = conn.execute(
                "SELECT * FROM memecoin_position_exit_overrides WHERE position_id=? AND active=1 "
                "ORDER BY id DESC LIMIT 1", (int(position_id),)).fetchone()
            override = dict(ov) if ov is not None else {}
        except Exception:
            logger.exception("candles: override lookup failed id=%s", position_id)
            override = {}
        if override:
            override_levels = _rail_levels(pos.get("entry_price"), {
                "stop_pct": abs(float(override["sl_pct"])) if override.get("sl_pct") else 0.0,
                "tp_pct": float(override["tp_pct"]) if override.get("tp_pct") else 0.0,
                "derisk_pct": float(override["derisk_pct"]) if override.get("derisk_pct") else 0.0,
            })
        position = {"id": pos.get("id"), "ticker": pos.get("ticker"),
                    "opened_at": pos.get("opened_at"), "closed_at": pos.get("closed_at"),
                    "is_open": pos.get("is_open"), "exit_reason": pos.get("exit_reason"),
                    "realized_pnl": pos.get("realized_pnl"),
                    "execution_mode": pos.get("execution_mode")}

    return {"chain_id": str(chain_id), "mint": str(mint), "interval": int(interval_s or 60),
            "candles": candles, "exec_candles": exec_candles,
            "fills": fills, "rails": rails, "levels": levels,
            "override": override, "override_levels": override_levels,
            "position": position, "mark_count": len(rows),
            "window": {"from": win_from, "to": win_to}}
