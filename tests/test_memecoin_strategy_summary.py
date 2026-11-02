import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _closed(c, strat, pnl, ttp):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-07T10:00:00+00:00",
                          strategy=strat)
    m.set_position_instrumentation(c, pid, time_to_peak_s=ttp, max_gain_pct=10.0)
    m.close_position(c, pid, "2026-07-07T10:05:00+00:00", pnl, exit_reason="x")   # sets is_open=0


def _closed_ev(c, strat, ev_id, pnl):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-07T10:00:00+00:00",
                          strategy=strat, origin_event_id=ev_id)
    m.set_position_instrumentation(c, pid, time_to_peak_s=1.0, max_gain_pct=10.0)
    m.close_position(c, pid, "2026-07-07T10:05:00+00:00", pnl, exit_reason="x")


def test_strategy_summary_matched_excludes_rebuy_only_arms():
    # REGRESSION (deep review P0 #4): arms desync on re-buys — a fast arm (momentum_a) exits and
    # re-enters a hot token at a NEW price while slow arms still hold the first entry, so the arms
    # accumulate different entry sets. The MATCHED read compares arms ONLY on events where ALL
    # arms entered (origin_event_id), so a re-buy one arm took can't skew the A/B.
    c = _conn()
    for strat, pnl in (("control", -8.0), ("momentum_a", 5.0), ("alpha_b", 3.0)):
        _closed_ev(c, strat, "E1", pnl)          # shared entry E1: all three arms
    _closed_ev(c, "momentum_a", "E2", 100.0)     # re-buy E2: momentum_a only, huge win
    allrows = {r["strategy"]: r for r in m.strategy_summary(c)}
    matched = {r["strategy"]: r for r in
               m.strategy_summary_matched(c, arms=("control", "momentum_a", "alpha_b"))}
    assert allrows["momentum_a"]["closed"] == 2                 # unmatched read double-counts the re-buy
    assert matched["momentum_a"]["closed"] == 1                 # E2 excluded (not all arms entered it)
    assert abs(matched["momentum_a"]["realized_pnl"] - 5.0) < 1e-6   # the 100.0 skew is gone
    assert matched["control"]["closed"] == 1 and matched["alpha_b"]["closed"] == 1
    assert matched["control"]["matched_events"] == 1


def _open_pos(c, strat, entry, notional, ev_id="E1", mint="M"):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                          entry_price=entry, entry_notional=notional, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-07T10:00:00+00:00",
                          strategy=strat, origin_event_id=ev_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=notional / entry,
                  notional=notional, fill_source="j", fraction=1.0,
                  created_at="2026-07-07T10:00:00+00:00")
    return pid


def test_strategy_summary_marks_open_positions_to_last_quote():
    # REGRESSION (deep review P0 #2): a realized-only summary hid control's OPEN losers while
    # alpha_b force-realized its losers at the 12h cap -> asymmetric censoring flipped the arm
    # ranking. Open positions are now marked to their last observed quote (unrealized_pnl); a
    # position we could never price is counted as unrealized_partial (excluded + flagged, not 0).
    # (Distinct mints: at most one OPEN position per (uid,mint,strategy) is enforced by a UNIQUE
    # index, so an arm holding two open positions holds two different tokens.)
    c = _conn()
    pid = _open_pos(c, "control", entry=1.0, notional=100.0, mint="M")     # 100 qty
    m.update_position_last_quote(c, pid, 0.5, "2026-07-07T10:00:15+00:00")  # -50% -> -50 USD unrealized
    _open_pos(c, "control", entry=1.0, notional=100.0, ev_id="E2", mint="M2")   # never quoted -> partial
    s = {r["strategy"]: r for r in m.strategy_summary(c)}
    assert s["control"]["open_count"] == 2
    assert abs(s["control"]["unrealized_pnl"] - (-50.0)) < 1e-6
    assert s["control"]["unrealized_partial"] == 1


def test_open_position_mtm_includes_booked_partial_realized():
    # platform review: a partially-exited STILL-OPEN position has its partial's realized delta in
    # fills only (partial_exit reopens without finalizing), so total_pnl must add it, not just mark
    # the remaining qty (which undercounts by the booked partial PnL).
    c = _conn()
    pid = _open_pos(c, "control", entry=1.0, notional=100.0)     # 100 qty @ 1.0
    m.insert_fill(c, position_id=pid, kind="partial_exit", price=2.0, qty=40.0, notional=80.0,
                  fill_source="d", fraction=0.4, created_at="t")  # booked realized (2-1)*40 = +40
    m.update_position_last_quote(c, pid, 1.5, "t")               # remaining 60 marked (1.5-1)*60 = +30
    s = {r["strategy"]: r for r in m.strategy_summary(c)}
    assert abs(s["control"]["total_pnl"] - 70.0) < 1e-6          # 40 booked + 30 unrealized


def test_strategy_summary_counts_noquote_and_stale_closes():
    # methodology review: a _noquote timecap close books 0 PnL (a token dark for 12h is likely a
    # rug ~ -100%, so 0 is optimistic + arm-asymmetric). Surface how many closes per arm rest on
    # that assumption so a reader can discount total_pnl rather than trust it blindly.
    c = _conn()
    for pnl, reason in ((0.0, "b_timecap_noquote"), (-50.0, "b_timecap_stale"), (5.0, "b_stop")):
        pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M"+reason, ticker="T",
                              entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                              entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-07T10:00:00+00:00",
                              strategy="alpha_b")
        m.close_position(c, pid, "2026-07-07T10:05:00+00:00", pnl, exit_reason=reason)
    s = {r["strategy"]: r for r in m.strategy_summary(c)}
    assert s["alpha_b"]["noquote_closes"] == 1 and s["alpha_b"]["stale_closes"] == 1
    assert s["alpha_b"]["noquote_shadow_pnl"] == -100.0
    assert s["alpha_b"]["noquote_shadow_adjustment"] == -100.0
    assert s["alpha_b"]["conservative_total_pnl"] == -145.0


def test_strategy_summary_and_exit_liquidity_rate():
    c = _conn()
    _closed(c, "momentum_a", 5.0, 40.0)    # peaked later -> not exit-liq
    _closed(c, "momentum_a", -3.0, 0.0)    # peaked at entry -> exit-liq
    _closed(c, "control", -8.0, 2.0)       # peaked within a sweep -> exit-liq
    s = {r["strategy"]: r for r in m.strategy_summary(c)}
    assert s["momentum_a"]["closed"] == 2 and abs(s["momentum_a"]["realized_pnl"] - 2.0) < 1e-6
    assert s["momentum_a"]["win_rate"] == 0.5
    assert abs(s["momentum_a"]["exit_liquidity_rate"] - 0.5) < 1e-6
    assert s["control"]["exit_liquidity_rate"] == 1.0


def _pos_with_fills(c, strat, pnl, *, entry_src, exit_src, ev_id="E1", mint="M"):
    """One closed position whose entry and exit fills carry explicit fill_source values."""
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source=entry_src,
                          entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at="2026-07-07T10:00:00+00:00", strategy=strat,
                          origin_event_id=ev_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source=entry_src, fraction=1.0, created_at="2026-07-07T10:00:00+00:00")
    m.insert_fill(c, position_id=pid, kind="mirror_exit", price=1.0, qty=100.0, notional=100.0,
                  fill_source=exit_src, fraction=1.0, created_at="2026-07-07T10:05:00+00:00")
    m.close_position(c, pid, "2026-07-07T10:05:00+00:00", pnl, exit_reason="mirror")
    return pid


def test_strategy_summary_excludes_fomo_priced_positions_by_default():
    # REGRESSION (2026-07-27): the arm A/B counted fomo-priced positions, so every arm read
    # deeply negative and the ranking INVERTED against the honest (arming) metric. On 2026-07-26
    # that contaminated view cut scalp_c (-$39,896 naive / +$6,926 honest) as a "deep loser".
    # The headline must use the SAME honest filter the arming gate uses; the dropped bucket is
    # reported separately so it is visible, never silently hidden.
    c = _conn()
    _pos_with_fills(c, "scalp_c", 40.0, entry_src="dexscreener", exit_src="dexscreener", mint="A")
    _pos_with_fills(c, "scalp_c", -90.0, entry_src="fomo", exit_src="dexscreener", mint="B")
    rows = {r["strategy"]: r for r in m.strategy_summary(c)}
    assert rows["scalp_c"]["closed"] == 1
    assert abs(rows["scalp_c"]["realized_pnl"] - 40.0) < 1e-6      # honest only, sign is positive
    assert rows["scalp_c"]["fomo_priced_closes"] == 1              # the drop is disclosed
    assert abs(rows["scalp_c"]["fomo_priced_pnl"] + 90.0) < 1e-6
    naive = {r["strategy"]: r for r in m.strategy_summary(c, honest_only=False)}
    assert naive["scalp_c"]["closed"] == 2
    assert abs(naive["scalp_c"]["realized_pnl"] + 50.0) < 1e-6     # the old, contaminated read


def test_strategy_summary_matched_is_honest_and_stays_apples_to_apples():
    # A fomo EXIT can hit one arm and not another on the SAME event (mirror arms only), so the
    # honest filter can split an event across arms. The matched read must re-match on the honest
    # subset, or it silently compares an arm's 1 close against another arm's 2.
    c = _conn()
    for strat in ("control", "alpha_b"):
        _pos_with_fills(c, strat, 10.0, entry_src="dexscreener", exit_src="dexscreener",
                        ev_id="E1", mint="A")
    _pos_with_fills(c, "control", 10.0, entry_src="dexscreener", exit_src="dexscreener",
                    ev_id="E2", mint="B")
    _pos_with_fills(c, "alpha_b", 500.0, entry_src="dexscreener", exit_src="fomo",
                    ev_id="E2", mint="B")
    matched = {r["strategy"]: r for r in
               m.strategy_summary_matched(c, arms=("control", "alpha_b"))}
    assert matched["control"]["closed"] == 1 and matched["alpha_b"]["closed"] == 1
    assert abs(matched["alpha_b"]["realized_pnl"] - 10.0) < 1e-6   # the fomo +500 never lands


def _closed_chain(c, strat, chain, pnl, *, mint="M"):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-07T10:00:00+00:00",
                          strategy=strat, network_id=chain, origin_event_id=f"E-{chain}")
    m.set_position_instrumentation(c, pid, time_to_peak_s=1.0, max_gain_pct=10.0)
    m.close_position(c, pid, "2026-07-07T10:05:00+00:00", pnl, exit_reason="x")


def test_strategy_summary_scopes_to_one_chain():
    """Chains are NEVER pooled (2026-07-30 per-chain evidence doctrine: no `all` scope on any
    per-chain surface). Pooling let 4663 — the DexScreener-blind chain, where closes routinely go
    down the censored `_noquote` path — move the arm read for solana, so an arm's PnL reflected
    which chain it happened to trade rather than how it traded."""
    c = _conn()
    _closed_chain(c, "control", "solana", 10.0, mint="S")
    _closed_chain(c, "control", "4663", -90.0, mint="R")

    pooled = {r["strategy"]: r for r in m.strategy_summary(c)}
    sol = {r["strategy"]: r for r in m.strategy_summary(c, network_id="solana")}
    rob = {r["strategy"]: r for r in m.strategy_summary(c, network_id="4663")}

    assert pooled["control"]["closed"] == 2 and abs(pooled["control"]["realized_pnl"] + 80.0) < 1e-9
    assert sol["control"]["closed"] == 1 and abs(sol["control"]["realized_pnl"] - 10.0) < 1e-9
    assert rob["control"]["closed"] == 1 and abs(rob["control"]["realized_pnl"] + 90.0) < 1e-9


def test_strategy_summary_matched_scopes_to_one_chain():
    c = _conn()
    _closed_chain(c, "control", "solana", 10.0, mint="S")
    _closed_chain(c, "control", "4663", -90.0, mint="R")
    rob = {r["strategy"]: r for r in
           m.strategy_summary_matched(c, arms=("control",), network_id="4663")}
    assert rob["control"]["closed"] == 1 and abs(rob["control"]["realized_pnl"] + 90.0) < 1e-9


def test_strategy_summary_chain_scope_normalises_ui_spelling():
    """`real_chains` stores the funded network id, but UI surfaces have historically spoken
    `robin`/`base`. canonical_chain_id() normalises on read so a UI spelling cannot silently
    select zero rows and render an empty arm board as 'no edge'."""
    c = _conn()
    _closed_chain(c, "control", "4663", -90.0, mint="R")
    rob = {r["strategy"]: r for r in m.strategy_summary(c, network_id="robin")}
    assert rob["control"]["closed"] == 1
