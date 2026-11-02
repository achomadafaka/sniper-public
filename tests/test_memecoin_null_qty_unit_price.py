"""A partial with an unknown quantity must not make the remaining bag read as down by its fraction.

m.open_qty sums fill qty, and SUM skips NULL: an unpriced derisk partial whose intent could not say
its quantity (settle_confirmed_derisk_unpriced, qty None) leaves open_qty at the pre-partial
quantity. The exact quote values only the bag actually held, so expected / open_qty understated the
unit price by the partial's fraction: a bag at +10% after a 0.5 partial read -45%, and catastrophe
and real_trail have no chain-quantity second witness (Beta finding, Alpha 2026-10-07, narrowed (a)).

Only such a position uses the quote's own amount (amount_atomic / 10**decimals) as the bag; every
other position keeps ledger open_qty, byte-identical. Unknown decimals: the unit price is unknown
and the price thresholds refuse to confirm.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr
from sniper.settings import memecoin as cfg

DECIMALS = 6


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _position(conn):
    opened = "2026-07-11T00:00:00+00:00"
    pid = m.open_position(
        conn, source_user_id="u", source_handle="h", mint="M", ticker="T", entry_price=1.0,
        entry_notional=100.0, entry_fill_source="chain", entry_lag_s=0, their_size_usd=0,
        opened_at=opened, strategy="alpha_b", execution_mode="real", network_id="solana")
    m.insert_fill(conn, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source="chain", fraction=1.0, created_at=opened)
    return pid


def _null_qty_partial(conn, pid):
    m.insert_fill(conn, position_id=pid, kind="derisk", price=None, qty=None, notional=None,
                  fill_source="chain_confirmed_unpriced", fraction=0.5,
                  created_at="2026-07-11T00:30:00+00:00", tx_sig="sig")


def _bag_at_plus_10(tokens=50):
    """The exact quote of the bag the wallet holds: `tokens` at 1.1 each."""
    return SimpleNamespace(expected_usdc=1.1 * tokens, amount_atomic=tokens * 10 ** DECIMALS,
                           route_id="r", quoted_at="2026-07-11T01:00:00+00:00")


@pytest.fixture(autouse=True)
def _pages(monkeypatch):
    sent = []
    monkeypatch.setattr(rr, "_UNIT_PRICE_UNKNOWN_PAGED", set())
    monkeypatch.setattr(rr.alerts, "send_alert", lambda kind, text, **k: sent.append((kind, text)))
    return sent


@pytest.fixture
def partial():
    conn = _conn()
    pid = _position(conn)
    _null_qty_partial(conn, pid)
    pos = dict(m.get_position(conn, pid), executable_peak_price=1.2,
               derisked_at="2026-07-11T00:30:00+00:00")
    return conn, pos


def test_a_null_qty_partial_values_the_bag_by_the_quotes_own_amount(partial):
    conn, pos = partial
    assert m.open_qty(conn, pos["id"]) == 100.0                 # the ledger still says 100
    assert rr._executable_unit_price(conn, pos, _bag_at_plus_10(),
                                     decimals_fn=lambda mint: DECIMALS) == pytest.approx(1.1)


@pytest.mark.parametrize("reason, override", [
    ("catastrophe", None), ("real_trail", {"trail_pct": 10.0}), ("b_stop", None)])
def test_a_bag_at_plus_10_after_a_null_qty_half_partial_fires_no_loss_rail(
        partial, monkeypatch, reason, override):
    conn, pos = partial
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 35.0)
    allowed, detail = rr.executable_exit_allowed(conn, pos, reason, _bag_at_plus_10(),
                                                 override=override,
                                                 decimals_fn=lambda mint: DECIMALS)
    assert allowed is False, detail
    assert detail["executable_unit_pnl_pct"] == pytest.approx(10.0)    # not -45


@pytest.mark.parametrize("decimals_fn", [None, lambda mint: None])
@pytest.mark.parametrize("reason, override", [
    ("catastrophe", None), ("real_trail", {"trail_pct": 10.0}), ("real_tp", None)])
def test_unknown_decimals_leave_the_unit_price_unknown_and_refuse(
        partial, monkeypatch, decimals_fn, reason, override):
    conn, pos = partial
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 5.0)
    assert rr._executable_unit_price(conn, pos, _bag_at_plus_10(), decimals_fn=decimals_fn) is None
    allowed, detail = rr.executable_exit_allowed(conn, pos, reason, _bag_at_plus_10(),
                                                 override=override, decimals_fn=decimals_fn)
    assert allowed is False and detail["code"] == "threshold_unknown_exit_qty", detail
    assert detail["executable_unit_pnl_pct"] is None


def test_a_raising_decimals_read_is_unknown_never_a_crash(partial):
    conn, pos = partial

    def boom(mint):
        raise TimeoutError("rpc")
    assert rr._executable_unit_price(conn, pos, _bag_at_plus_10(), decimals_fn=boom) is None


def test_a_normal_position_keeps_ledger_open_qty_and_never_asks_for_decimals():
    conn = _conn()
    pid = _position(conn)
    m.insert_fill(conn, position_id=pid, kind="derisk", price=1.0, qty=50.0, notional=50.0,
                  fill_source="chain", fraction=0.5, created_at="2026-07-11T00:30:00+00:00")
    pos = m.get_position(conn, pid)
    odd = SimpleNamespace(expected_usdc=55.0, amount_atomic=12345, route_id="r",
                          quoted_at="2026-07-11T01:00:00+00:00")       # an amount it must ignore

    def never(mint):
        raise AssertionError("decimals read for a position with every qty known")
    before = rr._executable_unit_price(conn, pos, odd)
    assert rr._executable_unit_price(conn, pos, odd, decimals_fn=never) == before == 55.0 / 50.0


def test_an_unpriced_partial_records_prior_proceeds_as_null_not_zero(partial, monkeypatch):
    conn, pos = partial
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    _allowed, detail = rr.executable_exit_allowed(conn, pos, "real_tp", _bag_at_plus_10(),
                                                  decimals_fn=lambda mint: DECIMALS)
    assert detail["prior_proceeds"] is None


@pytest.mark.parametrize("with_decimals, handed_to_exit", [(True, []), (False, ["b_stop"])])
def test_the_sweep_sells_nothing_from_a_bag_at_plus_10_after_a_null_qty_partial(
        monkeypatch, with_decimals, handed_to_exit):
    """End to end through real_exit_sweep: the context's decimals_fn reaches the nomination and the
    confirmation, and nothing fires. Without decimals, catastrophe is refused and only b_stop is
    handed to exit_fn, whose execution witness (tested above) re-checks it on the chain quantity."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    conn = _conn()
    pid = _position(conn)
    _null_qty_partial(conn, pid)
    conn.execute("UPDATE memecoin_positions SET derisked_at=? WHERE id=?",
                 ("2026-07-11T00:30:00+00:00", pid))
    conn.commit()
    calls = []
    context = {"preview_exit_fn": lambda _pos: _bag_at_plus_10(),
               "exit_fn": lambda _conn, _pos, reason, _now: calls.append(reason) or {"status": "unknown"}}
    if with_decimals:
        context["decimals_fn"] = lambda mint: DECIMALS
    rr.real_exit_sweep(conn, {"solana": context}, price_fn=lambda _mints: {"M": {"price": 0.40}},
                       now_fn=lambda: "2026-07-11T01:00:00+00:00")
    assert calls == handed_to_exit


def test_unknown_decimals_still_let_b_stop_confirm_and_its_chain_qty_witness_decides(partial, monkeypatch):
    """Alpha: never hold a bag through a real crash with no stop. The sweep's b_stop confirms on the
    total-proceeds fallback; confirm_execution_stop re-checks on the chain quantity it sells."""
    conn, pos = partial
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 35.0)
    allowed, detail = rr.executable_exit_allowed(conn, pos, "b_stop", _bag_at_plus_10())
    assert allowed is True and detail["bag_unknown"] is True, detail
    allowed, detail = rr.confirm_execution_stop(conn, pos, 55.0, 50.0)   # 50 tokens at 1.1
    assert allowed is False and detail["executable_unit_pnl_pct"] == pytest.approx(10.0), detail
    crash = rr.confirm_execution_stop(conn, pos, 25.0, 50.0)             # 50 tokens at 0.5: -50%
    assert crash[0] is True, crash


def test_an_unvaluable_bag_pages_the_operator_once_per_position(partial, _pages):
    conn, pos = partial
    for reason in ("catastrophe", "real_tp", "b_stop"):
        rr.executable_exit_allowed(conn, pos, reason, _bag_at_plus_10())
    assert len(_pages) == 1 and "unit price unknown: qty NULL partial + no decimals" in _pages[0][1]


def test_a_valued_bag_pages_nothing(partial, _pages):
    conn, pos = partial
    rr.executable_exit_allowed(conn, pos, "catastrophe", _bag_at_plus_10(),
                               decimals_fn=lambda mint: DECIMALS)
    assert _pages == []


# ---- Alpha 2026-10-07 on 50c9be7db: the execution witness HOLDs only on a fallback verdict ----

def test_bag_unknown_and_a_readable_witness_at_minus_50_sells(partial, _pages):
    conn, pos = partial
    allowed, detail = rr.confirm_execution_stop(conn, pos, 25.0, 50.0, bag_unknown=True)
    assert allowed is True, detail


@pytest.mark.parametrize("expected, quantity", [(None, 50.0), (25.0, 0.0)])
def test_bag_unknown_and_an_unreadable_witness_holds_and_pages_once(partial, _pages,
                                                                    expected, quantity):
    conn, pos = partial
    for _ in range(2):
        allowed, detail = rr.confirm_execution_stop(conn, pos, expected, quantity, bag_unknown=True)
        assert allowed is False and detail["code"] == "execution_quote_unreadable_bag_unknown_hold"
    assert len(_pages) == 1


def test_a_normal_position_with_an_unreadable_witness_still_sells():
    conn = _conn()
    pos = m.get_position(conn, _position(conn))
    allowed, detail = rr.confirm_execution_stop(conn, pos, None, 50.0)
    assert allowed is True and detail["code"] == "execution_quote_unreadable_sweep_verdict_stands"


def test_the_sweep_hands_the_fallback_flag_to_the_executor_and_clears_it(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    conn = _conn()
    pid = _position(conn)
    _null_qty_partial(conn, pid)
    seen = []

    def exit_fn(_conn, pos, reason, _now):
        seen.append((reason, pos.get("_exit_bag_unknown")))
        return {"status": "unknown"}
    rr.real_exit_sweep(conn, {"solana": {"preview_exit_fn": lambda _pos: _bag_at_plus_10(),
                                         "exit_fn": exit_fn}},
                       price_fn=lambda _mints: {"M": {"price": 0.40}},
                       now_fn=lambda: "2026-07-11T01:00:00+00:00")
    assert seen == [("b_stop", True)]


def test_unknown_sell_quantity_cannot_confirm_a_custom_mcap_stop_on_a_market_mark(partial):
    conn, pos = partial
    allowed, detail = rr.executable_exit_allowed(
        conn, pos, "b_stop", _bag_at_plus_10(), override={"sl_pct": 0, "sl_target_mcap": 1_000_000},
        current_mcap=900_000)
    assert allowed is False and detail.get("bag_unknown") and not detail.get("mcap_stop_confirmed")


def test_both_seams_pass_the_flag_explicitly():
    import inspect
    from sniper.execution import evm_real_seams, solana_real_seams
    for module in (evm_real_seams, solana_real_seams):
        assert 'bag_unknown=bool(position.get("_exit_bag_unknown"))' in inspect.getsource(module)
