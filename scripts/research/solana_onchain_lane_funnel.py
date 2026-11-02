#!/usr/bin/env python3
"""Solana on-chain watcher lane funnel: three counts, the attempt histogram, and the attribution
check that says whether `signal_source` carries detection lineage.

Read-only. One pass over sniper_tracker.db, no chain read, no provider call, no write anywhere.

Three COUNTS are reported and no conversion rate is, because every denominator here is produced by
our own behaviour: a detection exists only for an address our watcher subscribed to, an attempt only
where the hook fired, and a fill only where every gate passed. A ratio between two of those stages
would measure the roster and the gates, not the market.

The attribution section exists because the stage counts do NOT form a lineage chain.
`memecoin_entry_attempts.signal_source` records which lane created the attempt row, and the
wallet-activity poller (`follow_activity`) often creates it in the same second the on-chain watcher
detects the swap. Attribution below requires a validated exact transaction signature; proximity
alone cannot prove that two rows describe the same buy.

Usage:
    python3 scripts/research/solana_onchain_lane_funnel.py [--since ISO8601] [--until ISO8601] [--selftest]

`--since` defaults to the watcher start that first subscribed to 20 addresses.
"""
import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sniper.persistence.db_memecoin import event_transaction_signature

DB = "file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro"
DEFAULT_SINCE = "2026-09-23T09:42:53+00:00"
MATCH_WINDOW_S = 120


def parse(stamp):
    return dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def selftest():
    """Prove the bound this script relies on, and show what the SQLite alternative does.

    Our stamps carry 'T' at position 10; SQLite's datetime() renders a space there, and 'T' > ' '.
    So a datetime() UPPER bound sorts BELOW every same-day stamp and silently drops rows that are
    earlier in real time. The direction matters: as a lower bound datetime() is over-inclusive on
    the boundary day, which is why this class of error does not raise and does not look absurd.
    """
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t (at TEXT)")
    c.execute("INSERT INTO t VALUES ('2026-09-23T10:00:00+00:00')")
    py_lo, py_hi = "2026-09-23T09:42:53+00:00", "2026-09-23T23:59:59+00:00"
    got = c.execute("SELECT COUNT(*) FROM t WHERE at>=? AND at<=?", (py_lo, py_hi)).fetchone()[0]
    assert got == 1, "python-built bounds stopped selecting a 'T' stamp: %r" % got
    bad = c.execute("SELECT COUNT(*) FROM t WHERE at>=datetime('2026-09-23 09:42:53') "
                    " AND at<=datetime('2026-09-23 23:59:59')").fetchone()[0]
    assert bad == 0, ("a datetime() upper bound now selects a same-day 'T' stamp; the warning in "
                      "this file is stale and should be removed rather than trusted, got %r" % bad)
    c.close()
    print("selftest ok: python-built bounds select the row; a datetime() upper bound drops it")


def eligible(conn, source_user_id):
    """Is this wallet able to produce a solana entry attempt at all?

    ⚠️ Read at RUN TIME, not at detection time. A wallet armed or disarmed since the detection is
    classified by its state now. Stated rather than corrected: no per-row arm history is kept.
    """
    w = conn.execute(
        "SELECT real_approved, paused, excluded, real_chains FROM memecoin_wallets "
        " WHERE source_user_id=?", (source_user_id,)).fetchone()
    return bool(w) and w["real_approved"] == 1 and not w["paused"] and not w["excluded"] \
        and "solana" in (w["real_chains"] or "")


def armed_at(conn, source_user_id, when):
    """Was solana armed for this wallet at `when`, per the approvals ledger?

    `eligible()` reads the wallet row as it stands NOW, which misclassifies any detection taken
    while the roster was different. On the first run of this script all ten unmatched detections
    were one wallet inside the 2026-09-23T22:36:29Z sleep-roster revoke, restored 05:27:31Z: not a
    leak, a correct refusal before the hook. Returns True, False, or None when the ledger carries
    no solana action before `when` (unknown, not armed).
    """
    r = conn.execute(
        "SELECT action FROM memecoin_wallet_approvals "
        " WHERE source_user_id=? AND chain='solana' AND created_at<=? "
        " ORDER BY created_at DESC LIMIT 1", (source_user_id, when)).fetchone()
    if r is None:
        return None
    return r["action"] == "chain_arm"


def family(status, reason):
    text = reason or ""
    if status == "filled":
        return "filled"
    if "over ceiling" in text:
        return "mcap over ceiling"
    if "under absolute floor" in text or "under mcap floor" in text:
        return "source-size floor"
    if "under_abs_floor" in text:
        return "h1 volume floor"
    if "already held or reserved" in text:
        return "one position per coin"
    if "cooldown" in text:
        return "re-entry cooldown"
    if "above source price" in text or "past their entry" in text:
        return "chase cap"
    if "coin_too_young" in text:
        return "coin_too_young"
    return "other: " + text[:40]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--until", help="Inclusive fixed UTC end; defaults to the current time")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0

    since_at = parse(args.since)
    until_at = parse(args.until) if args.until else dt.datetime.now(dt.timezone.utc)
    if since_at.tzinfo is None or until_at.tzinfo is None or until_at < since_at:
        ap.error("--since and --until must be aware instants in ascending order")
    since = since_at.astimezone(dt.timezone.utc).isoformat()
    until = until_at.astimezone(dt.timezone.utc).isoformat()
    c = sqlite3.connect(DB, uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    hours = (until_at - since_at).total_seconds() / 3600
    print("window %sZ .. %sZ (%.1f h)" % (since[:19], until[:19], hours))

    det = c.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT address) addrs, COUNT(DISTINCT mint) mints "
        "  FROM onchain_wallet_detections WHERE network_id='solana' AND detected_at>=? AND detected_at<=?",
        (since, until)).fetchone()
    att = c.execute(
        "SELECT COUNT(*) n FROM memecoin_entry_attempts "
        " WHERE network_id='solana' AND signal_source='onchain_watch' "
        " AND decision_started_at>=? AND decision_started_at<=?", (since, until)).fetchone()
    fill = c.execute(
        "SELECT COUNT(*) n, ROUND(SUM(COALESCE(realized_pnl,0)),2) pnl, SUM(is_open) still_open "
        "  FROM memecoin_positions WHERE execution_mode='real' AND network_id='solana' "
        "   AND signal_source='onchain_watch' AND opened_at>=? AND opened_at<=?",
        (since, until)).fetchone()
    print("COUNT detections %d  (%d addresses, %d mints)" % (det["n"], det["addrs"], det["mints"]))
    print("COUNT attempts   %d  (signal_source='onchain_watch')" % att["n"])
    print("COUNT fills      %d  realized %s  still open %s" % (fill["n"], fill["pnl"],
                                                               fill["still_open"]))

    print("\nattempt families (signal_source='onchain_watch')")
    fams = {}
    for r in c.execute(
            "SELECT terminal_status, terminal_reason FROM memecoin_entry_attempts "
            " WHERE network_id='solana' AND signal_source='onchain_watch' "
            " AND decision_started_at>=? AND decision_started_at<=?", (since, until)):
        k = family(r["terminal_status"], r["terminal_reason"])
        fams[k] = fams.get(k, 0) + 1
    for k, v in sorted(fams.items(), key=lambda kv: -kv[1]):
        print("  %-24s %3d" % (k, v))

    lat = sorted(r[0] for r in c.execute(
        "SELECT detect_latency_s FROM onchain_wallet_detections "
        " WHERE network_id='solana' AND detected_at>=? AND detected_at<=? "
        " AND detect_latency_s IS NOT NULL", (since, until)))
    if lat:
        print("\ndetect_latency_s n=%d p50=%.2f p90=%.2f"
              % (len(lat), lat[len(lat) // 2], lat[int(len(lat) * 0.9)]))
    fm = c.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN feed_event_id IS NOT NULL THEN 1 ELSE 0 END) matched "
        "  FROM onchain_wallet_detections WHERE network_id='solana' "
        " AND detected_at>=? AND detected_at<=?", (since, until)).fetchone()
    print("feed-matched when recorded %s of %s (later delivery unresolved)"
          % (fm["matched"], fm["n"]))
    # A NULL is only the watcher's one-shot result. Re-read exact transaction identity at the
    # fixed cutoff; a same-wallet/coin event nearby is not proof of delivery of this swap.
    pending = c.execute(
        "SELECT signature,source_user_id,mint,detected_at FROM onchain_wallet_detections "
        "WHERE network_id='solana' AND feed_event_id IS NULL "
        "AND detected_at>=? AND detected_at<=?", (since, until)).fetchall()
    later_exact = 0
    feed_cache = {}

    def delivered_by_cutoff(stamp, detected):
        try:
            arrival = parse(stamp)
            return arrival.tzinfo is not None and parse(detected) < arrival <= until_at
        except (TypeError, ValueError):
            return False

    for detection in pending:
        key = (detection["source_user_id"], detection["mint"])
        if key not in feed_cache:
            feed_cache[key] = c.execute(
                "SELECT fomo_event_id,COALESCE(received_at_normalized,received_at) AS delivered_at "
                "FROM memecoin_events WHERE network_id='solana' AND source_user_id=? "
                "AND mint=? AND side='buy'", key).fetchall()
        if any(event_transaction_signature(event["fomo_event_id"]) == detection["signature"]
               and delivered_by_cutoff(event["delivered_at"], detection["detected_at"])
               for event in feed_cache[key]):
            later_exact += 1
    print("later exact feed arrival by cutoff %d of %d originally unmatched; "
          "%d remain unobserved/unknown" % (later_exact, len(pending), len(pending) - later_exact))

    print("\nATTRIBUTION: does signal_source carry detection lineage?")
    rows = c.execute(
        "SELECT signature, source_user_id, handle, mint, detected_at FROM onchain_wallet_detections "
        " WHERE network_id='solana' AND detected_at>=? AND detected_at<=?",
        (since, until)).fetchall()
    by_source, unmatched, elig_n = {}, [], 0
    for r in rows:
        if not eligible(c, r["source_user_id"]):
            continue
        elig_n += 1
        t = parse(r["detected_at"])
        lo = (t - dt.timedelta(seconds=MATCH_WINDOW_S)).isoformat()
        hi = min(t + dt.timedelta(seconds=MATCH_WINDOW_S), until_at).astimezone(dt.timezone.utc).isoformat()
        candidates = c.execute(
            "SELECT signal_source, origin_event_id FROM memecoin_entry_attempts "
            " WHERE network_id='solana' AND mint=? AND source_user_id=? "
            "   AND decision_started_at>=? AND decision_started_at<=? "
            " ORDER BY decision_started_at",
            (r["mint"], r["source_user_id"], lo, hi)).fetchall()
        a = next((item for item in candidates if
                  event_transaction_signature(item["origin_event_id"]) == r["signature"]), None)
        if a is None:
            unmatched.append((r["handle"], r["mint"], str(r["detected_at"])[11:19],
                              r["source_user_id"], str(r["detected_at"])))
        else:
            by_source[a["signal_source"]] = by_source.get(a["signal_source"], 0) + 1
    print("  detections on wallets able to attempt (state read now): %d of %d" % (elig_n, len(rows)))
    print("  exact-origin attempts within +/-%ds: %s" % (MATCH_WINDOW_S, by_source))
    print("  no exact-origin attempt: %d" % len(unmatched))
    for h, mint, at, uid, full_at in unmatched:
        state = armed_at(c, uid, full_at)
        label = {True: "armed at detection", False: "NOT armed at detection (revoked)",
                 None: "no solana action in the approvals ledger"}[state]
        print("    %-16s %s %sZ  %s" % (h, mint[:12], at, label))
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
