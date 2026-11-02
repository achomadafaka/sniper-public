"""Actual USD fill anchors SOL candles; absent conversion never invents gains."""
import json
import sqlite3
from datetime import datetime

import pytest

from sniper.services import memecoin_desk_risk as R
from sniper.services import memecoin_desk_risk_live as L
from test_memecoin_desk_risk_watcher import Env, POLICY, T0, W, pos
from test_memecoin_desk_risk_shadow import RW
from sniper.util import clock


def position():
    return {**pos(1), "entry_price": 0.0025}


def candles():
    # SOL/token, with a large pre-fill high and a mixed fill-second candle.
    return [{"t": int(T0 + t), "o": c, "h": h, "l": c, "c": c,
             "buy_usd": 10, "sell_usd": 5, "n_makers": 2}
            for t, c, h in [(-1, 0.00001, 0.000125), (0, 0.000025, 0.0001),
                            (1, 0.0000275, 0.00003)]]


def test_live_prices_use_actual_usd_fill_and_post_fill_extrema():
    f = L.position_facts(position(), POLICY, mark=None, bars=candles(), now=T0 + 2, sol_usd=100)
    assert f["rows"][0][4] == 4000
    assert f["last_bp"] == 11000
    assert f["high_bp"] == 12000  # pre-fill and mixed-second highs cannot become our peak
    assert f["dd_bp"] == 1000


def test_shadow_prices_use_the_same_actual_fill_baseline():
    f = R.facts(position(), POLICY, mark=None, bars=candles(), boundary="fill", now=T0 + 2, sol_usd=100)
    assert [b["c"] for b in f["bars_vs_fill"]] == [0.4, 1.0, 1.1]


@pytest.mark.parametrize("field", ["entry_price", "sol_usd"])
@pytest.mark.parametrize("bad", [None, 0, -1, True, float("nan"), float("inf"),
                                 pytest.param(10 ** 1000, id="oversized_int")])
def test_unknown_or_invalid_price_conversion_never_falls_back_to_first_candle(field, bad):
    p, rate = position(), 100
    if field == "entry_price":
        p[field] = bad
    else:
        rate = bad
    mark = {"sweep_ts": p["opened_at"], "executable_exit_value": 30.0}
    f = L.position_facts(p, POLICY, mark=mark, bars=candles(), now=T0 + 2, sol_usd=rate)
    assert f["tape"] == "unknown" and f["rows"] == []
    assert f["high_bp"] is None and f["last_bp"] is None
    assert f["pnl_pct_exact"] == 20.0  # price conversion cannot change executable PnL
    shadow = R.facts(p, POLICY, mark=mark, bars=candles(), boundary="fill", now=T0 + 2, sol_usd=rate)
    assert shadow["bars_vs_fill"] == []


def test_mixed_fill_second_alone_cannot_claim_a_post_fill_high():
    f = L.position_facts(position(), POLICY, mark=None, bars=candles()[:2], now=T0 + 0.5, sol_usd=100)
    assert f["rows"] and f["high_bp"] is None and f["last_bp"] is None


def heartbeat(tmp_path, **changes):
    beat = {"ts": T0 + 2, "connected": True, "sol_usd": 100, "sol_age_s": 2}
    beat.update(changes)
    (tmp_path / "universe.heartbeat").write_text(json.dumps(beat))


@pytest.mark.parametrize("changes", [
    {"sol_usd": 0}, {"sol_usd": True}, {"sol_usd": float("inf")},
    {"ts": T0 + 4}, {"ts": T0 - 4}, {"connected": False},
    {"sol_age_s": None}, {"sol_age_s": True}, {"sol_age_s": -1},
    {"sol_age_s": 120}, {"ts": True}, {"sol_age_s": float("nan")},
    pytest.param({"sol_usd": 10 ** 1000}, id="oversized_rate"),
    pytest.param({"sol_age_s": 10 ** 1000}, id="oversized_age"),
    pytest.param({"ts": 10 ** 1000}, id="oversized_timestamp"),
])
def test_local_conversion_refuses_unknown_future_stale_or_disconnected_heartbeat(tmp_path, changes):
    heartbeat(tmp_path, **changes)
    assert RW.local_sol_usd(tmp_path, now=T0 + 3) is None


def test_local_conversion_uses_combined_age_and_handles_missing_file(tmp_path):
    assert RW.local_sol_usd(tmp_path, now=T0 + 3) is None
    heartbeat(tmp_path, sol_age_s=119)
    assert RW.local_sol_usd(tmp_path, now=T0 + 3) == 100
    assert RW.local_sol_usd(tmp_path, now=T0 + 3.01) is None


def test_live_watcher_wires_validated_conversion_into_real_facts(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, [position()], bars=candles())
    e.t = T0 + 3
    heartbeat(tmp_path)
    e.tick()
    assert e.calls[0][0]["last_bp"] == 11000
    heartbeat(tmp_path, sol_age_s=120)
    e.tick(dt=5)
    assert e.calls[-1][0]["tape"] == "unknown"


def test_live_watcher_reads_real_sol_candles_and_fx_against_usd_fill(tmp_path, monkeypatch):
    real_bars = W.bars_since
    e = Env(tmp_path, monkeypatch, [position()])
    e.t = T0 + 3
    monkeypatch.setattr(W, "bars_since", real_bars)
    folder = tmp_path / "candles_1s"
    folder.mkdir()
    c = sqlite3.connect(folder / "2026-10-22.db")
    c.execute("CREATE TABLE candles_1s(pool TEXT,t INTEGER,o REAL,h REAL,l REAL,c REAL,"
              "buy_usd REAL,sell_usd REAL,n_makers INTEGER)")
    c.executemany("INSERT INTO candles_1s VALUES(?,?,?,?,?,?,?,?,?)", [
        ("P1", b["t"], b["o"], b["h"], b["l"], b["c"], b["buy_usd"], b["sell_usd"], b["n_makers"])
        for b in candles()])
    c.commit()
    c.close()
    heartbeat(tmp_path)
    e.tick(fill_sigs_of=lambda _: ())
    block = e.calls[0][0]
    assert block["rows"][0][4] == 4000
    assert (block["high_bp"], block["last_bp"]) == (12000, 11000)


def test_shadow_worker_wires_the_same_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr(RW, "open_desk_positions", lambda _: [(position(), POLICY, [], None)])
    monkeypatch.setattr(RW, "tape_bars", lambda *a, **k: candles())
    heartbeat(tmp_path)
    seen = []
    def model(_system, user):
        seen.append(json.loads(user))
        return {"text": '{"k":"pos:1","a":"H","r":null,"c":2}'}
    RW.run_pass(tmp_path / "ledger.db", tmp_path, tmp_path / "shadow.db", call_model=model,
                now_fn=lambda: T0 + 3)
    assert seen[0]["bars_vs_fill"][-1]["c"] == 1.1


@pytest.mark.parametrize("changes", [
    {"executable_exit_value": float("nan")}, {"executable_exit_value": float("inf")},
    {"executable_exit_value": True}, {"sweep_ts": "broken"}, {"sweep_ts": None},
    {"sweep_ts": "2026-10-22T09:00:00"},
    {"sweep_ts": "2026-10-22T09:00:05+00:00"},
])
def test_invalid_or_future_exact_mark_is_unknown_without_breaking_facts(changes):
    mark = {"sweep_ts": position()["opened_at"], "executable_exit_value": 30.0, **changes}
    f = R.facts(position(), POLICY, mark=mark, bars=[], boundary="fill", now=T0 + 2)
    assert f["pnl_pct_exact"] is None


@pytest.mark.parametrize("notional", [True, float("nan"), float("inf")])
def test_nonfinite_or_boolean_notional_is_not_valid_pnl(notional):
    p = {**position(), "entry_notional": notional}
    mark = {"sweep_ts": p["opened_at"], "executable_exit_value": 30.0}
    assert R.pnl_basis(p, mark, now=T0 + 2) == "no_entry_notional"


@pytest.mark.parametrize("notional,value", [pytest.param(10 ** 1000, 30.0, id="oversized_notional"),
    pytest.param(25.0, 10 ** 1000, id="oversized_quote"), pytest.param(1e-300, 1e300, id="ratio_overflow")])
def test_pnl_rejects_oversized_or_overflowing_values(notional, value):
    p = {**position(), "entry_notional": notional}
    mark = {"sweep_ts": p["opened_at"], "executable_exit_value": value}
    assert R.facts(p, POLICY, mark=mark, bars=[], boundary="fill", now=T0 + 2)["pnl_pct_exact"] is None


def _store_candles(desk, day, bars):
    folder = desk / "candles_1s"
    folder.mkdir(exist_ok=True)
    with sqlite3.connect(folder / (day + ".db")) as conn:
        conn.execute("CREATE TABLE candles_1s(pool TEXT,t INTEGER,o REAL,h REAL,l REAL,c REAL,"
                     "buy_usd REAL,sell_usd REAL,n_makers INTEGER)")
        conn.executemany("INSERT INTO candles_1s VALUES(?,?,?,?,?,?,?,?,?)", [
            ("P1", b["t"], b["o"], b["h"], b["l"], b["c"], b["buy_usd"], b["sell_usd"], b["n_makers"])
            for b in bars])


@pytest.mark.parametrize("skew", [120, -120, 25_200, -25_200])
def test_live_worker_separates_corrected_facts_from_host_heartbeat_attention_and_ttl(tmp_path, monkeypatch, skew):
    real_bars = W.bars_since
    monkeypatch.setattr(clock, "offset_s", lambda: skew)
    e = Env(tmp_path, monkeypatch, [position()])
    e.t = T0 + 3 + skew
    e.state = W.new_state(started_at=T0 - 10 + skew)
    monkeypatch.setattr(W, "bars_since", real_bars)
    _store_candles(tmp_path, RW.utc_day(e.t), candles() + [{**candles()[-1], "t": int(T0 + 4), "c": 1.0}])
    heartbeat(tmp_path, ts=e.t - 1)
    mark = {"sweep_ts": "2026-10-22T09:00:02+00:00", "executable_exit_value": 30.0}
    structure_times = []
    monkeypatch.setattr(W, "structure_of", lambda _p, **kw: structure_times.append(kw["now"]) or {})
    e.answer = lambda _blocks: '[{"k":"pos:1","a":"T","r":{"sl_pct":12},"c":6}]'
    W.run_pass(tmp_path / "ledger.db", tmp_path, e.out, call_model=e.model, now_fn=lambda: e.t,
               state=e.state, bounds={"sl_pct": (10, 40)}, positions_fn=lambda _: [(position(), POLICY, [], mark)],
               attention_fn=lambda _id, now: [("sell_burst", now - 2)])
    block = e.calls[0][0]
    assert (block["pnl_pct_exact"], block["mark_age_s"], block["held_s"], block["to_5min_s"]) == (20, 1, 3, 297)
    assert [r[0] for r in block["rows"]] == [-4, -3, -2]
    assert block["last_bp"] == 11000 and block["attn"] == [["sell_burst", 2]]
    assert structure_times == [e.t] and block["tape"] == "ok"
    row = e.rows()[0]
    assert (row["pnl_basis"], row["risk_version_mixed"]) == ("exact", 0)
    assert row["decided_at"] == e.t and row["expires_at"] == e.t + L.ORDER_TTL_S


@pytest.mark.parametrize("skew", [120, -120, 25_200, -25_200])
def test_shadow_worker_uses_corrected_mark_holding_and_boundary_time_with_raw_cooldown(tmp_path, monkeypatch, skew):
    monkeypatch.setattr(clock, "offset_s", lambda: skew)
    host_now = [T0 + 3 + skew]
    p = position()
    policy = {**POLICY, "max_hold_s": 300}
    mark = {"sweep_ts": "2026-10-22T09:00:02+00:00", "executable_exit_value": 30.0}
    monkeypatch.setattr(RW, "open_desk_positions", lambda _: [(p, policy, [], mark)])
    _store_candles(tmp_path, RW.utc_day(host_now[0]), candles())
    heartbeat(tmp_path, ts=host_now[0] - 1)
    seen = []
    def model(_system, user):
        seen.append(json.loads(user))
        return {"text": '{"k":"pos:1","a":"H","r":null,"c":2}'}
    def tick():
        return RW.run_pass(tmp_path / "ledger.db", tmp_path, tmp_path / "shadow.db", call_model=model,
                           now_fn=lambda: host_now[0])
    assert tick()["asked"] == 1
    assert (seen[0]["held_s"], seen[0]["to_timecap_s"], seen[0]["pnl_pct_exact"], seen[0]["mark_age_s"]) == (3, 297, 20, 1)
    assert seen[0]["bars_vs_fill"][-1]["c"] == 1.1
    host_now[0] += 238
    heartbeat(tmp_path, ts=host_now[0] - 1)
    assert tick()["asked"] == 1
    assert (seen[-1]["boundary"], seen[-1]["held_s"], seen[-1]["to_timecap_s"]) == ("timecap", 241, 59)
    with sqlite3.connect(tmp_path / "shadow.db") as conn:
        assert conn.execute("SELECT known_at,decided_at FROM decisions ORDER BY id").fetchall() == [
            (T0 + 3 + skew, T0 + 3 + skew), (T0 + 241 + skew, T0 + 241 + skew)]


@pytest.mark.parametrize("opened,skew", [("2026-10-22T23:59:58+00:00", 25_200),
                                         ("2026-10-22T00:00:02+00:00", -25_200)])
def test_curve_candles_use_chain_bounds_in_host_partitioned_files(tmp_path, monkeypatch, opened, skew):
    monkeypatch.setattr(clock, "offset_s", lambda: skew)
    opened_at = datetime.fromisoformat(opened).timestamp()
    host_now = opened_at + 3 + skew
    bars = [{**candles()[0], "t": int(opened_at + dt)} for dt in (-2, -1, 1, 4)]
    _store_candles(tmp_path, RW.utc_day(host_now), bars)
    got = W.bars_since(tmp_path, "P1", opened_at, host_now)
    assert [b["t"] for b in got] == [opened_at - 1, opened_at + 1]
    assert [b["t"] for b in RW.tape_bars(tmp_path, "P1", host_now)] == [
        opened_at - 2, opened_at - 1, opened_at + 1]


@pytest.mark.parametrize("skew", [120, -120])
def test_pumpswap_receipt_window_stays_host_time_and_final_bars_stay_chain_time(tmp_path, monkeypatch, skew):
    import test_desk_ps_tape as PS
    monkeypatch.setattr(clock, "offset_s", lambda: skew)
    monkeypatch.setattr(PS, "NOW", T0 + 3)
    heartbeat(tmp_path, ts=T0 + 2 + skew)
    PS.ps_tape(tmp_path, [(dt, price, 1, 10, "A", slot, PS.POOL, PS.PT.WSOL_MINT)
                          for dt, price, slot in [(-5, 2.0, 10), (-2, 2.2, 11), (-1, 2.3, 12), (1, 2.4, 13)]])
    with sqlite3.connect(PS.PT.tape_path(tmp_path)) as conn:
        conn.execute("UPDATE swaps_ps SET received_at=block_time+?", (skew,))
        # Delayed pre-entry and future-block rows are received inside the valid host-time window.
        conn.execute("UPDATE swaps_ps SET received_at=? WHERE signature IN ('ps0','ps3')", (T0 + 2.5 + skew,))
    got = W.bars_since(tmp_path, "P1", T0, T0 + 3 + skew, mint=PS.MINT)
    assert [b["t"] for b in got] == [T0 + 1, T0 + 2]
    assert [b["c"] for b in got] == pytest.approx([0.022, 0.023])
