"""EVM executor gas-price buffer (2026-07-13). Legacy txs on EIP-1559 chains (Robinhood/4663) are
rejected with 'max fee per gas less than block base fee' when an un-buffered gasPrice lands under a
baseFee that ticked up between the read and the send (observed live: node price 51178000 vs baseFee
51646000). _approve/_approve_token/_sign now multiply the node gas price by gasprice_buffer, and _sign
floors at the 0x quote's own fee. Confirmed live: a buffered approve on 4663 broadcast + confirmed."""
import types

from sniper.execution import evm_real_seams as R


class _Acct:
    address = "0x898BE9c27f8B391C47A3d9F2dA17781aa8aA3A1d"

    def __init__(self):
        self.txs = []

    def sign_transaction(self, tx):
        self.txs.append(tx)
        return types.SimpleNamespace(raw_transaction=b"\x01", hash=b"\x02")


def _executor(monkeypatch, acct, node_gp=100, buffer=1.5):
    monkeypatch.setattr(R, "eth_nonce", lambda *a, **k: 0)
    monkeypatch.setattr(R, "eth_gas_price", lambda *a, **k: node_gp)
    monkeypatch.setattr(R, "eth_send_raw", lambda *a, **k: "0xhash")
    monkeypatch.setattr(R, "eth_confirm", lambda *a, **k: "confirmed")
    return R.build_real_evm_executor(acct=acct, chain_id="4663", rpc_url="https://rpc",
                                     gasprice_buffer=buffer)


def test_approve_buffers_node_gas_price(monkeypatch):
    acct = _Acct()
    ex = _executor(monkeypatch, acct, node_gp=100, buffer=1.5)
    ok = ex["_approve"]("0x0000000000001ff3684f28c67538d4d072c22734", 12345)
    assert ok is True
    assert acct.txs[-1]["gasPrice"] == 150            # 100 * 1.5, clears baseFee


def test_sign_uses_max_of_quote_fee_and_buffered_node(monkeypatch):
    # 0x quote fee ABOVE buffered node -> use the quote fee
    acct = _Acct()
    ex = _executor(monkeypatch, acct, node_gp=100, buffer=1.5)
    ex["_sign"]({"to": "0x0000000000001ff3684f28c67538d4d072c22734", "data": "0x", "value": 0, "gas": 500000, "maxFeePerGas": "200"})
    assert acct.txs[-1]["gasPrice"] == 200

    # 0x quote fee BELOW buffered node (or absent) -> use the buffered node price
    ex["_sign"]({"to": "0x0000000000001ff3684f28c67538d4d072c22734", "data": "0x", "value": 0, "gas": 500000, "maxFeePerGas": "80"})
    assert acct.txs[-1]["gasPrice"] == 150
    ex["_sign"]({"to": "0x0000000000001ff3684f28c67538d4d072c22734", "data": "0x", "value": 0, "gas": 500000})   # no fee provided
    assert acct.txs[-1]["gasPrice"] == 150


def test_sign_parses_hex_quote_fee(monkeypatch):
    acct = _Acct()
    ex = _executor(monkeypatch, acct, node_gp=100, buffer=1.5)
    ex["_sign"]({"to": "0x0000000000001ff3684f28c67538d4d072c22734", "data": "0x", "value": 0, "gas": 500000, "gasPrice": "0x3e8"})
    assert acct.txs[-1]["gasPrice"] == 1000           # 0x3e8, above buffered 150


def test_sign_checksums_lowercase_to(monkeypatch):
    # 0x returns the router `to` lowercase; sign_tx must checksum it or eth_account rejects the tx
    from eth_utils import to_checksum_address
    acct = _Acct()
    ex = _executor(monkeypatch, acct, node_gp=100, buffer=1.5)
    lower = "0x0000000000001ff3684f28c67538d4d072c22734"
    ok = ex["_sign"]({"to": lower, "data": "0x", "value": 0, "gas": 500000, "maxFeePerGas": "200"})
    assert ok is not None
    assert acct.txs[-1]["to"] == to_checksum_address(lower)   # checksummed, not the lowercase input


def test_default_buffer_is_positive(monkeypatch):
    # a plain executor (default buffer) still buffers above 1.0x
    acct = _Acct()
    monkeypatch.setattr(R, "eth_nonce", lambda *a, **k: 0)
    monkeypatch.setattr(R, "eth_gas_price", lambda *a, **k: 1000)
    monkeypatch.setattr(R, "eth_send_raw", lambda *a, **k: "0xhash")
    monkeypatch.setattr(R, "eth_confirm", lambda *a, **k: "confirmed")
    ex = R.build_real_evm_executor(acct=acct, chain_id="4663", rpc_url="https://rpc")
    ex["_approve"]("0xspender", 1)
    assert acct.txs[-1]["gasPrice"] > 1000            # buffered above the raw node price
