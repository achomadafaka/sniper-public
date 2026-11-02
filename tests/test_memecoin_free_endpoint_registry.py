"""Task 3 — immutable official free-endpoint registry.

Pins the ONLY free public endpoints auto_free may use (chain-owner services, code-reviewed, never a
remote document or operator list) with URL-free provider labels and the chain identity each health
check must confirm. Verified live 2026-07-25: Base + Robinhood public answer eth_chainId; Base
Flashblocks answers eth_chainId; Solana mainnet-beta getGenesisHash returns the pinned genesis; the
Robinhood sequencer is submission-only (no read method) so it carries health_method='none'. Custody
and duplicate URLs are de-duplicated before provider construction.
"""
from sniper.execution import free_endpoint_registry as R


def _by_id(chain):
    return {e.provider_id: e for e in R.free_endpoints(chain)}


def test_solana_public_rpc_pins_official_url_and_genesis():
    e = _by_id("solana")["solana:public_rpc"]
    assert e.url == "https://api.mainnet-beta.solana.com"
    assert e.role == "public_rpc"
    assert e.expected_identity == R.SOLANA_MAINNET_GENESIS
    # full mainnet-beta genesis hash, verified live via getGenesisHash (not the abbreviated form)
    assert R.SOLANA_MAINNET_GENESIS == "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"


def test_base_endpoints_pin_official_urls_and_chain_identity():
    b = _by_id("8453")
    assert b["8453:base_standard"].url == "https://mainnet.base.org"
    assert b["8453:base_flashblocks"].url == "https://mainnet-preconf.base.org"
    assert b["8453:base_standard"].expected_identity == "0x2105"
    assert b["8453:base_flashblocks"].expected_identity == "0x2105"
    assert R.BASE_CHAIN_IDENTITY == "0x2105"
    assert all(e.health_method == "eth_chainId" for e in b.values())


def test_robinhood_public_and_sequencer_pin_official_urls():
    r = _by_id("4663")
    assert r["4663:public_rpc"].url == "https://rpc.mainnet.chain.robinhood.com"
    assert r["4663:sequencer"].url == "https://sequencer.mainnet.chain.robinhood.com"
    assert r["4663:public_rpc"].expected_identity == "0x1237"
    assert R.ROBINHOOD_CHAIN_IDENTITY == "0x1237"
    # public RPC is identity-checked; the sequencer is submission-only (no read method) -> 'none'
    assert r["4663:public_rpc"].health_method == "eth_chainId"
    assert r["4663:sequencer"].health_method == "none"
    assert r["4663:sequencer"].role == "sequencer"


def test_unknown_chain_has_no_free_endpoints():
    assert R.free_endpoints("999") == ()
    assert R.free_endpoints("polygon") == ()


def test_dedupe_removes_custody_and_duplicate_urls():
    entries = R.free_endpoints("8453")
    # custody equals the standard endpoint (with a trailing slash + case) -> standard dropped
    deduped = R.dedupe_endpoints("https://Mainnet.Base.org/", entries)
    ids = [e.provider_id for e in deduped]
    assert "8453:base_standard" not in ids
    assert "8453:base_flashblocks" in ids


def test_dedupe_drops_repeated_official_urls():
    e = R.free_endpoints("8453")[0]
    dup = R.dedupe_endpoints("https://custody.example/rpc", (e, e))
    assert len(dup) == 1


def test_labels_never_leak_urls():
    for chain in ("solana", "8453", "4663"):
        for e in R.free_endpoints(chain):
            assert "http" not in e.provider_id and "." not in e.provider_id.split(":")[-1]


def test_arc_owner_public_rpc_is_a_delivery_target():
    # 2026-09-16 BANK: the only send went to custody publicnode, which answered 200 and dropped it.
    a = _by_id("5042")
    assert a["5042:public_rpc"].url == "https://rpc.mainnet.arc.io"
    assert a["5042:public_rpc"].expected_identity == "0x13b2" == R.ARC_CHAIN_IDENTITY
    assert a["5042:public_rpc"].health_method == "eth_chainId"
