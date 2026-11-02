"""STRATA a935a15d: a known family must resolve the old ETH bridge without log scans."""
import time

import pytest

from sniper.execution import evm_v4_multihop as MH, evm_v4_quote as V
from sniper.execution.evm_v4_launchpad import pool_id_for_key

NATIVE = "0x" + "00" * 20
BRIDGE = "0xd34a99bc0f67ae1bbd63c660e6d0b0dd03e263b7"
COIN = "0x98395ac29b33c1761ab5b3d2639be03ccfbcf1c2"
POOL = "0xb07d640fd9e2eb9dc81b953c8e4fd006bdfeaf276010fb5418eb763ca15abfb3"
KEY = dict(currency0=NATIVE, currency1=BRIDGE, fee=10000, tick_spacing=200, hooks=NATIVE)
COIN_KEY = dict(currency0=COIN, currency1=BRIDGE, fee=0, tick_spacing=200,
                hooks="0x322dcec4958c14e021a9f1cd49df11b9457968cc")


@pytest.fixture
def offline(monkeypatch):
    calls = []
    monkeypatch.setattr(V, "_key_cache", {("1", pool_id_for_key(COIN_KEY)): COIN_KEY})
    monkeypatch.setattr(V, "_family_cache", {"1": (time.time() + 600, ((10000, 200, NATIVE),))})
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_store_pool_key", lambda *a, **k: pytest.fail("entry must not write"))
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *a, **k: ())
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a, **k: [])
    monkeypatch.setattr(MH, "_BRIDGE_LAST_GOOD", {})
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a, **k: [])
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *a, **k: [(POOL, 100000)])
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *a, **k: [(pool_id_for_key(COIN_KEY), 24759)])
    monkeypatch.setattr(MH, "_decision_block", lambda *a, **k: "0x18ecb7e")
    monkeypatch.setattr(MH, "_quote_path", lambda chain, currency, path, amount, rpc, **kw:
                        1000000 if currency == NATIVE else 97000)

    def rpc(url, method, params, **kw):
        calls.append(method)
        return {"result": "0x18ecb7e" if method == "eth_blockNumber" else []}

    monkeypatch.setattr(V, "_rpc", rpc)
    return calls


def test_strata_entry_derives_the_real_bridge_without_log_reads(offline):
    assert pool_id_for_key(KEY) == POOL
    route = MH.multihop_quote("1", NATIVE, COIN, 100000, rpc_url="offline://rpc",
                              bridge_currencies=(), clip_usd=20)
    assert route is not None
    assert route["bridge_key"] == KEY
    assert route["round_trip_pct"] == pytest.approx(-3)
    assert offline == []
    # The exit's shared bridge selector also reuses the verified key.
    assert MH._select_bridge("1", NATIVE, BRIDGE, "offline://rpc")[1] == KEY
    assert offline == []


@pytest.mark.parametrize("cash,families", [
    (NATIVE, ()),                              # cold family cache
    (NATIVE, ((10000, 60, NATIVE),)),           # wrong family
    ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", ((10000, 200, NATIVE),)),  # wrong pair
])
def test_unproven_bridge_keeps_the_log_fallback(offline, monkeypatch, cash, families):
    monkeypatch.setattr(V, "_family_cache", {"1": (time.time() + 600, families)})
    assert MH._select_bridge("1", cash, BRIDGE, "offline://rpc") is None
    assert "eth_getLogs" in offline
    assert ("1", POOL) not in V._key_cache


def test_dynamic_fee_bridge_stays_refused(offline, monkeypatch):
    key = dict(KEY, fee=0x800000)
    pid = pool_id_for_key(key)
    monkeypatch.setattr(V, "_family_cache", {"1": (time.time() + 600, ((0x800000, 200, NATIVE),))})
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *a, **k: [(pid, 100000)])
    assert MH._select_bridge("1", NATIVE, BRIDGE, "offline://rpc") is None
