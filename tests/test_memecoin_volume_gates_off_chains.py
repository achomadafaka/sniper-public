"""MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS skips every volume gate on the listed chains only
(operator 2026-09-29: "remove the volume filters for bsc and base")."""
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R


@pytest.fixture
def gated(monkeypatch):
    for name, value in {
        "MEMECOIN_REAL_MIN_VOLUME_H1_USD": 3000.0,
        "MEMECOIN_REAL_MIN_TURNOVER_H1": 0.01,
        "MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED": True,
        "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD": 2_000_000.0,
        "MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS": frozenset(),
        "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED": True,
        "MEMECOIN_REAL_MIN_TXNS_M5": 0,
        "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO": 0.0,
        "MEMECOIN_REAL_MAX_SELL_SHARE_PCT": 0.0,
    }.items():
        monkeypatch.setattr(R.cfg, name, value)
    monkeypatch.setattr(R.cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.0)
    monkeypatch.setattr(R.cfg, "inband_max_turnover_m5_for", lambda *args: 0.0)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(m, "entry_gate_exemptions", lambda *a, **k: frozenset())


def _hook(monkeypatch, chain):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "source")
    reached = []
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: reached.append(True) or {"status": "test_reached"})
    now = "2026-09-29T21:00:00+00:00"
    mint = "B5ANwoWkptDv1QszdPHXjCF2gTWdo27FGLffdfXzzoe6" if chain == "solana" else "0x" + "a" * 40
    event = SimpleNamespace(
        mint=mint, ticker="LOWVOL", source_user_id="u1", source_handle="source", network_id=chain,
        side="buy", fomo_event_id="ev-" + chain, created_at=now, their_price=8.5e-05,
        their_size_usd=698.6, signal_source="follow_activity", is_first_buy=True)
    result = R.real_entry_hook(
        conn, event, {chain: {}}, now_fn=lambda: now, trade_size=175, max_age_s=120,
        resolved_identity=R.ResolvedEntryIdentity(chain, "resolved", "test"),
        classification_context={"effective_mcap": 79_675.0, "assumed_micro": False,
            "consensus_n_60m": 1, "market_stats": {"price": event.their_price,
                "market_cap": 79_704, "volume_h1": 22.08, "volume_m5": None,
                "liquidity": 26_573.83}})
    return result, reached


@pytest.mark.parametrize("chain", ["56", "8453"])
def test_listed_chain_skips_the_volume_gate(gated, monkeypatch, chain):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS", frozenset({"56", "8453"}))
    result, reached = _hook(monkeypatch, chain)
    assert "turnover_under_floor" not in str(result.get("reason")), result
    assert reached == [True], result


@pytest.mark.parametrize("chain", ["solana", "56"])
def test_unlisted_chain_is_still_gated(gated, monkeypatch, chain):
    monkeypatch.setattr(R.cfg, "MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS",
                        frozenset({"8453"}) if chain == "56" else frozenset({"56", "8453"}))
    result, reached = _hook(monkeypatch, chain)
    assert result["status"] == "rejected", result
    assert "turnover_under_floor" in result["reason"]
    assert reached == []

