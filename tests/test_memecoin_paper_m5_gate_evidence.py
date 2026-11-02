"""funded_clone paper entries record the funded m5 volume gate's inputs (Gamma via Alpha,
2026-10-07, the operator's m5-floor question).

Gamma grades age x mcap x turnover once the young, under-$250k, below-floor cell has 30 paper closes
on exact exit quotes. The paper fan-out opens funded_clone without funded's admission gates, so the
evidence rides the entry snapshot: the own-tape lower bound the gate reads (organic_m5_usd), the
floor in USD for this mcap's band, the tested turnover ratio and pair_age_s. Paper rows only, no
new provider call, no admission change.
"""
import json
import sqlite3

import pytest

from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

NOW = "2026-10-07T07:00:00+00:00"
PAIR_MS = (1791356400 - 120) * 1000            # pair created 120 s before NOW
STATS = {"price": 1e-4, "liquidity": 20_000.0, "volume_m5": 5_000.0, "volume_h1": 40_000.0,
         "pair_created_at": PAIR_MS}


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    """The paper path ages the pair against real-UTC now (memecoin_copy_trader), so pin it to NOW;
    unpinned, pair_age_s grows with the wall clock and this file fails after 07:00Z 2026-10-07."""
    from sniper.util import clock
    monkeypatch.setattr(clock, "real_utc_now", lambda *a, **k: 1791356400.0)


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice")
    return c


def _ev(network="solana", mcap=200_000.0, eid="origin-1"):
    return CopyEvent("u1", "alice", "MINT1", "M1", "buy", 1e-4, mcap, 250.0, True, eid,
                     "2026-10-07T06:59:58+00:00", "2026-10-07T06:59:58+00:00", 0.3,
                     network_id=network)


def _attempt(conn, ev, *, mcap=200_000.0):
    """The funded decision on this origin, with the gate inputs its classification recorded (paper
    step 3: the paper evidence reads these, not a provider payload)."""
    m.start_entry_attempt(conn, attempt_id="att-" + ev.fomo_event_id,
                          decision_started_at="2026-10-07T06:59:59+00:00",
                          origin_event_id=ev.fomo_event_id, network_id=ev.network_id, mint=ev.mint)
    payload, digest = m.canonical_funded_json({"classification_version": 1, "coin_band": "micro",
        "facts": {"effective_mcap": mcap, "pair_age_s": 120.0, "volume_m5": STATS["volume_m5"],
                  "volume_h1": STATS["volume_h1"], "volume_mcap": mcap}})
    m.set_entry_attempt_classification(conn, "att-" + ev.fomo_event_id, classification_json=payload,
                                       classification_hash=digest, classification_version=1)
    from sniper.services import memecoin_volume_writer as writer
    stats = dict(STATS, market_cap=mcap, pair_age_s=120.0)
    observed = R.m5_floor_evidence(stats, ev, mcap=mcap, network_id=ev.network_id,
                                   coin_band="micro", decision_at=NOW, source_age_s=2.0)
    _active, _boundary, _ceiling, kwargs = R.volume_gate_args(
        stats, mcap=mcap, network_id=ev.network_id, coin_band="micro", source_user_id="u1",
        signal_usd=ev.their_size_usd if cfg.MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED else 0,
        source_age_s=2.0, decision_at=NOW)
    own = writer.gate_evidence(ev.network_id, ev.mint, ev.fomo_event_id, stats=stats)
    events = [{"name": "volume_gate", "mcap": mcap, "decision_kwargs": kwargs,
               "allowed": observed["would_allow"], "reason": observed["would_reason"],
               "source_age_s": 2.0, "pair_age_s": 120.0}]
    if own is not None:
        events.append({"name": "onchain_volume_m5", **own})
    conn.execute("UPDATE memecoin_entry_attempts SET trace_json=? WHERE attempt_id=?",
                 (json.dumps({"substage_events": events}), "att-" + ev.fomo_event_id))


def _open(conn, ev, arms=("funded_clone",)):
    if not m.entry_attempt_for_origin(conn, ev.fomo_event_id):
        _attempt(conn, ev, mcap=ev.their_market_cap or 200_000.0)
    out = ct.copy_event_to_arms(
        conn, ev, 100.0, NOW, arms=list(arms), fill_fn=ct.honest_fill,
        stats_fn=lambda *a, **k: dict(STATS),
        quote_fn=lambda mint, network_id, size_usd: (1e-4, "route_quote"))
    row = conn.execute("SELECT features_json FROM memecoin_entry_snapshots WHERE action='opened'"
                       ).fetchone()
    return out, json.loads(row["features_json"]) if row else None


def test_a_funded_clone_entry_records_the_m5_gate_inputs():
    out, features = _open(_conn(), _ev())
    assert set(out.values()) == {"opened"}
    gate = features["paper_m5_gate"]
    assert gate["mcap_usd"] == 200_000.0 and gate["coin_band"] == "micro"
    assert gate["pair_age_s"] == pytest.approx(120.0)
    assert gate["source_age_s"] == pytest.approx(2.0)
    assert gate["organic_m5_usd"] is None and gate["own_reason"] == "frozen_own_evidence_unknown"
    assert gate["m5_floor_usd"] == pytest.approx(200_000.0 * gate["m5_floor_ratio"])
    assert gate["tested_ratio"] == pytest.approx(gate["tested_m5_usd"] / 200_000.0)
    assert isinstance(gate["would_allow"], bool)


def test_the_own_tape_bound_is_the_organic_m5_and_lifts_the_tested_volume(monkeypatch):
    from sniper.services import memecoin_volume_writer as vw
    monkeypatch.setattr(vw, "gate_evidence", lambda chain, mint, origin, stats=None, **k: {
        "usd": 9_000.0, "reason": "ok", "coverage": "complete", "pools": []})
    _out, features = _open(_conn(), _ev(network="4663"))
    gate = features["paper_m5_gate"]
    assert gate["organic_m5_usd"] == 9_000.0 and gate["own_coverage"] == "complete"
    assert gate["tested_m5_usd"] == pytest.approx(max(9_000.0, gate["provider_organic_m5_usd"]))


def test_other_arms_record_nothing():
    _out, features = _open(_conn(), _ev(), arms=("alpha_b",))
    assert "paper_m5_gate" not in features


@pytest.mark.parametrize("mcap", [None, 0.0, -1.0, float("nan"), "x"])
def test_no_mcap_is_unknown_not_zero(mcap):
    assert R.m5_floor_evidence(STATS, _ev(), mcap=mcap, network_id="solana", coin_band=None,
                               decision_at=NOW) == {"reason": "mcap_unknown"}


def test_a_failed_read_never_costs_the_paper_entry(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tape down")
    monkeypatch.setattr(ct, "_paper_saved_m5_gate", boom)
    out, features = _open(_conn(), _ev())
    assert set(out.values()) == {"opened"}
    assert features["paper_m5_gate"] == {"error": "RuntimeError"}


# --- the entry hook's blind rule (Alpha, 2026-10-07, task d03055d6) -----------------------------

FLOOR = 0.05                                      # 5% of mcap: $10,000 at $200k


def _blind(monkeypatch, network, evidence, *, vol_m5=500.0):
    """m5_floor_evidence with the tape answering `evidence` and a provider m5 of $500, far under
    the floor, so only the tape decides whether the floor may refuse."""
    from sniper.services import memecoin_volume_writer as vw
    seen = {}

    def fake(chain, mint, origin, stats=None, source_tokens=None, source_usd=None):
        seen["source_tokens"], seen["source_usd"] = source_tokens, source_usd
        return dict(evidence)
    monkeypatch.setattr(vw, "gate_evidence", fake)
    monkeypatch.setattr(cfg, "inband_min_turnover_m5_for_chain", lambda chain, env=None: FLOOR)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VENUE_GAP_MIN_USD", 100.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_VENUE_GAP_RATIO", 3.0, raising=False)
    stats = dict(STATS, volume_m5=vol_m5, volume_h1=400_000.0)
    return R.m5_floor_evidence(stats, _ev(network=network), mcap=200_000.0, network_id=network,
                               coin_band="micro", decision_at=NOW, source_age_s=2.0), seen


def test_a_blind_tape_makes_the_m5_floor_undecidable_as_in_the_entry_hook(monkeypatch):
    gate, seen = _blind(monkeypatch, "4663", {"usd": None, "reason": "feed_stale"})
    assert gate["would_allow"] is True and gate["m5_floor_undecidable"] is True
    assert gate["own_measurement_basis"] == "onchain_blind_undecidable"
    assert seen["source_tokens"] == pytest.approx(250.0 / 1e-4)


def test_a_measured_tape_under_the_floor_still_refuses(monkeypatch):
    gate, _seen = _blind(monkeypatch, "4663", {"usd": 300.0, "reason": "ok", "coverage": "complete"})
    assert gate["would_allow"] is False and gate["m5_floor_undecidable"] is False
    assert gate["would_reason"] == "m5_turnover_under_floor"


def test_unpriced_swaps_are_undecidable_too(monkeypatch):
    gate, _seen = _blind(monkeypatch, "4663", {"usd": None, "reason": "ok",
                                               "unpriced_pools": [{"swaps": 4}]})
    assert gate["would_allow"] is True
    assert gate["own_measurement_basis"] == "onchain_unpriced_undecidable"


def test_on_56_a_blind_tape_keeps_the_floor(monkeypatch):
    gate, _seen = _blind(monkeypatch, "56", {"usd": None, "reason": "feed_stale"})
    assert gate["m5_floor_undecidable"] is False
    assert gate["own_measurement_basis"] == "bsc_m5_required_unknown"


@pytest.mark.parametrize("absent_usd, blind", [(50.0, True), (5_000.0, False)],
                         ids=["venue_gap", "quiet_coin"])
def test_an_absent_source_tx_takes_the_hooks_venue_gap_verdict(monkeypatch, absent_usd, blind):
    """source_tx_absent is blind only when the provider shows the coin trading elsewhere: $500 of
    provider m5 against $50 on our pools is a venue gap; against $5,000 the coin is quiet and our
    sum stands as the bound."""
    gate, _seen = _blind(monkeypatch, "4663", {"usd": None, "reason": "source_tx_absent",
                                               "source_absent_usd": absent_usd})
    assert gate["m5_floor_undecidable"] is blind
    if blind:
        assert gate["would_allow"] is True and gate["organic_m5_usd"] is None
    else:
        assert gate["organic_m5_usd"] == absent_usd and gate["own_reason"] == "ok"
        assert gate["own_coverage"] == "partial_source_tx_absent_quiet"
