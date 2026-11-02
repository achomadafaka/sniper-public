"""Gamma watch C: the source-sell mirror's first decisions on held coins, and the vault-seed deploy.

Three reads over one window, ledger only (read-only, no RPC):

  mirror    memecoin_exit_trigger_audit rows with lane source_sell_mirror (reason mirror_source_sell).
            Each decision is its own row since S27-04 (keyed on decision_at). Per row: position,
            allowed, share/cumulative/basis, recv_to_decision_ms, lock_wait_ms, exit_status, and
            receipt-to-send = the exit's first memecoin_submission_attempts.created_at (owner_kind
            close_claim = the position's close_claim_id) minus the sell's seen_at.
  voldeath  Solana vol_death audit verdicts ('<verdict> silence=<s>s' in detail), before vs after
            --cut. The vault seed (2d770bd0, deployed 11:24Z) lets the first trade after a 30 s
            epoch rollover emit a sol_swap row, which can turn a 'still' into a 'moving'. The audit
            upserts per (position, reason, minute), so these are per-minute verdicts, lossy.
  density   fastlane_sol_swap tape marks per held minute of funded Solana positions, before vs
            after --cut. A PROXY for recovered first-after-rollover trades: the seed rows are not
            logged, and the tape has a write floor, so this rises only if trades were being lost.

    python3 scripts/research/solana_exit_watch.py --since 2026-09-27T10:00:00Z --cut 2026-09-27T11:24:00Z
    python3 scripts/research/solana_exit_watch.py --selftest
    python3 -m pytest tests/test_research_solana_exit_watch.py -q
"""
import argparse
import collections
import contextlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEDGER = f"file:{os.path.join(REPO, 'sniper_tracker.db')}?mode=ro"
VERDICT = re.compile(r"^([a-z_]+) silence=")


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(dt):
    # The tape stamps '.ffffff+00:00' (43,679 of 43,679 Solana marks since 09-20), so a text bound
    # in the same spelling is exact and uses the (chain_id, mint, sweep_ts) index.
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def verdict(detail):
    m = VERDICT.match(detail or "")
    return m.group(1) if m else "other"


def mirror(c, since):
    out = []
    for pid, allowed, checked_at, detail in c.execute(
            "SELECT position_id, allowed, checked_at, detail FROM memecoin_exit_trigger_audit "
            "WHERE lane='source_sell_mirror' AND checked_at >= ? ORDER BY id", (iso(since),)):
        d = json.loads(detail or "{}")
        claim = c.execute("SELECT close_claim_id FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
        sent = c.execute("SELECT MIN(created_at) FROM memecoin_submission_attempts WHERE action='exit' "
                         "AND owner_kind='close_claim' AND owner_id=?", (claim[0],)).fetchone() if claim and claim[0] else None
        r2s = (round((ts(sent[0]) - ts(d["seen_at"])).total_seconds() * 1000, 1)
               if sent and sent[0] and d.get("seen_at") else None)
        out.append({"pos": pid, "allowed": allowed, "at": checked_at[11:23], "path": d.get("path"),
                    "share": d.get("share"), "cum": d.get("cumulative"), "basis": d.get("share_basis"),
                    "recv_to_decision_ms": d.get("recv_to_decision_ms"), "lock_wait_ms": d.get("lock_wait_ms"),
                    "exit": d.get("exit_status"), "receipt_to_send_ms": r2s})
    return out


def voldeath(c, since, cut):
    counts = {"before": collections.Counter(), "after": collections.Counter()}
    for checked_at, allowed, detail in c.execute(
            "SELECT a.checked_at, a.allowed, a.detail FROM memecoin_exit_trigger_audit a "
            "JOIN memecoin_positions p ON p.id=a.position_id WHERE a.reason='vol_death' "
            "AND p.network_id='solana' AND a.checked_at >= ?", (iso(since),)):
        counts["after" if ts(checked_at) >= cut else "before"][(verdict(detail), allowed)] += 1
    return counts


def density(c, since, cut, now):
    acc = {"before": [0, 0.0], "after": [0, 0.0]}      # [marks, held minutes]
    for mint, opened, closed in c.execute(
            "SELECT mint, opened_recorded_at, closed_at FROM memecoin_positions WHERE execution_mode='real' "
            "AND network_id='solana' AND opened_recorded_at IS NOT NULL AND (closed_at IS NULL OR closed_at >= ?)",
            (iso(since),)):
        lo, hi = max(ts(opened), since), min(ts(closed) if closed else now, now)
        for side, a, b in (("before", lo, min(hi, cut)), ("after", max(lo, cut), hi)):
            if b <= a:
                continue
            n = c.execute("SELECT count(*) FROM memecoin_position_marks WHERE chain_id='solana' AND mint=? "
                          "AND sweep_ts >= ? AND sweep_ts < ? AND source='fastlane_sol_swap'",
                          (mint, iso(a), iso(b))).fetchone()[0]
            acc[side][0] += n
            acc[side][1] += (b - a).total_seconds() / 60
    return {k: {"marks": m, "held_min": round(h, 1), "per_min": round(m / h, 2) if h else None}
            for k, (m, h) in acc.items()}


def selftest():
    assert verdict("measured_state_still silence=47.6s") == "measured_state_still"
    assert verdict("exact_quote_moving silence=-1.0s") == "exact_quote_moving"
    assert verdict(None) == "other" and verdict("closed by operator") == "other"
    assert iso(ts("2026-09-27T11:24:00Z")) == "2026-09-27T11:24:00.000000+00:00"
    # The ledger joins are tested on a fixture DB in tests/test_research_solana_exit_watch.py, not
    # here: a fixture that creates ledger tables in this file made the brain index list this
    # read-only script as a writer of four tables.
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--cut", default="2026-09-27T11:24:00Z")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        since, cut, now = ts(a.since), ts(a.cut), datetime.now(timezone.utc)
        with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=10)) as c:
            rows = mirror(c, since)
            print("mirror decisions", len(rows))
            for r in rows:
                print("  ", json.dumps(r))
            for side, cnt in voldeath(c, since, cut).items():
                print("vol_death", side, dict(cnt))
            print("sol_swap density", json.dumps(density(c, since, cut, now)))
