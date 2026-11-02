"""A seconds-old mint must be readable on the funded Solana entry path.

With no commitment a Solana node answers at FINALIZED, ~32 slots (~9 s) behind the tip, and a mint
created seconds ago does not exist there: getTokenSupply answers JSON-RPC -32602 "could not find
account". On the entry path that read FAILED OPEN (2026-09-26, Lambda's sweep after 210ac150):
  - token_decimals -> entry_token_snapshot decimals None -> the chase cap skipped itself on the
    first buy of the freshest coins;
  - token_supply -> executable_mcap None -> the Solana mcap fallback fell through to assume-micro
    and the mcap ceiling did not apply to a mint too fresh for DexScreener.
The double below answers like the node: the error at the finalized default, the value at
`confirmed`.
"""
import json

import pytest

from sniper.execution import solana_real_seams as S

MINT = "FreshMint1111111111111111111111111111111pump"
OWNER = "OurWallet11111111111111111111111111111111111"


def _node(seen):
    def http(_method, _url, body=None, **_kw):
        request = json.loads(body)
        seen.append(request)
        method, params = request["method"], request["params"]
        confirmed = any(isinstance(p, dict) and p.get("commitment") == "confirmed" for p in params)
        if method == "getTokenAccountsByOwner":
            return 200, {"result": {"value": []}}, None          # a first buy: the wallet holds none
        if method == "getTokenSupply":
            if not confirmed:                                     # finalized: not born yet
                return 200, {"error": {"code": -32602,
                                       "message": "Invalid param: could not find account"}}, None
            return 200, {"result": {"value": {"amount": "1000000000000000", "decimals": 6}}}, None
        return 200, {"result": {}}, None
    return http


def test_a_first_buy_of_a_seconds_old_mint_resolves_its_decimals():
    seen = []
    snapshot = S.entry_token_snapshot(OWNER, MINT, "http://rpc.invalid", http=_node(seen))
    assert (snapshot.atomic, snapshot.decimals) == (0, 6)
    supply_reads = [r for r in seen if r["method"] == "getTokenSupply"]
    assert supply_reads and supply_reads[0]["params"][1] == {"commitment": "confirmed"}


def test_a_seconds_old_mint_gets_an_executable_mcap(monkeypatch):
    monkeypatch.setattr(S, "_SUPPLY_CACHE", {})
    seen = []
    assert S.token_supply(MINT, "http://rpc.invalid", http=_node(seen)) == (1_000_000_000.0, 6)
    monkeypatch.setattr(S, "_SUPPLY_CACHE", {})
    # $100 buys 10,000,000 tokens (6 dp) -> $0.00001 per token x 1e9 supply = $10,000.
    mcap = S.executable_mcap(MINT, "http://rpc.invalid", usd=100.0, http=_node(seen),
                             quote_fn=lambda _mint, _usd: {"outAmount": "10000000000000"})
    assert mcap == pytest.approx(10_000.0)


def test_an_early_exit_finds_the_token_account_its_entry_created_seconds_ago(monkeypatch):
    """Exit path (Raydium build): the wallet's token account for a coin bought seconds ago does not
    exist yet at FINALIZED. Read there, the lookup answered "no account" (None) and the build
    derived the CLASSIC ATA, which for a Token-2022 coin is the wrong account (the MOX defect,
    2026-08-14). At `confirmed` the account the entry created is found."""
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_URL", "http://rpc.invalid")
    seen = []

    def http(_method, _url, body=None, **_kw):
        request = json.loads(body)
        seen.append(request)
        confirmed = request["params"][2].get("commitment") == "confirmed"
        value = ([{"pubkey": "Token2022Account1111111111111111111111111111",
                   "account": {"data": {"parsed": {"info": {"tokenAmount": {
                       "amount": "123456789", "decimals": 6}}}}}}] if confirmed else [])
        return 200, {"result": {"value": value}}, None

    assert S._owned_token_account(OWNER, MINT, http, sleep=lambda _s: None) == \
        "Token2022Account1111111111111111111111111111"
    assert seen[0]["params"][2] == {"encoding": "jsonParsed", "commitment": "confirmed"}
