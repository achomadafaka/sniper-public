"""Canonical chain vocabulary (spec 2026-07-30). Chains are never pooled."""
import pytest

from sniper.services.memecoin_chains import (
    CHAINS,
    chain_label,
    funded_chain_keys,
    network_ids_for,
    resolve_chain,
)


def test_every_alias_resolves_to_one_canonical_key():
    for value in ("solana", "SOLANA", " sol "):
        assert resolve_chain(value) == "solana"
    for value in ("4663", "robin", "Robinhood"):
        assert resolve_chain(value) == "robin"
    for value in ("56", "bsc"):
        assert resolve_chain(value) == "bsc"
    for value in ("8453", "base"):
        assert resolve_chain(value) == "base"
    for value in ("1", "eth", "ethereum"):
        assert resolve_chain(value) == "eth"


def test_there_is_no_all_scope():
    """'all' must not resolve - a pooled scope is exactly what this design removes."""
    assert resolve_chain("all") is None
    assert "all" not in {c["key"] for c in CHAINS}


def test_unresolved_is_its_own_bucket_not_a_chain():
    assert resolve_chain("evm-unknown") == "unresolved"
    assert next(c for c in CHAINS if c["key"] == "unresolved")["funded"] is False


def test_network_ids_are_disjoint_across_chains():
    seen = set()
    for chain in CHAINS:
        ids = set(network_ids_for(chain["key"]))
        assert not (ids & seen), f"{chain['key']} shares a network id with another chain"
        seen |= ids


def test_unknown_value_resolves_to_none():
    assert resolve_chain("dogecoin") is None
    assert resolve_chain(None) is None
    assert resolve_chain("") is None
    with pytest.raises(KeyError):
        network_ids_for("dogecoin")


def test_funded_keys_match_the_evm_allowlist_plus_solana():
    """MEMECOIN_EVM_ALLOWLIST=8453,4663 plus Solana. BSC and ETH are research-only."""
    assert funded_chain_keys() == ("solana", "base", "robin")


def test_labels_are_stable():
    assert chain_label("robin") == "Robinhood"
    assert chain_label("nonsense") == "nonsense"


def test_the_allowlist_is_authoritative_not_the_capability_flag(monkeypatch):
    """A chain with a funded implementation is armable ONLY when its id is in the allowlist.

    Chain 56 shipped its full funded implementation on 2026-09-06 while `CHAINS[...]["funded"]`
    was still False, so `set_wallet_chain_armed` refused every BSC arm with `chain_not_funded`
    although every other layer was ready. Two hand-synced sources; nothing kept them equal.
    """
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663"}, raising=False)
    assert funded_chain_keys() == ("solana", "base", "robin")

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663", "56"}, raising=False)
    assert funded_chain_keys() == ("solana", "base", "robin", "bsc")


def test_a_chain_without_a_funded_implementation_cannot_be_opened_by_the_allowlist(monkeypatch):
    """The capability flag still gates: an allowlist entry cannot arm a chain whose `funded`
    capability is False. Ethereum became funded-capable on 2026-09-30 (eth-mainnet spec), so the
    flag is turned off on its entry here to keep testing the gate itself."""
    from sniper.services import memecoin_chains as C
    from sniper.settings import memecoin as cfg

    monkeypatch.setitem(C._BY_KEY["eth"], "funded", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663", "1"}, raising=False)
    assert "eth" not in funded_chain_keys()
    assert "unresolved" not in funded_chain_keys()


def test_solana_is_funded_without_an_evm_allowlist_entry(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", set(), raising=False)
    assert funded_chain_keys() == ("solana",)


def test_arc_resolves_and_stays_inert_on_the_default_allowlist():
    # Arc (Circle, chain_id 5042, mainnet 2026-09-16). Registered as a capability so it can be
    # opened by ONE edit (MEMECOIN_EVM_ALLOWLIST); the default allowlist (8453,4663) does not
    # include it, so it must not appear in funded_chain_keys() until an operator adds it.
    assert resolve_chain("5042") == "arc"
    assert resolve_chain("Arc") == "arc"
    assert chain_label("arc") == "Arc"
    assert "arc" not in funded_chain_keys()


def test_arc_becomes_funded_only_once_its_id_is_in_the_allowlist(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663", "5042"}, raising=False)
    assert "arc" in funded_chain_keys()
