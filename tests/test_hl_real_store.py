"""Store layer for the HL real copy leg (hl_real_intents / hl_real_fills / hl_meta keys)."""
from sniper.hyperlive import intel_store as IS
from sniper.hyperlive import real_store as RS


def _conn(tmp_path):
    c = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(c)
    return c


def test_ensure_schema_idempotent(tmp_path):
    c = _conn(tmp_path)
    IS.ensure_schema(c)                                          # second call must not raise
    names = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"hl_real_intents", "hl_real_fills"} <= names


def test_deployment_created_once(tmp_path):
    c = _conn(tmp_path)
    d1 = RS.get_or_create_deployment(c, now_ms=1000)
    d2 = RS.get_or_create_deployment(c, now_ms=9999)
    assert d1 == d2 and d1["epoch_ms"] == 1000
    assert d1["deployment_id"]


def test_intent_idempotent_and_states(tmp_path):
    c = _conn(tmp_path)
    kw = dict(cloid="0xabc", deployment_id="d1", copy_id=7, leg="open", coin="ZEC",
              side="long", requested_usd=15.0, requested_sz=0.1, state="intended",
              reason=None, now_ms=1000)
    RS.record_intent(c, **kw)
    RS.record_intent(c, **kw)                                    # replay-safe
    assert len(RS.intents_in(c, ["intended"])) == 1
    RS.set_intent_state(c, "0xabc", "submitted", now_ms=2000)
    rows = RS.intents_in(c, ["submitted"])
    assert rows[0]["updated_ts_ms"] == 2000 and rows[0]["copy_id"] == 7
    assert RS.intent_for(c, 7, "open")["cloid"] == "0xabc"
    assert RS.intent_for(c, 7, "close") is None


def test_leg_open_positions_excludes_closed_and_skipped(tmp_path):
    c = _conn(tmp_path)
    RS.record_intent(c, cloid="0xo1", deployment_id="d", copy_id=1, leg="open", coin="ZEC",
                     side="long", requested_usd=15.0, requested_sz=0.1, state="filled",
                     reason=None, now_ms=1)
    RS.record_fill(c, cloid="0xo1", copy_id=1, leg="open", fill_px=100.0, fill_sz=0.1,
                   filled_ts_ms=1)
    RS.record_intent(c, cloid="0xo2", deployment_id="d", copy_id=2, leg="open", coin="SOL",
                     side="long", requested_usd=15.0, requested_sz=None, state="skipped",
                     reason="chase_guard", now_ms=1)
    pos = RS.leg_open_positions(c)
    assert [p["copy_id"] for p in pos] == [1]
    assert pos[0]["fill_px"] == 100.0 and pos[0]["coin"] == "ZEC"
    RS.record_intent(c, cloid="0xc1", deployment_id="d", copy_id=1, leg="close", coin="ZEC",
                     side="long", requested_usd=None, requested_sz=None, state="filled",
                     reason=None, now_ms=2)
    assert RS.leg_open_positions(c) == []


def test_close_finalize_and_day_pnl(tmp_path):
    c = _conn(tmp_path)
    RS.record_intent(c, cloid="0xc9", deployment_id="d", copy_id=3, leg="close", coin="ZEC",
                     side="long", requested_usd=None, requested_sz=None, state="filled",
                     reason=None, now_ms=5)
    # 2026-07-18T00:00:10Z
    RS.record_fill(c, cloid="0xc9", copy_id=3, leg="close", fill_px=102.0, fill_sz=0.1,
                   filled_ts_ms=1784678410000)
    RS.finalize_close(c, cloid="0xc9", realized_usd=-5.0, paper_twin_pnl_bps=10.0,
                      real_net_bps=-333.0, drift_bps=-343.0)
    assert RS.day_realized_usd(c, "2026-07-22") == -5.0
    assert RS.day_realized_usd(c, "2026-07-23") == 0.0
    RS.set_fill_fee(c, "0xc9", 0.02)
    row = c.execute("SELECT fee_usd, drift_bps FROM hl_real_fills WHERE cloid='0xc9'").fetchone()
    assert row["fee_usd"] == 0.02 and row["drift_bps"] == -343.0


def test_breaker_latch_roundtrip(tmp_path):
    c = _conn(tmp_path)
    assert RS.breaker_latched(c) is None
    RS.latch_breaker(c, day_utc="2026-07-18", now_ms=5)
    assert RS.breaker_latched(c) == "2026-07-18"
    RS.clear_breaker(c)
    assert RS.breaker_latched(c) is None
