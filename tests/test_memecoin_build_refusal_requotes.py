"""A route build refused before signing re-enters the re-quote lane (PENIS / JE3HT7, 2026-09-12)."""
from sniper.services.memecoin_real_runner import _is_transient_entry_miss


def test_a_pre_signing_build_refusal_is_retried():
    for why in ("route build refused: Raydium simulation refused the Token-2022 buy",
                "route build refused: Raydium simulation: price moved past min-out (router 0x26)",
                "route build refused: Raydium token account lookup unavailable"):
        assert _is_transient_entry_miss({"status": "aborted", "reason": why}), why


def test_only_the_pre_signing_abort_is_retried():
    assert not _is_transient_entry_miss(
        {"status": "failed", "reason": "route build refused: something"}), "a failed tx is not this class"
    assert not _is_transient_entry_miss({"status": "aborted", "reason": "sign failed"})
