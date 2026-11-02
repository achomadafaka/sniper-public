"""A censored paper exit must record WHY, and keep its label byte-identical while it does.

`executable_exit_fill` returns `(None, reason)` and BOTH censor sites discarded the reason, so six
distinct failures — `qty_unresolved`, `nonpositive_quote`, `quote_asset_price_unavailable`,
`exception:<Type>`, the sweep's plausibility veto and the mirror's bad-tick quarantine — all read
as the single label `<rail>_noquote`. Measured 2026-09-24 on funded_clone since 09-20: 4663 4 of
193 closes censored, solana 44 of 733, and `grep -c "paper exit quote unavailable"` over the whole
24 MB runner log returns 0 — so every censored row so far came from a cause that wrote nothing
anywhere and the ledger could not say which one.

What must NOT change, because the report censor and the `price_attribution` readers key on them:
`exit_reason` stays `<rail>_noquote`, `fill_source` stays `noquote`, `realized_pnl` stays 0/NULL.

The `rail` field is what keeps an `unquotable` censor — where the rail had already given up before
any quote was asked — from pooling with a live TP or stop that simply could not be priced.

PAPER ONLY. `_noquote` exists in `memecoin_exits.py`, `memecoin_copy_trader.py` and the paper
report's reader; the funded close path in `memecoin_real_runner.py` books an exact whole-bag sale
through its own functions and never appends the suffix.
"""
import json

import pytest

import sniper.services.memecoin_exits as ex
import sniper.services.memecoin_copy_trader as ct
from sniper.persistence import db_memecoin as m
from test_memecoin_exits import _conn, _open

NOW = "2026-07-07T10:05:00+00:00"


def _cause(conn, pid):
    """The recorded cause for `pid`, or None. Its own reason string, so nothing can upsert it."""
    row = conn.execute(
        "SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=? AND reason=?",
        (pid, ct.NOQUOTE_CAUSE_REASON)).fetchone()
    return json.loads(row["detail"]) if row else None


def _quote(price, reason="route_quote"):
    return lambda mint, network_id, qty, mid=None, liq=None: (price, reason)


@pytest.mark.parametrize("reason", [
    "qty_unresolved", "nonpositive_quote", "quote_asset_price_unavailable",
    "exception:ReadTimeout",
])
def test_each_quote_failure_lands_its_own_cause(monkeypatch, reason):
    c = _conn(); pid = _open(c, "funded_clone")
    monkeypatch.setattr(ex, "_exit_quote_fn", _quote(None, reason))
    out = ex._close(c, m.get_position(c, pid), 0.5, 1e15, NOW, "real_tp")

    assert out["exit_reason"] == "real_tp_noquote", "the label the report censors is unchanged"
    assert _cause(c, pid) == {"cause": reason, "rail": "real_tp"}
    fill = c.execute("SELECT fill_source, price_attribution FROM memecoin_fills"
                     " WHERE position_id=? AND kind='bracket_exit'", (pid,)).fetchone()
    assert fill["fill_source"] == "noquote"
    assert fill["price_attribution"] == m.PLACEHOLDER_PRICE_ATTRIBUTION


def test_the_plausibility_veto_is_its_own_cause_not_the_quote_s(monkeypatch):
    """The one censor that says nothing about whether the coin could be priced.

    A quote came back and `_quote_plausible` rejected it, so recording the quote's own reason
    (`route_quote`) would say the opposite of what happened. Uses the 2026-09-15 4663 standard:
    mid 0.3142, quote 3.4445, 10.96x.
    """
    c = _conn(); pid = _open(c, "funded_clone", entry=0.24093718456569713)
    monkeypatch.setattr(ex, "_exit_quote_fn", _quote(3.444469923602038))
    out = ex._close(c, m.get_position(c, pid), 0.3142, 13_925_538.7, NOW, "real_tp")

    assert out["exit_reason"] == "real_tp_noquote"
    assert _cause(c, pid) == {"cause": "plausibility_veto", "rail": "real_tp"}


def test_an_unquotable_rail_keeps_its_own_rail_so_it_never_pools(monkeypatch):
    """44 of solana's 44 censored rows since 09-20 are `unquotable` — the rail had already given
    up before any quote was asked. Pooling them with a live TP or stop is the whole error."""
    c = _conn(); pid = _open(c, "funded_clone")
    monkeypatch.setattr(ex, "_exit_quote_fn", _quote(None, "qty_unresolved"))
    ex._close(c, m.get_position(c, pid), 0.5, 1e15, NOW, "unquotable")

    assert _cause(c, pid) == {"cause": "qty_unresolved", "rail": "unquotable"}


def test_a_priced_exit_records_no_cause(monkeypatch):
    """The other direction. A writer that fires on every close measures closes, not censoring."""
    c = _conn(); pid = _open(c, "funded_clone", entry=0.24)
    monkeypatch.setattr(ex, "_exit_quote_fn", _quote(0.3142 * 1.2))
    out = ex._close(c, m.get_position(c, pid), 0.3142, 1e9, NOW, "real_tp")

    assert out["exit_reason"] == "real_tp", "priced, not censored"
    assert _cause(c, pid) is None


def test_the_mirror_censor_records_its_cause_too():
    """The second censor site. Patching only the sweep leaves this one blind."""
    from test_memecoin_mirror_executable_exit import _conn as mconn, _open as mopen, _sell_ev
    conn = mconn(); pid = mopen(conn)
    out = ct._mirror_close_arm(conn, _sell_ev(their_price=9.0), NOW, "alpha_b",
                               quote_fn=lambda mint, net, qty: (None, "qty_unresolved"))

    assert out == "exit"
    row = conn.execute("SELECT exit_reason FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    assert row["exit_reason"] == "b_mirror_noquote"
    assert _cause(conn, pid) == {"cause": "qty_unresolved", "rail": "b_mirror"}


def test_the_mirror_bad_tick_quarantine_is_not_reported_as_a_quote_failure():
    """A quote came back far above the observed peak and the spike guard rejected it.

    TWO fixture values decide whether the defect is even reachable, and my first attempt reached
    neither. The peak must be 1.2 and not the entry 1.0, because the guard excludes a prior EQUAL
    to entry on purpose ("an exact-entry peak is initialization, not an observation") and a
    prior of 0 disables it entirely. The quote must then clear `_SPIKE_RATIO_LIMIT` 10.0 against
    that peak: 15.0 / 1.2 = 12.5x quarantines, and anything under 12.0 would simply be BOOKED.
    """
    from test_memecoin_mirror_executable_exit import _conn as mconn, _open as mopen, _sell_ev
    conn = mconn(); pid = mopen(conn)
    m.update_position_peak(conn, pid, 1.2, NOW, 0)
    out = ct._mirror_close_arm(conn, _sell_ev(), NOW, "alpha_b",
                               quote_fn=lambda mint, net, qty: (15.0, "route_quote"))

    assert out == "exit"
    assert _cause(conn, pid) == {"cause": "mirror_bad_tick_quarantined", "rail": "b_mirror"}
