"""One runnable guard for the research-only processed transaction probe."""

import base64
import json
import struct

from scripts.research.solana_processed_tx_probe import (
    classify_notification, classify_processed_log, decode_pool_account, load_candidate_addresses,
    logs_request,
    subscription_request,
)
from sniper.research.memecoin_opportunity.pumpfun import PUMPFUN_PROGRAM


def _b58(raw):
    from sniper.research.memecoin_opportunity.decoders import base58_encode
    return base58_encode(raw)


def test_processed_notification_needs_exact_owner_buy_and_slot():
    wallet = "Buyer11111111111111111111111111111111111111"
    other = "Other11111111111111111111111111111111111111"
    mint = "Mint111111111111111111111111111111111111111"
    watch = {wallet: {"uid": "source-a", "handle": "trader-a"}}
    tx = {
        "meta": {
            "err": None,
            "preTokenBalances": [],
            "postTokenBalances": [{"owner": wallet, "mint": mint,
                                   "uiTokenAmount": {"uiAmount": 100}}],
            "preBalances": [2_000_000_000],
            "postBalances": [1_000_000_000],
        },
        "transaction": {"message": {"accountKeys": [wallet]}},
    }
    frame = {"method": "transactionNotification", "params": {"result": {
        "signature": "sig-a", "slot": 1234, "transaction": tx}}}

    request = subscription_request(watch)
    assert request["params"][1]["commitment"] == "processed"
    assert request["params"][1]["transactionDetails"] == "full"
    assert request["params"][0]["accountInclude"] == [wallet]
    assert classify_notification(frame, watch)["source_slot"] == 1234
    assert classify_notification(frame, watch)["mint"] == mint

    tx["meta"]["postTokenBalances"][0]["owner"] = other
    assert classify_notification(frame, watch) is None
    tx["meta"]["postTokenBalances"][0]["owner"] = wallet
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert classify_notification(frame, watch) is None
    tx["meta"]["err"] = None
    frame["params"]["result"]["slot"] = None
    assert classify_notification(frame, watch) is None


def test_processed_pump_log_is_actionable_without_get_transaction():
    actor_raw, mint_raw = bytes([3]) * 32, bytes([4]) * 32
    actor, mint = _b58(actor_raw), _b58(mint_raw)
    watch = {actor: {"uid": "source-a", "handle": "trader-a"}}
    payload = b"".join((
        bytes([189, 219, 127, 211, 78, 230, 97, 238]),
        mint_raw, struct.pack("<QQ?", 100_000_000, 500_000, True), actor_raw,
        struct.pack("<qQQQQ", 1_700_000_000, 1, 1, 1, 1),
        bytes([5]) * 32, struct.pack("<QQ", 100, 1), bytes([6]) * 32,
        struct.pack("<QQ", 100, 1),
    ))
    log = "Program data: " + base64.b64encode(payload).decode()
    frame = {"method": "logsNotification", "params": {"result": {
        "context": {"slot": 1234}, "value": {"signature": "sig-pump", "err": None,
        "logs": [f"Program {PUMPFUN_PROGRAM} invoke [1]", log,
                 f"Program {PUMPFUN_PROGRAM} success"]}}}}
    assert logs_request(actor, 1)["params"][1]["commitment"] == "processed"
    event = classify_processed_log(frame, watch)
    assert (event["source_slot"], event["mint"], event["address"]) == (1234, mint, actor)
    assert event["quote_amount_atomic"] == 100_000_000
    frame["params"]["result"]["value"]["err"] = "failed"
    assert classify_processed_log(frame, watch) is None
    frame["params"]["result"]["value"]["err"] = None
    frame["params"]["result"]["value"]["logs"][0] = "Program foreign invoke [1]"
    assert classify_processed_log(frame, watch) is None


def test_processed_pumpswap_log_identifies_actor_and_pool_then_pool_account_identifies_mint():
    from sniper.research.memecoin_opportunity.decoders import base58_encode

    actor_raw, pool_raw, mint_raw = bytes([3]) * 32, bytes([4]) * 32, bytes([5]) * 32
    actor, pool, mint = map(base58_encode, (actor_raw, pool_raw, mint_raw))
    program = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
    event_raw = (bytes([103, 244, 82, 31, 44, 245, 119, 119])
                 + struct.pack("<q", 1_700_000_000) + struct.pack("<13Q", *range(1, 14))
                 + pool_raw + actor_raw)
    frame = {"method": "logsNotification", "params": {"result": {
        "context": {"slot": 1234}, "value": {"signature": "sig-amm", "err": None,
        "logs": [f"Program {program} invoke [1]",
                 "Program data: " + base64.b64encode(event_raw).decode(),
                 f"Program {program} success"]}}}}
    event = classify_processed_log(frame, {actor: {"uid": "source-a", "handle": "a"}})
    assert (event["source_slot"], event["pool"], event["address"]) == (1234, pool, actor)
    assert event["mint"] is None
    assert event["base_amount_out_atomic"] == 1
    assert event["quote_amount_in_atomic"] == 7
    assert event["user_quote_amount_in_atomic"] == 13

    account_raw = (bytes([241, 154, 109, 4, 17, 177, 109, 188])
                   + bytes([1]) + struct.pack("<H", 1) + bytes([6]) * 32
                   + mint_raw + bytes([7]) * 32)
    account = {"owner": program, "data": [base64.b64encode(account_raw).decode(), "base64"]}
    assert decode_pool_account(account)[0] == mint
    assert decode_pool_account({**account, "owner": "foreign"}) is None


def test_research_candidate_file_requires_two_distinct_exact_buys(tmp_path):
    path = tmp_path / "candidates.jsonl"
    rows = [
        {"uid": "source-a", "handle": "a", "address": "wallet-a",
         "observed": [["wallet-a", "mint-1", "sig-1"],
                      ["wallet-a", "mint-2", "sig-2"]]},
        {"uid": "source-b", "handle": "b", "address": "wallet-b",
         "observed": [["wallet-b", "mint-1", "sig-1"],
                      ["wallet-b", "mint-1", "sig-2"]]},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    assert load_candidate_addresses(path, {"source-a", "source-b"}) == {
        "wallet-a": {"uid": "source-a", "handle": "a", "status": "exact_two_mints"}}


def test_pool_resolution_admission_is_bounded_and_reuses_identity(monkeypatch, capsys):
    import asyncio
    from scripts.research import solana_processed_tx_probe as probe

    async def exercise():
        pending, cache, asked = {}, {}, []
        release = asyncio.Event()
        async def to_thread(fn, pool):
            asked.append(pool)
            await release.wait()
            return ("mint", "quote"), None
        monkeypatch.setattr(probe.asyncio, "to_thread", to_thread)
        event = {"pool": "pool0", "signature": "s", "source_slot": 1}
        for i in range(20):
            await probe.admit_pool_resolution(dict(event, pool=f"pool{i}"), pending, cache)
        await probe.admit_pool_resolution(event, pending, cache)
        await asyncio.sleep(0)
        assert len(pending) == len(asked) == 4
        release.set()
        await asyncio.gather(*list(pending.values()))
        await probe.admit_pool_resolution(event, pending, cache)
        assert len(asked) == 4
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert sum(r.get("error") == "capacity_deferred" for r in rows) == 16
        assert sum(r.get("error") == "pool_pending" for r in rows) == 1
        assert rows[-1]["mint"] == "mint" and rows[-1]["cached"]
    asyncio.run(exercise())


def test_capture_clock_starts_after_acknowledgments(monkeypatch, tmp_path, capsys):
    import asyncio
    import sqlite3
    from scripts.research import solana_processed_tx_probe as probe, onchain_wallet_watch as watch
    from sniper.persistence import db_core
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE memecoin_wallets(source_user_id TEXT,handle TEXT,real_approved INT,"
                       "paused INT,excluded INT,real_chains TEXT);"
                       "CREATE TABLE axiom_trader_directory(x_handle TEXT,chain TEXT,address TEXT);"
                       "INSERT INTO memecoin_wallets VALUES('u','h',1,0,0,'solana');")
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda *a: conn)
    monkeypatch.setattr(watch, "load_watchlist", lambda: {"owner": {"uid": "u", "handle": "h"}})
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    now = [0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: now[0])
    class WS:
        def __init__(self):
            self.frames = iter([{"method": "logsNotification"}, {"id": 1, "result": 11},
                                {"candidate": True}])
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def send(self, data):
            pass
        async def recv(self):
            now[0] += 2
            return json.dumps(next(self.frames))
    monkeypatch.setattr(probe.websockets, "connect", lambda *a, **kw: WS())
    monkeypatch.setattr(probe, "classify_processed_log", lambda frame, _: {
        "signature": "post-ack", "mint": "M", "source_slot": 1} if frame.get("candidate") else None)
    assert asyncio.run(probe.run(1, public=True)) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    subscribed = next(row for row in rows if row["type"] == "subscribed")
    assert subscribed["startup_notifications_excluded"] == 1
    assert subscribed["supported_event_scope"] == "exactly_one_watched_buy_per_transaction"
    assert any(row.get("signature") == "post-ack" for row in rows)
