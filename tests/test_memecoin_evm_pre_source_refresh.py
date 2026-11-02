"""D-c (2026-10-16, Alpha; MEMECOIN_EVM_REFRESH_PRE_SOURCE_STATE): a winning quote priced on state
from a block BEFORE the source's own trade is re-asked before the preflight, however young it is.

CLAWDMKT 0x9f69a7ea…3a69 on 4663 (11:21:25Z, source block 90038422): the Pons counter cache served a
quote from an older block, 2.8% under the source's average fill. It was 561 ms old, under the 750 ms
refresh age, so it went to the preflight unrefreshed, reverted there, and the re-quote read +25.5%.
Pins: the trigger is `route.state_block` < the source block (the event's, else the J7 receipt read
already in hand); an unknown block on either side changes nothing; the re-ask is the existing
one-rung refresh, re-gated as every refresh is; a replacement still before the source is used and
named; the kill knob restores age-only refreshes; the leg hands the event's block only to an
executor that names it."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from sniper.execution.evm_swap_executor import execute_entry_evm  # noqa: E402
from sniper.services import memecoin_evm_leg as leg  # noqa: E402
from test_memecoin_evm_entry_executor import _TraceRecorder, _aged_quote, _reserve, _seams  # noqa: E402
from test_memecoin_evm_entry_executor import _c as _ledger  # noqa: E402
from test_memecoin_evm_leg import _Ev  # noqa: E402
from test_memecoin_evm_leg import _c as _leg_ledger  # noqa: E402

SOURCE = 90038422


def _pons(state_block, data):
    return {"sellAmount": "10000000", "buyAmount": "5",
            "transaction": {"to": "0xrouter", "data": data}, "issues": {},
            "route": {"protocol": "pons_v2_counter", "state_block": state_block}}


def _young(state_block, fresh):
    tries = []

    def requote(slippage_bps=None):
        tries.append(1)
        return fresh
    quote = _aged_quote(0.05, requote)                       # well under the 750 ms age
    quote["_sniper_winner"] = "pons_v2"
    quote["route"] = {"protocol": "pons_v2_counter", "state_block": state_block}
    return quote, tries


def _run(quote, **kwargs):
    c = _ledger(); pid = _reserve(c, network_id="4663")
    seams, calls = _seams(quote=quote, token=((0.0, 6), (5.0, 6)))
    seen, trace = [], _TraceRecorder()
    result = execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
        strategy="alpha_b", source_user_id="trader1", now_iso="2026-07-09T00:00:01Z",
        entry_preflight_fn=lambda q: seen.append(q["transaction"]["data"]) or (True, "ok"),
        trace=trace, **seams, **kwargs)
    refresh = [meta for name, meta in trace.substages if name == "evm.winner_refresh"]
    return result, seen, refresh


class _Ref:
    """A chase reference whose receipt read has (or has not) finished."""
    def __init__(self, post):
        self._post = post

    def payload(self):
        return self._post

    def resolve(self, avg_price, our_pool):
        return avg_price, "avg_fill", None


def test_a_young_quote_priced_before_the_source_is_re_asked_and_named():
    quote, tries = _young(SOURCE - 9, _pons(SOURCE + 3, "0xfresh"))
    result, seen, refresh = _run(quote, source_block=SOURCE)
    assert result["status"] == "filled" and len(tries) == 1
    assert seen == ["0xfresh"], "the preflight must judge the post-source quote"
    assert refresh[0]["outcome"] == "refreshed" and refresh[0]["trigger"] == "state_before_source"
    assert (refresh[0]["state_block"], refresh[0]["source_block"], refresh[0]["fresh_state_block"],
            refresh[0]["still_before_source"]) == (SOURCE - 9, SOURCE, SOURCE + 3, False)


@pytest.mark.parametrize("state_block,source_block", [
    (SOURCE, SOURCE),          # read at the source's own block: its trade is in the state
    (SOURCE + 1, SOURCE),
    (SOURCE - 9, None),        # no source block known: absent, not a trigger
    (None, SOURCE),            # a rung that pins no block
    (SOURCE - 9, True),        # a bool is not a block
    (SOURCE - 9, 0),
    (SOURCE - 9, str(SOURCE)),  # a string is not a block either, and must not raise
])
def test_no_pre_source_state_means_no_new_call(state_block, source_block):
    quote, tries = _young(state_block, _pons(SOURCE + 3, "0xfresh"))
    if state_block is None:
        quote["route"] = {"protocol": "v4"}
    result, seen, refresh = _run(quote, source_block=source_block)
    assert result["status"] == "filled" and tries == [] and seen == ["0xone"]
    assert refresh[0]["outcome"] == "fresh" and "trigger" not in refresh[0]


def test_the_j7_lane_uses_its_receipt_read_when_it_has_finished():
    seen_block = {"source_receipt_observation": {
        "network_id": "4663", "source_tx": "0xsrc", "source_block_number": SOURCE}}
    quote, tries = _young(SOURCE - 2, _pons(SOURCE, "0xfresh"))
    result, seen, refresh = _run(quote, chase_reference=_Ref(seen_block))
    assert len(tries) == 1 and seen == ["0xfresh"] and refresh[0]["source_block"] == SOURCE
    # Pending (payload None) or another chain's observation: no source block, no trigger.
    for post in (None, {"source_receipt_observation": dict(seen_block["source_receipt_observation"],
                                                           network_id="8453")}):
        quote, tries = _young(SOURCE - 2, _pons(SOURCE, "0xfresh"))
        _run(quote, chase_reference=_Ref(post))
        assert tries == []


def test_a_replacement_still_before_the_source_is_used_and_named():
    quote, tries = _young(SOURCE - 9, _pons(SOURCE - 1, "0xnewer"))
    result, seen, refresh = _run(quote, source_block=SOURCE)
    assert result["status"] == "filled" and seen == ["0xnewer"]
    assert refresh[0]["still_before_source"] is True and refresh[0]["fresh_state_block"] == SOURCE - 1


def test_a_refresh_that_answers_nothing_proceeds_on_the_original_and_is_named():
    quote, tries = _young(SOURCE - 9, None)
    result, seen, refresh = _run(quote, source_block=SOURCE)
    assert result["status"] == "filled" and len(tries) == 2 and seen == ["0xone"]
    assert refresh[0]["outcome"] == "unavailable" and refresh[0]["trigger"] == "state_before_source"


def test_the_kill_knob_restores_age_only_refreshes(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_REFRESH_PRE_SOURCE_STATE", "0")
    quote, tries = _young(SOURCE - 9, _pons(SOURCE + 3, "0xfresh"))
    result, seen, refresh = _run(quote, source_block=SOURCE)
    assert tries == [] and seen == ["0xone"] and refresh[0]["outcome"] == "fresh"


@pytest.mark.parametrize("names_it", [True, False])
def test_the_leg_hands_the_events_block_only_to_an_executor_that_names_it(monkeypatch, tmp_path,
                                                                          names_it):
    c, kill = _leg_ledger(monkeypatch, tmp_path)
    got = {}
    if names_it:
        def execute_fn(conn, pid, mint, size, now_iso, source_block=None):
            got["source_block"] = source_block
            return {"status": "filled", "tx_hash": "0xh", "position_id": pid}
    else:
        def execute_fn(conn, pid, mint, size, now_iso, **kwargs):
            got.update(kwargs)
            return {"status": "filled", "tx_hash": "0xh", "position_id": pid}
    ev = _Ev()
    ev.source_block_number = SOURCE
    leg.evm_real_entry(c, ev, route="evm:8453", day="2026-07-09", usdc_balance=300.0,
                       native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
                       token_safety_fn=lambda mint, **kw: (True, "ok"), execute_fn=execute_fn,
                       kill_path=kill)
    assert got.get("source_block") == (SOURCE if names_it else None)
