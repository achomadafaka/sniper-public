"""The safety sell probe says what it tried (2026-10-07, Alpha P0b (1)+(2)).

Chain 1, notanicecat69 on 0x24c2c6f7, 18:17-18:18Z: five attempts refused at evm.token_safety with
'configured sell venue returned no executable quote (route=direct_v3_single_hop; other venues not
tested)'. The label was a constant from the leg build: the probe actually ran the whole ladder
(v3, v2, v4, v4_multihop), and nothing recorded which amount it tried or why each rung missed. A
read-only repro then found v4_multihop discarding every SELL as 'round_trip_over_budget:None'.

(1) Each sell-probe quote records its amount, wall time, hit and the rungs that ran with their miss
    reason, in the safety timings the evm.token_safety substage already carries.
(2) The refusal names the rungs that actually ran on the last probe. Display only: no verdict, no
    call and no timing changes.
"""
import threading

from sniper.execution import evm_real_seams as S
from sniper.execution.evm_token_safety import build_evm_token_safety
from test_memecoin_evm_token_safety import TAKER, _standard_erc20_http

MINT = "0x" + "41" * 20


def _rung(name, hit=False, *, reason=None, blind=None, skipped=False, ms=12.0):
    rec = {"rung": name, "hit": hit, "skipped": skipped, "unavailable": False,
           "blind_reason": blind, "duration_ms": ms}
    if reason:
        rec["reason"] = reason
    return rec


class _Ladder:
    """The executor's quote seam: records its rungs on the CALLING thread, as the real ladder does
    (on_rung runs on the caller after the rung futures), and hands them back via take()."""

    def __init__(self, answer_for):
        self.answer_for, self.local = answer_for, threading.local()

    def quote(self, sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        quote, rungs = self.answer_for(sell_token, buy_token, int(amount_atomic))
        self.local.rungs = list(getattr(self.local, "rungs", None) or []) + rungs
        return quote

    def take(self):
        rungs = list(getattr(self.local, "rungs", None) or [])
        self.local.rungs = []
        return rungs


def _no_sell(sell_token, buy_token, amount):
    return None, [_rung("v3", reason="no_pool"), _rung("v2"), _rung("kyber", skipped=True),
                  _rung("v4_multihop", reason="round_trip_over_budget:None", ms=295.0)]


def test_a_refused_probe_records_each_amount_tried_and_why_each_rung_missed():
    ladder = _Ladder(_no_sell)
    safety = build_evm_token_safety(chain_id="1", rpc_url="https://rpc", taker=TAKER,
                                    http=_standard_erc20_http(MINT), quote_fn=ladder.quote,
                                    quote_rungs_fn=ladder.take)
    timings = {}
    ok, reason = safety(MINT, timings=timings)
    assert not ok
    probes = timings["sell_probes"]
    assert probes and all(p["hit"] is False for p in probes)
    assert int(probes[0]["amount"]) > 0
    rungs = probes[0]["rungs"]
    assert any(r.startswith("v4_multihop:miss:round_trip_over_budget:None:") for r in rungs), rungs
    assert not any(r.startswith("kyber") for r in rungs), "a skipped rung did not run"
    assert "route=v3+v2+v4_multihop;" in reason, reason


def test_the_label_falls_back_to_the_configured_text_without_a_rung_record():
    ladder = _Ladder(_no_sell)
    safety = build_evm_token_safety(chain_id="1", rpc_url="https://rpc", taker=TAKER,
                                    http=_standard_erc20_http(MINT), quote_fn=ladder.quote,
                                    sell_route_label="direct_v3_single_hop")
    ok, reason = safety(MINT, timings={})
    assert not ok and "route=direct_v3_single_hop;" in reason


def test_a_passing_probe_records_its_hit_and_changes_no_verdict():
    from test_memecoin_evm_token_safety import _standard_quote
    base = _standard_quote(MINT)

    def answer(sell_token, buy_token, amount):
        return base(sell_token, buy_token, amount), [_rung("v3", hit=True)]
    ladder = _Ladder(answer)
    safety = build_evm_token_safety(chain_id="1", rpc_url="https://rpc", taker=TAKER,
                                    http=_standard_erc20_http(MINT), quote_fn=ladder.quote,
                                    quote_rungs_fn=ladder.take)
    timings = {}
    assert safety(MINT, timings=timings) == (True, "ok")
    assert timings["sell_probes"][0]["hit"] is True
    assert timings["sell_probes"][0]["rungs"][0].startswith("v3:hit:")


def test_the_executor_hands_back_only_this_threads_rungs_and_clears_them():
    local = threading.local()
    local.rungs = [{"rung": "v4"}]
    other = []
    t = threading.Thread(target=lambda: other.append(S.take_rungs(local)))
    t.start()
    t.join()
    assert other == [[]], "another thread's rungs are not this probe's"
    assert S.take_rungs(local) == [{"rung": "v4"}]
    assert S.take_rungs(local) == []


def test_the_executor_exposes_the_seam():
    import inspect
    src = inspect.getsource(S.build_real_evm_executor)
    assert '"take_quote_rungs_fn": functools.partial(take_rungs, _preview_trace)' in src


def test_the_substage_keeps_a_non_numeric_timing():
    """The leg rounds every timing; a list there raised inside a bare except and the whole
    evm.token_safety substage was lost."""
    from sniper.services.memecoin_evm_leg import _safety_marks
    assert _safety_marks({"sell_quote_ms": 4820.44, "sell_probes": [{"amount": "1"}]}) == {
        "sell_quote_ms": 4820.4, "sell_probes": [{"amount": "1"}]}


def test_the_five_dollar_buy_legs_rungs_are_not_charged_to_the_sell_probe(monkeypatch):
    """The last probe buys $5 through the same ladder (not through quote_sell); its rungs stay on
    the thread and must not appear in the sell probe that follows."""
    from test_memecoin_evm_token_safety import _standard_quote
    monkeypatch.setattr(S, "usd_to_quote_atomic", lambda chain, usd, http=None: 2 * 10 ** 15)
    base = _standard_quote(MINT)

    def answer(sell_token, buy_token, amount):
        if str(buy_token).lower() == MINT:                      # the $5 BUY leg
            return base(sell_token, buy_token, amount), [_rung("v4_multihop", hit=True)]
        return _no_sell(sell_token, buy_token, amount)
    ladder = _Ladder(answer)
    safety = build_evm_token_safety(chain_id="1", rpc_url="https://rpc", taker=TAKER,
                                    http=_standard_erc20_http(MINT), quote_fn=ladder.quote,
                                    quote_rungs_fn=ladder.take)
    timings = {}
    ok, _reason = safety(MINT, timings=timings)
    assert not ok
    assert len(timings["sell_probes"]) == 2, timings["sell_probes"]     # 1 token, then the $5 sell
    last = timings["sell_probes"][-1]
    assert not any(r.startswith("v4_multihop:hit") for r in last["rungs"]), last
