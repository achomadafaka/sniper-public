"""Gate report script: pair rows, registered jump buckets + source, claimed-vs-realized,
posted-only quote sizing, grouping. This is pre-registered analysis code — these tests pin
the registered definitions so they cannot drift before the gate read."""
from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.crypto5m_period_attribution_report import bucketize, group_summary, pair_rows


def _campaign_db(campaign, windows, fills=(), quotes=(), actions=(), regimes=()):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE maker_windows(condition_id TEXT, window_start_ts INTEGER, up_inv REAL,
        down_inv REAL, cash REAL, realized_pnl REAL, settled INTEGER, winning_outcome TEXT,
        campaign_id TEXT, ts INTEGER);
      CREATE TABLE maker_fills(id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT,
        condition_id TEXT, window_start_ts INTEGER, token TEXT, side TEXT, price REAL,
        size REAL, fill_model TEXT, quote_id INTEGER, ts INTEGER);
      CREATE TABLE maker_quotes(id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT,
        condition_id TEXT, window_start_ts INTEGER, token TEXT, side TEXT, status TEXT,
        price REAL, size REAL, fair REAL, best_bid REAL, best_ask REAL, ts INTEGER);
      CREATE TABLE crypto5m_maker_v3_policy_actions(id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT, window_start_ts INTEGER, context_key TEXT, quote_action_arm TEXT,
        risk_state TEXT);
      CREATE TABLE maker_regime_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER, token TEXT,
        regime_id TEXT, ts INTEGER);
    """)
    for cid, ws, pnl, up, down, win in windows:
        c.execute("INSERT INTO maker_windows VALUES(?,?,?,?,0,?,1,?,?,0)",
                  (cid, ws, up, down, pnl, win, campaign))
    for cid, ws, side, price, size, qid in fills:
        c.execute("INSERT INTO maker_fills(campaign_id,condition_id,window_start_ts,token,side,"
                  "price,size,fill_model,quote_id,ts) VALUES(?,?,?,?,?,?,?,'print',?,0)",
                  (campaign, cid, ws, "up", side, price, size, qid))
    for qid, ws, status, fair, bb, ba, size in quotes:
        c.execute("INSERT INTO maker_quotes(id,campaign_id,condition_id,window_start_ts,token,"
                  "side,status,price,size,fair,best_bid,best_ask,ts) "
                  "VALUES(?,?,?,?,'up','bid',?,?,?,?,?,?,0)",
                  (qid, campaign, "cid", ws, status, fair, size, fair, bb, ba))
    for ws, ck, arm, risk in actions:
        c.execute("INSERT INTO crypto5m_maker_v3_policy_actions(campaign_id,window_start_ts,"
                  "context_key,quote_action_arm,risk_state) VALUES(?,?,?,?,?)",
                  (campaign, ws, ck, arm, risk))
    for ws, regime_id in regimes:
        c.execute("INSERT INTO maker_regime_decisions(campaign_id,condition_id,window_start_ts,"
                  "token,regime_id,ts) VALUES(?,?,?,'up',?,0)",
                  (campaign, "cid", ws, regime_id))
    c.commit()
    return c


CALM_CK = "mid_window|high|calm|flat|healthy|unconfirmed|healthy"
JUMP_CK = "mid_window|high|jump|flat|healthy|unconfirmed|healthy"


@pytest.fixture()
def pair():
    a = _campaign_db(
        "challenger",
        windows=[("cid1", 1000, 10.0, 5.0, 0.0, "up"), ("cid2", 2000, -4.0, 0.0, 3.0, "up")],
        fills=[("cid1", 1000, "buy", 0.40, 10.0, 1)],
        quotes=[(1, 1000, "posted", 0.55, 0.39, 0.43, 10.0),
                (2, 1000, "pulled", 0.55, 0.39, 0.43, 99.0)],
        actions=[(1000, CALM_CK, "base_small", "normal"),
                 (2000, JUMP_CK, "wide_tiny", "normal"),
                 (2000, JUMP_CK, "base_small", "normal"),
                 (2000, CALM_CK, "base_small", "normal"),
                 (2000, CALM_CK, "base_small", "normal")],
        # registered primary source: regime decisions. w1 calm (0/2), w2 high (1/4 = 25%)
        regimes=[(1000, "normal"), (1000, "mid_chop"),
                 (2000, "jump"), (2000, "normal"), (2000, "normal"), (2000, "extreme_odds")],
    )
    b = _campaign_db(
        "control",
        windows=[("cid1", 1000, 2.0, 1.0, 0.0, "up"), ("cid2", 2000, 6.0, 2.0, 0.0, "up")],
        fills=[("cid1", 1000, "buy", 0.50, 4.0, 1)],
        quotes=[(1, 1000, "posted", 0.60, 0.48, 0.52, 4.0)],
    )
    yield a, b
    a.close(); b.close()


def test_pair_rows_delta_and_regime_decision_jump_source(pair):
    a, b = pair
    rows = pair_rows(a, "challenger", b, "control", cap=None)

    assert [r["window_start_ts"] for r in rows] == [1000, 2000]
    assert rows[0]["delta"] == pytest.approx(8.0)     # 10 - 2
    assert rows[1]["delta"] == pytest.approx(-10.0)   # -4 - 6
    # jump share comes from maker_regime_decisions (the registered source), not context_key
    assert rows[0]["jump_source"] == "regime_decisions"
    assert rows[0]["jump_share"] == pytest.approx(0.0)
    assert rows[0]["jump_bucket"] == "calm"
    assert rows[1]["jump_share"] == pytest.approx(0.25)
    assert rows[1]["jump_bucket"] == "high"


def test_registered_bucket_boundaries_exactly():
    """calm = 0; moderate = (0, 0.10); high = >= 0.10 — 10% exactly is HIGH."""
    a = _campaign_db(
        "challenger",
        windows=[("c1", 1000, 1.0, 0, 0, "up"), ("c2", 2000, 1.0, 0, 0, "up"),
                 ("c3", 3000, 1.0, 0, 0, "up")],
        regimes=([(1000, "normal")] * 10
                 + [(2000, "jump")] + [(2000, "normal")] * 19          # 5% -> moderate
                 + [(3000, "jump")] + [(3000, "normal")] * 9),         # 10% exactly -> high
    )
    b = _campaign_db("control", windows=[("c1", 1000, 0, 0, 0, "up"),
                                         ("c2", 2000, 0, 0, 0, "up"),
                                         ("c3", 3000, 0, 0, 0, "up")])
    rows = pair_rows(a, "challenger", b, "control", cap=None)
    assert [r["jump_bucket"] for r in rows] == ["calm", "moderate", "high"]
    a.close(); b.close()


def test_jump_falls_back_to_policy_actions_when_regime_rows_pruned():
    a = _campaign_db(
        "challenger",
        windows=[("c1", 1000, 1.0, 0, 0, "up")],
        actions=[(1000, JUMP_CK, "base_small", "normal"),
                 (1000, CALM_CK, "base_small", "normal")],
        regimes=[],  # pruned
    )
    b = _campaign_db("control", windows=[("c1", 1000, 0, 0, 0, "up")])
    rows = pair_rows(a, "challenger", b, "control", cap=None)
    assert rows[0]["jump_source"] == "policy_actions_fallback"
    assert rows[0]["jump_share"] == pytest.approx(0.5)
    a.close(); b.close()


def test_posted_shares_excludes_pulled_quotes_and_claimed_edge_both_legs(pair):
    a, b = pair
    rows = pair_rows(a, "challenger", b, "control", cap=None)

    # pulled 99-share row must NOT count
    assert rows[0]["posted_shares"] == pytest.approx(10.0)
    # challenger: buy 10 @0.40, fair 0.55 -> +1.50
    assert rows[0]["claimed_edge"] == pytest.approx(1.50)
    assert rows[0]["claimed_minus_realized"] == pytest.approx(1.50 - 10.0)
    # control leg: buy 4 @0.50, fair 0.60 -> +0.40
    assert rows[0]["claimed_edge_b"] == pytest.approx(0.40)
    assert rows[0]["claimed_minus_realized_b"] == pytest.approx(0.40 - 2.0)
    # winner=up: winner_inventory = up_inv
    assert rows[0]["winner_inventory"] == pytest.approx(5.0)
    assert rows[1]["loser_inventory"] == pytest.approx(3.0)
    assert json.loads(rows[1]["arm_mix"]) == {"base_small": 3, "wide_tiny": 1}


def test_spread_and_odds_survive_zero_posted_quotes():
    """A window whose only quote snapshots are 'pulled' still yields spread/odds; only
    posted_shares is None."""
    a = _campaign_db(
        "challenger",
        windows=[("c1", 1000, 1.0, 0, 0, "up")],
        quotes=[(1, 1000, "pulled", 0.55, 0.40, 0.44, 7.0)],
    )
    b = _campaign_db("control", windows=[("c1", 1000, 0, 0, 0, "up")])
    row = pair_rows(a, "challenger", b, "control", cap=None)[0]
    assert row["posted_shares"] is None
    assert row["avg_spread"] == pytest.approx(0.04)
    assert row["mean_up_fair"] == pytest.approx(0.55)
    assert row["odds_bucket"] == "mid"
    a.close(); b.close()


def test_bucketize_and_group_summary(pair):
    a, b = pair
    rows = bucketize(pair_rows(a, "challenger", b, "control", cap=None))

    by_jump = group_summary(rows, "jump_bucket")
    assert by_jump["calm"] == {"n": 1, "delta_mean": 8.0, "delta_total": 8.0,
                               "a_mean": 10.0, "b_mean": 2.0}
    assert by_jump["high"]["delta_mean"] == -10.0
    # missing capture data lands in n/a, never crashes
    assert group_summary(rows, "vol_bucket")["n/a"]["n"] == 2
