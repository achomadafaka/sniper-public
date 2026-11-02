"""Acceptance suite for `tag_wallet_pnl` — B1 API source switch.

Spec: docs/superpowers/specs/2026-04-25-tag-wallet-pnl-api-source-switch.md

Tests encode contracts for:
- sniper.analysis.tag_wallet_pnl.compute_wallet_tag_pnl (API-source path)
- sniper.analysis.tag_wallet_pnl.fetch_closed_positions
- held_to_resolution derivation rule (§4.1)
- avg_hold_days column removal migration (§4.2)
- db.wallet_tag_pnl_rows reader compatibility (§7.4)
- B2 backward compat (§8.2 #10)
"""

from __future__ import annotations

import importlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests


WALLET_A = "0xaaa0000000000000000000000000000000000001"
WALLET_B = "0xbbb0000000000000000000000000000000000002"
T0 = 1_700_000_000  # arbitrary anchor (unix epoch seconds)

# Fixture path for real-data round-trip
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "polymarket_closed_positions_sample.json"

# Cids from the real fixture (load-bearing for round-trip test)
_FIXTURE_DATA = json.loads(FIXTURE_PATH.read_text())
FIXTURE_CID_0 = _FIXTURE_DATA[0]["conditionId"]
FIXTURE_CID_1 = _FIXTURE_DATA[1]["conditionId"]
FIXTURE_CID_2 = _FIXTURE_DATA[2]["conditionId"]
FIXTURE_CID_3 = _FIXTURE_DATA[3]["conditionId"]
FIXTURE_CID_4 = _FIXTURE_DATA[4]["conditionId"]
FIXTURE_WALLET = _FIXTURE_DATA[0]["proxyWallet"]


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    """In-memory sqlite with the three tables needed by tag_wallet_pnl.

    Column names and types verified via:
      python3 scripts/brain_index.py schema followed_wallet_activity
      python3 scripts/brain_index.py schema market_tags
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE followed_wallet_activity (
            activity_key       TEXT PRIMARY KEY,
            wallet             TEXT NOT NULL,
            transaction_hash   TEXT NOT NULL DEFAULT '',
            condition_id       TEXT NOT NULL DEFAULT '',
            activity_type      TEXT NOT NULL DEFAULT '',
            side               TEXT NOT NULL DEFAULT '',
            outcome            TEXT NOT NULL DEFAULT '',
            title              TEXT NOT NULL DEFAULT '',
            slug               TEXT NOT NULL DEFAULT '',
            event_slug         TEXT NOT NULL DEFAULT '',
            price              REAL NOT NULL DEFAULT 0,
            size               REAL NOT NULL DEFAULT 0,
            usdc_size          REAL NOT NULL DEFAULT 0,
            activity_ts        INTEGER NOT NULL DEFAULT 0,
            source_endpoint    TEXT NOT NULL DEFAULT 'activity',
            raw_json           TEXT NOT NULL DEFAULT '{}',
            inserted_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE market_tags (
            condition_id TEXT NOT NULL,
            tag          TEXT NOT NULL,
            tag_label    TEXT,
            ingested_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (condition_id, tag)
        )
        """
    )
    return conn


def _make_conn_with_wallet_tag_pnl() -> sqlite3.Connection:
    """Like _make_conn but also includes wallet_tag_pnl WITHOUT avg_hold_days
    (post-migration shape)."""
    conn = _make_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_tag_pnl (
            wallet                    TEXT    NOT NULL,
            tag                       TEXT    NOT NULL,
            n_positions               INTEGER NOT NULL,
            realized_pnl              REAL    NOT NULL,
            win_rate                  REAL    NOT NULL,
            conviction_concentration  REAL    NOT NULL,
            computed_at               INTEGER NOT NULL,
            win_rate_held_only        REAL,
            n_held_to_resolution      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (wallet, tag)
        )
        """
    )
    return conn


def _make_conn_with_legacy_wallet_tag_pnl() -> sqlite3.Connection:
    """Like _make_conn but wallet_tag_pnl has avg_hold_days (pre-migration shape)."""
    conn = _make_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_tag_pnl (
            wallet                    TEXT    NOT NULL,
            tag                       TEXT    NOT NULL,
            n_positions               INTEGER NOT NULL,
            realized_pnl              REAL    NOT NULL,
            win_rate                  REAL    NOT NULL,
            avg_hold_days             REAL    NOT NULL,
            conviction_concentration  REAL    NOT NULL,
            computed_at               INTEGER NOT NULL,
            PRIMARY KEY (wallet, tag)
        )
        """
    )
    return conn


def _insert_fwa(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    activity_type: str,
    side: str = "",
    outcome: str = "Yes",
    price: float = 0.5,
    size: float = 10.0,
    activity_ts: int = T0,
    key_suffix: str = "",
) -> None:
    """Insert a followed_wallet_activity row using production column names."""
    activity_key = f"{wallet}:{condition_id}:{outcome}:{activity_type}:{side}:{activity_ts}:{key_suffix}"
    conn.execute(
        """
        INSERT INTO followed_wallet_activity (
            activity_key, wallet, condition_id, activity_type, side, outcome,
            price, size, usdc_size, activity_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            activity_key, wallet, condition_id, activity_type, side, outcome,
            float(price), float(size), float(price * size), int(activity_ts),
        ),
    )


def _insert_tag(conn: sqlite3.Connection, cid: str, tag: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO market_tags (condition_id, tag) VALUES (?,?)",
        (cid, tag),
    )


def _make_api_row(
    *,
    proxy_wallet: str = WALLET_A,
    condition_id: str = "0xabc",
    outcome: str = "Yes",
    realized_pnl: float = 10.0,
    avg_price: float = 0.3,
    total_bought: float = 100.0,
    cur_price: float | None = 1.0,
    timestamp: int = T0,
    end_date: str = "2026-01-01T00:00:00Z",
    slug: str = "test-market",
) -> dict:
    """Build a synthetic /closed-positions API dict mirroring real API shape."""
    row: dict = {
        "proxyWallet": proxy_wallet,
        "conditionId": condition_id,
        "outcome": outcome,
        "realizedPnl": realized_pnl,
        "avgPrice": avg_price,
        "totalBought": total_bought,
        "timestamp": timestamp,
        "endDate": end_date,
        "slug": slug,
        "eventSlug": slug,
        "title": "Test market",
        "icon": "",
        "asset": "0xasset",
        "oppositeAsset": "0xoppaset",
        "oppositeOutcome": "No" if outcome == "Yes" else "Yes",
    }
    if cur_price is not None:
        row["curPrice"] = cur_price
    return row


# ---------------------------------------------------------------------------
# 0. Smoke — kept symbols
# ---------------------------------------------------------------------------


def test_module_exposes_required_symbols():
    """compute_wallet_tag_pnl must exist; reconstruct_positions is REMOVED per §7.2."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    assert callable(getattr(mod, "compute_wallet_tag_pnl", None)), (
        "compute_wallet_tag_pnl() must exist"
    )
    # reconstruct_positions is explicitly deleted in §7.2 — must NOT be present
    assert not hasattr(mod, "reconstruct_positions"), (
        "reconstruct_positions must be deleted per spec §7.2"
    )


def test_cli_script_is_importable():
    """scripts/build_wallet_tag_pnl.py must still exist with a main() entry point."""
    mod = importlib.import_module("scripts.build_wallet_tag_pnl")
    assert callable(getattr(mod, "main", None)), (
        "scripts/build_wallet_tag_pnl.py must expose a main() entrypoint"
    )


def test_no_activity_returns_empty_mapping():
    """No API rows → no output rows, no crash."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()
    sess = MagicMock()
    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]):
        rows = mod.compute_wallet_tag_pnl(conn, "0xnonexistentwallet0000000000000000000000", session=sess)
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# KEEP (with fixture rewritten to API-source shape): min_sample_filter
# ---------------------------------------------------------------------------


def test_min_sample_filter_excludes_thin_tag():
    """Tags with < min_sample positions are excluded from output.

    REWRITE-FIXTURE: fixture now uses API-shape dicts instead of fwa activity rows.
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()

    cid_a = "0xaa01"
    cid_b = "0xaa02"
    _insert_tag(conn, cid_a, "gamma")
    _insert_tag(conn, cid_b, "gamma")

    api_rows = [
        _make_api_row(proxy_wallet=WALLET_A, condition_id=cid_a, realized_pnl=5.0, cur_price=1.0),
        _make_api_row(proxy_wallet=WALLET_A, condition_id=cid_b, realized_pnl=3.0, cur_price=1.0),
    ]

    sess = MagicMock()
    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=api_rows):
        rows = mod.compute_wallet_tag_pnl(conn, WALLET_A, session=sess)

    assert "gamma" not in rows, (
        "gamma has only 2 positions; default min_sample=3 must exclude it"
    )


# ---------------------------------------------------------------------------
# KEEP (with fixture rewritten): unclassified_tag_assignment
# ---------------------------------------------------------------------------


def test_untagged_condition_falls_back_to_unclassified():
    """Conditions with no market_tags row bucket into 'unclassified'.

    REWRITE-FIXTURE: fixture now uses API-shape dicts.
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()

    cids = ["0xuntagged0", "0xuntagged1", "0xuntagged2"]
    # No market_tags rows for these cids
    api_rows = [
        _make_api_row(proxy_wallet=WALLET_A, condition_id=c, realized_pnl=5.0, cur_price=1.0)
        for c in cids
    ]

    sess = MagicMock()
    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=api_rows):
        rows = mod.compute_wallet_tag_pnl(conn, WALLET_A, session=sess)

    assert "unclassified" in rows, (
        f"no market_tags → must bucket to 'unclassified'; got {list(rows.keys())}"
    )


# ---------------------------------------------------------------------------
# KEEP (with fixture rewritten): primary_tag alphabetically first slug
# ---------------------------------------------------------------------------


def test_primary_tag_is_alphabetically_first_slug():
    """When condition tagged with multiple slugs, alphabetically first wins.

    REWRITE-FIXTURE: fixture now uses API-shape dicts.
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()

    cids = ["0xmulti0", "0xmulti1", "0xmulti2"]
    for cid in cids:
        _insert_tag(conn, cid, "beta")
        _insert_tag(conn, cid, "alpha")
        _insert_tag(conn, cid, "gamma")

    api_rows = [
        _make_api_row(proxy_wallet=WALLET_A, condition_id=c, realized_pnl=5.0, cur_price=1.0)
        for c in cids
    ]

    sess = MagicMock()
    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=api_rows):
        rows = mod.compute_wallet_tag_pnl(conn, WALLET_A, session=sess)

    assert "alpha" in rows, "alpha must be primary (alphabetically first)"
    assert "beta" not in rows, "beta must not double-count"
    assert "gamma" not in rows, "gamma must not double-count"


# ---------------------------------------------------------------------------
# KEEP: upsert tests (avg_hold_days removed from fixture table + INSERT)
# ---------------------------------------------------------------------------


def test_writer_upserts_wallet_tag_pnl_idempotently():
    """Running compute → write twice must not duplicate rows.

    avg_hold_days is excluded from the table DDL and INSERT (spec §4.2).
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")

    writer = (
        getattr(mod, "upsert_wallet_tag_pnl", None)
        or getattr(mod, "write_wallet_tag_pnl", None)
    )
    if writer is None:
        import db
        writer = (
            getattr(db, "upsert_wallet_tag_pnl", None)
            or getattr(db, "write_wallet_tag_pnl", None)
        )
    assert callable(writer), (
        "Expected upsert_wallet_tag_pnl or write_wallet_tag_pnl on tag_wallet_pnl or db"
    )

    conn = _make_conn_with_wallet_tag_pnl()
    cids = ["0xa0", "0xa1", "0xa2"]
    for cid in cids:
        _insert_tag(conn, cid, "alpha")

    api_rows = [
        _make_api_row(proxy_wallet=WALLET_A, condition_id=c, realized_pnl=6.0, cur_price=1.0)
        for c in cids
    ]

    sess = MagicMock()
    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=api_rows):
        rows_first = mod.compute_wallet_tag_pnl(conn, WALLET_A, session=sess)
    writer(conn, WALLET_A, rows_first)
    count_1 = conn.execute(
        "SELECT COUNT(*) FROM wallet_tag_pnl WHERE wallet=?", (WALLET_A,)
    ).fetchone()[0]

    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=api_rows):
        rows_second = mod.compute_wallet_tag_pnl(conn, WALLET_A, session=sess)
    writer(conn, WALLET_A, rows_second)
    count_2 = conn.execute(
        "SELECT COUNT(*) FROM wallet_tag_pnl WHERE wallet=?", (WALLET_A,)
    ).fetchone()[0]

    assert count_1 == count_2 == 1, (
        f"Expected idempotent upsert; got {count_1} then {count_2} rows"
    )


def test_db_wallet_tag_pnl_rows_returns_rows_for_wallet():
    """db.wallet_tag_pnl_rows must NOT return avg_hold_days (spec §7.4)."""
    import db

    assert callable(getattr(db, "wallet_tag_pnl_rows", None)), (
        "db.py must expose wallet_tag_pnl_rows(conn, wallet=None)"
    )

    conn = _make_conn_with_wallet_tag_pnl()
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO wallet_tag_pnl
            (wallet, tag, n_positions, realized_pnl, win_rate,
             conviction_concentration, computed_at, n_held_to_resolution)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (WALLET_A, "alpha", 3, 4.0, 2 / 3, 0.6, now, 2),
    )
    conn.execute(
        """
        INSERT INTO wallet_tag_pnl
            (wallet, tag, n_positions, realized_pnl, win_rate,
             conviction_concentration, computed_at, n_held_to_resolution)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (WALLET_A, "beta", 3, 2.0, 0.5, 0.4, now, 1),
    )
    conn.execute(
        """
        INSERT INTO wallet_tag_pnl
            (wallet, tag, n_positions, realized_pnl, win_rate,
             conviction_concentration, computed_at, n_held_to_resolution)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (WALLET_B, "alpha", 3, 1.0, 1.0, 1.0, now, 3),
    )
    conn.commit()

    rows = list(db.wallet_tag_pnl_rows(conn, wallet=WALLET_A))
    assert len(rows) == 2, f"expected 2 rows for WALLET_A, got {len(rows)}"

    def _get(r, k):
        return r[k] if isinstance(r, (dict, sqlite3.Row)) else getattr(r, k)

    tags = {_get(r, "tag") for r in rows}
    assert tags == {"alpha", "beta"}

    # avg_hold_days must NOT be present in any returned row
    for r in rows:
        if isinstance(r, dict):
            assert "avg_hold_days" not in r, "avg_hold_days must be absent from reader output"
        elif isinstance(r, sqlite3.Row):
            assert "avg_hold_days" not in r.keys(), "avg_hold_days must be absent from reader output"

    # All 3 rows
    rows_all = list(db.wallet_tag_pnl_rows(conn))
    assert len(rows_all) == 3


def test_backward_compat_win_rate_and_n_positions_unchanged():
    """win_rate and n_positions must still be present in reader output (B2 compat)."""
    import db

    conn = _make_conn_with_wallet_tag_pnl()
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO wallet_tag_pnl
            (wallet, tag, n_positions, realized_pnl, win_rate,
             conviction_concentration, computed_at, n_held_to_resolution)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (WALLET_A, "alpha", 5, 10.0, 0.8, 1.0, now, 4),
    )
    conn.commit()

    rows = list(db.wallet_tag_pnl_rows(conn, wallet=WALLET_A))
    assert len(rows) == 1
    r = rows[0]

    def _get(r, k):
        return r[k] if isinstance(r, (dict, sqlite3.Row)) else getattr(r, k)

    assert _get(r, "win_rate") == pytest.approx(0.8)
    assert _get(r, "n_positions") == 5


# ---------------------------------------------------------------------------
# --- A4 source switch (spec 2026-04-25) ---
# §8.2 NEW TESTS — all should FAIL until implementation lands
# ---------------------------------------------------------------------------


# §8.2 #1 — API fixture round-trip
def test_api_fixture_round_trip_compute_wallet_tag_pnl():
    """Load real fixture from tests/fixtures/polymarket_closed_positions_sample.json.

    Fixture has 5 rows all for proxyWallet=FIXTURE_WALLET; we tag 3 of the 5
    cids so they meet min_sample=3 under 'geopolitics'. Assert correct aggregation.

    All 5 have curPrice=1, realizedPnl>0 (all wins).
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()

    # Tag the first 3 cids under 'geopolitics'
    for cid in [FIXTURE_CID_0, FIXTURE_CID_1, FIXTURE_CID_2]:
        _insert_tag(conn, cid, "geopolitics")

    # All 5 rows from the real fixture
    fixture_rows = list(_FIXTURE_DATA)

    sess = MagicMock()
    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=fixture_rows):
        rows = mod.compute_wallet_tag_pnl(conn, FIXTURE_WALLET, session=sess)

    assert "geopolitics" in rows, f"expected 'geopolitics' tag; got {list(rows.keys())}"

    def _get(r, k):
        return r[k] if isinstance(r, dict) else getattr(r, k)

    g = rows["geopolitics"]
    assert _get(g, "n_positions") == 3

    # All 3 tagged positions have realizedPnl > 0 → 3 wins
    assert _get(g, "win_rate") == pytest.approx(1.0)

    expected_pnl = sum(
        row["realizedPnl"] for row in fixture_rows
        if row["conditionId"] in {FIXTURE_CID_0, FIXTURE_CID_1, FIXTURE_CID_2}
    )
    assert _get(g, "realized_pnl") == pytest.approx(expected_pnl)


# §8.2 #2 — held_to_resolution naive cases
def test_held_to_resolution_cur_price_1_no_sells_is_true():
    """curPrice==1.0, no SELLs in fwa → held_to_resolution=True.

    Cross-source fixture: API dict + fwa rows with only TRADE/BUY activity.
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()
    cid = "0xcid_held_win"

    # fwa: only a BUY (TRADE side=BUY), no SELL, no REDEEM
    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="TRADE", side="BUY", outcome="Yes", price=0.3, size=10.0)

    api_row = _make_api_row(
        proxy_wallet=WALLET_A, condition_id=cid, outcome="Yes",
        realized_pnl=7.0, cur_price=1.0, avg_price=0.3
    )

    pos = mod.Position(
        wallet=WALLET_A, condition_id=cid, outcome="Yes",
        realized_pnl=7.0, avg_entry_price=0.3, total_bought_usdc=30.0,
        cur_price=1.0, last_ts=T0,
        held_to_resolution=mod._compute_held_to_resolution(conn, WALLET_A, cid, cur_price=1.0),
    )
    assert pos.held_to_resolution is True, (
        "curPrice=1.0, no SELLs → must be held_to_resolution=True"
    )


def test_held_to_resolution_cur_price_0_no_sells_is_true():
    """curPrice==0.0, no SELLs → held_to_resolution=True (resolved as loss)."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()
    cid = "0xcid_held_loss"

    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="TRADE", side="BUY", outcome="Yes", price=0.6, size=10.0)

    held = mod._compute_held_to_resolution(conn, WALLET_A, cid, cur_price=0.0)
    assert held is True, "curPrice=0.0, no SELLs → must be True"


def test_held_to_resolution_mid_price_is_false():
    """curPrice==0.685 (not near 0 or 1) → held_to_resolution=False."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()
    cid = "0xcid_mid"

    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="TRADE", side="BUY", outcome="Yes", price=0.4, size=10.0)

    held = mod._compute_held_to_resolution(conn, WALLET_A, cid, cur_price=0.685)
    assert held is False, "curPrice=0.685 not near {0,1} → must be False"


def test_held_to_resolution_none_cur_price_is_false():
    """curPrice=None (API didn't return it) → held_to_resolution=False."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()
    cid = "0xcid_none"

    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="TRADE", side="BUY", outcome="Yes", price=0.4, size=10.0)

    held = mod._compute_held_to_resolution(conn, WALLET_A, cid, cur_price=None)
    assert held is False, "curPrice=None → must be False"


# §8.2 #3 — REGRESSION: sold-before-resolution trap
def test_held_to_resolution_sell_no_redeem_is_false():
    """ARCHITECT'S BLOCKING FINDING regression.

    curPrice==1.0 BUT fwa has SELL (activity_type='TRADE', side='SELL') and
    zero REDEEMs → held_to_resolution must be False.

    Cross-source fixture: API dict shows curPrice=1.0; fwa has TRADE/BUY + TRADE/SELL.
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()
    cid = "0xcid_sold_before_res"

    # fwa: buy then SELL — no REDEEM
    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="TRADE", side="BUY", outcome="Yes",
                price=0.3, size=10.0, activity_ts=T0)
    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="TRADE", side="SELL", outcome="Yes",
                price=0.95, size=10.0, activity_ts=T0 + 86400, key_suffix="sell")

    # API shows the position is fully resolved at 1.0 — but wallet already sold
    held = mod._compute_held_to_resolution(conn, WALLET_A, cid, cur_price=1.0)
    assert held is False, (
        "curPrice=1.0 but wallet SOLD before resolution (no REDEEM) → "
        "held_to_resolution must be False. This is the architect's blocking finding."
    )


# §8.2 #4 — SELL + REDEEM → held=True
def test_held_to_resolution_sell_plus_redeem_is_true():
    """curPrice==1.0, fwa has both SELL and REDEEM → held_to_resolution=True.

    SELL+REDEEM means partial sell then redeeming residual — still counts as
    held to resolution.
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()
    cid = "0xcid_sell_redeem"

    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="TRADE", side="BUY", outcome="Yes",
                price=0.3, size=10.0, activity_ts=T0)
    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="TRADE", side="SELL", outcome="Yes",
                price=0.8, size=3.0, activity_ts=T0 + 86400, key_suffix="sell")
    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="REDEEM", side="", outcome="Yes",
                price=1.0, size=7.0, activity_ts=T0 + 2 * 86400, key_suffix="redeem")

    held = mod._compute_held_to_resolution(conn, WALLET_A, cid, cur_price=1.0)
    assert held is True, (
        "SELL + REDEEM present → wallet held residual to resolution → must be True"
    )


# §8.2 #5 — epsilon boundary: 0.9998 counts as "near 1"
def test_held_to_resolution_epsilon_near_one_no_sells_is_true():
    """curPrice==0.9998 (within EPS=0.01 of 1.0), no SELLs → held_to_resolution=True."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()
    cid = "0xcid_eps"

    _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                activity_type="TRADE", side="BUY", outcome="Yes", price=0.4, size=10.0)

    held = mod._compute_held_to_resolution(conn, WALLET_A, cid, cur_price=0.9998)
    assert held is True, "curPrice=0.9998 within EPS=0.01 → must be True"


# §8.2 #6 — win_rate_held_only NULL sentinel
def test_win_rate_held_only_null_when_all_sold_before_resolution():
    """All positions sold-before-resolution → n_held_to_resolution=0, win_rate_held_only is None.

    Fixture: 3 positions all with curPrice=1.0 BUT fwa has SELLs and no REDEEMs.
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()

    cids = ["0xcid_sbr0", "0xcid_sbr1", "0xcid_sbr2"]
    for cid in cids:
        _insert_tag(conn, cid, "sold-tag")
        _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                    activity_type="TRADE", side="BUY", outcome="Yes",
                    price=0.3, size=10.0, activity_ts=T0)
        _insert_fwa(conn, wallet=WALLET_A, condition_id=cid,
                    activity_type="TRADE", side="SELL", outcome="Yes",
                    price=0.9, size=10.0, activity_ts=T0 + 86400, key_suffix="sell")
        # No REDEEM

    api_rows = [
        _make_api_row(proxy_wallet=WALLET_A, condition_id=c, outcome="Yes",
                      realized_pnl=6.0, cur_price=1.0)
        for c in cids
    ]

    sess = MagicMock()
    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=api_rows):
        rows = mod.compute_wallet_tag_pnl(conn, WALLET_A, session=sess)

    assert "sold-tag" in rows, f"got {list(rows.keys())}"

    def _get(r, k):
        return r[k] if isinstance(r, dict) else getattr(r, k)

    r = rows["sold-tag"]
    assert _get(r, "n_held_to_resolution") == 0
    assert _get(r, "win_rate_held_only") is None, (
        "all sold before resolution → n_held=0 → win_rate_held_only must be None/NULL"
    )


# §8.2 #7 — API empty
def test_api_empty_returns_no_rows_no_crash():
    """fetch_closed_positions returns [] → compute returns empty dict, no crash."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()
    sess = MagicMock()

    with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[]):
        rows = mod.compute_wallet_tag_pnl(conn, WALLET_A, session=sess)

    assert rows == {} or len(rows) == 0


# §8.2 #8 — API error propagated
def test_api_error_propagates_no_partial_db_write():
    """fetch_user_positions raises RequestException → propagates; no DB rows written."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn_with_wallet_tag_pnl()
    sess = MagicMock()

    with patch("sniper.markets.polymarket_api.fetch_user_positions",
               side_effect=requests.RequestException("network error")):
        with pytest.raises(requests.RequestException):
            mod.compute_wallet_tag_pnl(conn, WALLET_A, session=sess)

    count = conn.execute(
        "SELECT COUNT(*) FROM wallet_tag_pnl WHERE wallet=?", (WALLET_A,)
    ).fetchone()[0]
    assert count == 0, "RequestException must result in zero DB rows written"


# §8.2 #9 — avg_hold_days migration
def test_avg_hold_days_migration_drops_column_preserves_data():
    """ensure_wallet_tag_pnl_table removes avg_hold_days from existing table.

    Steps:
    1. Create OLD schema with avg_hold_days column.
    2. Insert a row with data in other columns.
    3. Call ensure_wallet_tag_pnl_table(conn).
    4. Assert avg_hold_days column absent from PRAGMA table_info.
    5. Assert data in other columns preserved.
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")

    assert callable(getattr(mod, "ensure_wallet_tag_pnl_table", None)), (
        "ensure_wallet_tag_pnl_table must exist on sniper.analysis.tag_wallet_pnl"
    )

    conn = _make_conn_with_legacy_wallet_tag_pnl()
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO wallet_tag_pnl
            (wallet, tag, n_positions, realized_pnl, win_rate,
             avg_hold_days, conviction_concentration, computed_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (WALLET_A, "alpha", 5, 42.0, 0.8, 7.3, 0.75, now),
    )
    conn.commit()

    # Run migration
    mod.ensure_wallet_tag_pnl_table(conn)

    # avg_hold_days must be gone
    cols = {row[1] for row in conn.execute("PRAGMA table_info(wallet_tag_pnl)").fetchall()}
    assert "avg_hold_days" not in cols, (
        f"avg_hold_days must be dropped by ensure_wallet_tag_pnl_table; cols={cols}"
    )

    # Other data preserved
    row = conn.execute(
        "SELECT * FROM wallet_tag_pnl WHERE wallet=? AND tag=?", (WALLET_A, "alpha")
    ).fetchone()
    assert row is not None, "existing row must survive migration"
    d = dict(row)
    assert d["n_positions"] == 5
    assert d["realized_pnl"] == pytest.approx(42.0)
    assert d["win_rate"] == pytest.approx(0.8)
    assert d["conviction_concentration"] == pytest.approx(0.75)


# §8.2 #10 — Reader compat: avg_hold_days absent, B2 compute_wallet_tag_fit no crash
def test_reader_compat_avg_hold_days_absent_b2_no_crash():
    """db.wallet_tag_pnl_rows does not return avg_hold_days.

    wallet_tag_fit.compute_wallet_tag_fit must not crash on the missing key.
    """
    import db

    conn = _make_conn_with_wallet_tag_pnl()
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO wallet_tag_pnl
            (wallet, tag, n_positions, realized_pnl, win_rate,
             conviction_concentration, computed_at, n_held_to_resolution)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (WALLET_A, "alpha", 3, 4.0, 0.667, 0.6, now, 2),
    )
    conn.commit()

    rows = list(db.wallet_tag_pnl_rows(conn, wallet=WALLET_A))
    assert len(rows) == 1

    r = rows[0]
    if isinstance(r, dict):
        assert "avg_hold_days" not in r
    elif isinstance(r, sqlite3.Row):
        assert "avg_hold_days" not in r.keys()

    # B2: compute_wallet_tag_fit must not raise KeyError on avg_hold_days
    try:
        fit_mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
        fit_mod.compute_wallet_tag_fit(WALLET_A, rows)
    except KeyError as e:
        pytest.fail(f"wallet_tag_fit.compute_wallet_tag_fit raised KeyError: {e}")
    except Exception:
        # Other errors (missing impl, etc.) are acceptable — KeyError is the regression
        pass


# §8.2 #11 — proxyWallet mismatch WARN
def test_proxy_wallet_mismatch_logs_warn_row_still_returned(caplog):
    """fetch_closed_positions logs WARN when proxyWallet != queried wallet.

    The row must still be present in the returned list (not silently dropped).
    """
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")

    mismatched_row = _make_api_row(
        proxy_wallet="0xdifferentwallet0000000000000000000000",
        condition_id="0xmismatch_cid",
        realized_pnl=5.0,
        cur_price=1.0,
    )

    with caplog.at_level(logging.WARNING):
        with patch("sniper.markets.polymarket_api.fetch_user_positions", return_value=[mismatched_row]):
            # fetch_closed_positions wraps fetch_user_positions and does the warn check
            result = mod.fetch_closed_positions(MagicMock(), WALLET_A)

    assert any("proxyWallet" in msg or "proxy" in msg.lower() for msg in caplog.messages), (
        "Expected a WARN log about proxyWallet mismatch"
    )
    assert len(result) == 1, "Mismatched row must still be returned (not dropped)"
    assert result[0]["conditionId"] == "0xmismatch_cid"


# §8.2 bonus — fetch_closed_positions smoke (function must exist)
def test_fetch_closed_positions_callable():
    """fetch_closed_positions must exist as a callable on the module (§7.1)."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    assert callable(getattr(mod, "fetch_closed_positions", None)), (
        "fetch_closed_positions must be exposed per spec §7.1"
    )


# §8.2 bonus — _fetch_sell_and_redeem_cids must exist
def test_fetch_sell_and_redeem_cids_callable():
    """_fetch_sell_and_redeem_cids must exist (spec §7.2 ADD list)."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    assert callable(getattr(mod, "_fetch_sell_and_redeem_cids", None)), (
        "_fetch_sell_and_redeem_cids must be added per spec §7.2"
    )


def test_fetch_sell_and_redeem_cids_returns_two_sets():
    """_fetch_sell_and_redeem_cids(conn, wallet) -> (set_of_sell_cids, set_of_redeem_cids)."""
    mod = importlib.import_module("sniper.analysis.tag_wallet_pnl")
    conn = _make_conn()

    sell_cid = "0xcid_with_sell"
    redeem_cid = "0xcid_with_redeem"
    both_cid = "0xcid_with_both"
    none_cid = "0xcid_no_exit"

    # sell_cid: TRADE/SELL
    _insert_fwa(conn, wallet=WALLET_A, condition_id=sell_cid,
                activity_type="TRADE", side="SELL", outcome="Yes", size=5.0)
    # redeem_cid: REDEEM size>0
    _insert_fwa(conn, wallet=WALLET_A, condition_id=redeem_cid,
                activity_type="REDEEM", side="", outcome="Yes", size=5.0)
    # both_cid: both
    _insert_fwa(conn, wallet=WALLET_A, condition_id=both_cid,
                activity_type="TRADE", side="SELL", outcome="Yes", size=3.0, key_suffix="s")
    _insert_fwa(conn, wallet=WALLET_A, condition_id=both_cid,
                activity_type="REDEEM", side="", outcome="Yes", size=2.0, key_suffix="r")
    # none_cid: only BUY
    _insert_fwa(conn, wallet=WALLET_A, condition_id=none_cid,
                activity_type="TRADE", side="BUY", outcome="Yes", size=10.0)

    sell_cids, redeem_cids = mod._fetch_sell_and_redeem_cids(conn, WALLET_A)

    assert sell_cid in sell_cids
    assert both_cid in sell_cids
    assert none_cid not in sell_cids
    assert redeem_cid not in sell_cids

    assert redeem_cid in redeem_cids
    assert both_cid in redeem_cids
    assert sell_cid not in redeem_cids
    assert none_cid not in redeem_cids


# ---------------------------------------------------------------------------
# init_db wiring — wallet_tag_pnl migrator must run as part of post-gate idempotent ensure
# ---------------------------------------------------------------------------


def test_init_db_creates_new_shape_wallet_tag_pnl_on_fresh_db():
    """init_db on a fresh DB must produce the post-B2 shape (no avg_hold_days,
    held-only + open-drag columns present). Locks the wiring of
    ensure_wallet_tag_pnl_table into _ensure_post_gate_idempotent."""
    import db as db_mod

    conn = sqlite3.connect(":memory:")
    db_mod.init_db(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(wallet_tag_pnl)").fetchall()}

    expected = {
        "wallet",
        "tag",
        "n_positions",
        "realized_pnl",
        "win_rate",
        "conviction_concentration",
        "computed_at",
        "win_rate_held_only",
        "n_held_to_resolution",
        "open_unrealized_pnl",
        "n_open_positions",
        "n_open_dead",
    }
    assert expected.issubset(cols), f"missing cols={expected - cols}"
    assert "avg_hold_days" not in cols, (
        "fresh-DB init_db must not leave avg_hold_days behind"
    )


def test_init_db_migrates_legacy_wallet_tag_pnl_dropping_avg_hold_days():
    """Legacy production-shape DB (avg_hold_days present, B2 columns missing)
    must migrate cleanly when init_db is invoked, without losing data.

    This is the regression that protects the VPS rollout: production DBs were
    created with the pre-B2 schema, and the analyzer's
    ensure_wallet_tag_pnl_table was not wired into init_db, so init_db never
    migrated them. After this fix, init_db owns the migration."""
    import db as db_mod

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE wallet_tag_pnl (
            wallet                    TEXT    NOT NULL,
            tag                       TEXT    NOT NULL,
            n_positions               INTEGER NOT NULL,
            realized_pnl              REAL    NOT NULL,
            win_rate                  REAL    NOT NULL,
            conviction_concentration  REAL    NOT NULL,
            computed_at               INTEGER NOT NULL,
            avg_hold_days             REAL,
            PRIMARY KEY (wallet, tag)
        )
        """
    )
    conn.execute(
        "INSERT INTO wallet_tag_pnl VALUES (?,?,?,?,?,?,?,?)",
        (WALLET_A, "alpha", 5, 42.0, 0.8, 0.75, T0, 7.3),
    )
    conn.commit()

    db_mod.init_db(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(wallet_tag_pnl)").fetchall()}
    assert "avg_hold_days" not in cols, "init_db must drop avg_hold_days"
    assert "win_rate_held_only" in cols
    assert "n_held_to_resolution" in cols
    assert "open_unrealized_pnl" in cols
    assert "n_open_positions" in cols
    assert "n_open_dead" in cols

    conn.row_factory = sqlite3.Row
    row = dict(
        conn.execute(
            "SELECT * FROM wallet_tag_pnl WHERE wallet=? AND tag=?",
            (WALLET_A, "alpha"),
        ).fetchone()
    )
    assert row["n_positions"] == 5
    assert row["realized_pnl"] == pytest.approx(42.0)
    assert row["win_rate"] == pytest.approx(0.8)
    assert row["conviction_concentration"] == pytest.approx(0.75)
    assert row["n_held_to_resolution"] == 0
    assert row["open_unrealized_pnl"] == pytest.approx(0.0)
    assert row["n_open_positions"] == 0
    assert row["n_open_dead"] == 0
