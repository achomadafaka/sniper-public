"""BSC (chain 56) funded parity — and 4663, Solana and Base do not move.

Spec: docs/superpowers/specs/2026-09-06-memecoin-bsc-56-funded-parity-design.md, sections 6, 6b, 6c.

THE VENUE IS DECIDED BY THE CENSUS, not by analogy with Base. Across the 20 most recent armed-wallet
buys on 56, PancakeSwap is the deepest venue for 20 of 20 — $5.77M across 73 pools — and the version
split is $4.89M V2 (85%) against $884K V3 (15%). So V2 is the PRIMARY rung here, the opposite of
Base where V2 is $42 across one pool. Both are Uniswap forks with CREATE2-deterministic factories,
so discovery is one `eth_call` per candidate and needs ZERO `eth_getLogs` — which matters because
the chain owner refuses getLogs entirely and publicnode reaches only ~1.12 h.

Offline. Real objects, fake RPC, no network.
"""
import pytest

from sniper.execution import evm_wallet as W
from sniper.execution.evm_v4_quote import V4
from sniper.services import memecoin_quotes as Q
from sniper.settings import memecoin as cfg

BSC = "56"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
OTHERS = ("4663", "8453", "solana")


# --- quote currency ---------------------------------------------------------------------------------

def test_bsc_quotes_in_wbnb_not_usdc():
    """WBNB holds 67.9% of census liquidity against USDT's 18.9%, nothing else above 2.5%."""
    quote = W.CHAIN_QUOTE[BSC]
    assert quote["token"].lower() == WBNB          # checksummed in the map, as 4663's is
    assert quote["decimals"] == 18 and quote["kind"] == "eth"
    assert W.QUOTE_SYMBOL[BSC] == "WBNB"
    assert BSC in W.native_quote_chains()


def test_the_bsc_stable_leg_is_usdt_not_usdc():
    """BSC's dominant stable is Binance-Peg BSC-USD. Its USDC is a minor pair and must not be the
    bridge leg just because `CHAIN_USDC` has an entry for the chain."""
    assert W.CHAIN_STABLE[BSC].lower() == USDT
    assert W.CHAIN_USDC[BSC].lower() != USDT


def test_a_150_dollar_clip_sizes_to_wbnb_atomic_from_the_bnb_usd_read():
    from sniper.execution.evm_real_seams import usd_to_quote_atomic

    assert usd_to_quote_atomic(BSC, 150.0, price_fn=lambda *_a, **_k: 600.0) == 25 * 10 ** 16
    assert usd_to_quote_atomic(BSC, 150.0, price_fn=lambda *_a, **_k: None) is None


# --- venues: PancakeSwap V2 primary, V3 secondary, no V4, no launchpad --------------------------------

def test_the_pancakeswap_addresses_are_the_ones_verified_on_chain():
    """Every one cross-confirmed by a non-empty `eth_getCode` on https://bsc-dataseed.bnbchain.org
    and every factory DERIVED via `factory()` rather than copied: V2Router02 -> 0xca143ce3…,
    V3SwapRouter -> 0x0bfbcf9f…, V3QuoterV2 -> 0x0bfbcf9f… (the same V3 factory, cross-checked)."""
    from sniper.settings.memecoin_v6_paper import (
        MEMECOIN_V6_EVM_FACTORIES_BY_CHAIN, MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN)

    # `protocol` joined this dict on 2026-09-08: chain 56's router is PancakeSwap's ORIGINAL
    # SwapRouter, whose exactInputSingle/exactInput carry a deadline INSIDE the params struct.
    # The addresses were verified on chain and were correct all along; only the calldata was wrong,
    # which is why the first funded BSC swap reverted with EMPTY data at ~25.6k gas.
    assert cfg.memecoin_evm_v3_addresses(BSC) == {
        "router": "0x1b81D678ffb9C0263b24A97847620C99d213eB14",
        "quoter": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
        "protocol": "pancake_v3"}
    assert MEMECOIN_V6_EVM_V2_ROUTER_BY_CHAIN[BSC] == \
        "0x10ED43C718714eb63d5aA57B78B54704E256024E"
    assert MEMECOIN_V6_EVM_FACTORIES_BY_CHAIN[BSC] == {
        "v3_factory": "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865",
        "v2_factory": "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"}


def test_v2_is_configured_as_a_first_class_venue_on_bsc():
    """The opposite of Base, where V2 is $42 across one pool. Here it is 85% of the liquidity."""
    assert cfg.memecoin_evm_v2_exit_config(BSC) == {
        "router": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
        "connectors": (WBNB, USDT)}


def test_the_v3_fee_tiers_lead_with_one_percent():
    """MEASURED: `getPool(USDT, BEN, ...)` returns ZERO at 100, 500 and 2500 and a real pool only at
    10000, whose address matches DexScreener's reported pair. The low stable tiers are the wrong
    shape for these coins, so 1% leads — and the others follow rather than being dropped, because a
    usually-empty tier costs only a call once the deep one has answered."""
    assert cfg.memecoin_evm_v3_fee_tiers(BSC)[0] == 10000
    assert set(cfg.memecoin_evm_v3_fee_tiers(BSC)) >= {10000, 2500, 500, 100}


def test_the_v3_connector_is_the_non_quote_currency():
    """`dex_v3_quote` skips a connector equal to the sell token, so with a WBNB bankroll it is USDT.
    The same trap that left Base with no two-hop path when its quote flipped."""
    connectors = cfg.memecoin_evm_v3_connectors(BSC)
    assert connectors == ((USDT, 100),)
    assert connectors[0][0] != W.CHAIN_QUOTE[BSC]["token"]


def test_bsc_has_no_v4_rung_and_no_launchpad_rung():
    """Spec decision: no V4 deployment is configured and the bonding curve is not material — the
    census found four.meme/flapsh on 3 of 20 coins, every one already graduated with $0 curve
    liquidity left. Neither rung may be built on log-scan discovery this chain cannot serve."""
    assert BSC not in V4
    assert BSC not in cfg.MEMECOIN_EVM_V4_LAUNCHPADS
    assert BSC not in cfg.MEMECOIN_EVM_MULTIHOP_SECOND_BRIDGES


# --- plumbing --------------------------------------------------------------------------------------

def test_the_bsc_rpc_pool_is_the_dataseed_plus_publicnode():
    pool = cfg._evm_rpc_pool({})
    assert pool[BSC] == ("https://bsc-dataseed.bnbchain.org", "https://bsc-rpc.publicnode.com")


def test_the_endpoint_registry_carries_the_0x38_identity():
    from sniper.execution.free_endpoint_registry import free_endpoints

    entries = free_endpoints(BSC)
    assert {e.expected_identity for e in entries} == {"0x38"}
    assert {e.provider_id for e in entries} == {"56:bsc_dataseed", "56:publicnode"}


def test_bsc_is_on_the_rung_ladder_but_not_the_dexscreener_blind_list():
    assert BSC in cfg.MEMECOIN_EVM_RUNG_LADDER_CHAINS
    assert BSC not in cfg.MEMECOIN_DEX_BLIND_CHAINS


def test_the_quote_identity_accepts_56():
    assert Q._validated_identity(BSC, "0x" + "Ab" * 20)[0] == BSC
    with pytest.raises(Q.IdentityInvalid):
        Q._validated_identity(BSC, "not-hex")


def test_the_hot_exit_lane_covers_bsc():
    from sniper.services.memecoin_evm_hot import HOT_CHAINS

    assert BSC in HOT_CHAINS


# --- the build blocker: token safety on a chain of minimal proxies -------------------------------------

def test_the_minimal_proxy_discovery_path_exists_and_is_reachable():
    """BUILD BLOCKER, not polish. MEASURED 2026-09-06: SIX OF EIGHT recent chain-56 coins carry
    exactly 45 bytes of code with the `363d3d` prefix — EIP-1167 minimal proxies. That is the shape
    that defeated storage-layout discovery on 4663 (OURO, 2026-09-04) and cost roughly half that
    chain's entries; on BSC it is the DOMINANT shape, not an exception.

    `_access_list_slots` is the only path that finds such a token's balance slot, and on 4663 only
    Alchemy served `eth_createAccessList`. Both BSC endpoints DO serve it: probed against two real
    chain-56 proxies and one normal token, each returned exactly one own-token storage slot on both
    the dataseed and publicnode.
    """
    from sniper.execution import evm_token_safety as S

    assert hasattr(S, "_access_list_slots")
    calls = []

    def http(method, url, headers=None, body=None, timeout=10):
        calls.append(body)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": {"accessList": [
            {"address": "0x" + "11" * 20, "storageKeys": ["0x" + "ab" * 32]},
            {"address": "0x" + "99" * 20, "storageKeys": ["0x" + "cd" * 32]},
        ]}}, {}

    slots = S._access_list_slots("0x" + "11" * 20, "0xdead", "https://rpc", http=http)

    assert slots == ("0x" + "ab" * 32,), "only the TOKEN's own slots, never the implementation's"
    assert '"eth_createAccessList"' in calls[0]
    assert '"gas"' in calls[0], "without gas the node reports insufficient funds, not a list"


def test_a_node_that_cannot_answer_leaves_the_scan_where_it_was():
    """Every failure returns () so the caller keeps its existing not-found behaviour — the method is
    not universally served and must never turn a missing answer into a wrong one."""
    from sniper.execution import evm_token_safety as S

    def boom(*_a, **_k):
        raise RuntimeError("method not supported")

    assert S._access_list_slots("0x" + "11" * 20, "0xdead", "https://rpc", http=boom) == ()
    assert S._access_list_slots("0x" + "11" * 20, "0xdead", None) == ()


# --- nothing else moves ------------------------------------------------------------------------------

def test_no_other_chain_moves():
    """This property has held across every parity release and is the one that keeps them safe."""
    assert W.CHAIN_QUOTE["4663"] == {
        "token": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", "decimals": 18, "kind": "eth",
        "usd_symbol": "ETH"}
    assert W.CHAIN_QUOTE["8453"] == {
        "token": "0x4200000000000000000000000000000000000006", "decimals": 18, "kind": "eth",
        "usd_symbol": "ETH"}
    # BSC quotes in WBNB and must be priced in BNB. It inherited the ether mark for two days.
    assert W.CHAIN_QUOTE["56"]["usd_symbol"] == "BNB"
    # chain 1 moved to WETH on 2026-09-30 (eth-mainnet spec); pinned in test_memecoin_eth_parity
    assert W.QUOTE_SYMBOL["4663"] == "WETH" and W.QUOTE_SYMBOL["8453"] == "WETH"
    assert cfg.memecoin_evm_v3_fee_tiers("4663") == cfg.MEMECOIN_EVM_V3_FEE_TIERS
    assert cfg.memecoin_evm_v3_fee_tiers("8453") == cfg.MEMECOIN_EVM_V3_FEE_TIERS
    assert cfg.MEMECOIN_EVM_V2_ROUTER["4663"] == "0x89e5DB8B5aA49aA85AC63f691524311AEB649eba"
    assert "4663" in V4 and "8453" in V4
    assert Q._validated_identity("solana", "So11111111111111111111111111111111111111112")[0] \
        == "solana"


def test_the_bsc_defaults_are_all_suffix_scoped():
    """A default that is not suffix-scoped is how one chain's tuning moves another's money."""
    import inspect

    src = inspect.getsource(cfg)
    for token in ("MEMECOIN_EVM_V3_QUOTER_56", "MEMECOIN_EVM_V3_ROUTER_56",
                  "MEMECOIN_EVM_V2_ROUTER_56", "MEMECOIN_EVM_V2_CONNECTORS_56",
                  "MEMECOIN_EVM_V3_FEE_TIERS_56", "MEMECOIN_EVM_V3_CONNECTOR_56"):
        assert token in src, token


def test_a_chain_with_no_v4_deployment_skips_the_rung_rather_than_failing_it(monkeypatch):
    """OBSERVED ON THE FIRST BSC CANARY. 56 is on the rung ladder for v2 and v3 but has no V4
    addresses, and the v4/v4_multihop rungs each attempted a read and reported `rpc_transport` — a
    BLIND reason. `classify_quote_miss` reports the blind rung first, so a coin with genuinely no
    route was attributed to a provider fault, and a real transport problem would have been
    indistinguishable from the noise. After the fix the same coin reports `no_route`.
    """
    from sniper.execution import evm_real_seams as RS

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", {BSC, "4663"}, raising=False)
    rungs = []
    ladder = RS.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=BSC, quote_token=WBNB, taker="0x" + "11" * 20,
        v2_quote_fn=lambda *a, **k: None, enabled=True,
        on_rung=lambda r: rungs.append(dict(r)))
    ladder(WBNB, "0x" + "22" * 20, 10 ** 15, slippage_bps=300)

    skipped = {r["rung"] for r in rungs if r.get("skipped")}
    assert {"v4", "v4_multihop"} <= skipped
    assert not any(r.get("blind_reason") for r in rungs), \
        "a chain without V4 must not report a provider fault for the rung it does not have"
    assert RS.classify_quote_miss(rungs)[0] == "no_route"


def test_a_chain_that_HAS_v4_still_runs_the_rung(monkeypatch):
    """The guard must be keyed on the deployment map, not on a chain literal."""
    from sniper.execution import evm_real_seams as RS

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RUNG_LADDER_CHAINS", {BSC, "4663"}, raising=False)
    rungs = []
    weth = W.CHAIN_QUOTE["4663"]["token"]
    ladder = RS.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=weth, taker="0x" + "11" * 20,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=lambda *a, **k: None, enabled=True,
        on_rung=lambda r: rungs.append(dict(r)))
    ladder(weth, "0x" + "22" * 20, 10 ** 15, slippage_bps=300)

    assert "v4" in {r["rung"] for r in rungs if not r.get("skipped")}
