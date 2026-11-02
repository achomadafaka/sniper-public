import json

import pytest

from sniper.hyperlive import intel_store as IS


def _conn(tmp_path):
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    return conn


def _manifest(**overrides):
    value = {
        "arm_id": "altshort-forward-v1",
        "policy": {"alt_short_only": True, "cost_bps": 39},
        "targets": ["0xabc"],
        "taxonomy_version": "altshort-core-v1",
        "benchmark_version": "loo-equal-weight-v1",
        "regime_version": "causal-hourly-v1",
    }
    value.update(overrides)
    return value


def _copy(conn, *, target="0xabc", coin="AAA", side="short", detect_ts_ms=2000):
    return IS.open_copy(conn, target, coin, side, 100.0, ts_ms=detect_ts_ms)


def test_arm_schema_and_identity_are_immutable(tmp_path):
    conn = _conn(tmp_path)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"hl_altshort_arms", "hl_altshort_observations", "hl_altshort_regime_marks"} <= tables
    created = IS.create_altshort_arm(
        conn, _manifest(), activated_ts_ms=1000, code_sha="abc123", created_ts_ms=900
    )
    replay = IS.create_altshort_arm(
        conn, _manifest(), activated_ts_ms=1000, code_sha="abc123", created_ts_ms=999
    )
    assert replay == created
    assert replay["policy_hash"]
    with pytest.raises(ValueError, match="immutable arm conflict"):
        IS.create_altshort_arm(
            conn, _manifest(targets=["0xother"]), activated_ts_ms=1000,
            code_sha="abc123", created_ts_ms=1000,
        )


def test_compatible_arm_ids_ignore_code_sha_and_arm_label_but_not_semantics(tmp_path):
    conn = _conn(tmp_path)
    IS.create_altshort_arm(
        conn, _manifest(arm_id="forward-v1"), activated_ts_ms=1000,
        code_sha="aaa", created_ts_ms=900,
    )
    IS.create_altshort_arm(
        conn, _manifest(arm_id="forward-v2"), activated_ts_ms=2000,
        code_sha="bbb", created_ts_ms=1900,
    )
    IS.create_altshort_arm(
        conn, _manifest(arm_id="forward-taxonomy-v2", taxonomy_version="altshort-core-v2"),
        activated_ts_ms=3000, code_sha="ccc", created_ts_ms=2900,
    )

    assert IS.compatible_altshort_arm_ids(conn, arm_id="forward-v2") == [
        "forward-v1", "forward-v2"
    ]
    assert IS.compatible_altshort_arm_ids(
        conn, arm_id="forward-taxonomy-v2"
    ) == ["forward-taxonomy-v2"]


def test_observation_requires_epoch_target_and_alt_short(tmp_path):
    conn = _conn(tmp_path)
    IS.create_altshort_arm(conn, _manifest(), activated_ts_ms=1000,
                           code_sha="abc123", created_ts_ms=900)
    old = _copy(conn, detect_ts_ms=999)
    wrong_target = _copy(conn, target="0xother")
    wrong_side = _copy(conn, side="long")
    with pytest.raises(ValueError, match="pre_arm_epoch"):
        IS.record_altshort_observation(conn, arm_id="altshort-forward-v1", copy_id=old,
                                       regime={"label": "unknown"}, benchmark={})
    with pytest.raises(ValueError, match="target_not_in_arm"):
        IS.record_altshort_observation(conn, arm_id="altshort-forward-v1", copy_id=wrong_target,
                                       regime={"label": "unknown"}, benchmark={})
    with pytest.raises(ValueError, match="not_alt_short"):
        IS.record_altshort_observation(conn, arm_id="altshort-forward-v1", copy_id=wrong_side,
                                       regime={"label": "unknown"}, benchmark={})


def test_observation_is_idempotent_marks_and_closes(tmp_path):
    conn = _conn(tmp_path)
    IS.create_altshort_arm(conn, _manifest(), activated_ts_ms=1000,
                           code_sha="abc123", created_ts_ms=900)
    copy_id = _copy(conn)
    regime = {"label": "unknown", "coverage": 1.0}
    benchmark = {"BBB": 100.0, "CCC": 200.0}
    first = IS.record_altshort_observation(
        conn, arm_id="altshort-forward-v1", copy_id=copy_id,
        regime=regime, benchmark=benchmark,
    )
    replay = IS.record_altshort_observation(
        conn, arm_id="altshort-forward-v1", copy_id=copy_id,
        regime=regime, benchmark=benchmark,
    )
    assert replay == first
    with pytest.raises(ValueError, match="observation conflict"):
        IS.record_altshort_observation(
            conn, arm_id="altshort-forward-v1", copy_id=copy_id,
            regime={"label": "bearish"}, benchmark=benchmark,
        )
    IS.update_altshort_observation_mark(conn, copy_id=copy_id, mark=90.0, ts_ms=2500)
    IS.close_copy(conn, copy_id, exit_mark=90.0, pnl_bps=961.0, ts_ms=3000)
    IS.close_altshort_observation(
        conn, copy_id=copy_id, exit_mark=90.0, exit_ts_ms=3000,
        benchmark={"basket_net_bps": 100.0, "coverage": 1.0},
    )
    row = IS.list_altshort_observations(conn, arm_id="altshort-forward-v1")[0]
    assert row["latest_mark"] == 90.0 and row["latest_mark_ts_ms"] == 3000
    assert row["status"] == "closed" and row["wallet_net_bps"] == 961.0
    assert json.loads(row["benchmark_exit_json"])["basket_net_bps"] == 100.0


def test_arm_close_charges_funding_and_cost_above_copy_fee(tmp_path):
    conn = _conn(tmp_path)
    IS.create_altshort_arm(conn, _manifest(), activated_ts_ms=1000,
                           code_sha="abc123", created_ts_ms=900)
    copy_id = _copy(conn)
    IS.record_altshort_observation(
        conn, arm_id="altshort-forward-v1", copy_id=copy_id,
        regime={"label": "unknown"}, benchmark={"BBB": 100.0},
    )
    IS.close_copy(conn, copy_id, exit_mark=90.0, pnl_bps=991.0, ts_ms=3000,
                  funding_bps=-4.0)

    IS.close_altshort_observation(
        conn, copy_id=copy_id, exit_mark=90.0, exit_ts_ms=3000,
        benchmark={"basket_net_bps": 100.0}, extra_cost_bps=30.0,
    )

    row = IS.get_altshort_observation(conn, copy_id=copy_id)
    assert row["wallet_net_bps"] == 965.0  # 991 - 30 - (-4 funding received)


def test_arm_close_marks_missing_signal_funding_invalid(tmp_path):
    conn = _conn(tmp_path)
    IS.create_altshort_arm(conn, _manifest(), activated_ts_ms=1000,
                           code_sha="abc123", created_ts_ms=900)
    copy_id = _copy(conn)
    IS.record_altshort_observation(
        conn, arm_id="altshort-forward-v1", copy_id=copy_id,
        regime={"label": "unknown"}, benchmark={"BBB": 100.0},
    )
    IS.close_copy(conn, copy_id, exit_mark=90.0, pnl_bps=991.0, ts_ms=3_700_000)

    IS.close_altshort_observation(
        conn, copy_id=copy_id, exit_mark=90.0, exit_ts_ms=3_700_000,
        benchmark={"valid": False}, funding_complete=False,
    )

    assert IS.get_altshort_observation(conn, copy_id=copy_id)["provenance_status"] \
        == "missing_signal_funding"


def test_hourly_regime_marks_are_bounded_and_loadable(tmp_path):
    conn = _conn(tmp_path)
    IS.record_altshort_regime_marks(
        conn, {"AAA": 100, "BTC": 200}, funding_rates={"AAA": 0.0001}, ts_ms=3_600_001
    )
    IS.record_altshort_regime_marks(
        conn, {"AAA": 101, "BTC": 201}, funding_rates={"AAA": 0.0002}, ts_ms=3_600_999
    )
    IS.record_altshort_regime_marks(conn, {"AAA": 90, "BTC": 190}, ts_ms=7_200_001)
    rows = conn.execute("SELECT * FROM hl_altshort_regime_marks ORDER BY ts_ms, coin").fetchall()
    assert len(rows) == 4  # same hour upserts, not append-every-cycle
    bars = IS.load_altshort_regime_bars(conn, start_ts_ms=0, end_ts_ms=8_000_000)
    assert bars["AAA"] == [
        {"ts_ms": 3_600_000, "open": 100.0, "high": 101.0, "low": 100.0,
         "close": 101.0, "funding_rate": 0.0002},
        {"ts_ms": 7_200_000, "open": 90.0, "high": 90.0, "low": 90.0,
         "close": 90.0},
    ]


def test_regime_mark_migration_adds_sampled_ohlc(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE hl_altshort_regime_marks ("
        "ts_ms INTEGER NOT NULL, coin TEXT NOT NULL, mark REAL NOT NULL, "
        "funding_rate REAL, PRIMARY KEY(ts_ms, coin))"
    )
    raw.execute(
        "INSERT INTO hl_altshort_regime_marks VALUES (3600000, 'AAA', 100, 0.0001)"
    )
    raw.commit()
    raw.close()

    conn = IS.connect(str(path))
    IS.ensure_schema(conn)
    IS.record_altshort_regime_marks(conn, {"AAA": 90}, ts_ms=3_600_500)
    row = conn.execute("SELECT * FROM hl_altshort_regime_marks").fetchone()
    assert row["open_mark"] == 100.0
    assert row["high_mark"] == 100.0
    assert row["low_mark"] == 90.0
    assert row["mark"] == 90.0


def test_regime_mark_tape_prunes_beyond_thirty_days(tmp_path):
    conn = _conn(tmp_path)
    day_ms = 86_400_000
    IS.record_altshort_regime_marks(conn, {"AAA": 100}, ts_ms=day_ms)
    IS.record_altshort_regime_marks(conn, {"AAA": 95}, ts_ms=31 * day_ms)
    assert len(conn.execute("SELECT 1 FROM hl_altshort_regime_marks").fetchall()) == 2
    IS.record_altshort_regime_marks(conn, {"AAA": 92}, ts_ms=32 * day_ms)
    assert len(conn.execute("SELECT 1 FROM hl_altshort_regime_marks").fetchall()) == 3
    IS.record_altshort_regime_marks(conn, {"AAA": 90}, ts_ms=34 * day_ms)
    rows = conn.execute("SELECT ts_ms FROM hl_altshort_regime_marks ORDER BY ts_ms").fetchall()
    assert [row["ts_ms"] for row in rows] == [31 * day_ms, 32 * day_ms, 34 * day_ms]
