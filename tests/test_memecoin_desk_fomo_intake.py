"""Offline checks for the public, partial Fomo discovery-row boundary."""
from copy import deepcopy
from importlib import import_module

import pytest


SOL = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"
EVM = "0xABCdef0000000000000000000000000000000002"


def adapter():
    return import_module("sniper.feeds.fomo_intake")


def row(chain="solana", mint=SOL, chain_id="1399811149"):
    return {
        "row_id": f"thesis-trigger-default-thesis-{mint}:{chain_id}",
        "href": f"/tokens/{chain}/{mint}",
        "text": "TOKEN\n$0\n0 holders",
        "cells": ["TOKEN", "$0", "0"],
    }


def test_observed_solana_row_preserves_case_raw_zero_and_partial_coverage():
    item = row()
    before = deepcopy(item)
    assert adapter().parse_dom_rows([item]) == [{
        "network_id": "solana", "chain_raw": "solana", "mint": SOL,
        "venue": None,
        "raw": {"transport": "dom", "coverage": "partial", "item": before},
    }]
    assert item == before


@pytest.mark.parametrize("chain,chain_id", [
    ("base", "8453"), ("ethereum", "1"), ("bsc", "56"),
    ("robinhood", "4663"), ("avalanche", "43114"),
])
def test_explicit_evm_chain_normalizes_mint_and_accepts_same_origin_url(chain, chain_id):
    item = row(chain, EVM, chain_id)
    item["href"] = "https://fomo.family" + item["href"]
    result = adapter().parse_dom_rows([item])[0]
    assert (result["network_id"], result["mint"]) == (chain_id, EVM.lower())
    assert result["raw"]["item"] == item


def test_missing_row_id_does_not_create_missing_public_fields():
    item = {"href": f"/tokens/solana/{SOL}"}
    result = adapter().parse_dom_rows([item])[0]
    assert result["mint"] == SOL
    assert result["raw"]["item"] == item
    assert "text" not in result["raw"]["item"]
    assert "price" not in result


def test_unknown_explicit_chain_keeps_raw_identity_without_authority():
    item = {"href": "/tokens/future-chain/UnknownMintCase"}
    assert adapter().parse_dom_rows([item]) == [{
        "network_id": None, "chain_raw": "future-chain", "mint": "UnknownMintCase",
        "venue": None,
        "raw": {"transport": "dom", "coverage": "partial", "item": item},
    }]


@pytest.mark.parametrize("chain_id", ["777", "8453", "1399811149"])
def test_unknown_slug_with_numeric_row_id_stays_observable_without_authority(chain_id):
    item = row("unmapped", "A1b2C3", chain_id)
    assert adapter().parse_dom_rows([item]) == [{
        "network_id": None, "chain_raw": "unmapped", "mint": "A1b2C3", "venue": None,
        "raw": {"transport": "dom", "coverage": "partial", "item": item},
    }]


@pytest.mark.parametrize("href", [
    f"https://evil.example/tokens/solana/{SOL}",
    f"https://fomo.family.evil.example/tokens/solana/{SOL}",
    f"https://user@fomo.family/tokens/solana/{SOL}",
    f"https://fomo.family:444/tokens/solana/{SOL}",
    f"http://fomo.family/tokens/solana/{SOL}",
    f"//fomo.family/tokens/solana/{SOL}",
    f"/trade/solana/{SOL}", f"/tokens/solana/{SOL}/buy",
    f"/tokens/solana/{SOL}?action=buy", f"/tokens/solana/{SOL}#buy",
    f"/tokens/solana/{SOL}\n", "/tokens/solana/", "/tokens//mint",
    "/tokens/future/..", "/tokens/future/%2ftrade",
])
def test_noncanonical_or_action_routes_are_malformed(href):
    assert adapter().parse_dom_rows([{"href": href}]) == [None]


@pytest.mark.parametrize("item", [
    None, [], "not a row", {}, {"href": None}, {"href": 0},
    {"href": f"/tokens/solana/{EVM}"},
    {"href": f"/tokens/base/{SOL}"},
    {"href": "/tokens/solana/not-an-address"},
    {"href": "/tokens/base/0x123"},
    {"href": f"/tokens/solana/{SOL}", "row_id": "bad"},
    {"href": f"/tokens/solana/{SOL}", "row_id": 0},
    {"href": f"/tokens/solana/{SOL}",
     "row_id": f"thesis-trigger-default-thesis-{SOL}:8453"},
    {"href": f"/tokens/solana/{SOL}",
     "row_id": "thesis-trigger-default-thesis-" + SOL.lower() + ":1399811149"},
    {"href": f"/tokens/base/{EVM}",
     "row_id": "thesis-trigger-default-thesis-0x" + "1" * 40 + ":8453"},
    {"href": "/tokens/unmapped/A1b2C3",
     "row_id": "thesis-trigger-default-thesis-WrongMint:777"},
])
def test_malformed_rows_keep_their_slot_instead_of_becoming_empty_success(item):
    parsed = adapter().parse_dom_rows([item, row()])
    assert parsed[0] is None
    assert parsed[1]["mint"] == SOL


def test_evm_row_id_case_does_not_create_false_identity_conflict():
    item = row("base", EVM, "8453")
    item["row_id"] = item["row_id"].lower()
    assert adapter().parse_dom_rows([item])[0]["mint"] == EVM.lower()


@pytest.mark.parametrize("rows", [None, {}, "", (), 0])
def test_nonlist_outer_shape_is_an_error(rows):
    with pytest.raises(ValueError, match="list"):
        adapter().parse_dom_rows(rows)


def test_parser_allows_empty_list_without_claiming_a_verified_empty_page():
    assert adapter().parse_dom_rows([]) == []


@pytest.mark.parametrize("status,reason", [
    (401, "login_expired"), (403, "http_403"), (429, "http_429"),
])
def test_http_refusals_precede_page_content(status, reason):
    assert adapter().halt_reason("https://fomo.family/discover", "captcha", True, status) == reason


@pytest.mark.parametrize("url", [
    "https://other.invalid/discover", "http://fomo.family/discover",
    "https://fomo.family:444/discover", "https://user@fomo.family/discover",
    "https://[invalid/discover", "/discover", "",
])
def test_unexpected_origin_halts_even_with_positive_authentication(url):
    assert adapter().halt_reason(url, "En tendencia", True) == "unexpected_origin"


@pytest.mark.parametrize("url,text", [
    ("https://fomo.family/cdn-cgi/challenge-platform/", ""),
    ("https://fomo.family/challenge", ""),
    ("https://fomo.family/discover", "Just a moment..."),
    ("https://fomo.family/discover", "Verify you are human"),
    ("https://fomo.family/discover", "Checking your browser before accessing"),
    ("https://fomo.family/discover", "Verifica que eres humano"),
    ("https://fomo.family/discover", "Completa el CAPTCHA"),
])
def test_challenges_are_named_even_when_authentication_is_unknown(url, text):
    assert adapter().halt_reason(url, text, None) == "challenge"


@pytest.mark.parametrize("url,text,authenticated", [
    ("https://fomo.family/login", "", True),
    ("https://fomo.family/signin", "", True),
    ("https://fomo.family/discover", "Iniciar sesión", True),
    ("https://fomo.family/discover", "Inicia sesión para continuar", True),
    ("https://fomo.family/discover", "Sign in to Fomo", True),
    ("https://fomo.family/discover", "En tendencia", False),
    ("https://fomo.family/discover", "Iniciar sesión", None),
])
def test_explicit_login_evidence_is_login_required(url, text, authenticated):
    assert adapter().halt_reason(url, text, authenticated) == "login_required"


@pytest.mark.parametrize("authenticated", [None, 0, 1, "true", "false", ""])
def test_unknown_authentication_is_auth_unproven_without_truthiness(authenticated):
    assert adapter().halt_reason(
        "https://fomo.family/discover", "En tendencia", authenticated,
    ) == "auth_unproven"


def test_challenge_takes_priority_over_positive_login_evidence():
    assert adapter().halt_reason(
        "https://fomo.family/login", "Verify you are human. Iniciar sesión", False,
    ) == "challenge"


def test_authenticated_discovery_and_nonrefusal_status_are_not_errors():
    module = adapter()
    assert list(module.LIST_LABELS.values()) == [
        "En tendencia", "Blue chips", "Con más holders", "Graduados", "Bonding",
    ]
    assert list(module.LIST_LABELS) == [
        "trending24h", "blue_chips", "most_holders", "graduated", "bonding",
    ]
    text = "En tendencia Blue chips Con más holders Graduados Bonding Cerrar sesión"
    assert module.halt_reason("https://fomo.family/discover", text, True, 200) is None
