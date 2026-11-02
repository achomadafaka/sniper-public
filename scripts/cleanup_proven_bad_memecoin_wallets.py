#!/usr/bin/env python3
"""Dry-run-first exclusion of proven-negative, watch-only memecoin wallets.

Apply is intentionally cumbersome: the operator must confirm the preview count, a verified SQLite
backup and target manifest are written first, and the exact target identities are revalidated under
one write lock before every wallet is excluded atomically.

`--reinstate-actor ACTOR` runs the inverse for repairing ONE bad sweep: it un-excludes wallets that
carry an audit row from ACTOR but are not proven-bad on the honest metric. It is deliberately not a
general un-exclude — `excluded=1` stays permanent for every wallet outside the named batch — because
the only thing that justifies reversal is that the original decision used the wrong metric.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.persistence import db_memecoin as memedb  # noqa: E402
from sniper.settings import memecoin as cfg  # noqa: E402
from sniper.util.clock import real_utc_now  # noqa: E402
from sniper.util.state_paths import state_path  # noqa: E402


def open_existing_db(path: Path, *, writable: bool) -> sqlite3.Connection:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    mode = "rw" if writable else "ro"
    conn = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    return conn


def load_targets(conn: sqlite3.Connection, *, min_closes: int, min_pnl: float) -> list[dict]:
    """Return active watch-only wallets below the symmetric canonical promotion floor."""
    stats = memedb.alpha_b_copy_stats_all(conn)
    rows = conn.execute(
        "SELECT source_user_id,handle FROM memecoin_wallets "
        "WHERE COALESCE(real_approved,0)=0 AND COALESCE(excluded,0)=0 AND paused=0 "
        "ORDER BY source_user_id").fetchall()
    targets = []
    for row in rows:
        evidence = stats.get(row["source_user_id"])
        if is_proven_bad(evidence, min_closes=min_closes, min_pnl=min_pnl):
            targets.append({
                "source_user_id": row["source_user_id"],
                "handle": row["handle"],
                "n_closes": int(evidence["n_closes"]),
                "pnl": float(evidence["pnl"]),
            })
    return targets


def is_proven_bad(evidence: dict | None, *, min_closes: int, min_pnl: float) -> bool:
    """The single canonical proven-bad test, on the HONEST (fomo-excluded) alpha_b metric."""
    if not evidence:
        return False
    return (int(evidence["n_closes"]) >= int(min_closes)
            and float(evidence["pnl"]) <= -float(min_pnl))


def load_reinstate_targets(conn: sqlite3.Connection, *, actor: str, min_closes: int,
                           min_pnl: float) -> list[dict]:
    """Excluded wallets from ONE audited sweep that the honest metric does not convict.

    `cleanup-2026-07-27` excluded 40 wallets citing "honest alpha_b closed PnL<0" but recorded the
    BLENDED fomo-included numbers (its audit `copy_closes` match the ledger exactly when the fomo
    exclusion is dropped). Fomo-priced fills are a no-impact fantasy no real fill can hit, so they
    are barred from every arm/revoke decision — a sweep computed on them convicts wallets no honest
    evidence convicts."""
    stats = memedb.alpha_b_copy_stats_all(conn)
    rows = conn.execute(
        "SELECT source_user_id,handle FROM memecoin_wallets "
        "WHERE COALESCE(excluded,0)=1 AND source_user_id IN "
        "(SELECT DISTINCT source_user_id FROM memecoin_wallet_approvals WHERE actor=?) "
        "ORDER BY source_user_id", (actor,)).fetchall()
    targets = []
    for row in rows:
        evidence = stats.get(row["source_user_id"]) or {"n_closes": 0, "pnl": 0.0}
        if is_proven_bad(evidence, min_closes=min_closes, min_pnl=min_pnl):
            continue
        targets.append({
            "source_user_id": row["source_user_id"],
            "handle": row["handle"],
            "n_closes": int(evidence["n_closes"]),
            "pnl": float(evidence["pnl"]),
        })
    return targets


def apply_reinstate(conn: sqlite3.Connection, targets: list[dict], *, actor: str,
                    min_closes: int, min_pnl: float, now: str) -> int:
    """Un-exclude and un-pause back to WATCH-ONLY. Never grants real approval — a reinstated wallet
    still has to clear the promote gate on its own honest evidence."""
    expected_ids = [str(target["source_user_id"]) for target in targets]
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = load_reinstate_targets(
            conn, actor=actor, min_closes=min_closes, min_pnl=min_pnl)
        current_ids = [str(target["source_user_id"]) for target in current]
        if current_ids != expected_ids:
            raise RuntimeError(
                f"target set changed after backup: expected={expected_ids!r} current={current_ids!r}")
        if not expected_ids:
            conn.commit()
            return 0
        placeholders = ",".join("?" for _ in expected_ids)
        changed = conn.execute(
            "UPDATE memecoin_wallets SET excluded=0,paused=0,real_approved=0,real_chains=NULL "
            f"WHERE source_user_id IN ({placeholders}) AND COALESCE(excluded,0)=1",
            expected_ids)
        if changed.rowcount != len(expected_ids):
            raise RuntimeError(
                f"atomic reinstate matched {changed.rowcount} rows, expected {len(expected_ids)}")
        for target in targets:
            conn.execute(
                "INSERT INTO memecoin_wallet_approvals(source_user_id, action, actor, reason, "
                "copy_pnl, copy_closes, forced, created_at) VALUES (?,?,?,?,?,?,0,?)",
                (target["source_user_id"], "reinstate", f"reinstate-{actor}",
                 f"exclusion by {actor} used the fomo-blended metric; honest alpha_b is "
                 f"{target['n_closes']} closes / ${target['pnl']:+.2f}, not proven-bad "
                 f"(>= {min_closes} closes AND <= -${min_pnl:g}). Restored to WATCH-ONLY.",
                 target["pnl"], target["n_closes"], now))
        conn.commit()
        return changed.rowcount
    except Exception:
        conn.rollback()
        raise


def target_digest(targets: list[dict]) -> str:
    identities = [str(target["source_user_id"]) for target in targets]
    raw = json.dumps(identities, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp.replace(path)


def backup_database(
        conn: sqlite3.Connection, db_path: Path, timestamp: str,
        label: str = "provenbad") -> tuple[Path, Path]:
    db_path = db_path.expanduser().resolve()
    stem = f"{db_path.name}.bak-{label}-{timestamp}"
    backup_path = db_path.with_name(f"{stem}.db")
    manifest_path = db_path.with_name(f"{stem}.json")
    if backup_path.exists() or manifest_path.exists():
        raise FileExistsError(f"backup artifact already exists: {stem}")

    destination = sqlite3.connect(backup_path)
    try:
        conn.backup(destination)
    finally:
        destination.close()

    check = open_existing_db(backup_path, writable=False)
    try:
        integrity = [str(row[0]) for row in check.execute("PRAGMA integrity_check")]
    finally:
        check.close()
    if integrity != ["ok"]:
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    return backup_path, manifest_path


def apply_targets(
        conn: sqlite3.Connection, targets: list[dict], *, min_closes: int, min_pnl: float) -> int:
    expected_ids = [str(target["source_user_id"]) for target in targets]
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = load_targets(conn, min_closes=min_closes, min_pnl=min_pnl)
        current_ids = [str(target["source_user_id"]) for target in current]
        if current_ids != expected_ids:
            raise RuntimeError(
                f"target set changed after backup: expected={expected_ids!r} current={current_ids!r}")
        if not expected_ids:
            conn.commit()
            return 0
        placeholders = ",".join("?" for _ in expected_ids)
        changed = conn.execute(
            "UPDATE memecoin_wallets SET excluded=1,paused=1,real_approved=0,real_chains=NULL "
            f"WHERE source_user_id IN ({placeholders}) AND COALESCE(real_approved,0)=0 "
            "AND COALESCE(excluded,0)=0 AND paused=0",
            expected_ids)
        if changed.rowcount != len(expected_ids):
            raise RuntimeError(
                f"atomic exclusion matched {changed.rowcount} rows, expected {len(expected_ids)}")
        conn.commit()
        return changed.rowcount
    except Exception:
        conn.rollback()
        raise


def _timestamp() -> str:
    return datetime.fromtimestamp(real_utc_now(), tz=timezone.utc).strftime("%Y%m%d-%H%M%S")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=state_path("sniper_tracker.db"),
        help="Existing SQLite ledger (default: SNIPER_STATE_ROOT/sniper_tracker.db)")
    parser.add_argument("--apply", action="store_true", help="Apply the previewed exclusions")
    parser.add_argument(
        "--expect-count", type=int,
        help="Required with --apply; must exactly match the current preview count")
    parser.add_argument(
        "--expect-digest",
        help="Required with --apply; SHA-256 target digest printed by the audited dry-run")
    parser.add_argument(
        "--reinstate-actor",
        help="Repair mode: un-exclude wallets swept by this audit actor that the honest metric "
             "does not convict (e.g. cleanup-2026-07-27). Restores WATCH-ONLY, never real approval.")
    return parser


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.apply and args.expect_count is None:
        parser.error("--apply requires --expect-count N from an audited dry-run")
    if args.apply and not args.expect_digest:
        parser.error("--apply requires --expect-digest SHA256 from an audited dry-run")
    if args.expect_count is not None and args.expect_count < 0:
        parser.error("--expect-count must be non-negative")

    min_closes = int(cfg.MEMECOIN_REAL_PROMOTE_MIN_CLOSES)
    min_pnl = float(cfg.MEMECOIN_REAL_PROMOTE_MIN_PNL_USD)
    if min_closes <= 0 or min_pnl <= 0:
        parser.error("promotion floors must be positive")

    conn = None
    try:
        conn = open_existing_db(args.db, writable=bool(args.apply))
        reinstate_actor = args.reinstate_actor
        if reinstate_actor:
            targets = load_reinstate_targets(
                conn, actor=reinstate_actor, min_closes=min_closes, min_pnl=min_pnl)
        else:
            targets = load_targets(conn, min_closes=min_closes, min_pnl=min_pnl)
        digest = target_digest(targets)
        print(f"database: {args.db.expanduser().resolve()}")
        if reinstate_actor:
            print(f"mode: REINSTATE wallets excluded by actor={reinstate_actor!r} that are NOT "
                  f"proven-bad on the honest metric (closes>={min_closes} AND pnl<=-{min_pnl:g}) "
                  "-> excluded=0, paused=0, real_approved untouched")
        else:
            print(f"criterion: honest closes>={min_closes} AND honest_pnl<=-{min_pnl:g} "
                  "(active watch-only wallets)")
        for target in targets:
            label = target["handle"] or target["source_user_id"]
            print(f"  {label:<20} uid={target['source_user_id']} "
                  f"closes={target['n_closes']:<4} honest_pnl={target['pnl']:.2f}")
        print(f"TOTAL: {len(targets)}")
        print(f"TARGET-DIGEST: {digest}")

        if not args.apply:
            print("\nDRY-RUN: no database writes; rerun with --apply --expect-count N "
                  "--expect-digest SHA256")
            return 0
        if int(args.expect_count) != len(targets):
            parser.error(f"expected {args.expect_count} targets, found {len(targets)}")
        if str(args.expect_digest).lower() != digest:
            parser.error(f"target digest mismatch: expected {args.expect_digest}, current {digest}")

        now = datetime.fromtimestamp(real_utc_now(), tz=timezone.utc).isoformat()
        backup_path, manifest_path = backup_database(
            conn, args.db, _timestamp(), "reinstate" if reinstate_actor else "provenbad")
        manifest = {
            "status": "planned",
            "created_at": now,
            "database": str(args.db.expanduser().resolve()),
            "backup": str(backup_path),
            "mode": "reinstate" if reinstate_actor else "exclude",
            "reinstate_actor": reinstate_actor,
            "criterion": {"min_closes": min_closes, "max_pnl": -min_pnl},
            "target_digest": digest,
            "targets": targets,
        }
        _write_json_atomic(manifest_path, manifest)
        if reinstate_actor:
            changed = apply_reinstate(
                conn, targets, actor=reinstate_actor, min_closes=min_closes, min_pnl=min_pnl,
                now=now)
        else:
            changed = apply_targets(
                conn, targets, min_closes=min_closes, min_pnl=min_pnl)
        manifest.update({
            "status": "applied",
            "applied_at": datetime.fromtimestamp(real_utc_now(), tz=timezone.utc).isoformat(),
            "changed": changed,
        })
        _write_json_atomic(manifest_path, manifest)
        print(f"\nbackup: {backup_path} (integrity_check=ok)")
        print(f"manifest: {manifest_path}")
        print(f"APPLIED: {'reinstated' if reinstate_actor else 'excluded'}={changed}")
        return 0
    except Exception as exc:
        parser.exit(1, f"error: {exc}\n")
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
