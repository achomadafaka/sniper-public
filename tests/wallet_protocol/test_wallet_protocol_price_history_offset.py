"""price_history_fn returns raw timestamp prices; consumers own offsets."""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import run_fh6_directional_skill
from sniper.analysis.wallet_protocol.held_analyst import run_h6_exit_quality
from sniper.analysis.wallet_protocol.price_history_adapter import build_price_history_fn


def test_build_price_history_fn_does_not_take_offset_seconds_kwarg():
    sig = inspect.signature(build_price_history_fn)
    assert "offset_seconds" not in sig.parameters


def test_h6_passes_sell_ts_plus_24h_to_price_history_fn():
    calls: list[tuple[str, int]] = []

    def fake_price_history_fn(market_id: str, ts: int) -> float:
        calls.append((market_id, int(ts)))
        return 0.40

    sell_ts = 1700000000
    activity = [
        {
            "asset": f"asset{i}",
            "side": "SELL",
            "timestamp": sell_ts + i * 60,
            "price": 0.50,
        }
        for i in range(25)
    ]

    run_h6_exit_quality(activity, fake_price_history_fn)

    assert calls
    expected = {int(r["timestamp"]) + 24 * 3600 for r in activity}
    assert {ts for _, ts in calls}.issubset(expected)


def test_fh6_passes_raw_timestamps_to_closure():
    calls: list[tuple[str, int]] = []

    def fake_price_history_fn(market_id: str, ts: int) -> float:
        calls.append((market_id, int(ts)))
        return 0.40 + (ts % 3600) / 100000.0

    flips = [
        Flip(
            condition_id=f"0xcid{i}",
            outcome="Yes",
            asset_id=f"asset{i}",
            buy_ts=1700000000 + i * 3600,
            sell_ts=1700000000 + i * 3600 + 600,
            buy_price=0.50,
            sell_price=0.55,
            size=100.0,
            buy_notional=100.0,
            flip_roi=0.10,
            hold_duration_seconds=600,
        )
        for i in range(60)
    ]

    run_fh6_directional_skill(flips, fake_price_history_fn, n_iter=200, seed=1)

    expected_raw = {ts for f in flips for ts in (f.buy_ts, f.sell_ts)}
    assert calls
    assert {ts for _, ts in calls}.issubset(expected_raw)


def test_real_adapter_returns_price_at_input_timestamp_no_offset():
    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"history": [{"t": 1700000000, "p": 0.42}]}

    def _fake_get(url, params=None, timeout=None):
        captured["params"] = dict(params or {})
        return _FakeResp()

    session = MagicMock()
    session.get = _fake_get
    fn = build_price_history_fn(session=session)

    assert fn("asset123", 1700000000) == 0.42
    assert captured["params"]["startTs"] == 1700000000
    assert captured["params"]["endTs"] - captured["params"]["startTs"] < 24 * 3600
