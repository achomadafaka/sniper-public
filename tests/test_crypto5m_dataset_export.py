"""Phase 1B Deliverable #9 — deterministic training-dataset exporter.

One row per (condition_id × checkpoint); label up_won from the Polymarket winner (never Binance);
model-eligibility gated on settlement reconciled + not conflict + feature_status==ok + not
receive_time_missing + book not quarantined + market rule supported; Parquet + manifest + deterministic
hash; NO imputation (nulls preserved).
"""
import json
import os

import duckdb

from sniper.algos.polymarket_crypto5m import dataset_export as DX
from sniper.algos.polymarket_crypto5m import features as FE
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import readiness_audit as RA
from sniper.algos.polymarket_crypto5m import sim_persistence as SP


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "d.sqlite"))
    P.ensure_schema(conn)
    SP.ensure_sim_schema(conn)
    return conn


def _seed(conn, cid, cp, *, winner="Up", recon="gamma_only", feature_status="ok", recv_missing=False,
          quarantined=False, rule_supported=True, book_integrity="usable", features=None):
    SP.upsert_feature_snapshot(conn, dict(
        condition_id=cid, market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=cp,
        decision_ts=1000 + cp, feature_cutoff_ts=1000 + cp, feature_version=FE.FEATURE_VERSION,
        feature_status=feature_status, price_to_beat_quality="unavailable",
        book_integrity_status=book_integrity, receive_time_missing=recv_missing, market_rule_hash="h",
        features=features or {"binance_return_15s": 0.1, "legacy_spot_signal_diagnostic": "flat",
                              "basis_quality": "ok"}), now_ts=1)
    conn.execute("INSERT OR IGNORE INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES (?,?,?)", (cid, winner, recon))
    conn.execute("INSERT OR IGNORE INTO crypto5m_market_rules (condition_id, rule_text_hash, "
                 "rule_supported) VALUES (?,?,?)", (cid, "h", int(rule_supported)))
    if quarantined:
        conn.execute("INSERT INTO crypto5m_book_reconciliation (condition_id, token_id, "
                     "quarantined_interval) VALUES (?,?,?)", (cid, "t", 1))
    conn.commit()


def test_one_row_per_condition_checkpoint(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 15)
    _seed(conn, "a", 30)
    _seed(conn, "b", 15)
    out = os.path.join(str(tmp_path), "ds.parquet")
    res = DX.export_dataset(conn, out_path=out)
    assert res["row_count"] == 3
    con = duckdb.connect()
    assert con.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0] == 3
    distinct = con.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT condition_id, checkpoint "
                           f"FROM read_parquet('{out}'))").fetchone()[0]
    assert distinct == 3


def test_label_from_polymarket_winner_not_binance(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "up", 15, winner="Up")
    _seed(conn, "dn", 15, winner="Down")
    _seed(conn, "cf", 15, winner=None, recon="conflict")
    rows = {r["condition_id"]: r for r in DX.build_rows(conn)}
    assert rows["up"]["up_won"] == 1 and rows["up"]["label_available"] is True
    assert rows["dn"]["up_won"] == 0
    assert rows["cf"]["up_won"] is None and rows["cf"]["label_available"] is False


def test_eligibility_gates(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "ok", 15)
    _seed(conn, "badfs", 15, feature_status="boundary_unavailable_at_decision")
    _seed(conn, "leak", 15, recv_missing=True)
    _seed(conn, "conf", 15, winner=None, recon="conflict")
    _seed(conn, "stalebook", 15, book_integrity="stale")     # per-snapshot book gate excludes
    _seed(conn, "norule", 15, rule_supported=False)
    rows = {r["condition_id"]: r for r in DX.build_rows(conn)}
    assert rows["ok"]["model_eligible"] is True
    for bad in ("badfs", "leak", "conf", "stalebook", "norule"):
        assert rows[bad]["model_eligible"] is False


def test_reconciliation_quarantine_is_diagnostic_not_an_eligibility_gate(tmp_path):
    # ROOT-CAUSE LOCK (2026-06-21): a quarantined book-reconciliation interval fires on benign WS-vs-REST
    # book movement (divergence_levels>0), so it hit 100% of conditions and zeroed model_eligible. The real
    # guard is the per-snapshot book_integrity_status; the condition-level quarantine is diagnostic only.
    conn = _conn(tmp_path)
    _seed(conn, "quar", 15, quarantined=True)                # usable decision book + a quarantined interval
    rows = {r["condition_id"]: r for r in DX.build_rows(conn)}
    assert rows["quar"]["book_quarantined"] is True          # still recorded as a diagnostic
    assert rows["quar"]["model_eligible"] is True            # ...but no longer excluded


def test_stale_book_excluded_by_per_snapshot_gate_without_reconciliation_row(tmp_path):
    # the per-snapshot book_integrity_status alone must exclude a stale/quarantined decision book,
    # with NO reconciliation row present — proving it is the real integrity guard.
    conn = _conn(tmp_path)
    _seed(conn, "stale", 15, book_integrity="stale")
    rows = {r["condition_id"]: r for r in DX.build_rows(conn)}
    assert rows["stale"]["model_eligible"] is False


def test_unsupported_current_rule_not_masked_by_old_supported(tmp_path):
    # the snapshot's CURRENT market_rule_hash is unsupported; an older supported hash must not mask it.
    conn = _conn(tmp_path)
    SP.upsert_feature_snapshot(conn, dict(
        condition_id="a", market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=15,
        decision_ts=1015, feature_cutoff_ts=1015, feature_version=FE.FEATURE_VERSION, feature_status="ok",
        price_to_beat_quality="unavailable", book_integrity_status="usable", receive_time_missing=False,
        market_rule_hash="new", features={}), now_ts=1)
    conn.execute("INSERT INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES ('a','Up','gamma_only')")
    conn.execute("INSERT INTO crypto5m_market_rules (condition_id, rule_text_hash, rule_supported) "
                 "VALUES ('a','old',1)")                    # supported but a DIFFERENT (old) hash
    conn.execute("INSERT INTO crypto5m_market_rules (condition_id, rule_text_hash, rule_supported) "
                 "VALUES ('a','new',0)")                    # the CURRENT hash is unsupported
    conn.commit()
    rows = {r["condition_id"]: r for r in DX.build_rows(conn)}
    assert rows["a"]["model_eligible"] is False


def test_rerun_updates_snapshot_rule_hash_before_export(tmp_path):
    conn = _conn(tmp_path)
    snap = dict(
        condition_id="rerun", market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=15,
        decision_ts=1015, feature_cutoff_ts=1015, feature_version=FE.FEATURE_VERSION, feature_status="ok",
        price_to_beat_quality="unavailable", book_integrity_status="usable", receive_time_missing=False,
        features={})
    SP.upsert_feature_snapshot(conn, {**snap, "market_rule_hash": "old"}, now_ts=1)
    SP.upsert_feature_snapshot(conn, {**snap, "market_rule_hash": "new"}, now_ts=2)
    conn.execute("INSERT INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES ('rerun','Up','gamma_only')")
    conn.execute("INSERT INTO crypto5m_market_rules (condition_id, rule_text_hash, rule_supported) "
                 "VALUES ('rerun','old',1)")
    conn.execute("INSERT INTO crypto5m_market_rules (condition_id, rule_text_hash, rule_supported) "
                 "VALUES ('rerun','new',0)")
    conn.commit()

    row = DX.build_rows(conn)[0]
    assert row["rule_supported"] is False
    assert row["model_eligible"] is False


def test_price_to_beat_known_after_decision_not_eligible(tmp_path):
    conn = _conn(tmp_path)
    SP.upsert_feature_snapshot(conn, dict(
        condition_id="b", market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=60,
        decision_ts=1060, feature_cutoff_ts=1060, feature_version=FE.FEATURE_VERSION, feature_status="ok",
        price_to_beat_quality="validated_rtds_reconstruction", price_to_beat_known_ts=1200,
        book_integrity_status="usable", receive_time_missing=False, market_rule_hash="h",
        features={}), now_ts=1)
    conn.execute("INSERT INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES ('b','Up','gamma_only')")
    conn.execute("INSERT INTO crypto5m_market_rules (condition_id, rule_text_hash, rule_supported) "
                 "VALUES ('b','h',1)")
    conn.commit()
    rows = {r["condition_id"]: r for r in DX.build_rows(conn)}
    assert rows["b"]["model_eligible"] is False             # Price-to-Beat known AFTER the decision -> leak


def test_rerun_updates_price_to_beat_known_ts_before_export_and_audit(tmp_path):
    conn = _conn(tmp_path)
    snap = dict(
        condition_id="p2b", market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=60,
        decision_ts=1060, feature_cutoff_ts=1060, feature_version=FE.FEATURE_VERSION, feature_status="ok",
        price_to_beat_quality="validated_rtds_reconstruction", book_integrity_status="usable",
        receive_time_missing=False, market_rule_hash="h", features={})
    SP.upsert_feature_snapshot(conn, {**snap, "price_to_beat_known_ts": 1050}, now_ts=1)
    SP.upsert_feature_snapshot(conn, {**snap, "price_to_beat_known_ts": 1200}, now_ts=2)
    conn.execute("INSERT INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES ('p2b','Up','gamma_only')")
    conn.execute("INSERT INTO crypto5m_market_rules (condition_id, rule_text_hash, rule_supported) "
                 "VALUES ('p2b','h',1)")
    conn.commit()

    row = DX.build_rows(conn)[0]
    assert row["model_eligible"] is False
    assert RA.leakage_checks(conn)["violations"] == [
        {"check": "price_to_beat_known_after_decision", "count": 1}
    ]


def test_deterministic_hash_independent_of_insertion_order(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 30)
    _seed(conn, "a", 15)
    _seed(conn, "b", 15)
    h1 = DX.export_dataset(conn, out_path=os.path.join(str(tmp_path), "d1.parquet"))["dataset_hash"]
    h2 = DX.export_dataset(conn, out_path=os.path.join(str(tmp_path), "d2.parquet"))["dataset_hash"]
    assert h1 == h2 and len(h1) == 64


def test_no_imputation_nulls_preserved(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 15, features={"binance_return_15s": None, "binance_volume_30s": None,
                                   "legacy_spot_signal_diagnostic": "flat", "basis_quality": "unavailable"})
    out = os.path.join(str(tmp_path), "ds.parquet")
    DX.export_dataset(conn, out_path=out)
    con = duckdb.connect()
    val = con.execute(f"SELECT binance_return_15s FROM read_parquet('{out}')").fetchone()[0]
    assert val is None                              # missing feature stays NULL, never imputed


def test_hash_invariant_to_int_float_representation(tmp_path):
    # seconds_remaining lands in a DOUBLE column; 240 (int) and 240.0 (float) must hash identically so
    # the manifest hash matches the parquet's stored (float) values.
    ca = _conn(tmp_path)
    _seed(ca, "a", 15, features={"seconds_remaining": 240, "legacy_spot_signal_diagnostic": "flat",
                                 "basis_quality": "ok"})
    cb = _conn(tmp_path)  # different sqlite file under same tmp dir
    cb = P.connect(os.path.join(str(tmp_path), "d2.sqlite"))
    P.ensure_schema(cb)
    SP.ensure_sim_schema(cb)
    _seed(cb, "a", 15, features={"seconds_remaining": 240.0, "legacy_spot_signal_diagnostic": "flat",
                                 "basis_quality": "ok"})
    assert DX.dataset_hash(DX.build_rows(ca)) == DX.dataset_hash(DX.build_rows(cb))


def test_seconds_remaining_stored_as_double(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 15, features={"seconds_remaining": 240})
    out = os.path.join(str(tmp_path), "ds.parquet")
    DX.export_dataset(conn, out_path=out)
    val = duckdb.connect().execute(f"SELECT seconds_remaining FROM read_parquet('{out}')").fetchone()[0]
    assert isinstance(val, float) and val == 240.0


def test_grain_filters_to_current_feature_version(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 15)                          # fv0 == current FEATURE_VERSION
    SP.upsert_feature_snapshot(conn, dict(       # stale other-version snapshot for the same grain
        condition_id="a", market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=15,
        decision_ts=1015, feature_cutoff_ts=1015, feature_version="fvX", feature_status="ok",
        price_to_beat_quality="unavailable", book_integrity_status="usable",
        receive_time_missing=False, features={}), now_ts=2)
    rows = DX.build_rows(conn)
    assert len(rows) == 1 and rows[0]["feature_version"] == FE.FEATURE_VERSION   # one row per grain, current version


def test_manifest_written(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 15, winner="Up")
    _seed(conn, "b", 15, winner="Down")
    out = os.path.join(str(tmp_path), "ds.parquet")
    res = DX.export_dataset(conn, out_path=out)
    man = json.load(open(res["manifest_path"]))
    assert man["row_count"] == 2 and man["no_imputation"] is True
    assert man["dataset_hash"] == res["dataset_hash"]
    assert "binance_return_15s" in man["columns"]
    assert man["label_distribution"]["up"] == 1 and man["label_distribution"]["down"] == 1
    assert "price_to_beat_known_ts<=decision_ts" in man["eligibility_rule"]
