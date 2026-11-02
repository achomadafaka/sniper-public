"""Arc (5042) Uniswap V3 direct-swap addresses (2026-09-16).

Every address is cited from Uniswap Labs' own unified deployments feed
(docs.uniswap.org/deployments.json -> developers.uniswap.org/deployments.json), each record
tier="labs-supported", status="active" -- not inferred from another chain's pattern:

    v3 UniswapV3Factory Arc 5042 0xf0db7b58379503491d857dB50AC9ece64c653918
    v3 SwapRouter02     Arc 5042 0x53BF6B0684Ec7eF91e1387Da3D1a1769bC5A6F77
    v3 QuoterV2         Arc 5042 0x7DfD4F31be6814D2906BDE155c3e1B146EAc1468

Cross-checked THREE ways before use:
1. The Factory address matches the `factory()` call read directly off a real, live pool
   (ARGUS/USDC, $1.6M+ liquidity) on the operator's dedicated Arc RPC.
2. eth_getCode on all three: factory 24,535 bytes, router 24,497 bytes, quoter 8,273 bytes --
   real contracts, not stubs.
3. A live `quoteExactInputSingle` call on the QuoterV2 (1 USDC -> ARGUS, fee=10000, the pool's
   own confirmed fee tier) returned an implied price of $0.032235/ARGUS against DexScreener's
   contemporaneous $0.03195 -- 0.89% apart, well inside "a few %".
"""
from sniper.settings import memecoin as cfg

ARC = "5042"


def test_arc_v3_router_and_quoter_are_the_cited_addresses():
    assert cfg.MEMECOIN_EVM_V3_ROUTER[ARC] == "0x53BF6B0684Ec7eF91e1387Da3D1a1769bC5A6F77"
    assert cfg.MEMECOIN_EVM_V3_QUOTER[ARC] == "0x7DfD4F31be6814D2906BDE155c3e1B146EAc1468"


def test_arc_v3_addresses_resolve_through_the_same_function_every_chain_uses():
    assert cfg.memecoin_evm_v3_addresses(ARC) == {
        "router": cfg.MEMECOIN_EVM_V3_ROUTER[ARC], "quoter": cfg.MEMECOIN_EVM_V3_QUOTER[ARC]}
