"""fwlm-v0 capture round — fail-closed before freeze, episode/entry lifecycle, Control 4."""
import importlib.util
import sys
from pathlib import Path

import pytest

from sniper.hyperlive import far_whale_store as S

_spec = importlib.util.spec_from_file_location(
    "run_far_whale_capture",
    Path(__file__).resolve().parent.parent / "scripts" / "run_far_whale_capture.py")
cap = importlib.util.module_from_spec(_spec)
sys.modules["run_far_whale_capture"] = cap
_spec.loader.exec_module(cap)


def _ctxs(mid=3000.0, spread=2.0):
    return {"ETH": {"mid": mid, "oi": 1e6, "day_ntl_vlm": 5e8, "spread_bps": spread}}


def _whale_state(liq=2950.0, szi=700.0, entry=3010.0):
    # 700 * 3010 = $2.107M notional, ~167bps from 3000 -> in band, over $1M
    return {"assetPositions": [{"position": {
        "coin": "ETH", "liquidationPx": str(liq), "szi": str(szi), "entryPx": str(entry)}}]}


@pytest.fixture()
def conn():
    c = S.connect(":memory:")
    S.ensure_schema(c)
    return c


def _freeze(conn, ts=0):
    S.save_report(conn, kind="freeze",
                  payload={"thresholds": {"ETH": 1_000_000.0},
                           "campaign_start_ts_ms": ts}, ts_ms=ts)


def test_sweep_mode_before_freeze_no_candidates(conn):
    stats = cap.run_round(
        conn, fetch_ctxs=lambda: _ctxs(),
        fetch_state=lambda a: _whale_state(),
        harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    assert stats["sweep_mode"] is True
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_candidates").fetchone()[0] == 0
    # but the sweep pool + control pool still accrue
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_position_snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_market_state").fetchone()[0] == 1


def test_frozen_candidate_creates_episode_and_primary_entry(conn):
    _freeze(conn)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    cands = conn.execute("SELECT is_primary FROM hl_far_whale_liq_candidates").fetchall()
    entries = conn.execute("SELECT is_primary, status FROM hl_far_whale_liq_paper_entries").fetchall()
    assert [c[0] for c in cands] == [1]
    assert [(e[0], e[1]) for e in entries] == [(1, "open")]


def test_second_round_same_whale_no_second_entry(conn):
    _freeze(conn)
    for ts in (1_000_000, 1_180_000):
        cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                      fetch_state=lambda a: _whale_state(),
                      harvest=lambda assets: ["0xwhale"], now_ms=ts)
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_paper_entries").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_candidates").fetchone()[0] == 2
    assert conn.execute("SELECT n_candidates FROM hl_far_whale_liq_episodes").fetchone()[0] == 2


def test_anomaly_counted_and_dropped(conn):
    _freeze(conn)
    # long with liq ABOVE mid -> §2 anomaly
    stats = cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                          fetch_state=lambda a: _whale_state(liq=3050.0, szi=700.0),
                          harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    assert stats["anomalies_dropped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM hl_far_whale_liq_candidates").fetchone()[0] == 0


def test_whale_close_sets_episode_closed_and_entry_exits_on_target_moved_only_for_move(conn):
    _freeze(conn)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    # whale still polled OK but position gone -> episode closed; entry keeps running (ITT)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(mid=2990.0),
                  fetch_state=lambda a: {"assetPositions": []},
                  harvest=lambda assets: [], now_ms=1_180_000)
    ep = conn.execute("SELECT status FROM hl_far_whale_liq_episodes").fetchone()[0]
    entry = conn.execute("SELECT status FROM hl_far_whale_liq_paper_entries").fetchone()[0]
    assert ep == "closed"
    assert entry == "open"          # ITT: evaporation never force-exits the paper trade


def test_target_hit_settles_with_position_persisted_flag(conn):
    _freeze(conn)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(mid=2960.0),   # past 75% target 2962.5
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: [], now_ms=1_180_000)
    out = conn.execute("SELECT exit_reason, position_persisted FROM"
                       " hl_far_whale_liq_outcomes").fetchone()
    assert tuple(out) == ("target_hit", 1)


def test_liq_price_moved_over_tolerance_force_exits_and_opens_new_episode(conn):
    """The ONE deliberate ITT exception (spec + CLAUDE.md doctrine): a whale whose
    liquidation_price drifts > EPISODE_LIQ_TOL_BPS (25bps) IS a legitimate in-sim exit,
    distinct from mere evaporation (closed/shrank/source_disappeared — see
    test_whale_close_sets_episode_closed_and_entry_exits_on_target_moved_only_for_move
    and test_whale_shrank_does_not_force_exit below, neither of which force-exits).

    Round 1 opens episode e1 + primary entry1 at liq=2950 (mid=3000). Round 2 the SAME
    whale reports liq moved +40bps to 2961.8 (over the 25bps tolerance) with mid still
    3000 — still inside the 50-300bps candidate band, so this single round both:
      (a) force-exits entry1 via Control 4's independent drift check (exit_reason
          "target_moved" forwarded from whale_event="liq_price_moved"), and
      (b) passes assign_episode's own >25bps check, closing e1 and opening a brand-new
          episode e2 + a new primary entry2 in the SAME round.
    """
    _freeze(conn)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    moved = 2950.0 * (1 + 40 / 1e4)   # +40bps, over EPISODE_LIQ_TOL_BPS (25)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(liq=moved),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_180_000)

    episodes = {r["episode_id"]: r["status"] for r in
               conn.execute("SELECT episode_id, status FROM hl_far_whale_liq_episodes")}
    assert len(episodes) == 2
    old_eids = [e for e, s in episodes.items() if s == "liq_price_moved"]
    new_eids = [e for e, s in episodes.items() if s == "open"]
    assert len(old_eids) == 1 and len(new_eids) == 1
    old_eid, new_eid = old_eids[0], new_eids[0]

    entries = conn.execute(
        "SELECT episode_id, status, is_primary FROM hl_far_whale_liq_paper_entries"
        " ORDER BY entry_id").fetchall()
    assert len(entries) == 2
    old_entry, new_entry = entries
    assert (old_entry["episode_id"], old_entry["status"]) == (old_eid, "closed")
    assert (new_entry["episode_id"], new_entry["status"], new_entry["is_primary"]) \
        == (new_eid, "open", 1)

    out = conn.execute(
        "SELECT o.exit_reason FROM hl_far_whale_liq_outcomes o"
        " JOIN hl_far_whale_liq_paper_entries e ON e.entry_id = o.entry_id"
        " WHERE e.episode_id = ?", (old_eid,)).fetchone()
    assert out["exit_reason"] == "target_moved"


def test_whale_shrank_does_not_force_exit(conn):
    """Ordinary evaporation via a genuine notional shrink (< 50% of the episode's first
    recorded notional, same liq level so no drift) must NOT force the open entry to
    exit — only liq_price_moved is a legitimate in-sim exit (ITT otherwise)."""
    _freeze(conn)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(),   # szi=700 -> notional $2.107M
                  harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(szi=300.0),  # $903k < 50% of $2.107M
                  harvest=lambda assets: ["0xwhale"], now_ms=1_180_000)
    ep = conn.execute("SELECT status FROM hl_far_whale_liq_episodes").fetchone()[0]
    entry = conn.execute("SELECT status FROM hl_far_whale_liq_paper_entries").fetchone()[0]
    assert ep == "shrank"
    assert entry == "open"


def test_persisted_through_exit_leaves_liq_fired_null(conn):
    """Regression: when the episode is still 'open' (whale persisted) at the moment a
    paper entry settles (e.g. adverse_stop), liq_fired must be NULL — we genuinely do
    not know whether the whale is ever liquidated after our trade closes. Coding it as
    0 would silently assert "did not fire" for an unobserved future event, violating
    the None-vs-0 distinction (CLAUDE.md "domain-correct data handling") and the
    brief's own round contract ("NULL when the whale outlived the trade unobserved")."""
    _freeze(conn)
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(),
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_000_000)
    # whale persists (same liq level), price moves against the trade -> adverse_stop.
    # adverse_stop_price = 3000 + 0.50*50 = 3025 for this short-direction entry.
    cap.run_round(conn, fetch_ctxs=lambda: _ctxs(mid=3030.0),
                  fetch_state=lambda a: _whale_state(),
                  harvest=lambda assets: ["0xwhale"], now_ms=1_180_000)
    ep_status = conn.execute("SELECT status FROM hl_far_whale_liq_episodes").fetchone()[0]
    out = conn.execute("SELECT exit_reason, position_persisted, liq_fired FROM"
                       " hl_far_whale_liq_outcomes").fetchone()
    assert ep_status == "open"
    assert out["exit_reason"] == "adverse_stop"
    assert out["position_persisted"] == 1
    assert out["liq_fired"] is None
