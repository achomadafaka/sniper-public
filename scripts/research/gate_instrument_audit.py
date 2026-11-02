#!/usr/bin/env python3
"""Gate instrument audit: do our entry gates measure what we think, and block what we think?

Operator 2026-09-28 via Alpha: "we are still unsure if our measurements make sense and if we
actually block what we think we are blocking". For each blocking gate on the funded path, by 7-day
refusal count, chains never pooled:
  inputs     what the gate read, from the refusal's own trace (classification facts and substages)
  recompute  the gate's number rebuilt from evidence that does not come from the gate's code path
             (chain, ledger, captures) on a seeded sample, with the mismatch rate
  outcome    the refusal-capture counterfactual (TP 25 / stop 35 on exact whole-bag sell quotes,
             bootstrap CI) beside our own fills graded the SAME way on memecoin_route_series

Subcommands (each prints and exits; `chain` needs the research key and a flat funded Solana book):
    counts | inputs | ledger | outcome | chain
    python3 scripts/research/gate_instrument_audit.py outcome --since 2026-09-21
    python3 scripts/research/gate_instrument_audit.py --selftest

Known biases of the capture counterfactual, corrected or stated per row:
  * entry price: the capture buys minutes after the refusal (buy age p50 is printed) at its own
    quote. `outcome` re-bases each capture to our fills' median premium over the source.
  * size: captures buy $20; our fills are $100-175. Smaller clips pay less impact on both legs, so
    the capture side is flattered by an amount this script cannot remove. Stated, not corrected.
"""
import argparse
import collections
import contextlib
import json
import random
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
CAPS = "file:%s?mode=ro" % (REPO / "data" / "memecoin_refusal_captures.db")
LEDGER = str(REPO / "sniper_tracker.db")
TP, SL = 0.25, -0.35
QUOTE_GATES = {"chase_quote", "chase_past_entry", "chase_first_seen", "roundtrip_cap", "impact_cap",
               "route_over_mid", "route_over_source", "evm_chase_quote"}
SEED = 20260928
NUM = re.compile(r"[+-]?\$?[0-9][0-9,.]*%?")


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def ledger():
    from sniper.persistence.db_core import get_readonly_connection
    return contextlib.closing(get_readonly_connection(LEDGER))


def gate_of(reason, status=None):
    from sniper.services.memecoin_refusal_capture import classify_gate
    g = classify_gate(reason)
    if g:
        return g
    text = str(reason or status or "")
    for pat, name in ((r"^mcap \$[\d,]+ over ceiling", "mcap_over_ceiling_far"),
                      (r"^source buy .*under absolute floor", "source_buy_abs_floor"),
                      (r"^h1 organic volume .*under floor", "h1_volume_under_floor"),
                      (r"^one position per coin", "one_position_per_coin"),
                      (r"^unresolved signed transaction", "unresolved_signed_tx"),
                      (r"^coin_too_young", "coin_too_young"), (r"^below min-trade floor", "below_min_trade"),
                      (r"^no quote", "no_quote"), (r"^not_approved", "not_approved"),
                      (r"^unroutable_chain", "unroutable_chain"), (r"^quiet_hours", "quiet_hours"),
                      (r"^daily loss limit", "daily_loss_latch"), (r"^entry older than", "entry_age"),
                      (r"^route build refused", "route_build_refused"), (r"^kill_switch", "kill_switch")):
        if re.match(pat, text):
            return name
    return "other:" + NUM.sub("N", text.split(":")[0])[:40]


def boot_mean(xs, rng, draws=2000):
    b = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(draws))
    return b[int(0.025 * draws)], b[int(0.975 * draws) - 1]


def path_outcome(path):
    """TP / stop at the first quote that crosses, else the last quote. path = [(age_s, return)]."""
    for _, r in path:
        if r >= TP:
            return TP
        if r <= SL:
            return SL
    return path[-1][1]


def dedupe(caps):
    """Keep one capture per coin while an earlier kept capture's horizon still covers it."""
    last_end, kept = {}, []
    for c in sorted(caps, key=lambda c: c["refused"]):
        end = last_end.get(c["mint"])
        if end is not None and c["refused"] < end:
            continue
        last_end[c["mint"]] = c["refused"] + timedelta(seconds=c["horizon_s"])
        kept.append(c)
    return kept


# --- counts ---------------------------------------------------------------------------------------
def cmd_counts(args):
    with ledger() as c:
        rows = c.execute("SELECT network_id, terminal_status, terminal_reason FROM memecoin_entry_attempts "
                         "WHERE decision_started_at>=? AND terminal_status IN ('refused','aborted','rejected')",
                         (args.since,)).fetchall()
    cnt = collections.Counter((gate_of(r["terminal_reason"], r["terminal_status"]), r["network_id"]) for r in rows)
    tot = collections.Counter()
    for (g, _), v in cnt.items():
        tot[g] += v
    print("refusals since %s: %d" % (args.since, len(rows)))
    for g, v in tot.most_common(40):
        per = ", ".join("%s %d" % (n, cnt[(g, n)]) for n in ("solana", "4663", "56", "8453", "5042", "1", "143")
                        if cnt[(g, n)])
        print("  %5d  %-32s %s" % (v, g, per))


# --- inputs: what each gate read, from its own refusal trace -------------------------------------
INPUT_KEYS = ("effective_mcap", "mcap_basis", "source_size_usd", "effective_floor_usd", "floor_usd",
              "decided_organic_volume_m5", "decided_organic_volume_h1", "coverage", "coverage_scope",
              "gap_pct", "cap_pct", "source_price", "quote_price", "liquidity_source")


def trace_inputs(trace_json, classification_json):
    out = {}
    try:
        facts = (json.loads(classification_json or "{}").get("facts") or {})
    except ValueError:
        facts = {}
    for k in ("effective_mcap", "mcap_basis", "liquidity_source", "volume_h1", "volume_m5", "pair_age_s"):
        if k in facts:
            out["facts." + k] = facts[k]
    try:
        events = json.loads(trace_json or "{}").get("substage_events") or []
    except ValueError:
        events = []
    for e in events:
        for k in INPUT_KEYS:
            if k in e and ("sub." + k) not in out:
                out["sub." + k] = e[k]
        if "activity_pick" in e and isinstance(e["activity_pick"], dict):
            out["pick.liquidity_usd"] = e["activity_pick"].get("liquidity_usd")
            out["pick.price"] = e["activity_pick"].get("price")
    return out


def cmd_inputs(args):
    with ledger() as c:
        rows = c.execute("SELECT network_id, terminal_status, terminal_reason, source_price, source_market_cap, "
                         "trace_json, classification_json FROM memecoin_entry_attempts WHERE decision_started_at>=? "
                         "AND terminal_status IN ('refused','aborted','rejected')", (args.since,)).fetchall()
    by = collections.defaultdict(list)
    for r in rows:
        by[(gate_of(r["terminal_reason"], r["terminal_status"]), r["network_id"])].append(r)
    for (g, net), lst in sorted(by.items(), key=lambda kv: -len(kv[1]))[:args.top]:
        present = collections.Counter()
        basis = collections.Counter()
        consts = collections.defaultdict(collections.Counter)
        for r in lst:
            inp = trace_inputs(r["trace_json"], r["classification_json"])
            inp["attempt.source_market_cap"] = r["source_market_cap"]
            inp["attempt.source_price"] = r["source_price"]
            for k, v in inp.items():
                if v not in (None, "", 0, 0.0):
                    present[k] += 1
                if k in ("facts.mcap_basis", "facts.liquidity_source", "sub.coverage", "sub.coverage_scope"):
                    basis[(k, v)] += 1
                if k in ("sub.source_size_usd", "attempt.source_market_cap") and v is not None:
                    consts[k][round(float(v), 2)] += 1
        print("\n%s / %s  n=%d" % (g, net, len(lst)))
        print("   present: " + ", ".join("%s %d" % (k, v) for k, v in present.most_common(14)))
        if basis:
            print("   basis:   " + ", ".join("%s=%s %d" % (k.split('.')[-1], v, n) for (k, v), n in basis.most_common(8)))
        for k, cc in consts.items():
            top = cc.most_common(3)
            print("   top values %s: %s (distinct %d)" % (k, ", ".join("%s x%d" % t for t in top), len(cc)))


# --- ledger recomputes ----------------------------------------------------------------------------
def cmd_ledger(args):
    """Rebuild each ledger-decidable gate's verdict from the ledger at decision time."""
    rng = random.Random(SEED)
    with ledger() as c:
        cool = c.execute("SELECT attempt_id, network_id, mint, decision_started_at FROM memecoin_entry_attempts "
                         "WHERE decision_started_at>=? AND terminal_reason LIKE 're-entry cooldown%'",
                         (args.since,)).fetchall()
        sample = rng.sample(cool, min(20, len(cool)))
        miss = []
        for r in sample:
            t0 = utc(r["decision_started_at"])
            last = c.execute("SELECT MAX(closed_at) FROM memecoin_positions WHERE execution_mode='real' AND "
                             "network_id=? AND mint=? AND is_open=0 AND closed_at<?",
                             (r["network_id"], r["mint"], r["decision_started_at"])).fetchone()[0]
            ok = last is not None and (t0 - utc(last)).total_seconds() <= 86400
            if not ok:
                miss.append((r["attempt_id"][:8], r["network_id"], last))
        print("re-entry cooldown: sample %d of %d; no real close in the prior 24 h: %d %s"
              % (len(sample), len(cool), len(miss), miss[:5]))

        one = c.execute("SELECT attempt_id, network_id, mint, decision_started_at, terminal_reason FROM "
                        "memecoin_entry_attempts WHERE decision_started_at>=? AND terminal_reason LIKE "
                        "'one position per coin%'", (args.since,)).fetchall()
        sample = rng.sample(one, min(20, len(one)))
        miss = []
        for r in sample:
            t0 = r["decision_started_at"]
            held = c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND network_id=? "
                             "AND mint=? AND opened_at<=? AND (is_open=1 OR closed_at>?)",
                             (r["network_id"], r["mint"], t0, t0)).fetchone()[0]
            lo = (utc(t0) - timedelta(seconds=180)).isoformat()
            pend = c.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE network_id=? AND mint=? AND "
                             "reserved_at>=? AND reserved_at<=?", (r["network_id"], r["mint"], lo[:19], t0)).fetchone()[0]
            if not held and not pend:
                miss.append((r["attempt_id"][:8], r["network_id"], r["terminal_reason"][:40]))
        print("one position per coin: sample %d of %d; nothing held or reserved at decision: %d %s"
              % (len(sample), len(one), len(miss), miss[:5]))

        unres = c.execute("SELECT attempt_id, network_id, mint, decision_started_at FROM memecoin_entry_attempts "
                          "WHERE decision_started_at>=? AND terminal_reason LIKE 'unresolved signed transaction%'",
                          (args.since,)).fetchall()
        rows = []
        for r in unres:
            t0 = r["decision_started_at"]
            lo = (utc(t0) - timedelta(seconds=120)).isoformat()
            # The guard runs AFTER decision start, so a blocker can broadcast a few ms after it
            # (85503dbd: its slot-lane twin broadcast 38 ms later). Up to +2 s, same coin first.
            hi = (utc(t0) + timedelta(seconds=2)).isoformat()
            p = c.execute("SELECT id, mint, status, broadcast_at, signal_source FROM memecoin_pending_entries WHERE "
                          "network_id=? AND tx_sig IS NOT NULL AND broadcast_at>=? AND broadcast_at<=? "
                          "ORDER BY (mint=?) DESC, broadcast_at DESC LIMIT 1",
                          (r["network_id"], lo[:19], hi, r["mint"])).fetchone()
            rows.append((r["attempt_id"][:8], "same coin" if p and p["mint"] == r["mint"] else
                         "other coin" if p else "no in-flight tx found",
                         p["status"] if p else None, p["signal_source"] if p else None))
        print("unresolved signed tx: %d refusals; blocking tx: %s" % (len(unres), collections.Counter(
            (x[1], x[2], x[3]) for x in rows).most_common()))


# --- outcome --------------------------------------------------------------------------------------
def load_captures(since):
    with contextlib.closing(sqlite3.connect(CAPS, uri=True, timeout=10)) as c:
        rows = c.execute("SELECT capture_id, origin_event_id, network_id, mint, gate, refused_at, age_s, horizon_s, "
                         "quote_side, quote_price, quote_input_usd, quote_output_atomic FROM memecoin_refusal_captures "
                         "WHERE quality='ok' AND quote_side IN ('buy','sell') AND refused_at>=? "
                         # Sell atoms are USDC (1e6) only on Solana; EVM exact rows carry WETH/WBNB.
                         "AND network_id='solana' "
                         "ORDER BY capture_id, age_s", (since,)).fetchall()
    series = collections.defaultdict(list)
    for r in rows:
        series[r[0]].append(r)
    caps = []
    for cid, rs in series.items():
        buy = [r for r in rs if r[8] == "buy"]
        sells = [(r[6], int(r[11]) / 1e6) for r in rs if r[8] == "sell" and r[11]]
        if not buy or not sells or not buy[0][10]:
            continue
        clip = buy[0][10]
        caps.append({"capture": cid, "origin": rs[0][1], "net": rs[0][2], "mint": rs[0][3], "gate": rs[0][4],
                     "refused": utc(rs[0][5]), "horizon_s": rs[0][7], "buy_age_s": buy[0][6],
                     "buy_price": buy[0][9], "clip": clip,
                     "path": [(a, out / clip - 1) for a, out in sorted(sells)]})
    return caps


def our_fills_graded(c, since, horizon_s):
    """Our real Solana fills graded on the same instrument: exact whole-bag exit quotes
    (memecoin_route_series seam exit_decision) within the capture horizon, TP 25 / stop 35."""
    out, realized, prem = [], [], []
    for p in c.execute("SELECT id, entry_notional, realized_pnl, opened_recorded_at, attempt_id FROM "
                       "memecoin_positions WHERE execution_mode='real' AND network_id='solana' AND is_open=0 "
                       "AND opened_at>=? AND entry_notional>0 AND realized_pnl IS NOT NULL", (since,)):
        realized.append(p["realized_pnl"] / p["entry_notional"])
        t0 = p["opened_recorded_at"]
        if not t0:
            continue
        hi = (utc(t0) + timedelta(seconds=horizon_s)).isoformat()
        q = c.execute("SELECT observed_at, expected_usdc FROM memecoin_route_series WHERE position_id=? AND "
                      "seam='exit_decision' AND observed_at<=? ORDER BY id", (p["id"], hi)).fetchall()
        if q:
            out.append(path_outcome([(0, x["expected_usdc"] / p["entry_notional"] - 1) for x in q]))
    for a in c.execute("SELECT fill_price, source_price FROM memecoin_entry_attempts WHERE network_id='solana' "
                       "AND decision_started_at>=? AND position_id IS NOT NULL AND fill_price>0 AND source_price>0",
                       (since,)):
        prem.append(a["fill_price"] / a["source_price"] - 1)
    return out, realized, prem


def cmd_outcome(args):
    rng = random.Random(SEED)
    caps = load_captures(args.since)
    with ledger() as c:
        src = {}
        for cap in caps:
            r = c.execute("SELECT source_price FROM memecoin_entry_attempts WHERE origin_event_id=? AND mint=? "
                          "AND source_price>0 LIMIT 1", (cap["origin"], cap["mint"])).fetchone()
            src[cap["capture"]] = r["source_price"] if r else None
        horizon = statistics.median(cap["horizon_s"] for cap in caps) if caps else 810
        fills_policy, fills_realized, fill_prem = our_fills_graded(c, args.since, horizon)
    prem_fill = statistics.median(fill_prem) if fill_prem else 0.0
    print("captures (solana, exact round trip) since %s: %d; horizon %s s; our fill premium over source p50 %+.1f%% (n=%d)"
          % (args.since, len(caps), horizon, 100 * prem_fill, len(fill_prem)))
    lo, hi = boot_mean([x * 100 for x in fills_policy], rng) if len(fills_policy) > 1 else (float("nan"),) * 2
    print("OUR FILLS on the same instrument (route_series exit_decision, TP25/SL35): n=%d mean %+.1f%% [%+.1f, %+.1f]"
          % (len(fills_policy), 100 * statistics.fmean(fills_policy), lo, hi) if fills_policy else "our fills: none")
    lo, hi = boot_mean([x * 100 for x in fills_realized], rng)
    print("OUR FILLS realized: n=%d mean %+.1f%% [%+.1f, %+.1f]" % (len(fills_realized),
          100 * statistics.fmean(fills_realized), lo, hi))
    by = collections.defaultdict(list)
    for cap in dedupe(caps):
        by[cap["gate"]].append(cap)
    print("\n%-26s %4s %8s %17s %8s %17s %5s %5s %9s %9s" % ("gate", "n", "raw%", "95% CI", "rebased%", "95% CI",
                                                             "tp", "stop", "buyage50", "prem50"))
    for gate, lst in sorted(by.items(), key=lambda kv: -len(kv[1])):
        raw = [path_outcome(c["path"]) for c in lst]
        reb = []
        prems = []
        # A quote-based gate refused AT a price: the capture's own quote ~2 s later is the entry we
        # would have paid, so re-basing it to our fills' typical premium would invent a cheaper
        # entry the gate never saw. Only pre-quote gates are re-based.
        rebase = gate not in QUOTE_GATES
        for cap in lst:
            s = src.get(cap["capture"])
            if s and cap["buy_price"]:
                p = cap["buy_price"] / s - 1
                prems.append(p)
                if rebase:
                    k = (1 + p) / (1 + prem_fill)
                    reb.append(path_outcome([(a, (1 + r) * k - 1) for a, r in cap["path"]]))
        rl, rh = boot_mean([x * 100 for x in raw], rng) if len(raw) > 1 else (float("nan"),) * 2
        bl, bh = boot_mean([x * 100 for x in reb], rng) if len(reb) > 1 else (float("nan"),) * 2
        print("%-26s %4d %+8.1f [%+6.1f,%+7.1f] %+8.1f [%+6.1f,%+7.1f] %5d %5d %9.0f %+8.1f%%" % (
            gate, len(lst), 100 * statistics.fmean(raw), rl, rh,
            100 * statistics.fmean(reb) if reb else float("nan"), bl, bh,
            sum(1 for x in raw if x == TP), sum(1 for x in raw if x == SL),
            statistics.median(c["buy_age_s"] for c in lst), 100 * statistics.median(prems) if prems else float("nan")))


def selftest():
    assert path_outcome([(1, 0.1), (2, 0.3), (3, -0.5)]) == TP
    assert path_outcome([(1, -0.36), (2, 0.3)]) == SL
    assert path_outcome([(1, 0.1), (2, -0.1)]) == -0.1
    t = utc("2026-09-28T00:00:00+00:00")
    caps = [{"mint": "m", "refused": t, "horizon_s": 810},
            {"mint": "m", "refused": t + timedelta(seconds=100), "horizon_s": 810},
            {"mint": "m", "refused": t + timedelta(seconds=900), "horizon_s": 810}]
    assert len(dedupe(caps)) == 2
    assert gate_of("source buy $100 under absolute floor $197") == "source_buy_abs_floor"
    assert gate_of("unresolved signed transaction on solana") == "unresolved_signed_tx"
    print("selftest ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", nargs="?", choices=("counts", "inputs", "ledger", "outcome", "chain"))
    ap.add_argument("--since", default="2026-09-21T12:00:00")
    ap.add_argument("--top", type=int, default=24)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    if args.cmd == "chain":
        import gate_instrument_chain
        return gate_instrument_chain.main(args)
    return {"counts": cmd_counts, "inputs": cmd_inputs, "ledger": cmd_ledger, "outcome": cmd_outcome}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
