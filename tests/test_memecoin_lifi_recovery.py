import json
import sqlite3
import threading
import pytest

from eth_account import Account

from sniper.execution.evm_real_seams import build_real_evm_executor
from sniper.execution.lifi_executor import (
    DropProof,
    recover_lifi_lifecycles,
    prove_finalized_replacement,
)
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_real_runner import lifi_alert_baseline_ready, lifi_recovery_loop


NOW = "2026-08-30T20:00:00+00:00"
CUSTODY = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
SPENDER = "0x3333333333333333333333333333333333333333"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
SIGNED = "0x" + "aa" * 32
REPLACEMENT = "0x" + "bb" * 32
BLOCK_HASH = "0x" + "cc" * 32


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _unknown_approval(conn):
    pending_id = m.reserve_pending(
        conn, mint=TOKEN, source_user_id="wallet", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    lifecycle_id = m.start_lifi_lifecycle(
        conn, pending_id, chain_id="4663", custody_wallet=CUSTODY, token=TOKEN,
        release_sha="a" * 40, manifest_hash="b" * 64, created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="reserved", new_state="approval_signed",
        event_type="approval_signed", stage="approval", token=TOKEN, spender=SPENDER,
        amount_atomic=1000, tx_hash=SIGNED, tx_identity=SIGNED, nonce=7,
        signed_payload_sha256="d" * 64, block_number=100, block_hash=BLOCK_HASH,
        created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="approval_signed",
        new_state="approval_submitted_unknown", event_type="approval_submitted_unknown",
        stage="approval", tx_hash=SIGNED, created_at=NOW,
    )
    return lifecycle_id


def _rpc(*, finalized=True, replacement=True, receipt=None, final_number=101,
         replacement_block=101):
    def block_hash(number):
        return "0x" + format(number, "064x")

    def call(method, params):
        if method == "eth_getBlockByNumber":
            tag, full = params
            if tag == "finalized":
                if not finalized:
                    return {"result": None}
                return {"result": {
                    "number": hex(final_number), "hash": block_hash(final_number),
                }}
            number = int(tag, 16)
            txs = []
            if full and replacement and number == replacement_block:
                txs = [{"hash": REPLACEMENT, "from": CUSTODY, "nonce": "0x7"}]
            return {"result": {
                "number": hex(number), "hash": block_hash(number),
                "transactions": txs,
            }}
        if method == "eth_getTransactionReceipt":
            tx_hash = params[0]
            if tx_hash == SIGNED:
                return {"result": receipt}
            return {"result": {
                "transactionHash": REPLACEMENT,
                "blockHash": block_hash(replacement_block),
                "status": "0x1", "logs": [],
            }}
        if method == "eth_getTransactionCount":
            return {"result": "0x8"}
        raise AssertionError(method)
    return call


def _proof_row():
    return {
        "tx_hash": SIGNED, "nonce": "7", "block_number": "100",
        "custody_wallet": CUSTODY, "token": TOKEN, "spender": SPENDER,
        "reconcile_fn": lambda **_kwargs: {
            "complete": True, "settlement_effect": False, "allowance_atomic": 0,
            "balance_sha256": "e" * 64,
        },
    }


def test_finalized_replacement_requires_complete_two_rpc_proof():
    proof = prove_finalized_replacement(_proof_row(), _rpc(), _rpc())

    assert isinstance(proof, DropProof)
    assert proof.finalized_block_number == 101
    assert proof.replacement_tx_hash == REPLACEMENT
    assert proof.allowance_atomic == 0

    assert prove_finalized_replacement(_proof_row(), _rpc(finalized=False), _rpc()) is None
    assert prove_finalized_replacement(_proof_row(), _rpc(), _rpc(replacement=False)) is None
    incomplete = {**_proof_row(), "reconcile_fn": lambda **_kwargs: {"complete": False}}
    assert prove_finalized_replacement(incomplete, _rpc(), _rpc()) is None


def test_recovery_reconciles_unknown_approval_once_without_send():
    conn = _conn()
    lifecycle_id = _unknown_approval(conn)
    calls = []
    context = {
        "chain_id": "4663", "custody_wallet": CUSTODY,
        "release_sha": "a" * 40, "manifest_hash": "b" * 64,
        "receipt_fn": lambda tx_hash: calls.append(("receipt", tx_hash)) or {
            "transactionHash": tx_hash, "status": "0x1",
        },
        "allowance_fn": lambda *_args: 1000,
    }

    first = recover_lifi_lifecycles(conn, context, NOW)
    second = recover_lifi_lifecycles(conn, context, NOW)

    assert first.resolved == 1
    assert second.resolved == 0
    assert m.latest_lifi_lifecycle(conn, lifecycle_id)["state"] == "approval_confirmed"
    assert calls == [("receipt", SIGNED)]


def test_recovery_keeps_unknown_and_invalid_lifecycles_reserved():
    conn = _conn()
    lifecycle_id = _unknown_approval(conn)
    context = {
        "chain_id": "4663", "custody_wallet": CUSTODY,
        "release_sha": "wrong", "manifest_hash": "b" * 64,
        "receipt_fn": lambda _tx_hash: None,
    }

    snapshot = recover_lifi_lifecycles(conn, context, NOW)

    assert snapshot.invalid == 1
    assert snapshot.unresolved == 1
    assert m.latest_lifi_lifecycle(conn, lifecycle_id)["state"] \
        == "approval_submitted_unknown"
    assert m.lifi_slots_used(conn, "4663", CUSTODY) == 1


def test_recovery_start_gate_does_not_admit_before_full_scan():
    conn = _conn()
    _unknown_approval(conn)
    context = {
        "chain_id": "4663", "custody_wallet": CUSTODY,
        "release_sha": "a" * 40, "manifest_hash": "b" * 64,
        "receipt_fn": lambda _tx_hash: None,
        "custody_rpc": _rpc(replacement=False),
        "proof_rpc": _rpc(replacement=False),
        "reconcile_drop_fn": lambda **_kwargs: {
            "complete": True, "settlement_effect": False, "allowance_atomic": 0,
        },
    }

    snapshot = recover_lifi_lifecycles(conn, context, NOW)

    assert snapshot.scan_complete
    assert not snapshot.entry_admitted


def test_recovery_keeps_signed_identity_for_finalized_drop_proof():
    conn = _conn()
    lifecycle_id = _unknown_approval(conn)
    context = {
        "chain_id": "4663", "custody_wallet": CUSTODY,
        "release_sha": "a" * 40, "manifest_hash": "b" * 64,
        "receipt_fn": lambda _tx_hash: None,
        "custody_rpc": _rpc(), "proof_rpc": _rpc(),
        "reconcile_drop_fn": lambda **_kwargs: {
            "complete": True, "settlement_effect": False, "allowance_atomic": 0,
        },
    }

    snapshot = recover_lifi_lifecycles(conn, context, NOW)

    assert snapshot.resolved >= 1
    latest = m.latest_lifi_lifecycle(conn, lifecycle_id)
    assert latest["state"] == "entry_failed"
    assert latest["tx_hash"] == SIGNED
    assert latest["nonce"] == "7"


def test_recovery_persists_bounded_replacement_scan_progress():
    conn = _conn()
    lifecycle_id = _unknown_approval(conn)
    rpc = _rpc(final_number=170, replacement_block=165)
    context = {
        "chain_id": "4663", "custody_wallet": CUSTODY,
        "release_sha": "a" * 40, "manifest_hash": "b" * 64,
        "receipt_fn": lambda _tx_hash: None,
        "custody_rpc": rpc, "proof_rpc": rpc,
        "reconcile_drop_fn": lambda **_kwargs: {
            "complete": True, "settlement_effect": False, "allowance_atomic": 0,
        },
    }

    first = recover_lifi_lifecycles(conn, context, NOW)
    middle = m.latest_lifi_lifecycle(conn, lifecycle_id)
    second = recover_lifi_lifecycles(conn, context, NOW)

    assert first.resolved == 0
    assert json.loads(middle["detail_json"])["scan_next_block"] == 164
    assert second.resolved >= 1
    assert m.latest_lifi_lifecycle(conn, lifecycle_id)["state"] == "entry_failed"


def test_recovery_worker_waits_for_first_heartbeat_and_runs_one_bounded_pass(tmp_path):
    path = tmp_path / "recovery.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.close()
    waited = []
    stop = threading.Event()

    class Ready:
        def wait(self):
            waited.append(True)

    def connect():
        value = sqlite3.connect(path)
        value.row_factory = sqlite3.Row
        stop.set()
        return value

    context = {
        "chain_id": "4663", "custody_wallet": CUSTODY,
        "release_sha": "a" * 40, "manifest_hash": "b" * 64,
    }
    lifi_recovery_loop(
        context, Ready(), connection_factory=connect, stop_event=stop,
        now_fn=lambda: NOW, sleep_fn=lambda _seconds: None,
    )

    assert waited == [True]
    assert context["recovery_snapshot"].scan_complete


def test_recovery_worker_runs_the_authenticated_probe_once(tmp_path):
    path = tmp_path / "recovery.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.close()
    calls = []
    stop = threading.Event()

    class Ready:
        def wait(self):
            return None

    def connect():
        value = sqlite3.connect(path)
        value.row_factory = sqlite3.Row
        stop.set()
        return value

    context = {
        "chain_id": "4663", "custody_wallet": CUSTODY,
        "release_sha": "a" * 40, "manifest_hash": "b" * 64,
        "authenticate_fn": lambda: calls.append("probe") or True,
    }
    lifi_recovery_loop(
        context, Ready(), connection_factory=connect, stop_event=stop,
        now_fn=lambda: NOW, sleep_fn=lambda _seconds: None,
    )

    assert calls == ["probe"]
    assert context["authentication_probe_complete"] is True
    assert context["recovery_bootstrap_complete"] is True


@pytest.mark.parametrize("first_failure", [False, TimeoutError("probe timeout")])
def test_recovery_worker_retries_failed_auth_off_path_at_most_once_per_minute(tmp_path, monkeypatch, first_failure):
    path = tmp_path / "retry.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.close()
    stop = threading.Event()
    clock = [0.0]
    calls = []
    monkeypatch.setattr("sniper.services.memecoin_real_runner.time.monotonic", lambda: clock[0])

    class Ready:
        def wait(self):
            pass

    def authenticate():
        calls.append(clock[0])
        if len(calls) == 1:
            if isinstance(first_failure, Exception):
                raise first_failure
            return first_failure
        return True

    def connect():
        value = sqlite3.connect(path)
        value.row_factory = sqlite3.Row
        if clock[0] >= 130:
            stop.set()
        return value

    def sleep(seconds):
        clock[0] += seconds

    context = {"chain_id": "4663", "custody_wallet": CUSTODY,
               "release_sha": "a" * 40, "manifest_hash": "b" * 64,
               "authenticate_fn": authenticate}
    lifi_recovery_loop(context, Ready(), connection_factory=connect, stop_event=stop,
                       now_fn=lambda: NOW, sleep_fn=sleep)
    assert calls == [0.0, 60.0]
    assert context["authentication_probe_complete"] is True
    assert context["recovery_bootstrap_complete"] is True


def test_lifi_alert_baseline_waits_for_the_first_recovery_attempt():
    status = {"mode": "authenticated_funded", "disable_reasons": []}
    contexts = {"4663": {"lifi_recovery_context": {}}}

    assert not lifi_alert_baseline_ready(contexts, status)

    contexts["4663"]["lifi_recovery_context"]["recovery_bootstrap_complete"] = True
    assert lifi_alert_baseline_ready(contexts, status)
    assert lifi_alert_baseline_ready(
        contexts,
        {"mode": "authenticated_funded", "disable_reasons": ["configuration_invalid"]},
    )


def test_real_evm_recovery_callbacks_require_exact_finalized_chain_evidence():
    account = Account.create()
    balances = {TOKEN: 1_000, WETH: 2_000}
    rpc_calls = []

    def http(_method, _url, *, body=None, **_kwargs):
        request = json.loads(body)
        rpc_calls.append(request)
        assert request["method"] == "eth_call"
        call, block = request["params"]
        assert block == "0x64"
        if call["data"].startswith("0x70a08231"):
            value = balances[call["to"].lower()]
        elif call["data"].startswith("0xdd62ed3e"):
            value = 0
        else:
            raise AssertionError(call["data"])
        return 200, {"result": hex(value)}, None

    executor = build_real_evm_executor(
        acct=account, chain_id="4663", rpc_url="https://rpc.example", http=http,
        venue_quote_fn=lambda *_args, **_kwargs: None,
        usdc_fn=lambda: 10.0, token_fn=lambda _token: 0.0,
        decimals_fn=lambda _token: 18, price_fn=lambda: 2_500.0,
    )
    local = executor["lifi_local"]
    recover_confirmed = local.get("recover_confirmed_fn")
    reconcile_drop = local.get("reconcile_drop_fn")

    assert callable(recover_confirmed)
    assert callable(reconcile_drop)

    lifecycle = {
        "custody_wallet": account.address.lower(),
        "token": TOKEN,
        "spender": SPENDER,
        "detail_json": json.dumps({
            "balance_baseline": {
                "input_token": TOKEN,
                "input_atomic": "1000",
                "output_token": WETH,
                "output_atomic": "2000",
            },
        }),
    }
    replacement_receipt = {
        "transactionHash": REPLACEMENT,
        "blockNumber": "0x64",
        "blockHash": BLOCK_HASH,
        "status": "0x1",
        "logs": [],
    }
    exact = reconcile_drop(
        lifecycle,
        finalized_block_number=100,
        finalized_block_hash=BLOCK_HASH,
        replacement_receipt=replacement_receipt,
    )
    assert exact["complete"] is True
    assert exact["settlement_effect"] is False
    assert exact["allowance_atomic"] == 0
    assert len(rpc_calls) == 3

    balances[WETH] = 2_001
    changed = reconcile_drop(
        lifecycle,
        finalized_block_number=100,
        finalized_block_hash=BLOCK_HASH,
        replacement_receipt=replacement_receipt,
    )
    assert changed.get("settlement_effect") is not False

    conn = _conn()
    approval_id = _unknown_approval(conn)
    swap_pending = m.reserve_pending(
        conn, mint=WETH, source_user_id="swap", strategy="control", notional=10,
        reserved_at=NOW, network_id="4663",
    )
    swap_id = m.start_lifi_lifecycle(
        conn, swap_pending, chain_id="4663", custody_wallet=CUSTODY, token=WETH,
        release_sha="a" * 40, manifest_hash="b" * 64, created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, swap_id, expected_state="reserved", new_state="swap_submitted_unknown",
        event_type="swap_submitted_unknown", stage="entry", token=WETH,
        spender=SPENDER, input_atomic=10, output_min_atomic=9,
        tx_hash=REPLACEMENT, created_at=NOW,
    )
    receipts = {
        SIGNED: {"transactionHash": SIGNED, "status": "0x1", "logs": []},
        REPLACEMENT: {
            "transactionHash": REPLACEMENT, "status": "0x1", "logs": [],
        },
    }
    recover_lifi_lifecycles(conn, {
        "chain_id": "4663", "custody_wallet": CUSTODY,
        "release_sha": "a" * 40, "manifest_hash": "b" * 64,
        "receipt_fn": receipts.get, "allowance_fn": lambda *_args: 1_000,
        "recover_confirmed_fn": recover_confirmed,
    }, NOW)

    assert m.latest_lifi_lifecycle(conn, approval_id)["state"] == "approval_confirmed"
    assert m.latest_lifi_lifecycle(conn, swap_id)["state"] == "swap_submitted_unknown"
