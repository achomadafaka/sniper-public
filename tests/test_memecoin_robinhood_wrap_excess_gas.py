"""The Robinhood wrap script shares the funded wallet's coordination and leaves a durable receipt.

Codex review 2026-09-23, finding A5: the script obtained a nonce and broadcast while the funded
wallet lock was held, and an unknown confirmation left no record. It now takes the same
per-(wallet, chain) lock the executor uses, non-blocking with a bounded wait, re-reads balances and
the nonce under it, and writes the receipt BEFORE the broadcast. Every chain/key/sign/send seam is
faked; nothing here touches a network.
"""
import contextlib
import io
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALLET = "0x" + "12" * 20


@pytest.fixture
def wrap(tmp_path, monkeypatch):
    from sniper.execution import evm_wallet, evm_real_seams
    from sniper.settings import memecoin as cfg
    from sniper.util import envfiles
    module = runpy.run_path(str(ROOT / "scripts/memecoin_robinhood_wrap_excess_gas.py"))
    sent = []
    (tmp_path / "data").mkdir()
    import sqlite3
    from sniper.persistence import db_memecoin as m
    ledger = sqlite3.connect(tmp_path / "ledger.db"); ledger.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(ledger); ledger.close()     # an empty ledger: nothing in flight
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(envfiles, "load_env_files", lambda *a: None)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "offline://stub")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI", 10 ** 15)
    monkeypatch.setattr(evm_wallet, "load_keypair", lambda *a: SimpleNamespace(address=WALLET))
    monkeypatch.setattr(evm_wallet, "_rpc",
                        lambda *a: {"result": hex(4663 if a[1] == "eth_chainId" else 30000)})
    monkeypatch.setattr(evm_wallet, "native_balance_raw_or_none", lambda *a: 10 ** 17)
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *a: 0)
    monkeypatch.setattr(evm_real_seams, "eth_nonce", lambda *a: 42)
    monkeypatch.setattr(evm_real_seams, "eth_gas_price", lambda *a: 1)
    monkeypatch.setattr(evm_real_seams, "sign_tx", lambda *a: ("FAKE_BYTES", "FAKE_SIG"))
    monkeypatch.setattr(evm_real_seams, "eth_send_raw", lambda *a: sent.append(a) or "FAKE_SIG")
    monkeypatch.setattr(evm_real_seams, "eth_confirm", lambda *a, **kw: "unknown")
    monkeypatch.setattr(module["_time"], "sleep", lambda *_: None) if "_time" in module else None
    monkeypatch.setattr(sys, "argv", ["wrap", "--confirm", "--state-root", str(tmp_path),
                                      "--lock-wait-s", "0"])
    return module, sent, tmp_path


def _run(module):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        module["main"]()
    return out.getvalue()


def test_a_held_wallet_lock_refuses_and_sends_nothing(wrap):
    from sniper.execution import exec_lock
    module, sent, root = wrap
    with exec_lock.wallet_exec_lock("evm-4663-" + WALLET, lock_dir=str(root / "data")):
        with pytest.raises(ValueError, match="lock"):
            _run(module)
    assert sent == []
    assert list((root / "data").glob("robinhood-wrap-*.json")) == []


def test_a_free_lock_wraps_once_and_the_receipt_precedes_the_broadcast(wrap):
    module, sent, root = wrap
    seen_at_send = []
    original = module["eth_send_raw"] if "eth_send_raw" in module else None
    from sniper.execution import evm_real_seams

    def send(raw, rpc):
        receipts = list((root / "data").glob("robinhood-wrap-*.json"))
        seen_at_send.append([r.read_text() for r in receipts])
        sent.append((raw, rpc))
        return "FAKE_SIG"
    with patch.object(evm_real_seams, "eth_send_raw", send):
        _run(module)
    assert len(sent) == 1
    assert seen_at_send and seen_at_send[0], "the receipt must exist BEFORE the broadcast"
    assert '"wrap_submitted"' in seen_at_send[0][0]
    final = list((root / "data").glob("robinhood-wrap-*.json"))[0].read_text()
    assert '"wrap_status": "unknown"' in final, "an unknown confirmation stays on disk"


def test_an_unknown_wrap_refuses_a_second_send_until_it_is_reconciled(wrap):
    """A5 recheck, 2026-09-23. Every invocation minted a new request_id and never read the
    receipts already on disk, so an `unknown` confirmation -- a transaction that may still land --
    was followed by a second wrap at the next nonce. The receipt is the point of writing it before
    the broadcast; refusing to broadcast past an unresolved one is what makes it useful."""
    module, sent, root = wrap
    _run(module)
    assert len(sent) == 1
    with pytest.raises(ValueError, match="unresolved"):
        _run(module)
    assert len(sent) == 1, "a second wrap was broadcast past an unresolved receipt"
    assert len(list((root / "data").glob("robinhood-wrap-*.json"))) == 1


def test_a_reconcile_resolves_a_confirmed_wrap_and_sends_nothing(wrap):
    """The resume path is read-only with respect to the chain: it re-polls the hash the receipt
    already holds, writes the answer back through the same receipt, and never signs."""
    from sniper.execution import evm_real_seams
    module, sent, root = wrap
    _run(module)
    with patch.object(evm_real_seams, "eth_confirm", lambda *a, **kw: "confirmed"), \
            patch.object(sys, "argv", ["wrap", "--reconcile", "--state-root", str(root)]):
        out = _run(module)
    assert len(sent) == 1, "reconcile must not broadcast"
    receipt = list((root / "data").glob("robinhood-wrap-*.json"))[0].read_text()
    assert '"state": "done"' in receipt, receipt
    assert "confirmed" in out
    _run(module)                                   # the block is gone, so a fresh wrap may go out
    assert len(sent) == 2


def test_a_wrap_the_chain_reverted_does_not_block_the_next_one(wrap):
    """`wrap_failed` is RESOLVED: the chain answered, the nonce is consumed and nothing is
    outstanding. Only silence blocks."""
    from sniper.execution import evm_real_seams
    module, sent, root = wrap
    with patch.object(evm_real_seams, "eth_confirm", lambda *a, **kw: "failed"):
        _run(module)
    assert len(sent) == 1
    with patch.object(evm_real_seams, "eth_confirm", lambda *a, **kw: "failed"):
        _run(module)
    assert len(sent) == 2


def test_an_unreadable_receipt_blocks_rather_than_being_ignored(wrap):
    """Fail closed: a receipt this script cannot parse is an unknown transaction, not an absent
    one. It cannot be reconciled automatically either, so it needs a human."""
    module, sent, root = wrap
    (root / "data" / "robinhood-wrap-corrupt.json").write_text("{not json")
    with pytest.raises(ValueError, match="unresolved"):
        _run(module)
    assert sent == []
