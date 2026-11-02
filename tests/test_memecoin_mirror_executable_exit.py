"""Mirror exits must book EXECUTABLE route quotes, never a DexScreener mid or the trader's
own price (2026-08-05 review finding #2).

The old sell path priced every mirror close off a pool mid (modeled slippage) with a
`their_price` 'fomo' fallback. On the DexScreener-blind chain (4663) that meant EVERY mirror
close booked source='fomo', and one fomo fill censors its whole origin from the strict forward
cohort — so all robin-armed wallets stayed permanently evidence-less for copy-arm selection.

Contract under test:
  1. mirror closes price via the executable exit-quote seam (route_quote fill source);
  2. an unavailable quote books a `_noquote` close at entry (censored), NEVER fomo/mid;
  3. the report censors those noquote mirror closes (report_pnl None);
  4. one quote per (network, qty) per sell event — arms sharing the entry fill share one call;
  5. PAPER_FILL_MODEL is bumped so v1-priced rows can never pool with v2 rows (cohort fork);
  6. a sell that omits the chain still mirror-closes a concretely-tagged position (the outer
     pre-check used to skip the fallback `_mirror_close_arm` already had).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _open(conn, *, strategy="alpha_b", network_id="solana", mint="M1", uid="u1",
          entry_price=1.0, qty=100.0):
    pid = m.open_position(
        conn, source_user_id=uid, source_handle="h", mint=mint, ticker="T",
        entry_price=entry_price, entry_notional=entry_price * qty,
        entry_fill_source="route_quote", entry_lag_s=0.1, their_size_usd=0.0,
        opened_at="2026-08-05T00:00:00+00:00", strategy=strategy, network_id=network_id)
    m.insert_fill(conn, position_id=pid, kind="entry", price=entry_price, qty=qty,
                  notional=entry_price * qty, fill_source="route_quote", fraction=1.0,
                  created_at="2026-08-05T00:00:00+00:00")
    return pid


def _sell_ev(mint="M1", network_id="solana", uid="u1", their_price=2.0):
    return SimpleNamespace(source_user_id=uid, mint=mint, network_id=network_id,
                           side="sell", their_price=their_price, fomo_event_id="ev-1")


NOW = "2026-08-05T01:00:00+00:00"


def _fill_rows(conn, pid):
    return [dict(r) for r in conn.execute(
        "SELECT kind, price, qty, fill_source FROM memecoin_fills "
        "WHERE position_id=? AND kind != 'entry'", (pid,))]


def test_mirror_close_books_route_quote():
    conn = _conn()
    pid = _open(conn)
    out = ct._mirror_close_arm(conn, _sell_ev(), NOW, "alpha_b",
                               quote_fn=lambda mint, net, qty: (1.5, "route_quote"))
    assert out == "exit"
    row = conn.execute("SELECT is_open, exit_reason, realized_pnl FROM memecoin_positions "
                       "WHERE id=?", (pid,)).fetchone()
    assert row["is_open"] == 0 and row["exit_reason"] == "b_mirror"
    assert abs(row["realized_pnl"] - 50.0) < 1e-9          # (1.5 - 1.0) * 100
    (leg,) = _fill_rows(conn, pid)
    assert leg["fill_source"] == "route_quote" and leg["price"] == 1.5


def test_unavailable_quote_books_noquote_never_fomo():
    conn = _conn()
    pid = _open(conn)
    out = ct._mirror_close_arm(conn, _sell_ev(their_price=9.0), NOW, "alpha_b",
                               quote_fn=lambda mint, net, qty: (None, "quote_unavailable"))
    assert out == "exit"
    row = conn.execute("SELECT is_open, exit_reason, realized_pnl FROM memecoin_positions "
                       "WHERE id=?", (pid,)).fetchone()
    assert row["is_open"] == 0 and row["exit_reason"] == "b_mirror_noquote"
    # UPDATED 2026-08-29. The comment on this line already said "no fabricated PnL" while the
    # assertion demanded one: entry-priced means UNPRICED, so it books NULL now.
    assert row["realized_pnl"] is None
    (leg,) = _fill_rows(conn, pid)
    assert leg["fill_source"] == "noquote" and leg["price"] == 1.0
    # the trader's price must never leak into the ledger as a fill
    assert not [r for r in _fill_rows(conn, pid) if r["fill_source"] == "fomo"]


def test_report_censors_noquote_mirror_close():
    conn = _conn()
    pid = _open(conn)
    ct._mirror_close_arm(conn, _sell_ev(), NOW, "alpha_b",
                         quote_fn=lambda mint, net, qty: (None, "quote_unavailable"))
    from sniper.services.memecoin_paper_report import _add_outcome, _project_rows
    now = datetime(2026, 8, 5, 2, tzinfo=timezone.utc)
    (row,) = [_add_outcome(r, now) for r in _project_rows(conn, "solana")
              if r["id"] == pid]
    assert row["report_pnl"] is None
    assert row["report_outcome_state"] == "censored_noquote"


def test_spike_guarded_quote_books_noquote_not_the_glitch():
    conn = _conn()
    pid = _open(conn)
    m.update_position_last_quote(conn, pid, 1.1, "2026-08-05T00:30:00+00:00")
    out = ct._mirror_close_arm(conn, _sell_ev(), NOW, "alpha_b",
                               quote_fn=lambda mint, net, qty: (1.1 * 11, "route_quote"))
    assert out == "exit"
    row = conn.execute("SELECT exit_reason, realized_pnl FROM memecoin_positions WHERE id=?",
                       (pid,)).fetchone()
    assert row["exit_reason"] == "b_mirror_noquote" and row["realized_pnl"] is None


def test_one_quote_per_qty_across_arms():
    conn = _conn()
    for arm in ("control", "alpha_b", "real_shadow"):
        _open(conn, strategy=arm)
    calls = []

    def quote_fn(mint, net, qty):
        calls.append((net, qty))
        return 1.4, "route_quote"

    res = ct.copy_event_to_arms(
        conn, _sell_ev(), 100.0, NOW, arms=("control", "alpha_b", "real_shadow"),
        fill_fn=lambda *a, **k: (None, None), exit_quote_fn=quote_fn)
    assert res == {"control": "exit", "alpha_b": "exit", "real_shadow": "exit"}
    assert len(calls) == 1                                  # shared entry fill -> shared quote


def test_fill_model_forks_the_cohort():
    assert ct.PAPER_FILL_MODEL == "jupiter-executable-route-v2"
    from sniper.services.memecoin_policy import build_paper_cohort
    v2_id, _ = build_paper_cohort(("control", "alpha_b"), notional_usd=100.0)
    import sniper.services.memecoin_copy_trader as mod
    orig = mod.PAPER_FILL_MODEL
    try:
        mod.PAPER_FILL_MODEL = "jupiter-executable-route-v1"
        v1_id, _ = build_paper_cohort(("control", "alpha_b"), notional_usd=100.0)
    finally:
        mod.PAPER_FILL_MODEL = orig
    assert v1_id != v2_id


def test_chain_omitted_sell_still_mirrors_via_fanout():
    conn = _conn()
    pid = _open(conn, network_id="4663", mint="0xabc")
    res = ct.copy_event_to_arms(
        conn, _sell_ev(mint="0xabc", network_id=None), 100.0, NOW, arms=("alpha_b",),
        fill_fn=lambda *a, **k: (None, None),
        exit_quote_fn=lambda mint, net, qty: (1.2, "route_quote"))
    assert res == {"alpha_b": "exit"}
    row = conn.execute("SELECT is_open FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    assert row["is_open"] == 0


def test_4663_default_seam_converts_weth_to_usd(monkeypatch):
    """End-to-end through the DEFAULT seam (executable_exit_fill): a 4663 route quote comes
    back WETH-denominated and must book in USD, or the ~2000x unit-mixing bug returns."""
    from sniper.services import memecoin_quotes as q
    monkeypatch.setattr(ct, "_exit_token_decimals", lambda mint, net: 18)
    monkeypatch.setattr(
        q, "quote_exit",
        lambda position, qty_atomic, rpc_url=None, evm_rpc_url=None:
        SimpleNamespace(unit_price=0.0005))              # WETH per token
    monkeypatch.setattr(q, "quote_usd_price", lambda net: 2000.0)   # ETH/USD
    conn = _conn()
    pid = _open(conn, network_id="4663", mint="0xabc", entry_price=0.9)
    out = ct._mirror_close_arm(conn, _sell_ev(mint="0xabc", network_id="4663"), NOW,
                               "alpha_b")
    assert out == "exit"
    (leg,) = _fill_rows(conn, pid)
    assert leg["fill_source"] == "route_quote"
    assert abs(leg["price"] - 1.0) < 1e-9                  # 0.0005 WETH * $2000
