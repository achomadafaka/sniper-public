from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from eth_account import Account
import pytest

from sniper.execution.lifi_runtime import build_lifi_runtime
from sniper.execution.lifi_route import DecodedQuote
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from _source_guard import code_of


CUSTODY = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
NOW = "2026-08-31T12:00:00+00:00"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
SPENDER = "0xb477751b76cf82d00a686a1232f5fcd772414af3"


def test_funded_canary_creates_an_attempt_before_reservation():
    source = code_of(Path("scripts/memecoin_lifi_canary.py"))

    assert "EntryAttemptTrace.start" in source
    assert "trace=trace" in source


def test_lifi_operator_scripts_force_the_state_root_ledger():
    for path in ("scripts/memecoin_lifi_canary.py", "scripts/memecoin_lifi_activate.py"):
        source = code_of(Path(path))

        assert "os.environ['DB_PATH'] = str(STATE_ROOT / 'sniper_tracker.db')" in source


def test_lifi_activation_accepts_an_explicit_verified_release_sha():
    source = code_of(Path("scripts/memecoin_lifi_activate.py"))

    assert "parser.add_argument('--release-sha')" in source
    assert 'release_sha = _release_sha(args.release_sha or current_release)' in source
    assert "'git', '-C'" in source


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _runtime(conn, *, remaining=11990, http=None, canary=False, activation=None):
    manifest, manifest_hash = cfg.load_lifi_manifest()
    return build_lifi_runtime(
        conn=conn, chain_id="4663", custody=CUSTODY,
        rpc_url="https://custody.example", proof_rpc_url="https://proof.example",
        http=http or (lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP must not run"))),
        acct=Account.create(), manifest=manifest, manifest_hash=manifest_hash,
        release_sha="a" * 40,
        activation=activation or {
            "quota_limit": "12000", "quota_remaining": str(remaining),
            "quota_reset": "7200",
        },
        submission_transport=None, decimals_fn=lambda _token: 18, canary=canary,
    )


def test_activated_runtime_probes_authenticated_quote_once(monkeypatch):
    conn = _conn()
    seen = {}
    monkeypatch.setenv("MEMECOIN_LIFI_API_KEY", "test-key")
    monkeypatch.setattr(m, "latest_lifi_lifecycle", lambda *_args: {
        "detail_json": json.dumps({"allowances": [{"token": TOKEN}]}),
    })

    def http(method, url, *, headers=None, **_kwargs):
        seen.update(method=method, query=parse_qs(urlparse(url).query), headers=headers)
        return 200, {"code": "NO_ROUTE"}, {
            "ratelimit-limit": "12000",
            "ratelimit-remaining": "11989",
            "ratelimit-reset": "7200",
        }

    runtime = _runtime(
        conn, http=http,
        activation={
            "quota_limit": "12000", "quota_remaining": "11990",
            "quota_reset": "7200", "canary_lifecycle_id": "activated-canary",
        },
    )

    assert runtime["recovery_context"]["authenticate_fn"]()
    assert runtime["recovery_context"]["authenticated"] is True
    assert seen["method"] == "GET"
    assert seen["headers"]["x-lifi-api-key"] == "test-key"
    # li.quest 403s a request with no User-Agent (Cloudflare 1010) and returns no quota
    # headers, which is exactly what keeps this probe unauthenticated. See
    # test_quote_always_sends_a_user_agent_and_does_not_change_request_identity.
    assert seen["headers"]["user-agent"]
    assert seen["query"]["fromChain"] == ["4663"]
    assert seen["query"]["toToken"] == [TOKEN]


def _decoded(input_token, output_token, input_atomic, output_min, suffix, gas_limit=None):
    transaction = {"to": SPENDER, "data": "0xdead", "value": "0"}
    if gas_limit is not None:
        transaction["gasLimit"] = gas_limit
    return DecodedQuote(
        calldata_sha256=suffix * 64, decoded_semantics_sha256=suffix * 64,
        fee_atomic=input_atomic * 25 // 10_000, input_atomic=input_atomic,
        output_min_atomic=output_min, path=(input_token, input_token, output_token),
        request_sha256=suffix * 64, response_sha256=suffix.upper() * 64,
        spender=SPENDER, swap_input_atomic=input_atomic - input_atomic * 25 // 10_000,
        target=SPENDER, tool="nordstern", custody=CUSTODY,
        input_recipients=(SPENDER,),
        transaction=transaction,
    )


def _transfer(token, sender, recipient, amount):
    return {
        "address": token,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + recipient[2:],
        ],
        "data": "0x" + format(amount, "064x"),
    }


def _open_lifi_position(conn):
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    lifecycle_id = m.start_lifi_lifecycle(
        conn, pending_id, chain_id="4663", custody_wallet=CUSTODY, token=TOKEN,
        release_sha="a" * 40, manifest_hash=cfg.load_lifi_manifest()[1], created_at=NOW,
    )
    m.append_lifi_event(
        conn, lifecycle_id, expected_state="reserved", new_state="entry_settled",
        event_type="entry_settled", created_at=NOW,
    )
    position_id = m.finalize_lifi_entry(
        conn, lifecycle_id=lifecycle_id, pending_id=pending_id,
        position_kwargs={
            "source_user_id": "wallet", "source_handle": "wallet", "mint": TOKEN,
            "ticker": "T", "entry_price": 10, "entry_notional": 10,
            "entry_fill_source": "chain", "entry_lag_s": 1, "their_size_usd": 10,
            "opened_at": NOW, "strategy": "control", "execution_mode": "real",
            "network_id": "4663",
        },
        fill_kwargs={
            "kind": "entry", "price": 10, "qty": 1, "notional": 10,
            "fill_source": "chain", "fraction": 1, "created_at": NOW,
        },
        created_at=NOW,
    )
    return lifecycle_id, m.get_position(conn, position_id)


def test_runtime_recovery_finalizes_a_confirmed_entry(monkeypatch):
    from sniper.execution import lifi_runtime as runtime_module

    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    lifecycle_id = m.start_lifi_lifecycle(
        conn, pending_id, chain_id="4663", custody_wallet=CUSTODY, token=TOKEN,
        release_sha="a" * 40, manifest_hash=cfg.load_lifi_manifest()[1], created_at=NOW,
        detail={"balance_baseline": {
            "input_token": WETH, "input_atomic": "1000000000000000000",
            "input_decimals": 18, "output_token": TOKEN, "output_atomic": "0",
            "output_decimals": 18, "input_usd": 2500.0,
        }},
    )
    tx_hash = "0x" + "ab" * 32
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="reserved", new_state="swap_signed",
        event_type="swap_signed", stage="entry", token=WETH, spender=SPENDER,
        input_atomic=4_000_000_000_000_000, output_min_atomic=900_000_000_000_000_000,
        fee_atomic=10_000_000_000_000, tx_hash=tx_hash, tx_identity=tx_hash, nonce=7,
        signed_payload_sha256="e" * 64, block_number=42, block_hash="0x" + "cd" * 32,
        proof_sha256="f" * 64, request_sha256="a" * 64, response_sha256="b" * 64,
        calldata_sha256="c" * 64, decoded_semantics_sha256="d" * 64,
        tool="nordstern", created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="swap_signed",
        new_state="swap_submitted_unknown", event_type="swap_submitted_unknown",
        stage="entry", tx_hash=tx_hash, created_at=NOW,
    )
    receipt = {
        "status": "0x1", "transactionHash": tx_hash, "blockNumber": "0x2a",
        "blockHash": "0x" + "cd" * 32,
        "logs": [
            _transfer(WETH, CUSTODY, SPENDER, 4_000_000_000_000_000),
            _transfer(TOKEN, "0x3333333333333333333333333333333333333333",
                      CUSTODY, 920_000_000_000_000_000),
        ],
    }
    monkeypatch.setattr(
        runtime_module, "audit_receipt_block",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, code="ok"),
    )
    context = _runtime(conn)["recovery_context"]
    context["allowance_fn"] = lambda *_args: 0

    assert context["recover_confirmed_fn"](
        m.latest_lifi_lifecycle(conn, lifecycle_id), receipt, NOW, conn=conn,
    )
    latest = m.latest_lifi_lifecycle(conn, lifecycle_id)
    position = m.get_position(conn, latest["position_id"])
    assert latest["state"] == "position_open"
    assert position["is_open"] == 1
    assert position["lifi_dependent"] == 1
    assert m.pending_row(conn, pending_id)["status"] == "confirmed"


def test_runtime_recovery_finalizes_a_confirmed_full_exit(monkeypatch):
    from sniper.execution import lifi_runtime as runtime_module

    conn = _conn()
    lifecycle_id, position = _open_lifi_position(conn)
    claim_id = m.claim_position_close(conn, position["id"], NOW, "manual")
    tx_hash = "0x" + "bc" * 32
    conn.execute(
        "UPDATE memecoin_positions SET close_sig=?,close_submitted_at=?,close_pending_at=NULL "
        "WHERE id=? AND close_claim_id=?",
        (tx_hash, NOW, position["id"], claim_id),
    )
    conn.commit()
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="position_open", new_state="position_open",
        event_type="exit_balance_baseline", stage="exit", token=TOKEN, created_at=NOW,
        detail={"balance_baseline": {
            "input_token": TOKEN, "input_atomic": "1000000000000000000",
            "input_decimals": 18, "output_token": WETH, "output_atomic": "0",
            "output_decimals": 18, "output_usd": 2500.0, "fraction": 1.0,
            "claim_id": claim_id, "exit_reason": "manual",
        }},
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="position_open", new_state="swap_signed",
        event_type="swap_signed", stage="exit", token=TOKEN, spender=SPENDER,
        input_atomic=1_000_000_000_000_000_000,
        output_min_atomic=3_600_000_000_000_000, fee_atomic=2_500_000_000_000_000,
        tx_hash=tx_hash, tx_identity=tx_hash, nonce=8,
        signed_payload_sha256="e" * 64, block_number=43, block_hash="0x" + "de" * 32,
        proof_sha256="f" * 64, request_sha256="a" * 64, response_sha256="b" * 64,
        calldata_sha256="c" * 64, decoded_semantics_sha256="d" * 64,
        tool="nordstern", created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="swap_signed",
        new_state="swap_submitted_unknown", event_type="swap_submitted_unknown",
        stage="exit", tx_hash=tx_hash, created_at=NOW,
    )
    receipt = {
        "status": "0x1", "transactionHash": tx_hash, "blockNumber": "0x2b",
        "blockHash": "0x" + "de" * 32,
        "logs": [
            _transfer(TOKEN, CUSTODY, SPENDER, 1_000_000_000_000_000_000),
            _transfer(WETH, "0x3333333333333333333333333333333333333333",
                      CUSTODY, 4_000_000_000_000_000),
        ],
    }
    monkeypatch.setattr(
        runtime_module, "audit_receipt_block",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, code="ok"),
    )
    context = _runtime(conn)["recovery_context"]
    context["allowance_fn"] = lambda *_args: 0

    assert context["recover_confirmed_fn"](
        m.latest_lifi_lifecycle(conn, lifecycle_id), receipt, NOW, conn=conn,
    )
    latest = m.latest_lifi_lifecycle(conn, lifecycle_id)
    closed = m.get_position(conn, position["id"])
    assert latest["state"] == "position_closed"
    assert closed["is_open"] == 0
    assert closed["close_claim_id"] is None


def test_runtime_exports_all_callbacks_and_unverified_recovery_state():
    runtime = _runtime(_conn())

    assert callable(runtime["entry_fn"])
    assert callable(runtime["preview_fn"])
    assert callable(runtime["exit_fn"])
    recovery = runtime["recovery_context"]
    assert recovery["authenticated"] is False
    assert recovery["quota_guard"].remaining == 11990
    assert recovery["effective_limit"] == 12000


def test_runtime_does_not_create_authority_for_a_non_no_route_result():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    result = _runtime(conn, remaining=20_000)["entry_fn"](
        conn=conn, pending_id=pending_id, mint=TOKEN, usd_amount=10,
        now_iso=NOW, direct_miss={"code": "provider_timeout"}, trace=None,
        sell_token="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        sell_token_usd=2500, local={}, safety_gate_fn=lambda: (True, "ok"),
        chase_max_pct=30, impact_cap_pct=30,
    )

    assert result == {"status": "not_selected", "reason": "direct_not_no_route"}
    assert conn.execute("SELECT COUNT(*) FROM memecoin_lifi_authority_events").fetchone()[0] == 0
    assert m.pending_row(conn, pending_id)["status"] == "failed"


def test_runtime_checks_quota_before_it_creates_authority():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )

    result = _runtime(conn, remaining=7209)["entry_fn"](
        conn=conn, pending_id=pending_id, mint=TOKEN, usd_amount=10,
        now_iso=NOW, direct_miss={"code": "no_route"}, trace=None,
        sell_token="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        sell_token_usd=2500, local={}, safety_gate_fn=lambda: (True, "ok"),
        chase_max_pct=30, impact_cap_pct=30,
    )

    assert result == {"status": "aborted", "reason": "insufficient_quota"}
    assert conn.execute("SELECT COUNT(*) FROM memecoin_lifi_authority_events").fetchone()[0] == 0
    assert m.pending_row(conn, pending_id)["status"] == "failed"


def test_cold_start_does_not_trust_persisted_quota_as_current_authentication():
    runtime = _runtime(_conn())

    assert runtime["recovery_context"]["authenticated"] is False


def test_unresolved_authority_abort_releases_the_new_pending_reservation():
    conn = _conn()
    blocked_pending = m.reserve_pending(
        conn, mint="0x4444444444444444444444444444444444444444",
        source_user_id="blocked", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    assert m.start_lifi_lifecycle(
        conn, blocked_pending, chain_id="4663", custody_wallet=CUSTODY,
        token="0x4444444444444444444444444444444444444444",
        release_sha="a" * 40, manifest_hash=cfg.load_lifi_manifest()[1],
        created_at=NOW,
    )
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )

    result = _runtime(conn, remaining=20_000)["entry_fn"](
        conn=conn, pending_id=pending_id, mint=TOKEN, usd_amount=10,
        now_iso=NOW, direct_miss={"code": "no_route"}, trace=None,
        sell_token=WETH, sell_token_usd=2500, local={},
        safety_gate_fn=lambda: (True, "ok"), chase_max_pct=30, impact_cap_pct=30,
    )

    assert result == {"status": "aborted", "reason": "unresolved_lifi_authority"}
    assert m.pending_row(conn, pending_id)["status"] == "failed"


def test_canary_uses_current_lifi_request_fields_and_exact_five_dollars():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="lifi-canary", strategy="control", notional=5,
        reserved_at=NOW, network_id="4663",
    )
    requests = []

    def http(_method, url, **_kwargs):
        requests.append(url)
        return 503, {"message": "stop"}, {
            "ratelimit-limit": "12000", "ratelimit-remaining": "11999",
            "ratelimit-reset": "7200",
        }

    result = _runtime(conn, remaining=7, http=http, canary=True)["entry_fn"](
        conn=conn, pending_id=pending_id, mint=TOKEN, usd_amount=5,
        now_iso=NOW, direct_miss={"code": "no_route"}, trace=None,
        sell_token=WETH, sell_token_usd=2500, local={},
        safety_gate_fn=lambda: (True, "ok"), chase_max_pct=30, impact_cap_pct=30,
    )

    assert result["reason"] == "lifi_provider_error"
    assert "integrator=sniper" in requests[0]
    assert "referrer=" + CUSTODY in requests[0]

    second = m.reserve_pending(
        conn, mint="0x4444444444444444444444444444444444444444",
        source_user_id="lifi-canary", strategy="control", notional=6,
        reserved_at=NOW, network_id="4663",
    )
    refused = _runtime(conn, remaining=7, http=http, canary=True)["entry_fn"](
        conn=conn, pending_id=second,
        mint="0x4444444444444444444444444444444444444444", usd_amount=6,
        now_iso=NOW, direct_miss={"code": "no_route"}, trace=None,
        sell_token=WETH, sell_token_usd=2500, local={},
        safety_gate_fn=lambda: (True, "ok"), chase_max_pct=30, impact_cap_pct=30,
    )
    assert refused == {"status": "aborted", "reason": "invalid_canary_amount"}
    assert m.pending_row(conn, second)["status"] == "failed"


def test_preapproval_provider_fault_closes_lifecycle_and_releases_reservation():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    runtime = _runtime(
        conn, http=lambda *_args, **_kwargs: (503, {"message": "down"}, {}),
    )

    result = runtime["entry_fn"](
        conn=conn, pending_id=pending_id, mint=TOKEN, usd_amount=10,
        now_iso=NOW, direct_miss={"code": "no_route"}, trace=None,
        sell_token="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        sell_token_usd=2500, local={}, safety_gate_fn=lambda: (True, "ok"),
        chase_max_pct=30, impact_cap_pct=30,
    )

    assert result["reason"] == "lifi_provider_error"
    row = conn.execute(
        "SELECT state FROM memecoin_lifi_authority_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    assert row["state"] == "entry_failed"
    assert m.pending_row(conn, pending_id)["status"] == "failed"


def test_preapproval_internal_error_closes_lifecycle_and_releases_reservation():
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    runtime = _runtime(
        conn, http=lambda *_args, **_kwargs: (200, {"route": "bad"}, "date"),
    )

    try:
        runtime["entry_fn"](
            conn=conn, pending_id=pending_id, mint=TOKEN, usd_amount=10,
            now_iso=NOW, direct_miss={"code": "no_route"}, trace=None,
            sell_token=WETH, sell_token_usd=2500, local={},
            safety_gate_fn=lambda: (True, "ok"), chase_max_pct=30,
            impact_cap_pct=30,
        )
    except AttributeError:
        pass

    row = conn.execute(
        "SELECT state FROM memecoin_lifi_authority_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    assert row["state"] == "entry_failed"
    assert m.pending_row(conn, pending_id)["status"] == "failed"


def test_a_new_entry_attempt_does_not_reuse_the_prior_approval_block(monkeypatch):
    from sniper.execution import lifi_runtime as runtime_module

    input_atomic = 4_000_000_000_000_000
    results = iter([
        _decoded(WETH, TOKEN, input_atomic, 900_000, "a", gas_limit=250_000),
        _decoded(TOKEN, WETH, 900_000, 3_900_000_000_000_000, "b", gas_limit=250_000),
        None,
        _decoded(WETH, TOKEN, input_atomic, 900_000, "c", gas_limit=250_000),
        None,
    ])

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def quote(self, _request, role, stage):
            decoded = next(results)
            if decoded is None:
                return SimpleNamespace(
                    ok=False, failure_type="provider_error", quota={}, received_at=NOW,
                )
            return SimpleNamespace(
                ok=True, decoded=decoded, request_sha256=decoded.request_sha256,
                response_sha256=decoded.response_sha256, received_at=NOW,
                quota={"limit": 12000, "remaining": 11989, "reset": 7200},
                failure_type=None,
            )

    proof_tags = []
    monkeypatch.setattr(runtime_module, "LifiClient", Client)
    monkeypatch.setattr(
        runtime_module, "prove_quote_authority",
        lambda *_args, stage, block_tag="latest", **_kwargs: (
            proof_tags.append((stage, block_tag)) or SimpleNamespace(
                stage=stage, received_at=NOW, block_number=40,
            )
        ),
    )
    monkeypatch.setattr(
        runtime_module, "approve_exact_and_wait",
        lambda *_args, **_kwargs: SimpleNamespace(status="confirmed", block_number=42),
    )
    conn = _conn()
    runtime = _runtime(conn)
    local = {
        "estimate_gas_fn": lambda _tx: 250_000,
        "gas_price_fn": lambda: 1,
        "native_balance_fn": lambda: 1,
    }
    for mint in (TOKEN, "0x4444444444444444444444444444444444444444"):
        pending_id = m.reserve_pending(
            conn, mint=mint, source_user_id="wallet", strategy="control", notional=10,
            reserved_at=NOW, network_id="4663",
        )
        runtime["entry_fn"](
            conn=conn, pending_id=pending_id, mint=mint, usd_amount=10,
            now_iso=NOW, direct_miss={"code": "no_route"}, trace=None,
            sell_token=WETH, sell_token_usd=2500, local=local,
            safety_gate_fn=lambda: (True, "ok"), chase_max_pct=30,
            impact_cap_pct=30,
        )

    assert [tag for stage, tag in proof_tags if stage == "initial_buy"] == [
        "latest", "latest",
    ]


def test_preview_returns_the_proved_guaranteed_minimum(monkeypatch):
    from sniper.execution import lifi_runtime as runtime_module

    decoded = _decoded(TOKEN, WETH, 900_000, 4_000_000_000_000_000, "a")

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def quote(self, _request, role, stage):
            return SimpleNamespace(
                ok=True, decoded=decoded, request_sha256=decoded.request_sha256,
                response_sha256=decoded.response_sha256, received_at=NOW,
                failure_type=None,
            )

    monkeypatch.setattr(runtime_module, "LifiClient", Client)
    monkeypatch.setattr(
        runtime_module, "prove_quote_authority", lambda *_args, **_kwargs: object())
    result = _runtime(_conn())["preview_fn"](
        position={"mint": TOKEN}, amount_atomic=900_000,
        direct_miss={"code": "no_route"}, local={}, sell_token=WETH,
        sell_token_usd=2500,
    )

    assert result.failure is None
    assert result.preview.expected_usdc == 10.0
    assert result.preview.amount_atomic == 900_000
    assert result.quote_evidence["venue"] == "lifi"
    assert result.quote_evidence["minimum_output_atomic"] == "4000000000000000"
    assert result.quote_evidence["valuation_basis"] == "minimum_output"


def test_confirmed_entry_uses_current_proofs_sell_simulation_and_balance_deltas(
        monkeypatch):
    from sniper.execution import lifi_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "real_utc_now_iso", lambda: NOW)
    conn = _conn()
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    input_atomic = 4_000_000_000_000_000
    quotes = iter([
        _decoded(WETH, TOKEN, input_atomic, 900_000, "a", gas_limit=250_000),
        _decoded(TOKEN, WETH, 900_000, 3_900_000_000_000_000, "b", gas_limit=250_000),
        _decoded(WETH, TOKEN, input_atomic, 910_000, "c", gas_limit=250_000),
        _decoded(TOKEN, WETH, 910_000, 3_900_000_000_000_000, "d", gas_limit=250_000),
    ])

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def quote(self, _request, role, stage):
            decoded = next(quotes)
            return SimpleNamespace(
                ok=True, decoded=decoded, request_sha256=decoded.request_sha256,
                response_sha256=decoded.response_sha256, received_at=NOW,
                quota={"limit": 12000, "remaining": 11989, "reset": 7200},
                failure_type=None,
            )

    monkeypatch.setattr(runtime_module, "LifiClient", Client)
    proof_tags = []
    proof_blocks = {
        "initial_buy": 40, "initial_sell": 41,
        "refreshed_buy": 43, "final_sell": 44,
    }

    def prove_quote(*_args, stage, received_at, block_tag="latest", **_kwargs):
        proof_tags.append((stage, block_tag))
        return SimpleNamespace(
            stage=stage, received_at=received_at, block_number=proof_blocks[stage],
        )

    monkeypatch.setattr(runtime_module, "prove_quote_authority", prove_quote)
    def prove_execution(unsigned, decoded, **kwargs):
        assert kwargs["authority"].stage == "refreshed_buy"
        authority = SimpleNamespace(
            block_number=42, block_hash="0x" + "cd" * 32,
            proof_sha256="e" * 64,
        )
        signed = ("0xraw", "0x" + "ab" * 32, 7)
        kwargs["persist"](authority, signed)
        return SimpleNamespace(
            transaction={**unsigned, "nonce": 7}, signed=signed, authority=authority,
        )
    monkeypatch.setattr(
        runtime_module, "prove_execution_block", prove_execution,
    )
    monkeypatch.setattr(
        runtime_module, "audit_receipt_block",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, code="ok"),
    )
    tx_hash = "0x" + "ab" * 32
    receipt = {
        "status": "0x1", "transactionHash": tx_hash,
        "blockNumber": "0x2a", "blockHash": "0x" + "cd" * 32,
        "logs": [
            _transfer(WETH, CUSTODY, SPENDER, input_atomic),
            _transfer(TOKEN, "0x3333333333333333333333333333333333333333", CUSTODY, 920_000),
        ],
    }
    approval_hash = "0x" + "11" * 32
    approval_receipt = {
        "status": "0x1", "transactionHash": approval_hash,
        "blockNumber": "0x2a", "logs": [],
    }
    allowance_reads = iter([input_atomic, 0])
    estimate_calls = []
    simulation_calls = []
    balance_calls = []
    balance_values = {WETH: iter([input_atomic, 0]), TOKEN: iter([0, 920_000])}
    settlement_balances = []
    real_settle_entry = runtime_module.settle_entry

    def settle_entry(receipt_value, balances, decoded):
        settlement_balances.append(balances)
        return real_settle_entry(receipt_value, balances, decoded)

    monkeypatch.setattr(runtime_module, "settle_entry", settle_entry)

    def balance_raw(token):
        balance_calls.append(token)
        return next(balance_values[token])

    local = {
        "nonce_fn": lambda: 1, "gas_price_fn": lambda: 1,
        "sign_fn": lambda _tx: ("0xapprove", approval_hash),
        "send_fn": lambda _raw: "accepted",
        "receipt_fn": lambda tx: approval_receipt if tx == approval_hash else receipt,
        "allowance_fn": lambda *_args: next(allowance_reads),
        "approve_gas": 100000,
        "estimate_gas_fn": lambda tx: estimate_calls.append(tx) or 200000,
        "construct_tx_fn": lambda tx, gas: {**tx, "from": CUSTODY, "chainId": 4663,
                                             "gas": gas, "gasPrice": 1, "nonce": 7},
        "allowance_at_block_fn": lambda *_args: input_atomic,
        "simulate_fn": lambda tx, block: simulation_calls.append((tx, block)) or True,
        "sign_complete_fn": lambda _tx: ("0xraw", tx_hash, 7),
        "send_swap_fn": lambda _raw, **_kwargs: "accepted",
        "balance_raw_fn": balance_raw,
        "native_balance_fn": lambda: 1,
    }
    result = _runtime(conn)["entry_fn"](
        conn=conn, pending_id=pending_id, mint=TOKEN, usd_amount=10,
        now_iso=NOW, direct_miss={"code": "no_route"}, trace=None,
        sell_token=WETH, sell_token_usd=2500, local=local,
        safety_gate_fn=lambda: (True, "ok"), chase_max_pct=30, impact_cap_pct=30,
    )

    assert result["status"] == "filled"
    assert estimate_calls == []
    assert {
        "proof_tags": proof_tags,
        "simulation_blocks": [block for _tx, block in simulation_calls],
        "balance_calls": balance_calls,
        "settlement_balances": settlement_balances,
    } == {
        "proof_tags": [
            ("initial_buy", "latest"), ("initial_sell", "latest"),
            ("refreshed_buy", "latest"), ("final_sell", "latest"),
        ],
        "simulation_blocks": [44],
        "balance_calls": [WETH, TOKEN, WETH, TOKEN],
        "settlement_balances": [{
            "expected_tx_hash": tx_hash,
            "current": True,
            "input_delta_atomic": input_atomic,
            "output_delta_atomic": 920_000,
        }],
    }
    assert result["tx_hash"] == tx_hash
    positions = conn.execute(
        "SELECT * FROM memecoin_positions WHERE execution_mode='real'"
    ).fetchall()
    assert len(positions) == 1
    assert positions[0]["lifi_dependent"] == 1
    assert m.pending_row(conn, pending_id)["status"] == "confirmed"


@pytest.mark.parametrize("refuse_stop", [False, True])
def test_confirmed_full_exit_cleans_allowance_and_closes_atomically(monkeypatch, refuse_stop):
    from sniper.execution import lifi_runtime as runtime_module

    conn = _conn()
    lifecycle_id, position = _open_lifi_position(conn)
    amount_atomic = 900_000
    output_atomic = 3_900_000_000_000_000
    decoded = _decoded(TOKEN, WETH, amount_atomic, output_atomic, "a")
    from dataclasses import replace
    decoded = replace(decoded, output_expected_atomic=output_atomic + 1000)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def quote(self, _request, role, stage):
            return SimpleNamespace(
                ok=True, decoded=decoded, request_sha256=decoded.request_sha256,
                response_sha256=decoded.response_sha256, received_at=NOW,
                quota={"limit": 12000, "remaining": 11989, "reset": 7200},
                failure_type=None,
            )

    monkeypatch.setattr(runtime_module, "LifiClient", Client)
    monkeypatch.setattr(runtime_module, "real_utc_now_iso", lambda: NOW)
    monkeypatch.setattr(
        runtime_module, "prove_quote_authority",
        lambda *_args, stage, received_at, **_kwargs: SimpleNamespace(
            stage=stage, received_at=received_at),
    )
    tx_hash = "0x" + "ab" * 32
    approval_hash = "0x" + "11" * 32
    authority = SimpleNamespace(
        block_number=42, block_hash="0x" + "cd" * 32, proof_sha256="e" * 64,
    )

    def prove_execution(_unsigned, _decoded_value, **kwargs):
        signed = ("0xraw", tx_hash, 7)
        kwargs["persist"](authority, signed)
        return SimpleNamespace(transaction={}, signed=signed, authority=authority)

    monkeypatch.setattr(runtime_module, "prove_execution_block", prove_execution)
    monkeypatch.setattr(
        runtime_module, "audit_receipt_block",
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, code="ok"),
    )
    approval_receipt = {"status": "0x1", "transactionHash": approval_hash, "logs": []}
    receipt = {
        "status": "0x1", "transactionHash": tx_hash,
        "blockNumber": "0x2a", "blockHash": "0x" + "cd" * 32,
        "logs": [
            _transfer(TOKEN, CUSTODY, SPENDER, amount_atomic),
            _transfer(WETH, "0x3333333333333333333333333333333333333333",
                      CUSTODY, output_atomic),
        ],
    }
    allowance_reads = iter([amount_atomic, 0])
    local = {
        "nonce_fn": lambda: 1, "gas_price_fn": lambda: 1,
        "sign_fn": lambda _tx: ("0xapprove", approval_hash),
        "send_fn": lambda _raw: "accepted",
        "receipt_fn": lambda tx: approval_receipt if tx == approval_hash else receipt,
        "allowance_fn": lambda *_args: next(allowance_reads), "approve_gas": 100000,
        "estimate_gas_fn": lambda _tx: 200000,
        "construct_tx_fn": lambda tx, gas: {
            **tx, "from": CUSTODY, "chainId": 4663, "gas": gas,
            "gasPrice": 1, "nonce": 7,
        },
        "allowance_at_block_fn": lambda *_args: amount_atomic,
        "simulate_fn": lambda *_args: True,
        "sign_complete_fn": lambda _tx: ("0xraw", tx_hash, 7),
        "send_swap_fn": lambda _raw: "accepted",
        "balance_raw_fn": lambda _token: amount_atomic,
    }
    claim_id = m.claim_position_close(conn, position["id"], NOW, "manual")
    checked = []
    def check(_position, quote, qty):
        checked.append((quote["buyAmount"], quote["minBuyAmount"], qty))
        return False, {"code": "executable_threshold_not_met"}
    if refuse_stop:
        local["sign_fn"] = lambda _: pytest.fail("a refused stop must not sign an approval")
    result = _runtime(conn)["exit_fn"](
        conn=conn, local=local, position=position, claim_id=claim_id,
        exit_reason="manual", slippage_ladder=(3,), fraction=1.0,
        direct_miss={"code": "no_route"}, now_iso=NOW,
        sell_token=WETH, sell_token_usd=2500,
        quote_check_fn=check if refuse_stop else None,
    )
    if refuse_stop:
        assert result["status"] == "deferred"
        assert checked == [(str(output_atomic + 1000), str(output_atomic), amount_atomic / 10**18)]
        assert m.get_position(conn, position["id"])["close_claim_id"] is None
        assert m.latest_lifi_lifecycle(conn, lifecycle_id)["state"] == "position_open"
        return
    assert result["status"] == "closed"
    assert m.get_position(conn, position["id"])["is_open"] == 0
    assert m.latest_lifi_lifecycle(conn, lifecycle_id)["state"] == "position_closed"


@pytest.mark.parametrize(
    ("fraction", "claim_column"),
    [(1.0, "close_claim_id"), (0.5, "derisk_claim_id")],
)
def test_pre_sign_exit_defer_releases_the_exact_claim(fraction, claim_column):
    conn = _conn()
    _lifecycle_id, position = _open_lifi_position(conn)
    if fraction == 1.0:
        claim_id = m.claim_position_close(conn, position["id"], NOW, "manual")
    else:
        claim_id = m.claim_position_derisk(conn, position["id"], NOW)

    result = _runtime(conn)["exit_fn"](
        conn=conn, local={}, position=position, claim_id=claim_id,
        exit_reason="manual", slippage_ladder=(3,), fraction=fraction,
        direct_miss={"code": "no_route"}, now_iso=NOW,
        sell_token=WETH, sell_token_usd=2500,
    )

    assert result == {"status": "deferred", "reason": "lifi_balance_unavailable"}
    assert m.get_position(conn, position["id"])[claim_column] is None


@pytest.mark.parametrize("reason", ["real_tp", "mcap_target", "b_stop"])
@pytest.mark.parametrize("fault", ["missing_expected", "invalid_expected", "below_minimum", "callback"])
def test_custom_target_quote_check_errors_cannot_authorize_a_lifi_exit(monkeypatch, reason, fault):
    from dataclasses import replace
    from sniper.execution import lifi_runtime as runtime_module

    conn = _conn()
    lifecycle_id, position = _open_lifi_position(conn)
    minimum = 3_900_000_000_000_000
    expected = {"missing_expected": None, "invalid_expected": "bad",
                "below_minimum": minimum - 1, "callback": minimum + 1000}[fault]
    decoded = replace(_decoded(TOKEN, WETH, 10**18, minimum, "a"),
                      output_expected_atomic=expected)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def quote(self, _request, role, stage):
            return SimpleNamespace(ok=True, decoded=decoded,
                request_sha256=decoded.request_sha256, response_sha256=decoded.response_sha256,
                received_at=NOW, failure_type=None)

    monkeypatch.setattr(runtime_module, "LifiClient", Client)
    monkeypatch.setattr(runtime_module, "real_utc_now_iso", lambda: NOW)
    monkeypatch.setattr(runtime_module, "prove_quote_authority", lambda *_args, **_kwargs: object())
    approvals = []

    def approval(*_args, **_kwargs):
        approvals.append(True)
        return SimpleNamespace(status="fixture_blocked", cleanup_required=False)

    monkeypatch.setattr(runtime_module, "approve_exact_and_wait", approval)
    claim_id = m.claim_position_close(conn, position["id"], NOW, reason)

    def check(*_args):
        raise RuntimeError("execution quote check unavailable")

    result = _runtime(conn)["exit_fn"](
        conn=conn, local={"balance_raw_fn": lambda _token: 10**18},
        position=position, claim_id=claim_id, exit_reason=reason,
        slippage_ladder=(3,), fraction=1.0, direct_miss={"code": "no_route"}, now_iso=NOW,
        sell_token=WETH, sell_token_usd=2500, quote_check_fn=check)

    if reason == "b_stop":
        assert approvals == [True]
        assert result == {"status": "deferred", "reason": "lifi_approval_fixture_blocked"}
    else:
        assert approvals == []
        assert result == {"status": "deferred", "reason": "execution_target_check_failed"}
        assert m.get_position(conn, position["id"])["close_claim_id"] is None
        assert m.latest_lifi_lifecycle(conn, lifecycle_id)["state"] == "position_open"
    conn.close()


@pytest.mark.parametrize('quantity,allowed', [(10*10**18,True),(6*10**18,False),(0,False)])
def test_lifi_hard_copy_ceiling_uses_decoded_guaranteed_output(monkeypatch,quantity,allowed):
    from sniper.execution import lifi_runtime as runtime
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT',35.)
    conn=_conn()
    pid=m.reserve_pending(conn,mint=TOKEN,source_user_id='wallet',strategy='control',notional=10,
        reserved_at=NOW,network_id='4663',source_price=1.)
    def check(**kwargs):
        decision=kwargs['gate_fn']('chase',SimpleNamespace(output_min_atomic=quantity),None)
        assert decision.allowed is allowed
        return {'status':'aborted','reason':'fixture_checked'}
    monkeypatch.setattr(runtime,'execute_lifi_entry',check)
    result=_runtime(conn)['entry_fn'](conn=conn,pending_id=pid,mint=TOKEN,usd_amount=10,now_iso=NOW,
        direct_miss={'code':'no_route'},trace=None,sell_token=WETH,sell_token_usd=2500,local={},
        safety_gate_fn=lambda:(True,'ok'),chase_max_pct=100,impact_cap_pct=30)
    assert result['reason']=='fixture_checked'
    conn.close()
