import pytest
from sniper.analysis.memecoin_x import normalize as nz


def test_evm_address_lowercased():
    a = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
    assert nz.normalize_address(a, "8453") == a.lower()


def test_solana_address_case_preserved():
    a = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"
    assert nz.normalize_address(a, "solana") == a  # base58 case preserved


def test_handle_identity_is_case_insensitive():
    assert nz.normalize_handle(" @_OHT_ ") == "_oht_"


def test_dex_pair_url_is_context_not_a_token_mint():
    pair = "0xABCdef0000000000000000000000000000000001"
    ms = nz.extract_coin_mentions("gm", [f"https://dexscreener.com/base/{pair}"])
    assert not [m for m in ms if m["mint"]]


def test_dex_network_context_plus_exact_ca_is_eligible():
    ca = "0xABCdef0000000000000000000000000000000002"
    urls = ["https://dexscreener.com/robinhood/0x1111111111111111111111111111111111111111"]
    ms = nz.extract_coin_mentions(f"CA: {ca}", urls)
    m = [x for x in ms if x["extraction_method"] == "contract_text"][0]
    assert m["network_id"] == "4663"
    assert m["mint"] == ca.lower()
    assert m["eligible"] is True and m["ambiguous"] is False


def test_bare_evm_contract_in_text_is_ambiguous_network():
    # a raw 0x contract with no chain context: network unknown -> not eligible
    ms = nz.extract_coin_mentions(
        "aping 0xABCdef0000000000000000000000000000000002", [])
    m = [x for x in ms if x["extraction_method"] == "contract_text"][0]
    assert m["mint"] == "0xabcdef0000000000000000000000000000000002"
    assert m["ambiguous"] is True and m["eligible"] is False


@pytest.mark.parametrize("raw", [
    "0x" + "a" * 41,
    "xx0x" + "a" * 40 + "yy",
    "0x" + "a" * 40 + "g",
])
def test_rejects_non_exact_evm_addresses(raw):
    mentions = nz.extract_coin_mentions(f"Robinhood Chain CA: {raw}", [])
    assert not [m for m in mentions if m["mint"]]


def test_solana_contract_in_text_is_eligible():
    ms = nz.extract_coin_mentions(
        "CA: 62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump", [])
    m = [x for x in ms if x["extraction_method"] == "contract_text"][0]
    assert m["network_id"] == "solana"
    assert m["eligible"] is True


@pytest.mark.parametrize("forbidden", ["I", "O", "l"])
def test_rejects_forbidden_base58_characters(forbidden):
    mentions = nz.extract_coin_mentions(f"CA: {forbidden * 32}", [])
    assert not [m for m in mentions if m["mint"]]


def test_arbitrary_base58_prose_is_not_a_contract():
    ca = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"
    assert not [m for m in nz.extract_coin_mentions(f"wallet {ca}", []) if m["mint"]]


def test_launchpad_url_preserves_solana_address_case():
    ca = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"
    ms = nz.extract_coin_mentions("gm", [f"https://pump.fun/coin/{ca}"])
    assert any(m["mint"] == ca and m["network_id"] == "solana" for m in ms)


def test_hood_and_noxa_urls_supply_robinhood_network_context():
    ca = "0xABCdef0000000000000000000000000000000003"
    for url in ("https://hood.fun/token/foo", "https://fun.noxa.fi/robinhood/foo"):
        m = [x for x in nz.extract_coin_mentions(f"CA: {ca}", [url]) if x["mint"]][0]
        assert m["network_id"] == "4663" and m["eligible"] is True


def test_conflicting_network_contexts_do_not_become_eligible():
    ca = "0xABCdef0000000000000000000000000000000004"
    urls = [
        "https://dexscreener.com/base/pair1",
        "https://dexscreener.com/robinhood/pair2",
    ]
    m = [x for x in nz.extract_coin_mentions(f"CA: {ca}", urls) if x["mint"]][0]
    assert m["network_id"] is None and m["ambiguous"] is True
    assert m["eligible"] is False


def test_ticker_only_is_hypothesis_not_eligible():
    ms = nz.extract_coin_mentions("send $WIF", [])
    m = [x for x in ms if x["extraction_method"] == "ticker_only"][0]
    assert m["ticker"] == "WIF" and m["mint"] is None
    assert m["eligible"] is False


def test_network_never_inferred_from_absence():
    # no CA, no url, just prose -> no eligible mention at all
    ms = nz.extract_coin_mentions("wagmi frens", [])
    assert all(x["eligible"] is False for x in ms)


def test_normalize_direct_card_with_solana_ca():
    card = {
        "canonical_post_id": "1800000000000000001",
        "url": "https://x.com/zinceth/status/1800000000000000001",
        "author_handle": "zinceth", "actor_handle": "zinceth", "relationship": "direct",
        "full_text": "CA: 62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump", "text_complete": True,
        "posted_at_raw": "2026-07-13T00:00:00.000Z", "urls": [],
        "referenced_author": None, "referenced_post_id": None,
    }
    out = nz.normalize_card(card)
    assert out["eligible_for_forward"] is True
    assert out["post"]["canonical_post_id"] == "1800000000000000001"
    assert out["relationship"]["relationship"] == "direct"
    assert out["relationship"]["actor_handle"] == "zinceth"
    assert any(m["eligible"] and m["network_id"] == "solana" for m in out["mentions"])


def test_incomplete_robinhood_card_retains_ineligible_exact_mention():
    ca = "0xABCdef0000000000000000000000000000000005"
    card = {
        "canonical_post_id": "1800000000000000010",
        "url": "https://x.com/hooddotfun/status/1800000000000000010",
        "author_handle": "hooddotfun", "actor_handle": "hooddotfun",
        "relationship": "direct", "full_text": f"Robinhood Chain CA: {ca}",
        "text_complete": False, "posted_at_raw": None, "urls": [],
        "referenced_author": None, "referenced_post_id": None,
    }
    out = nz.normalize_card(card)
    mention = [m for m in out["mentions"] if m["mint"]][0]
    assert out["eligible_for_forward"] is True  # retain the immutable card observation
    assert mention["network_id"] == "4663"
    assert mention["mint"] == ca.lower()
    assert mention["eligible"] is False


def test_card_without_post_id_is_diagnostic_only():
    card = {"canonical_post_id": None, "url": "", "author_handle": "x",
            "actor_handle": "x", "relationship": "direct", "full_text": "hi",
            "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": None, "referenced_post_id": None}
    out = nz.normalize_card(card)
    assert out["eligible_for_forward"] is False
    assert out["post"] is None


def test_repost_relationship_carries_referenced_author():
    card = {"canonical_post_id": "1799999999999999999",
            "url": "https://x.com/blknoiz06/status/1799999999999999999",
            "author_handle": "blknoiz06", "actor_handle": "_OHT_", "relationship": "repost",
            "full_text": "banger", "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": "blknoiz06", "referenced_post_id": "1799999999999999999"}
    out = nz.normalize_card(card)
    assert out["relationship"]["relationship"] == "repost"
    assert out["relationship"]["referenced_author"] == "blknoiz06"
    assert out["post"]["author_handle"] == "blknoiz06"  # post author is the original, not the amplifier


@pytest.mark.parametrize("relationship", ["quote", "reply"])
def test_quote_and_reply_attribute_the_source_post(relationship):
    card = {"canonical_post_id": "1799999999999999999",
            "url": "https://x.com/blknoiz06/status/1799999999999999999",
            "author_handle": "@BlkNoiz06", "actor_handle": "@_OHT_",
            "relationship": relationship, "full_text": "source text",
            "text_complete": True, "posted_at_raw": "2026-07-13T00:00:00Z", "urls": [],
            "referenced_author": "blknoiz06", "referenced_post_id": "1799999999999999999"}
    out = nz.normalize_card(card)
    assert out["post"]["author_handle"] == "blknoiz06"
    assert out["relationship"]["actor_handle"] == "_oht_"
    assert out["post"]["posted_at"] == "2026-07-13T00:00:00+00:00"


def test_invalid_relationship_is_diagnostic_only():
    card = {"canonical_post_id": "1", "url": "u", "author_handle": "a",
            "actor_handle": "b", "relationship": "like", "full_text": "x",
            "text_complete": True, "posted_at_raw": "not-a-time", "urls": []}
    out = nz.normalize_card(card)
    assert out["eligible_for_forward"] is False and out["post"] is None
    assert out["raw_card"]["relationship"] == "like"


def test_direct_requires_actor_author_match():
    card = {"canonical_post_id": "1", "url": "u", "author_handle": "upstream",
            "actor_handle": "tier1seed", "relationship": "direct", "full_text": "x",
            "text_complete": True, "posted_at_raw": None, "urls": []}
    out = nz.normalize_card(card)
    assert out["eligible_for_forward"] is False
    assert out["post"] is None
