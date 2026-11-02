"""Post-approval re-quote through the winning rung only (2026-09-28, Alpha GO, flag default OFF).

After a confirmed exact-amount approval, entry step 3a re-quoted through `quote_fn`, which is
`quote_at_snapshot` -> `_venue_quote_combined`: every rung again, with the measured ladder's post-hit
grace. Since 09-06 that re-quote took p50 1565 / p90 2551 ms on Base (n=3) against the winning rung's
own p50 533 / p90 848 ms, and p50 419 / p90 1816 ms on BSC (n=39) against p50 642 / p90 941 ms.

MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE=1 re-asks only the winner through the quote's own
`_requote` (same amount_atomic, i.e. exactly the approved amount; same slippage). A None, an
exception or a mismatched answer falls back to today's ladder call. Every gate after 3a runs on the
fresh quote either way. Flag 0 (the default) is today's path exactly. The trace mark names the path
and rung regardless of the flag.
"""
import pytest

from sniper.execution.evm_swap_executor import execute_entry_evm
from sniper.settings import memecoin as cfg
from test_memecoin_evm_entry_executor import _QUOTE, _TraceRecorder, _c, _reserve, _seams

FLAG = "MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE"


def _run(monkeypatch, *, flag, requote_result=None, requote_raises=False, winner="v3",
         ladder_quote=None, **kw):
    monkeypatch.setattr(cfg, FLAG, flag, raising=False)
    c = _c(); pid = _reserve(c)
    seen = {"requote": [], "ladder": []}

    def requote(slippage_bps=None):
        seen["requote"].append(slippage_bps)
        if requote_raises:
            raise RuntimeError("rung down")
        return requote_result

    initial = {**_QUOTE, "_requote": requote, "_sniper_winner": winner}
    fresh_ladder = ladder_quote if ladder_quote is not None else {**_QUOTE, "_sniper_winner": "v2"}
    seams, calls = _seams(quote=initial)
    answers = iter([initial, fresh_ladder])

    def quote_fn(*args, **kwargs):
        seen["ladder"].append((args, kwargs))
        return next(answers)
    seams["quote_fn"] = quote_fn
    trace = _TraceRecorder()
    result = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="8453", usd_amount=10.0,
                               strategy="alpha_b", source_user_id="trader1",
                               now_iso="2026-07-09T00:00:01Z", trace=trace, **seams, **kw)
    marks = [md for name, md in trace.substages if name == "evm.quote_post_approval"]
    return result, seen, calls, marks


def test_flag_off_is_todays_ladder_call_byte_for_byte(monkeypatch):
    result, seen, calls, marks = _run(monkeypatch, flag=False, requote_result={**_QUOTE})
    assert result["status"] == "filled"
    assert seen["requote"] == [], "the winner-only path never runs with the flag off"
    assert seen["ladder"] == [(("0xtok", 10.0), {}), (("0xtok", 10.0), {})]
    assert marks and marks[0]["path"] == "ladder" and marks[0]["rung"] == "v2"


def test_flag_on_re_asks_only_the_winner(monkeypatch):
    fresh = {**_QUOTE, "_sniper_winner": "v3"}
    result, seen, calls, marks = _run(monkeypatch, flag=True, requote_result=fresh)
    assert result["status"] == "filled"
    assert seen["requote"] == [None] and len(seen["ladder"]) == 1, "no second ladder"
    assert calls["approve"] == [("0xspender", 10000000)] and calls["sign"] == 1
    assert marks[0]["path"] == "winner" and marks[0]["rung"] == "v3"


@pytest.mark.parametrize("case", ["none", "raises", "other_spender", "other_amount"])
def test_flag_on_falls_back_to_the_ladder(monkeypatch, case):
    bad = {"none": None, "raises": None,
           "other_spender": {**_QUOTE, "issues": {"allowance": {"spender": "0xother"}}},
           "other_amount": {**_QUOTE, "sellAmount": "9999999"}}[case]
    result, seen, calls, marks = _run(monkeypatch, flag=True, requote_result=bad,
                                      requote_raises=(case == "raises"))
    assert result["status"] == "filled"
    assert seen["requote"] == [None] and len(seen["ladder"]) == 2
    assert marks[0]["path"] == "ladder"


def test_the_post_approval_gates_still_run_on_the_winner_quote(monkeypatch):
    """The chase cap is re-checked on the winner's fresh quote exactly as on a ladder answer:
    the initial quote is at the source price, the winner's fresh one is +70% (cap 40%)."""
    monkeypatch.setattr(cfg, FLAG, True, raising=False)
    from sniper.persistence import db_memecoin as m
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="4663",
                            source_price=1.0)
    fresh = {**_QUOTE, "buyAmount": "5882353", "_sniper_winner": "v3"}
    used = []
    initial = {**_QUOTE, "buyAmount": "10000000", "_sniper_winner": "v3",
               "_requote": lambda slippage_bps=None: used.append(1) or fresh}
    seams, calls = _seams(quote=initial, token=((0.0, 6), (0.0, 6)))
    result = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
                               strategy="alpha_b", source_user_id="trader1",
                               now_iso="2026-07-09T00:00:00Z", chase_max_pct=40.0, **seams)
    assert used == [1], "the winner answered the post-approval re-quote"
    assert result["status"] == "skipped_chase" and "cap 40%" in result["reason"]
    assert calls["sign"] == 0 and calls["send"] == 0


def test_the_flag_defaults_off(monkeypatch):
    import importlib
    monkeypatch.delenv(FLAG, raising=False)
    assert importlib.reload(cfg).MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE is False
    monkeypatch.setenv(FLAG, "1")
    assert importlib.reload(cfg).MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE is True
    monkeypatch.delenv(FLAG)
    importlib.reload(cfg)
