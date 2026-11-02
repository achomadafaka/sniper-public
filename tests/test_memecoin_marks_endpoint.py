"""Local price-mark chart endpoint — the fix for "charts don't work on non-Solana".

DexScreener indexes no pair for chain 4663 (Robinhood), so the UI needs the locally persisted mark
series. Current rows come from GeckoTerminal; historical provider provenance remains readable.

This endpoint serves that tape so the front end can draw an inline chart. Marks are not candles or
traded volume, and the UI must label them as such.
"""
import sqlite3
from pathlib import Path

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint


def _client():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client(), c


def _mark(c, chain, mint, ts, mid, mcap=None, source="geckoterminal", quality="ok"):
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
              "executable_exit_value, liquidity, mcap, source, quality) "
              "VALUES(?,?,?,?,?,?,?,?,?)", (chain, mint, ts, mid, mid, None, mcap, source, quality))
    c.commit()


def test_serves_the_mark_series_for_a_4663_token():
    cl, c = _client()
    _mark(c, "4663", "0xwood", "2026-07-15T00:00:00Z", 0.011, 11_000_000)
    _mark(c, "4663", "0xwood", "2026-07-15T00:01:00Z", 0.013, 13_000_000)
    _mark(c, "4663", "0xwood", "2026-07-15T00:02:00Z", 0.017, 17_000_000)

    j = cl.get("/api/memecoin/marks/4663/0xwood").get_json()
    assert j["mint"] == "0xwood" and j["chain"] == "4663"
    assert j["source"] == "geckoterminal"
    assert [p["mid"] for p in j["points"]] == [0.011, 0.013, 0.017]   # chronological
    assert j["last"] == 0.017
    assert j["n"] == 3
    assert round(j["change_pct"], 1) == 54.5                          # 0.011 -> 0.017
    assert j["mcap"] == 17_000_000


def test_series_reports_the_newest_persisted_source():
    cl, c = _client()
    _mark(c, "4663", "0xmix", "2026-07-15T00:00:00Z", 1.0, source="zerox")
    _mark(c, "4663", "0xmix", "2026-07-15T00:01:00Z", 1.1,
          source="geckoterminal")

    j = cl.get("/api/memecoin/marks/4663/0xmix").get_json()

    assert j["source"] == "geckoterminal"
    assert [point["source"] for point in j["points"]] == ["zerox", "geckoterminal"]


def test_series_preserves_price_quality_without_guessing_from_source():
    cl, c = _client()
    try:
        for i, quality in enumerate(("exact_route", "live", None, "unrecognized")):
            _mark(c, "56", "0xmix", f"2026-07-15T00:0{i}:00Z", 1 + i / 10,
                  source="fastlane_rpc", quality=quality)
        points = cl.get("/api/memecoin/marks/56/0xmix").get_json()["points"]
        assert [point.get("quality") for point in points] == ["exact_route", "live", None, "unrecognized"]
        assert [point["source"] for point in points] == ["fastlane_rpc"] * 4
        assert [point["mid"] for point in points] == [1, 1.1, 1.2, 1.3]
    finally:
        c.close()


def test_verified_robinhood_tape_points_keep_market_quality_beside_exact_quotes(monkeypatch):
    from sniper.services import memecoin_evm_tape_marks as tape

    cl, c = _client()
    try:
        m.open_position(c, source_user_id="u", source_handle="trader", mint="0xmix", ticker="MIX",
                        entry_price=1, entry_notional=25, entry_fill_source="chain", entry_lag_s=0,
                        their_size_usd=100, execution_mode="real", network_id="4663",
                        opened_at="2026-07-15T00:00:00Z")
        _mark(c, "4663", "0xmix", "2026-07-15T00:00:01Z", .8,
              source="fastlane_rpc", quality="exact_route")
        monkeypatch.setattr(tape, "tape_series", lambda *_a, **_kw: [
            {"t": "2026-07-15T00:00:02Z", "mid": 1.1, "mcap": None, "source": tape.SOURCE},
            {"t": "2026-07-15T00:00:03Z", "mid": 1.2, "mcap": None, "source": tape.SOURCE},
        ])
        points = cl.get("/api/memecoin/marks/4663/0xmix").get_json()["points"]
        assert [point.get("quality") for point in points] == ["exact_route", "live", "live"]
        assert [point["source"] for point in points] == ["fastlane_rpc", tape.SOURCE, tape.SOURCE]
    finally:
        c.close()


def test_scoped_by_chain_and_mint_no_cross_contamination():
    cl, c = _client()
    _mark(c, "4663", "0xa", "2026-07-15T00:00:00Z", 1.0)
    _mark(c, "solana", "0xa", "2026-07-15T00:00:00Z", 999.0)          # same mint, other chain
    _mark(c, "4663", "0xb", "2026-07-15T00:00:00Z", 5.0)             # other mint
    pts = cl.get("/api/memecoin/marks/4663/0xa").get_json()["points"]
    assert [p["mid"] for p in pts] == [1.0]


def test_empty_series_is_an_honest_collecting_state_not_an_error():
    cl, c = _client()
    j = cl.get("/api/memecoin/marks/4663/0xnever").get_json()
    assert j["n"] == 0 and j["points"] == [] and j["last"] is None
    assert j["change_pct"] is None                                    # no fabricated 0%


def test_bad_quotes_are_excluded_from_the_chart():
    """A quarantined spike must never appear on the chart — it is not a real price."""
    cl, c = _client()
    _mark(c, "4663", "0xc", "2026-07-15T00:00:00Z", 1.0)
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
              "executable_exit_value, source, quality) VALUES('4663','0xc','2026-07-15T00:01:00Z',"
              "50.0,50.0,'geckoterminal','quarantined_spike')")
    c.commit()
    pts = cl.get("/api/memecoin/marks/4663/0xc").get_json()["points"]
    assert [p["mid"] for p in pts] == [1.0], "quarantined spike must be excluded"


def test_series_is_bounded_so_a_long_lived_coin_does_not_blow_the_payload():
    cl, c = _client()
    for i in range(1500):
        _mark(c, "4663", "0xd", "2026-07-15T%02d:%02d:00Z" % (i // 60, i % 60), 1.0 + i / 1000.0)
    j = cl.get("/api/memecoin/marks/4663/0xd").get_json()
    assert j["n"] <= 600, "payload must be capped"
    assert j["last"] == 1.0 + 1499 / 1000.0, "the most recent price must survive the cap"


def test_local_chart_refreshes_while_it_remains_open():
    """A chart opened at 0-1 marks must fill in without requiring close/reopen."""
    source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    start = source.index("async _renderLocalChart(")
    end = source.index("\n    async _", start + 20)
    renderer = source[start:end]

    assert "LOCAL_CHART_REFRESH_MS" in source
    assert "setTimeout" in renderer
    assert "box.isConnected" in renderer
