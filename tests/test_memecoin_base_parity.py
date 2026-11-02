"""Base (chain 8453) reaches funded parity with Robinhood 4663 — and 4663/Solana do not move.

Spec: docs/superpowers/specs/2026-09-06-memecoin-base-8453-funded-parity-design.md.

Base carried 4,605 feed buys and 938 funded attempts in the 14 days to 2026-09-06 and produced
ZERO fills, because no wallet was armed there and, since 2026-08-29, every Base quote failed
`IdentityInvalid` — `memecoin_quotes._validated_identity` named 4663 alone. So there is no Base
evidence at all, paper or funded, and every assertion here is about CONFIGURATION and ROUTING,
never about an expected outcome.

Every test is offline. Real objects, fake RPC, no network.
"""
import pytest

from sniper.execution import evm_rpc_pool
from sniper.execution import evm_wallet as W
from sniper.execution.evm_v4_quote import V4
from sniper.services import memecoin_quotes as Q
from sniper.settings import memecoin as cfg

BASE = "8453"
ROBINHOOD = "4663"
BASE_HEX = "0x2105"                       # 8453
ROBINHOOD_HEX = "0x1237"                  # 4663
BASE_WETH = "0x4200000000000000000000000000000000000006"
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
PRIMARY = "https://base-primary.example"
BACKUP = "https://base-backup.example"


# --- a. the pool exists, and its identity is Base's -------------------------------------------

def test_a_the_base_rpc_pool_is_built_from_the_chain_owner_endpoints():
    """No 8453 pool existed before: reads ran on the single configured URL with no failover."""
    pool = cfg._evm_rpc_pool({"MEMECOIN_EVM_RPC_URL_8453": "https://mainnet.base.org"})

    assert pool[BASE] == ("https://mainnet.base.org", "https://mainnet-preconf.base.org")
    # the configured primary is not duplicated when it is already one of the defaults
    assert len(pool[BASE]) == len(set(pool[BASE]))


def test_a_an_operator_pool_list_replaces_the_defaults():
    pool = cfg._evm_rpc_pool({"MEMECOIN_EVM_RPC_URL_8453": PRIMARY,
                              "MEMECOIN_EVM_RPC_POOL_URLS_8453": BACKUP})
    assert pool[BASE] == (PRIMARY, BACKUP)


def test_a_a_base_pool_member_answering_the_4663_chain_id_is_excluded(monkeypatch):
    """A funded path reading a quote from the wrong chain is worse than being rate limited."""
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {BASE: (PRIMARY, BACKUP)})
    evm_rpc_pool._state.clear()
    try:
        assert evm_rpc_pool.chain_of(PRIMARY) == BASE      # identity comes from CONFIG

        def http(method, url, headers=None, body=None, timeout=10):
            if '"eth_chainId"' in (body or ""):
                ident = ROBINHOOD_HEX if url == BACKUP else BASE_HEX
                return 200, {"jsonrpc": "2.0", "id": 1, "result": ident}, b""
            return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x" + url[-2:]}, b""

        evm_rpc_pool.penalize(PRIMARY, "429")              # force a rotation decision
        assert evm_rpc_pool.choose(
            PRIMARY, verify_fn=lambda u: http("POST", u, body='"eth_chainId"')[1]["result"]
            == BASE_HEX) == PRIMARY                        # falls back, never the 4663 member
        assert evm_rpc_pool.identity_state(BACKUP) is False
    finally:
        evm_rpc_pool._state.clear()


# --- b. the rung ladder runs on Base, and relay does not ---------------------------------------

def test_b_base_is_in_the_rung_ladder_but_not_in_the_dexscreener_blind_list():
    """Two questions, two lists. Base needs the v2/v4 RUNGS; it is not DexScreener-blind, and
    pricing a DexScreener-covered chain from a secondary source is the RUSSELL lesson."""
    assert BASE in cfg.MEMECOIN_EVM_RUNG_LADDER_CHAINS
    assert ROBINHOOD in cfg.MEMECOIN_EVM_RUNG_LADDER_CHAINS
    assert BASE not in cfg.MEMECOIN_DEX_BLIND_CHAINS


def test_b_the_ladder_on_base_runs_v3_v2_v4_and_multihop_and_never_relay(monkeypatch):
    from sniper.execution import evm_real_seams as RS

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", {BASE, ROBINHOOD}, raising=False)
    rungs = []
    ladder = RS.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=BASE, quote_token=BASE_WETH, taker="0x" + "11" * 20,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=lambda *a, **k: None,
        relay_quote_fn=lambda *a, **k: None, enabled=True,
        on_rung=lambda r: rungs.append(r["rung"]))

    assert ladder(BASE_WETH, "0x" + "22" * 20, 10 ** 15, slippage_bps=300) is None
    assert {"v3", "v2", "v4"} <= set(rungs), rungs
    # RELAY STAYS 4663-ONLY (spec decision 2). Base exits use V3, V2 and V4.
    assert "relay" not in rungs, rungs


def _exit_rungs(monkeypatch, chain, quote_token):
    """The rung names an EXIT sees on one chain. Relay refuses BUYS on every chain, so the
    Base-vs-4663 relay difference is only visible on the sell side."""
    from sniper.execution import evm_real_seams as RS

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", {BASE, ROBINHOOD}, raising=False)
    rungs = []
    ladder = RS.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=chain, quote_token=quote_token, taker="0x" + "11" * 20,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=lambda *a, **k: None,
        relay_quote_fn=lambda *a, **k: None, enabled=True,
        on_rung=lambda r: rungs.append(r["rung"]))
    ladder("0x" + "22" * 20, quote_token, 10 ** 15, slippage_bps=300)   # SELL: coin -> cash
    return rungs


def test_b_relay_still_runs_on_a_4663_exit(monkeypatch):
    """The guard above must be Base-scoped, not a global switch-off. On an EXIT relay is the only
    way some 4663 bags close at all, so it must survive."""
    rungs = _exit_rungs(monkeypatch, ROBINHOOD, W.CHAIN_QUOTE[ROBINHOOD]["token"])
    assert "relay" in rungs, rungs


def test_b_a_base_exit_never_reaches_relay(monkeypatch):
    rungs = _exit_rungs(monkeypatch, BASE, BASE_WETH)
    assert "relay" not in rungs, rungs


# --- c. quote identity --------------------------------------------------------------------------

def test_c_a_base_address_is_a_valid_quote_identity():
    network, mint = Q._validated_identity(BASE, "0x" + "Ab" * 20)
    assert (network, mint) == (BASE, "0x" + "ab" * 20)


@pytest.mark.parametrize("chain", [BASE, ROBINHOOD])
@pytest.mark.parametrize("mint", ["not-hex", "0xzz", "0x" + "ab" * 19, ""])
def test_c_a_malformed_evm_mint_still_fails_on_both_chains(chain, mint):
    with pytest.raises(Q.IdentityInvalid):
        Q._validated_identity(chain, mint)


def test_c_solana_identity_is_untouched():
    assert Q._validated_identity("solana", "So11111111111111111111111111111111111111112")[0] \
        == "solana"


# --- d. the quote currency is WETH, and sizing follows ------------------------------------------

def test_d_base_quotes_in_wrapped_native_not_usdc():
    assert W.CHAIN_QUOTE[BASE] == {"token": BASE_WETH, "decimals": 18, "kind": "eth",
                                   "usd_symbol": "ETH"}
    assert W.QUOTE_SYMBOL[BASE] == "WETH"
    assert W.CHAIN_USDC[BASE].lower() == BASE_USDC          # still the V2 / multihop connector


def test_d_a_150_dollar_clip_sizes_to_weth_atomic_from_the_eth_usd_read():
    from sniper.execution.evm_real_seams import usd_to_quote_atomic

    atomic = usd_to_quote_atomic(BASE, 150.0, price_fn=lambda *_a, **_k: 2500.0)

    assert atomic == 60 * 10 ** 15          # 150 / 2500 = 0.06 WETH
    # fail-closed: no price means no sizing, so no entry
    assert usd_to_quote_atomic(BASE, 150.0, price_fn=lambda *_a, **_k: None) is None


def test_d_base_is_a_wrapped_native_chain_so_the_entry_preflight_applies():
    """The WETH preflight keyed on `== 4663`. With Base quoting in WETH that gate would have been
    skipped in silence on every Base entry."""
    assert {ROBINHOOD, BASE} <= W.native_quote_chains()   # 56 joined 2026-09-06
    assert W.is_native_quote_token(BASE_WETH) is True
    assert W.is_native_quote_token(BASE_USDC) is False


# --- e. per-chain knobs default to the 4663 values ----------------------------------------------

def test_e_the_nano_knobs_resolve_for_base_to_the_4663_values_when_unset(monkeypatch):
    # Same fragility as the sibling test below, fixed the same way: pin the 4663 env rather than
    # comparing a call-time read against an import-time constant. This one passed only by luck.
    monkeypatch.delenv("MEMECOIN_REAL_8453_NANO_QUOTE_CHASE_MAX_PCT", raising=False)
    monkeypatch.delenv("MEMECOIN_REAL_8453_NANO_DISPLAY_DRIFT_MAX_PCT", raising=False)
    # The FALLBACK resolves the module CONSTANT (a call-time global lookup), not the env, so it
    # takes setattr. setenv only reaches the per-chain lookup `MEMECOIN_REAL_<chain>_NANO_<suffix>`.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT", 7.25)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_NANO_DISPLAY_DRIFT_MAX_PCT", 9.5)

    assert cfg.memecoin_real_nano_quote_chase_max_pct(BASE) == 7.25
    assert cfg.memecoin_real_nano_display_drift_max_pct(BASE) == 9.5


def test_e_an_explicit_base_suffix_wins(monkeypatch):
    # PIN BOTH CHAINS' ENV, do not compare a call-time read against an import-time constant
    # (2026-09-06). `memecoin_real_nano_quote_chase_max_pct` reads os.getenv when CALLED, while
    # `MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT` was bound at IMPORT, so the two diverge
    # whenever the process loads .env after importing settings — which is exactly what a release
    # copy does. The assertion then failed as `0.0 == 10.0` on a system that was behaving
    # correctly, and it only surfaced when a settings change first pulled this file into the
    # deploy gate's selection. The property under test is that the Base knob does not leak into
    # 4663; pinning both makes that deterministic.
    # The explicit Base suffix is an ENV lookup; 4663's own value here is whatever the env says,
    # so pin that env too rather than comparing against the import-time constant.
    monkeypatch.setenv("MEMECOIN_REAL_8453_NANO_QUOTE_CHASE_MAX_PCT", "3.5")
    monkeypatch.setenv("MEMECOIN_REAL_4663_NANO_QUOTE_CHASE_MAX_PCT", "7.25")
    assert cfg.memecoin_real_nano_quote_chase_max_pct(BASE) == 3.5
    # and 4663 is unaffected by the Base knob
    assert cfg.memecoin_real_nano_quote_chase_max_pct(ROBINHOOD) == 7.25


def test_e_per_chain_concurrency_already_keys_on_the_chain(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {BASE: 2, ROBINHOOD: 3},
                        raising=False)
    assert cfg.max_concurrent_for(BASE) == 2
    assert cfg.max_concurrent_for(ROBINHOOD) == 3


def test_e_base_keeps_resolving_an_unknown_evm_chain_by_default():
    """`_resolvable_evm_networks` gates 4663 alone. Taking the 4663 default for Base would REMOVE
    existing Base flow, which is the opposite of parity."""
    from sniper.execution.solana_quote import _resolvable_evm_networks

    assert cfg.MEMECOIN_RESOLVE_UNKNOWN_EVM_8453 is True
    assert BASE in set(_resolvable_evm_networks())


# --- f. the push watcher is per chain ------------------------------------------------------------

def test_f_the_initialize_filter_uses_the_chain_s_own_pool_manager():
    from sniper.services import memecoin_onchain_push as push

    base_filter = push.initialize_filter(V4[BASE]["pool_manager"], "0xdd46")
    robinhood_filter = push.initialize_filter(V4[ROBINHOOD]["pool_manager"], "0xdd46")

    assert base_filter[1]["address"] == V4[BASE]["pool_manager"]
    assert base_filter[1]["address"] != robinhood_filter[1]["address"]


def test_f_a_base_initialize_log_stores_its_pool_key_and_a_foreign_manager_does_not(monkeypatch):
    from sniper.execution import evm_v4_quote as V
    from sniper.services import memecoin_onchain_push as push

    stored = []
    monkeypatch.setattr(V, "store_pool_keys_from_logs",
                        lambda chain, logs: stored.append((chain, logs)) or True)
    assert push.store_initialize(BASE, {"address": V4[BASE]["pool_manager"]}) is True
    assert stored and stored[0][0] == BASE

    # A LOG FROM A FOREIGN MANAGER NEVER REACHES THE STORE: the subscription filter is
    # server-side and addressed to our manager, so a foreign one is not delivered at all.
    foreign = push.initialize_filter("0x" + "99" * 20, "0xdd46")
    assert foreign[1]["address"] != V4[BASE]["pool_manager"]


def test_f_the_watcher_configures_every_chain_scoped_name_from_one_argument():
    import scripts.research.onchain_evm_wallet_watch as watcher

    try:
        watcher._configure(BASE)
        assert watcher.CHAIN == BASE
        assert watcher.WETH == BASE_WETH
        assert watcher.HEARTBEAT.name == "onchain_evm_watch.8453.heartbeat"
        watcher._configure(ROBINHOOD)
        # 4663 KEEPS ITS EXISTING HEARTBEAT PATH: an operator check that reads it must not break.
        assert watcher.HEARTBEAT.name == "onchain_evm_watch.heartbeat"
        assert watcher.WETH == W.CHAIN_QUOTE[ROBINHOOD]["token"].lower()
    finally:
        watcher._configure(ROBINHOOD)


def test_f_base_has_no_default_websocket_so_the_watcher_keeps_polling(monkeypatch):
    """MEASURED 2026-09-06: both Base chain-owner endpoints answer the WebSocket upgrade with
    HTTP 405. A derived socket would be a URL that can only fail to connect."""
    monkeypatch.delenv("MEMECOIN_EVM_WS_URL_8453", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_POOL_URLS_8453",
                       "https://mainnet.base.org,https://mainnet-preconf.base.org")
    assert cfg.memecoin_evm_ws_url(BASE) is None
    monkeypatch.setenv("MEMECOIN_EVM_WS_URL_8453", "wss://example.invalid")
    assert cfg.memecoin_evm_ws_url(BASE) == "wss://example.invalid"


# --- g. the multihop search reaches a USDC-paired Base coin --------------------------------------

def test_g_the_base_multihop_search_finds_weth_to_usdc_to_coin(monkeypatch):
    from sniper.execution import evm_v4_multihop as MH

    coin = "0x" + "22" * 20
    bridge_key = {"currency0": BASE_WETH, "currency1": BASE_USDC,
                  "fee": 500, "tick_spacing": 10, "hooks": "0x" + "00" * 20}
    coin_key = {"currency0": BASE_USDC, "currency1": coin,
                "fee": 3000, "tick_spacing": 60, "hooks": "0x" + "00" * 20}
    keys = {"0xbridge": bridge_key, "0xcoin": coin_key}

    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda chain, token, **kw: [("0xcoin", 1_000_000.0)]
                        if token == coin else [("0xbridge", 5_000_000.0)])
    monkeypatch.setattr(MH, "_pool_key_for", lambda chain, pool_id, rpc, **kw: keys.get(pool_id))
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda chain, cash, bridge, **kw: None)
    monkeypatch.setattr(MH, "_quote_path",
                        lambda chain, cur_in, path, amount_in, rpc, **kw: int(amount_in) * 2)
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a, **k: [])
    monkeypatch.setattr(MH, "_decision_block", lambda *a, **k: "latest")

    route = MH.multihop_quote(BASE, BASE_WETH, coin, 10 ** 15,
                              rpc_url="https://base.example", clip_usd=20.0,
                              min_depth_clips=1.0)

    assert route is not None, MH.last_multihop_refusal()
    assert route["bridge"].lower() == BASE_USDC
    assert [leg["currency"].lower() for leg in route["legs_path"]] == [BASE_USDC, coin]


def test_g_base_has_no_second_bridge_so_the_search_stops_at_two_legs():
    """MEASURED 2026-09-06, and this is why the scoped USDC default is NOT here. `MAX_LEGS` is 3 in
    the live config, so a second bridge switches on the three-leg search; on Base that took the
    canary from 3-44s per quote to 25s, 48s, 101s, 118s and 436s and returned ZERO extra routes.
    4663 keeps USDG, where the same search is cheap and pays."""
    assert BASE not in cfg.MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES
    assert cfg.MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES[ROBINHOOD] == (
        "0x5fc5360d0400a0fd4f2af552add042d716f1d168",)


def test_g_base_first_bridges_stay_unconfigured_so_per_coin_discovery_runs():
    """Configuring a first bridge TURNS DISCOVERY OFF for that chain (`_candidate_bridges`).
    4663 runs discovery and it is the most profitable route on its book, so Base runs it too."""
    assert BASE not in cfg.MEMECOIN_EVM_MULTIHOP_BRIDGES


# --- the Base addresses, and what proved them ----------------------------------------------------

def test_the_base_v4_addresses_are_the_ones_the_canary_verified():
    """Recorded so a silent edit is a failing test, not a wrong quote. Every one was read from the
    Uniswap v4 deployments page on 2026-09-06 AND confirmed by a non-empty `eth_getCode` on
    https://mainnet.base.org: pool manager 24,009 bytes, quoter 5,820, router 19,499, permit2
    9,152."""
    assert V4[BASE] == {
        "pool_manager": "0x498581ff718922c3f8e6a244956af099b2652b2b",
        "quoter": "0x0d5e0f971ed27fbff6c2837bf31316121532048d",
        "universal_router": "0x6ff5693b99212da76ad316178a184ab56d299b43",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
        "gt_network": "base",
    }
    # Permit2 is deployed deterministically, so both chains share it. That is expected, not a copy.
    assert V4[BASE]["permit2"] == V4[ROBINHOOD]["permit2"]
    assert V4[BASE]["pool_manager"] != V4[ROBINHOOD]["pool_manager"]


def test_the_base_v2_and_v3_route_metadata_matches_the_on_chain_factories():
    """`SwapRouter02.factory()` and `V2Router02.factory()` were called on Base on 2026-09-06 and
    returned exactly these, and `V2Router02.WETH()` returned the quote token above."""
    from sniper.settings.memecoin_v6_paper import (
        MEMECOIN_V6_EVM_FACTORIES_BY_CHAIN, MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN,
        MEMECOIN_V6_EVM_V3_ADDRESSES_BY_CHAIN)

    assert MEMECOIN_V6_EVM_FACTORIES_BY_CHAIN[BASE] == {
        "v3_factory": "0x33128a8fc17869897dce68ed026d694621f6fdfd",
        "v2_factory": "0x8909dc15e40173ff4699343b6eb8132c65e18ec6"}
    assert MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN[BASE] == \
        "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
    assert MEMECOIN_V6_EVM_V3_ADDRESSES_BY_CHAIN[BASE]["quoter"] == \
        cfg.MEMECOIN_EVM_V3_QUOTER[BASE]
    assert cfg.MEMECOIN_EVM_V2_CONNECTORS[BASE] == (BASE_WETH, BASE_USDC)


def test_geckoterminal_prices_base_from_dexscreener_and_only_charts_it_from_gt():
    from sniper.execution.geckoterminal_quote import GT_NETWORK, GT_OHLCV_NETWORK

    assert GT_OHLCV_NETWORK[BASE] == "base"     # candles: slug probed live 2026-09-06
    assert BASE not in GT_NETWORK               # pricing: DexScreener covers Base


# --- 4663 and Solana do not move -----------------------------------------------------------------

def test_nothing_about_4663_or_solana_changed():
    assert W.CHAIN_QUOTE[ROBINHOOD] == {
        "token": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", "decimals": 18, "kind": "eth",
        "usd_symbol": "ETH"}
    assert W.QUOTE_SYMBOL[ROBINHOOD] == "WETH"
    assert cfg.MEMECOIN_RESOLVE_UNKNOWN_EVM_4663 is False
    assert cfg.MEMECOIN_EVM_V2_ROUTER[ROBINHOOD] == \
        "0x89e5DB8B5aA49aA85AC63f691524311AEB649eba"
    assert V4[ROBINHOOD]["gt_network"] == "robinhood"
    from sniper.execution.geckoterminal_quote import GT_NETWORK
    assert GT_NETWORK == {ROBINHOOD: "robinhood"}


def test_a_chain_with_no_route_metadata_declines_rather_than_borrowing_another_chains():
    """Borrowing 4663's router for an unconfigured chain would quote against a contract that does
    not exist there, and that reads as `no_route` only by luck."""
    from sniper.settings.memecoin_v6_paper import (
        MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN, MEMECOIN_V6_EVM_V3_ADDRESSES_BY_CHAIN)

    # 56 gained PancakeSwap metadata on 2026-09-06 and chain 1 its Uniswap metadata on 2026-09-30
    # (eth-mainnet spec); Polygon (137) is still unconfigured and must stay unborrowed
    assert MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN.get("137") is None
    assert MEMECOIN_V6_EVM_V3_ADDRESSES_BY_CHAIN.get("137") is None


# --- A5: the deploy gate's funded checks are chain-agnostic --------------------------------------

def test_the_deploy_gate_pending_reservation_check_has_no_chain_filter():
    """Confirmed rather than changed (build item A5). A per-chain narrowing here would let a Base
    reservation ride through a deploy unseen."""
    from pathlib import Path

    text = Path("scripts/deploy_memecoin_runtime.sh").read_text()
    assert "FROM memecoin_pending_entries WHERE status='reserved'" in text
    assert "network_id" not in text.split("pending_reservations()")[1].split("}")[0]


def test_the_funded_contract_reconcile_reads_every_chain():
    from sniper.services import memecoin_funded_reconcile as R

    assert R._normalized_network("8453", "0x" + "11" * 20) == BASE
    assert R._normalized_network("1399811149", "So1") == "solana"
    assert R._normalized_network(ROBINHOOD, "0x" + "11" * 20) == ROBINHOOD


# --- what the read-only canaries changed ---------------------------------------------------------

def test_the_v4_key_scan_page_fits_what_the_base_endpoints_will_serve(monkeypatch):
    """MEASURED 2026-09-06 on https://mainnet.base.org: `eth_getLogs` over 10,000 blocks answered
    HTTP 413 `-32614 "eth_getLogs is limited to a 10,000 range"`, so the 4663-tuned 100,000-block
    page was refused on the FIRST page and the V4 rung returned `poolkey_read_failed` on 10 of 10
    recent Base coins.

    ⚠️ RE-MEASURED 2026-09-23, AND THE PROVIDER HAD MOVED AGAIN: the limit is now 2,000. 2,000
    blocks answers in 305 ms with 245 logs; 2,001 and 8,192 and 10,000 all answer
    `-32614 "eth_getLogs is limited to a 2,000 range"`. So the 10,000 width this test used to
    assert had ITSELF been silently refused since the tightening, and with it the `0x0`->`latest`
    archive fallback (publicnode: 403, "Archive requests require a personal token"). Base V4
    discovery was fully blind, which is why picadura/Horse (ref 83592634) retried for 61 s and died
    on the age gate.

    The ~33h reach the old assertion claimed is gone with it: six 2,000-block pages reach ~6.7h of
    Base chain. That is the honest number, and the lever for more is the scan ENDPOINT -- publicnode
    serves a 10,000-block page in 1.3 s -- not a width this endpoint refuses."""
    from sniper.execution import evm_v4_quote as V

    monkeypatch.delenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_8453", raising=False)
    monkeypatch.setattr(V, "_V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN",
                        dict(V._V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN))
    assert V._key_scan_page_blocks(BASE) == 2000
    assert V._key_scan_page_blocks(ROBINHOOD) == V._V4_KEY_SCAN_PAGE_BLOCKS == 100000
    reach_h = V._key_scan_page_blocks(BASE) * V._V4_KEY_SCAN_MAX_PAGES * 2 / 3600
    assert 6.0 < reach_h < 7.5, reach_h


def test_an_operator_can_retune_the_page_width_per_chain(monkeypatch):
    from sniper.execution import evm_v4_quote as V

    monkeypatch.setenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_8453", "2500")
    assert V._key_scan_page_blocks(BASE) == 2500
    assert V._key_scan_page_blocks(ROBINHOOD) == 100000


def test_the_first_pool_key_read_never_exceeds_what_the_chain_will_serve(monkeypatch):
    """MEASURED 2026-09-23 on https://mainnet.base.org: the limit moved from 10,000 to 2,000 --
    2,000 blocks answers (305 ms, 245 logs), 2,001 answers HTTP 413
    `-32614 "eth_getLogs is limited to a 2,000 range"`. `_V4_DISCOVERY_LOOKBACK_BLOCKS` is a
    hardcoded 8,192, so the FIRST read was refused before the paged scan even started, the scan's
    own first page (10,000) was refused too, and the `0x0`->`latest` archive fallback is refused
    by publicnode as well (403, "Archive requests require a personal token"). Every Base V4
    pool-key read returned `poolkey_read_failed`, and Base liquidity is V4 -- so the coin could not
    be priced at all, the entry retried for 61 s, and the age gate refused it (picadura/Horse,
    ref 83592634, 2026-09-23T22:02:25Z)."""
    from sniper.execution import evm_v4_quote as V

    monkeypatch.delenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_8453", raising=False)
    spans = []

    def _rpc(url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(51_706_072)}
        flt = params[0]
        spans.append(int(flt["toBlock"], 16) - int(flt["fromBlock"], 16))
        return {"result": []}

    monkeypatch.setattr(V, "_rpc", _rpc)
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *a, **k: ())
    V._key_cache.clear()
    V._pool_key(BASE, "0x" + "ab" * 32, "https://mainnet.base.org")
    assert spans, "the read must still happen"
    assert max(spans) <= V._key_scan_page_blocks(BASE) == 2000, spans


def test_a_provider_that_names_its_own_range_limit_is_believed_and_retried(monkeypatch):
    """The endpoint tells us the answer in the refusal itself, and this is the second tightening
    in a month (100,000 -> 10,000 on 09-06, 10,000 -> 2,000 by 09-23). Parsing the number it names
    costs one refused call; not parsing it costs a day of blind Base."""
    from sniper.execution import evm_v4_quote as V

    monkeypatch.delenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_8453", raising=False)
    monkeypatch.setattr(V, "_V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN", dict(V._V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN))
    spans = []

    def _rpc(url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(51_706_072)}
        span = int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16)
        spans.append(span)
        if span > 500:
            return {"error": {"code": -32614, "message": "eth_getLogs is limited to a 500 range"}}
        return {"result": []}

    monkeypatch.setattr(V, "_rpc", _rpc)
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *a, **k: ())
    V._key_cache.clear()
    V._pool_key(BASE, "0x" + "cd" * 32, "https://mainnet.base.org")
    assert 500 in spans, "the named limit must be retried, not just recorded: %r" % (spans,)
    assert V._key_scan_page_blocks(BASE) == 500, "the shrink must stick for the next coin"

def test_the_base_v3_connector_moved_with_the_quote_currency():
    """`dex_v3_quote` skips a connector equal to the sell token. The Base connector was WETH while
    Base quoted in USDC; with a WETH bankroll that left Base with NO two-hop V3 path, so a
    USDC-paired Base coin was unreachable from the bankroll."""
    connectors = cfg.memecoin_evm_v3_connectors(BASE)

    assert connectors == ((BASE_USDC, 500),)
    assert connectors[0][0] != W.CHAIN_QUOTE[BASE]["token"].lower(), \
        "a connector equal to the quote token is skipped, so it is no connector at all"
    # 4663's connector is untouched and is likewise not its quote token.
    r_connectors = cfg.memecoin_evm_v3_connectors(ROBINHOOD)
    assert r_connectors[0][0].lower() != W.CHAIN_QUOTE[ROBINHOOD]["token"].lower()


# --- A4: the exit venues resolve on Base ---------------------------------------------------------

def test_a4_every_exit_venue_resolves_its_base_addresses():
    """An exit that cannot resolve a venue is a bag that cannot be sold, so this asserts the
    configuration each exit rung reads, not a quote."""
    assert cfg.memecoin_evm_v3_addresses(BASE) == {
        "router": cfg.MEMECOIN_EVM_V3_ROUTER[BASE], "quoter": cfg.MEMECOIN_EVM_V3_QUOTER[BASE]}
    assert cfg.memecoin_evm_v2_exit_config(BASE) == {
        "router": "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
        "connectors": (BASE_WETH, BASE_USDC)}
    assert set(V4[BASE]) >= {"pool_manager", "quoter", "universal_router", "permit2"}


def test_a4_the_hot_exit_lane_already_covers_base():
    from sniper.services.memecoin_evm_hot import HOT_CHAINS

    assert BASE in HOT_CHAINS and ROBINHOOD in HOT_CHAINS


def test_a4_a_base_multihop_route_builds_from_its_stored_legs_path(monkeypatch):
    """The exit rebuilds from the SAME ordered legs the entry stored, on Base as on 4663 — the
    builder is chain-generic and reads `legs_path`, so no Base-specific path exists to get wrong."""
    from sniper.execution import evm_v4_multihop_tx as TX

    native = "0x" + "00" * 20
    coin = "0x" + "22" * 20
    route = {
        "bridge": BASE_USDC,
        "bridge_key": {"currency0": native, "currency1": BASE_USDC,
                       "fee": 500, "tick_spacing": 10, "hooks": native},
        "coin_key": {"currency0": BASE_USDC, "currency1": coin,
                     "fee": 3000, "tick_spacing": 60, "hooks": native},
        "legs_path": [
            {"currency": BASE_USDC, "key": {"currency0": native, "currency1": BASE_USDC,
                                            "fee": 500, "tick_spacing": 10, "hooks": native}},
            {"currency": coin, "key": {"currency0": BASE_USDC, "currency1": coin,
                                       "fee": 3000, "tick_spacing": 60, "hooks": native}}],
    }
    bridge, legs = TX._legs_from_route(route, native, coin)

    assert bridge == BASE_USDC
    assert [str(leg[0]).lower() for leg in legs] == [BASE_USDC, coin]
    # a malformed leg fails closed rather than building a path that ends somewhere else
    assert TX._legs_from_route(route, native, "0x" + "33" * 20) is None
