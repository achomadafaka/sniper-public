"""Live trader's-desk compute: fair value (where BTC already is vs the book), per-side edge, dynamic entry.
Pure functions only — no network."""
import json
import sqlite3

from sniper.algos.polymarket_crypto5m import live_desk as LD


def test_fair_value_directional_and_guards():
    # BTC up with time left -> P(Up) > 0.5; down -> < 0.5; flat -> 0.5
    up = LD.fair_value_up(btc_now=100.5, btc_start=100.0, vol_per_s=1e-4, seconds_left=60)
    dn = LD.fair_value_up(btc_now=99.5, btc_start=100.0, vol_per_s=1e-4, seconds_left=60)
    flat = LD.fair_value_up(btc_now=100.0, btc_start=100.0, vol_per_s=1e-4, seconds_left=60)
    assert up > 0.5 and dn < 0.5 and abs(flat - 0.5) < 1e-9
    assert up == 1 - dn or abs(up - (1 - dn)) < 1e-9                 # symmetric
    # guards -> None (never fabricate a probability)
    assert LD.fair_value_up(None, 100, 1e-4, 60) is None
    assert LD.fair_value_up(100, 100, 1e-4, 0) is None              # no time left
    assert LD.fair_value_up(100, 100, 0, 60) is None                # no vol


def test_side_edges_underpricing():
    # fair Up 0.70: Up ask 0.60 is UNDERPRICED (+edge); Down ask 0.35 vs fair-down 0.30 is OVERpriced (-edge)
    e = LD.side_edges(0.70, up_ask=0.60, down_ask=0.35, fee_rate=0.0)
    assert e["up"] == round(0.70 - 0.60, 4) and e["up"] > 0
    assert e["down"] == round(0.30 - 0.35, 4) and e["down"] < 0
    # fee makes a thin edge vanish
    assert LD.side_edges(0.70, up_ask=0.69, down_ask=None, fee_rate=0.07)["up"] < 0.70 - 0.69


def test_entry_signal_dynamic():
    enter = LD.entry_signal({"up": 0.05, "down": -0.2}, min_edge=0.02)
    assert enter["action"] == "ENTER" and enter["side"] == "Up" and enter["edge"] == 0.05
    wait = LD.entry_signal({"up": 0.01, "down": -0.2}, min_edge=0.02)            # 0.01 below threshold
    assert wait["action"] == "WAIT" and wait["best_side"] == "Up"
    none = LD.entry_signal({"up": None, "down": None})
    assert none["action"] == "WAIT" and none["best_side"] is None


def test_realized_vol():
    assert LD.realized_vol_per_s([100, 100, 100, 100, 100]) in (None, 0.0)      # flat -> ~0/None
    v = LD.realized_vol_per_s([100, 100.1, 99.95, 100.2, 99.9, 100.3])
    assert v is not None and v > 0
    assert LD.realized_vol_per_s([100, 101]) is None                            # too few points


def test_live_vol_floor_matches_btc_1s_baseline_not_subtick_noise():
    assert LD.VOL_FLOOR_PER_S == 1e-4
    # Regression: 2e-5 made a routine ~1.6 bp move with ~3 min left look like P(Up) ~= 72%.
    fair = LD.fair_value_up(btc_now=62563.94, btc_start=62553.60,
                            vol_per_s=LD.VOL_FLOOR_PER_S, seconds_left=194)
    assert 0.53 < fair < 0.56


def test_adaptive_momentum_scales_horizon_and_confirms_across_scales():
    closes = {s: 1000.0 + 0.01 * s for s in range(0, 401)}      # clean steady uptrend
    m30, h30, a30 = LD.adaptive_momentum(closes, now=400, seconds_left=30)
    m250, h250, a250 = LD.adaptive_momentum(closes, now=400, seconds_left=250)
    assert h30 == 30 and h250 == 120               # horizon matches time-left (clamped to 120)
    assert m30 > 0 and m250 > 0                     # uptrend detected
    assert a30 == 1.0 and a250 == 1.0              # all scales agree on a clean trend
    assert LD.adaptive_momentum({}, 400, 30) == (None, None, None)
    assert LD.adaptive_momentum(closes, 400, 0) == (None, None, None)


def test_book_imbalance_signed_and_guards():
    buy = {"bids": [{"price": "0.50", "size": "100"}], "asks": [{"price": "0.51", "size": "20"}]}
    sell = {"bids": [{"price": "0.50", "size": "20"}], "asks": [{"price": "0.51", "size": "100"}]}
    assert LD.book_imbalance(buy) > 0          # more bid size -> buy pressure (+)
    assert LD.book_imbalance(sell) < 0         # more ask size -> sell pressure (-)
    assert LD.book_imbalance({"bids": [], "asks": []}) is None


def test_diffusion_z_matches_fair_value():
    import math
    # Phi(diffusion_z) must equal fair_value_up for the same inputs.
    args = dict(btc_now=100100.0, btc_start=100000.0, vol_per_s=0.0002, seconds_left=120)
    z = LD.diffusion_z(**args)
    assert z is not None
    assert 0.5 * (1 + math.erf(z / math.sqrt(2))) == LD.fair_value_up(**args)


def test_diffusion_z_guards():
    assert LD.diffusion_z(btc_now=0, btc_start=100, vol_per_s=0.0002, seconds_left=120) is None
    assert LD.diffusion_z(btc_now=100, btc_start=100, vol_per_s=0.0, seconds_left=120) is None
    assert LD.diffusion_z(btc_now=100, btc_start=100, vol_per_s=0.0002, seconds_left=0) is None


def test_signal_series_shape_and_trend():
    from sniper.algos.polymarket_crypto5m.live_desk import signal_series
    now = 1000
    kl = [[(now - 60 + i) * 1000, 0, 0, 0, 100 + 0.1 * i, 1, 0, 0, 0, 0.5] for i in range(61)]  # rising
    s = signal_series(kl, 100.0, 0.0002, now, 120)
    assert s is not None and len(s["price"]) == 61
    assert set(s.keys()) >= {"ts", "price", "ema_fast", "ema_slow", "prob_up"}
    assert s["ema_fast"][-1] > s["ema_slow"][-1]      # uptrend: fast EMA leads the slow
    assert s["prob_up"][-1] > s["prob_up"][0]         # our P(Up) projection rises with price
    assert signal_series([], 100.0, 0.0002, now, 120) is None   # too few klines -> None


def test_desk_exposes_z_v2_shadow_fields(monkeypatch):
    import math
    now = 1782000200
    ws = now - (now % 300)
    # a calm kline ramp + a current price; binance kline rows [open_ms,o,h,l,c,v,...,taker_buy]
    kl = []
    px = 60000.0
    for s in range(ws - 90, now + 1):
        px *= math.exp(1e-4 if s % 2 == 0 else -1e-4)
        kl.append([s * 1000, "0", "0", "0", f"{px:.2f}", "1", "0", "0", "1", "0"])
    monkeypatch.setattr(LD, "server_now", lambda: now)
    monkeypatch.setattr(LD, "_get", lambda url, timeout=6: (
        {"price": f"{px:.2f}"} if "ticker" in url else
        kl if "klines" in url else
        [{"markets": [{"conditionId": "0xc", "clobTokenIds": "[\"u\",\"d\"]"}]}]))
    monkeypatch.setattr(LD, "_best_ask", lambda b: 0.56 if b else None)
    monkeypatch.setattr(LD, "_best_bid", lambda b: 0.54 if b else None)
    monkeypatch.setattr(LD, "book_imbalance", lambda b, levels=3: 0.0)
    monkeypatch.setattr(LD, "microprice", lambda b: (None, None))
    monkeypatch.setattr(LD, "tape_order_flow",
                        lambda *a, **k: {"ofi_10s": None, "ofi_30s": None, "tape_n_10s": 0})
    d = LD.build_live_desk(now)
    for k in ("z_v2", "z_ours", "z_market", "z_weight", "vol_slow", "vol_fast", "jump_flag", "divergence"):
        assert k in d
    assert d["z"] is not None                       # current z still computed (unchanged path)
    assert d["z_v2"] is not None


def test_build_live_desk_does_not_fall_back_to_skewed_local_clock(monkeypatch):
    monkeypatch.setattr(LD, "server_now", lambda: (_ for _ in ()).throw(RuntimeError("time down")))
    monkeypatch.setattr(LD.time, "time", lambda: 1_783_192_000)

    d = LD.build_live_desk()

    assert d["available"] is False
    assert d["now_ts"] is None
    assert d["window_start_ts"] is None
    assert "server time" in d["message"]


def test_tape_prints_reads_the_requested_token_side(tmp_path):
    db = tmp_path / "capture.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE crypto5m_book_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT, token_id TEXT, side TEXT, event_type TEXT,
            exchange_ts INTEGER, recv_ts INTEGER, persist_ts INTEGER,
            reconnect_generation INTEGER, quality_flags TEXT, raw_payload TEXT
        )
    """)
    conn.execute(
        "INSERT INTO crypto5m_book_events(condition_id,side,event_type,exchange_ts,raw_payload) "
        "VALUES(?,?,?,?,?)",
        ("0xc", "Up", "last_trade_price", 101_000, json.dumps({"price": "0.35", "size": "10"})),
    )
    conn.execute(
        "INSERT INTO crypto5m_book_events(condition_id,side,event_type,exchange_ts,raw_payload) "
        "VALUES(?,?,?,?,?)",
        ("0xc", "Down", "last_trade_price", 102_000, json.dumps({"price": "0.44", "size": "7"})),
    )
    conn.commit()
    conn.close()

    assert LD.tape_prints("0xc", 100, 103, token="up", capture_db=str(db)) == [(0.35, 10.0)]
    assert LD.tape_prints("0xc", 100, 103, token="down", capture_db=str(db)) == [(0.44, 7.0)]


def test_tape_prints_does_not_fabricate_down_from_up_only(tmp_path):
    db = tmp_path / "capture.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE crypto5m_book_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT, token_id TEXT, side TEXT, event_type TEXT,
            exchange_ts INTEGER, recv_ts INTEGER, persist_ts INTEGER,
            reconnect_generation INTEGER, quality_flags TEXT, raw_payload TEXT
        )
    """)
    conn.execute(
        "INSERT INTO crypto5m_book_events(condition_id,side,event_type,exchange_ts,raw_payload) "
        "VALUES(?,?,?,?,?)",
        ("0xc", "Up", "last_trade_price", 101_000, json.dumps({"price": "0.35", "size": "10"})),
    )
    conn.commit()
    conn.close()

    assert LD.tape_prints("0xc", 100, 103, token="down", capture_db=str(db)) == []
