"""Exit speed on source-sell exits: from the source's sell to our landing, and what the wait cost.

Alpha, 2026-09-28 (operator: "we keep getting rugged"). Funded Solana closes with exit_reason
'mirror' (J7 sell event) or 'mirror_source_sell' (the source-sell mirror's account / slot sockets).
Per exit, each stage on ONE clock, method named:
  * source sell slot: the source-sell mirror audit's source_slot (the account update or the slot-lane
    frame carrying the source's sell). J7-only exits have no slot in the ledger: 'n/a'.
  * learned: the earliest allowed mirror decision and its lane (source_sell_mirror path
    account_processed / slot_processed, or mirror_on_sell = J7). recv_to_decision_ms is monotonic.
  * decision -> submitted: close_submitted_at - decision_at (both corrected clock, written by one process).
  * exit stages: positions.exit_timing_json (quote / sign / submit / confirm, monotonic ms).
  * landing: getSignatureStatuses on close_sig (research key, only with --chain, flat-gated): landing slot,
    and landing - source slot in SLOTS (exact, one clock).
  * price: our exit unit price (entry_notional + realized_pnl) / (entry_notional / entry_price) against the
    source's own sell price (the J7 sell event's their_price, the last one received at or before our
    decision + 1 s). $ lost = (source price - our price) x our tokens. Both prices carry that party's own
    fees and impact.

    python3 scripts/research/source_sell_exit_speed.py --ids 66255,66333,66336 [--chain]
    python3 scripts/research/source_sell_exit_speed.py --since 2026-09-21T06:00:00+00:00 [--chain]
    python3 scripts/research/source_sell_exit_speed.py --selftest
"""
import argparse
import contextlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
LEDGER = str(REPO / "sniper_tracker.db")


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None


def exit_price(entry_notional, entry_price, realized_pnl):
    if not entry_notional or not entry_price or realized_pnl is None:
        return None, None
    tokens = entry_notional / entry_price
    return (entry_notional + realized_pnl) / tokens, tokens


def first_decision(audits):
    """Earliest allowed mirror decision: (at, lane, detail)."""
    rows = [(a["checked_at"], a["lane"], a["detail"]) for a in audits
            if a["allowed"] and a["reason"] in ("mirror", "mirror_source_sell")]
    return min(rows) if rows else (None, None, None)


def load(conn, ids=None, since=None):
    conn.row_factory = sqlite3.Row
    q = ("SELECT id, source_handle, source_user_id, mint, entry_price, entry_notional, realized_pnl, "
         "opened_recorded_at, closed_at, close_submitted_at, close_sig, exit_reason, exit_timing_json "
         "FROM memecoin_positions WHERE execution_mode='real' AND network_id='solana' AND is_open=0 ")
    if ids:
        rows = conn.execute(q + "AND id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()
    else:
        rows = conn.execute(q + "AND exit_reason IN ('mirror','mirror_source_sell') AND opened_at >= ?",
                            (since,)).fetchall()
    out = []
    for p in rows:
        audits = conn.execute("SELECT reason, allowed, checked_at, lane, detail FROM memecoin_exit_trigger_audit "
                              "WHERE position_id=? ORDER BY id", (p["id"],)).fetchall()
        dec_at, lane, detail = first_decision(audits)
        d = {}
        for a in audits:
            if a["reason"] == "mirror_source_sell" and (a["detail"] or "").startswith("{"):
                d = json.loads(a["detail"])
                break
        limit = (ts(dec_at or p["closed_at"]) + timedelta(seconds=1)).isoformat()
        sell = conn.execute("SELECT their_price, received_at FROM memecoin_events WHERE source_user_id=? AND mint=? "
                            "AND side='sell' AND received_at <= ? ORDER BY received_at DESC LIMIT 1",
                            (p["source_user_id"], p["mint"], limit)).fetchone()
        px, tokens = exit_price(p["entry_notional"], p["entry_price"], p["realized_pnl"])
        src_px = sell["their_price"] if sell else None
        out.append({
            "id": p["id"], "handle": p["source_handle"], "reason": p["exit_reason"], "pnl": p["realized_pnl"],
            "hold_s": (ts(p["closed_at"]) - ts(p["opened_recorded_at"])).total_seconds(),
            "source_slot": d.get("source_slot"), "mirror_path": d.get("path"), "lock_wait_ms": d.get("lock_wait_ms"),
            "first_lane": lane, "decision_at": dec_at,
            "decision_to_submit_s": ((ts(p["close_submitted_at"]) - ts(dec_at)).total_seconds()
                                     if dec_at and p["close_submitted_at"] else None),
            "stages": json.loads(p["exit_timing_json"] or "{}"), "close_sig": p["close_sig"],
            "exit_px": px, "source_px": src_px,
            "loss_pct": (px / src_px - 1.0) * 100.0 if px and src_px else None,
            "lost_usd": (src_px - px) * tokens if px and src_px else None})
    return out


def landing_slots(sigs):
    """{sig: slot or None} via getSignatureStatuses on the research key, flat-gated."""
    import funded_exit_truth as fet
    from venue_census import book_open
    out = {}
    sigs = [s for s in sigs if s]
    for k in range(0, len(sigs), 256):
        if book_open(LEDGER):
            print("chain read stopped: funded Solana book not flat")
            break
        chunk = sigs[k:k + 256]
        res = fet.rpc("getSignatureStatuses", [chunk, {"searchTransactionHistory": True}]) or {}
        for s, v in zip(chunk, res.get("value") or []):
            out[s] = (v or {}).get("slot")
    return out


def pq(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids")
    ap.add_argument("--since", default="2026-09-21T06:00:00+00:00")
    ap.add_argument("--chain", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % LEDGER, uri=True, timeout=20)) as c:
        rows = load(c, ids=[int(x) for x in a.ids.split(",")] if a.ids else None, since=a.since)
    land = landing_slots([r["close_sig"] for r in rows]) if a.chain else {}
    for r in rows:
        r["landing_slot"] = land.get(r["close_sig"])
        r["slots_source_to_landing"] = (r["landing_slot"] - r["source_slot"]
                                        if r["landing_slot"] and r["source_slot"] else None)
    if a.ids:
        for r in rows:
            st = r["stages"].get("stages_ms") or {}
            print("%s %s %s pnl $%+.2f hold %.0f s" % (r["id"], r["handle"], r["reason"], r["pnl"], r["hold_s"]))
            print("  source sell slot %s (mirror path %s); first decision %s lane %s; lock_wait_ms %s"
                  % (r["source_slot"], r["mirror_path"], (r["decision_at"] or "")[11:23], r["first_lane"], r["lock_wait_ms"]))
            print("  decision->submitted %.3f s; exit stages quote %s sign %s submit %s confirm %s ms (rungs %s)"
                  % (r["decision_to_submit_s"] or float("nan"), st.get("quote"), st.get("sign"), st.get("submit"),
                     st.get("confirm"), r["stages"].get("ladder_rungs")))
            print("  landing slot %s; source slot -> landing %s slots" % (r["landing_slot"], r["slots_source_to_landing"]))
            print("  price: source sold at %s, we exited at %s: %+.1f%% ($%.2f lost to the gap)"
                  % (r["source_px"], r["exit_px"], r["loss_pct"] or float("nan"), r["lost_usd"] or float("nan")))
        return
    print("mirror / mirror_source_sell funded Solana exits since %s: n=%d, PnL $%+.2f"
          % (a.since[:19], len(rows), sum(r["pnl"] or 0 for r in rows)))
    for lane in sorted({r["first_lane"] for r in rows}, key=str):
        rs = [r for r in rows if r["first_lane"] == lane]
        print("  first lane %-20s n=%d" % (lane, len(rs)))
    for name, key in (("decision->submitted s", "decision_to_submit_s"), ("source slot->landing (slots)", "slots_source_to_landing"),
                      ("our exit vs source sell price %", "loss_pct"), ("$ lost to the gap per exit", "lost_usd")):
        xs = [r[key] for r in rows if r[key] is not None]
        print("  %-32s n=%d p50 %s p90 %s" % (name, len(xs), pq(xs, .5), pq(xs, .9)))
    for stage in ("quote", "sign", "submit", "confirm"):
        xs = [(r["stages"].get("stages_ms") or {}).get(stage) for r in rows]
        xs = [x for x in xs if x is not None]
        print("  exit stage %-8s ms n=%d p50 %s p90 %s" % (stage, len(xs), pq(xs, .5), pq(xs, .9)))
    lost = [r["lost_usd"] for r in rows if r["lost_usd"] is not None]
    print("  $ lost to the gap, total over %d exits with a source price: $%.2f (positive = we sold below the source)"
          % (len(lost), sum(lost)))


def selftest():
    px, tok = exit_price(93.42, 4.5186311624771124e-05, -31.60692)
    assert abs(tok - 93.42 / 4.5186311624771124e-05) < 1e-6 and abs(px * tok - 61.81308) < 1e-6
    assert exit_price(0, 1, 1) == (None, None) and exit_price(10, 1, None) == (None, None)
    audits = [{"allowed": 1, "reason": "mirror", "checked_at": "2026-09-28T02:03:06.687", "lane": "mirror_on_sell", "detail": ""},
              {"allowed": 1, "reason": "mirror_source_sell", "checked_at": "2026-09-28T02:03:06.671", "lane": "source_sell_mirror", "detail": "{}"},
              {"allowed": 1, "reason": "b_stop", "checked_at": "2026-09-28T02:03:00.000", "lane": "fastlane_poll", "detail": ""},
              {"allowed": 0, "reason": "mirror", "checked_at": "2026-09-28T02:03:01.000", "lane": "mirror_on_sell", "detail": ""}]
    assert first_decision(audits)[:2] == ("2026-09-28T02:03:06.671", "source_sell_mirror")
    assert pq([3, None, 1, 2], .5) == 2
    print("selftest ok")


if __name__ == "__main__":
    main()
