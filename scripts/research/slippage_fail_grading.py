#!/usr/bin/env python3
"""Would the Solana buys that failed on slippage have paid if filled at the worse price?

Operator via Alpha, 2026-09-30 ("TRANSACTION FAILED ... Price moved beyond limit", DegenCapitalLLC
c2f5b06a): should we have filled, and should botted wallets get a wider slippage exception?
Pre-registered as brain episode dc950dc7 before any post-failure price was read.

POPULATION: Solana attempts decided since --since with terminal_status 'failed' and a slippage-class
reason (BuySlippageBelowMin*, Jupiter 0x1771 named or bare, 'exceeds desired slippage limit').
SHORTFALL: available / min tokens from the trace (slot_pump_slippage), else the tx's Left/Right log.
REQUIRED CAP over the source price = (min / available) * (1 + cap_pct/100) - 1 (slot_price_limit).
ENTRY p0 (as if filled at the AVAILABLE amount): spend SOL / available tokens; without `available`,
the last successful SOL-quoted trade before our tx in block order ('anchor'). Units check: where
both exist, p0 / anchor must lie in NEAR, else the row is flagged and excluded.
INSTRUMENT: the mint's successful SOL-quoted trades after our tx (getTransactionsForAddress on the
research key, one job, every page waits for a flat funded Solana book), return px / p0 - 1 over
blockTime age; cap_refusal_outcomes.grade (TP +25%, stop -35%, else nearest +300 s within 20 s,
else censored). Chart space: no exit impact, so optimistic for every row alike.
CONTROL: the same wallets' funded Solana fills since --since, p0 = our own fill tx's price, same
instrument; secondary realized_pnl / entry_notional.
FEE: meta.fee of our failed tx (trace fee_lamports when the tx is unreadable), USD at the trace's
implied SOL rate. CROWD: companions after the source in memecoin_wallet_slot_reads, where read.

    python3 scripts/research/slippage_fail_grading.py [--since 2026-09-13] [--pages 15] [--no-control]
    python3 scripts/research/slippage_fail_grading.py --report logs/acceptance/slippage_fail_grading.jsonl
    python3 scripts/research/slippage_fail_grading.py --selftest
"""
import argparse
import collections
import contextlib
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]

from cap_refusal_outcomes import MARK_S, TOL_S, grade, mint_ci  # noqa: E402

LEDGER = REPO / "sniper_tracker.db"
GRAPH = REPO / "data" / "memecoin_wallet_graph.db"
SLOT_S = 0.268
PRE_SLOTS = 40                                   # read a little before our slot for the anchor
POST_SLOTS = int((MARK_S + TOL_S + 10) / SLOT_S)
NEAR = (0.67, 1.5)
CAPS = (35, 40, 50, 75, 100)
SEED = 20261002
SLIP_WHERE = ("(terminal_reason LIKE '%BuySlippageBelowMin%' OR terminal_reason LIKE '%0x1771%' "
              "OR terminal_reason LIKE '%slippage tolerance exceeded%' "
              "OR terminal_reason LIKE '%exceeds desired slippage limit%')")
LEFT_RIGHT = re.compile(r"(Left|Right):\s*(\d+)")


def ro(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def trace_facts(trace_json):
    by = {}
    for e in (json.loads(trace_json or "{}").get("substage_events") or []):
        if isinstance(e, dict):
            by.setdefault(e.get("name"), e)
    lim, slip = by.get("slot_price_limit") or {}, by.get("slot_pump_slippage") or {}
    spend, cost = lim.get("spend_lamports"), lim.get("cost_usdc_atomic")
    return {"available": slip.get("available_tokens_atomic"),
            "min": slip.get("min_tokens_atomic") or lim.get("min_tokens_atomic"),
            "expected": lim.get("expected_tokens_atomic"), "cap_pct": lim.get("cap_pct"),
            "decimals": lim.get("base_decimals"), "spend_sol": spend / 1e9 if spend else None,
            "clip_usd": cost / 1e6 if cost else None,
            "sol_usd": (cost / 1e6) / (spend / 1e9) if cost and spend else None,
            "trace_fee_lamports": slip.get("fee_lamports")}


def required_cap(mn, available, cap_pct):
    """Cap over the source price that a fill at `available` would have needed, in percent."""
    if not mn or not available or cap_pct is None:
        return None
    return 100.0 * ((mn / available) * (1 + cap_pct / 100.0) - 1)


def left_right(logs):
    """(left, right) from an anchor require_* log, e.g. pump's min-out check, or (None, None)."""
    vals = dict((k, int(v)) for k, v in LEFT_RIGHT.findall(" ".join(logs or [])))
    return vals.get("Left"), vals.get("Right")


def failures(conn, since):
    out = []
    for r in conn.execute(
            # The 09-26 rows keep their signature only on the reservation (a47a6c14 audit).
            "SELECT a.attempt_id, a.origin_event_id, a.mint, a.source_user_id, a.source_handle, "
            "a.decision_started_at, a.terminal_reason, COALESCE(a.tx_sig, pe.tx_sig) AS tx_sig, a.trace_json "
            "FROM memecoin_entry_attempts a LEFT JOIN memecoin_pending_entries pe ON pe.attempt_id=a.attempt_id "
            "WHERE a.network_id='solana' AND a.decision_started_at>=? AND a.terminal_status='failed' AND "
            + SLIP_WHERE.replace("terminal_reason", "a.terminal_reason") + " ORDER BY a.decision_started_at", (since,)):
        f = trace_facts(r["trace_json"])
        f.update(kind="fail", attempt=r["attempt_id"], mint=r["mint"], wallet=r["source_handle"],
                 wallet_id=r["source_user_id"], at=r["decision_started_at"], origin=r["origin_event_id"],
                 reason=str(r["terminal_reason"]).replace("chain_error:", ""), sig=r["tx_sig"])
        out.append(f)
    return out


def control_fills(conn, since, wallets):
    q = ",".join("?" * len(wallets))
    rows = conn.execute(
        "SELECT p.id, p.mint, p.source_handle, p.source_user_id, p.opened_at, p.entry_notional, "
        "p.realized_pnl, p.is_open, COALESCE(a.tx_sig, pe.tx_sig) AS sig FROM memecoin_positions p "
        "LEFT JOIN memecoin_entry_attempts a ON a.attempt_id=p.attempt_id "
        "LEFT JOIN memecoin_pending_entries pe ON pe.attempt_id=p.attempt_id "
        "WHERE p.execution_mode='real' AND p.network_id='solana' AND p.opened_at>=? "
        "AND p.source_user_id IN (%s) ORDER BY p.opened_at" % q, (since, *wallets)).fetchall()
    return [{"kind": "fill", "position": r["id"], "mint": r["mint"], "wallet": r["source_handle"],
             "wallet_id": r["source_user_id"], "at": r["opened_at"], "sig": r["sig"],
             "clip_usd": r["entry_notional"],
             "realized": (r["realized_pnl"] / r["entry_notional"]
                          if not r["is_open"] and r["realized_pnl"] is not None and r["entry_notional"]
                          else None)} for r in rows]


def crowd(graph, row):
    """Followers after the source in its slot read, or None when the source slot was not read."""
    origin = str(row.get("origin") or "")
    sig = origin.split(":", 1)[1] if origin.startswith(("slotlane:", "onchain:")) else None
    rec = graph.execute("SELECT idx, companions FROM memecoin_wallet_slot_reads WHERE signature=? "
                        "AND status='ok'", (sig,)).fetchone() if sig else None
    if rec is None:
        # ISO bounds built here: SQLite datetime() uses a space separator and would loosen the bound.
        at = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
        rec = graph.execute(
            "SELECT idx, companions FROM memecoin_wallet_slot_reads WHERE source_user_id=? AND mint=? "
            "AND status='ok' AND detected_at<=? AND detected_at>=? ORDER BY detected_at DESC LIMIT 1",
            (row["wallet_id"], row["mint"], at.isoformat(), (at - timedelta(seconds=600)).isoformat())).fetchone()
    if rec is None or rec["idx"] is None:
        return None
    return sum(1 for c in json.loads(rec["companions"] or "[]")
               if c.get("index") is not None and c["index"] > rec["idx"])


def sol_trade(dfx, tx, mint):
    """(SOL per token) of a successful SOL-quoted trade on `mint`, else None."""
    t1, t1000 = dfx.trade_of(tx, mint, 1.0), dfx.trade_of(tx, mint, 1000.0)
    if not t1 or not t1000 or not t1.get("tokens") or not t1.get("price") or not t1000.get("price"):
        return None
    if abs(t1000["price"] / t1["price"] - 1000.0) >= 1.0:
        return None
    return t1["sol"] / t1["tokens"]


def gtfa(fet, mint, lo, hi, *, succeeded, order="asc", token=None):
    opts = {"transactionDetails": "full", "sortOrder": order, "limit": 100, "encoding": "json",
            "maxSupportedTransactionVersion": 1, "commitment": "confirmed",
            "filters": {"slot": {"gte": lo, "lte": hi}}}
    if succeeded:
        # Failed bot buys filled 40 pages on busy coins in the first run and censored 9 rows.
        opts["filters"]["status"] = "succeeded"
    if token:
        opts["paginationToken"] = token
    res = fet.rpc("getTransactionsForAddress", [mint, opts]) or {}
    return res.get("data") or [], res.get("paginationToken")


def observe(row, pages, conn_fn):
    """Read our tx, the price we met, and the mint's successful trades after it; grade.

    Three reads: our slot at any status (our transactionIndex and the in-slot anchor), the 40
    slots before it newest-first when our slot has no earlier trade (anchor), then the successful
    trades after us until the first TP/stop touch or +320 s."""
    import dump_forensics as dfx
    import funded_exit_truth as fet
    from slot_minout_grading import wait_flat
    fet.SPACING_S = 0.5
    wait_flat(conn_fn)
    ours = dfx.get_tx(row["sig"]) if row.get("sig") else None
    if not ours:
        return dict(row, status="tx_not_found")
    meta, slot, t0 = ours.get("meta") or {}, ours["slot"], ours.get("blockTime")
    row = dict(row, fee_lamports=meta.get("fee"), our_slot=slot, our_time=t0)
    if row["kind"] == "fail" and not row.get("available"):
        left, right = left_right(meta.get("logMessages"))
        if left and right:
            row["log_left"], row["log_right"] = left, right
    n, key, anchor, own_px, token = 0, None, None, None, None
    same = []
    while n < 5:                                   # our slot, any status
        wait_flat(conn_fn)
        page, token = gtfa(fet, row["mint"], slot, slot, succeeded=False, token=token)
        n += 1
        same.extend(page)
        if not token or len(page) < 100:
            break
    for t in same:
        if t["transaction"]["signatures"][0] == row["sig"]:
            key = (t["slot"], t["transactionIndex"])
            if row["kind"] == "fill":
                own_px = sol_trade(dfx, t, row["mint"])
    row["our_found"] = key is not None
    key = key or (slot, 10 ** 9)                   # not listed: every same-slot trade counts as before us
    for t in same:
        if (t.get("meta") or {}).get("err") is None and (t["slot"], t["transactionIndex"]) < key:
            anchor = sol_trade(dfx, t, row["mint"]) or anchor
    if anchor is None:
        wait_flat(conn_fn)
        page, _ = gtfa(fet, row["mint"], slot - PRE_SLOTS, slot - 1, succeeded=True, order="desc")
        n += 1
        anchor = next((px for px in (sol_trade(dfx, t, row["mint"]) for t in page) if px), None)
    if row["kind"] == "fill":
        p0, basis = own_px, "own_fill"
    elif row.get("available") and row.get("spend_sol") and row.get("decimals") is not None:
        p0, basis = row["spend_sol"] / (row["available"] / 10 ** row["decimals"]), "available"
    else:
        p0, basis = anchor, "anchor"
    row.update(anchor=anchor, p0=p0, p0_basis=basis)
    if basis == "available" and anchor:
        row["p0_over_anchor"] = p0 / anchor
        if not NEAR[0] <= p0 / anchor <= NEAR[1]:
            return dict(row, status="units_flag", pages=n)
    if not p0 or not t0:
        return dict(row, status="no_price", pages=n)
    obs, token, truncated = [], None, False
    while True:                                    # successful trades after us
        if n >= pages:
            truncated = True
            break
        wait_flat(conn_fn)
        page, token = gtfa(fet, row["mint"], slot, slot + POST_SLOTS, succeeded=True, token=token)
        n += 1
        for t in page:
            if (t["slot"], t["transactionIndex"]) <= key or not t.get("blockTime"):
                continue
            px = sol_trade(dfx, t, row["mint"])
            if px is not None:
                obs.append((t["blockTime"] - t0, px))
        if not token or len(page) < 100:
            break
        if grade([(a, px / p0 - 1) for a, px in obs])[1] in ("tp", "stop"):
            break                                  # decided at the first touch
    ret, how = grade([(a, px / p0 - 1) for a, px in obs])
    return dict(row, status="ok", ret=ret, how=how, pages=n, truncated=truncated, n_obs=len(obs),
                last_age=obs[-1][0] if obs else None)


def summarize(rows, label, out=print):
    g = [r for r in rows if r.get("ret") is not None]
    if not g:
        out("  %-40s graded 0 of %d" % (label, len(rows)))
        return None
    mean = statistics.mean(r["ret"] for r in g)
    lo, hi = mint_ci([(r["mint"], r["ret"]) for r in g], seed=SEED) if len(g) >= 5 else (None, None)
    usd = sum(r["ret"] * (r.get("clip_usd") or 0) for r in g)
    out("  %-40s graded %2d of %2d  mean %+6.1f%%  CI %s  sum $%+8.2f  tp/stop/mark %s" % (
        label, len(g), len(rows), 100 * mean,
        "[%+.1f, %+.1f]" % (100 * lo, 100 * hi) if lo is not None else "(n<5)", usd,
        "/".join(str(sum(r["how"] == h for r in g)) for h in ("tp", "stop", "mark"))))
    return mean, lo, hi, len(g)


def report(rows):
    fails = [r for r in rows if r["kind"] == "fail"]
    fills = [r for r in rows if r["kind"] == "fill"]
    clips = [r["clip_usd"] for r in fails if r.get("clip_usd")]
    med_clip = statistics.median(clips) if clips else None
    for r in fails:
        if not r.get("clip_usd"):
            r["clip_usd"], r["clip_basis"] = med_clip, "median"
    print("FAILURES: %d; status %s" % (len(fails), dict(collections.Counter(r["status"] for r in fails))))
    print("  p0 basis %s; units p0/anchor %s" % (
        dict(collections.Counter(r.get("p0_basis") for r in fails)),
        sorted(round(r["p0_over_anchor"], 2) for r in fails if r.get("p0_over_anchor"))))
    rates = [r["sol_usd"] for r in fails if r.get("sol_usd")]
    sol_usd = statistics.median(rates) if rates else None
    fee = sum((r.get("fee_lamports") or r.get("trace_fee_lamports") or 0) for r in fails) / 1e9
    print("  fee burned: %.4f SOL = $%.2f (at median implied SOL $%.1f); fee known on %d of %d" % (
        fee, fee * (sol_usd or 0), sol_usd or 0,
        sum(1 for r in fails if r.get("fee_lamports") or r.get("trace_fee_lamports")), len(fails)))
    print("\nPER FAILURE (required cap over source price; shortfall avail/min; crowd = followers after source)")
    for r in fails:
        short = (r["available"] / r["min"]) if r.get("available") and r.get("min") else None
        print("  %s %s %-16s %-34s req %s short %s crowd %s | %s %s %s" % (
            r["at"][:16], r["attempt"][:8], (r["wallet"] or "?")[:16], r["reason"][:34],
            "%+6.1f%%" % r["req_cap"] if r.get("req_cap") is not None else "     -",
            "%.2f" % short if short else "  - ", r.get("crowd"), r["status"], r.get("how") or "",
            "%+.1f%%" % (100 * r["ret"]) if r.get("ret") is not None else ""))
    print("\nPOOLED")
    summarize(fails, "all failures, filled at available")
    summarize(fills, "control: same wallets' fills, same instrument")
    rz = [r for r in fills if r.get("realized") is not None]
    if rz:
        print("  %-40s n %d mean %+.1f%%" % ("control: realized / notional", len(rz),
                                              100 * statistics.mean(r["realized"] for r in rz)))
    for cap in CAPS:
        summarize([r for r in fails if r.get("req_cap") is not None and r["req_cap"] <= cap],
                  "failures with required cap <= %d%%" % cap)
    print("\nPER WALLET (failures | own fills, same instrument | realized)")
    for w, n in collections.Counter(r["wallet"] for r in fails).most_common():
        summarize([r for r in fails if r["wallet"] == w], "%s failures" % w)
        summarize([r for r in fills if r["wallet"] == w], "%s fills" % w)
        rz = [r["realized"] for r in fills if r["wallet"] == w and r.get("realized") is not None]
        if rz:
            print("  %-40s n %d mean %+.1f%%" % ("%s realized" % w, len(rz), 100 * statistics.mean(rz)))


def selftest():
    assert abs(required_cap(3337170587854, 2471353520672, 30.0) - 75.55) < 0.05
    assert required_cap(None, 1, 30.0) is None
    assert left_right(["Program log: Left: 12", "Program log: Right: 30"]) == (12, 30)
    f = trace_facts(json.dumps({"substage_events": [
        {"name": "slot_price_limit", "spend_lamports": 845778644, "cost_usdc_atomic": 101420000,
         "base_decimals": 6, "cap_pct": 30.0, "min_tokens_atomic": 3337170587854},
        {"name": "slot_pump_slippage", "available_tokens_atomic": 2471353520672, "fee_lamports": 993000}]}))
    assert abs(f["sol_usd"] - 119.91) < 0.01 and f["available"] == 2471353520672
    assert abs(f["spend_sol"] / (f["available"] / 1e6) - 3.4223e-7) < 1e-10
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2026-09-13")
    ap.add_argument("--pages", type=int, default=15)
    ap.add_argument("--no-control", action="store_true")
    ap.add_argument("--attempts", help="comma-separated attempt_id prefixes: grade only these failures")
    ap.add_argument("--out", default=str(REPO / "logs" / "acceptance" / "slippage_fail_grading.jsonl"))
    ap.add_argument("--report", help="summarise an existing jsonl without any network read")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.report:
        report([json.loads(x) for x in open(args.report)])
        return 0
    from sniper.util.envfiles import load_env_files
    load_env_files(str(REPO))
    conn_fn = lambda: ro(LEDGER)          # noqa: E731
    with contextlib.closing(conn_fn()) as c, contextlib.closing(ro(GRAPH)) as graph:
        todo = failures(c, args.since)
        if args.attempts:
            keep = tuple(x.strip() for x in args.attempts.split(",") if x.strip())
            todo = [r for r in todo if r["attempt"].startswith(keep)]
        for r in todo:
            r["req_cap"] = required_cap(r.get("min"), r.get("available"), r.get("cap_pct"))
            r["crowd"] = crowd(graph, r)
        wallets = sorted({r["wallet_id"] for r in todo if r.get("wallet_id")})
        ctl = [] if args.no_control else control_fills(c, args.since, wallets)
    print("failures %d, control fills %d" % (len(todo), len(ctl)), flush=True)
    rows = []
    with open(args.out, "w") as fh:
        for r in todo + ctl:
            import funded_exit_truth as fet
            try:
                g = observe(r, args.pages, conn_fn)
            except fet.Stop as exc:
                print("STOP: %s" % exc, flush=True)
                break
            if g["kind"] == "fail" and not g.get("available") and g.get("log_left") and g.get("log_right"):
                # A failed min-out has available < min whichever side the program logs first.
                g["available"], g["min"] = sorted((g["log_left"], g["log_right"]))
                g["req_cap"] = required_cap(g["min"], g["available"], g.get("cap_pct"))
            fh.write(json.dumps(g) + "\n")
            fh.flush()
            rows.append(g)
            print("  %s %-5s %-16s %s %s" % (g["at"][:16], g["kind"], (g["wallet"] or "?")[:16], g["status"],
                                           "%+.1f%% %s" % (100 * g["ret"], g["how"]) if g.get("ret") is not None
                                           else g.get("how") or ""), flush=True)
    report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
