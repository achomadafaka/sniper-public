"""WO-2 — the engine decision report grades decisions against what the tape did next. This guards
the counterfactual math, the coin-band join, the censoring, and the n<30 gate."""
import sqlite3

from sniper.persistence import db_exit_state as xs
from sniper.persistence import db_memecoin as m

import scripts.research.engine_decision_report as R


def _exit_db(tmp_path):
    path = str(tmp_path / "exit_state.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    xs.ensure_exit_state_tables(c)
    return path, c


def _snap(c, *, pid, sid, computed_at, price, chain="solana", pool="POOL"):
    c.execute(
        "INSERT INTO memecoin_exit_market_state(id, position_id, chain_id, mint, engine_version, "
        "feature_version, computed_at, state, executable_price, pool) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sid, pid, chain, "MINT", "exit-brain-v2", "fv1", computed_at, "trending", price, pool))
    c.commit()


def _decision(c, *, pid, sid, decided_at, action, outcome=None, confirmed=0):
    xs.write_candidate_decision(c, {
        "position_id": pid, "decided_at": decided_at, "engine_version": "exit-brain-v2",
        "feature_version": "fv1", "state_id": sid, "candidate_action": action,
        "reason_codes": [], "executable_confirmed": confirmed, "censored": 0, "outcome": outcome})


def test_counterfactual_and_censoring(tmp_path):
    path, c = _exit_db(tmp_path)
    T = "2026-07-25T00:00:00+00:00"
    # decision-time snapshot @ price 1.0, then +5m up to 1.2, +15m down to 0.8. No +30m snapshot.
    _snap(c, pid=1, sid=10, computed_at="2026-07-25T00:00:00+00:00", price=1.0)
    _snap(c, pid=1, sid=11, computed_at="2026-07-25T00:05:00+00:00", price=1.2)
    _snap(c, pid=1, sid=12, computed_at="2026-07-25T00:15:00+00:00", price=0.8)
    _decision(c, pid=1, sid=10, decided_at=T, action="CUT", outcome="closed", confirmed=1)
    c.close()

    # ledger with a nano-cap entry_mcap
    ledger = str(tmp_path / "ledger.db")
    lc = sqlite3.connect(ledger); m.ensure_memecoin_tables(lc)
    lc.execute("INSERT INTO memecoin_positions(id, source_user_id, mint, entry_mcap, is_open, "
               "execution_mode) VALUES(1,'u','MINT',500000,0,'real')")
    lc.commit(); lc.close()

    decisions, timeline, bands = R.load(path, ledger)
    assert bands[1] == "nano<1M"
    cells = R.counterfactuals(decisions, timeline, bands)
    cell = cells[("CUT", "solana", "nano<1M")]
    # +5m: price 1.0 -> 1.2 = +0.2 ; +15m: 1.0 -> 0.8 = -0.2 ; +30m: no snapshot -> censored
    assert abs(cell["cf"][5]["returns"][0] - 0.2) < 1e-9
    assert abs(cell["cf"][15]["returns"][0] + 0.2) < 1e-9
    assert cell["cf"][30]["returns"] == [] and cell["cf"][30]["censored"] == 1
    assert cell["outcomes"]["closed"] == 1 and cell["confirmed"] == 1


def test_gate_blocks_verdict_below_min_n(tmp_path):
    path, c = _exit_db(tmp_path)
    # one acted decision -> n=1, far below 30 -> render must say counts only, emit no proposal
    _snap(c, pid=1, sid=1, computed_at="2026-07-25T00:00:00+00:00", price=1.0)
    _snap(c, pid=1, sid=2, computed_at="2026-07-25T00:05:00+00:00", price=2.0)
    _snap(c, pid=1, sid=3, computed_at="2026-07-25T00:15:00+00:00", price=2.0)
    _snap(c, pid=1, sid=4, computed_at="2026-07-25T00:30:00+00:00", price=2.0)
    _decision(c, pid=1, sid=1, decided_at="2026-07-25T00:00:00+00:00", action="CUT")
    c.close()
    decisions, timeline, bands = R.load(path, str(tmp_path / "missing_ledger.db"))
    cells = R.counterfactuals(decisions, timeline, bands)
    report, proposals = R.render(cells)
    assert "counts only" in report
    assert proposals == []          # n=1 can never propose


def test_empty_db_is_honest(tmp_path):
    decisions, timeline, bands = R.load(str(tmp_path / "nope.db"), str(tmp_path / "nope2.db"))
    assert decisions == [] and timeline == {} and bands == {}
    report, proposals = R.render(R.counterfactuals(decisions, timeline, bands))
    assert "No decisions recorded yet" in report and proposals == []


def test_full_exit_counterfactual_continues_on_raw_tape_after_position_closes(tmp_path):
    from datetime import datetime, timezone
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t

    path, c = _exit_db(tmp_path)
    t0 = 1_800_000_000
    iso = lambda ep: datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()
    _snap(c, pid=1, sid=1, computed_at=iso(t0), price=1.0, chain="4663", pool="0xpool")
    _decision(c, pid=1, sid=1, decided_at=iso(t0), action="CUT", outcome="closed", confirmed=1)
    c.close()  # no later snapshot: the position closed at the CUT

    ticks = str(tmp_path / "ticks.db")
    tc = sqlite3.connect(ticks); tc.row_factory = sqlite3.Row
    t.ensure_tick_tables(tc); t.ensure_block_times(tc); ensure_feature_tables(tc)
    tc.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,token0_decimals,"
               "token1_decimals,resolved_at) VALUES('4663','0xpool','Q','B',1,18,18,?)", (iso(t0),))
    rows = []
    for block, seconds, price in ((1, 0, 1.0), (2, 300, 1.2), (3, 900, 0.8), (4, 1800, 1.5)):
        rows.append({"chain_id": "4663", "pool": "0xpool", "kind": "v2_swap",
                     "block_no": block, "tx_sig": f"s{block}", "log_index": 0,
                     "maker": "R", "amount0": "100", "amount1": str(int(price * 100)),
                     "observed_at": iso(t0 + seconds)})
        t.backfill_block_times(tc, "4663", [block], lambda _b, ts=t0 + seconds: ts, iso(t0))
    t.insert_ticks(tc, rows); tc.close()

    decisions, timeline, bands = R.load(path, str(tmp_path / "missing.db"), ticks_db=ticks)
    cell = R.counterfactuals(decisions, timeline, bands)[("CUT", "4663", "unknown")]
    assert [round(cell["cf"][n]["returns"][0], 6) for n in (5, 15, 30)] == [0.2, -0.2, 0.5]
    assert all(cell["cf"][n]["censored"] == 0 for n in (5, 15, 30))


def test_raw_tape_without_decision_time_anchor_is_censored_not_unit_mixed(tmp_path):
    from datetime import datetime, timezone
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t

    path, c = _exit_db(tmp_path)
    t0 = 1_800_000_000
    iso = lambda ep: datetime.fromtimestamp(ep, tz=timezone.utc).isoformat()
    _snap(c, pid=1, sid=1, computed_at=iso(t0), price=25.0, chain="4663", pool="0xpool")
    _decision(c, pid=1, sid=1, decided_at=iso(t0), action="CUT", outcome="closed", confirmed=1)
    c.close()

    ticks = str(tmp_path / "late_ticks.db")
    tc = sqlite3.connect(ticks); tc.row_factory = sqlite3.Row
    t.ensure_tick_tables(tc); t.ensure_block_times(tc); ensure_feature_tables(tc)
    tc.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,token0_decimals,"
               "token1_decimals,resolved_at) VALUES('4663','0xpool','Q','B',1,6,18,?)", (iso(t0),))
    # The raw path starts after the decision. Its atomic ratio is not comparable to the exact $25
    # snapshot, so the report must censor instead of inventing a return from mixed units.
    for block, seconds, amount1 in ((1, 300, 120), (2, 900, 80), (3, 1800, 150)):
        t.insert_ticks(tc, [{"chain_id": "4663", "pool": "0xpool", "kind": "v2_swap",
                             "block_no": block, "tx_sig": f"s{block}", "log_index": 0,
                             "maker": "R", "amount0": "100", "amount1": str(amount1),
                             "observed_at": iso(t0 + seconds)}])
        t.backfill_block_times(tc, "4663", [block], lambda _b, ts=t0 + seconds: ts, iso(t0))
    tc.close()

    decisions, timeline, bands = R.load(path, str(tmp_path / "missing.db"), ticks_db=ticks)
    cell = R.counterfactuals(decisions, timeline, bands)[("CUT", "4663", "unknown")]
    assert all(cell["cf"][n]["returns"] == [] for n in (5, 15, 30))
    assert all(cell["cf"][n]["censored"] == 1 for n in (5, 15, 30))
