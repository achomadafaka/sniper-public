#!/usr/bin/env python3
"""Grade every slot-lane buy that failed on its signed minimum output: what would filling have done?

Operator, 2026-09-28 ("we need to fill these", "these blocks are most certainly incorrect"), after
DegenCapitalLLC on BcoDTtWh...pump: the lane landed in the source's own slot at index 1308, 12 buys
and 43.27 SOL landed between the source (index 312) and us, the price had moved 2.70x, and the
transaction failed BuySlippageBelowMinTokensOut. Alpha's order: grade every such failure since
09-15, with the fallback lanes' verdicts and a control of slot-lane fills.

Per failure:
  multiple      the price our clip would have paid over the price the lane signed against (the
                source's post-trade price): expected_tokens / available_tokens from the trace
                (slot_price_limit, slot_pump_slippage). None when the trace lacks either.
  anchor        the average price of the last successful SOL-quoted trade before our transaction
                in block order: an approximation of the price we met (an average fill sits below
                its own post-trade price).
  +60s / +120s  median SOL-quoted trade price in +-10 slots around S + t/0.268 s, over the anchor.
  max / min     over every SOL-quoted trade after the source slot inside the window read.
  fallback      every other attempt by the same wallet on the same coin within 120 s, and why.
The control is the slot lane's funded fills: the multiple they paid (total_copy_drift_pct, fill
against the post-trade price) and their realized return.

SOL-quoted trades only: dump_forensics.trade_of prices a USDC leg at 1.0 and a SOL leg at sol_usd,
so a trade is kept only when decoding at sol_usd 1 and 1000 scales its USD `price` 1000x. (The first
cut tested the `sol` field, which is constant on a SOL leg, and dropped every trade.)

Research key only (funded_exit_truth.rpc), one job, <= --pages pages per coin. Before every page
the funded book must be flat on Solana (0 open real positions, 0 reserved entries); while it is
not, the run waits, and it stops after 30 minutes of waiting. Read-only.

    python3 scripts/research/slot_minout_grading.py --since 2026-09-15
    python3 scripts/research/slot_minout_grading.py --selftest
"""
import argparse
import contextlib
import json
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))

SLOT_S = 0.268                 # measured Solana slot time since ~09-18 (268 ms, not 400)
LEDGER = str(REPO / "sniper_tracker.db")


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def window_median(points, slot, half=10):
    v = [p for s, p in points if slot - half <= s <= slot + half]
    return statistics.median(v) if v else None


def sol_quoted(trade_1, trade_1000):
    """True when the USD price scaled 1000x between the decodes at sol_usd 1 and 1000: the quote
    leg is SOL. A USDC leg keeps its price and shrinks its `sol` instead."""
    if not trade_1 or not trade_1000 or not trade_1.get("price") or not trade_1000.get("price"):
        return False
    return abs(trade_1000["price"] / trade_1["price"] - 1000.0) < 1.0


def flat(conn_fn):
    with contextlib.closing(conn_fn()) as c:
        op = c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                       "AND is_open=1 AND network_id='solana' AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]
        rs = c.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
                       "AND network_id='solana'").fetchone()[0]
    return op == 0 and rs == 0


def wait_flat(conn_fn, max_wait_s=1800):
    waited = 0
    while not flat(conn_fn):
        if waited >= max_wait_s:
            raise SystemExit("stopped: book not flat for %d s" % waited)
        time.sleep(20)
        waited += 20


def trace_facts(trace_json):
    ev = (json.loads(trace_json or "{}").get("substage_events") or [])
    by = {}
    for e in ev:
        by.setdefault(e.get("name"), e)
    lim = by.get("slot_price_limit") or {}
    slip = by.get("slot_pump_slippage") or {}
    sent = by.get("slot_send_call") or {}
    expected, available = lim.get("expected_tokens_atomic"), slip.get("available_tokens_atomic")
    multiple = (expected / available) if expected and available else None
    return {"our_sig": sent.get("tx_sig"), "multiple": multiple,
            "landing_slot": slip.get("landing_slot")}


def failures(conn, since):
    rows = conn.execute(
        "SELECT attempt_id, origin_event_id, mint, source_user_id, source_handle, "
        "decision_started_at, terminal_reason, tx_sig, trace_json FROM memecoin_entry_attempts "
        "WHERE signal_source='slot_lane' AND decision_started_at>=? "
        "AND terminal_reason LIKE '%BuySlippageBelowMin%' ORDER BY decision_started_at",
        (since,)).fetchall()
    out = []
    for r in rows:
        f = trace_facts(r["trace_json"])
        t0 = utc(r["decision_started_at"])
        fb = conn.execute(
            "SELECT signal_source, terminal_status, terminal_reason, decision_started_at "
            "FROM memecoin_entry_attempts WHERE mint=? AND source_user_id=? AND attempt_id<>? "
            "AND decision_started_at>=? AND decision_started_at<? ORDER BY decision_started_at",
            (r["mint"], r["source_user_id"], r["attempt_id"], t0.isoformat()[:19],
             (t0 + timedelta(seconds=120)).isoformat())).fetchall()
        origin = str(r["origin_event_id"] or "")
        out.append({"attempt": r["attempt_id"][:8], "mint": r["mint"],
                    "wallet": r["source_handle"], "at": r["decision_started_at"][:19],
                    "reason": str(r["terminal_reason"]).split(":")[-1],
                    "source_sig": origin.split(":", 1)[1] if origin.startswith("slotlane:") else None,
                    "our_sig": r["tx_sig"] or f["our_sig"], "multiple": f["multiple"],
                    "fallback": ["%s %s %s" % (x["signal_source"], x["terminal_status"],
                                               str(x["terminal_reason"])[:48]) for x in fb]})
    return out


def grade(f, pages, conn_fn):
    import funded_exit_truth as fet
    import dump_forensics as dfx
    fet.SPACING_S = 0.5
    src = dfx.get_tx(f["source_sig"]) if f["source_sig"] else None
    if not src:
        return dict(f, status="source_not_found")
    s = src["slot"]
    hi = s + int(125 / SLOT_S)
    txs, token, n = [], None, 0
    while n < pages:
        wait_flat(conn_fn)
        opts = {"transactionDetails": "full", "sortOrder": "asc", "limit": 100, "encoding": "json",
                "maxSupportedTransactionVersion": 1, "commitment": "confirmed",
                "filters": {"slot": {"gte": s, "lte": hi}}}
        if token:
            opts["paginationToken"] = token
        res = fet.rpc("getTransactionsForAddress", [f["mint"], opts]) or {}
        page = res.get("data") or []
        txs.extend(page)
        n += 1
        token = res.get("paginationToken")
        if not token or len(page) < 100:
            token = None
            break
    points, ours, last_before = [], None, None
    for tx in txs:
        sig = tx["transaction"]["signatures"][0]
        if sig == f["our_sig"]:
            ours = (tx["slot"], tx["transactionIndex"])
    for tx in txs:
        if (tx.get("meta") or {}).get("err") is not None:
            continue
        t1, t1000 = dfx.trade_of(tx, f["mint"], 1.0), dfx.trade_of(tx, f["mint"], 1000.0)
        if not sol_quoted(t1, t1000) or not t1.get("tokens"):
            continue
        key, px = (tx["slot"], tx["transactionIndex"]), t1["sol"] / t1["tokens"]
        points.append((tx["slot"], px))
        if ours and key < ours:
            last_before = px
    last_slot = txs[-1]["slot"] if txs else s
    out = dict(f, status="ok" if ours and last_before else "ours_not_found" if not ours
               else "no_trade_before_us", source_slot=s, our_slot=ours[0] if ours else None,
               truncated=token is not None and last_slot < s + int(120 / SLOT_S), pages=n)
    if last_before:
        for t in (60, 120):
            m = window_median(points, s + int(t / SLOT_S))
            out["r%d" % t] = round(m / last_before, 3) if m else None
        after = [p for sl, p in points if sl > s]
        out["max"] = round(max(after) / last_before, 3) if after else None
        out["min"] = round(min(after) / last_before, 3) if after else None
    return out


def control(conn):
    rows = conn.execute("SELECT id, total_copy_drift_pct, realized_pnl, entry_notional, exit_reason "
                        "FROM memecoin_positions WHERE execution_mode='real' AND signal_source='slot_lane' "
                        "AND is_open=0 AND entry_notional>0").fetchall()
    return [{"id": r["id"], "multiple": 1 + (r["total_copy_drift_pct"] or 0) / 100.0,
             "ret": 100 * r["realized_pnl"] / r["entry_notional"], "exit": r["exit_reason"]}
            for r in rows if r["realized_pnl"] is not None]


def selftest():
    pts = [(100, 1.0), (105, 3.0), (130, 9.0)]
    assert window_median(pts, 102) == 2.0 and window_median(pts, 200) is None
    # Shapes as dump_forensics.trade_of returns them: a SOL leg scales `price`, a USDC leg does not.
    assert sol_quoted({"sol": 2.0, "price": 3e-8}, {"sol": 2.0, "price": 3e-5})
    assert not sol_quoted({"sol": 2.0, "price": 3e-6}, {"sol": 0.002, "price": 3e-6})
    f = trace_facts(json.dumps({"substage_events": [
        {"name": "slot_price_limit", "expected_tokens_atomic": 11118126416148},
        {"name": "slot_pump_slippage", "available_tokens_atomic": 4246701860116},
        {"name": "slot_send_call", "tx_sig": "abc"}]}))
    assert f["our_sig"] == "abc" and abs(f["multiple"] - 2.618) < 0.001
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2026-09-15")
    ap.add_argument("--pages", type=int, default=20)
    ap.add_argument("--out", default=str(REPO / "logs" / "acceptance" / "slot_minout_grading.jsonl"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    from sniper.persistence.db_core import get_readonly_connection
    conn_fn = lambda: get_readonly_connection(LEDGER)
    with contextlib.closing(conn_fn()) as c:
        todo, ctl = failures(c, args.since), control(c)
    wait_flat(conn_fn)
    import slot_lane_send_monitor as mon
    from sniper.execution.solana_rpc_pool import key_id
    print("research key %s; failures %d; control fills %d" % (key_id(mon.rpc_url()), len(todo), len(ctl)))
    graded = []
    with open(args.out, "w") as fh:
        for f in todo:
            g = grade(f, args.pages, conn_fn)
            fh.write(json.dumps(g) + "\n")
            graded.append(g)
            print("  %s %s %-16s %-26s mult %s r60 %s r120 %s max %s min %s trunc %s %s | fallback: %s" % (
                g["at"], g["attempt"], (g["wallet"] or "?")[:16], g["reason"][:26],
                "%.2f" % g["multiple"] if g.get("multiple") else "-", g.get("r60"), g.get("r120"),
                g.get("max"), g.get("min"), g.get("truncated"), g["status"],
                "; ".join(g["fallback"]) or "none"))
    ok = [g for g in graded if g.get("r120")]
    if ok:
        r = sorted(g["r120"] for g in ok)
        print("r120 over %d graded: median %.3f; >1 on %d" % (len(r), statistics.median(r),
                                                              sum(1 for x in r if x > 1)))
    print("control, slot-lane funded fills: n=%d, multiple p50 %.3f max %.3f, return p50 %+.1f%%" % (
        len(ctl), statistics.median(x["multiple"] for x in ctl), max(x["multiple"] for x in ctl),
        statistics.median(x["ret"] for x in ctl)) if ctl else "control: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
