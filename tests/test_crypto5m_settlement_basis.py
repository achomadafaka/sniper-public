"""Settlement capture/reconciliation + Binance↔Chainlink basis audit (Lane B, research only)."""
from __future__ import annotations

import json
import os

from sniper.algos.polymarket_crypto5m import basis_audit as BA
from sniper.algos.polymarket_crypto5m import market_resolver as MR
from sniper.algos.polymarket_crypto5m import settlement as S

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")


def _spec(name):
    with open(os.path.join(FX, name)) as f:
        spec, _ = MR.parse_event(json.load(f))
    return spec


# --- settlement -----------------------------------------------------------------------------

def test_winner_from_outcome_prices():
    assert S.winner_from_outcome_prices(["1", "0"], "U", "D") == ("U", "Up")
    assert S.winner_from_outcome_prices(["0", "1"], "U", "D") == ("D", "Down")
    assert S.winner_from_outcome_prices(["0.4", "0.6"], "U", "D") == (None, None)
    assert S.winner_from_outcome_prices(None, "U", "D") == (None, None)


def test_from_gamma_builds_settlement_with_oracle():
    spec = _spec("event_resolved_up.json")  # outcomePrices ["1","0"] -> Up
    st = S.from_gamma(spec)
    assert st.winning_outcome == "Up"
    assert st.winning_token_id == spec.up_token_id
    assert st.uma_resolution_status == "resolved"
    assert st.source_used == "gamma"
    assert abs(st.gamma_price_to_beat - spec.price_to_beat) < 1e-9


def test_from_gamma_unresolved_when_no_outcome_prices():
    spec = _spec("event_live.json")  # outcomePrices None
    st = S.from_gamma(spec)
    assert st.winning_outcome is None
    assert st.reconciliation_status in ("gamma_only", "unresolved")


def test_reconcile_agree_and_conflict():
    g = S.Settlement("c", "U", "Up", None, "resolved", ("1", "0"), "gamma", "gamma_only", 100.0)
    w = S.Settlement("c", "U", "Up", None, "resolved", ("1", "0"), "websocket", "ws_only", None)
    assert S.reconcile(g, w).reconciliation_status == "agree"
    w2 = S.Settlement("c", "D", "Down", None, "resolved", ("0", "1"), "websocket", "ws_only", None)
    conflict = S.reconcile(g, w2)
    assert conflict.reconciliation_status == "conflict"
    # a conflict must NOT emit a consumable winner — quarantine it
    assert conflict.winning_outcome is None and conflict.winning_token_id is None
    assert S.reconcile(g, None).reconciliation_status == "gamma_only"
    assert S.reconcile(None, None).reconciliation_status == "unresolved"


# --- basis audit ----------------------------------------------------------------------------

def test_binance_direction_mirrors_chainlink_rule():
    assert BA.binance_direction(start=100.0, end=101.0) == "Up"
    assert BA.binance_direction(start=100.0, end=99.0) == "Down"
    assert BA.binance_direction(start=100.0, end=100.0) == "Up"   # flat -> Up (>=)
    assert BA.binance_direction(start=None, end=100.0) is None


def test_audit_row_basis_and_agreement():
    row = BA.audit_row(condition_id="c", price_to_beat=63456.0, binance_start=63450.0,
                       binance_end=63500.0, winner="Up", window_start_ts=1781947200)
    assert row["binance_direction"] == "Up"
    assert row["agrees_with_winner"] is True
    assert abs(row["basis_binance_minus_chainlink"] - (63450.0 - 63456.0)) < 1e-9
    assert abs(row["binance_move"] - 50.0) < 1e-9


def test_audit_row_disagreement_near_threshold():
    # tiny move the wrong way -> Binance proxy disagrees with the Chainlink-settled winner
    row = BA.audit_row(condition_id="c", price_to_beat=63456.0, binance_start=63456.0,
                       binance_end=63455.9, winner="Up", window_start_ts=1781947200)
    assert row["binance_direction"] == "Down"
    assert row["agrees_with_winner"] is False


def test_aggregate_disagreement_rate_and_missing():
    rows = [
        BA.audit_row(condition_id="c", price_to_beat=100.0, binance_start=100.0, binance_end=101.0, winner="Up", window_start_ts=0),     # agree
        BA.audit_row(condition_id="c", price_to_beat=100.0, binance_start=100.0, binance_end=99.9, winner="Up", window_start_ts=0),      # disagree
        BA.audit_row(condition_id="c", price_to_beat=100.0, binance_start=None, binance_end=None, winner="Up", window_start_ts=0),       # missing
    ]
    agg = BA.aggregate(rows)
    assert agg["n"] == 3
    assert agg["n_usable"] == 2
    assert agg["disagreement_rate"] == 0.5
    assert agg["missing"] == 1


def test_near_threshold_uses_distance_to_threshold_not_raw_move():
    # a flat window FAR from the threshold must NOT count as near-threshold (the abs_move bug)
    far_flat = BA.audit_row(condition_id="c", price_to_beat=63456.0, binance_start=63000.0,
                            binance_end=63001.0, winner="Down", window_start_ts=0)
    assert far_flat["distance_to_threshold"] == abs(63001.0 - 63456.0)   # ~455, not "near"
    # a window whose end sits right on the threshold IS near-threshold
    close = BA.audit_row(condition_id="c", price_to_beat=63456.0, binance_start=63450.0,
                         binance_end=63455.9, winner="Up", window_start_ts=0)
    agg = BA.aggregate([far_flat, close], near_threshold_abs=1.0)
    assert agg["near_threshold_n"] == 1   # only the genuinely close window
