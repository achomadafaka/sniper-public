"""Deterministic training-dataset exporter (Phase 1B #9).

Grain: one row per ``(condition_id × decision checkpoint)``. Each row carries market identity, window +
decision timestamps, the feature snapshot (flattened), feature/price-to-beat/book/fee quality flags, the
final settlement label (``up_won`` 1/0 from the Polymarket winner — NEVER Binance), label availability,
source lineage, and a ``model_eligible`` flag.

Model-eligible ONLY when: settlement reconciled & not conflict, feature_status == "ok" (known-at-time
passed), not receive_time_missing (no unknown-arrival leakage), book interval not quarantined, and the
market rule is supported. No imputation — missing features stay NULL and the quality columns preserve the
reason. Output: Parquet (via duckdb, deterministic order) + manifest JSON + a content hash over the
canonical-ordered rows (independent of duckdb's parquet byte layout).
"""
from __future__ import annotations

import hashlib
import json

from . import feature_registry as FR
from . import features as FE

_STRING_FEATURES = {"legacy_spot_signal_diagnostic", "basis_quality"}

META_COLUMNS = [
    ("condition_id", "VARCHAR"), ("market_id", "VARCHAR"),
    ("window_start_ts", "BIGINT"), ("window_end_ts", "BIGINT"),
    ("checkpoint", "INTEGER"), ("decision_ts", "BIGINT"), ("feature_cutoff_ts", "BIGINT"),
    ("feature_version", "VARCHAR"), ("feature_status", "VARCHAR"),
    ("price_to_beat_quality", "VARCHAR"), ("book_integrity_status", "VARCHAR"),
    ("fee_status", "VARCHAR"), ("receive_time_missing", "BOOLEAN"),
    ("book_quarantined", "BOOLEAN"), ("rule_supported", "BOOLEAN"),
    ("up_won", "INTEGER"), ("label_available", "BOOLEAN"), ("model_eligible", "BOOLEAN"),
    ("source_lineage", "VARCHAR"),
]
SOURCE_TABLES = ["crypto5m_feature_snapshots", "crypto5m_settlements", "crypto5m_market_rules",
                 "crypto5m_book_reconciliation", "crypto5m_fee_configs"]
ELIGIBILITY_RULE = ("settlement reconciled & not conflict & feature_status==ok & not "
                    "receive_time_missing & price_to_beat_known_ts<=decision_ts & "
                    "book_integrity_status==usable & market rule supported "
                    "(condition-level reconciliation quarantine is diagnostic-only, not a gate)")


def _feature_columns():
    return [(n, ("VARCHAR" if n in _STRING_FEATURES else "DOUBLE")) for n in FR.canonical_feature_names()]


def _columns():
    return META_COLUMNS + _feature_columns()


def build_rows(conn, *, feature_version=None) -> list:
    """One row per (condition_id, checkpoint) for a SINGLE feature_version (defaults to the current
    FE.FEATURE_VERSION) — the snapshot table can hold multiple versions per grain, so without this filter
    a version bump would duplicate the grain."""
    feature_version = feature_version or FE.FEATURE_VERSION
    names = FR.canonical_feature_names()
    settlements = {r["condition_id"]: r for r in conn.execute(
        "SELECT condition_id, winning_outcome, reconciliation_status FROM crypto5m_settlements")}
    # key by the EXACT (condition_id, rule_text_hash) — an older supported rule must not mask the
    # snapshot's current (possibly unsupported) market_rule_hash.
    rules = {(r["condition_id"], r["rule_text_hash"]): bool(r["rule_supported"]) for r in conn.execute(
        "SELECT condition_id, rule_text_hash, rule_supported FROM crypto5m_market_rules")}
    quarantined = {r[0] for r in conn.execute(
        "SELECT DISTINCT condition_id FROM crypto5m_book_reconciliation WHERE quarantined_interval=1")}
    fees = {}
    for r in conn.execute("SELECT condition_id, fee_status FROM crypto5m_fee_configs ORDER BY id"):
        fees[r["condition_id"]] = r["fee_status"]          # last (newest) wins

    rows = []
    for s in conn.execute("SELECT * FROM crypto5m_feature_snapshots WHERE feature_version = ? "
                          "ORDER BY condition_id, checkpoint", (feature_version,)):
        cid = s["condition_id"]
        st = settlements.get(cid)
        winner = st["winning_outcome"] if st else None
        recon = st["reconciliation_status"] if st else None
        label_available = winner in ("Up", "Down") and recon != "conflict"
        up_won = (1 if winner == "Up" else 0) if label_available else None
        rule_ok = rules.get((cid, s["market_rule_hash"]), False)
        quar = cid in quarantined            # diagnostic only (see book_ok below) — NOT an eligibility gate
        recv_missing = bool(s["receive_time_missing"])
        feature_status = s["feature_status"]
        # The real decision-time book guard is the PER-SNAPSHOT integrity (usable/stale/quarantined), set
        # from capture-time book quality_flags. The condition-level reconciliation quarantine (`quar`)
        # fires on benign WS-vs-REST-refetch movement, so it is recorded as a diagnostic but does NOT gate.
        book_ok = (s["book_integrity_status"] == "usable")
        p2b_known = s["price_to_beat_known_ts"]
        p2b_ok = (p2b_known is None or p2b_known <= s["decision_ts"])   # boundary value not known late
        eligible = bool(label_available and feature_status == "ok" and not recv_missing
                        and book_ok and rule_ok and p2b_ok)
        feats = json.loads(s["features_json"] or "{}")
        row = {
            "condition_id": cid, "market_id": s["market_id"],
            "window_start_ts": s["window_start_ts"], "window_end_ts": s["window_end_ts"],
            "checkpoint": s["checkpoint"], "decision_ts": s["decision_ts"],
            "feature_cutoff_ts": s["feature_cutoff_ts"], "feature_version": s["feature_version"],
            "feature_status": feature_status, "price_to_beat_quality": s["price_to_beat_quality"],
            "book_integrity_status": s["book_integrity_status"], "fee_status": fees.get(cid),
            "receive_time_missing": recv_missing, "book_quarantined": quar, "rule_supported": rule_ok,
            "up_won": up_won, "label_available": bool(label_available), "model_eligible": eligible,
            "source_lineage": "+".join(SOURCE_TABLES),
        }
        for n in names:
            row[n] = feats.get(n)
        rows.append(row)
    return rows


def _ordered(rows):
    return sorted(rows, key=lambda r: (r["condition_id"], r["checkpoint"]))


def _coerce(v, ddl):
    """Coerce a cell to its STORED parquet representation so the hash describes the file (a DOUBLE column
    stores 240 as 240.0 — the hash must agree)."""
    if v is None:
        return None
    if ddl == "DOUBLE":
        return float(v)
    if ddl in ("BIGINT", "INTEGER"):
        return int(v)
    if ddl == "BOOLEAN":
        return bool(v)
    return str(v)


def _canonical_cells(rows, cols):
    return [[_coerce(r.get(n), t) for n, t in cols] for r in _ordered(rows)]


def dataset_hash(rows, cols=None) -> str:
    cols = cols or _columns()
    return hashlib.sha256(json.dumps(_canonical_cells(rows, cols), default=str).encode()).hexdigest()


def _label_distribution(rows) -> dict:
    out = {"up": 0, "down": 0, "none": 0}
    for r in rows:
        out["up" if r["up_won"] == 1 else "down" if r["up_won"] == 0 else "none"] += 1
    return out


def export_dataset(conn, *, out_path: str, enforce_leakage: bool = True) -> dict:
    import duckdb
    if enforce_leakage:
        from . import readiness_audit as RA   # lazy: RA imports this module
        result = RA.leakage_checks(conn)
        if not result["passed"]:
            raise RA.LeakageError(result["violations"])   # any leakage fails the build
    rows = build_rows(conn)
    cols = _columns()
    cells = _canonical_cells(rows, cols)              # coerced to stored types -> hash matches parquet
    dhash = hashlib.sha256(json.dumps(cells, default=str).encode()).hexdigest()

    con = duckdb.connect()
    try:
        con.execute(f"CREATE TABLE ds ({', '.join(f'{n} {t}' for n, t in cols)})")
        if cells:
            ph = ",".join(["?"] * len(cols))
            con.executemany(f"INSERT INTO ds VALUES ({ph})", cells)
        con.execute(f"COPY (SELECT * FROM ds ORDER BY condition_id, checkpoint) "
                    f"TO '{out_path}' (FORMAT PARQUET)")
    finally:
        con.close()

    eligible = sum(1 for r in rows if r["model_eligible"])
    manifest = {
        "dataset_version": 1,
        "feature_version": FE.FEATURE_VERSION,
        "row_count": len(rows),
        "eligible_count": eligible,
        "label_distribution": _label_distribution(rows),
        "columns": [n for n, _ in cols],
        "source_tables": SOURCE_TABLES,
        "eligibility_rule": ELIGIBILITY_RULE,
        "no_imputation": True,
        "dataset_hash": dhash,
    }
    manifest_path = out_path + ".manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return {"parquet_path": out_path, "manifest_path": manifest_path, "dataset_hash": dhash,
            "row_count": len(rows), "eligible_count": eligible}
