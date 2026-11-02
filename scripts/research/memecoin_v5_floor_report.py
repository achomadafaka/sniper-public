#!/usr/bin/env python3
"""WO-13 — read-only per-chain evidence-floor / gate-cadence report.

Reports, for each chain, the WO-7 floors against the LIVE DBs and emits CODE_COMPLETE_EVIDENCE_BLOCKED
unless every floor is met. It NEVER opens holdout data, NEVER registers a constant, NEVER mutates
policy — a green report is an operator signal, not an automatic read. Persists the report to a
committed evidence file (not a production DB).
"""
import hashlib
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sniper.util.envfiles import load_env_files
from sniper.util.state_paths import state_root

from sniper.persistence import db_ticks
from sniper.persistence.db_core import get_readonly_connection
from sniper.services import memecoin_v5_replay_gate as gate
from sniper.util.clock import real_utc_now_iso

CHAINS = ("solana", "4663")
OUT = os.path.join(_ROOT, "docs", "superpowers", "evidence", "memecoin_v5_floor_report.json")


def _git_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_ROOT).decode().strip()
    except Exception:
        return "unknown"


def _db_fingerprint(path, tables):
    """Path + size + per-table row counts, hashed — an honest reproducible identity without hashing a
    multi-GB file. Never the file contents byte-for-byte (impractical on the firehose tape)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"path": path, "exists": False}
    counts = {}
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        for tbl in tables:
            try:
                counts[tbl] = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except Exception:
                counts[tbl] = None
        c.close()
    except Exception:
        pass
    fp = hashlib.sha256(json.dumps(
        {"path": path, "size": size, "counts": counts}, sort_keys=True).encode()).hexdigest()
    return {"path": path, "size_bytes": size, "row_counts": counts, "content_fingerprint_sha256": fp}


def _paths():
    """Resolve state paths without mutating process environment.

    Shell values win; state-root env files supply only missing path overrides. This report must be
    structurally read-only and must not change later imports by populating ``os.environ``.
    """
    file_env = {}
    root = state_root()
    load_env_files(str(root), env=file_env)
    def _value(key, default):
        return os.environ.get(key) or file_env.get(key) or str(root / default)
    return {
        "ledger": _value("DB_PATH", "sniper_tracker.db"),
        "4663": _value("MEMECOIN_TICKS_DB", db_ticks.DEFAULT_TICKS_DB),
        "solana": _value("MEMECOIN_SOL_TAPE_DB", db_ticks.DEFAULT_SOL_TAPE_DB),
    }


def main():
    paths = _paths()
    mconn = get_readonly_connection(paths["ledger"])
    tape_conns = {chain: db_ticks.connect_ticks_readonly(paths[chain]) for chain in CHAINS}
    calibration_samples = {
        chain: len(gate.real_close_samples(mconn, chain)) for chain in CHAINS}
    report = gate.floor_report(
        mconn, tape_conns, chains=CHAINS, calibration_samples=calibration_samples)

    manifest = gate.policy_manifest(
        baseline_policy_sha="PENDING_PREV_V5_REPLAY", candidate_policy_sha="PENDING_CANDIDATE_V5_REPLAY",
        evidence_cutoff="SEALED_NOT_SET_evidence_blocked")
    report["policy_manifest"] = manifest
    report["policy_sha256"] = gate.policy_sha256(manifest)
    report["as_of_corrected_utc"] = real_utc_now_iso()
    report["code_sha"] = _git_sha()
    report["engine_version"] = gate.ENGINE_VERSION
    report["fill_model_version"] = gate.FILL_MODEL_VERSION
    report["evidence_dbs"] = {
        "ledger": _db_fingerprint(paths["ledger"],
                                  ("memecoin_positions", "memecoin_fills")),
        "tapes": {
            chain: _db_fingerprint(paths[chain], ("pool_ticks", "block_times"))
            for chain in CHAINS
        },
    }
    report["sealed_train_holdout_cutoff"] = None    # NOT set — no read until floors are met + operator
    for chain in CHAINS:
        cs = report["chains"][chain]
        cs["lp_removal_kind"] = gate._lp_kind_for(chain)
        cs["exclusions"] = (
            "provisional (tx_sig NULL), missing exact same-tick preview, and mid-only marks "
            "are NOT evidence")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(f"VERDICT: {report['verdict']}")
    for chain in CHAINS:
        cs = report["chains"][chain]
        m = cs["metrics"]
        print(f"  {chain}: position_days={m['position_days']} closed_overlapping={m['closed_overlapping']} "
              f"decoded_lp_removals={m['decoded_lp_removals']} calib_samples={cs['calibration_sample_count']} "
              f"floors_met={cs['floors_met']}")
    print(f"  holdout_read={report['holdout_read']} policy_mutated={report['policy_mutated']}")
    print(f"  written: {os.path.relpath(OUT, _ROOT)}")
    for conn in tape_conns.values():
        conn.close()
    mconn.close()


if __name__ == "__main__":
    main()
