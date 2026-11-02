"""WS1 (2026-07-15l audit): the web UI's per-wallet copy-PnL columns must show the SAME honest
metric the arm gate uses (alpha_b_copy_stats: alpha_b-only, non-real, fomo-excluded) — not the
blended all-arms / fomo-priced total that inflated smol_intern to +$598/161c while the honest record
was -$63.40/19c.

Two dishonest data surfaces were fixed:
  - Traders tab cp·pnl  (list_curated_with_stats -> wallet_stats): blended over every strategy incl
    fomo fantasy fills.
  - Legacy trader_performance payload: same blend, no strategy filter.

Both data surfaces carry an explicit honest αb value (copy_ab_closed/copy_ab_realized and
ab_closed/ab_pnl) alongside the relabeled blended numbers. The current Performance tab supersedes
the legacy payload with the Wallet x Arm matrix and renders its explicit alpha_b column.
"""
import sqlite3
from pathlib import Path

from sniper.persistence import db_memecoin as m

APP_JS = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _seed_close_with_fills(c, uid, mint, pnl, *, entry_src, exit_src, strategy="alpha_b",
                           mode="paper"):
    """One closed position with explicit entry+exit fill sources (fomo taint == fantasy fill)."""
    pid = m.open_position(c, source_user_id=uid, source_handle=uid, mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source=entry_src,
                          entry_lag_s=0.1, their_size_usd=1.0, opened_at="2026-07-01T00:00:00Z",
                          strategy=strategy, execution_mode=mode)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source=entry_src, fraction=1.0, created_at="2026-07-01T00:00:00Z")
    m.insert_fill(c, position_id=pid, kind="mirror_exit", price=1.0 + pnl / 10.0, qty=10.0,
                  notional=10.0 + pnl, fill_source=exit_src, fraction=1.0,
                  created_at="2026-07-01T01:00:00Z")
    m.close_position(c, pid, "2026-07-01T01:00:00Z", pnl, exit_reason="b_mirror")
    return pid


def _seed_honest_plus_fomo(c, uid):
    """2 honest alpha_b closes (+$3 each) + 1 fomo-fantasy alpha_b close (+$100). Honest = 2c/+$6;
    blended = 3c/+$106."""
    _seed_close_with_fills(c, uid, f"{uid}H0", 3.0, entry_src="dexscreener", exit_src="dexscreener")
    _seed_close_with_fills(c, uid, f"{uid}H1", 3.0, entry_src="dexscreener", exit_src="dexscreener")
    _seed_close_with_fills(c, uid, f"{uid}F0", 100.0, entry_src="fomo", exit_src="fomo")


# ---- batched honest helper -----------------------------------------------------------------------

def test_alpha_b_copy_stats_all_matches_single_wallet_and_drops_zero_honest():
    c = _conn()
    _seed_honest_plus_fomo(c, "u1")
    _seed_close_with_fills(c, "u2", "u2H0", 5.0, entry_src="dexscreener", exit_src="dexscreener")
    # a wallet whose ONLY closes are fomo / a different arm has zero honest evidence
    _seed_close_with_fills(c, "u3", "u3F0", 100.0, entry_src="fomo", exit_src="fomo")
    _seed_close_with_fills(c, "u3", "u3C0", 100.0, entry_src="dexscreener", exit_src="dexscreener",
                           strategy="control")

    allst = m.alpha_b_copy_stats_all(c)
    assert allst["u1"] == m.alpha_b_copy_stats(c, "u1") == {"n_closes": 2, "pnl": 6.0}
    assert allst["u2"] == m.alpha_b_copy_stats(c, "u2") == {"n_closes": 1, "pnl": 5.0}
    assert "u3" not in allst, "zero honest closes -> absent, never a blended number"


def test_alpha_b_copy_stats_all_excludes_real_positions():
    c = _conn()
    _seed_close_with_fills(c, "u1", "R0", 9.0, entry_src="chain", exit_src="chain", mode="real")
    assert "u1" not in m.alpha_b_copy_stats_all(c), "real fills are not paper copy evidence"


def test_alpha_b_copy_stats_single_and_batched_exclude_every_unmeasurable_close():
    c = _conn()
    clean = _seed_close_with_fills(
        c, "u1", "CLEAN", 7.0, entry_src="route_quote", exit_src="route_quote")
    c.execute("UPDATE memecoin_positions SET network_id='4663' WHERE id=?", (clean,))

    noquote = _seed_close_with_fills(
        c, "u1", "NOQUOTE", 50.0, entry_src="route_quote", exit_src="noquote")
    c.execute("UPDATE memecoin_positions SET network_id='4663', exit_reason='a_rollover_noquote' "
              "WHERE id=?", (noquote,))

    orphan = m.open_position(
        c, source_user_id="u1", source_handle="u1", mint="ORPHAN", ticker="T",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="route_quote",
        entry_lag_s=0.1, their_size_usd=1.0, opened_at="2026-07-01T00:00:00Z",
        strategy="alpha_b", network_id="4663")
    m.insert_fill(c, position_id=orphan, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="route_quote", fraction=1.0,
                  created_at="2026-07-01T00:00:00Z")
    m.close_position(c, orphan, "2026-07-01T01:00:00Z", 40.0, exit_reason="b_mirror")

    unquotable = _seed_close_with_fills(
        c, "u1", "UNQUOTABLE", 30.0, entry_src="route_quote", exit_src="route_quote")
    c.execute("UPDATE memecoin_positions SET network_id='4663', exit_reason='unquotable', "
              "last_quote_price=0 WHERE id=?", (unquotable,))
    c.commit()

    expected = {"n_closes": 1, "pnl": 7.0}
    assert m.alpha_b_copy_stats(c, "u1", chain="robin") == expected
    assert m.alpha_b_copy_stats_all(c)["u1"] == expected


# ---- Traders tab (list_curated_with_stats) -------------------------------------------------------

def test_list_curated_with_stats_carries_honest_ab_distinct_from_blended():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _seed_honest_plus_fomo(c, "u1")
    row = next(r for r in m.list_curated_with_stats(c) if r["source_user_id"] == "u1")
    # honest αb column = the arming criterion
    assert row["copy_ab_closed"] == 2 and row["copy_ab_realized"] == 6.0
    # blended still present but MUST differ (proves the honest column isn't the fantasy total)
    assert row["copy_closed"] == 3 and row["copy_realized"] == 106.0


def test_list_curated_with_stats_zero_honest_reads_zero_not_blended():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    _seed_close_with_fills(c, "u1", "F0", 100.0, entry_src="fomo", exit_src="fomo")
    row = next(r for r in m.list_curated_with_stats(c) if r["source_user_id"] == "u1")
    assert row["copy_ab_closed"] == 0 and row["copy_ab_realized"] == 0.0


# ---- Performance tab (trader_performance) --------------------------------------------------------

def test_trader_performance_carries_honest_ab_distinct_from_blended():
    c = _conn()
    _seed_honest_plus_fomo(c, "u1")
    row = next(r for r in m.trader_performance(c) if r["source_user_id"] == "u1")
    assert row["ab_closed"] == 2 and row["ab_pnl"] == 6.0
    # the pre-existing blended headline stays but is now clearly not the only/honest number
    assert row["closed"] == 3 and row["closed_pnl"] == 106.0


# ---- app.js render contract (the honest column must actually be shown, blended must be relabeled) --

def test_honest_ab_is_shown_per_chain_not_pooled_on_the_curated_pool():
    """The honest alpha_b number is the arming criterion, so it is rendered where arming
    happens: the per-chain Armed board's Gate column, fed by `alpha_b_copy_stats(chain=)`.

    It used to be a column on the chain-independent Curated pool. That was a POOLED number —
    the one thing the per-chain evidence doctrine forbids — and after the 2026-07-30 ledger
    purge it read `0c` for nearly every row anyway. Curated now points at Live → Armed instead
    of showing a cross-chain total (2026-08-03).
    """
    # The gate number and the threshold it is judged against are both rendered.
    assert "THE arming criterion on this chain" in APP_JS
    assert "chain-scoped alpha_b" in APP_JS
    # And the criterion is never pooled back into the chain-independent pool.
    assert "Chain-independent" in APP_JS
    assert "never pooled into one number here" in APP_JS


def test_blended_all_arms_copy_pnl_is_not_rendered_as_a_copy_pnl_headline():
    """The blended all-arms/fomo number must not read as THE copy PnL. It is no longer shown
    on the Curated pool at all; the Armed board labels its all-arm figure as exactly that and
    marks the best-arm column DESCRIPTIVE."""
    assert "cp·pnl·all-arms" not in APP_JS
    assert "DESCRIPTIVE only" in APP_JS
    assert "all-arm paper PnL over this wallet" in APP_JS


def test_combined_wallet_projection_has_constant_query_count_and_no_dead_entry_quality():
    c = _conn()
    for index in range(25):
        uid = f"u{index}"
        m.add_wallet(c, uid, uid)
        c.execute(
            "INSERT INTO memecoin_events(fomo_event_id,source_user_id,created_at) VALUES(?,?,?)",
            (f"e{index}", uid, "2026-07-28T12:00:00+00:00"),
        )
    c.commit()
    statements = []
    c.set_trace_callback(lambda sql: statements.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)

    rows = m.list_curated_with_stats(c)

    c.set_trace_callback(None)
    assert len(rows) == 25
    assert len(statements) <= 10
    assert all("entry_quality_candidate" not in row for row in rows)


def test_accounting_copy_describes_global_caps_and_chain_local_funds():
    assert "slots + exposure are global across all funded chains" in APP_JS
    assert "balances + gas stay chain-local" in APP_JS
    assert "EVM unbounded" not in APP_JS
    assert "no slot cap" not in APP_JS
    assert "cap is enforced on the Solana book only" not in APP_JS
