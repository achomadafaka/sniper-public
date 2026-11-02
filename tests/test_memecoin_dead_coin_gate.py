"""Dead-coin conjunction gate (operator via Alpha, 2026-10-07: "dead old coin why are we entering this").

SWCH (yansanity, position 71539): a 104-day-old coin, decision-time volume_h1 $0.49, mcap $60k,
closed manually -$0.59. Solana nano/micro has no hourly floor, and no gate knew the coin's age.
Alpha's read (funded Solana closes since 09-08 with trace volume_h1, n=330): age alone is fine
(>=30d n=34, +1.4%), low volume alone is fine (h1<$100 n=9, +4.4%, fresh launches), OLD AND DEAD
loses (age>=7d and h1<$100 n=4, -8.2%, 0 wins; age>=7d and h1 $100-1k n=7, -2.3%). Small n: the
operator decides activation. The gate refuses only on MEMECOIN_REAL_DEAD_COIN_CHAINS (default empty)
and records its would-be decision on every chain. Unknown age or unknown volume never refuses.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R

DAY = 86_400.0


@pytest.mark.parametrize("age_s,h1,expected", [
    (104 * DAY, 0.49, (True, "dead_coin")),
    (7 * DAY, 999.99, (True, "dead_coin")),          # the bounds: age >= min, h1 < max
    (7 * DAY - 1, 0.0, (False, "young")),
    (30 * DAY, 1000.0, (False, "active")),
    (None, 0.0, (None, "age_unknown")),               # unknown age never refuses
    (30 * DAY, None, (None, "h1_unknown")),           # unknown volume never refuses
])
def test_the_decision_is_a_conjunction_and_unknown_is_never_dead(age_s, h1, expected):
    assert R.dead_coin_decision(age_s, h1, min_age_s=7 * DAY, max_h1_usd=1000.0) == expected


def test_a_zero_knob_turns_the_decision_off():
    assert R.dead_coin_decision(104 * DAY, 0.0, min_age_s=0.0, max_h1_usd=1000.0) == (False, "off")
    assert R.dead_coin_decision(104 * DAY, 0.0, min_age_s=7 * DAY, max_h1_usd=0.0) == (False, "off")


@pytest.fixture
def open_gates(monkeypatch):
    """Solana nano/micro as it runs today: no hourly floor, no m5 floor."""
    for name, value in {
        "MEMECOIN_REAL_MIN_VOLUME_H1_USD": 0.0,
        "MEMECOIN_REAL_MIN_TURNOVER_H1": 0.0,
        "MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED": False,
        "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD": 2_000_000.0,
        "MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS": frozenset(),
        "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED": True,
        "MEMECOIN_REAL_MIN_TXNS_M5": 0,
        "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO": 0.0,
        "MEMECOIN_REAL_MAX_SELL_SHARE_PCT": 0.0,
        "MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS": frozenset(),
        "MEMECOIN_REAL_DEAD_COIN_MIN_AGE_S": 7 * DAY,
        "MEMECOIN_REAL_DEAD_COIN_MAX_H1_USD": 1000.0,
    }.items():
        monkeypatch.setattr(R.cfg, name, value, raising=False)
    monkeypatch.setattr(R.cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.0)
    monkeypatch.setattr(R.cfg, "inband_max_turnover_m5_for", lambda *args: 0.0)
    monkeypatch.setattr(R.cfg, "micro_min_volume_m5_usd_for", lambda *args: 0.0)
    monkeypatch.setattr(R.cfg, "solana_micro_h1_floor", lambda *args: 0.0, raising=False)
    monkeypatch.setattr(R, "solana_micro_h1_floor", lambda *args: 0.0)
    monkeypatch.setattr(R, "h1_abs_floor_for", lambda *a: (0.0, None))
    monkeypatch.setattr(R, "_alert_entry_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(m, "entry_gate_exemptions", lambda *a, **k: frozenset())


def _hook(monkeypatch, *, chain="solana", age_s=104 * DAY, h1=0.49, size=698.6):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "yansanity")
    reached = []
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: reached.append(True) or {"status": "test_reached"})
    now = "2026-10-07T15:51:55+00:00"
    mint = "6gnvghh8LKoM59p1WZSuTgYmdJrnZnhU7BzCcEaEASY" if chain == "solana" else "0x" + "a" * 40
    event = SimpleNamespace(
        mint=mint, ticker="SWCH", source_user_id="u1", source_handle="yansanity", network_id=chain,
        side="buy", fomo_event_id="ev-" + chain, created_at=now, their_price=6.0e-05,
        their_size_usd=size, signal_source="follow_activity", is_first_buy=True)
    stats = {"price": event.their_price, "market_cap": 60_000, "volume_h1": h1, "volume_m5": 0.17,
             "liquidity": 20_000.0}
    if age_s is not None:
        stats["token_age_s"] = age_s
    result = R.real_entry_hook(
        conn, event, {chain: {}}, now_fn=lambda: now, trade_size=200, max_age_s=120,
        resolved_identity=R.ResolvedEntryIdentity(chain, "resolved", "test"),
        classification_context={"effective_mcap": 60_000.0, "assumed_micro": False,
                                "consensus_n_60m": 1, "market_stats": stats})
    traces = [json.loads(r[0] or "{}") for r in conn.execute("SELECT trace_json FROM memecoin_entry_attempts")]
    gate = [e for t in traces for e in t.get("substage_events", []) if e.get("name") == "dead_coin_gate"]
    return result, reached, gate


def test_swch_is_refused_on_a_listed_chain_and_says_why(open_gates, monkeypatch):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_DEAD_COIN_CHAINS", frozenset({"solana"}), raising=False)
    result, reached, gate = _hook(monkeypatch)
    assert result["status"] == "rejected" and reached == []
    assert "dead_coin" in result["reason"] and "104d" in result["reason"]
    assert gate and gate[-1]["would_refuse"] is True and gate[-1]["enforced"] is True


def test_an_unlisted_chain_admits_and_records_the_would_be_refusal(open_gates, monkeypatch):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_DEAD_COIN_CHAINS", frozenset(), raising=False)
    result, reached, gate = _hook(monkeypatch)
    assert reached == [True], result
    assert gate and gate[-1]["would_refuse"] is True and gate[-1]["enforced"] is False
    assert gate[-1]["reason"] == "dead_coin" and gate[-1]["age_s"] == pytest.approx(104 * DAY)


@pytest.mark.parametrize("age_s,h1,reason", [(2 * DAY, 0.49, "young"), (104 * DAY, 25_000.0, "active"),
                                             (None, 0.49, "age_unknown"), (104 * DAY, None, "h1_unknown")])
def test_young_active_or_unknown_coins_pass_on_a_listed_chain(open_gates, monkeypatch, age_s, h1, reason):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_DEAD_COIN_CHAINS", frozenset({"solana"}), raising=False)
    result, reached, gate = _hook(monkeypatch, age_s=age_s, h1=h1)
    assert reached == [True], result
    assert gate and gate[-1]["reason"] == reason and gate[-1]["would_refuse"] is not True


def test_the_copied_buy_is_not_counted_as_the_coins_activity(open_gates, monkeypatch):
    """The source's own $698 buy inside the provider's h1 must not make a dead coin look alive."""
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_DEAD_COIN_CHAINS", frozenset({"solana"}), raising=False)
    monkeypatch.setattr(R.cfg, "provider_index_lag_s", lambda chain: 0.0)      # the buy is indexed
    result, reached, gate = _hook(monkeypatch, h1=1100.0, size=698.6)
    assert gate and gate[-1]["organic_h1_usd"] < 1000.0
    assert result["status"] == "rejected" and "dead_coin" in result["reason"]
