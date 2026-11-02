"""Desk PAPER engine (operator order 2026-10-26, Alpha approved design B: 'we are clearly entering rugs blindly').

WHAT. With MEMECOIN_DESK_EXECUTION=paper the desk courier writes NO memecoin_manual_intents row: the funded runner never
sees a paper intent, so no path signs or spends. The request lands here instead. This engine applies the desk's own book
gates over PAPER positions only, fills the clip with the exact native Pump curve arithmetic on OUR tape at the decision
+ ENTRY_DELAY_S (our landing delay), and exits on the runner's own rail function (alpha_b_real_exit_reason, desk trail
arming included) valued by the exact full-quantity sell at the exit tick + EXIT_DELAY_S. After graduation the sell is
the exact PumpSwap AMM sell on the captured pool state. Native routes only (never Jupiter), stated.

NEVER. This module loads no keypair, builds no transaction, sends nothing, and writes nothing to the funded ledger; its
positions live in the desk DB (desk_paper_positions), so funded bankroll, capacity, the daily-loss breaker and real PnL
never count them. A real-mode flip changes only where NEW requests go; a paper position stays paper.

REALISM LIMITS (logged, never hidden): no landing failure is simulated (the twin read measures that optimism); the
execution cost is a constant measured from real desk Solana fills (EXEC_COST); our own clip's impact IS in the
arithmetic; other traders' reaction to it is not.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import sqlite3
import math
import os
import time
import uuid
from datetime import datetime, timezone

log = logging.getLogger(__name__)

VERSION = "desk-paper-v2"
# landing delays (Alpha 10-26, FINAL for desk-paper-v2, Delta's host-clock measure over all 46 real desk exits):
# ENTRY verdict -> swap observed p50 0.86 / p90 1.29 -> 1.0 s; rail/vol exits (sweep decision -> swap) p50 0.93 /
# p90 1.68 -> 1.0 s; RISK-model exits (RISK_EXIT response -> swap) p50 1.45 / p90 2.35 -> 1.5 s. Tails are not captured
# by a constant: every close logs its own applied delay (exit_json.exit_delay_s / delay_kind).
ENTRY_DELAY_S = 1.0
EXIT_DELAY_S = 1.0
RISK_EXIT_DELAY_S = 1.5
RISK_EXIT_REASON = "desk_risk_exit"


def exit_delay_s(reason):
    return RISK_EXIT_DELAY_S if reason == RISK_EXIT_REASON else EXIT_DELAY_S
# per-leg network + priority fee in SOL, MEASURED (Gamma 2026-10-26, docs/audits/2026-10-26-desk-exec-cost.md,
# scripts/research/desk_exec_cost.py): meta.fee of all 92 real desk fill signatures read from chain. ENTRY mean 0.001899
# (median 0.000830, p90 0.005005; 24 % of entries at the 0.005005 priority cap): the mean is the expected cost. EXIT
# always 0.000105. NOT included: builder tips (none visible), net rent, slippage vs the tape state, landing failures.
EXEC_COST_SOL = {"entry": 0.001899, "exit": 0.000105, "source": "docs/audits/2026-10-26-desk-exec-cost.md (92 desk fills)"}
PNL_BASIS = "after measured network+priority fees; tips/rent/slippage/landing not included"
MIN_SOL_USD_AGE_S = 120.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_paper_positions (
    id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE, intent_id TEXT NOT NULL, mint TEXT NOT NULL,
    network_id TEXT NOT NULL, cohort_id TEXT NOT NULL, version TEXT NOT NULL, state TEXT NOT NULL,
    requested_at TEXT NOT NULL, decided_at TEXT, filled_at TEXT, size_usd REAL NOT NULL, rails_json TEXT NOT NULL,
    desk_metadata_json TEXT NOT NULL, verdict_ref_usd REAL, quote1_json TEXT, fill_json TEXT,
    entry_price_usd REAL, qty_atomic TEXT, entry_cost_usd REAL, peak_price_usd REAL, peak_at TEXT,
    last_mark_usd REAL, last_mark_at TEXT, last_quote_at TEXT, exit_reason TEXT, exit_requested_at TEXT,
    exit_json TEXT, closed_at TEXT, exit_usd REAL, pnl_usd REAL, pnl_pct REAL, refusal TEXT, log_json TEXT,
    entry_card_at TEXT, end_card_at TEXT);
CREATE INDEX IF NOT EXISTS idx_desk_paper_state ON desk_paper_positions(state);
CREATE INDEX IF NOT EXISTS idx_desk_paper_cohort_state ON desk_paper_positions(cohort_id,state);
CREATE TABLE IF NOT EXISTS desk_paper_marks (
    id INTEGER PRIMARY KEY, position_id INTEGER NOT NULL, cohort_id TEXT NOT NULL, at_host TEXT NOT NULL,
    at_ts REAL NOT NULL, lamports TEXT, reason TEXT NOT NULL, binding_kind TEXT NOT NULL, tip_side TEXT NOT NULL,
    proof_sha256 TEXT, run_id TEXT NOT NULL, tick_seq INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_desk_paper_marks_at ON desk_paper_marks(at_ts);
CREATE TABLE IF NOT EXISTS desk_paper_marks_prune (id INTEGER PRIMARY KEY CHECK (id = 1), cutoff_ts REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS desk_paper_marks_prune_insert BEFORE INSERT ON desk_paper_marks_prune
    WHEN NEW.cutoff_ts > CAST(strftime('%s', 'now') AS REAL) - 1209600
    BEGIN SELECT RAISE(ABORT, 'prune cutoff inside the 14-day retention'); END;
CREATE TRIGGER IF NOT EXISTS desk_paper_marks_prune_update BEFORE UPDATE ON desk_paper_marks_prune
    WHEN NEW.cutoff_ts < OLD.cutoff_ts OR NEW.cutoff_ts > CAST(strftime('%s', 'now') AS REAL) - 1209600
    BEGIN SELECT RAISE(ABORT, 'prune cutoff is forward-only and outside the 14-day retention'); END;
CREATE TRIGGER IF NOT EXISTS desk_paper_marks_prune_delete BEFORE DELETE ON desk_paper_marks_prune
    BEGIN SELECT RAISE(ABORT, 'prune cutoff cannot be removed'); END;
CREATE TRIGGER IF NOT EXISTS desk_paper_marks_no_update BEFORE UPDATE ON desk_paper_marks
    BEGIN SELECT RAISE(ABORT, 'desk_paper_marks is append-only'); END;
CREATE TRIGGER IF NOT EXISTS desk_paper_marks_retention BEFORE DELETE ON desk_paper_marks
    WHEN old.at_ts >= COALESCE((SELECT cutoff_ts FROM desk_paper_marks_prune WHERE id = 1), -1)
    BEGIN SELECT RAISE(ABORT, 'desk_paper_marks rows inside retention are immutable'); END;
CREATE TABLE IF NOT EXISTS desk_paper_cohort_trips (
    cohort_id TEXT PRIMARY KEY, known_at TEXT NOT NULL, limit_usd REAL NOT NULL,
    pnl_usd REAL NOT NULL, reason TEXT NOT NULL);
"""
OPEN_STATES = ("pending_fill", "open")


def execution_mode(environ=None):
    """MEMECOIN_DESK_EXECUTION: 'real' (default) or 'paper'. Anything else is refused at startup, never guessed."""
    raw = ((os.environ if environ is None else environ).get("MEMECOIN_DESK_EXECUTION") or "real").strip().lower()
    if raw not in ("real", "paper"):
        raise ValueError("MEMECOIN_DESK_EXECUTION must be real or paper")
    return raw


def ensure(conn):
    conn.executescript(SCHEMA)


# desk_paper_marks: one append-only row per mark attempt (priced or refused), so priced share and tip side are
# measurable. Unknown binding/tip side is stored as 'unknown', never defaulted. A log failure never touches the mark.
# BEST-EFFORT: the batch is written after the tick's marks commit, so a crash between them loses that tick's rows;
# the absence of a row never proves no mark. Each process writes its own run_id and a tick_seq that rises by one per
# logged tick, so a lost tick inside a run is a gap and a crash is a run_id change.
MARKS_RETENTION_S = 14 * 86400        # prune rule (also enforced by trigger): rows with at_ts < wall clock - 14 d
MARK_LOG_ERRORS = {"mark_log_errors": 0}
RUN_ID = uuid.uuid4().hex
_TICK_SEQ = itertools.count(1)       # next() is atomic under the GIL: no two writers share a seq
_LAST_PRUNE = [float("-inf")]


def _mark_row(row, at, lamports, reason, evidence):
    proof = evidence.get("proof")
    return (row["id"], row["cohort_id"], _iso(at), at, None if lamports is None else str(lamports), str(reason),
            evidence.get("binding_kind") or "unknown", (proof or {}).get("tip_side") or "unknown",
            hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if proof is not None else None)


def _write_marks(conn, rows, *, lost=0):
    """One insert batch after every mark and exit of the tick is committed: the log never delays them. `lost` rows
    that could not be built burn one extra tick_seq, so even a partial loss is a visible gap."""
    if lost:
        next(_TICK_SEQ)
    if not rows:
        return
    seq = next(_TICK_SEQ)                 # taken before the write: a failed write leaves a visible gap
    try:
        with conn:
            conn.executemany("INSERT INTO desk_paper_marks(position_id, cohort_id, at_host, at_ts, lamports, reason, "
                             "binding_kind, tip_side, proof_sha256, run_id, tick_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             [r + (RUN_ID, seq) for r in rows])
    except Exception:
        MARK_LOG_ERRORS["mark_log_errors"] += 1
        log.exception("desk paper mark log write failed rows=%d", len(rows))


def prune_marks(conn, *, now=None):
    """Delete mark rows older than MARKS_RETENTION_S by the WALL clock (the triggers refuse any cutoff inside the
    window or below the last one). Returns rows deleted."""
    # whole seconds, 1 s back: SQLite's strftime('%s','now') in the trigger truncates the wall clock to seconds
    cutoff = math.floor(time.time() if now is None else now) - 1 - MARKS_RETENTION_S
    with conn:
        conn.execute("INSERT INTO desk_paper_marks_prune(id, cutoff_ts) VALUES (1, ?) "
                     "ON CONFLICT(id) DO UPDATE SET cutoff_ts = MAX(cutoff_ts, excluded.cutoff_ts)", (cutoff,))
        return conn.execute("DELETE FROM desk_paper_marks WHERE at_ts < ?", (cutoff,)).rowcount


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ------------------------------------------------------------------ admission: the desk book over PAPER positions only
def _cohort_refusal(conn, cohort_id, *, now_iso):
    """Check and latch under the caller's paper-only write transaction."""
    if not isinstance(cohort_id, str) or not cohort_id:
        return "desk_cohort_policy_unknown"
    if conn.execute("SELECT 1 FROM desk_paper_cohort_trips WHERE cohort_id=?", (cohort_id,)).fetchone():
        return "desk_cohort_killed"
    try:
        row = conn.execute("SELECT policy_json FROM desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
        policy = json.loads(row[0]) if row else None
        limit = policy.get("cumulative_kill_usd") if isinstance(policy, dict) else None
    except (sqlite3.Error, ValueError, TypeError):
        return "desk_cohort_policy_unknown"
    try:
        valid_limit = type(limit) in (int, float) and math.isfinite(limit) and limit < 0
    except OverflowError:
        valid_limit = False
    if not valid_limit:
        return "desk_cohort_policy_unknown"
    try:
        count, unknown, pnl = conn.execute(
            "SELECT COUNT(*),SUM(CASE WHEN pnl_usd IS NULL OR TYPEOF(pnl_usd) NOT IN ('integer','real') "
            "THEN 1 ELSE 0 END),SUM(pnl_usd) FROM desk_paper_positions WHERE cohort_id=? AND state='closed'",
            (cohort_id,)).fetchone()
    except sqlite3.Error:
        return "desk_cohort_accounting_unknown"
    if unknown or (count and (type(pnl) not in (int, float) or not math.isfinite(pnl))):
        return "desk_cohort_accounting_unknown"
    pnl = float(pnl) if count else 0.0
    if pnl <= limit:
        conn.execute("INSERT INTO desk_paper_cohort_trips(cohort_id,known_at,limit_usd,pnl_usd,reason) "
                     "VALUES(?,?,?,?,?)", (cohort_id, now_iso, limit, pnl, "cumulative_paper_loss"))
        return "desk_cohort_killed"
    return None


def cohort_refusal(conn, cohort_id, *, now_iso):
    """Authoritative pre-model paper gate; requires an idle writable Desk connection."""
    from sniper.persistence import db_memecoin_desk as store
    if conn.in_transaction:
        raise ValueError("transaction_active")
    now_iso = store._time(now_iso)
    started = time.monotonic()
    ensure(conn)
    with store._write(conn):
        known_at = _iso(_ts(now_iso) + max(0.0, time.monotonic() - started))
        return _cohort_refusal(conn, cohort_id, now_iso=known_at)


def book_refusal(conn, *, request, now_iso):
    """The desk's book gates (memecoin_origin.desk_book_refusal semantics) counted over paper positions, plus the
    runner's request checks (chain, clip, rails bounds, metadata). None = admitted. Never reads the funded ledger."""
    import sniper.settings.memecoin as cfg
    from sniper.persistence.db_memecoin import normalized_coin_network
    from sniper.services.memecoin_manual_intents import validate_desk_rails
    from sniper.services.memecoin_origin import validate_desk_metadata
    p = request["payload"]
    if not cfg.MEMECOIN_DESK_ENABLED:
        return "desk_disabled"
    if normalized_coin_network(p["network_id"], p["mint"]) not in cfg.MEMECOIN_DESK_CHAINS:
        return "desk_chain_not_allowed"
    try:
        size = float(p["size_usd"])
    except (TypeError, ValueError, KeyError):
        return "desk_intent_malformed"
    if not (math.isfinite(size) and 0 < size <= float(cfg.MEMECOIN_DESK_CLIP_USD or 0.0)):
        return "desk_clip_exceeded"
    ok, why = validate_desk_rails(p.get("rails"), cfg.MEMECOIN_DESK_RAIL_BOUNDS)
    if not ok:
        return "desk_rails_invalid: %s" % why
    ok, why = validate_desk_metadata(p.get("desk_metadata"))
    if not ok:
        return "desk_metadata_invalid: %s" % why
    limit = int(cfg.MEMECOIN_DESK_MAX_CONCURRENT or 0)
    live = conn.execute("SELECT COUNT(*) FROM desk_paper_positions WHERE state IN (?,?)", OPEN_STATES).fetchone()[0]
    if limit <= 0 or live >= limit:
        return "desk_max_concurrent"
    halt = float(cfg.MEMECOIN_DESK_DAILY_LOSS_USD or 0.0)
    realized = conn.execute("SELECT COALESCE(SUM(pnl_usd),0) FROM desk_paper_positions WHERE state='closed' AND "
                            "pnl_usd IS NOT NULL AND closed_at>=?", (now_iso[:10] + "T00:00:00+00:00",)).fetchone()[0]
    if halt <= 0 or realized <= -halt:
        return "desk_daily_loss"
    return None


def admit(conn, request, *, now_iso):
    """Record one courier request as a paper position (pending_fill) or a refused row. Idempotent on ticket_id."""
    from sniper.persistence import db_memecoin_desk as store
    if conn.in_transaction:
        raise ValueError("transaction_active")
    now_iso = store._time(now_iso)
    started = time.monotonic()
    ensure(conn)
    p = request["payload"]
    with store._write(conn):
        if conn.execute("SELECT 1 FROM desk_paper_positions WHERE ticket_id=?", (p["desk_ticket_id"],)).fetchone():
            return None
        meta = p["desk_metadata"]
        known_at = _iso(_ts(now_iso) + max(0.0, time.monotonic() - started))
        cumulative = _cohort_refusal(conn, meta.get("cohort_id"), now_iso=known_at)
        refusal = book_refusal(conn, request=request, now_iso=now_iso) or cumulative
        conn.execute(
            "INSERT INTO desk_paper_positions(ticket_id, intent_id, mint, network_id, cohort_id, version, state, requested_at, "
            "size_usd, rails_json, desk_metadata_json, verdict_ref_usd, refusal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["desk_ticket_id"], request["intent_id"], p["mint"], p["network_id"], meta["cohort_id"], VERSION,
             "refused" if refusal else "pending_fill", request["requested_at"], float(p["size_usd"]), json.dumps(p["rails"]),
             json.dumps(meta, sort_keys=True), (meta.get("verdict_price") or {}).get("ref_price_usd"), refusal))
    return refusal


# ------------------------------------------------------------------ the tape: exact native state at a time
def curve_state(desk_dir, mint, at, *, max_quiet_s=900.0):
    """The coin's curve state right after the last swap at or before `at` (reserves change only on swaps), or
    (None, reason). A capture gap between that swap and `at` makes the state unknown (tape_gap)."""
    from sniper.services import memecoin_desk_lessons as L
    rows = L.load_curve(desk_dir, mint, at - max_quiet_s, at)
    rows = [r for r in rows if r["received_at"] <= at]
    if not rows:
        return None, "no_state"
    r = rows[-1]
    from sniper.services import memecoin_desk_structure_tape as ST
    try:
        if ST.gaps_between(desk_dir, r["received_at"], at):
            return None, "tape_gap"
    except ST.TapeUnreadable:
        return None, "gaps_unreadable"
    for day in L.days_between(r["received_at"], at):  # a day with no tape rows is a gap, never 'no gap' (Alpha: 10-25 had none)
        f = os.path.join(str(desk_dir), "swaps", "%s.db" % day)
        try:
            if not L.q(f, "SELECT 1 FROM swaps LIMIT 1", strict=True):
                return None, "tape_day_missing"
        except (RuntimeError, sqlite3.Error):
            return None, "tape_day_missing"
    proof = L._curve_proof_error(r)                     # the shared strict currency/fee proof (Codex)
    if proof is not None:
        return None, proof
    need = ("virtual_quote", "virtual_token", "real_quote", "real_token")
    if any(r.get(k) is None for k in need):
        return None, "state_incomplete"
    if int(r["real_token"]) == 0:
        return None, "curve_complete"
    return {"virtual_sol_reserves": int(r["virtual_quote"]), "virtual_token_reserves": int(r["virtual_token"]),
            "real_sol_reserves": int(r["real_quote"]), "real_token_reserves": int(r["real_token"]),
            "fee_basis_points": int(r["fee_bps"]), "creator_fee_basis_points": int(r["creator_fee_bps"]),
            "cashback_fee_basis_points": int(r.get("cashback_fee_bps") or 0), "at": r["received_at"]}, None


def buy_fill(state, spend_lamports):
    """Exact native buy_exact_sol_in (pump_curve_buy.expected_tokens_out: atomic-exact on 19 of 20 landed buys)."""
    from sniper.execution.pump_curve_buy import expected_tokens_out
    tokens = expected_tokens_out(spend_lamports, state)
    return tokens if tokens > 0 else None


PROVEN_ZERO = "pool_sell_zero"       # sell_proceeds' only reason computed from proven state with a zero output


def sell_proceeds(desk_dir, mint, qty, at):
    """Exact full-quantity sell at `at`, in lamports: the curve while it trades, the PumpSwap pool after graduation.
    (None, reason) when no executable state exists (unsellable is recorded, never a guessed price)."""
    from sniper.execution.pump_native_exit import curve_sell_quote
    state, why = curve_state(desk_dir, mint, at)
    if state is not None:
        quote, refusal = curve_sell_quote(state, int(qty), 50.0, protocol_bps=state["fee_basis_points"],
                                          creator_bps=state["creator_fee_basis_points"])
        return (quote["expected_out"], "curve") if quote else (None, refusal)
    if why != "curve_complete":
        return None, why
    from sniper.services import memecoin_desk_lessons as L
    invalid = []
    # load_pool reads received_at < hi: the next float above `at` keeps a row exactly AT the quote time (Codex e2e 2)
    pool = L.load_pool(desk_dir, mint, at - 3600, math.nextafter(at, math.inf), invalid_states=invalid)
    if not pool:
        return None, "pool_unknown" if pool is None else "pool_no_state"
    st = [s for s in pool if s["t"] <= at]
    if not st:
        return None, "pool_no_state"
    last = st[-1]
    strict = L._LearningTape(L.Tape([], pool, None, pool_invalid_states=invalid))   # shared freshness/invalid barriers
    sol = strict.pool_value_at(at, int(qty))
    if sol is None:
        return None, "pool_" + (sorted(strict.used_errors)[0] if strict.used_errors else "stale")
    from sniper.services import memecoin_desk_ps_tape as PT
    try:
        if PT.ps_gaps(desk_dir, last["t"], at):
            return None, "pool_tape_gap"
    except PT.TapeUnreadable:
        return None, "pool_gaps_unreadable"
    return (int(round(sol * 1e9)), "pumpswap") if sol else (None, "pool_sell_zero")


# ------------------------------------------------------------------ fills and exits
class FxHistory:
    """CAUSAL SOL/USD (Codex): the latest observed (sol_at, sol_usd) at or before an event time, no older than
    MIN_SOL_USD_AGE_S, from the observations this engine has seen; never a later FX for an earlier fill or exit."""

    def __init__(self, keep_s=900.0):
        self.rows, self.keep_s = [], keep_s

    def add(self, sol_usd, sol_at):
        ok = type(sol_usd) in (int, float) and math.isfinite(sol_usd) and sol_usd > 0 and type(sol_at) in (int, float)
        if ok and (not self.rows or sol_at > self.rows[-1][0]):
            self.rows.append((float(sol_at), float(sol_usd)))
            self.rows = [r for r in self.rows if r[0] >= sol_at - self.keep_s]

    def at(self, t):
        best = None
        for sol_at, usd in self.rows:
            if sol_at <= t:
                best = (sol_at, usd)
        return best[1] if best is not None and t - best[0] <= MIN_SOL_USD_AGE_S else None


def _fx_fn(sol_usd, sol_at, fx_at):
    if fx_at is not None:
        return fx_at
    h = FxHistory()
    h.add(sol_usd, sol_at)
    return h.at


PS_PROOF_PENDING = frozenset("entry_failed:" + r for r in ("ps_capture_unproven", "ps_gap_pending", "capture_gap"))
FRONTIER_WAIT_S = 30.0    # an entry whose fill time the tape never commits within this is censored, never filled blind


def _cut(desk_dir, now, venue, *, pool=None, integrity=False):
    """The committed receipt cut for `venue` ('curve' | 'ps') at now (memecoin_desk_capture.capture_proof), or None."""
    from sniper.services.memecoin_desk_capture import capture_proof
    # New-mode preguard proves only health/frontier. PSP verifies the quote's exact dependency interval itself.
    strict = {"pool": pool, "integrity": True, "window_s": 0} if integrity else {}
    proof = capture_proof(desk_dir, now, venues=(venue,), **strict)
    return None if proof is None else proof["through"]


def fill_pending(conn, *, desk_dir, now, sol_usd=None, sol_at=None, candidate_fn=None, fx_at=None):
    """Fill every pending paper entry whose decision + ENTRY_DELAY_S has passed: quote_1 at the decision, the fill at
    the decision + ENTRY_DELAY_S. A fill above the frozen verdict cap is refused skipped_chase, exactly as real."""
    from sniper.persistence import db_memecoin_desk as store
    if conn.in_transaction:
        raise ValueError("transaction_active")
    started = time.monotonic()
    ensure(conn)
    out = []
    for row in conn.execute("SELECT * FROM desk_paper_positions WHERE state='pending_fill'").fetchall():
        row = dict(row)
        with store._write(conn):
            current = conn.execute("SELECT state FROM desk_paper_positions WHERE id=?", (row["id"],)).fetchone()
            if current is None or current[0] != "pending_fill":
                continue
            known_now = now + max(0.0, time.monotonic() - started)
            refusal = _cohort_refusal(conn, row["cohort_id"], now_iso=_iso(known_now))
            if refusal:
                result = _cancel_pending(row, refusal, known_now)
                conn.execute("UPDATE desk_paper_positions SET " + ",".join("%s=?" % k for k in result) + " WHERE id=?",
                             (*result.values(), row["id"]))
        if refusal:
            out.append({"ticket_id": row["ticket_id"], "state": "refused", "refusal": refusal})
            continue
        t0 = _ts(row["requested_at"])
        if now < t0 + ENTRY_DELAY_S:
            continue
        cfn = candidate_fn or _candidate_of(conn)
        venue = ((cfn(row["ticket_id"]) or {}).get("venue") or "pumpfun")
        capture_args = _ps_capture_args(conn, row) if venue == "pumpswap" else {}
        cut = _cut(desk_dir, now, "ps" if venue == "pumpswap" else "curve", **capture_args)
        if cut is None or cut < t0 + ENTRY_DELAY_S:       # the fill moment is not on committed tape yet (Codex frontier)
            if now - (t0 + ENTRY_DELAY_S) <= FRONTIER_WAIT_S:
                continue
            result = {"state": "failed", "refusal": "entry_failed:capture_unproven", "decided_at": _iso(t0)}
        else:
            fx = _fx_fn(sol_usd, sol_at, fx_at)(t0 + ENTRY_DELAY_S)      # quote reads stay outside the paper write lock
            meta = json.loads(row["desk_metadata_json"])
            result = (_fill_ps(conn, desk_dir, row, t0, fx, meta, now=now, candidate_fn=cfn) if venue == "pumpswap"
                      else _fill(desk_dir, row, t0, fx, meta, candidate_fn=cfn))
            # A loss receipt still pending attribution clears within seconds: re-read the same frozen fill moment.
            if (result.get("refusal") in PS_PROOF_PENDING and now - (t0 + ENTRY_DELAY_S) <= FRONTIER_WAIT_S):
                continue
        with store._write(conn):
            current = conn.execute("SELECT state FROM desk_paper_positions WHERE id=?", (row["id"],)).fetchone()
            if current is None or current[0] != "pending_fill":
                continue
            known_now = now + max(0.0, time.monotonic() - started)
            refusal = _cohort_refusal(conn, row["cohort_id"], now_iso=_iso(known_now))
            if refusal:
                result = _cancel_pending(row, refusal, known_now)
            conn.execute("UPDATE desk_paper_positions SET " + ",".join("%s=?" % k for k in result) + " WHERE id=?",
                         (*result.values(), row["id"]))
        out.append({"ticket_id": row["ticket_id"], **{k: result[k] for k in ("state",)}, "refusal": result.get("refusal")})
        refusal = result.get("refusal") or ""
        if result["state"] == "refused" and (refusal.startswith("desk verdict price: exact quote is")
                                              or refusal.startswith("desk limit watch:")):
            from sniper.services import memecoin_desk as desk       # the real chase / LIMIT arm logic, unchanged
            got = desk.paper_chase(conn, row["ticket_id"], refusal, now_iso=_iso(now))
            if got is not None:
                out.append({"step": "paper_chase", **got})
    conn.commit()
    return out


def _cancel_pending(row, reason, now):
    return {"state": "refused", "refusal": reason, "decided_at": _iso(now),
            "log_json": json.dumps({"cancelled_at": _iso(now), "intended_fill_at":
                                    _iso(_ts(row["requested_at"]) + ENTRY_DELAY_S), "reason": reason})}


def _candidate_of(conn):
    def fn(ticket_id):
        r = conn.execute("SELECT candidate_json FROM desk_candidate_emissions WHERE opportunity_id=? AND is_primary=1",
                         (ticket_id,)).fetchone()
        return json.loads(r[0]) if r else None
    return fn


def _fill(desk_dir, row, t0, fx, meta, candidate_fn=None):
    if fx is None:
        return {"state": "failed", "refusal": "entry_failed:sol_usd_stale", "decided_at": _iso(t0)}
    spend = int(row["size_usd"] / fx * 1e9)
    q1_state, why1 = curve_state(desk_dir, row["mint"], t0)
    state, why = curve_state(desk_dir, row["mint"], t0 + ENTRY_DELAY_S)
    q1 = {"tokens": buy_fill(q1_state, spend) if q1_state else None, "reason": why1, "at": t0}
    if state is None:
        return {"state": "failed", "refusal": "entry_failed:" + why, "decided_at": _iso(t0), "quote1_json": json.dumps(q1)}
    tokens = buy_fill(state, spend)
    if not tokens:
        return {"state": "failed", "refusal": "entry_failed:zero_out", "decided_at": _iso(t0), "quote1_json": json.dumps(q1)}
    cost_sol = EXEC_COST_SOL["entry"]
    entry_cost_usd = row["size_usd"] + (cost_sol or 0.0) * fx         # cost unknown -> PnL labelled before costs
    price = row["size_usd"] / (tokens / 1e6)                       # USD per whole token paid (clip / tokens received)
    fill = {"tokens": tokens, "spend_lamports": spend, "sol_usd": fx, "state_at": state["at"], "price_usd": price,
            "exec_cost_sol": cost_sol, "exec_cost": "unknown" if cost_sol is None else "measured",
            "slippage_vs_verdict_pct": (price / row["verdict_ref_usd"] - 1) * 100 if row["verdict_ref_usd"] else None}
    refusal = entry_refusal(row, meta, price, at=t0 + ENTRY_DELAY_S, desk_dir=desk_dir, candidate_fn=candidate_fn)
    if refusal is not None:
        return {"state": "refused", "refusal": refusal, "decided_at": _iso(t0), "quote1_json": json.dumps(q1),
                "fill_json": json.dumps(fill)}
    at = _iso(t0 + ENTRY_DELAY_S)
    return {"state": "open", "decided_at": _iso(t0), "filled_at": at, "quote1_json": json.dumps(q1),
            "fill_json": json.dumps(fill), "entry_price_usd": price, "qty_atomic": str(int(tokens)), "entry_cost_usd": entry_cost_usd,   # qty: exact decimal TEXT
            "peak_price_usd": price, "peak_at": at, "last_mark_usd": price, "last_mark_at": at, "last_quote_at": at}


def _mint_decimals(conn, ticket_id):
    """The coin's decimals from the ticket's OWN hard-filter PASS evidence (the intake assembler's mint read), or None."""
    from sniper.persistence import db_memecoin_desk as store
    t = store.get_ticket(conn, ticket_id) or {}
    stage = next((s for s in t.get("stages") or [] if s.get("stage") == "hard_filter" and s.get("verdict") == "PASS"), None)
    d = ((((stage or {}).get("inputs") or {}).get("evidence") or {}).get("checks") or {}).get("token_safety", {}).get("decimals")
    return d if type(d) is int and 0 <= d <= 18 else None


def _ps_inputs_version(conn, cohort_id):
    """The immutable registered cohort selects new evidence semantics; fill JSON never grants that mode."""
    row = conn.execute("SELECT policy_json FROM desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
    version = json.loads(row[0]).get("ps_inputs") if row is not None else None
    if version not in (None, "pumpswap-facts-v1", "pumpswap-facts-v2"):
        raise ValueError("ps_inputs_version_unknown")
    return version


def _ps_integrity(conn, cohort_id):
    return _ps_inputs_version(conn, cohort_id) is not None


def _ps_capture_args(conn, row):
    if not _ps_integrity(conn, row["cohort_id"]):
        return {}
    from sniper.services.memecoin_desk_ps_tape import canonical_pool
    return {"pool": canonical_pool(row["mint"]), "integrity": True}


def _fill_ps(conn, desk_dir, row, t0, fx, meta, *, now, candidate_fn=None):
    """A graduated (PumpSwap) paper entry: Delta's exact pool buy (memecoin_desk_ps_paper.buy) at the decision +
    ENTRY_DELAY_S on committed tape, decimals from the ticket's own mint read (never 1e6). Unknown = no fill. The entry
    cost is the FULL clip (conservative: the unspent cash Delta reports is recorded, not credited)."""
    from sniper.services import memecoin_desk_ps_paper as PSP
    if fx is None:
        return {"state": "failed", "refusal": "entry_failed:sol_usd_stale", "decided_at": _iso(t0)}
    decimals = _mint_decimals(conn, row["ticket_id"])
    if decimals is None:
        return {"state": "failed", "refusal": "entry_failed:decimals_unknown", "decided_at": _iso(t0)}
    spend = int(row["size_usd"] / fx * 1e9)
    strict = {"integrity": True} if _ps_integrity(conn, row["cohort_id"]) else {}
    q1, why1 = PSP.buy(desk_dir, row["mint"], spend, t0, evaluated_at=now, **strict)  # decision quote, attribution only
    got, why = PSP.buy(desk_dir, row["mint"], spend, t0 + ENTRY_DELAY_S, evaluated_at=now, **strict)
    quote1 = {"tokens": int(q1["tokens_atomic"]) if q1 else None, "reason": why1, "at": t0}
    if got is None:
        return {"state": "failed", "refusal": "entry_failed:" + str(why), "decided_at": _iso(t0),
                "quote1_json": json.dumps(quote1)}
    tokens = int(got["tokens_atomic"])
    if tokens <= 0:
        return {"state": "failed", "refusal": "entry_failed:zero_out", "decided_at": _iso(t0), "quote1_json": json.dumps(quote1)}
    cost_sol = EXEC_COST_SOL["entry"]
    entry_cost_usd = row["size_usd"] + (cost_sol or 0.0) * fx
    price = row["size_usd"] / (tokens / 10 ** decimals)
    fill = {"venue": "pumpswap", "decimals": decimals, "tokens": tokens, "spend_lamports": int(got["spend_lamports"]),
            "cash_left_atomic": got.get("cash_left_atomic"), "clip_lamports": spend, "sol_usd": fx,
            "state_at": got["state_at"], "pool": got["pool"], "proof": got.get("proof"), "price_usd": price,
            "exec_cost_sol": cost_sol, "exec_cost": "unknown" if cost_sol is None else "measured",
            "slippage_vs_verdict_pct": (price / row["verdict_ref_usd"] - 1) * 100 if row["verdict_ref_usd"] else None}
    refusal = entry_refusal(row, meta, price, at=t0 + ENTRY_DELAY_S, desk_dir=desk_dir, candidate_fn=candidate_fn)
    if refusal is not None:
        return {"state": "refused", "refusal": refusal, "decided_at": _iso(t0), "quote1_json": json.dumps(quote1),
                "fill_json": json.dumps(fill)}
    at = _iso(t0 + ENTRY_DELAY_S)
    return {"state": "open", "decided_at": _iso(t0), "filled_at": at, "quote1_json": json.dumps(quote1),
            "fill_json": json.dumps(fill), "entry_price_usd": price, "qty_atomic": str(tokens), "entry_cost_usd": entry_cost_usd,
            "peak_price_usd": price, "peak_at": at, "last_mark_usd": price, "last_mark_at": at, "last_quote_at": at}


def _ps_position(row):
    """(True, decimals) for a PumpSwap paper position (fill_json.venue), else (False, 6)."""
    fill = json.loads(row.get("fill_json") or "{}")
    return (True, fill["decimals"]) if fill.get("venue") == "pumpswap" else (False, 6)


def _ps_sell(desk_dir, mint, qty, at, now, *, integrity=False, evidence=None):
    """(lamports | None, venue-or-reason) from Delta's exact pool sell; a proven 0 is PROVEN_ZERO. A priced quote's
    proof and binding_kind are copied into `evidence` (a dict) when given."""
    from sniper.services import memecoin_desk_ps_paper as PSP
    got, why = PSP.sell(desk_dir, mint, qty, at, evaluated_at=now, **({"integrity": True} if integrity else {}))
    if got is None:
        return None, str(why)
    if evidence is not None:
        evidence.update(proof=got.get("proof"), binding_kind=got.get("binding_kind"))
    lamports = int(got["lamports"])
    return (lamports, "pumpswap") if lamports > 0 else (None, PROVEN_ZERO)


def coin_age_s(candidate, at):
    """The coin's age at `at` from the emission fact (pumpfun create block time) plus the elapsed time; None if unknown."""
    field = (((candidate or {}).get("snapshot") or {}).get("fields") or {}).get("age_s") or {}
    age, known = field.get("value"), field.get("known_at")
    if type(age) in (int, float) and type(known) in (int, float) and math.isfinite(age) and field.get("completeness") == "complete":
        return float(age) + (at - float(known))
    return None


def entry_refusal(row, meta, quote_px, *, at, desk_dir=None, candidate_fn=None):
    """The funded executor's desk entry refusals, reproduced with the SAME rules and the same reason strings (so the
    chase-limit / LIMIT / drift paths parse them): coin-age floor (memecoin_risk.coin_age_refusal, desk entries carry no
    source), then LIMIT arm, fill drift, and the verdict cap with MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT
    (solana_swap_executor S6 block). quote_px = the paper fill's USD per token (the executor's exact-quote price)."""
    import sniper.settings.memecoin as cfg
    from sniper.services.memecoin_risk import coin_age_refusal
    cand = candidate_fn(row["ticket_id"]) if candidate_fn else None
    age = coin_age_s(cand, at) if cand is not None else None

    class _Cls:                       # the classifier's facts shape the age gate reads; unknown age admits (as real)
        facts = {"pair_age_s": age, "mint": row["mint"]}
    too_young = coin_age_refusal(_Cls(), row["network_id"], None)
    if too_young:
        return too_young
    cap = meta.get("verdict_price") or {}
    try:
        d_ref, d_cap = float(cap["ref_price_usd"]), float(cap["cap_pct"])
    except (KeyError, TypeError, ValueError):
        return "desk_verdict_price_missing"
    d_pct = (quote_px / d_ref - 1.0) * 100.0 if quote_px and d_ref > 0 else None
    net = (d_pct - float(cap["fee_pct"]) - float(cap["impact_pct"])) if d_pct is not None else None
    if cap.get("limit_arm") == 1 and net is not None and net > 0:
        return "desk limit watch: quote is %+.2f%% over the sheet price net of fee and impact" % net
    limit = cap.get("drift_limit_pct")
    if limit is not None and cap.get("drift_enforce") and net is not None and net > limit:
        return ("desk drift: quote is %+.2f%% over the verdict price net of fee and impact (limit %.2f%% = %.1f x noise %.3f%%)"
                % (net, limit, cap["drift_k"], cap["noise_pct"]))
    headroom = cfg.MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT
    if d_pct is None or d_pct > d_cap - headroom:
        return ("desk verdict price: exact quote is %s over the verdict price (cap %.2f%%%s)"
                % ("unknown" if d_pct is None else "%+.2f%%" % d_pct, d_cap,
                   ", min headroom %.2f%%" % headroom if d_pct is not None and d_pct <= d_cap else ""))
    return None


def qty_of(row):
    """The paper position's exact atomic quantity (stored as canonical decimal TEXT: no int64 limit, no float)."""
    raw = row["qty_atomic"]
    if not isinstance(raw, str) or not raw.isdigit() or str(int(raw)) != raw:
        raise ValueError("qty_atomic must be canonical decimal TEXT")
    return int(raw)


def _pos_view(row):
    """The runner-shaped position the rail function reads (alpha_b_real_exit_reason), desk rails as the override."""
    return {"entry_price": row["entry_price_usd"], "opened_at": row["filled_at"], "signal_source": "desk",
            "network_id": row["network_id"], "executable_peak_price": row["peak_price_usd"],
            "executable_peak_at": row["peak_at"], "last_quote_ts": row["last_quote_at"]}


def tick(conn, *, desk_dir, now, sol_usd=None, sol_at=None, risk_exits=None, fx_at=None):
    """Mark every open paper position with its exact executable sell price, apply the runner's rails (and any RISK
    exit decision for the ticket in `risk_exits`), and close at the exit + EXIT_DELAY_S exact sell."""
    from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason   # pure rail function, no executor use
    if conn.in_transaction:
        raise ValueError("transaction_active")
    started = time.monotonic()
    ensure(conn)  # additive schema setup precedes every close/latch transaction
    fx_of = _fx_fn(sol_usd, sol_at, fx_at)
    out, fx, marks, lost = [], fx_of(now), [], 0
    risk_exits = risk_exits or {}
    for row in conn.execute("SELECT * FROM desk_paper_positions WHERE state='open'").fetchall():
        row = dict(row)
        if row["exit_requested_at"]:
            out += _close_if_due(conn, desk_dir, row, now, fx_of, started=started)
            continue
        qty = qty_of(row)
        ps, decimals = _ps_position(row)
        evidence = {}
        if ps:                                    # graduated coin: Delta's exact pool sell at the committed PS cut
            capture_args = _ps_capture_args(conn, row)
            cut = _cut(desk_dir, now, "ps", **capture_args)
            strict = {"integrity": True} if capture_args else {}
            lamports, venue, at = ((None, "ps_capture_unproven", now) if cut is None
                                   else (*_ps_sell(desk_dir, row["mint"], qty, min(now, cut), now, evidence=evidence,
                                                   **strict), min(now, cut)))
        else:
            lamports, venue, at = _sell_on_committed_tape(desk_dir, row["mint"], qty, now)
        mid = (lamports / 1e9 * fx_of(at)) / (qty / 10 ** decimals) if lamports and fx_of(at) else None
        updates = {}
        if ps and venue == PROVEN_ZERO:           # same mark semantics as a proven-zero close attempt
            updates.update(last_mark_usd=0, last_mark_at=_iso(at))
        if mid is not None:                       # marked at the committed cut, never from a row past it (Codex frontier)
            updates.update(last_mark_usd=mid, last_mark_at=_iso(at), last_quote_at=_iso(at))
            if mid > (row["peak_price_usd"] or 0):
                updates.update(peak_price_usd=mid, peak_at=_iso(at))
        merged = {**row, **updates}
        reason = risk_exits.get(row["ticket_id"]) or alpha_b_real_exit_reason(
            _pos_view(merged), mid, _iso(now), override=json.loads(row["rails_json"]))
        if reason:
            updates.update(exit_reason=reason, exit_requested_at=_iso(now))
        try:                                  # building the log row (iso/hash/JSON) never touches the mark
            marks.append(_mark_row(row, at, lamports, venue, evidence))
        except Exception:
            lost += 1
            MARK_LOG_ERRORS["mark_log_errors"] += 1
            log.exception("desk paper mark log row build failed position=%s", row["id"])
        if updates:
            conn.execute("UPDATE desk_paper_positions SET " + ",".join("%s=?" % k for k in updates) + " WHERE id=?",
                         (*updates.values(), row["id"]))
            conn.commit()  # the next position's quote must not inherit this mark's writer lock
        if reason:
            out.append({"ticket_id": row["ticket_id"], "state": "exit_requested", "reason": reason})
    conn.commit()
    _write_marks(conn, marks, lost=lost)
    if now - _LAST_PRUNE[0] >= 3600:      # retention runs at most hourly, after every mark of this tick is written
        _LAST_PRUNE[0] = now
        try:
            prune_marks(conn)
        except Exception:
            MARK_LOG_ERRORS["mark_log_errors"] += 1
            log.exception("desk paper mark log prune failed")
    return out


def _sell_on_committed_tape(desk_dir, mint, qty, now):
    """(lamports, venue, at): the exact sell at the committed curve cut (<= now); a PumpSwap answer also needs the
    PS cut to reach `at`. (None, reason, at) when the tape is not committed: unknown, never a value."""
    cut = _cut(desk_dir, now, "curve")
    if cut is None:
        return None, "capture_unproven", now
    at = min(now, cut)
    lamports, venue = sell_proceeds(desk_dir, mint, qty, at)
    if venue == "pumpswap" or (lamports is None and str(venue).startswith("pool")):
        ps = _cut(desk_dir, now, "ps")
        if ps is None or ps < at:
            return None, "ps_capture_unproven", at
    return lamports, venue, at


def _close_if_due(conn, desk_dir, row, now, fx_of, *, started=None):
    """Value the exit at the request + EXIT_DELAY_S; a failed attempt is retried at each LATER tick's own time (like the
    funded exit retry), never re-read at the same frozen moment. Unknown FX =
    the price is unknown (mark unchanged, never 0). Only a proven zero sell (PROVEN_ZERO) marks 0; missing state, a
    tape gap, an unknown pool or a quote refusal is unknown value (mark and age unchanged).
    Every attempt is logged in exit_json.attempts."""
    from sniper.persistence import db_memecoin_desk as store
    started = time.monotonic() if started is None else started
    delay = exit_delay_s(row["exit_reason"])
    first = _ts(row["exit_requested_at"]) + delay
    if now < first:
        return []
    prior = json.loads(row["exit_json"] or "{}").get("attempts", [])
    ps_pos, _ = _ps_position(row)
    capture_args = _ps_capture_args(conn, row) if ps_pos else {}
    cut = _cut(desk_dir, now, "ps" if ps_pos else "curve", **capture_args)
    if cut is None or cut < first:
        return []                         # the exit moment is not on committed tape yet: wait, no attempt is spent
    t_exit = first if not prior else max(first, min(now, cut))
    fx = fx_of(t_exit)
    evidence = {}                         # the PS exit quote's proof + binding_kind, stored on new closes only
    if ps_pos:                            # graduated coin: Delta's exact full sell, the PS cut already reaches t_exit
        strict = {"integrity": True} if capture_args else {}
        lamports, venue = _ps_sell(desk_dir, row["mint"], qty_of(row), t_exit, now, evidence=evidence, **strict)
    else:
        lamports, venue = sell_proceeds(desk_dir, row["mint"], qty_of(row), t_exit)
    if not ps_pos and (venue == "pumpswap" or (lamports is None and str(venue).startswith("pool"))):
        ps = _cut(desk_dir, now, "ps")
        if ps is None or ps < t_exit:
            return []                     # PS tape not committed to the exit moment: wait, no attempt spent (Codex e2e 1)
    if not lamports or fx is None:
        attempt = {"at": t_exit, "reason": "fx_unknown" if lamports and fx is None else venue}
        attempts = prior + [attempt]
        if venue == PROVEN_ZERO:          # the exact pool sell from proven state returns 0 SOL: a known zero value
            changed = conn.execute("UPDATE desk_paper_positions SET last_mark_usd=0, last_mark_at=?, exit_json=? "
                         "WHERE id=? AND state='open' AND exit_requested_at=? AND exit_reason=?",
                         (_iso(t_exit), json.dumps({"attempts": attempts}), row["id"],
                          row["exit_requested_at"], row["exit_reason"])).rowcount
        else:                             # unknown FX, no state, tape gap, unknown pool, quote refusal: value UNKNOWN,
            changed = conn.execute("UPDATE desk_paper_positions SET exit_json=? WHERE id=? AND state='open' "
                         "AND exit_requested_at=? AND exit_reason=?",
                         (json.dumps({"attempts": attempts}), row["id"], row["exit_requested_at"], row["exit_reason"])).rowcount
        conn.commit()
        return [{"ticket_id": row["ticket_id"], "state": "exit_retry", "reason": attempt["reason"]}] if changed else []
    cost_sol = EXEC_COST_SOL["exit"]
    exit_usd = lamports / 1e9 * fx - (cost_sol or 0.0) * fx      # cost unknown -> PnL labelled before costs
    pnl = exit_usd - row["entry_cost_usd"]
    exit_json = json.dumps({"lamports": lamports, "venue": venue, "sol_usd": fx, "at": t_exit,
                           "exec_cost_sol": cost_sol, "attempts": prior,
                           "exit_delay_s": t_exit - _ts(row["exit_requested_at"]),
                           "delay_kind": "risk" if delay == RISK_EXIT_DELAY_S else "rails",
                           "pnl_basis": "before network/priority costs" if cost_sol is None
                           or EXEC_COST_SOL["entry"] is None else PNL_BASIS, **evidence})
    # No quote/read-only evidence work is inside this paper-only lock. Close and crossing latch are indivisible.
    with store._write(conn):
        current = conn.execute("SELECT state,exit_requested_at,exit_reason FROM desk_paper_positions WHERE id=?",
                               (row["id"],)).fetchone()
        if current is None or tuple(current) != ("open", row["exit_requested_at"], row["exit_reason"]):
            return []
        conn.execute("UPDATE desk_paper_positions SET state='closed',closed_at=?,exit_usd=?,pnl_usd=?,pnl_pct=?,exit_json=? "
                     "WHERE id=?", (_iso(t_exit), exit_usd, pnl, pnl / row["entry_cost_usd"] * 100, exit_json, row["id"]))
        known_at = _iso(now + max(0.0, time.monotonic() - started))
        _cohort_refusal(conn, row["cohort_id"], now_iso=known_at)
    return [{"ticket_id": row["ticket_id"], "state": "closed", "reason": row["exit_reason"], "pnl_usd": pnl}]


# ------------------------------------------------------------------ PAPER Telegram cards (operator add-on 10-26)
SUPPLY_TOKENS = 1_000_000_000          # Pump.fun fixed supply (whole tokens): mcap = price x supply


def _short(mint):
    return "%s…%s" % (mint[:4], mint[-4:])


def _rails(row):
    r = json.loads(row["rails_json"])
    return "TP %s%% / SL %s%% / trail %s" % (r.get("tp_pct"), r.get("sl_pct"),
                                             "%s%%" % r["trail_pct"] if r.get("trail_pct") is not None else "off")


def entry_card(row, judge_code=None):
    fill = json.loads(row["fill_json"] or "{}")
    slip = fill.get("slippage_vs_verdict_pct")
    return "\n".join([
        "📝 PAPER — desk ENTRY (no real money)",
        "coin %s  mcap %s USD" % (_short(row["mint"]), "{:,.0f}".format(row["entry_price_usd"] * SUPPLY_TOKENS)),
        "paper fill %.4g USD/token (exact curve buy at decision + %.1f s)%s" % (
            row["entry_price_usd"], ENTRY_DELAY_S, "" if slip is None else ", %+.1f%% vs verdict" % slip),
        "clip %.2f USD  rails %s" % (row["size_usd"], _rails(row)),
        "JUDGE reason code %s  ticket %s" % (judge_code, row["ticket_id"])])


def end_card(row):
    if row["state"] == "closed":
        held = _ts(row["closed_at"]) - _ts(row["filled_at"])
        return "\n".join([
            "📝 PAPER — desk EXIT %s (no real money)" % ("WIN" if row["pnl_usd"] > 0 else "LOSS"),
            "coin %s  exit %s" % (_short(row["mint"]), row["exit_reason"]),
            "paper PnL %+.2f USD (%+.1f%%)  held %dm%02ds%s" % (row["pnl_usd"], row["pnl_pct"], held // 60, held % 60,
                "  (before network/priority costs)" if EXEC_COST_SOL["entry"] is None or EXEC_COST_SOL["exit"] is None
                else "  (%s)" % PNL_BASIS),
            "ticket %s" % row["ticket_id"]])
    return "\n".join([
        "📝 PAPER — desk entry %s (no real money)" % ("REFUSED" if row["state"] == "refused" else "FAILED"),
        "coin %s  reason %s" % (_short(row["mint"]), row["refusal"]), "ticket %s" % row["ticket_id"]])


def send_cards(conn, *, send_fn, judge_code_fn=lambda ticket_id: None, now_iso):
    """Send each PAPER card once (entry on open; result on close, refusal or failure). A failed send is retried next pass."""
    sent = []
    for row in conn.execute("SELECT * FROM desk_paper_positions WHERE entry_card_at IS NULL AND filled_at IS NOT NULL").fetchall():
        if _ok(send_fn(entry_card(dict(row), judge_code_fn(row["ticket_id"])), kind="desk_paper_entry", source_id=row["ticket_id"])):
            conn.execute("UPDATE desk_paper_positions SET entry_card_at=? WHERE id=?", (now_iso, row["id"]))
            sent.append((row["ticket_id"], "entry"))
    for row in conn.execute("SELECT * FROM desk_paper_positions WHERE end_card_at IS NULL AND state IN "
                            "('closed','refused','failed')").fetchall():
        if _ok(send_fn(end_card(dict(row)), kind="desk_paper_result", source_id=row["ticket_id"])):
            conn.execute("UPDATE desk_paper_positions SET end_card_at=? WHERE id=?", (now_iso, row["id"]))
            sent.append((row["ticket_id"], "end"))
    conn.commit()
    return sent


def _ok(result):
    return bool(result[0]) if isinstance(result, tuple) else bool(result)


# ------------------------------------------------------------------ RISK seat for PAPER positions (go-live blocker 2)
RISK_OUT_DB = "risk_live_paper.db"
APPLIES_SQL = ("CREATE TABLE IF NOT EXISTS desk_paper_risk_applies(decision_id INTEGER PRIMARY KEY, position_id INTEGER, "
               "action TEXT, status TEXT, reason TEXT, applied_at TEXT, rails_before TEXT, rails_after TEXT)")


def risk_positions(desk_db, *, now=None):
    """The open PAPER positions in the RISK watcher's (pos, policy, rail_ids, mark) shape, read-only. `policy` is the
    desk rails plus the funded defaults the real desk position carries (catastrophe, timecap); `mark` is the paper
    engine's exact executable value (sweep_ts = its mark time)."""
    import sqlite3
    import sniper.settings.memecoin as cfg
    conn = sqlite3.connect("file:%s?mode=ro" % desk_db, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='desk_paper_positions'").fetchone():
            return []
        out = []
        for row in conn.execute("SELECT * FROM desk_paper_positions WHERE state='open' AND exit_requested_at IS NULL"):
            row = dict(row)
            rails = json.loads(row["rails_json"])
            try:
                ps_inputs = _ps_inputs_version(conn, row["cohort_id"])
            except ValueError as exc:
                if str(exc) != "ps_inputs_version_unknown":
                    raise
                ps_inputs = "ps_inputs_version_unknown"  # Keep this row blind without hiding other positions.
            strict = ps_inputs is not None
            if strict:
                fill = json.loads(row["fill_json"] or "{}")
                decimals = fill.get("decimals") if isinstance(fill, dict) and fill.get("venue") == "pumpswap" else None
                decimals = decimals if type(decimals) is int and 0 <= decimals <= 18 else None
                qty = qty_of(row) / 10 ** decimals if decimals is not None else None
            else:
                qty = qty_of(row) / 10 ** _ps_position(row)[1]  # legacy unit/shape unchanged
            pos = {"id": row["id"], "mint": row["mint"], "network_id": row["network_id"], "opened_at": row["filled_at"],
                   "entry_price": row["entry_price_usd"], "entry_notional": row["entry_cost_usd"], "signal_source": "desk",
                   "execution_mode": "paper", "executable_peak_price": row["peak_price_usd"], "open_qty": qty,
                   "open_qty_known": qty is not None, "executed_pool": None, "desk_cohort_id": row["cohort_id"],
                   "paper_ticket_id": row["ticket_id"]}
            if strict:
                pos.update(desk_ps_inputs=ps_inputs, desk_base_decimals=decimals)
            policy = {"stop_pct": rails.get("sl_pct"), "tp_pct": rails.get("tp_pct"), "trail_pct": rails.get("trail_pct"),
                      "catastrophe_pct": cfg.MEMECOIN_REAL_CATASTROPHE_PCT, "max_hold_s": cfg.MEMECOIN_REAL_MAX_HOLD_S}
            mark = ({"executable_exit_value": row["last_mark_usd"] * qty, "sweep_ts": row["last_mark_at"],
                     "unit_price": row["last_mark_usd"], "open_qty": qty}
                    if qty is not None and row["last_mark_usd"] and row["last_mark_at"] else None)
            out.append((pos, policy, [], mark))
        return out
    finally:
        conn.close()


def risk_orders(conn, *, desk_dir, now):
    """Apply the PAPER RISK watcher's live, unexpired, unapplied decisions: EXIT -> the exit (reason desk_risk_exit);
    TIGHTEN -> stricter rails only (a field that loosens is ignored); LOOSEN_TRAIL -> the trail it names. Each decision
    gets one ack row. Simplification vs the funded apply (memecoin_desk_risk_apply): no exact-quote re-confirmation of
    a tighten that would trigger at once; such a tighten fires on the next tick through the rails, named."""
    import sqlite3
    conn.execute(APPLIES_SQL)
    path = os.path.join(str(desk_dir), RISK_OUT_DB)
    if not os.path.exists(path):
        return {}
    src = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute("SELECT id, position_id, action, rails_json, decided_at, expires_at FROM risk_decisions "
                           "WHERE live=1 AND action IN ('EXIT','TIGHTEN','LOOSEN_TRAIL') ORDER BY id").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        src.close()
    exits = {}
    for d in rows:
        if conn.execute("SELECT 1 FROM desk_paper_risk_applies WHERE decision_id=?", (d["id"],)).fetchone():
            continue
        pos = conn.execute("SELECT * FROM desk_paper_positions WHERE id=?", (d["position_id"],)).fetchone()
        status, reason, before, after = "refused", None, None, None
        if pos is None or pos["state"] != "open":
            reason = "position_not_open"
        elif not (type(d["decided_at"]) in (int, float) and type(d["expires_at"]) in (int, float)
                  and d["decided_at"] <= now < d["expires_at"]):
            reason = "decision_expired"
        elif pos["exit_requested_at"]:
            reason = "exit_already_requested"
        elif d["action"] == "EXIT":
            # Codex P1: the exit request is written WITH its ack in one transaction; a crash before the next tick can
            # never lose it (tick closes any exit_requested_at row)
            conn.execute("UPDATE desk_paper_positions SET exit_reason=?, exit_requested_at=? WHERE id=?",
                         (RISK_EXIT_REASON, _iso(now), pos["id"]))
            exits[pos["ticket_id"]], status = RISK_EXIT_REASON, "applied"
        else:
            before = json.loads(pos["rails_json"])
            asked = json.loads(d["rails_json"] or "{}")
            after = dict(before)
            for k in ("sl_pct", "tp_pct", "trail_pct"):
                v = asked.get(k)
                if type(v) not in (int, float):
                    continue
                if d["action"] == "LOOSEN_TRAIL" and k == "trail_pct":
                    after[k] = v
                elif d["action"] == "TIGHTEN" and (before.get(k) is None or v < before[k]):
                    after[k] = v                      # tighter = a smaller stop distance, a nearer TP, a smaller trail
            status, reason = ("applied", None) if after != before else ("refused", "not_stricter")
            if status == "applied":
                conn.execute("UPDATE desk_paper_positions SET rails_json=? WHERE id=?", (json.dumps(after), pos["id"]))
        conn.execute("INSERT INTO desk_paper_risk_applies VALUES (?,?,?,?,?,?,?,?)",
                     (d["id"], d["position_id"], d["action"], status, reason, _iso(now),
                      json.dumps(before) if before else None, json.dumps(after) if after else None))
        conn.commit()                       # per decision: the ack and its position change land together
    return exits
