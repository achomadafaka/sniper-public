"""Big-cap ceiling lift (operator GO via Alpha, 2026-10-06; Gamma ~/.cache/gamma/bigcap REPORT.md).

A would-be `over_ceiling` refusal on a listed chain is admitted up to MEMECOIN_REAL_BIGCAP_LIFT_MAX_MCAP_USD
($500K) only when the source buy is >= $2.5K AND our OWN 5-minute volume (the on-chain tape,
memecoin_volume_writer.gate_evidence) is >= 10% of the mcap. Unknown own volume never lifts. A
per-wallet mcap ceiling is never raised. No market-data provider is asked.
"""
import math
import sqlite3
import types

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as RR
from sniper.services import memecoin_volume_writer as VW
from sniper.settings import memecoin as cfg

MINT = "0x" + "ab" * 20


class Trace:
    def __init__(self):
        self.subs = []

    def mark_substage(self, name, **kw):
        self.subs.append((name, kw))

    def mark(self, *a, **k):
        pass


def _ev(network_id="4663", src=3000.0, uid="u", mint=MINT, mc=None):
    return types.SimpleNamespace(side="buy", source_user_id=uid, their_size_usd=src, mint=mint,
                                 network_id=network_id, fomo_event_id="0xsrc", their_market_cap=mc)


def _evidence(usd, **kw):
    return {"usd": usd, "coverage": "partial", "covered_s": 300.0, "newest_block_age_s": 0.4,
            "measured_at": 1_000.0, "reason": None if usd is not None else "feed_stale", **kw}


def _lift(ev, mcap=400_000.0, ceiling=250_000.0, evidence=None, calls=None, trace=None):
    def fn(chain, mint, origin, source_tokens=None):
        if calls is not None:
            calls.append((chain, mint, origin))
        return evidence
    return RR._bigcap_lift(ev, ev.network_id, mcap, ceiling, trace=trace, evidence_fn=fn)


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BIGCAP_LIFT_CHAINS", "4663,8453")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BIGCAP_LIFT_MAX_MCAP_USD", 500_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BIGCAP_LIFT_MIN_SOURCE_USD", 2_500.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BIGCAP_LIFT_MIN_V5_MCAP_RATIO", 0.10)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {})


def test_the_code_default_lists_4663_and_8453_only():
    import importlib
    import os
    saved = os.environ.pop("MEMECOIN_REAL_BIGCAP_LIFT_CHAINS", None)
    try:
        fresh = importlib.reload(cfg)
        assert fresh.MEMECOIN_REAL_BIGCAP_LIFT_CHAINS == "4663,8453"
        assert fresh.MEMECOIN_REAL_BIGCAP_LIFT_MAX_MCAP_USD == 500_000.0
        assert fresh.MEMECOIN_REAL_BIGCAP_LIFT_MIN_SOURCE_USD == 2_500.0
        assert fresh.MEMECOIN_REAL_BIGCAP_LIFT_MIN_V5_MCAP_RATIO == 0.10
    finally:
        if saved is not None:
            os.environ["MEMECOIN_REAL_BIGCAP_LIFT_CHAINS"] = saved
        importlib.reload(cfg)


@pytest.mark.parametrize("chain", ["4663", "8453"])
def test_own_volume_at_ten_percent_lifts_the_global_ceiling(chain):
    trace, calls = Trace(), []
    assert _lift(_ev(chain), evidence=_evidence(40_000.0), calls=calls, trace=trace) == 500_000.0
    assert calls == [(chain, MINT, "0xsrc")]
    (name, rec), = trace.subs
    assert name == "bigcap_lift" and rec["decision"] == "lifted"
    assert rec["own_v5"] == 40_000.0 and rec["v5_source"] == "onchain_m5:partial"
    assert rec["v5_age_s"] == 0.4 and rec["v5_measured_at"] == 1_000.0 and rec["v5_covered_s"] == 300.0
    assert rec["mcap"] == 400_000.0 and rec["source_usd"] == 3000.0


def test_absent_own_volume_never_lifts():
    """Leave-behind (Alpha 2026-10-06): an unknown own m5 is absent, never zero and never a pass.
    The recorded reason names the None guard, so removing the guard fails this test."""
    trace = Trace()
    assert _lift(_ev(), evidence=_evidence(None), trace=trace) is None
    (_, rec), = trace.subs
    assert rec["decision"] == "not_lifted" and rec["why"] == "own_v5_unknown"
    assert rec["v5_reason"] == "feed_stale"
    for missing in (None, {}):
        t2 = Trace()
        assert _lift(_ev(), evidence=missing, trace=t2) is None
        assert t2.subs[0][1]["why"] == "own_v5_unknown"


@pytest.mark.parametrize("usd, why", [(0.0, "own_v5_under_ratio"), (39_999.0, "own_v5_under_ratio"),
                                      (float("nan"), "own_v5_unknown"), ("x", "own_v5_unknown"),
                                      (-5.0, "own_v5_unknown")])
def test_a_known_low_or_unusable_volume_does_not_lift(usd, why):
    trace = Trace()
    assert _lift(_ev(), evidence=_evidence(usd), trace=trace) is None
    assert trace.subs[0][1]["why"] == why


@pytest.mark.parametrize("src", [2_499.0, 0.0, None, float("nan"), "x"])
def test_a_small_or_unknown_source_buy_never_reads_the_tape(src):
    calls, trace = [], Trace()
    assert _lift(_ev(src=src), evidence=_evidence(90_000.0), calls=calls, trace=trace) is None
    assert calls == [] and trace.subs[0][1]["why"] == "source_under_min"


@pytest.mark.parametrize("mcap, why", [(500_001.0, "mcap_over_lift_max"), (None, "mcap_unknown"),
                                       (float("inf"), "mcap_unknown")])
def test_the_lift_band_is_bounded(mcap, why):
    calls, trace = [], Trace()
    assert _lift(_ev(), mcap=mcap, evidence=_evidence(90_000.0), calls=calls, trace=trace) is None
    assert calls == [] and trace.subs[0][1]["why"] == why


@pytest.mark.parametrize("chain", ["56", "solana", "1"])
def test_an_unlisted_chain_keeps_todays_ceiling_and_reads_nothing(chain):
    calls, trace = [], Trace()
    assert _lift(_ev(chain), evidence=_evidence(90_000.0), calls=calls, trace=trace) is None
    assert calls == [] and trace.subs == []


@pytest.mark.parametrize("override", [600_000.0, 300_000.0])
def test_a_per_wallet_mcap_ceiling_is_never_raised(monkeypatch, override):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {"4663:u": override})
    calls, trace = [], Trace()
    assert _lift(_ev(), ceiling=override, evidence=_evidence(90_000.0), calls=calls, trace=trace) is None
    assert calls == [] and trace.subs[0][1]["why"] == "wallet_ceiling"


def test_a_lift_max_not_above_the_ceiling_is_a_no_op(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BIGCAP_LIFT_MAX_MCAP_USD", 250_000.0)
    trace = Trace()
    assert _lift(_ev(), evidence=_evidence(90_000.0), trace=trace) is None
    assert trace.subs[0][1]["why"] == "lift_not_above_ceiling"


def test_a_tape_read_failure_is_no_lift():
    def boom(chain, mint, origin, source_tokens=None):
        raise RuntimeError("store locked")
    trace = Trace()
    assert RR._bigcap_lift(_ev(), "4663", 400_000.0, 250_000.0, trace=trace, evidence_fn=boom) is None
    assert trace.subs[0][1]["why"] == "own_v5_error"


def test_the_volume_gate_reuses_a_fresh_lift_read_and_rereads_a_stale_one(monkeypatch):
    calls = []
    monkeypatch.setattr(VW, "gate_evidence", lambda *a, **k: calls.append(a) or _evidence(1.0))
    ev = _ev()
    memo = dict(_evidence(40_000.0), measured_at=10_000.0)
    ev._bigcap_own_m5 = ("4663", memo)
    monkeypatch.setattr(RR, "_real_now_s", lambda: 10_002.0)
    assert RR._own_m5_for_gate(ev, "4663", {}) is memo and calls == []
    monkeypatch.setattr(RR, "_real_now_s", lambda: 10_006.0)              # over 5 s: re-read
    assert RR._own_m5_for_gate(ev, "4663", {})["usd"] == 1.0 and len(calls) == 1
    monkeypatch.setattr(RR, "_real_now_s", lambda: 10_002.0)
    assert RR._own_m5_for_gate(ev, "8453", {})["usd"] == 1.0 and len(calls) == 2   # other chain


# ------------------------------------------------------------------ through the funded hook

def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


_VENUES = {"solana": object(), "4663": object(), "8453": object()}


def _hook(c, monkeypatch, evidence):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(VW, "gate_evidence", lambda *a, **k: evidence)
    m.add_wallet(c, "u", "tq")
    m.set_wallet_real_approved(c, "u", True, chains=["solana", "8453", "4663"], force=True)
    monkeypatch.setattr(RR, "real_entry_hook", lambda *a, **k: {"status": "filled"})
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: None)
    return RR.make_real_hook(None, _VENUES, now_fn=lambda: "t", trade_size=10.0, max_age_s=None)


def test_the_hook_admits_a_lifted_coin(monkeypatch):
    c = _conn()
    assert _hook(c, monkeypatch, _evidence(45_000.0))(c, _ev(mc=400_000.0))["status"] == "filled"


def test_the_hook_keeps_refusing_when_own_volume_is_absent(monkeypatch):
    c = _conn()
    res = _hook(c, monkeypatch, _evidence(None))(c, _ev(mc=400_000.0))
    assert res["status"] == "skipped_mcap"
    assert res["reason"] == "mcap $400,000 over ceiling $250,000 (over_ceiling)"


def test_the_hook_keeps_refusing_above_the_lift_band(monkeypatch):
    c = _conn()
    res = _hook(c, monkeypatch, _evidence(900_000.0))(c, _ev(mc=600_000.0))
    assert res["status"] == "skipped_mcap" and "over ceiling $250,000" in res["reason"]


def test_the_priced_history_mcap_is_rechecked_at_the_lifted_bar(monkeypatch):
    # Gamma LOW on 501611e4b: event mcap $400K lifts, but the priced-history shadow reads $600K, over
    # the $500K lifted ceiling, so the coin is still refused and the render names the bar that applied.
    c = _conn()
    hook = _hook(c, monkeypatch, _evidence(90_000.0))
    monkeypatch.setattr(cfg, "MEMECOIN_MCAP_HISTORY_PRICED", True)
    monkeypatch.setattr(RR, "_priced_history_mcap", lambda *a, **k: 600_000.0)
    res = hook(c, _ev(mc=400_000.0))
    assert res["status"] == "skipped_mcap"
    assert res["reason"] == "mcap $600,000 over ceiling $500,000 (over_ceiling)"


# --- Codex N9 (night review 2026-10-06): an invalid threshold must refuse, never become zero -------

@pytest.mark.parametrize("knob", ["MEMECOIN_REAL_BIGCAP_LIFT_MAX_MCAP_USD",
                                  "MEMECOIN_REAL_BIGCAP_LIFT_MIN_SOURCE_USD",
                                  "MEMECOIN_REAL_BIGCAP_LIFT_MIN_V5_MCAP_RATIO"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1.0, 0.0])
def test_an_invalid_threshold_keeps_the_refusal(monkeypatch, knob, bad):
    """Codex's probe: ratio NaN, own volume 0, source $3,000, cap $400K lifted to $500K."""
    monkeypatch.setattr(cfg, knob, bad)
    trace = Trace()
    assert _lift(_ev(src=3000.0), evidence=_evidence(0.0), trace=trace) is None
    assert trace.subs[-1][1]["why"] == "config_invalid"


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-1", "0", "x"])
def test_the_settings_refuse_an_invalid_threshold_at_startup(bad):
    with pytest.raises(ValueError):
        cfg._bigcap_threshold({"MEMECOIN_REAL_BIGCAP_LIFT_MIN_V5_MCAP_RATIO": bad},
                              "MEMECOIN_REAL_BIGCAP_LIFT_MIN_V5_MCAP_RATIO", "0.10")
    assert cfg._bigcap_threshold({}, "MEMECOIN_REAL_BIGCAP_LIFT_MIN_V5_MCAP_RATIO", "0.10") == 0.10
