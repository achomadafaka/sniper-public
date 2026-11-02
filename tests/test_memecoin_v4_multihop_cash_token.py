"""The multihop's cash-currency bug (2026-09-17, Arc build).

dex_v4_multihop_quote/dex_v4_multihop_exit_quote hardcoded _NATIVE as the bridge-search cash
currency, chain-wide. On 4663/8453/56 that is correct: their V4 pools price against wrapped
native (or its unwrapped form), and _NATIVE is exactly that currency. On Arc (5042), V4 pools
price against the real USDC ERC-20 (0x3600...0000) -- native and quote share one BALANCE there
(native_is_quote), but the POOL KEYS reference the ERC-20 address, never the native zero-address
sentinel. Searching for a bridge against _NATIVE on Arc therefore finds nothing even when a real,
adequately-deep, fixed-fee USDC-paired bridge exists: XAUM/USDC, pool 0x5ca7a0c9..., fee 10000
(1%), unhooked, $5,232 GT depth, confirmed nonzero on-chain liquidity -- which resolves AURGUS and
AU's real routes (round trips -10.78% and -8.04%) the moment the search asks about the right
currency. `_multihop_cash(chain)` is the fix: the real quote token for a native_is_quote chain,
_NATIVE for every other chain, unchanged.
"""
import pytest

import sniper.execution.evm_v4_multihop as MH
from sniper.execution.evm_v4_quote import _NATIVE

CHAIN_4663 = "4663"
CHAIN_ARC = "5042"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
COIN = "0xffbb" + "22" * 18
DYNAMIC_FEE = 0x800000
CLIP_USD = 32.81

# Real addresses from this session's live investigation.
ARC_USDC = "0x3600000000000000000000000000000000000000"
XAUM = "0x178b01f61cbea1d2a5581fe1621be607835ec349"
AURGUS = "0xe0553fef4e865ee02a184ac63771c9d5d176534d"
AU = "0x82e7ab2c72d6c6b06d310833ad2101ddf6d94da9"


def _key(c0, c1, fee, hooks=None):
    return {"currency0": c0, "currency1": c1, "fee": fee, "tick_spacing": 60,
           "hooks": hooks or "0x" + "00" * 20}


# --- _multihop_cash: the fix itself -------------------------------------------------------------

def test_arc_cash_is_the_real_usdc_quote_token():
    assert MH._multihop_cash(CHAIN_ARC) == ARC_USDC


def test_4663_8453_56_cash_stays_native():
    for chain in ("4663", "8453", "56"):
        assert MH._multihop_cash(chain) == _NATIVE


# --- dispatch: dex_v4_multihop_quote/_exit_quote use _multihop_cash, not a bare _NATIVE ---------

def test_entry_dispatch_searches_the_real_cash_token_on_arc(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    seen = {}

    def fake_multihop_quote(chain, cash_token, mint, amount, *, rpc_url, **kw):
        seen["cash"] = cash_token
        return {"amount_out": 1_000_000, "coin_key": _key(XAUM, AURGUS, 10000),
               "round_trip_pct": -8.0, "coin_depth_usd": 5000, "legs_path": None,
               "bridge": XAUM, "bridge_key": _key(ARC_USDC, XAUM, 10000)}
    monkeypatch.setattr(MH, "multihop_quote", fake_multihop_quote)

    from sniper.execution import evm_v4_multihop_tx as tx
    build_seen = {}

    def fake_as_zerox_quote(chain, cash_token, mint, route, amount, min_out, **kw):
        build_seen["cash"] = cash_token
        return {"route": {}}
    monkeypatch.setattr(tx, "as_zerox_quote", fake_as_zerox_quote)

    got = MH.dex_v4_multihop_quote(
        CHAIN_ARC, ARC_USDC, AURGUS, 40_000_000, "0xtaker", rpc_url="https://rpc",
        clip_usd=CLIP_USD)
    assert got is not None
    assert seen["cash"] == ARC_USDC
    assert build_seen["cash"] == ARC_USDC


def test_entry_dispatch_still_searches_native_on_4663(monkeypatch):
    """Byte-identical pin: 4663 must see the exact same _NATIVE value it always did."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    seen = {}

    def fake_multihop_quote(chain, cash_token, mint, amount, *, rpc_url, **kw):
        seen["cash"] = cash_token
        return None
    monkeypatch.setattr(MH, "multihop_quote", fake_multihop_quote)

    MH.dex_v4_multihop_quote(
        CHAIN_4663, weth, COIN, 10**18, "0xtaker", rpc_url="https://rpc", clip_usd=CLIP_USD)
    assert seen["cash"] == _NATIVE


def test_exit_dispatch_searches_the_real_cash_token_on_arc(monkeypatch):
    seen = {"candidates_cash": None, "bridge_cash": None}
    orig_candidates = MH._candidate_bridges
    orig_select_bridge = MH._select_bridge

    def spy_candidates(chain, cash, mint, rpc_url, configured, **kw):
        seen["candidates_cash"] = cash
        return []
    monkeypatch.setattr(MH, "_candidate_bridges", spy_candidates)

    got = MH.dex_v4_multihop_exit_quote(
        CHAIN_ARC, AURGUS, 500_000, "0xtaker", route=None, rpc_url="https://rpc",
        clip_usd=CLIP_USD)
    assert got is None                      # no candidates -> no route, that's fine
    assert seen["candidates_cash"] == ARC_USDC


def test_exit_dispatch_still_searches_native_on_4663(monkeypatch):
    seen = {}

    def spy_candidates(chain, cash, mint, rpc_url, configured, **kw):
        seen["cash"] = cash
        return []
    monkeypatch.setattr(MH, "_candidate_bridges", spy_candidates)

    MH.dex_v4_multihop_exit_quote(
        CHAIN_4663, COIN, 10**18, "0xtaker", route=None, rpc_url="https://rpc",
        clip_usd=CLIP_USD)
    assert seen["cash"] == _NATIVE


def test_stored_route_replay_keeps_using_that_routes_own_bridge_no_native_leak(monkeypatch):
    """A STORED route (from a prior entry) must not re-derive cash from _NATIVE anywhere in the
    2-leg exit math -- _pair_of(bridge_key, cash, bridge) must use the real cash token too."""
    monkeypatch.setattr(MH, "_quote_leg", lambda chain, key, zf1, amt, rpc, **k:
                        900 if zf1 is False else None)
    route = {"bridge": XAUM, "bridge_key": _key(ARC_USDC, XAUM, 10000),
            "coin_key": _key(XAUM, AURGUS, 10000)}

    from sniper.execution import evm_v4_multihop_tx as tx
    build_seen = {}

    def fake_as_zerox_exit_quote(chain, cash_token, mint, selected, amount, min_out, **kw):
        build_seen["cash"] = cash_token
        return {"route": {}}
    monkeypatch.setattr(tx, "as_zerox_exit_quote", fake_as_zerox_exit_quote)

    MH.dex_v4_multihop_exit_quote(
        CHAIN_ARC, AURGUS, 500_000, "0xtaker", route=route, rpc_url="https://rpc",
        clip_usd=CLIP_USD)
    assert build_seen.get("cash") == ARC_USDC


# --- quote-side fixture: real AU/AURGUS shape, USDC resolves, _NATIVE does not ------------------

@pytest.fixture
def xaum_wire(monkeypatch):
    st = {"pools": {}, "keys": {}, "depth": {}, "quotes": {}, "paths": {}}

    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda chain, tok, **k: [(p, st["depth"].get(p)) for p in st["pools"].get(tok, [])])
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *_a, **_kw: None)
    monkeypatch.setattr(MH, "_pool_key_for", lambda chain, pid, rpc, **k: st["keys"].get(pid))
    monkeypatch.setattr(MH, "_quote_leg",
                        lambda chain, key, zf1, amt, rpc, **k: st["quotes"].get(
                            (key["currency0"], key["currency1"], zf1)))

    def quote_path(_chain, currency_in, _path, _amount, _rpc, **_kw):
        # Keyed purely on the STARTING currency: this fixture only needs to prove whether a full
        # forward+reverse round trip exists starting from cash (USDC) vs starting from _NATIVE --
        # _select_bridge already decides that difference (real pool is XAUM/USDC, not
        # XAUM/_NATIVE) before this is ever reached in the _NATIVE case.
        return st["paths"].get(str(currency_in).lower())

    monkeypatch.setattr(MH, "_quote_path", quote_path)
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a, **k: [])
    monkeypatch.setattr(MH, "_decision_block", lambda *a, **k: "latest")
    # real pool: XAUM(c0)/USDC(c1) 1% fee, unhooked, real depth (0x5ca7a0c9..., $5,232.47 GT depth)
    st["pools"][XAUM] = ["p_bridge"]
    st["keys"]["p_bridge"] = _key(XAUM, ARC_USDC, 10000)
    st["depth"]["p_bridge"] = 5232.47
    st["paths"][ARC_USDC.lower()] = 2_450_067_069_875_571_452_632_038   # forward: USDC -> ... -> AURGUS
    st["paths"][AURGUS.lower()] = 41_000_000                            # reverse: AURGUS -> ... -> USDC
    return st


def test_entry_route_carries_rpc_url_for_the_permit2_nonce_lookup(monkeypatch):
    """multihop_quote's own return dict never carried rpc_url -- build_multihop_entry's permit2
    nonce lookup (_permit2_input -> _permit2_nonce) reads route.get("rpc_url") and silently
    build_failed for any ERC-20 cash token (every existing multihop chain's cash is native, so
    this branch was never exercised before Arc's real, live route reached it during the dry
    proof: TRANSFER_FROM_FAILED never happened -- entry build_failed happened first)."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)

    def fake_multihop_quote(chain, cash_token, mint, amount, *, rpc_url, **kw):
        return {"amount_out": 1_000_000, "coin_key": _key(XAUM, AURGUS, 10000),
               "round_trip_pct": -8.0, "coin_depth_usd": 5000, "legs_path": None,
               "bridge": XAUM, "bridge_key": _key(ARC_USDC, XAUM, 10000)}
    monkeypatch.setattr(MH, "multihop_quote", fake_multihop_quote)

    from sniper.execution import evm_v4_multihop_tx as tx
    seen = {}

    def fake_as_zerox_quote(chain, cash_token, mint, route, amount, min_out, **kw):
        seen["route"] = route
        return {"route": {}}
    monkeypatch.setattr(tx, "as_zerox_quote", fake_as_zerox_quote)

    MH.dex_v4_multihop_quote(
        CHAIN_ARC, ARC_USDC, AURGUS, 40_000_000, "0xtaker", rpc_url="https://rpc.example",
        clip_usd=CLIP_USD)
    assert seen["route"].get("rpc_url") == "https://rpc.example"


def test_usdc_cash_resolves_the_xaum_bridge(xaum_wire):
    """multihop_quote(chain, USDC, AURGUS, ...) must find the real XAUM/USDC bridge."""
    st = xaum_wire
    st["pools"][AURGUS] = ["p_coin"]
    st["keys"]["p_coin"] = _key(XAUM, AURGUS, 10000, hooks="0x" + "ff" * 20)

    route = MH.multihop_quote(
        CHAIN_ARC, ARC_USDC, AURGUS, 40_000_000, rpc_url="https://rpc",
        bridge_currencies=(XAUM,), clip_usd=CLIP_USD)
    assert route is not None
    assert route["bridge"] == XAUM
    assert route["bridge_pool_id"] == "p_bridge"


def test_native_cash_does_not_resolve_the_xaum_bridge(xaum_wire):
    """The bug, pinned: searching against _NATIVE on this exact fixture finds nothing, because
    the real bridge pool is XAUM/USDC, not XAUM/_NATIVE -- _select_bridge's own pairing check
    refuses it before any quote is even attempted."""
    st = xaum_wire
    st["pools"][AURGUS] = ["p_coin"]
    st["keys"]["p_coin"] = _key(XAUM, AURGUS, 10000, hooks="0x" + "ff" * 20)

    route = MH.multihop_quote(
        CHAIN_ARC, _NATIVE, AURGUS, 40_000_000, rpc_url="https://rpc",
        bridge_currencies=(XAUM,), clip_usd=CLIP_USD)
    assert route is None
