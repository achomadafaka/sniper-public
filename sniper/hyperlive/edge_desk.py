"""Read-only Hyperliquid Edge Desk state assembly.

This module never migrates schemas or calls exchange APIs. It derives UI state from the
paper ledger, locally persisted source snapshots and marks, and sealed runtime artifacts.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import statistics
import sqlite3
from pathlib import Path


STALE_AFTER_MS = 300_000
FUTURE_TOLERANCE_MS = 60_000
EDGE_DESK_SCHEMA_VERSION = 3
POSITION_STATES = (
    "current_lane",
    "draining_legacy",
    "reconcile_ready",
    "reconcile_blocked",
    "source_unverified",
)
MAX_JSON_BYTES = 2_000_000


def comparison_key(symbol: object) -> str:
    """Return a comparison-only coin key without changing the stored symbol."""
    return str(symbol or "").casefold()


def _unique_index(mapping: dict) -> tuple[dict[str, tuple[str, object]], set[str]]:
    index: dict[str, tuple[str, object]] = {}
    collisions: set[str] = set()
    for original, value in mapping.items():
        key = comparison_key(original)
        if not key:
            continue
        if key in index:
            index.pop(key, None)
            collisions.add(key)
        elif key not in collisions:
            index[key] = (str(original), value)
    return index, collisions


def _fresh(
    ts_ms: object,
    *,
    now_ms: int,
    stale_after_ms: int,
    future_tolerance_ms: int,
) -> bool:
    try:
        age = int(now_ms) - int(ts_ms)
    except (TypeError, ValueError):
        return False
    return -int(future_tolerance_ms) <= age <= int(stale_after_ms)


def _load_source_state(row: sqlite3.Row | None) -> tuple[dict | None, str | None]:
    if row is None:
        return None, "missing_source_state"
    try:
        positions = json.loads(row["positions_json"])
    except (TypeError, json.JSONDecodeError):
        return None, "invalid_source_positions"
    if not isinstance(positions, dict):
        return None, "invalid_source_positions"
    return positions, None


def _unrealized_bps(side: object, entry: object, mark: object) -> float | None:
    try:
        entry_value = float(entry)
        mark_value = float(mark)
    except (TypeError, ValueError):
        return None
    if entry_value <= 0 or mark_value <= 0:
        return None
    gross = (mark_value - entry_value) / entry_value * 10_000.0
    if str(side or "").lower() == "short":
        gross = -gross
    return round(gross, 2)


def _latest_marks(conn: sqlite3.Connection) -> tuple[dict[str, tuple[str, object]], set[str]]:
    rows = conn.execute(
        "SELECT m.coin, m.mark, m.ts_ms FROM hl_altshort_regime_marks m "
        "JOIN (SELECT coin, MAX(ts_ms) AS max_ts FROM hl_altshort_regime_marks GROUP BY coin) x "
        "ON x.coin=m.coin AND x.max_ts=m.ts_ms"
    ).fetchall()
    return _unique_index({row["coin"]: dict(row) for row in rows})


def classify_open_positions(
    conn: sqlite3.Connection,
    *,
    now_ms: int,
    current_arm_id: str | None,
    copy_heartbeat: dict | None,
    stale_after_ms: int = STALE_AFTER_MS,
    future_tolerance_ms: int = FUTURE_TOLERANCE_MS,
    include_groups: bool = True,
) -> dict:
    """Classify every open paper-copy row from current local evidence.

    The function is query-only and preserves stored coin spelling in every result row.
    """
    groups: dict[str, list[dict]] = {state: [] for state in POSITION_STATES}
    counts = {state: 0 for state in POSITION_STATES}
    targets = {
        row["wallet"]: bool(row["active"])
        for row in conn.execute("SELECT wallet, active FROM hl_copy_targets")
    }
    source_rows = {
        row["wallet"]: row
        for row in conn.execute(
            "SELECT wallet, positions_json, ts_ms FROM hl_copy_trader_state"
        )
    }
    arm_copy_ids: set[int] = set()
    if current_arm_id:
        arm_copy_ids = {
            int(row["copy_id"])
            for row in conn.execute(
                "SELECT copy_id FROM hl_altshort_observations WHERE arm_id=?",
                (current_arm_id,),
            )
        }
    mark_index, mark_collisions = _latest_marks(conn)
    marks_fresh = bool(copy_heartbeat) and _fresh(
        (copy_heartbeat or {}).get("ts_ms"),
        now_ms=now_ms,
        stale_after_ms=stale_after_ms,
        future_tolerance_ms=future_tolerance_ms,
    )

    open_rows = conn.execute(
        "SELECT * FROM hl_copy_book WHERE status='open' "
        "ORDER BY detect_ts_ms DESC, id DESC"
    ).fetchall()
    for source_row in open_rows:
        book = dict(source_row)
        coin_key = comparison_key(book.get("coin"))
        target = str(book.get("target") or "")
        target_active = bool(targets.get(target, False))
        current_arm = int(book["id"]) in arm_copy_ids
        state_row = source_rows.get(target)
        source_sync_ts_ms = state_row["ts_ms"] if state_row is not None else None
        positions, source_error = _load_source_state(state_row)
        source_fresh = source_error is None and _fresh(
            source_sync_ts_ms,
            now_ms=now_ms,
            stale_after_ms=stale_after_ms,
            future_tolerance_ms=future_tolerance_ms,
        )

        source_matches = False
        source_collision = False
        if positions is not None:
            source_index, source_collisions = _unique_index(positions)
            source_collision = coin_key in source_collisions
            source_pair = source_index.get(coin_key)
            if source_pair is not None and isinstance(source_pair[1], dict):
                source_matches = (
                    str(source_pair[1].get("side") or "").lower()
                    == str(book.get("side") or "").lower()
                )

        mark_collision = coin_key in mark_collisions
        mark_pair = mark_index.get(coin_key)
        current_mark = None
        if marks_fresh and not mark_collision and mark_pair is not None:
            try:
                candidate = float(mark_pair[1]["mark"])
            except (KeyError, TypeError, ValueError):
                candidate = 0.0
            if candidate > 0:
                current_mark = candidate
        mark_available = current_mark is not None

        if source_error:
            state, reason = "source_unverified", source_error
        elif not source_fresh:
            state, reason = "source_unverified", "stale_or_future_source_sync"
        elif source_collision:
            state, reason = "source_unverified", "ambiguous_source_coin_key"
        elif mark_collision:
            state, reason = "source_unverified", "ambiguous_mark_coin_key"
        elif not source_matches and not mark_available:
            state, reason = "reconcile_blocked", "source_exited_mark_unavailable"
        elif not source_matches:
            state, reason = "reconcile_ready", "source_exited_runner_not_reconciled"
        elif current_arm and target_active:
            state, reason = "current_lane", "current_arm_source_held"
        elif not target_active:
            state, reason = "draining_legacy", "inactive_target_source_held"
        else:
            state, reason = "draining_legacy", "legacy_provenance_source_held"

        result = {
            "id": int(book["id"]),
            "target": target,
            "coin": book.get("coin"),
            "side": book.get("side"),
            "entry_mark": book.get("entry_mark"),
            "detect_ts_ms": book.get("detect_ts_ms"),
            "is_cold_start": bool(book.get("is_cold_start")),
            "target_active": target_active,
            "current_arm": current_arm,
            "source_sync_ts_ms": source_sync_ts_ms,
            "source_fresh": source_fresh,
            "source_matches": source_matches,
            "mark_available": mark_available,
            "current_mark": current_mark,
            "unrealized_bps": _unrealized_bps(
                book.get("side"), book.get("entry_mark"), current_mark
            ),
            "state": state,
            "reason": reason,
        }
        counts[state] += 1
        if include_groups:
            groups[state].append(result)

    payload = {"counts": counts, "total": sum(counts.values())}
    if include_groups:
        payload["groups"] = groups
    return payload


def _empty_positions() -> dict:
    groups = {state: [] for state in POSITION_STATES}
    return {
        "counts": {state: 0 for state in POSITION_STATES},
        "groups": groups,
        "total": 0,
    }


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None, "too_large"
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, json.JSONDecodeError):
        return None, "malformed"
    return (value, None) if isinstance(value, dict) else (None, "not_object")


def _heartbeat_ts_ms(payload: dict) -> int | None:
    try:
        if payload.get("ts_ms") is not None:
            return int(payload["ts_ms"])
        if payload.get("ts") is not None:
            return int(float(payload["ts"]) * 1000)
    except (TypeError, ValueError):
        return None
    return None


def _heartbeat_freshness(payload: dict, *, now_ms: int) -> tuple[bool, int | None]:
    ts_ms = _heartbeat_ts_ms(payload)
    if ts_ms is None:
        return False, None
    age_ms = int(now_ms) - ts_ms
    return (
        -FUTURE_TOLERANCE_MS <= age_ms <= STALE_AFTER_MS,
        age_ms,
    )


def _paper_stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean_bps": None, "median_bps": None, "win_rate": None}
    return {
        "n": len(values),
        "mean_bps": round(sum(values) / len(values), 2),
        "median_bps": round(statistics.median(values), 2),
        "win_rate": round(sum(value > 0 for value in values) / len(values), 4),
    }


def read_edge_paper(*, data_dir: Path, now_ms: int) -> dict:
    """Read the dedicated forward edge ledger without migrating or writing it."""
    data_dir = Path(data_dir)
    heartbeat, heartbeat_error = _read_json(data_dir / "hl_altshort_paper.heartbeat")
    if heartbeat_error or heartbeat is None:
        return {
            "available": False, "fresh": False,
            "reason": f"heartbeat_{heartbeat_error or 'missing'}",
        }
    fresh, age_ms = _heartbeat_freshness(heartbeat, now_ms=now_ms)
    arm = heartbeat.get("arm_id")
    base = {"available": False, "fresh": fresh, "arm_id": arm, "age_ms": age_ms}
    db_path = data_dir / "hl_altshort_paper.db"
    if not db_path.exists():
        return {**base, "reason": "database_missing"}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        required = {
            "hl_paper_arms": {"arm_id", "policy_hash", "policy_json", "clip_usd", "max_concurrent"},
            "hl_paper_observations": {
                "arm_id", "gated_in", "portfolio_slot_eligible", "entry_status", "regime_label",
            },
            "hl_paper_regimes": {"arm_id", "decision_ts_ms", "label", "coverage"},
            "hl_paper_positions": {
                "arm_id", "status", "net_bps_clip", "net_bps_ref", "fill_delta_bps",
                "funding_status",
            },
        }
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not set(required).issubset(tables):
            return {**base, "reason": "schema_missing"}
        for table, columns in required.items():
            present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(present):
                return {**base, "reason": "schema_missing"}
        obs_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(hl_paper_observations)")
        }
        episode_select = (
            "episode_primary" if "episode_primary" in obs_columns
            else "NULL AS episode_primary"
        )
        episode_id_select = (
            "episode_id" if "episode_id" in obs_columns else "NULL AS episode_id"
        )
        arm_row = conn.execute("SELECT * FROM hl_paper_arms WHERE arm_id=?", (arm,)).fetchone()
        if arm_row is None:
            return {**base, "reason": "persisted_arm_missing"}
        if heartbeat.get("policy_hash") != arm_row["policy_hash"]:
            return {**base, "reason": "policy_hash_mismatch"}
        try:
            policy = json.loads(arm_row["policy_json"])
        except (TypeError, json.JSONDecodeError):
            return {**base, "reason": "policy_json_invalid"}

        obs_rows = conn.execute(
            "SELECT gated_in,portfolio_slot_eligible,entry_status,regime_label,"
            f"{episode_select},{episode_id_select} "
            "FROM hl_paper_observations "
            "WHERE arm_id=?", (arm,),
        ).fetchall()
        pos_columns = {row[1] for row in conn.execute("PRAGMA table_info(hl_paper_positions)")}
        funding_select = (
            "COALESCE(p.funding_provenance_status,p.funding_status)"
            if "funding_provenance_status" in pos_columns
            else "p.funding_status"
        )
        pos_rows = conn.execute(
            "SELECT p.status,p.net_bps_clip,p.net_bps_ref,p.fill_delta_bps,"
            f"{funding_select} AS funding_status,"
            "o.regime_label FROM hl_paper_positions p JOIN hl_paper_observations o "
            "ON o.obs_id=p.obs_id WHERE p.arm_id=?", (arm,),
        ).fetchall()
        regime_hours = conn.execute(
            "SELECT COUNT(*) FROM hl_paper_regimes WHERE arm_id=?", (arm,)
        ).fetchone()[0]
        counts = {
            "observations": len(obs_rows),
            "accepted": sum(int(row["gated_in"] or 0) for row in obs_rows),
            "stood_down": sum(row["entry_status"] == "stood_down" for row in obs_rows),
            "opened": sum(row["entry_status"] == "opened" for row in obs_rows),
            "rejected": sum(row["entry_status"] == "rejected" for row in obs_rows),
            "portfolio_eligible": sum(int(row["portfolio_slot_eligible"] or 0) for row in obs_rows),
            "episode_repeats": sum(
                row["episode_id"] is not None and row["episode_primary"] == 0
                for row in obs_rows
            ),
            "open": sum(row["status"] == "open" for row in pos_rows),
            "closed": sum(row["status"] == "closed" for row in pos_rows),
            "funding_complete": sum(row["status"] == "closed" and row["funding_status"] in
                                    {"complete", "not_applicable"}
                                    for row in pos_rows),
            "funding_missing": sum(row["status"] == "closed" and row["funding_status"] not in
                                   {"complete", "not_applicable"}
                                   for row in pos_rows),
        }
        closed = [row for row in pos_rows if row["status"] == "closed"]
        clip = [float(row["net_bps_clip"]) for row in closed if row["net_bps_clip"] is not None]
        reference = [float(row["net_bps_ref"]) for row in closed if row["net_bps_ref"] is not None]
        fill_delta = [float(row["fill_delta_bps"]) for row in closed
                      if row["fill_delta_bps"] is not None]
        closed_by_label = Counter(str(row["regime_label"] or "unknown") for row in closed)
        complete_by_label = Counter(
            str(row["regime_label"] or "unknown") for row in closed
            if row["net_bps_clip"] is not None
        )
        return {
            "available": True,
            "fresh": fresh,
            "age_ms": age_ms,
            "reason": "ok" if fresh else "heartbeat_stale_or_future",
            "arm_id": arm,
            "policy_hash": arm_row["policy_hash"],
            "policy": policy.get("policy", policy),
            "clip_usd": float(arm_row["clip_usd"]),
            "reference_notional_usd": 5_000.0,
            "max_concurrent": int(arm_row["max_concurrent"]),
            "universe": int(heartbeat.get("universe") or 0),
            "counts": counts,
            "performance": {
                "clip": _paper_stats(clip),
                "reference": _paper_stats(reference),
                "fill_delta": _paper_stats(fill_delta),
            },
            "regime_evidence": {
                "recorded_hours": int(regime_hours),
                "closed_by_label": dict(sorted(closed_by_label.items())),
                "complete_net_by_label": dict(sorted(complete_by_label.items())),
            },
            "milestones": {
                "fill_calibration_target": 25,
                "proof_target": 200,
                "fill_calibration_closes": counts["closed"],
                "complete_net_closes": len(clip),
                "regime_label_target": 2,
                "min_closes_per_regime_label": 20,
                "represented_regime_labels": sum(
                    count >= 20 for count in complete_by_label.values()
                ),
            },
        }
    except sqlite3.Error:
        return {**base, "reason": "database_unreadable"}
    finally:
        if conn is not None:
            conn.close()


def _arm_runtime(
    *,
    manifest: dict | None,
    manifest_error: str | None,
    heartbeat: dict | None,
    heartbeat_error: str | None,
    conn: sqlite3.Connection | None,
    now_ms: int,
) -> dict:
    if heartbeat_error:
        return {
            "available": False,
            "fresh": False,
            "reason": f"heartbeat_{heartbeat_error}",
        }
    assert heartbeat is not None
    fresh, age_ms = _heartbeat_freshness(heartbeat, now_ms=now_ms)
    evidence_arm_id = heartbeat.get("evidence_arm_id") or heartbeat.get("arm_id")
    deployment_arm_id = heartbeat.get("deployment_arm_id") or heartbeat.get("arm_id")
    out = {
        "available": True,
        "fresh": fresh,
        "age_ms": age_ms,
        "arm_id": evidence_arm_id,
        "evidence_arm_id": evidence_arm_id,
        "deployment_arm_id": deployment_arm_id,
    }
    for field in ("counts", "open", "closed", "dirty", "code_sha", "policy_hash",
                  "source_fills", "depth_capture_format_version", "depth_capture_scope"):
        if field in heartbeat:
            out[field] = heartbeat[field]
    if not fresh:
        out["reason"] = "heartbeat_stale_or_future"
        return out
    if manifest_error:
        out.update({"fresh": False, "reason": f"manifest_{manifest_error}"})
        return out
    assert manifest is not None
    if deployment_arm_id != manifest.get("arm_id"):
        out.update({"fresh": False, "reason": "heartbeat_manifest_arm_mismatch"})
        return out
    if conn is None:
        out.update({"fresh": False, "reason": "database_missing"})
        return out
    try:
        persisted = conn.execute(
            "SELECT policy_hash, manifest_json FROM hl_altshort_arms WHERE arm_id=?",
            (evidence_arm_id,),
        ).fetchone()
    except sqlite3.Error:
        persisted = None
    if persisted is None:
        out.update({"fresh": False, "reason": "persisted_arm_missing"})
        return out
    if heartbeat.get("policy_hash") != persisted["policy_hash"]:
        out.update({"fresh": False, "reason": "policy_hash_mismatch"})
        return out
    try:
        persisted_manifest = json.loads(persisted["manifest_json"])
    except (TypeError, json.JSONDecodeError):
        out.update({"fresh": False, "reason": "persisted_manifest_invalid"})
        return out
    semantic_fields = ("policy", "targets", "taxonomy_version", "benchmark_version",
                       "regime_version")
    if any(persisted_manifest.get(field) != manifest.get(field) for field in semantic_fields):
        out.update({"fresh": False, "reason": "persisted_manifest_mismatch"})
        return out
    out["reason"] = "ok"
    return out


def _study_payload(*, data_dir: Path, manifest: dict | None, manifest_error: str | None) -> dict:
    if manifest_error or manifest is None:
        return {
            "available": False,
            "integrity": "unavailable",
            "reason": f"manifest_{manifest_error or 'missing'}",
        }
    retrospective = manifest.get("retrospective")
    if not isinstance(retrospective, dict):
        return {"available": False, "integrity": "unavailable", "reason": "report_not_declared"}
    raw_path = retrospective.get("report_path")
    expected_sha = retrospective.get("report_sha256")
    if not raw_path or not expected_sha:
        return {"available": False, "integrity": "unavailable", "reason": "report_identity_missing"}
    repo_root = data_dir.resolve().parent
    report_path = (repo_root / str(raw_path)).resolve()
    try:
        report_path.relative_to(data_dir.resolve())
    except ValueError:
        return {"available": False, "integrity": "failed", "reason": "report_path_outside_data"}
    report, report_error = _read_json(report_path)
    if report_error:
        return {
            "available": False,
            "integrity": "unavailable",
            "reason": f"report_{report_error}",
        }
    if hashlib.sha256(report_path.read_bytes()).hexdigest() != str(expected_sha):
        return {"available": False, "integrity": "failed", "reason": "sha256_mismatch"}
    assert report is not None
    provenance = report.get("provenance") if isinstance(report.get("provenance"), dict) else {}
    return {
        "available": True,
        "integrity": "verified",
        "study_id": report.get("study_id"),
        "verdict": report.get("verdict") or {},
        "events": report.get("events") or {},
        "inference": report.get("inference") or {},
        "regime_replication": report.get("regime_replication") or {},
        "exit_observation_bridge": report.get("exit_observation_bridge") or {},
        "evidence_status": report.get("evidence_status") or "unknown",
        "cache_seal_sha256": provenance.get("cache_seal_sha256"),
        "report_sha256": str(expected_sha),
    }


def _history_coverage(*, data_dir: Path, study: dict) -> dict:
    base = {
        "status": "unknown",
        "integrity": "unavailable",
        "provider_symbols": None,
        "available_candles": None,
        "missing_candles": None,
        "free_archive": "unavailable",
        "evidence_status": study.get("evidence_status") or "unknown",
        "delisted_tail_status": "unknown",
        "symbol_identity": "unknown",
    }
    expected_sha = study.get("cache_seal_sha256")
    if study.get("integrity") != "verified" or not expected_sha:
        return base
    study_dir = Path(data_dir) / "research" / "altshort_study"
    seal_path = None
    for candidate in sorted(study_dir.glob("cache*/_cache_seal.json")):
        try:
            if hashlib.sha256(candidate.read_bytes()).hexdigest() == str(expected_sha):
                seal_path = candidate
                break
        except OSError:
            continue
    if seal_path is None:
        return {**base, "integrity": "seal_missing"}
    seal, error = _read_json(seal_path)
    if error or seal is None:
        return {**base, "integrity": f"seal_{error or 'missing'}"}
    symbols = seal.get("symbols")
    if not isinstance(symbols, list):
        return {**base, "integrity": "seal_schema_invalid"}
    candle_statuses = [
        item.get("candles", {}).get("status")
        for item in symbols
        if isinstance(item, dict) and isinstance(item.get("candles"), dict)
    ]
    available = sum(status == "present" for status in candle_statuses)
    missing = len(symbols) - available
    evidence_status = str(seal.get("evidence_status") or "unknown")
    return {
        "status": "blocked" if missing or evidence_status != "complete" else "complete",
        "integrity": "verified",
        "provider_symbols": len(symbols),
        "available_candles": available,
        "missing_candles": missing,
        "free_archive": "unavailable",
        "evidence_status": evidence_status,
        "delisted_tail_status": str(seal.get("delisted_tail_status") or "unknown"),
        "symbol_identity": str(seal.get("symbol_identity") or "unknown"),
    }


def _source_timing(conn: sqlite3.Connection | None, wallet_copy: dict) -> dict:
    base = {
        "status": "unknown",
        "historical_rows": None,
        "historical_event_reconstructed": None,
        "forward_event_captured": None,
        "forward_receive_captured": None,
        "source_events": None,
        "source_gaps": None,
        "historical_receive_recovery": "irrecoverable",
    }
    if conn is None:
        return base
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total,"
            "SUM(source_latency_status='historical_not_captured') AS historical_rows,"
            "SUM(source_latency_status='historical_event_reconstructed') AS reconstructed,"
            "SUM(source_event_ts_ms IS NOT NULL AND source_latency_status NOT LIKE 'historical%') "
            "AS forward_events,"
            "SUM(source_received_ts_ms IS NOT NULL AND source_latency_status NOT LIKE 'historical%') "
            "AS forward_received FROM hl_copy_book"
        ).fetchone()
        source_events = conn.execute("SELECT COUNT(*) FROM hl_copy_source_events").fetchone()[0]
        source_gaps = conn.execute("SELECT COUNT(*) FROM hl_copy_source_gaps").fetchone()[0]
    except sqlite3.Error:
        return base
    source_health = wallet_copy.get("source_fills") or {}
    return {
        "status": "collecting" if wallet_copy.get("fresh") else "degraded",
        "historical_rows": int(row["historical_rows"] or 0),
        "historical_event_reconstructed": int(row["reconstructed"] or 0),
        "forward_event_captured": int(row["forward_events"] or 0),
        "forward_receive_captured": int(row["forward_received"] or 0),
        "source_events": int(source_events),
        "source_gaps": int(source_gaps),
        "interval_complete": source_health.get("interval_complete"),
        "historical_receive_recovery": "irrecoverable",
    }


def _capacity_scope(conn: sqlite3.Connection | None, wallet_copy: dict) -> dict:
    base = {
        "status": "unknown",
        "captured_entry_books": None,
        "captured_exit_books": None,
        "legacy_15_level_books": None,
        "full_provider_books": None,
        "prospective_format_version": None,
        "prospective_scope": "unknown",
        "exact_depth_limit": "provider_returned_book",
    }
    if conn is None:
        return base
    try:
        row = conn.execute(
            "SELECT "
            "SUM(entry_depth_json IS NOT NULL) AS entries,"
            "SUM(exit_depth_json IS NOT NULL) AS exits,"
            "SUM(CASE WHEN entry_depth_json IS NOT NULL AND json_valid(entry_depth_json) "
            "AND json_extract(entry_depth_json,'$.capture_format_version')=2 THEN 1 ELSE 0 END) + "
            "SUM(CASE WHEN exit_depth_json IS NOT NULL AND json_valid(exit_depth_json) "
            "AND json_extract(exit_depth_json,'$.capture_format_version')=2 THEN 1 ELSE 0 END) AS v2 "
            "FROM hl_copy_book"
        ).fetchone()
    except sqlite3.Error:
        return base
    entries = int(row["entries"] or 0)
    exits = int(row["exits"] or 0)
    full_provider = int(row["v2"] or 0)
    format_version = wallet_copy.get("depth_capture_format_version")
    scope = wallet_copy.get("depth_capture_scope") or "unknown"
    return {
        "status": "collecting" if format_version == 2 and wallet_copy.get("fresh") else "legacy",
        "captured_entry_books": entries,
        "captured_exit_books": exits,
        "legacy_15_level_books": max(0, entries + exits - full_provider),
        "full_provider_books": full_provider,
        "prospective_format_version": format_version,
        "prospective_scope": scope,
        "exact_depth_limit": "provider_returned_book",
    }


def _forward_progress(edge_paper: dict) -> dict:
    milestones = edge_paper.get("milestones") if edge_paper.get("available") else {}
    regime = edge_paper.get("regime_evidence") if edge_paper.get("available") else {}
    return {
        "status": "collecting" if edge_paper.get("fresh") else "degraded",
        "complete_net_closes": milestones.get("complete_net_closes"),
        "fill_calibration_closes": milestones.get("fill_calibration_closes"),
        "fill_calibration_target": milestones.get("fill_calibration_target", 25),
        "proof_target": milestones.get("proof_target", 200),
        "recorded_regime_hours": regime.get("recorded_hours"),
        "represented_regime_labels": milestones.get("represented_regime_labels"),
        "regime_label_target": milestones.get("regime_label_target", 2),
    }


def _copy_book_truth(conn: sqlite3.Connection | None) -> dict:
    if conn is None:
        return {"status": "unknown", "reason": "database_unavailable"}
    try:
        row = conn.execute(
            "SELECT ts_ms,realized_n,realized_mean,total_bps,open_n,top1_share,top5_share "
            "FROM hl_scorecard_snapshots WHERE holdout=0 ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return {"status": "unknown", "reason": "scorecard_schema_unavailable"}
    if row is None:
        return {"status": "unknown", "reason": "scorecard_snapshot_unavailable"}
    return {
        "status": "available",
        "as_of_ts_ms": int(row["ts_ms"]),
        "realized_n": int(row["realized_n"]),
        "realized_mean_bps": row["realized_mean"],
        "open_n": int(row["open_n"]),
        "total_bps": row["total_bps"],
        "top1_share": row["top1_share"],
        "top5_share": row["top5_share"],
    }


def _evidence_debt(
    *, conn: sqlite3.Connection | None, data_dir: Path, study: dict,
    wallet_copy: dict, edge_paper: dict,
) -> dict:
    forward = _forward_progress(edge_paper)
    complete = forward.get("complete_net_closes")
    target = forward.get("proof_target")
    reason = (
        "manual_review_required"
        if isinstance(complete, int) and isinstance(target, int) and complete >= target
        else "forward_evidence_incomplete"
    )
    if edge_paper.get("available") is not True:
        collection = {
            "status": "unavailable",
            "label": "FORWARD TEST UNAVAILABLE",
            "reason": edge_paper.get("reason") or "paper_collector_unavailable",
        }
    elif edge_paper.get("fresh") is True:
        collection = {
            "status": "running",
            "label": "FORWARD TEST RUNNING",
            "reason": "paper_collector_fresh",
        }
    else:
        collection = {
            "status": "stale",
            "label": "FORWARD TEST STALE",
            "reason": edge_paper.get("reason") or "paper_collector_stale",
        }
    return {
        "verdict": {"status": "paused", "label": "PROMOTION PAUSED", "reason": reason},
        "collection": collection,
        "history": _history_coverage(data_dir=data_dir, study=study),
        "source_timing": _source_timing(conn, wallet_copy),
        "capacity": _capacity_scope(conn, wallet_copy),
        "forward": forward,
        "copy_book": _copy_book_truth(conn),
    }


def _real_runtime(*, heartbeat: dict | None, error: str | None, now_ms: int) -> dict:
    if error:
        return {"available": False, "fresh": False, "reason": f"heartbeat_{error}"}
    assert heartbeat is not None
    fresh, age_ms = _heartbeat_freshness(heartbeat, now_ms=now_ms)
    out = {
        "available": True,
        "fresh": fresh,
        "age_ms": age_ms,
        "enabled": bool(heartbeat.get("enabled")),
        "dry_run": bool(heartbeat.get("dry_run")),
        "open_n": int(heartbeat.get("open_n") or 0),
        "pending": int(heartbeat.get("pending") or 0),
        "breaker": heartbeat.get("breaker"),
    }
    out["reason"] = "ok" if fresh else "heartbeat_stale_or_future"
    return out


def _latest_regime(conn: sqlite3.Connection | None, arm_id: str | None) -> dict:
    if conn is None or not arm_id:
        return {"available": False, "reason": "current_arm_unavailable"}
    try:
        row = conn.execute(
            "SELECT decision_ts_ms, regime_json FROM hl_altshort_signal_regimes "
            "WHERE arm_id=? ORDER BY decision_ts_ms DESC LIMIT 1",
            (arm_id,),
        ).fetchone()
    except sqlite3.Error:
        return {"available": False, "reason": "schema_missing"}
    if row is None:
        return {"available": False, "reason": "no_forward_regime"}
    try:
        regime = json.loads(row["regime_json"])
    except (TypeError, json.JSONDecodeError):
        return {"available": False, "reason": "regime_malformed"}
    basket = regime.get("basket_return_bps")
    basket_72h_bps = (
        basket.get("259200000")
        if isinstance(basket, dict)
        else basket if isinstance(basket, (int, float)) and not isinstance(basket, bool)
        else None
    )
    return {
        "available": True,
        "label": regime.get("label") or "unknown",
        "basket_72h_bps": basket_72h_bps,
        "decision_ts_ms": int(row["decision_ts_ms"]),
    }


def _compact_edge_atlas(atlas: dict) -> dict:
    """Keep only fields used by the compact decision surface."""
    if atlas.get("available") is False:
        return {"available": False, "reason": atlas.get("reason") or "unavailable"}
    focus = atlas.get("focus") or {}
    return {
        "available": bool(atlas.get("available")),
        "focus": {"status": focus.get("status") or "unknown"},
        "forward": atlas.get("forward") or {},
        "next_direction": atlas.get("next_direction") or {},
        "methodology": {"warning": (atlas.get("methodology") or {}).get("warning")},
    }


def _promotion_gate(
    *, study: dict, standalone_manifest: dict | None, standalone_runtime: dict
) -> dict:
    verdict = study.get("verdict") if study.get("available") else {}
    replication = study.get("regime_replication") if study.get("available") else {}
    policy = (
        standalone_manifest.get("policy")
        if isinstance(standalone_manifest, dict)
        else {}
    ) or {}
    counts = standalone_runtime.get("counts") or {}
    return {
        "status": verdict.get("status") or "unknown",
        "episode_count": int(replication.get("episode_count") or 0),
        "required_episodes": int(policy.get("min_regime_episodes") or 0),
        "minimum_closes_per_episode": int(policy.get("min_episode_closes") or 0),
        "forward_closes": int(counts.get("closed") or 0),
        "incomplete": list(verdict.get("incomplete") or []),
        "failed": list(verdict.get("failed") or []),
        "blocked": list(verdict.get("blocked") or []),
    }


def _open_database(db_path: str) -> tuple[sqlite3.Connection | None, dict]:
    path = Path(db_path)
    if not path.exists():
        return None, {"available": False, "reason": "missing"}
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        required = {
            "hl_altshort_arms",
            "hl_altshort_observations",
            "hl_altshort_regime_marks",
            "hl_altshort_signal_regimes",
            "hl_copy_book",
            "hl_copy_targets",
            "hl_copy_trader_state",
        }
        present = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not required.issubset(present):
            conn.close()
            return None, {"available": False, "reason": "schema_missing"}
        return conn, {"available": True, "reason": "ok"}
    except sqlite3.Error:
        return None, {"available": False, "reason": "unreadable"}


def _latest_copy_manifest(study_dir: Path) -> tuple[dict | None, str | None]:
    candidates: list[tuple[int, Path]] = []
    for path in study_dir.glob("forward_arm.v*.json"):
        try:
            version = int(path.stem.rsplit(".v", 1)[1])
        except (IndexError, ValueError):
            continue
        candidates.append((version, path))
    if not candidates:
        return None, "missing"
    return _read_json(max(candidates, key=lambda item: item[0])[1])


def _latest_standalone_manifest(study_dir: Path) -> tuple[dict | None, str | None]:
    candidates: list[tuple[int, Path]] = []
    for path in study_dir.glob("standalone_forward_arm.v*.json"):
        try:
            version = int(path.stem.rsplit(".v", 1)[1])
        except (IndexError, ValueError):
            continue
        candidates.append((version, path))
    if not candidates:
        return None, "missing"
    return _read_json(max(candidates, key=lambda item: item[0])[1])


def build_payload(*, db_path: str, data_dir: Path, now_ms: int) -> dict:
    """Assemble the read-only Edge Desk payload from local persisted evidence."""
    from sniper.hyperlive import edge_atlas as EA

    data_dir = Path(data_dir)
    standalone_manifest, standalone_manifest_error = _latest_standalone_manifest(
        data_dir / "research" / "altshort_study"
    )
    copy_manifest, copy_manifest_error = _latest_copy_manifest(
        data_dir / "research" / "altshort_study"
    )
    standalone_heartbeat, standalone_heartbeat_error = _read_json(
        data_dir / "hl_altshort_signal.heartbeat"
    )
    copy_heartbeat, copy_heartbeat_error = _read_json(data_dir / "hl_copy.heartbeat")
    real_heartbeat, real_heartbeat_error = _read_json(data_dir / "hl_real.heartbeat")
    study = _study_payload(
        data_dir=data_dir,
        manifest=standalone_manifest,
        manifest_error=standalone_manifest_error,
    )
    edge_paper = read_edge_paper(data_dir=data_dir, now_ms=now_ms)
    conn, database = _open_database(db_path)
    try:
        standalone = _arm_runtime(
            manifest=standalone_manifest,
            manifest_error=standalone_manifest_error,
            heartbeat=standalone_heartbeat,
            heartbeat_error=standalone_heartbeat_error,
            conn=conn,
            now_ms=now_ms,
        )
        wallet_copy = _arm_runtime(
            manifest=copy_manifest,
            manifest_error=copy_manifest_error,
            heartbeat=copy_heartbeat,
            heartbeat_error=copy_heartbeat_error,
            conn=conn,
            now_ms=now_ms,
        )
        current_copy_arm = wallet_copy.get("arm_id") if wallet_copy.get("fresh") else None
        edge_atlas_full = (
            EA.read_edge_atlas(
                conn,
                current_arm_id=current_copy_arm,
                now_ms=now_ms,
            )
            if conn is not None
            else {"available": False, "reason": "database_missing"}
        )
        edge_atlas = _compact_edge_atlas(edge_atlas_full)
        positions = (
            classify_open_positions(
                conn,
                now_ms=now_ms,
                current_arm_id=current_copy_arm,
                copy_heartbeat=copy_heartbeat,
                include_groups=False,
            )
            if conn is not None
            else _empty_positions()
        )
        current_standalone_arm = (
            standalone.get("arm_id") if standalone.get("fresh") else None
        )
        regime = _latest_regime(conn, current_standalone_arm)
        evidence_debt = _evidence_debt(
            conn=conn,
            data_dir=data_dir,
            study=study,
            wallet_copy=wallet_copy,
            edge_paper=edge_paper,
        )
    finally:
        if conn is not None:
            conn.close()
    return {
        "schema_version": EDGE_DESK_SCHEMA_VERSION,
        "available": True,
        "as_of_ts_ms": int(now_ms),
        "database": database,
        "standalone": standalone,
        "wallet_copy": wallet_copy,
        "real": _real_runtime(
            heartbeat=real_heartbeat,
            error=real_heartbeat_error,
            now_ms=now_ms,
        ),
        "regime": regime,
        "study": study,
        "promotion_gate": _promotion_gate(
            study=study,
            standalone_manifest=standalone_manifest,
            standalone_runtime=standalone,
        ),
        "edge_paper": edge_paper,
        "evidence_debt": evidence_debt,
        "edge_atlas": edge_atlas,
        "positions": positions,
    }
