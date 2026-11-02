import json
import sqlite3

from sniper.persistence.db_memecoin import ensure_memecoin_tables
import pytest

from sniper.analysis.memecoin_x.fomo_adapter import (
    FomoBuyPage,
    fetch_eligible_fomo_buy_page,
    fetch_eligible_fomo_buys,
)


SOL = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"
EVM = "0xABCdef0000000000000000000000000000000005"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_tables(conn)
    return conn


def _insert(conn, event_id, *, side="buy", approved=1, version=2,
            clock="observed_real_utc", network="solana", mint=SOL,
            user_id="u1", handle="zinceth"):
    conn.execute("""
        INSERT INTO memecoin_feed_observations_v2
            (upstream_event_id, raw_type, normalized_side, source_user_id, source_handle,
             network_id, mint, observed_at, was_curated, was_real_approved, raw_json,
             capture_version, clock_domain)
        VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)
    """, (event_id, "swap", side, user_id, handle, network, mint,
          "2026-07-13T12:00:00+00:00", approved, json.dumps({"id": event_id}),
          version, clock))
    conn.commit()


def test_fetch_eligible_fomo_buys_filters_boundary_and_preserves_network_identity():
    conn = _conn()
    _insert(conn, "sol")
    _insert(conn, "hood", network="4663", mint=EVM)
    _insert(conn, "base", network="8453", mint=EVM)
    _insert(conn, "legacy", version=1)
    _insert(conn, "sell", side="sell")
    _insert(conn, "unapproved", approved=0)
    _insert(conn, "bad-clock", clock="legacy_unknown")
    _insert(conn, "no-mint", mint=None)
    _insert(conn, "no-user", user_id=None)
    _insert(conn, "no-handle", handle=None)

    rows = fetch_eligible_fomo_buys(conn)
    assert [row["upstream_event_id"] for row in rows] == ["sol", "hood", "base"]
    by_id = {row["upstream_event_id"]: row for row in rows}
    assert by_id["sol"]["mint"] == SOL
    assert by_id["hood"]["network_id"] == "4663"
    assert by_id["base"]["network_id"] == "8453"
    assert by_id["hood"]["mint"] == EVM.lower()
    assert all(row["capture_version"] == 2 for row in rows)
    assert all(row["normalized_side"] == "buy" for row in rows)
    assert all(row["was_real_approved"] == 1 for row in rows)


def test_fetch_eligible_fomo_buys_supports_id_watermark_and_limit():
    conn = _conn()
    _insert(conn, "one")
    _insert(conn, "two")
    _insert(conn, "three")
    first = fetch_eligible_fomo_buys(conn, limit=2)
    assert [row["upstream_event_id"] for row in first] == ["one", "two"]
    second = fetch_eligible_fomo_buys(conn, after_id=first[-1]["id"], limit=2)
    assert [row["upstream_event_id"] for row in second] == ["three"]


def test_fetch_eligible_fomo_buys_rejects_blank_identity_and_wrong_chain_address():
    conn = _conn()
    _insert(conn, "blank-event", user_id=" ")
    _insert(conn, "blank-handle", handle="\t")
    _insert(conn, "blank-network", network=" ")
    _insert(conn, "blank-mint", mint=" ")
    _insert(conn, "bad-sol", network="solana", mint=EVM)
    _insert(conn, "bad-evm", network="4663", mint=SOL)
    _insert(conn, "valid", user_id=" u1 ", handle=" zinceth ", network=" 4663 ", mint=EVM)

    rows = fetch_eligible_fomo_buys(conn)
    assert [row["upstream_event_id"] for row in rows] == ["valid"]
    assert rows[0]["source_user_id"] == "u1"
    assert rows[0]["source_handle"] == "zinceth"
    assert rows[0]["network_id"] == "4663"
    assert rows[0]["mint"] == EVM.lower()


def test_page_advances_through_invalid_rows_before_python_validation():
    conn = _conn()
    _insert(conn, "valid-one")
    _insert(conn, "invalid-address", network="4663", mint=SOL)
    _insert(conn, "valid-two", network="4663", mint=EVM)
    source_ids = [row[0] for row in conn.execute(
        "SELECT id FROM memecoin_feed_observations_v2 ORDER BY id")]

    first = fetch_eligible_fomo_buy_page(conn, limit=2)
    assert isinstance(first, FomoBuyPage)
    assert isinstance(first.buys, tuple)
    assert [row["upstream_event_id"] for row in first.buys] == ["valid-one"]
    assert first.source_rows == 2
    assert first.through_id == source_ids[1]

    second = fetch_eligible_fomo_buy_page(conn, after_id=first.through_id, limit=2)
    assert [row["upstream_event_id"] for row in second.buys] == ["valid-two"]
    assert second.through_id == source_ids[2]
    assert second.source_rows == 1


def test_empty_page_preserves_cursor_and_limits_are_bounded():
    conn = _conn()
    assert fetch_eligible_fomo_buy_page(conn, after_id=77) == FomoBuyPage(
        buys=(), through_id=77, source_rows=0)
    for limit in (0, 10001):
        with pytest.raises(ValueError, match="limit must be between 1 and 10000"):
            fetch_eligible_fomo_buy_page(conn, limit=limit)
