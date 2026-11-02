"""J7 relays carry no market cap on 12% of buys (2026-09-11: 63/504, 53 on 4663). The alert
printed "mcap ~?". The sender thread fills a DISPLAY-only value; the copy path never waits."""
import sqlite3
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_runner as run


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); m.add_wallet(c, "u1", "alice"); return c


def _ev(**kw):
    d = dict(fomo_event_id="e1", source_user_id="u1", source_handle="alice", mint="0xabc",
             ticker="ABC", their_size_usd=500.0, their_market_cap=0.0, their_price=0.002,
             created_at="2026-09-11T10:00:00Z", network_id="4663", lag_s=1.0,
             signal_source="follow_activity")
    d.update(kw)
    return SimpleNamespace(**d)


def test_prepare_marks_a_missing_mcap_for_the_sender_and_a_present_one_not():
    c = _conn()
    job = run._prepare_alert(c, _ev())
    assert "rebuild" in job and "mcap ~?" in job["message"]
    job2 = run._prepare_alert(c, _ev(fomo_event_id="e2", their_market_cap=123456.0))
    assert "rebuild" not in job2 and "$123.5k" in job2["message"]


def test_sender_fills_dexscreener_mcap_and_rebuilds(monkeypatch):
    c = _conn()
    job = run._prepare_alert(c, _ev(network_id="solana", mint="So1"))
    monkeypatch.setattr("sniper.services.memecoin_follow_activity._live_display_mcap",
                        lambda mint, net: 52800.0)
    job = run.finish_alert_job(job)
    assert "$52.8k" in job["message"] and "mcap ~?" not in job["message"]


def test_sender_falls_back_to_price_times_supply_on_a_dexscreener_blind_chain(monkeypatch):
    c = _conn()
    job = run._prepare_alert(c, _ev())
    monkeypatch.setattr("sniper.services.memecoin_follow_activity._live_display_mcap",
                        lambda mint, net: None)
    seen = {}
    def _builder():
        def mcap_for(chain, mint, price):
            seen.update(chain=chain, mint=mint, price=price); return price * 1_000_000_000
        return mcap_for
    monkeypatch.setattr("sniper.services.memecoin_evm_price.build_evm_mcap_for", _builder)
    job = run.finish_alert_job(job)
    assert seen == {"chain": "4663", "mint": "0xabc", "price": 0.002}
    assert "$2.0M" in job["message"]


def test_a_lookup_failure_leaves_the_alert_unchanged(monkeypatch):
    c = _conn()
    job = run._prepare_alert(c, _ev())
    before = job["message"]
    def _boom(mint, net): raise RuntimeError("provider down")
    monkeypatch.setattr("sniper.services.memecoin_follow_activity._live_display_mcap", _boom)
    assert run.finish_alert_job(job)["message"] == before
    assert run.finish_alert_job({"message": "x"}) == {"message": "x"}
