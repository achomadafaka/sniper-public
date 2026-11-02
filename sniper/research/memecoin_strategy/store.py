from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from .models import Candidate, CandidateSpec


ACTIVE_CAP = 5
IMMUTABLE_COLUMNS = (
    "candidate_id", "name", "spawn_index", "generation", "parent_id", "predicate_json",
    "exit_policy_json", "sizing_json", "costs_json", "controls_json", "gates_json",
    "params_hash", "born_at", "discovery_cutoff", "capture_version_floor", "execution_mode",
    "timecap_s", "alpha_alloc", "notes",
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def canonical_params_hash(spec_or_predicate, exit_policy=None, sizing=None, costs=None,
                          controls=None, gates=None):
    if isinstance(spec_or_predicate, CandidateSpec):
        spec = spec_or_predicate
        payload = {
            "predicate": spec.predicate, "exit_policy": spec.exit_policy,
            "sizing": spec.sizing, "costs": spec.costs,
            "controls": spec.controls, "gates": spec.gates,
        }
    else:
        payload = {
            "predicate": spec_or_predicate, "exit_policy": exit_policy, "sizing": sizing,
            "costs": costs, "controls": controls, "gates": gates,
        }
    return hashlib.sha256(_canonical(payload).encode("ascii")).hexdigest()


def ensure_strategy_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_strategy_candidates (
            candidate_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'candidate_waiting','candidate_active','killed','retired','promoted_first_class'
            )),
            spawn_index INTEGER NOT NULL UNIQUE CHECK(spawn_index > 0),
            generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
            parent_id TEXT,
            predicate_json TEXT NOT NULL,
            exit_policy_json TEXT NOT NULL,
            sizing_json TEXT NOT NULL,
            costs_json TEXT NOT NULL,
            controls_json TEXT NOT NULL,
            gates_json TEXT NOT NULL,
            params_hash TEXT NOT NULL UNIQUE,
            born_at TEXT NOT NULL,
            discovery_cutoff TEXT NOT NULL,
            capture_version_floor INTEGER NOT NULL CHECK(capture_version_floor >= 2),
            execution_mode TEXT NOT NULL CHECK(execution_mode = 'paper'),
            timecap_s INTEGER NOT NULL CHECK(timecap_s > 0),
            alpha_alloc REAL NOT NULL CHECK(alpha_alloc > 0 AND alpha_alloc < 1),
            notes TEXT NOT NULL DEFAULT ''
        )""")
    immutable = ",".join(IMMUTABLE_COLUMNS)
    conn.execute(f"""
        CREATE TRIGGER IF NOT EXISTS mc_strategy_candidate_immutable
        BEFORE UPDATE OF {immutable} ON memecoin_strategy_candidates
        BEGIN SELECT RAISE(ABORT, 'candidate parameters are immutable'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_strategy_candidate_no_delete
        BEFORE DELETE ON memecoin_strategy_candidates
        BEGIN SELECT RAISE(ABORT, 'candidate ledger is append-only'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_strategy_active_cap_insert
        BEFORE INSERT ON memecoin_strategy_candidates
        WHEN NEW.status='candidate_active' AND
             (SELECT COUNT(*) FROM memecoin_strategy_candidates
               WHERE status='candidate_active') >= 5
        BEGIN SELECT RAISE(ABORT, 'active candidate cap exceeded'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_strategy_active_cap_update
        BEFORE UPDATE OF status ON memecoin_strategy_candidates
        WHEN NEW.status='candidate_active' AND OLD.status!='candidate_active' AND
             (SELECT COUNT(*) FROM memecoin_strategy_candidates
               WHERE status='candidate_active') >= 5
        BEGIN SELECT RAISE(ABORT, 'active candidate cap exceeded'); END
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_candidate_signals (
            signal_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            upstream_observation_id INTEGER,
            control_observation_id INTEGER,
            episode_key TEXT NOT NULL,
            evidence_stage TEXT NOT NULL DEFAULT 'candidate',
            network_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            matched INTEGER NOT NULL CHECK(matched IN (0,1)),
            reason TEXT NOT NULL,
            feature_json TEXT NOT NULL,
            source_observation_ids_json TEXT NOT NULL,
            params_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_candidate_episode_match "
                 "ON memecoin_candidate_signals(candidate_id, episode_key) WHERE matched=1")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_signal_immutable
        BEFORE UPDATE ON memecoin_candidate_signals
        BEGIN SELECT RAISE(ABORT, 'candidate signal is immutable'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_signal_no_delete
        BEFORE DELETE ON memecoin_candidate_signals
        BEGIN SELECT RAISE(ABORT, 'candidate signal ledger is append-only'); END
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_candidate_positions (
            position_id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            episode_key TEXT NOT NULL,
            network_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            evidence_stage TEXT NOT NULL DEFAULT 'candidate',
            control_for_signal_id TEXT,
            execution_mode TEXT NOT NULL DEFAULT 'paper' CHECK(execution_mode='paper'),
            timecap_s INTEGER NOT NULL CHECK(timecap_s>0),
            entry_observation_id INTEGER,
            entry_price REAL NOT NULL CHECK(entry_price>0),
            entry_notional REAL NOT NULL CHECK(entry_notional>0),
            quantity REAL NOT NULL CHECK(quantity>0),
            entry_cost REAL NOT NULL DEFAULT 0 CHECK(entry_cost>=0),
            opened_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('open','closed')),
            stop_hits INTEGER NOT NULL DEFAULT 0 CHECK(stop_hits>=0),
            last_observation_id INTEGER,
            last_observed_at TEXT,
            last_price REAL,
            last_price_observation_id INTEGER,
            last_price_observed_at TEXT,
            exit_observation_id INTEGER,
            exit_price REAL,
            exit_cost REAL,
            exit_reason TEXT,
            closed_at TEXT,
            gross_pnl REAL,
            net_pnl REAL,
            UNIQUE(candidate_id, episode_key, control_for_signal_id)
        )""")
    for table in ("memecoin_candidate_signals", "memecoin_candidate_positions"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "evidence_stage" not in columns:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN evidence_stage TEXT NOT NULL DEFAULT 'candidate'",
            )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_candidate_fills (
            fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            candidate_id TEXT NOT NULL,
            observation_id INTEGER,
            side TEXT NOT NULL CHECK(side IN ('entry','exit')),
            price REAL NOT NULL CHECK(price>0),
            notional REAL NOT NULL CHECK(notional>=0),
            cost REAL NOT NULL CHECK(cost>=0),
            source TEXT NOT NULL,
            filled_at TEXT NOT NULL,
            UNIQUE(position_id, side)
        )""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_fill_immutable
        BEFORE UPDATE ON memecoin_candidate_fills
        BEGIN SELECT RAISE(ABORT, 'candidate fill is immutable'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_fill_no_delete
        BEFORE DELETE ON memecoin_candidate_fills
        BEGIN SELECT RAISE(ABORT, 'candidate fill ledger is append-only'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_position_no_delete
        BEFORE DELETE ON memecoin_candidate_positions
        BEGIN SELECT RAISE(ABORT, 'candidate position ledger is append-only'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_position_entry_immutable
        BEFORE UPDATE OF candidate_id,signal_id,episode_key,network_id,mint,
            evidence_stage,control_for_signal_id,execution_mode,timecap_s,entry_observation_id,
            entry_price,entry_notional,quantity,entry_cost,opened_at
        ON memecoin_candidate_positions
        BEGIN SELECT RAISE(ABORT, 'candidate position entry is immutable'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_position_closed_immutable
        BEFORE UPDATE ON memecoin_candidate_positions WHEN OLD.status='closed'
        BEGIN SELECT RAISE(ABORT, 'closed candidate position is immutable'); END
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_candidate_reads (
            read_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            read_type TEXT NOT NULL,
            asof TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_candidate_stages (
            stage_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            stage_name TEXT NOT NULL CHECK(stage_name='first_class_paper'),
            approved_at TEXT NOT NULL,
            approval_read_id TEXT NOT NULL UNIQUE,
            evidence_cutoff TEXT NOT NULL,
            execution_mode TEXT NOT NULL CHECK(execution_mode='paper'),
            created_at TEXT NOT NULL,
            UNIQUE(candidate_id, stage_name)
        )""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_read_immutable
        BEFORE UPDATE ON memecoin_candidate_reads
        BEGIN SELECT RAISE(ABORT, 'candidate read is immutable'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_read_no_delete
        BEFORE DELETE ON memecoin_candidate_reads
        BEGIN SELECT RAISE(ABORT, 'candidate read ledger is append-only'); END
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_candidate_one_fixed_read "
                 "ON memecoin_candidate_reads(candidate_id) WHERE read_type='fixed_candidate'")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_stage_immutable
        BEFORE UPDATE ON memecoin_candidate_stages
        BEGIN SELECT RAISE(ABORT, 'candidate stage is immutable'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_stage_no_delete
        BEFORE DELETE ON memecoin_candidate_stages
        BEGIN SELECT RAISE(ABORT, 'candidate stage ledger is append-only'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS mc_candidate_promotion_requires_stage
        BEFORE UPDATE OF status ON memecoin_strategy_candidates
        WHEN NEW.status='promoted_first_class' AND OLD.status!='promoted_first_class'
             AND NOT EXISTS (SELECT 1 FROM memecoin_candidate_stages
                              WHERE candidate_id=NEW.candidate_id
                                AND stage_name='first_class_paper')
        BEGIN SELECT RAISE(ABORT, 'first-class promotion requires approved stage'); END
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_candidate_primary_episode "
                 "ON memecoin_candidate_positions(candidate_id,episode_key) "
                 "WHERE control_for_signal_id IS NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_candidate_control_episode "
                 "ON memecoin_candidate_positions(candidate_id,episode_key,control_for_signal_id) "
                 "WHERE control_for_signal_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_candidate_signal_time "
                 "ON memecoin_candidate_signals(candidate_id, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_candidate_position_open "
                 "ON memecoin_candidate_positions(candidate_id, status, opened_at)")
    conn.commit()


def _validate_spec(spec):
    if not isinstance(spec, CandidateSpec):
        raise TypeError("spec must be CandidateSpec")
    if not spec.name or not spec.discovery_cutoff:
        raise ValueError("name and discovery_cutoff are required")
    mode = spec.sizing.get("execution_mode")
    if mode != "paper":
        raise ValueError("candidate execution_mode must be paper")
    timecap = spec.exit_policy.get("timecap_s")
    if isinstance(timecap, bool):
        raise ValueError("candidate requires a positive whole-second time cap")
    try:
        numeric = float(timecap)
    except (TypeError, ValueError):
        raise ValueError("candidate requires a positive whole-second time cap") from None
    if not math.isfinite(numeric) or numeric <= 0 or not numeric.is_integer():
        raise ValueError("candidate requires a positive whole-second time cap")
    if int(spec.capture_version_floor) < 2:
        raise ValueError("candidate requires capture version 2 or newer")
    try:
        canonical_params_hash(spec)
    except (TypeError, ValueError):
        raise ValueError("candidate parameters must be finite canonical JSON") from None
    base_alpha = spec.gates.get("base_alpha", 0.05)
    try:
        base_alpha = float(base_alpha)
    except (TypeError, ValueError):
        raise ValueError("base_alpha must be between zero and one") from None
    if not math.isfinite(base_alpha) or not 0 < base_alpha < 1:
        raise ValueError("base_alpha must be between zero and one")
    return int(numeric), base_alpha


def _candidate_from_row(row):
    return Candidate(
        candidate_id=row["candidate_id"], name=row["name"], status=row["status"],
        spawn_index=int(row["spawn_index"]), generation=int(row["generation"]),
        parent_id=row["parent_id"], predicate=json.loads(row["predicate_json"]),
        exit_policy=json.loads(row["exit_policy_json"]), sizing=json.loads(row["sizing_json"]),
        costs=json.loads(row["costs_json"]), controls=json.loads(row["controls_json"]),
        gates=json.loads(row["gates_json"]), params_hash=row["params_hash"],
        born_at=row["born_at"], discovery_cutoff=row["discovery_cutoff"],
        capture_version_floor=int(row["capture_version_floor"]),
        execution_mode=row["execution_mode"], timecap_s=int(row["timecap_s"]),
        alpha_alloc=float(row["alpha_alloc"]), notes=row["notes"],
    )


def enroll_candidate(conn, spec, now_iso):
    timecap_s, base_alpha = _validate_spec(spec)
    params_hash = canonical_params_hash(spec)
    existing = conn.execute(
        "SELECT * FROM memecoin_strategy_candidates WHERE params_hash=?", (params_hash,),
    ).fetchone()
    if existing:
        return _candidate_from_row(existing)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM memecoin_strategy_candidates WHERE params_hash=?", (params_hash,),
        ).fetchone()
        if existing:
            conn.commit()
            return _candidate_from_row(existing)
        spawn_index = int(conn.execute(
            "SELECT COALESCE(MAX(spawn_index),0)+1 FROM memecoin_strategy_candidates",
        ).fetchone()[0])
        active = int(conn.execute(
            "SELECT COUNT(*) FROM memecoin_strategy_candidates WHERE status='candidate_active'",
        ).fetchone()[0])
        status = "candidate_active" if active < ACTIVE_CAP else "candidate_waiting"
        alpha_alloc = base_alpha / (spawn_index * (spawn_index + 1))
        candidate_id = "mc-" + params_hash[:24]
        conn.execute(
            "INSERT INTO memecoin_strategy_candidates(candidate_id,name,status,spawn_index,"
            "generation,parent_id,predicate_json,exit_policy_json,sizing_json,costs_json,"
            "controls_json,gates_json,params_hash,born_at,discovery_cutoff,capture_version_floor,"
            "execution_mode,timecap_s,alpha_alloc,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (candidate_id, spec.name, status, spawn_index, spec.generation, spec.parent_id,
             _canonical(spec.predicate), _canonical(spec.exit_policy), _canonical(spec.sizing),
             _canonical(spec.costs), _canonical(spec.controls), _canonical(spec.gates), params_hash,
             now_iso, spec.discovery_cutoff, spec.capture_version_floor, "paper", timecap_s,
             alpha_alloc, spec.notes),
        )
        row = conn.execute(
            "SELECT * FROM memecoin_strategy_candidates WHERE candidate_id=?", (candidate_id,),
        ).fetchone()
        conn.commit()
        return _candidate_from_row(row)
    except Exception:
        conn.rollback()
        raise


def get_candidate(conn, candidate_id):
    row = conn.execute(
        "SELECT * FROM memecoin_strategy_candidates WHERE candidate_id=?", (candidate_id,),
    ).fetchone()
    return _candidate_from_row(row) if row else None


def list_candidates(conn, statuses=None):
    if statuses:
        statuses = tuple(statuses)
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"SELECT * FROM memecoin_strategy_candidates WHERE status IN ({placeholders}) "
            "ORDER BY spawn_index", statuses,
        )
    else:
        rows = conn.execute("SELECT * FROM memecoin_strategy_candidates ORDER BY spawn_index")
    return [_candidate_from_row(row) for row in rows]


def _episode_key(candidate_id, network_id, mint, observed_at, evidence_stage):
    parsed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("episode observed_at must be timezone aware")
    day = parsed.astimezone(timezone.utc).date().isoformat()
    mint = str(mint or "")
    mint = mint.lower() if mint.lower().startswith("0x") else mint
    if not network_id or not mint:
        raise ValueError("episode requires network and mint")
    return f"{candidate_id}|{evidence_stage}|{network_id}|{mint}|{day}"


def record_evaluation(conn, candidate, observation, evaluation, now_iso, control=None, *,
                      commit=True, evidence_stage="candidate"):
    episode_key = _episode_key(
        candidate.candidate_id, observation.get("network_id"), observation.get("mint"),
        observation.get("observed_at"), evidence_stage,
    )
    matched = bool(evaluation.matched)
    reason = evaluation.reason
    if matched and conn.execute(
        "SELECT 1 FROM memecoin_candidate_signals WHERE candidate_id=? AND episode_key=? "
        "AND matched=1", (candidate.candidate_id, episode_key),
    ).fetchone():
        matched = False
        reason = "episode_duplicate"
    elif matched and candidate.controls.get("enabled") and control is None:
        reason = "control_missing"
    observation_identity = observation.get("id") or observation.get("upstream_event_id")
    signal_id = "sig-" + hashlib.sha256(
        f"{candidate.candidate_id}|{evidence_stage}|{observation_identity}".encode("utf-8"),
    ).hexdigest()[:24]
    control_id = control.observation_id if control is not None else None
    stored_mint = str(observation.get("mint") or "")
    stored_mint = stored_mint.lower() if stored_mint.lower().startswith("0x") else stored_mint
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_candidate_signals(signal_id,candidate_id,"
        "upstream_observation_id,control_observation_id,episode_key,evidence_stage,"
        "network_id,mint,matched,"
        "reason,feature_json,source_observation_ids_json,params_hash,observed_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (signal_id, candidate.candidate_id, observation.get("id"), control_id, episode_key,
         evidence_stage, str(observation.get("network_id")), stored_mint, 1 if matched else 0,
         reason, _canonical(evaluation.features), _canonical(list(evaluation.source_observation_ids)),
         candidate.params_hash, observation.get("observed_at"), now_iso),
    )
    if commit:
        conn.commit()
    row = conn.execute(
        "SELECT * FROM memecoin_candidate_signals WHERE signal_id=?", (signal_id,),
    ).fetchone()
    return dict(row)
