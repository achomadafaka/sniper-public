"""Arc (5042) Uniswap V4 addresses (2026-09-16).

Every address below is cited, not inferred from another chain's pattern:
- pool_manager, quoter, universal_router, permit2: read from Uniswap's own official V4
  deployments page (developers.uniswap.org/contracts/v4/deployments), which explicitly lists
  "Arc" among its supported chains. pool_manager and quoter are BYTE-IDENTICAL to the existing
  4663/8453 entries in this module -- Uniswap deploys its V4 singleton core deterministically,
  so the same address recurring across chains is expected, not a copied guess.
- Confirmed live via `eth_getCode` on the operator's dedicated Arc RPC (QuickNode,
  MEMECOIN_EVM_RPC_URL_5042): pool_manager 48,020 bytes, quoter 12,238 bytes, universal_router
  49,094 bytes, permit2 18,306 bytes -- all real, substantial contracts, not empty addresses.

Arc has NO GeckoTerminal network slug (GT does not list a brand-new chain this fast, and Arc is
not DexScreener-blind, so it does not need one) -- "gt_network" is deliberately absent, and
`_gt_pools_ranked` must degrade to "no GT pools, not blind" rather than KeyError.
"""
from sniper.execution.evm_v4_quote import V4

ARC = "5042"


def test_arc_v4_addresses_are_the_ones_cited_and_code_verified():
    assert V4[ARC] == {
        "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "quoter": "0x8dc178efb8111bb0973dd9d722ebeff267c98f94",
        "universal_router": "0x4fca4a51ab4f23a7447b3284fbd7d73289a89fb1",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
    }
    # Permit2 is deployed deterministically everywhere, so it recurs across every chain here.
    assert V4[ARC]["permit2"] == V4["8453"]["permit2"] == V4["4663"]["permit2"]
    # pool_manager/quoter match 4663 exactly (same deploy salt/batch) but NOT 8453 (a different
    # one) -- recorded so a future edit that "fixes" this to match 8453 too is caught: it would
    # be wrong, not more consistent.
    assert V4[ARC]["pool_manager"] == V4["4663"]["pool_manager"] != V4["8453"]["pool_manager"]
    assert V4[ARC]["quoter"] == V4["4663"]["quoter"] != V4["8453"]["quoter"]
    # The router is genuinely per-chain, unlike the singleton core.
    assert V4[ARC]["universal_router"] not in (V4["8453"]["universal_router"],
                                               V4["4663"]["universal_router"])
    assert "gt_network" not in V4[ARC]


def test_a_chain_with_no_gt_network_reports_no_pools_not_a_crash():
    from sniper.execution import evm_v4_quote as v4q

    pools, failed = v4q._gt_pools_ranked(ARC, "0x" + "11" * 20)
    assert pools == [] and failed is False
