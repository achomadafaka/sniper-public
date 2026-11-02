"""Channel B (CLV via price-history) + Channel E (on-chain funding graph). Both must
degrade LOUDLY: a missing price -> None (gap), no RPC -> dark + data_gap, never a silent
zero or a phantom coordination claim."""
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis import clv as CLV
from sniper.analysis.winning_wallet_thesis import funding_graph as FG
from helpers_thesis import make_episode


# ---- Channel B: CLV ---------------------------------------------------------------------------

def test_clv_positive_when_close_above_entry():
    raw = [{"asset": "tok1", "conditionId": "c1", "outcomeIndex": 0}]
    ep = make_episode(cid="c1", oidx=0, entry=0.3, cost=300, won=1, resolution_ts=10000)
    fn = CLV.build_clv_fn(raw, price_history_fn=lambda a, ts: 0.8 if a == "tok1" else None)
    v = fn(ep)
    assert v is not None and v > 0.4  # 0.8 - 0.3


def test_clv_none_when_price_unavailable():
    raw = [{"asset": "tok1", "conditionId": "c1", "outcomeIndex": 0}]
    ep = make_episode(cid="c1", oidx=0, entry=0.3, cost=300, won=1, resolution_ts=10000)
    fn = CLV.build_clv_fn(raw, price_history_fn=lambda a, ts: None)
    assert fn(ep) is None


def test_clv_none_when_no_asset_mapping():
    ep = make_episode(cid="cX", oidx=0, entry=0.3, cost=300, won=1, resolution_ts=10000)
    fn = CLV.build_clv_fn([], price_history_fn=lambda a, ts: 0.9)
    assert fn(ep) is None


def test_clv_none_when_no_resolution_ts():
    raw = [{"asset": "tok1", "conditionId": "c1", "outcomeIndex": 0}]
    ep = make_episode(cid="c1", oidx=0, entry=0.3, cost=300, won=1, resolution_ts=0)
    fn = CLV.build_clv_fn(raw, price_history_fn=lambda a, ts: 0.8)
    assert fn(ep) is None


# ---- Channel E: funding graph -----------------------------------------------------------------

def test_funding_identifies_funder_and_age():
    logs = [{"from": "0xFUND", "ts": 1000, "value": 500.0},
            {"from": "0xFUND", "ts": 2000, "value": 100.0}]
    out = FG.build_funding("0xW", log_fetch=lambda w: logs, now_ts=1000 + 10 * 86400)
    assert out["funder"] == "0xfund"
    assert out["funded_ts"] == 1000
    assert abs(out["funder_age_days"] - 10) < 0.1


def test_funding_linkage_and_synchronized_entries():
    logs = [{"from": "0xFUND", "ts": 1000, "value": 500.0}]
    out = FG.build_funding(
        "0xW", log_fetch=lambda w: logs,
        peer_funders={"0xpeer": "0xfund", "0xother": "0xzzz"},
        my_entries=[("cMatch", 5000)],
        peer_entries={"0xpeer": [("cMatch", 5300)]},
        sync_window_s=3600, now_ts=2000)
    assert "0xpeer" in out["linked_wallets"]
    assert "0xother" not in out["linked_wallets"]
    assert out["synchronized_entries"] >= 1


def test_funding_no_sync_outside_window():
    logs = [{"from": "0xFUND", "ts": 1000, "value": 500.0}]
    out = FG.build_funding(
        "0xW", log_fetch=lambda w: logs,
        peer_funders={"0xpeer": "0xfund"},
        my_entries=[("cMatch", 5000)],
        peer_entries={"0xpeer": [("cMatch", 99999)]},  # far outside window
        sync_window_s=3600, now_ts=2000)
    assert out["synchronized_entries"] == 0


def test_funding_dark_when_no_rpc():
    out = FG.build_funding("0xW", log_fetch=None, rpc_url=None)
    assert out["funder"] is None
    assert any("no RPC" in g.lower() or "no rpc" in g.lower() for g in out["data_gaps"])
    assert out["linked_wallets"] == []


def test_funding_rpc_error_is_disclosed_not_silent_empty():
    out = FG.build_funding("0xW", log_fetch=lambda w: [{"_error": "rpc timeout"}], rpc_url=None)
    assert out["funder"] is None
    assert any("rpc timeout" in g.lower() for g in out["data_gaps"])
