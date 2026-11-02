"""A routed source buy is valued from the whole route, or stays explicitly unknown.

Alert 753be289 (2026-09-26 22:43Z): untaxxable bought GFYQZ4Bt through a router, 2 SOL ->
PumpSwap SOL/USDC -> 241.9516 USDC -> BPxx -> target. The event-bound valuation took the first
PumpSwap leg (buying USDC) as the purchase, its temporary USDC account was closed in the same
transaction, and the detection went out as 'unknown size', which the source-buy floor refused as
if the buy were under $45. The receipt already holds every leg; no RPC is needed.

The fixture is the REAL receipt (tests/fixtures/memecoin/route_source_buys.json). Negative cases
are that receipt with ONE named field changed, so each reaches its branch on real structure.
"""
import asyncio
import copy
import json
import sqlite3
import time
from pathlib import Path

import pytest

FIX = json.loads((Path(__file__).parent / "fixtures/memecoin/route_source_buys.json").read_text())
OWNER = FIX["owner"]
TARGET = "GFYQZ4BtpyewiL91j3LWwcxyvqEHG6Z9jswG3nug6fSx"
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NET_RECEIPT = 31_879_376_735_261      # owner balance delta; the 32.87M transfer pays a 3% fee


def _tx():
    return copy.deepcopy(FIX["untaxxable-route-753be289"])


def _value(tx, sol_usd=(120.0, 100.0), now=105.0):
    from sniper.services.memecoin_source_valuation import value_source_buy
    return value_source_buy(tx, OWNER, sol_usd=sol_usd, now=now)


def _transfers(tx):
    out = []
    for group in tx["meta"]["innerInstructions"]:
        for ix in group["instructions"]:
            p = ix.get("parsed")
            if isinstance(p, dict) and p.get("type") == "transferChecked":
                out.append(p["info"])
    return out


def test_the_753be289_route_is_valued_from_the_complete_route():
    facts = _value(_tx())
    assert facts.get("unknown_reason") is None, facts
    assert facts["basis"] == "route_owner_quote_out"
    assert facts["event_unknown_reason"] == "output_identity_or_quantity"   # kept for audit
    assert facts["mint"] == TARGET
    assert facts["token_amount_atomic"] == str(NET_RECEIPT)
    assert facts["token_decimals"] == 6
    assert facts["token_program"] == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    assert facts["quote_out_atomic"] == {WSOL: "2000000000", USDC: "0"}
    assert facts["source_size_usd"] == pytest.approx(2.0 * 120.0)
    assert facts["source_price"] == pytest.approx(240.0 / (NET_RECEIPT / 1e6))


def test_without_a_fresh_sol_price_the_amounts_stay_and_the_size_is_unknown():
    facts = _value(_tx(), sol_usd=None)
    assert facts["source_size_usd"] is None
    assert facts["unknown_reason"] == "sol_usd_unavailable"
    assert facts["quote_out_atomic"][WSOL] == "2000000000"


def test_quote_coming_back_to_the_owner_is_unknown_not_netted():
    """USDC out cut to 200 of the 241.95 it received: the owner kept quote, which a later native
    or unwrap leg may have spent. Netting it would guess."""
    tx = _tx()
    leg = next(t for t in _transfers(tx) if t["mint"] == USDC and t["authority"] == OWNER)
    leg["tokenAmount"]["amount"] = "200000000"
    assert _value(tx)["unknown_reason"] == "route_quote_inflow"


def test_plt_forwarded_wsol_residue_proves_the_exact_usdc_buy():
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    from sniper.services.memecoin_source_valuation import value_source_buy
    owner = "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv"
    tx = json.loads((Path(__file__).parent / "fixtures/memecoin/plt-route-2ebfcf66.json").read_text())
    facts = value_source_buy(tx, owner, sol_usd=None, now=105.0)
    assert facts.get("unknown_reason") is None, facts
    assert facts["source_size_usd"] == 500.0
    assert facts["token_amount_atomic"] == "16675750779965"
    assert facts["quote_out_atomic"] == {WSOL: "0", USDC: "500000000"}
    proof = solana_receipt_initiation(tx, owner, facts)
    assert proof["beneficiary"] == proof["payer"] == owner
    assert proof["payment_atomic"] == "500000000"
    unsigned = copy.deepcopy(tx)
    next(k for k in unsigned["transaction"]["message"]["accountKeys"]
         if k["pubkey"] == owner)["signer"] = False
    assert solana_receipt_initiation(unsigned, owner, facts) is None

    # An unwrap to the trader, an unclosed account, an existing account, or the wrong
    # close authority cannot explain away quote income.
    for mutation in ("retained", "unclosed", "existing", "authority", "not_native"):
        bad = copy.deepcopy(tx)
        parsed = [ix["parsed"] for group in bad["meta"]["innerInstructions"]
                  for ix in group["instructions"] if isinstance(ix.get("parsed"), dict)]
        close = next(p for p in parsed if p["type"] == "closeAccount")
        account = close["info"]["account"]
        if mutation == "retained":
            close["info"]["destination"] = owner
        elif mutation == "unclosed":
            close["type"] = "unknown"
        elif mutation == "existing":
            keys = [k["pubkey"] for k in bad["transaction"]["message"]["accountKeys"]]
            bad["meta"]["preBalances"][keys.index(account)] = 1
        elif mutation == "authority":
            close["info"]["owner"] = "another-wallet"
        else:
            next(p for p in parsed if p["type"] == "initializeAccount3"
                 and p["info"]["account"] == account)["info"]["mint"] = USDC
        refused = value_source_buy(bad, owner, sol_usd=None, now=105.0)
        assert refused.get("unknown_reason"), mutation
        assert solana_receipt_initiation(bad, owner, refused) is None, mutation


def test_a_non_quote_token_spent_by_the_owner_is_unknown():
    """The owner passing on MORE BPxx than it received means it funded the buy with a token."""
    tx = _tx()
    leg = next(t for t in _transfers(tx)
               if t["mint"].startswith("BPxx") and t["authority"] == OWNER)
    leg["tokenAmount"]["amount"] = str(int(leg["tokenAmount"]["amount"]) + 1)
    assert _value(tx)["unknown_reason"] == "route_non_quote_funding"


def test_two_bought_tokens_are_ambiguous():
    tx = _tx()
    extra = copy.deepcopy(next(b for b in tx["meta"]["postTokenBalances"]
                               if b["owner"] == OWNER and b["mint"] == TARGET))
    used = {b["accountIndex"] for side in ("preTokenBalances", "postTokenBalances")
            for b in tx["meta"][side]}
    extra["accountIndex"] = next(i for i in range(len(tx["transaction"]["message"]["accountKeys"]))
                                 if i not in used)            # a real key with no balance row
    extra["mint"] = "Other1111111111111111111111111111111111111"
    tx["meta"]["postTokenBalances"].append(extra)
    assert _value(tx)["unknown_reason"] == "route_target_ambiguous"


def test_a_receipt_without_parsed_instructions_is_unknown():
    tx = _tx()
    for group in tx["meta"]["innerInstructions"]:
        for ix in group["instructions"]:
            ix.pop("parsed", None)
    facts = _value(tx)
    assert facts["unknown_reason"] == "instruction_metadata_invalid"
    assert facts["source_size_usd"] is None and facts["source_price"] is None


def test_the_watcher_writes_the_route_size_on_the_detection_row(monkeypatch):
    import importlib
    watcher = importlib.import_module("scripts.research.onchain_wallet_watch")
    tx = _tx()
    monkeypatch.setattr(watcher, "_http_rpc", lambda *a, **k: tx)
    monkeypatch.setattr(watcher, "get_readonly_connection", lambda *a: sqlite3.connect(":memory:"))
    monkeypatch.setattr(watcher, "match_feed", lambda *a: (None, None))
    monkeypatch.setattr(watcher, "_stage_write", lambda row: None)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(watcher.SCHEMA)
    sig = tx["transaction"]["signatures"][0]
    asyncio.run(watcher.process_signature(sig, OWNER, {"uid": "u", "handle": "h", "status": "c"}, c,
                                          sol_usd=(120.0, time.time())))
    row = c.execute("SELECT * FROM onchain_wallet_detections").fetchone()
    assert row["mint"] == TARGET
    assert row["source_size_usd"] == pytest.approx(240.0)
    assert json.loads(row["source_valuation_json"])["basis"] == "route_owner_quote_out"


def test_watcher_persists_plt_receipt_proof_without_balance_fallback(monkeypatch):
    from scripts.research import onchain_wallet_watch as watcher
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    from sniper.services.memecoin_onchain_lane import OnchainEv
    tx = json.loads((Path(__file__).parent / "fixtures/memecoin/plt-route-2ebfcf66.json").read_text())
    owner = "F5MYbjEATQFD6rxwdS2zXzEHBGuUhSGvJkUhLFAcr4hv"
    monkeypatch.setattr(watcher, "_http_rpc", lambda *a, **k: tx)
    monkeypatch.setattr(watcher, "get_readonly_connection", lambda *a: sqlite3.connect(":memory:"))
    monkeypatch.setattr(watcher, "match_feed", lambda *a: (None, None))
    monkeypatch.setattr(watcher, "_stage_write", lambda row: None)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    c.executescript(watcher.SCHEMA)
    asyncio.run(watcher.process_signature(tx["transaction"]["signatures"][0], owner,
                {"uid": "u1", "handle": "OmakaseOnly", "status": "c"}, c, sol_usd=None))
    row = dict(c.execute("SELECT * FROM onchain_wallet_detections").fetchone())
    facts = json.loads(row["source_valuation_json"])
    assert row["source_size_usd"] == 500.0 and facts["basis"] == "route_owner_quote_out"
    assert facts["source_initiation"]["source_user_id"] == "u1"
    assert source_initiation_refusal(OnchainEv(row), "solana") is None


def test_a_curve_quoted_in_another_token_is_valued_by_its_route():
    """notdecu, 2026-09-27 02:42:12Z (detection 2463): a pump.fun curve whose quote asset is the
    token WXMRyRZh, bought through FLASHX from 0.02475 wSOL via USDC. The event path stops at
    non_sol_quote; 12 buys on this coin were refused 'source buy unknown'. The route prices it."""
    from sniper.services.memecoin_source_valuation import value_source_buy
    tx = copy.deepcopy(FIX["notdecu-nonsol-curve-5CUHe5jr"])
    facts = value_source_buy(tx, FIX["owner_notdecu"], sol_usd=(120.0, 100.0), now=105.0)
    assert facts.get("unknown_reason") is None, facts
    assert facts["event_unknown_reason"] == "non_sol_quote"
    assert facts["basis"] == "route_owner_quote_out"
    assert facts["mint"] == "BAfpFwAhQ5GVr1hRWb2YzGs2MxK1HwTdy4fpskbsmDW6"
    assert facts["token_amount_atomic"] == "122802026667"        # equals the event's token_amount
    assert facts["quote_out_atomic"] == {WSOL: "24750000", USDC: "0"}
    assert facts["source_size_usd"] == pytest.approx(0.02475 * 120.0)


def test_two_own_buy_events_on_different_legs_are_a_route():
    """notdecu 823NjegkV3, 2026-09-27 03:49:02Z: SOL -> PumpSwap (buying CbcyNo7m) -> pump curve
    quoted in CbcyNo7m. Two own buy events, one per leg; 22 such buys went out 'unknown size'."""
    from sniper.services.memecoin_source_valuation import value_source_buy
    tx = copy.deepcopy(FIX["notdecu-two-legs-voizEg93"])
    facts = value_source_buy(tx, FIX["owner_notdecu"], sol_usd=(120.0, 100.0), now=105.0)
    assert facts.get("unknown_reason") is None, facts
    assert facts["event_unknown_reason"] == "multiple_buy_events"
    assert facts["basis"] == "route_owner_quote_out"
    assert facts["mint"] == "823NjegkV3aJvruCCD2keK6K1d7AqWavj7u1SFMPLmzr"
    assert facts["token_amount_atomic"] == "65663074808827"       # the curve event's token_amount
    assert int(facts["quote_out_atomic"][WSOL]) > 0 and facts["quote_out_atomic"][USDC] == "0"


def test_an_exactly_duplicated_buy_event_stays_ambiguous():
    """Codex's guard: the same event line twice is a receipt contradicting itself, not a route."""
    from sniper.services.memecoin_source_valuation import value_source_buy
    tx = copy.deepcopy(FIX["notdecu-two-legs-voizEg93"])
    tx["meta"]["logMessages"] = tx["meta"]["logMessages"] * 2
    facts = value_source_buy(tx, FIX["owner_notdecu"], sol_usd=(120.0, 100.0), now=105.0)
    assert facts["unknown_reason"] == "ambiguous_buy_event" and facts["source_size_usd"] is None
