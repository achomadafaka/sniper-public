"""Adapter over hyperliquid-python-sdk. Contract: EVERY market call carries explicit
slippage (the SDK default is 5% — unacceptable); closes go through market_close (reduce-only
IOC per SDK); leverage is isolated 1x; all reads parse to plain dicts; SDK HTTP calls have a
finite timeout."""
import sys
import types

from hyperliquid.utils.types import Cloid

from sniper.hyperlive.hl_exchange import HlExchange


class FakeSdkExchange:
    def __init__(self):
        self.calls = []

    def market_open(self, name, is_buy, sz, px=None, slippage=0.05, cloid=None):
        self.calls.append(("market_open", name, is_buy, sz, px, slippage, cloid))
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(sz), "avgPx": "100.0"}}]}}}

    def market_close(self, coin, sz=None, px=None, slippage=0.05, cloid=None):
        self.calls.append(("market_close", coin, sz, px, slippage, cloid))
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.15", "avgPx": "101.0"}}]}}}

    def update_leverage(self, leverage, name, is_cross=True):
        self.calls.append(("update_leverage", leverage, name, is_cross))
        return {"status": "ok"}


class FakeSdkInfo:
    def __init__(self):
        self.meta_calls = 0

    def meta(self):
        self.meta_calls += 1
        return {"universe": [{"name": "ZEC", "szDecimals": 2},
                             {"name": "BTC", "szDecimals": 5}]}

    def user_state(self, address):
        return {"assetPositions": [
            {"position": {"coin": "ZEC", "szi": "0.15", "entryPx": "100.0"}},
            {"position": {"coin": "BTC", "szi": "-0.001", "entryPx": "50000.0"}}]}

    def query_order_by_cloid(self, user, cloid):
        if cloid.to_raw() == "0x" + "ab" * 16:
            return {"status": "order", "order": {"status": "filled", "order": {}}}
        return {"status": "unknownOid"}

    def user_fills(self, address):
        c = "0x" + "ab" * 16
        return [
            {"coin": "ZEC", "px": "100.0", "sz": "0.10", "fee": "0.01", "cloid": c},
            {"coin": "ZEC", "px": "102.0", "sz": "0.05", "fee": "0.005", "cloid": c},
            {"coin": "BTC", "px": "50000", "sz": "0.001", "fee": "0.02", "cloid": None},
        ]


def _adapter():
    fe, fi = FakeSdkExchange(), FakeSdkInfo()
    ad = HlExchange(api_wallet_file="unused.json", account_address="0xacc",
                    base_url="https://x", entry_slippage=0.005, exit_slippage=0.01,
                    sdk_exchange=fe, sdk_info=fi)
    return ad, fe, fi


CLOID = "0x" + "ab" * 16


def test_market_open_explicit_slippage_and_cloid():
    ad, fe, _ = _adapter()
    ad.market_open("ZEC", True, 0.15, CLOID)
    (name, coin, is_buy, sz, px, slippage, cloid) = fe.calls[0]
    assert (name, coin, is_buy, sz) == ("market_open", "ZEC", True, 0.15)
    assert slippage == 0.005                        # never the 5% SDK default
    assert isinstance(cloid, Cloid) and cloid.to_raw() == CLOID


def test_market_close_explicit_slippage_and_cloid():
    ad, fe, _ = _adapter()
    ad.market_close("ZEC", CLOID)
    (name, coin, sz, px, slippage, cloid) = fe.calls[0]
    assert (name, coin, sz) == ("market_close", "ZEC", None)   # sz None = close whole position
    assert slippage == 0.01
    assert isinstance(cloid, Cloid)


def test_set_isolated_1x_once_per_coin():
    ad, fe, _ = _adapter()
    ad.set_isolated_1x("ZEC")
    ad.set_isolated_1x("ZEC")                       # cached — no second call
    assert fe.calls == [("update_leverage", 1, "ZEC", False)]


def test_positions_parse():
    ad, _, _ = _adapter()
    pos = ad.positions()
    assert pos == {"ZEC": {"szi": 0.15, "entry_px": 100.0},
                   "BTC": {"szi": -0.001, "entry_px": 50000.0}}


def test_sz_decimals_cached():
    ad, _, fi = _adapter()
    assert ad.sz_decimals("ZEC") == 2
    assert ad.sz_decimals("BTC") == 5
    assert fi.meta_calls == 1


def test_order_status_and_unknown():
    ad, _, _ = _adapter()
    st = ad.order_status(CLOID)
    assert st["status"] == "filled"
    assert ad.order_status("0x" + "cd" * 16) is None


def test_fills_for_cloid_aggregates():
    ad, _, _ = _adapter()
    agg = ad.fills_for_cloid(CLOID)
    assert abs(agg["sz"] - 0.15) < 1e-12
    assert abs(agg["px"] - (100.0 * 0.10 + 102.0 * 0.05) / 0.15) < 1e-9   # size-weighted
    assert abs(agg["fee"] - 0.015) < 1e-12
    assert ad.fills_for_cloid("0x" + "ee" * 16) is None


def test_lazy_sdk_info_has_finite_timeout(monkeypatch):
    captured = {}

    class FakeInfo:
        def __init__(self, base_url, *, skip_ws, timeout):
            captured.update(base_url=base_url, skip_ws=skip_ws, timeout=timeout)

    monkeypatch.setitem(sys.modules, "hyperliquid.info", types.SimpleNamespace(Info=FakeInfo))
    ad = HlExchange(api_wallet_file="unused.json", account_address="0xacc",
                    base_url="https://x", entry_slippage=0.005, exit_slippage=0.01)
    ad._get_info()
    assert captured == {"base_url": "https://x", "skip_ws": True, "timeout": 10.0}
