from __future__ import annotations

from sniper.research.wallet_execution.publication import require_study

import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass
from datetime import datetime

from .store import get_candidate


@dataclass(frozen=True)
class CandidateRead:
    read_id: str
    candidate_id: str
    status: str
    closes: int
    distinct_coins: int
    distinct_days: int
    paired_closes: int
    paired_coins: int
    mean_delta: float | None
    median_delta: float | None
    lower_bound: float | None
    p_value: float | None
    alpha: float
    deficits: dict


@dataclass(frozen=True)
class CandidateStage:
    stage_id: str
    candidate_id: str
    stage_name: str
    approved_at: str
    approval_read_id: str
    evidence_cutoff: str
    execution_mode: str


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("statistics timestamp must be timezone aware")
    return parsed


def paired_sign_flip_pvalue(deltas, *, samples=65_536):
    values = [float(value) for value in deltas if math.isfinite(float(value))]
    if not values:
        return 1.0
    target = sum(values)
    magnitudes = [abs(value) for value in values]
    count = 0
    n = len(magnitudes)
    tolerance = 1e-15 * max(1.0, abs(target))
    if n <= 20:
        total = 1 << n
        signed_sum = -sum(magnitudes)
        previous_gray = 0
        for index in range(total):
            if index:
                gray = index ^ (index >> 1)
                changed = gray ^ previous_gray
                bit = changed.bit_length() - 1
                signed_sum += 2 * magnitudes[bit] if gray & changed else -2 * magnitudes[bit]
                previous_gray = gray
            if signed_sum >= target - tolerance:
                count += 1
        return count / total
    seed = int(hashlib.sha256(_canonical(values).encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    for _ in range(samples):
        signed_sum = sum(value if rng.getrandbits(1) else -value for value in magnitudes)
        count += signed_sum >= target - tolerance
    return (count + 1) / (samples + 1)


def _lower_bound(values, alpha):
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    std = statistics.stdev(values)
    if std == 0:
        return mean
    z = statistics.NormalDist().inv_cdf(1.0 - float(alpha))
    return mean - z * std / math.sqrt(len(values))


def _candidate_positions(conn, candidate_id, asof, stage="candidate"):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM memecoin_candidate_positions "
        "WHERE candidate_id=? AND evidence_stage=? AND control_for_signal_id IS NULL "
        "AND status='closed' AND closed_at<=? ORDER BY closed_at,position_id",
        (candidate_id, stage, asof),
    )]


def _paired_positions(conn, candidate_id, asof, stage="candidate"):
    return [dict(row) for row in conn.execute(
        "SELECT p.position_id,p.signal_id,p.network_id,p.mint,p.closed_at,"
        "p.entry_notional,p.net_pnl,c.position_id control_position_id,"
        "c.entry_notional control_notional,c.net_pnl control_pnl "
        "FROM memecoin_candidate_positions p JOIN memecoin_candidate_positions c "
        "ON c.candidate_id=p.candidate_id AND c.evidence_stage=p.evidence_stage "
        "AND c.control_for_signal_id=p.signal_id AND c.status='closed' "
        "WHERE p.candidate_id=? AND p.evidence_stage=? AND p.control_for_signal_id IS NULL "
        "AND p.status='closed' AND p.closed_at<=? AND c.closed_at<=? "
        "ORDER BY p.closed_at,p.position_id",
        (candidate_id, stage, asof, asof),
    )]


def _read_from_manifest(read_id, candidate_id, manifest):
    metrics = manifest["metrics"]
    return CandidateRead(
        read_id=read_id, candidate_id=candidate_id, status="blocked_protocol",
        closes=metrics["closes"], distinct_coins=metrics["distinct_coins"],
        distinct_days=metrics["distinct_days"], paired_closes=metrics["paired_closes"],
        paired_coins=metrics["paired_coins"], mean_delta=metrics["mean_delta"],
        median_delta=metrics["median_delta"], lower_bound=metrics["lower_bound"],
        p_value=metrics["p_value"], alpha=manifest["alpha"], deficits={**manifest["deficits"], "protocol_missing_or_legacy": {"have": 0, "need": 1}},
    )


def candidate_read(conn, candidate_id, asof):
    candidate = get_candidate(conn, candidate_id)
    if candidate is None:
        raise ValueError("candidate not found")
    _time(asof)
    fixed = conn.execute(
        "SELECT read_id,manifest_json FROM memecoin_candidate_reads "
        "WHERE candidate_id=? AND read_type='fixed_candidate'", (candidate_id,),
    ).fetchone()
    if fixed:
        return _read_from_manifest(fixed["read_id"], candidate_id, json.loads(fixed["manifest_json"]))
    positions = [row for row in _candidate_positions(conn, candidate_id, asof)
                 if row["opened_at"] >= candidate.discovery_cutoff]
    pairs = [row for row in _paired_positions(conn, candidate_id, asof)
             if row["closed_at"] >= candidate.discovery_cutoff]
    coins = {(row["network_id"], row["mint"]) for row in positions}
    days = {_time(row["closed_at"]).date().isoformat() for row in positions}
    deltas_by_coin = {}
    for row in pairs:
        candidate_return = float(row["net_pnl"]) / float(row["entry_notional"])
        control_return = float(row["control_pnl"]) / float(row["control_notional"])
        deltas_by_coin.setdefault((row["network_id"], row["mint"]), []).append(
            candidate_return - control_return,
        )
    coin_deltas = [statistics.fmean(values) for _, values in sorted(deltas_by_coin.items())]
    mean_delta = statistics.fmean(coin_deltas) if coin_deltas else None
    median_delta = statistics.median(coin_deltas) if coin_deltas else None
    p_value = paired_sign_flip_pvalue(coin_deltas) if coin_deltas else None
    lower_bound = _lower_bound(coin_deltas, candidate.alpha_alloc) if coin_deltas else None
    gates = candidate.gates
    requirements = {
        "closes": (len(positions), int(gates.get("read_closes", 40))),
        "distinct_coins": (len(coins), int(gates.get("read_coins", 20))),
        "distinct_days": (len(days), int(gates.get("read_days", 10))),
        "paired_closes": (len(pairs), int(gates.get("read_closes", 40))),
        "paired_coins": (len(coin_deltas), int(gates.get("read_coins", 20))),
    }
    deficits = {
        name: {"have": have, "need": need, "short": max(0, need - have)}
        for name, (have, need) in requirements.items() if have < need
    }
    if deficits:
        status = "accruing"
    else:
        passed = (
            mean_delta is not None and mean_delta > 0
            and median_delta is not None and median_delta > 0
            and lower_bound is not None and lower_bound > 0
            and p_value is not None and p_value <= candidate.alpha_alloc
        )
        status = "ready" if passed else "failed"
    evidence = [{
        "position_id": row["position_id"], "control_position_id": row["control_position_id"],
        "network_id": row["network_id"], "mint": row["mint"],
        "candidate_pnl": row["net_pnl"], "control_pnl": row["control_pnl"],
    } for row in pairs]
    evidence_hash = hashlib.sha256(_canonical(evidence).encode()).hexdigest()
    metrics = {
        "closes": len(positions), "distinct_coins": len(coins), "distinct_days": len(days),
        "paired_closes": len(pairs), "paired_coins": len(coin_deltas),
        "mean_delta": mean_delta, "median_delta": median_delta,
        "lower_bound": lower_bound, "p_value": p_value,
    }
    manifest = {
        "status": "blocked_protocol", "diagnostic_status": status, "stage": "candidate", "cutoff": candidate.discovery_cutoff,
        "asof": asof, "alpha": candidate.alpha_alloc, "deficits": deficits,
        "metrics": metrics, "evidence_hash": evidence_hash,
    }
    read_type = "progress"
    read_id = "read-" + hashlib.sha256(
        f"{candidate_id}|{read_type}|{asof}|{evidence_hash}".encode(),
    ).hexdigest()[:24]
    existing = conn.execute(
        "SELECT manifest_json FROM memecoin_candidate_reads WHERE read_id=?", (read_id,),
    ).fetchone()
    if existing:
        return _read_from_manifest(read_id, candidate_id, json.loads(existing[0]))
    conn.execute(
        "INSERT INTO memecoin_candidate_reads(read_id,candidate_id,read_type,asof,manifest_json,"
        "evidence_hash,created_at) VALUES (?,?,?,?,?,?,?)",
        (read_id, candidate_id, read_type, asof, _canonical(manifest), evidence_hash, asof),
    )
    conn.commit()
    return _read_from_manifest(read_id, candidate_id, manifest)


def _fill_active_slots(conn):
    active = int(conn.execute(
        "SELECT COUNT(*) FROM memecoin_strategy_candidates WHERE status='candidate_active'",
    ).fetchone()[0])
    for row in conn.execute(
        "SELECT candidate_id FROM memecoin_strategy_candidates WHERE status='candidate_waiting' "
        "ORDER BY spawn_index LIMIT ?", (max(0, 5 - active),),
    ).fetchall():
        conn.execute(
            "UPDATE memecoin_strategy_candidates SET status='candidate_active' WHERE candidate_id=?",
            (row[0],),
        )


def apply_candidate_kills(conn, asof):
    asof_dt = _time(asof)
    killed = []
    candidates = [dict(row) for row in conn.execute(
        "SELECT * FROM memecoin_strategy_candidates WHERE status='candidate_active' "
        "ORDER BY spawn_index",
    )]
    for row in candidates:
        gates = json.loads(row["gates_json"])
        positions = _candidate_positions(conn, row["candidate_id"], asof)
        pnl = sum(float(position["net_pnl"] or 0) for position in positions)
        mean = pnl / len(positions) if positions else None
        coins = {(position["network_id"], position["mint"]) for position in positions}
        age_days = (asof_dt - _time(row["born_at"])).total_seconds() / 86400.0
        reason = None
        # Sampled mark-model performance cannot kill a strategy candidate.
        performance_eligible = False
        if performance_eligible and pnl <= float(gates.get("kill_pnl", -150)):
            reason = "cumulative_loss"
        elif performance_eligible and len(positions) >= int(gates.get("kill_closes", 25)) and mean <= 0:
            reason = "nonpositive_ev"
        elif (age_days >= int(gates.get("starvation_days", 30))
              and len(coins) < int(gates.get("starvation_coins", 10))):
            reason = "starvation"
        if reason is None:
            continue
        manifest = {
            "status": "killed", "reason": reason, "asof": asof,
            "closes": len(positions), "net_pnl": pnl, "distinct_coins": len(coins),
            "age_days": age_days,
        }
        evidence_hash = hashlib.sha256(_canonical(manifest).encode()).hexdigest()
        read_id = "kill-" + hashlib.sha256(
            f"{row['candidate_id']}|{asof}|{evidence_hash}".encode(),
        ).hexdigest()[:24]
        conn.execute(
            "INSERT OR IGNORE INTO memecoin_candidate_reads(read_id,candidate_id,read_type,asof,"
            "manifest_json,evidence_hash,created_at) VALUES (?,?,'kill',?,?,?,?)",
            (read_id, row["candidate_id"], asof, _canonical(manifest), evidence_hash, asof),
        )
        conn.execute(
            "UPDATE memecoin_strategy_candidates SET status='killed' WHERE candidate_id=?",
            (row["candidate_id"],),
        )
        killed.append(row["candidate_id"])
    _fill_active_slots(conn)
    conn.commit()
    return killed


def _stage_from_row(row):
    return CandidateStage(
        stage_id=row["stage_id"], candidate_id=row["candidate_id"],
        stage_name=row["stage_name"], approved_at=row["approved_at"],
        approval_read_id=row["approval_read_id"], evidence_cutoff=row["evidence_cutoff"],
        execution_mode=row["execution_mode"],
    )


def approve_first_class_paper(conn, candidate_id, approved_at, read_id):
    _time(approved_at)
    # Validate before even returning a previously persisted approval. A cached
    # ready/stage row is history, never proof under the current protocol.
    proof_row = conn.execute(
        "SELECT manifest_json FROM memecoin_candidate_reads WHERE read_id=? AND candidate_id=?",
        (read_id, candidate_id),
    ).fetchone()
    proof = json.loads(proof_row[0]) if proof_row else {}
    require_study(proof.get('execution_study'), proof.get('execution_episodes') or ())
    existing = conn.execute(
        "SELECT * FROM memecoin_candidate_stages WHERE candidate_id=? "
        "AND stage_name='first_class_paper'", (candidate_id,),
    ).fetchone()
    if existing:
        if existing["approval_read_id"] != read_id:
            raise ValueError("candidate already approved from a different fixed read")
        return _stage_from_row(existing)
    candidate = get_candidate(conn, candidate_id)
    if candidate is None or candidate.status != "candidate_active":
        raise ValueError("candidate must be active for first-class approval")
    read = conn.execute(
        "SELECT * FROM memecoin_candidate_reads WHERE read_id=? AND candidate_id=? "
        "AND read_type='fixed_candidate'", (read_id, candidate_id),
    ).fetchone()
    if read is None or json.loads(read["manifest_json"]).get("status") != "ready":
        raise ValueError("approval requires a ready fixed candidate read")
    stage_id = "stage-" + hashlib.sha256(
        f"{candidate_id}|first_class_paper|{approved_at}|{read_id}".encode(),
    ).hexdigest()[:24]
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO memecoin_candidate_stages(stage_id,candidate_id,stage_name,approved_at,"
            "approval_read_id,evidence_cutoff,execution_mode,created_at) "
            "VALUES (?,?,'first_class_paper',?,?,?,'paper',?)",
            (stage_id, candidate_id, approved_at, read_id, approved_at, approved_at),
        )
        conn.execute(
            "UPDATE memecoin_strategy_candidates SET status='promoted_first_class' "
            "WHERE candidate_id=? AND status='candidate_active'", (candidate_id,),
        )
        _fill_active_slots(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _stage_from_row(conn.execute(
        "SELECT * FROM memecoin_candidate_stages WHERE stage_id=?", (stage_id,),
    ).fetchone())
