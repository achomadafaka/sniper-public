#!/usr/bin/env python3
"""Helius G1 creator history + G2 coin age worker (stage 1: nothing here is read by JUDGE; spec docs/superpowers/specs/2026-10-27-helius-history-backfill-design.md). RESEARCH ONLY: the paid
research key `a56bd061` (MEMECOIN_RESEARCH_HELIUS_RPC_URL), never a funded provider, never the entry path. nice 19 / ionice idle, single instance (flock).
    nice -n 19 ionice -c3 python3 scripts/memecoin_desk_backfill_worker.py nightly [--day YYYY-MM-DD] [--run-cap N]
    python3 scripts/memecoin_desk_backfill_worker.py calibrate --launches-day YYYY-MM-DD [--coins 20] [--creators 3]     # step 0: spends ~350 credits, writes calibration.json
    python3 scripts/memecoin_desk_backfill_worker.py attest-costs --dashboard-delta N [--shared-key --expected-min M]     # operator read after calibrate; writes costs.json iff N fits the bound (shared key: iff N >= M)
Files under <desk>/backfill: cycle.json (operator, once per cycle), credits.db (ledger), history.db (results), costs.json (after calibration), worker.heartbeat.json."""
import argparse
import fcntl
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_backfill_history as H  # noqa: E402
from sniper.services import memecoin_desk_backfill_ledger as L  # noqa: E402

from sniper.services.memecoin_desk_backfill_transport import ENV_PATH, KEY_ENV, KEY_PREFIX, Refused, helius_url, load_costs, make_transport  # noqa: E402,F401

DOC_COSTS = {H.METHOD_FULL: 10}          # Helius docs 2026-10-28: full mode 10 credits per 100 txs returned (ceil, min 10); every request here asks <= 100 txs
MODEL_LABEL = "operator_attested_10_per_attempt_shared_key_floor_only"
DEFAULT_RUN_CAP = 100_000                # about 10,000 coins a night; the month cap (5,000,000) allows ~166,000 a day
TERMINAL = frozenset(("known", "no_history", "first_tx_not_creation", "block_time_unusable", "slot_unusable", "unreadable_tx"))


def _ro(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)


def load_targets(desk, day):
    """Mints seen on `day`, tiered: candidate emissions (tier 1), then Fomo, DexScreener and Pump.fun first sightings (Solana only). -> [(mint, tier)] first tier wins, stable order."""
    seen, out = set(), []

    def add(mint, tier):
        if mint and mint not in seen:
            seen.add(mint)
            out.append((mint, tier))
    path = Path(desk) / "candidate_emissions" / ("%s.jsonl" % day)
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("network_id") == "solana":
                add(d.get("mint"), 1)
    for feed, tier in (("fomo", 2), ("dexscreener", 3), ("pumpfun", 4)):
        db = Path(desk) / "intake" / feed / "first_sightings.db"
        if db.exists():
            con = _ro(db)
            try:
                for (mint,) in con.execute("select mint from first_sightings where day=? and network_id='solana' order by observed_at", (day,)):
                    add(mint, tier)
            finally:
                con.close()
    return out


def open_run(bdir, costs, *, clock=time.time):
    cycle = L.load_cycle(Path(bdir) / "cycle.json")
    ledger = L.Ledger(Path(bdir) / "credits.db", cycle, costs=costs)
    con = H.connect(Path(bdir) / "history.db")
    return con, ledger


def nightly(desk, day, run_cap, transport, *, clock=time.time, page_cap=H.CREATOR_PAGE_CAP):
    bdir = Path(desk) / "backfill"
    costs = load_costs(bdir)
    if costs is None:
        raise Refused("costs_not_calibrated")
    con, ledger = open_run(bdir, costs)
    ledger.start_run(run_cap)
    if ledger.stopped():
        return {"stopped": "max_usage_tonight"}
    f = H.Fetcher(con, ledger, transport, clock=clock, creator_page_cap=page_cap)
    now = clock()
    run_id = con.execute("insert into g12_runs(mode, day, started_at, run_cap) values('nightly',?,?,?)", (day, now, int(run_cap))).lastrowid     # the run window a reader checks a 'nightly' claim against
    con.commit()
    H.freeze_targets(con, day, [("coin", m, m, t) for m, t in load_targets(desk, day)], now=now)
    stats = {"coins": 0, "creators": 0, "gaps": {}}
    tiers = [r[0] for r in con.execute("select distinct priority from g12_targets where day=? and kind='coin' order by priority", (day,))]     # EVERY tier, not only tiers with pending coins: a done coin's unfinished creator is still owed
    for tier in tiers:                                           # tier by tier: a tier's coins, then ITS creators, before the next tier spends the cap (33k Pump.fun coins would eat 100k credits alone)
        for (mint,) in con.execute("select key from g12_targets where day=? and kind='coin' and state='pending' and priority=? order by rowid", (day, tier)).fetchall():
            if f.stopped:
                break
            res = f.fetch_coin(mint)
            terminal = res in TERMINAL                           # a stop or transient gap leaves the target pending: the next run retries it
            con.execute("update g12_targets set state=?, gap=? where day=? and kind='coin' and key=?", ("done" if terminal else "pending", None if res == "known" else res, day, mint))
            stats["coins"] += 1
            if res != "known":
                stats["gaps"][res] = stats["gaps"].get(res, 0) + 1
        con.commit()
        creators = [r[0] for r in con.execute("select distinct c.creator from coin_creation c join g12_targets t on t.key=c.mint and t.kind='coin' and t.day=? and t.priority=? "
                                              "where c.state='known' and c.creator is not null and c.creator not in (select creator from creator_scans where state='complete')", (day, tier))]
        for cr in creators:
            if f.stopped:
                break
            res = f.fetch_creator(cr)
            stats["creators"] += 1
            if res != "complete":
                stats["gaps"][res] = stats["gaps"].get(res, 0) + 1
        if f.stopped:
            break
    stats.update(stopped=f.stopped, ledger_total=ledger.total(), run_spent=ledger.run_spent, month_cap=ledger.cycle["month_cap"])
    con.execute("update g12_runs set finished_at=?, run_spent=?, outcome=? where run_id=?", (clock(), ledger.run_spent, f.stopped or "done", run_id))
    con.commit()
    return stats


def calibrate(desk, launches_day, n_coins, n_creators, transport, *, clock=time.time):
    bdir = Path(desk) / "backfill"
    con, ledger = open_run(bdir, DOC_COSTS)
    ledger.start_run(1000)
    lcon = _ro(Path(desk) / "launches" / ("%s.db" % launches_day))
    truth = lcon.execute("select mint, creator, slot, signature, block_time from launches where creator is not null order by slot limit ?", (n_coins,)).fetchall()
    lcon.close()
    f = H.Fetcher(con, ledger, transport, clock=clock, creator_page_cap=5)
    rep = {"coins": [], "creators": []}
    for mint, creator, slot, sig, bt in truth:
        res = f.fetch_coin(mint)
        row = con.execute("select slot, signature, block_time, creator from coin_creation where mint=?", (mint,)).fetchone()
        rep["coins"].append({"mint": mint[:6], "res": res, "slot_match": bool(row and row[0] == slot), "sig_match": bool(row and row[1] == sig), "bt_diff_s": (row[2] - bt) if row and row[2] and bt else None,
                             "creator_match": bool(row and row[3] == creator)})
    for creator in sorted({t[1] for t in truth})[:n_creators]:
        res = f.fetch_creator(creator)
        sc = con.execute("select state, reason, pages, txs, launches from creator_scans where creator=?", (creator,)).fetchone()
        rep["creators"].append({"creator": creator[:6], "res": res, "scan": sc})
    rep.update(calls=ledger.counts(), ledger_total=ledger.total(), ran_at=clock(), reserved_bound_per_call=DOC_COSTS[H.METHOD_FULL])
    (Path(desk) / "backfill" / "calibration.json").write_text(json.dumps(rep, indent=1, sort_keys=True, default=str))
    return rep


def _nonneg_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def attest_costs(desk, delta, *, shared_key=False, expected_min=None):
    """Record the operator's attestation of the documented cost assumption. Everything is validated and every read failure is typed BEFORE costs.json is written: a malformed calibration record never enables spend.
    Strict (default): the dashboard delta must not exceed the ledger's own bound. Shared key (Alpha 2026-10-28: a56bd061 is also spent by the research rotation and funded RH deposit proofs): no upper bound
    can be tested; the delta must only reach `expected_min`, an AGGREGATE floor. That floor cannot detect an undercounting model (other consumers can supply the whole delta): costs.json records an
    operator-attested documented 10 credits per request assumption with an aggregate floor ONLY, not a validated upper bound and not the account's remaining credits."""
    bdir = Path(desk) / "backfill"
    try:
        cal = json.loads((bdir / "calibration.json").read_text())
    except (OSError, ValueError) as exc:
        raise Refused("calibration_record_unreadable:%s" % type(exc).__name__)
    bound = cal.get("ledger_total") if isinstance(cal, dict) else None
    if not _nonneg_int(bound):
        raise Refused("calibration_ledger_total_invalid")
    if not _nonneg_int(delta):
        raise Refused("dashboard_delta_invalid")
    if shared_key:
        if not isinstance(expected_min, int) or isinstance(expected_min, bool) or expected_min <= 0:
            raise Refused("shared_key_needs_expected_min")
        if expected_min > bound:
            raise Refused("expected_min_%s_above_ledger_bound_%s" % (expected_min, bound))          # a floor above what our own ledger ever attempted is not a floor
        if delta < expected_min:
            raise Refused("dashboard_delta_%s_below_expected_min_%s" % (delta, expected_min))
    elif delta > bound:
        raise Refused("dashboard_delta_%s_exceeds_documented_bound_%s" % (delta, bound))   # the documented upper bound was wrong: do not enable
    rec = {"calibrated": True, H.METHOD_FULL: 10, "dashboard_delta": delta, "ledger_bound": bound, "attested_at": time.time(), "model": MODEL_LABEL}
    if shared_key:
        rec.update(shared_key=True, expected_min=expected_min,
                   note="operator-attested documented 10 credits/request assumption with an aggregate floor ONLY; a56bd061 is shared (research rotation + funded RH deposit proofs), so the delta includes other "
                        "consumers; no upper bound, no per-call verification and no remaining-credit claim")
    (bdir / "costs.json").write_text(json.dumps(rec))
    return {"costs": "attested", "dashboard_delta": delta, "ledger_bound": bound, "shared_key": bool(shared_key), "expected_min": expected_min}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("nightly", "calibrate", "attest-costs"))
    ap.add_argument("--desk-dir", default=str(ROOT / "data" / "desk"))
    ap.add_argument("--day", default=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat())
    ap.add_argument("--run-cap", type=int, default=DEFAULT_RUN_CAP)
    ap.add_argument("--launches-day")
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--creators", type=int, default=3)
    ap.add_argument("--dashboard-delta", type=int)
    ap.add_argument("--shared-key", action="store_true", help="the key is shared with other consumers: the delta is tested against --expected-min (a floor), not the ledger's upper bound")
    ap.add_argument("--expected-min", type=int)
    ap.add_argument("--env-file", default=ENV_PATH)
    a = ap.parse_args(argv)
    bdir = Path(a.desk_dir) / "backfill"
    bdir.mkdir(parents=True, exist_ok=True)
    lock = open(bdir / "worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 75
    try:
        if a.mode == "attest-costs":
            out = attest_costs(a.desk_dir, a.dashboard_delta, shared_key=a.shared_key, expected_min=a.expected_min)
        else:
            transport = make_transport(helius_url(a.env_file))
            out = nightly(a.desk_dir, a.day, a.run_cap, transport) if a.mode == "nightly" else calibrate(a.desk_dir, a.launches_day, a.coins, a.creators, transport)
        err = None
    except (Refused, L.Refusal) as exc:
        out, err = {}, "%s: %s" % (type(exc).__name__, exc)
    (bdir / "worker.heartbeat.json").write_text(json.dumps({"ts": time.time(), "pid": os.getpid(), "mode": a.mode, "last": out if a.mode != "calibrate" else {"calls": out.get("calls")}, "error": err}, sort_keys=True, default=str))
    print(json.dumps(out if err is None else {"refused": err}, sort_keys=True, default=str)[:2000])
    return 0 if err is None else 2


if __name__ == "__main__":
    sys.exit(main())
