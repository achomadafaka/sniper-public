#!/usr/bin/env python3
"""Plan one stable-UID display rename. --apply requires a local receipt; no network or authority edits."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--evidence", required=True,
                        help="sanitized JSON containing alias_change and stored observation_ids")
    parser.add_argument("--state-root", help="configuration root; defaults to the database directory")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", help="local before/after receipt required for --apply")
    args = parser.parse_args(argv)
    if args.apply and not args.receipt:
        parser.error("--apply requires --receipt")

    from sniper.util.envfiles import load_env_files
    load_env_files(args.state_root or str(Path(args.db).resolve().parent), names=(".env",))
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.persistence.db_memecoin import rename_wallet_handle
    from sniper.util.atomic import atomic_write_json
    from sniper.util.clock import real_utc_now_iso

    report = {"checked_at": real_utc_now_iso(), "read_only": not args.apply,
              "db": str(Path(args.db).resolve())}
    conn = None
    applied = False
    phase = None
    try:
        if args.receipt and Path(args.receipt).resolve() in {
                Path(args.db).resolve(), Path(args.evidence).resolve()}:
            raise ValueError("receipt_path_conflict")
        raw = Path(args.evidence).read_bytes()
        if len(raw) > 256 * 1024:
            raise ValueError("evidence_too_large")
        alias = json.loads(raw).get("alias_change")
        if not isinstance(alias, dict):
            raise ValueError("alias_evidence_mismatch")
        uid, old, new = (alias.get(k) for k in ("source_user_id", "expected_handle", "new_handle"))
        ids = alias.get("observation_ids")
        if not all(isinstance(v, str) and v.strip() for v in (uid, old, new)):
            raise ValueError("alias_evidence_mismatch")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 10
                or any(type(i) is not int or i < 1 for i in ids)):
            raise ValueError("alias_evidence_mismatch")
        conn = get_readonly_connection(args.db)
        conn.execute("PRAGMA busy_timeout=200")
        observations = []
        for rid in ids:
            row = conn.execute(
                "SELECT id,source_user_id,source_handle,normalized_side,source_ts,raw_json "
                "FROM memecoin_feed_observations_v2 WHERE id=?", (rid,)).fetchone()
            if (row is None or row["source_user_id"] != uid or row["source_handle"] != new
                    or row["normalized_side"] not in ("buy", "sell")
                    or json.loads(row["raw_json"]).get("signal_source") != "follow_activity"):
                raise ValueError("alias_evidence_mismatch")
            observations.append({k: row[k] for k in ("id", "source_user_id", "source_handle", "source_ts")})
        row = conn.execute("SELECT * FROM memecoin_wallets WHERE source_user_id=?", (uid,)).fetchone()
        report["before"] = dict(row) if row else None
        report["provenance"] = {"source_user_id": uid, "expected_handle": old, "new_handle": new,
                                "observation_ids": ids, "observations": observations,
                                "evidence_sha256": hashlib.sha256(raw).hexdigest()}
        report["change"] = rename_wallet_handle(conn, uid, expected_handle=old,
                                                new_handle=new, dry_run=True)
        conn.close()
        conn = None
        if report["change"]["status"] == "refused":
            print(json.dumps(report, sort_keys=True))
            return 1
        if args.apply:
            # Journal the exact target before committing. After a crash the root task can compare
            # this pending receipt with the stable UID; replaying the CAS is unchanged or refuses.
            pending = dict(report, application_state="pending")
            atomic_write_json(args.receipt, pending, mode=0o600)
            conn = sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=rw", uri=True,
                                   timeout=0.2, isolation_level=None)
            conn.row_factory = sqlite3.Row
            started = time.perf_counter()
            report["change"] = rename_wallet_handle(conn, uid, expected_handle=old, new_handle=new)
            applied = report["change"]["status"] in ("renamed", "unchanged")
            report["transaction_ms"] = (time.perf_counter() - started) * 1000.0
            phase = "verification"
            row = conn.execute("SELECT * FROM memecoin_wallets WHERE source_user_id=?", (uid,)).fetchone()
            report["after"] = dict(row) if row else None
            report["application_state"] = "finished"
            phase = "receipt_write"
            atomic_write_json(args.receipt, report, mode=0o600)
        print(json.dumps(report, sort_keys=True))
        return int(report["change"]["status"] == "refused")
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
        if applied:
            report["application_state"] = f"{phase}_failed_after_commit"
            report["error"] = str(exc)
        else:
            report["change"] = {"status": "refused", "reason": str(exc)}
        print(json.dumps(report, sort_keys=True))
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
