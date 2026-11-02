"""Operator mcap targets read FDV (price x pinned total supply), not the provider's circulating cap.

Hogdog 64977: entry_price 4.4111e-05, provider market_cap 35,307.79 (~800M circulating) against a
1B total supply, so the FDV GMGN shows is ~44.1K and every target set from GMGN sat 20% off.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr

PX = 4.4111360808459964e-05
CIRC = 35307.78886348997
FDV = PX * 1_000_000_000          # 44,111.36
NOW = "2026-09-25T12:00:00+00:00"


class _Cfg:
    MEMECOIN_AUTO_TEMPLATES = "off"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_positions(id, mint, network_id, execution_mode, is_open, "
              "entry_price, entry_mcap, opened_at) VALUES(64977,'HOG','solana','real',1,?,?,?)",
              (PX, CIRC, NOW))
    return c


def _pos(conn):
    return dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=64977").fetchone())


def _snapshot(**over):
    st = {"price": PX, "market_cap": CIRC, "fdv": FDV}
    st.update(over)
    return st


def _decide(conn, st, override):
    pos = _pos(conn)
    _ov, mcap = rr._resolve_override_and_mcap(conn, pos, st, st.get("price"), {pos["id"]: None},
                                              _Cfg())
    reason = rr.alpha_b_real_exit_reason(pos, PX, NOW, override=override, current_mcap=mcap)
    return reason, mcap, pos.get("_mcap_basis")


def test_the_rail_reads_fdv_so_a_gmgn_stop_between_the_two_caps_does_not_fire():
    conn = _conn()
    pos = _pos(conn)
    assert rr.pin_fdv_supply(conn, pos, _snapshot()) is not None
    reason, mcap, basis = _decide(conn, _snapshot(), {"sl_target_mcap": 40_000.0})
    assert abs(mcap - FDV) < 1e-6 and basis == "fdv"
    assert reason != "b_stop"          # 44.1K FDV is above the 40K floor; circulating 35.3K is not


def test_hogdog_stop_is_read_on_fdv_not_the_circulating_provider_cap():
    """Uses only functions that exist before the change: on the old code this reads 35.3K and
    stops; on the new code the pinned supply makes it read 44.1K and hold."""
    conn = _conn()
    pos = dict(_pos(conn), fdv_supply=1_000_000_000.0)
    _ov, mcap = rr._resolve_override_and_mcap(conn, pos, _snapshot(), PX, {pos["id"]: None},
                                              _Cfg())
    assert abs(mcap - FDV) < 1e-6, mcap
    assert rr.alpha_b_real_exit_reason(pos, PX, NOW, override={"sl_target_mcap": 40_000.0},
                                       current_mcap=mcap) != "b_stop"


def test_the_mirror_take_profit_fires_on_fdv():
    conn = _conn()
    rr.pin_fdv_supply(conn, _pos(conn), _snapshot())
    reason, _mcap, _basis = _decide(conn, _snapshot(), {"tp_target_mcap": 40_000.0})
    assert reason == "real_tp"


def test_no_basis_flip_when_a_later_tick_lacks_fdv():
    conn = _conn()
    rr.pin_fdv_supply(conn, _pos(conn), _snapshot())
    # fdv missing on this tick, circulating still present: the target must stay on FDV.
    reason, mcap, basis = _decide(conn, _snapshot(fdv=None), {"sl_target_mcap": 40_000.0})
    assert abs(mcap - FDV) < 1e-6 and basis == "fdv" and reason != "b_stop"


def test_a_tick_without_a_price_uses_the_newest_mark_price_times_the_same_supply():
    conn = _conn()
    rr.pin_fdv_supply(conn, _pos(conn), _snapshot())
    conn.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, mcap, source)"
                 " VALUES('solana','HOG','2026-09-25T11:59:00+00:00', ?, ?, 'dexscreener')",
                 (PX * 2, CIRC * 2))
    pos = _pos(conn)
    _ov, mcap = rr._resolve_override_and_mcap(conn, pos, {}, None, {pos["id"]: None}, _Cfg())
    assert abs(mcap - FDV * 2) < 1e-6 and pos["_mcap_basis"] == "fdv_mark"


def test_unpinned_positions_keep_todays_basis():
    conn = _conn()
    pos = _pos(conn)
    _ov, mcap = rr._resolve_override_and_mcap(conn, pos, _snapshot(), PX, {pos["id"]: None}, _Cfg())
    assert mcap == CIRC and pos["_mcap_basis"] == "provider_mcap"


def test_the_pin_is_once_and_refuses_what_it_cannot_trust(monkeypatch):
    conn = _conn()
    assert rr.fdv_supply_from_snapshot({"price": PX, "fdv": None}) is None       # absent
    assert rr.fdv_supply_from_snapshot({"price": PX, "fdv": 0}) is None          # zero
    assert rr.fdv_supply_from_snapshot({"price": 0, "fdv": FDV}) is None
    assert rr.fdv_supply_from_snapshot(_snapshot(fdv=CIRC * 0.9)) is None        # total < circ
    first = rr.pin_fdv_supply(conn, _pos(conn), _snapshot())
    assert abs(first - 1e9) < 1
    # a later, different snapshot never moves it
    again = rr.pin_fdv_supply(conn, _pos(conn), _snapshot(fdv=FDV * 3))
    assert again == first
    assert conn.execute("SELECT fdv_supply FROM memecoin_positions").fetchone()[0] == first
    # chains outside MEMECOIN_REAL_MCAP_FDV_CHAINS keep today's basis
    conn.execute("UPDATE memecoin_positions SET fdv_supply=NULL, network_id='4663'")
    assert rr.pin_fdv_supply(conn, _pos(conn), _snapshot()) is None


@pytest.mark.parametrize("swap_price", [None, PX * 2])
def test_fastlane_late_pin_preserves_provider_ratio(monkeypatch, swap_price):
    from sniper.services import memecoin_fastlane as fast
    conn = _conn()
    conn.execute("UPDATE memecoin_positions SET strategy=?", (rr.cfg.MEMECOIN_REAL_POLICY,))
    conn.commit()
    class Cache:
        def quote_mints(self): return set()
        def fresh_price_map(self, *a, **kw):
            return {("solana", "HOG"): {"price": swap_price, "source": "solana_swap"}}
    marks = fast.refresh_open_positions(conn, {}, mid_batch_fn=lambda keys: {
        ("solana", "HOG"): _snapshot()}, swap_mark_cache=Cache() if swap_price else None)
    assert marks[("solana", "HOG")]["price"] == (swap_price or PX)
    rr.real_exit_sweep(conn, {}, price_fn=lambda mints: marks, now_fn=lambda: NOW)
    assert _pos(conn)["fdv_supply"] == pytest.approx(1e9)
    rr.real_exit_sweep(conn, {}, price_fn=lambda mints: {
        ("solana", "HOG"): _snapshot(fdv=FDV * 3)}, now_fn=lambda: NOW)
    assert _pos(conn)["fdv_supply"] == pytest.approx(1e9)


@pytest.mark.parametrize("fdv", [None, 0, float("nan"), float("inf")])
def test_fastlane_invalid_fdv_keeps_supply_unknown(fdv):
    from sniper.services.memecoin_fastlane import _batched_mid_snapshot
    marks, _ = _batched_mid_snapshot([{"mint": "HOG", "network_id": "solana"}],
        batch_fn=lambda keys: {("solana", "HOG"): _snapshot(fdv=fdv)})
    conn = _conn()
    assert rr.pin_fdv_supply(conn, _pos(conn), marks[("solana", "HOG")]) is None
    assert _pos(conn)["fdv_supply"] is None


@pytest.mark.parametrize("snapshot", [None, [], "bad", 1])
def test_malformed_nested_fdv_snapshot_stays_unknown(snapshot):
    assert rr.fdv_supply_from_snapshot({"fdv_snapshot": snapshot}) is None


# --- the entry pin falls back to the entry's own mint read (Beta R2, 2026-09-29) ------------------
# With X1 off on Solana the sweep's own-mark rows carry no fdv, so the sweep never pins. A position
# whose entry snapshot had no fdv would stay on today's circulating basis for life.

def _mint_read(supply, decimals=6, failure=None):
    return ({"data": {"parsed": {"info": {"supply": supply, "decimals": decimals}}}}, failure)


@pytest.mark.parametrize("read, want", [
    (_mint_read("1000000000000000", 6), 1e9),                  # a pump coin, 6 decimals
    (_mint_read("950000000000000000", 9), 9.5e8),              # 9 decimals, part burned
    (None, None),                                              # absent: no read kept on the event
    (_mint_read("1000000000000000", 6, failure="rpc"), None),  # a failed read
    (({"data": {}}, None), None),                              # malformed
    (("not a dict", None), None),
    (_mint_read("0", 6), None),                                # zero supply is not a supply
    (_mint_read("-5", 6), None),
    (_mint_read("1000", True), None),                          # bool decimals
    (_mint_read("1000", 19), None),                            # out of range
    (_mint_read("1000", "6"), None),                           # decimals must be an int
])
def test_the_mint_read_supply_is_whole_tokens_and_refuses_what_it_cannot_trust(read, want):
    got = rr.fdv_supply_from_mint_read(read)
    assert got == (pytest.approx(want) if want is not None else None)


def test_the_entry_pin_takes_dexscreener_first(caplog):
    conn = _conn()
    with caplog.at_level("WARNING"):
        got = rr.pin_entry_fdv_supply(conn, _pos(conn), _snapshot(), _mint_read("900000000000000"))
    assert got == pytest.approx(1e9) and _pos(conn)["fdv_supply"] == pytest.approx(1e9)
    line = [r.getMessage() for r in caplog.records if "fdv pin sources" in r.getMessage()]
    assert len(line) == 1 and "pos=64977" in line[0] and "mint_read=ok" in line[0], line
    assert "dex_supply=1000000000 " in line[0] and "mint_supply=900000000 " in line[0], line
    assert "ratio=1.111111" in line[0], line
    import re
    assert re.search(r" at=20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00 ", line[0]), line


def test_the_entry_pin_falls_back_to_the_mint_read_when_dexscreener_has_no_fdv(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:solana")
    conn = _conn()
    got = rr.pin_entry_fdv_supply(conn, _pos(conn), _snapshot(fdv=None), _mint_read("950000000000000"))
    assert got == pytest.approx(9.5e8) and _pos(conn)["fdv_supply"] == pytest.approx(9.5e8)
    # once pinned, a later snapshot never moves it (the pin's own contract)
    assert rr.pin_fdv_supply(conn, _pos(conn), _snapshot()) == pytest.approx(9.5e8)


@pytest.mark.parametrize("mint_supply,dex_supply", [
    (950_310_144.2, 999_076_496.8446976),  # ROUTER 70188, entry mint-read log
    (958_389_217.0, 1_000_468_920.3925844),  # Instinct 70185
])
def test_onchain_entry_supply_beats_stale_fdv_in_ui_and_custom_tp(
        monkeypatch, mint_supply, dex_supply):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:solana")
    conn = _conn()
    stale = _snapshot(fdv=PX * dex_supply)
    rr.pin_entry_fdv_supply(conn, _pos(conn), stale,
                            _mint_read(str(round(mint_supply * 10**6))))
    pos = _pos(conn)
    assert pos["fdv_supply"] == pytest.approx(mint_supply)
    # The card and operator target use the same supply. A target between the
    # real and stale caps must not sell early while GMGN is still below it.
    from sniper.routes import api_memecoin as api
    row = api._real_ticker_payload(conn, NOW, positions=[pos])["positions"][0]
    assert row["cap_supply"] == pytest.approx(mint_supply)
    assert row["target_supply"] == pytest.approx(mint_supply)
    target = PX * (mint_supply + dex_supply) / 2
    reason, mcap, basis = _decide(conn, stale, {"tp_pct": 0, "tp_target_mcap": target})
    assert mcap == pytest.approx(PX * mint_supply) and basis == "fdv"
    assert reason != "real_tp"
    assert rr.alpha_b_real_exit_reason(pos, target / mint_supply, NOW,
        override={"tp_pct": 0, "tp_target_mcap": target}, current_mcap=target) == "real_tp"


@pytest.mark.parametrize("read", [None, _mint_read("0"),
                                  _mint_read("950000000000000", failure="rpc_timeout")])
def test_onchain_entry_unavailable_supply_preserves_fdv_fallback(monkeypatch, read):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:solana")
    conn = _conn()
    assert rr.pin_entry_fdv_supply(conn, _pos(conn), _snapshot(), read) == pytest.approx(1e9)


def test_onchain_entry_does_not_rewrite_an_existing_pin(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:solana")
    conn = _conn()
    rr.pin_fdv_supply(conn, _pos(conn), _snapshot())
    assert rr.pin_entry_fdv_supply(conn, _pos(conn), _snapshot(),
        _mint_read("950000000000000")) == pytest.approx(1e9)


def test_with_x1_on_the_entry_keeps_todays_pin_and_only_logs_the_mint_supply(monkeypatch, caplog):
    """Alpha C1: while X1 prices Solana the sweep still pins later from DexScreener's fdv/price, so
    the entry must not write a once-only mint pin first. The line still logs both supplies."""
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:4663,X1:56")
    conn = _conn()
    with caplog.at_level("WARNING"):
        got = rr.pin_entry_fdv_supply(conn, _pos(conn), _snapshot(fdv=None), _mint_read("950000000000000"))
    assert got is None and _pos(conn)["fdv_supply"] is None
    line = [r.getMessage() for r in caplog.records if "fdv pin sources" in r.getMessage()]
    assert len(line) == 1 and "mint_supply=950000000 " in line[0] and "mint_read=ok" in line[0], line
    # DexScreener's fdv still pins at entry, as today
    assert rr.pin_entry_fdv_supply(conn, _pos(conn), _snapshot(), _mint_read("950000000000000")) \
        == pytest.approx(1e9)


def test_no_mint_read_and_no_fdv_leaves_todays_basis(caplog):
    conn = _conn()
    with caplog.at_level("WARNING"):
        assert rr.pin_entry_fdv_supply(conn, _pos(conn), _snapshot(fdv=None), None) is None
    assert _pos(conn)["fdv_supply"] is None
    line = [r.getMessage() for r in caplog.records if "fdv pin sources" in r.getMessage()]
    assert len(line) == 1 and "mint_read=absent" in line[0] and "dex_supply=None" in line[0], line


def test_the_entry_pin_is_solana_only(monkeypatch, caplog):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1")
    conn = _conn()
    conn.execute("UPDATE memecoin_positions SET network_id='4663'")
    with caplog.at_level("WARNING"):
        got = rr.pin_entry_fdv_supply(conn, _pos(conn), _snapshot(fdv=None), _mint_read("950000000000000"))
    assert got is None and _pos(conn)["fdv_supply"] is None
    assert not [r for r in caplog.records if "fdv pin sources" in r.getMessage()]


def test_the_entry_hook_pins_from_the_events_mint_read(monkeypatch, tmp_path):
    """The hook hands `ev.mint_account_read` to the pin after the fill; DexScreener has no fdv."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from types import SimpleNamespace
    from test_memecoin_entry_identity import NOW, FakeClock, _arm, _event, _solana_context
    monkeypatch.setattr(rr.cfg, "MEMECOIN_ENTRY_EARLY_QUOTE", False, raising=False)
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "X1:solana")
    _arm(monkeypatch)
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_positions(id, mint, network_id, execution_mode, is_open, "
                 "entry_price, opened_at) VALUES(7,'So1Mint','solana','real',1,0.25,?)", (NOW,))
    conn.commit()
    monkeypatch.setattr(rr, "real_entry", lambda conn, ev, **kw: {"status": "filled", "position_id": 7})
    monkeypatch.setattr(rr, "_alert_entry_result", lambda *a, **k: None)
    monkeypatch.setattr(rr, "chase_impact_allowance", lambda *a, **k: (None, "test"))
    # Gamma T1: trace.finish refuses a caller-owned transaction (the 09-25 trace-loss trap), so the
    # pin's write must be committed before it runs.
    open_at_finish = []
    trace = SimpleNamespace(attempt_id="attempt-r2", mark=lambda *a, **k: True,
                            finish=lambda *a, **k: open_at_finish.append(conn.in_transaction) or True,
                            mark_substage=lambda *a, **k: True)
    ev = _event(network_id="solana", mint="So1Mint", market_cap=100_000.0)   # their_price 0.25
    ev.mint_account_read = _mint_read("970000000000000")
    rr.real_entry_hook(conn, ev, {"solana": _solana_context({})}, now_fn=lambda: NOW,
                       trade_size=10.0, max_age_s=120,
                       stats_fn=lambda mint, network_id=None: {"price": 0.25, "market_cap": 100_000.0},
                       trace=trace, trace_clock=FakeClock())
    assert open_at_finish and not any(open_at_finish), open_at_finish
    assert not conn.in_transaction
    got = conn.execute("SELECT fdv_supply FROM memecoin_positions WHERE id=7").fetchone()[0]
    assert got == pytest.approx(9.7e8)
