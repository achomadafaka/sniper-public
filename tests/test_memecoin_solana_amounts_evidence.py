"""`_invokes_program` must not report a negative it never observed.

It read two sources — parsed instructions, then log lines — and both fell through `or []` to an
empty sequence, so `any([])` returned False. A transaction that was never fetched claimed the
program was NOT invoked, indistinguishable from a successful read proving absence.

Third instance of one family on 2026-09-08: `_RETRY_TRANSIENT`'s `("failed", None)` matched a shape
the producer had stopped emitting; `_program_error_name` was fed logs the caller never sent; and
`_owned_token_account` returned "no account" for a failed RPC and so re-derived the classic ATA that
killed the MOX exit ladder. Here the cost is pump-curve rows in the tape, not a trade.
"""
import pytest

from sniper.services import memecoin_solana_amounts as A

PUMP = A.PUMP_PROGRAM_ID


def _tx(*, instructions=None, logs=None, keys=None):
    tx = {"transaction": {"message": {}}, "meta": {}}
    if keys is not None:
        tx["transaction"]["message"]["accountKeys"] = keys
    if instructions is not None:
        tx["transaction"]["message"]["instructions"] = instructions
    if logs is not None:
        tx["meta"]["logMessages"] = logs
    return tx


# --- the three states ---------------------------------------------------------------------

def test_a_parsed_instruction_naming_the_program_is_True():
    assert A._invokes_program(_tx(instructions=[{"programId": PUMP}]), PUMP) is True


def test_a_log_line_naming_the_program_is_True():
    assert A._invokes_program(_tx(logs=[f"Program {PUMP} invoke [1]"]), PUMP) is True


def test_evidence_present_and_the_program_absent_is_False():
    """A real read that proves the program was not involved."""
    tx = _tx(instructions=[{"programId": "OtherProgram111"}], logs=["Program Other invoke [1]"])
    assert A._invokes_program(tx, PUMP) is False


def test_no_transaction_at_all_is_None_not_False():
    """THE DEFECT. A read that never happened must not answer the question."""
    assert A._invokes_program(None, PUMP) is None


def test_a_transaction_with_neither_instructions_nor_logs_is_None():
    assert A._invokes_program(_tx(), PUMP) is None


def test_logs_present_but_empty_is_still_None():
    """An empty log list is not evidence of absence when no instructions were parsed either."""
    assert A._invokes_program(_tx(logs=[]), PUMP) is None


def test_instructions_present_and_logs_absent_can_still_prove_absence():
    assert A._invokes_program(_tx(instructions=[{"programId": "Other111"}]), PUMP) is False


def test_an_indexed_instruction_resolves_through_account_keys():
    tx = _tx(instructions=[{"programIdIndex": 1}], keys=["Other111", PUMP])
    assert A._invokes_program(tx, PUMP) is True


# --- the callers decline on BOTH falsey states, and only emit on True ----------------------

@pytest.mark.parametrize("tx", [None, {}, {"transaction": {"message": {}}, "meta": {}}])
def test_curve_resolution_declines_when_there_is_no_evidence(tx):
    assert A.resolve_pump_curve_accounts(tx, "mint111") is None


@pytest.mark.parametrize("tx", [None, {}, {"transaction": {"message": {}}, "meta": {}}])
def test_curve_rows_decline_when_there_is_no_evidence(tx):
    assert A.pump_curve_rows(tx, "mint111", "vault", "owner", slot=1, signature="s") == []


def test_a_proven_absence_also_declines():
    """False and None both decline. The distinction is about what we can claim, not what we do."""
    tx = _tx(instructions=[{"programId": "Other111"}])
    assert A.resolve_pump_curve_accounts(tx, "mint111") is None
    assert A.pump_curve_rows(tx, "mint111", "vault", "owner", slot=1, signature="s") == []
