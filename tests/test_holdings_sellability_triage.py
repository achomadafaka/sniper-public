"""A failed read must never print as a dead market, because the output is a hide list.

`holdings_sellability_triage.py` prints an "unsellable" verdict straight into a HIDE LIST the
operator pastes into the hidden-holdings file. Every defect here turns an unknown into that
verdict, or mislabels what a venue actually offered.

Offline: fake RPC replies, fake quote callables and temporary databases. No network, no chain.
"""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from scripts.research import holdings_sellability_triage as triage
from sniper.services import memecoin_quotes as quotes

TOKEN = "0x" + "11" * 20
OTHER = "0x" + "22" * 20


def _keys_db(*keys):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memecoin_evm_v4_pool_keys(network_id TEXT, pool_id TEXT,"
                 " key_json TEXT)")
    for i, key in enumerate(keys):
        conn.execute("INSERT INTO memecoin_evm_v4_pool_keys VALUES('4663',?,?)",
                     (f"pool{i}", json.dumps(key)))
    return conn


def _holdings_db(tmp_path, *, network_id, decimals, amount=1.0):
    path = tmp_path / "holdings.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memecoin_other_holdings(network_id TEXT, mint TEXT, symbol TEXT,"
                 " amount REAL, decimals INTEGER, price_usd REAL, value_usd REAL,"
                 " liquidity_usd REAL, observed_at TEXT)")
    conn.execute("INSERT INTO memecoin_other_holdings VALUES(?,?,?,?,?,?,?,?,?)",
                 (network_id, TOKEN, "SYM", amount, decimals, 1.0, 1.0, 1.0,
                  "2026-09-23T12:00:00+00:00"))
    conn.commit()
    conn.close()
    return path


def _main(monkeypatch, path, quoter):
    monkeypatch.setattr(triage, "DB", path)
    monkeypatch.setattr("sniper.util.envfiles.load_env_files", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["triage"])
    monkeypatch.setattr(triage, "quote_4663", quoter)
    assert triage.main() == 0


# --- N9: a provider failure is not a refusal ------------------------------------------------

def test_a_json_rpc_rate_limit_is_not_a_contract_refusal(monkeypatch):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"error": {"code": -32429, "message": "rate limit"}}).encode()

    monkeypatch.setattr(triage.urllib.request, "urlopen", lambda *a, **k: Reply())
    assert triage.rpc_call(TOKEN, "0x", triage.Budget(1)) != "refused"


def test_an_execution_revert_is_still_a_refusal(monkeypatch):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"error": {"code": -32000, "message": "execution reverted"}}).encode()

    monkeypatch.setattr(triage.urllib.request, "urlopen", lambda *a, **k: Reply())
    assert triage.rpc_call(TOKEN, "0x", triage.Budget(1)) == "refused"


def test_every_venue_erroring_is_unknown_not_unsellable(monkeypatch):
    monkeypatch.setattr(triage, "v2_out", lambda *a: "error:TimeoutError")
    monkeypatch.setattr(triage, "v3_out", lambda *a: "error:TimeoutError")
    verdict, detail = triage.quote_4663(_keys_db(), TOKEN, 100, triage.Budget(50), lambda *a: None)
    assert verdict == "unknown", detail


def test_a_negative_names_the_scope_it_searched(monkeypatch):
    """A bounded walk of stored keys cannot establish that no market exists anywhere."""
    monkeypatch.setattr(triage, "v2_out", lambda *a: 0)
    monkeypatch.setattr(triage, "v3_out", lambda *a: 0)
    verdict, detail = triage.quote_4663(_keys_db(), TOKEN, 100, triage.Budget(50), lambda *a: None)
    assert verdict == "unknown"
    assert "scope" in detail or "tested" in detail, detail


# --- N10: chain and output-token identity ----------------------------------------------------

def test_a_base_holding_is_never_quoted_against_the_robinhood_node(monkeypatch, tmp_path, capsys):
    asked = []
    _main(monkeypatch, _holdings_db(tmp_path, network_id="8453", decimals=18),
          lambda *a: asked.append(a) or ("unsellable", "fake"))
    out = capsys.readouterr().out
    assert asked == [], "8453 is not this tool's chain"
    assert "unknown" in out


def test_a_v4_pool_paying_an_unrelated_token_is_not_native_proceeds():
    conn = _keys_db({"currency0": TOKEN, "currency1": OTHER, "fee": 500})
    verdict, detail = triage.quote_4663(conn, TOKEN, 100, triage.Budget(50), lambda *a: 10 ** 18)
    assert verdict != "sellable", detail
    assert OTHER[:10] in detail or "native" in detail, detail


def test_a_v4_pool_paying_native_is_sellable():
    conn = _keys_db({"currency0": triage.ZERO_ADDR, "currency1": TOKEN, "fee": 500})
    verdict, detail = triage.quote_4663(conn, TOKEN, 100, triage.Budget(50), lambda *a: 10 ** 18)
    assert verdict == "sellable", detail


# --- N11: Jupiter settles in USDC, not SOL ---------------------------------------------------

def test_jupiter_proceeds_are_reported_as_usdc(monkeypatch):
    monkeypatch.setattr(quotes, "jupiter_exit_quote",
                        lambda *a, **k: {"outAmount": "1000000", "outputMint": quotes.USDC_MINT})
    verdict, detail = triage.quote_solana("Mint", 100)
    assert verdict == "sellable"
    assert "lamports" not in detail, detail
    assert "USDC" in detail and "1.00" in detail, detail


# --- N12: zero decimals is a value, not a missing field --------------------------------------

def test_zero_decimals_quotes_one_atomic_unit(monkeypatch, tmp_path, capsys):
    asked = []
    _main(monkeypatch, _holdings_db(tmp_path, network_id="4663", decimals=0),
          lambda conn, token, amount, *rest: asked.append(amount) or ("unknown", "fake"))
    assert asked == [1], "decimals 0 means the balance IS the atomic amount"


def test_missing_decimals_is_unknown_not_eighteen(monkeypatch, tmp_path, capsys):
    asked = []
    _main(monkeypatch, _holdings_db(tmp_path, network_id="4663", decimals=None),
          lambda *a: asked.append(a) or ("unsellable", "fake"))
    out = capsys.readouterr().out
    assert asked == [], "an unknown scale cannot be quoted"
    assert "unknown" in out and "decimals" in out


def test_a_typed_no_route_is_evidence_about_the_token(monkeypatch):
    """`jupiter_exit_quote` RAISES rather than returning None, so the typed failure decides."""
    def raise_no_route(*a, **k):
        raise quotes.QuoteUnavailable("no route", failure=quotes.TypedQuoteFailure.NO_ROUTE)

    monkeypatch.setattr(quotes, "jupiter_exit_quote", raise_no_route)
    verdict, detail = triage.quote_solana("Mint", 100)
    assert verdict == "unsellable", detail


@pytest.mark.parametrize("failure", ["RATE_LIMIT", "RPC_TRANSPORT", "PROVIDER_TIMEOUT",
                                     "MALFORMED_RESPONSE"])
def test_a_typed_provider_failure_is_evidence_about_us(monkeypatch, failure):
    def raise_provider(*a, **k):
        raise quotes.QuoteUnavailable("provider", failure=getattr(quotes.TypedQuoteFailure,
                                                                 failure))

    monkeypatch.setattr(quotes, "jupiter_exit_quote", raise_provider)
    verdict, detail = triage.quote_solana("Mint", 100)
    assert verdict == "unknown", detail
    assert failure.lower() in detail.lower(), detail
