#!/usr/bin/env python3
"""M1 of the S-landing spec (2026-10-09): replay recorded slot S on each slot-lane send and price a
hypothetical buy of our clip at each position, net of each cost tier. Local tape reads only: no RPC,
no money, one job, every tape read by (mint, slot) on its index.

Per send (memecoin_entry_attempts, slot lane, a tx_sig) of a wallet:
- the source's own swap in the signer-window tape (pool_swaps), its pool (coin_vault), its post-trade
  price p0 and block time;
- POSITIONS in S on the same pool: right after the source (k=0), after the k-th later swap (k=1..3),
  and at S's end;
- ENTRY: on a constant-product row (cp=1) with a SOL or USDC quote, our clip's own impact on the
  reserves at that position (fee_bps, default 125); otherwise price-only (impact unmodelled, flagged).
  p0 and every position are on the same basis: the recorded reserves after each swap (the tape's
  `price` is a swap's own execution price, never a spot). BIAS: a PumpSwap pool's vault reserves omit
  its virtual quote, so impact and price moves read LARGER than they are there (flagged by venue);
- the CAP: the buy refuses (reverts) when our average price exceeds p0 x (1 + --cap-pct/100); a
  landed revert pays its priority fee;
- EXIT at +30 s and +60 s after the source's block time: the same pool's last recorded state at or
  before it, our tokens sold into those reserves (cp) or at that price;
- COST tiers in SOL per landed send (--tiers);
- TODAY: realized PnL when filled, priority cost when confirmed failed; unresolved sends stay unknown.
- ANY S: every swap boundary in S priced, per send the worst (s_min, the go line) and the mean (s_avg).
- CENSORED: a send whose tape did not read S densely and every slot to +60 s at bucket <= 12 (tape_gap).
- FEED HEAD PROXY: source receipt before the local S head, or <=80 ms after its notification. Delayed
  delivery cannot identify bank phase; bank feasibility and fee movability remain censored.
- BIAS: the k-positions and the late arm sell into recorded reserves that lack our own buy, so their
  proceeds read LOW; a filled send's own swap is on the tape, so TODAY has no such gap. Against S-landing.
Wallets are reported one by one, never pooled. Option A's go/no-go: the share q of sends that must move
from today's outcome to a position for the tier to beat today, q* = dfee / (G - B + dfee).

    python3 scripts/research/s_landing_replay.py --since 2026-09-26 --uid aefe2ddd-c580-5245-a2f5-e4ed62f7ef10
    python3 scripts/research/s_landing_replay.py --since 2026-09-26 --min-sends 5     # every armed wallet
"""
import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parent)]

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
POSITIONS = ("k0", "k1", "k2", "k3", "end")
ANY_S = ("s_min", "s_avg")         # over EVERY swap boundary in S: the worst one, and their uniform mean
EARLY_MS = 80.0                    # local head-notification age only; no bank-phase inference
SLOT_S = 0.38                      # a fast slot, so the exit window's slot span is not undercounted
EXIT_MAX_BUCKET = 12               # the tape's coverage width allowed over [S, exit]: entry 0, minute 5, path 12
HORIZONS = (30, 60)


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def sends(ledger, since, uid=None):
    sql = ("SELECT a.source_user_id, coalesce(w.handle, a.source_handle), a.attempt_id, a.origin_event_id, a.mint, "
           "a.terminal_status, a.position_id, a.decision_started_at, a.trace_json, "
           "a.chain_confirmation_completed_at FROM memecoin_entry_attempts a "
           "LEFT JOIN memecoin_wallets w ON w.source_user_id=a.source_user_id WHERE a.network_id='solana' "
           "AND a.decision_started_at>=? AND a.delivery_lane LIKE 'slot_lane%' AND a.tx_sig IS NOT NULL")
    args = [since]
    if uid:
        sql += " AND a.source_user_id=?"
        args.append(uid)
    return ledger.execute(sql + " ORDER BY a.decision_started_at", args).fetchall()


def feed_head_proxy(trace_json):
    """Local head-notification timing only; delayed delivery leaves bank phase unknown."""
    try:
        events = (json.loads(trace_json or "{}").get("substage_events") or ())
    except ValueError:
        return None
    for ev in events:
        if ev.get("name") == "slot_receipt":
            minus, age = ev.get("head_at_recv_minus_s"), ev.get("head_at_recv_age_ms")
            if minus is None:
                return None
            if minus < 0:
                return True
            if minus > 0:
                return False
            return None if age is None else float(age) <= EARLY_MS
    return None


def tape_complete(tape, mint, S, horizon_s):
    """The signal-window tape read S densely and every slot to the exit at bucket <= EXIT_MAX_BUCKET, so
    a missing swap there is a quiet pool, not an unread page (a sampled bucket can still hide its last
    ~2 s on a busy pool)."""
    import sol_signal_window_tape as tw
    cov = [tuple(r) for r in tape.execute("SELECT lo, hi, bucket FROM coverage WHERE mint=?", (mint,))]
    return (not tw.uncovered(cov, S, S, 0)
            and not tw.uncovered(cov, S, S + int(math.ceil(horizon_s / SLOT_S)), EXIT_MAX_BUCKET))


def armed_solana(ledger):
    from sniper.persistence import db_memecoin as m
    ledger.row_factory = sqlite3.Row
    try:
        return m.real_approved_user_ids(ledger, network_id="solana")
    finally:
        ledger.row_factory = None


def pool_rows(tape, mint, s_slot, horizon_s):
    return tape.execute(
        "SELECT slot, tx_index, signature, price, coin_vault, quote_mint, quote_decimals, coin_decimals, "
        "coin_reserve_after, quote_reserve_after, cp, fee_bps, block_time FROM pool_swaps "
        "WHERE mint=? AND slot>=? AND slot<=? AND price IS NOT NULL ORDER BY slot, tx_index",
        (mint, s_slot, s_slot + int(horizon_s / 0.4) + 400)).fetchall()


def quote_usd(quote_mint, sol_usd):
    return sol_usd if quote_mint == WSOL else 1.0 if quote_mint == USDC else None


def state(row):
    """(price, coin reserve, quote reserve, cp, fee) in human units for one recorded swap."""
    price = num(row[3])
    qd, cd = row[6], row[7]
    coin = num(row[8]) / 10 ** cd if row[8] is not None and cd is not None else None
    quote = num(row[9]) / 10 ** qd if row[9] is not None and qd is not None else None
    return price, coin, quote, bool(row[10]), (row[11] if row[11] is not None else 125) / 10_000.0


def buy(st, q_in):
    """(tokens, average price, impact modelled) for q_in quote units into a recorded state."""
    price, coin, quote, cp, fee = st
    if cp and coin and quote and q_in is not None:
        net = q_in * (1 - fee)
        tokens = coin * net / (quote + net)
        return tokens, q_in / tokens, True
    return q_in / (price * (1 + fee)), price * (1 + fee), False


def sell(st, tokens):
    price, coin, quote, cp, fee = st
    if cp and coin and quote:
        return quote * tokens / (coin + tokens) * (1 - fee)
    return tokens * price * (1 - fee)


def q_star(gain, today, dfee):
    """The share q of sends moved from today's outcome to `gain` (both net of their own fee; the tier's
    extra fee dfee is paid on every send) at which q * gain + (1 - q) * (today - dfee) = today."""
    denom = gain - today + dfee
    return round(dfee / denom, 3) if denom > 0 else None


def selftest():
    st = (1.0, 1_000_000.0, 1_000.0, True, 0.0125)            # cp pool, spot 0.001, 125 bps
    tokens, avg, modelled = buy(st, 10.0)
    assert modelled and avg > 0.001                           # our own impact raises the average
    after = (None, st[1] - tokens, st[2] + 10.0 * (1 - st[4]), True, st[4])
    back = sell(after, tokens)
    assert 10.0 * (1 - st[4]) ** 2 * 0.99 < back < 10.0 * (1 - st[4]) ** 2 + 1e-9   # round trip pays both fees
    q = q_star(10.0, -5.0, 5.0)                               # break-even: 0.25 * 10 + 0.75 * (-10) = -5
    assert q == 0.25 and q_star(-11.0, -5.0, 5.0) is None    # a gain below today net of the fee never pays
    def tr(minus, age):
        return json.dumps({"substage_events": [{"name": "slot_receipt", "head_at_recv_minus_s": minus,
                                                "head_at_recv_age_ms": age}]})
    assert [feed_head_proxy(tr(m, a)) for m, a in ((-1, None), (0, 80.0), (0, 80.1), (1, 5.0), (None, 5.0))] == \
        [True, True, False, False, None] and feed_head_proxy("{}") is None
    print("selftest ok")


def replay_send(tape, send, *, clip_usd, sol_usd, cap_pct, tiers, today_priority_sol, ledger,
                max_exit_age_s=60.0):
    uid, handle, aid, origin, mint, status, position_id, at, trace_json = send[:9]
    confirmed_at = send[9] if len(send) > 9 else None
    source = origin.split(":", 1)[1]
    signal = tape.execute("SELECT s_slot, landing_sig, landing_slot FROM signals WHERE signal_key=? "
                          "AND source_sig=? AND mint=?", (source + ":" + mint, source, mint)).fetchone()
    if not signal or signal[0] is None:
        return dict(attempt_id=aid, censored="no_tape_row")
    rows = pool_rows(tape, mint, signal[0], max(HORIZONS))
    src = [r for r in rows if r[2] == source]
    if not src:
        return dict(attempt_id=aid, censored="no_source_swap")
    s_row = src[-1]
    pool, S, ix0, bt0 = s_row[4], s_row[0], s_row[1], num(s_row[12])
    if bt0 is None:
        return dict(attempt_id=aid, censored="source_clock_unknown")
    if not tape_complete(tape, mint, S, max(HORIZONS)):
        return dict(attempt_id=aid, censored="tape_gap")
    # p0 is the post-trade SPOT on the same basis as our modelled buy: a constant-product row's reserves
    # after the source (the tape's `price` column is that swap's own execution price, not a spot).
    _p, coin0, quote0, cp0, _f = state(s_row)
    p0 = quote0 / coin0 if cp0 and coin0 and quote0 else num(s_row[3])
    basis = "reserves" if cp0 and coin0 and quote0 else "execution_price"
    same = [r for r in rows if r[4] == pool]
    # Dense slots do not establish a timed exit state. Require a source-bound clock and an
    # explicit age bound for every paired horizon; never substitute the modeled entry state.
    exits_by_horizon = {}
    for h in HORIZONS:
        exits = [r for r in same if num(r[12]) is not None and bt0 <= num(r[12]) <= bt0 + h]
        if not exits:
            return dict(attempt_id=aid, censored="horizon_exit_unknown:%s" % h)
        ex = max(exits, key=lambda r: (num(r[12]), r[0], r[1]))
        if bt0 + h - num(ex[12]) > max_exit_age_s:
            return dict(attempt_id=aid, censored="stale_horizon_exit:%s" % h)
        exits_by_horizon[h] = ex
    later_in_s = [r for r in same if r[0] == S and r[1] > ix0]
    at_pos = {"k0": s_row}
    for k in (1, 2, 3):
        at_pos["k%d" % k] = later_in_s[k - 1] if len(later_in_s) >= k else (later_in_s[-1] if later_in_s else s_row)
    at_pos["end"] = later_in_s[-1] if later_in_s else s_row
    qu = num(quote_usd(s_row[5], sol_usd))
    if qu is None or qu <= 0:
        return dict(attempt_id=aid, censored="quote_usd_unknown")
    q_in = clip_usd / qu
    # A signed broadcast rejection also says failed; only a recorded chain confirmation proves fees.
    confirmed_failure = status == "failed" and bool(confirmed_at)
    today_usd = None
    if status == "filled" and position_id:
        got = ledger.execute("SELECT realized_pnl FROM memecoin_positions WHERE id=?", (position_id,)).fetchone()
        today_usd = num(got[0]) if got else None
    elif confirmed_failure:
        today_usd = -today_priority_sol * sol_usd
    out = dict(attempt_id=aid, at=at, status=status, mint=mint, pool=pool, quote_mint=s_row[5], basis=basis,
               venue_cp=cp0, feed_head_proxy=feed_head_proxy(trace_json), bank_feasible=None, fee_movable=None,
               later_swaps_in_s=len(later_in_s), today_usd=today_usd, positions={})

    def priced(row):
        st = state(row)
        tokens, avg, modelled = buy(st, q_in)
        within = avg <= p0 * (1 + cap_pct / 100.0)
        res = dict(avg_vs_p0_pct=round((avg / p0 - 1) * 100, 2), within_cap=within, impact_modelled=modelled)
        for h in HORIZONS:
            ex = exits_by_horizon[h]
            ret = None
            if within:
                proceeds = sell(state(ex), tokens)
                ret = proceeds / q_in - 1
            res["ret_%ds" % h] = None if ret is None else round(ret * 100, 2)
            res["exit_age_%ds" % h] = (bt0 + h) - num(ex[12])
            for tier in tiers:
                cost = tier * sol_usd
                res["pnl_%ds_%s" % (h, tier)] = round((ret * clip_usd if within else 0.0) - cost, 2)
        return res
    for name in POSITIONS:
        out["positions"][name] = priced(at_pos[name])
    # ANY S (Gamma): prices inside S are not monotone, so landing "somewhere in S" is priced over every
    # swap boundary there: the worst one per send (the go line) and their uniform mean (a variant).
    every = [priced(r) for r in [s_row] + later_in_s]
    keys = [k for k in every[0] if k.startswith("pnl_")]
    out["positions"]["s_min"] = dict({k: min(b[k] for b in every) for k in keys},
                                     within_cap=all(b["within_cap"] for b in every), boundaries=len(every))
    out["positions"]["s_avg"] = dict({k: round(sum(b[k] for b in every) / len(every), 2) for k in keys},
                                     within_cap=sum(b["within_cap"] for b in every) / len(every))
    # THE BASELINE ARM (Gamma): our own landing slot, NO cap, the same exit rule. Entry is the pool's
    # state entering our landing slot (an optimistic bound: we filled no earlier than that). S-landing's
    # value is its increment over this arm; if this arm pays, a per-wallet cap lift is the cheaper test.
    land_slot = signal[2]
    if land_slot is None or (status != "filled" and not confirmed_failure):
        out["late_uncapped"] = None
    else:
        before = [r for r in same if r[0] < land_slot]
        st = state(before[-1] if before else s_row)
        tokens, avg, _m = buy(st, q_in)
        late = {"landing_offset_slots": land_slot - S, "avg_vs_p0_pct": round((avg / p0 - 1) * 100, 2)}
        for h in HORIZONS:
            ex = exits_by_horizon[h]
            ret = sell(state(ex), tokens) / q_in - 1
            late["ret_%ds" % h] = round(ret * 100, 2)
            for tier in tiers:
                late["pnl_%ds_%s" % (h, tier)] = round(ret * clip_usd - tier * sol_usd, 2)
        out["late_uncapped"] = late
    # TODAY, REPLAYED with the same exit rule: our own landed swap on this pool, bought into the state
    # just before it; a send that landed without a fill (max price) costs today's priority only.
    landing = signal[1]
    if confirmed_failure:
        out["today_replayed"] = {"pnl_%ds" % h: round(-today_priority_sol * sol_usd, 2) for h in HORIZONS}
    elif status != "filled":
        out["today_replayed"] = None
    else:
        ours = [i for i, r in enumerate(same) if r[2] == landing]
        if ours and ours[0] > 0:
            before = same[ours[0] - 1]
            st = state(before)
            tokens, avg, _m = buy(st, q_in)
            rep = {}
            for h in HORIZONS:
                ex = exits_by_horizon[h]
                ret = sell(state(ex), tokens) / q_in - 1
                rep["pnl_%ds" % h] = round(ret * clip_usd - today_priority_sol * sol_usd, 2)
            rep["landing_offset_slots"] = same[ours[0]][0] - S
            out["today_replayed"] = rep
        else:
            out["today_replayed"] = None
    return out


def wallet_report(rows, tiers, sol_usd, today_priority_sol):
    graded = [r for r in rows if "positions" in r]
    rep = dict(sends=len(rows), graded=len(graded),
               censored={c: sum(1 for r in rows if r.get("censored") == c) for c in {r.get("censored") for r in rows} if c},
               today_total_usd=round(sum(r["today_usd"] or 0 for r in graded), 2),
               today_known=sum(1 for r in graded if r["today_usd"] is not None), positions={})
    rep["today_unknown"] = len(graded) - rep["today_known"]
    for pos in POSITIONS + ANY_S:
        cell = {"within_cap": sum(r["positions"][pos]["within_cap"] for r in graded)}
        for h in HORIZONS:
            for tier in tiers:
                key = "pnl_%ds_%s" % (h, tier)
                cell[key + "_total"] = round(sum(r["positions"][pos][key] for r in graded), 2)
        rep["positions"][pos] = cell
    # TODAY under the same +30/+60 s exit rule, on the sends whose landing replays (apples to apples).
    base = [r for r in graded if r.get("today_replayed")]
    rep["today_replayed_n"] = len(base)
    for h in HORIZONS:
        rep["today_replayed_%ds_total" % h] = round(sum(r["today_replayed"]["pnl_%ds" % h] for r in base), 2)
    # Option A's go/no-go per tier and horizon, on the same sends (B is today replayed): q_star is the
    # share that must move to k0 (before S's first later swap), q_star_s_end the share that must move to
    # S's end (landing anywhere in S, priced after S's last recorded swap). Both under one exit rule.
    def go_cells(sub):
        if not sub:
            return {}
        n = len(sub)
        go = {}
        for h in HORIZONS:
            for tier in tiers:
                if tier <= today_priority_sol:
                    continue
                B = sum(r["today_replayed"]["pnl_%ds" % h] for r in sub) / n
                dfee = (tier - today_priority_sol) * sol_usd
                cell = dict(n=len(sub), B_per_send=round(B, 2), extra_fee_per_send=round(dfee, 2))
                for pos in ("k0", "end", "s_min", "s_avg"):
                    g = sum(r["positions"][pos]["pnl_%ds_%s" % (h, tier)] for r in sub) / n
                    cell["G_%s_per_send" % pos], cell["q_star_%s" % pos] = round(g, 2), q_star(g, B, dfee)
                go["%ds_%s" % (h, tier)] = cell
        return go
    rep["option_a_go_no_go"] = go_cells(base)
    rep["feed_head_proxy_share"] = dict(early=sum(1 for r in base if r.get("feed_head_proxy") is True),
                                      late=sum(1 for r in base if r.get("feed_head_proxy") is False),
                                      unknown=sum(1 for r in base if r.get("feed_head_proxy") is None))
    rep["bank_feasibility"] = rep["fee_movability"] = "censored_missing_bank_phase_witness"
    rep["option_a_go_no_go_early"] = None
    late = [r for r in graded if r.get("late_uncapped")]
    rep["late_uncapped_n"] = len(late)
    for h in HORIZONS:
        rep["late_uncapped_%ds_total" % h] = round(sum(r["late_uncapped"]["pnl_%ds_%s" % (h, tiers[0])]
                                                     for r in late), 2)
        rep["late_uncapped_%ds_up" % h] = sum(1 for r in late if r["late_uncapped"]["ret_%ds" % h] > 0)
        # k0's increment over the uncapped late arm on the SAME sends, at each tier.
        for tier in tiers:
            rep["k0_minus_late_%ds_%s" % (h, tier)] = round(
                sum(r["positions"]["k0"]["pnl_%ds_%s" % (h, tier)] - r["late_uncapped"]["pnl_%ds_%s" % (h, tiers[0])]
                    for r in late), 2)
    return rep


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--since")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--uid")
    p.add_argument("--min-sends", type=int, default=5)
    p.add_argument("--clip-usd", type=float, default=110.0)
    p.add_argument("--sol-usd", type=float, default=110.0)
    p.add_argument("--cap-pct", type=float, default=30.0)
    p.add_argument("--tiers", default="0.0015,0.010,0.150")
    p.add_argument("--max-exit-age-s", type=float, default=60.0,
                   help="censor paired replay if a timed horizon exit exceeds this age")
    p.add_argument("--out")
    a = p.parse_args()
    if a.selftest:
        return selftest()
    if not a.since:
        p.error("--since is required")
    from sniper.util.state_paths import state_root
    root = state_root()
    tiers = tuple(float(x) for x in a.tiers.split(","))
    ledger = sqlite3.connect(f"file:{root / 'sniper_tracker.db'}?mode=ro", uri=True, timeout=30)
    ledger.row_factory = None
    tape = sqlite3.connect(f"file:{root / 'data' / 'research_signal_windows_solana.db'}?mode=ro", uri=True, timeout=30)
    rows = sends(ledger, a.since, a.uid)
    by = {}
    for r in rows:
        by.setdefault((r[0], r[1]), []).append(r)
    armed = armed_solana(ledger) if not a.uid else None
    report = dict(since=a.since, clip_usd=a.clip_usd, sol_usd=a.sol_usd, cap_pct=a.cap_pct, tiers=tiers,
                  max_exit_age_s=a.max_exit_age_s, wallets={})
    for (uid, handle), group in sorted(by.items(), key=lambda kv: -len(kv[1])):
        if not a.uid and (len(group) < a.min_sends or uid not in armed):
            continue
        replayed = [replay_send(tape, s, clip_usd=a.clip_usd, sol_usd=a.sol_usd, cap_pct=a.cap_pct, tiers=tiers,
                                today_priority_sol=tiers[0], ledger=ledger,
                                max_exit_age_s=a.max_exit_age_s) for s in group]
        report["wallets"][handle or uid] = dict(uid=uid, summary=wallet_report(replayed, tiers, a.sol_usd, tiers[0]),
                                               sends=replayed)
    text = json.dumps(report, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    json.dump({w: v["summary"] for w, v in report["wallets"].items()}, sys.stdout, indent=1, default=str)
    print()


if __name__ == "__main__":
    main()
