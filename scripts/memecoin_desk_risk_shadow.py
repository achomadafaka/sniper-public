#!/usr/bin/env python3
"""Desk RISK seat, Phase 0 SHADOW worker (sniper/services/memecoin_desk_risk.py; design §9).

Every second it reads the open desk positions from the funded ledger READ-ONLY, finds the boundaries
(fill, rail hit, structure break, timecap - N), and asks the seat once per boundary. Every boundary
gets a row in its own DB, data/desk/risk_shadow.db, including HOLDs, cooldown skips, call errors and
refusals, so the read has its denominator. NOTHING IS EVER APPLIED: no override, no exit intent, no
ledger or desk-ticket write. It is not on the fill path or in the exit sweep.

Model: Haiku 5.5 through the JUDGE's Agent SDK adapter (subscription, no API key), 5 s.
MEMECOIN_DESK_RISK_SHADOW_ENABLED=0 stops it. The structure break is risk-break-v1 (N = 3, Gamma's
pre-registration docs/audits/2026-10-21-desk-risk-shadow-preregistration.md).

--exit-observer instead logs frozen exit-observer-v1 curve candidates to exit_observer.db. This mode
does not import the model adapter; absent cohort policy disables sampling. Its separate kill knob is
MEMECOIN_DESK_EXIT_OBSERVER_ENABLED=0. Native SOL modeled proceeds stay separate from retained USDC quotes.

    python3 scripts/memecoin_desk_risk_shadow.py --once | --loop
    # cron (operator): * * * * * cd /home/bigpapi/Desktop/Sniper/.runtime/memecoin/current && SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper /home/bigpapi/.pyenv/versions/3.13.0/bin/python3 scripts/memecoin_desk_risk_shadow.py --loop >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_risk_shadow.log 2>&1
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_risk as R      # noqa: E402
from sniper.util import clock                            # noqa: E402

DDL = ("CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, position_id INTEGER NOT NULL, "
       "mint TEXT, boundary TEXT NOT NULL, boundary_key TEXT NOT NULL, known_at REAL NOT NULL, "
       "decided_at REAL, risk_version TEXT NOT NULL, structure_rule_version TEXT NOT NULL, model TEXT, "
       "inputs_hash TEXT, inputs_json TEXT, action TEXT, rails_json TEXT, reason TEXT, reason_code INTEGER, "
       "call_error TEXT, latency_ms REAL, expired INTEGER, session_id TEXT, result_id TEXT, model_echo TEXT, "
       "UNIQUE(position_id, boundary, boundary_key))")


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()


def local_sol_usd(desk_dir, *, now):
    """Existing local rate only: unknown if its heartbeat or original price age is unproven."""
    try:
        beat = json.loads((Path(desk_dir) / "universe.heartbeat").read_text())
        ts, rate, age = beat.get("ts"), beat.get("sol_usd"), beat.get("sol_age_s")
        if (beat.get("connected") is not True
                or any(not R._finite_number(v) for v in (ts, rate, age))
                or not 0 <= now - ts <= 5.0 or not 0 < rate < 1e9
                or age < 0 or age + now - ts > 120.0):
            return None
        return float(rate)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def exact_mark(conn, position_id):
    """The position's newest EXACT whole-bag exit quote, shaped as a mark: {sweep_ts, executable_exit_value
    (= USD the whole bag sells for), source}, or None.

    WHY NOT memecoin_position_marks (desk-risk-live-v3 bug, 2026-10-23): its executable_exit_value is an alias of
    `mid`, a per-token PRICE (~3e-5 USD on the desk's curve coins), so value / entry_notional read -99.99% on
    every desk mark, and it was NULL until the first fastlane mark landed. memecoin_route_series seam
    'exit_decision' is the exit sweep's exact route quote of the WHOLE bag in USDC (fees and impact included),
    written about once a second while the position is open. Its stored open_qty and unit_price are carried
    unchanged for residual-unit checks; both are None when either legacy column is absent."""
    tail = (" FROM memecoin_route_series WHERE position_id=? AND "
            "seam='exit_decision' ORDER BY observed_at DESC LIMIT 1")
    try:
        row = conn.execute("SELECT observed_at, expected_usdc, open_qty, unit_price" + tail, (position_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if str(exc) not in ("no such column: open_qty", "no such column: unit_price"):
            raise
        row = conn.execute("SELECT observed_at, expected_usdc, NULL, NULL" + tail, (position_id,)).fetchone()
    if row is None:
        return None
    return {"sweep_ts": row[0], "executable_exit_value": row[1], "source": "route_series:exit_decision",
            "open_qty": row[2], "unit_price": row[3]}


def open_desk_positions(ledger_db, *, observer=False, observer_after_id=None):
    from sniper.persistence import db_memecoin as m
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services.memecoin_funded_policy import effective_funded_policy
    conn = get_readonly_connection(str(ledger_db))
    try:
        if observer:
            conn.execute("BEGIN")  # bind fills, quote and frozen cohort to one short RO snapshot
        census, cursor, head = [], observer_after_id, None
        if observer and observer_after_id is not None:
            head = conn.execute("SELECT COALESCE(MAX(id),0) FROM memecoin_positions").fetchone()[0]
            if observer_after_id < 0:
                cursor = head
            else:
                batch = conn.execute("SELECT id,mint,network_id,opened_at,is_open,signal_source,execution_mode,"
                                     "desk_cohort_id,funded_contract_json FROM memecoin_positions "
                                     "WHERE id>? ORDER BY id LIMIT 500", (observer_after_id,)).fetchall()
                for row in batch:
                    cursor = row["id"]
                    if row["signal_source"] == "desk" and row["execution_mode"] == "real" and not row["is_open"]:
                        meta = dict(row)
                        meta["_exit_observer_config"], meta["_exit_observer_reason"] = exit_observer_config(conn, meta)
                        census.append(meta)
        out = []
        for pos in conn.execute("SELECT * FROM memecoin_positions WHERE signal_source='desk' AND "
                                "execution_mode='real' AND is_open=1").fetchall():
            pos = dict(pos)
            pos["open_qty"] = m.open_qty(conn, pos["id"])
            pos["open_qty_known"] = not m.exit_qty_unknown(conn, pos["id"])
            frozen, effective = (None, None) if observer else effective_funded_policy(
                pos, override=m.effective_exit_override(conn, pos["id"]))
            rail_ids = [r[0] for r in conn.execute("SELECT id FROM memecoin_fills WHERE position_id=? AND "
                                                   "kind IN ('derisk','partial_exit')", (pos["id"],))]
            if observer:
                pos["_exit_observer_config"], pos["_exit_observer_reason"] = exit_observer_config(conn, pos)
                try:
                    pos["_exit_observer_own_signatures"] = m.funded_fill_signatures(conn, pos["mint"])
                    if conn.execute("SELECT 1 FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
                                    "WHERE p.mint=? AND p.network_id=? AND p.execution_mode='real' "
                                    "AND (f.tx_sig IS NULL OR typeof(f.tx_sig)<>'text' OR trim(f.tx_sig)='') LIMIT 1",
                                    (pos["mint"], pos["network_id"])).fetchone():
                        pos["_exit_observer_own_signatures"] = None
                except (sqlite3.Error, TypeError, ValueError):
                    pos["_exit_observer_own_signatures"] = None
                try:
                    marks = ",".join("?" for _ in m.EXIT_KINDS)
                    partials = conn.execute("SELECT created_at FROM memecoin_fills WHERE position_id=? AND "
                                            "kind IN (" + marks + ")", (pos["id"], *m.EXIT_KINDS)).fetchall()
                    stamps = [datetime.fromisoformat(row[0]) for row in partials]
                    if any(t.utcoffset() is None for t in stamps):
                        raise ValueError("partial_clock_unknown")
                    pos["_exit_observer_partial_at"] = max(stamps).isoformat() if stamps else None
                except (sqlite3.Error, TypeError, ValueError):
                    pos["_exit_observer_partial_at"] = "unproven"
                quote = m.latest_exit_decision_quote(conn, pos["id"])
            else:
                quote = exact_mark(conn, pos["id"])
            out.append((pos, effective if frozen is not None else None, rail_ids, quote))
        return (out, census, cursor, head) if observer_after_id is not None else out
    finally:
        conn.close()


def exit_observer_config(conn, pos):
    """Only the position's immutable, hash-verified funded cohort can enroll it."""
    from sniper.persistence import db_memecoin as m
    try:
        meta = m._desk_contract_metadata(pos.get("funded_contract_json"))
        if not meta or meta["cohort_id"] != pos.get("desk_cohort_id"):
            return None, "cohort_identity"
        row = conn.execute("SELECT producer,policy_json,policy_hash FROM memecoin_desk_cohorts "
                           "WHERE cohort_id=?", (meta["cohort_id"],)).fetchone()
        if (row is None or row[0] != meta["producer"] or not isinstance(row[1], str)
                or hashlib.sha256(row[1].encode()).hexdigest() != row[2]):
            return None, "cohort_integrity"
        envelope = json.loads(row[1])
        if envelope["producer"] != meta["producer"]:
            return None, "cohort_identity"
        config = envelope["policy"].get("exit_observer")
        if config is None:
            return None, "cohort_off"
        if (not isinstance(config, dict) or set(config) != {"version", "grid", "baseline_drop_pct"}
                or config["version"] != "exit-observer-v1" or config["grid"] != [2, 3, 4, 6]
                or any(type(k) is not int for k in config["grid"])
                or type(config["baseline_drop_pct"]) not in (int, float)
                or config["baseline_drop_pct"] != 10.0):
            return None, "observer_config_unsupported"
        return config, None
    except (sqlite3.Error, TypeError, ValueError, KeyError, AttributeError):
        return None, "cohort_unreadable"


OBSERVER_DDL = (
    "CREATE TABLE IF NOT EXISTS exit_observer_meta(id INTEGER PRIMARY KEY CHECK(id=1), started_at REAL NOT NULL, "
    "position_cursor INTEGER, source_head INTEGER)",
    "CREATE TABLE IF NOT EXISTS exit_observer_population(position_id INTEGER PRIMARY KEY, cohort_id TEXT, "
    "mint TEXT, opened_at TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL, enabled INTEGER NOT NULL, "
    "config_reason TEXT, enrolled INTEGER NOT NULL, first_atomic TEXT, quantity_changed INTEGER NOT NULL DEFAULT 0, "
    "closed_seen REAL, sampling_reason TEXT)",
    "CREATE TABLE IF NOT EXISTS exit_observations(position_id INTEGER NOT NULL, cohort_id TEXT, "
    "rule_version TEXT NOT NULL, evaluated_at REAL NOT NULL, observer_started_at REAL NOT NULL, cut REAL, "
    "residual_atomic TEXT, config_hash TEXT NOT NULL, facts_json TEXT NOT NULL, "
    "UNIQUE(position_id,rule_version,evaluated_at))",
)


def _observer_enrolled(opened_at, started, now):
    try:
        opened = datetime.fromisoformat(opened_at)
        return opened.utcoffset() is not None and started <= opened.timestamp() + clock.offset_s() <= now
    except (TypeError, ValueError, OverflowError):
        return False


def run_exit_observer_pass(ledger_db, desk_dir, out_db, *, now_fn=time.time, positions_fn=None,
                           snapshot_fn=None, fill_sigs_of=None, enabled=True):
    """Separate measurement DB only. No SDK, outcome lookup or engine consumer."""
    stats = {"enabled": enabled, "recorded": 0, "enabled_open": 0, "disabled_open": 0,
             "enabled_closed": 0, "disabled_closed": 0, "missed_closed": 0, "census_complete": None}
    if not enabled:
        return stats
    from sniper.services import memecoin_desk_exit_observer as O
    from sniper.services import memecoin_desk_exit_observer_tape as tape
    now = now_fn()
    if not R._finite_number(now):
        raise ValueError("observer_clock_unknown")
    # Importing the watcher supplies only local capture proof, never its model adapter.
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from memecoin_desk_risk_watcher import capture_proof
    snapshot_fn = snapshot_fn or tape.read_snapshot
    with closing(sqlite3.connect(str(out_db), timeout=5)) as out, out:
        out.row_factory = sqlite3.Row
        for ddl in OBSERVER_DDL:
            out.execute(ddl)
        meta = out.execute("SELECT started_at,position_cursor FROM exit_observer_meta WHERE id=1").fetchone()
        census, cursor, head = [], None, None
        if positions_fn is None:
            positions, census, cursor, head = open_desk_positions(
                ledger_db, observer=True, observer_after_id=meta[1] if meta and meta[1] is not None else -1)
            stats["census_complete"] = cursor >= head
        else:
            positions = positions_fn(ledger_db)
        if meta is None:
            now = now_fn()  # first ledger snapshot precedes the durable forward enrollment boundary
            out.execute("INSERT INTO exit_observer_meta VALUES(1,?,?,?)", (now, cursor, head))
            started = now
        else:
            started = meta[0]
            out.execute("UPDATE exit_observer_meta SET position_cursor=?,source_head=? WHERE id=1", (cursor, head))
        for pos in census:
            out.execute("INSERT OR IGNORE INTO exit_observer_population(position_id,cohort_id,mint,opened_at,"
                        "first_seen,last_seen,enabled,config_reason,enrolled,closed_seen,sampling_reason) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,'closed_between_samples')",
                        (pos["id"], pos.get("desk_cohort_id"), pos.get("mint"), pos.get("opened_at"), now, now,
                         int(pos.get("_exit_observer_config") is not None), pos.get("_exit_observer_reason"),
                         int(pos.get("_exit_observer_config") is not None and
                             _observer_enrolled(pos.get("opened_at"), started, now)), now))
        ids = set()
        for pos, _policy, _rail_ids, quote in positions:
            pid = pos["id"]
            ids.add(pid)
            config = pos.get("_exit_observer_config")
            on = config is not None
            stats["enabled_open" if on else "disabled_open"] += 1
            enrolled = on and _observer_enrolled(pos.get("opened_at"), started, now)
            out.execute("INSERT OR IGNORE INTO exit_observer_population(position_id,cohort_id,mint,opened_at,"
                        "first_seen,last_seen,enabled,config_reason,enrolled) VALUES(?,?,?,?,?,?,?,?,?)",
                        (pid, pos.get("desk_cohort_id"), pos.get("mint"), pos.get("opened_at"), now, now,
                         int(on), pos.get("_exit_observer_reason"), int(enrolled)))
            out.execute("UPDATE exit_observer_population SET last_seen=?,closed_seen=NULL WHERE position_id=?",
                        (now, pid))
            if not on:
                continue
            own = fill_sigs_of or (lambda _mint: pos.get("_exit_observer_own_signatures"))
            try:
                snap = snapshot_fn(pos, quote, desk_dir=desk_dir, now=now, fill_sigs_of=own, proof_fn=capture_proof)
            except Exception as exc:
                snap = dict(rows=[], cut=None, coverage_from=None, opened_at=None, residual_atomic=None,
                            own_signatures=[], own_complete=False, source_reason="reader_error:" + type(exc).__name__)
            facts = O.evaluate(snap["rows"], **{k: snap.get(k) for k in (
                "cut", "coverage_from", "opened_at", "residual_atomic", "own_signatures", "own_complete", "launch_at")})
            population = out.execute("SELECT * FROM exit_observer_population WHERE position_id=?", (pid,)).fetchone()
            atoms = facts["residual_atomic"]
            changed = bool(population["quantity_changed"] or (
                atoms is not None and population["first_atomic"] is not None and atoms != population["first_atomic"]))
            out.execute("UPDATE exit_observer_population SET first_atomic=COALESCE(first_atomic,?),quantity_changed=? "
                        "WHERE position_id=?", (atoms, int(changed), pid))
            censored = []
            identity_ok = (population["cohort_id"] == pos.get("desk_cohort_id")
                           and population["mint"] == pos.get("mint")
                           and population["opened_at"] == pos.get("opened_at") and bool(population["enabled"]) == on)
            if not identity_ok:
                censored.append("position_identity_changed")
            if not population["enrolled"]:
                censored.append("pre_observer_position")
            if not facts["peak_covered_from_fill"]:
                censored.append("peak_left_censored")
            if snap.get("source_reason"):
                censored.append(snap["source_reason"])
            current, peak = facts["value_sol"], facts["peak_value_sol"]
            latest = out.execute("SELECT cut FROM exit_observations WHERE position_id=? AND rule_version=? "
                                 "ORDER BY evaluated_at DESC LIMIT 1", (pid, O.VERSION)).fetchone()
            raw = snap["rows"]
            ordered = [r for r in raw if O._order(r) is not None]
            fees = max(ordered, key=O._order) if ordered else {}
            audit_quote, invalid_fields = {}, []
            for key, value in (snap.get("exact_quote") or {}).items():
                if value is None or type(value) in (str, int, bool) or R._finite_number(value):
                    audit_quote[key] = value
                else:
                    audit_quote[key] = None
                    invalid_fields.append(key)
            peak_usdc = pos.get("executable_peak_price")
            facts.update(value_units="SOL", enrolled=bool(population["enrolled"] and identity_ok), censored=censored,
                         census_complete=stats["census_complete"],
                         quantity_changed_since_first=changed,
                         baseline_drop_now=(current <= peak * .9 if not censored and current is not None
                                            and peak is not None and peak > 0 else None),
                         quantity_basis="retained_quote_balance_atomic", quantity_reason=snap.get("quantity_reason"),
                         source_reason=snap.get("source_reason"), venue=snap.get("venue"), clocks=snap.get("clocks"),
                         previous_cut=latest[0] if latest else None,
                         exact_quote=audit_quote or None, exact_quote_invalid_fields=invalid_fields,
                         executable_peak_price_usdc=peak_usdc if R._finite_number(peak_usdc) else None,
                         fee_components={k: fees.get(k) if type(fees.get(k)) is int and 0 <= fees[k] < 10_000 else None
                                         for k in O.FEES},
                         fee_invalid_fields=[k for k in O.FEES if fees.get(k) is not None and not (
                             type(fees[k]) is int and 0 <= fees[k] < 10_000)])
            digest = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            inserted = out.execute("INSERT OR IGNORE INTO exit_observations VALUES(?,?,?,?,?,?,?,?,?)",
                                  (pid, pos.get("desk_cohort_id"), O.VERSION, now, started, snap.get("cut"), atoms,
                                   digest, json.dumps(facts, sort_keys=True, separators=(",", ":"), allow_nan=False)))
            stats["recorded"] += inserted.rowcount
        for row in out.execute("SELECT position_id FROM exit_observer_population WHERE closed_seen IS NULL").fetchall():
            if row[0] not in ids:
                out.execute("UPDATE exit_observer_population SET closed_seen=? WHERE position_id=?", (now, row[0]))
        for row in out.execute("SELECT enabled,COUNT(*) FROM exit_observer_population WHERE closed_seen IS NOT NULL "
                               "GROUP BY enabled"):
            stats["enabled_closed" if row[0] else "disabled_closed"] = row[1]
        stats["missed_closed"] = out.execute("SELECT COUNT(*) FROM exit_observer_population "
                                           "WHERE sampling_reason='closed_between_samples'").fetchone()[0]
    return stats


def tape_bars(desk_dir, pool, now, *, n=R.STRUCTURE_PRIOR_S + 10, fact_now=None):
    """The pool's last 1 s bars from U's candles_1s day files (curve only: none after graduation)."""
    fact_now = now - clock.offset_s() if fact_now is None else fact_now
    bars = []
    # Day files follow producer host time; each candle's t follows chain UTC.
    for d in sorted({utc_day(now - 86400), utc_day(now), utc_day(fact_now - 86400), utc_day(fact_now)}):
        path = Path(desk_dir) / "candles_1s" / ("%s.db" % d)
        if not path.exists():
            continue
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            bars += [dict(r) for r in conn.execute("SELECT t, o, h, l, c, buy_usd, sell_usd, n_makers FROM candles_1s "
                                                   "WHERE pool=? AND t <= ? ORDER BY t DESC LIMIT ?", (pool, fact_now, n))]
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return sorted(bars, key=lambda b: b["t"])[-n:]


def run_pass(ledger_db, desk_dir, out_db, *, call_model, now_fn=time.time, structure_n=R.STRUCTURE_N, bounds=None):
    out = sqlite3.connect(str(out_db), timeout=30)
    out.execute("PRAGMA journal_mode=WAL")
    out.execute(DDL)
    if "model_echo" not in {r[1] for r in out.execute("PRAGMA table_info(decisions)")}:
        out.execute("ALTER TABLE decisions ADD COLUMN model_echo TEXT")   # the raw echo, stored as given
    stats = {"positions": 0, "boundaries": 0, "asked": 0, "cooldown": 0, "no_policy": 0}
    offset = clock.offset_s()
    try:
        for pos, policy, rail_ids, mark in open_desk_positions(ledger_db):
            stats["positions"] += 1
            now = now_fn()
            fact_now = now - offset
            seen = {(r[0], r[1]) for r in out.execute(
                "SELECT boundary, boundary_key FROM decisions WHERE position_id=?", (pos["id"],))}
            last_call = out.execute("SELECT MAX(decided_at) FROM decisions WHERE position_id=? AND call_error IS NOT "
                                    "'cooldown'", (pos["id"],)).fetchone()[0]
            if policy is None:
                if ("fill", "no_policy") not in seen:
                    _record(out, pos, "fill", "no_policy", now, call_error="no_frozen_policy")
                stats["no_policy"] += 1
                continue
            from sniper.services.memecoin_desk_universe import bonding_curve
            bars = tape_bars(desk_dir, pos.get("executed_pool") or bonding_curve(pos["mint"]), now,
                             fact_now=fact_now)
            sol_usd = local_sol_usd(desk_dir, now=now)
            for kind, key in R.boundaries(pos, policy, now=fact_now, seen=seen, rail_fill_ids=rail_ids, bars=bars,
                                          structure_n=structure_n):
                stats["boundaries"] += 1
                f = R.facts(pos, policy, mark=mark, bars=bars, boundary=kind, now=fact_now,
                            sol_usd=sol_usd)
                if last_call is not None and now - last_call < R.COOLDOWN_S:
                    _record(out, pos, kind, key, now, facts=f, call_error="cooldown")   # denominator kept
                    stats["cooldown"] += 1
                    continue
                result = call_model(R.SYSTEM_PROMPT, json.dumps(f, sort_keys=True, separators=(",", ":")))
                decided = now_fn()
                error = result.get("call_error") or (
                    "model_result_error" if result.get("is_error") or not result.get("text") else None)
                if error is None and not R.echo_ok(result.get("model_echo")):
                    error = "model_mismatch"
                action, rails, reason, code = (("HOLD", None, "call_failed", None) if error else
                                               R.validate(result["text"], key=f["k"], policy=policy, bounds=bounds))
                _record(out, pos, kind, key, now, facts=f, decided=decided, action=action, rails=rails,
                        reason=reason, code=code, call_error=error, result=result)
                last_call = decided
                stats["asked"] += 1
    finally:
        out.close()
    return stats


def _record(out, pos, kind, key, known_at, *, facts=None, decided=None, action=None, rails=None, reason=None,
            code=None, call_error=None, result=None):
    result = result or {}
    with out:
        out.execute("INSERT OR IGNORE INTO decisions(position_id, mint, boundary, boundary_key, known_at, decided_at, "
                    "risk_version, structure_rule_version, model, inputs_hash, inputs_json, action, rails_json, "
                    "reason, reason_code, call_error, latency_ms, expired, session_id, result_id, model_echo) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pos["id"], pos["mint"], kind, key, known_at, decided if decided is not None else known_at,
                     R.RISK_VERSION, R.STRUCTURE_RULE_VERSION, R.MODEL if result else None,
                     R.inputs_hash(facts) if facts else None, json.dumps(facts, sort_keys=True) if facts else None,
                     action, json.dumps(rails, sort_keys=True) if rails else None, reason, code, call_error,
                     round((decided - known_at) * 1000, 1) if decided is not None else None,
                     int(decided - known_at > R.TTL_S) if decided is not None else None,
                     result.get("session_id"), result.get("result_id"), result.get("model_echo")))


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root, state_path
    load_env_files(str(state_root()))
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):     # the subscription seat, never a key
        os.environ.pop(key, None)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    p.add_argument("--ledger-db", default=os.getenv("DB_PATH") or str(state_path("sniper_tracker.db")))
    p.add_argument("--exit-observer", action="store_true", help="Log frozen native-curve candidates without a model call")
    a = p.parse_args(argv)
    kill = "MEMECOIN_DESK_EXIT_OBSERVER_ENABLED" if a.exit_observer else "MEMECOIN_DESK_RISK_SHADOW_ENABLED"
    if os.getenv(kill, "1").strip() == "0":
        print("[desk-risk-shadow] %s=0; exiting" % kill)
        return 0
    if not Path(a.ledger_db).is_file():
        print("[desk-risk-shadow] refused: ledger DB missing")
        return 2
    desk_dir = Path(state_root()) / "data" / "desk"
    desk_dir.mkdir(parents=True, exist_ok=True)
    lock = open(desk_dir / ("exit_observer.lock" if a.exit_observer else "risk_shadow.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-risk-shadow] another shadow worker holds the lock; exiting")
        return 0
    if a.exit_observer:
        while os.getenv(kill, "1").strip() != "0":
            stats = run_exit_observer_pass(a.ledger_db, desk_dir, desk_dir / "exit_observer.db")
            print(json.dumps(stats), flush=True)
            if a.once:
                return 0
            time.sleep(1.0)
        return 0
    from sniper.settings.memecoin import parse_desk_rail_bounds
    bounds = parse_desk_rail_bounds(os.getenv("MEMECOIN_DESK_RAIL_BOUNDS", ""))
    sys.path.insert(0, str(ROOT / "scripts"))
    from memecoin_desk_judge_worker import sdk_call_model

    def call_model(system, user):
        return sdk_call_model(system, user, deadline_s=R.DEADLINE_S, model=R.MODEL, persist_session=False)
    while True:
        stats = run_pass(a.ledger_db, desk_dir, desk_dir / "risk_shadow.db", call_model=call_model,
                         bounds=bounds)
        if stats["asked"] or stats["cooldown"]:
            print(json.dumps(stats), flush=True)
        if a.once:
            return 0
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
