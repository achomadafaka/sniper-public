#!/usr/bin/env python3
"""Can J7 be retired per chain? Did our own watchers see every J7 event from an armed wallet?

Operator order 2026-09-28 (via Alpha): "retire J7 on every chain if we can see everything without
it". Read-only, ledger only. For every J7 event (signal_source 'follow_activity') from a wallet
armed on that chain, per chain and per SIDE (buys open positions; sells drive mirror exits), was
the same source transaction seen by one of our lanes, and first or later?

  Our sightings, joined on the source transaction:
    Solana buy   onchain_wallet_detections.signature == the J7 signature (watcher + routed rows),
                 or a slot-lane attempt 'slotlane:<sig>'
    Solana sell  onchain_wallet_sells_v2.signature == the J7 signature (watcher sell rows)
    EVM buy      onchain_wallet_detections.signature '<chain>:<tx>:<token>', tx lower-cased
    EVM sell     no writer exists: the EVM watcher records buys only
  Timing, J7 = memecoin_events.received_at (CORRECTED clock, observed_real_utc):
    watcher/routed detected_at is the HOST clock, so (ours - J7) is a mixed-clock pair and reads
      our lane LATE by the J7 clock offset (host - corrected, data/clock_offset.json, ~+0.05 s);
    slot-lane decision_started_at is the corrected clock, same as J7, and is a decision (after
      the receipt), so a slot-lane "first" is conservative.
  A miss is one of:
    not_watched    the J7 trader address is not in the watcher's address set (the same set the
                   watcher arms from), and the wallet has no watched address on this chain
    other_address  the wallet IS watched on this chain, but J7 attributes the trade to another
                   address (a bot/router wallet the watcher does not subscribe to)
    watched_miss   the J7 trader address is watched and no lane recorded the transaction
    no_tx          the J7 id carries no transaction (a 'swap:<uuid>' relay) AND no lane row of the
                   same wallet, mint and side lies within +-30 s (the fallback join for those ids)
  An EVM watched_miss is split by the watcher's own stamped outcome line for that transaction
  (logs/onchain_evm_armed[.<chain>].log, 10-hex prefix, since b494ff21 every delivered transfer
  writes one): 'delivered:<decode>' = the transfer ARRIVED (arrival host clock) and was not
  decoded as a buy; 'not_delivered' = no line at all.

Armed = armed NOW (memecoin_wallets.real_chains, the lanes' own definition); the approvals ledger
rows inside the window are printed so a roster change is visible. Watched = the current address
set: armed_verified_owners (Solana) / watcher_address_owners x armed (EVM).

    python3 scripts/research/j7_retire_census.py [--list 12]
    python3 scripts/research/j7_retire_census.py --selftest
"""
import argparse
import collections
import contextlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
LEDGER = str(REPO / "sniper_tracker.db")
FALLBACK_S = 30.0
# J7 labels a SOL -> USDC swap as a "sell" of mint So111... (and USDC -> SOL as a "buy" of it). Those
# are quote-asset legs, not coin trades: the watcher's classify_sell excludes the same mints, so
# no lane can ever "see" them, and counting them as misses read as a sell-classifier gap (Beta,
# 2026-09-28: 8 of 9 Solana sell misses; frames of StonkyScheffler 5iCy5Mcv/4yQU9fxT: -56.85 SOL
# native, +6,736.76 USDC through DFlow, no coin moved). Same set as onchain_wallet_watch.QUOTE_MINTS.
SOLANA_QUOTE_LEGS = frozenset({"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                               "So11111111111111111111111111111111111111112",
                               "11111111111111111111111111111111"})


def is_quote_leg(chain, mint):
    """True for a J7 event whose 'coin' is the Solana quote asset itself."""
    return chain == "solana" and str(mint or "") in SOLANA_QUOTE_LEGS
EVM_LOGS = {"4663": "onchain_evm_armed.log", "56": "onchain_evm_armed.56.log", "8453": "onchain_evm_armed.8453.log"}
TRANSFER = re.compile(r"^(\S+) \[onchain-evm\] transfer (0x[0-9a-f]{10}) arrival=(\S+) outcome=(\S+).*?(?:decode=(\S+))?$")
# Each chain's window starts at its latest watcher fix (Alpha, 2026-09-28): Solana at the pinned
# watcher's launch (f64457da, 12:38:04Z); EVM at the d255f7cd launch that carried b494ff21 (null
# read retry, live since 11:08Z) and b9f143eb (read-source host), 11:26:45Z.
WINDOWS = {"solana": "2026-09-28T12:38:04+00:00", "4663": "2026-09-28T11:26:45+00:00",
           "8453": "2026-09-28T11:26:45+00:00", "56": "2026-09-28T11:26:45+00:00"}


def utc(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def parse_j7(fomo_event_id, source_user_id):
    """(tx, trader) from a J7 id. swap:raybot:<tx>:<trader>:<mint>; pump:<sig>:<mint>:<side>:...
    (a pump id names no trader: a 'pump:<address>' profile id IS the wallet, else None)."""
    parts = str(fomo_event_id or "").split(":")
    if parts[:2] == ["swap", "raybot"] and len(parts) >= 5:
        return parts[2], parts[3]
    if parts[0] == "pump" and len(parts) >= 3:
        uid = str(source_user_id or "")
        return parts[1], (uid[5:] if uid.startswith("pump:") else None)
    return None, None


def arming_timeline(rows):
    """{uid: [(created_at, armed bool), ...]} for ONE chain from memecoin_wallet_approvals rows
    (created_at, source_user_id, action, chain), oldest first: chain_arm / chain_revoke on this
    chain only. Arming is per chain since 2026-07-30; a global approve arms no chain
    (real_chains NULL is armed nowhere), so chain-NULL rows are not arming evidence. Counting them
    put wallets never armed on Solana into the census (n=203 against the true ~80)."""
    out = collections.defaultdict(list)
    for at, uid, action, _chain in rows:
        if action == "chain_arm":
            out[str(uid)].append((str(at), True))
        elif action == "chain_revoke":
            out[str(uid)].append((str(at), False))
    return out


def armed_at_buy(timeline, armed_now, uid, at):
    """Was `uid` armed on this chain at `at`? ARMED-AT-BUY (Alpha, 2026-09-28): the operator's
    21:53Z roster cut disarmed 288 (wallet, chain) pairs, and an armed-NOW filter would have
    dropped every earlier event of those wallets and kept events of wallets armed later.
    The last event at or before `at` decides. With none, the first later event is a CHANGE, so
    the wallet held the opposite state before it; with no event at all, today's state holds."""
    events = timeline.get(str(uid), ())
    before = [on for when, on in events if when <= str(at)]
    if before:
        return before[-1]
    if events:
        return not events[0][1]
    return str(uid) in armed_now


def classify(seen_ms, trader, watched_addrs, wallet_watched):
    """One bucket for one J7 event. seen_ms: (ours - J7) in ms, or None when no lane saw it."""
    if seen_ms is not None:
        return "first" if seen_ms < 0 else "later"
    if trader is not None and trader.lower() in watched_addrs:
        return "watched_miss"
    return "other_address" if wallet_watched else "not_watched"


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))] if xs else None


def load(conn):
    from sniper.persistence import db_memecoin as m
    from sniper.services.axiom_address_verify import watcher_address_owners
    from sniper.services.memecoin_address_registry import armed_verified_owners
    out = {}
    owners_evm = watcher_address_owners(conn)
    handles = {str(r[0]): r[1] for r in conn.execute("SELECT source_user_id, handle FROM memecoin_wallets")}
    for chain, since in WINDOWS.items():
        armed = {str(u) for u in m.real_approved_user_ids(conn, network_id=chain)}
        timeline = arming_timeline(conn.execute(
            "SELECT created_at, source_user_id, action, chain FROM memecoin_wallet_approvals "
            "WHERE chain=? ORDER BY created_at", (chain,)).fetchall())
        # The address map covers every wallet that was armed at some point in the window, so an
        # event from a wallet disarmed since is still classified against its own addresses.
        ever = armed | set(timeline)
        if chain == "solana":
            watched = {a.lower(): uid for a, (uid, _h) in (armed_verified_owners(conn, armed=ever) or {}).items()}
        else:
            watched = {str(a).lower(): uid for (ch, a), (uid, _h) in owners_evm.items()
                       if str(ch) == chain and uid in ever}
        events = [dict(r) for r in conn.execute(
            "SELECT id, fomo_event_id, source_user_id, source_handle, mint, side, received_at, delivery_lane "
            "FROM memecoin_events WHERE signal_source='follow_activity' AND network_id=? AND received_at>=? "
            "ORDER BY received_at", (chain, since))]
        det, sells, slot = {}, {}, {}
        by_coin = collections.defaultdict(list)       # (uid, mint, side) -> [(at, via)] for uuid ids

        def keep(table, key, at, via, uid, mint, side):
            if key not in table or at < table[key][0]:
                table[key] = (at, via)
            by_coin[(str(uid), str(mint).lower(), side)].append((at, via))
        for sig, at, val, uid, mint in conn.execute(
                "SELECT signature, detected_at, source_valuation_json, source_user_id, mint "
                "FROM onchain_wallet_detections WHERE network_id=? AND detected_at>=?", (chain, since)):
            key = sig if chain == "solana" else str(sig).split(":")[1].lower()
            backfill = '"backfill": true' in (val or "") or '"backfill":true' in (val or "")
            keep(det, key, utc(at), "backfill" if backfill else "watcher", uid, mint, "buy")
        if chain == "solana":
            for sig, at, uid, mint in conn.execute(
                    "SELECT signature, detected_at, source_user_id, mint FROM onchain_wallet_sells_v2 "
                    "WHERE network_id='solana' AND detected_at>=?", (since,)):
                keep(sells, sig, utc(at), "watcher_sell", uid, mint, "sell")
            for origin, at, uid, mint in conn.execute(
                    "SELECT origin_event_id, decision_started_at, source_user_id, mint FROM memecoin_entry_attempts "
                    "WHERE signal_source='slot_lane' AND decision_started_at>=?", (since,)):
                sig = str(origin).split(":", 1)[1] if str(origin).startswith("slotlane:") else None
                if sig:
                    keep(slot, sig, utc(at), "slot_lane", uid, mint, "buy")
        arrivals = {}
        if chain in EVM_LOGS and (REPO / "logs" / EVM_LOGS[chain]).exists():
            with open(REPO / "logs" / EVM_LOGS[chain], errors="replace") as fh:
                for line in fh:
                    mt = TRANSFER.match(line.rstrip("\n"))
                    if mt and mt.group(2) not in arrivals:
                        arrivals[mt.group(2)] = (utc(mt.group(3)), mt.group(4), mt.group(5))
        held = [(str(r[0]), str(r[1]).lower(), utc(r[2]), utc(r[3]) if r[3] else None) for r in conn.execute(
            "SELECT source_user_id, mint, opened_recorded_at, closed_at FROM memecoin_positions "
            "WHERE execution_mode='real' AND network_id=? AND opened_recorded_at IS NOT NULL "
            "AND (closed_at IS NULL OR closed_at>=?)", (chain, since))]
        roster = [tuple(r) for r in conn.execute(
            "SELECT created_at, source_user_id, action FROM memecoin_wallet_approvals "
            "WHERE chain=? AND created_at>=? ORDER BY created_at", (chain, since))]
        out[chain] = dict(since=since, armed=armed, timeline=timeline, watched=watched, events=events, det=det, sells=sells,
                          slot=slot, held=held, roster=roster, handles=handles,
                          by_coin=by_coin, arrivals=arrivals)
    return out


def census(data, list_n):
    for chain, d in data.items():
        wallet_watched = set(d["watched"].values())
        print("\n== %s  since %s  armed %d  watched addresses %d (wallets %d)" % (
            chain, d["since"][:19], len(d["armed"]), len(d["watched"]), len(wallet_watched)))
        if d["roster"]:
            print("   roster changes in window: %s" % collections.Counter(a for _t, _u, a in d["roster"]).most_common())
        cells = collections.defaultdict(list)
        quote_legs = collections.Counter()
        for ev in d["events"]:
            uid = str(ev["source_user_id"])
            if not armed_at_buy(d["timeline"], d["armed"], uid, ev["received_at"]):
                continue
            side = str(ev["side"] or "").lower()
            if is_quote_leg(chain, ev["mint"]):
                quote_legs[side] += 1
                continue
            tx, trader = parse_j7(ev["fomo_event_id"], uid)
            t_j7 = utc(ev["received_at"])
            ours, via = None, None
            if tx is not None:
                key = tx if chain == "solana" else tx.lower()
                tables = (d["det"], d["slot"]) if side == "buy" else (d["sells"],)
                cands = [t[key] for t in tables if key in t]
            else:
                cands = [(at, v + "~coin") for at, v in d["by_coin"].get((uid, str(ev["mint"]).lower(), side), [])
                         if abs((at - t_j7).total_seconds()) <= FALLBACK_S]
            if cands:
                ours, via = min(cands)
            seen_ms = None if ours is None else (ours - t_j7).total_seconds() * 1000.0
            bucket = ("no_tx" if tx is None and ours is None
                      else classify(seen_ms, trader, d["watched"], uid in wallet_watched))
            arrival = d["arrivals"].get(tx.lower()[:12]) if (tx and bucket == "watched_miss" and chain != "solana") else None
            if bucket == "watched_miss" and chain != "solana":
                bucket = ("delivered:%s" % (arrival[2] or arrival[1])) if arrival else "not_delivered"
            mint = str(ev["mint"] or "").lower()
            held = any(u == uid and mt == mint and o <= t_j7 and (c is None or c >= t_j7)
                       for u, mt, o, c in d["held"])
            cells[side].append(dict(ev=ev, bucket=bucket, seen_ms=seen_ms, via=via, held=held, trader=trader,
                                    arrival_ms=None if not arrival else (arrival[0] - t_j7).total_seconds() * 1000.0))
        for side in ("buy", "sell"):
            rows = cells.get(side, [])
            n = len(rows)
            b = collections.Counter(r["bucket"] for r in rows)
            seen = [r for r in rows if r["bucket"] in ("first", "later")]
            ms = [r["seen_ms"] for r in seen]
            vias = collections.Counter(r["via"] for r in seen)
            heldn = [r for r in rows if r["held"]]
            print("  %-4s J7 events from armed wallets n=%d  seen %d (%s)  first %d  later %d" % (
                      side, n, len(seen), ", ".join("%s %d" % kv for kv in sorted(vias.items())),
                      b["first"], b["later"]))
            if quote_legs[side]:
                print("       excluded: %d J7 %s(s) of the quote asset itself (SOL/USDC legs, not coin trades)"
                      % (quote_legs[side], side))
            for k, v in sorted(b.items()):
                if k not in ("first", "later"):
                    print("       miss bucket %-40s %d" % (k, v))
            arr = [r["arrival_ms"] for r in rows if r["arrival_ms"] is not None]
            if arr:
                print("       delivered-not-decoded arrival - J7 ms (mixed clock): p50 %.0f  p90 %.0f  n=%d" % (
                    pct(arr, 0.5), pct(arr, 0.9), len(arr)))
            if ms:
                print("       ours - J7 ms (mixed clock for watcher rows): p10 %.0f  p50 %.0f  p90 %.0f  max %.0f" % (
                    pct(ms, 0.1), pct(ms, 0.5), pct(ms, 0.9), max(ms)))
            if heldn:
                hb = collections.Counter(r["bucket"] for r in heldn)
                print("       while we HELD the coin (mirror-relevant): n=%d  %s" % (len(heldn), dict(sorted(hb.items()))))
            miss = [r for r in rows if r["bucket"] not in ("first", "later")]
            by_wallet = collections.Counter((r["ev"]["source_handle"] or r["ev"]["source_user_id"], r["bucket"]) for r in miss)
            for (h, bk), k in by_wallet.most_common(list_n):
                print("       miss %-14s %-22s x%d" % (bk, str(h)[:22], k))
        cov = sorted(d["armed"] - wallet_watched)
        counts = collections.Counter(str(ev["source_user_id"]) for ev in d["events"])
        print("  armed with NO watched address: %d  %s" % (len(cov), ", ".join(
            "%s(J7 ev %d)" % ((d["handles"].get(u) or u)[:18], counts[u]) for u in cov)))


def selftest():
    assert parse_j7("swap:raybot:0xAB:0xcd:0xef", "u") == ("0xAB", "0xcd")
    assert parse_j7("pump:SIG:MINT:sell:1", "pump:ADDR") == ("SIG", "ADDR")
    assert parse_j7("pump:SIG:MINT:buy:1", "fomo-uuid") == ("SIG", None)
    assert parse_j7("0f3c-uuid", "u") == (None, None)
    watched = {"0xcd": "u1"}
    assert classify(-12.0, "0xcd", watched, True) == "first"
    assert classify(0.0, "0xcd", watched, True) == "later"
    assert classify(None, "0xCD", watched, True) == "watched_miss"      # case-folded join
    assert classify(None, "0xzz", watched, True) == "other_address"
    assert classify(None, "0xzz", watched, False) == "not_watched"
    assert classify(None, None, watched, False) == "not_watched"
    assert pct([1, 2, 3, 4], 0.5) == 3 and pct([], 0.5) is None
    tl = arming_timeline([("2026-09-28T10:00:00", "u1", "chain_arm", "solana"),
                          ("2026-09-28T21:53:00", "u1", "chain_revoke", "solana"),
                          ("2026-09-28T21:53:00", "u2", "chain_revoke", "solana"),
                          ("2026-09-28T12:00:00", "u3", "chain_arm", "solana"),
                          ("2026-09-28T09:00:00", "u5", "retire", None),
                          ("2026-09-28T09:00:00", "u6", "approve", None)])
    assert armed_at_buy(tl, set(), "u1", "2026-09-28T20:00:00") is True       # armed then
    assert armed_at_buy(tl, set(), "u1", "2026-09-28T22:00:00") is False      # disarmed since
    assert armed_at_buy(tl, set(), "u2", "2026-09-28T20:00:00") is True       # armed before its only revoke
    assert armed_at_buy(tl, {"u3"}, "u3", "2026-09-28T11:00:00") is False     # not yet armed
    assert armed_at_buy(tl, {"u4"}, "u4", "2026-09-28T11:00:00") is True      # no history: today's state
    assert armed_at_buy(tl, set(), "u5", "2026-09-28T08:00:00") is False      # a global row is no chain arming
    assert armed_at_buy(tl, set(), "u6", "2026-09-28T10:00:00") is False
    assert is_quote_leg("solana", "So11111111111111111111111111111111111111112")
    assert is_quote_leg("solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    assert not is_quote_leg("solana", "3RNy7erxjwGRRYvM6KzVKVtuFnNSpkQFwwiHhUH26xoa")
    assert not is_quote_leg("4663", "So11111111111111111111111111111111111111112")
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", type=int, default=12)
    ap.add_argument("--chain", action="append", help="only these chains (repeatable)")
    ap.add_argument("--since", help="window start for the chosen chains, e.g. a fix's go-live instant")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.chain:
        for ch in list(WINDOWS):
            if ch not in args.chain:
                WINDOWS.pop(ch)
    if args.since:
        for ch in WINDOWS:
            WINDOWS[ch] = args.since
    from sniper.persistence.db_core import get_readonly_connection
    with contextlib.closing(get_readonly_connection(LEDGER)) as conn:
        data = load(conn)
    census(data, args.list)


if __name__ == "__main__":
    main()
