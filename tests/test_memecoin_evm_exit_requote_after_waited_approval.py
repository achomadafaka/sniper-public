"""A waited exit approval re-quotes before the swap is signed (BUNKER 72163, 2026-10-08).

Chain 1 waits for the exit approval's canonical receipt (17.3 s on BUNKER). The swap was then
signed from the sweep's preview, quoted BEFORE that wait. Its minimum output was 17 s old and the
coin had fallen: an eth_call replay at blocks 26149898..26149902 reverts with
V4TooLittleReceived(min 11446679421073261, got 10819621723950043), 5.5% short. Flashbots
Protect held the reverting swap until its deadline; the re-armed stop sold at -52.6%.

Rule (operator via Alpha, option (b)): wait for the approval's receipt, THEN build and sign the
swap. After a waited approval (True) the executor quotes the same rung again, so the minimum
output and the deadline are fresh. A pipelined approval (a dict: 4663, 56) signs at nonce+1 at
once and keeps its quote; no approval keeps its quote. Chain 1 never pipelines: nothing is signed
through Protect against a pending approval.
"""
import pytest

from sniper.execution.evm_exit_executor import execute_exit_evm, execute_partial_exit_evm
from sniper.persistence import db_memecoin as m
from test_memecoin_evm_exit_executor import _AT_3, _c, _pos, _seams

STALE = {"sellAmount": "10", "buyAmount": "12", "minBuyAmount": "11",
         "transaction": {"to": "0xr", "data": "0xstale"},
         "issues": {"allowance": {"spender": "0xspender"}}}
FRESH = {"sellAmount": "10", "buyAmount": "10", "minBuyAmount": "9",
         "transaction": {"to": "0xr", "data": "0xfresh"},
         "issues": {"allowance": {"spender": "0xspender"}}}
PIPELINED = {"pipelined": True, "nonce": 6, "tx_hash": "0xappr"}


def _run(approve, *, allowance_fn=None, quote_fn=None, ladder=(3,)):
    c = _c(); pos = _pos(c)
    seams, calls = _seams(approve=approve)
    signed, quotes = [], []
    seams["sign_fn"] = lambda tx: signed.append(tx) or ("0xraw", "0xhash", 7)
    seams["quote_fn"] = quote_fn or (lambda *a: quotes.append(a) or FRESH)
    if allowance_fn == "chain":       # 0 until our approval mines, then the amount
        allowance_fn = lambda *_: 10 if calls["approve"] else 0      # noqa: E731
    result = execute_exit_evm(
        c, pos, "b_stop", "2026-10-08T19:53:48+00:00", slippage_ladder=list(ladder),
        prepared_preview={"amount_atomic": 10, "quote": STALE, "quote_evidence": _AT_3},
        allowance_fn=allowance_fn, sleep_fn=lambda _s: None, **seams)
    return c, pos, result, signed, quotes, calls


def test_a_waited_approval_signs_a_fresh_quote_not_the_pre_approval_preview():
    c, pos, result, signed, quotes, calls = _run(True, allowance_fn="chain")
    assert signed == [FRESH["transaction"]], signed
    assert len(quotes) == 1 and quotes[0][2] == 3, quotes       # same rung, same slippage
    assert len(calls["approve"]) == 1
    assert result["status"] == "closed"
    (j,) = c.execute("SELECT intent_json FROM memecoin_exit_intents WHERE position_id=? "
                     "AND status='confirmed'", (pos["id"],)).fetchone()
    assert '"min_output_atomic": 9' in j, j


def test_a_pipelined_approval_keeps_its_quote_and_adds_no_search():
    _c_, _p, result, signed, quotes, _calls = _run(PIPELINED, allowance_fn=lambda *_: 0)
    assert signed == [STALE["transaction"]] and quotes == []
    assert result["status"] == "closed"


def test_no_approval_keeps_the_prepared_quote_and_adds_no_search():
    _c_, _p, result, signed, quotes, calls = _run(True, allowance_fn=lambda *_: 10)
    assert signed == [STALE["transaction"]] and quotes == [] and calls["approve"] == []


def test_an_unreadable_allowance_approves_once_and_re_quotes_once():
    def boom(*_a):
        raise TimeoutError("allowance read")
    _c_, _p, result, signed, quotes, calls = _run(True, allowance_fn=boom)
    assert len(calls["approve"]) == 1 and len(quotes) == 1
    assert signed == [FRESH["transaction"]] and result["status"] == "closed"


def test_a_failed_re_quote_never_signs_the_stale_preview():
    _c_, _p, result, signed, _quotes, _calls = _run(
        True, allowance_fn="chain", quote_fn=lambda *_a: None, ladder=(3, 10))
    assert STALE["transaction"] not in signed and signed == []
    assert result["status"] != "closed"


def test_a_partial_exit_re_quotes_after_a_waited_approval_and_approves_once():
    c = _c(); pos = _pos(c)
    seams, calls = _seams(approve=True)
    seams.pop("quote_fn")
    answers = iter([STALE, FRESH])
    seams["quote_fn"] = lambda mint, toks, slip, fraction: next(answers)
    signed = []
    seams["sign_fn"] = lambda tx: signed.append(tx) or ("0xraw", "0xhash", 7)
    result = execute_partial_exit_evm(c, pos, 0.5, "t0", slippage_ladder=[3],
                                      sleep_fn=lambda _s: None, **seams)
    assert signed == [FRESH["transaction"]], signed
    assert len(calls["approve"]) == 1
    assert result["status"] == "derisked"


def test_a_decision_superseded_during_the_wait_is_refused_not_re_quoted():
    """The redo re-binds the intent. It must come AFTER the post-approval re-verification, or a
    controller change during the 17 s wait would be bound into a fresh intent and sold."""
    c = _c(); pos = _pos(c)
    seams, _calls = _seams()
    quotes, signed = [], []

    def approve(*_a):
        m.persist_controller_state_cas(c, pos["id"], 0, consecutive_break_events=1)
        return True
    seams.update(approve_fn=approve, quote_fn=lambda *a: quotes.append(a) or FRESH,
                 sign_fn=lambda tx: signed.append(tx) or ("0xraw", "0xhash", 7))
    result = execute_exit_evm(c, pos, "b_stop", "t", slippage_ladder=[3],
                              allowance_fn=lambda *_: 0, **seams)
    assert result == {"status": "superseded", "reason": "decision_superseded"}
    assert len(quotes) == 1 and signed == []


def test_a_partial_exit_with_a_pipelined_approval_keeps_its_quote():
    c = _c(); pos = _pos(c)
    seams, _calls = _seams(approve=PIPELINED)
    seams.pop("quote_fn")
    quotes = []
    seams["quote_fn"] = lambda *a: quotes.append(a) or STALE
    signed = []
    seams["sign_fn"] = lambda tx: signed.append(tx) or ("0xraw", "0xhash", 7)
    execute_partial_exit_evm(c, pos, 0.5, "t0", slippage_ladder=[3], sleep_fn=lambda _s: None,
                             **seams)
    assert len(quotes) == 1 and signed == [STALE["transaction"]]


# --- chain 1 never pipelines an approval through Protect --------------------------------------------

def test_chain_1_in_the_approval_pipeline_fails_the_startup_gate():
    from sniper.settings import memecoin_submission as C
    env = {"MEMECOIN_EVM_APPROVAL_PIPELINE": "1", "MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS": "4663,1"}
    with pytest.raises(C.SubmissionConfigError, match="chain 1"):
        C.validate_submission_config(env, funded_evm_chains=("4663", "1"))


def test_chain_1_never_reads_as_pipelined(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    monkeypatch.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "1,4663")
    assert cfg.memecoin_evm_approval_pipeline("1") is False


# --- Gamma (review of ac08bd938): the marker is "not pipelined", never "is exactly True" ------------

@pytest.mark.parametrize("waited", [True, "0xapprovalhash", {"tx_hash": "0xappr"}, 1])
def test_any_approval_that_is_not_a_pipelined_dict_re_quotes(waited):
    """A waited approver that one day answers a truthy non-bool (a hash, a dict without
    `pipelined`) must still re-quote. Testing `is True` would sign the stale quote silently."""
    _c_, _p, result, signed, quotes, _calls = _run(waited, allowance_fn="chain")
    assert signed == [FRESH["transaction"]] and len(quotes) == 1, (waited, signed)


def test_a_partial_exit_re_quotes_after_a_non_bool_waited_approval():
    c = _c(); pos = _pos(c)
    seams, calls = _seams(approve="0xapprovalhash")
    seams.pop("quote_fn")
    answers = iter([STALE, FRESH])
    seams["quote_fn"] = lambda mint, toks, slip, fraction: next(answers)
    signed = []
    seams["sign_fn"] = lambda tx: signed.append(tx) or ("0xraw", "0xhash", 7)
    execute_partial_exit_evm(c, pos, 0.5, "t0", slippage_ladder=[3], sleep_fn=lambda _s: None,
                             **seams)
    assert signed == [FRESH["transaction"]] and len(calls["approve"]) == 1



def test_waited_exit_approval_and_receipt_accounting_have_separate_timing(monkeypatch):
    import json
    from sniper.execution.exit_timing import ExitTiming
    tick = [0.0]
    import sniper.execution.exit_timing as E
    monkeypatch.setattr(E, "ExitTiming", lambda: ExitTiming(clock=lambda: tick[0]))
    c = _c(); pos = _pos(c)
    seams, calls = _seams()
    def approve(*a):
        calls["approve"].append(a)
        tick[0] += 2.0
        return True
    def quote(*a):
        tick[0] += 0.1
        return FRESH
    seams.update(approve_fn=approve, quote_fn=quote)
    def proceeds(h):
        tick[0] += 1.0
        return 15.0
    result = execute_exit_evm(
        c, pos, "real_tp", "t", slippage_ladder=[3],
        allowance_fn=lambda *_: 10 if calls["approve"] else 0,
        receipt_proceeds_fn=proceeds, require_receipt_proceeds=True,
        **seams)
    assert result["status"] == "closed"
    timing = json.loads(m.get_position(c, pos["id"])["exit_timing_json"])
    assert timing["stages_ms"]["approve"] == pytest.approx(2000)
    assert timing["stages_ms"]["quote"] == pytest.approx(200)
    assert timing["stages_ms"]["proceeds"] == pytest.approx(1000)
    assert timing["pre_broadcast_ms"] >= 2200
