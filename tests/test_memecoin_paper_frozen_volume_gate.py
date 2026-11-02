"""Paper observes the saved funded gate, including its transaction inputs and own tape."""
import json

import pytest

from sniper.services import memecoin_copy_trader as ct, memecoin_real_runner as runner
from sniper.services import memecoin_volume_writer as writer
from sniper.settings import memecoin as cfg
from test_memecoin_paper_entry_funded_facts import _conn, _ev, _attempt, _run, _features

NOW = "2026-10-09T00:00:01+00:00"


def _no_current_evidence(monkeypatch):
    monkeypatch.setattr(ct, "PROVIDER_SHADOW", False)
    monkeypatch.setattr(writer, "gate_evidence",
                        lambda *a, **k: pytest.fail("paper reread decision-time tape"))
    monkeypatch.setattr(runner, "volume_gate_args",
                        lambda *a, **k: pytest.fail("paper read current gate policy"))


def test_actual_funded_hook_trace_reaches_paper_with_frozen_inputs(monkeypatch):
    c = _conn()
    ev = _ev(mcap=50_000)
    ev.created_at = "2026-10-09T00:00:00+00:00"
    ev.signal_source = "follow_activity"
    st = {"price": ev.their_price, "market_cap": 50_000, "volume_m5": 5000,
          "volume_h1": 10_000, "txns_m5_buys": 3, "txns_m5_sells": 1, "pair_age_s": 300}
    own = {"usd": 9000, "reason": "ok", "coverage": "complete", "txns": 2,
           "pools": [{"pool": "p", "swaps": 3, "txns": 2}]}
    for key, value in {"MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD": 10_000,
                       "MEMECOIN_REAL_MIN_TXNS_M5": 2,
                       "MEMECOIN_REAL_TXNS_M5_REQUIRED": 1,
                       "MEMECOIN_REAL_MIN_TURNOVER_M5": .005,
                       "MEMECOIN_REAL_TURNOVER_FLOOR_OVER_BAND": .005,
                       "MEMECOIN_REAL_MAX_TURNOVER_M5": 0,
                       "MEMECOIN_REAL_MIN_VOLUME_H1_USD": 0,
                       "MEMECOIN_REAL_MIN_TURNOVER_H1": 0,
                       "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED": False,
                       "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO": 0,
                       "MEMECOIN_REAL_MAX_SELL_SHARE_PCT": 0}.items():
        monkeypatch.setattr(cfg, key, value)
    monkeypatch.setattr(cfg, "micro_min_volume_m5_usd_for", lambda *a: 0)
    monkeypatch.setattr(runner, "_own_m5_for_gate", lambda *a: dict(own))
    def no_send(conn, *args, **kwargs):
        runner._persist_attempt_classification(conn, kwargs["trace"], kwargs["classification"])
        return {"status": "test_reached"}
    monkeypatch.setattr(runner, "real_entry", no_send)
    out = runner.real_entry_hook(
        c, ev, {"solana": {}}, now_fn=lambda: NOW, trade_size=100, max_age_s=120,
        resolved_identity=runner.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={"effective_mcap": 50_000, "assumed_micro": False,
                                "consensus_n_60m": 1, "market_stats": st})
    assert out["status"] == "test_reached", out
    attempt = runner.m.entry_attempt_for_origin(c, ev.fomo_event_id)
    saved = json.loads(attempt["trace_json"])
    gate = next(e for e in saved["substage_events"] if e["name"] == "volume_gate")
    assert gate["allowed"] is True and gate["reason"] == "ok"
    assert gate["decision_kwargs"]["m5_txns"] == 4
    assert gate["decision_kwargs"]["m5_txns_lower_bound"] == 2
    _no_current_evidence(monkeypatch)
    _run(c, ev, None)
    paper = _features(c)["paper_m5_gate"]
    c.close()
    assert (paper["would_allow"], paper["would_reason"]) == (True, "ok")
    assert paper["organic_m5_usd"] == 9000
    assert paper["txns_m5_total"] == 4
    assert paper["txns_m5_buys"] == 3 and paper["txns_m5_sells"] == 1
    assert paper["m5_floor_ratio"] == .005


def test_saved_gate_json_supplies_counts_and_preserves_the_frozen_refusal(monkeypatch):
    c = _conn()
    _attempt(c)
    inputs = {"mcap": 48_000, "volume_m5": 0, "volume_h1": 10000,
              "txns_m5_buys": 0, "txns_m5_sells": 1, "txns_m5_total": 1,
              "onchain_m5": {"usd": 0, "reason": "ok", "coverage": "complete"},
              "ratio_floor": .02, "why": "m5_txns_under_floor"}
    gate = {"name": "volume_gate", "allowed": False, "reason": "m5_txns_under_floor",
            "mcap": 48_000, "txns": 1, "pair_age_s": 300}
    c.execute("UPDATE memecoin_entry_attempts SET gate_inputs_json=?,trace_json=?",
              (json.dumps(inputs), json.dumps({"substage_events": [gate]})))
    _no_current_evidence(monkeypatch)
    _run(c, _ev(mcap=50000), None)
    paper = _features(c)["paper_m5_gate"]
    facts = _features(c)["funded_facts"]
    c.close()
    assert (paper["would_allow"], paper["would_reason"]) == (False, "m5_txns_under_floor")
    assert facts["gate_inputs"] == inputs
    assert paper["txns_m5_buys"] == 0 and paper["txns_m5_sells"] == 1
    assert paper["organic_m5_usd"] == 0 and paper["mcap_usd"] == 48000


def test_frozen_ceiling_keeps_the_tested_provider_value(monkeypatch):
    c = _conn()
    _attempt(c)
    gate = {"name": "volume_gate", "allowed": False, "reason": "m5_turnover_over_ceiling",
            "mcap": 50_000, "tested_volume_m5": 40_000,
            "decision_kwargs": {"vol_m5": 40_000, "signal_usd": 0, "m5_floor": .01}}
    own = {"name": "onchain_volume_m5", "usd": 90_000, "reason": "ok"}
    c.execute("UPDATE memecoin_entry_attempts SET trace_json=?",
              (json.dumps({"substage_events": [gate, own]}),))
    _no_current_evidence(monkeypatch)
    _run(c, _ev(mcap=50_000), None)
    paper = _features(c)["paper_m5_gate"]
    c.close()
    assert paper["would_allow"] is False
    assert paper["organic_m5_usd"] == 90_000
    assert paper["tested_m5_usd"] == 40_000 and paper["tested_ratio"] == .8


@pytest.mark.parametrize("raw", [None, "{", "[]", "null"])
def test_legacy_or_malformed_frozen_decision_is_unknown_and_still_opens(monkeypatch, raw):
    c = _conn()
    _attempt(c)
    c.execute("UPDATE memecoin_entry_attempts SET gate_inputs_json=?,trace_json=?", (raw, raw))
    _no_current_evidence(monkeypatch)
    assert set(_run(c, _ev(), None).values()) == {"opened"}
    gate = _features(c)["paper_m5_gate"]
    c.close()
    assert gate["would_allow"] is None
    assert gate["would_reason"] == "frozen_decision_unknown"
