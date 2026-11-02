"""Operator 2026-10-03: remove the Robinhood hourly dollar-volume veto (SPORE 530c00dd)."""
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg
from test_memecoin_volume_gates_off_chains import gated, _hook


@pytest.fixture(autouse=True)
def hourly_policy(gated, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", 0.02)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663", None, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS", frozenset())


def _args(chain, mcap):
    return R.volume_gate_args(
        {"volume_h1": 0.0}, mcap=mcap, network_id=chain,
        coin_band="small", source_user_id="source", signal_usd=349.611737)[3]


@pytest.mark.parametrize("mcap", [None, 0.0, 14_219.0, 1_000_000.0])
def test_explicit_zero_removes_rh_hourly_dollar_veto_even_without_mcap(monkeypatch, mcap):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663", 0.0)
    kwargs = _args("4663", mcap)
    allowed, need, why = R.volume_alive_decision(0.0, mcap, **kwargs)
    assert allowed and need is None and why == "ok"


@pytest.mark.parametrize("mcap,expected", [(None, 3000.0), (14_219.0, 284.38)])
def test_unset_override_keeps_existing_rh_policy(mcap, expected):
    assert _args("4663", mcap)["abs_floor"] == pytest.approx(expected)


@pytest.mark.parametrize("chain", ["56", "8453", "solana"])
def test_rh_override_does_not_remove_other_chain_hourly_floors(monkeypatch, chain):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663", 0.0)
    assert _args(chain, None)["abs_floor"] == 3000.0


def test_rh_override_keeps_short_horizon_volume_gate(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663", 0.0)
    monkeypatch.setattr(cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.02)
    kwargs = _args("4663", 100_000.0)
    kwargs["vol_m5"] = 10.0
    assert R.volume_alive_decision(0.0, 100_000.0, **kwargs)[2] == "m5_turnover_under_floor"


@pytest.mark.parametrize("override,reason", [
    (None, "h1 organic volume $0 under floor $3000 (under_abs_floor)"),
    (0.0, "coin band is not reservable: 'out_of_scope'"),
])
def test_spore_inputs_pass_hourly_gate_without_bypassing_band_gate(monkeypatch, override, reason):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663", override)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "source", "seralberttrades")
    reached = []
    monkeypatch.setattr(R, "real_entry", lambda *a, **kw:
                        reached.append(True) or {"status": "test_executor_reached"})
    now = "2026-10-03T14:58:15.616910+00:00"
    event = SimpleNamespace(
        mint="0xa5127fae2d0986a4cb6619b9c4ec53461726454b", ticker="SPORE",
        source_user_id="source", source_handle="seralberttrades", network_id="4663",
        side="buy", fomo_event_id="spore-530c00dd", created_at=now,
        their_price=1.421e-05, their_size_usd=349.611737,
        signal_source="follow_activity", is_first_buy=True)
    try:
        result = R.real_entry_hook(
            conn, event, {"4663": {}}, now_fn=lambda: now, trade_size=90, max_age_s=120,
            resolved_identity=R.ResolvedEntryIdentity("4663", "resolved", "test"),
            classification_context={"effective_mcap": None, "assumed_micro": False,
                "consensus_n_60m": 1, "market_stats": {"price": event.their_price,
                    "market_cap": None, "volume_h1": 0.0, "volume_m5": None,
                    "liquidity": 3.23}})
        assert reached == []
        assert result["reason"] == reason
    finally:
        conn.close()


def test_low_hourly_volume_reaches_executor_with_reservable_band(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663", 0.0)
    result, reached = _hook(monkeypatch, "4663")
    assert reached == [True], result


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), True, "bad"])
def test_invalid_override_refuses_armed_startup(monkeypatch, value):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663", value)
    with pytest.raises(RuntimeError, match="MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663"):
        cfg.validate_real_exit_policy()


@pytest.mark.parametrize("value", [None, 0.0, 1000.0])
def test_valid_override_allows_armed_startup(monkeypatch, value):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD_4663", value)
    assert cfg.validate_real_exit_policy() is None
