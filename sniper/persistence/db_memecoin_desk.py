"""Immutable desk evidence and handoff records in an explicit, dedicated SQLite file."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3


_ACTIVE = ("open", "waiting", "queued", "unresolved", "filled")
_TERMINAL = ("killed", "skipped", "refused", "expired", "closed")
_WAIT_STATES = ("expired", "triggered_refused", "triggered_failed",
                "triggered_submitted_unknown", "triggered_filled")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name)
    return value


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("invalid_utc_timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid_utc_timestamp")
    return parsed.astimezone(timezone.utc).isoformat()


def _number(value, name):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                              or not math.isfinite(value) or value < 0):
        raise ValueError(name)
    return value


@contextmanager
def _write(conn):
    if conn.in_transaction:
        raise ValueError("transaction_active")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def record_judge_prompt(conn, *, text, prompt_version, seen_at):
    """The exact JUDGE system prompt bytes, once per hash (replay provenance; eCat 2026-10-23 lost historical prompt bytes).
    Returns the sha256 the call results carry. Idempotent; a hash that already exists is never rewritten."""
    _text(text, "text")
    _text(prompt_version, "prompt_version")
    digest = hashlib.sha256(text.encode()).hexdigest()
    with _write(conn):
        conn.execute("INSERT OR IGNORE INTO desk_judge_prompts VALUES (?,?,?,?)", (digest, prompt_version, text, _time(seen_at)))
    return digest


def connect(path):
    """Open only the caller-supplied desk path; no production path or ledger import."""
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name IN "
                        "('memecoin_positions','memecoin_pending_entries','memecoin_manual_intents') "
                        "LIMIT 1").fetchone() is not None:
            raise ValueError("funded_database")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_tables(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def ensure_tables(conn):
    """Create the desk schema in a short transaction; never commit caller work."""
    schema = """
    CREATE TABLE IF NOT EXISTS desk_cohorts (
        cohort_id TEXT PRIMARY KEY, producer TEXT NOT NULL CHECK(producer IN ('H','U')),
        prompt_version TEXT NOT NULL, policy_json TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS desk_snapshots (
        snapshot_hash TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS desk_ticket_heads (
        ticket_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL UNIQUE,
        cohort_id TEXT NOT NULL REFERENCES desk_cohorts(cohort_id),
        producer TEXT NOT NULL CHECK(producer IN ('H','U')), network_id TEXT NOT NULL,
        mint TEXT NOT NULL, pool TEXT, snapshot_hash TEXT NOT NULL REFERENCES desk_snapshots(snapshot_hash),
        emitted_at TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','waiting','queued','unresolved','filled',
                                             'killed','skipped','refused','expired','closed')));
    CREATE UNIQUE INDEX IF NOT EXISTS idx_desk_active_coin ON desk_ticket_heads(network_id,mint)
        WHERE state IN ('open','waiting','queued','unresolved','filled');
    CREATE INDEX IF NOT EXISTS idx_desk_head_coin ON desk_ticket_heads(network_id,mint,created_at);
    CREATE TABLE IF NOT EXISTS desk_candidate_emissions (
        opportunity_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES desk_ticket_heads(ticket_id),
        producer TEXT NOT NULL CHECK(producer IN ('H','U')),
        cohort_id TEXT NOT NULL REFERENCES desk_cohorts(cohort_id), network_id TEXT NOT NULL,
        mint TEXT NOT NULL, pool TEXT, emitted_at TEXT NOT NULL, received_at TEXT NOT NULL,
        snapshot_hash TEXT NOT NULL REFERENCES desk_snapshots(snapshot_hash), candidate_json TEXT NOT NULL,
        is_primary INTEGER NOT NULL CHECK(is_primary IN (0,1)), reason TEXT,
        gate_state TEXT CHECK(gate_state IN ('pass','skip')));
    CREATE TABLE IF NOT EXISTS desk_intake_source_receipts (
        opportunity_id TEXT PRIMARY KEY REFERENCES desk_candidate_emissions(opportunity_id),
        cohort_id TEXT NOT NULL REFERENCES desk_cohorts(cohort_id), hard_policy_hash TEXT NOT NULL,
        candidate_hash TEXT NOT NULL, known_at REAL NOT NULL, receipt_hash TEXT NOT NULL, receipt_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS desk_tickets (
        id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES desk_ticket_heads(ticket_id),
        opportunity_id TEXT NOT NULL, cohort_id TEXT NOT NULL REFERENCES desk_cohorts(cohort_id),
        producer TEXT NOT NULL CHECK(producer IN ('H','U')), network_id TEXT NOT NULL,
        mint TEXT NOT NULL, pool TEXT, stage TEXT NOT NULL, seat TEXT NOT NULL,
        verdict TEXT NOT NULL CHECK(verdict IN ('PASS','SKIP','KILL','ENTER','WAIT','HOLD','TIGHTEN','EXIT')),
        reason TEXT NOT NULL, inputs_hash TEXT NOT NULL, inputs_json TEXT NOT NULL, output_json TEXT,
        model TEXT, prompt_version TEXT, latency_ms REAL CHECK(latency_ms>=0),
        gate_state TEXT CHECK(gate_state IN ('pass','skip')), gate_inputs_json TEXT,
        gate_evaluated_at TEXT, response_at TEXT, trigger_at TEXT,
        wait_state TEXT CHECK(wait_state IN ('expired','triggered_refused','triggered_failed',
                                             'triggered_submitted_unknown','triggered_filled')),
        cost_usd REAL CHECK(cost_usd>=0), intent_id TEXT, position_id INTEGER, ts TEXT NOT NULL,
        UNIQUE(ticket_id,stage), UNIQUE(network_id,mint,seat,inputs_hash));
    CREATE INDEX IF NOT EXISTS idx_desk_tickets_coin ON desk_tickets(network_id,mint,ts);
    CREATE INDEX IF NOT EXISTS idx_desk_tickets_verdict ON desk_tickets(verdict,ts);
    CREATE INDEX IF NOT EXISTS idx_desk_tickets_stage_ts ON desk_tickets(stage,ts);
    CREATE TABLE IF NOT EXISTS desk_outbox (
        ticket_id TEXT PRIMARY KEY REFERENCES desk_ticket_heads(ticket_id), intent_id TEXT NOT NULL UNIQUE,
        requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL, request_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','sent','expired','refused')),
        resolved_at TEXT, reason TEXT);
    -- JUDGE pre-quote hints (Alpha 2026-10-23, option A): written when a JUDGE call STARTS, read by the
    -- runner pre-quote thread. A hint never reserves, signs or authorizes, and expires with the call.
    CREATE TABLE IF NOT EXISTS desk_prequote_hints (
        ticket_id TEXT PRIMARY KEY, network_id TEXT NOT NULL, mint TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_desk_prequote_hints_expiry ON desk_prequote_hints(expires_at);
    CREATE TABLE IF NOT EXISTS desk_judge_prompts (
        sha256 TEXT PRIMARY KEY, prompt_version TEXT NOT NULL, text TEXT NOT NULL, first_seen_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS desk_judge_calls (
        call_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE REFERENCES desk_ticket_heads(ticket_id),
        worker TEXT NOT NULL, claimed_at TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('claimed','responded')),
        call_started_at TEXT, response_at TEXT, elapsed_ms REAL CHECK(elapsed_ms>=0), result_json TEXT,
        result_session_id TEXT, result_id TEXT);
    CREATE INDEX IF NOT EXISTS idx_desk_judge_calls_claimed ON desk_judge_calls(claimed_at);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_desk_judge_calls_result ON desk_judge_calls(result_session_id,result_id)
        WHERE result_id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS desk_arm_admissions (
        ticket_id TEXT NOT NULL REFERENCES desk_ticket_heads(ticket_id),
        arm TEXT NOT NULL CHECK(arm IN ('F','SJ','SR')),
        decision TEXT NOT NULL CHECK(decision IN ('taken','skipped_full','latched','halted','not_selected')),
        decided_at TEXT NOT NULL, PRIMARY KEY(ticket_id,arm));
    """
    with _write(conn):
        for statement in schema.split(";"):
            if statement.strip():
                conn.execute(statement)
        for table in ("desk_cohorts", "desk_snapshots", "desk_candidate_emissions", "desk_intake_source_receipts",
                      "desk_tickets", "desk_arm_admissions"):
            for operation in ("UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                             f"BEFORE {operation} ON {table} BEGIN "
                             "SELECT RAISE(ABORT,'immutable_desk_record'); END")


        # the FIRST pre-expiry cost-gate hold and its facts (Codex P2, 2026-10-22): an expiry is classified from
        # this durable evidence, never from the gate re-read after the TTL
        outbox_cols = {r[1] for r in conn.execute("PRAGMA table_info(desk_outbox)")}
        for column in ("first_hold_at", "first_hold_json"):
            if column not in outbox_cols:
                conn.execute("ALTER TABLE desk_outbox ADD COLUMN %s TEXT" % column)
        conn.execute("CREATE TRIGGER IF NOT EXISTS desk_judge_calls_no_delete BEFORE DELETE ON desk_judge_calls "
                     "BEGIN SELECT RAISE(ABORT,'immutable_desk_record'); END")
        conn.execute("CREATE TRIGGER IF NOT EXISTS desk_judge_calls_once BEFORE UPDATE ON desk_judge_calls "
                     "WHEN OLD.state <> 'claimed' OR NEW.state <> 'responded' OR NEW.call_id <> OLD.call_id "
                     "OR NEW.ticket_id <> OLD.ticket_id BEGIN SELECT RAISE(ABORT,'immutable_desk_record'); END")


def register_cohort(conn, *, cohort_id, producer, prompt_version, policy, created_at):
    _text(cohort_id, "cohort_id")
    _text(prompt_version, "prompt_version")
    if producer not in ("H", "U") or not isinstance(policy, dict):
        raise ValueError("cohort_configuration")
    row = {"cohort_id": cohort_id, "producer": producer, "prompt_version": prompt_version,
           "policy_json": _json(policy), "created_at": _time(created_at)}
    with _write(conn):
        existing = conn.execute("SELECT * FROM desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
        if existing is not None:
            if dict(existing) != row:
                raise ValueError("cohort_conflict")
        else:
            conn.execute("INSERT INTO desk_cohorts VALUES (:cohort_id,:producer,:prompt_version,"
                         ":policy_json,:created_at)", row)
    return row


def _stage_row(row):
    result = dict(row)
    result["inputs"] = json.loads(result["inputs_json"])
    result["output"] = json.loads(result["output_json"]) if result["output_json"] is not None else None
    return result


def get_cohort(conn, cohort_id):
    row = conn.execute("SELECT * FROM desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
    return {**dict(row), "policy": json.loads(row["policy_json"])} if row is not None else None


def get_ticket(conn, ticket_id):
    row = conn.execute("SELECT * FROM desk_ticket_heads WHERE ticket_id=?", (ticket_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    snapshot = conn.execute("SELECT snapshot_json FROM desk_snapshots WHERE snapshot_hash=?",
                            (result["snapshot_hash"],)).fetchone()
    result["snapshot"] = json.loads(snapshot[0])
    emission = conn.execute("SELECT candidate_json FROM desk_candidate_emissions WHERE opportunity_id=? "
                            "AND is_primary=1", (result["opportunity_id"],)).fetchone()
    result["candidate"] = json.loads(emission[0])
    result["candidate"].pop("snapshot_hash", None)
    result["stages"] = [_stage_row(stage) for stage in conn.execute(
        "SELECT * FROM desk_tickets WHERE ticket_id=? ORDER BY id", (ticket_id,))]
    return result


def _close_pnl(conn, ticket_id):
    row = conn.execute("SELECT json_extract(output_json,'$.realized_pnl') FROM desk_tickets WHERE ticket_id=? "
                       "AND stage='close' ORDER BY ts DESC,rowid DESC LIMIT 1", (ticket_id,)).fetchone()
    return row[0] if row is not None and type(row[0]) in (int, float) else None


def _tripwire_tripped(conn, cohort_id):
    """The operator's loss re-entry tripwire, in code: at least TRIPWIRE_MIN_REENTRIES closed re-entries that followed
    a LOSING close, with a mean realized_pnl below the first-entry mean minus one clip loss. Cohort-scoped."""
    firsts, after_loss = [], []
    for ticket_id, prev in conn.execute(
            "SELECT h.ticket_id, json_extract(e.candidate_json,'$.reentry.prev_position_id') FROM desk_ticket_heads h "
            "JOIN desk_candidate_emissions e ON e.opportunity_id=h.opportunity_id AND e.is_primary=1 "
            "WHERE h.cohort_id=? AND h.state='closed'", (cohort_id,)).fetchall():
        pnl = _close_pnl(conn, ticket_id)
        if pnl is None:
            continue
        if prev is None:
            firsts.append(pnl)
            continue
        src = conn.execute("SELECT ticket_id FROM desk_tickets WHERE stage='close' AND position_id=? LIMIT 1",
                           (prev,)).fetchone()
        prev_pnl = _close_pnl(conn, src[0]) if src else None
        if prev_pnl is not None and prev_pnl <= 0:
            after_loss.append(pnl)
    if len(after_loss) < TRIPWIRE_MIN_REENTRIES or not firsts:
        return False
    return sum(after_loss) / len(after_loss) < sum(firsts) / len(firsts) - TRIPWIRE_CLIP_LOSS_USD


def close_of_position(conn, position_id):
    """v23 reentry facts: the desk close output of `position_id` with closed_at as epoch seconds ('closed_at_s'), or None.
    Read from the desk DB close stage only (never the ledger)."""
    row = conn.execute("SELECT output_json FROM desk_tickets WHERE stage='close' AND position_id=? "
                       "ORDER BY ts DESC,rowid DESC LIMIT 1", (position_id,)).fetchone()
    if row is None:
        return None
    close = json.loads(row[0])
    try:
        close["closed_at_s"] = datetime.fromisoformat(_time(close.get("closed_at"))).timestamp()
    except (TypeError, ValueError):
        close["closed_at_s"] = None
    return close


def _reentry_refusal(conn, head, reentry, now_iso, cap, window_s, emitted_at, rule=None):
    """None when a winner re-entry may skip the cooldown: the mint's latest ticket closed with a recorded
    realized_pnl > 0 on the claimed position at the claimed exit price, and fewer than `cap` re-entries
    opened on the mint in the last hour (durable: counted from the emissions table)."""
    rule = rule or {}
    if (reentry.get("trigger") == "watch") != (rule.get("version") == REENTRY_WATCH_VERSION):
        return "reentry_trigger"           # a watch claim only in a v3 cohort, and a v3 cohort takes nothing else
    if head["state"] != "closed":
        return "reentry_not_closed"
    row = conn.execute("SELECT output_json FROM desk_tickets WHERE ticket_id=? AND stage='close' "
                       "ORDER BY ts DESC,rowid DESC LIMIT 1", (head["ticket_id"],)).fetchone()
    close = json.loads(row[0]) if row is not None else {}
    pnl = close.get("realized_pnl")
    if type(pnl) not in (int, float) or not math.isfinite(pnl):
        return "reentry_not_winner"
    soft = pnl <= 0 and rule.get("loss_soft") is True and close.get("exit_reason") in SOFT_LOSS_EXITS
    if not pnl > 0 and not soft:
        return "reentry_not_winner"
    price = close.get("exit_price_usd") if pnl > 0 else (close.get("entry_fill_price_usd")
                                                         if close.get("exit_price_usd") is not None else None)
    if close.get("position_id") != reentry["prev_position_id"] or price is None or price != reentry["exit_price_usd"]:
        return "reentry_unproven"
    if soft and _tripwire_tripped(conn, head["cohort_id"]):
        return "reentry_tripwire"
    now = datetime.fromisoformat(now_iso)
    try:
        closed_at = datetime.fromisoformat(_time(close.get("closed_at")))
    except (TypeError, ValueError):
        return "reentry_unproven"
    trigger = datetime.fromisoformat(emitted_at)
    if not closed_at < trigger <= now or (trigger - closed_at).total_seconds() > window_s:
        return "reentry_window"            # same UTC clock: close < trigger <= now, trigger within window
    recent = conn.execute("SELECT received_at, json_extract(candidate_json,'$.reentry.prev_position_id') "
                          "FROM desk_candidate_emissions WHERE network_id=? AND mint=? AND is_primary=1 "
                          "AND json_extract(candidate_json,'$.reentry') IS NOT NULL",
                          (head["network_id"], head["mint"])).fetchall()
    if any(pid == reentry["prev_position_id"] for _, pid in recent):
        return "reentry_consumed"          # one chance per winning close; a JUDGE SKIP or refusal spends it
    if sum(1 for at, _ in recent if (now - datetime.fromisoformat(at)).total_seconds() < 3600) >= cap:
        return "reentry_cap"
    return None


SOFT_LOSS_EXITS = ("desk_risk_exit", "real_trail")      # v2: a loss by these exits may re-arm (operator 2026-10-23)
TRIPWIRE_MIN_REENTRIES, TRIPWIRE_CLIP_LOSS_USD = 10, 25.0 * 0.18   # as memecoin_desk_report.reentry_section


REENTRY_SOFT_VERSIONS = ("winner-reentry-v2", "winner-reentry-v3")
REENTRY_WATCH_VERSION = "winner-reentry-v3"      # v23: the claim's trigger is "watch" and JUDGE sets the entry trigger


def _reentry_policy(policy_json):
    """The cohort's frozen winner_reentry {"version", "per_hour", "window_s"} (v1), or v2/v3 with "loss_soft": true,
    else None (re-entry off). v3 = re-entry as a WATCH: only trigger "watch" claims (and v1/v2 refuse them)."""
    rule = json.loads(policy_json).get("winner_reentry")
    keys = {"version", "per_hour", "window_s"} | ({"loss_soft"} if isinstance(rule, dict) and
                                                  rule.get("version") in REENTRY_SOFT_VERSIONS else set())
    if (not isinstance(rule, dict) or set(rule) != keys
            or (keys != {"version", "per_hour", "window_s"} and rule["loss_soft"] is not True)
            or not isinstance(rule["version"], str) or type(rule["per_hour"]) is not int or rule["per_hour"] < 1
            or type(rule["window_s"]) not in (int, float) or not math.isfinite(rule["window_s"])
            or rule["window_s"] <= 0):
        return None
    return rule


DRIFT_REQUEUE_VERSION = "drift-requeue-v1"


def _drift_requeue_refusal(conn, head, claim, policy_json):
    """None when a fill-drift re-judge may open a fresh ticket (skipping the cooldown): the cohort froze drift-requeue-v1, the
    mint's latest ticket IS the claimed one, its fill stage is the recorded drift refusal with the same drift block, it is not
    itself a re-judge (one per original, then final), and no re-judge was emitted for it before."""
    if json.loads(policy_json).get("drift_requeue") != DRIFT_REQUEUE_VERSION:
        return "drift_requeue_disabled"
    if head["ticket_id"] != claim["prev_ticket_id"]:
        return "drift_requeue_not_latest"
    row = conn.execute("SELECT verdict, output_json FROM desk_tickets WHERE ticket_id=? AND stage='fill' "
                       "ORDER BY ts DESC,rowid DESC LIMIT 1", (head["ticket_id"],)).fetchone()
    fill = json.loads(row[1]) if row is not None else {}
    if row is None or row[0] != "SKIP" or fill.get("drift") != claim["drift"]:
        return "drift_requeue_unproven"
    prev = conn.execute("SELECT json_extract(candidate_json,'$.drift_requeue') FROM desk_candidate_emissions "
                        "WHERE ticket_id=? AND is_primary=1", (head["ticket_id"],)).fetchone()
    if prev is not None and prev[0] is not None:
        return "drift_requeue_final"        # a re-judged ticket refused by drift again is final
    spent = conn.execute("SELECT 1 FROM desk_candidate_emissions WHERE network_id=? AND mint=? "
                         "AND json_extract(candidate_json,'$.drift_requeue.prev_ticket_id')=?",
                         (head["network_id"], head["mint"], head["ticket_id"])).fetchone()
    return "drift_requeue_consumed" if spent else None


NOISE_WATCH_REASON = "px_hold_noise_watch"


def _noise_rejudge_refusal(conn, head, claim, policy_json):
    """None when a young-coin px_hold WATCH may open its ONE re-judge: a v23-facts cohort, the claimed ticket is the mint's
    active head, its vet is that WATCH, it is not itself a re-judge (then final), and no re-judge was emitted for it."""
    if json.loads(policy_json).get("facts") != "v23-facts-v1":
        return "noise_rejudge_disabled"
    if head["ticket_id"] != claim["prev_ticket_id"]:
        return "noise_rejudge_not_latest"
    row = conn.execute("SELECT reason FROM desk_tickets WHERE ticket_id=? AND stage='vet' ORDER BY ts DESC,rowid DESC LIMIT 1",
                       (head["ticket_id"],)).fetchone()
    if row is None or row[0] != NOISE_WATCH_REASON:
        return "noise_rejudge_unproven"
    prev = conn.execute("SELECT json_extract(candidate_json,'$.noise_rejudge') FROM desk_candidate_emissions "
                        "WHERE ticket_id=? AND is_primary=1", (head["ticket_id"],)).fetchone()
    if prev is not None and prev[0] is not None:
        # standing-v2 alert_young: a FIRST re-judge may open one more (max 2, Alpha 10-26); anything else is final
        policy = json.loads(policy_json)
        first = json.loads(prev[0]).get("count", 1) == 1
        if not (first and (policy.get("standing_orders") or {}).get("version") == "standing-v2"
                and claim.get("count") == 2):
            return "noise_rejudge_final"
    spent = conn.execute("SELECT 1 FROM desk_candidate_emissions WHERE network_id=? AND mint=? "
                         "AND json_extract(candidate_json,'$.noise_rejudge.prev_ticket_id')=?",
                         (head["network_id"], head["mint"], head["ticket_id"])).fetchone()
    return "noise_rejudge_consumed" if spent else None


def _watch_look_refusal(conn, head, claim, policy_json):
    """None when a watch-list look may open: the cohort froze watch_list, the claimed ticket is the mint's head, its vet
    is a WATCH, the look is exactly the parent's + 1 and within max_looks, and no look was opened from it before."""
    policy = json.loads(policy_json)
    rule = policy.get("watch_list")
    if rule is None:
        return "watch_look_disabled"
    if head["ticket_id"] != claim["prev_ticket_id"]:
        return "watch_look_not_latest"
    row = conn.execute("SELECT reason FROM desk_tickets WHERE ticket_id=? AND stage='vet' ORDER BY ts DESC,rowid DESC LIMIT 1",
                       (head["ticket_id"],)).fetchone()
    if row is None or row[0] != "watch":
        return "watch_look_unproven"
    prev = conn.execute("SELECT json_extract(candidate_json,'$.watch_look.look') FROM desk_candidate_emissions "
                        "WHERE ticket_id=? AND is_primary=1", (head["ticket_id"],)).fetchone()
    parent_look = (prev[0] if prev is not None and prev[0] is not None else 1)
    if claim["look"] != parent_look + 1 or claim["look"] > rule.get("max_looks", 0):
        return "watch_look_out_of_order"
    spent = conn.execute("SELECT 1 FROM desk_candidate_emissions WHERE network_id=? AND mint=? "
                         "AND json_extract(candidate_json,'$.watch_look.prev_ticket_id')=?",
                         (head["network_id"], head["mint"], head["ticket_id"])).fetchone()
    return "watch_look_consumed" if spent else None


def _limit_watch_refusal(conn, head, claim, policy_json):
    """None when a LIMIT-arm watch may open its child ticket: the cohort froze standing_orders.limit_arm true, the claimed
    ticket is the mint's latest, its fill stage recorded the limit refusal, it is not itself a limit child, not spent."""
    policy = json.loads(policy_json)
    if (policy.get("standing_orders") or {}).get("limit_arm") is not True and "chase_limit" not in policy:
        return "limit_watch_disabled"
    if head["ticket_id"] != claim["prev_ticket_id"]:
        return "limit_watch_not_latest"
    row = conn.execute("SELECT verdict, output_json FROM desk_tickets WHERE ticket_id=? AND stage='fill' "
                       "ORDER BY ts DESC,rowid DESC LIMIT 1", (head["ticket_id"],)).fetchone()
    if row is None or row[0] != "SKIP" or json.loads(row[1]).get("limit_watch") is not True:
        return "limit_watch_unproven"
    prev = conn.execute("SELECT json_extract(candidate_json,'$.limit_watch') FROM desk_candidate_emissions "
                        "WHERE ticket_id=? AND is_primary=1", (head["ticket_id"],)).fetchone()
    if prev is not None and prev[0] is not None:
        return "limit_watch_final"
    spent = conn.execute("SELECT 1 FROM desk_candidate_emissions WHERE network_id=? AND mint=? "
                         "AND json_extract(candidate_json,'$.limit_watch.prev_ticket_id')=?",
                         (head["network_id"], head["mint"], head["ticket_id"])).fetchone()
    return "limit_watch_consumed" if spent else None


def get_intake_source_receipt(conn, opportunity_id, *, cohort_id, hard_policy, candidate, as_of):
    """Read authenticated local receipt storage, then verify its content/policy/candidate/time binding."""
    from sniper.services import memecoin_desk_contract as contract
    from sniper.services.memecoin_desk_intake_proof import source_proof_refusal
    try:
        original = dict(candidate)
        original.pop("snapshot_hash", None)  # store convenience key is not part of the producer envelope
        original = contract.validate_candidate(original)
        if original["opportunity_id"] != opportunity_id:
            return None
        policy_hash = _hash(_json(hard_policy))
        cohort = conn.execute("SELECT policy_json FROM desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
        if cohort is None or _json(json.loads(cohort[0]).get("hard_filter")) != _json(hard_policy):
            return None
        row = conn.execute("SELECT * FROM desk_intake_source_receipts WHERE opportunity_id=?", (opportunity_id,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        proof = json.loads(row["receipt_json"])
        if (not isinstance(proof, dict) or row["cohort_id"] != cohort_id or row["hard_policy_hash"] != policy_hash
                or row["receipt_hash"] != _hash(row["receipt_json"])
                or row["candidate_hash"] != _hash(_json(original))
                or row["known_at"] != proof.get("known_at")
                or source_proof_refusal(proof, original, policy_hash=policy_hash, as_of=as_of) is not None):
            return None
        return proof
    except (sqlite3.Error, TypeError, ValueError, KeyError, OverflowError, RecursionError):
        return None


def record_candidate(conn, candidate, *, cohort_id, now_iso, cooldown_s, reentry_enabled=False,
                     intake_dir=None, source_now_fn=None):
    """Retain every immutable source emission; only a new primary opens a funnel. A candidate carrying a
    `reentry` claim is ALWAYS verified (even after the ordinary cooldown has elapsed) and spends its
    per-close chance and a cap slot. Cap and window come from the cohort's frozen policy["winner_reentry"];
    reentry_enabled (the kill knob) False or no frozen rule refuses the claim."""
    now_iso = _time(now_iso)
    _number(cooldown_s, "cooldown_s")
    if cooldown_s is None or not isinstance(candidate, dict):
        raise ValueError("candidate")
    candidate = dict(candidate)
    for field in ("opportunity_id", "ticket_id", "producer", "network_id", "mint"):
        _text(candidate.get(field), field)
    if candidate["opportunity_id"] != candidate["ticket_id"]:
        raise ValueError("opportunity_identity")
    emitted_at = _time(candidate.get("emitted_at"))
    snapshot = candidate.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot")
    snapshot_hash = snapshot.get("hash")
    body = {key: value for key, value in snapshot.items() if key != "hash"}
    if snapshot_hash != _hash(_json(body)) or candidate.get("snapshot_hash", snapshot_hash) != snapshot_hash:
        raise ValueError("snapshot_hash")
    candidate["snapshot_hash"] = snapshot_hash
    candidate_json, snapshot_json = _json(candidate), _json(snapshot)
    key, producer = candidate["opportunity_id"], candidate["producer"]
    chain, mint, pool = candidate["network_id"], candidate["mint"], candidate.get("pool")
    registered = conn.execute("SELECT policy_json FROM desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
    hard_policy = (json.loads(registered[0]).get("hard_filter") or {}) if registered else {}
    source_version = hard_policy.get("intake_source_version")
    if source_version is not None and source_version != "desk-intake-source-v1":
        raise ValueError("intake_source_version")
    source_required = source_version == "desk-intake-source-v1"
    proof = None
    if source_required:
        from sniper.services.memecoin_desk_intake_proof import verify_source
        ensure_tables(conn)  # schema setup must finish before the atomic receipt/admission transaction
        original = dict(candidate)
        original.pop("snapshot_hash", None)
        exists = conn.execute("SELECT candidate_json,cohort_id FROM desk_candidate_emissions WHERE opportunity_id=?",
                              (key,)).fetchone()
        if exists is not None:
            if exists[0] != candidate_json or exists[1] != cohort_id:
                raise ValueError("emission_conflict")
            proof = get_intake_source_receipt(conn, key, cohort_id=cohort_id, hard_policy=hard_policy,
                                             candidate=original, as_of=datetime.fromisoformat(now_iso).timestamp())
            if proof is None:
                raise ValueError("intake_source_receipt_unknown")
        else:
            if intake_dir is None:
                raise ValueError("intake_source_root_required")
            clock_arg = {} if source_now_fn is None else {"now_fn": source_now_fn}
            proof = verify_source(original, intake_dir=intake_dir, policy_hash=_hash(_json(hard_policy)), **clock_arg)
            if proof["status"] != "pass":
                raise ValueError(proof["reason"])
            now_iso = datetime.fromtimestamp(max(datetime.fromisoformat(now_iso).timestamp(), proof["known_at"]),
                                             timezone.utc).isoformat()
    with _write(conn):
        cohort = conn.execute("SELECT producer,policy_json FROM desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
        if cohort is None:
            raise ValueError("cohort_unknown")
        if cohort[0] != producer:
            raise ValueError("producer_mismatch")
        policy = json.loads(cohort[1])
        venue = candidate.get("venue", "pumpfun")
        if venue not in ("pumpfun", "pumpswap") or policy.get("venue", "pumpfun") != venue:
            raise ValueError("venue_mismatch")
        if "intake" in candidate:
            from sniper.services.memecoin_desk_contract import intake_feeds
            if (not isinstance(candidate["intake"], dict)
                    or candidate["intake"].get("feed") not in intake_feeds(policy.get("hard_filter"))):
                raise ValueError("intake_feed_not_enrolled")
        existing = conn.execute("SELECT * FROM desk_candidate_emissions WHERE opportunity_id=?", (key,)).fetchone()
        if existing is not None:
            if existing["candidate_json"] != candidate_json or existing["cohort_id"] != cohort_id:
                raise ValueError("emission_conflict")
            if source_required and get_intake_source_receipt(conn, key, cohort_id=cohort_id,
                    hard_policy=hard_policy, candidate=original,
                    as_of=max(datetime.fromisoformat(now_iso).timestamp(), proof["known_at"])) is None:
                raise ValueError("intake_source_receipt_unknown")
            ticket_id, primary, reason = existing["ticket_id"], False, "duplicate"
        else:
            snapshot_row = conn.execute("SELECT snapshot_json FROM desk_snapshots WHERE snapshot_hash=?",
                                        (snapshot_hash,)).fetchone()
            if snapshot_row is not None and snapshot_row[0] != snapshot_json:
                raise ValueError("snapshot_hash_conflict")
            conn.execute("INSERT INTO desk_snapshots VALUES (?,?) ON CONFLICT(snapshot_hash) DO NOTHING",
                         (snapshot_hash, snapshot_json))
            head = conn.execute("SELECT * FROM desk_ticket_heads WHERE network_id=? AND mint=? "
                                "AND state IN ('open','waiting','queued','unresolved','filled')",
                                (chain, mint)).fetchone()
            reason = "active_ticket" if head is not None else None
            if head is None:
                head = conn.execute("SELECT * FROM desk_ticket_heads WHERE network_id=? AND mint=? "
                                    "ORDER BY created_at DESC,rowid DESC LIMIT 1", (chain, mint)).fetchone()
                if head is not None:
                    last = conn.execute("SELECT MAX(ts) FROM desk_tickets WHERE ticket_id=?",
                                        (head["ticket_id"],)).fetchone()[0] or head["created_at"]
                    elapsed = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(last)).total_seconds()
                    if elapsed < cooldown_s:
                        reason = "cooldown"
            if candidate.get("reentry") is not None and reason != "active_ticket":
                if head is None:
                    raise ValueError("reentry_no_prior")
                rule = _reentry_policy(cohort[1]) if reentry_enabled else None
                reason = ("reentry_disabled" if rule is None else
                          _reentry_refusal(conn, head, candidate["reentry"], now_iso, rule["per_hour"],
                                           rule["window_s"], emitted_at, rule))
            if candidate.get("limit_watch") is not None and reason != "active_ticket":
                if head is None:
                    raise ValueError("limit_watch_no_prior")
                reason = _limit_watch_refusal(conn, head, candidate["limit_watch"], cohort[1])
            if candidate.get("watch_look") is not None:
                if head is None:
                    raise ValueError("watch_look_no_prior")
                reason = _watch_look_refusal(conn, head, candidate["watch_look"], cohort[1])
            if candidate.get("noise_rejudge") is not None:
                # the parent WATCH is still the active head: the child replaces it (the caller expires the parent next)
                if head is None:
                    raise ValueError("noise_rejudge_no_prior")
                reason = _noise_rejudge_refusal(conn, head, candidate["noise_rejudge"], cohort[1])
            if candidate.get("drift_requeue") is not None and reason != "active_ticket":
                if head is None:
                    raise ValueError("drift_requeue_no_prior")
                reason = _drift_requeue_refusal(conn, head, candidate["drift_requeue"], cohort[1])
            if reason is not None and head["cohort_id"] != cohort_id:
                head_policy = conn.execute("SELECT policy_json FROM desk_cohorts WHERE cohort_id=?",
                                           (head["cohort_id"],)).fetchone()[0]
                if venue == "pumpswap" or json.loads(head_policy).get("venue") == "pumpswap":
                    raise ValueError("cohort_attachment")
            primary = reason is None
            ticket_id = key if primary else head["ticket_id"]
            if primary:
                conn.execute("INSERT INTO desk_ticket_heads VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')",
                             (key, key, cohort_id, producer, chain, mint, pool, snapshot_hash,
                              emitted_at, now_iso, now_iso))
            else:
                conn.execute("UPDATE desk_ticket_heads SET last_seen_at=? WHERE ticket_id=?",
                             (now_iso, ticket_id))
            conn.execute("INSERT INTO desk_candidate_emissions "
                         "(opportunity_id,ticket_id,producer,cohort_id,network_id,mint,pool,emitted_at,"
                         "received_at,snapshot_hash,candidate_json,is_primary,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (key, ticket_id, producer, cohort_id, chain, mint, pool, emitted_at,
                          now_iso, snapshot_hash, candidate_json, int(primary), reason))
            if source_required:
                receipt_json = _json(proof)
                conn.execute("INSERT INTO desk_intake_source_receipts VALUES(?,?,?,?,?,?,?)",
                             (key, cohort_id, proof["policy_hash"], proof["candidate_hash"], proof["known_at"],
                              _hash(receipt_json), receipt_json))
    return {**get_ticket(conn, ticket_id), "is_primary": primary, "emission_opportunity_id": key,
            "reason": reason}


def _outbox_request(outbox):
    if not isinstance(outbox, dict) or set(outbox) != {"intent_id", "requested_at", "expires_at", "payload"}:
        raise ValueError("outbox_request")
    _text(outbox["intent_id"], "intent_id")
    if not isinstance(outbox["payload"], dict):
        raise ValueError("outbox_payload")
    result = dict(outbox)
    result["requested_at"], result["expires_at"] = _time(outbox["requested_at"]), _time(outbox["expires_at"])
    if datetime.fromisoformat(result["expires_at"]) <= datetime.fromisoformat(result["requested_at"]):
        raise ValueError("outbox_expiry")
    return result


def record_stage(conn, *, ticket_id, stage, seat, verdict, reason, inputs, ts, output=None,
                 response_at=None, latency_ms=None, cost_usd=None, outbox=None, model=None,
                 prompt_version=None, gate_state=None, gate_inputs=None, gate_evaluated_at=None,
                 trigger_at=None, wait_state=None, position_id=None, intent_id=None, ticket_state=None,
                 expire_outbox_reason=None):
    """Freeze one judgment and its optional request atomically; a replay never overwrites it."""
    for value, name in ((stage, "stage"), (seat, "seat"), (reason, "reason")):
        _text(value, name)
    if not isinstance(inputs, dict):
        raise ValueError("inputs")
    if verdict not in ("PASS", "SKIP", "KILL", "ENTER", "WAIT", "WATCH", "HOLD", "TIGHTEN", "EXIT"):
        raise ValueError("verdict")
    if gate_state not in (None, "pass", "skip") or (gate_state is not None and stage != "hard_filter"):
        raise ValueError("gate_state")
    if wait_state is not None and wait_state not in _WAIT_STATES:
        raise ValueError("wait_state")
    if ticket_state is not None and ticket_state not in _ACTIVE + _TERMINAL:
        raise ValueError("ticket_state")
    closing = stage == "close" or ticket_state == "closed"
    if closing:
        if (stage != "close" or seat != "engine" or verdict != "EXIT" or ticket_state != "closed"
                or type(position_id) is not int or position_id <= 0 or not isinstance(output, dict)
                or output.get("state") != "closed" or type(output.get("position_id")) is not int
                or output["position_id"] != position_id or type(output.get("is_open")) is not int
                or output["is_open"] != 0):
            raise ValueError("close_proof")
        if datetime.fromisoformat(_time(output.get("closed_at"))) > datetime.fromisoformat(_time(ts)):
            raise ValueError("close_proof")
    inputs_json = _json(inputs)
    request = _outbox_request(outbox) if outbox is not None else None
    if request is not None:
        if stage != "size" or verdict != "ENTER":
            raise ValueError("outbox_stage")
        if intent_id is not None and intent_id != request["intent_id"]:
            raise ValueError("intent_id")
        intent_id = request["intent_id"]
    row = {"ticket_id": ticket_id, "stage": stage, "seat": seat, "verdict": verdict,
           "reason": reason, "inputs_hash": _hash(inputs_json), "inputs_json": inputs_json,
           "output_json": _json(output) if output is not None else None, "model": model,
           "prompt_version": prompt_version, "latency_ms": _number(latency_ms, "latency_ms"),
           "cost_usd": _number(cost_usd, "cost_usd"), "intent_id": intent_id,
           "position_id": position_id, "ts": _time(ts), "gate_state": gate_state,
           "gate_inputs_json": _json(gate_inputs) if gate_inputs is not None else None,
           "response_at": _time(response_at) if response_at is not None else None,
           "gate_evaluated_at": _time(gate_evaluated_at) if gate_evaluated_at is not None else None,
           "trigger_at": _time(trigger_at) if trigger_at is not None else None, "wait_state": wait_state}
    with _write(conn):
        head = conn.execute("SELECT * FROM desk_ticket_heads WHERE ticket_id=?", (ticket_id,)).fetchone()
        if head is None:
            raise ValueError("ticket_unknown")
        if head["producer"] == "U" and gate_state is not None:
            raise ValueError("u_activity_gate")
        row.update({key: head[key] for key in ("opportunity_id", "cohort_id", "producer", "network_id", "mint", "pool")})
        existing = conn.execute("SELECT * FROM desk_tickets WHERE ticket_id=? AND stage=?",
                                (ticket_id, stage)).fetchone()
        prior_request = conn.execute("SELECT request_json FROM desk_outbox WHERE ticket_id=?", (ticket_id,)).fetchone()
        if request is not None and prior_request is not None and prior_request[0] != _json(request):
            raise ValueError("outbox_conflict")
        if existing is not None:
            if any(existing[key] != value for key, value in row.items()):
                raise ValueError("stage_conflict")
            if request is not None and prior_request is None:
                raise ValueError("outbox_conflict")
            result = _stage_row(existing)
        else:
            if request is not None and head["state"] in _TERMINAL:
                raise ValueError("ticket_terminal")
            if closing:
                fill = conn.execute("SELECT position_id FROM desk_tickets WHERE ticket_id=? AND stage='fill'",
                                    (ticket_id,)).fetchone()
                if head["state"] not in ("filled", "unresolved") or fill is None or fill[0] != position_id:
                    raise ValueError("close_proof")
            previous = conn.execute("SELECT 1 FROM desk_tickets WHERE network_id=? AND mint=? "
                                    "AND seat=? AND inputs_hash=?", (head["network_id"], head["mint"],
                                                                   seat, row["inputs_hash"])).fetchone()
            if previous is not None:
                raise ValueError("inputs_already_judged")
            columns = ",".join(row)
            values = ",".join(":" + name for name in row)
            cursor = conn.execute(f"INSERT INTO desk_tickets ({columns}) VALUES ({values})", row)
            if request is not None:
                payload_json = _json(request["payload"])
                conn.execute("INSERT INTO desk_outbox "
                             "(ticket_id,intent_id,requested_at,expires_at,payload_json,payload_hash,request_json,state) "
                             "VALUES (?,?,?,?,?,?,?,'pending')",
                             (ticket_id, request["intent_id"], request["requested_at"], request["expires_at"],
                              payload_json, _hash(payload_json), _json(request)))
            state = ticket_state
            if state is None and request is not None:
                state = "queued"
            if state is None:
                state = {"KILL": "killed", "SKIP": "skipped", "WAIT": "waiting", "WATCH": "waiting"}.get(verdict)
            if state is None and stage == "fill":
                status = output.get("state", output.get("status")) if isinstance(output, dict) else None
                if status in _ACTIVE + _TERMINAL:
                    state = status
                elif position_id is not None:
                    state = "filled"
                elif status in ("submitted_unknown", "pending", "processing"):
                    state = "unresolved"
            if expire_outbox_reason is not None:
                # the stage and the expiry of its PENDING outbox row land together (Codex P2): head -> expired
                _text(expire_outbox_reason, "reason")
                if head["state"] != "queued":
                    raise ValueError("ticket_active_execution")
                if conn.execute("UPDATE desk_outbox SET state='expired',reason=?,resolved_at=? WHERE ticket_id=? "
                                "AND state='pending'", (expire_outbox_reason, row["ts"], ticket_id)).rowcount != 1:
                    raise ValueError("outbox_not_pending")
                state = "expired"
            elif state is not None:
                if state == "closed" and not closing:
                    raise ValueError("close_proof")
                if (state in _TERMINAL and head["state"] in ("queued", "unresolved", "filled")
                        and stage != "fill" and not closing):
                    raise ValueError("ticket_active_execution")
            if state is not None:
                conn.execute("UPDATE desk_ticket_heads SET state=? WHERE ticket_id=?", (state, ticket_id))
            result = _stage_row(conn.execute("SELECT * FROM desk_tickets WHERE id=?", (cursor.lastrowid,)).fetchone())
    return result


def _outbox_row(row):
    result = dict(row)
    result["payload"] = json.loads(result["payload_json"])
    return result


def list_tickets_with_stage(conn, *, stage, since_iso, until_iso):
    """Ticket ids that recorded `stage` in [since_iso, until_iso), oldest first (the post-mortem's
    day of closes)."""
    rows = conn.execute("SELECT DISTINCT ticket_id FROM desk_tickets WHERE stage=? AND ts>=? AND ts<? "
                        "ORDER BY ticket_id", (stage, _time(since_iso), _time(until_iso))).fetchall()
    return [row[0] for row in rows]


def note_outbox_hold(conn, ticket_id, *, facts, at_iso):
    """Record the first cost-gate hold of a pending row, once; later holds never overwrite it. True if written."""
    if not isinstance(facts, dict):
        raise ValueError("hold_facts")
    with _write(conn):
        cur = conn.execute("UPDATE desk_outbox SET first_hold_at=?, first_hold_json=? WHERE ticket_id=? "
                           "AND state='pending' AND first_hold_at IS NULL AND ?<expires_at",
                           (_time(at_iso), _json(facts), ticket_id, _time(at_iso)))
    return cur.rowcount == 1


def list_outbox(conn, *, limit=10):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit")
    return [_outbox_row(row) for row in conn.execute(
        "SELECT * FROM desk_outbox WHERE state='pending' ORDER BY requested_at,ticket_id LIMIT ?", (limit,))]


def list_intents(conn, *, limit=10):
    """Sent requests still missing their terminal fill record; settled rows cannot block the page."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit")
    return [_outbox_row(row) for row in conn.execute(
        "SELECT o.* FROM desk_outbox o WHERE o.state='sent' AND NOT EXISTS "
        "(SELECT 1 FROM desk_tickets t WHERE t.ticket_id=o.ticket_id AND t.stage='fill') "
        "ORDER BY o.requested_at,o.ticket_id LIMIT ?", (limit,))]


def list_undispatched_decisions(conn, *, limit=10):
    """Recover accepted decisions after a crash before their atomic size/outbox write."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit")
    rows = conn.execute(
        "SELECT h.ticket_id FROM desk_ticket_heads h JOIN desk_tickets v "
        "ON v.ticket_id=h.ticket_id AND ((v.stage='vet' AND v.verdict='ENTER') OR (v.stage='trigger' AND v.verdict='ENTER')) "
        "WHERE h.state IN ('open','waiting') AND NOT EXISTS "
        "(SELECT 1 FROM desk_tickets t WHERE t.ticket_id=h.ticket_id AND t.stage IN ('size','fill')) "
        "ORDER BY v.ts,h.ticket_id LIMIT ?", (limit,)).fetchall()
    return [get_ticket(conn, row[0]) for row in rows]


def list_waiting_tickets(conn, *, limit=200):
    """Open watches (vet WAIT on a price trigger, or WATCH), oldest first: the standing-order executor's work list."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit")
    rows = conn.execute("SELECT h.ticket_id FROM desk_ticket_heads h WHERE h.state='waiting' "
                        "ORDER BY h.created_at,h.ticket_id LIMIT ?", (limit,)).fetchall()
    return [get_ticket(conn, row[0]) for row in rows]


def list_filled_tickets(conn, *, limit=10):
    """Position-bound tickets needing a funded close observation; unknown IDs remain held."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit")
    rows = conn.execute(
        "SELECT h.ticket_id,f.position_id FROM desk_ticket_heads h JOIN desk_tickets f "
        "ON f.ticket_id=h.ticket_id AND f.stage='fill' "
        "WHERE h.state IN ('filled','unresolved') AND typeof(f.position_id)='integer' AND f.position_id>0 "
        "AND NOT EXISTS (SELECT 1 FROM desk_tickets c WHERE c.ticket_id=h.ticket_id AND c.stage='close') "
        "ORDER BY f.ts,h.ticket_id LIMIT ?", (limit,)).fetchall()
    return [{**get_ticket(conn, row[0]), "position_id": row[1]} for row in rows]


def resolve_outbox(conn, ticket_id, *, state, intent_id=None, reason=None, now_iso, ticket_state=None):
    if state not in ("pending", "sent", "expired", "refused"):
        raise ValueError("outbox_state")
    if ticket_state is not None and ticket_state not in _ACTIVE + _TERMINAL:
        raise ValueError("ticket_state")
    if ticket_state == "closed":
        raise ValueError("close_proof")
    now_iso = _time(now_iso)
    with _write(conn):
        row = conn.execute("SELECT * FROM desk_outbox WHERE ticket_id=?", (ticket_id,)).fetchone()
        if row is None:
            raise ValueError("outbox_unknown")
        if intent_id is not None and intent_id != row["intent_id"]:
            raise ValueError("outbox_intent_conflict")
        if row["state"] != "pending":
            if row["state"] != state or (reason is not None and reason != row["reason"]):
                raise ValueError("outbox_resolution_conflict")
        elif state != "pending":
            head = conn.execute("SELECT state FROM desk_ticket_heads WHERE ticket_id=?", (ticket_id,)).fetchone()
            if state in ("expired", "refused") and head[0] in ("unresolved", "filled"):
                raise ValueError("ticket_active_execution")
            conn.execute("UPDATE desk_outbox SET state=?,reason=?,resolved_at=? WHERE ticket_id=?",
                         (state, reason, now_iso, ticket_id))
            new_state = ticket_state or {"sent": "unresolved", "expired": "expired", "refused": "refused"}[state]
            if head[0] not in _TERMINAL and not (state == "sent" and head[0] == "filled"):
                conn.execute("UPDATE desk_ticket_heads SET state=? WHERE ticket_id=?", (new_state, ticket_id))
        result = _outbox_row(conn.execute("SELECT * FROM desk_outbox WHERE ticket_id=?", (ticket_id,)).fetchone())
    return result


def record_admission(conn, *, ticket_id, arm, decision, decided_at):
    if arm not in ("F", "SJ", "SR"):
        raise ValueError("arm")
    if decision not in ("taken", "skipped_full", "latched", "halted", "not_selected"):
        raise ValueError("admission_decision")
    row = {"ticket_id": ticket_id, "arm": arm, "decision": decision, "decided_at": _time(decided_at)}
    with _write(conn):
        if conn.execute("SELECT 1 FROM desk_ticket_heads WHERE ticket_id=?", (ticket_id,)).fetchone() is None:
            raise ValueError("ticket_unknown")
        existing = conn.execute("SELECT * FROM desk_arm_admissions WHERE ticket_id=? AND arm=?",
                                (ticket_id, arm)).fetchone()
        if existing is not None:
            if dict(existing) != row:
                raise ValueError("admission_conflict")
        else:
            conn.execute("INSERT INTO desk_arm_admissions VALUES (:ticket_id,:arm,:decision,:decided_at)", row)
    return row


# --- desk JUDGE invocation claims (Lambda, step 4) ---------------------------------------------
# A listing is not a claim. A worker claims a ticket durably BEFORE it calls the model, saves the
# one final result, and only then records the decision, so a restart or a second worker never
# re-asks the model: a claim without a saved result is recorded as lost, never re-invoked.

def record_preflight_skip(conn, *, ticket_id, reason, inputs, ts):
    """Freeze a no-call refusal and its F exclusion together; a racing model claim wins."""
    _text(reason, "reason")
    if not isinstance(inputs, dict):
        raise ValueError("inputs")
    at, encoded = _time(ts), _json(inputs)
    result = {"action": "SKIP", "reason": reason, "rails": None, "condition": None,
              "reason_code": None, "enter_ttl_s": None, "next_check_s": None}
    with _write(conn):
        head = get_ticket(conn, ticket_id)
        if head is None:
            raise ValueError("ticket_unknown")
        row = {k: head[k] for k in ("ticket_id", "opportunity_id", "cohort_id", "producer", "network_id", "mint", "pool")}
        row.update(stage="vet", seat="judge", verdict="SKIP", reason=reason, inputs_hash=_hash(encoded),
                   inputs_json=encoded, output_json=_json({"result": result}), cost_usd=0.0,
                   latency_ms=0.0, ts=at, response_at=at)
        if conn.execute("SELECT 1 FROM desk_judge_calls WHERE ticket_id=?", (ticket_id,)).fetchone():
            return False
        prior = conn.execute("SELECT * FROM desk_tickets WHERE ticket_id=? AND stage='vet'", (ticket_id,)).fetchone()
        if prior is not None and any(prior[k] != v for k, v in row.items()):
            raise ValueError("stage_conflict")
        admission = get_admission(conn, ticket_id=ticket_id, arm="F")
        if admission is not None and admission["decision"] != "not_selected":
            raise ValueError("admission_conflict")
        if prior is not None:
            return True
        if (head["state"] not in ("open", "waiting") or at < head["emitted_at"]
                or not any(s["stage"] == "hard_filter" and s["verdict"] == "PASS" for s in head["stages"])):
            raise ValueError("hard_filter_required")
        if admission is None:
            conn.execute("INSERT INTO desk_arm_admissions VALUES(?,'F','not_selected',?)", (ticket_id, at))
        columns, values = ",".join(row), ",".join(":" + k for k in row)
        conn.execute(f"INSERT INTO desk_tickets({columns}) VALUES({values})", row)
        conn.execute("UPDATE desk_ticket_heads SET state='skipped' WHERE ticket_id=?", (ticket_id,))
    return True


def record_ps_preflight_skip(conn, *, ticket_id, reason, inputs, ts):
    """Compatibility entry point for PS callers; the shared write remains atomic."""
    head = get_ticket(conn, ticket_id)
    if head is None or head["candidate"].get("venue") != "pumpswap":
        raise ValueError("ps_ticket_required")
    return record_preflight_skip(conn, ticket_id=ticket_id, reason=reason, inputs=inputs, ts=ts)


def list_awaiting_judge(conn, *, limit=10, newest_first=False, not_cohort=None):
    """Hard-filter PASS tickets with no vet stage and no claim, oldest hard-filter first (newest first on request).
    not_cohort skips one cohort (the Office eval cohort: its tickets are Office input, never desk-judged)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit")
    rows = conn.execute(
        "SELECT h.ticket_id FROM desk_ticket_heads h JOIN desk_tickets f "
        "ON f.ticket_id=h.ticket_id AND f.stage='hard_filter' AND f.verdict='PASS' "
        "WHERE h.state IN ('open','waiting') "
        "AND NOT EXISTS (SELECT 1 FROM desk_tickets v WHERE v.ticket_id=h.ticket_id AND v.stage='vet') "
        "AND NOT EXISTS (SELECT 1 FROM desk_judge_calls c WHERE c.ticket_id=h.ticket_id) "
        + ("AND h.cohort_id<>? " if not_cohort is not None else "")
        + ("ORDER BY f.ts DESC,h.ticket_id DESC LIMIT ?" if newest_first else "ORDER BY f.ts,h.ticket_id LIMIT ?"),
        ((not_cohort,) if not_cohort is not None else ()) + (limit,)).fetchall()
    return [get_ticket(conn, row[0]) for row in rows]


def list_awaiting_judge_where(conn, *, limit, producer=None, not_producer=None, opportunity_ids=None,
                              passed_before=None, select_version=None, not_select_version=None):
    """list_awaiting_judge narrowed in SQL, oldest hard-filter first, so a selection never pages through
    tickets it would skip (the U runners shortlist: pending tickets must not fill the window)."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit")
    where, args = [], []
    if producer is not None:
        where.append("h.producer=?")
        args.append(producer)
    if not_producer is not None:
        where.append("h.producer<>?")
        args.append(not_producer)
    if opportunity_ids is not None:
        where.append("h.opportunity_id IN (SELECT value FROM json_each(?))")
        args.append(json.dumps(sorted(opportunity_ids)))
    if passed_before is not None:
        where.append("f.ts<?")
        args.append(_time(passed_before))
    if select_version is not None:         # the cohort's frozen selection rule (a trigger cohort bypasses the ranker gate)
        where.append("h.cohort_id IN (SELECT cohort_id FROM desk_cohorts WHERE json_extract(policy_json,'$.select_version')=?)")
        args.append(select_version)
    if not_select_version is not None:
        where.append("h.cohort_id IN (SELECT cohort_id FROM desk_cohorts "
                      "WHERE COALESCE(json_extract(policy_json,'$.select_version'),'')<>?)")
        args.append(not_select_version)
    rows = conn.execute(
        "SELECT h.ticket_id FROM desk_ticket_heads h JOIN desk_tickets f "
        "ON f.ticket_id=h.ticket_id AND f.stage='hard_filter' AND f.verdict='PASS' "
        "WHERE h.state IN ('open','waiting') "
        "AND NOT EXISTS (SELECT 1 FROM desk_tickets v WHERE v.ticket_id=h.ticket_id AND v.stage='vet') "
        "AND NOT EXISTS (SELECT 1 FROM desk_judge_calls c WHERE c.ticket_id=h.ticket_id) "
        + "".join(" AND " + w for w in where) +
        " ORDER BY f.ts,h.ticket_id LIMIT ?", (*args, limit)).fetchall()
    return [get_ticket(conn, row[0]) for row in rows]


def claim_judge_call(conn, *, ticket_id, call_id, worker, claimed_at):
    """True only for the one worker that wins the ticket; any later claim returns False."""
    _text(ticket_id, "ticket_id")
    _text(call_id, "call_id")
    _text(worker, "worker")
    claimed_at = _time(claimed_at)
    with _write(conn):
        if conn.execute("SELECT 1 FROM desk_tickets WHERE ticket_id=? AND stage='vet'", (ticket_id,)).fetchone():
            return False
        cur = conn.execute("INSERT OR IGNORE INTO desk_judge_calls(call_id,ticket_id,worker,claimed_at,state) "
                           "VALUES (?,?,?,?,'claimed')", (call_id, ticket_id, worker, claimed_at))
        return cur.rowcount == 1


def save_judge_response(conn, *, call_id, call_started_at, response_at, elapsed_ms, result):
    """Freeze the single final model result of a claimed call (once)."""
    _number(elapsed_ms, "elapsed_ms")
    if not isinstance(result, dict):
        raise ValueError("judge_result")
    with _write(conn):
        row = conn.execute("SELECT state FROM desk_judge_calls WHERE call_id=?", (call_id,)).fetchone()
        if row is None or row[0] != "claimed":
            raise ValueError("judge_call_state")
        sid, rid = result.get("session_id"), result.get("result_id")
        ids = (sid, rid) if isinstance(sid, str) and isinstance(rid, str) and sid and rid else (None, None)
        if ids[1] is not None and conn.execute(
                "SELECT 1 FROM desk_judge_calls WHERE result_session_id=? AND result_id=?", ids).fetchone():
            # one SDK result authorizes one ticket only: a replay is frozen as such, ids withheld
            result, ids = {**result, "replayed_result": True}, (None, None)
        conn.execute("UPDATE desk_judge_calls SET state='responded',call_started_at=?,response_at=?,"
                     "elapsed_ms=?,result_json=?,result_session_id=?,result_id=? WHERE call_id=?",
                     (_time(call_started_at), _time(response_at), float(elapsed_ms), _json(result),
                      ids[0], ids[1], call_id))


def list_orphan_judge_claims(conn, *, limit=100):
    """Calls still 'claimed' although their ticket already has a vet stage (a lost call recorded by a release that left
    the claim open)."""
    return [dict(r) for r in conn.execute(
        "SELECT c.* FROM desk_judge_calls c WHERE c.state='claimed' AND EXISTS "
        "(SELECT 1 FROM desk_tickets v WHERE v.ticket_id=c.ticket_id AND v.stage='vet') "
        "ORDER BY c.claimed_at LIMIT ?", (limit,)).fetchall()]


def list_judge_calls_to_record(conn, *, limit=10, after=None):
    """Claimed or responded calls whose ticket has no vet stage yet (crash recovery).
    `after` = (claimed_at, call_id) keyset cursor, so a refused prefix cannot starve later calls."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit")
    after_at, after_id = after if after is not None else ("", "")
    rows = conn.execute(
        "SELECT c.* FROM desk_judge_calls c WHERE NOT EXISTS "
        "(SELECT 1 FROM desk_tickets v WHERE v.ticket_id=c.ticket_id AND v.stage='vet') "
        "AND (c.claimed_at,c.call_id) > (?,?) "
        "ORDER BY c.claimed_at,c.call_id LIMIT ?", (after_at, after_id, limit)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json")) if item["result_json"] is not None else None
        out.append(item)
    return out


# desk_judge_calls rows (alias c) that spent a model call: in flight, or answered. A no-call claim does not count (Gamma, 20dd1becf).
# In a TRIGGER cohort (select_version desk-judge-select-v2, pinned equal to the judge's constant by a test) a stale_snapshot claim is
# a no-call too: it spends nothing, so a tape gap must not shrink the 15/h capacity. Old cohorts keep counting it (unchanged).
TRIGGER_SELECT_VERSION = "desk-judge-select-v2"
CALL_SPENT_SQL = (
    "(c.state='claimed' OR (COALESCE(json_extract(c.result_json,'$.call_error'),'') NOT IN "
    "('budget_exhausted','budget_skipped','ranker_cut','ranker_missing','signal_aged','band_below','band_curve_complete','band_unknown','moved_since_emission','seat_cut') "
    "AND NOT (COALESCE(json_extract(c.result_json,'$.call_error'),'')='stale_snapshot' AND c.ticket_id IN "
    "(SELECT h.ticket_id FROM desk_ticket_heads h JOIN desk_cohorts k ON k.cohort_id=h.cohort_id "
    "WHERE json_extract(k.policy_json,'$.select_version')='" + TRIGGER_SELECT_VERSION + "'))))")


def list_awaiting_looks(conn, *, limit):
    """watch-list looks (candidate carries watch_look) waiting for their JUDGE call, oldest look first."""
    rows = conn.execute(
        "SELECT h.ticket_id FROM desk_ticket_heads h JOIN desk_candidate_emissions e ON e.opportunity_id=h.ticket_id "
        "AND e.is_primary=1 JOIN desk_tickets f ON f.ticket_id=h.ticket_id AND f.stage='hard_filter' AND f.verdict='PASS' "
        "WHERE h.state IN ('open','waiting') AND json_extract(e.candidate_json,'$.watch_look') IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM desk_tickets v WHERE v.ticket_id=h.ticket_id AND v.stage='vet') "
        "AND NOT EXISTS (SELECT 1 FROM desk_judge_calls c WHERE c.ticket_id=h.ticket_id) "
        "ORDER BY f.ts,h.ticket_id LIMIT ?", (limit,)).fetchall()
    return [get_ticket(conn, row[0]) for row in rows]


def look_calls_since(conn, *, since_iso):
    """Model calls spent on watch-list looks since since_iso (the frozen re-look seat cap counts these)."""
    return conn.execute(
        "SELECT COUNT(*) FROM desk_judge_calls c JOIN desk_candidate_emissions e ON e.opportunity_id=c.ticket_id "
        "AND e.is_primary=1 WHERE c.claimed_at>=? AND json_extract(e.candidate_json,'$.watch_look') IS NOT NULL AND "
        + CALL_SPENT_SQL, (_time(since_iso),)).fetchone()[0]


def judge_calls_since(conn, *, since_iso, cohort_id=None):
    """Model calls claimed at or after since_iso: in flight or answered. Budget-SKIP claims made no
    call and do not count, or sustained arrivals would starve the seat forever (Gamma, 20dd1becf).
    cohort_id: only that cohort's calls (a cohort with its own frozen judge_calls_per_hour, e.g. intake)."""
    if cohort_id is None:
        return conn.execute("SELECT COUNT(*) FROM desk_judge_calls c WHERE c.claimed_at>=? AND " + CALL_SPENT_SQL,
                            (_time(since_iso),)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM desk_judge_calls c JOIN desk_ticket_heads h ON h.ticket_id=c.ticket_id "
                        "WHERE c.claimed_at>=? AND h.cohort_id=? AND " + CALL_SPENT_SQL,
                        (_time(since_iso), cohort_id)).fetchone()[0]



def get_admission(conn, *, ticket_id, arm):
    row = conn.execute("SELECT * FROM desk_arm_admissions WHERE ticket_id=? AND arm=?", (ticket_id, arm)).fetchone()
    return dict(row) if row is not None else None



def write_prequote_hint(conn, *, ticket_id, network_id, mint, created_at, expires_at):
    """One JUDGE call's pre-quote hint (epoch seconds). Idempotent; expired hints are pruned here."""
    # Its own short write transaction: the judge's claim (claim_judge_call) is already committed by
    # its own _write, and nothing else of this call is written before the model returns.
    with _write(conn):
        conn.execute("DELETE FROM desk_prequote_hints WHERE expires_at < ?", (float(created_at),))
        conn.execute("INSERT OR REPLACE INTO desk_prequote_hints VALUES(?,?,?,?,?)",
                     (ticket_id, network_id, mint, float(created_at), float(expires_at)))


def clear_prequote_hint(conn, ticket_id):
    """The call returned (answer or error): stop pre-quoting its mint."""
    with _write(conn):
        conn.execute("DELETE FROM desk_prequote_hints WHERE ticket_id=?", (ticket_id,))


def live_prequote_hints(conn, *, now, limit):
    """The newest `limit` unexpired hints: [(ticket_id, network_id, mint, expires_at)]."""
    return [tuple(r) for r in conn.execute(
        "SELECT ticket_id, network_id, mint, expires_at FROM desk_prequote_hints WHERE expires_at >= ? "
        "ORDER BY created_at DESC LIMIT ?", (float(now), int(limit)))]
