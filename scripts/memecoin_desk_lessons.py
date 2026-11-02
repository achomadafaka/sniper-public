#!/usr/bin/env python3
"""Desk LESSONS worker (REPORT ONLY; spec sniper/services/memecoin_desk_lessons.py). One run: find the desk trades (and the counterfactual
units) whose 15-minute after-window has passed and that have no row yet, compute their facts from the curve/PumpSwap tape, and append
IMMUTABLE rows to <state root>/data/desk/lessons.db. Read-only on the ledger, the desk DB and every tape (short autocommit statements,
handles closed each time: no WAL pin). Writes only lessons.db. A failure here cannot touch trading.

STATE ROOT: every default path comes from SNIPER_STATE_ROOT (sniper.util.state_paths), never from the release the cron starts in, and the
state root's .env is loaded BEFORE any knob is read (kill switch, sample share).

Units are classified from DURABLE state (the desk outbox, the fill ticket, the ledger position), never from a missing row:
  realized             a closed desk position (an open or submitted_unknown one waits until it closes)
  counterfactual       a JUDGE ENTER whose outbox intent EXPIRED before any attempt, or whose fill ticket says refused
  skip_counterfactual  a hash-sampled share (MEMECOIN_DESK_LESSONS_SKIP_SAMPLE_PCT, default 20) of JUDGE SKIPs and of hard-filter PASSes
                       that were never vetted
An ENTER whose intent was sent without a fill ticket yet, or whose position exists but is not closed, is NOT classified: it waits.

RETRY vs TERMINAL: an unreadable tape, an unproven capture, an unreadable PumpSwap tape or a missing day file is an OUTAGE: the unit is not
stored and is retried on a later run, until it is older than GIVE_UP_S, then it is stored as censored for good. A fact that is absent while the
capture is PROVEN alive is terminal evidence and is stored as censored with its reason.

TIME: ledger timestamps are corrected UTC; desk response/fire and tape times are raw host time. Every ledger time is converted with the
learned offset (host = corrected + offset) before it meets the tape; human-facing times subtract that offset once.

Cron (Alpha installs; every 5 min; flock makes a second copy exit):
  */5 * * * * cd <release> && SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper python3 scripts/memecoin_desk_lessons.py >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_lessons.log 2>&1
Kill: MEMECOIN_DESK_LESSONS_ENABLED=0 in the state .env (exits at once) or remove the cron line; rollback = delete lessons.db.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import statistics
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_lessons as L          # noqa: E402

MAX_PER_RUN = 12                  # CPU bound per cron tick (a unit costs about 0.3 s); the backlog drains over the next ticks
STATUS_SCHEMA = ("CREATE TABLE IF NOT EXISTS lessons_run(at REAL, queued INTEGER, written INTEGER, retryable INTEGER, censored INTEGER, "
                 "waiting INTEGER, offset_s REAL, error TEXT)")
ATTEMPTS_SCHEMA = "CREATE TABLE IF NOT EXISTS lessons_attempts(key TEXT PRIMARY KEY, last_at REAL, n INTEGER)"


def _ep(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


class Clock:
    """Corrected UTC (ledger, desk DB) -> the tape's host clock. host = corrected + the learned offset."""

    def __init__(self, offset_s):
        self.offset_s = offset_s

    def tape(self, iso):
        return _ep(iso) + self.offset_s


def entry_swap(desk_dir, mint, sig, qty_ledger, near):
    """(row, status): the curve swap of our entry by signature + mint + BUY, disambiguated by the ledger fill's token quantity.
    status: ok | absent (the tape is readable and has no such swap) | ambiguous (more than one fits) | unreadable (no day file)."""
    seen_file = False
    for day in (L.utc_day(near), L.utc_day(near - 86400), L.utc_day(near + 86400)):
        path = Path(desk_dir) / "swaps" / ("%s.db" % day)
        conn = L.ro(path)
        if conn is None:
            continue
        conn.close()
        seen_file = True
        columns = {r["name"] for r in L.q(path, "PRAGMA table_info(swaps)")}
        optional = ", ".join(name if name in columns else "NULL AS " + name for name in ("quote_mint", "cashback_fee_bps", "tx_index"))
        rows = L.q(path, "SELECT received_at, token_atomic, quote_atomic, fee_bps, creator_fee_bps, actor, %s FROM swaps "
                         "WHERE signature=? AND mint=? AND is_buy=1" % optional, (sig, mint))
        if qty_ledger:
            bound = [r for r in rows if abs(r["token_atomic"] / 1e6 - qty_ledger) <= 1e-6 * qty_ledger]
            if rows and not bound:
                return None, "mismatch"                 # a row with this signature exists but is not our fill's quantity: never accepted
            rows = bound
        if len(rows) == 1:
            return rows[0], "ok"
        if len(rows) > 1:
            return None, "ambiguous"
    return None, ("absent" if seen_file else "unreadable")


def fill_latency(desk_db):
    rows = L.q(desk_db, "SELECT v.response_at AS d, f.ts AS f FROM desk_tickets v JOIN desk_tickets f ON f.ticket_id=v.ticket_id "
                        "WHERE v.stage='vet' AND v.verdict='ENTER' AND f.stage='fill' AND f.verdict='ENTER'")
    lat = [_ep(r["f"]) - _ep(r["d"]) for r in rows if r["d"] and r["f"]]
    return statistics.median(lat) if lat else 1.5


def _vet_facts(v):
    """(rails, emitted_at iso) of a vet ticket row; each None when absent."""
    rails = emitted = None
    try:
        rails = (json.loads(v["output_json"]).get("result") or {}).get("rails")
        emitted = json.loads(v["inputs_json"])["asof"]["candidate"]["emitted_at"]
    except (ValueError, KeyError, TypeError):
        pass
    return rails, emitted


def _learning_inputs(vet, trigger=None):
    """Only frozen JUDGE metadata and the durable fire receipt; request/queue times are not touches."""
    def parsed(row, field):
        try:
            value = json.loads((row or {}).get(field) or "{}")
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def epoch(value):
        if L._fin(value):
            return float(value)
        try:
            if value is None:
                return None
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    mapping = lambda value: value if isinstance(value, dict) else {}
    inputs, output, fired = parsed(vet, "inputs_json"), parsed(vet, "output_json"), parsed(trigger, "inputs_json")
    sheet, standing = mapping(inputs.get("sheet")), mapping(inputs.get("trigger"))
    result = mapping(output.get("result")) or mapping(inputs.get("result"))
    plan, fire = mapping(result.get("plan")), mapping(fired.get("fire"))
    response = epoch(inputs.get("response_at"))
    return {"watch_started_at": response if response is not None else epoch((vet or {}).get("response_at")),
            "watch_expires_at": epoch(standing.get("expires_at")),
            "trigger_at": epoch(fire.get("received_at")), "judge_mcap_usd": sheet.get("mcap_usd"),
            "plan_horizon_s": plan.get("horizon_s", plan.get("h_s")), "entry_venue": sheet.get("venue")}


def discover(ledger, desk_db, now, clock, skip_pct, done):
    """Units not yet stored, classified from durable state. Returns (units, waiting) where `waiting` counts units that cannot be classified
    yet (open / unsent / sent-without-fill). Heavy per-unit lookups run only for units NOT in `done`."""
    units, waiting = [], 0
    tickets = L.q(desk_db, "SELECT ticket_id, mint, stage, verdict, reason, ts, response_at, output_json, inputs_json, cohort_id, prompt_version "
                           "FROM desk_tickets WHERE stage IN ('vet','fill','hard_filter','trigger')", strict=True)
    ob_cols = {r["name"] for r in L.q(desk_db, "PRAGMA table_info(desk_outbox)", strict=True)}      # an older outbox has no reason column
    outbox = {r["ticket_id"]: (r["state"], r.get("reason")) for r in L.q(
        desk_db, "SELECT ticket_id, state%s FROM desk_outbox" % (", reason" if "reason" in ob_cols else ""), strict=True)}
    fills, vets, hf, triggers = {}, {}, {}, {}
    for t in tickets:
        {"fill": fills, "vet": vets, "hard_filter": hf, "trigger": triggers}[t["stage"]][t["ticket_id"]] = t
    pos_cols = {r["name"] for r in L.q(ledger, "PRAGMA table_info(memecoin_positions)", strict=True)}
    positions = L.q(ledger, "SELECT id, mint, opened_at, closed_at, realized_pnl, exit_reason, entry_notional, desk_opportunity_id, is_open "
                            ", %s AS entry_mcap FROM memecoin_positions WHERE signal_source='desk' ORDER BY id" % (
                                "entry_mcap" if "entry_mcap" in pos_cols else "NULL"), strict=True)
    by_opp = {p["desk_opportunity_id"]: p for p in positions if p["desk_opportunity_id"]}
    for p in positions:
        key = "pos:%d" % p["id"]
        if key in done:
            continue
        if p["is_open"] or p["closed_at"] is None:
            waiting += 1
            continue
        te = clock.tape(p["closed_at"])
        if now < te + L.HOLD_S + 60:
            waiting += 1
            continue
        fl = L.q(ledger, "SELECT kind, tx_sig, qty FROM memecoin_fills WHERE position_id=? ORDER BY id", (p["id"],), strict=True)   # a read failure is not 'no fill'
        entries = [f for f in fl if f["kind"] == "entry"]
        entry = entries[0] if entries else None
        disposals = [f for f in fl if f["kind"] != "entry"]
        exit_ = disposals[-1] if disposals else None
        v = vets.get(p["desk_opportunity_id"]) if p["desk_opportunity_id"] else None
        rails, emitted = _vet_facts(v) if v else (None, None)
        units.append({"kind": "realized", "key": key, "position_id": p["id"], "ticket_id": p["desk_opportunity_id"], "mint": p["mint"],
                      "t_ref": clock.tape(p["opened_at"]), "t_verdict": _ep(v["response_at"]) if v and v["response_at"] else None,        # explicit response time or unknown: never the request time
                      "rails": rails, "emitted_at": _ep(emitted) if emitted else None,       # ledger = corrected UTC -> tape; desk = raw host = tape
                      "entry_sig": entry["tx_sig"] if entry else None, "entry_qty": entry["qty"] if entry else None, "multi_entry": len(entries) > 1,
                      "cohort": v["cohort_id"] if v else None, "prompt_version": v["prompt_version"] if v else None,
                      **_learning_inputs(v, triggers.get(p["desk_opportunity_id"])), "fill_mcap_usd": p["entry_mcap"],
                      "closed_at_corrected": _ep(p["closed_at"]),
                      "exit_info": {"exit_ts": te, "exit_sig": exit_["tx_sig"] if exit_ else None,
                                    "legs": [{"sig": d["tx_sig"], "qty": d["qty"], "kind": d["kind"]} for d in disposals] if disposals else None,
                                    "realized_pnl_usd": p["realized_pnl"],
                                    "exit_reason": p["exit_reason"], "entry_notional_usd": p["entry_notional"]}})
    for tid, v in vets.items():
        key = "tkt:" + tid
        if key in done:
            continue
        t_ref = _ep(v["response_at"] or v["ts"])                    # desk timestamps are raw host time already: no second offset (a NULL response_at gates
        no_verdict_time = not v["response_at"]                      # the wait only; the unit is stored CENSORED, never given a simulated decision time)
        if now < t_ref + L.HOLD_S + 60:
            waiting += 1
            continue
        rails, emitted = _vet_facts(v)
        base = {"key": key, "ticket_id": tid, "mint": v["mint"], "t_ref": t_ref, "rails": rails, "no_verdict_time": no_verdict_time,
                "emitted_at": _ep(emitted) if emitted else None, "cohort": v["cohort_id"], "prompt_version": v["prompt_version"],
                **_learning_inputs(v, triggers.get(tid))}
        if v["verdict"] == "ENTER":
            f = fills.get(tid)
            if tid in by_opp:                                        # a position exists: it is realized when it closes, never a counterfactual
                continue
            state, why = outbox.get(tid, (None, None))
            if f is not None and f["verdict"] != "ENTER":
                units.append({**base, "kind": "counterfactual", "why": "refused"})
            elif f is None and state == "expired":
                units.append({**base, "kind": "counterfactual", "why": "expired_before_attempt"})
            elif f is None and state == "refused":                   # terminal pre-enqueue refusal: no fill row will ever arrive
                units.append({**base, "kind": "counterfactual",
                              "why": "refused_pre_enqueue:" + (why if isinstance(why, str) and re.fullmatch(r"[a-z0-9_.-]{1,60}", why) else "unknown")})
            else:                                                    # sent / claimed / unknown without a terminal fill: wait, do not guess
                waiting += 1
        elif L.hash_sampled(tid, skip_pct):
            units.append({**base, "kind": "skip_counterfactual", "rails": None, "why": v["reason"]})
    for tid, h in hf.items():                                        # PASSed the hard filter and was never vetted
        key = "tkt:" + tid
        if h["verdict"] != "PASS" or tid in vets or key in done:
            continue
        t_ref = _ep(h["ts"])
        if now >= t_ref + L.HOLD_S + 60 and L.hash_sampled(tid, skip_pct):
            units.append({"kind": "skip_counterfactual", "key": key, "ticket_id": tid, "mint": h["mint"], "t_ref": t_ref, "rails": None,
                          "emitted_at": None, "why": "never_vetted", "cohort": h["cohort_id"], "prompt_version": None})
    return units, waiting


class Retry(Exception):
    """An outage (not evidence): the unit is not stored and is retried on a later run."""


def compute(unit, desk_dir, latency, now, clock):
    t_ref = unit["t_ref"]
    cost = qty = t_fill = None
    exit_info = unit.get("exit_info")
    old = now - t_ref > L.GIVE_UP_S
    closed = unit.get("closed_at_corrected")
    if closed is None and exit_info and L._fin(exit_info.get("exit_ts")):
        closed = exit_info["exit_ts"] - clock.offset_s
    identity = {"cohort_id": unit.get("cohort"), "prompt_version": unit.get("prompt_version"), "clock_offset_s": clock.offset_s,
                "position_id": unit.get("position_id"), "ticket_id": unit.get("ticket_id"),
                "clock_domains": {"ledger": "corrected_utc_to_tape_by_offset", "desk": "raw_host_equals_tape"},
                "clock_offset_provenance": "current_offset_at_compute_time",
                "assumptions": {"fx0": L.FX0, "lag_s": L.LAG_S, "hold_s": L.HOLD_S, "pool_fresh_s": L.POOL_FRESH_S,
                                "migration_max_s": L.MIGRATION_MAX_S, "death_k": L.DEATH_K, "death_k_provisional": True}}

    def completed(facts, tags=()):
        facts.setdefault("feature_version", L.FEATURE_VERSION)
        facts.setdefault("kind", unit["kind"])
        facts.setdefault("t0", t_fill if t_fill is not None else t_ref)
        facts["t0_corrected"] = facts["t0"] - clock.offset_s
        facts["closed_at_corrected"] = closed
        facts["identity"] = dict(identity)
        if "learning_loop" not in facts:
            facts["learning_loop"] = L.learning_facts({**unit, "t_fill": t_fill, "cost_sol": cost, "qty": qty,
                                                       "closed_at_corrected": closed}, L.Tape([]), clock_offset_s=clock.offset_s)
            facts["learning_loop"]["censored"] = list(dict.fromkeys(facts.get("censored", []) + facts["learning_loop"]["censored"]))
        if unit["kind"] == "realized" and "exit" not in facts:
            facts["exit"] = {"reason": (exit_info or {}).get("exit_reason"), "realized_R": None,
                             "realized_pnl_usd": L._r((exit_info or {}).get("realized_pnl_usd"), 2),
                             "entry_notional_usd": L._r((exit_info or {}).get("entry_notional_usd"), 2)}
        if unit.get("why"):
            facts["why"] = unit["why"]
        return facts, list(tags)

    def censored(reason):
        if not old:
            raise Retry(reason)
        return completed({"censored": [reason, "gave_up_after_outage"]})
    if unit.get("no_verdict_time"):                              # a vet row without an explicit response time proves no decision moment: no entry, no rank, no alternatives
        return completed({"censored": ["no_verdict_time"]})
    if unit["kind"] == "realized":
        if unit.get("entry_venue") == "pumpswap":
            return completed({"censored": ["post_migration_entry_censored"]})
        if not unit.get("entry_sig"):
            return completed({"censored": ["no_entry_fill"]})
        es, status = entry_swap(desk_dir, unit["mint"], unit["entry_sig"], unit.get("entry_qty"), t_ref)
        if status == "unreadable":
            return censored("tape_unreadable")
        if status == "ambiguous":
            return completed({"censored": ["entry_swap_ambiguous"]})
        if status == "mismatch":
            return completed({"censored": ["entry_swap_mismatch"]})
        if unit.get("multi_entry"):
            return completed({"censored": ["multiple_entry_fills"]})
        if es is None:
            cov = L.capture_coverage(desk_dir, t_ref, t_ref + 120)
            if cov is None or cov < t_ref + 60:                      # capture not proven alive past the entry: an outage, not evidence
                return censored("capture_unproven")
            return completed({"censored": ["entry_swap_missing"]})
        from sniper.execution.pump_curve_buy import SOL_QUOTE_MINTS
        if es.get("quote_mint") is not None and es["quote_mint"] not in SOL_QUOTE_MINTS:
            return completed({"censored": ["non_sol_quote_mint"]})
        parts = L._curve_fee_parts(es)
        if parts is None:
            return completed({"censored": ["entry_fee_unknown"]})
        cost = L.curve_spend_for_net(int(es["quote_atomic"]), parts) / 1e9
        qty, t_fill = es["token_atomic"], es["received_at"]
    hi = max(t_ref, t_fill or t_ref, exit_info["exit_ts"] if exit_info else t_ref) + L.HOLD_S + 60
    launch_at = L.launch_time(desk_dir, unit["mint"], t_ref)
    lo = min(t_ref - 1500, launch_at - 5) if launch_at is not None else t_ref - 1500
    watch_start = unit.get("watch_started_at")
    if L._fin(watch_start):
        lo = min(lo, watch_start - 5)
    if not (L.tape_day_readable(desk_dir, t_ref) and L.tape_day_readable(desk_dir, hi)):
        return censored("tape_day_missing")
    rows = L.load_curve(desk_dir, unit["mint"], lo, hi)
    if not rows:
        return censored("no_curve_rows")
    cov = L.capture_coverage(desk_dir, t_fill or t_ref, hi)
    if cov is None:
        return censored("capture_unproven")
    if cov < hi - 60 and not old:                               # the capture has not yet reached the end of this unit's window: pending, not final
        raise Retry("capture_pending")
    pool_stats, invalid_pool = {}, []
    pool = L.load_pool(desk_dir, unit["mint"], t_ref - 5, hi, stats=pool_stats, invalid_states=invalid_pool)
    if pool is None:
        return censored("pool_tape_unreadable")
    tape = L.Tape(rows, pool, coverage_until=cov, pool_invalid_states=invalid_pool)
    f, tags = L.facts_for({"kind": unit["kind"], "t_ref": t_ref, "cost_sol": cost, "qty": qty, "t_fill": t_fill, "rails": unit.get("rails"),
                           "emitted_at": unit.get("emitted_at"), "t_verdict": unit.get("t_verdict")}, tape, launch_at=launch_at, fill_latency=latency, exit_info=exit_info)
    watch_cov = L.capture_coverage(desk_dir, watch_start, hi) if L._fin(watch_start) else None
    modeled = unit["kind"] != "realized"
    f["learning_loop"] = L.learning_facts({**unit, "t_fill": f.get("t0") if modeled else t_fill,
                                          "cost_sol": L.CLIP_USD / L.FX0 if modeled else cost,
                                          "qty": f.get("qty") if modeled else qty, "closed_at_corrected": closed}, tape,
                                          watch_coverage_until=watch_cov, clock_offset_s=clock.offset_s)
    identity["source_watermark"] = {"capture_proven_until": cov, "watch_capture_proven_until": watch_cov,
                                    "curve_rows": len(rows), "pool_states": len(pool), "pool_invalid_states": len(invalid_pool),
                                    "pool_dropped": pool_stats}
    return completed(f, tags)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--desk-dir", default=None)
    ap.add_argument("--desk-db", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--max", type=int, default=MAX_PER_RUN)
    a = ap.parse_args(argv)
    from sniper.util.state_paths import require_state_root_outside_release, state_path, state_root
    require_state_root_outside_release()
    from sniper.util.envfiles import load_env_files
    load_env_files(str(state_root()))                             # the state root's .env BEFORE any knob is read
    if os.getenv("MEMECOIN_DESK_LESSONS_ENABLED", "1").strip() == "0":
        print("[lessons] MEMECOIN_DESK_LESSONS_ENABLED=0; exiting")
        return 0
    db_env = os.getenv("DB_PATH") or "sniper_tracker.db"
    ledger = a.ledger or (db_env if os.path.isabs(db_env) else str(state_path(db_env)))
    desk_dir = Path(a.desk_dir or state_path("data", "desk"))
    desk_db = a.desk_db or str(state_path("data", "memecoin_desk.db"))
    out = Path(a.out) if a.out else desk_dir / "lessons.db"
    desk_dir.mkdir(parents=True, exist_ok=True)
    lock = open(desk_dir / "lessons.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    db = L.connect(out)
    db.execute(STATUS_SCHEMA)
    db.execute(ATTEMPTS_SCHEMA)
    now = time.time()
    from sniper.util import clock as clockmod
    clock = Clock(clockmod.offset_s())
    done = {r[0] for r in db.execute("SELECT key FROM lessons WHERE feature_version=?", (L.FEATURE_VERSION,))}
    skip_pct = int(os.getenv("MEMECOIN_DESK_LESSONS_SKIP_SAMPLE_PCT", "20"))
    try:
        units, waiting = discover(ledger, desk_db, now, clock, skip_pct, done)
    except (RuntimeError, sqlite3.Error) as exc:                  # a required source unreadable: say so (log + status row), never an empty run
        print("[lessons] source unreadable: %s" % exc, file=sys.stderr)
        db.execute("INSERT INTO lessons_run VALUES (?,?,?,?,?,?,?,?)", (now, 0, 0, 0, 0, 0, clock.offset_s, str(exc)[:300]))
        db.commit()
        return 0
    last_try = {r[0]: r[1] for r in db.execute("SELECT key, last_at FROM lessons_attempts")}
    # realized first; inside a kind the units tried LEAST recently go first (never tried = 0), so a retrying outage cannot starve the rest
    units.sort(key=lambda u: (u["kind"] != "realized", last_try.get(u["key"], 0.0), u["t_ref"]))
    latency = fill_latency(desk_db)
    written = retryable = censored = 0
    attempts = 0
    for u in units:
        if attempts >= a.max:                                # EVERY attempt (write, retry, failure) spends the CPU batch budget
            break
        attempts += 1
        db.execute("INSERT INTO lessons_attempts VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET last_at=excluded.last_at, n=n+1", (u["key"], now))
        db.commit()
        try:
            facts, tags = compute(u, desk_dir, latency, now, clock)
        except Retry:
            retryable += 1
            continue
        except Exception:
            logging.getLogger("lessons").exception("lessons unit failed key=%s", u["key"])
            retryable += 1
            continue
        if u["kind"] == "realized" and facts.get("exit_regret_R") is not None:
            # the band uses only what was STORED when this row was computed (labels available then), never a later backfill
            ident = (u.get("cohort"), u.get("prompt_version"))
            prior = []
            for (fj,) in db.execute("SELECT facts_json FROM lessons WHERE kind='realized' AND feature_version=? ORDER BY computed_at", (L.FEATURE_VERSION,)):
                pf = json.loads(fj)
                pid = pf.get("identity") or {}
                if (pid.get("cohort_id"), pid.get("prompt_version")) == ident:      # the band is the cohort's OWN history, never another cohort's
                    prior.append(pf.get("exit_regret_R"))
            band = L.good_exit_band(prior)
            facts["good_exit_band"] = {"median_regret_R": band[0], "n": band[1]}
            tags = L.unit_tags(tags, facts["exit_regret_R"], band)
        if facts.get("censored"):
            censored += 1
        L.store(db, u["key"], u["kind"], u["mint"], u.get("position_id"), u.get("ticket_id"), now, facts.get("t0"), facts, tags)
        written += 1
    queued = max(0, len(units) - attempts)
    db.execute("INSERT INTO lessons_run VALUES (?,?,?,?,?,?,?,NULL)", (now, queued, written, retryable, censored, waiting, clock.offset_s))
    db.commit()
    print(json.dumps({"at": now, "queued": queued, "written": written, "retryable": retryable, "censored": censored, "waiting": waiting,
                      "offset_s": clock.offset_s}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
