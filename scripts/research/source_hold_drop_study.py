#!/usr/bin/env python3
"""Catch the copier dump while the source still holds: a sub-second drop entry, rebuilt from chain.

Operator design 2026-09-28 (via Alpha): "after a big-following wallet buys, copiers spike it and it
DROPS within SECONDS while the source still holds; catch that drop."

Population: every armed Solana wallet's BUY since --since that reached our entry hook (filled AND
refused), one row per SOURCE transaction (the signature in the origin id; a J7 uuid origin is
resolved only for a filled row, from our own entry transaction). Wallets run in copier-crowd order
(median crowd SOL in slots S..S+2 from logs/wallet_scoreboard_20260927.jsonl), top first.

Per buy, from chain only (research key e1f95763 via funded_exit_truth.rpc; every buy waits for a
flat funded Solana book): every succeeded transaction on the MINT from the source's slot S to
S + 458 (~120 s at 268 ms), ascending, with transactionIndex (Helius getTransactionsForAddress).
Each is decoded by dump_forensics.trade_of and kept only when SOL-quoted (trade_of prices a USDC
leg at 1.0 and a SOL leg at sol_usd; a mixed path invents spikes). Prices are SOL per token: a
curve trade is the post-trade virtual-reserve spot, a pool trade the trade's own average price
from the pool's deltas. The SOURCE's own sells come from its own token balance on the mint in each
transaction (dump_forensics.owner_bag), never from trade_of's largest mover.

Simulation (report): after the source's buy, track the running peak of prints. A drop of X% from
that peak inside W seconds of the source buy, with the source still holding (no own-balance
decrease yet), triggers. Entry = the first print at least one slot after the trigger, times
ENTRY_MARKUP (own impact + curve fee measured on S+1 fills, dump_forensics.S_ENTRY_MARKUP). Exit =
the first later print at >= +25% (TP) or <= -35% (stop), else the last print at or before S + 458.
The same exit rule is applied to: our ACTUAL fill (filled rows; our own entry print) and an entry
AT REFUSAL (refused rows; the first print at or after S + K, K = the median slot lag of our actual
fills in this data, times ENTRY_MARKUP). Our own entry prints are excluded from every path.
Windows are in SLOTS (one clock): W seconds = W / SLOT_S slots.

PRE-REGISTRATION (Alpha's order, committed 2026-09-28 before the remaining 937 buys were fetched).
The first 189 buys (the five top copier-crowd wallets) were already read when this was written; their
keys are frozen in logs/acceptance/source_hold_drop.seen_keys.txt and they are EXCLUDED from the
primary read (reported separately as exploratory).
  PRIMARY    W 15 s, drop 20%: drop-entry return minus entry-at-refusal return, paired, on REFUSED
             in-scope buys (family not over_ceiling / not_approved), holdout keys only.
             Positive only if n_paired >= 30, the bootstrap 95% CI of the mean excludes 0 above,
             AND the mean stays > 0 with every exit-censored triggered row at its worst case
             (drop arm at the stop -35%, refusal arm at the TP +25%). Otherwise: not shown.
  SECONDARY  W 15 s, drop 20%: drop-entry return minus our actual fill, paired, on FILLED buys,
             holdout keys only. Descriptive; same statistics.
  Every other cell is EXPLORATORY. Censoring is reported per group with a peak-multiple proxy
  (max print / source price within the covered first 15 s), complete vs truncated.

    python3 scripts/research/source_hold_drop_study.py run --out logs/acceptance/source_hold_drop.jsonl --wallets 10
    python3 scripts/research/source_hold_drop_study.py report --out logs/acceptance/source_hold_drop.jsonl
    python3 scripts/research/source_hold_drop_study.py --selftest
"""

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)
import argparse
import contextlib
import json
import random
import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")
SCOREBOARD = REPO / "logs" / "wallet_scoreboard_20260927.jsonl"
SLOT_S = 0.268                 # getRecentPerformanceSamples median 269.1 ms, slotSubscribe 267.9 (09-26)
HORIZON_SLOTS = 458            # ~120 s plus a margin of 10 slots
SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")   # a Solana signature; a 44-char mint/owner is not one
PAGES = 40                     # 4,000 transactions; a busier coin is TRUNCATED and censored past its last slot.
# Most of a busy window is arbitrage no-ops (HVi6V.., succeeded, every balance unchanged): 1,500 txs
# covered only 51-265 slots on DegenCapitalLLC coins (2026-09-28), with 5 of 100 decodable in one burst.
ENTRY_MARKUP = 1.035
TP, STOP = 0.25, -0.35
# A TP/stop decided on a print FILLS at the next print within EXIT_LAG_SLOTS (Solana decision->fill
# p50 1.89 s = ~7 slots, finding-three-fill-stamps), else at the trigger print (no trade = no move).
# Crediting the trigger print itself paid single spike prints of +382% as TP fills (2026-09-28).
EXIT_LAG_SLOTS = 7
DROPS = (0.10, 0.20, 0.30, 0.40)
WINDOWS_S = (5, 15, 30, 60)
PRIMARY_W, PRIMARY_DROP = 15, 0.20
SEEN_KEYS = REPO / "logs" / "acceptance" / "source_hold_drop.seen_keys.txt"


def ro():
    return sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=30)


def family(status, reason):
    if status == "filled":
        return "filled"
    r = str(reason or "")
    for key, pat in (("over_ceiling", "over ceiling"), ("not_approved", "not_approved"),
                     ("source_buy_floor", "source buy"), ("chase", "above source price|already ran"),
                     ("reentry", "re-entry|losing re-entry"), ("one_position", "one position"),
                     ("roundtrip", "round trip"), ("impact", "price impact"), ("volume", "organic volume|turnover")):
        if re.search(pat, r):
            return key
    return "other"


def crowd_rank():
    """{source_user_id: median crowd SOL S..S+2} from the 09-27 scoreboard (None = unmeasured)."""
    out = {}
    for line in SCOREBOARD.read_text().splitlines():
        r = json.loads(line)
        out[r["uid"]] = ((r.get("summary") or {}).get("crowd_sol") or {}).get("median")
    return out


def population(since):
    """[{sig|None, uid, handle, mint, status, family, our_sig, owner, decided}] one per source buy."""
    from fast_lane_coverage_census import source_sig
    with contextlib.closing(ro()) as c:
        c.row_factory = sqlite3.Row
        armed = {r[0]: r[1] for r in c.execute(
            "SELECT source_user_id, handle FROM memecoin_wallets WHERE real_approved=1 AND COALESCE(paused,0)=0 "
            "AND COALESCE(excluded,0)=0 AND real_chains LIKE '%solana%'")}
        owner = {r[0]: r[1] for r in c.execute(
            "SELECT source_user_id, address FROM memecoin_address_registry WHERE network_id='solana' "
            "AND status IN ('verified','registry_verified')")}
        rows = c.execute(
            "SELECT a.attempt_id, a.source_user_id, a.source_handle, a.mint, a.origin_event_id, a.terminal_status, "
            "a.terminal_reason, a.decision_started_at, COALESCE(a.tx_sig, pe.tx_sig) AS our_sig, "
            "p.id AS position_id FROM memecoin_entry_attempts a "
            "LEFT JOIN memecoin_positions p ON p.attempt_id=a.attempt_id AND p.execution_mode='real' "
            "LEFT JOIN memecoin_pending_entries pe ON pe.attempt_id=a.attempt_id "
            "WHERE a.network_id='solana' AND a.side='buy' AND a.decision_started_at>=? "
            "AND a.source_user_id IN (%s) ORDER BY a.decision_started_at" % ",".join("?" * len(armed)),
            [since, *armed]).fetchall()
    buys, uuid_refused = {}, 0
    for r in rows:
        filled = r["terminal_status"] == "filled" or r["position_id"] is not None
        sig = source_sig(r["origin_event_id"])
        sig = sig if sig and SIG.match(sig) else None     # a pump:<owner>:<mint> origin yields a mint here
        key = sig or ("uuid:" + r["attempt_id"] if filled else None)
        if key is None:
            uuid_refused += 1
            continue
        uid = r["source_user_id"]
        b = buys.setdefault(key, {"key": key, "sig": sig, "uid": uid, "handle": armed.get(uid) or r["source_handle"],
                                  "mint": r["mint"], "owner": uid[5:] if uid.startswith("pump:") else owner.get(uid),
                                  "status": "refused", "reason": r["terminal_reason"], "our_sig": None,
                                  "decided": r["decision_started_at"]})
        if filled:
            b["status"], b["our_sig"] = "filled", r["our_sig"] or b["our_sig"]
    for b in buys.values():
        b["family"] = family(b["status"], b["reason"])
    return list(buys.values()), uuid_refused


def resolve_uuid_source(b, our_tx):
    """The owner's last buy of the mint at or before our entry, from its own balance-changing txs."""
    from wallet_crowd_dump_scoreboard import gtfa
    import dump_forensics as dfx
    slot = our_tx["slot"]
    txs, _ = gtfa(b["owner"], {"slot": {"gte": slot - 300, "lte": slot}}, order="desc", limit=50,
                  token_accounts="balanceChanged")
    for tx in txs:
        pre, post = dfx.owner_bag(tx, b["owner"], b["mint"])
        if post > pre:
            return tx["transaction"]["signatures"][0]
    return None


def fetch(b, ours):
    """One record: the source trade, every SOL-quoted print on the mint to S + HORIZON_SLOTS, and the
    source's own balance moves. Returns (record, None) or (None, why)."""
    import dump_forensics as dfx
    from wallet_crowd_dump_scoreboard import gtfa
    if not b["owner"]:
        return None, "no_owner_address"
    our_tx = dfx.get_tx(b["our_sig"]) if b["our_sig"] else None
    if b["sig"] is None:
        if not our_tx:
            return None, "uuid_no_entry_tx"
        b["sig"] = resolve_uuid_source(b, our_tx)
        if not b["sig"]:
            return None, "uuid_source_not_found"
    src = dfx.get_tx(b["sig"])
    if not src or (src.get("meta") or {}).get("err") is not None:
        return None, "source_tx_unreadable"
    pre, post = dfx.owner_bag(src, b["owner"], b["mint"])
    if post <= pre:
        return None, "source_tx_not_an_owner_buy"
    S = src["slot"]
    txs, complete = gtfa(b["mint"], {"slot": {"gte": S, "lte": S + HORIZON_SLOTS}}, limit=100, pages=PAGES)
    trades, moves, src_idx, non_sol, undecoded = [], [], None, 0, 0
    for tx in txs:
        sig = tx["transaction"]["signatures"][0]
        slot, idx = tx.get("slot"), tx.get("transactionIndex") or 0
        if sig == b["sig"]:
            src_idx = idx
        else:
            p0, p1 = dfx.owner_bag(tx, b["owner"], b["mint"])
            if p0 != p1:
                moves.append([slot, idx, p0, p1])
        t1 = dfx.trade_of(tx, b["mint"], 1.0)
        if not t1 or not t1.get("price"):
            undecoded += 1
            continue
        t2 = dfx.trade_of(tx, b["mint"], 1000.0)
        if not (t2 and t2.get("price") and abs(t2["price"] / t1["price"] - 1000.0) < 1.0):
            non_sol += 1
            continue
        flag = "s" if sig == b["sig"] else "o" if sig in ours or sig == b["our_sig"] else ""
        trades.append([slot, idx, tx.get("blockTime"), t1["price"], t1["side"], flag])
    if src_idx is None:
        return None, "source_tx_not_in_mint_list"
    return {"key": b["key"], "sig": b["sig"], "uid": b["uid"], "handle": b["handle"], "mint": b["mint"],
            "status": b["status"], "family": b["family"], "our_sig": b["our_sig"], "S": S, "src_idx": src_idx,
            "our_slot": our_tx["slot"] if our_tx else None, "bought": post - pre, "trades": trades, "moves": moves,
            "complete": complete, "n_tx": len(txs), "last_slot": max([t.get("slot") or 0 for t in txs] or [S]),
            "non_sol": non_sol, "undecoded": undecoded}, None


def run(args):
    import funded_exit_truth as fet
    import slot_lane_send_monitor as mon
    from slot_minout_grading import wait_flat
    from sniper.execution.solana_rpc_pool import key_id
    import dump_forensics as dfx
    from wallet_crowd_dump_scoreboard import our_entry_sigs
    print("research key", key_id(mon.rpc_url()), flush=True)
    buys, uuid_refused = population(args.since)
    rank = crowd_rank()
    wallets = sorted({b["uid"] for b in buys}, key=lambda u: -(rank.get(u) if rank.get(u) is not None else -1))
    wallets = wallets[:args.wallets] if args.wallets else wallets
    out = Path(args.out)
    done = set()
    if out.exists():
        done = {json.loads(l)["key"] for l in out.read_text().splitlines() if l.strip()}
    # A buy decided less than --settle-s ago has no complete +120 s window on chain yet: leave it
    # unwritten so the next run reads it whole (the forward shadow runs on a schedule).
    settled = (__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
               - __import__("datetime").timedelta(seconds=args.settle_s)).isoformat()
    todo = [b for u in wallets for b in sorted((x for x in buys if x["uid"] == u), key=lambda x: x["decided"])
            if b["key"] not in done and b["decided"] <= settled]
    print("population: %d source buys (%d filled), %d refused J7-uuid attempts skipped; wallets %d; todo %d"
          % (len(buys), sum(b["status"] == "filled" for b in buys), uuid_refused, len(wallets), len(todo)), flush=True)
    ours = our_entry_sigs()
    with out.open("a") as fh:
        for b in todo:
            wait_flat(ro)
            try:
                rec, why = fetch(b, ours)
            except fet.Stop as exc:
                print("STOPPED:", exc, flush=True)
                break
            except OSError as exc:        # transport: skip unwritten, a rerun retries it
                print("SKIPPED %s %s: %r" % (b["handle"], b["key"][:12], exc), flush=True)
                continue
            rec = rec or {"key": b["key"], "uid": b["uid"], "handle": b["handle"], "status": b["status"],
                          "family": b["family"], "skip": why}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print("%-16s %-8s %-16s %s" % (b["handle"], b["status"], b["family"],
                                         why or "tx %d prints %d complete %s" % (rec["n_tx"], len(rec["trades"]),
                                                                                  rec["complete"])), flush=True)
    print("research-key 429s %d" % fet.RATE_429[0], flush=True)


# ---------------------------------------------------------------- simulation (pure; tested)

def path_of(rec):
    """Market prints after the source's trade, in (slot, index) order, our own prints and >5x
    outliers (vs the record's median print) removed: [(slot, idx, price)]."""
    key = (rec["S"], rec["src_idx"])
    prints = [(s, i, p) for s, i, _bt, p, _side, flag in rec["trades"] if flag != "o" and p > 0]
    if not prints:
        return []
    med = statistics.median(p for _s, _i, p in prints)
    return sorted((s, i, p) for s, i, p in prints if (s, i) > key and med / 5 <= p <= med * 5)


def source_price(rec):
    return next((p for s, i, _bt, p, _side, flag in rec["trades"] if flag == "s"), None)


def first_sell(rec):
    """(slot, idx) of the source's first own-balance decrease after its buy, or None."""
    sells = sorted((s, i) for s, i, pre, post in rec["moves"] if post < pre and (s, i) > (rec["S"], rec["src_idx"]))
    return sells[0] if sells else None


def horizon(rec):
    """Last slot the record can speak for: S + HORIZON_SLOTS when complete, else its last read slot."""
    return rec["S"] + HORIZON_SLOTS if rec["complete"] else rec["last_slot"]


def exit_from(path, entry_slot, entry_idx, entry_price, end_slot, sell_key=None, complete=True):
    """{ret, how} for an entry at (slot, idx, price): TP/stop on later prints, else the mark at
    end_slot. `sell_key` adds the mirror exit (first print one slot after the source's sell). A
    TRUNCATED record (complete=False) has no mark: without a TP/stop inside its coverage it is
    censored (ret None), never marked at wherever the read happened to stop."""
    after = [(s, i, p) for s, i, p in path if (s, i) > (entry_slot, entry_idx) and s <= end_slot]
    out = {"ret": None, "how": None, "mirror": None}
    for k, (s, i, p) in enumerate(after):
        r = p / entry_price - 1.0
        if r >= TP or r <= STOP:
            fill = next((p2 for s2, _i2, p2 in after[k + 1:] if s + 1 <= s2 <= s + EXIT_LAG_SLOTS), p)
            out.update(ret=fill / entry_price - 1.0, how="tp" if r >= TP else "stop")
            break
    if out["ret"] is None and after and complete:
        out.update(ret=after[-1][2] / entry_price - 1.0, how="mark")
    if sell_key:
        m = [(s, i, p) for s, i, p in after if s >= sell_key[0] + 1]
        if m:
            out["mirror"] = m[0][2] / entry_price - 1.0
    return out


def drop_entry(rec, path, drop, window_s):
    """('triggered', trigger, entry) | ('source_sold_first', key, None) | ('no_drop', None, None)
    | ('censored', None, None)."""
    p0 = source_price(rec)
    if not p0:
        return "censored", None, None
    w_end = rec["S"] + int(round(window_s / SLOT_S))
    if horizon(rec) < w_end:
        return "censored", None, None
    sell = first_sell(rec)
    peak = p0
    for s, i, p in path:
        if s > w_end:
            break
        if sell and (s, i) >= sell:
            return "source_sold_first", sell, None
        peak = max(peak, p)
        if p <= peak * (1.0 - drop):
            nxt = [(s2, i2, p2) for s2, i2, p2 in path if s2 >= s + 1]
            if not nxt:
                return "censored", None, None
            return "triggered", (s, i, p), nxt[0]
    return "no_drop", None, None


def boot(values, draws=5000, seed=1):
    if len(values) < 2:
        return None, None
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(draws))
    return means[int(0.025 * draws)], means[int(0.975 * draws) - 1]


def fmt(vals):
    if not vals:
        return "n=0"
    lo, hi = boot(vals)
    ci = "[%+.1f, %+.1f]" % (100 * lo, 100 * hi) if lo is not None else "[na]"
    return "n=%d mean %+.1f%% %s median %+.1f%% win %d/%d" % (
        len(vals), 100 * statistics.mean(vals), ci, 100 * statistics.median(vals), sum(v > 0 for v in vals), len(vals))


def primary_read(recs, label):
    """The pre-registered cell (W 15 s, drop 20%) with censoring bounds, on one set of records."""
    w_slots = int(round(PRIMARY_W / SLOT_S))
    print("\n%s" % label)
    for name, rs, other in (("PRIMARY refused in scope, minus entry at refusal",
                             [r for r in recs if r["status"] == "refused"
                              and r["family"] not in ("over_ceiling", "not_approved")], "_refusal"),
                            ("SECONDARY filled, minus our actual fill",
                             [r for r in recs if r["status"] == "filled"], "_actual")):
        paired, worst, best, counts = [], [], [], __import__("collections").Counter()
        for r in rs:
            how, _key, entry = drop_entry(r, r["_path"], PRIMARY_DROP, PRIMARY_W)
            counts[how] += 1
            if how != "triggered":
                continue
            e = exit_from(r["_path"], entry[0], entry[1], entry[2] * ENTRY_MARKUP, horizon(r), r["_sell"], r["complete"])
            o = r.get(other)
            if e["ret"] is not None and o is not None:
                paired.append(e["ret"] - o)
                worst.append(e["ret"] - o)
                best.append(e["ret"] - o)
            else:
                counts["triggered_exit_censored"] += 1
                d_lo = e["ret"] if e["ret"] is not None else STOP
                d_hi = e["ret"] if e["ret"] is not None else TP
                o_lo = o if o is not None else STOP
                o_hi = o if o is not None else TP
                worst.append(d_lo - o_hi)
                best.append(d_hi - o_lo)
        print("  %s: buys %d, outcomes %s" % (name, len(rs), dict(counts)))
        print("    paired (both arms read): %s" % fmt(paired))
        print("    censor bounds (triggered rows incl. exit-censored at worst / best): worst %s | best %s"
              % (fmt(worst), fmt(best)))
        if name.startswith("PRIMARY"):
            lo, _hi = boot(paired)
            ok = len(paired) >= 30 and lo is not None and lo > 0 and worst and statistics.mean(worst) > 0
            print("    PRE-REGISTERED VERDICT: %s (n_paired %d; needs >= 30, CI above 0, worst-case mean > 0)"
                  % ("POSITIVE" if ok else "NOT SHOWN", len(paired)))
    for name, rs in (("refused in scope", [r for r in recs if r["status"] == "refused"
                                           and r["family"] not in ("over_ceiling", "not_approved")]),
                     ("filled", [r for r in recs if r["status"] == "filled"]),
                     ("over_ceiling", [r for r in recs if r["family"] == "over_ceiling"])):
        peak = {True: [], False: []}
        for r in rs:
            p0 = source_price(r)
            covered = [p for s, _i, p in r["_path"] if s <= r["S"] + w_slots]
            if p0 and covered:
                peak[r["complete"]].append(max(covered) / p0)
        med = lambda v: "%.3f" % statistics.median(v) if v else "na"
        print("  censoring %-16s truncated %d of %d; peak multiple in first 15 s: complete med %s (n=%d), truncated med %s (n=%d)"
              % (name, sum(not r["complete"] for r in rs), len(rs), med(peak[True]), len(peak[True]),
                 med(peak[False]), len(peak[False])))


def report(args):
    recs = [json.loads(l) for l in Path(args.out).read_text().splitlines() if l.strip()]
    skips = [r for r in recs if r.get("skip")]
    recs = [r for r in recs if not r.get("skip")]
    print("records %d usable, %d skipped: %s" % (len(recs), len(skips),
                                                 dict(sorted(__import__("collections").Counter(r["skip"] for r in skips).items()))))
    print("  truncated at %d pages: %d; wallets %d; statuses %s" % (
        PAGES, sum(not r["complete"] for r in recs), len({r["uid"] for r in recs}),
        dict(__import__("collections").Counter(r["status"] for r in recs))))
    lags = sorted(r["our_slot"] - r["S"] for r in recs if r["status"] == "filled" and r.get("our_slot"))
    K = args.k if getattr(args, "k", None) is not None else (lags[len(lags) // 2] if lags else 7)
    print("  our actual fill lag, slots after the source: n=%d p50 %s p90 %s -> K=%d for the refusal arm"
          % (len(lags), K, lags[int(0.9 * (len(lags) - 1))] if lags else None, K))
    base = {"actual": [], "refusal": []}
    for r in recs:
        path, end, sell = path_of(r), horizon(r), first_sell(r)
        r["_path"], r["_sell"] = path, sell
        if r["status"] == "filled" and r.get("our_slot"):
            ours = [(s, i, p) for s, i, _bt, p, _side, flag in r["trades"] if flag == "o"]
            if ours:
                e = exit_from(path, ours[0][0], ours[0][1], ours[0][2], end, sell, r["complete"])
                r["_actual"] = e["ret"]
                if e["ret"] is not None:
                    base["actual"].append(e["ret"])
        if r["status"] == "refused":
            nxt = [(s, i, p) for s, i, p in path if s >= r["S"] + K]
            if nxt:
                e = exit_from(path, nxt[0][0], nxt[0][1], nxt[0][2] * ENTRY_MARKUP, end, sell, r["complete"])
                r["_refusal"] = e["ret"]
                if e["ret"] is not None:
                    base["refusal"].append(e["ret"])
    print("\nBASELINES (TP %+d / stop %d on prints, else mark at +120 s)" % (100 * TP, 100 * STOP))
    print("  actual fill (filled rows):   ", fmt(base["actual"]))
    print("  at refusal S+%d (refused):    " % K, fmt(base["refusal"]))
    seen = set(SEEN_KEYS.read_text().split()) if SEEN_KEYS.exists() else set()
    primary_read([r for r in recs if r["key"] not in seen], "PRE-REGISTERED READ on holdout keys (%d records)"
                 % sum(r["key"] not in seen for r in recs))
    primary_read([r for r in recs if r["key"] in seen], "same cell on the 189 SEEN keys (exploratory; informed the choice)")
    print("\nEXPLORATORY: every cell below")
    groups = [("ALL", recs), ("filled", [r for r in recs if r["status"] == "filled"]),
              ("refused, in scope (not over_ceiling / not_approved)",
               [r for r in recs if r["status"] == "refused" and r["family"] not in ("over_ceiling", "not_approved")]),
              ("refused over_ceiling", [r for r in recs if r["family"] == "over_ceiling"])]
    for label, rs in groups:
        print("\n%s: buys %d" % (label, len(rs)))
        for w in WINDOWS_S:
            for d in DROPS:
                trig, sold, nodrop, cens, rets, mir, paired_a, paired_r, tp = [], 0, 0, 0, [], [], [], [], 0
                for r in rs:
                    how, key, entry = drop_entry(r, r["_path"], d, w)
                    if how == "source_sold_first":
                        sold += 1
                    elif how == "no_drop":
                        nodrop += 1
                    elif how == "censored":
                        cens += 1
                    else:
                        e = exit_from(r["_path"], entry[0], entry[1], entry[2] * ENTRY_MARKUP, horizon(r), r["_sell"],
                                      r["complete"])
                        if e["ret"] is None:
                            cens += 1
                            continue
                        trig.append(r)
                        rets.append(e["ret"])
                        tp += e["how"] == "tp"
                        if e["mirror"] is not None:
                            mir.append(e["mirror"])
                        if r.get("_actual") is not None:
                            paired_a.append(e["ret"] - r["_actual"])
                        if r.get("_refusal") is not None:
                            paired_r.append(e["ret"] - r["_refusal"])
                print("  W %2ds drop %2d%%: triggered %3d | source sold first %3d | no drop %3d | censored %3d | TP %d | %s"
                      % (w, 100 * d, len(trig), sold, nodrop, cens, tp, fmt(rets)))
                if paired_a or paired_r:
                    print("      paired minus actual: %s | minus refusal-entry: %s | mirror exit: %s"
                          % (fmt(paired_a), fmt(paired_r), fmt(mir)))


def selftest():
    rec = {"S": 100, "src_idx": 5, "complete": True, "last_slot": 558,
           "trades": [[100, 5, 0, 1.00, "buy", "s"], [100, 9, 0, 1.30, "buy", ""], [101, 2, 0, 1.50, "buy", ""],
                      [102, 0, 0, 1.40, "buy", "o"], [103, 1, 0, 1.10, "sell", ""], [104, 0, 0, 1.12, "buy", ""],
                      [110, 0, 0, 1.45, "buy", ""], [120, 0, 0, 90.0, "buy", ""], [300, 0, 0, 1.20, "sell", ""]],
           "moves": [[200, 0, 100, 40]]}
    path = path_of(rec)
    assert [p for _s, _i, p in path] == [1.30, 1.50, 1.10, 1.12, 1.45, 1.20], path   # ours + 90x outlier gone
    assert first_sell(rec) == (200, 0)
    how, trig, entry = drop_entry(rec, path, 0.20, 5)
    assert how == "triggered" and trig == (103, 1, 1.10) and entry == (104, 0, 1.12), (how, trig, entry)
    assert drop_entry(rec, path, 0.40, 60)[0] == "source_sold_first"          # no 40% drop before slot 200
    assert drop_entry(rec, path, 0.40, 5)[0] == "no_drop"
    assert drop_entry({**rec, "complete": False, "last_slot": 110}, path, 0.20, 5)[0] == "censored"
    e = exit_from(path, 104, 0, 1.12 * ENTRY_MARKUP, 558, (200, 0))
    assert e["how"] == "tp" and abs(e["ret"] - (1.45 / (1.12 * ENTRY_MARKUP) - 1)) < 1e-12, e   # no print in lag
    spike = [(104, 0, 1.12), (110, 0, 5.00), (112, 0, 1.40), (300, 0, 1.20)]
    e3 = exit_from(spike, 104, 0, 1.12, 558)
    assert e3["how"] == "tp" and abs(e3["ret"] - (1.40 / 1.12 - 1)) < 1e-12, e3    # fills at the next print
    assert abs(e["mirror"] - (1.20 / (1.12 * ENTRY_MARKUP) - 1)) < 1e-12
    e2 = exit_from(path, 110, 0, 1.45, 558)
    assert e2["how"] == "mark" and abs(e2["ret"] - (1.20 / 1.45 - 1)) < 1e-12, e2
    assert exit_from(path, 110, 0, 1.45, 558, complete=False)["ret"] is None       # truncated: censored
    assert family("refused", "mcap $300,000 over ceiling $250,000 (over_ceiling)") == "over_ceiling"
    assert family("filled", "x") == "filled" and family("refused", "") == "other"
    assert SIG.match("5Bs1SNBmVJJYLyXwY3A9ZK1hthuiVjm3wz7GpPZpAe7jtPxWLxoRbvnaLXt3XPnr2vP3jEZHU4XitWhMewoZCn28")
    assert not SIG.match("E7pskdV9xCCMXtsu747tjjFEMq9Fwwnvwo6fApQWBwxP")      # a 44-char address
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("run")
    p.add_argument("--out", required=True)
    p.add_argument("--since", default="2026-09-21")
    p.add_argument("--wallets", type=int, default=0, help="top-N wallets by copier crowd (0 = all)")
    p.add_argument("--settle-s", type=float, default=300.0, help="skip buys decided less than this ago")
    q = sub.add_parser("report")
    q.add_argument("--out", required=True)
    q.add_argument("--k", type=int, help="freeze the refusal-arm lag in slots (the forward read uses 7)")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run(a) if a.cmd == "run" else report(a)



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('primary_read', 'report', 'main'))

if __name__ == "__main__":
    main()
