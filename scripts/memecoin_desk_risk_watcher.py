#!/usr/bin/env python3
"""Desk RISK seat, LIVE watcher (sniper/services/memecoin_desk_risk_live.py; design Lambda 2026-10-22).

One process, one flock. Every CYCLE_S seconds while at least one desk position is open (earlier, never
faster than EVENT_MIN_GAP_S, when a position shows a new structure break) it sends ONE call covering every
open position: the 1 s tape bars since entry, the exact-quote PnL (or null), the rails and the time held.
The model answers HOLD / TIGHTEN / LOOSEN_TRAIL / EXIT per position; the answer is validated (tighten-only except a trail-only,
capped LOOSEN_TRAIL, bounds, a
tighten that already fires must be an EXIT) and written to data/desk/risk_live.db. This process NEVER
touches the funded ledger (read-only handle), the desk tickets or an exit: the engine (exit sweep, Beta)
reads risk_decisions and applies live rows that are unexpired, stricter than the position's current
rails, and not yet applied. Deterministic rails run first there and always win.

Every position gets a row every cycle, including HOLDs and call errors, so the read has its denominator.
A failed, late, malformed or over-budget answer is a HOLD with the cause recorded; six failed cycles in a
row with positions open page the operator (the rails keep running).

Model: Haiku through the JUDGE's Agent SDK adapter (subscription, no API key). Knobs:
  MEMECOIN_DESK_RISK_WATCHER_ENABLED (default 1; 0 exits)   MEMECOIN_DESK_RISK_LIVE (default 1; 0 = rows
  are written with live=0 and the engine ignores them: shadow)   MEMECOIN_DESK_RISK_CALLS_PER_HOUR (300)
    python3 scripts/memecoin_desk_risk_watcher.py --once | --loop
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import logging
import os
import sqlite3
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sniper.services import memecoin_desk_risk as R           # noqa: E402
from sniper.services import memecoin_desk_risk_live as L      # noqa: E402
from sniper.services import memecoin_desk_risk_gate as G      # noqa: E402
from sniper.util import clock                                # noqa: E402

log = logging.getLogger("desk_risk_watcher")

DDL = (
    "CREATE TABLE IF NOT EXISTS risk_calls(id INTEGER PRIMARY KEY, cycle_at REAL NOT NULL, "
    "call_started_at REAL NOT NULL, response_at REAL, latency_ms REAL, n_positions INTEGER NOT NULL, "
    "prompt_hash TEXT NOT NULL, user_z BLOB, raw_text TEXT, model TEXT, model_echo TEXT, session_id TEXT, "
    "result_id TEXT, usage_json TEXT, notional_usd REAL, call_error TEXT, live INTEGER NOT NULL, "
    "risk_version TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS risk_decisions(id INTEGER PRIMARY KEY, cycle_at REAL NOT NULL, "
    "call_id INTEGER, position_id INTEGER NOT NULL, mint TEXT, key TEXT NOT NULL, inputs_hash TEXT, "
    "pnl_pct_exact REAL, tape_state TEXT, action TEXT NOT NULL, rails_json TEXT, reason TEXT, "
    "reason_code INTEGER, call_error TEXT, decided_at REAL, expires_at REAL, live INTEGER NOT NULL, "
    "risk_version TEXT NOT NULL, UNIQUE(cycle_at, position_id))",
    "CREATE INDEX IF NOT EXISTS risk_decisions_pos ON risk_decisions(position_id, decided_at)",
    "CREATE INDEX IF NOT EXISTS risk_calls_response ON risk_calls(response_at)",
    "CREATE TABLE IF NOT EXISTS risk_attention_events(position_id INTEGER NOT NULL, signal TEXT NOT NULL, "
    "detected_at REAL NOT NULL, done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(position_id,signal,detected_at))",
)
# v4 columns (Gamma 2026-10-23), added in place: pnl_basis says why pnl_pct_exact is null; cadence_s is the cycle
# period in force; attention_signal is the code that woke the cycle (NULL = the timer); risk_version_mixed = 1
# for a position opened before this watcher process started (it may have been watched under an older version).
V4_COLUMNS = (("pnl_basis", "TEXT"), ("cadence_s", "REAL"), ("attention_signal", "TEXT"),
              ("risk_version_mixed", "INTEGER"), ("gate_json", "TEXT"))
FAIL_BACKOFF = ("auth_expired", "rate_limited")
BACKOFF_S = 60.0
PROBE_S = 3.0                  # one candles read per open position per probe (Gamma LOW 2026-10-22)


def bars_since(desk_dir, pool, opened_ts, now, mint=None, *, fact_now=None):
    """The pool's 1 s bars from one second before the fill to now. With `mint`, a coin that graduated continues on its
    canonical PumpSwap pool (memecoin_desk_ps_tape, 2026-10-23): bars after the last curve bar, same unit (SOL per token).
    Without a fresh heartbeat SOL/USD the PumpSwap part is left out (flow unknown, never 0)."""
    from memecoin_desk_risk_shadow import utc_day
    fact_now = now - clock.offset_s() if fact_now is None else fact_now
    offset = now - fact_now
    bars = []
    # Files use producer host days; bounds use the chain-UTC candle timestamp.
    for d in sorted({utc_day(opened_ts), utc_day(fact_now), utc_day(opened_ts + offset), utc_day(now)}):
        path = Path(desk_dir) / "candles_1s" / ("%s.db" % d)
        if not path.exists():
            continue
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            bars += [dict(r, venue="curve") for r in conn.execute(
                "SELECT t, o, h, l, c, buy_usd, sell_usd, n_makers FROM candles_1s WHERE pool=? AND t >= ? AND t <= ? "
                "ORDER BY t", (pool, int(opened_ts) - 1, int(fact_now)))]
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    bars = sorted(bars, key=lambda b: b["t"])
    if mint:
        from sniper.services import memecoin_desk_ps_tape as PT
        from sniper.services.memecoin_desk_structure_tape import _heartbeat
        sol, why = _heartbeat(desk_dir, now)
        if why is None:
            try:
                last = bars[-1]["t"] if bars else None
                ps = PT.bars_1s(PT.load_ps_swaps(desk_dir, mint, int(opened_ts) - 1 + offset, now, sol,
                                               with_gaps=True)[0])
                bars += [dict(b, venue="ps") for b in ps if last is None or b["t"] > last]
            except Exception:                    # a PumpSwap read problem leaves the curve bars as they were
                log.exception("pumpswap bars read failed mint=%s", mint)
    return [b for b in bars if int(opened_ts) - 1 <= b["t"] <= fact_now]


def _knob_flag(name):
    return os.getenv(name, "1").strip() != "0"


def _knob_k():
    try:
        v = float(os.getenv("MEMECOIN_DESK_RISK_SB_K", str(R.SB_K)))
    except ValueError:
        return R.SB_K
    return v if v > 0 and math.isfinite(v) else R.SB_K                              # a bad k keeps the default, never 0 / nan / negative


SB_CLOSES = _knob_flag("MEMECOIN_DESK_RISK_SB_CLOSES")      # kill knob: 0 = the v1 rule (1 s closes, post-fill bars)
REGIME = _knob_flag("MEMECOIN_DESK_RISK_REGIME")            # kill knob: 0 = no rg block in the prompt
SB_K = _knob_k()                                            # provisional, unvalidated; range5 has no death-study calibration


def refresh_knobs():
    """Re-resolve the three knobs AFTER main() has loaded the state .env (the import-time values came from the shell, and a watchdog launch
    exports only the state root). Called ONCE at startup, before any thread runs: never from the money path."""
    global SB_CLOSES, REGIME, SB_K
    SB_CLOSES, REGIME, SB_K = _knob_flag("MEMECOIN_DESK_RISK_SB_CLOSES"), _knob_flag("MEMECOIN_DESK_RISK_REGIME"), _knob_k()
SB_PRE_S = 420.0                                                                  # pre-fill history the break test reads (60 s support + 300 s noise)


def pre_fill_bars(desk_dir, mint, opened_ts, offset, sol_usd=None):
    """1 s bars of the coin's CURVE swaps from the tape in the SB_PRE_S before the fill (strictly before it), same unit as the candle bars
    (SOL per token) and the chain-UTC second (host receipt time minus the learned offset): the history risk-break-v2 needs for its support
    and its noise. [] when the tape or the coin has none (the rule then answers unknown, never a break)."""
    from memecoin_desk_risk_shadow import utc_day
    host_open = opened_ts + offset
    rows = []
    for day in sorted({utc_day(host_open - SB_PRE_S), utc_day(host_open)}):
        path = Path(desk_dir) / "swaps" / ("%s.db" % day)
        if not path.exists():
            continue
        conn = None
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5, isolation_level=None)
            conn.row_factory = sqlite3.Row
            rows += [dict(r) for r in conn.execute(
                "SELECT received_at, virtual_quote, virtual_token, is_buy, quote_atomic FROM swaps WHERE mint=? AND received_at>=? AND received_at<? "
                "ORDER BY received_at", (mint, host_open - SB_PRE_S, host_open - 1.0))]
        except sqlite3.Error:
            log.warning("pre-fill bars unreadable day=%s mint=%s", day, mint)
        finally:
            if conn is not None:
                conn.close()
    out = {}
    for r in rows:
        if not r["virtual_quote"] or not r["virtual_token"]:
            continue
        px = (r["virtual_quote"] / 1e9) / (r["virtual_token"] / 1e6)
        t = int(r["received_at"] - offset)
        b = out.setdefault(t, {"t": t, "o": px, "h": px, "l": px, "c": px, "buy_usd": 0.0, "sell_usd": 0.0, "n_makers": 0})
        b["h"], b["l"], b["c"] = max(b["h"], px), min(b["l"], px), px
        usd = (r["quote_atomic"] or 0) / 1e9 * sol_usd if sol_usd else 0.0     # USD like the candle bars; unknown SOL/USD = 0 (unknown flow)
        b["buy_usd" if r["is_buy"] else "sell_usd"] += usd
    return [out[t] for t in sorted(out)]


def regime_of(pos, desk_dir, now):
    """The coin's regime block as of host time `now` (the swaps tape's clock), or None when the knob is off. Any failure is an unknown block."""
    if not REGIME:
        return None
    from sniper.services import memecoin_desk_regime as RG
    if not capture_healthy(desk_dir, now):
        return {"version": RG.VERSION, "regime": "unknown", "reason": "capture_unproven"}
    try:
        return RG.regime_for(pos["mint"], desk_dir=desk_dir, as_of=now)
    except Exception:
        log.exception("regime read failed pos=%s", pos.get("id"))
        return {"version": RG.VERSION, "regime": "unknown", "reason": "regime_error"}


from sniper.services.memecoin_desk_capture import COMMIT_SLACK_S, capture_proof   # noqa: E402,F401  one source (paper engine too)


def capture_cut(desk_dir, now, venues=("curve",), window_s=300.0):
    proof = capture_proof(desk_dir, now, venues, window_s)
    return proof["through"] if proof else None


def capture_healthy(desk_dir, now, venues=("curve",), window_s=300.0):
    return capture_cut(desk_dir, now, venues, window_s) is not None


def evidence_of(pos, *, desk_dir, now, sol_usd, fill_sigs_of, sb):
    from sniper.services.memecoin_desk_risk_tape import evidence_of as read
    return read(pos, desk_dir=desk_dir, now=now, sol_usd=sol_usd, fill_sigs_of=fill_sigs_of, sb=sb,
                proof_fn=capture_proof)


def rail_proximity(block, pos, params):
    """Known exact executable distance to each rail; missing quotes/noise/peak remain unknown."""
    unit, entry, n5 = block.get("exact_exit_unit_price"), pos.get("entry_price"), (block.get("ev") or {}).get("n5")
    rails = block.get("rails") or {}
    out = {"stop": None, "tp": None, "trail": None}
    if not all(R._finite_number(x) and x > 0 for x in (unit, entry, n5)):
        return out
    margin = params["rail_near_n5"] * n5
    if not math.isfinite(margin):
        return out
    for name, key, direction in (("stop", "sl_pct", -1), ("tp", "tp_pct", 1)):
        rail = rails.get(key)
        if R._finite_number(rail) and rail > 0:
            level = entry * (1 + direction * rail / 100)
            if R._finite_number(level) and level > 0:
                out[name] = abs(G._log_ratio(unit, level)) <= margin
    # The persisted executable unit peak and actual fill share USD/token units, including after partial exits.
    peak, trail, arm = pos.get("executable_peak_price"), rails.get("trail_pct"), block.get("trail_arm_pct")
    if (all(R._finite_number(x) and x > 0 for x in (peak, trail)) and trail < 100
            and R._finite_number(arm) and arm >= 0 and peak >= entry * (1 + arm / 100)):
        level = peak * (1 - trail / 100)
        if R._finite_number(level) and level > 0:
            out["trail"] = abs(G._log_ratio(unit, level)) <= margin
    return out


def _gate_block(block, pos, policy, mark, *, desk_dir, now, sol_usd, fill_sigs_of, sb, params, bars):
    block["ev"] = evidence_of(pos, desk_dir=desk_dir, now=now, sol_usd=sol_usd, fill_sigs_of=fill_sigs_of, sb=sb)
    ev = block["ev"]
    start = ev.get("coverage_from")
    # A mint-local loss invalidates pre-loss support even while the global source remains healthy.
    confirmed = (bool(sb) and ev.get("fresh") is True and R._finite_number(start)
                 and structure_break_of(pos, bars, desk_dir, now, clock.offset_s(), coverage_from=start) is not None)
    block["sb"] = ev["sb"] = int(confirmed)
    block["trail_arm_pct"] = policy.get("trail_arm_pct")
    block["exact_exit_unit_price"] = None
    qty, quoted, unit, value = (pos.get("open_qty"), (mark or {}).get("open_qty"),
                                (mark or {}).get("unit_price"), (mark or {}).get("executable_exit_value"))
    if (pos.get("open_qty_known") is True and R.pnl_basis(pos, mark, now=now - clock.offset_s()) == "exact"
            and all(R._finite_number(x) and x > 0 for x in (qty, quoted, unit, value))
            and math.isclose(qty, quoted, rel_tol=1e-9, abs_tol=0)
            and math.isclose(unit, value / qty, rel_tol=1e-9, abs_tol=0)):
        block["exact_exit_unit_price"] = unit
    block["rail_proximity"] = rail_proximity(block, pos, params)
    return block


def _reference(block):
    ev = block.get("ev") or {}
    price = ev.get("last_price")
    if ev.get("fresh") is True and R._finite_number(price) and price > 0:
        return {"ev": dict(ev), "rail_proximity": dict(block["rail_proximity"])}
    return None


def gate_settings():
    flag = os.getenv("MEMECOIN_DESK_RISK_EVIDENCE_GATES", "0").strip()
    if flag not in ("0", "1"):
        raise ValueError("MEMECOIN_DESK_RISK_EVIDENCE_GATES must be 0 or 1")
    return G.settings(os.environ) if flag == "1" else None


def structure_break_of(pos, bars, desk_dir, now, offset, *, coverage_from=None):
    """The pos's break streak key under the rule in force: risk-break-v2 (pre-fill history + 5 s closes + noise size test), or the v1 rule
    on the post-fill bars when the kill knob is 0. None = no break (including: unknown)."""
    if coverage_from is not None:
        first = math.ceil((coverage_from - offset) / R.SB_BUCKET_S) * R.SB_BUCKET_S
        bars = [b for b in bars if b["t"] >= first]
    if not SB_CLOSES:
        return R.structure_break(bars) if bars else None
    try:
        from memecoin_desk_risk_shadow import local_sol_usd
        sol = local_sol_usd(desk_dir, now=now)
    except Exception:                                    # no SOL/USD: pre-fill flow unknown (0), prices unaffected
        sol = None
    pre = pre_fill_bars(desk_dir, pos["mint"], R._ts(pos["opened_at"]), offset, sol)
    opened = R._ts(pos["opened_at"])
    if not any(b["t"] > opened for b in bars):               # no post-fill bar yet: a pre-fill run is history, not a break of THIS position
        return None
    on_ps = any(b.get("venue") == "ps" for b in bars)
    if on_ps:
        # Prices on the two venues are never compared (the migration step is a venue jump, not a structure break): after graduation the rule
        # reads the PumpSwap bars ONLY, with no curve pre-history, and stays unknown until they alone give the support and noise it needs.
        hist = [b for b in bars if b.get("venue") == "ps"]
    else:
        hist = pre + [b for b in bars if not pre or b["t"] > pre[-1]["t"]]
    proof = capture_proof(desk_dir, now, ("ps",) if on_ps else ("curve",))
    if proof is None:
        return None
    cut = proof["through"]
    start = max(proof["from"], coverage_from) if coverage_from is not None else proof["from"]
    first = math.ceil((start - offset) / R.SB_BUCKET_S) * R.SB_BUCKET_S
    hist = [b for b in hist if first <= b["t"] <= cut - offset]
    # sb_closes subtracts its write lag. Never let that clock close a bucket beyond the actual committed cut.
    as_of = min(now - R.SB_LATE_S, cut) + R.SB_LATE_S - offset
    return R.sb_closes(hist, k=SB_K, after=opened, as_of=as_of) if hist else None


def structure_of(pos, *, desk_dir, now, bounds=None, fill_sigs_of=None):
    """The position's market structure at `now` from Lambda's adapter (structure_for, pure local reads), or
    an explicit unknown: never a calm-looking empty block, never an exception out of the cycle.
    fill_sigs_of(mint) -> OUR funded fill signatures (ledger, read-only). An open position always has its entry
    fill, so a missing reader, an error or no signatures is blind 'ours_unknown': never a chart with our own
    swaps counted as demand or supply (same rule as the JUDGE path)."""
    unknown = lambda reason: {"version": "structure-v1", "tape": "unknown", "reason": reason}
    try:
        from sniper.services.memecoin_desk_structure_tape import structure_for
    except ImportError:
        return unknown("adapter_missing")
    try:
        sigs = tuple(fill_sigs_of(pos["mint"])) if fill_sigs_of is not None else ()
    except Exception:
        log.exception("fill signatures read failed pos=%s", pos.get("id"))
        sigs = ()
    if not sigs:
        return unknown("ours_unknown")
    try:
        out = structure_for(pos["mint"], desk_dir=desk_dir, as_of=now, bounds=bounds, fill_sigs=sigs)
        out.pop("meta", None)
        return out
    except Exception:
        log.exception("structure read failed pos=%s", pos.get("id"))
        return unknown("unreadable")


def ledger_fill_sigs(ledger_db):
    """fill_sigs_of for structure_of: one read-only ledger connection per call (never a writable handle)."""
    def fn(mint):
        from sniper.persistence.db_core import get_readonly_connection
        from sniper.persistence.db_memecoin import funded_fill_signatures
        conn = get_readonly_connection(str(ledger_db))
        try:
            return funded_fill_signatures(conn, mint)
        finally:
            conn.close()
    return fn


ATTN_WINDOW_S = 60.0           # fired attention events shown to the model, newest first


def fired_attention(position_id, now, *, paths=None):
    """[(signal, detected_at)] fired engine attention events (tape.db + runner.db) for one position in the last
    ATTN_WINDOW_S, oldest first. Read-only; an absent or unreadable file is no events (logged by the reader).
    DRY rows (thresholds unset, fired NULL) and 'unknown' rows never count: nothing fires before Gamma freezes
    the thresholds, so v5 wakes nothing until then."""
    from sniper.services import memecoin_desk_attention as att
    from sniper.services import memecoin_desk_risk_apply as A
    paths = paths if paths is not None else (A.tape_db_path(), att.runner_db_path())
    rows = []
    for path in paths:
        rows += [(r["signal"], A.detected_at(r)) for r in A._fired_rows(path, position_id, now - ATTN_WINDOW_S)]
    return sorted(rows, key=lambda x: x[1])


def connect(out_db):
    out = sqlite3.connect(str(out_db), timeout=30)
    out.execute("PRAGMA journal_mode=WAL")
    for stmt in DDL:
        out.execute(stmt)
    have = {r[1] for r in out.execute("PRAGMA table_info(risk_decisions)")}
    for name, kind in V4_COLUMNS:
        if name not in have:
            out.execute("ALTER TABLE risk_decisions ADD COLUMN %s %s" % (name, kind))
    return out


def pending_attention(out_db, open_ids, fresh_by_id, now):
    """Durable wakes, not fresh facts: preserve event identities until one current-evidence call acknowledges them.

    Returns ({position_id: [(signal, original_detected_at), ...]}, response-based failure backoff). This own connection
    commits before returning; call before opening the decision transaction. Closed positions and expired DONE receipts are pruned.
    """
    if not R._finite_number(now):
        raise ValueError("pending attention needs finite host time")
    open_ids = set(open_ids)
    out = connect(out_db)
    try:
        with out:
            if open_ids:
                out.execute("DELETE FROM risk_attention_events WHERE position_id NOT IN (%s)" % ",".join("?" * len(open_ids)),
                            tuple(open_ids))
            else:
                out.execute("DELETE FROM risk_attention_events")
            out.execute("DELETE FROM risk_attention_events WHERE done=1 AND detected_at<?", (now - ATTN_WINDOW_S,))
            fresh = []
            for pid, events in fresh_by_id.items():
                if pid not in open_ids:
                    continue
                for event in events:
                    if not isinstance(event, (tuple, list)) or len(event) != 2:
                        continue
                    signal, at = event
                    if (isinstance(signal, str) and signal.strip() and R._finite_number(at)
                            and now - ATTN_WINDOW_S <= at <= now):
                        fresh.append((pid, signal, at))
            out.executemany("INSERT OR IGNORE INTO risk_attention_events(position_id,signal,detected_at) VALUES(?,?,?)", fresh)
            pending = {}
            for pid, signal, at in out.execute("SELECT position_id,signal,detected_at FROM risk_attention_events WHERE done=0 "
                                               "ORDER BY detected_at,position_id,signal"):
                pending.setdefault(pid, []).append((signal, at))
            row = out.execute("SELECT MAX(response_at) FROM risk_calls WHERE response_at>? AND response_at<=? AND call_error IN (?,?)",
                              (now - BACKOFF_S, now, *FAIL_BACKOFF)).fetchone()
            backoff = row[0] + BACKOFF_S if row and R._finite_number(row[0]) else 0
        return pending, backoff
    finally:
        out.close()


def ack_attention(out_connection, keys):
    """Acknowledge original (position_id,signal,detected_at) identities with the caller's decision transaction; never commit here."""
    out_connection.executemany("UPDATE risk_attention_events SET done=1 WHERE position_id=? AND signal=? AND detected_at=?", keys)


def new_state(started_at=None):
    started = time.time() if started_at is None else started_at
    return {"last_cycle": None, "seen_breaks": set(), "backoff_until": 0.0, "consec_fail": 0, "paged": False,
            "started_at": started, "started_utc_at": started - clock.offset_s()}


def _calls_last_hour(out, now):
    return out.execute("SELECT COUNT(*) FROM risk_calls WHERE call_started_at > ? AND call_error IS NOT 'budget_exhausted'",
                       (now - 3600,)).fetchone()[0]


def _row(out, cycle_at, pos, key, *, call_id=None, block=None, verdict=None, call_error=None, decided=None,
         live=1, basis=None, mixed=None, signal=None, gate_audit=None):
    action, rails, reason, code = verdict if verdict else ("HOLD", None, "call_failed" if call_error else "hold", None)
    expires = decided + L.ORDER_TTL_S if decided is not None and action in ("EXIT", "TIGHTEN", "LOOSEN_TRAIL") else None
    out.execute("INSERT OR IGNORE INTO risk_decisions(cycle_at, call_id, position_id, mint, key, inputs_hash, "
                "pnl_pct_exact, tape_state, action, rails_json, reason, reason_code, call_error, decided_at, "
                "expires_at, live, risk_version, pnl_basis, cadence_s, attention_signal, risk_version_mixed, gate_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cycle_at, call_id, pos["id"], pos.get("mint"), key, L.block_hash(block) if block else None,
                 block.get("pnl_pct_exact") if block else None, block.get("tape") if block else None,
                 action, json.dumps(rails, sort_keys=True) if rails else None, reason, code, call_error, decided,
                 expires, live, L.RISK_LIVE_VERSION, basis, L.CYCLE_S, signal, mixed,
                 json.dumps(gate_audit, sort_keys=True) if gate_audit is not None else None))


def run_pass(ledger_db, desk_dir, out_db, *, call_model, now_fn=time.time, bounds=None, live=True,
             hourly_cap=300, state=None, alert_fn=None, positions_fn=None, fill_sigs_of=None, attention_fn=None):
    """One tick: returns stats. Does nothing unless a cycle is due. Never raises on a model failure."""
    state = state if state is not None else new_state()
    params = gate_settings()
    stats = {"positions": 0, "cycle": False, "calls": 0, "failed_calls": 0, "skipped": 0, "unbacked_decisions": 0,
             "gated_positions": 0, "avoided_calls": 0}
    if positions_fn is None:
        from memecoin_desk_risk_shadow import open_desk_positions as positions_fn
    if fill_sigs_of is None:
        fill_sigs_of = ledger_fill_sigs(ledger_db)
    opened = positions_fn(ledger_db)
    stats["positions"] = len(opened)
    references = state.setdefault("gate_references", {})
    open_keys = {"pos:%d" % p["id"] for p, *_ in opened}
    for key in list(references):
        if key not in open_keys:
            del references[key]
    if not opened:
        if params is not None and Path(out_db).exists():
            pending_attention(out_db, set(), {}, now_fn())
        state["consec_fail"], state["paged"] = 0, False
        return stats
    now = now_fn()
    offset = clock.offset_s()
    fact_now = now - offset
    # Fired attention is read EVERY tick (two read-only sqlite opens per position): it wakes the cycle at once.
    attention_fn = attention_fn or fired_attention
    seen_attn = state.setdefault("seen_attn", set())
    attn_of, woke = {}, {}
    for pos, _policy, _rails, _mark in opened:
        try:
            attn = attention_fn(pos["id"], now)
        except Exception:                                    # a broken attention read is no events, never a dead pass
            log.exception("attention read failed pos=%s", pos.get("id"))
            attn = []
        if params is not None:
            attn = [(sig, at) for sig, at in attn if R._finite_number(at) and now - ATTN_WINDOW_S <= at <= now]
        attn_of[pos["id"]] = attn
        new = sorted({sig for sig, at in attn if (pos["id"], sig, at) not in seen_attn})
        if new:
            woke[pos["id"]] = (new, {(pos["id"], sig, at) for sig, at in attn})
    if params is not None:
        pending, restored_backoff = pending_attention(out_db, {p["id"] for p, *_ in opened}, attn_of, now)
        if not state.get("gate_backoff_restored"):
            state["backoff_until"] = max(state["backoff_until"], restored_backoff)
            state["gate_backoff_restored"] = True
        woke = {pid: (sorted({sig for sig, _at in events}), {(pid, sig, at) for sig, at in events})
                for pid, events in pending.items()}
    if (state["last_cycle"] is not None and now - state["last_cycle"] < L.CYCLE_S and not woke
            and now - state.get("last_probe", 0.0) < PROBE_S):
        return stats                           # between cycles the structure-break probe runs every PROBE_S, not every tick
    state["last_probe"] = now
    from sniper.services.memecoin_desk_universe import bonding_curve
    items, fresh = [], set()
    for pos, policy, rail_ids, mark in opened:
        bars = bars_since(desk_dir, pos.get("executed_pool") or bonding_curve(pos["mint"]),
                          R._ts(pos["opened_at"]), now, mint=pos["mint"], fact_now=fact_now)
        brk = structure_break_of(pos, bars, desk_dir, now, now - fact_now)
        if brk is not None and (pos["id"], brk) not in state["seen_breaks"]:
            fresh.add((pos["id"], brk))                      # consumed only when a cycle actually runs
        items.append((pos, policy, mark, bars, attn_of[pos["id"]], brk is not None))
    since = None if state["last_cycle"] is None else now - state["last_cycle"]
    # A fired attention event wakes the cycle at once (calls still never overlap: one thread, one call at a time).
    if since is not None and since < L.CYCLE_S and not woke and not (fresh and since >= L.EVENT_MIN_GAP_S):
        return stats
    if params is None:
        state["seen_breaks"] |= fresh
        for _sigs, keys in woke.values():
            seen_attn |= keys
    state["last_cycle"] = now
    stats["cycle"] = True
    out = connect(out_db)
    try:
        from memecoin_desk_risk_shadow import local_sol_usd
        sol_usd = local_sol_usd(desk_dir, now=now)
        blocks, policies, byp = [], {}, {}
        extra, gate_audits = {}, {}                              # key -> observer audit columns
        considered = 0
        for pos, policy, mark, bars, attn, sb_now in items:
            codes = list(woke.get(pos["id"], ((), ()))[0])
            if any(p == pos["id"] for p, _ in fresh):
                codes.append("structure_break")
            ex = {"basis": R.pnl_basis(pos, mark, now=fact_now),
                  "mixed": int(R._ts(pos["opened_at"]) < state.get("started_utc_at", state["started_at"] - offset)),
                  "signal": ",".join(codes) or None}
            extra["pos:%d" % pos["id"]] = ex
            if policy is None:                                   # no frozen policy: nothing to validate against
                _row(out, now, pos, "pos:%d" % pos["id"], call_error="no_frozen_policy", live=int(live), **ex)
                continue
            block = L.position_facts(pos, policy, mark=mark, bars=bars, now=fact_now, host_now=now,
                                     attn=attn, sol_usd=sol_usd, sb=sb_now, regime=regime_of(pos, desk_dir, now),
                                     structure=structure_of(pos, desk_dir=desk_dir, now=now, bounds=bounds,
                                                                            fill_sigs_of=fill_sigs_of))
            considered += 1
            if params is not None:
                _gate_block(block, pos, policy, mark, desk_dir=desk_dir, now=now, sol_usd=sol_usd,
                            fill_sigs_of=fill_sigs_of, sb=sb_now, params=params, bars=bars)
                if not block["sb"]:
                    fresh = {x for x in fresh if x[0] != pos["id"]}
                    codes = [c for c in codes if c != "structure_break"]
                    ex["signal"] = ",".join(codes) or None
                previous = references.get(block["k"])
                changed, selection = G.material(block, previous, params)
                if codes:
                    changed, selection = True, "attention" if pos["id"] in woke else "structure_break"
                elif previous is None and any(v is True for v in block["rail_proximity"].values()):
                    changed, selection = True, "rail_proximity"
                elif selection == "unknown" or (selection == "baseline" and _reference(block) is None):
                    changed, selection = True, "evidence_gated:unknown_called"
                audit = {"params": params, "selection": selection, "snapshot_evidence": block["ev"]}
                if pos["id"] in woke:
                    audit["pending_attention"] = sorted(woke[pos["id"]][1])
                gate_audits[block["k"]] = audit
                if not changed:
                    if previous is None and _reference(block) is not None:
                        references[block["k"]] = _reference(block)
                    stats["gated_positions"] += 1
                    _row(out, now, pos, block["k"], block=block,
                         verdict=("HOLD", None, "evidence_gated:" + selection, None),
                         live=int(live), gate_audit=audit, **ex)
                    continue
            blocks.append(block)
            policies[block["k"]] = policy
            byp[block["k"]] = pos
        if params is not None and state["backoff_until"] <= now:
            available = max(0, hourly_cap - _calls_last_hour(out, now))
            before = min(available, math.ceil(considered / L.MAX_POSITIONS))
            after = min(available, math.ceil(len(blocks) / L.MAX_POSITIONS))
            stats["avoided_calls"] = before - after
        cycle_failed = bool(blocks)
        for i in range(0, len(blocks), L.MAX_POSITIONS):
            chunk = blocks[i:i + L.MAX_POSITIONS]
            gate = ("backoff" if state["backoff_until"] > now else
                    "budget_exhausted" if _calls_last_hour(out, now) >= hourly_cap else None)
            if gate:
                stats["skipped"] += len(chunk)
                for b in chunk:
                    _row(out, now, byp[b["k"]], b["k"], block=b, call_error=gate, live=int(live),
                         gate_audit=gate_audits.get(b["k"]), **extra[b["k"]])
                continue
            user = L.build_user(chunk, bounds, gate_params=params)
            started = now_fn()
            result = call_model(L.SYSTEM_PROMPT, user)
            decided = now_fn()
            stats["calls"] += 1
            error = result.get("call_error") or (
                "model_result_error" if result.get("is_error") or not result.get("text") else None)
            if error is None and not R.echo_ok(result.get("model_echo")):
                error = "model_mismatch"
            decision_blocks, decision_policies, changed_positions = chunk, policies, set()
            if params is not None and not error:
                try:
                    current = {p["id"]: (p, pol, mark) for p, pol, _rails, mark in positions_fn(ledger_db)}
                except Exception:
                    log.exception("RISK decision snapshot unreadable")
                    current = {}
                decision_blocks, decision_policies = [], dict(policies)
                for b in chunk:
                    old = byp[b["k"]]
                    current_item = current.get(old["id"])
                    audit = gate_audits[b["k"]]
                    audit["rechecked_at"] = decided
                    if (current_item is None or current_item[1] is None
                            or any(current_item[0].get(k) != old.get(k) for k in ("mint", "opened_at", "network_id"))):
                        changed_positions.add(b["k"])
                        decision_blocks.append(b)
                        audit["decision_evidence"] = None
                        continue
                    p, pol, mark = current_item
                    bars = bars_since(desk_dir, p.get("executed_pool") or bonding_curve(p["mint"]),
                                      R._ts(p["opened_at"]), decided, mint=p["mint"], fact_now=decided - clock.offset_s())
                    sb = structure_break_of(p, bars, desk_dir, decided, clock.offset_s()) is not None
                    current_sol = local_sol_usd(desk_dir, now=decided)
                    fresh_block = L.position_facts(p, pol, mark=mark, bars=bars, now=decided - clock.offset_s(),
                                                   host_now=decided, sol_usd=current_sol, sb=sb)
                    _gate_block(fresh_block, p, pol, mark, desk_dir=desk_dir, now=decided, sol_usd=current_sol,
                                fill_sigs_of=fill_sigs_of, sb=sb, params=params, bars=bars)
                    decision_blocks.append(fresh_block)
                    decision_policies[b["k"]] = pol
                    audit["decision_evidence"] = fresh_block["ev"]
                    audit["decision_rail_proximity"] = fresh_block["rail_proximity"]
            verdicts = (None if error else L.validate(result["text"], blocks=decision_blocks,
                                                     policies=decision_policies, bounds=bounds, gate_params=params))
            if verdicts is not None:
                for key in changed_positions:
                    verdicts[key] = ("HOLD", None, "position_changed", None)
            cur = out.execute(
                "INSERT INTO risk_calls(cycle_at, call_started_at, response_at, latency_ms, n_positions, prompt_hash, "
                "user_z, raw_text, model, model_echo, session_id, result_id, usage_json, notional_usd, call_error, "
                "live, risk_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, started, decided, round((decided - started) * 1000, 1), len(chunk), L.inputs_hash(chunk, bounds, gate_params=params),
                 zlib.compress(user.encode()), result.get("text"), L.MODEL, result.get("model_echo"),
                 result.get("session_id"), result.get("result_id"),
                 json.dumps(result.get("usage") or {}, sort_keys=True), result.get("notional_usd"), error,
                 int(live), L.RISK_LIVE_VERSION))
            call_id = cur.lastrowid
            for b in chunk:
                if not error and verdicts[b["k"]][2].startswith("unbacked_reason:"):
                    stats["unbacked_decisions"] += 1
                _row(out, now, byp[b["k"]], b["k"], call_id=call_id, block=b,
                     verdict=None if error else verdicts[b["k"]], call_error=error, decided=decided, live=int(live),
                     gate_audit=gate_audits.get(b["k"]), **extra[b["k"]])
            if error:
                stats["failed_calls"] += 1
                if error in FAIL_BACKOFF:
                    state["backoff_until"] = decided + BACKOFF_S
            else:
                cycle_failed = False
                if params is not None:
                    for b in chunk:
                        if _reference(b) is not None:
                            references[b["k"]] = _reference(b)
                        pid = byp[b["k"]]["id"]
                        state["seen_breaks"] |= {x for x in fresh if x[0] == pid}
                        if pid in woke:
                            seen_attn |= woke[pid][1]
                            ack_attention(out, woke[pid][1])
        out.commit()
        if blocks:
            _streak(state, cycle_failed, alert_fn)
    finally:
        out.close()
        state["last_cycle"] = now_fn()                   # the period runs from the END of the cycle (and its calls)
    return stats


def _streak(state, failed, alert_fn):
    if not failed:
        state["consec_fail"], state["paged"] = 0, False
        return
    state["consec_fail"] += 1
    if state["consec_fail"] >= L.BLIND_CYCLES_PAGE and not state["paged"]:
        state["paged"] = True
        text = ("desk RISK seat is blind: %d failed cycles in a row with desk positions open. The model sees nothing; "
                "deterministic rails still run." % state["consec_fail"])
        log.warning(text)
        try:
            if alert_fn is None:
                from sniper.services.memecoin_alerts import send_alert as alert_fn
            alert_fn("desk_risk_blind", text)
        except Exception:                                         # a failed page must not stop the watcher
            log.exception("desk risk blind page failed")


def main(argv=None):
    from sniper.util.state_paths import require_state_root_outside_release
    require_state_root_outside_release()   # never a state root inside .runtime/ (Gamma 2026-10-21)
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root, state_path
    load_env_files(str(state_root()))
    refresh_knobs()
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):     # the subscription seat, never a key
        os.environ.pop(key, None)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    p.add_argument("--ledger-db", default=os.getenv("DB_PATH") or str(state_path("sniper_tracker.db")))
    p.add_argument("--paper", action="store_true",
                   help="the RISK seat for PAPER desk positions (desk_paper_positions, desk DB); its own lock and its own "
                        "decisions DB (risk_live_paper.db), read only by the paper engine; never the funded ledger")
    a = p.parse_args(argv)
    if os.getenv("MEMECOIN_DESK_RISK_WATCHER_ENABLED", "1").strip() == "0":
        print("[desk-risk-watcher] MEMECOIN_DESK_RISK_WATCHER_ENABLED=0; exiting")
        return 0
    if not a.paper and not Path(a.ledger_db).is_file():
        print("[desk-risk-watcher] refused: ledger DB missing")
        return 2
    from sniper.settings.memecoin import parse_desk_rail_bounds
    bounds = parse_desk_rail_bounds(os.getenv("MEMECOIN_DESK_RAIL_BOUNDS", ""))
    live = os.getenv("MEMECOIN_DESK_RISK_LIVE", "1").strip() != "0"
    cap = int(os.getenv("MEMECOIN_DESK_RISK_CALLS_PER_HOUR", "300"))
    desk_dir = Path(state_root()) / "data" / "desk"
    desk_dir.mkdir(parents=True, exist_ok=True)
    lock = open(desk_dir / ("risk_watcher_paper.lock" if a.paper else "risk_watcher.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[desk-risk-watcher] another watcher holds the lock; exiting")
        return 0
    from memecoin_desk_judge_worker import WarmSpare, _sdk_options, sdk_call_model
    # The JUDGE's warm spare (P2), its own instance for this seat: one pre-connected CLI, one call each.
    spare = None
    if os.getenv("MEMECOIN_DESK_RISK_WARM_SPARE", "1").strip() != "0":
        spare = WarmSpare(_sdk_options(L.SYSTEM_PROMPT, model=L.MODEL, effort="low", thinking=None,
                                       persist_session=False),
                          max_age_s=float(os.getenv("MEMECOIN_DESK_JUDGE_WARM_SPARE_MAX_AGE_S", "600")))
        import atexit
        atexit.register(spare.close)

    def call_model(system, user):
        return sdk_call_model(system, user, deadline_s=L.DEADLINE_S, model=L.MODEL, persist_session=False,
                              **({"spare": spare} if spare else {}))
    state = new_state()
    while True:
        try:
            if a.paper:
                from sniper.services import memecoin_desk_paper as paper
                desk_db = Path(state_root()) / "data" / "memecoin_desk.db"
                stats = run_pass(a.ledger_db, desk_dir, desk_dir / paper.RISK_OUT_DB, call_model=call_model,
                                 bounds=bounds, live=live, hourly_cap=cap, state=state,
                                 positions_fn=lambda _ledger: paper.risk_positions(desk_db), fill_sigs_of=lambda _m: set(),
                                 attention_fn=lambda _pid, _now: [])   # attention files are keyed by REAL position ids
            else:
                stats = run_pass(a.ledger_db, desk_dir, desk_dir / "risk_live.db", call_model=call_model,
                                 bounds=bounds, live=live, hourly_cap=cap, state=state)
            if stats["cycle"]:
                print(json.dumps(stats), flush=True)
        except Exception:                                         # a bad pass is logged, never a silent exit
            log.exception("desk risk watcher pass failed")
            time.sleep(5.0)
        if a.once:
            return 0
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
