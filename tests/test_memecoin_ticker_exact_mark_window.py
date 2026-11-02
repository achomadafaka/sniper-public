"""The ticker bounds its exact-route mark lookup to the same 300s window it already enforces.

`quality` is not in `idx_mc_marks_uniq (chain_id, mint, sweep_ts)`. An unbounded
`quality='exact_route' ORDER BY sweep_ts DESC LIMIT 1` therefore walked every mark for the mint
backwards when the recent tape carried no exact row -- 46ms on a 137k-mark coin, four times a
second for each open SSE tab.

Both consumers of that row (`exact_is_fresh` in `_real_ticker_payload` and `_select_display_mark`)
already reject an exact mark older than `_MEASURED_MARK_MAX_AGE_S`, so the floor cannot change what
the operator reads. These tests pin that equivalence at both sides of the boundary.
"""
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.settings import memecoin as cfg

NOW = "2026-08-12T16:53:23Z"


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "nope_sol.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(tmp_path / "nope_evm.json"))
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: NOW)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="BACKED", ticker="BACKED",
        entry_price=0.0003, entry_notional=25.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at="2026-08-12T16:46:14Z", strategy="alpha_b",
        execution_mode="real", network_id="4663", entry_mcap=638_160.37,
    )
    return app.test_client(), c


def _mark(c, *, ts, mid, quality):
    c.execute(
        "INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality)"
        " VALUES('4663','BACKED',?,?,'fastlane_rpc',?)",
        (ts, mid, quality),
    )
    c.commit()


def _row(client):
    rows = client.get("/api/memecoin/real/ticker").get_json()
    rows = rows.get("positions", rows) if isinstance(rows, dict) else rows
    assert rows, "the ticker returned no rows for an open real position"
    return rows[0]


def test_an_exact_mark_inside_the_window_still_prices_the_row(monkeypatch, tmp_path):
    """299s old -- inside 300s. The floor must not drop it."""
    client, c = _client(monkeypatch, tmp_path)
    _mark(c, ts="2026-08-12T16:48:24Z", mid=0.00041, quality="exact_route")
    _mark(c, ts="2026-08-12T16:53:22Z", mid=0.00030, quality="live")

    row = _row(client)
    assert row["mark_basis"] == "measured_route"
    assert row["last_quote_price"] == 0.00041


def test_an_exact_mark_past_the_window_is_ignored_exactly_as_before(monkeypatch, tmp_path):
    """301s old -- outside 300s. Both the old scan and the bounded seek reject it."""
    client, c = _client(monkeypatch, tmp_path)
    _mark(c, ts="2026-08-12T16:48:22Z", mid=0.00041, quality="exact_route")
    _mark(c, ts="2026-08-12T16:53:22Z", mid=0.00030, quality="live")

    row = _row(client)
    assert row["mark_basis"] != "measured_route"
    # 2026-10-08: a non-exact row never prices a funded EVM card: the price is unknown and the card
    # shows the newest of our own marks it knew, with its time.
    assert row["last_quote_price"] is None
    assert (row["last_known_price"], row["last_known_at"]) == (0.00030, "2026-08-12T16:53:22Z")


def test_no_exact_mark_at_all_leaves_the_mid_path_intact(monkeypatch, tmp_path):
    """The scan case the floor exists to kill: a long tape with no exact row anywhere."""
    client, c = _client(monkeypatch, tmp_path)
    for i in range(50):
        _mark(c, ts=f"2026-08-12T15:{i:02d}:00Z", mid=0.0005, quality="live")
    _mark(c, ts="2026-08-12T16:53:22Z", mid=0.00030, quality="live")

    row = _row(client)
    assert row["mark_basis"] != "measured_route"
    # 2026-10-08: with no exact price the funded EVM card is unknown, never the provider mid.
    assert row["last_quote_price"] is None
