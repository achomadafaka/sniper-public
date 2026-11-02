"""OP-stack fee accounting for funded Ink transactions (plan Task 8: L2 execution + L1 data +
operator fee, from authoritative chain evidence; a missing part is unknown, never 0)."""
import pytest

from ink_constants import INK_TOKEN
from sniper.execution import evm_op_fees as F
from sniper.persistence import db_memecoin as db

L1 = {"l1Fee": hex(34_766_893_215), "l1GasUsed": hex(5_328), "l1GasPrice": hex(945_643_171)}


def test_the_three_parts_sum_exactly():
    fee = F.tx_fee({"gasUsed": hex(255_139), "effectiveGasPrice": hex(1_000_284), "l1Fee": hex(7)},
                   operator_wei=11)
    assert fee == {"l2_execution_wei": str(255_139 * 1_000_284), "l1_data_wei": "7",
                   "operator_wei": "11", "total_wei": str(255_139 * 1_000_284 + 7 + 11),
                   "status": "complete"}


@pytest.mark.parametrize("receipt, operator, missing", [
    ({"gasUsed": "0x1", "effectiveGasPrice": "0x1"}, 0, "l1_data"),
    ({"gasUsed": "0x1", "l1Fee": "0x1"}, 0, "l2_execution"),
    ({"gasUsed": "0x1", "effectiveGasPrice": "0x1", "l1Fee": "0x1"}, None, "operator"),
    ({"gasUsed": "0x1", "effectiveGasPrice": "0x1", "l1Fee": "garbage"}, 0, "l1_data"),
])
def test_a_missing_part_is_unknown_never_zero(receipt, operator, missing):
    fee = F.tx_fee(receipt, operator_wei=operator)
    assert fee["status"] == "unknown:" + missing and fee["total_wei"] is None


def test_a_read_zero_operator_fee_is_complete():
    fee = F.tx_fee({"gasUsed": "0x1", "effectiveGasPrice": "0x2", "l1Fee": "0x3"}, operator_wei=0)
    assert fee["status"] == "complete" and fee["operator_wei"] == "0" and fee["total_wei"] == "5"


def _filled(ink_real, **chain):
    t = ink_real(l1_fee=L1, **chain)
    r = t.ex["execute_fn"](t.conn, t.reserve(), INK_TOKEN, 10.0, "2026-09-29T09:00:01Z")
    assert r["status"] == "filled", r
    return t, r["tx_hash"]


def test_the_swap_fee_comes_from_its_canonical_receipt_and_the_oracle_at_its_block(ink_real):
    t, tx_hash = _filled(ink_real, operator_fee=123)
    fee = t.ex["op_fee_fn"](tx_hash)
    receipt = t.chain.mined[tx_hash]
    l2 = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
    assert fee["status"] == "complete"
    assert int(fee["total_wei"]) == l2 + int(L1["l1Fee"], 16) + 123
    assert t.chain.oracle_blocks == [receipt["blockNumber"]], "the operator fee is read at the tx's own block"


@pytest.mark.parametrize("chain, missing", [({"operator_fee": None}, "operator"),
                                            ({"l1_fee": {}}, "l1_data")])
def test_an_unavailable_part_records_unknown_and_a_later_read_completes_it(ink_real, chain, missing):
    kwargs = {"l1_fee": L1, **chain}
    t = ink_real(**kwargs)
    r = t.ex["execute_fn"](t.conn, t.reserve(), INK_TOKEN, 10.0, "2026-09-29T09:00:01Z")
    out = F.reconcile_fees(t.conn, "57073", t.ex["op_fee_fn"], now_iso="2026-09-29T09:01:00Z")
    assert out == {"recorded": 0, "unknown": 1, "pending": 0}
    row = t.conn.execute("SELECT status, total_wei FROM memecoin_evm_tx_fees").fetchone()
    assert tuple(row) == ("unknown:" + missing, None)
    t.chain.operator_fee, t.chain.l1_fee = 0, L1                  # the oracle answers again
    t.chain.mined[r["tx_hash"]].update(L1)
    assert F.reconcile_fees(t.conn, "57073", t.ex["op_fee_fn"])["recorded"] == 1
    assert t.conn.execute("SELECT status FROM memecoin_evm_tx_fees").fetchone()[0] == "complete"


def test_a_complete_fee_row_is_never_rewritten(ink_real):
    t, tx_hash = _filled(ink_real)
    F.reconcile_fees(t.conn, "57073", t.ex["op_fee_fn"])
    before = tuple(t.conn.execute("SELECT * FROM memecoin_evm_tx_fees").fetchone())
    db.record_evm_tx_fee(t.conn, network_id="57073", tx_hash=tx_hash, kind="entry",
                         fee={"status": "unknown:operator", "total_wei": None})
    assert tuple(t.conn.execute("SELECT * FROM memecoin_evm_tx_fees").fetchone()) == before
    assert F.reconcile_fees(t.conn, "57073", t.ex["op_fee_fn"]) == {"recorded": 0, "unknown": 0,
                                                                   "pending": 0}


def test_a_provisional_receipt_records_nothing(ink_real):
    t, tx_hash = _filled(ink_real)
    t.chain.receipt_mode = "provisional"
    assert t.ex["op_fee_fn"](tx_hash) is None
    assert F.reconcile_fees(t.conn, "57073", t.ex["op_fee_fn"]) == {"recorded": 0, "unknown": 0,
                                                                   "pending": 1}
    assert t.conn.execute("SELECT COUNT(*) FROM memecoin_evm_tx_fees").fetchone()[0] == 0


def test_two_custody_transactions_in_one_block_each_carry_their_own_fee(ink_real):
    """Same block, two funded swaps: each fee comes from its own receipt. Neither is the block's
    balance delta, and they do not sum into one another."""
    t = ink_real(l1_fee=L1, same_block=True)
    hashes = []
    for i, mint in enumerate((INK_TOKEN, "0x" + "c" * 40)):
        pid = db.reserve_pending(t.conn, mint=mint, source_user_id="w%d" % i, strategy="alpha_b",
                                 notional=10.0, reserved_at="2026-09-29T09:00:00Z", network_id="57073")
        r = t.ex["execute_fn"](t.conn, pid, mint, 10.0, "2026-09-29T09:00:01Z")
        assert r["status"] == "filled", r
        hashes.append(r["tx_hash"])
    blocks = {t.chain.mined[h]["blockNumber"] for h in hashes}
    assert len(blocks) == 1, "the fixture must put both swaps in one block"
    F.reconcile_fees(t.conn, "57073", t.ex["op_fee_fn"])
    rows = dict(t.conn.execute("SELECT tx_hash, total_wei FROM memecoin_evm_tx_fees").fetchall())
    for h in hashes:
        rec = t.chain.mined[h]
        own = int(rec["gasUsed"], 16) * int(rec["effectiveGasPrice"], 16) + int(L1["l1Fee"], 16)
        assert int(rows[h.lower()]) == own
    assert len(set(rows.values())) == 2


def test_off_the_op_stack_chains_there_is_no_fee_seam():
    from eth_account import Account
    from sniper.execution import evm_real_seams as S
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
                                   http=lambda *_a, **_k: (200, {"result": "0x0"}, None),
                                   usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)
    assert ex["op_fee_fn"] is None
