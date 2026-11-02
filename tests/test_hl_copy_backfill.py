"""Tests for the open-book backfill of target fill + detection-lag (Task J/N)."""
import importlib
import os

from sniper.hyperlive import intel_store as IS


def _mod():
    return importlib.import_module("scripts.hl_copy_backfill")


def _db(tmp_path):
    c = IS.connect(os.path.join(str(tmp_path), "hl.sqlite"))
    IS.ensure_schema(c)
    return c


def test_backfill_stamps_target_fill_and_lag(tmp_path):
    mod = _mod()
    c = _db(tmp_path)
    IS.open_copy(c, "0xt", "ETH", "long", 2000.0, ts_ms=1)      # we entered 2000
    IS.open_copy(c, "0xt", "BTC", "short", 100.0, ts_ms=1)      # we entered 100

    def fake_state(wallet):
        return {"assetPositions": [
            {"position": {"coin": "ETH", "szi": "8", "entryPx": "1999"}},    # target long from 1999
            {"position": {"coin": "BTC", "szi": "-5", "entryPx": "101"}},    # target short from 101
        ]}

    n = mod.backfill_open(c, fetch_state=fake_state)
    assert n == 2
    eth = c.execute("SELECT * FROM hl_copy_book WHERE coin='ETH'").fetchone()
    assert eth["target_entry_px"] == 1999.0 and eth["target_szi"] == 8.0
    assert eth["target_notional"] == round(8 * 1999.0, 2)
    assert eth["lag_bps"] > 0                                    # we bought above target's fill
    btc = c.execute("SELECT * FROM hl_copy_book WHERE coin='BTC'").fetchone()
    assert btc["lag_bps"] > 0                                    # short sold below target's fill


def test_backfill_skips_side_mismatch_and_missing(tmp_path):
    mod = _mod()
    c = _db(tmp_path)
    IS.open_copy(c, "0xt", "ETH", "long", 2000.0, ts_ms=1)      # we long; target now short -> skip
    IS.open_copy(c, "0xt", "SOL", "long", 150.0, ts_ms=1)       # target no longer holds -> skip

    def fake_state(wallet):
        return {"assetPositions": [{"position": {"coin": "ETH", "szi": "-8", "entryPx": "1999"}}]}

    n = mod.backfill_open(c, fetch_state=fake_state)
    assert n == 0
    assert c.execute("SELECT target_entry_px FROM hl_copy_book WHERE coin='ETH'").fetchone()[0] is None


def test_lag_report_aggregates(tmp_path):
    mod = _mod()
    c = _db(tmp_path)
    cid = IS.open_copy(c, "0xt", "ETH", "long", 2000.0, ts_ms=1)
    c.execute("UPDATE hl_copy_book SET lag_bps=7.0, target_notional=15000.0 WHERE id=?", (cid,))
    c.commit()
    rep = mod.lag_report(c)
    assert rep["lag_n"] == 1 and rep["lag_mean_bps"] == 7.0 and rep["lag_pct_positive"] == 100.0
    assert rep["target_notional_median"] == 15000.0
