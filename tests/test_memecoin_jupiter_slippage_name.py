"""LILMET (2026-09-11 17:28Z): Jupiter v6 failed a slippage breach with the bare code, so the
revert persisted as "custom program error: 0x1771" and the retry lane never re-quoted it."""
from sniper.execution import solana_real_seams as R
from sniper.services import memecoin_real_runner as RR

JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
LOGS = [
    "Program ComputeBudget111111111111111111111111111111 invoke [1]",
    "Program %s invoke [1]" % JUP,
    "Program dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN success",
    "Program %s consumed 102957 of 134728 compute units" % JUP,
    "Program %s failed: custom program error: 0x1771" % JUP,
]


def test_jupiter_0x1771_is_named_as_slippage():
    assert R._program_error_name(LOGS) == "slippage tolerance exceeded (Jupiter 0x1771)"


def test_the_same_code_from_another_program_stays_a_bare_code():
    """6001 means something else in another Anchor program; never guess from a number."""
    other = [l.replace(JUP, "SomeOtherProgram1111111111111111111111111111") for l in LOGS]
    assert R._program_error_name(other) == "custom program error: 0x1771"


def test_the_named_revert_is_retried_once_through_every_gate():
    res = {"status": "failed", "reason": "chain_error:" + R._program_error_name(LOGS)}
    assert RR._is_transient_entry_miss(res) is True
    bare = {"status": "failed", "reason": "chain_error:custom program error: 0x1771"}
    assert RR._is_transient_entry_miss(bare) is False
