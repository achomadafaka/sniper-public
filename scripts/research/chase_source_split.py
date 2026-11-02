#!/usr/bin/env python3
"""How much of a Solana chase-cap gap is the SOURCE'S OWN buy, and how much came after it?

Operator idea, 2026-09-27 via Alpha (ref e6fd711e, SHOGGOTH: our quote +109% above DegenCapitalLLC's
$2.0k fill, round trip 3.79%, refused). The chase gates compare our price with the source's
AVERAGE fill. The source's own buy moves the price from pre-trade to post-trade, and the part of
that move above its average fill is inside the gate's gap. Only the move AFTER the source's buy is
other buyers in front of us.

Pre-registered with Alpha at 2026-09-27T21:32:23Z, before any outcome was read:

  G  gate gap = our quote (or display price, or our fill) / source price - 1, as the gate computed
  I  source post-trade price / source average fill - 1, from the source's own receipt: the pump
     curve TradeEvent or the PumpSwap Buy event(s) on this mint (same-venue ratios only)
  A  (1+G)/(1+I) - 1: our price against the source's post-trade price. It holds other buyers'
     move plus our own route cost at the clip (<= 5% by MEMECOIN_ENTRY_IMPACT_CAP_PCT).
  P1 fills: AUC P(random loser has higher A than a random winner); SUPPORTED ("A does not
     separate") if the 95% bootstrap CI contains 0.5.
  P2 R* = admit iff A <= the cap the attempt faced AND G <= 150% (secondary 100%, none).
     "Refusals R* admits do not make money at +300 s" SUPPORTED if the mean exact round trip at
     +300 s is <= 0 or its 95% CI contains 0.

Refusal outcome = exact capture round trip (data/memecoin_refusal_captures.db): USDC out of a sell
quote for the exact token quantity the capture's first buy quote returned, over that buy's USD in.
Independent quotes: the sell never sees our own buy, so it is optimistic by our impact.

Read-only. Chain reads only on the research key (funded_exit_truth.rpc), and only while no funded
Solana position or reservation is open: the guard is re-checked before EVERY read.

    python3 scripts/research/chase_source_split.py fetch --until 2026-09-27T21:32:23 --out split.json
    python3 scripts/research/chase_source_split.py report --split split.json
    python3 scripts/research/chase_source_split.py --selftest

WEEKLY, ON GAMMA'S BOARD (no cron, Alpha 2026-09-28): fetch the last 7 days while the book is flat,
then report. The report prints each lane's reference basis: the slot lane's source price is already
post-trade (A = G there), J7 and the on-chain lanes use the source's average fill.

    python3 scripts/research/chase_source_split.py fetch --since <now-7d> --until <now> --out /tmp/chase_split.json
    python3 scripts/research/chase_source_split.py report --split /tmp/chase_split.json
"""
import argparse
import contextlib
import json
import math
import random
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
LEDGER = REPO / "sniper_tracker.db"
CAPTURES = REPO / "data" / "memecoin_refusal_captures.db"
COPY_LANES = ("follow_activity", "onchain_watch", "slot_lane")
CAP_RE = re.compile(r"\(cap ([\d.]+)%")
GAP_RE = re.compile(r"([+-]?[\d.]+)%")


def ro(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def source_sig(origin):
    """The source transaction signature an origin id carries, or None (J7 'swap:<uuid>' ids)."""
    p = str(origin or "").split(":")
    if p[0] in ("onchain", "slotlane") and len(p) > 1:
        return p[1]
    if p[:2] == ["swap", "raybot"] and len(p) > 2:
        return p[2]
    return None


def reference_basis(origin):
    """What the chase gate's source price IS for this lane (2026-09-28). The slot lane prices the
    source from its own trade event AFTER the trade (curve virtual reserves, PumpSwap vaults), so its
    G already excludes the source's impact. J7 and the on-chain lanes use the source's average fill."""
    return "post_trade" if str(origin or "").startswith("slotlane:") else "average_fill"


def split(g_pct, avg, post, basis="average_fill"):
    """(I %, A %) from the gate gap and the source's average fill and post-trade price.

    UNITS: avg and post are in the pool's own quote-per-token units (pool-specific decimals and
    quote asset). Only their RATIO is meaningful; never compare either with the USD source or quote
    price without a per-pool unit (across pools the log ratio spreads by about 3, measured
    2026-09-28)."""
    if g_pct is None or not avg or not post:
        return None, None
    i = post / avg - 1.0
    if basis == "post_trade":
        return i * 100.0, g_pct
    return i * 100.0, ((1.0 + g_pct / 100.0) / (1.0 + i) - 1.0) * 100.0


def refusals(since, until):
    with contextlib.closing(ro(LEDGER)) as c:
        rows = c.execute(
            "SELECT attempt_id, origin_event_id, mint, source_user_id, source_handle, delivery_lane,"
            " signal_source, decision_started_at, terminal_reason, quote_price, source_price, trace_json"
            " FROM memecoin_entry_attempts WHERE network_id='solana' AND decision_started_at >= ?"
            " AND decision_started_at < ? AND (terminal_reason LIKE 'exact quote is %above source price%'"
            " OR terminal_reason LIKE 'coin already ran %') ORDER BY decision_started_at",
            (since, until)).fetchall()
    out, seen = [], set()
    for r in rows:
        key = source_sig(r["origin_event_id"]) or r["origin_event_id"]
        if key in seen:
            continue
        seen.add(key)
        reason = r["terminal_reason"]
        fam = "exact_quote" if reason.startswith("exact quote") else "display_chase"
        gap = None
        for e in (json.loads(r["trace_json"] or "{}").get("substage_events") or []):
            if e.get("name") == "chase_quote_confirmation" and e.get("gap_pct") is not None:
                gap = float(e["gap_pct"])
        if gap is None:
            m = GAP_RE.search(reason)
            gap = float(m.group(1)) if m else None
        cap = CAP_RE.search(reason)
        out.append({"kind": "refusal", "family": fam, "attempt_id": r["attempt_id"],
                    "origin": r["origin_event_id"], "sig": source_sig(r["origin_event_id"]),
                    "mint": r["mint"], "handle": r["source_handle"],
                    "lane": r["delivery_lane"] or r["signal_source"], "at": r["decision_started_at"],
                    "reason": reason, "g_pct": gap, "cap_pct": float(cap.group(1)) if cap else None,
                    "quote_price": r["quote_price"], "source_price": r["source_price"]})
    return out


def fills(since, until):
    with contextlib.closing(ro(LEDGER)) as c:
        rows = c.execute(
            "SELECT id, origin_event_id, mint, source_handle, signal_source, opened_at, entry_price,"
            " their_price, entry_notional, realized_pnl, exit_reason FROM memecoin_positions"
            " WHERE execution_mode='real' AND network_id='solana' AND is_open=0 AND opened_at >= ?"
            " AND opened_at < ? ORDER BY id", (since, until)).fetchall()
    out = []
    for r in rows:
        if r["signal_source"] not in COPY_LANES:
            continue
        g = ((r["entry_price"] / r["their_price"] - 1.0) * 100.0
             if r["entry_price"] and r["their_price"] else None)
        out.append({"kind": "fill", "position_id": r["id"], "origin": r["origin_event_id"],
                    "sig": source_sig(r["origin_event_id"]), "mint": r["mint"],
                    "handle": r["source_handle"], "lane": r["signal_source"], "at": r["opened_at"],
                    "g_pct": g, "pnl": r["realized_pnl"], "notional": r["entry_notional"],
                    "exit_reason": r["exit_reason"]})
    return out


# --- the source's own trade, from its receipt -----------------------------------------------------

def book_open():
    with contextlib.closing(ro(LEDGER)) as c:
        pos = c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                        "AND network_id='solana' AND is_open=1 AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]
        res = c.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
                        "AND COALESCE(network_id,'solana')='solana'").fetchone()[0]
    return pos + res


def source_trade(tx, mint, pool_facts):
    """Every BUY event on `mint` in the source tx, on one exact venue: (pre of the first, post of
    the last, average = total quote / total base, venue key, n events) or a reason string."""
    import jito_evaluation as je
    import funded_exit_truth as fet
    from sniper.execution.pump_curve_buy import parse_trade_event
    from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
    from sniper.services.memecoin_opportunity_live import program_data_logs
    if tx is None:
        return "unreadable"
    if (tx.get("meta") or {}).get("err") is not None:
        return "failed_tx"
    logs = tx["meta"].get("logMessages") or []
    evs = []
    for _i, line in program_data_logs(logs, fet.PUMP):
        try:
            if anchor_event_bytes(line)[:8] != fet.TRADE_DISC:
                continue
            e = parse_trade_event(line)
        except Exception:
            continue
        if e.get("mint") == mint and e.get("is_buy"):
            vs, vt = int(e["virtual_sol_reserves"]), int(e["virtual_token_reserves"])
            evs.append(("curve:%s" % (e.get("quote_mint") or fet.WSOL), je._curve_pre_price(e),
                        (vs / 1e9) / (vt / 1e6) if vs > 0 and vt > 0 else None,
                        int(e["sol_amount"]) / 1e9, int(e["token_amount"]) / 1e6))
    for _i, line in program_data_logs(logs, je.PUMP_AMM):
        try:
            e = je._amm_event(line)
        except Exception:
            continue
        if not e or not e["is_buy"]:
            continue
        facts = pool_facts(e["pool"])
        if not (facts and facts[0] == mint and e["base"] > 0):
            continue
        vq = facts[2]
        base_post, quote_post = e["base"] - e["base_amount"], e["quote"] + e["quote_amount"]
        evs.append(("pumpswap:%s" % e["pool"], (e["quote"] + vq) / e["base"],
                    (quote_post + vq) / base_post if base_post > 0 else None,
                    e["quote_amount"], e["base_amount"]))
    if not evs:
        return "unpriced_venue"
    if len({e[0] for e in evs}) > 1:
        return "multi_venue"
    q, b = sum(e[3] for e in evs), sum(e[4] for e in evs)
    if not (b > 0 and evs[0][1] and evs[-1][2]):
        return "unpriced_venue"
    return {"venue": evs[0][0], "pre": evs[0][1], "post": evs[-1][2], "avg": q / b,
            "n_events": len(evs), "quote_amount": q}


def fetch(args):
    import dump_forensics as dfx
    import funded_exit_truth as fet
    import jito_evaluation as je
    rows = refusals(args.since, args.until) + fills(args.since, args.until)
    by_sig = {}

    def guarded_pool_facts(pool):
        if pool not in je._POOLS and book_open():
            raise fet.Stop("funded Solana book opened; stopping chain reads")
        return je._pool_facts(pool)

    try:
        for r in rows:
            sig = r["sig"]
            if not sig:
                r["status"] = "no_source_sig"
                continue
            if sig not in by_sig:
                if book_open():
                    raise fet.Stop("funded Solana book is not flat; stopping chain reads")
                by_sig[sig] = source_trade(dfx.get_tx(sig), r["mint"], guarded_pool_facts)
            t = by_sig[sig]
            if isinstance(t, str):
                r["status"] = t
                continue
            r.update(t)
            r["status"] = "ok"
            r["i_pct"], r["a_pct"] = split(r["g_pct"], t["avg"], t["post"], reference_basis(r["origin"]))
            r["full_impact_pct"] = (t["post"] / t["pre"] - 1.0) * 100.0 if t["pre"] else None
    except fet.Stop as exc:
        print("STOPPED: %s (%d of %d rows read)" % (exc, len(by_sig), len(rows)), file=sys.stderr)
    Path(args.out).write_text(json.dumps(rows, indent=1))
    print("wrote %d rows, %d source txs, research-key 429s %d" % (len(rows), len(by_sig), fet.RATE_429[0]))


# --- outcomes and the report -----------------------------------------------------------------------

def capture_roundtrips(origin, horizons=(60, 300, 600), tol_s=20.0):
    """{h: exact round trip %} for one refused origin, from its capture rows."""
    with contextlib.closing(ro(CAPTURES)) as c:
        rows = c.execute("SELECT age_s, quote_side, quote_input_usd, token_qty_atomic,"
                         " quote_output_atomic FROM memecoin_refusal_captures WHERE network_id='solana'"
                         " AND origin_event_id=? AND quality='ok' ORDER BY age_s", (origin,)).fetchall()
    buy = next((r for r in rows if r["quote_side"] == "buy" and r["quote_input_usd"]), None)
    if buy is None:
        return {}
    out = {}
    sells = [r for r in rows if r["quote_side"] == "sell" and r["token_qty_atomic"] == buy["token_qty_atomic"]
             and r["quote_output_atomic"] and r["age_s"] is not None]
    for h in horizons:
        near = min(sells, key=lambda r: abs(r["age_s"] - h), default=None)
        if near is not None and abs(near["age_s"] - h) <= tol_s:
            out[h] = (int(near["quote_output_atomic"]) / 1e6 / float(buy["quote_input_usd"]) - 1.0) * 100.0
    return out


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def auc(losers, winners):
    """P(random loser value > random winner value), ties 0.5."""
    if not losers or not winners:
        return None
    s = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in losers for b in winners)
    return s / (len(losers) * len(winners))


def boot(fn, rows, n=10_000, seed=20260927):
    rnd = random.Random(seed)
    vals = []
    for _ in range(n):
        v = fn([rows[rnd.randrange(len(rows))] for _ in rows])
        if v is not None:
            vals.append(v)
    return pct(vals, 0.025), pct(vals, 0.975)


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def fmt(xs):
    xs = [x for x in xs if x is not None]
    return ("n=%d p10 %+.1f p50 %+.1f p90 %+.1f" % (len(xs), pct(xs, .1), pct(xs, .5), pct(xs, .9))
            if xs else "n=0")


def rule_admits(r, ceiling):
    return (r.get("a_pct") is not None and r.get("cap_pct") is not None
            and r["a_pct"] <= r["cap_pct"] and (ceiling is None or r["g_pct"] <= ceiling))


def report(args):
    rows = json.loads(Path(args.split).read_text())
    for r in rows:        # a split written before the basis fix divided slot-lane A by I twice
        if r.get("status") == "ok" and r.get("avg") and r.get("post"):
            r["i_pct"], r["a_pct"] = split(r["g_pct"], r["avg"], r["post"], reference_basis(r["origin"]))
    R = [r for r in rows if r["kind"] == "refusal"]
    F = [r for r in rows if r["kind"] == "fill"]
    for name, grp in (("refusals", R), ("fills", F)):
        st = {}
        for r in grp:
            st[r.get("status")] = st.get(r.get("status"), 0) + 1
        print("%s: %d  status %s" % (name, len(grp), st))
    ok = [r for r in R if r.get("status") == "ok"]
    print("\n(1) REFUSALS, split (%%): G %s" % fmt([r["g_pct"] for r in ok]))
    print("    I (source post / its avg fill) %s" % fmt([r["i_pct"] for r in ok]))
    print("    full source impact pre->post %s" % fmt([r["full_impact_pct"] for r in ok]))
    print("    A (ours / source post) %s" % fmt([r["a_pct"] for r in ok]))
    share = [100 * math.log1p(r["i_pct"] / 100) / math.log1p(r["g_pct"] / 100)
             for r in ok if r["g_pct"] and r["g_pct"] > 0]
    print("    share of G that is the source's own impact (log): %s" % fmt(share))
    print("    A <= its own cap: %d of %d" % (sum(1 for r in ok if r["a_pct"] <= (r["cap_pct"] or 0)), len(ok)))
    for lane in sorted({r["lane"] for r in ok}):
        sub = [r for r in ok if r["lane"] == lane]
        print("    lane %-34s basis %-12s n=%d  G %s | A %s | A <= cap %d" % (
            lane, reference_basis(sub[0]["origin"]), len(sub), fmt([r["g_pct"] for r in sub]),
            fmt([r["a_pct"] for r in sub]), sum(1 for r in sub if r["a_pct"] <= (r["cap_pct"] or 0))))
    for fam in ("exact_quote", "display_chase"):
        sub = [r for r in ok if r["family"] == fam]
        print("    %-13s n=%d  G %s | A %s" % (fam, len(sub), fmt([r["g_pct"] for r in sub]),
                                               fmt([r["a_pct"] for r in sub])))
    print("\n(2) REFUSED COINS AFTER (exact capture round trip %, entry at the capture's buy quote):")
    for r in R:
        r["rt"] = capture_roundtrips(r["origin"])
    for h in (60, 300, 600):
        xs = [r["rt"][h] for r in R if h in r["rt"]]
        print("    +%3ds all refusals %s  mean %s  win %s" % (
            h, fmt(xs), "%+.1f" % mean(xs) if xs else "-",
            "%d/%d" % (sum(1 for x in xs if x > 0), len(xs)) if xs else "-"))
    print("\n(3) FILLS: split vs outcome (pnl %% of notional)")
    fok = [r for r in F if r.get("status") == "ok" and r.get("pnl") is not None and r.get("notional")
           and r.get("a_pct") is not None]
    for r in fok:
        r["ret"] = 100.0 * r["pnl"] / r["notional"]
    print("    G %s | I %s | A %s" % (fmt([r["g_pct"] for r in fok]), fmt([r["i_pct"] for r in fok]),
                                     fmt([r["a_pct"] for r in fok])))
    for lane in sorted({r["lane"] for r in fok}):
        sub = [r for r in fok if r["lane"] == lane]
        print("    lane %-34s basis %-12s n=%d net $%+.2f  A %s" % (
            lane, reference_basis(sub[0]["origin"]), len(sub), sum(r["pnl"] for r in sub),
            fmt([r["a_pct"] for r in sub])))
    for key in ("a_pct", "g_pct", "i_pct"):
        f = (lambda rs, k=key: auc([r[k] for r in rs if r["pnl"] <= 0],
                                   [r[k] for r in rs if r["pnl"] > 0]))
        lo, hi = boot(f, fok)
        print("    AUC %s P(loser higher) %.3f  95%% CI [%.3f, %.3f]  n=%d (%d wins)" % (
            key[0].upper(), f(fok), lo, hi, len(fok), sum(1 for r in fok if r["pnl"] > 0)))
    for lo_, hi_ in ((None, 10), (10, 30), (30, None)):
        sub = [r for r in fok if (lo_ is None or r["a_pct"] >= lo_) and (hi_ is None or r["a_pct"] < hi_)]
        print("    A in [%s, %s): n=%d net $%+.2f mean %s win %d" % (
            lo_, hi_, len(sub), sum(r["pnl"] for r in sub),
            "%+.1f%%" % mean([r["ret"] for r in sub]) if sub else "-", sum(1 for r in sub if r["pnl"] > 0)))
    print("\n(4) RULE R*: admit iff A <= own cap AND G <= ceiling")
    for ceiling in (150.0, 100.0, None):
        adm = [r for r in ok if rule_admits(r, ceiling)]
        xs = [r["rt"][300] for r in adm if 300 in r["rt"]]
        ci = boot(mean, xs) if len(xs) > 1 else (None, None)
        print("    ceiling %s: admits %d of %d priced refusals; +300 s exact rt n=%d mean %s CI %s win %s"
              % (ceiling, len(adm), len(ok), len(xs), "%+.1f" % mean(xs) if xs else "-",
                 "[%+.1f, %+.1f]" % ci if ci[0] is not None else "-",
                 "%d/%d" % (sum(1 for x in xs if x > 0), len(xs)) if xs else "-"))
        for r in adm:
            print("      %s %-16s G %+6.1f I %+6.1f A %+6.1f cap %s  rt60 %s rt300 %s rt600 %s" % (
                r["at"][:16], (r["handle"] or "?")[:16], r["g_pct"], r["i_pct"], r["a_pct"], r["cap_pct"],
                *("%+.1f" % r["rt"][h] if h in r["rt"] else "-" for h in (60, 300, 600))))


# ---- FORWARD READ of MEMECOIN_REAL_CHASE_REFERENCE=post_trade (live 2026-09-28T15:01:04Z, operator's call:
# "measure what we can actually control"). Ledger only, no chain. Pre-registered STOP line (Gamma, for Alpha):
#   FLIPPED = a funded fill whose last chase_quote_confirmation judged on the post-trade reference and passed
#             (gap_pct <= cap_pct) while the AVERAGE-fill gap (quote_price / source_price - 1) was over the cap.
#             Only these rows exist because of the change; fills both references admit are not its evidence.
#   STOP-EARLY  the first 10 closed flipped fills sum <= -$150 AND >= 7 of them lost.
#   STOP-READ   at >= 30 closed flipped fills, the mean realized return's 95% bootstrap CI (resampling mints)
#               lies entirely below 0.
#   On either: revert MEMECOIN_REAL_CHASE_REFERENCE to the average fill (unset). Otherwise continue.
CUTOVER = "2026-09-28T15:01:04"
STOP_EARLY_N, STOP_EARLY_SUM, STOP_EARLY_LOSERS, STOP_READ_N = 10, -150.0, 7, 30


def chase_mark(trace_json):
    """The LAST chase_quote_confirmation mark (a fresh re-quote writes a second one), or None."""
    marks = [e for e in (json.loads(trace_json or "{}").get("substage_events") or [])
             if e.get("name") == "chase_quote_confirmation"]
    return marks[-1] if marks else None


def classify(mark):
    """(basis, avg_gap_pct, flipped) for one mark; basis post_trade / post_trade_clamped / avg_fill."""
    basis = mark.get("reference_basis") or "avg_fill"
    src, quote, ref = mark.get("source_price"), mark.get("quote_price"), mark.get("reference_price")
    cap, gap = mark.get("cap_pct"), mark.get("gap_pct")
    avg_gap = (quote / src - 1.0) * 100.0 if src and quote else None
    if basis == "post_trade" and src and ref and abs(ref / src - 2.0) < 1e-6:
        basis = "post_trade_clamped"
    flipped = (basis != "avg_fill" and None not in (avg_gap, cap, gap) and gap <= cap < avg_gap)
    return basis, avg_gap, flipped


def stop_verdict(closed):
    """closed: [(pnl_usd, ret_frac, mint)] of closed FLIPPED fills in fill order."""
    first = closed[:STOP_EARLY_N]
    if len(first) == STOP_EARLY_N and sum(c[0] for c in first) <= STOP_EARLY_SUM \
            and sum(c[0] < 0 for c in first) >= STOP_EARLY_LOSERS:
        return "STOP-EARLY: revert MEMECOIN_REAL_CHASE_REFERENCE to the average fill"
    if len(closed) >= STOP_READ_N:
        by = {}
        for c in closed:
            by.setdefault(c[2], []).append(c[1])
        keys, rnd, means = list(by), random.Random(20260928), []
        for _ in range(4000):
            xs = [x for k in (keys[rnd.randrange(len(keys))] for _ in keys) for x in by[k]]
            means.append(sum(xs) / len(xs))
        means.sort()
        hi = means[int(0.975 * len(means)) - 1]
        return ("STOP-READ: mean return CI upper %.1f%% < 0, revert" % (100 * hi) if hi < 0
                else "CONTINUE at the registered read (CI upper %+.1f%%)" % (100 * hi))
    return "CONTINUE (closed flipped %d; early check at %d, registered read at %d)" % (
        len(closed), STOP_EARLY_N, STOP_READ_N)


def forward(args):
    from sniper.persistence.db_memecoin import robot_performance_sql
    with contextlib.closing(ro(LEDGER)) as c:
        rows = c.execute(
            "SELECT a.attempt_id, a.signal_source, a.mint, a.terminal_status, a.trace_json, a.decision_started_at, "
            "p.id, p.is_open, p.realized_pnl, p.entry_notional FROM memecoin_entry_attempts a "
            "LEFT JOIN memecoin_positions p ON p.attempt_id=a.attempt_id AND p.execution_mode='real' "
            f"AND {robot_performance_sql('p')} "
            "WHERE a.network_id='solana' AND a.decision_started_at>=? ORDER BY a.decision_started_at",
            (args.since,)).fetchall()
    tally, closed_flipped = {}, []
    for aid, lane, mint, status, tj, dec, pid, is_open, pnl, notional in rows:
        mark = chase_mark(tj)
        if not mark:
            continue
        basis, avg_gap, flipped = classify(mark)
        t = tally.setdefault((lane, basis), {"marks": 0, "passed": 0, "refused": 0, "flipped": 0,
                                               "fills": 0, "flipped_fills": 0})
        t["marks"] += 1
        passed = mark.get("gap_pct") is not None and mark.get("cap_pct") is not None and mark["gap_pct"] <= mark["cap_pct"]
        t["passed" if passed else "refused"] += 1
        t["flipped"] += flipped
        if pid:
            t["fills"] += 1
            t["flipped_fills"] += flipped
            if flipped and not is_open and pnl is not None and notional:
                closed_flipped.append((pnl, pnl / notional, mint))
    print("chase reference forward read since %s (MEMECOIN_REAL_CHASE_REFERENCE cutover %s)" % (args.since, CUTOVER))
    for (lane, basis), t in sorted(tally.items()):
        print("  %-15s %-19s marks %4d passed %4d refused %4d | flipped (admitted only by post_trade) %3d, "
              "funded fills %3d of which flipped %3d" % (lane, basis, t["marks"], t["passed"], t["refused"],
                                                         t["flipped"], t["fills"], t["flipped_fills"]))
    if closed_flipped:
        rets = [x[1] for x in closed_flipped]
        print("  closed flipped fills n=%d sum $%+.2f mean %+.1f%% losers %d" % (
            len(rets), sum(x[0] for x in closed_flipped), 100 * sum(rets) / len(rets),
            sum(x[0] < 0 for x in closed_flipped)))
    print("  VERDICT:", stop_verdict(closed_flipped))


def selftest():
    assert source_sig("onchain:ABC") == "ABC" and source_sig("slotlane:XYZ") == "XYZ"
    assert source_sig("swap:raybot:SIG:trader:mint") == "SIG"
    assert source_sig("swap:51a20c6c-3e1e") is None and source_sig(None) is None
    # the source paid avg 1.0 and left the price at 1.5; our quote 2.09x its avg
    i, a = split(109.0, 1.0, 1.5)
    assert abs(i - 50.0) < 1e-9 and abs(a - (2.09 / 1.5 - 1) * 100) < 1e-9
    assert split(None, 1.0, 1.5) == (None, None) and split(10.0, 0.0, 1.5) == (None, None)
    # A slot-lane chase reference is already the POST-trade price, so the source's own impact is not
    # inside its G and A must not divide it out again.
    assert reference_basis("slotlane:SIG") == "post_trade"
    assert reference_basis("swap:raybot:SIG:t:m") == reference_basis("onchain:SIG") == "average_fill"
    i, a = split(109.0, 1.0, 1.5, "post_trade")
    assert abs(i - 50.0) < 1e-9 and abs(a - 109.0) < 1e-9
    assert auc([3, 4], [1, 2]) == 1.0 and auc([1], [1]) == 0.5 and auc([], [1]) is None
    r = {"a_pct": 20.0, "cap_pct": 30.0, "g_pct": 120.0}
    assert rule_admits(r, 150.0) and not rule_admits(r, 100.0) and rule_admits(r, None)
    assert not rule_admits(dict(r, a_pct=31.0), None)
    m = {"reference_basis": "post_trade", "source_price": 1.0, "reference_price": 1.2, "quote_price": 1.5,
         "gap_pct": 25.0, "cap_pct": 30.0}
    assert classify(m) == ("post_trade", 50.0, True)                              # avg 50% > cap 30 >= ref 25%
    assert classify(dict(m, reference_price=2.0, gap_pct=-25.0))[0] == "post_trade_clamped"
    assert classify(dict(m, quote_price=1.2, gap_pct=0.0))[2] is False             # avg 20% passes anyway
    assert classify(dict(m, reference_basis="avg_fill"))[2] is False
    loss = [(-20.0, -0.15, "m%d" % i) for i in range(8)] + [(5.0, 0.03, "w1"), (5.0, 0.03, "w2")]
    assert stop_verdict(loss).startswith("STOP-EARLY")
    assert stop_verdict(loss[:9]).startswith("CONTINUE")
    assert stop_verdict([(-5.0, -0.2, "m%d" % i) for i in range(30)]).startswith("STOP-READ")
    assert stop_verdict([(5.0, 0.1 if i % 2 else -0.1, "m%d" % i) for i in range(30)]).startswith("CONTINUE")
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch")
    f.add_argument("--since", default="2026-09-20T00:00:00")
    f.add_argument("--until", required=True)
    f.add_argument("--out", required=True)
    rp = sub.add_parser("report")
    rp.add_argument("--split", required=True)
    fw = sub.add_parser("forward", help="ledger-only read of the post_trade reference since the cutover, with the STOP line")
    fw.add_argument("--since", default=CUTOVER)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.cmd == "fetch":
        return fetch(a)
    if a.cmd == "report":
        return report(a)
    if a.cmd == "forward":
        return forward(a)
    ap.print_help()


if __name__ == "__main__":
    main()
