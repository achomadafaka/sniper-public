"""The exit dust-route guard (operator "fix everything", Gamma MED, 2026-10-02; rebuilt 2026-10-06).

DAODUN 68544 on chain 56: Kyber valued the whole ~$44 bag at 0.0002 WBNB (~$0.16), the runner
marked -99.8%, fired b_stop INSTANT and signed 9 sells; only the reverts saved it. RIZOWEEN 71056
on 4663 (2026-10-06): Kyber paid $3.67 for a $50 bag five seconds after entry; the operator sold it
by hand 36 minutes later for $10.13.

The rule (Gamma's MED on d219672fb, operator ruling b2ae12c0): a dust CANDIDATE is an exit whose
PRE-slippage quote unit price sits at or under MEMECOIN_EXIT_DUST_FRACTION of the reference (last
good executable price while fresh, else entry). A candidate is HELD unless our own NEWEST market
mark shows the market down too (quote > 0.25 x mark). No own mark: HOLD + page. DexScreener never.
"""
import inspect
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_exit_dust_guard as G

NOW = "2026-10-02T15:12:00+00:00"
MINT = "0xf98a0f657b990b1496b46f8ea76bf7ca561a8888"
SOL_MINT = "So1ana" + "1" * 38
QTY = 2_037_962.7755378648
ENTRY_PRICE = 4.398019486707483e-05          # $89.63 / QTY
DUST_FLOOR_USD = 0.11                        # ~0.00014 WBNB, the signed floor of the 9 sends
DUST_UNIT = 7.75e-08                         # the fastlane_rpc exact_route marks


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_DUST_GUARD", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_DUST_FRACTION", 0.10)
    monkeypatch.setattr(G, "_PAGED", set())


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _daodun(**over):
    return {"id": 68544, "network_id": "56", "mint": MINT, "ticker": "DAODUN",
            "entry_price": ENTRY_PRICE, "entry_notional": 89.63, **over}


def _sol(**over):
    return _daodun(id=70001, network_id="solana", mint=SOL_MINT, ticker="SOLC", **over)


def _mark(c, ts, mid, source, quality="live", liquidity=None, chain="56", mint=MINT):
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, liquidity, source, "
              "quality) VALUES(?,?,?,?,?,?,?)", (chain, mint, ts, mid, liquidity, source, quality))


def _sol_mark(c, ts, mid, source="fastlane_sol_swap", quality="live"):
    _mark(c, ts, mid, source, quality, chain="solana", mint=SOL_MINT)


def _hold(c, pos=None, *, floor=DUST_FLOOR_USD, reason="b_stop", pages=None, payload=None,
          tape=None, qty=QTY):
    return G.hold_reason(c, pos or _daodun(), exit_reason=reason, floor_usd=floor, sell_qty=qty,
                         now_iso=NOW, quote_payload=payload,
                         page_fn=(pages.append if pages is not None else lambda t: None),
                         tape_fn=lambda conn, pos, now: tape)


def _payload(expected_over_min):
    """An EVM quote payload whose pre-slippage output is `expected_over_min` x its signed minimum."""
    return {"buyAmount": str(int(1_000_000 * expected_over_min)), "minBuyAmount": "1000000"}


# ------------------------------------------------------------------ DAODUN / RIZOWEEN: no own mark

def test_daodun_with_only_our_own_exit_quotes_and_a_chart_holds_and_pages_once(conn):
    for i in range(3):                                      # our own exit quotes: not evidence
        _mark(conn, "2026-10-02T15:11:%02d+00:00" % (15 + 2 * i), DUST_UNIT, "fastlane_rpc", "exact_route")
    _mark(conn, "2026-10-02T15:11:50+00:00", 5.53e-05, "dexscreener", liquidity=22_900.0)
    pages = []
    reason = _hold(conn, pages=pages)
    assert reason and "no own market mark" in reason
    assert len(pages) == 1 and "keeps its slot" in pages[0] and "any_price" in pages[0]
    assert _hold(conn, pages=pages)                         # held again, no second page
    assert len(pages) == 1


def test_rizoween_shape_on_4663_holds_when_the_tape_has_no_mark(conn):
    pos = _daodun(id=71056, network_id="4663", entry_price=8.20670768314941e-05,
                  mint="0x7d265a52af142fec10ac3fd256d2c93a2de485cc")
    _mark(conn, "2026-10-02T15:11:55+00:00", 8.23e-05, "dexscreener", chain="4663", mint=pos["mint"])
    qty = 609257.7185692078
    reason = _hold(conn, pos, floor=3.30, qty=qty, payload=_payload(3.67 / 3.30), tape=None)
    assert reason and "no own market mark" in reason


def test_a_4663_tape_mark_that_still_prices_the_coin_holds(conn):
    pos = _daodun(network_id="4663")
    reason = _hold(conn, pos, tape={"price": ENTRY_PRICE * 0.9, "source": "onchain_tape_entry_anchor"})
    assert reason and "onchain_tape_entry_anchor" in reason


def test_a_4663_tape_mark_down_too_lets_the_exit_proceed(conn):
    pos = _daodun(network_id="4663")
    assert _hold(conn, pos, tape={"price": DUST_UNIT * 2, "source": "onchain_tape_entry_anchor"}) is None


# ------------------------------------------------------------------ legit crashes are never held

@pytest.mark.parametrize("drop", [0.86, 0.88, 0.90])
def test_a_legit_86_to_90_pct_crash_is_not_held(conn, drop):
    # Signed floor at a 50% slippage rung would read "dust" under the old floor rule; the rule now
    # reads the PRE-slippage quote, and our own swap marks say the market is down just as far.
    unit = ENTRY_PRICE * (1 - drop)
    floor = unit * QTY * 0.5
    _sol_mark(conn, "2026-10-02T15:09:00+00:00", ENTRY_PRICE)          # pre-crash, older
    _sol_mark(conn, "2026-10-02T15:11:58+00:00", unit * 1.02)          # newest: down too
    assert _hold(conn, _sol(), floor=floor, payload=_payload(2.0)) is None


def test_a_thin_minus_80_high_impact_stop_is_not_held(conn):
    # Whole-bag quote -80% before slippage, signed floor at the 60% rung: 0.08 x entry, under the
    # old 10% line. Pre-slippage it is no candidate at all.
    floor = ENTRY_PRICE * 0.20 * QTY * 0.40
    assert _hold(conn, _sol(), floor=floor, payload=_payload(1 / 0.40)) is None


def test_alive_is_read_from_the_newest_mark_not_any_mark_in_the_window(conn):
    _sol_mark(conn, "2026-10-02T15:08:00+00:00", ENTRY_PRICE)          # alive, 4 min old
    _sol_mark(conn, "2026-10-02T15:11:59+00:00", DUST_UNIT * 1.5)      # newest: collapsed
    assert _hold(conn, _sol()) is None


# ------------------------------------------------------------------ dust routes on a live market

def test_a_dust_route_while_our_own_newest_mark_prices_the_coin_holds(conn):
    _sol_mark(conn, "2026-10-02T15:11:58+00:00", ENTRY_PRICE * 0.9)
    pages = []
    reason = _hold(conn, _sol(), pages=pages)
    assert reason and "fastlane_sol_swap" in reason and len(pages) == 1


@pytest.mark.parametrize("source", ["fastlane_sol_curve", "fastlane_sol_state"])
def test_every_own_solana_pool_read_is_market_evidence(conn, source):
    _sol_mark(conn, "2026-10-02T15:11:58+00:00", ENTRY_PRICE * 0.9, source=source)
    assert source in _hold(conn, _sol())


@pytest.mark.parametrize("why, rows", [
    ("only stale marks", [("2026-10-02T15:06:59+00:00", 1e-09)]),
    ("a future mark", [("2026-10-02T15:13:00+00:00", 1e-09)]),
    ("a quarantined spike", [("2026-10-02T15:11:58+00:00", 1e-09, "fastlane_sol_swap", "quarantined_spike")]),
    ("a zero mid", [("2026-10-02T15:11:58+00:00", 0.0)]),
    ("a negative mid", [("2026-10-02T15:11:58+00:00", -1.0)]),
    ("our exit-anchored post-close tape", [("2026-10-02T15:11:58+00:00", 1e-09,
                                            "onchain_tape_post_close_post_swap_anchor")]),
    ("a chart row", [("2026-10-02T15:11:58+00:00", 1e-09, "dexscreener")]),
])
def test_no_valid_own_mark_holds_and_never_reads_as_a_collapse(conn, why, rows):
    for row in rows:
        _sol_mark(conn, *row)
    reason = _hold(conn, _sol())
    assert reason and "no own market mark" in reason, why


def test_a_malformed_newest_mark_falls_back_to_the_newest_valid_one(conn):
    _sol_mark(conn, "2026-10-02T15:11:50+00:00", DUST_UNIT * 2)        # valid: down too
    _sol_mark(conn, "2026-10-02T15:11:59+00:00", 0.0)                  # newest, absent
    assert _hold(conn, _sol()) is None


# ------------------------------------------------------------------ the candidate and its inputs

def test_pre_slippage_value_comes_from_the_quote_payload_ratio():
    assert G.pre_slippage_usd(1.0, _payload(2.5)) == pytest.approx(2.5)
    assert G.pre_slippage_usd(1.0, {"outAmount": "300", "otherAmountThreshold": "100"}) == pytest.approx(3.0)
    for bad in (None, {}, {"buyAmount": "5"}, {"buyAmount": "50", "minBuyAmount": "0"},
                {"buyAmount": "50", "minBuyAmount": "100"}, {"buyAmount": "x", "minBuyAmount": "1"}):
        assert G.pre_slippage_usd(1.0, bad) == 1.0          # unknown ratio: the signed floor
    assert G.pre_slippage_usd(None, _payload(2.0)) is None


def test_manual_off_and_a_fair_exit_never_hold(conn, monkeypatch):
    assert _hold(conn, reason="manual") is None              # the operator's Sell at any price
    assert _hold(conn, reason="manual_any_price") is None
    assert _hold(conn, floor=60.0) is None                    # a fair exit
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_DUST_GUARD", False)
    assert _hold(conn) is None


def test_a_fair_exit_never_reads_a_mark(conn):
    def boom(*a, **kw):
        raise AssertionError("no market read off the candidate path")
    monkeypatch_free = _daodun(network_id="4663")
    assert G.hold_reason(conn, monkeypatch_free, exit_reason="b_stop", floor_usd=60.0, sell_qty=QTY,
                         now_iso=NOW, page_fn=lambda t: None, tape_fn=boom) is None


def test_the_reference_is_the_last_good_executable_price_while_fresh():
    fresh = _daodun(last_executable_price=6.0e-05, last_executable_at="2026-10-02T15:00:00+00:00")
    stale = _daodun(last_executable_price=6.0e-05, last_executable_at="2026-10-02T13:00:00+00:00")
    future = _daodun(last_executable_price=0.1, last_executable_at="2026-10-03T00:00:00Z")
    assert G.reference_unit_price(fresh, NOW) == 6.0e-05
    assert G.reference_unit_price(stale, NOW) == ENTRY_PRICE
    assert G.reference_unit_price(future, NOW) == ENTRY_PRICE


@pytest.mark.parametrize("last", [float("inf"), float("nan")])
def test_nonfinite_reference_falls_back_to_entry(last):
    assert G.reference_unit_price(_daodun(last_executable_price=last, last_executable_at=NOW), NOW) == ENTRY_PRICE


@pytest.mark.parametrize("floor", [float("nan"), float("inf"), -1])
def test_invalid_floor_is_unknown_not_permission_to_sell(conn, floor):
    assert _hold(conn, floor=floor).startswith("dust_route_hold:")


def test_guard_failure_after_dust_detection_holds(conn, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("bad evidence")
    monkeypatch.setattr(G, "newest_own_mark", broken)
    assert _hold(conn).startswith("dust_route_hold:")


# ------------------------------------------------------------------ the sweep's half

def test_sweep_dust_route_reads_the_same_rule(conn):
    assert G.sweep_dust_route(_daodun(), DUST_UNIT, NOW) is True          # no conn: candidate only
    assert G.sweep_dust_route(_daodun(), 0.0, NOW) is True
    assert G.sweep_dust_route(_daodun(), 3.9e-05, NOW, conn) is False     # -11%: no candidate
    assert G.sweep_dust_route(_sol(), DUST_UNIT, NOW, conn) is True       # no own mark
    _sol_mark(conn, "2026-10-02T15:11:58+00:00", DUST_UNIT * 1.5)
    assert G.sweep_dust_route(_sol(), DUST_UNIT, NOW, conn) is False      # market down too


def test_the_sweep_never_sells_a_dust_route_on_its_first_read_and_keeps_the_last_good_price(monkeypatch):
    # DAODUN: "b_stop INSTANT at -99.8% ... first read sells, confirmation skipped" on a dust quote.
    from test_memecoin_stop_instant_grace import _c, _open_real, _rails, _sweep
    from sniper.settings import memecoin as cfg
    _rails(monkeypatch)
    c = _c()
    pid = _open_real(c, opened_at="2026-09-04T00:00:00+00:00")
    exits = []
    _sweep(c, 0.002, "2026-09-04T00:00:31+00:00", exits)   # dust: -99.8%, past the grace window
    assert exits == []                                      # no INSTANT on a dust route
    assert m.get_position(c, pid)["last_executable_price"] is None   # a dust read is not "good"
    _sweep(c, 0.002, "2026-09-04T00:00:33+00:00", exits)   # the normal confirmation's second read
    assert exits == ["b_stop"]                              # the executor's guard decides from here
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_DUST_GUARD", False)
    c2 = _c()
    _open_real(c2, opened_at="2026-09-04T00:00:00+00:00")
    off = []
    _sweep(c2, 0.002, "2026-09-04T00:00:31+00:00", off)
    assert off == ["b_stop"]                                # guard off: INSTANT as today


def test_the_sweep_sells_a_real_collapse_on_its_first_read(monkeypatch):
    # The market is down too (our own swap mark): not a dust route, the INSTANT stop stands.
    from test_memecoin_stop_instant_grace import _c, _open_real, _rails, _sweep
    _rails(monkeypatch)
    c = _c()
    pid = _open_real(c, opened_at="2026-09-04T00:00:00+00:00")
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality) "
              "VALUES('solana','GRACEQ','2026-09-04T00:00:30+00:00',0.0025,'fastlane_sol_swap','live')")
    exits = []
    _sweep(c, 0.002, "2026-09-04T00:00:31+00:00", exits)
    assert exits == ["b_stop"]
    assert m.get_position(c, pid)["last_executable_price"] == pytest.approx(0.002)


# ------------------------------------------------------------------ the executor

def test_the_evm_executor_holds_signs_nothing_and_releases_the_claim(conn):
    from sniper.execution.evm_exit_executor import execute_exit_evm
    pid = m.open_position(conn, source_user_id="u", source_handle="h", mint=MINT, ticker="DAODUN",
                          entry_price=ENTRY_PRICE, entry_notional=89.63, entry_fill_source="chain",
                          entry_lag_s=0.0, their_size_usd=0.0, opened_at="2026-10-02T14:52:27Z",
                          strategy="alpha_b", execution_mode="real", network_id="56")
    m.insert_fill(conn, position_id=pid, kind="entry", price=ENTRY_PRICE, qty=QTY, notional=89.63,
                  fill_source="chain", fraction=1.0, created_at="2026-10-02T14:52:27Z", tx_sig="E")
    signed, pages = [], []
    quote = {"sellAmount": "2037962775537864660227353", "buyAmount": "200000000000000",
             "minBuyAmount": "140000000000000", "transaction": {"to": "0x" + "6" * 40},
             "issues": {"allowance": {"spender": "0x" + "6" * 40}}}

    def dust_hold(intent, quote_payload, pos, sell_qty):
        return G.hold_reason(conn, pos, exit_reason="b_stop", floor_usd=DUST_FLOOR_USD,
                             sell_qty=sell_qty, now_iso=NOW, quote_payload=quote_payload,
                             page_fn=pages.append, tape_fn=lambda *a: None)
    result = execute_exit_evm(
        conn, m.get_position(conn, pid), "b_stop", NOW, quote_fn=lambda mint, toks, slip: quote,
        approve_fn=lambda *a: True, sign_fn=lambda tx: signed.append(tx) or None,
        send_fn=lambda raw: None, confirm_fn=lambda h: None, usdc_fn=lambda: 0.0,
        token_balance_fn=lambda mint: QTY, dust_hold_fn=dust_hold)
    assert result["status"] == "dust_route_hold" and "no own market mark" in result["reason"]
    assert signed == [] and len(pages) == 1
    pos = m.get_position(conn, pid)
    assert pos["is_open"] and pos["close_claim_id"] is None and not pos["stuck_bag"]


# ------------------------------------------------------------------ leave-behind: no DexScreener

def test_the_guard_never_reads_dexscreener_again(conn, monkeypatch):
    """Operator ruling b2ae12c0 (2026-10-06): the guard's market evidence is our own pool state only.
    Fails if any DexScreener read, worker or stored chart row comes back into the guard."""
    src = inspect.getsource(G).lower()
    assert "dexscreener" not in src and "dex_screener" not in src
    from sniper.execution import solana_quote
    def no_ds(*a, **kw):
        raise AssertionError("the dust guard read DexScreener")
    monkeypatch.setattr(solana_quote, "dexscreener_stats_batch_keyed", no_ds)
    _mark(conn, "2026-10-02T15:11:58+00:00", 1e-09, "dexscreener", liquidity=1.0)   # a dead chart
    reason = G.hold_reason(conn, _daodun(), exit_reason="b_stop", floor_usd=DUST_FLOOR_USD,
                           sell_qty=QTY, now_iso=NOW, page_fn=lambda t: None)
    assert reason and "no own market mark" in reason        # the chart decides nothing


def test_other_series_and_future_rows_never_crowd_out_our_own_newest_mark(conn):
    _sol_mark(conn, "2026-10-02T15:10:00+00:00", DUST_UNIT * 2)             # own, down too
    for i in range(250):                                                    # newer, not ours
        _mark(conn, "2026-10-02T15:10:%02d.%06d+00:00" % (1 + i // 10, i), ENTRY_PRICE,
              "dexscreener", chain="solana", mint=SOL_MINT)
        _mark(conn, "2026-10-03T00:00:%02d.%06d+00:00" % (i // 10, i), ENTRY_PRICE,
              "fastlane_sol_swap", chain="solana", mint=SOL_MINT)          # future rows
    assert _hold(conn, _sol()) is None


def test_a_caller_held_pre_slippage_value_replaces_the_payload_ratio(conn):
    # The flap.sh quote exit passes its own expected USD: a -80% quote signed at a deep floor is no
    # candidate; an expected value under the floor is unusable and the floor stands.
    floor = ENTRY_PRICE * 0.20 * QTY * 0.40
    assert G.hold_reason(conn, _sol(), exit_reason="b_stop", floor_usd=floor, sell_qty=QTY, now_iso=NOW,
                         expected_usd=ENTRY_PRICE * 0.20 * QTY, page_fn=lambda t: None) is None
    assert G.hold_reason(conn, _sol(), exit_reason="b_stop", floor_usd=floor, sell_qty=QTY, now_iso=NOW,
                         expected_usd=floor / 2, page_fn=lambda t: None)       # floor stands: held
