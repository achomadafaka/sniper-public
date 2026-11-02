"""Per-(wallet, tag) fit score — B2 tag-archetype-fit.

Spec: spec/tag-archetype-fit-2026-04-24.

Projects wallet_tag_pnl onto a normalized within-tag percentile rank.
NOT cross-tag portable. NOT a forward forecast.

Each wallet×tag pair receives a fit_score in [0, 1] as an equal-weighted
mean of four percentile components:
  edge      (§4.1) — realized_pnl / n_positions, ranked within tag
  sample    (§4.2) — sqrt(n_held_to_resolution / 100), capped at 1
  consistency (§4.3) — Bayesian-shrunk WR, ranked within tag
  recency   (§4.4) — 0.5 ** (days_since_last / 90), ranked within tag
  concentration (§4.5) — weight=0 in v1, raw value persisted in inputs_json
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)

WEIGHTS = {
    "edge": 0.25,
    "sample": 0.25,
    "consistency": 0.25,
    "recency": 0.25,
    "concentration": 0.0,
}

CONFIDENCE_OK_FLOOR = 30
MIN_N_HELD = 3  # §5.1: wallet must have n_held_to_resolution >= 3


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def sample_strength(n_held: int) -> float:
    """§4.2: min(sqrt(n_held) / sqrt(100), 1.0)."""
    return min(math.sqrt(max(0, n_held)) / math.sqrt(100), 1.0)


def recency_weight(days_since_last: float) -> float:
    """§4.4: 0.5 ** (days / 90). Half-life 90 days."""
    return 0.5 ** (days_since_last / 90.0)


def bayesian_posterior_mean(wins: int, n: int, mu: float, k: float) -> float:
    """§4.3: (wins + mu*k) / (n + k)."""
    return (wins + mu * k) / (n + k)


def _percentile_rank(value: float, all_values: list[float]) -> float:
    """Fraction of values <= value, in [0, 1].

    Handles ties by using "fraction of values <= v" (inclusive).
    Single-element lists → 1.0.
    """
    if not all_values:
        return 0.0
    n = len(all_values)
    count_le = sum(1 for v in all_values if v <= value)
    return count_le / n


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PeerStats:
    """Per-tag peer summary, built from wallet_tag_pnl rows with n_held >= 3."""
    tag: str
    peer_count: int
    peer_mean_wr_held: float
    peer_median_n_held: float
    k_prior_strength: float
    # Per-peer distribution arrays for percentile-rank computation.
    edge_values: list[float] = field(default_factory=list)
    recency_values: list[float] = field(default_factory=list)
    consistency_posteriors: list[float] = field(default_factory=list)
    sample_values: list[float] = field(default_factory=list)

    def __getitem__(self, key: str):
        return getattr(self, key)


@dataclass
class FitRow:
    wallet: str
    tag: str
    fit_score: float
    fit_rank: int  # assigned by orchestrator; pure fn sets to 0
    peer_count: int
    confidence_flag: str
    computed_at: int
    inputs_json: str


# ---------------------------------------------------------------------------
# Peer stats
# ---------------------------------------------------------------------------


def compute_peer_stats(conn: sqlite3.Connection) -> dict[str, PeerStats]:
    """Load wallet_tag_pnl, group by tag, build PeerStats for each tag.

    Only wallets with n_held_to_resolution >= 3 count as peers.
    """
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT wallet, tag, realized_pnl, n_positions,
                   win_rate_held_only, n_held_to_resolution,
                   conviction_concentration, computed_at
              FROM wallet_tag_pnl
             WHERE n_held_to_resolution >= ?
            """,
            (MIN_N_HELD,),
        ).fetchall()
    finally:
        conn.row_factory = prev

    # group raw rows by tag
    by_tag: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        tag = r["tag"] or ""
        if not tag:
            continue
        by_tag.setdefault(tag, []).append(r)

    out: dict[str, PeerStats] = {}
    for tag, tag_rows in by_tag.items():
        peer_count = len(tag_rows)
        wr_vals = [
            float(r["win_rate_held_only"] or 0.0)
            for r in tag_rows
        ]
        n_held_vals = [int(r["n_held_to_resolution"] or 0) for r in tag_rows]
        peer_mean_wr = sum(wr_vals) / len(wr_vals) if wr_vals else 0.0
        sorted_n = sorted(n_held_vals)
        n = len(sorted_n)
        if n % 2 == 1:
            peer_median_n = float(sorted_n[n // 2])
        else:
            peer_median_n = (sorted_n[n // 2 - 1] + sorted_n[n // 2]) / 2.0
        k = max(5.0, peer_median_n)

        # Compute per-peer component values for percentile ranking later.
        edge_vals = [
            float(r["realized_pnl"] or 0.0) / max(1, int(r["n_positions"] or 1))
            for r in tag_rows
        ]
        sample_vals = [
            sample_strength(int(r["n_held_to_resolution"] or 0))
            for r in tag_rows
        ]
        # Consistency posteriors for each peer (Bayesian-shrunk WR)
        consistency_posts = []
        for r in tag_rows:
            wr_held = float(r["win_rate_held_only"] or 0.0)
            n_held = int(r["n_held_to_resolution"] or 0)
            wins = round(wr_held * n_held)
            post = bayesian_posterior_mean(wins=wins, n=n_held, mu=peer_mean_wr, k=k)
            consistency_posts.append(post)

        # Recency: we do NOT have last_activity_ts in wallet_tag_pnl — that must
        # be fetched per-wallet from followed_wallet_activity in compute_all_fits.
        # We store a placeholder empty list here; compute_all_fits fills it in.
        out[tag] = PeerStats(
            tag=tag,
            peer_count=peer_count,
            peer_mean_wr_held=peer_mean_wr,
            peer_median_n_held=peer_median_n,
            k_prior_strength=k,
            edge_values=edge_vals,
            sample_values=sample_vals,
            consistency_posteriors=consistency_posts,
            recency_values=[],  # filled by compute_all_fits
        )

    return out


# ---------------------------------------------------------------------------
# Pure computation for a single wallet×tag row
# ---------------------------------------------------------------------------


def compute_wallet_tag_fit(
    row: dict,
    peer_stats: PeerStats | dict,
) -> FitRow:
    """Compute FitRow for one (wallet, tag) row.

    `row` must contain:
      wallet, tag, realized_pnl, n_positions,
      win_rate_held_only, n_held_to_resolution,
      conviction_concentration, last_activity_ts, computed_at.

    `peer_stats` must expose:
      tag, peer_count, peer_mean_wr_held, peer_median_n_held,
      k_prior_strength, edge_values, recency_values,
      consistency_posteriors, sample_values.
    Accepts both PeerStats dataclass and plain dict.
    """
    # Retrieve fields from row (dict or sqlite3.Row)
    wallet = row["wallet"]
    tag = row["tag"]
    realized_pnl = float(row.get("realized_pnl") or 0.0)
    n_positions = int(row.get("n_positions") or 1)
    win_rate_held_only = float(row.get("win_rate_held_only") or 0.0)
    n_held = int(row.get("n_held_to_resolution") or 0)
    conviction_concentration = float(row.get("conviction_concentration") or 0.0)
    last_activity_ts = int(row.get("last_activity_ts") or 0)
    computed_at = int(row.get("computed_at") or int(time.time()))

    # Helper to access peer_stats fields from either type
    def _ps(key: str):
        if isinstance(peer_stats, dict):
            return peer_stats[key]
        return getattr(peer_stats, key)

    peer_count = int(_ps("peer_count"))
    peer_mean_wr = float(_ps("peer_mean_wr_held"))
    peer_median_n = float(_ps("peer_median_n_held"))
    k = float(_ps("k_prior_strength"))
    edge_values: list[float] = list(_ps("edge_values"))
    recency_values: list[float] = list(_ps("recency_values"))
    consistency_posteriors: list[float] = list(_ps("consistency_posteriors"))
    sample_vals: list[float] = list(_ps("sample_values"))

    # §4.1 Realized edge
    realized_pnl_per_position = realized_pnl / max(1, n_positions)
    edge_pct = _percentile_rank(realized_pnl_per_position, edge_values)

    # §4.2 Sample strength (raw value, not percentile-ranked — it's a direct score)
    raw_sample = sample_strength(n_held)
    sample_pct = _percentile_rank(raw_sample, sample_vals)

    # §4.3 Consistency (Bayesian-shrunk WR, percentile-ranked)
    wins = round(win_rate_held_only * n_held)
    wr_posterior_mean = bayesian_posterior_mean(wins=wins, n=n_held, mu=peer_mean_wr, k=k)
    consistency_pct = _percentile_rank(wr_posterior_mean, consistency_posteriors)

    # §4.4 Recency
    if last_activity_ts > 0 and computed_at >= last_activity_ts:
        days_since_last = (computed_at - last_activity_ts) / 86400.0
    elif last_activity_ts > 0:
        days_since_last = 0.0
    else:
        days_since_last = 0.0
    rw = recency_weight(days_since_last)
    if recency_values:
        recency_pct = _percentile_rank(rw, recency_values)
    else:
        recency_pct = rw  # fallback for pure-function tests with pre-built distribution

    # §4.5 Concentration (weight=0; raw value persisted)
    concentration_raw = conviction_concentration

    # Equal-weight fit_score (concentration excluded)
    fit_score = (
        WEIGHTS["edge"] * edge_pct
        + WEIGHTS["sample"] * sample_pct
        + WEIGHTS["consistency"] * consistency_pct
        + WEIGHTS["recency"] * recency_pct
        # concentration weight = 0.0
    )
    fit_score = max(0.0, min(1.0, fit_score))

    confidence_flag = "ok" if peer_count >= CONFIDENCE_OK_FLOOR else "low_confidence"

    inputs = {
        "version": "v1",
        "components": {
            "edge_pct": edge_pct,
            "sample_pct": sample_pct,
            "consistency_pct": consistency_pct,
            "recency_pct": recency_pct,
        },
        "raw": {
            "realized_pnl_per_position": realized_pnl_per_position,
            "n_held_to_resolution": n_held,
            "win_rate_held_only": win_rate_held_only,
            "wr_posterior_mean": wr_posterior_mean,
            "days_since_last": days_since_last,
            "concentration_raw": concentration_raw,
        },
        "peer_stats": {
            "tag": tag,
            "peer_count": peer_count,
            "peer_mean_wr_held": peer_mean_wr,
            "peer_median_n_held": peer_median_n,
            "k_prior_strength": k,
        },
        "weights": dict(WEIGHTS),
    }

    return FitRow(
        wallet=wallet,
        tag=tag,
        fit_score=fit_score,
        fit_rank=0,  # assigned by compute_all_fits
        peer_count=peer_count,
        confidence_flag=confidence_flag,
        computed_at=computed_at,
        inputs_json=json.dumps(inputs, sort_keys=True),
    )


# ---------------------------------------------------------------------------
# Table DDL
# ---------------------------------------------------------------------------


def ensure_wallet_tag_fit_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_tag_fit (
            wallet          TEXT    NOT NULL,
            tag             TEXT    NOT NULL,
            fit_score       REAL    NOT NULL,
            fit_rank        INTEGER NOT NULL,
            peer_count      INTEGER NOT NULL,
            confidence_flag TEXT    NOT NULL,
            computed_at     INTEGER NOT NULL,
            inputs_json     TEXT    NOT NULL,
            PRIMARY KEY (wallet, tag)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wtf_tag_rank ON wallet_tag_fit (tag, fit_rank)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wtf_wallet ON wallet_tag_fit (wallet)"
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _fetch_last_activity_ts_by_wallet_tag(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], int]:
    """Return {(wallet, tag): max(activity_ts)} from fwa joined to market_tags."""
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT fwa.wallet, mt.tag, MAX(fwa.activity_ts) AS last_ts
              FROM followed_wallet_activity fwa
              JOIN market_tags mt ON mt.condition_id = fwa.condition_id
             GROUP BY fwa.wallet, mt.tag
            """
        ).fetchall()
    finally:
        conn.row_factory = prev

    out: dict[tuple[str, str], int] = {}
    for r in rows:
        wallet = r["wallet"]
        tag = r["tag"]
        ts = int(r["last_ts"] or 0)
        if wallet and tag:
            out[(wallet, tag)] = ts
    return out


def compute_all_fits(
    conn: sqlite3.Connection,
    min_peers: int = 10,
) -> Iterable[FitRow]:
    """Compute fit scores for all (wallet, tag) pairs satisfying the floor.

    §5.1 reporting floor:
      - tag peer_count (wallets with n_held >= 3) >= min_peers
      - wallet n_held_to_resolution >= 3

    Persists results to wallet_tag_fit via upsert.
    Assigns fit_rank within each tag (1 = best).
    """
    ensure_wallet_tag_fit_table(conn)

    # Load peer stats (filters to n_held >= 3 internally)
    peer_stats_by_tag = compute_peer_stats(conn)

    # Load all wallet_tag_pnl rows for eligible wallets
    prev = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        pnl_rows = conn.execute(
            """
            SELECT wallet, tag, realized_pnl, n_positions,
                   win_rate, win_rate_held_only, n_held_to_resolution,
                   conviction_concentration, computed_at
              FROM wallet_tag_pnl
             WHERE n_held_to_resolution >= ?
            """,
            (MIN_N_HELD,),
        ).fetchall()
    finally:
        conn.row_factory = prev

    # Fetch recency data (last activity per wallet × tag)
    last_ts_map = _fetch_last_activity_ts_by_wallet_tag(conn)

    # Use computed_at from pnl row as "now" reference for recency
    # Build per-tag recency value distribution (needed for percentile ranking)
    # We need to know all recency_weights for a tag before we can percentile-rank.

    # First pass: gather per-tag rows with their recency raw values
    tag_rows_map: dict[str, list[dict]] = {}
    for r in pnl_rows:
        tag = r["tag"] or ""
        ps = peer_stats_by_tag.get(tag)
        if ps is None or ps.peer_count < min_peers:
            continue
        wallet = r["wallet"]
        last_ts = last_ts_map.get((wallet, tag), 0)
        computed_at = int(r["computed_at"] or 0)
        if last_ts > 0 and computed_at >= last_ts:
            days_since = (computed_at - last_ts) / 86400.0
        else:
            days_since = 0.0
        rw = recency_weight(days_since)
        tag_rows_map.setdefault(tag, []).append({
            "wallet": wallet,
            "tag": tag,
            "realized_pnl": float(r["realized_pnl"] or 0.0),
            "n_positions": int(r["n_positions"] or 1),
            "win_rate": float(r["win_rate"] or 0.0),
            "win_rate_held_only": float(r["win_rate_held_only"] or 0.0),
            "n_held_to_resolution": int(r["n_held_to_resolution"] or 0),
            "conviction_concentration": float(r["conviction_concentration"] or 0.0),
            "computed_at": computed_at,
            "last_activity_ts": last_ts,
            "_recency_weight": rw,
        })

    all_fit_rows: list[FitRow] = []

    for tag, rows_for_tag in tag_rows_map.items():
        ps = peer_stats_by_tag[tag]
        # Build recency distribution for this tag's eligible wallets
        recency_vals = [r["_recency_weight"] for r in rows_for_tag]
        # Attach to peer_stats (we mutate the object; it's tag-local)
        ps.recency_values = recency_vals

        tag_fit_rows: list[FitRow] = []
        for row_dict in rows_for_tag:
            try:
                fit_row = compute_wallet_tag_fit(row_dict, ps)
            except Exception:
                logger.exception(
                    "wallet_tag_fit computation failed wallet=%s tag=%s",
                    row_dict.get("wallet"), tag,
                )
                continue
            tag_fit_rows.append(fit_row)

        # Assign fit_rank within tag: sort descending by fit_score
        tag_fit_rows.sort(key=lambda r: r.fit_score, reverse=True)
        for i, fr in enumerate(tag_fit_rows, start=1):
            fr.fit_rank = i

        all_fit_rows.extend(tag_fit_rows)

    # Persist
    if all_fit_rows:
        upsert_wallet_tag_fit(conn, all_fit_rows)

    return all_fit_rows


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def upsert_wallet_tag_fit(
    conn: sqlite3.Connection,
    rows: Iterable[FitRow],
) -> int:
    """Idempotent upsert by (wallet, tag). Returns rows written."""
    ensure_wallet_tag_fit_table(conn)
    count = 0
    for fr in rows:
        conn.execute(
            """
            INSERT INTO wallet_tag_fit
                (wallet, tag, fit_score, fit_rank, peer_count,
                 confidence_flag, computed_at, inputs_json)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(wallet, tag) DO UPDATE SET
                fit_score       = excluded.fit_score,
                fit_rank        = excluded.fit_rank,
                peer_count      = excluded.peer_count,
                confidence_flag = excluded.confidence_flag,
                computed_at     = excluded.computed_at,
                inputs_json     = excluded.inputs_json
            """,
            (
                fr.wallet, fr.tag, fr.fit_score, fr.fit_rank,
                fr.peer_count, fr.confidence_flag, fr.computed_at,
                fr.inputs_json,
            ),
        )
        count += 1
    conn.commit()
    return count


__all__ = [
    "PeerStats",
    "FitRow",
    "sample_strength",
    "recency_weight",
    "bayesian_posterior_mean",
    "compute_peer_stats",
    "compute_wallet_tag_fit",
    "compute_all_fits",
    "ensure_wallet_tag_fit_table",
    "upsert_wallet_tag_fit",
]
