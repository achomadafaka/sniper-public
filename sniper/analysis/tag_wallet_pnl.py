"""Per-(wallet, tag) realized-PnL matrix — API source (B1 source switch).

Spec: docs/superpowers/specs/2026-04-25-tag-wallet-pnl-api-source-switch.md

Computes realized PnL by fetching closed positions from the Polymarket
/closed-positions API endpoint, then aggregates by the primary tag of each
market (alphabetically first slug in market_tags, else 'unclassified').

No hardcoded tag slugs anywhere — all tag values are treated as opaque strings.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable

import requests

import sniper.markets.polymarket_api as polymarket_api

logger = logging.getLogger(__name__)


UNCLASSIFIED_TAG = "unclassified"
_EPS = 0.01  # proximity to 0 or 1 for held_to_resolution


def _clamp_price(p: float) -> float:
    return max(0.0, min(1.0, float(p)))


# ---------------------------------------------------------------------------
# Data classes (new API-source shape per spec §4)
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """A closed position from the /closed-positions API."""

    wallet: str
    condition_id: str
    outcome: str
    realized_pnl: float          # api: realizedPnl
    avg_entry_price: float       # api: avgPrice
    total_bought_usdc: float     # api: totalBought
    cur_price: float | None      # api: curPrice (None if absent)
    last_ts: int                 # api: timestamp
    held_to_resolution: bool     # derived per §4.1


@dataclass
class TagPnlRow:
    tag: str
    n_positions: int
    realized_pnl: float
    win_rate: float
    conviction_concentration: float
    win_rate_held_only: float | None
    n_held_to_resolution: int
    # Open-position drag (added 2026-04-25 to fix survivor-bias bug —
    # /closed-positions excludes losing positions the wallet never redeemed).
    # net_pnl = realized_pnl + open_unrealized_pnl gives the true tag P&L.
    open_unrealized_pnl: float = 0.0   # sum of cashPnl from /positions, this tag
    n_open_positions: int = 0          # count of currently-open positions on tag
    n_open_dead: int = 0               # subset of n_open_positions where curPrice <= 0.01 (effectively-resolved-against-wallet, just unredeemed)


# ---------------------------------------------------------------------------
# fetch_closed_positions — re-exported from polymarket_api (§7.1)
# ---------------------------------------------------------------------------

def fetch_closed_positions(
    session: requests.Session, wallet: str
) -> list[dict]:
    """Fetch closed positions, warning on proxyWallet mismatches."""
    return polymarket_api.fetch_closed_positions(session, wallet)


def fetch_open_positions(
    session: requests.Session, wallet: str
) -> list[dict]:
    """Fetch currently-open positions from /positions endpoint.

    Required because /closed-positions only returns positions the wallet
    explicitly redeemed. Losing positions where the wallet's side resolved
    to $0 typically stay in /positions forever (gas cost > $0 redemption).
    Without including them, every wallet's apparent P&L is biased upward.
    Verified 2026-04-25 against SemyonMarmeladov: /closed-positions said
    +$18M but UI showed -$1.09M; the missing -$19M was 673 dead-but-open
    positions sitting at curPrice ~= 0.

    Note: /positions does NOT accept sortBy params (returns 400). Use raw
    pagination without sort.
    """
    result = polymarket_api.fetch_user_positions(
        session, wallet, endpoint="positions"
    )
    # Real function returns (rows, FetchTermination); legacy test mocks return a list.
    return result[0] if isinstance(result, tuple) else list(result)  # type: ignore[index]


# ---------------------------------------------------------------------------
# Cross-source DB helpers (§7.2)
# ---------------------------------------------------------------------------


def _fetch_sell_and_redeem_cids(
    conn: sqlite3.Connection, wallet: str
) -> tuple[set[str], set[str]]:
    """Return (sell_cids, redeem_cids) from followed_wallet_activity.

    sell_cids: condition_ids with activity_type='TRADE', side='SELL'
    redeem_cids: condition_ids with activity_type='REDEEM', size > 0
    """
    cur = conn.execute(
        "SELECT DISTINCT condition_id FROM followed_wallet_activity "
        "WHERE wallet=? AND activity_type='TRADE' AND side='SELL'",
        (wallet,),
    )
    sell_cids = {r[0] for r in cur if r[0]}

    cur = conn.execute(
        "SELECT DISTINCT condition_id FROM followed_wallet_activity "
        "WHERE wallet=? AND activity_type='REDEEM' AND size > 0",
        (wallet,),
    )
    redeem_cids = {r[0] for r in cur if r[0]}

    return sell_cids, redeem_cids


def _compute_held_to_resolution(
    conn: sqlite3.Connection,
    wallet: str,
    condition_id: str,
    cur_price: float | None,
) -> bool:
    """Derive held_to_resolution per spec §4.1.

    held = (cur_price is not None)
         AND (cur_price <= EPS OR cur_price >= 1.0 - EPS)
         AND NOT (had_any_sell AND NOT had_any_redeem)
    """
    if cur_price is None:
        return False
    if not (cur_price <= _EPS or cur_price >= 1.0 - _EPS):
        return False

    sell_cids, redeem_cids = _fetch_sell_and_redeem_cids(conn, wallet)
    had_sell = condition_id in sell_cids
    had_redeem = condition_id in redeem_cids

    if had_sell and not had_redeem:
        return False
    return True


# ---------------------------------------------------------------------------
# Tag assignment
# ---------------------------------------------------------------------------


def _primary_tag_for_cids(
    conn: sqlite3.Connection, condition_ids: Iterable[str]
) -> dict[str, str]:
    """Return {cid: primary_tag} where primary_tag is alphabetically first
    slug in market_tags for that cid, else `unclassified`.
    """
    cids = list({c for c in condition_ids if c})
    if not cids:
        return {}
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in cids)
        rows = conn.execute(
            f"SELECT condition_id, tag FROM market_tags "
            f"WHERE condition_id IN ({placeholders})",
            cids,
        ).fetchall()
    finally:
        conn.row_factory = prev
    grouped: dict[str, list[str]] = {}
    for r in rows:
        cid = r["condition_id"]
        tag = r["tag"]
        if not cid or not tag:
            continue
        grouped.setdefault(cid, []).append(tag)
    out: dict[str, str] = {}
    for cid in cids:
        tags = grouped.get(cid)
        if tags:
            out[cid] = sorted(tags)[0]
        else:
            out[cid] = UNCLASSIFIED_TAG
    return out


# ---------------------------------------------------------------------------
# Main compute (§7.2 — API source)
# ---------------------------------------------------------------------------


def compute_wallet_tag_pnl(
    conn: sqlite3.Connection,
    wallet: str,
    session: requests.Session | None = None,
    min_sample: int = 3,
) -> dict[str, TagPnlRow]:
    """Aggregate closed positions by primary tag for a wallet.

    Fetches from /closed-positions API, cross-references followed_wallet_activity
    for held_to_resolution derivation, groups by tag, filters min_sample.
    conviction_concentration denominator uses ALL positions (even below min_sample).
    """
    if session is None:
        session = requests.Session()

    # May raise RequestException — let it propagate (no partial write).
    # Route through fetch_closed_positions so the proxyWallet-mismatch WARN
    # fires on the production compute path, not just when the wrapper is
    # invoked in isolation (per reviewer 2026-04-25).
    api_rows = polymarket_api.fetch_closed_positions(session, wallet)

    # Pull open positions too — required to capture losing positions the
    # wallet never redeemed (sits at curPrice ~= 0). Without this, every
    # wallet's apparent edge is biased upward.
    try:
        open_rows = fetch_open_positions(session, wallet)
    except Exception:
        logger.exception("tag_wallet_pnl: fetch_open_positions failed wallet=%s", wallet)
        open_rows = []

    if not api_rows and not open_rows:
        return {}

    sell_cids, redeem_cids = _fetch_sell_and_redeem_cids(conn, wallet)

    positions: list[Position] = []
    for row in api_rows:
        try:
            cid = row.get("conditionId") or ""
            outcome = row.get("outcome") or ""
            if not cid or not outcome:
                continue
            realized_pnl = float(row.get("realizedPnl") or 0.0)
            avg_entry = float(row.get("avgPrice") or 0.0)
            total_bought = float(row.get("totalBought") or 0.0)
            raw_cur = row.get("curPrice")
            cur_price: float | None = (
                _clamp_price(float(raw_cur)) if raw_cur is not None else None
            )
            last_ts = int(row.get("timestamp") or 0)

            # §4.1 held_to_resolution
            if cur_price is None:
                held = False
            elif not (cur_price <= _EPS or cur_price >= 1.0 - _EPS):
                held = False
            else:
                had_sell = cid in sell_cids
                had_redeem = cid in redeem_cids
                held = not (had_sell and not had_redeem)

            positions.append(Position(
                wallet=wallet,
                condition_id=cid,
                outcome=outcome,
                realized_pnl=realized_pnl,
                avg_entry_price=avg_entry,
                total_bought_usdc=total_bought,
                cur_price=cur_price,
                last_ts=last_ts,
                held_to_resolution=held,
            ))
        except Exception:
            logger.exception("tag_wallet_pnl: bad row from API wallet=%s", wallet)

    # Per-tag open-position aggregates (cashPnl, count, dead-count).
    # Built from open_rows; keyed by tag via the same _primary_tag_for_cids
    # helper. Tags that appear ONLY in open_rows (no closed positions) are
    # NOT emitted as wallet_tag_pnl rows — would be all-zeros on the
    # closed side and misleading. Filter at end.
    all_cids = {p.condition_id for p in positions}
    all_cids.update(r.get("conditionId") or "" for r in open_rows if r.get("conditionId"))
    cid_to_tag = _primary_tag_for_cids(conn, all_cids)

    open_by_tag: dict[str, dict[str, float | int]] = {}
    for row in open_rows:
        try:
            cid = row.get("conditionId") or ""
            if not cid:
                continue
            tag = cid_to_tag.get(cid, UNCLASSIFIED_TAG) or UNCLASSIFIED_TAG
            cash_pnl = float(row.get("cashPnl") or 0.0)
            cur_price = row.get("curPrice")
            is_dead = (
                cur_price is not None
                and (float(cur_price) <= _EPS or float(cur_price) >= 1.0 - _EPS)
            )
            agg = open_by_tag.setdefault(tag, {"unrealized": 0.0, "n": 0, "n_dead": 0})
            agg["unrealized"] = float(agg["unrealized"]) + cash_pnl
            agg["n"] = int(agg["n"]) + 1
            if is_dead:
                agg["n_dead"] = int(agg["n_dead"]) + 1
        except Exception:
            logger.exception("tag_wallet_pnl: bad open row wallet=%s", wallet)

    if not positions:
        # No closed positions; we don't emit rows for open-only tags.
        return {}

    by_tag: dict[str, list[Position]] = {}
    total_abs_pnl_all = 0.0
    for p in positions:
        tag = cid_to_tag.get(p.condition_id, UNCLASSIFIED_TAG) or UNCLASSIFIED_TAG
        by_tag.setdefault(tag, []).append(p)
        total_abs_pnl_all += abs(p.realized_pnl)

    out: dict[str, TagPnlRow] = {}
    for tag, plist in by_tag.items():
        if len(plist) < min_sample:
            continue
        realized_pnl = sum(p.realized_pnl for p in plist)
        wins = sum(1 for p in plist if p.realized_pnl > 0.0)
        win_rate = wins / len(plist)

        held_group = [p for p in plist if p.held_to_resolution]
        n_held = len(held_group)
        if n_held > 0:
            held_wins = sum(1 for p in held_group if p.realized_pnl > 0.0)
            win_rate_held_only: float | None = held_wins / n_held
        else:
            win_rate_held_only = None

        tag_abs = sum(abs(p.realized_pnl) for p in plist)
        concentration = (
            tag_abs / total_abs_pnl_all if total_abs_pnl_all > 0 else 0.0
        )

        open_agg = open_by_tag.get(tag, {"unrealized": 0.0, "n": 0, "n_dead": 0})

        out[tag] = TagPnlRow(
            tag=tag,
            n_positions=len(plist),
            realized_pnl=realized_pnl,
            win_rate=win_rate,
            conviction_concentration=concentration,
            win_rate_held_only=win_rate_held_only,
            n_held_to_resolution=n_held,
            open_unrealized_pnl=float(open_agg["unrealized"]),
            n_open_positions=int(open_agg["n"]),
            n_open_dead=int(open_agg["n_dead"]),
        )
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def ensure_wallet_tag_pnl_table(conn: sqlite3.Connection) -> None:
    """Create wallet_tag_pnl table or migrate legacy schema idempotently.

    - Creates the table if it doesn't exist (new schema without avg_hold_days).
    - If avg_hold_days column exists, drops it (§4.2 migration).
    - If win_rate_held_only / n_held_to_resolution are missing, adds them (B2 migration).
    """
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
            open_unrealized_pnl       REAL    NOT NULL DEFAULT 0,
            n_open_positions          INTEGER NOT NULL DEFAULT 0,
            n_open_dead               INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (wallet, tag)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wallet_tag_pnl_wallet "
        "ON wallet_tag_pnl (wallet)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wallet_tag_pnl_tag "
        "ON wallet_tag_pnl (tag)"
    )

    # Idempotent migrations based on current schema state
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(wallet_tag_pnl)").fetchall()
    }

    # §4.2: drop avg_hold_days if present
    if "avg_hold_days" in cols:
        conn.execute("ALTER TABLE wallet_tag_pnl DROP COLUMN avg_hold_days")

    # B2: add held-only columns if missing
    if "win_rate_held_only" not in cols:
        conn.execute(
            "ALTER TABLE wallet_tag_pnl ADD COLUMN win_rate_held_only REAL"
        )
    if "n_held_to_resolution" not in cols:
        conn.execute(
            "ALTER TABLE wallet_tag_pnl ADD COLUMN n_held_to_resolution "
            "INTEGER NOT NULL DEFAULT 0"
        )

    # 2026-04-25: open-position drag columns (fix for survivor-bias bug —
    # wallets with hundreds of unredeemed losers were ranked as winners).
    if "open_unrealized_pnl" not in cols:
        conn.execute(
            "ALTER TABLE wallet_tag_pnl ADD COLUMN open_unrealized_pnl "
            "REAL NOT NULL DEFAULT 0"
        )
    if "n_open_positions" not in cols:
        conn.execute(
            "ALTER TABLE wallet_tag_pnl ADD COLUMN n_open_positions "
            "INTEGER NOT NULL DEFAULT 0"
        )
    if "n_open_dead" not in cols:
        conn.execute(
            "ALTER TABLE wallet_tag_pnl ADD COLUMN n_open_dead "
            "INTEGER NOT NULL DEFAULT 0"
        )


def upsert_wallet_tag_pnl(
    conn: sqlite3.Connection,
    wallet: str,
    rows: dict[str, TagPnlRow] | Iterable[TagPnlRow],
) -> int:
    """Idempotent upsert by (wallet, tag). Returns rows written."""
    ensure_wallet_tag_pnl_table(conn)
    now = int(time.time())

    if isinstance(rows, dict):
        iterable = list(rows.values())
    else:
        iterable = list(rows)

    count = 0
    for r in iterable:
        tag = r.tag
        n = int(r.n_positions)
        pnl = float(r.realized_pnl)
        wr = float(r.win_rate)
        cc = float(r.conviction_concentration)
        wrh = r.win_rate_held_only  # may be None
        nhr = int(r.n_held_to_resolution)
        oup = float(getattr(r, "open_unrealized_pnl", 0.0))
        nop = int(getattr(r, "n_open_positions", 0))
        nod = int(getattr(r, "n_open_dead", 0))
        conn.execute(
            """
            INSERT INTO wallet_tag_pnl
                (wallet, tag, n_positions, realized_pnl, win_rate,
                 conviction_concentration, computed_at,
                 win_rate_held_only, n_held_to_resolution,
                 open_unrealized_pnl, n_open_positions, n_open_dead)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(wallet, tag) DO UPDATE SET
                n_positions              = excluded.n_positions,
                realized_pnl             = excluded.realized_pnl,
                win_rate                 = excluded.win_rate,
                conviction_concentration = excluded.conviction_concentration,
                computed_at              = excluded.computed_at,
                win_rate_held_only       = excluded.win_rate_held_only,
                n_held_to_resolution     = excluded.n_held_to_resolution,
                open_unrealized_pnl      = excluded.open_unrealized_pnl,
                n_open_positions         = excluded.n_open_positions,
                n_open_dead              = excluded.n_open_dead
            """,
            (wallet, tag, n, pnl, wr, cc, now, wrh, nhr, oup, nop, nod),
        )
        count += 1
    conn.commit()
    return count


# Alias per spec §8
write_wallet_tag_pnl = upsert_wallet_tag_pnl


__all__ = [
    "Position",
    "TagPnlRow",
    "UNCLASSIFIED_TAG",
    "fetch_closed_positions",
    "_fetch_sell_and_redeem_cids",
    "_compute_held_to_resolution",
    "_primary_tag_for_cids",
    "compute_wallet_tag_pnl",
    "ensure_wallet_tag_pnl_table",
    "upsert_wallet_tag_pnl",
    "write_wallet_tag_pnl",
]
