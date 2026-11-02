#!/usr/bin/env python3
"""The true on-chain price path of funded Solana positions, from our entry to 10 min after exit.

Operator question 2026-09-27: did we start losing when the faster fills and exit fixes went live,
and were the vol_death exits bad? Our marks for curve coins are fastlane_mid (DexScreener) and are
not trusted here; memecoin_position_marks is never read.

Chain only, on the FIRST MEMECOIN_RESEARCH_HELIUS_RPC_URLS key (slot_lane_send_monitor.rpc_url,
which refuses a key shared with a funded variable); stops on any HTTP 429. Per position:
getSignaturesForAddress(mint) between our entry tx and our exit tx (and from our exit tx forward
for the after-window), then getTransaction on the trades it needs. A trade's price is the pump
curve's post-trade spot (virtual reserves) when a TradeEvent names the mint, else the execution
price of the pool leg that swapped the mint against wSOL/USDC (vault balance deltas).

    python3 scripts/research/funded_exit_truth.py 65860 65904 ...
    python3 scripts/research/funded_exit_truth.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TRADE_DISC = bytes.fromhex("bddb7fd34ee661ee")
SPACING_S = 1.1         # the research key 429d at 0.13 s spacing (2026-09-27 03:48Z)
DECODE_CAP = 80           # getTransaction calls per position for the held path (sampled evenly)


class Stop(Exception):
    pass


_URL = None
_PACER = None
RATE_429 = [0]


def rpc(method, params):
    """One call on the research key, through the key's SHARED pacer (Alpha 2026-10-02): one gap and
    one 429 pause across every process that reads this key (the slot-end shadow, entry capture T1,
    the wallet graph and these scripts)."""
    global _URL, _PACER
    if _URL is None:
        import slot_lane_send_monitor as mon
        _URL = mon.rpc_url()
    if _PACER is None:
        from sniper.services.memecoin_research_pacer import SharedPacer
        _PACER = SharedPacer.for_url(_URL, gap_s=SPACING_S)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(_URL, body, {"Content-Type": "application/json"})
    # A rate 429 ('Too Many Requests') pauses every reader of the key for 6, 12, then 24 s and
    # stops after the third; a quota 429 ('max usage') or any other HTTP error stops at once. Every
    # 429 is counted and printed.
    for attempt in range(4):
        _PACER.acquire()
        try:
            out = json.loads(urllib.request.urlopen(req, timeout=30).read())
            break
        except urllib.error.HTTPError as e:
            text = e.read()[:120]
            if e.code == 429:
                _PACER.pause(6 * 2 ** attempt)
            if e.code != 429 or b"max usage" in text or attempt == 3:
                raise Stop(f"HTTP {e.code} on {method}: {text!r}")
            RATE_429[0] += 1
            print(f"429 on {method}, waiting {6 * 2 ** attempt} s", file=sys.stderr, flush=True)
    if "error" in out:
        raise Stop(f"RPC error on {method}: {out['error']}")
    return out.get("result")


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None


def epoch(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def signatures(address, before=None, until=None, max_pages=8):
    """Newest-first sigs between `until` (exclusive, older) and `before` (exclusive, newer)."""
    out, cursor = [], before
    for _ in range(max_pages):
        opts = {"limit": 1000, "commitment": "confirmed"}
        if cursor:
            opts["before"] = cursor
        if until:
            opts["until"] = until
        page = rpc("getSignaturesForAddress", [address, opts]) or []
        out.extend(page)
        if len(page) < 1000:
            return out, True
        cursor = page[-1]["signature"]
    return out, False


def price_of(tx, mint, sol_usd):
    """(price_usd, kind) of the trade on `mint` in this tx, or (None, reason)."""
    if tx is None or (tx.get("meta") or {}).get("err") is not None:
        return None, "failed_or_missing"
    from sniper.execution.pump_curve_buy import parse_trade_event
    from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
    from sniper.services.memecoin_opportunity_live import program_data_logs
    for _i, line in program_data_logs(tx["meta"].get("logMessages") or [], PUMP):
        try:
            if anchor_event_bytes(line)[:8] != TRADE_DISC:
                continue
            e = parse_trade_event(line)
        except Exception:
            continue
        if e.get("mint") == mint and e.get("virtual_token_reserves"):
            vq = e.get("virtual_sol_reserves") or 0
            quote = e.get("quote_mint")
            if vq and (quote in (None, WSOL, "11111111111111111111111111111111")):
                return (vq / 1e9) / (e["virtual_token_reserves"] / 1e6) * sol_usd, "curve_spot"
            return None, "curve_non_sol_quote"
    # Pool leg: an owner whose target balance and wSOL/USDC balance moved in opposite directions.
    meta = tx["meta"]
    deltas = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            key = (b.get("owner"), b.get("mint"))
            deltas[key] = deltas.get(key, 0) + sign * int(b["uiTokenAmount"]["amount"])
    dec = {b.get("mint"): b["uiTokenAmount"]["decimals"]
           for b in (meta.get("postTokenBalances") or []) + (meta.get("preTokenBalances") or [])}
    best = None
    for (owner, m), d in deltas.items():
        if m != mint or not d:
            continue
        for q, qdec, to_usd in ((WSOL, 9, sol_usd), (USDC, 6, 1.0)):
            dq = deltas.get((owner, q), 0)
            if dq and (dq > 0) != (d > 0):
                p = (abs(dq) / 10**qdec * to_usd) / (abs(d) / 10**dec.get(mint, 6))
                size = abs(dq) / 10**qdec * to_usd
                if best is None or size > best[1]:
                    best = (p, size)
    return (best[0], "pool_leg") if best else (None, "no_trade_on_mint")


def load_positions(ids):
    q = ",".join("?" * len(ids))
    with contextlib.closing(sqlite3.connect(f"file:{REPO / 'sniper_tracker.db'}?mode=ro", uri=True,
                                            timeout=10)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            f"SELECT p.id, p.mint, p.source_handle, p.opened_recorded_at, p.closed_at, p.exit_reason, "
            f"p.entry_price, p.entry_notional, p.realized_pnl, p.close_sig, p.close_submitted_at, "
            f"p.exit_timing_json, a.tx_sig AS entry_sig, a.source_price "
            f"FROM memecoin_positions p LEFT JOIN memecoin_entry_attempts a ON a.attempt_id=p.attempt_id "
            f"WHERE p.id IN ({q}) ORDER BY p.id", ids).fetchall()
    return [dict(r) for r in rows]


def fire_time(p):
    """The exit decision instant: submission minus the pre-broadcast time the exit recorded."""
    try:
        pre = json.loads(p.get("exit_timing_json") or "{}").get("pre_broadcast_ms")
        return epoch(p["close_submitted_at"]) - (pre or 0) / 1000.0
    except (TypeError, ValueError):
        return epoch(p["closed_at"]) if p.get("closed_at") else None


AFTER_MARKS_S = (60, 300, 600)
AFTER_SLACK_S = 60


def after_marks(after, t_out):
    """{k: signature row} = the first successful trade at or after exit + k s, for each k.

    `after` is getSignaturesForAddress output (newest first). The window reaches AFTER_SLACK_S past
    the last target: bounded at exactly t_out + 600, the +10 min mark needed a block time equal to
    a fractional instant and was empty by construction (2 of 2 rows on 2026-09-27)."""
    rows = [s for s in reversed(after) if s.get("err") is None and s.get("blockTime")
            and s["blockTime"] <= t_out + AFTER_MARKS_S[-1] + AFTER_SLACK_S]
    return {k: nxt for k in AFTER_MARKS_S
            if (nxt := next((s for s in rows if s["blockTime"] >= t_out + k), None))}


def analyse(p, sol_usd):
    mint, entry = p["mint"], float(p["entry_price"])
    t_in, t_fire = epoch(p["opened_recorded_at"]), fire_time(p)
    t_out = epoch(p["closed_at"])
    held, held_complete = signatures(mint, before=p["close_sig"], until=p["entry_sig"])
    held = [s for s in held if s.get("err") is None]
    after, after_complete = signatures(mint, until=p["close_sig"], max_pages=10)
    # Decode: every held trade up to the cap (evenly sampled beyond it), all in the 30 s before
    # the fire, and the first trade at/after exit+1, +5, +10 min.
    held_sorted = sorted(held, key=lambda s: (s["slot"], s.get("blockTime") or 0))
    step = max(1, len(held_sorted) // DECODE_CAP)
    pick = {s["signature"] for s in held_sorted[::step]}
    pre_fire = [s for s in held_sorted if t_fire and t_fire - 30 <= (s.get("blockTime") or 0) <= t_fire]
    pick |= {s["signature"] for s in pre_fire}
    marks = after_marks(after, t_out)
    pick |= {s["signature"] for s in marks.values()}
    prices = {}
    for sig in pick:
        tx = rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                          "commitment": "confirmed"}])
        prices[sig] = price_of(tx, mint, sol_usd)
    path = [(s.get("blockTime"), prices[s["signature"]][0]) for s in held_sorted
            if s["signature"] in prices and prices[s["signature"]][0]]
    hi = max(path, key=lambda x: x[1]) if path else (None, None)
    lo = min(path, key=lambda x: x[1]) if path else (None, None)
    before_fire = [x for x in path if t_fire and x[0] and x[0] <= t_fire]
    at_fire = before_fire[-1][1] if before_fire else None
    qty = float(p["entry_notional"]) / entry
    exit_px = (float(p["entry_notional"]) + float(p["realized_pnl"] or 0)) / qty
    rel = lambda v: None if v is None else round((v / entry - 1) * 100, 1)
    pre_fire_trades = sum(1 for s in pre_fire if prices.get(s["signature"], (None,))[0])
    return {
        "id": p["id"], "handle": p["source_handle"], "mint": mint[:10], "exit": p["exit_reason"],
        "pnl": round(float(p["realized_pnl"] or 0), 2),
        "entry_vs_source_pct": rel_source(entry, p.get("source_price")),
        "held_s": round(t_out - t_in, 1),
        "held_txs": len(held), "held_txs_complete": held_complete, "decoded": len(path),
        "max_pct": rel(hi[1]), "max_at_s": None if hi[0] is None else round(hi[0] - t_in),
        "min_pct": rel(lo[1]), "min_at_s": None if lo[0] is None else round(lo[0] - t_in),
        "tp25_reached_spot": bool(hi[1] and hi[1] >= 1.25 * entry),
        "fire_at": iso(t_fire), "true_at_fire_pct": rel(at_fire), "our_exit_pct": rel(exit_px),
        "trades_30s_before_fire": pre_fire_trades, "txs_30s_before_fire": len(pre_fire),
        "after_1m_pct": rel(prices.get(marks[60]["signature"], (None,))[0]) if 60 in marks else None,
        "after_5m_pct": rel(prices.get(marks[300]["signature"], (None,))[0]) if 300 in marks else None,
        "after_10m_pct": rel(prices.get(marks[600]["signature"], (None,))[0]) if 600 in marks else None,
        "after_complete": after_complete,
    }


def rel_source(entry, source_price):
    try:
        return round((entry / float(source_price) - 1) * 100, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def selftest():
    tx = {"meta": {"err": None, "logMessages": [], "preTokenBalances": [
        {"owner": "POOL", "mint": "M", "uiTokenAmount": {"amount": "1000000", "decimals": 6}},
        {"owner": "POOL", "mint": WSOL, "uiTokenAmount": {"amount": "5000000000", "decimals": 9}}],
        "postTokenBalances": [
        {"owner": "POOL", "mint": "M", "uiTokenAmount": {"amount": "900000", "decimals": 6}},
        {"owner": "POOL", "mint": WSOL, "uiTokenAmount": {"amount": "5010000000", "decimals": 9}}]}}
    p, kind = price_of(tx, "M", 100.0)       # pool sold 0.1 token for 0.01 SOL = $1 -> $10/token
    assert kind == "pool_leg" and abs(p - 10.0) < 1e-9, (p, kind)
    tx["meta"]["err"] = {"x": 1}
    assert price_of(tx, "M", 100.0) == (None, "failed_or_missing")
    assert rel_source(1.1, 1.0) == 10.0 and rel_source(1.0, None) is None
    # A fractional exit and a trade 5 s past +10 min: the +10 min mark must exist.
    after = [{"signature": s, "blockTime": bt, "err": None}
             for s, bt in (("t605", 1605), ("t301", 1301), ("t61", 1061), ("fail", 1062))]
    after[3]["err"] = {"x": 1}
    marks = after_marks(after, 1000.4)
    assert {k: v["signature"] for k, v in marks.items()} == {60: "t61", 300: "t301", 600: "t605"}, marks
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--sol-usd", type=float, default=121.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    for p in load_positions(args.ids):
        try:
            print(json.dumps(analyse(p, args.sol_usd)), flush=True)
        except Stop as exc:
            print(json.dumps({"id": p["id"], "STOPPED": str(exc)}), flush=True)
            return


if __name__ == "__main__":
    main()
