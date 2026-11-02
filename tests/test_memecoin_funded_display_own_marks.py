"""A funded position's card, chart and series show only our own marks (operator via Alpha, 2026-10-08).

Douyin 72229 (chain 56): the funded b_stop decided on the exact whole-balance preview, but the card's
market price, its chart and its peak read DexScreener mids. The mark tape is keyed (chain, mint) with
no position, and a paper twin on the same coin (72230) wrote DexScreener rows through the paper exit
sweep, so the funded card picked them up. Now:
- the funded EVM ticker's market price is our own (the 56 V2 reserves mark, the 4663 swap tape) or the
  last exact route price, labelled "route"; never a DexScreener row; none or stale shows unknown;
- the display peak comes from the executable peak (exact) or leaves the card;
- the chart, the post-mortem candles and the last-known price read only our own sources for a funded
  position;
- the paper sweep labels its rows `paper_dexscreener`, so no funded read can mistake them for ours.
Display only: no RPC, no rail, no runner path.
"""
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.settings import memecoin as cfg

MINT = "0x3ed24042e8e986943d9467eab5225123ac8d6666"
NOW = "2026-10-08T20:53:00Z"


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "nope_sol.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(tmp_path / "nope_evm.json"))
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: NOW)
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client(), c


def _mark(c, ts, mid, *, source, quality="live", chain="56", mint=MINT):
    c.execute("INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,executable_exit_value,"
              "source,quality) VALUES(?,?,?,?,?,?,?)", (chain, mint, ts, mid, mid, source, quality))
    c.commit()


def _position(c, *, peak=None, exec_peak=None, chain="56", mint=MINT, mode="real"):
    pid = m.open_position(
        c, source_user_id="u", source_handle="badabeepp", mint=mint, ticker="Douyin",
        entry_price=4.4978e-05, entry_notional=122.19, entry_fill_source="chain", entry_lag_s=0.0,
        their_size_usd=0.0, opened_at="2026-10-08T20:52:20Z", strategy="alpha_b",
        execution_mode=mode, network_id=chain)
    c.execute("UPDATE memecoin_positions SET peak_price=?, executable_peak_price=? WHERE id=?",
              (peak, exec_peak, pid))
    c.commit()
    return pid


def _ticker_row(client):
    rows = client.get("/api/memecoin/real/ticker").get_json()
    rows = rows.get("positions", rows) if isinstance(rows, dict) else rows
    return rows[0]


# --- the ticker (the Real card's 1 s price) ----------------------------------------------------------

def test_a_paper_dexscreener_row_never_becomes_the_funded_market_price(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    _mark(c, "2026-10-08T20:52:58Z", 4.144e-05, source="fastlane_rpc", quality="exact_route")
    _mark(c, "2026-10-08T20:52:59Z", 3.805e-05, source="dexscreener")      # the paper twin's row
    row = _ticker_row(client)
    assert row["market_mid_price"] == 4.144e-05 and row["market_mid_basis"] == "route", row
    assert row["market_mid_source"] != "dexscreener"


def test_no_own_mark_and_no_exact_price_is_unknown_not_dexscreener(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    _mark(c, "2026-10-08T20:52:59Z", 3.805e-05, source="dexscreener")
    row = _ticker_row(client)
    assert row["market_mid_price"] is None, row
    assert row["selected_exit_price"] is None, row


def test_a_stale_exact_price_is_never_replaced_by_a_fresh_dexscreener_mid(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    _mark(c, "2026-10-08T20:45:00Z", 4.144e-05, source="fastlane_rpc", quality="exact_route")
    _mark(c, "2026-10-08T20:52:59Z", 3.805e-05, source="dexscreener")
    row = _ticker_row(client)
    assert row["market_mid_price"] is None, row
    assert row["selected_exit_price"] != 3.805e-05, row


def test_the_display_peak_is_the_executable_peak_or_nothing(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _position(c, peak=6.0e-05, exec_peak=5.0584e-05)
    row = _ticker_row(client)
    assert round(row["max_gain_pct"], 2) == round((5.0584e-05 / 4.4978e-05 - 1) * 100, 2), row
    assert row["peak_basis"] == "exact"


def test_no_executable_peak_leaves_the_peak_off_the_card(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _position(c, peak=6.0e-05, exec_peak=None)
    assert _ticker_row(client)["max_gain_pct"] is None


# --- the chart, the candles, the last-known price ----------------------------------------------------

def test_the_inline_chart_of_a_held_coin_shows_only_our_own_marks(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    _mark(c, "2026-10-08T20:52:40Z", 4.53e-05, source="fastlane_rpc", quality="exact_route")
    _mark(c, "2026-10-08T20:52:51Z", 4.69e-05, source="dexscreener")
    _mark(c, "2026-10-08T20:52:52Z", 4.81e-05, source="fastlane_rpc", quality="exact_route")
    j = client.get("/api/memecoin/marks/56/%s" % MINT).get_json()
    assert {p["source"] for p in j["points"]} == {"fastlane_rpc"}, j["points"]


def test_the_chart_of_a_coin_we_do_not_hold_is_unchanged(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _mark(c, "2026-10-08T20:52:51Z", 4.69e-05, source="dexscreener")
    j = client.get("/api/memecoin/marks/56/%s" % MINT).get_json()
    assert [p["source"] for p in j["points"]] == ["dexscreener"]


def test_the_funded_post_mortem_candles_carry_no_dexscreener_series(monkeypatch, tmp_path):
    from sniper.services.memecoin_candles import candles_payload
    _client_, c = _client(monkeypatch, tmp_path)
    pid = _position(c)
    for i, (src, q) in enumerate([("dexscreener", "live"), ("fastlane_rpc", "exact_route")] * 3):
        _mark(c, "2026-10-08T20:5%d:%02dZ" % (2 + i // 2, 10 * i), 4.5e-05 + i * 1e-07, source=src, quality=q)
    out = candles_payload(c, chain_id="56", mint=MINT, interval_s=60, position_id=pid)
    assert out["candles"] == [], out["candles"]
    assert out["exec_candles"], "the exact series still draws"


def test_the_paper_post_mortem_candles_are_unchanged(monkeypatch, tmp_path):
    from sniper.services.memecoin_candles import candles_payload
    _client_, c = _client(monkeypatch, tmp_path)
    pid = _position(c, mode="paper")
    _mark(c, "2026-10-08T20:52:30Z", 4.5e-05, source="dexscreener")
    _mark(c, "2026-10-08T20:53:30Z", 4.6e-05, source="dexscreener")
    out = candles_payload(c, chain_id="56", mint=MINT, interval_s=60, position_id=pid)
    assert out["candles"], "a paper chart keeps its provider series"


def test_the_last_known_price_of_a_funded_row_ignores_dexscreener(monkeypatch, tmp_path):
    from sniper.routes.api_memecoin import _last_known_price
    _client_, c = _client(monkeypatch, tmp_path)
    _mark(c, "2026-10-08T20:52:59Z", 3.805e-05, source="dexscreener")
    p = {"mint": MINT, "quote_price": 4.5008e-05, "quote_observed_at": "2026-10-08T20:52:20Z",
         "entry_price": 4.4978e-05, "opened_at": "2026-10-08T20:52:20Z"}
    out = _last_known_price(c, p, "56", priced=False)
    assert out["last_known_source"] == "entry quote" and out["last_known_price"] == 4.5008e-05, out


# --- the paper sweep labels its rows -----------------------------------------------------------------

def test_the_paper_sweep_writes_its_marks_as_paper_dexscreener(monkeypatch, tmp_path):
    from sniper.services import memecoin_exits as EX
    _client_, c = _client(monkeypatch, tmp_path)
    EX.sweep_exits(c, {("56", MINT): {"price": 4.69e-05, "liquidity": 25_000.0}}, "2026-10-08T20:52:51+00:00")
    (source,) = c.execute("SELECT source FROM memecoin_position_marks WHERE mint=?", (MINT,)).fetchone()
    assert source == "paper_dexscreener"
