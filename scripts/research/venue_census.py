#!/usr/bin/env python3
"""Venue census of armed-wallet Solana buys: which venue each source buy traded on, how many reached
our gates, how many we filled, and how fast.

Operator order 2026-09-27 via Alpha ("fast fills on ANY venue", lever 1, step 1). Read-only.

THE UNIVERSE, keyed by source signature (the union slot_lane_eligibility.py uses):
  - confirmed-watcher detections (onchain_wallet_detections; armed owners only);
  - J7 buys not refused as an unarmed wallet (the raybot id carries the signature);
  - entry attempts whose origin carries a signature, except `not_approved` (unarmed) attempts.
J7 uuid-format events carry no signature: counted, never keyed. A buy none of our lanes saw is
outside this census.

THE VENUE comes from the source transaction itself (getTransaction on the research key): the first
known DEX or launchpad program invoked, in log order. `routed` = that program ran under CPI (depth
>= 2), i.e. behind an aggregator or a bot router. A transaction with no known venue program is
labelled by its first non-infrastructure top-level program, so unknown venues surface by count
instead of being guessed.

OUR SIDE comes from the ledger: an attempt on the signature = reached our gates; a reservation =
passed them; a funded position = filled. decision->fill = the filling attempt's decision_started_at
to memecoin_positions.opened_recorded_at (the confirmed fill); first_seen->fill starts at our first
sighting of the buy on any lane.

Chain reads only on the research key, re-checking before EVERY read that no funded Solana
position or reservation is open; progress is saved so a stopped run resumes.

    python3 scripts/research/venue_census.py fetch --days 7 --out census.json
    python3 scripts/research/venue_census.py report --census census.json
    python3 scripts/research/venue_census.py --selftest
"""
import argparse
import collections
import contextlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
STATE_ROOT = "/home/bigpapi/Desktop/Sniper"      # .env and the ledger live in the main checkout

VENUES = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump_curve",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pumpswap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "raydium_cpmm",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "raydium_clmm",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "meteora_dlmm",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "meteora_damm_v1",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "meteora_damm_v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "meteora_dbc",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "orca_whirlpool",
}
INFRA = {
    "11111111111111111111111111111111", "ComputeBudget111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
}
INVOKE = re.compile(r"^Program (\S+) invoke \[(\d+)\]")


def venue_of(logs):
    """(venue, routed, top-level programs, inner programs) from a transaction's log messages."""
    top, inner, first = [], [], None
    for line in logs or []:
        m = INVOKE.match(line)
        if not m:
            continue
        prog, depth = m.group(1), int(m.group(2))
        if prog in INFRA:
            continue
        if depth == 1 and prog not in top:
            top.append(prog)
        if depth >= 2 and prog not in inner:
            inner.append(prog)
        if first is None and prog in VENUES:
            first = (VENUES[prog], depth >= 2)
    if first:
        return first[0], first[1], top, inner
    return ("other:%s" % top[0][:8]) if top else "no_program", False, top, inner


def ro(path):
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def ts(s):
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace(" ", "T").replace("Z", "+00:00"))
    return d.timestamp() if d.tzinfo else None


def arming_timeline(conn):
    """{source_user_id: [(created_at, armed bool), ...]} from the approval audit (memecoin_wallet_
    approvals, written by db_memecoin._audit_approval / record_wallet_audit): chain_arm and
    chain_revoke on solana, plus the global approve (arms), revoke and retire (disarm)."""
    import collections
    out = collections.defaultdict(list)
    for uid, action, at in conn.execute(
            "SELECT source_user_id, action, created_at FROM memecoin_wallet_approvals "
            "WHERE (chain='solana' AND action IN ('chain_arm','chain_revoke')) "
            "OR (chain IS NULL AND action IN ('approve','revoke','retire')) ORDER BY created_at"):
        out[uid].append((at, action in ("chain_arm", "approve")))
    return out


def armed_at(timeline, user, at):
    """True/False from the last arming event at or before `at`; None when there is none."""
    state = None
    for when, on in timeline.get(user, ()):
        if when > at:
            break
        state = on
    return state


def universe(ledger, since, armed_only=True):
    """Every keyed Solana source buy since `since`, with its attempts. ARMED ONLY by default
    (2026-09-28): the watcher's detections include wallets that were not armed on Solana at the
    buy, which inflated census v2's buys/day (CPMM 105/day, of which 52.2 armed); a detection
    counts only when its wallet was armed at detected_at. J7 events keep their own filter, and an
    attempt row is armed by construction (on every venue armed-at-buy equalled attempted)."""
    from sniper.persistence.db_memecoin import event_transaction_signature
    buys, uuid_j7 = {}, 0

    def seen(sig, at, via, handle=None, mint=None):
        b = buys.setdefault(sig, {"sig": sig, "first_seen": at, "via": set(), "handle": handle,
                                  "mint": mint, "attempts": []})
        b["via"].add(via)
        if at and (b["first_seen"] is None or at < b["first_seen"]):
            b["first_seen"] = at
        b["handle"] = b["handle"] or handle
        b["mint"] = b["mint"] or mint

    with contextlib.closing(ro(ledger)) as c:
        timeline = arming_timeline(c) if armed_only else None
        for r in c.execute("SELECT signature, detected_at, handle, mint, source_user_id "
                           "FROM onchain_wallet_detections "
                           "WHERE network_id='solana' AND detected_at >= ?", (since,)):
            if timeline is not None and not armed_at(timeline, r[4], r[1]):
                continue
            seen(r[0], r[1], "watcher", r[2], r[3])
        for r in c.execute("SELECT fomo_event_id, COALESCE(received_at_normalized, received_at), "
                           "source_handle, mint FROM memecoin_events WHERE network_id='solana' AND side='buy' "
                           "AND received_at >= ? AND COALESCE(real_decision_reason,'') NOT LIKE "
                           "'wallet not armed%'", (since,)):
            sig = event_transaction_signature(r[0])
            if sig is None:
                uuid_j7 += 1
            else:
                seen(sig, r[1], "j7", r[2], r[3])
        for r in c.execute(
                "SELECT a.origin_event_id, a.delivery_lane, a.signal_source, a.decision_started_at, "
                "a.terminal_status, a.terminal_reason, a.reservation_completed_at, a.source_handle, a.mint, "
                "a.tx_sig, "
                "p.id AS position_id, p.opened_recorded_at FROM memecoin_entry_attempts a "
                "LEFT JOIN memecoin_positions p ON p.execution_mode='real' "
                " AND (p.id=a.position_id OR p.attempt_id=a.attempt_id) "
                "WHERE a.network_id='solana' AND a.decision_started_at >= ? "
                "ORDER BY a.decision_started_at", (since,)):
            o = r["origin_event_id"] or ""
            sig = o.split(":", 1)[1] if o.startswith("slotlane:") else event_transaction_signature(o)
            if not sig or r["terminal_reason"] == "not_approved":
                continue
            seen(sig, r["decision_started_at"], "attempt", r["source_handle"], r["mint"])
            buys[sig]["attempts"].append({
                "lane": r["delivery_lane"] or r["signal_source"], "start": r["decision_started_at"],
                "status": r["terminal_status"], "reason": r["terminal_reason"],
                "reserved": r["reservation_completed_at"] is not None,
                "position_id": r["position_id"], "tx_sig": r["tx_sig"], "fill_at": r["opened_recorded_at"]
                if r["terminal_status"] == "filled" else None})
    for b in buys.values():
        b["via"] = sorted(b["via"])
    return buys, uuid_j7


def book_open(ledger):
    with contextlib.closing(ro(ledger)) as c:
        pos = c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
                        "AND network_id='solana' AND is_open=1 AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]
        res = c.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
                        "AND COALESCE(network_id,'solana')='solana'").fetchone()[0]
    return pos + res


def priority(b):
    a = b["attempts"]
    return (0 if any(x["status"] == "filled" for x in a) else 1 if any(x["reserved"] for x in a)
            else 2 if a else 3, b["first_seen"] or "")


def fetch(args):
    import funded_exit_truth as fet
    from sniper.util.envfiles import load_env_files
    from slot_lane_send_monitor import pick_research_url
    import os
    load_env_files(STATE_ROOT)
    fet._URL = pick_research_url(os.environ)
    fet.SPACING_S = args.spacing
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    buys, uuid_j7 = universe(args.ledger, since)
    out = Path(args.out)
    done = json.loads(out.read_text())["venues"] if out.exists() else {}
    todo = [b for b in sorted(buys.values(), key=priority) if b["sig"] not in done]
    print("universe %d keyed buys since %s, %d J7 uuid unkeyed; %d already read, %d to read"
          % (len(buys), since[:16], uuid_j7, len(done), len(todo)), flush=True)
    stopped = None
    try:
        for i, b in enumerate(todo[:args.limit] if args.limit else todo):
            if book_open(args.ledger):
                stopped = "funded Solana book not flat"
                break
            tx = fet.rpc("getTransaction", [b["sig"], {"encoding": "json", "commitment": "confirmed",
                                                       "maxSupportedTransactionVersion": 1}])
            if tx is None:
                done[b["sig"]] = {"venue": "unreadable"}
            else:
                v, routed, top, inner = venue_of((tx.get("meta") or {}).get("logMessages"))
                done[b["sig"]] = {"venue": v, "routed": routed, "top": top[:4], "inner": inner[:6],
                                  "slot": tx.get("slot"),
                                  "err": (tx.get("meta") or {}).get("err") is not None}
            if i % 50 == 49:
                out.write_text(json.dumps({"since": since, "venues": done}))
                print("  %d read, 429s %d" % (i + 1, fet.RATE_429[0]), flush=True)
    except fet.Stop as exc:
        stopped = str(exc)
    out.write_text(json.dumps({"since": since, "venues": done}))
    print("saved %d venues; stopped: %s; research-key 429s %d" % (len(done), stopped, fet.RATE_429[0]))


# --- v2: the venue the COIN came from (2026-09-27 22:3xZ correction) -------------------------------
#
# v1 labelled a buy by the FIRST venue program in log order. On a multi-hop route that is the hop that
# buys the quote asset (USDC -> SOL, ZEC, DOGE), not the pool the coin came from: 1,148 of 2,370 rows
# touched two or more venue programs (DLMM -> CPMM 226, CLMM -> CPMM 137, ...). v2 reads the coin's own
# token balances: the account holding the bought mint whose balance FELL the most is the pool vault
# that paid it out. Its owner names the venue: a known pool authority PDA (no account behind it) by
# address, otherwise the program that owns the owner account.
# path-ok: the sys.path[:0] assignment near the top already puts the repo root first (a slice,
# which the bootstrap check does not recognise); `--selftest` from a bare launch passes.
from sniper.execution.raydium_launchlab_buy import LAUNCHLAB_AUTHORITY  # noqa: E402

AUTHORITIES = {
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL": "raydium_cpmm",    # vault_and_lp_mint_auth_seed
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "raydium_v4",      # AMM v4 authority
    LAUNCHLAB_AUTHORITY: "raydium_launchlab",                           # vault_auth_seed (was pda:WLHv2UAZ)
    # Meteora pool authorities are system-owned PDAs, so v2 printed both as other:11111111 (71 buys).
    "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM": "meteora_dbc",       # DBC pool authority
    "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC": "meteora_damm_v2",   # DAMM v2 (cp-amm) pool authority
}


def coin_sender_owner(meta, mint):
    """Owner of the token account holding `mint` whose balance fell the most in this tx, or None."""
    pre = {b["accountIndex"]: b for b in (meta or {}).get("preTokenBalances") or () if b.get("mint") == mint}
    post = {b["accountIndex"]: b for b in (meta or {}).get("postTokenBalances") or () if b.get("mint") == mint}
    best, owner = 0, None
    for idx in set(pre) | set(post):
        amount = lambda b: int(((b or {}).get("uiTokenAmount") or {}).get("amount") or 0)
        delta = amount(post.get(idx)) - amount(pre.get(idx))
        if delta < best:
            best, owner = delta, (pre.get(idx) or post.get(idx)).get("owner")
    return owner


def fetch2(args):
    import funded_exit_truth as fet
    from sniper.util.envfiles import load_env_files
    from slot_lane_send_monitor import pick_research_url
    import os
    load_env_files(STATE_ROOT)
    fet._URL = pick_research_url(os.environ)
    fet.SPACING_S = args.spacing
    base = json.loads(Path(args.census).read_text())
    buys, _uuid = universe(args.ledger, base["since"])
    out = Path(args.out)
    done = json.loads(out.read_text()) if out.exists() else {"since": base["since"], "senders": {}, "owners": {}}
    todo = [b for b in sorted(buys.values(), key=priority)
            if b["sig"] not in done["senders"] and b.get("mint")]
    print("v2: %d buys with a mint, %d to read" % (sum(1 for b in buys.values() if b.get("mint")), len(todo)),
          flush=True)
    stopped = None
    try:
        for i, b in enumerate(todo):
            if book_open(args.ledger):
                stopped = "funded Solana book not flat"
                break
            tx = fet.rpc("getTransaction", [b["sig"], {"encoding": "json", "commitment": "confirmed",
                                                       "maxSupportedTransactionVersion": 1}])
            done["senders"][b["sig"]] = coin_sender_owner((tx or {}).get("meta"), b["mint"]) if tx else "unreadable"
            if i % 100 == 99:
                out.write_text(json.dumps(done))
                print("  %d read" % (i + 1), flush=True)
        owners = sorted({o for o in done["senders"].values() if o and o != "unreadable"
                         and o not in AUTHORITIES and o not in done["owners"]})
        for k in range(0, len(owners), 100):
            if book_open(args.ledger):
                stopped = "funded Solana book not flat"
                break
            chunk = owners[k:k + 100]
            res = fet.rpc("getMultipleAccounts", [chunk, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])
            for o, v in zip(chunk, res["value"]):
                done["owners"][o] = (v or {}).get("owner")      # None = no account (a PDA)
    except fet.Stop as exc:
        stopped = str(exc)
    out.write_text(json.dumps(done))
    print("v2 saved %d senders, %d owner programs; stopped: %s; 429s %d"
          % (len(done["senders"]), len(done["owners"]), stopped, fet.RATE_429[0]))


def coin_venue(sender, owners):
    if sender in (None, "unreadable"):
        return sender or "no_coin_outflow"
    if sender in AUTHORITIES:
        return AUTHORITIES[sender]
    program = owners.get(sender)
    if program is None:
        return "pda:%s" % sender[:8]
    return VENUES.get(program, "other:%s" % program[:8])


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def pp(xs):
    xs = [x for x in xs if x is not None]
    return "%6.0f %6.0f" % (pct(xs, .5), pct(xs, .9)) if xs else "     -      -"


def report(args):
    saved = json.loads(Path(args.census).read_text())
    since, venues = saved["since"], saved["venues"]
    if getattr(args, "v2", None):
        v2 = json.loads(Path(args.v2).read_text())
        venues = {sig: {"venue": coin_venue(sender, v2["owners"]), "routed": False, "top": [], "inner": []}
                  for sig, sender in v2["senders"].items()}
    buys, uuid_j7 = universe(args.ledger, since)
    days = (time.time() - ts(since)) / 86400.0
    rows = collections.defaultdict(lambda: {"buys": 0, "gates": 0, "reserved": 0, "fills": 0,
                                            "dec_fill": [], "seen_fill": [], "fill_lanes": collections.Counter(),
                                            "manual_fills": 0})
    unread = 0
    other_top = collections.Counter()
    for sig, b in buys.items():
        v = venues.get(sig)
        if v is None:
            unread += 1
            continue
        if v.get("err"):
            rows["(source tx failed)"]["buys"] += 1
            continue
        key = v["venue"] + (" [routed]" if v.get("routed") else "")
        if v["venue"].startswith("other:") or v["venue"] == "no_program":
            other_top[tuple((v.get("top") or [])[:1] + (v.get("inner") or [])[:2])] += 1
        t = rows[key]
        t["buys"] += 1
        a = b["attempts"]
        t["gates"] += bool(a)
        t["reserved"] += any(x["reserved"] for x in a)
        f = next((x for x in a if x["status"] == "filled" and x["fill_at"]), None)
        if f and f["lane"] == "manual":
            t["manual_fills"] += 1                  # the operator's own click, not a copy path
            f = None
        if f:
            t["fills"] += 1
            t["fill_lanes"][f["lane"]] += 1
            d, fa, s0 = ts(f["start"]), ts(f["fill_at"]), ts(b["first_seen"])
            t["dec_fill"].append((fa - d) * 1000 if d and fa else None)
            t["seen_fill"].append((fa - s0) * 1000 if s0 and fa else None)
    print("since %s (%.2f d): %d keyed armed-wallet Solana buys (%d not yet read), %d J7 uuid unkeyed"
          % (since[:16], days, len(buys), unread, uuid_j7))
    print("%-34s %6s %7s %6s %6s %6s  %-13s %-13s %s" % (
        "venue", "buys", "buys/d", "gates", "resvd", "fills", "dec->fill ms", "seen->fill ms", "fill lanes"))
    print("%-34s %6s %7s %6s %6s %6s  %-13s %-13s" % ("", "", "", "", "", "", "  p50    p90", "  p50    p90"))
    for key, t in sorted(rows.items(), key=lambda kv: -kv[1]["buys"]):
        print("%-34s %6d %7.1f %6d %6d %6d  %s  %s  %s" % (
            key[:34], t["buys"], t["buys"] / days, t["gates"], t["reserved"], t["fills"],
            pp(t["dec_fill"]), pp(t["seen_fill"]), dict(t["fill_lanes"])))
    print("manual fills (excluded above): %d" % sum(t["manual_fills"] for t in rows.values()))
    print("\nunlabelled venues: first top-level program > first inner programs:")
    for k, n in other_top.most_common(12):
        print("  %4d  %s" % (n, " > ".join(k)))


def selftest():
    logs = ["Program ComputeBudget111111111111111111111111111111 invoke [1]",
            "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]",
            "Program CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C invoke [2]",
            "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [3]"]
    assert venue_of(logs) == ("raydium_cpmm", True, ["JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"],
                              ["CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"])
    direct = ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]"]
    assert venue_of(direct) == ("pump_curve", False, ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"], [])
    assert venue_of(["Program XyzRouter11111111111111111111111111 invoke [1]"])[0] == "other:XyzRoute"
    assert venue_of([])[0] == "no_program" and venue_of(None)[0] == "no_program"
    meta = {"preTokenBalances": [{"accountIndex": 3, "mint": "C", "owner": "Pool",
                                  "uiTokenAmount": {"amount": "1000"}},
                                 {"accountIndex": 5, "mint": "C", "owner": "Router",
                                  "uiTokenAmount": {"amount": "0"}}],
            "postTokenBalances": [{"accountIndex": 3, "mint": "C", "owner": "Pool",
                                   "uiTokenAmount": {"amount": "400"}},
                                  {"accountIndex": 5, "mint": "C", "owner": "Router",
                                   "uiTokenAmount": {"amount": "0"}},
                                  {"accountIndex": 7, "mint": "C", "owner": "Buyer",
                                   "uiTokenAmount": {"amount": "600"}}]}
    assert coin_sender_owner(meta, "C") == "Pool" and coin_sender_owner(meta, "X") is None
    assert coin_venue("GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL", {}) == "raydium_cpmm"
    assert coin_venue("WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh", {}) == "raydium_launchlab"
    assert coin_venue("FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM",
                      {"FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM": "11111111111111111111111111111111"}) == "meteora_dbc"
    assert coin_venue("HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",
                      {"HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC": "11111111111111111111111111111111"}) == "meteora_damm_v2"
    assert coin_venue("Pool", {"Pool": "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"}) == "meteora_dlmm"
    assert coin_venue("Pda1", {"Pda1": None}) == "pda:Pda1" and coin_venue(None, {}) == "no_coin_outflow"
    tl = {"W1": [("2026-09-20T00:00:00+00:00", True), ("2026-09-22T00:00:00+00:00", False)]}
    assert armed_at(tl, "W1", "2026-09-21T00:00:00+00:00") is True
    assert armed_at(tl, "W1", "2026-09-23T00:00:00+00:00") is False
    assert armed_at(tl, "W1", "2026-09-19T00:00:00+00:00") is None and armed_at(tl, "W2", "x") is None
    b = {"attempts": [{"status": "rejected", "reserved": True}], "first_seen": "a"}
    assert priority(b)[0] == 1 and priority({"attempts": [], "first_seen": "a"})[0] == 3
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--ledger", default=STATE_ROOT + "/sniper_tracker.db")
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch")
    f.add_argument("--days", type=float, default=7.0)
    f.add_argument("--out", required=True)
    f.add_argument("--limit", type=int, default=0)
    f.add_argument("--spacing", type=float, default=0.25)
    r = sub.add_parser("report")
    r.add_argument("--census", required=True)
    r.add_argument("--v2", help="the fetch2 output: label buys by the venue the coin came from")
    f2 = sub.add_parser("fetch2")
    f2.add_argument("--census", required=True)
    f2.add_argument("--out", required=True)
    f2.add_argument("--spacing", type=float, default=0.25)
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.cmd == "fetch":
        return fetch(a)
    if a.cmd == "report":
        return report(a)
    if a.cmd == "fetch2":
        return fetch2(a)
    ap.print_help()


if __name__ == "__main__":
    main()
