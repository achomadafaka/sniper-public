from dataclasses import replace

from sniper.execution.lifi_executor import settle_entry, settle_exit
from sniper.execution.lifi_route import DecodedQuote


CUSTODY = "0x1111111111111111111111111111111111111111"
WETH = "0x2222222222222222222222222222222222222222"
TOKEN = "0x3333333333333333333333333333333333333333"
DIAMOND = "0x4444444444444444444444444444444444444444"
OTHER = "0x5555555555555555555555555555555555555555"
TX_HASH = "0x" + "aa" * 32
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _topic(address):
    return "0x" + address[2:].rjust(64, "0")


def _transfer(token, sender, recipient, amount):
    return {
        "address": token,
        "topics": [TRANSFER, _topic(sender), _topic(recipient)],
        "data": "0x" + format(amount, "064x"),
    }


def _decoded(input_token=WETH, output_token=TOKEN):
    return DecodedQuote(
        calldata_sha256="c" * 64,
        decoded_semantics_sha256="d" * 64,
        fee_atomic=2,
        input_atomic=1000,
        output_min_atomic=900,
        path=(input_token, input_token, output_token),
        request_sha256="a" * 64,
        response_sha256="b" * 64,
        spender=DIAMOND,
        swap_input_atomic=998,
        target=DIAMOND,
        tool="nordstern",
        custody=CUSTODY,
        input_recipients=(DIAMOND,),
    )


def _receipt(logs, *, status="0x1", tx_hash=TX_HASH):
    return {"transactionHash": tx_hash, "status": status, "logs": logs}


def test_entry_uses_exact_receipt_movements_when_balance_read_lags():
    decoded = _decoded()
    receipt = _receipt([
        _transfer(WETH, CUSTODY, DIAMOND, 1000),
        _transfer(TOKEN, OTHER, CUSTODY, 950),
    ])

    result = settle_entry(
        receipt, {"expected_tx_hash": TX_HASH, "current": False}, decoded,
    )

    assert result.ok
    assert result.input_atomic == 1000
    assert result.output_atomic == 950
    assert result.source == "receipt"


def test_entry_rejects_wrong_recipient_short_output_and_current_balance_conflict():
    decoded = _decoded()
    wrong_recipient = _receipt([
        _transfer(WETH, CUSTODY, OTHER, 1000),
        _transfer(TOKEN, OTHER, CUSTODY, 950),
    ])
    assert settle_entry(wrong_recipient, {"expected_tx_hash": TX_HASH}, decoded).code \
        == "unexpected_input_recipient"

    short = _receipt([
        _transfer(WETH, CUSTODY, DIAMOND, 1000),
        _transfer(TOKEN, OTHER, CUSTODY, 899),
    ])
    assert settle_entry(short, {"expected_tx_hash": TX_HASH}, decoded).code \
        == "minimum_output"

    good = _receipt([
        _transfer(WETH, CUSTODY, DIAMOND, 1000),
        _transfer(TOKEN, OTHER, CUSTODY, 950),
    ])
    conflict = settle_entry(good, {
        "expected_tx_hash": TX_HASH,
        "current": True,
        "input_delta_atomic": 999,
        "output_delta_atomic": 950,
    }, decoded)
    assert conflict.code == "balance_conflict"


def test_exit_requires_exact_mint_output_and_minimum_weth_proceeds():
    decoded = replace(
        _decoded(TOKEN, WETH), input_atomic=950, output_min_atomic=800,
    )
    receipt = _receipt([
        _transfer(TOKEN, CUSTODY, DIAMOND, 950),
        _transfer(WETH, OTHER, CUSTODY, 820),
    ])

    result = settle_exit(receipt, {
        "expected_tx_hash": TX_HASH,
        "current": True,
        "input_delta_atomic": 950,
        "output_delta_atomic": 820,
    }, decoded)

    assert result.ok
    assert result.source == "balance_and_receipt"


def test_exit_rejects_unexpected_custody_token_movement_and_wrong_receipt():
    decoded = replace(
        _decoded(TOKEN, WETH), input_atomic=950, output_min_atomic=800,
    )
    unexpected = _receipt([
        _transfer(TOKEN, CUSTODY, DIAMOND, 950),
        _transfer(TOKEN, OTHER, CUSTODY, 1),
        _transfer(WETH, OTHER, CUSTODY, 820),
    ])
    assert settle_exit(unexpected, {"expected_tx_hash": TX_HASH}, decoded).code \
        == "unexpected_custody_movement"

    failed = _receipt([], status="0x0")
    assert settle_exit(failed, {"expected_tx_hash": TX_HASH}, decoded).code \
        == "receipt_failed"

    wrong_hash = _receipt([], tx_hash="0x" + "bb" * 32)
    assert settle_exit(wrong_hash, {"expected_tx_hash": TX_HASH}, decoded).code \
        == "receipt_hash_mismatch"
