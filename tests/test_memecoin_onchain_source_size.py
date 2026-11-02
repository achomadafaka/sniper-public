"""The detector must preserve the quote spend through the event adapter."""
import asyncio
import importlib
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.services.memecoin_onchain_lane import OnchainEv


@pytest.fixture
def watcher(monkeypatch, tmp_path):
    read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **kw:
                        "MEMECOIN_RESEARCH_HELIUS_RPC_URL=https://test.helius.test/?api-key=fake"
                        if p.name == ".env" else read_text(p, *a, **kw))
    mod = importlib.import_module("scripts.research.onchain_wallet_watch")
    # Never the production stage file: 72 test rows (sig "sig", addr "wallet") had landed in
    # data/onchain_watch_stages.jsonl and sat inside the watcher's before/after windows.
    monkeypatch.setattr(mod, "STAGE_LOG", tmp_path / "stages.jsonl")
    return mod


@pytest.mark.parametrize("usdc_spent", [100.0, 0.0])
def test_quote_spend_survives_detection_and_event_adapter(watcher, monkeypatch, usdc_spent):
    def balance(mint, amount):
        return {"owner": "wallet", "mint": mint, "uiTokenAmount": {"uiAmount": amount}}

    tx = {"transaction": {"signatures": ["sig"], "message": {"accountKeys": ["wallet"]}}, "meta": {
        "err": None, "preTokenBalances": [balance(watcher.USDC, 150)],
        "postTokenBalances": [balance(watcher.USDC, 150 - usdc_spent), balance("TOKEN", 1000)],
        "preBalances": [1_000_000_000], "postBalances": [999_800_000]}}
    monkeypatch.setattr(watcher, "_http_rpc", lambda *a: tx)
    monkeypatch.setattr(watcher, "get_readonly_connection", lambda *a: sqlite3.connect(":memory:"))
    monkeypatch.setattr(watcher, "match_feed", lambda *a: (None, None))
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(watcher.SCHEMA)
    asyncio.run(watcher.process_signature("sig", "wallet", {"uid": "u", "handle": "h", "status": "confirmed"}, c))
    row = c.execute("SELECT * FROM onchain_wallet_detections").fetchone()
    assert row["sol_spent"] == pytest.approx(0.0002)
    # A bare owner USDC debit with no bound swap transfer never establishes size or price
    # (c9c57f2f8, RICK 73818): this receipt has no instructions, so both stay unknown.
    assert row["source_size_usd"] is None
    assert row["source_price"] is None
    ev = OnchainEv(row)
    assert ev.their_size_usd is None
    assert ev.their_price == row["source_price"]


def test_confirmed_sol_valuation_is_persisted_before_publication(watcher, monkeypatch):
    tx = json.loads((Path(__file__).parent / "fixtures/memecoin/confirmed_source_buys.json").read_text())["untaxxable"]
    owner = tx["meta"]["postTokenBalances"][0]["owner"]
    signature = tx["transaction"]["signatures"][0]
    calls = []
    def rpc(method, *args):
        calls.append(method)
        return tx
    monkeypatch.setattr(watcher, "_http_rpc", rpc)
    monkeypatch.setattr(watcher, "get_readonly_connection", lambda *a: sqlite3.connect(":memory:"))
    monkeypatch.setattr(watcher, "match_feed", lambda *a: (None, None))
    monkeypatch.setattr(watcher.time, "time", lambda: 105.0)
    monkeypatch.setattr(watcher, "_stage_write", lambda row: None)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(watcher.SCHEMA)
    asyncio.run(watcher.process_signature(signature, owner, dict(uid="u", handle="h", status="confirmed"),
                                         conn, sol_usd=(120, 100)))
    row = conn.execute("SELECT * FROM onchain_wallet_detections").fetchone()
    assert row["source_size_usd"] == pytest.approx(0.019753085 * 120)
    assert row["source_price"] == pytest.approx(0.019753085 * 120 / 55516.485324)
    facts = json.loads(row["source_valuation_json"])
    assert facts["source_slot"] == tx["slot"]
    assert facts["sol_usd_age_s"] == 5
    assert facts["signature"] == signature
    assert calls == ["getTransaction"]


def test_wrapped_sol_buy_with_another_fee_payer_is_detected(watcher):
    def balance(mint, amount):
        return {"owner": "wallet", "mint": mint, "uiTokenAmount": {"uiAmount": amount}}
    tx = {"transaction": {"message": {"accountKeys": ["payer", "wallet"]}}, "meta": {
        "err": None, "preTokenBalances": [balance(watcher.WSOL, 3)],
        "postTokenBalances": [balance(watcher.WSOL, 2), balance("TOKEN", 100)],
        "preBalances": [10**9, 10**9], "postBalances": [999_995_000, 10**9]}}
    assert watcher.classify_buy(tx, "wallet") == ("TOKEN", 100, 0, 0)


def test_price_refresh_is_separate_and_failure_does_not_renew_timestamp(watcher, monkeypatch):
    async def check():
        cache = {"snapshot": (120, 0)}
        def fail():
            raise TimeoutError("unavailable")
        monkeypatch.setattr(watcher, "_capture_sol_usd", fail)
        async def stop(_):
            raise asyncio.CancelledError()
        monkeypatch.setattr(watcher.asyncio, "sleep", stop)
        with pytest.raises(asyncio.CancelledError):
            await watcher._refresh_sol_usd(cache)
        assert cache["snapshot"] == (120, 0)
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [("inputMint", "wrong"), ("outputMint", "wrong"),
                                        ("inAmount", "9"), ("outAmount", "nan")])
def test_cache_quote_identity_and_capture_role(watcher, monkeypatch, field, value):
    from sniper.execution import solana_real_seams as seams
    quote = dict(inputMint=watcher.WSOL, outputMint=watcher.USDC,
                 inAmount="1000000000", outAmount="120000000")
    quote[field] = value
    def get_quote(*args):
        assert seams.active_role() == seams.ROLE_CAPTURE
        return quote
    monkeypatch.setattr(seams, "_quote_with_fallback", get_quote)
    assert watcher._capture_sol_usd() is None
    assert seams.active_role() == seams.ROLE_FUNDED


@pytest.mark.parametrize("returned", [[], ["wrong-signature"]])
def test_missing_or_wrong_transaction_identity_cannot_publish_value(watcher, monkeypatch, returned):
    tx = json.loads((Path(__file__).parent / "fixtures/memecoin/confirmed_source_buys.json").read_text())["untaxxable"]
    owner = tx["meta"]["postTokenBalances"][0]["owner"]
    tx["transaction"]["signatures"] = returned
    monkeypatch.setattr(watcher, "_http_rpc", lambda *a: tx)
    monkeypatch.setattr(watcher, "get_readonly_connection", lambda *a: sqlite3.connect(":memory:"))
    monkeypatch.setattr(watcher, "match_feed", lambda *a: (None, None))
    monkeypatch.setattr(watcher, "_stage_write", lambda row: None)
    conn = sqlite3.connect(":memory:")
    conn.executescript(watcher.SCHEMA)
    asyncio.run(watcher.process_signature("wanted", owner, dict(uid="u", handle="h", status="confirmed"), conn))
    if returned:
        assert conn.execute("SELECT COUNT(*) FROM onchain_wallet_detections").fetchone()[0] == 0
    else:
        row = conn.execute("SELECT source_size_usd, source_price, block_time, source_valuation_json FROM onchain_wallet_detections").fetchone()
        assert row[:3] == (None, None, None)
        assert json.loads(row[3])["unknown_reason"] == "missing_transaction_signature"
