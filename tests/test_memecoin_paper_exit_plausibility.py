"""A paper exit may not book a route quote far above the live mark that fired the rail.

STANDARD (4663, 2026-09-15): the TP rail fired on DexScreener mids 0.3142 and 0.3549, and the
whole-bag route quote came back at 3.4445 and 2.5137 USD per token, 10.96x and 7.08x the mid.
Four $100 paper positions booked +$1,329 and two booked +$804, $6,925.60 in all. Real tracked trades
within two minutes stayed near 0.31-0.35 and the next sweep (01:57:26) quoted 0.98x its mid. The
source of the bad number is not proven (the 4663 node keeps no historical state for a replay), so
the booking is bounded instead: such a quote is treated as unavailable, which is the existing
`_noquote` close that every paper report censors. No mid, and no stale-mark close, is bounded.
"""
import pytest

import sniper.services.memecoin_exits as ex
from sniper.persistence import db_memecoin as m
from test_memecoin_exits import _conn, _open

NOW = "2026-07-07T10:05:00+00:00"


def _quote(price):
    return lambda mint, network_id, qty, mid=None, liq=None: (price, "route_quote")


def test_standard_real_tp_does_not_book_an_eleven_times_quote(monkeypatch):
    c = _conn(); pid = _open(c, "funded_clone", entry=0.24093718456569713)
    pos = m.get_position(c, pid)
    monkeypatch.setattr(ex, "_exit_quote_fn", _quote(3.444469923602038))
    out = ex._close(c, pos, 0.3142, 13_925_538.7, NOW, "real_tp")
    closed = m.get_position(c, pid)
    assert out["exit_reason"] == "real_tp_noquote"
    assert closed["exit_reason"] == "real_tp_noquote"
    assert abs(closed["realized_pnl"] or 0.0) < 1e-9, "entry-priced, censored, never +$1,329"
    fill = c.execute("SELECT price, fill_source, price_attribution FROM memecoin_fills "
                     "WHERE position_id=? AND kind='bracket_exit'", (pid,)).fetchone()
    assert fill["fill_source"] == "noquote" and fill["price_attribution"] == m.PLACEHOLDER_PRICE_ATTRIBUTION


def test_a_quote_near_the_mark_is_booked(monkeypatch):
    c = _conn(); pid = _open(c, "funded_clone", entry=0.24)
    pos = m.get_position(c, pid)
    monkeypatch.setattr(ex, "_exit_quote_fn", _quote(0.3142 * 1.2))
    out = ex._close(c, pos, 0.3142, 1e9, NOW, "real_tp")
    closed = m.get_position(c, pid)
    assert out["exit_reason"] == "real_tp" and closed["exit_reason"] == "real_tp"
    assert closed["realized_pnl"] == pytest.approx(100.0 * 0.3142 * 1.2 / 0.24 - 100.0)


@pytest.mark.parametrize("ratio,booked", [(4.9, True), (5.1, False)])
def test_the_bound_is_the_configured_multiple_of_the_larger_of_mid_and_peak(monkeypatch, ratio, booked):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_PAPER_EXIT_MAX_QUOTE_TO_MARK", 5.0)
    c = _conn(); pid = _open(c, "funded_clone", entry=1.0)
    m.update_position_peak(c, pid, 2.0, NOW, 0)
    pos = m.get_position(c, pid)
    monkeypatch.setattr(ex, "_exit_quote_fn", _quote(2.0 * ratio))     # peak 2.0 > mid 1.5
    out = ex._close(c, pos, 1.5, 1e9, NOW, "real_tp")
    assert (out["exit_reason"] == "real_tp") is booked


def test_a_stale_mark_close_is_not_bounded(monkeypatch):
    """A time cap on an unquotable coin passes its LAST mark, which can be hours old; the route quote
    is the only current price there (PVE 2026-09-13 exited at 5.3x its stale mark, and real trades
    confirmed the price)."""
    c = _conn(); pid = _open(c, "funded_clone", entry=1.0)
    pos = m.get_position(c, pid)
    monkeypatch.setattr(ex, "_exit_quote_fn", _quote(20.0))
    out = ex._close(c, pos, 1.0, None, NOW, "b_timecap_stale", source="dexscreener_stale")
    assert out["exit_reason"] == "b_timecap_stale"


def test_the_rejected_quote_is_logged_with_both_prices(monkeypatch, caplog):
    c = _conn(); pid = _open(c, "funded_clone", entry=1.0)
    pos = m.get_position(c, pid)
    monkeypatch.setattr(ex, "_exit_quote_fn", _quote(50.0))
    with caplog.at_level("WARNING"):
        ex._close(c, pos, 1.25, 1e9, NOW, "real_tp")
    assert "50" in caplog.text and "1.25" in caplog.text and str(pid) in caplog.text
