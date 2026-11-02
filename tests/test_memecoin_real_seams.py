"""Item 4 — bind the DISARMED executor seams to real HTTP (Jupiter v6 + Solana/Helius JSON-RPC +
solders signing). These are the request-construction / response-parsing / signing units; they are
tested against an injected `http` (never a live endpoint). The live smoke test (item 22) is the only
thing that proves the end-to-end path — these tests prove we build the right requests and parse the
right responses, and that a real keypair round-trips a Jupiter-shaped unsigned tx.
"""
import base64
import json

import pytest

from sniper.execution import solana_real_seams as S
from sniper.execution.solana_wallet import USDC_MINT


def _fake_http(script):
    """script: list of (assert_fn(method,url,body)->None, response_json) consumed in order.
    Returns an http(method,url,headers,body,timeout) -> (200, json, None)."""
    calls = {"i": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        idx = calls["i"]; calls["i"] += 1
        check, resp, status = script[idx]
        if check:
            check(method, url, body)
        return status, resp, None
    return http


# --- Jupiter quote (entry: USDC -> mint) ---

@pytest.mark.parametrize("c2", [False, True], ids=["serial", "c2_parallel"])
def test_entry_quote_builds_usdc_to_mint_request(monkeypatch, c2):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", c2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    seen, ray = {}, []

    def check(method, url, body):
        seen["method"] = method; seen["url"] = url
    scripted = _fake_http([(check, {"priceImpactPct": "0.004", "outAmount": "123"}, 200)])

    def http(method, url, headers=None, body=None, timeout=10):
        if "raydium.io" in url:                       # C2's call beside Jupiter's, its own request
            ray.append(url)
            return 200, {"success": False}, None
        return scripted(method, url, headers=headers, body=body, timeout=timeout)
    q = S.jupiter_entry_quote("MintX", 10.0, http=http, slippage_bps=300)
    if c2:
        import time
        deadline = time.monotonic() + 2.0
        while not ray and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(ray) == 1 and "amount=10000000" in ray[0]
    else:
        assert ray == []
    assert seen["method"] == "GET"
    assert f"inputMint={USDC_MINT}" in seen["url"]
    assert "outputMint=MintX" in seen["url"]
    assert "amount=10000000" in seen["url"]          # 10 USDC * 1e6 atomic
    assert "slippageBps=300" in seen["url"]
    assert "restrictIntermediateTokens=false" in seen["url"]
    assert q["priceImpactPct"] == "0.004"


def test_entry_quote_fail_closed_on_non_200():
    http = _fake_http([(None, {"error": "no route"}, 404)])
    assert S.jupiter_entry_quote("MintX", 10.0, http=http) is None


# --- Jupiter quote (exit: mint -> USDC), amount is already atomic ---

def test_exit_quote_builds_mint_to_usdc_request():
    seen = {}

    def check(method, url, body):
        seen["url"] = url
    http = _fake_http([(check, {"priceImpactPct": "0.01", "outAmount": "5"}, 200)])
    q = S.jupiter_exit_quote("MintX", 42, 10.0, http=http)   # slip 10%
    assert "inputMint=MintX" in seen["url"]
    assert f"outputMint={USDC_MINT}" in seen["url"]
    assert "amount=42" in seen["url"]
    assert "slippageBps=1000" in seen["url"]                 # 10% -> 1000 bps
    assert "restrictIntermediateTokens" not in seen["url"]
    assert q["outAmount"] == "5"


# --- Jupiter swap tx build ---

def test_swap_tx_posts_quote_and_returns_b64():
    seen = {}

    def check(method, url, body):
        seen["method"] = method; seen["url"] = url; seen["body"] = json.loads(body)
    http = _fake_http([(check, {"swapTransaction": "QkFTRTY0VFg="}, 200)])
    tx = S.jupiter_swap_tx({"quote": 1}, "PubKeyABC", http=http)
    assert seen["method"] == "POST"
    assert seen["body"]["userPublicKey"] == "PubKeyABC"
    assert seen["body"]["quoteResponse"] == {"quote": 1}
    assert tx == "QkFTRTY0VFg="


def test_exit_swap_tx_priority_fee_is_explicit_and_default_off():
    bodies = []
    http = _fake_http([
        (lambda _m, _u, body: bodies.append(json.loads(body)), {"swapTransaction": "A"}, 200),
        (lambda _m, _u, body: bodies.append(json.loads(body)), {"swapTransaction": "B"}, 200),
    ])
    S.jupiter_swap_tx({"quote": 1}, "P", http=http, priority_fee_microlamports=0)
    S.jupiter_swap_tx({"quote": 1}, "P", http=http, priority_fee_microlamports=5000)
    assert "prioritizationFeeLamports" not in bodies[0]
    assert "computeUnitPriceMicroLamports" not in bodies[0]
    assert bodies[1]["computeUnitPriceMicroLamports"] == 5000


def test_swap_tx_strips_internal_quote_metadata_before_jupiter_post():
    seen = {}

    def check(method, url, body):
        seen["quote"] = json.loads(body)["quoteResponse"]

    http = _fake_http([(check, {"swapTransaction": "QkFTRTY0VFg="}, 200)])
    S.jupiter_swap_tx({
        "outAmount": "1", "_sniper_quote_id": "q1:not-for-jupiter",
        "_sniper_quote_price": 1.0, "_sniper_route_target": "AMM",
    }, "PubKeyABC", http=http)
    assert seen["quote"] == {"outAmount": "1"}


def test_swap_tx_fail_closed_when_missing():
    http = _fake_http([(None, {"error": "x"}, 200)])
    assert S.jupiter_swap_tx({"quote": 1}, "P", http=http) is None


# --- solders signing round-trip on a Jupiter-shaped unsigned tx ---

def _unsigned_tx_b64(kp):
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey
    from solders.hash import Hash
    from solders.signature import Signature
    ix = Instruction(Pubkey.default(), b"", [])
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    unsigned = VersionedTransaction.populate(msg, [Signature.default()])
    return base64.b64encode(bytes(unsigned)).decode()


def test_sign_swap_tx_round_trips():
    from solders.keypair import Keypair
    kp = Keypair()
    b64 = _unsigned_tx_b64(kp)
    signed_b64, sig = S.sign_swap_tx(b64, kp)
    assert isinstance(signed_b64, str) and len(signed_b64) > 0
    assert isinstance(sig, str) and len(sig) > 0
    # signed tx deserializes and carries a non-default signature
    from solders.transaction import VersionedTransaction
    from solders.signature import Signature
    resigned = VersionedTransaction.from_bytes(base64.b64decode(signed_b64))
    assert resigned.signatures[0] != Signature.default()


def test_sign_swap_tx_fail_closed_on_garbage():
    from solders.keypair import Keypair
    assert S.sign_swap_tx("not-base64-a-tx!!", Keypair()) is None


# --- RPC send + confirm ---

def test_rpc_send_posts_base64_and_returns_sig():
    seen = {}

    def check(method, url, body):
        seen["body"] = json.loads(body)
    http = _fake_http([(check, {"result": "SigABC"}, 200)])
    sig = S.rpc_send("SIGNEDB64", "https://rpc", http=http, preflight_commitment="confirmed")
    assert seen["body"]["method"] == "sendTransaction"
    assert seen["body"]["params"][0] == "SIGNEDB64"
    assert seen["body"]["params"][1]["encoding"] == "base64"
    assert seen["body"]["params"][1]["preflightCommitment"] == "confirmed"
    assert seen["body"]["params"][1]["skipPreflight"] is False
    assert sig == "SigABC"


def test_rpc_send_distinguishes_explicit_preflight_rejection_from_uncertain_send():
    reject = _fake_http([(None, {"error": {"code": -32002, "data": {
        "err": {"InstructionError": [3, {"Custom": 6001}]}}}}, 200)])
    assert S.rpc_send("SIGNED", "https://rpc", http=reject,
                      classify_preflight=True) == "explicit_rejected"
    uncertain = _fake_http([(None, {"error": {"code": 429}}, 429)])
    assert S.rpc_send("SIGNED", "https://rpc", http=uncertain,
                      classify_preflight=True) is None


def test_rpc_confirm_confirmed():
    http = _fake_http([(None, {"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}}, 200)])
    assert S.rpc_confirm("Sig", "https://rpc", http=http, sleep=lambda s: None) == "confirmed"


def test_rpc_confirm_failed_on_err():
    http = _fake_http([(None, {"result": {"value": [{"confirmationStatus": "processed", "err": {"x": 1}}]}}, 200)])
    assert S.rpc_confirm("Sig", "https://rpc", http=http, sleep=lambda s: None) == "failed"


def test_rpc_confirm_not_found_after_valid_null_responses():
    # Explicit null in a valid response is materially different from an unavailable RPC.
    script = [(None, {"result": {"value": [None]}}, 200) for _ in range(3)]
    http = _fake_http(script)
    assert S.rpc_confirm("Sig", "https://rpc", http=http, sleep=lambda s: None, max_attempts=3) == "not_found"


def test_rpc_confirm_pending_when_signature_is_processed():
    http = _fake_http([
        (None, {"result": {"value": [{"confirmationStatus": "processed", "err": None}]}}, 200),
    ])
    assert S.rpc_confirm(
        "Sig", "https://rpc", http=http, sleep=lambda s: None, max_attempts=1,
    ) == "pending"


@pytest.mark.parametrize("status,payload", [
    (503, {"error": "upstream"}),
    (200, {"error": {"code": -32000}}),
    (200, {"result": {"value": []}}),
    (200, {"result": "wrong-shape"}),
])
def test_rpc_confirm_unavailable_on_transport_or_malformed_response(status, payload):
    http = _fake_http([(None, payload, status)])
    assert S.rpc_confirm(
        "Sig", "https://rpc", http=http, sleep=lambda s: None, max_attempts=1,
    ) == "unavailable"


def test_rpc_confirm_unavailable_on_exception():
    def http(*_args, **_kwargs):
        raise TimeoutError("rpc timeout")

    assert S.rpc_confirm(
        "Sig", "https://rpc", http=http, sleep=lambda s: None, max_attempts=1,
    ) == "unavailable"


def test_rpc_confirm_preserves_pending_over_later_unavailable_poll():
    http = _fake_http([
        (None, {"result": {"value": [{"confirmationStatus": "processed", "err": None}]}}, 200),
        (None, {"error": "down"}, 503),
    ])
    assert S.rpc_confirm(
        "Sig", "https://rpc", http=http, sleep=lambda s: None, max_attempts=2,
    ) == "pending"


# --- factory: execute_fn reads source_user_id+strategy from the pending row ---

@pytest.mark.parametrize("c2", [False, True], ids=["serial", "c2_parallel"])
def test_build_executor_execute_fn_reads_pending_row_and_fills(monkeypatch, c2):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", c2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from solders.keypair import Keypair
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    # a reserved pending entry carries the source_user_id + strategy the executor must recover
    pid = m.reserve_pending(c, mint="MintX", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", signal_source="manual")
    # HTTP order: entry quote, planned-quantity sell quote, swap, send, confirmation.
    kp = Keypair()
    unsigned = _unsigned_tx_b64(kp)
    calls = {"i": 0}

    ray = []

    def http(method, url, headers=None, body=None, timeout=10):
        if "raydium.io" in url:
            # C2's call beside the entry quote (Gamma LOW on 1074ecbf6): routed by URL, so it takes
            # no slot of the ordered script; a healthy Jupiter route never uses its answer.
            ray.append(url)
            return 200, {"success": False}, None
        i = calls["i"]; calls["i"] += 1
        if i == 0:
            return 200, {
                "inAmount": "10000000",
                "outAmount": "1000",
                "otherAmountThreshold": "900",
                "priceImpactPct": "0.001",
            }, None
        if i == 1:
            assert method == "GET"
            assert "inputMint=MintX" in url
            assert "amount=900" in url
            return 200, {
                "inAmount": "900",
                "outAmount": "9000000",
                "otherAmountThreshold": "8100000",
            }, None
        if i == 2:
            return 200, {"swapTransaction": unsigned}, None
        if i == 3:
            return 200, {"result": "SigZ"}, None
        return 200, {"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}}, None

    ex = S.build_real_solana_executor(keypair=kp, rpc_url="https://rpc", http=http,
                                      usdc_fn=_seq([100.0, 90.0]), token_fn=_seq([0.0, 5.0]))
    # signal-time mcap + trader price on record -> the real position must capture BOTH (were NULL).
    # fill: spent 100−90=10 / got 5−0=5 -> fill_price 2.0; their_price 1.6 -> gap (2.0−1.6)/1.6 = +25%.
    c.executemany(
        "INSERT INTO memecoin_events(fomo_event_id,source_user_id,network_id,mint,their_price,"
        "their_market_cap,created_at,side) VALUES(?,?,?,?,?,?,?,?)",
        [
            ("ev1", "trader1", "solana", "MintX", 1.6, 3200000,
             "2026-07-09T00:00:00Z", "buy"),
            # Same address on another chain and another trader's later Solana buy are not the
            # signal this pending entry copied. Neither may contaminate execution-drift telemetry.
            ("ev2", "trader1", "4663", "MintX", 9.0, 9000000,
             "2026-07-09T00:00:01Z", "buy"),
            ("ev3", "trader2", "solana", "MintX", 8.0, 3200000,
             "2026-07-09T00:00:02Z", "buy"),
        ],
    )
    c.commit()
    res = ex["execute_fn"](c, pid, "MintX", 10.0, "2026-07-09T00:00:01Z")
    assert res["status"] == "filled"
    pos = m.get_position(c, res["position_id"])
    assert pos["source_user_id"] == "trader1"      # recovered from the pending row
    assert pos["strategy"] == "alpha_b"
    assert pos["execution_mode"] == "real"
    assert pos["entry_mcap"] == 3200000            # captured from the feed (resolve_mint_mcap)
    assert pos["their_price"] == 1.6               # the trader price we copied
    assert abs(pos["fill_gap_pct"] - 25.0) < 1e-9  # our fill drifted +25% above the trader's price


def test_funded_solana_binding_passes_exact_atomic_balance_snapshots(monkeypatch):
    import sqlite3
    from solders.keypair import Keypair
    from sniper.execution import solana_swap_executor
    from sniper.execution.solana_wallet import TokenBalanceSnapshot
    from sniper.persistence import db_memecoin as m

    sentinel = TokenBalanceSnapshot(atomic=9_007_199_254_740_993_001, decimals=6)
    monkeypatch.setattr(S, "token_balance_snapshot", lambda *_a, **_k: sentinel)
    seen = {}

    def fake_execute_entry(conn, **kwargs):
        seen["snapshot"] = kwargs["token_fn"]("MintX")
        seen["chase_max_pct"] = kwargs["chase_max_pct"]
        return {"status": "unknown"}

    monkeypatch.setattr(solana_swap_executor, "execute_entry", fake_execute_entry)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pending_id = m.reserve_pending(
        conn, mint="MintX", source_user_id="u", strategy="alpha_b", notional=1.0,
        reserved_at="t", network_id="solana", signal_source="manual",
    )
    executor = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc", http=lambda *_a, **_k: (200, {}, None),
        usdc_fn=lambda: 1.0,
    )
    executor["execute_fn"](
        conn, pending_id, "MintX", 1.0, "t", chase_max_pct=30.0)
    assert seen["snapshot"] is sentinel
    assert seen["chase_max_pct"] == 30.0


def test_funded_solana_binding_reads_a_verified_receipt_only_after_delta_failure(monkeypatch):
    import sqlite3
    from solders.keypair import Keypair
    from sniper.execution import solana_swap_executor
    from sniper.persistence import db_memecoin as m

    kp = Keypair()
    owner = str(kp.pubkey())
    seen = {}

    def fake_execute_entry(_conn, **kwargs):
        seen["fill"] = kwargs["receipt_fill_fn"]("SIG123", "MintX", 10.0)
        return {"status": "anomaly"}

    def http(_method, _url, body=None, **_kwargs):
        request = json.loads(body)
        assert request["method"] == "getTransaction"
        assert request["params"] == ["SIG123", {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 1,
        }]
        return 200, {"result": {
            "transaction": {
                "signatures": ["SIG123"],
                "message": {"accountKeys": [{"pubkey": owner, "signer": True}]},
            },
            "meta": {
                "err": None,
                "preTokenBalances": [{"mint": USDC_MINT, "owner": owner,
                                      "uiTokenAmount": {"amount": "10000000", "decimals": 6}}],
                "postTokenBalances": [
                    {"mint": USDC_MINT, "owner": owner,
                     "uiTokenAmount": {"amount": "0", "decimals": 6}},
                    {"mint": "MintX", "owner": owner,
                     "uiTokenAmount": {"amount": "500", "decimals": 0}},
                ],
            },
        }}, None

    monkeypatch.setattr(solana_swap_executor, "execute_entry", fake_execute_entry)
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pending_id = m.reserve_pending(
        conn, mint="MintX", source_user_id="u", strategy="alpha_b", notional=10.0,
        reserved_at="t", network_id="solana", signal_source="manual",
    )
    executor = S.build_real_solana_executor(
        keypair=kp, rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 10.0, token_fn=lambda _mint: 0.0,
    )

    executor["execute_fn"](conn, pending_id, "MintX", 10.0, "t")

    assert seen["fill"] == (10.0, 500.0, 0)
    assert callable(executor["recover_confirmed_entry_fn"])


def test_funded_solana_binding_reads_exact_exit_proceeds_from_the_receipt():
    from solders.keypair import Keypair

    kp = Keypair()
    owner = str(kp.pubkey())

    def http(_method, _url, body=None, **_kwargs):
        request = json.loads(body)
        assert request["method"] == "getTransaction"
        return 200, {"result": {
            "transaction": {
                "signatures": ["SIG-EXIT"],
                "message": {"accountKeys": [{"pubkey": owner, "signer": True}]},
            },
            "meta": {
                "err": None,
                "preTokenBalances": [{"mint": USDC_MINT, "owner": owner,
                                      "uiTokenAmount": {"amount": "7000000", "decimals": 6}}],
                "postTokenBalances": [{"mint": USDC_MINT, "owner": owner,
                                       "uiTokenAmount": {"amount": "22000000", "decimals": 6}}],
            },
        }}, None

    executor = S.build_real_solana_executor(
        keypair=kp, rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 22.0, token_fn=lambda _mint: 0.0,
    )

    assert executor["receipt_proceeds_fn"]("SIG-EXIT") == 15.0


def _seq(values):
    """A no-arg fn returning successive values (models a balance that changes after the swap)."""
    it = {"i": 0}

    def f(*_a, **_k):
        v = values[min(it["i"], len(values) - 1)]; it["i"] += 1
        return v
    return f


def test_solana_preview_quotes_exact_raw_balance_without_execution(monkeypatch):
    from solders.keypair import Keypair

    seen = {}
    monkeypatch.setattr(S, "token_balance_raw", lambda *_a, **_k: 123456789012345678)

    def quote(mint, atomic, slip, http=None):
        seen.update(mint=mint, atomic=atomic, slip=slip)
        return {"outAmount": "25000000"}

    monkeypatch.setattr(S, "jupiter_exit_quote", quote)
    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc", http=lambda *_a, **_k: (200, {}, None),
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )
    preview = ex["preview_exit_fn"]({"mint": "MintX"})
    assert preview.expected_usdc == 25.0
    assert preview.amount_atomic == 123456789012345678
    assert seen["atomic"] == 123456789012345678


def _token_accounts(amount="123", owner="owner"):
    return {"result": {"value": [{
        "pubkey": "account", "account": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {"parsed": {"type": "account", "info": {"owner": owner, "mint": "MintX",
        "tokenAmount": {"amount": amount, "decimals": 6}}}}}
    }]}}


@pytest.mark.parametrize("exc", [OSError("transport"), TimeoutError("timeout")])
def test_solana_preview_retries_one_balance_transport_failure(monkeypatch, exc):
    from solders.keypair import Keypair

    calls = {"balance": 0, "jupiter": 0}
    sleeps = []

    def http(method, url, **_kwargs):
        if url == "https://rpc":
            calls["balance"] += 1
            if calls["balance"] == 1:
                raise exc
            return 200, _token_accounts(owner=json.loads(_kwargs["body"])["params"][0]), None
        calls["jupiter"] += 1
        return 200, {"outAmount": "25000000"}, None

    monkeypatch.setattr(S, "_sleep", sleeps.append, raising=False)
    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )

    preview = ex["preview_exit_fn"]({"mint": "MintX"})

    assert preview.expected_usdc == 25.0
    assert preview.provider_id == "solana:exact_quote"
    assert preview.provider_group == "solana:exact_quote"
    assert calls == {"balance": 2, "jupiter": 1}
    assert sleeps == [0.2]


@pytest.mark.parametrize("exc", [OSError("transport"), TimeoutError("timeout")])
def test_solana_preview_retries_one_jupiter_transport_failure(monkeypatch, exc):
    from solders.keypair import Keypair

    calls = {"balance": 0, "jupiter": 0}
    sleeps = []

    def http(method, url, **_kwargs):
        if url == "https://rpc":
            calls["balance"] += 1
            return 200, _token_accounts(owner=json.loads(_kwargs["body"])["params"][0]), None
        calls["jupiter"] += 1
        if calls["jupiter"] == 1:
            raise exc
        return 200, {"outAmount": "25000000"}, None

    monkeypatch.setattr(S, "_sleep", sleeps.append, raising=False)
    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )

    preview = ex["preview_exit_fn"]({"mint": "MintX"})

    assert preview.expected_usdc == 25.0
    assert preview.provider_id == "solana:exact_quote"
    assert preview.provider_group == "solana:exact_quote"
    assert calls == {"balance": 1, "jupiter": 2}
    assert sleeps == [0.2]


def test_solana_preview_reports_balance_provider_after_retry_exhaustion(monkeypatch):
    from solders.keypair import Keypair
    # Read-endpoint failover (2026-09-09) adds one call to a DIFFERENT endpoint after custody
    # gives up; this test pins the custody retry behaviour alone, so the failover is switched
    # off here and covered in test_memecoin_exit_preview_balance_failover.py.
    from sniper.settings import memecoin as _cfg
    monkeypatch.setattr(_cfg, "MEMECOIN_SOLANA_READ_RPC_URL", "")
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    calls = []
    sleeps = []

    def http(_method, url, **_kwargs):
        calls.append(url)
        raise OSError("balance transport down")

    monkeypatch.setattr(S, "_sleep", sleeps.append, raising=False)
    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )

    result = ex["preview_exit_fn"]({"mint": "MintX"})

    assert result.failure == QuoteFailure.RPC_TRANSPORT
    assert result.provider_id == "solana:balance_rpc"
    assert result.provider_group == "solana:exact_quote"
    assert calls == ["https://rpc", "https://rpc"]
    assert sleeps == [0.2]


def test_solana_preview_reports_jupiter_provider_after_retry_exhaustion(monkeypatch):
    from solders.keypair import Keypair
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    calls = {"balance": 0, "jupiter": 0}
    sleeps = []

    def http(_method, url, **_kwargs):
        if url == "https://rpc":
            calls["balance"] += 1
            return 200, _token_accounts(owner=json.loads(_kwargs["body"])["params"][0]), None
        calls["jupiter"] += 1
        raise OSError("Jupiter transport down")

    monkeypatch.setattr(S, "_sleep", sleeps.append, raising=False)
    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )

    result = ex["preview_exit_fn"]({"mint": "MintX"})

    assert result.failure == QuoteFailure.RPC_TRANSPORT
    assert result.provider_id == "solana:jupiter"
    assert result.provider_group == "solana:exact_quote"
    assert calls == {"balance": 1, "jupiter": 2}
    assert sleeps == [0.2]


def test_solana_preview_returns_typed_rate_limit(monkeypatch):
    from solders.keypair import Keypair
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    calls = []
    sleeps = []

    def http(*_args, **_kwargs):
        calls.append(1)
        return 429, {"error": "rate limited"}, None

    monkeypatch.setattr(S, "token_balance_raw", lambda *_a, **_k: 123)
    monkeypatch.setattr(S, "_sleep", sleeps.append)
    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc",
        http=http,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )
    result = ex["preview_exit_fn"]({"mint": "MintX"})
    assert result.failure == QuoteFailure.RATE_LIMIT
    assert result.provider_id == "solana:jupiter"
    # One call per Jupiter host PLUS the Raydium venue fallback (2026-08-14 20:11: a throttle
    # burst refused three routable entries, so a 429 now tries the second venue too). When every
    # venue answers 429 — this test — the typed verdict is still RATE_LIMIT and the budget's
    # backoff still fires.
    assert calls == [1, 1, 1]
    # Still no retry SLEEP: a rate limit is never retried against the same host.
    assert sleeps == []


def test_solana_balance_rpc_failure_is_infra_not_token_rejection():
    from solders.keypair import Keypair
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc",
        http=lambda *_a, **_k: (429, {"error": "rate limited"}, None),
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )
    result = ex["preview_exit_fn"]({"mint": "MintX"})
    assert result.failure == QuoteFailure.RATE_LIMIT
    assert result.provider_id == "solana:balance_rpc"


def test_solana_balance_json_rpc_error_is_infra_not_token_rejection():
    from solders.keypair import Keypair
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {"error": {"code": -32000, "message": "node busy"}}, None),
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )
    result = ex["preview_exit_fn"]({"mint": "MintX"})
    assert result.failure == QuoteFailure.RPC_TRANSPORT
    assert result.provider_id == "solana:balance_rpc"


def test_solana_preview_does_not_retry_balance_credit_exhaustion(monkeypatch):
    from solders.keypair import Keypair
    # Read-endpoint failover (2026-09-09) adds one call to a DIFFERENT endpoint after custody
    # gives up; this test pins the custody retry behaviour alone, so the failover is switched
    # off here and covered in test_memecoin_exit_preview_balance_failover.py.
    from sniper.settings import memecoin as _cfg
    monkeypatch.setattr(_cfg, "MEMECOIN_SOLANA_READ_RPC_URL", "")
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    calls = []
    sleeps = []

    def http(*_args, **_kwargs):
        calls.append(1)
        return 200, {"error": {"code": -32429, "message": "credits exhausted"}}, None

    monkeypatch.setattr(S, "_sleep", sleeps.append)
    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )

    result = ex["preview_exit_fn"]({"mint": "MintX"})

    assert result.failure == QuoteFailure.RATE_LIMIT
    assert result.provider_id == "solana:balance_rpc"
    assert calls == [1]
    assert sleeps == []


# --- bounded fast confirmation window (2026-07-28) ---

def _status_http(sequence):
    """http seam yielding one getSignatureStatuses value per call."""
    it = iter(sequence)

    def http(*_a, **_k):
        return 200, {"result": {"value": [next(it)]}}, None
    return http


def test_rpc_confirm_uses_the_bounded_fast_window_then_the_slow_tail():
    """Every measured funded Solana confirmation floored at ~1.43s against a flat 1s poll — the
    stage was waiting on the poll, not the chain."""
    sleeps = []
    http = _status_http([None, None, {"confirmationStatus": "confirmed", "err": None}])
    assert S.rpc_confirm("Sig", "https://rpc", http=http, sleep=sleeps.append,
                         max_attempts=10, poll_s=1.0, fast_poll_s=0.25,
                         fast_attempts=8) == "confirmed"
    assert sleeps == [0.25, 0.25]


def test_rpc_confirm_reverts_to_the_slow_cadence_past_the_fast_window():
    """A signature that never lands must not poll at 250ms forever — RPC load stays bounded."""
    sleeps = []
    http = _status_http([None] * 6)
    S.rpc_confirm("Sig", "https://rpc", http=http, sleep=sleeps.append,
                  max_attempts=6, poll_s=1.0, fast_poll_s=0.25, fast_attempts=2)
    assert sleeps == [0.25, 0.25, 1.0, 1.0, 1.0]


def test_rpc_confirm_default_cadence_unchanged_without_a_fast_window():
    sleeps = []
    http = _status_http([None] * 3)
    S.rpc_confirm("Sig", "https://rpc", http=http, sleep=sleeps.append,
                  max_attempts=3, poll_s=1.0)
    assert sleeps == [1.0, 1.0]
