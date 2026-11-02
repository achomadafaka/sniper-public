"""Entry/close chain-identity symmetry: entries store a RESOLVED network, but sell events
often omit or under-resolve the chain — the close/dup paths must not key on the raw sell
tag alone. Regression for the 2026-07-15 entry-speed-coverage review findings:
  1. real/paper mirror-close missed a concretely-tagged position when the sell arrived
     with network_id=None ('evm-unknown' bucket) -> funded bag held through the dump;
  2. open-side dedup double-opened the same coin during the evm-unknown -> concrete heal
     window.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_copy_trader as ct
from sniper.services import memecoin_real_runner as real_runner


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _open(conn, *, network_id, mint="0xabc", mode="paper", strategy="alpha_b", uid="u1"):
    pid = m.open_position(
        conn, source_user_id=uid, source_handle="h", mint=mint, ticker="T",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="dexscreener",
        entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-15T00:00:00+00:00",
        strategy=strategy, execution_mode=mode, network_id=network_id)
    m.insert_fill(conn, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="dexscreener", fraction=1.0,
                  created_at="2026-07-15T00:00:00+00:00")
    return pid


def _sell_ev(mint="0xabc", network_id=None, uid="u1"):
    return SimpleNamespace(source_user_id=uid, mint=mint, network_id=network_id,
                           side="sell", their_price=1.2, fomo_event_id="ev-sell-1")


def test_open_positions_for_coin_is_network_agnostic():
    conn = _conn()
    _open(conn, network_id="4663")
    rows = m.open_positions_for_coin(conn, "u1", "0xabc", "alpha_b")
    assert len(rows) == 1
    # exact-bucket lookup with the raw (missing) sell tag misses it — the fallback must not
    assert m.get_open_position(conn, "u1", "0xabc", "alpha_b", network_id=None) is None


def test_paper_mirror_close_falls_back_when_sell_omits_chain():
    conn = _conn()
    pid = _open(conn, network_id="4663")
    out = ct._mirror_close_arm(conn, _sell_ev(network_id=None), "2026-07-15T01:00:00+00:00",
                               "alpha_b", quote_fn=lambda mint, net, qty: (1.2, "route_quote"))
    assert out == "exit"
    row = conn.execute("SELECT is_open, exit_reason FROM memecoin_positions WHERE id=?",
                       (pid,)).fetchone()
    assert row["is_open"] == 0 and row["exit_reason"] == "b_mirror"


def test_paper_mirror_close_refuses_cross_chain_ambiguity():
    conn = _conn()
    a = _open(conn, network_id="8453")
    b = _open(conn, network_id="4663")
    out = ct._mirror_close_arm(conn, _sell_ev(network_id=None), "2026-07-15T01:00:00+00:00",
                               "alpha_b", quote_fn=lambda mint, net, qty: (1.2, "route_quote"))
    assert out == "ignored"
    open_flags = [r[0] for r in conn.execute(
        "SELECT is_open FROM memecoin_positions WHERE id IN (?,?)", (a, b))]
    assert open_flags == [1, 1]


def test_open_arm_dedups_across_network_buckets():
    conn = _conn()
    _open(conn, network_id="evm-unknown")   # pre-heal row
    ev = SimpleNamespace(source_user_id="u1", mint="0xabc", network_id="8453")
    out = ct._open_arm(conn, ev, 100.0, 1.0, "dexscreener", 50_000.0,
                       "2026-07-15T01:00:00+00:00", "alpha_b")
    assert out == "skipped_dup"
    n = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1").fetchone()[0]
    assert n == 1


def test_real_mirror_close_falls_back_when_sell_omits_chain(monkeypatch):
    monkeypatch.setattr(real_runner.cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)
    conn = _conn()
    pid = _open(conn, network_id="4663", mode="real")
    monkeypatch.setattr(real_runner.cfg, "MEMECOIN_REAL_POLICY", "alpha_b")
    calls = []

    def exit_fn(c, pos, reason, now_iso):
        calls.append((pos["id"], reason))
        return {"status": "closed"}

    monkeypatch.setattr(real_runner, "_alert_exit_result", lambda *a, **k: None)
    monkeypatch.setattr(real_runner, "check_wallet_demotion", lambda *a, **k: None)
    contexts = {"4663": {"exit_fn": exit_fn}}
    res = real_runner.real_mirror_on_sell(conn, _sell_ev(network_id=None), contexts,
                                          now_fn=lambda: "2026-07-15T01:00:00+00:00")
    assert calls == [(pid, "mirror")]
    assert res == {"status": "closed"}


def test_real_mirror_close_refuses_cross_chain_ambiguity(monkeypatch):
    conn = _conn()
    _open(conn, network_id="8453", mode="real")
    _open(conn, network_id="4663", mode="real")
    monkeypatch.setattr(real_runner.cfg, "MEMECOIN_REAL_POLICY", "alpha_b")
    calls = []
    contexts = {"8453": {"exit_fn": lambda *a: calls.append(a)},
                "4663": {"exit_fn": lambda *a: calls.append(a)}}
    res = real_runner.real_mirror_on_sell(conn, _sell_ev(network_id=None), contexts,
                                          now_fn=lambda: "2026-07-15T01:00:00+00:00")
    assert res is None and calls == []
