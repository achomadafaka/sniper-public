import math
import sqlite3

from sniper.algos.polymarket_crypto5m import vol_floor_shadow as VFS


def test_candidate_fair_exposes_floor_compression():
    out = VFS.candidate_fairs(
        btc_now=100.05,
        btc_start=100.0,
        raw_vol=1.5e-5,
        seconds_left=150,
        floors=(1e-5, 1e-4),
    )

    assert out["1e-05"]["floor_bound"] is False
    assert out["0.0001"]["floor_bound"] is True
    assert out["1e-05"]["fair_up"] > 0.99
    assert 0.65 < out["0.0001"]["fair_up"] < 0.67


def test_fill_edge_and_settlement_pnl_are_token_side_aware():
    assert VFS.fill_edge(token="up", side="bid", price=0.40, fair_up=0.70) == 0.30
    assert VFS.fill_edge(token="down", side="bid", price=0.40, fair_up=0.70) == -0.10
    assert VFS.settled_fill_pnl(token="up", side="bid", price=0.40, size=10, winner="Up") == 6.0
    assert VFS.settled_fill_pnl(token="up", side="ask", price=0.70, size=10, winner="Up") == -3.0
    assert VFS.settled_fill_pnl(token="down", side="ask", price=0.70, size=10, winner="Up") == 7.0


def test_build_vol_floor_shadow_report_scores_floors_against_market_and_fills():
    capture = sqlite3.connect(":memory:")
    capture.row_factory = sqlite3.Row
    capture.executescript(
        """
        CREATE TABLE crypto5m_binance_observations(
          id INTEGER PRIMARY KEY, symbol TEXT, source_event_ts INTEGER, price REAL);
        CREATE TABLE crypto5m_markets(
          condition_id TEXT, window_start_ts INTEGER, window_end_ts INTEGER,
          up_token_id TEXT, down_token_id TEXT);
        CREATE TABLE crypto5m_settlements(condition_id TEXT, winning_outcome TEXT);
        CREATE TABLE crypto5m_book_snapshots(
          condition_id TEXT, token_id TEXT, best_bid REAL, best_ask REAL, exchange_ts INTEGER);
        """
    )
    ws = 1_000_000
    px = 100.0
    for sec in range(ws - 180, ws + 151):
        if sec == ws + 10:
            px *= math.exp(0.0005)
        else:
            px *= math.exp(1e-6 if sec % 2 else -1e-6)
        capture.execute(
            "INSERT INTO crypto5m_binance_observations(symbol,source_event_ts,price) VALUES(?,?,?)",
            ("BTCUSDT", sec * 1000, px),
        )
    capture.execute(
        "INSERT INTO crypto5m_markets VALUES(?,?,?,?,?)",
        ("0xw", ws, ws + 300, "up-token", "down-token"),
    )
    capture.execute("INSERT INTO crypto5m_settlements VALUES(?,?)", ("0xw", "Up"))
    for sec in (ws + 10, ws + 20, ws + 30):
        capture.execute(
            "INSERT INTO crypto5m_book_snapshots VALUES(?,?,?,?,?)",
            ("0xw", "up-token", 0.49, 0.51, sec * 1000),
        )
    capture.commit()

    maker = sqlite3.connect(":memory:")
    maker.row_factory = sqlite3.Row
    maker.executescript(
        """
        CREATE TABLE maker_windows(
          condition_id TEXT, window_start_ts INTEGER, settled INTEGER,
          winning_outcome TEXT, realized_pnl REAL, campaign_id TEXT);
        CREATE TABLE maker_fills(
          campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER, token TEXT,
          side TEXT, price REAL, size REAL, fair REAL, ts INTEGER);
        """
    )
    maker.execute(
        "INSERT INTO maker_windows VALUES(?,?,?,?,?,?)",
        ("0xw", ws, 1, "Up", 6.0, "maker-regime-v1"),
    )
    maker.execute(
        "INSERT INTO maker_fills VALUES(?,?,?,?,?,?,?,?,?)",
        ("maker-regime-v1", "0xw", ws, "up", "bid", 0.40, 10.0, 0.66, ws + 10),
    )
    maker.commit()

    report = VFS.build_vol_floor_shadow_report(
        capture,
        maker,
        campaign="maker-regime-v1",
        floors=(1e-5, 1e-4),
        sample_step_s=10,
    )

    assert report["vol_summary"]["floors"]["0.0001"]["bind_count"] > 0
    assert report["brier_by_floor"]["1e-05"]["brier"] < report["brier_by_floor"]["0.0001"]["brier"]
    assert report["brier_market_mid"]["brier"] == 0.25
    assert report["fill_edge_by_floor"]["1e-05"]["mean_claimed_edge"] > report["fill_edge_by_floor"]["0.0001"]["mean_claimed_edge"]
    assert report["fill_edge_by_floor"]["0.0001"]["mean_realized_pnl_per_share"] == 0.6


def test_resolved_windows_can_be_cut_before_forward_anchor():
    capture = sqlite3.connect(":memory:")
    capture.row_factory = sqlite3.Row
    capture.executescript(
        """
        CREATE TABLE crypto5m_markets(
          condition_id TEXT, window_start_ts INTEGER, window_end_ts INTEGER,
          up_token_id TEXT, down_token_id TEXT);
        CREATE TABLE crypto5m_settlements(condition_id TEXT, winning_outcome TEXT);
        """
    )
    for cid, ws in (("0xold", 1_000), ("0xanchor", 1_300), ("0xnew", 1_600)):
        capture.execute("INSERT INTO crypto5m_markets VALUES(?,?,?,?,?)", (cid, ws, ws + 300, "up", "down"))
        capture.execute("INSERT INTO crypto5m_settlements VALUES(?,?)", (cid, "Up"))
    capture.commit()

    rows = VFS._load_resolved_windows(capture, before_window_start_ts=1_300)

    assert [r["condition_id"] for r in rows] == ["0xold"]


def test_fill_edge_report_honors_forward_anchor():
    maker = sqlite3.connect(":memory:")
    maker.row_factory = sqlite3.Row
    maker.executescript(
        """
        CREATE TABLE maker_windows(
          condition_id TEXT, window_start_ts INTEGER, settled INTEGER,
          winning_outcome TEXT, realized_pnl REAL, campaign_id TEXT);
        CREATE TABLE maker_fills(
          campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER, token TEXT,
          side TEXT, price REAL, size REAL, fair REAL, ts INTEGER);
        """
    )
    for cid, ws in (("0xold", 1_000), ("0xnew", 1_300)):
        maker.execute("INSERT INTO maker_windows VALUES(?,?,?,?,?,?)", (cid, ws, 1, "Up", 1.0, "maker-regime-v1"))
        maker.execute(
            "INSERT INTO maker_fills VALUES(?,?,?,?,?,?,?,?,?)",
            ("maker-regime-v1", cid, ws, "up", "bid", 0.40, 10.0, 0.60, ws + 10),
        )
    maker.commit()
    series_index = ([1_000, 1_010, 1_300, 1_310], [100.0, 100.1, 100.0, 100.1])
    vol_by_sec = {1_010: 1e-4, 1_310: 1e-4}

    out = VFS._fill_edge_report(
        maker,
        "maker-regime-v1",
        series_index,
        vol_by_sec,
        (1e-4,),
        before_window_start_ts=1_300,
    )

    assert out["0.0001"]["n_fills"] == 1


def test_build_vol_floor_shadow_report_scores_market_blends():
    capture = sqlite3.connect(":memory:")
    capture.row_factory = sqlite3.Row
    capture.executescript(
        """
        CREATE TABLE crypto5m_binance_observations(
          id INTEGER PRIMARY KEY, symbol TEXT, source_event_ts INTEGER, price REAL);
        CREATE TABLE crypto5m_markets(
          condition_id TEXT, window_start_ts INTEGER, window_end_ts INTEGER,
          up_token_id TEXT, down_token_id TEXT);
        CREATE TABLE crypto5m_settlements(condition_id TEXT, winning_outcome TEXT);
        CREATE TABLE crypto5m_book_snapshots(
          condition_id TEXT, token_id TEXT, best_bid REAL, best_ask REAL, exchange_ts INTEGER);
        """
    )
    ws = 2_000_000
    px = 100.0
    for sec in range(ws - 180, ws + 151):
        px *= math.exp(0.0002 if sec >= ws + 10 else 1e-6)
        capture.execute(
            "INSERT INTO crypto5m_binance_observations(symbol,source_event_ts,price) VALUES(?,?,?)",
            ("BTCUSDT", sec * 1000, px),
        )
    capture.execute(
        "INSERT INTO crypto5m_markets VALUES(?,?,?,?,?)",
        ("0xblend", ws, ws + 300, "up-token", "down-token"),
    )
    capture.execute("INSERT INTO crypto5m_settlements VALUES(?,?)", ("0xblend", "Up"))
    for sec in (ws + 10, ws + 20, ws + 30, ws + 40):
        capture.execute(
            "INSERT INTO crypto5m_book_snapshots VALUES(?,?,?,?,?)",
            ("0xblend", "up-token", 0.79, 0.81, sec * 1000),
        )
    capture.commit()

    maker = sqlite3.connect(":memory:")
    maker.row_factory = sqlite3.Row
    maker.executescript(
        """
        CREATE TABLE maker_windows(
          condition_id TEXT, window_start_ts INTEGER, settled INTEGER,
          winning_outcome TEXT, realized_pnl REAL, campaign_id TEXT);
        CREATE TABLE maker_fills(
          campaign_id TEXT, condition_id TEXT, window_start_ts INTEGER, token TEXT,
          side TEXT, price REAL, size REAL, fair REAL, ts INTEGER);
        """
    )
    maker.execute(
        "INSERT INTO maker_windows VALUES(?,?,?,?,?,?)",
        ("0xblend", ws, 1, "Up", 2.0, "maker-regime-v1"),
    )
    maker.execute(
        "INSERT INTO maker_fills VALUES(?,?,?,?,?,?,?,?,?)",
        ("maker-regime-v1", "0xblend", ws, "up", "bid", 0.78, 10.0, 0.90, ws + 20),
    )
    maker.commit()

    report = VFS.build_vol_floor_shadow_report(
        capture,
        maker,
        campaign="maker-regime-v1",
        floors=(1e-4,),
        blend_weights=(0.25, 0.50, 0.75),
        sample_step_s=10,
    )

    assert "0.5@0.0001" in report["brier_by_blend"]
    assert report["brier_by_blend"]["0.5@0.0001"]["weight_structural"] == 0.5
    assert report["brier_by_blend"]["0.5@0.0001"]["n"] > 0
    assert report["fill_edge_by_blend"]["0.5@0.0001"]["n_fills"] == 1
    assert report["fill_edge_by_blend"]["0.5@0.0001"]["mean_realized_pnl_per_share"] == 0.22
