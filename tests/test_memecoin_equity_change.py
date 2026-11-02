"""The equity change breakdown (operator 2026-10-08, spec 2026-10-08-memecoin-equity-change-breakdown-
design.md): the runner records total equity once a minute and splits a window's change into Trading,
native price drift and Other. Other is always shown and never clamped; a missing part is unknown,
never $0. Display only: no funded seam, no lock the fill path takes, the ledger read-only."""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_equity_change as eq
from sniper.services import memecoin_native_float as nf

T0 = 1_791_500_000.0          # 2026-10-08 22:53:20Z
NETS = ["solana", "1", "4663", "5042", "56", "8453"]
PRICES = {"ETHUSDT": 2000.0, "BNBUSDT": 600.0, "SOLUSDT": 150.0}
# (native, wrapped) per chain; Solana's wrapped is never read
UNITS = {"4663": (0.03, 0.06), "8453": (0.02, 0.03), "1": (0.04, 0.0), "56": (0.1, 0.2), "solana": (2.0, None)}


@pytest.fixture(autouse=True)
def _clean_recorder():
    nf._LAST.clear()
    yield
    nf._LAST.clear()


def _binance(prices):
    def get(url, timeout):
        return [{"symbol": s, "price": str(p)} for s, p in prices.items()]
    return get


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _record(now, units=UNITS, usdc=500.0):
    for chain, (native, wrapped) in units.items():
        nf.remember(chain, "native", native, now=now)
        if wrapped is not None:
            nf.remember(chain, "wrapped", wrapped, now=now)
    if usdc is not None:
        nf.remember("solana", "quote", usdc, now=now)


def _nf_sample(tmp_path, now, store=None, prices=PRICES):
    store = store or nf.Store(str(tmp_path / "native_float.db"))
    snap = nf.sample_once(store, now=now, get=_binance(prices), post_for=lambda c: None,
                          evm_address=None, json_path=str(tmp_path / "native_float.json"), public_due=False)
    return store, snap


def _ledger():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, *, mint="CoinA", network_id="solana", notional=50.0, price=0.02, mid=None, mark_at=T0, paused=False):
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="T",
                          entry_price=price, entry_notional=notional, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-10-08T00:00:00+00:00",
                          strategy="alpha_b", execution_mode="real", network_id=network_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=price, qty=notional / price, notional=notional,
                  fill_source="chain", fraction=1.0, created_at="2026-10-08T00:00:00+00:00", tx_sig="E%s" % mint)
    if mid is not None:
        c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source) "
                  "VALUES (?,?,?,?,'fastlane_sol_swap')",
                  (network_id, mint, _iso(mark_at), mid))
    if paused:
        c.execute("UPDATE memecoin_positions SET tracking_paused_at=? WHERE id=?", (_iso(T0 - 7200), pid))
    c.commit()
    return pid


def _close(c, pid, *, at, pnl):
    c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=?, realized_pnl=? WHERE id=?", (_iso(at), pnl, pid))
    c.commit()


def _recorder(tmp_path, c, *, arc=100.0, nets=NETS, clock=None):
    reads = []

    def post_for(chain):
        def post(body):
            reads.append(chain)
            if arc is None:
                raise OSError("arc down")
            return [{"id": 1, "result": hex(5042)},
                    {"id": 2, "result": "0x" + format(int(arc * 10 ** 6), "064x")}]
        return post if chain == "5042" else None
    rec = eq.EquityRecorder(state_path_fn=lambda *p: str(tmp_path / p[-1]), evm_address="0x" + "11" * 20,
                            post_for=post_for, connect=lambda: c, networks_fn=lambda: list(nets))
    return rec, reads


def _step(tmp_path, rec, now, nf_store=None, prices=PRICES, **record):
    _record(now, **record)
    nf_store, snap = _nf_sample(tmp_path, now, nf_store, prices=prices)
    out = rec.sample(nf_store, snap, now)
    return nf_store, out


# ------------------------------------------------------------------ the sample: the hero's formula

def test_a_full_sample_is_the_heros_equity(tmp_path):
    c = _ledger()
    rec, _ = _recorder(tmp_path, c)
    _, out = _step(tmp_path, rec, T0)
    s = out["now"]
    by = s["by_network"]
    assert by["4663"]["cash_usd"] == pytest.approx(120.0) and by["4663"]["native_usd"] == pytest.approx(60.0)
    assert by["1"]["equity_usd"] == pytest.approx(80.0) and by["56"]["equity_usd"] == pytest.approx(180.0)
    assert by["solana"]["cash_usd"] == 500.0 and by["solana"]["native_usd"] == pytest.approx(300.0)
    assert by["5042"]["cash_usd"] == pytest.approx(100.0) and by["5042"]["native_usd"] == 0.0
    assert s["equity_usd"] == pytest.approx(180 + 100 + 80 + 180 + 800 + 100) and s["missing"] == []


def test_open_positions_count_at_their_stored_mark_and_reserved_cash_once(tmp_path):
    c = _ledger()
    _open(c, mid=0.03)                                     # 2,500 coins: cost 50, mark 75
    rec, _ = _recorder(tmp_path, c)
    _, out = _step(tmp_path, rec, T0)
    sol = out["now"]["by_network"]["solana"]
    assert sol["open_value_usd"] == pytest.approx(75.0) and sol["unrealized_usd"] == pytest.approx(25.0)
    assert out["now"]["unrealized_usd"] == pytest.approx(25.0)


# Gamma REFUSE on d514d4f51: real_accounting keeps an unmarked position at cost (unquotable_n), and the
# stored mid had no age bound. A position without a fresh mark is unknown, never its cost.

def test_an_open_position_without_a_mark_makes_its_network_unknown(tmp_path):
    c = _ledger()
    _open(c, notional=100.0)                                 # no stored mid: unquotable
    rec, _ = _recorder(tmp_path, c)
    _, out = _step(tmp_path, rec, T0)
    sol = out["now"]["by_network"]["solana"]
    assert out["now"]["equity_usd"] is None and "solana" in out["now"]["missing"]
    assert sol["missing"] == "marks" and sol["equity_usd"] is None and sol["open_value_usd"] is None
    assert out["now"]["unrealized_usd"] is None


def test_a_stored_mark_older_than_the_bound_is_no_mark(tmp_path):
    c = _ledger()
    _open(c, mid=0.03, mark_at=T0 - eq.MARK_MAX_AGE_S - 60)
    rec, _ = _recorder(tmp_path, c)
    _, out = _step(tmp_path, rec, T0)
    assert out["now"]["by_network"]["solana"]["missing"] == "marks" and out["now"]["equity_usd"] is None


@pytest.mark.parametrize("fresh", [True, False])
def test_a_paused_position_is_valued_at_its_fresh_mark_or_unknown_never_cost(tmp_path, fresh):
    c = _ledger()
    _open(c, mid=0.03, mark_at=T0 - 30 if fresh else T0 - 3600, paused=True)
    rec, _ = _recorder(tmp_path, c)
    _, out = _step(tmp_path, rec, T0)
    sol = out["now"]["by_network"]["solana"]
    if fresh:
        assert sol["open_value_usd"] == pytest.approx(75.0)
    else:
        assert sol["open_value_usd"] is None and out["now"]["equity_usd"] is None


def test_an_open_position_on_a_chain_outside_the_list_is_named(tmp_path):
    c = _ledger()
    _open(c, network_id="57073", mint="0x" + "ab" * 20, mid=0.03)
    rec, _ = _recorder(tmp_path, c)
    _, out = _step(tmp_path, rec, T0)
    assert "57073" in out["now"]["missing"] and out["now"]["equity_usd"] is None


def test_trading_is_unknown_when_an_end_had_an_unmarked_position(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0, "24h": T0})
    c = _ledger()
    pid = _open(c, notional=100.0)                         # unmarked at the start
    rec, _ = _recorder(tmp_path, c)
    store, _ = _step(tmp_path, rec, T0)
    _close(c, pid, at=T0 + 100, pnl=-40.0)
    _, out = _step(tmp_path, rec, T0 + 600, store)
    w = out["windows"]["24h"]
    assert w["trading"]["realized"] == pytest.approx(-40.0) and w["trading"]["usd"] is None
    assert w["other"]["usd"] is None and w["status"] == "unknown_at_start"


def test_an_unknown_arc_balance_makes_the_sample_unknown_never_zero(tmp_path):
    c = _ledger()
    rec, _ = _recorder(tmp_path, c, arc=None)
    _, out = _step(tmp_path, rec, T0)
    assert out["now"]["equity_usd"] is None and "5042" in out["now"]["missing"]
    assert out["now"]["by_network"]["5042"]["equity_usd"] is None
    assert out["now"]["by_network"]["solana"]["equity_usd"] == pytest.approx(800.0)   # the rest still shown


def test_a_measured_zero_balance_is_zero_and_known(tmp_path):
    c = _ledger()
    rec, _ = _recorder(tmp_path, c, arc=0.0)
    _, out = _step(tmp_path, rec, T0)
    assert out["now"]["by_network"]["5042"]["equity_usd"] == 0.0 and out["now"]["equity_usd"] is not None


@pytest.mark.parametrize("gap", ["price", "solana_usdc", "book", "chain"])
def test_any_missing_part_makes_the_sample_unknown(tmp_path, gap):
    c = _ledger()
    rec, _ = _recorder(tmp_path, c)
    if gap == "book":
        rec._connect = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
    units = dict(UNITS)
    if gap == "chain":
        units.pop("56")
    prices = {k: v for k, v in PRICES.items() if not (gap == "price" and k == "BNBUSDT")}
    _, out = _step(tmp_path, rec, T0, prices=prices, units=units, usdc=None if gap == "solana_usdc" else 500.0)
    assert out["now"]["equity_usd"] is None and out["now"]["missing"]


def test_an_arc_read_happens_every_five_minutes_and_carries_until_its_age_limit(tmp_path):
    c = _ledger()
    rec, reads = _recorder(tmp_path, c)
    store = None
    for i in range(6):
        store, _ = _step(tmp_path, rec, T0 + 60 * i, store)
    assert reads == ["5042", "5042"]                       # T0 and T0+300
    rec._post_for = lambda chain: None                     # the endpoint goes away
    store, out = _step(tmp_path, rec, T0 + 1200, store)
    assert out["now"]["by_network"]["5042"]["equity_usd"] == pytest.approx(100.0)   # read 900 s ago
    store, out = _step(tmp_path, rec, T0 + 1260, store)
    assert out["now"]["by_network"]["5042"]["equity_usd"] is None                  # 960 s: unknown


# ------------------------------------------------------------------ windows

def _two_samples(tmp_path, *, start=T0, end=T0 + 600, closes=(), arc_end=100.0, end_prices=PRICES):
    c = _ledger()
    rec, _ = _recorder(tmp_path, c)
    store, _ = _step(tmp_path, rec, start)
    for at, pnl in closes:
        _close(c, _open(c, mint="C%d" % int(at)), at=at, pnl=pnl)
    if arc_end is None:
        rec._post_for = lambda chain: None
    return _step(tmp_path, rec, end, store, prices=end_prices)[1]


def test_other_is_the_change_less_trading_and_drift_with_its_sign(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0, "24h": T0})
    out = _two_samples(tmp_path, end_prices={"ETHUSDT": 1900.0, "BNBUSDT": 600.0, "SOLUSDT": 150.0},
                       closes=[(T0 + 100, 12.5)])
    w = out["windows"]["24h"]
    assert w["status"] == "ok" and w["start_at"] == T0 and w["end_at"] == T0 + 600
    eth = sum(n + (wr or 0) for c, (n, wr) in UNITS.items() if c in ("4663", "8453", "1"))
    assert w["drift"]["usd"] == pytest.approx(eth * -100.0)
    assert w["trading"]["realized"] == pytest.approx(12.5) and w["trading"]["closes"] == 1
    assert w["other"]["usd"] == pytest.approx(w["change"] - w["trading"]["usd"] - w["drift"]["usd"])
    # The close's proceeds never reached a recorded balance: Other carries the -12.5, unclamped.
    assert w["other"]["usd"] == pytest.approx(-12.5)


def test_a_quiet_window_has_other_near_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0, "24h": T0})
    out = _two_samples(tmp_path, end_prices={"ETHUSDT": 1950.0, "BNBUSDT": 610.0, "SOLUSDT": 140.0})
    w = out["windows"]["today"]
    assert w["change"] == pytest.approx(w["drift"]["usd"]) and abs(w["other"]["usd"]) < 1e-6


def test_an_unpriced_close_keeps_equity_measured_and_trading_and_other_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0, "24h": T0})
    out = _two_samples(tmp_path, closes=[(T0 + 100, None), (T0 + 200, 12.5)])
    w = out["windows"]["today"]
    assert w["status"] == "ok" and w["equity_now"] is not None and w["change"] == 0.0
    assert w["trading"]["closes"] == 2 and w["trading"]["realized"] is None
    assert w["trading"]["unrealized_change"] == 0.0 and w["trading"]["usd"] is None
    assert w["other"]["usd"] is None and w["other"]["why_unknown"] == "trading unknown"


def test_a_close_before_the_start_sample_is_already_in_the_start_equity(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0, "24h": T0})
    c = _ledger()
    _close(c, _open(c, mint="Early"), at=T0 - 30, pnl=99.0)
    _close(c, _open(c, mint="AtEnd"), at=T0 + 600, pnl=1.0)
    rec, _ = _recorder(tmp_path, c)
    store, _ = _step(tmp_path, rec, T0)
    _, out = _step(tmp_path, rec, T0 + 600, store)
    assert out["windows"]["24h"]["trading"]["realized"] == pytest.approx(1.0)


def test_before_the_first_sample_a_window_is_not_recorded_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0 - 3600, "24h": T0 - 86400})
    c = _ledger()
    rec, _ = _recorder(tmp_path, c)
    _, out = _step(tmp_path, rec, T0)
    for w in out["windows"].values():
        assert w["status"] == "not_recorded_yet" and w["other"]["usd"] is None and "other" in w


def test_a_window_that_began_before_recording_is_partial_from_the_first_sample(tmp_path, monkeypatch):
    # Operator 2026-10-08 ("doesn't work"): with samples in hand, a window that starts before the
    # recorder's first sample runs from that sample, marked partial, never "not recorded yet".
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0 - 3600, "24h": T0 - 86400})
    out = _two_samples(tmp_path, end_prices={"ETHUSDT": 1900.0, "BNBUSDT": 600.0, "SOLUSDT": 150.0})
    for w in out["windows"].values():
        assert w["status"] == "ok" and w["partial"] is True and w["since_at"] == T0
        assert w["since"] == "recording_began" and w["start_at"] == T0
        assert w["other"]["usd"] == pytest.approx(w["change"] - w["trading"]["usd"] - w["drift"]["usd"])


def test_a_gap_at_the_window_start_runs_from_the_first_sample_after_it(tmp_path, monkeypatch):
    c = _ledger()
    rec, _ = _recorder(tmp_path, c)
    store, _ = _step(tmp_path, rec, T0)
    store, _ = _step(tmp_path, rec, T0 + 2000, store)
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0 + 1000, "24h": T0 + 1000})
    _, out = _step(tmp_path, rec, T0 + 2060, store)
    w = out["windows"]["24h"]
    assert w["status"] == "ok" and w["partial"] is True and w["since_at"] == T0 + 2000 and w["since"] == "gap"


def test_a_window_with_one_sample_is_not_recorded_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0 - 3600, "24h": T0 - 86400})
    rec, _ = _recorder(tmp_path, _ledger())
    _, out = _step(tmp_path, rec, T0)
    assert all(w["status"] == "not_recorded_yet" and w["partial"] is False for w in out["windows"].values())


def test_a_window_covered_from_its_start_is_not_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0, "24h": T0})
    out = _two_samples(tmp_path)
    assert out["windows"]["today"]["partial"] is False and out["windows"]["today"]["since_at"] is None


def test_an_unknown_end_is_unknown_now_and_other_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(eq, "window_starts", lambda now: {"today": T0, "24h": T0})
    out = _two_samples(tmp_path, arc_end=None, end=T0 + 1200)
    w = out["windows"]["24h"]
    assert w["status"] == "unknown_now" and w["other"]["usd"] is None and w["other"]["why_unknown"]
    assert w["equity_now"] is None and "5042" in w["missing"]


# ------------------------------------------------------------------ publication and the web

def test_the_snapshot_is_written_atomically_and_the_store_keeps_each_network(tmp_path):
    c = _ledger()
    rec, _ = _recorder(tmp_path, c)
    _step(tmp_path, rec, T0)
    snap = json.loads((tmp_path / "equity_change.json").read_text())
    assert snap["ts"] == T0 and set(snap["windows"]) == {"today", "24h"}
    db = sqlite3.connect(str(tmp_path / "memecoin_equity.db"))
    assert {r[0] for r in db.execute("SELECT network FROM equity_by_network")} == set(NETS)
    assert db.execute("SELECT equity_usd FROM equity_samples").fetchone()[0] == pytest.approx(1440.0)


def test_the_route_reads_the_file_and_marks_it_stale(tmp_path, monkeypatch):
    from test_memecoin_real_deploy_routes import _client
    cl, _c = _client({"equity_change_json": str(tmp_path / "equity_change.json")})
    assert cl.get("/api/memecoin/real/equity-change").get_json()["available"] is False
    nf.write_json_atomic(str(tmp_path / "equity_change.json"), {"ts": T0, "now": {}, "windows": {}})
    monkeypatch.setattr(eq.time, "time", lambda: T0 + 181)
    got = cl.get("/api/memecoin/real/equity-change").get_json()
    assert got["available"] is True and got["stale"] is True and got["age_s"] == 181.0


def test_the_recorder_never_raises_into_the_sampler(tmp_path, caplog):
    rec, _ = _recorder(tmp_path, _ledger())
    out = rec.sample(None, None, T0)                          # no native float sample this minute
    assert out["now"]["equity_usd"] is None and out["now"]["missing"]
    rec._store = None
    rec._state_path_fn = lambda *p: (_ for _ in ()).throw(OSError("disk full"))
    assert rec.sample(None, None, T0) is None
    assert any("equity change" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ guards (Alpha 2026-10-08)

def test_the_recorder_reads_the_ledger_read_only_and_takes_no_fill_path_lock():
    import inspect
    src = inspect.getsource(eq)
    assert "get_readonly_connection" in src and "sniper_tracker" not in src and "DB_PATH" not in src
    for forbidden in ("exec_lock", "wallet_exec_lock", "solana_real_seams", "_quote_with_fallback",
                      "evm_rpc_for_chain", "dexscreener", "MEMECOIN_SOLANA_RPC_URL", "jupiter",
                      "get_connection(", "_lock_path"):
        assert forbidden not in src, forbidden


def test_the_default_ledger_handle_is_read_only(tmp_path, monkeypatch):
    from sniper.persistence import db_core
    opened = []
    real = db_core.get_readonly_connection
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda *a, **k: opened.append(a) or real(str(tmp_path / "l.db")))
    sqlite3.connect(str(tmp_path / "l.db")).close()
    rec = eq.EquityRecorder(state_path_fn=lambda *p: str(tmp_path / p[-1]), evm_address=None,
                            post_for=lambda chain: None, networks_fn=lambda: ["solana"])
    conn = rec._ledger()
    assert opened and conn.execute("PRAGMA query_only").fetchone()[0] == 1


def test_the_sampler_runs_the_recorder_after_each_sample_unless_its_knob_is_off(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    calls = []

    class Done(Exception):
        pass

    class Rec:
        def __init__(self, **kw):
            calls.append("init")

        def sample(self, store, snapshot, now):
            calls.append(("sample", snapshot is not None))

    def run(knob):
        calls.clear()
        monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_RECORDER", knob, raising=False)
        monkeypatch.setattr(cfg, "MEMECOIN_NATIVE_FLOAT_SAMPLER", True, raising=False)
        monkeypatch.setattr(eq, "EquityRecorder", Rec)
        nf.remember("solana", "native", 1.0, now=T0)

        def sleep(s):
            raise Done
        monkeypatch.setattr(nf.threading, "Thread", lambda target, **kw: type(
            "T", (), {"start": lambda self: None, "run": staticmethod(target)})())
        t = nf.start_sampler(evm_address=None, state_path_fn=lambda *p: str(tmp_path / p[-1]),
                             get=_binance(PRICES), sleep=sleep, clock=lambda: T0)
        with pytest.raises(Done):
            t.run()
        return list(calls)
    assert run(True) == ["init", ("sample", True)]
    assert run(False) == []


def test_the_prefetch_records_the_solana_usdc_it_already_read():
    import inspect
    from sniper.services import memecoin_slot_lane
    lane = inspect.getsource(memecoin_slot_lane.LanePrefetcher)
    assert 'remember("solana", "quote", usdc)' in lane


def test_the_quote_recorder_keeps_a_cash_value_with_its_time():
    nf.remember("solana", "quote", 412.5, now=T0)
    assert nf.recorded_value("solana", "quote", T0 + 10) == (412.5, T0)
    assert nf.recorded_value("solana", "quote", T0 + nf.RECORDER_MAX_AGE_S + 1) is None


# ------------------------------------------------------------------ the ledger reads

def test_realized_between_counts_real_closes_in_the_half_open_interval():
    c = _ledger()
    _close(c, _open(c, mint="A"), at=T0, pnl=5.0)                 # at the start: excluded
    _close(c, _open(c, mint="B"), at=T0 + 10, pnl=2.0)
    _close(c, _open(c, mint="C"), at=T0 + 20, pnl=-1.0)           # at the end: included
    assert m.real_realized_between(c, _iso(T0), _iso(T0 + 20)) == (pytest.approx(1.0), 2)


def test_realized_between_keeps_unpriced_closes_unknown_and_excludes_outside_the_window():
    c = _ledger()
    _close(c, _open(c, mint="Before"), at=T0, pnl=None)
    _close(c, _open(c, mint="After"), at=T0 + 21, pnl=None)
    _close(c, _open(c, mint="Known"), at=T0 + 10, pnl=0.0)
    assert m.real_realized_between(c, _iso(T0), _iso(T0 + 20)) == (0.0, 1)
    _close(c, _open(c, mint="Unknown"), at=T0 + 20, pnl=None)
    assert m.real_realized_between(c, _iso(T0), _iso(T0 + 20)) == (None, 2)


def test_latest_position_mids_reads_the_newest_positive_stored_mark():
    c = _ledger()
    _open(c, mint="A", mid=0.01, mark_at=T0 - 86400)          # 2026-10-07: older than the next mark
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source) "
              "VALUES ('solana','A',?,?,'fastlane_sol_swap')",
              ("2026-10-08T00:00:09+00:00", 0.02))
    _open(c, mint="B")
    c.commit()
    got = m.latest_position_mids(c, m.list_open_positions(c, mode="real"))
    assert got == {m.accounting_mark_key("solana", "A"): 0.02}
    # The age bound: a mark before `since_iso` is no mark (whole-second bound, both stored formats).
    assert m.latest_position_mids(c, m.list_open_positions(c, mode="real"),
                                  since_iso="2026-10-08T00:00:10+00:00") == {}
    assert m.latest_position_mids(c, m.list_open_positions(c, mode="real"),
                                  since_iso="2026-10-08T00:00:09+00:00") == {m.accounting_mark_key("solana", "A"): 0.02}


@pytest.mark.parametrize("source,quality,mid", [
    ("paper_dexscreener", "quarantined_spike", 100.0),
    ("paper_dexscreener", None, 100.0),
    ("fastlane_sol_swap", "quarantined_spike", 100.0),
    (None, None, 100.0),
    ("fastlane_sol_swap", None, 0.0),
    ("fastlane_sol_swap", None, -1.0),
])
def test_equity_mark_skips_unsafe_or_nonpositive_latest_row(source, quality, mid):
    c = _ledger()
    _open(c, mid=0.03, mark_at=T0)
    c.execute("INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality) "
              "VALUES ('solana','CoinA',?,?,?,?)", (_iso(T0 + 1), mid, source, quality))
    positions = m.list_open_positions(c, mode="real")
    assert m.latest_position_mids(c, positions) == {("solana", "CoinA"): 0.03}
    assert m.latest_position_mids(c, positions, since_iso=_iso(T0 + 1)) == {}


def test_equity_unsafe_only_mark_makes_open_book_unknown(tmp_path):
    c = _ledger()
    _open(c)
    c.execute("INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality) "
              "VALUES ('solana','CoinA',?,100,'paper_dexscreener','quarantined_spike')", (_iso(T0),))
    recorder, _ = _recorder(tmp_path, c)
    _, out = _step(tmp_path, recorder, T0)
    assert out["now"]["equity_usd"] is None
    assert out["now"]["by_network"]["solana"]["missing"] == "marks"
