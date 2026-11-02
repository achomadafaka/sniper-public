from sniper.execution import evm_real_seams as seams
from sniper.execution.evm_exit_executor import _confirmed_proceeds
from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.persistence import db_memecoin as db
import sqlite3


def test_ink_native_proceeds_cannot_use_whole_block_balance_delta(monkeypatch):
    calls = []
    def rpc(_url, method, _params, http=None):
        calls.append(method)
        return {"result": None}
    monkeypatch.setattr(seams, "_rpc", rpc)
    assert seams.native_received_in_receipt(
        "0x" + "a" * 64, "0x" + "b" * 40, "rpc", allow_block_delta=False) is None
    assert calls == []


def test_ink_missing_transaction_local_proceeds_cannot_use_wallet_delta():
    reads = []
    def balance():
        reads.append(True)
        return 999.0
    assert _confirmed_proceeds(
        balance, 0.0, lambda _s: None, lambda _h: None,
        "0x" + "a" * 64, require_receipt=True) is None
    assert reads == []


def test_ink_receipt_transfer_must_link_to_canonical_header(monkeypatch):
    tx = "0x" + "a" * 64
    token = "0x" + "b" * 40
    owner = "0x" + "c" * 40
    block = "0x" + "d" * 64
    receipt = {"transactionHash": tx, "blockHash": block, "blockNumber": "0x10",
               "status": "0x1", "logs": [{"transactionHash": tx, "blockHash": block,
               "logIndex": "0x0", "address": token,
               "topics": [seams._TRANSFER_TOPIC, "0x" + "0" * 64,
                          "0x" + owner[2:].rjust(64, "0")],
               "data": "0x" + f"{123:064x}"}]}
    monkeypatch.setattr(seams, "_rpc", lambda _u, method, _p, http=None: {
        "result": receipt if method == "eth_getTransactionReceipt"
        else {"hash": "0x" + "e" * 64, "number": "0x10"}})
    assert seams.erc20_received_in_receipt(
        tx, token, owner, "rpc", require_canonical=True) is None


def test_ink_entry_books_transaction_amounts_not_concurrent_wallet_delta():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    mint = "0x" + "b" * 40
    pending = db.reserve_pending(conn, mint=mint, source_user_id="source", strategy="alpha_b",
                                 notional=10.0, reserved_at="2026-09-29T09:00:00Z",
                                 network_id="57073")
    result = execute_entry_evm(
        conn, pending_id=pending, mint=mint, network_id="57073", usd_amount=10.0,
        strategy="alpha_b", source_user_id="source", now_iso="2026-09-29T09:00:01Z",
        quote_fn=lambda *_a, **_k: {"sellAmount": "5000000000000000",
            "transaction": {"to": "0xrouter", "data": "0xdead"}, "issues": {}},
        approve_fn=lambda *_a: True,
        sign_fn=lambda _tx: ("0xraw", "0x" + "a" * 64, 1),
        send_fn=lambda _raw: True, confirm_fn=lambda _h: "confirmed",
        usdc_fn=lambda: 100.0, token_fn=lambda _mint: 1000.0,
        receipt_token_fn=lambda _h, _mint: (5.0, 18),
        receipt_spent_fn=lambda _h: 0.005,
        sell_decimals=18, sell_token_usd=2000.0,
        require_receipt_amounts=True)
    assert result["status"] == "filled"
    assert result["notional"] == 10.0
    assert db.open_qty(conn, result["position_id"]) == 5.0
