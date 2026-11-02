"""Minimal locally-constructed signed Solana transaction for submission tests.

The removed ``solana_fast_transaction`` module used to double as a fixture factory. Tests that only
needed *a* signed VersionedTransaction (with a recoverable first signature and blockhash) build one
here directly with solders — no production code, no tip/priority-fee construction. The recent
blockhash is ``Hash.default()`` so callers can assert the recovered blockhash deterministically.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

_JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


def signed_tx(*, last_valid_height=777):
    """Return a signed tx handle exposing ``signed_b64``, ``signature``, ``blockhash``,
    ``last_valid_height`` — the exact fields the submission wiring/executor tests consume."""
    kp = Keypair()
    blockhash = Hash.default()
    ix = Instruction(Pubkey.from_string(_JUP), b"swap", [AccountMeta(kp.pubkey(), True, True)])
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], blockhash)
    vt = VersionedTransaction(msg, [kp])
    return SimpleNamespace(
        signed_b64=base64.b64encode(bytes(vt)).decode(),
        signature=str(vt.signatures[0]),
        blockhash=str(blockhash),
        last_valid_height=last_valid_height)
