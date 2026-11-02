"""A per-token price reads as a market cap wherever the supply is known (operator, 2026-09-28:
"I don't want to see the mcap as 0.00000343 but as 34k or 344k").

Supply: the position's pinned total supply, else mcap / mid of ONE stored mark (the same read).
Never entry_mcap / their_price: those come from different reads on most lanes. Unknown supply
keeps the price, never a guess.
"""
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_telegram as tg

APP = Path("static/app.js").read_text()


def _method(name, end):
    return APP[APP.index(f"    {name}("):APP.index(f"    {end}(")]


def _node(script):
    node = shutil.which("node")
    assert node is not None
    return json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                     check=True).stdout)


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_the_display_supply_is_the_pinned_supply_else_one_marks_own_ratio():
    c = _ledger()
    assert m.position_display_supply(c, {"fdv_supply": 1e9, "mint": "M", "network_id": "solana"}) == 1e9
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, mcap) "
              "VALUES ('solana', 'M', '2026-09-28T09:00:00Z', 4e-5, 40000)")
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, mcap) "
              "VALUES ('solana', 'M', '2026-09-28T09:01:00Z', 5e-5, 45000)")      # the newest wins
    assert m.position_display_supply(c, {"mint": "M", "network_id": "solana"}) == 45000 / 5e-5


def test_the_source_buy_ratio_is_never_used():
    """entry_mcap and their_price come from different reads on most lanes (Alpha, 2026-09-28)."""
    c = _ledger()
    for unknown in ({}, None, {"mint": "M", "network_id": "solana", "entry_mcap": 32000, "their_price": 3.2e-5},
                    {"mint": "M", "fdv_supply": 0}):
        assert m.position_display_supply(c, unknown) is None
    assert m.position_display_supply(None, {"mint": "M", "entry_mcap": 1, "their_price": 1}) is None


def test_a_telegram_fill_reads_as_a_market_cap_when_the_supply_is_known():
    assert tg._notification_at(0.0000343, 1e9) == "$34K mcap"
    assert tg._notification_at(0.000344, 1e9) == "$344K mcap"
    assert tg._notification_at(0.0012, 1e9) == "$1.2M mcap"
    assert tg._notification_at(0.0000343, None) == "3.43e-05"          # unknown supply: the price
    assert tg._notification_at(None, 1e9) == "Unknown"


def test_the_card_fill_and_sale_lines_use_the_market_cap():
    base = {"network_id": "solana", "mint": "Mint1", "ticker": "FUND", "display_supply": 1e9}
    sold = tg.render_entry_notification(dict(base, message_kind="exit", status="sold",
        filled_notional=107.0, filled_price=0.0000566, reason="real_tp"))
    assert "Our sale: $107.00 @ $57K mcap" in sold, sold
    bought = tg.render_entry_notification(dict(base, status="filled", filled_notional=100.0,
        filled_price=0.0000343, origin_event_id="e1", source_handle="alice", position_id=7))
    assert "Our fill: $100.00 @ $34K mcap" in bought, bought


def test_the_worker_reads_the_supply_from_the_position():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="alice", mint="Mint1", ticker="FUND",
                          entry_price=0.0000566, entry_notional=100.0, entry_fill_source="chain",
                          entry_lag_s=0.5, their_size_usd=500.0, opened_at="2026-09-28T09:00:00Z",
                          strategy="alpha_b", execution_mode="real", network_id="solana")
    c.execute("UPDATE memecoin_positions SET fdv_supply=1e9 WHERE id=?", (pid,))
    c.commit()
    worker = SimpleNamespace(read_conn=c, write_conn=c, _source_intent=lambda view: None)
    text = tg._NotificationWorker._render(worker, {"message_kind": "exit", "status": "sold",
        "mint": "Mint1", "ticker": "FUND", "network_id": "solana", "position_id": pid,
        "filled_notional": 107.0, "filled_price": 0.0000566, "reason": "real_tp"})
    assert "@ $57K mcap" in text, text


def _ticker_row(p):
    body = (_method("_marketMidCell", "_mergeLastKnown") + _method("_realTickerRows", "_cardSig")
            + _method("_operatorQuote", "_marketSourceLabel") + _method("_marketSourceLabel", "_marketMidCell")
            + _method("_supplyOf", "_fmtAgo") + _method("_tokenPrice", "_operatorTime")
            + _method("_operatorPrice", "_tokenPrice"))
    script = ("const esc = s => String(s); global.window = { mcRange: require(process.cwd() + '/static/mc-range.js') };"
              " const view = { _operatorTime(v) { return v; }, _quoteAge(v) { return 'AGE'; },"
              " _ageTag(k, v) { return k; }, _operatorValueRows() { return ''; }, _realRangeBar() { return ''; },"
              " _realFactsLine() { return ''; }, " + body + "}; const p = " + json.dumps(p) + ";"
              " console.log(JSON.stringify([view._realTickerRows(p), view._marketMidCell(p.market_mid_price,"
              " 'market', 'fastlane_mid', 't', null, false, view._supplyOf(p))]));")
    return _node(script)


def test_the_open_card_shows_the_market_cap_and_keeps_the_price_on_hover():
    row, cell = _ticker_row({"market_mid_price": 0.0000343, "market_mid_basis": "market",
                             "market_mid_source": "fastlane_mid", "market_mid_at": "t", "target_supply": 1e9})
    assert "<span>Market cap</span>" in row and ">$34K</strong>" in row, row
    assert 'title="$0.0₄343 per token"' in row
    assert cell.startswith("$34K"), cell


def test_without_a_supply_the_card_keeps_the_price():
    row, cell = _ticker_row({"market_mid_price": 0.0000343, "market_mid_basis": "market",
                             "market_mid_source": "fastlane_mid", "market_mid_at": "t"})
    assert "<span>Market price</span>" in row and "$0.0₄343" in row, row
    assert cell.startswith("$0.0₄343"), cell


def test_the_display_supply_also_counts_when_the_bar_has_none():
    row, _cell = _ticker_row({"market_mid_price": 0.000344, "market_mid_basis": "market",
                              "market_mid_source": "fastlane_mid", "market_mid_at": "t",
                              "display_supply": 1e9})
    assert ">$344K</strong>" in row, row


def test_the_routes_carry_the_display_supply(monkeypatch, tmp_path):
    from test_memecoin_trade_names import _client, _no_dex
    from sniper.services import memecoin_coin_names as coin_names
    monkeypatch.setattr(coin_names, "_default_batch", _no_dex)
    cl, c = _client(monkeypatch, tmp_path)
    for mint, closed in (("MintOpen", False), ("MintClosed", True)):
        pid = m.open_position(c, source_user_id="u1", source_handle="alice", mint=mint, ticker="T",
                              entry_price=4e-5, entry_notional=100.0, entry_fill_source="chain",
                              entry_lag_s=0.5, their_size_usd=500.0, opened_at="2026-09-28T09:00:00Z",
                              strategy="alpha_b", execution_mode="real", network_id="solana")
        c.execute("UPDATE memecoin_positions SET their_price=3.2e-5, entry_mcap=32000 WHERE id=?", (pid,))
        m.pin_position_display_total_supply(c, pid, 1_000_000_000)
        c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, mcap) "
                  "VALUES ('solana', ?, '2026-09-28T09:01:00Z', 4e-5, 40000)", (mint,))
        c.commit()
        if closed:
            m.close_position(c, pid, "2026-09-28T09:05:00Z", realized_pnl=7.0)
    opened = cl.get("/api/memecoin/real/positions").get_json()["positions"][0]
    shut = cl.get("/api/memecoin/real/closed").get_json()["closed"][0]
    assert round(opened["display_supply"]) == round(shut["display_supply"]) == 1_000_000_000
