"""`/api/memecoin/real/ticker` resolves each mark field from the freshest row that CARRIES it.

The mark tape interleaves sources. `fastlane_rpc` writes an exact_route `mid` about once a second
with `mcap` and `liquidity` both NULL; the `web` source writes all three, seconds apart. Reading one
`ORDER BY sweep_ts DESC LIMIT 1` row therefore discarded a measured mcap that was seconds old and
fell through to the entry-anchored estimate.

Live example 2026-08-12T16:53Z, funded position 36386 (BACKED, chain 4663): the `web` mark measured
mcap 322,010 while the ticker showed 609,720, derived from `entry_mcap` 638,160. That is a 1.9x
error and it persists for the life of the position, because `entry_mcap` never updates.

mcap is price x supply, so the carried value is the measured SUPPLY valued at the current mid --
carrying the mcap number itself would report an old price. `entry_mcap / entry` is that same ratio
measured once at entry and never refreshed, and on 36386 it implied 1.97e9 supply against the 9.8e8
the live marks imply. That ratio error IS the 1.9x.

`liquidity` had the identical staleness shape and it is the worse of the two: it feeds
`modeled_fill`, so it moves the PRICE the operator reads, not just a label.
"""
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.settings import memecoin as cfg


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "nope_sol.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(tmp_path / "nope_evm.json"))
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client(), c


def _mark(c, *, mint, ts, mid, mcap=None, liquidity=None, source="fastlane_rpc"):
    c.execute(
        "INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,mcap,liquidity,source,quality)"
        " VALUES(?,?,?,?,?,?,?,'live')",
        ("4663", mint, ts, mid, mcap, liquidity, source),
    )
    c.commit()


def _position(c):
    return m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="BACKED", ticker="BACKED",
        entry_price=0.00032317989124595246, entry_notional=25.0, entry_fill_source="chain",
        entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-08-12T16:46:14Z",
        strategy="alpha_b", execution_mode="real", network_id="4663", entry_mcap=638_160.37,
    )


def _row(client):
    res = client.get("/api/memecoin/real/ticker")
    assert res.status_code == 200
    rows = res.get_json()
    rows = rows.get("positions", rows) if isinstance(rows, dict) else rows
    assert rows, "the ticker returned no rows for an open real position"
    return rows[0]


def test_a_measured_mcap_beats_the_entry_anchored_estimate(monkeypatch, tmp_path):
    """The freshest row has no mcap; a `web` row two seconds older measured one. Use the measurement."""
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso",
                        lambda: "2026-08-12T16:53:23Z")
    _mark(c, mint="BACKED", ts="2026-08-12T16:53:20Z", mid=0.000328481352282214,
          mcap=322_010.13, liquidity=94_639.51, source="web")
    _exact(c, mint="BACKED", ts="2026-08-12T16:53:22Z", mid=0.00030877717715540264)
    # The current price is our own exact route price (2026-10-08: no provider row prices a funded
    # EVM card); the provider row still supplies the measured supply and the liquidity.
    row = _row(client)
    assert round(row["market_cap"]) == 302_694          # measured SUPPLY at the current price
    assert round(row["liquidity"]) == 94_640


def test_a_stale_measurement_does_not_fall_back_to_the_entry_ratio(monkeypatch, tmp_path):
    """The source market cap over our fill price is not a measured supply."""
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso",
                        lambda: "2026-08-12T16:53:23Z")
    _mark(c, mint="BACKED", ts="2026-08-12T15:00:00Z", mid=0.00032, mcap=322_010.13,
          liquidity=94_639.51, source="web")
    _mark(c, mint="BACKED", ts="2026-08-12T16:53:22Z", mid=0.00030877717715540264)

    row = _row(client)
    assert row["market_cap"] is None
    assert row["target_supply"] == 638_160.37 / 0.00032317989124595246


def test_no_mark_carries_the_field_at_all(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso",
                        lambda: "2026-08-12T16:53:23Z")
    _mark(c, mint="BACKED", ts="2026-08-12T16:53:22Z", mid=0.00030877717715540264)

    row = _row(client)
    assert row["market_cap"] is None


def test_an_exact_route_mark_is_not_charged_the_exit_twice(monkeypatch, tmp_path):
    """`fastlane_rpc` divides the position's own full-balance `preview_exit_fn` by the open
    quantity, so its impact is MEASURED. Position 36325 read liquidity $9.49, which pins
    `modeled_fill` to its 50% ceiling and halved an exact 4.31e-05 to 2.16e-05 -- the operator
    saw -67% on a position the route said was -34%."""
    client, c = _client(monkeypatch, tmp_path)
    m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="THIN", ticker="THIN",
        entry_price=6.534516548756555e-05, entry_notional=25.0, entry_fill_source="chain",
        entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-08-12T16:18:13Z",
        strategy="alpha_b", execution_mode="real", network_id="4663",
    )
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    c.execute(
        "INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,mcap,liquidity,source,quality)"
        " VALUES('4663','THIN','2026-08-12T16:53:20Z',3.714033e-05,NULL,9.4885,'web','live'),"
        "       ('4663','THIN','2026-08-12T16:53:22Z',4.314789e-05,NULL,NULL,'fastlane_rpc','exact_route')")
    c.commit()

    row = _row(client)
    assert row["last_quote_price"] == 4.314789e-05      # the exact price, not half of it
    assert row["liquidity"] == 9.4885                   # still reported, it is real evidence
    # the label describes THIS price, in the same update
    assert row["mark_basis"] == "measured_route"
    assert row["mark_observed_at"] == "2026-08-12T16:53:22Z"


def test_a_ticker_update_carries_the_basis_and_observation_time_of_its_own_price(
        monkeypatch, tmp_path):
    """The client merges a ticker update over the row it already holds. Shipping the price alone
    left the full poll's `mark_basis` and age standing over a DIFFERENT source's number -- a tape
    mid rendered under a `measured_route` label. Price, basis and observed_at travel together."""
    client, c = _client(monkeypatch, tmp_path)
    pid = _position(c)
    m.insert_fill(c, position_id=pid, kind="entry", price=0.00032317989124595246,
                  qty=77_357.0, notional=25.0, fill_source="chain", fraction=1.0,
                  created_at="2026-08-12T16:46:14Z")
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    _mark(c, mint="BACKED", ts="2026-08-12T16:53:20Z", mid=0.000328481352282214,
          mcap=322_010.13, liquidity=94_639.51, source="web")

    row = _row(client)
    # 2026-10-08: a provider row alone never prices a funded EVM card, so there is no price and no
    # observation time to carry; the basis still travels with what is (not) shown.
    assert row["last_quote_price"] is None
    assert row["mark_basis"] == "mid"
    assert row["mark_observed_at"] is None and row["mark_age_s"] is None


def test_an_unmarked_price_reports_an_unknown_observation_time(monkeypatch, tmp_path):
    """`last_quote_ts` is stamped with the exit sweep's own now_iso, not an observation time, so
    it reads ~0s no matter how old the price is. Falling back to it would assert freshness the
    row does not have; unknown is the honest answer."""
    client, c = _client(monkeypatch, tmp_path)
    pid = _position(c)
    m.update_position_last_quote(c, pid, 0.00031, "2026-08-12T16:53:23Z")
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")

    row = _row(client)
    assert row["last_quote_price"] == 0.00031            # the price still renders
    assert row["mark_observed_at"] is None               # but it is NOT dated by the sweep stamp


def test_a_reused_exact_runner_price_keeps_its_exact_observation_time(monkeypatch, tmp_path):
    """A new sweep can reuse an old exact price. The ticker must not call it a new quote."""
    client, c = _client(monkeypatch, tmp_path)
    pid = _position(c)
    c.execute(
        "UPDATE memecoin_positions SET last_quote_price=?, last_quote_ts=?, "
        "last_executable_price=?, last_executable_at=? WHERE id=?",
        (0.00031, "2026-08-12T16:53:23Z", 0.00031, "2026-08-12T16:48:00Z", pid),
    )
    c.commit()
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")

    row = _row(client)

    assert row["runner_decision_at"] == "2026-08-12T16:48:00Z"
    assert row["last_quote_ts"] == "2026-08-12T16:48:00Z"


def test_ticker_prefers_a_fresh_exact_route_over_a_newer_mid(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    pid = _position(c)
    m.insert_fill(c, position_id=pid, kind="entry", price=0.00032, qty=100_000,
                  notional=32.0, fill_source="chain", fraction=1.0,
                  created_at="2026-08-12T16:50:00Z")
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso",
                        lambda: "2026-08-12T16:53:23Z")
    c.execute(
        "INSERT INTO memecoin_position_marks"
        "(chain_id,mint,sweep_ts,mid,liquidity,source,quality) VALUES"
        "('4663','BACKED','2026-08-12T16:53:20Z',0.00030,NULL,'fastlane_rpc','exact_route'),"
        "('4663','BACKED','2026-08-12T16:53:22Z',0.00040,100000,'web','live')"
    )
    c.commit()

    row = _row(client)

    assert row["last_quote_price"] == 0.00030
    # 2026-10-08: the newer provider row is not the funded market price; the route price is.
    assert (row["market_mid_price"], row["market_mid_basis"]) == (0.00030, "route")
    assert row["market_mid_at"] == "2026-08-12T16:53:20Z"
    assert row["mark_basis"] == "measured_route"
    assert row["mark_observed_at"] == "2026-08-12T16:53:20Z"
    assert row["mark_age_s"] == 3.0


def test_ticker_rejects_an_exact_route_past_the_shared_age_limit(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    pid = _position(c)
    m.insert_fill(c, position_id=pid, kind="entry", price=0.00032, qty=100_000,
                  notional=32.0, fill_source="chain", fraction=1.0,
                  created_at="2026-08-12T16:00:00Z")
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso",
                        lambda: "2026-08-12T16:53:23Z")
    c.execute(
        "INSERT INTO memecoin_position_marks"
        "(chain_id,mint,sweep_ts,mid,liquidity,source,quality) VALUES"
        "('4663','BACKED','2026-08-12T16:44:59Z',0.00040,100000,'web','live'),"
        "('4663','BACKED','2026-08-12T16:45:00Z',0.00030,NULL,'fastlane_rpc','exact_route')"
    )
    c.commit()

    row = _row(client)

    # 2026-10-08: past the window the funded EVM price is UNKNOWN, and the card says what it last
    # knew from our own marks and when; the provider mid never replaces it.
    assert row["last_quote_price"] is None and row["market_mid_price"] is None
    assert (row["last_known_price"], row["last_known_at"], row["last_known_source"]) == (
        0.00030, "2026-08-12T16:45:00Z", "fastlane_rpc")


def _exact(c, *, mint, ts, mid, chain="4663"):
    c.execute(
        "INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality)"
        " VALUES(?,?,?,?,'fastlane_rpc','exact_route')", (chain, mint, ts, mid))
    c.commit()


def test_an_exact_only_tape_shows_the_route_price_as_a_labelled_market_price(monkeypatch, tmp_path):
    """Alpha 2026-09-17: on 5042 and 56 every sample, and on 4663 60%, the tape held only exact_route
    marks, so Market price and Market cap read "—" beside a fresh route price and a known supply."""
    client, c = _client(monkeypatch, tmp_path)
    pid = _position(c)
    supply = 1_000_000_000
    m.pin_position_display_total_supply(c, pid, supply)
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    _exact(c, mint="BACKED", ts="2026-08-12T16:53:20Z", mid=0.0003)

    row = _row(client)
    assert (row["market_mid_price"], row["market_mid_basis"]) == (0.0003, "route")
    assert row["market_mid_at"] == "2026-08-12T16:53:20Z"
    assert round(row["market_cap"]) == round(0.0003 * supply)


def test_a_market_mark_keeps_the_market_basis_over_a_fresher_route_price(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    _mark(c, mint="BACKED", ts="2026-08-12T16:53:10Z", mid=0.00031, source="dexscreener")
    _exact(c, mint="BACKED", ts="2026-08-12T16:53:22Z", mid=0.0003)

    row = _row(client)
    # 2026-10-08: a DexScreener row is no market mark for a funded EVM card; our own market marks
    # (the 4663 tape, the 56 V2 reserves) keep the market basis in their own tests.
    assert (row["market_mid_price"], row["market_mid_basis"]) == (0.0003, "route")


def _live_mark(monkeypatch, *, price, observed_at, source="fastlane_sol_swap"):
    """The runner's published live display mark for SOLCAT (sniper/services/memecoin_live_marks.py)."""
    monkeypatch.setattr("sniper.routes.api_memecoin._live_marks", lambda now: {"solana:SOLCAT": {
        "price_usd": price, "observed_at": observed_at, "source": source, "healthy": True,
        "checked_at": "2026-08-12T16:53:23Z"}})


def test_an_unbounded_market_mark_read_takes_the_newest_instant_not_the_last_spelling(monkeypatch, tmp_path):
    """DR-03a: the EVM market-mark read has no window, and text order put a whole-second stamp
    above a fractional one from the same second."""
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    # 2026-10-08: the EVM market read is now the exact read; the same spelling trap applies to it.
    _exact(c, mint="BACKED", ts="2026-08-12T16:53:22Z", mid=0.00030)
    _exact(c, mint="BACKED", ts="2026-08-12T16:53:22.600000Z", mid=0.00031)

    row = _row(client)
    assert (row["market_mid_price"], row["market_mid_at"]) == (0.00031, "2026-08-12T16:53:22.600000Z")


def test_solana_card_shows_the_live_mark_over_a_newer_provider_mid_and_the_tape(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="SOLCAT", ticker="SOLCAT",
        entry_price=0.0001, entry_notional=25.0, entry_fill_source="chain",
        entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-08-12T16:46:14Z",
        strategy="alpha_b", execution_mode="real", network_id="solana",
    )
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    c.execute(
        "INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality) VALUES"
        "('solana','SOLCAT','2026-08-12T16:53:22Z',0.00012,'fastlane_sol_swap','live'),"
        "('solana','SOLCAT','2026-08-12T16:53:22.500000Z',0.00010,'fastlane_mid','live')"
    )
    c.commit()
    _live_mark(monkeypatch, price=0.00011, observed_at="2026-08-12T16:53:21Z")

    row = _row(client)
    assert row["market_mid_price"] == 0.00011
    assert row["market_mid_at"] == "2026-08-12T16:53:21Z"
    assert row["market_mid_source"] == "fastlane_sol_swap"


def test_bsc_card_uses_fresh_verified_pool_mid_and_keeps_route_separate(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    pos = m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="0x" + "aa" * 20,
        ticker="BSC", entry_price=0.00001, entry_notional=25.0, entry_fill_source="chain",
        entry_lag_s=0.1, their_size_usd=100, opened_at="2026-08-12T16:46:14Z",
        strategy="alpha_b", execution_mode="real", network_id="56")
    c.execute("UPDATE memecoin_positions SET executed_pool=? WHERE id=?",
              ("0x" + "cc" * 20, pos))
    c.execute("INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality) "
              "VALUES(?,?,?,?,?,?)", ("56", "0x" + "aa" * 20,
                                      "2026-08-12T16:53:22Z", 0.00001,
                                      "fastlane_rpc", "exact_route"))
    c.commit()
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    monkeypatch.setattr("sniper.services.memecoin_bsc_v2_live.read_marks",
                        lambda positions, now: {pos: {"price": 0.00002,
                            "observed_at": now - 0.25, "source": "bsc_v2_sync"}})
    row = _row(client)
    assert row["market_mid_price"] == 0.00002
    assert row["market_mid_source"] == "bsc_v2_sync"
    assert row["selected_exit_price"] == 0.00001


def test_bsc_card_does_not_call_an_old_provider_mark_live(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    m.open_position(c, source_user_id="u", source_handle="(real)", mint="BSC",
                    ticker="BSC", entry_price=0.00001, entry_notional=25.0,
                    entry_fill_source="chain", entry_lag_s=0.1, their_size_usd=100,
                    opened_at="2026-08-12T16:46:14Z", strategy="alpha_b",
                    execution_mode="real", network_id="56")
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    c.execute("INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality) "
              "VALUES('56','BSC','2026-08-12T16:51:00Z',0.00002,'web','live'),"
              "('56','BSC','2026-08-12T16:53:22Z',0.00001,'fastlane_rpc','exact_route')")
    c.commit()
    row = _row(client)
    assert row["market_mid_basis"] == "route"
    assert row["market_mid_price"] == 0.00001


def test_solana_card_shows_no_tape_swap_old_quarantined_or_fresh(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="SOLCAT", ticker="SOLCAT",
        entry_price=0.0001, entry_notional=25.0, entry_fill_source="chain",
        entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-08-12T16:46:14Z",
        strategy="alpha_b", execution_mode="real", network_id="solana",
    )
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    c.execute(
        "INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality) VALUES"
        "('solana','SOLCAT','2026-08-12T16:53:10Z',0.00012,'fastlane_sol_swap','live'),"
        "('solana','SOLCAT','2026-08-12T16:53:21Z',0.00013,'fastlane_sol_swap','quarantined_spike'),"
        "('solana','SOLCAT','2026-08-12T16:53:22.500000Z',0.00014,'fastlane_sol_swap','live'),"
        "('solana','SOLCAT','2026-08-12T16:53:22Z',0.00010,'fastlane_mid','live')"
    )
    c.commit()

    row = _row(client)
    # No tape swap is shown, not even a fresh one: the fastlane values it with DexScreener's SOL/USD
    # (DR-04). Neither is the DexScreener mid (operator 2026-09-27: no DexScreener on the screen).
    # With no live mark and no route price there is no market price.
    assert row["market_mid_price"] is None
    assert row["market_mid_source"] is None


def test_solana_card_rejects_a_future_dated_live_mark(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="SOLCAT", ticker="SOLCAT",
        entry_price=0.0001, entry_notional=25.0, entry_fill_source="chain",
        entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-08-12T16:46:14Z",
        strategy="alpha_b", execution_mode="real", network_id="solana",
    )
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    c.execute(
        "INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,source,quality) VALUES"
        "('solana','SOLCAT','2026-08-12T16:53:22Z',0.00010,'fastlane_mid','live')"
    )
    c.commit()
    _live_mark(monkeypatch, price=0.00012, observed_at="2026-08-12T16:53:25Z")   # 2 s after now

    row = _row(client)
    # The future-dated live mark is refused, and the DexScreener mid is not a Solana market price.
    assert row["market_mid_price"] is None
    assert row["market_mid_source"] is None


def test_a_stale_route_price_is_not_shown_as_the_market_price(monkeypatch, tmp_path):
    client, c = _client(monkeypatch, tmp_path)
    _position(c)
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T17:10:00Z")
    _exact(c, mint="BACKED", ts="2026-08-12T16:53:20Z", mid=0.0003)      # 16 min old
    # The runner's stored exact quote is read without the tape's 300 s floor. It is stale too.
    c.execute("UPDATE memecoin_positions SET last_executable_price=0.00029, "
              "last_executable_at='2026-08-12T16:54:00Z'")
    c.commit()

    row = _row(client)
    assert (row["market_mid_price"], row["market_mid_basis"], row["market_cap"]) == (None, None, None)


def test_feed_supply_keeps_the_legacy_target_basis_without_claiming_a_display_cap(monkeypatch, tmp_path):
    """Feed fill price and market cap need not be from the same observation."""
    client, c = _client(monkeypatch, tmp_path)
    m.open_position(
        c, source_user_id="u", source_handle="(real)", mint="0xabc", ticker="BSC",
        entry_price=0.0001, entry_notional=25.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at="2026-08-12T16:46:14Z", strategy="alpha_b",
        execution_mode="real", network_id="56", entry_mcap=None)
    c.execute("INSERT INTO memecoin_events(fomo_event_id,mint,network_id,side,their_price,their_market_cap)"
              " VALUES('e1','0xabc','56','buy',0.0002,200000.0)")
    c.commit()
    monkeypatch.setattr("sniper.routes.api_memecoin._now_iso", lambda: "2026-08-12T16:53:23Z")
    _exact(c, mint="0xabc", ts="2026-08-12T16:53:20Z", mid=0.0003, chain="56")

    row = _row(client)
    assert (row["market_mid_price"], row["market_mid_basis"]) == (0.0003, "route")
    assert row["market_cap"] is None
    assert row["target_supply"] == 1_000_000_000
