"""Slot-lane eligibility ledger: every armed-wallet Solana buy since the lane went live, by what the
lane did with it, whether any lane filled it, where we landed against the source slot, and the $.

Question (Alpha 12:13Z, Codex docs/audits/2026-09-27-lemon-dougherty-s7.md item 7): how many fills
would the lane have taken at S/S+1 had it known the pool (pool_unknown) and supported a reduced clip
(slot_lane_scaled_clip)? Lemon and Dougherty both lost the lane to pool_unknown and landed S+7.

THE UNIVERSE is a union, keyed by source signature where one exists:
  - confirmed-watcher detections (onchain_wallet_detections, signature + source_slot);
  - J7 raybot buys whose wallet was not refused as unarmed (the id carries the signature; its
    slot comes from getSignatureStatuses);
  - slot-lane decision lines in the runner log. A line carries handle and source slot S but no
    signature, so it joins a signed buy on (handle, S); an unmatched line is a lane-only buy.
J7 uuid-format events carry no signature and cannot be keyed; they are counted, not merged.
Every source is our own subscription, so "not seen" means not seen by the lane among buys SOME
lane of ours saw; a buy no lane saw is outside this ledger.

A FILL is a funded Solana position whose attempt origin names the source signature
(slotlane:/onchain:/swap:raybot:). Its landing offset is our entry tx slot minus the source slot,
both from getSignatureStatuses or the ledger. The counterfactual is NOT a replay: a pool_unknown or
scaled_clip buy whose fallback filled is a buy the lane would have attempted, and its actual
fallback offset and PnL are what the lane would have changed; a fallback refused on price/chase is
listed separately because an S/S+1 lane might have passed that gate, and nothing here proves it.

Research key only (funded_exit_truth.rpc), refuses while a real Solana position is open.

    python3 scripts/research/slot_lane_eligibility.py --since 2026-09-26T21:07:13Z
    python3 scripts/research/slot_lane_eligibility.py --selftest
"""
import argparse
import collections
import contextlib
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = f"file:{REPO / 'sniper_tracker.db'}?mode=ro"
LOG = REPO / "logs" / "memecoin_runner.log"
LINE = re.compile(r"^(\S+) \[memecoin\] slot lane (\S+) (\S+) S=(\d+) -> (\S+) ?(.*?) ms=")
CLASSES = ("lane_fill", "lane_sent_failed", "pool_unknown", "scaled_clip", "price_unreadable",
           "lane_infra_other", "lane_policy", "not_seen")
PRICE_REFUSAL = re.compile(r"chase|above source|x the (chart mid|source price)|price impact|round trip|post_peak")


def lane_class(outcome, reason):
    if outcome == "filled":
        return "lane_fill"
    if outcome == "failed":
        return "lane_sent_failed"
    if reason.startswith("pool_unknown"):
        return "pool_unknown"
    if reason.startswith("slot_lane_scaled_clip"):
        return "scaled_clip"
    if reason.startswith("slot_lane_price_unreadable"):
        return "price_unreadable"
    if reason.startswith("slot_lane_") or outcome == "aborted":
        return "lane_infra_other"                 # stale balances, busy wallet, quote not SOL, leg stale
    return "lane_policy"                          # floors, ceiling, cooldown, latch, kill switch, caps


def offset_bucket(k):
    if k is None:
        return "unknown"
    return "S+0..1" if k <= 1 else "S+2..4" if k <= 4 else "S+5..9" if k <= 9 else "S+10+"


def lane_lines(since):
    out = []
    with open(LOG, errors="replace") as fh:
        for line in fh:
            m = LINE.match(line)
            if m and m.group(1) >= since:
                out.append({"at": m.group(1), "handle": m.group(2).lower(), "short": m.group(3),
                            "S": int(m.group(4)), "outcome": m.group(5), "reason": m.group(6).strip()})
    return out


def merge(signed, lines):
    """signed: {sig: {handle, slot, ...}}. Attach each lane line to the signed buy with the same
    (handle, slot); a line with no match becomes a lane-only buy keyed 'lane:<handle>:<S>'.
    A later line for the same buy (a retry) keeps the FIRST line's class."""
    by_key = {}
    for sig, b in signed.items():
        if b.get("slot") is not None:
            by_key.setdefault((b["handle"], b["slot"]), sig)
    buys = {sig: dict(b, lane=None) for sig, b in signed.items()}
    for ln in lines:
        sig = by_key.get((ln["handle"], ln["S"])) or "lane:%s:%d" % (ln["handle"], ln["S"])
        b = buys.setdefault(sig, {"handle": ln["handle"], "slot": ln["S"], "mint": None, "lane": None})
        if b["lane"] is None:
            b["lane"] = lane_class(ln["outcome"], ln["reason"])
            b["lane_reason"] = ln["reason"]
    for b in buys.values():
        b["lane"] = b["lane"] or "not_seen"
    return buys


def tally(buys):
    out = collections.defaultdict(lambda: {"buys": 0, "filled": 0, "pnl": 0.0, "open": 0,
                                           "offsets": collections.Counter(), "fallback_refusals": collections.Counter()})
    for b in buys.values():
        t = out[b["lane"]]
        t["buys"] += 1
        f = b.get("fill")
        if f:
            t["filled"] += 1
            t["offsets"][offset_bucket(f.get("offset"))] += 1
            if f.get("pnl") is None:
                t["open"] += 1
            else:
                t["pnl"] += f["pnl"]
        elif b.get("fallback_reason"):
            t["fallback_refusals"]["price" if PRICE_REFUSAL.search(b["fallback_reason"]) else "other"] += 1
    return out


def slots_of(sigs):
    import funded_exit_truth as fet
    out = {}
    sigs = sorted(set(s for s in sigs if s))
    for i in range(0, len(sigs), 200):
        chunk = sigs[i:i + 200]
        res = fet.rpc("getSignatureStatuses", [chunk, {"searchTransactionHistory": True}])
        for s, v in zip(chunk, res["value"]):
            out[s] = v["slot"] if v and v.get("err") is None else None
    return out, fet.RATE_429[0]


def main(since):
    from sniper.persistence.db_memecoin import event_transaction_signature
    import slot_lane_send_monitor as mon
    import funded_exit_truth as fet
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=10)) as c:
        c.row_factory = sqlite3.Row
        if c.execute("SELECT count(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
                     "AND network_id='solana' AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]:
            raise SystemExit("a real Solana position is open: no research-key reads (standing rule)")
        signed = {}
        for r in c.execute("SELECT signature, lower(handle) h, mint, source_valuation_json v FROM onchain_wallet_detections "
                           "WHERE network_id='solana' AND detected_at >= ?", (since,)):
            slot = (json.loads(r["v"] or "{}")).get("source_slot")
            signed[r["signature"]] = {"handle": r["h"], "slot": slot, "mint": r["mint"], "via": "watcher"}
        uuid_j7 = 0
        for r in c.execute("SELECT fomo_event_id, lower(source_handle) h, mint FROM memecoin_events WHERE network_id='solana' "
                           "AND side='buy' AND received_at >= ? AND COALESCE(real_decision_reason,'') NOT LIKE 'wallet not armed%'",
                           (since,)):
            sig = event_transaction_signature(r["fomo_event_id"])
            if sig is None:
                uuid_j7 += 1
            elif sig not in signed:
                signed[sig] = {"handle": r["h"], "slot": None, "mint": r["mint"], "via": "j7"}
        fills, fallback, unkeyed_fills = {}, {}, 0
        # The first lane fills (2026-09-26 21:33-22:01Z) left attempts.position_id NULL: join the
        # position on attempt_id too, and take our tx from the reservation when the attempt lacks it.
        for r in c.execute("SELECT a.origin_event_id o, a.terminal_status st, a.terminal_reason rs, "
                           "COALESCE(a.tx_sig, pe.tx_sig) tx_sig, p.id position_id, "
                           "lower(a.source_handle) h, p.realized_pnl, p.is_open FROM memecoin_entry_attempts a "
                           "LEFT JOIN memecoin_positions p ON p.execution_mode='real' "
                           " AND (p.id=a.position_id OR p.attempt_id=a.attempt_id) "
                           "LEFT JOIN memecoin_pending_entries pe ON pe.attempt_id=a.attempt_id "
                           "WHERE a.network_id='solana' AND a.decision_started_at >= ? ORDER BY a.decision_started_at", (since,)):
            o = r["o"] or ""
            sig = o.split(":", 1)[1] if o.startswith("slotlane:") else event_transaction_signature(o)
            if not sig:
                unkeyed_fills += r["st"] == "filled"
                continue
            if sig not in signed:          # a lane-only buy: its slot comes from the chain below
                signed[sig] = {"handle": r["h"], "slot": None, "mint": None, "via": "attempt"}
            if r["st"] == "filled" and r["position_id"]:
                fills.setdefault(sig, {"position": r["position_id"], "our_sig": r["tx_sig"],
                                       "pnl": None if r["is_open"] else r["realized_pnl"]})
            elif not o.startswith("slotlane:"):
                fallback.setdefault(sig, r["rs"] or r["st"])
    need = [s for s, b in signed.items() if b["slot"] is None] + [f["our_sig"] for f in fills.values()]
    fet._URL = mon.rpc_url()
    slot, n429 = slots_of(need)
    for s, b in signed.items():
        if b["slot"] is None:
            b["slot"] = slot.get(s)
    buys = merge(signed, lane_lines(since))
    for sig, b in buys.items():
        f = fills.get(sig)
        if f:
            ours = slot.get(f["our_sig"])
            b["fill"] = dict(f, offset=(ours - b["slot"]) if ours is not None and b.get("slot") is not None else None)
        elif sig in fallback:
            b["fallback_reason"] = fallback[sig]
    report(buys, uuid_j7, unkeyed_fills, n429, since)


def report(buys, uuid_j7, unkeyed_fills, n429, since):
    t = tally(buys)
    print(f"since {since}: {len(buys)} armed-wallet Solana buys; NOT keyable: {uuid_j7} J7 uuid events, "
          f"{unkeyed_fills} fills with no source signature; 429s {n429}")
    print(f"{'lane outcome':<18}{'buys':>6}{'filled':>7}{'open':>5}{'pnl $':>10}  landing offsets of fills        fallback refusals")
    for k in CLASSES:
        if k in t:
            s = t[k]
            print(f"{k:<18}{s['buys']:>6}{s['filled']:>7}{s['open']:>5}{s['pnl']:>10.2f}  "
                  f"{dict(s['offsets'])!s:<32}{dict(s['fallback_refusals'])}")
    print("\nJOIN CHECK (a lane fill line with no fill, or a fill the lane never saw):")
    for sig, b in sorted(buys.items(), key=lambda kv: kv[1].get("slot") or 0):
        if (b["lane"] == "lane_fill") != bool(b.get("fill")) and b["lane"] in ("lane_fill", "not_seen"):
            f = b.get("fill") or {}
            print(f"  {b['lane']:<13} {b['handle']:<18} S={b.get('slot')} sig={sig[:14]} pos={f.get('position')} "
                  f"offset={f.get('offset')}")
    print("\nCOUNTERFACTUAL rows (pool_unknown, scaled_clip):")
    for sig, b in sorted(buys.items(), key=lambda kv: kv[1].get("slot") or 0):
        if b["lane"] in ("pool_unknown", "scaled_clip"):
            f = b.get("fill") or {}
            print(f"  {b['lane']:<13} {b['handle']:<18} S={b.get('slot')} sig={sig[:12]} "
                  + (f"FILLED pos={f['position']} offset={f.get('offset')} pnl={f.get('pnl')}" if f
                     else f"not filled: {(b.get('fallback_reason') or 'no fallback attempt')[:70]}"))


def selftest():
    assert lane_class("rejected", "pool_unknown") == "pool_unknown"
    assert lane_class("rejected", "slot_lane_scaled_clip") == "scaled_clip"
    assert lane_class("rejected", "slot_lane_stale_balances") == "lane_infra_other"
    assert lane_class("rejected", "source buy $10 (0.04%) under mcap floor 0.500% ($121)") == "lane_policy"
    assert lane_class("failed", "chain_error:x") == "lane_sent_failed" and lane_class("filled", "") == "lane_fill"
    assert [offset_bucket(k) for k in (0, 1, 2, 7, 12, None)] == ["S+0..1", "S+0..1", "S+2..4", "S+5..9", "S+10+", "unknown"]
    m = LINE.match("2026-09-27T10:49:06.821845+00:00 [memecoin] slot lane limfork qNadZjDe S=450974624 -> "
                   "rejected pool_unknown ms=0.0 recv-S=0")
    assert m and m.group(2) == "limfork" and m.group(4) == "450974624" and m.group(6) == "pool_unknown"
    signed = {"SIGA": {"handle": "limfork", "slot": 100, "mint": "M"}, "SIGB": {"handle": "x", "slot": 5, "mint": "N"}}
    lines = [{"handle": "limfork", "S": 100, "outcome": "rejected", "reason": "pool_unknown"},
             {"handle": "limfork", "S": 100, "outcome": "rejected", "reason": "source buy $1 under absolute floor $200"},
             {"handle": "y", "S": 7, "outcome": "filled", "reason": ""}]
    buys = merge(signed, lines)
    assert buys["SIGA"]["lane"] == "pool_unknown"          # the first line decides, a retry does not
    assert buys["SIGB"]["lane"] == "not_seen" and buys["lane:y:7"]["lane"] == "lane_fill"
    buys["SIGA"]["fill"] = {"position": 1, "offset": 7, "pnl": 27.8}
    buys["SIGB"]["fallback_reason"] = "exact quote is +40% above source price (cap 30%)"
    t = tally(buys)
    assert t["pool_unknown"]["filled"] == 1 and t["pool_unknown"]["offsets"]["S+5..9"] == 1
    assert t["pool_unknown"]["pnl"] == 27.8 and t["not_seen"]["fallback_refusals"]["price"] == 1
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-26T21:07:13")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    selftest() if a.selftest else main(a.since)
