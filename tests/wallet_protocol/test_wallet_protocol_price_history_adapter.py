"""Acceptance tests for the wallet-protocol prices-history adapter.

Closes wallet-protocol-bulletproofing gap C5. The adapter wraps Polymarket's
`/prices-history` endpoint so the protocol's CLI can inject a
`price_history_fn` and H6 (held-analyst exit quality) can run.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _mock_response(status_code: int = 200, json_data: dict | None = None,
                   raise_for_json: bool = False) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if raise_for_json:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = json_data or {"history": []}
    return resp


def test_returns_first_bar_at_or_after_target_ts():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    target_ts = 1_775_000_000
    bars = [
        {"t": target_ts - 60, "p": 0.40},        # before target
        {"t": target_ts + 30, "p": 0.55},        # first at/after target
        {"t": target_ts + 600, "p": 0.60},
    ]
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={"history": bars})

    fn = build_price_history_fn(session=session)
    price = fn("0xasset", target_ts)
    assert price == 0.55
    call_kwargs = session.get.call_args.kwargs
    params = call_kwargs["params"]
    assert params["market"] == "0xasset"
    assert params["fidelity"] == 60
    assert params["startTs"] == target_ts
    assert params["endTs"] - params["startTs"] < 24 * 3600


def test_returns_none_on_http_error():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    session = MagicMock()
    session.get.return_value = _mock_response(status_code=500, json_data={})
    fn = build_price_history_fn(session=session)
    assert fn("0xasset", 1_775_000_000) is None

    session.get.return_value = _mock_response(status_code=404, json_data={})
    # Use a different sell_ts so we bypass cache and re-issue.
    fn2 = build_price_history_fn(session=session)
    assert fn2("0xasset", 1_775_000_000) is None


def test_returns_none_when_history_empty():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    session = MagicMock()
    session.get.return_value = _mock_response(json_data={"history": []})
    fn = build_price_history_fn(session=session)
    assert fn("0xasset", 1_775_000_000) is None


def test_returns_none_on_request_exception():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    import requests as requests_module

    session = MagicMock()
    session.get.side_effect = requests_module.ConnectionError("boom")
    fn = build_price_history_fn(session=session)
    assert fn("0xasset", 1_775_000_000) is None


def test_returns_none_on_invalid_json():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    session = MagicMock()
    session.get.return_value = _mock_response(raise_for_json=True)
    fn = build_price_history_fn(session=session)
    assert fn("0xasset", 1_775_000_000) is None


def test_closure_is_memoized_no_second_http_call():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    sell_ts = 1_775_000_000
    target_ts = sell_ts + 24 * 3600
    session = MagicMock()
    session.get.return_value = _mock_response(
        json_data={"history": [{"t": target_ts + 1, "p": 0.42}]},
    )
    fn = build_price_history_fn(session=session)
    p1 = fn("0xasset", sell_ts)
    p2 = fn("0xasset", sell_ts)
    assert p1 == 0.42
    assert p2 == 0.42
    assert session.get.call_count == 1


def test_negative_cache_also_memoized():
    """Empty-history (None) results must memoize too — otherwise we'd
    pummel the API with retries on every dead market."""
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    session = MagicMock()
    session.get.return_value = _mock_response(json_data={"history": []})
    fn = build_price_history_fn(session=session)
    assert fn("0xasset", 1_775_000_000) is None
    assert fn("0xasset", 1_775_000_000) is None
    assert session.get.call_count == 1


def test_custom_fidelity_propagates_without_offset():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    target_ts = 1_775_000_000
    custom_fidelity = 1
    session = MagicMock()
    session.get.return_value = _mock_response(
        json_data={"history": [{"t": target_ts + 1, "p": 0.33}]},
    )
    fn = build_price_history_fn(
        session=session, fidelity=custom_fidelity,
    )
    price = fn("0xasset", target_ts)
    assert price == 0.33
    params = session.get.call_args.kwargs["params"]
    assert params["fidelity"] == custom_fidelity
    assert params["startTs"] == target_ts
    assert params["endTs"] == target_ts + 600


def test_external_cache_shared_across_closures():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    sell_ts = 1_775_000_000
    cache: dict = {}
    session1 = MagicMock()
    session1.get.return_value = _mock_response(
        json_data={"history": [{"t": sell_ts + 86400 + 1, "p": 0.7}]},
    )
    fn1 = build_price_history_fn(session=session1, cache=cache)
    fn1("0xasset", sell_ts)

    session2 = MagicMock()
    fn2 = build_price_history_fn(session=session2, cache=cache)
    price = fn2("0xasset", sell_ts)
    assert price == 0.7
    # Second closure hit the shared cache; never called HTTP.
    assert session2.get.call_count == 0


def test_falls_back_to_last_bar_when_none_reach_target():
    """Window edge: if every bar is before raw target_ts (e.g., market closed),
    use the last bar so we still return *something* rather than punting."""
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    target_ts = 1_775_000_000
    bars = [
        {"t": target_ts - 120, "p": 0.20},
        {"t": target_ts - 60, "p": 0.18},
    ]
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={"history": bars})
    fn = build_price_history_fn(session=session)
    assert fn("0xasset", target_ts) == 0.18


def test_zero_sell_ts_returns_none():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    session = MagicMock()
    fn = build_price_history_fn(session=session)
    assert fn("0xasset", 0) is None
    assert session.get.call_count == 0


def test_empty_market_id_returns_none():
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    session = MagicMock()
    fn = build_price_history_fn(session=session)
    assert fn("", 1_775_000_000) is None
    assert session.get.call_count == 0


def test_lazy_session_when_none_passed():
    """When session=None, the adapter constructs a Session lazily."""
    from sniper.analysis.wallet_protocol.price_history_adapter import (
        build_price_history_fn,
    )

    fake_session = MagicMock()
    fake_session.get.return_value = _mock_response(
        json_data={"history": [{"t": 1_775_000_000 + 86400 + 1, "p": 0.9}]},
    )
    with patch(
        "sniper.analysis.wallet_protocol.price_history_adapter.requests.Session",
        return_value=fake_session,
    ) as ctor:
        fn = build_price_history_fn()
        assert fn("0xasset", 1_775_000_000) == 0.9
    assert ctor.called
