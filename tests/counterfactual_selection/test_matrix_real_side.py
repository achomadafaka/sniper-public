"""Real per-side resolution + nearest-neighbor matching.

Locks the fix for the SwissMiss mislabel: when a wallet picks the NO/underdog
side (or a named multi-outcome side), the row must resolve on HER chosen outcome,
not the always-YES side; and candidates must be compared on the same-role side.
"""
from __future__ import annotations

from sniper.analysis.counterfactual_selection.pit_market_context import MarketContext
from sniper.analysis.counterfactual_selection.matrix import (
    build_decision_matrix, nearest_neighbor_match,
)


def _dense(price):
    return [(1000 + 50 * k, price) for k in range(40)]


def test_no_side_pick_resolves_on_her_side_not_yes():
    # YES side trades ~0.30 (YES is the underdog); NO wins (winning_index=1).
    meta = {"m": {"created_at": 1000, "end_date": 1_000_000, "category": "sports",
                  "market_type": "binary", "outcomes": ["Yes", "No"], "winning_index": 1}}
    ctx = MarketContext(market_meta=meta, price_index={"m": _dense(0.30)})
    # she bought NO at ~0.70 → she won (NO resolved)
    eng = [{"conditionId": "m", "asset": "tNo", "outcome": "No", "entry_vwap": 0.70,
            "first_entry_ts": 3000}]
    rows = build_decision_matrix(eng, ctx)
    pick = [r for r in rows if r["picked"] == 1][0]
    assert pick["resolved"] == 1                 # NOT the YES-collapse (which would be 0)
    assert pick["features"]["outcome_role"] == "favorite"   # 0.70 entry → favorite side
    assert "favorite" in pick["group_id"]


def test_pick_feature_price_uses_side_mid_not_execution_vwap():
    meta = {"m": {"created_at": 1000, "end_date": 1_000_000, "category": "sports",
                  "market_type": "binary", "outcomes": ["Yes", "No"], "winning_index": 0}}
    ctx = MarketContext(market_meta=meta, price_index={"m": _dense(0.30)})
    eng = [{"conditionId": "m", "asset": "tYes", "outcome": "Yes", "entry_vwap": 0.90,
            "first_entry_ts": 3000}]

    pick = [r for r in build_decision_matrix(eng, ctx) if r["picked"] == 1][0]

    assert pick["features"]["price"] == 0.30
    assert pick["features"]["entry_price"] == 0.90


def test_yes_side_pick_resolves_yes():
    meta = {"m": {"created_at": 1000, "end_date": 1_000_000, "category": "sports",
                  "market_type": "binary", "outcomes": ["Yes", "No"], "winning_index": 0}}
    ctx = MarketContext(market_meta=meta, price_index={"m": _dense(0.30)})
    eng = [{"conditionId": "m", "asset": "tYes", "outcome": "Yes", "entry_vwap": 0.30,
            "first_entry_ts": 3000}]
    pick = [r for r in build_decision_matrix(eng, ctx) if r["picked"] == 1][0]
    assert pick["resolved"] == 1                 # YES won, she held YES
    assert pick["features"]["outcome_role"] == "underdog"   # 0.30 entry


def test_candidate_matched_on_same_role_side():
    # pick: underdog YES @0.30 that won. candidate: YES @0.20 is the underdog side;
    # the underdog side LOST there (winning_index=0 means YES won → underdog won...).
    meta = {
        "p": {"created_at": 1000, "end_date": 1_000_000, "category": "sports",
              "market_type": "binary", "outcomes": ["Yes", "No"], "winning_index": 0},
        "c": {"created_at": 1000, "end_date": 1_000_000, "category": "sports",
              "market_type": "binary", "outcomes": ["Yes", "No"], "winning_index": 1},
    }
    ctx = MarketContext(market_meta=meta,
                        price_index={"p": _dense(0.30), "c": _dense(0.20)})
    eng = [{"conditionId": "p", "asset": "t", "outcome": "Yes", "entry_vwap": 0.30,
            "first_entry_ts": 3000}]
    rows = build_decision_matrix(eng, ctx)
    cand = [r for r in rows if r["picked"] == 0]
    assert cand, "candidate c should be in the underdog-scope counterfactual"
    # c: YES price 0.20 → YES is the underdog side (comp_idx=0); NO won (winning_index=1)
    # so the underdog side LOST → resolved 0
    assert cand[0]["resolved"] == 0
    assert cand[0]["features"]["outcome_role"] == "underdog"


def test_c4_event_sibling_scope_keeps_candidates_within_event():
    # event A has 3 date-variant markets, event B has 2. She picks a1; the C4
    # counterfactual must be a2/a3 (same event), never b1/b2.
    def mk(ev, win):
        return {"created_at": 1000, "end_date": 1_000_000, "category": "geo",
                "market_type": "binary", "outcomes": ["Yes", "No"],
                "winning_index": win, "event_family": ev}
    meta = {"a1": mk("A", 1), "a2": mk("A", 1), "a3": mk("A", 0),
            "b1": mk("B", 0), "b2": mk("B", 1)}
    ctx = MarketContext(market_meta=meta,
                        price_index={c: _dense(0.30) for c in meta})
    eng = [{"conditionId": "a1", "asset": "t", "outcome": "No", "entry_vwap": 0.70,
            "first_entry_ts": 3000}]
    rows = build_decision_matrix(eng, ctx, real_scope_axes=("event_family",))
    cand_cids = {r.get("_cid") for r in rows if r["picked"] == 0}  # may be unset
    cands = [r for r in rows if r["picked"] == 0]
    pick = [r for r in rows if r["picked"] == 1][0]
    assert pick["group_id"].startswith("A")          # grouped by event
    assert len(cands) == 2                            # a2, a3 — the same-event siblings
    # none of the B-event markets leak in (they'd have different group; here zero rows)
    assert all(r["group_id"].startswith("A") for r in cands)


def test_nn_match_returns_picks_plus_balanced_controls():
    # one group; picks cluster at market_age ~small, half the candidates share it,
    # half are far. Matching should pull the near controls, not the far ones.
    rows = []
    for i in range(8):
        rows.append({"group_id": "g", "picked": 1, "ts": 0,
                     "features": {"price": 0.3, "market_age": 100 + i, "time_to_resolution": 500}})
    for i in range(8):            # near controls
        rows.append({"group_id": "g", "picked": 0, "ts": 0,
                     "features": {"price": 0.3, "market_age": 100 + i, "time_to_resolution": 500}})
    for i in range(8):            # far controls
        rows.append({"group_id": "g", "picked": 0, "ts": 0,
                     "features": {"price": 0.3, "market_age": 100000 + i, "time_to_resolution": 500}})
    matched = nearest_neighbor_match(rows, k=1)
    picks = [r for r in matched if r["picked"] == 1]
    ctrls = [r for r in matched if r["picked"] == 0]
    assert len(picks) == 8 and len(ctrls) == 8
    # every matched control should be a NEAR one (age ~100), not a far one
    assert all(r["features"]["market_age"] < 1000 for r in ctrls)
