"""On-chain lane latency cut C1 (spec docs/superpowers/specs/2026-10-01-memecoin-onchain-lane-
latency-design.md; operator order 2026-10-01, LILMET 68051).

C1: an on-chain lane event carries no market cap, so the early quote was skipped and the quote waited
for the market-data GET. With MEMECOIN_ENTRY_EARLY_QUOTE_ONCHAIN an unknown mcap passes the pre-screen
for signal_source 'onchain_watch' only; a J7 event with an unknown mcap still skips.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_early_quote as EQ
from sniper.settings import memecoin as cfg


# --- C1 -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("source,mcap,knob,expected", [
    ("onchain_watch", None, True, None),               # the cut
    ("onchain_watch", None, False, "mcap_unknown"),    # knob off: today
    ("j7", None, True, "mcap_unknown"),                # scope is in code, not only the knob
    ("onchain_watch", 0.0, True, None),
    ("onchain_watch", 2e6, True, "mcap_over_ceiling"),  # a known mcap is judged as today
])
def test_c1_prescreen_passes_an_unknown_mcap_for_the_onchain_lane_only(monkeypatch, source, mcap,
                                                                      knob, expected):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE_ONCHAIN", knob, raising=False)
    ev = SimpleNamespace(signal_source=source, their_market_cap=mcap)
    assert EQ.cheap_prescreen(ev, quiet_hours=False, mcap_ceiling=1e6) == expected


def _hook(monkeypatch, tmp_path, *, knob, source):
    from test_memecoin_entry_early_quote import _hook as hook
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_QUOTE_ONCHAIN", knob, raising=False)
    from test_memecoin_entry_identity import _event
    import test_memecoin_entry_identity as ident

    def onchain_event(**kw):
        ev = _event(**kw)
        ev.signal_source = source
        ev.their_market_cap = None                   # OnchainEv carries no market cap
        return ev
    monkeypatch.setattr(ident, "_event", onchain_event)
    t, seen, trace = hook(monkeypatch, tmp_path, flag=True)
    return t, seen, trace


def test_c1_an_onchain_lane_decision_quotes_inside_the_market_data_get(monkeypatch, tmp_path):
    t, seen, _trace = _hook(monkeypatch, tmp_path, knob=True, source="onchain_watch")
    early = seen["ev"].entry_early_quote
    assert early is not None
    early._thread.join(5)
    assert early.refusal is None, early.refusal
    assert t["quote_start"] < t["get_end"], "the quote runs beside the GET, not after it"


@pytest.mark.parametrize("knob,source", [(False, "onchain_watch"), (True, "j7")])
def test_c1_knob_off_or_a_j7_unknown_mcap_starts_nothing(monkeypatch, tmp_path, knob, source):
    t, seen, _trace = _hook(monkeypatch, tmp_path, knob=knob, source=source)
    assert getattr(seen["ev"], "entry_early_quote", None) is None and "quote_start" not in t


def test_c1_every_attempt_records_whether_the_early_quote_ran_and_how_long_the_executor_waited():
    marks = []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: marks.append((name, kw)))
    started = threading.Event()

    def slow_quote(mint, clip):
        started.set()
        time.sleep(0.15)
        return {"outAmount": "1"}
    early = EQ.EarlyEntryQuote(mint="M", quote_fn=slow_quote, sizing_fn=lambda: (25.0, None),
                               trace=trace)
    started.wait(2)
    assert early.take(25.0, max_wait_s=2.0) == {"outAmount": "1"}
    (name, kw), = marks
    assert name == "early_quote" and kw["used"] is True
    assert 50.0 < kw["wait_ms"] < 2000.0, kw         # quote_wait_ms: the executor blocked here
