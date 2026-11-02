"""Known provider currencies must rule out unrelated pools before a PoolKey log scan."""
import json
from pathlib import Path

import pytest

from sniper.execution import evm_v4_multihop as mh, evm_v4_quote as v4
from sniper.execution.evm_wallet import CHAIN_QUOTE

CHAIN = "1"
CASH = "0x" + "00" * 20
WRAPPED = CHAIN_QUOTE[CHAIN]["token"]
BRIDGE = "0xc9eef266834730340a55b6cc24621b31baf55581"
OTHER = "0xd4419c2d3daa986dc30444fa333a846be44fd1eb"
GOOD, WRONG, UNKNOWN = ("0x" + byte * 32 for byte in ("aa", "bb", "cc"))


def _ds(rows):
    return {"pairs": [{"chainId": "ethereum", "pairAddress": pid,
                       "baseToken": a, "quoteToken": b, "liquidity": {"usd": depth}}
                      for pid, a, b, depth in rows]}


def _gt(rows):
    def token(value):
        if not isinstance(value, dict):
            return value
        address = value.get("address")
        return {"data": {"id": "eth_" + address}} if address else {}
    return {"data": [{"id": "eth_" + pid, "attributes": {"reserve_in_usd": depth},
                      "relationships": {"base_token": token(a), "quote_token": token(b)}}
                     for pid, a, b, depth in rows]}


def _read(provider, rows, *, token=BRIDGE, pair_token=CASH):
    return mh._pools_with_depth(
        CHAIN, token, include_store=False, pair_token=pair_token,
        gt_http=lambda _url: _gt(rows) if provider == "gt" else {"data": []},
        ds_http=lambda _url: _ds(rows) if provider == "ds" else {"pairs": []})


@pytest.mark.parametrize("provider", ["gt", "ds"])
@pytest.mark.parametrize("reversed_pair", [False, True])
@pytest.mark.parametrize("cash", [CASH, WRAPPED])
def test_pair_filter_keeps_genuine_pair_in_either_orientation_and_native_alias(
        provider, reversed_pair, cash):
    a, b = {"address": BRIDGE.upper().replace("0X", "0x")}, {"address": cash}
    if reversed_pair:
        a, b = b, a
    rows = [(WRONG, {"address": BRIDGE}, {"address": OTHER}, 99_000),
            (GOOD, a, b, 20_000)]
    assert _read(provider, rows) == [(GOOD, 20_000)]


@pytest.mark.parametrize("provider", ["gt", "ds"])
@pytest.mark.parametrize("missing", [{}, {"address": "bad"}, [], None])
@pytest.mark.parametrize("side", [0, 1])
def test_incomplete_pair_metadata_remains_unknown_for_authoritative_key_proof(provider, missing, side):
    currencies = [{"address": BRIDGE}, {"address": OTHER}]
    currencies[side] = missing
    assert _read(provider, [(UNKNOWN, *currencies, 1_000)]) == [(UNKNOWN, 1_000)]


def test_arc_usdc_is_an_erc20_pool_currency_not_a_native_alias():
    quote = CHAIN_QUOTE["5042"]["token"]
    data = _ds([(WRONG, {"address": BRIDGE}, {"address": CASH}, 20_000)])
    for pair in data["pairs"]:
        pair["chainId"] = "arc"
    pools, failed = v4._ds_pools_ranked("5042", BRIDGE, pair_token=quote, ds_http=lambda _u: data)
    assert not failed and pools == []


@pytest.mark.parametrize("provider", ["gt", "ds"])
def test_pair_filter_preserves_provider_failure(provider):
    if provider == "gt":
        assert v4._gt_pools_ranked(CHAIN, BRIDGE, gt_http=lambda _u: None,
                                   pair_token=CASH) == ([], True)
    else:
        assert v4._ds_pools_ranked(CHAIN, BRIDGE, ds_http=lambda _u: None,
                                   pair_token=CASH) == ([], True)


@pytest.fixture
def pair_options(monkeypatch):
    monkeypatch.setattr(mh, "_direct_bridge_pools", lambda *a, **kw: None)
    monkeypatch.setattr(mh, "_onchain_pair_options", lambda *a, **kw: [])
    monkeypatch.setattr(mh, "_BRIDGE_LAST_GOOD", {})
    monkeypatch.setattr(v4, "_stored_pools_for_mint", lambda *a, **kw: [])


@pytest.mark.parametrize("leg", ["bridge", "middle"])
def test_incognito_spcx_zik_fixture_never_reaches_pool_key_discovery(pair_options, monkeypatch, leg):
    data = json.loads((Path(__file__).parent / "fixtures/memecoin/incognito_spcx_pairs.json").read_text())
    seen = []
    monkeypatch.setattr(mh, "_pool_key_for", lambda _c, pid, _r, **kw: seen.append(pid))
    kw = dict(limit=3, gt_http=lambda _u: {"data": []}, ds_http=lambda _u: data)
    fn = mh._bridge_options if leg == "bridge" else mh._middle_leg_options
    assert fn(CHAIN, CASH, BRIDGE, "https://offline.example", **kw) == []
    assert seen == [], "the source bridge's unrelated SPCX/ZIK pools must not spend log-scan capacity"


@pytest.mark.parametrize("leg", ["bridge", "middle"])
@pytest.mark.parametrize("key_matches", [False, True])
def test_provider_pair_filter_retains_exact_onchain_currency_check(
        pair_options, monkeypatch, leg, key_matches):
    seen = []
    key = {"currency0": CASH if key_matches else OTHER, "currency1": BRIDGE,
           "fee": 500, "tick_spacing": 10, "hooks": CASH}
    def read_key(_c, pid, _r, **kw):
        seen.append(pid)
        return key
    monkeypatch.setattr(mh, "_pool_key_for", read_key)
    rows = [(WRONG, {"address": BRIDGE}, {"address": OTHER}, 99_000),
            (GOOD, {"address": WRAPPED}, {"address": BRIDGE}, 20_000)]
    fn = mh._bridge_options if leg == "bridge" else mh._middle_leg_options
    got = fn(CHAIN, CASH, BRIDGE, "https://offline.example", limit=3,
             gt_http=lambda _u: {"data": []}, ds_http=lambda _u: _ds(rows))
    assert seen == [GOOD]
    assert got == ([(GOOD, key, 20_000)] if key_matches else [])
