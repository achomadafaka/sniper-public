"""Lane A — the promotion shadow only admits what the funded gates would have admitted.

The lane exists because the paper lanes opened a position for every signal regardless of the gates:
2,242 of 2,346 paper rows in the week to 2026-09-23 came from attempts the funded path had REFUSED,
so a disarmed wallet's paper record answered "were its coins good" and never "would we have taken
them". These tests pin the distinction the lane is for.

THRESHOLDS ARE STATED, NEVER INHERITED FROM `.env`. A settings read without dotenv silently returns
the code default, which is looser than production, so a test that inherits real settings is testing
whichever environment it happens to run in. The mcap, source-buy and chase knobs come from `_cfg()`;
the VOLUME gate resolves its own floors from `sniper.settings.memecoin` because it is the funded
path's own function, so that one test sets the floor on that module and says why.
"""
import datetime as dt
import json
import sqlite3
import types
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_promotion_lane as lane

MARKET = {"market_cap": 120_000.0, "volume_h1": 50_000.0, "volume_m5": 4_000.0,
          "txns_m5_buys": 12, "txns_m5_sells": 9, "price": 1e-5, "liquidity": 20_000.0}
OPEN_LEDGER = {"cooldown_blocked": False, "losing_blocked": False, "winner_cap_blocked": None}


def _cfg(**over):
    """The production shape of every knob the lane reads, so a test states its own world."""
    base = dict(MEMECOIN_REAL_MAX_MCAP_USD=250_000.0, MEMECOIN_REAL_MIN_MCAP_USD=0.0,
                MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET={}, MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET={},
                MEMECOIN_REAL_MIN_SOURCE_BUY_USD=200.0, MEMECOIN_REAL_MAX_SOURCE_BUY_USD=0.0,
                MEMECOIN_REAL_CHASE_MAX_PCT=40.0)
    base.update(over)
    return types.SimpleNamespace(**base)


def _evaluate(market=None, *, size=900.0, ledger=None, cfg=None):
    return lane.evaluate_gates(
        facts={"coin_band": "micro"}, market=market or MARKET, source_size_usd=size,
        network_id="solana", coin_band="micro", source_user_id="u1",
        ledger=OPEN_LEDGER if ledger is None else ledger, cfg=cfg or _cfg())


def test_a_clean_signal_is_admitted():
    states, _inputs, refused = _evaluate()
    assert refused is None
    assert lane.verdict_of(states, refused) == "admitted"


@pytest.mark.parametrize("label,kwargs,family", [
    ("over the mcap ceiling", {"market": {**MARKET, "market_cap": 31_000_000.0}},
     "coin_band_unreservable"),
    ("source buy under the floor", {"size": 5.0}, "source_buy_floor"),
    ("inside the re-entry cooldown",
     {"ledger": {**OPEN_LEDGER, "cooldown_blocked": True}}, "reentry_cooldown"),
])
def test_a_gate_that_fails_refuses_and_names_itself(label, kwargs, family):
    """Each of these opened a paper position in the old lane. None of them may here."""
    states, _inputs, refused = _evaluate(**kwargs)
    assert lane.verdict_of(states, refused) == "refused", label
    assert refused[0] == family
    assert states[family] == lane.FAIL


def test_a_dead_coin_is_refused_by_the_volume_family(monkeypatch):
    """The volume gate reads the REAL settings module, not the cfg this test passes in.

    `volume_gate_args` and `volume_alive_decision` are the funded path's own functions and they
    resolve their floors from `sniper.settings.memecoin` themselves — which is exactly why this
    lane reuses them instead of restating the arithmetic, and why the floor has to be set on that
    module here. Under the bare code defaults the h1 floor is 0, i.e. the gate is OFF, so a test
    that did not set it would pass in CI and prove nothing about production.
    """
    import sniper.settings.memecoin as real_cfg

    monkeypatch.setattr(real_cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 3000.0, raising=False)
    states, _inputs, refused = _evaluate(market={**MARKET, "volume_h1": 0.0, "volume_m5": 0.0})
    assert lane.verdict_of(states, refused) == "refused"
    assert refused[0].startswith("m5_") or refused[0] == "m5_turnover_under_floor"


def test_a_market_read_that_says_nothing_is_unreadable_not_refused():
    """A read we could not make is never the gates saying no; counting it as one would make the
    lane's refusal rate a measure of our own provider luck."""
    with pytest.raises(lane.Unreadable):
        _evaluate(market={"market_cap": None, "volume_h1": None, "volume_m5": None})


def test_the_chase_families_are_recorded_and_never_decide():
    states, _inputs, refused = _evaluate()
    for family in lane.LAGGED:
        assert states[family] == lane.LAGGED_STATE
    # A chase gap far past any cap still cannot refuse: the quote is from after the decision.
    lagged = lane.lagged_chase(source_price=1.0, quote_price=5.0, cfg=_cfg())
    assert lagged["lagged_gap_pct"] == pytest.approx(400.0)
    assert lagged["would_have_failed_if_current"] is True
    assert lagged["state"] == lane.LAGGED_STATE
    assert lane.verdict_of(states, refused) == "admitted", "a lagged family must not refuse"


def test_an_unknown_family_neither_admits_nor_refuses_on_its_own():
    """`winner_cap_blocked` is None here: unanswerable, so `unknown`, and the verdict rests on the
    families that were actually evaluated."""
    states, _inputs, refused = _evaluate(ledger={**OPEN_LEDGER, "winner_cap_blocked": None})
    assert states["winner_reentry_daily_cap"] == lane.UNKNOWN
    assert lane.verdict_of(states, refused) == "admitted"


def test_every_named_family_has_a_state():
    """The fifteen families the funded shadow names must each be answered or explicitly not."""
    states, _inputs, _refused = _evaluate()
    covered = set(lane.REPRODUCIBLE) | set(lane.LAGGED) | set(lane.NOT_EVALUATED)
    assert covered == set(states)
    assert covered >= m.EXEMPTIBLE_GATE_FAMILIES, sorted(m.EXEMPTIBLE_GATE_FAMILIES - covered)


# --- the reentry cutoff, which is the one gate this lane implements itself -----------------------

def _ledger_db():
    """The funded ledger shape the re-entry readers query: coin-scoped, not wallet-scoped."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, source_user_id TEXT, "
                 "mint TEXT, network_id TEXT, execution_mode TEXT, is_open INTEGER, "
                 "opened_at TEXT, closed_at TEXT, realized_pnl REAL)")
    conn.execute("CREATE TABLE memecoin_fills (id INTEGER PRIMARY KEY, position_id INTEGER, "
                 "kind TEXT, qty REAL, notional REAL)")
    return conn


def _close(conn, pnl, closed_at, *, mint="M", network_id="solana", wallet="u1"):
    conn.execute("INSERT INTO memecoin_positions (source_user_id, mint, network_id, "
                 "execution_mode, is_open, opened_at, closed_at, realized_pnl) "
                 "VALUES (?,?,?,'real',0,?,?,?)",
                 (wallet, mint, network_id, closed_at, closed_at, pnl))
    conn.commit()


def _reentry_cfg(**over):
    """The funded re-entry knobs, STATED. Live values on 2026-09-23: cooldown 86,400 s, winners
    still cooled, losing block on with a 24 h lookback, winner daily cap 2."""
    base = dict(MEMECOIN_REAL_REENTRY_COOLDOWN_S=86_400.0,
                MEMECOIN_REENTRY_WINNER_UNBLOCKED=False,
                MEMECOIN_REAL_BLOCK_LOSING_REENTRY=True,
                MEMECOIN_REENTRY_WINNER_DAILY_CAP=2,
                losing_reentry_since_iso=lambda now: "2026-09-22T12:00:00+00:00")
    base.update(over)
    return types.SimpleNamespace(**base)


def _attempt(stamp="2026-09-23T12:00:00+00:00", mint="M", network_id="solana"):
    return {"attempt_id": "a", "source_user_id": "u1", "mint": mint, "network_id": network_id,
            "decision_started_at": stamp}


def test_a_position_closed_after_the_decision_does_not_inform_it():
    """The whole point of the cutoff: at 12:00 we did not know about a close at 12:30."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _ledger_db()
    _close(conn, -5.0, "2026-09-23T12:30:00+00:00")
    state = loop.reentry_state(conn, _attempt(), cfg_module=_reentry_cfg())
    assert state["cooldown_blocked"] is False
    assert state["losing_blocked"] is False


def test_the_latest_close_decides_not_the_whole_history():
    """The 2026-09-23 review's counterexample. The old shadow asked whether EVERY prior close lost,
    so a +$10 followed by a -$5 passed it while the funded latest-loss rule refuses."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _ledger_db()
    _close(conn, 10.0, "2026-09-23T10:00:00+00:00")
    _close(conn, -5.0, "2026-09-23T11:00:00+00:00")
    state = loop.reentry_state(conn, _attempt(), cfg_module=_reentry_cfg())
    assert state["losing_blocked"] is True


def test_a_loss_on_the_coin_from_another_wallet_still_blocks():
    """Funded re-entry is scoped to the COIN, whoever's signal opened it. The wallet-scoped version
    let a coin the book had already been beaten by through on a different wallet's signal."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _ledger_db()
    _close(conn, -5.0, "2026-09-23T11:00:00+00:00", wallet="someone-else")
    state = loop.reentry_state(conn, _attempt(), cfg_module=_reentry_cfg())
    assert state["losing_blocked"] is True
    assert state["cooldown_blocked"] is True


def test_the_same_address_on_another_chain_is_a_different_coin():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _ledger_db()
    _close(conn, -5.0, "2026-09-23T11:00:00+00:00", network_id="4663")
    state = loop.reentry_state(conn, _attempt(network_id="solana"), cfg_module=_reentry_cfg())
    assert state["losing_blocked"] is False
    assert state["cooldown_blocked"] is False


def test_an_inert_winner_cap_passes_rather_than_reading_unknown(monkeypatch):
    """`MEMECOIN_REENTRY_WINNER_UNBLOCKED` is 0 live, so the funded winner-cap branch cannot fire.
    An inert gate admitted; calling it unknown would count configuration as missing evidence.

    The knob is set on `sniper.settings.memecoin` as well as on the stub, because
    `recently_closed_real_coin_ids` is the funded path's own reader and resolves that knob from the
    real settings module whatever cfg it is handed -- and without dotenv that module answers with
    its CODE DEFAULT, which is the opposite of production here.
    """
    import sniper.settings.memecoin as real_cfg
    from scripts import memecoin_promotion_lane_loop as loop

    monkeypatch.setattr(real_cfg, "MEMECOIN_REENTRY_WINNER_UNBLOCKED", False, raising=False)
    conn = _ledger_db()
    _close(conn, 50.0, "2026-09-23T11:00:00+00:00")
    state = loop.reentry_state(conn, _attempt(), cfg_module=_reentry_cfg())
    assert state["winner_cap_blocked"] is False
    assert state["winner_cap_inert"] == "MEMECOIN_REENTRY_WINNER_UNBLOCKED=0", \
        "a readout must be able to tell an inert gate from one that ran and admitted"
    states, _inputs, refused = lane.evaluate_gates(
        facts={}, market=MARKET, source_size_usd=900.0, network_id="solana", coin_band="micro",
        source_user_id="u1", ledger=state, cfg=_cfg())
    assert states["winner_reentry_daily_cap"] == "pass"
    assert refused[0] == "reentry_cooldown", refused


def test_the_winner_daily_cap_fires_when_the_operator_unblocks_winners(monkeypatch):
    import sniper.settings.memecoin as real_cfg
    from scripts import memecoin_promotion_lane_loop as loop

    monkeypatch.setattr(real_cfg, "MEMECOIN_REENTRY_WINNER_UNBLOCKED", True, raising=False)
    conn = _ledger_db()
    _close(conn, 50.0, "2026-09-23T10:00:00+00:00")
    conn.execute("INSERT INTO memecoin_positions (source_user_id, mint, network_id, "
                 "execution_mode, is_open, opened_at) VALUES "
                 "('u1','M','solana','real',1,'2026-09-23T11:00:00+00:00')")
    conn.commit()
    cfg_on = _reentry_cfg(MEMECOIN_REENTRY_WINNER_UNBLOCKED=True,
                          MEMECOIN_REENTRY_WINNER_DAILY_CAP=2)
    state = loop.reentry_state(conn, _attempt(), cfg_module=cfg_on)
    assert state["winner_cap_blocked"] is True, "two campaigns today against a cap of two"


# --- the table ------------------------------------------------------------------------------------

def test_a_replayed_attempt_cannot_double_count_a_signal():
    conn = sqlite3.connect(":memory:")
    m.ensure_promotion_lane_table(conn)
    row = {"attempt_id": "a1", "network_id": "solana", "mint": "M", "verdict": "admitted",
           "decision_started_at": "2026-09-23T12:00:00+00:00",
           "evaluated_at": "2026-09-23T12:01:00+00:00", "eval_lag_s": 60.0,
           "created_at": "2026-09-23T12:01:00+00:00"}
    assert m.record_promotion_lane_row(conn, row)
    m.record_promotion_lane_row(conn, {**row, "verdict": "refused"})
    kept = conn.execute("SELECT verdict, eval_lag_s FROM memecoin_promotion_lane").fetchall()
    assert kept == [("admitted", 60.0)], "the first evaluation stands; a replay adds nothing"


def test_the_lag_is_a_column_not_an_assertion():
    """Every gate result here is read late, so no readout may quote one without the lag beside it."""
    conn = sqlite3.connect(":memory:")
    m.ensure_promotion_lane_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_promotion_lane)")}
    assert {"eval_lag_s", "horizon_s", "censored_by", "exit_series_n", "fill_basis"} <= cols


def test_a_stale_signal_is_never_scored_against_a_market_that_has_moved():
    """The whole not_approved history must never be gated on today's market."""
    import datetime as dt

    from scripts import memecoin_promotion_lane_loop as loop

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, origin_event_id TEXT, "
                 "network_id TEXT, mint TEXT, ticker TEXT, source_user_id TEXT, source_handle TEXT, "
                 "decision_started_at TEXT, source_price REAL, source_market_cap REAL, "
                 "chase_source_size_usd REAL, classification_json TEXT, terminal_reason TEXT, "
                 "terminal_status TEXT)")
    now = dt.datetime.now(dt.timezone.utc)
    for name, age_s in (("fresh", 60), ("two hours old", 7200)):
        conn.execute("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (name, "e", "solana", "M", "T", "u1", "h",
                      (now - dt.timedelta(seconds=age_s)).isoformat(),
                      1e-5, 1000.0, 900.0, "{}", "wallet not_approved", "refused"))
    conn.commit()
    assert [r["attempt_id"] for r in loop.pending_attempts(conn)] == ["fresh"]


def test_intake_retries_failed_rows_and_drains_timestamp_ties_after_a_full_page():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, origin_event_id TEXT, "
                 "network_id TEXT, mint TEXT, ticker TEXT, source_user_id TEXT, source_handle TEXT, "
                 "decision_started_at TEXT, source_price REAL, source_market_cap REAL, "
                 "chase_source_size_usd REAL, classification_json TEXT, terminal_reason TEXT, "
                 "terminal_status TEXT)")
    m.ensure_promotion_lane_table(conn)
    t1 = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)).isoformat()
    t2 = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30)).isoformat()
    for name, stamp in (("a0", t1), ("a1", t2), ("a2", t2), ("a3", t2)):
        conn.execute("INSERT INTO memecoin_entry_attempts "
                     "(attempt_id, network_id, mint, decision_started_at, terminal_reason, "
                     "terminal_status) VALUES (?,?,?,?,'not_approved','refused')",
                     (name, "solana", name, stamp))
    first = loop.pending_attempts(conn, limit=3)
    assert [r["attempt_id"] for r in first] == ["a0", "a1", "a2"]
    # a0 failed evaluation; the other two were persisted, leaving a0 and a3 unrecorded.
    for row in first[1:]:
        m.record_promotion_lane_row(conn, {
            "attempt_id": row["attempt_id"], "network_id": "solana", "mint": row["mint"],
            "decision_started_at": row["decision_started_at"], "evaluated_at": t2,
            "created_at": t2, "verdict": "refused"})
    second = loop.pending_attempts(conn, limit=3)
    assert [r["attempt_id"] for r in second] == ["a0", "a3"]


# --- the fill is settled after the horizon, never guessed at evaluation ------------------------

def _settleable_db():
    """A lane table with one admitted row, plus the quote ledger the settle pass reads."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_promotion_lane_table(conn)
    conn.execute("CREATE TABLE memecoin_quote_ledger (mint TEXT, ts_minute TEXT, ts TEXT, "
                 "price REAL, liquidity REAL, source TEXT, quality TEXT, network_id TEXT)")
    m.record_promotion_lane_row(conn, {
        "attempt_id": "a1", "network_id": "solana", "mint": "M", "verdict": "admitted",
        "decision_started_at": "2026-09-23T12:00:00+00:00",
        "evaluated_at": "2026-09-23T12:00:44+00:00", "eval_lag_s": 44.0,
        "clip_usd": 100.0, "fill_basis": "pending", "horizon_target_s": 600.0,
        "policy_id": "follow-30-50-v2",
        # The real field names off `_follow3050_policy`: `tp_pct`, not `take_profit_pct`.
        "policy_json": '{"policy_id": "follow-30-50-v2", "max_hold_s": 600.0, "tp_pct": 25.0, '
                       '"stop_pct": 35.0, "catastrophe_pct": 80.0, "stop_debounce_sweeps": 1, '
                       '"derisk_mode": "off", "derisk_pct": 0.0, "derisk_fraction": 0.0, '
                       '"trail_pct": 0.0, "trail_arm_pct": 0.0, "trail_requires_derisk": false, '
                       '"mirror_on_sell": false, "unquotable_exit_s": 86400.0}',
        "created_at": "2026-09-23T12:00:44+00:00"})
    conn.commit()
    return conn


def _quote(conn, ts, price, *, mint="M", network_id="solana"):
    conn.execute("INSERT INTO memecoin_quote_ledger (mint, ts_minute, ts, price, network_id) "
                 "VALUES (?,?,?,?,?)", (mint, ts[:16], ts, price, network_id))
    conn.commit()


def _at(minute, second=0):
    import datetime as dt
    return dt.datetime(2026, 9, 23, 12, minute, second, tzinfo=dt.timezone.utc)


def test_an_admitted_row_is_pending_at_evaluation_not_filled(monkeypatch):
    """The window is still in the future when the gates are read, so a fill here would be a guess.

    The first supervised pass wrote four admitted rows and all four found an empty series; one of
    them (JIZZCOIN) had six ledger rows minutes later.
    """
    from scripts import memecoin_promotion_lane_loop as loop

    monkeypatch.setattr(loop, "market_read", lambda mint, network_id: dict(MARKET))
    monkeypatch.setattr(loop, "reentry_state", lambda conn, attempt, **kw: dict(OPEN_LEDGER))
    monkeypatch.setattr(loop, "exit_series", lambda *a, **k: pytest.fail(
        "evaluation must not read the exit series"))
    attempt = {"attempt_id": "a1", "network_id": "solana", "mint": "M", "ticker": "T",
               "source_user_id": "u1", "decision_started_at": "2026-09-23T12:00:00+00:00",
               "source_price": 1e-5, "chase_source_size_usd": 900.0,
               "classification_json": '{"facts": {"coin_band": "micro"}}'}
    row = loop.evaluate_one(attempt, ro_conn=None, now=_at(0, 44), cfg_module=_cfg())
    assert row["verdict"] == "admitted"
    assert row["fill_basis"] == "pending"
    assert row["horizon_target_s"] > 0, "the settle pass needs to know when this row is due"
    assert row.get("fill_price") is None and row.get("exit_reason") is None


def test_a_row_inside_its_horizon_is_not_settled_yet():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    _quote(conn, "2026-09-23T12:01:00+00:00", 1.0)
    settled, not_due, skipped = loop.settle_pending(conn, conn, now=_at(5))
    assert (settled, not_due, skipped) == (0, 1, 0)
    row = conn.execute("SELECT fill_basis, settled_at FROM memecoin_promotion_lane").fetchone()
    assert tuple(row) == ("pending", None), \
        "a row priced before its horizon would censor its own winners"


def test_an_admitted_row_settles_to_quoted_when_the_ledger_priced_the_mint():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    for minute, price in ((1, 1.0), (3, 1.10), (6, 1.05), (9, 1.02)):
        _quote(conn, "2026-09-23T12:%02d:00+00:00" % minute, price)
    settled, not_due, skipped = loop.settle_pending(conn, conn, now=_at(20))
    assert (settled, not_due, skipped) == (1, 0, 0)
    row = conn.execute("SELECT fill_basis, fill_price, exit_series_n, exit_series_source, "
                       "settled_at, horizon_s FROM memecoin_promotion_lane").fetchone()
    assert row["fill_basis"] == "quoted"
    assert row["fill_price"] == 1.0, "the fill is the first quote at or after the decision"
    assert row["exit_series_n"] == 4
    assert row["exit_series_source"] == "memecoin_quote_ledger"
    assert row["settled_at"] is not None and row["horizon_s"] > 0


def test_an_admitted_row_settles_to_unquoted_never_none_when_nothing_priced_it():
    """`none` read as "no fill step ran" when it meant "no quote existed". Lane A admits the coins
    we did NOT take, and the ledger covers 1 of 113 mints we never held, so this is the common
    case and it must not be mistaken for a broken worker."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    settled, _not_due, _skipped = loop.settle_pending(conn, conn, now=_at(20))
    assert settled == 1
    row = conn.execute("SELECT fill_basis, exit_series_n, censored_by, settled_at, pnl_usd, "
                       "exit_series_source FROM memecoin_promotion_lane").fetchone()
    assert row["fill_basis"] == "unquoted"
    assert row["fill_basis"] != "none"
    assert (row["exit_series_n"], row["censored_by"], row["pnl_usd"]) == (0, "tape", None)
    assert row["settled_at"] is not None
    assert row["exit_series_source"] == "unpriced_by_any", \
        "naming the ledger here would read as the ledger having priced the row"


def test_a_take_profit_inside_the_horizon_books_a_pnl():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    for minute, price in ((1, 1.0), (3, 1.0), (5, 1.40)):
        _quote(conn, "2026-09-23T12:%02d:00+00:00" % minute, price)
    assert loop.settle_pending(conn, conn, now=_at(20))[0] == 1
    row = conn.execute("SELECT fill_basis, exit_reason, pnl_usd, pnl_pct, censored_by "
                       "FROM memecoin_promotion_lane").fetchone()
    assert row["fill_basis"] == "quoted"
    assert row["exit_reason"] not in (None, "open_at_horizon", "no_fill"), row["exit_reason"]
    assert row["pnl_usd"] > 0 and row["censored_by"] is None


def test_a_second_settle_pass_is_a_no_op_not_a_second_opinion():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    _quote(conn, "2026-09-23T12:01:00+00:00", 1.0)
    assert loop.settle_pending(conn, conn, now=_at(20))[0] == 1
    first = conn.execute("SELECT settled_at, fill_price FROM memecoin_promotion_lane").fetchone()
    _quote(conn, "2026-09-23T12:02:00+00:00", 99.0)
    assert loop.settle_pending(conn, conn, now=_at(30)) == (0, 0, 0)
    again = conn.execute("SELECT settled_at, fill_price FROM memecoin_promotion_lane").fetchone()
    assert tuple(again) == tuple(first), "the first price stands; a replay adds nothing"


def test_the_legacy_none_rows_are_settleable():
    """Four rows written before the settle pass existed carry `fill_basis='none'` and no price."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    conn.execute("UPDATE memecoin_promotion_lane SET fill_basis='none', exit_series_n=0, "
                 "horizon_target_s=NULL")
    conn.commit()
    _quote(conn, "2026-09-23T12:01:00+00:00", 1.0)
    # No `horizon_target_s`: the hold comes from the policy the row was ADMITTED with, never a
    # fresh read of today's timecap.
    assert loop.settle_pending(conn, conn, now=_at(20))[0] == 1
    assert conn.execute("SELECT fill_basis FROM memecoin_promotion_lane").fetchone()[0] == "quoted"


def test_a_settle_reads_only_its_own_chain():
    """The ledger carries NULL `network_id` on legacy rows, so a bare mint match crosses chains."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    _quote(conn, "2026-09-23T12:01:00+00:00", 7.0, network_id="4663")
    assert loop.settle_pending(conn, conn, now=_at(20))[0] == 1
    row = conn.execute("SELECT fill_basis, exit_series_n FROM memecoin_promotion_lane").fetchone()
    assert (row["fill_basis"], row["exit_series_n"]) == ("unquoted", 0)


def test_a_settle_never_rewrites_what_the_gates_decided():
    """A late price may not move a verdict: the gates were read at decision time and that stands."""
    conn = _settleable_db()
    conn.execute("UPDATE memecoin_promotion_lane SET verdict='refused', "
                 "refused_family='coin_band_unreservable'")
    conn.commit()
    assert m.settle_promotion_lane_row(conn, "a1", {
        "fill_basis": "quoted", "fill_price": 1.0, "settled_at": "2026-09-23T13:00:00+00:00"}) \
        is False
    row = conn.execute("SELECT verdict, fill_basis, settled_at FROM memecoin_promotion_lane").fetchone()
    assert (row["verdict"], row["fill_basis"], row["settled_at"]) == \
        ("refused", "pending", None)


# --- the lane's own price samples ---------------------------------------------------------------

def _sampling_db(rows=(("a1", "solana", "M"),)):
    """A lane table with admitted rows inside their horizon, plus the tables the sampler reads."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_promotion_lane_table(conn)
    m.ensure_promotion_lane_samples_table(conn)
    conn.execute("CREATE TABLE memecoin_positions (mint TEXT, network_id TEXT, "
                 "execution_mode TEXT, is_open INTEGER)")
    conn.execute("CREATE TABLE memecoin_quote_ledger (mint TEXT, ts_minute TEXT, ts TEXT, "
                 "price REAL, liquidity REAL, source TEXT, quality TEXT, network_id TEXT)")
    for attempt_id, network_id, mint in rows:
        m.record_promotion_lane_row(conn, {
            "attempt_id": attempt_id, "network_id": network_id, "mint": mint,
            "verdict": "admitted", "decision_started_at": "2026-09-23T12:00:00+00:00",
            "evaluated_at": "2026-09-23T12:00:44+00:00", "clip_usd": 100.0,
            "fill_basis": "pending", "horizon_target_s": 600.0,
            "created_at": "2026-09-23T12:00:44+00:00"})
    conn.commit()
    return conn


def _quoter(monkeypatch, payload, reason=None):
    from sniper.services import memecoin_quotes as quotes

    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed",
                        lambda mint, usd, **kw: (payload, reason, "api.jup.ag"), raising=False)


@pytest.mark.parametrize("lag", [0, 0.01, -1, None, "bad", float("nan"), float("inf")])
def test_frozen_fill_never_reselects_a_competing_price(monkeypatch, lag):
    from scripts import memecoin_promotion_lane_loop as loop
    conn = _sampling_db()
    row = m.promotion_lane_rows_open_for_sampling(conn)[0]
    row.update(exit_series_source=loop.SAMPLE_BASIS, fill_price=2.0, fill_lag_s=lag)
    monkeypatch.setattr(loop, "exit_series", lambda *args: [(_at(0).timestamp(), 100.0)])
    series, source = loop.price_series(conn, row, _at(1).isoformat())
    assert source == loop.SAMPLE_BASIS
    expected = [(_at(0).timestamp() + lag, 2.0)] if lag in (0, 0.01) else []
    assert series == expected
    conn.close()


@pytest.mark.parametrize("samples_table", [True, False])
def test_unsupported_page_cannot_starve_solana_sampler(monkeypatch, samples_table):
    from scripts import memecoin_promotion_lane_loop as loop
    conn = _sampling_db(rows=tuple([(f"e{i}", "4663", f"E{i}") for i in range(200)]
                                   + [("sol", "solana", "S")]))
    conn.execute("UPDATE memecoin_promotion_lane SET decision_started_at=? WHERE attempt_id='sol'",
                 (_at(0, 1).isoformat(),))
    if not samples_table:
        conn.execute("DROP TABLE memecoin_promotion_lane_samples")
    asked = []
    monkeypatch.setattr(loop, "sample_one", lambda row, **kw: asked.append(row["mint"]))
    loop.sample_pending(conn, conn, now=_at(1))
    assert asked == ["S"]
    # The generic selector must still support consumers on all chains.
    assert len(m.promotion_lane_rows_open_for_sampling(conn)) == 200
    conn.close()


def test_a_lane_sample_is_priced_in_atomic_units_and_says_so(monkeypatch):
    """USD per ATOMIC token: converting to whole tokens needs the mint's decimals, and that is a
    Solana RPC read this worker may not make unattended."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    _quoter(monkeypatch, {"outAmount": "2000000000"})
    counts = loop.sample_pending(conn, conn, now=_at(1))
    assert counts["sampled"] == 1 and counts["errors"] == 0
    row = conn.execute("SELECT price, price_basis, price_unit, quote_usd, out_atomic, source, "
                       "reason FROM memecoin_promotion_lane_samples").fetchone()
    assert row["price"] == pytest.approx(100.0 / 2_000_000_000)
    assert (row["price_basis"], row["price_unit"]) == ("lane_quote", "usd_per_atomic_token")
    assert (row["quote_usd"], row["out_atomic"], row["reason"]) == (100.0, "2000000000", None)


def test_a_failed_quote_is_recorded_with_its_reason_never_as_a_zero(monkeypatch):
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    _quoter(monkeypatch, None, reason="no_route")
    counts = loop.sample_pending(conn, conn, now=_at(1))
    assert (counts["sampled"], counts["errors"]) == (0, 1)
    row = conn.execute("SELECT price, reason FROM memecoin_promotion_lane_samples").fetchone()
    assert row["price"] is None, "a quote that did not happen is not a price of zero"
    assert row["reason"] == "no_route"


def test_the_sampler_skips_a_mint_we_are_holding_funded(monkeypatch):
    """A lane quote on a held mint is a second reader on the route the exit rail needs."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    conn.execute("INSERT INTO memecoin_positions VALUES ('M','solana','real',1)")
    conn.commit()
    _quoter(monkeypatch, {"outAmount": "1"})
    counts = loop.sample_pending(conn, conn, now=_at(1))
    assert (counts["sampled"], counts["skipped_held"]) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_promotion_lane_samples").fetchone()[0] == 0


def test_a_closed_or_paper_position_does_not_block_sampling(monkeypatch):
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    conn.execute("INSERT INTO memecoin_positions VALUES ('M','solana','real',0)")
    conn.execute("INSERT INTO memecoin_positions VALUES ('M','solana','paper',1)")
    conn.commit()
    _quoter(monkeypatch, {"outAmount": "1000"})
    counts = loop.sample_pending(conn, conn, now=_at(1))
    assert (counts["sampled"], counts["skipped_held"]) == (1, 0)


def test_an_evm_row_is_ineligible_because_the_pool_tape_prices_it(monkeypatch):
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db(rows=(("a1", "4663", "0xM"),))
    _quoter(monkeypatch, {"outAmount": "1000"})
    counts = loop.sample_pending(conn, conn, now=_at(1))
    assert (counts["sampled"], counts["skipped"]) == (0, 0)


def test_a_row_past_its_horizon_is_settles_business_not_the_samplers(monkeypatch):
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    _quoter(monkeypatch, {"outAmount": "1000"})
    counts = loop.sample_pending(conn, conn, now=_at(30))   # horizon_target_s is 600
    assert counts["sampled"] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_promotion_lane_samples").fetchone()[0] == 0


def test_the_per_pass_cap_is_counted_not_silently_dropped(monkeypatch):
    """`sampled` is a fact about our queue while the cap binds, so a readout needs `capped`."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db(rows=tuple((f"a{i}", "solana", f"M{i}") for i in range(25)))
    _quoter(monkeypatch, {"outAmount": "1000"})
    counts = loop.sample_pending(conn, conn, now=_at(1))
    assert (counts["sampled"], counts["capped"]) == (20, 5)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_promotion_lane_samples").fetchone()[0] == 20


def test_a_blind_open_position_read_samples_nothing(monkeypatch):
    """Sampling while we cannot tell what we hold could land a quote on a held mint."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    conn.execute("DROP TABLE memecoin_positions")
    conn.commit()
    _quoter(monkeypatch, {"outAmount": "1000"})
    counts = loop.sample_pending(conn, conn, now=_at(1))
    assert (counts["sampled"], counts["errors"]) == (0, 1)


def test_settle_prices_from_lane_samples_when_the_ledger_never_saw_the_mint():
    """The whole point of the sampler: the ledger covered 1 of 113 mints we never held."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    for minute, price in ((1, 5e-8), (3, 6.5e-8)):
        m.record_promotion_lane_sample(conn, {
            "attempt_id": "a1", "network_id": "solana", "mint": "M",
            "sampled_at": "2026-09-23T12:%02d:00+00:00" % minute, "price": price,
            "price_basis": "lane_quote", "price_unit": "usd_per_atomic_token"})
    assert loop.settle_pending(conn, conn, now=_at(20))[0] == 1
    row = conn.execute("SELECT fill_basis, exit_series_source, exit_series_n, fill_lag_s, "
                       "exit_reason, pnl_pct FROM memecoin_promotion_lane").fetchone()
    assert (row["fill_basis"], row["exit_series_source"]) == ("quoted", "lane_quote")
    assert row["exit_series_n"] == 2
    assert row["fill_lag_s"] == pytest.approx(60.0), "the fill is a minute after the decision"
    assert row["pnl_pct"] == pytest.approx(30.0), "+30% is exact in either unit"


def test_settle_never_mixes_a_ledger_price_with_a_lane_sample():
    """The ledger is USD per whole token and a lane sample is USD per ATOMIC token. One series
    holding both would produce a PnL that is pure unit error."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    _quote(conn, "2026-09-23T12:01:00+00:00", 1.0)
    m.record_promotion_lane_sample(conn, {
        "attempt_id": "a1", "network_id": "solana", "mint": "M",
        "sampled_at": "2026-09-23T12:02:00+00:00", "price": 5e-8,
        "price_basis": "lane_quote", "price_unit": "usd_per_atomic_token"})
    assert loop.settle_pending(conn, conn, now=_at(20))[0] == 1
    row = conn.execute("SELECT exit_series_source, exit_series_n, fill_price "
                       "FROM memecoin_promotion_lane").fetchone()
    assert row["exit_series_source"] == "memecoin_quote_ledger"
    assert (row["exit_series_n"], row["fill_price"]) == (1, 1.0)


def test_an_unpriced_sample_stays_out_of_the_series_but_not_out_of_the_record():
    """The reader returns the failure so the replay can stop at it; the SERIES still has no bar.

    Changed 2026-09-23 (Codex review N5): filtering the failure away in the reader is what let a
    carried price satisfy a second stop confirmation, so the drop happens where the series is
    built and the reason survives for the replay to see.
    """
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    m.record_promotion_lane_sample(conn, {
        "attempt_id": "a1", "network_id": "solana", "mint": "M",
        "sampled_at": "2026-09-23T12:01:00+00:00", "price": None, "reason": "no_route"})
    rows = m.promotion_lane_samples(conn, "a1")
    assert [(r["price"], r["reason"]) for r in rows] == [(None, "no_route")]
    row = {"attempt_id": "a1", "network_id": "solana", "mint": "M",
           "decision_started_at": "2026-09-23T12:00:00+00:00"}
    assert loop.price_series(conn, row, _at(20).isoformat()) == ([], loop.UNPRICED_SOURCE)


def test_the_cap_never_starves_a_row_that_has_no_fill_yet(monkeypatch):
    """The fill IS the first sample, so a backlog holding the whole cap would not lose a price --
    it would move the fill hours past the decision and change what the row measures."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db(rows=tuple((f"old{i}", "solana", f"M{i}") for i in range(20)))
    for i in range(20):      # every older row already has a fill
        m.record_promotion_lane_sample(conn, {
            "attempt_id": f"old{i}", "network_id": "solana", "mint": f"M{i}",
            "sampled_at": "2026-09-23T12:00:30+00:00", "price": 1e-8})
    m.record_promotion_lane_row(conn, {
        "attempt_id": "fresh", "network_id": "solana", "mint": "NEW", "verdict": "admitted",
        "decision_started_at": "2026-09-23T12:00:30+00:00",
        "evaluated_at": "2026-09-23T12:01:00+00:00", "clip_usd": 100.0, "fill_basis": "pending",
        "horizon_target_s": 600.0, "created_at": "2026-09-23T12:01:00+00:00"})
    conn.commit()
    _quoter(monkeypatch, {"outAmount": "1000"})
    counts = loop.sample_pending(conn, conn, now=_at(2))
    assert counts["sampled"] == 20 and counts["capped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memecoin_promotion_lane_samples "
                        "WHERE attempt_id='fresh'").fetchone()[0] == 1, \
        "the row with no fill yet must be quoted before any row that already has one"


# --- R1: a pending outcome completes; the deadline runs from the fill ---------------------------

def _horizon_db(horizon_s=180.0):
    conn = _sampling_db()
    conn.execute("UPDATE memecoin_promotion_lane SET horizon_target_s=?", (horizon_s,))
    conn.commit()
    return conn


def test_a_later_tape_resolves_a_pending_outcome_instead_of_freezing_it():
    """R1. The old worker evaluated once against whatever tape existed at its first poll and the
    INSERT OR IGNORE made that first answer permanent, so a take-profit that happened afterwards
    could never be recorded."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _horizon_db()
    _quote(conn, "2026-09-23T12:00:10+00:00", 1.0)
    _quote(conn, "2026-09-23T12:00:40+00:00", 1.0)
    assert loop.settle_pending(conn, conn, now=_at(1)) == (0, 1, 0)
    assert conn.execute("SELECT fill_basis FROM memecoin_promotion_lane").fetchone()[0] == "pending"

    _quote(conn, "2026-09-23T12:02:10+00:00", 1.5)      # +50% against a 25% take-profit
    assert loop.settle_pending(conn, conn, now=_at(10))[0] == 1
    row = conn.execute("SELECT fill_basis, exit_reason, pnl_pct, exit_series_n "
                       "FROM memecoin_promotion_lane").fetchone()
    assert row["fill_basis"] == "quoted"
    assert row["exit_reason"] not in (None, "open_at_horizon", "no_fill"), row["exit_reason"]
    assert row["pnl_pct"] == pytest.approx(50.0)
    assert row["exit_series_n"] == 3


def test_the_deadline_runs_from_the_fill_not_from_the_decision():
    """A fill lands after the decision -- 44 s on the first admitted row that had any tape. Anchoring
    the horizon on the decision would censor every row early by exactly its own fill lag."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _horizon_db()
    _quote(conn, "2026-09-23T12:01:00+00:00", 1.0)      # the fill, 60 s after the decision
    _quote(conn, "2026-09-23T12:03:30+00:00", 1.0)
    # decision + 180 s has passed, fill + 180 s has not.
    assert loop.settle_pending(conn, conn, now=_at(3, 30)) == (0, 1, 0)
    assert loop.settle_pending(conn, conn, now=_at(4, 30)) == (0, 1, 0)
    assert loop.settle_pending(conn, conn, now=_at(7, 1))[0] == 1
    row = conn.execute("SELECT fill_lag_s, horizon_s, censored_by, exit_series_n "
                       "FROM memecoin_promotion_lane").fetchone()
    assert row["fill_lag_s"] == pytest.approx(60.0)
    assert row["exit_series_n"] == 2


def test_a_mint_nothing_ever_quoted_settles_rather_than_waiting_forever():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _horizon_db()
    assert loop.settle_pending(conn, conn, now=_at(1)) == (0, 1, 0)
    assert loop.settle_pending(conn, conn, now=_at(10))[0] == 1
    row = conn.execute("SELECT fill_basis, exit_series_source FROM memecoin_promotion_lane").fetchone()
    assert (row["fill_basis"], row["exit_series_source"]) == ("unquoted", "unpriced_by_any")


def test_the_funded_readers_are_byte_identical_without_an_as_of_bound():
    """`before_iso` is for the shadow. Every funded caller omits it, and when it is omitted the SQL
    TEXT and the bound parameters must be exactly what the funded path has always executed -- an
    as-of bound that leaked into the live entry gate would change which coins it refuses."""
    class Row(dict):
        """Answers both a positional read (the campaign count) and a column read (the last close)."""
        def __getitem__(self, key):
            return 0 if isinstance(key, int) else None

    class Result(list):
        def fetchone(self):
            return Row()

    class Recorder:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=()):
            self.calls.append((sql, tuple(params)))
            return Result()

    now = "2026-09-23T12:00:00+00:00"
    rec = Recorder()
    m.recently_closed_real_coin_ids(rec, now, 86_400.0)
    assert rec.calls[0][0] == (
        "SELECT network_id, mint, closed_at, realized_pnl FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=0 AND closed_at >= ? ORDER BY closed_at")
    assert rec.calls[0][1] == ("2026-09-22T12:00:00",)

    rec = Recorder()
    m.losing_real_coin_ids(rec, since_iso="2026-09-22T12:00:00+00:00")
    assert rec.calls[0][0] == (
        "SELECT network_id, mint, realized_pnl, MAX(closed_at) FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=0 AND closed_at IS NOT NULL "
        "AND closed_at>=? GROUP BY network_id, mint")
    assert rec.calls[0][1] == ("2026-09-22T12:00:00+00:00",)

    rec = Recorder()
    m.real_campaigns_today_for_coin(rec, "solana", "M", now)
    assert rec.calls[0][0].endswith("AND p.opened_at >= ?"), rec.calls[0][0]
    assert rec.calls[0][1] == ("M", "solana", "2026-09-23T00:00:00")

    rec = Recorder()
    m.last_real_close_for_coin(rec, "solana", "M")
    assert rec.calls[0][0].endswith("ORDER BY p.closed_at DESC, f.id DESC LIMIT 1")
    assert " AND p.closed_at <= ?" not in rec.calls[0][0]
    assert rec.calls[0][1] == ("M", "solana")

    # And with the bound, each one carries exactly one more parameter.
    for call, extra in ((lambda r: m.recently_closed_real_coin_ids(r, now, 86_400.0,
                                                                   before_iso=now), 2),
                        (lambda r: m.losing_real_coin_ids(r, since_iso=now, before_iso=now), 2),
                        (lambda r: m.real_campaigns_today_for_coin(r, "solana", "M", now,
                                                                   before_iso=now), 4),
                        (lambda r: m.last_real_close_for_coin(r, "solana", "M",
                                                              before_iso=now), 3)):
        rec = Recorder()
        call(rec)
        assert len(rec.calls[0][1]) == extra, rec.calls[0]
        assert "<=?" in rec.calls[0][0].replace(" <= ", " <=").replace("<= ?", "<=?"), \
            rec.calls[0][0]


# --- R4: the whole source-buy family, one evaluator, two callers -------------------------------

def _legacy_source_buy_reason(their_usd, source_mcap, prior_net, ratio, unknown_mcap_floor,
                              abs_min, abs_max, family_active, is_manual=False):
    """The funded arithmetic EXACTLY as it read inline at memecoin_real_runner.py:2068-2167 before
    the extraction, copied here as the golden reference. This is not a restatement of the rule for
    its own sake: it is the previous implementation, kept so a drift in the extracted function
    fails a test rather than a funded entry.
    """
    from sniper.services.memecoin_real_runner import source_buy_band_reason as band

    source_reason = None
    if not is_manual and family_active:
        if source_mcap is not None and ratio > 0:
            source_floor = source_mcap * ratio
            judged_usd = their_usd
            if their_usd is not None and prior_net is not None and prior_net > 0:
                judged_usd = their_usd + prior_net
            ratio_pct = None if judged_usd is None else 100.0 * judged_usd / source_mcap
            if judged_usd is None or judged_usd < source_floor:
                if judged_usd is None:
                    rendered = "unknown"
                elif judged_usd != their_usd:
                    rendered = "$%.0f + $%.0f earlier = $%.0f (%.3f%%)" % (
                        their_usd, judged_usd - their_usd, judged_usd, ratio_pct)
                else:
                    rendered = "$%.0f (%.3f%%)" % (their_usd, ratio_pct)
                source_reason = "source buy %s under mcap floor %.3f%% ($%.0f)" % (
                    rendered, 100.0 * ratio, source_floor)
        elif source_mcap is None and unknown_mcap_floor > 0:
            if their_usd is None or their_usd < unknown_mcap_floor:
                rendered = "unknown" if their_usd is None else "$%.0f" % their_usd
                source_reason = "source buy %s under unknown-mcap floor $%.0f" % (
                    rendered, unknown_mcap_floor)
    if source_reason is None and their_usd is not None and not is_manual:
        # ONE DELIBERATE CHANGE since the extraction (operator 2026-10-02, degen_ak AGNET 4663): the
        # absolute FLOOR judges the same net the ratio floor judges (only ever raised); the ceiling
        # stays on the single buy.
        net = their_usd + prior_net if prior_net is not None and prior_net > 0 else None
        source_reason = band(their_usd, abs_min, abs_max, judged_usd=net)
    return source_reason


SOURCE_BUY_GRID = [
    (their, mcap, prior, ratio, unknown_floor, abs_min, abs_max, active, manual)
    for their in (None, 0.0, 100.0, 900.0, 5000.0)
    for mcap in (None, 200_000.0)
    for prior in (None, 0.0, 700.0)
    for ratio in (0.0, 0.008)
    for unknown_floor in (0.0, 100.0)
    for abs_min in (0.0, 200.0)
    for abs_max in (0.0, 2500.0)
    for active in (True, False)
    for manual in (True, False)
]


def test_the_extracted_family_answers_exactly_what_the_inline_code_answered():
    """The extraction may not change one funded refusal. Drives both implementations over the grid
    the review's four legs span: unknown/zero/under/over size, known and unknown cap, no prior net
    and a positive one, the ratio and the unknown-cap floor on and off, and manual."""
    from sniper.services import memecoin_real_runner as rr

    for (their, mcap, prior, ratio, unknown_floor, abs_min, abs_max, active, manual) \
            in SOURCE_BUY_GRID:
        new, _info = rr.source_buy_family_reason(
            their_usd=their, source_mcap=mcap, prior_net_usd=prior, ratio=ratio,
            unknown_mcap_floor=unknown_floor, abs_min=abs_min, abs_max=abs_max,
            family_active=active, is_manual=manual)
        old = _legacy_source_buy_reason(their, mcap, prior, ratio, unknown_floor, abs_min,
                                        abs_max, active, manual)
        assert new == old, (their, mcap, prior, ratio, unknown_floor, abs_min, abs_max, active,
                            manual, new, old)


def test_the_review_counterexample_now_refuses_in_the_shadow(monkeypatch):
    """R4. Cap $200,000, source buy $900, absolute minimum $200, ratio 0.008: funded needs $1,600
    and refuses. The shadow ran only the absolute band, so $900 cleared the $200 floor and passed."""
    from sniper.settings import memecoin as real_cfg
    monkeypatch.setattr(real_cfg, "MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT", 2.0)
    cfg = _cfg(MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO=0.008,
               MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD=0.0,
               MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND={})
    states, inputs, refused = lane.evaluate_gates(
        facts={}, market=MARKET, source_size_usd=900.0, network_id="solana", coin_band="micro",
        source_user_id="u1", ledger=OPEN_LEDGER, cfg=cfg, source_mcap=200_000.0)
    assert states["source_buy_floor"] == "fail"
    assert refused[0] == "source_buy_floor"
    assert "under mcap floor" in refused[1]
    assert inputs["source_buy_floor_usd"] == pytest.approx(1_600.0)
    assert inputs["source_buy_effective_floor_usd"] == pytest.approx(1_568.0)
    assert inputs["source_buy_floor_tolerance_pct"] == 2.0


def test_an_earlier_net_buy_into_the_same_coin_clears_the_floor():
    """The cumulative leg, operator 2026-09-13: a trader who scales in is judged on the total."""
    cfg = _cfg(MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO=0.008,
               MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD=0.0,
               MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND={})
    states, inputs, _refused = lane.evaluate_gates(
        facts={}, market=MARKET, source_size_usd=900.0, network_id="solana", coin_band="micro",
        source_user_id="u1", ledger=OPEN_LEDGER, cfg=cfg, source_mcap=200_000.0,
        prior_net_usd=800.0)
    assert states["source_buy_floor"] == "pass"
    assert inputs["source_buy_judged_usd"] == pytest.approx(1_700.0)


def test_the_band_override_decides_the_ratio_for_this_chain_and_size():
    """Live config carries {'nano': 0.005, 'solana:micro': 0.0025}; the chain-qualified key wins."""
    cfg = _cfg(MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO=0.008,
               MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD=0.0,
               MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND={"solana:micro": 0.0025})
    states, inputs, _refused = lane.evaluate_gates(
        facts={}, market=MARKET, source_size_usd=900.0, network_id="solana", coin_band="micro",
        source_user_id="u1", ledger=OPEN_LEDGER, cfg=cfg, source_mcap=200_000.0)
    assert inputs["source_buy_ratio"] == pytest.approx(0.0025)
    assert inputs["source_buy_floor_usd"] == pytest.approx(500.0)
    assert states["source_buy_floor"] == "pass", "$900 clears a $500 floor"


def test_wallet_ratio_zero_override_matches_funded_rules_after_band_selection():
    cfg = _cfg(MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO=.008,
               MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND={'solana:micro': .02},
               MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET={'u1': 0.0})
    states, inputs, _ = lane.evaluate_gates(
        facts={}, market=MARKET, source_size_usd=900., network_id='solana', coin_band='micro',
        source_user_id='u1', ledger=OPEN_LEDGER, cfg=cfg, source_mcap=200_000.)
    assert states['source_buy_floor'] == 'pass'
    assert inputs['source_buy_ratio'] == 0.0


def test_an_unknown_cap_refuses_on_the_unknown_cap_floor_rather_than_passing():
    cfg = _cfg(MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO=0.008,
               MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD=1_000.0,
               MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND={},
               MEMECOIN_REAL_MIN_SOURCE_BUY_USD=0.0)
    states, _inputs, refused = lane.evaluate_gates(
        facts={}, market=MARKET, source_size_usd=900.0, network_id="solana", coin_band="micro",
        source_user_id="u1", ledger=OPEN_LEDGER, cfg=cfg, source_mcap=None)
    assert states["source_buy_floor"] == "fail"
    assert "unknown-mcap floor" in refused[1]


def test_the_shadow_judges_the_cap_the_source_saw_not_the_late_market_read():
    """The funded leg judges against the classification's own mcap. A read taken minutes later is a
    different number from a different moment, and using it would compare their buy to a cap that did
    not exist when they made it."""
    from scripts import memecoin_promotion_lane_loop as loop

    assert loop.source_buy_mcap({"source_market_cap": 50_000.0},
                                {"effective_mcap": 200_000.0}) == 200_000.0
    assert loop.source_buy_mcap({"source_market_cap": 50_000.0}, {}) == 50_000.0
    assert loop.source_buy_mcap({}, {}) is None


# --- Codex recheck: R1, R2, R4, N4, N6, N8 ---------------------------------------------------

def test_a_null_chain_quote_is_not_assigned_to_the_requested_chain():
    """R2: a legacy bare-mint row has an UNKNOWN chain, and unknown is not every chain."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    _quote(conn, _at(1).isoformat(), 1000.0, network_id=None)
    assert loop.exit_series(conn, "M", "4663", _at(0).isoformat(), _at(2).isoformat()) == []


def test_a_quote_on_the_requested_chain_is_still_read():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    _quote(conn, _at(1).isoformat(), 7.0, network_id="4663")
    assert [price for _ts, price in
            loop.exit_series(conn, "M", "4663", _at(0).isoformat(), _at(2).isoformat())] == [7.0]


def _events_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_events (fomo_event_id TEXT, source_user_id TEXT, "
                 "mint TEXT, network_id TEXT, created_at TEXT, side TEXT, their_size_usd REAL)")
    # The prior read's second table since 2026-09-30 (buys only the on-chain lane saw).
    conn.execute("CREATE TABLE onchain_wallet_detections (signature TEXT, source_user_id TEXT, mint TEXT, "
                 "network_id TEXT, source_size_usd REAL, block_time INTEGER, detected_at TEXT)")
    return conn


def test_the_source_buy_anchor_is_the_event_time_not_the_decision():
    """R4: funded anchors the cumulative read on the source EVENT; the lane anchored on its own
    decision, so a buy arriving in between raised the lane's total above what funded saw."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _events_db()
    conn.execute("INSERT INTO memecoin_events VALUES('origin','u','M','solana',?,'buy',900)",
                 (_at(0, 0).isoformat(),))
    conn.execute("INSERT INTO memecoin_events VALUES('between','u','M','solana',?,'buy',1000)",
                 (_at(0, 5).isoformat(),))
    conn.commit()
    attempt = {"attempt_id": "a1", "source_user_id": "u", "network_id": "solana", "mint": "M",
               "origin_event_id": "origin", "decision_started_at": _at(0, 10).isoformat()}
    cfg_module = SimpleNamespace(MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S=60)

    anchor, basis = loop.source_event_anchor(conn, attempt)
    assert anchor == _at(0, 0).isoformat() and basis == "origin_event"
    # The $1,000 buy at +5s lands after the source event, so it is not prior evidence.
    assert loop.source_prior_net(conn, attempt, cfg_module=cfg_module) == 0.0


def test_the_attempts_own_source_stamp_wins_when_it_has_one():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _events_db()
    attempt = {"attempt_id": "a1", "source_user_id": "u", "network_id": "solana", "mint": "M",
               "origin_event_id": "origin", "source_created_at": _at(0, 0).isoformat(),
               "decision_started_at": _at(0, 10).isoformat()}
    assert loop.source_event_anchor(conn, attempt) == (_at(0, 0).isoformat(), "attempt_row")


def test_an_unresolvable_anchor_is_unknown_never_the_decision_time():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _events_db()
    conn.execute("INSERT INTO memecoin_events VALUES('later','u','M','solana',?,'buy',1000)",
                 (_at(0, 5).isoformat(),))
    attempt = {"attempt_id": "a1", "source_user_id": "u", "network_id": "solana", "mint": "M",
               "origin_event_id": "missing", "decision_started_at": _at(0, 10).isoformat()}
    assert loop.source_event_anchor(conn, attempt) == (None, "unknown")
    assert loop.source_prior_net(
        conn, attempt,
        cfg_module=SimpleNamespace(MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S=60)) is None


def test_failed_quotes_are_charged_to_the_sample_budget(monkeypatch):
    """N4: the cap counted successes, so an outage issued one request per pending row."""
    from scripts import memecoin_promotion_lane_loop as loop
    from sniper.services import memecoin_quotes as quotes

    conn = _sampling_db(rows=tuple((f"a{i}", "solana", f"M{i}") for i in range(25)))
    calls = []
    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed",
                        lambda mint, usd, **kw: (calls.append(mint) or (None, "rate_limited", "h")),
                        raising=False)
    counts = loop.sample_pending(conn, conn, now=_at(1),
                                 cfg_module=SimpleNamespace(MEMECOIN_PROMOTION_LANE_SAMPLE_MAX=2))
    assert len(calls) <= 2, f"{len(calls)} requests issued under a cap of 2"
    assert counts["attempted"] == 2 and counts["sampled"] == 0 and counts["errors"] == 2


def test_the_sampler_runs_to_the_fill_horizon_not_the_decision_horizon(monkeypatch):
    """R1: the fill is the first sample, so a hold that starts late must end late."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    m.record_promotion_lane_sample(conn, {
        "attempt_id": "a1", "network_id": "solana", "mint": "M",
        "sampled_at": _at(1).isoformat(), "price": 1.0, "price_basis": "lane_quote"})
    calls = []
    _quoter(monkeypatch, {"outAmount": "100"})
    from sniper.services import memecoin_quotes as quotes
    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed",
                        lambda mint, usd, **kw: (calls.append(mint) or ({"outAmount": "100"},
                                                                        None, "h")),
                        raising=False)
    # horizon 600 s from the 12:01 fill ends at 12:11, so 12:10:30 is still inside it.
    loop.sample_pending(conn, conn, now=_at(10, 30))
    assert calls == ["M"]


def test_a_fresh_reading_just_after_the_deadline_closes_the_timecap():
    """R1: the endpoint quote does not have to land exactly on the deadline second."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    for minute, second in ((1, 0), (3, 7), (6, 12), (9, 14), (11, 3)):
        _quote(conn, _at(minute, second).isoformat(), 1.0)
    row = m.promotion_lane_rows_unsettled(conn)[0]
    fields = loop.settle_fields(row, ro_conn=conn, now=_at(12))
    assert fields["exit_reason"] == "max_hold" and fields["pnl_usd"] == 0
    assert fields["endpoint_delay_s"] == pytest.approx(3.0)
    assert fields["censored_by"] is None


def test_an_endpoint_far_past_the_deadline_is_not_borrowed():
    """A reading long after the hold ended is a different fact, not the endpoint."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    _quote(conn, _at(1).isoformat(), 1.0)
    _quote(conn, _at(30).isoformat(), 1.0)          # 19 minutes past the 12:11 deadline
    row = m.promotion_lane_rows_unsettled(conn)[0]
    fields = loop.settle_fields(row, ro_conn=conn, now=_at(40))
    assert fields["exit_reason"] == "open_at_horizon"
    assert fields["censored_by"] == "tape"


@pytest.mark.parametrize("first_minute", (10, 20))
def test_a_first_quote_at_or_after_the_decision_deadline_is_not_a_fill(first_minute):
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    _quote(conn, _at(first_minute).isoformat(), 1.0)
    _quote(conn, _at(first_minute + 1).isoformat(), 0.6)
    row = m.promotion_lane_rows_unsettled(conn)[0]
    fields = loop.settle_fields(row, ro_conn=conn, now=_at(40))
    assert fields["fill_basis"] == loop.UNQUOTED
    assert fields["exit_series_n"] == 0
    assert fields.get("pnl_usd") is None


def test_sampler_does_not_quote_a_row_whose_first_price_is_after_the_deadline(monkeypatch):
    from scripts import memecoin_promotion_lane_loop as loop
    from sniper.services import memecoin_quotes as quotes

    conn = _sampling_db()
    _quote(conn, _at(20).isoformat(), 1.0)
    calls = []
    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed",
                        lambda *args, **kwargs: (calls.append(args) or {"outAmount": "100"}, None, "h"),
                        raising=False)
    counts = loop.sample_pending(conn, conn, now=_at(21))
    assert counts["attempted"] == 0
    assert calls == []


def test_endpoint_waits_for_its_tolerance_and_sampler_keeps_asking(monkeypatch):
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    m.record_promotion_lane_sample(conn, {
        "attempt_id": "a1", "network_id": "solana", "mint": "M",
        "sampled_at": _at(1).isoformat(), "price": 1.0, "price_basis": "lane_quote"})
    row = m.promotion_lane_rows_unsettled(conn)[0]
    assert loop.settle_fields(row, ro_conn=conn, now=_at(11, 1)) is None
    calls = []
    from sniper.services import memecoin_quotes as quotes
    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed",
                        lambda mint, usd, **kw: (calls.append(mint) or {"outAmount": "100"},
                                                None, "h"), raising=False)
    loop.sample_pending(conn, conn, now=_at(11, 1))
    assert calls == ["M"]


def test_future_sample_is_not_a_historical_endpoint():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _sampling_db()
    m.record_promotion_lane_sample(conn, {
        "attempt_id": "a1", "network_id": "solana", "mint": "M",
        "sampled_at": _at(12).isoformat(), "price": 1.0, "price_basis": "lane_quote"})
    row = m.promotion_lane_rows_unsettled(conn)[0]
    assert loop.price_series(conn, row, _at(11).isoformat())[0] == []


def test_a_late_ledger_mark_does_not_replace_an_established_sample_fill():
    """N6: the instrument that established the fill owns the row; a later mark is diagnostic."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    m.ensure_promotion_lane_samples_table(conn)
    for minute, price in ((1, 1.0), (3, 1.4)):
        m.record_promotion_lane_sample(conn, {
            "attempt_id": "a1", "network_id": "solana", "mint": "M",
            "sampled_at": _at(minute).isoformat(), "price": price, "price_basis": "lane_quote"})
    row = m.promotion_lane_rows_unsettled(conn)[0]
    before = loop.settle_fields(row, ro_conn=conn, now=_at(20))
    _quote(conn, _at(8).isoformat(), 99.0)
    after = loop.settle_fields(row, ro_conn=conn, now=_at(20))

    assert before["exit_series_source"] == loop.SAMPLE_BASIS
    assert after["exit_series_source"] == before["exit_series_source"]
    assert after["fill_price"] == before["fill_price"]


def test_first_seen_fill_receipt_survives_an_earlier_stamped_late_mark():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    m.ensure_promotion_lane_samples_table(conn)
    _sample(conn, 1, price=1.0)
    _sample(conn, 3, price=1.4)
    assert loop.settle_pending(conn, conn, now=_at(4)) == (0, 1, 0)
    _quote(conn, _at(0, 30).isoformat(), 99.0)
    row = m.promotion_lane_rows_unsettled(conn)[0]
    series, source = loop.price_series(conn, row, _at(4).isoformat())
    assert source == loop.SAMPLE_BASIS
    assert series[0] == (loop._epoch(_at(1).isoformat()), 1.0)


def test_the_modelled_pnl_is_labelled_a_buy_mark_model():
    """N8: every sample is a BUY quote, so their ratios are not proceeds from a sale."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    m.ensure_promotion_lane_samples_table(conn)
    for minute, price in ((1, 1.0), (3, 1.4)):
        m.record_promotion_lane_sample(conn, {
            "attempt_id": "a1", "network_id": "solana", "mint": "M",
            "sampled_at": _at(minute).isoformat(), "price": price, "price_basis": "lane_quote"})
    row = m.promotion_lane_rows_unsettled(conn)[0]
    fields = loop.settle_fields(row, ro_conn=conn, now=_at(20))
    assert fields["pnl_basis"] == "buy_mark_model"


# --- Codex recheck: N5, N7 (these also move db_memecoin.py) ----------------------------------

def _sample(conn, minute, price=None, reason=None):
    m.record_promotion_lane_sample(conn, {
        "attempt_id": "a1", "network_id": "solana", "mint": "M",
        "sampled_at": _at(minute).isoformat(), "price": price, "price_basis": "lane_quote",
        "reason": reason})


def test_a_failed_quote_is_visible_to_the_replay_not_filtered_away():
    """N5: the writer records a failure with its reason; the reader dropped it."""
    conn = _settleable_db()
    m.ensure_promotion_lane_samples_table(conn)
    _sample(conn, 1, price=1.0)
    _sample(conn, 3, reason="rate_limited")
    rows = m.promotion_lane_samples(conn, "a1")
    assert len(rows) == 2
    assert rows[1]["price"] is None and rows[1]["reason"] == "rate_limited"


def test_a_carried_price_across_a_provider_outage_is_not_a_second_confirmation():
    """N5: two confirmations need two observations, and one of these never happened."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    m.ensure_promotion_lane_samples_table(conn)
    row = m.promotion_lane_rows_unsettled(conn)[0]
    policy = json.loads(row["policy_json"])
    policy["stop_debounce_sweeps"] = 2
    row["policy_json"] = json.dumps(policy)
    for minute, price, reason in ((1, 1.0, None), (2, 0.6, None), (3, None, "rate_limited"),
                                  (4, None, "provider_error"), (5, 1.0, None)):
        _sample(conn, minute, price=price, reason=reason)

    fields = loop.settle_fields(row, ro_conn=conn, now=_at(20))
    assert fields["exit_reason"] != "stop", "the second breach was the first one, carried"
    assert fields["censored_by"] == "sample_gap"
    assert fields.get("pnl_usd") is None, "a censored row carries no PnL at all"
    assert fields["gap_after_s"] == pytest.approx(120.0), "the tape went dark two minutes in"


def test_an_uninterrupted_series_still_confirms_a_stop():
    """The censor must not swallow a stop that two real observations did confirm."""
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    m.ensure_promotion_lane_samples_table(conn)
    row = m.promotion_lane_rows_unsettled(conn)[0]
    policy = json.loads(row["policy_json"])
    policy["stop_debounce_sweeps"] = 2
    row["policy_json"] = json.dumps(policy)
    for minute, price in ((1, 1.0), (2, 0.6), (3, 0.6), (4, 0.6)):
        _sample(conn, minute, price=price)

    fields = loop.settle_fields(row, ro_conn=conn, now=_at(20))
    assert fields["exit_reason"] == "stop"
    assert fields["censored_by"] is None


def test_skipped_requests_do_not_duplicate_one_stop_breach():
    from scripts import memecoin_promotion_lane_loop as loop

    conn = _settleable_db()
    m.ensure_promotion_lane_samples_table(conn)
    row = m.promotion_lane_rows_unsettled(conn)[0]
    policy = json.loads(row["policy_json"])
    policy["stop_debounce_sweeps"] = 2
    row["policy_json"] = json.dumps(policy)
    for minute, price in ((1, 1.0), (2, 0.6), (11, 1.0)):
        _sample(conn, minute, price=price)

    fields = loop.settle_fields(row, ro_conn=conn, now=_at(20))
    assert fields["exit_reason"] != "stop"


def test_a_new_row_is_not_hidden_behind_the_selectors_limit(monkeypatch):
    """N7: SQL truncated to the oldest 200 and Python prioritised afterwards, so a new fill
    outside those 200 never reached the sort."""
    from scripts import memecoin_promotion_lane_loop as loop
    from sniper.services import memecoin_quotes as quotes

    conn = _sampling_db(rows=tuple((str(i), "solana", str(i)) for i in range(201)))
    conn.execute("UPDATE memecoin_promotion_lane SET decision_started_at=? WHERE attempt_id='200'",
                 (_at(1).isoformat(),))
    conn.commit()
    for i in range(200):
        _sample_for(conn, str(i), _at(1).isoformat(), 1.0)

    calls = []
    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed",
                        lambda mint, usd, **kw: (calls.append(mint) or ({"outAmount": "100"},
                                                                        None, "h")),
                        raising=False)
    loop.sample_pending(conn, conn, now=_at(2),
                        cfg_module=SimpleNamespace(MEMECOIN_PROMOTION_LANE_SAMPLE_MAX=1))
    assert calls == ["200"], "the row with no price yet is the one the fill depends on"


def test_a_row_whose_only_samples_failed_still_counts_as_unpriced(monkeypatch):
    """N7: an attempt is not evidence of a fill; only a price is."""
    from scripts import memecoin_promotion_lane_loop as loop
    from sniper.services import memecoin_quotes as quotes

    conn = _sampling_db(rows=(("a1", "solana", "M1"), ("a2", "solana", "M2")))
    # a1 is the NEWER row and every one of its samples failed; a2 is older and has a real price.
    # Oldest-first alone would serve a2, and a2 already has the fill it needs.
    conn.execute("UPDATE memecoin_promotion_lane SET decision_started_at=? WHERE attempt_id='a1'",
                 (_at(1).isoformat(),))
    _sample_for(conn, "a1", _at(1, 30).isoformat(), None, reason="rate_limited")
    _sample_for(conn, "a2", _at(0, 30).isoformat(), 1.0)
    conn.commit()

    calls = []
    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed",
                        lambda mint, usd, **kw: (calls.append(mint) or ({"outAmount": "100"},
                                                                        None, "h")),
                        raising=False)
    loop.sample_pending(conn, conn, now=_at(2),
                        cfg_module=SimpleNamespace(MEMECOIN_PROMOTION_LANE_SAMPLE_MAX=1))
    assert calls == ["M1"], "a row that only ever errored has no fill and goes first"


def _sample_for(conn, attempt_id, sampled_at, price, reason=None):
    m.record_promotion_lane_sample(conn, {
        "attempt_id": attempt_id, "network_id": "solana", "mint": attempt_id,
        "sampled_at": sampled_at, "price": price, "price_basis": "lane_quote", "reason": reason})


# --- Codex recheck, bounded suggestion: per-request sample stamps ----------------------------

def test_each_sample_is_stamped_when_its_own_answer_came_back(monkeypatch):
    """The pass clock was written before the request, so every row after the first was early."""
    from scripts import memecoin_promotion_lane_loop as loop
    from sniper.services import memecoin_quotes as quotes

    conn = _sampling_db(rows=(("a1", "solana", "M1"), ("a2", "solana", "M2")))
    clock = {"t": _at(1)}

    def slow_quote(mint, usd, **kw):
        clock["t"] = clock["t"] + dt.timedelta(seconds=7)
        return {"outAmount": "100"}, None, "h"

    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed", slow_quote, raising=False)
    monkeypatch.setattr(loop, "_now", lambda: clock["t"])
    loop.sample_pending(conn, conn, now=_at(1))

    stamps = [r[0] for r in conn.execute(
        "SELECT sampled_at FROM memecoin_promotion_lane_samples ORDER BY sampled_at")]
    assert len(set(stamps)) == 2, "two requests seven seconds apart are not one instant"
    assert stamps[0] == _at(1, 7).isoformat() and stamps[1] == _at(1, 14).isoformat()


def test_the_pass_clock_survives_as_the_scheduling_time(monkeypatch):
    """Scheduling time is still a fact; it is simply not when the price was observed."""
    from scripts import memecoin_promotion_lane_loop as loop
    from sniper.services import memecoin_quotes as quotes

    conn = _sampling_db(rows=(("a1", "solana", "M1"), ("a2", "solana", "M2")))
    clock = {"t": _at(1)}

    def slow_quote(mint, usd, **kw):
        clock["t"] = clock["t"] + dt.timedelta(seconds=7)
        return {"outAmount": "100"}, None, "h"

    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed", slow_quote, raising=False)
    monkeypatch.setattr(loop, "_now", lambda: clock["t"])
    loop.sample_pending(conn, conn, now=_at(1))

    scheduled = {r[0] for r in conn.execute(
        "SELECT scheduled_at FROM memecoin_promotion_lane_samples")}
    assert scheduled == {_at(1).isoformat()}


def test_a_failed_quote_is_stamped_when_the_failure_was_observed(monkeypatch):
    """A timeout is an observation: the request happened and we saw it fail."""
    from scripts import memecoin_promotion_lane_loop as loop
    from sniper.services import memecoin_quotes as quotes

    conn = _sampling_db()
    clock = {"t": _at(1)}

    def slow_failure(mint, usd, **kw):
        clock["t"] = clock["t"] + dt.timedelta(seconds=5)
        return None, "rate_limited", "h"

    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed", slow_failure, raising=False)
    monkeypatch.setattr(loop, "_now", lambda: clock["t"])
    loop.sample_pending(conn, conn, now=_at(1))

    row = conn.execute("SELECT sampled_at, scheduled_at, price, reason "
                       "FROM memecoin_promotion_lane_samples").fetchone()
    assert row["sampled_at"] == _at(1, 5).isoformat()
    assert row["scheduled_at"] == _at(1).isoformat()
    assert row["price"] is None and row["reason"] == "rate_limited"


def test_an_exception_is_also_stamped_when_it_was_observed(monkeypatch):
    from scripts import memecoin_promotion_lane_loop as loop
    from sniper.services import memecoin_quotes as quotes

    conn = _sampling_db()
    clock = {"t": _at(1)}

    def raise_late(mint, usd, **kw):
        clock["t"] = clock["t"] + dt.timedelta(seconds=9)
        raise TimeoutError("no answer")

    monkeypatch.setattr(quotes, "jupiter_entry_quote_detailed", raise_late, raising=False)
    monkeypatch.setattr(loop, "_now", lambda: clock["t"])
    loop.sample_pending(conn, conn, now=_at(1))

    row = conn.execute("SELECT sampled_at, reason FROM memecoin_promotion_lane_samples").fetchone()
    assert row["sampled_at"] == _at(1, 9).isoformat()
    assert row["reason"] == "exception: TimeoutError"


def test_a_chain_with_volume_gates_off_is_not_refused_on_volume(monkeypatch):
    """The shadow follows MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS like the funded path (2026-09-29)."""
    import sniper.settings.memecoin as real_cfg

    monkeypatch.setattr(real_cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 3000.0, raising=False)
    dead = {**MARKET, "volume_h1": 0.0, "volume_m5": 0.0}
    states, inputs, refused = _evaluate(
        market=dead, cfg=_cfg(MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS=frozenset({"solana"})))
    assert not (refused and refused[0].startswith("m5_")), refused
    assert inputs["volume_gate_off_chain_would_refuse"]
    states, _inputs, refused = _evaluate(
        market=dead, cfg=_cfg(MEMECOIN_REAL_VOLUME_GATES_OFF_CHAINS=frozenset({"56"})))
    assert refused and refused[0].startswith("m5_"), "an unlisted chain is still refused"
