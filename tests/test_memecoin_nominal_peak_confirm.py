"""The display peak needs a second tick (ANIMA 73930, 2026-10-17): one fastlane_sol_state tick at
1.884e-04 (+70%) while every swap read ~1.2e-04 set the card's peak; 73892 one swap print at 4.5x.
_quarantine_spikes (10x up-bound) does not catch either."""
import pytest

from sniper.services import memecoin_real_runner as R

# The ANIMA sweep ticks of 2026-10-17 16:06:11..16:06:29Z (memecoin_position_marks) plus the one state
# tick the focus pass saw at 16:06:20.765Z (seconds from 16:06:00).
SWAPS = [(11.138, 1.2070696870014628e-04), (12.186, 1.2179905994280006e-04), (13.614, 1.2137821103503452e-04),
         (15.119, 1.2009406399362323e-04), (16.275, 1.1847799884926976e-04), (17.597, 1.2168323835536685e-04),
         (18.683, 1.2360842609356328e-04), (20.263, 1.1814520956530844e-04), (21.556, 1.1808303163442067e-04),
         (22.600, 1.1619594960174848e-04), (24.612, 1.1323010583811622e-04), (26.466, 1.1643286746531030e-04),
         (27.666, 1.1962006417969098e-04), (29.116, 1.2495656649037876e-04)]
STATE = (20.765, 1.8844828798277076e-04)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(R, "_PEAK_TICKS", R.collections.OrderedDict())


def test_the_anima_state_tick_never_sets_the_peak():
    ticks = sorted([(t, p, "fastlane_sol_swap") for t, p in SWAPS] + [(STATE[0], STATE[1], "fastlane_sol_state")])
    peak = 1.1076301930446505e-04                                       # entry
    for t, price, source in ticks:
        confirmed = R._confirmed_nominal_peak(73930, source, price, now_mono=1000.0 + t)
        if confirmed is not None and confirmed > peak:
            peak = confirmed
    # The peak stays at a real swap level (+12.8%: the 16:06:29 print, bounded above by the
    # still-fresh state tick, and below by itself), never the state tick's +70%.
    assert peak == pytest.approx(1.2495656649037876e-04) and peak < STATE[1] / 1.4


def test_a_move_two_ticks_confirm_still_sets_the_peak_and_a_lone_first_tick_does_not():
    assert R._confirmed_nominal_peak(1, "fastlane_sol_swap", 2.0, now_mono=0.0) is None
    assert R._confirmed_nominal_peak(1, "fastlane_sol_swap", 2.1, now_mono=1.0) == 2.0
    assert R._confirmed_nominal_peak(1, "fastlane_sol_state", 2.2, now_mono=2.0) == 2.1


def test_a_stale_other_tick_does_not_confirm():
    R._confirmed_nominal_peak(2, "fastlane_sol_swap", 1.0, now_mono=0.0)
    assert R._confirmed_nominal_peak(2, "fastlane_sol_state", 4.5, now_mono=R._PEAK_CONFIRM_S + 0.1) is None


def test_the_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(R, "_PEAK_TICKS_MAX", 3)
    for pid in range(5):
        R._confirmed_nominal_peak(pid, "s", 1.0, now_mono=0.0)
    assert len(R._PEAK_TICKS) == 3


def _sweep(c, price, source):
    from test_memecoin_real_peak_tracking import NOW, _no_exit_context
    R.real_exit_sweep(c, _no_exit_context(), now_fn=lambda: NOW,
                      price_fn=lambda mints: {("solana", "So1"): {"price": price, "source": source, "quality": "live"}})


@pytest.fixture
def no_rails(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0)


@pytest.mark.parametrize("ticks,lone,source", [
    ([(1.13, "fastlane_sol_swap"), (1.13, "fastlane_sol_swap")], 1.70, "fastlane_sol_state"),   # ANIMA 73930
    ([(1.0, "fastlane_sol_swap"), (1.02, "fastlane_sol_swap")], 4.5, "fastlane_sol_swap"),      # 73892's print
])
def test_a_lone_tick_holds_the_peak_and_marks_it_unconfirmed_on_the_card(no_rails, ticks, lone, source):
    from flask import Flask
    from sniper.persistence import db_memecoin as m
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    from test_memecoin_real_peak_tracking import _c, _open_real
    c = _c()
    pid = _open_real(c, entry_price=1.0)
    for price, src in ticks:
        _sweep(c, price, src)
    confirmed = ticks[-1][0]                                  # the lone tick confirms the tick before it, no more
    _sweep(c, lone, source)                                   # one tick, nothing agrees with it
    pos = m.get_position(c, pid)
    assert pos["peak_price"] == pytest.approx(confirmed) and pos["peak_unconfirmed_price"] == pytest.approx(lone)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    row = next(p for p in app.test_client().get("/api/memecoin/real/positions").get_json()["positions"]
               if p["id"] == pid)
    assert row["peak_unconfirmed"] is True and row["peak_unconfirmed_pct"] == pytest.approx((lone - 1) * 100)
    m.update_position_peak(c, pid, lone + 0.1, "2026-07-09T12:00:00+00:00", 0)          # a confirmed higher peak
    row = next(p for p in app.test_client().get("/api/memecoin/real/positions").get_json()["positions"]
               if p["id"] == pid)
    assert row["peak_unconfirmed"] is False and row["peak_unconfirmed_pct"] is None


def test_a_source_never_confirms_itself_while_another_source_is_fresh():
    """Gamma BLOCK on 318f4ee4d: ANIMA's state source printed three big_move ticks (slots 807/809/
    810). swap, state bad, state bad, swap: the peak stays at the swap level."""
    seq = [("fastlane_sol_swap", 1.21e-04), ("fastlane_sol_state", 1.88e-04),
           ("fastlane_sol_state", 1.87e-04), ("fastlane_sol_swap", 1.22e-04)]
    peak = 1.10e-04
    for i, (source, price) in enumerate(seq):
        confirmed = R._confirmed_nominal_peak(73930, source, price, now_mono=100.0 + i)
        if confirmed is not None and confirmed > peak:
            peak = confirmed
    assert peak <= 1.22e-04


def test_with_no_other_source_fresh_the_previous_own_tick_still_confirms():
    assert R._confirmed_nominal_peak(9, "fastlane_sol_swap", 1.5, now_mono=0.0) is None
    assert R._confirmed_nominal_peak(9, "fastlane_sol_swap", 1.6, now_mono=1.0) == 1.5
