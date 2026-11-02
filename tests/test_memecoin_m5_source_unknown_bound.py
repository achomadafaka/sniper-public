"""A J7 signal (no source tx) gets the tape's m5 total, less the source's paid USD, as its m5
floor lower bound (spec 2026-10-15-memecoin-m5-provider-lag-design.md option (d), operator yes via
Alpha). a3211624: provider m5 $305 against a $414.79 floor, while the chain carried $7,335 in the
same five minutes. Knob MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND, default off. A blind tape keeps
today's provider decision; the bound only ever admits."""
import math
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_real_runner as runner
from sniper.services import memecoin_volume_writer as vw
from sniper.settings import memecoin as cfg

MINT = "0x430bc084381737a3d1a41edf7fda80a191ea2567"
TAPE = {"usd": 7335.36, "reason": "ok", "coverage": "partial", "swaps": 49,
        "pools": [{"pool": "0x801d", "txns": 49}], "measured_at": 1000.0}
UNKNOWN = {"usd": None, "reason": "source_tx_unknown", "pools": [], "coverage": "unknown"}


@pytest.fixture
def tape(monkeypatch):
    calls = []

    def read(chain, mint, *, exclude_tx=None, source_tokens=None):
        calls.append(dict(chain=chain, mint=mint, exclude_tx=exclude_tx, source_tokens=source_tokens))
        return dict(answer[0])
    answer = [TAPE]
    monkeypatch.setattr(vw, "read_for_gate", read)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS", {"4663", "8453"}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND", True, raising=False)
    return calls, answer


def test_a_j7_signal_gets_the_tape_total_less_the_source_as_its_bound(tape):
    calls, _ = tape
    got = vw.gate_evidence("4663", MINT, "swap:ef149840", source_usd=100.0)
    assert got["usd"] == pytest.approx(7235.36) and got["reason"] == "ok"
    assert got["basis"] == "source_unknown_tape_bound" and got["tape_total_usd"] == 7335.36
    assert got["source_subtracted_usd"] == 100.0
    assert calls == [dict(chain="4663", mint=MINT, exclude_tx=None, source_tokens=None)]


def test_knob_off_is_todays_unknown_with_no_read(tape, monkeypatch):
    calls, _ = tape
    monkeypatch.setattr(cfg, "MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND", False, raising=False)
    assert vw.gate_evidence("4663", MINT, "swap:ef149840", source_usd=100.0) == UNKNOWN
    assert calls == []


@pytest.mark.parametrize("source_usd", [None, float("nan"), -1.0, float("inf"), True])
def test_an_unknown_source_size_gives_no_bound(tape, source_usd):
    assert vw.gate_evidence("4663", MINT, "swap:ef149840", source_usd=source_usd) == UNKNOWN


@pytest.mark.parametrize("blind", [dict(usd=None, reason="feed_stale"),
                                   dict(usd=None, reason="protocol_lagging"),
                                   dict(usd=None, reason="volume_store_unavailable")])
def test_a_blind_tape_keeps_todays_provider_decision(tape, blind):
    # Not blind on purpose: a blind reason here would make the J7 floor undecidable (fail open).
    _, answer = tape
    answer[0] = dict(blind, pools=[], coverage="unknown")
    assert vw.gate_evidence("4663", MINT, "swap:ef149840", source_usd=100.0) == UNKNOWN


def test_the_bound_never_goes_below_zero_and_counts_the_source_in_full(tape):
    _, answer = tape
    answer[0] = dict(TAPE, usd=100.0)
    assert vw.gate_evidence("4663", MINT, "swap:ef149840", source_usd=100.0)["usd"] == 0.0
    answer[0] = dict(TAPE, usd=40.0)
    assert vw.gate_evidence("4663", MINT, "swap:ef149840", source_usd=100.0)["usd"] == 0.0


def test_a_known_source_tx_is_unchanged(tape):
    calls, _ = tape
    tx = "0x" + "83" * 32
    vw.gate_evidence("4663", MINT, "onchain:4663:%s:%s" % (tx, MINT), source_usd=100.0)
    assert calls[0]["exclude_tx"] == tx


def test_the_bound_never_proves_a_trade_count(tape):
    got = vw.gate_evidence("4663", MINT, "swap:ef149840", source_usd=100.0)
    assert runner.onchain_m5_txns_lower_bound(got) is None        # the source may be one of them
    assert runner.onchain_h1_lower_bound(got) == pytest.approx(7235.36)


# --- the entry gate's read ---------------------------------------------------------------------

def _ev(**kw):
    return SimpleNamespace(mint=MINT, fomo_event_id="swap:ef149840", their_size_usd=100.0,
                           their_price=1e-5, source_evidence={}, **kw)


@pytest.mark.parametrize("excluded, size, want", [
    (False, 100.0, 0.0),            # exclusion off: the gate excludes nothing anywhere, full total
    (True, 100.0, 100.0),
    (True, None, None),             # size unknown: no bound
    (True, 0.0, None),
    (True, float("nan"), None),
])
def test_the_source_size_for_the_bound_follows_the_gates_own_exclusion(monkeypatch, excluded, size, want):
    # Gamma LOW on 308aa8c3e: the exclusion-off rule was unpinned.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", excluded, raising=False)
    assert runner._source_usd_for_bound(SimpleNamespace(their_size_usd=size)) == want


ADMIT, REFUSE = (True, 414.79, "ok"), (False, 414.79, "m5_turnover_under_floor")


def _redecide(first, evidence=UNKNOWN, decide=None):
    kwargs = {"m5_floor_lower_bound": None, "h1_floor_lower_bound": None}
    seen = []

    def again():
        seen.append(dict(kwargs))
        return decide() if decide else (True, 414.79, "ok")
    got = runner._m5_redecide_with_source_unknown_bound(first, _ev(), "4663", {}, evidence, kwargs, again)
    return got, seen, kwargs


def test_no_tape_read_when_the_provider_admits(tape, monkeypatch):
    # Alpha (CLAUDE.md rule 2): an admit pays nothing; only a would-be refusal reads the tape.
    calls, _ = tape
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", True, raising=False)
    (decision, bound), seen, _kw = _redecide(ADMIT)
    assert decision == ADMIT and bound is None and seen == [] and calls == []


def test_a_would_be_refusal_reads_the_bound_and_decides_again(tape, monkeypatch):
    calls, _ = tape
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", True, raising=False)
    (decision, bound), seen, kwargs = _redecide(REFUSE)
    assert decision == (True, 414.79, "ok") and bound["usd"] == pytest.approx(7235.36)
    assert seen[0]["m5_floor_lower_bound"] == pytest.approx(7235.36)
    assert seen[0]["h1_floor_lower_bound"] == pytest.approx(7235.36)
    assert len(calls) == 1 and calls[0]["exclude_tx"] is None


@pytest.mark.parametrize("why", ["knob_off", "known_tx", "blind_tape", "no_size"])
def test_otherwise_the_refusal_stands_unread_or_unchanged(tape, monkeypatch, why):
    calls, answer = tape
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", True, raising=False)
    evidence = UNKNOWN
    ev = _ev()
    if why == "knob_off":
        monkeypatch.setattr(cfg, "MEMECOIN_M5_SOURCE_UNKNOWN_TAPE_BOUND", False, raising=False)
    elif why == "known_tx":
        evidence = dict(TAPE, usd=50.0)                 # an on-chain lane's own bound already ran
    elif why == "blind_tape":
        answer[0] = dict(usd=None, reason="feed_stale", pools=[], coverage="unknown")
    else:
        ev.their_size_usd = None
    kwargs = {"m5_floor_lower_bound": None}
    got = runner._m5_redecide_with_source_unknown_bound(
        REFUSE, ev, "4663", {}, evidence, kwargs, lambda: pytest.fail("decided again"))
    assert got == (REFUSE, None) and kwargs == {"m5_floor_lower_bound": None}
    assert len(calls) == (1 if why == "blind_tape" else 0)


def test_a3211624_passes_the_m5_floor_with_the_bound_and_refuses_without():
    kwargs = dict(abs_floor=0.0, h1_floor_lower_bound=None, index_lag_s=15.0, m5_abs_floor_usd=0.0,
                  m5_band_max_mcap=0.0, m5_ceiling=2.0, m5_floor=0.02, m5_floor_over_band=0.0,
                  m5_required=False, m5_txns=16, m5_txns_floor=0, m5_txns_lower_bound=None,
                  m5_txns_required=False, pair_age_s=185042.1, signal_usd=100.0, source_age_s=3.1,
                  turnover_floor=0.0, vol_m5=305.02)
    refused = runner.volume_alive_decision(305.02, 20739.5, m5_floor_lower_bound=None, **kwargs)
    assert refused[0] is False and refused[2] == "m5_turnover_under_floor"
    allowed = runner.volume_alive_decision(305.02, 20739.5, m5_floor_lower_bound=7235.36, **kwargs)
    assert allowed[0] is True
