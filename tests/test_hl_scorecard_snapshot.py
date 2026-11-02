"""Tests for scorecard snapshot persistence (intel_store + CLI core)."""
import importlib
import os

from sniper.hyperlive import intel_store as IS


def _db(tmp_path):
    c = IS.connect(os.path.join(str(tmp_path), "hl.sqlite"))
    IS.ensure_schema(c)
    return c


def test_snapshot_table_and_roundtrip(tmp_path):
    c = _db(tmp_path)
    payload = {
        "realized": {"n": 104, "mean": 21.8, "ci_lo": -22.8, "ci_hi": 69.2, "spans_zero": True},
        "total_book": {"total_bps": 9318.3, "open_n": 171},
        "concentration": {"top1_share": 0.34, "top5_share": 1.64},
        "beta_neutral": {"n": 104, "alpha": {"mean": 52.2, "ci_lo": 10.4, "ci_hi": 97.4,
                                             "spans_zero": False}},
    }
    sid = IS.save_scorecard_snapshot(c, payload, ts_ms=1000, holdout=False)
    assert sid > 0
    rows = IS.get_scorecard_snapshots(c, holdout=False)
    assert len(rows) == 1
    r = rows[0]
    assert r["realized_n"] == 104 and r["realized_mean"] == 21.8 and r["spans_zero"] == 1
    assert r["total_bps"] == 9318.3 and r["alpha_mean"] == 52.2 and r["alpha_spans_zero"] == 0
    assert r["top1_share"] == 0.34


def test_holdout_filter(tmp_path):
    c = _db(tmp_path)
    p = {"realized": {"n": 5, "mean": 1.0}, "total_book": {}, "concentration": {}, "beta_neutral": {}}
    IS.save_scorecard_snapshot(c, p, ts_ms=1000, holdout=False)
    IS.save_scorecard_snapshot(c, p, ts_ms=1001, holdout=True)
    assert len(IS.get_scorecard_snapshots(c, holdout=True)) == 1
    assert len(IS.get_scorecard_snapshots(c, holdout=False)) == 1
    assert len(IS.get_scorecard_snapshots(c)) == 2


def test_cli_take_snapshot(tmp_path, monkeypatch):
    db = os.path.join(str(tmp_path), "hl.sqlite")
    c = IS.connect(db)
    IS.ensure_schema(c)
    cid = IS.open_copy(c, "0xa", "ETH", "long", 2000.0, ts_ms=1000)
    IS.close_copy(c, cid, exit_mark=2040.0, pnl_bps=191.0, ts_ms=int(3.6e6))
    c.close()
    from sniper.routes import api_hyperlive as APIHL
    from sniper.markets import hyperliquid_api as HL
    monkeypatch.setattr(APIHL, "INTEL_DB", db)
    monkeypatch.setattr(HL, "fetch_marks", lambda: {"ETH": 2040.0})
    monkeypatch.setattr(HL, "fetch_candles", lambda *a, **k: [])
    mod = importlib.import_module("scripts.hl_scorecard_snapshot")
    conn = IS.connect(db)
    n = mod.take_snapshot(conn, now_ms=5000)
    assert n >= 1                                        # at least the all-rows snapshot saved
    assert len(IS.get_scorecard_snapshots(conn, holdout=False)) == 1
