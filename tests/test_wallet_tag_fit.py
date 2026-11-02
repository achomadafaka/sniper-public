"""Failing acceptance suite for B2 `wallet_tag_fit`.

Spec: spec/tag-archetype-fit-2026-04-24 (memory_name: spec/tag-archetype-fit-2026-04-24).

These tests encode the §7.1 unit-test contract for:
- `sniper.analysis.wallet_tag_fit` pure functions
  - `compute_peer_stats(conn) -> dict[tag, PeerStats]`
  - `compute_wallet_tag_fit(wallet_tag_pnl_row, peer_stats) -> FitRow`
  - `compute_all_fits(conn, min_peers=10) -> Iterable[FitRow]`
- The `wallet_tag_fit` table (§5 schema)
- `db.wallet_tag_fit_rows` reader (§6)
- `scripts/build_wallet_tag_fit.py` CLI surface (§6)

Schema lookups performed:
  python3 scripts/brain_index.py schema wallet_tag_pnl       -> verified 2026-04-25
  python3 scripts/brain_index.py schema followed_wallet_activity -> verified 2026-04-25
  python3 scripts/brain_index.py schema market_tags          -> verified 2026-04-25

B1 patch is merged on main:
  wallet_tag_pnl now has win_rate_held_only (REAL nullable)
  and n_held_to_resolution (INTEGER NOT NULL DEFAULT 0).

Recency fixture strategy (§4.4):
  The spec says days_since_last must be derived from the wallet's most recent
  activity on that tag (joined from followed_wallet_activity via market_tags).
  For UNIT tests of compute_wallet_tag_fit() (pure function), we pass
  `last_activity_ts` directly in the wallet_tag_pnl_row dict so the implementer
  can wire DB→pure-fn however they like.  For INTEGRATION tests of
  compute_all_fits(), we insert followed_wallet_activity rows with the expected
  activity_ts.  Both patterns are labeled with comments below.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import time

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECONDS_PER_DAY = 86_400
NOW = 1_750_000_000  # arbitrary fixed "now" used throughout; avoids real time()

# Five wallets for the 5×2 fixture
W1 = "0xwallet000000000000000000000000000000001"
W2 = "0xwallet000000000000000000000000000000002"
W3 = "0xwallet000000000000000000000000000000003"
W4 = "0xwallet000000000000000000000000000000004"
W5 = "0xwallet000000000000000000000000000000005"

TAG_A = "sports"
TAG_B = "politics"


# ---------------------------------------------------------------------------
# In-memory DB helpers — mirror wallet_tag_pnl and related tables exactly.
# All column names verified against brain_index schema output.
# ---------------------------------------------------------------------------


def _make_conn() -> sqlite3.Connection:
    """Create tables needed by B2.

    wallet_tag_pnl: all columns including B1-patch columns.
    wallet_tag_fit: per-spec §5 schema (table being tested).
    followed_wallet_activity: subset of real schema (activity_ts is the key column).
    market_tags: for tag→condition_id mapping.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Source table for B2 inputs (B1 output)
    conn.execute("""
        CREATE TABLE wallet_tag_pnl (
            wallet                   TEXT    NOT NULL,
            tag                      TEXT    NOT NULL,
            n_positions              INTEGER NOT NULL,
            realized_pnl             REAL    NOT NULL,
            win_rate                 REAL    NOT NULL,
            avg_hold_days            REAL    NOT NULL,
            conviction_concentration REAL    NOT NULL,
            computed_at              INTEGER NOT NULL,
            win_rate_held_only       REAL,
            n_held_to_resolution     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (wallet, tag)
        )
    """)

    # B2 output table (spec §5)
    conn.execute("""
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
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wtf_tag_rank ON wallet_tag_fit (tag, fit_rank)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wtf_wallet ON wallet_tag_fit (wallet)"
    )

    # followed_wallet_activity — columns per brain_index (only used cols here)
    conn.execute("""
        CREATE TABLE followed_wallet_activity (
            activity_key  TEXT PRIMARY KEY,
            wallet        TEXT    NOT NULL,
            condition_id  TEXT    NOT NULL DEFAULT '',
            activity_type TEXT    NOT NULL DEFAULT '',
            side          TEXT    NOT NULL DEFAULT '',
            outcome       TEXT    NOT NULL DEFAULT '',
            price         REAL    NOT NULL DEFAULT 0,
            size          REAL    NOT NULL DEFAULT 0,
            usdc_size     REAL    NOT NULL DEFAULT 0,
            activity_ts   INTEGER NOT NULL DEFAULT 0
        )
    """)

    # market_tags — per brain_index schema
    conn.execute("""
        CREATE TABLE market_tags (
            condition_id TEXT NOT NULL,
            tag          TEXT NOT NULL,
            tag_label    TEXT,
            PRIMARY KEY (condition_id, tag)
        )
    """)

    return conn


def _insert_pnl_row(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    tag: str,
    n_positions: int = 10,
    realized_pnl: float = 0.0,
    win_rate: float = 0.5,
    avg_hold_days: float = 5.0,
    conviction_concentration: float = 0.5,
    win_rate_held_only: float | None = None,
    n_held_to_resolution: int = 10,
    computed_at: int = NOW,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO wallet_tag_pnl
            (wallet, tag, n_positions, realized_pnl, win_rate, avg_hold_days,
             conviction_concentration, computed_at, win_rate_held_only,
             n_held_to_resolution)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            wallet, tag, n_positions, realized_pnl, win_rate, avg_hold_days,
            conviction_concentration, computed_at,
            win_rate_held_only if win_rate_held_only is not None else win_rate,
            n_held_to_resolution,
        ),
    )


def _insert_activity(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    activity_ts: int,
    activity_type: str = "TRADE",
    side: str = "BUY",
    key_suffix: str = "",
) -> None:
    """Insert a followed_wallet_activity row for recency derivation in integration tests."""
    key = f"{wallet}:{condition_id}:{activity_ts}:{key_suffix}"
    conn.execute(
        """
        INSERT OR IGNORE INTO followed_wallet_activity
            (activity_key, wallet, condition_id, activity_type, side,
             outcome, price, size, usdc_size, activity_ts)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (key, wallet, condition_id, activity_type, side, "Yes", 0.5, 10.0, 5.0, activity_ts),
    )


def _insert_tag(conn: sqlite3.Connection, condition_id: str, tag: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO market_tags (condition_id, tag) VALUES (?,?)",
        (condition_id, tag),
    )


def _get(r, k):
    """Dict-or-Row-or-dataclass-safe field accessor."""
    if isinstance(r, dict):
        return r[k]
    try:
        return r[k]  # sqlite3.Row
    except (TypeError, IndexError, KeyError):
        return getattr(r, k)


# ---------------------------------------------------------------------------
# Build a compact peer set for a given tag.
# `specs` is a list of dicts: {wallet, realized_pnl, win_rate_held_only,
#                               n_held_to_resolution, conviction_concentration}
# ---------------------------------------------------------------------------


def _build_peers(conn, tag, specs):
    for s in specs:
        _insert_pnl_row(
            conn,
            wallet=s["wallet"],
            tag=tag,
            realized_pnl=s.get("realized_pnl", 0.0),
            win_rate_held_only=s.get("win_rate_held_only", 0.5),
            n_held_to_resolution=s.get("n_held_to_resolution", 10),
            conviction_concentration=s.get("conviction_concentration", 0.5),
        )


# ===========================================================================
# 0. SMOKE — module + CLI importability (§6)
# ===========================================================================


def test_module_exposes_required_symbols():
    """spec §6: module must expose three pure-function symbols."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    assert callable(getattr(mod, "compute_peer_stats", None)), (
        "compute_peer_stats() missing from sniper.analysis.wallet_tag_fit"
    )
    assert callable(getattr(mod, "compute_wallet_tag_fit", None)), (
        "compute_wallet_tag_fit() missing"
    )
    assert callable(getattr(mod, "compute_all_fits", None)), (
        "compute_all_fits() missing"
    )


def test_cli_script_importable_with_main():
    """spec §6: scripts/build_wallet_tag_fit.py must expose main()."""
    mod = importlib.import_module("scripts.build_wallet_tag_fit")
    assert callable(getattr(mod, "main", None)), (
        "scripts/build_wallet_tag_fit.py must expose main()"
    )


def test_db_reader_exposed():
    """spec §6: db.wallet_tag_fit_rows(conn, wallet=None, tag=None,
    confidence_only=False, limit=None) must be callable."""
    import db  # noqa: F401
    assert callable(getattr(db, "wallet_tag_fit_rows", None)), (
        "db.wallet_tag_fit_rows not found — must be added per spec §6"
    )


# ===========================================================================
# 1. HAPPY PATH — 5 wallets × 2 tags; known winner ranks #1 (§7.1 fixture)
# ===========================================================================


def _build_5x2_fixture(conn):
    """Insert 5 wallets across TAG_A and TAG_B.

    Wallet W1 is the clear winner on TAG_A (highest realized_pnl, high WR,
    many resolved positions, recent activity).
    Wallet W2 is the clear winner on TAG_B.

    Recency: we insert followed_wallet_activity rows with activity_ts so the
    implementation can derive days_since_last by joining fwa → market_tags.
    Each condition_id is unique per wallet/tag pair so the JOIN is unambiguous.
    """
    # TAG_A wallets (W1 wins)
    tag_a_data = [
        dict(wallet=W1, realized_pnl=5000.0, win_rate_held_only=0.80, n_held_to_resolution=20,
             conviction_concentration=0.3, cid="0xcid_a1"),
        dict(wallet=W2, realized_pnl=200.0,  win_rate_held_only=0.55, n_held_to_resolution=10,
             conviction_concentration=0.5, cid="0xcid_a2"),
        dict(wallet=W3, realized_pnl=-50.0,  win_rate_held_only=0.45, n_held_to_resolution=8,
             conviction_concentration=0.5, cid="0xcid_a3"),
        dict(wallet=W4, realized_pnl=100.0,  win_rate_held_only=0.60, n_held_to_resolution=12,
             conviction_concentration=0.4, cid="0xcid_a4"),
        dict(wallet=W5, realized_pnl=300.0,  win_rate_held_only=0.65, n_held_to_resolution=15,
             conviction_concentration=0.6, cid="0xcid_a5"),
    ]
    # TAG_B wallets (W2 wins)
    tag_b_data = [
        dict(wallet=W1, realized_pnl=50.0,   win_rate_held_only=0.52, n_held_to_resolution=5,
             conviction_concentration=0.5, cid="0xcid_b1"),
        dict(wallet=W2, realized_pnl=8000.0, win_rate_held_only=0.85, n_held_to_resolution=25,
             conviction_concentration=0.2, cid="0xcid_b2"),
        dict(wallet=W3, realized_pnl=100.0,  win_rate_held_only=0.58, n_held_to_resolution=9,
             conviction_concentration=0.5, cid="0xcid_b3"),
        dict(wallet=W4, realized_pnl=-200.0, win_rate_held_only=0.40, n_held_to_resolution=7,
             conviction_concentration=0.5, cid="0xcid_b4"),
        dict(wallet=W5, realized_pnl=400.0,  win_rate_held_only=0.70, n_held_to_resolution=14,
             conviction_concentration=0.4, cid="0xcid_b5"),
    ]

    for d in tag_a_data:
        _insert_pnl_row(
            conn, wallet=d["wallet"], tag=TAG_A,
            realized_pnl=d["realized_pnl"],
            win_rate_held_only=d["win_rate_held_only"],
            n_held_to_resolution=d["n_held_to_resolution"],
            conviction_concentration=d["conviction_concentration"],
        )
        _insert_tag(conn, d["cid"], TAG_A)
        # Recent activity: 5 days ago for all (recency not a differentiator here)
        _insert_activity(
            conn, wallet=d["wallet"], condition_id=d["cid"],
            activity_ts=NOW - 5 * SECONDS_PER_DAY,
        )

    for d in tag_b_data:
        _insert_pnl_row(
            conn, wallet=d["wallet"], tag=TAG_B,
            realized_pnl=d["realized_pnl"],
            win_rate_held_only=d["win_rate_held_only"],
            n_held_to_resolution=d["n_held_to_resolution"],
            conviction_concentration=d["conviction_concentration"],
        )
        _insert_tag(conn, d["cid"], TAG_B)
        _insert_activity(
            conn, wallet=d["wallet"], condition_id=d["cid"],
            activity_ts=NOW - 5 * SECONDS_PER_DAY,
        )

    return conn


def test_happy_path_known_winner_ranks_first_tag_a():
    """§7.1 fixture: W1 is clear winner on TAG_A → fit_rank == 1 (lowest rank int)."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    tag_a_rows = [r for r in rows if _get(r, "tag") == TAG_A]
    assert tag_a_rows, f"No {TAG_A} rows produced by compute_all_fits"

    best = min(tag_a_rows, key=lambda r: _get(r, "fit_rank"))
    assert _get(best, "wallet") == W1, (
        f"W1 should rank #1 on {TAG_A}; actual top wallet: {_get(best, 'wallet')}"
    )
    assert _get(best, "fit_rank") == 1, (
        f"fit_rank of winner should be 1; got {_get(best, 'fit_rank')}"
    )


def test_happy_path_known_winner_ranks_first_tag_b():
    """§7.1 fixture: W2 is clear winner on TAG_B → fit_rank == 1."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    tag_b_rows = [r for r in rows if _get(r, "tag") == TAG_B]
    assert tag_b_rows, f"No {TAG_B} rows produced"

    best = min(tag_b_rows, key=lambda r: _get(r, "fit_rank"))
    assert _get(best, "wallet") == W2, (
        f"W2 should rank #1 on {TAG_B}; actual top wallet: {_get(best, 'wallet')}"
    )


# ===========================================================================
# 2. WITHIN-TAG SCALING — all component percentiles and fit_score in [0, 1]
# ===========================================================================


def test_component_percentiles_in_0_1():
    """§7.1: all four component percentiles must be in [0, 1]."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    for row in rows:
        inp = json.loads(_get(row, "inputs_json"))
        for name, val in inp["components"].items():
            assert 0.0 <= val <= 1.0, (
                f"Component {name}={val} out of [0,1] for wallet={_get(row, 'wallet')}"
            )


def test_fit_score_in_0_1():
    """§7.1: fit_score must be in [0, 1] for all rows."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    for row in rows:
        fs = _get(row, "fit_score")
        assert 0.0 <= fs <= 1.0, f"fit_score={fs} out of [0,1]"


# ===========================================================================
# 3. BAYESIAN SHRINKAGE — 1W/0L wallet, peer_mean_wr=0.6 → posterior in [0.55, 0.75]
# ===========================================================================


def test_bayesian_shrinkage_helper():
    """§7.1: bayesian_posterior_mean(wins=1, n=1, mu=0.6, k=8) ∈ [0.55, 0.75].

    The spec gives: posterior = (wins + mu*k) / (n + k).
    Expected: (1 + 0.6*8) / (1 + 8) = (1 + 4.8) / 9 = 5.8 / 9 ≈ 0.644.
    Tests that the helper doesn't return 1.0 (raw WR) and is properly shrunk.
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    fn = getattr(mod, "bayesian_posterior_mean", None)
    assert callable(fn), (
        "sniper.analysis.wallet_tag_fit must expose bayesian_posterior_mean(wins, n, mu, k)"
    )
    result = fn(wins=1, n=1, mu=0.6, k=8)
    assert 0.55 <= result <= 0.75, (
        f"Expected posterior_mean in [0.55, 0.75], got {result}. "
        "Should NOT be 1.0 (raw WR). Shrinkage formula: (wins + mu*k) / (n + k)."
    )
    # Explicit check: must not be naively returning raw win rate (1.0)
    assert result != pytest.approx(1.0), "posterior_mean returned raw 1.0 — shrinkage not applied"


def test_bayesian_shrinkage_wired_in_inputs_json():
    """§7.1 (end-to-end): inputs_json.raw.wr_posterior_mean is shrunk, not raw WR.

    Fixture: wallet has win_rate_held_only=1.0 (3/3 wins), n_held=3.
    Peer mean WR ~0.6. Posterior should be clearly below 1.0.
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()

    # Build 10 peers on "gamma" tag with WR ~0.6 and n_held=10.
    # The focal wallet has WR=1.0, n_held=3 (minimum floor).
    for i in range(9):
        w = f"0xpeer{i:040d}"
        _insert_pnl_row(conn, wallet=w, tag="gamma",
                        realized_pnl=float(i * 10 - 40),  # some +, some -
                        win_rate_held_only=0.60,
                        n_held_to_resolution=10)
        _insert_tag(conn, f"0xcid_g{i}", "gamma")
        _insert_activity(conn, wallet=w, condition_id=f"0xcid_g{i}",
                         activity_ts=NOW - 10 * SECONDS_PER_DAY)

    focal = "0xfocal" + "0" * 34
    _insert_pnl_row(conn, wallet=focal, tag="gamma",
                    realized_pnl=500.0,
                    win_rate_held_only=1.0,
                    n_held_to_resolution=3)
    _insert_tag(conn, "0xcid_focal", "gamma")
    _insert_activity(conn, wallet=focal, condition_id="0xcid_focal",
                     activity_ts=NOW - 2 * SECONDS_PER_DAY)

    rows = list(mod.compute_all_fits(conn, min_peers=10))
    focal_rows = [r for r in rows if _get(r, "wallet") == focal]
    assert focal_rows, "Focal wallet (1W/0L) not in compute_all_fits output"

    inp = json.loads(_get(focal_rows[0], "inputs_json"))
    pm = inp["raw"]["wr_posterior_mean"]
    assert pm < 1.0, (
        f"Posterior mean should be shrunk below 1.0; got {pm}. "
        "Raw win_rate_held_only=1.0 is being used instead of Bayesian posterior."
    )
    assert pm >= 0.55, f"Posterior mean {pm} implausibly low (peer_mean ≈ 0.60)"


# ===========================================================================
# 4. RECENCY DECAY — days_since_last=90 → recency_weight ≈ 0.5
# ===========================================================================


def test_recency_weight_helper_90_days():
    """§7.1: recency_weight(90) == 0.5 ± 0.01.

    Formula: 0.5 ** (days / 90). At 90 days: 0.5 ** 1 = 0.5.
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    fn = getattr(mod, "recency_weight", None)
    assert callable(fn), (
        "sniper.analysis.wallet_tag_fit must expose recency_weight(days_since_last)"
    )
    w = fn(90)
    assert abs(w - 0.5) <= 0.01, f"Expected recency_weight(90)≈0.5, got {w}"


def test_recency_weight_helper_0_days():
    """Sanity: recency_weight(0) == 1.0 (just traded)."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    fn = mod.recency_weight
    assert fn(0) == pytest.approx(1.0), "recency_weight(0) should be 1.0"


def test_recency_weight_helper_180_days():
    """Sanity: recency_weight(180) == 0.25 (two half-lives)."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    fn = mod.recency_weight
    assert fn(180) == pytest.approx(0.25, abs=0.01), (
        f"recency_weight(180) should be 0.25; got {fn(180)}"
    )


# ===========================================================================
# 5. CONCENTRATION ZEROED — changing concentration_raw does NOT change fit_score
# ===========================================================================


def test_concentration_does_not_affect_fit_score():
    """§7.1: weight=0 means concentration_raw changes do not move fit_score.

    We call compute_wallet_tag_fit twice with identical rows except
    concentration_raw differs dramatically. fit_score must be identical.

    The pure-function signature allows this without DB.
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    fn = getattr(mod, "compute_wallet_tag_fit", None)
    assert callable(fn)

    # Minimal peer_stats object — exact type depends on implementation.
    # We build a dict and let the implementer accept dict or typed object.
    peer_stats = {
        "tag": TAG_A,
        "peer_count": 15,
        "peer_mean_wr_held": 0.60,
        "peer_median_n_held": 8,
        "k_prior_strength": 8,
        "edge_values": [100.0, 200.0, 300.0, 400.0, 500.0,
                        600.0, 700.0, 800.0, 900.0, 1000.0,
                        1100.0, 1200.0, 1300.0, 1400.0, 1500.0],
        "recency_values": [0.2, 0.4, 0.5, 0.6, 0.7,
                           0.75, 0.8, 0.82, 0.85, 0.88,
                           0.9, 0.92, 0.93, 0.95, 0.98],
        "consistency_posteriors": [0.3, 0.4, 0.45, 0.5, 0.55,
                                   0.60, 0.62, 0.65, 0.70, 0.72,
                                   0.75, 0.78, 0.80, 0.82, 0.85],
        "sample_values": [0.1, 0.2, 0.3, 0.4, 0.45,
                          0.50, 0.55, 0.60, 0.65, 0.70,
                          0.72, 0.75, 0.78, 0.80, 0.85],
    }

    # Row with low concentration
    row_low = {
        "wallet": W1, "tag": TAG_A,
        "realized_pnl": 1000.0, "n_positions": 10,
        "win_rate_held_only": 0.70,
        "n_held_to_resolution": 10,
        "conviction_concentration": 0.10,  # low
        "last_activity_ts": NOW - 5 * SECONDS_PER_DAY,
        "computed_at": NOW,
    }
    # Same row with extreme concentration
    row_high = {**row_low, "conviction_concentration": 0.99}

    result_low = fn(row_low, peer_stats)
    result_high = fn(row_high, peer_stats)

    score_low = _get(result_low, "fit_score") if not isinstance(result_low, (int, float)) else result_low
    score_high = _get(result_high, "fit_score") if not isinstance(result_high, (int, float)) else result_high

    assert score_low == pytest.approx(score_high, abs=1e-9), (
        f"fit_score changed when concentration_raw changed: {score_low} vs {score_high}. "
        "Concentration weight must be 0.0 in v1."
    )


# ===========================================================================
# 6. CROSS-TAG NOT COMPARABLE — spec §4.6 and §2
# ===========================================================================


def test_cross_tag_scores_not_globally_orderable():
    """§7.1 cross-tag NOT comparable: within-tag percentile rank only.

    Setup:
      TAG_A: W1 has realized_pnl_per_position=5000 (dominant), other 4 at ~0.
      TAG_B: W2 has realized_pnl_per_position=10 (dominant in that tag), others at ~-5.

    W1 is objectively better on absolute PnL. But within TAG_B, W2 is rank 1
    and has fit_score ≈ 1.0.  W1 in TAG_B might have fit_score lower than W2.

    The test proves: the two #1-rank wallets BOTH have fit_score near 1.0
    despite very different absolute PnL magnitudes, i.e. within-tag scaling.
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()

    WALLETS = [f"0x{str(i).zfill(40)}" for i in range(1, 6)]

    # TAG_A: W0 utterly dominant (pnl_per_pos = 5000 / 10 = 500)
    tag_a_specs = [
        (WALLETS[0], 5000.0, 0.90, 20),
        (WALLETS[1], -10.0, 0.45, 5),
        (WALLETS[2],   5.0, 0.50, 4),
        (WALLETS[3],  10.0, 0.55, 6),
        (WALLETS[4],  20.0, 0.52, 7),
    ]
    # TAG_B: W1 dominant but with small absolute values (pnl_per_pos = 50/5 = 10)
    tag_b_specs = [
        (WALLETS[0],  -50.0, 0.40, 5),
        (WALLETS[1],   50.0, 0.75, 5),
        (WALLETS[2],  -30.0, 0.42, 4),
        (WALLETS[3],  -20.0, 0.45, 6),
        (WALLETS[4],  -40.0, 0.43, 7),
    ]

    for i, (w, pnl, wr, n_held) in enumerate(tag_a_specs):
        cid = f"0xcida{i}"
        _insert_pnl_row(conn, wallet=w, tag=TAG_A, realized_pnl=pnl,
                        win_rate_held_only=wr, n_held_to_resolution=n_held)
        _insert_tag(conn, cid, TAG_A)
        _insert_activity(conn, wallet=w, condition_id=cid,
                         activity_ts=NOW - 5 * SECONDS_PER_DAY)

    for i, (w, pnl, wr, n_held) in enumerate(tag_b_specs):
        cid = f"0xcidb{i}"
        _insert_pnl_row(conn, wallet=w, tag=TAG_B, realized_pnl=pnl,
                        win_rate_held_only=wr, n_held_to_resolution=n_held)
        _insert_tag(conn, cid, TAG_B)
        _insert_activity(conn, wallet=w, condition_id=cid,
                         activity_ts=NOW - 5 * SECONDS_PER_DAY)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    a_rows = {_get(r, "wallet"): r for r in rows if _get(r, "tag") == TAG_A}
    b_rows = {_get(r, "wallet"): r for r in rows if _get(r, "tag") == TAG_B}

    assert WALLETS[0] in a_rows, "TAG_A winner not in output"
    assert WALLETS[1] in b_rows, "TAG_B winner not in output"

    # Both tag-winners should have high fit_score (>= 0.8)
    assert _get(a_rows[WALLETS[0]], "fit_score") >= 0.8, (
        "TAG_A dominant wallet should have fit_score >= 0.8"
    )
    assert _get(b_rows[WALLETS[1]], "fit_score") >= 0.8, (
        "TAG_B dominant wallet should have fit_score >= 0.8 despite low absolute PnL"
    )

    # Within-tag rank 1 for each
    assert _get(a_rows[WALLETS[0]], "fit_rank") == 1
    assert _get(b_rows[WALLETS[1]], "fit_rank") == 1


# ===========================================================================
# 7. REPORTING FLOOR — peer_count thresholds and confidence_flag
# ===========================================================================


def _build_n_wallet_tag(conn, tag, n_wallets, n_held=10):
    """Insert n_wallets with n_held_to_resolution=n_held into a tag."""
    for i in range(n_wallets):
        w = f"0xf{tag[:3]}{i:036d}"
        _insert_pnl_row(
            conn, wallet=w, tag=tag,
            realized_pnl=float(i * 10 - n_wallets * 5),
            win_rate_held_only=0.55 + 0.001 * i,
            n_held_to_resolution=n_held,
        )
        cid = f"0xcid_{tag}_{i}"
        _insert_tag(conn, cid, tag)
        _insert_activity(conn, wallet=w, condition_id=cid,
                         activity_ts=NOW - (i % 30 + 1) * SECONDS_PER_DAY)


def test_confidence_flag_low_when_peer_count_25():
    """§7.1: peer_count=25 (< 30) → confidence_flag='low_confidence'."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_n_wallet_tag(conn, "tagX25", 25)

    rows = list(mod.compute_all_fits(conn, min_peers=10))
    tag_rows = [r for r in rows if _get(r, "tag") == "tagX25"]
    assert tag_rows, "No rows for 25-peer tag (expected because peer_count >= 10)"

    for row in tag_rows:
        assert _get(row, "confidence_flag") == "low_confidence", (
            f"peer_count=25 should yield 'low_confidence'; got {_get(row, 'confidence_flag')}"
        )
        assert _get(row, "peer_count") == 25


def test_confidence_flag_ok_when_peer_count_35():
    """§7.1: peer_count=35 (>= 30) → confidence_flag='ok'."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_n_wallet_tag(conn, "tagY35", 35)

    rows = list(mod.compute_all_fits(conn, min_peers=10))
    tag_rows = [r for r in rows if _get(r, "tag") == "tagY35"]
    assert tag_rows, "No rows for 35-peer tag"

    for row in tag_rows:
        assert _get(row, "confidence_flag") == "ok", (
            f"peer_count=35 should yield 'ok'; got {_get(row, 'confidence_flag')}"
        )


def test_excluded_when_peer_count_below_10():
    """§7.1 reporting floor: tag with only 9 peers → no rows written (peer_count < 10)."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_n_wallet_tag(conn, "thinTag", 9)

    rows = list(mod.compute_all_fits(conn, min_peers=10))
    thin_rows = [r for r in rows if _get(r, "tag") == "thinTag"]
    assert thin_rows == [], (
        f"peer_count=9 should produce no rows; got {len(thin_rows)}"
    )


# ===========================================================================
# 8. MIN SAMPLE — wallet with n_held_to_resolution=2 → excluded from peers
# ===========================================================================


def test_wallet_excluded_when_n_held_lt_3():
    """§5.1: wallet with n_held_to_resolution=2 must not appear in output.

    Even if the tag has 15 other wallets with n_held>=3, the thin wallet
    should neither appear in output rows nor be counted as a peer for scoring.
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()

    # 12 wallets with n_held=10 (valid peers)
    _build_n_wallet_tag(conn, "testTag", 12, n_held=10)

    # One thin wallet with n_held=2 — must be excluded
    thin_wallet = "0xthin" + "0" * 35
    _insert_pnl_row(conn, wallet=thin_wallet, tag="testTag",
                    realized_pnl=9999.0,  # would dominate if included
                    win_rate_held_only=1.0,
                    n_held_to_resolution=2)
    _insert_tag(conn, "0xcid_thin", "testTag")
    _insert_activity(conn, wallet=thin_wallet, condition_id="0xcid_thin",
                     activity_ts=NOW - SECONDS_PER_DAY)

    rows = list(mod.compute_all_fits(conn, min_peers=10))
    tag_rows = [r for r in rows if _get(r, "tag") == "testTag"]

    # Thin wallet must not appear in output
    thin_in_output = [r for r in tag_rows if _get(r, "wallet") == thin_wallet]
    assert thin_in_output == [], (
        f"Wallet with n_held=2 must not appear in output; it appeared in {len(thin_in_output)} rows"
    )

    # It also must not inflate peer_count (peers should be 12, not 13)
    for row in tag_rows:
        assert _get(row, "peer_count") == 12, (
            f"peer_count should be 12 (excluding thin wallet); got {_get(row, 'peer_count')}"
        )


# ===========================================================================
# 9. DETERMINISM — same input → identical output
# ===========================================================================


def test_determinism_same_input_same_output():
    """§7.1: compute_all_fits is deterministic — two runs produce identical fit_scores."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    run1 = {
        (_get(r, "wallet"), _get(r, "tag")): _get(r, "fit_score")
        for r in mod.compute_all_fits(conn, min_peers=5)
    }
    run2 = {
        (_get(r, "wallet"), _get(r, "tag")): _get(r, "fit_score")
        for r in mod.compute_all_fits(conn, min_peers=5)
    }

    assert run1 == run2, (
        "compute_all_fits is not deterministic — two identical runs produced different fit_scores"
    )
    assert run1, "No rows produced; cannot verify determinism"


def test_determinism_inputs_json_identical():
    """§7.1: inputs_json must also be bit-identical across runs."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    def _collect(conn):
        return {
            (_get(r, "wallet"), _get(r, "tag")): _get(r, "inputs_json")
            for r in mod.compute_all_fits(conn, min_peers=5)
        }

    j1 = _collect(conn)
    j2 = _collect(conn)
    assert j1 == j2, "inputs_json differs between identical runs"


# ===========================================================================
# 10. EMPTY DATA — no positions → no rows, no crash
# ===========================================================================


def test_empty_wallet_tag_pnl_produces_no_rows():
    """§7.1: empty wallet_tag_pnl → no rows, no exception."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    # No rows inserted at all
    rows = list(mod.compute_all_fits(conn, min_peers=10))
    assert rows == [], f"Expected empty result for empty DB; got {rows}"


def test_empty_tag_produces_no_crash():
    """Empty tag (0 wallets) must not crash compute_peer_stats."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    # Insert a tag mapping but no wallet_tag_pnl rows for it
    _insert_tag(conn, "0xempty", "empty_tag")
    # Must not raise
    peer_stats = mod.compute_peer_stats(conn)
    assert isinstance(peer_stats, dict), "compute_peer_stats must return a dict"


# ===========================================================================
# 11. inputs_json SHAPE — all required keys, weights sum to 1.0, concentration=0.0
# ===========================================================================


def test_inputs_json_all_keys_present():
    """§5.2: inputs_json must contain version, components, raw, peer_stats, weights."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    assert rows, "No rows to inspect"

    required_top = {"version", "components", "raw", "peer_stats", "weights"}
    required_components = {"edge_pct", "sample_pct", "consistency_pct", "recency_pct"}
    required_raw = {
        "realized_pnl_per_position", "n_held_to_resolution",
        "win_rate_held_only", "wr_posterior_mean", "days_since_last",
        "concentration_raw",
    }
    required_peer_stats = {
        "tag", "peer_count", "peer_mean_wr_held",
        "peer_median_n_held", "k_prior_strength",
    }
    required_weights = {"edge", "sample", "consistency", "recency", "concentration"}

    for row in rows:
        inp = json.loads(_get(row, "inputs_json"))

        missing_top = required_top - inp.keys()
        assert not missing_top, f"inputs_json missing top-level keys: {missing_top}"

        missing_comp = required_components - inp["components"].keys()
        assert not missing_comp, f"inputs_json.components missing: {missing_comp}"

        missing_raw = required_raw - inp["raw"].keys()
        assert not missing_raw, f"inputs_json.raw missing: {missing_raw}"

        missing_ps = required_peer_stats - inp["peer_stats"].keys()
        assert not missing_ps, f"inputs_json.peer_stats missing: {missing_ps}"

        missing_w = required_weights - inp["weights"].keys()
        assert not missing_w, f"inputs_json.weights missing: {missing_w}"


def test_inputs_json_version_v1():
    """§5.2: version must be 'v1'."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    for row in rows:
        inp = json.loads(_get(row, "inputs_json"))
        assert inp["version"] == "v1", (
            f"inputs_json.version must be 'v1'; got {inp['version']}"
        )


def test_inputs_json_weights_sum_to_1():
    """§5.2 and §4: edge+sample+consistency+recency+concentration == 1.0."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    for row in rows:
        inp = json.loads(_get(row, "inputs_json"))
        w = inp["weights"]
        total = w["edge"] + w["sample"] + w["consistency"] + w["recency"] + w["concentration"]
        assert total == pytest.approx(1.0), (
            f"weights sum must be 1.0; got {total} for wallet={_get(row, 'wallet')}"
        )


def test_inputs_json_concentration_weight_is_zero():
    """§4.5: concentration weight must be 0.0 in v1."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    for row in rows:
        inp = json.loads(_get(row, "inputs_json"))
        assert inp["weights"]["concentration"] == 0.0, (
            f"concentration weight must be 0.0; got {inp['weights']['concentration']}"
        )


def test_inputs_json_equal_weights_025():
    """§4: four active components have equal weight = 0.25 each in v1."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    for row in rows:
        inp = json.loads(_get(row, "inputs_json"))
        w = inp["weights"]
        for key in ("edge", "sample", "consistency", "recency"):
            assert w[key] == pytest.approx(0.25), (
                f"weights[{key}] must be 0.25 in v1; got {w[key]}"
            )


def test_inputs_json_concentration_raw_persisted():
    """§4.5: concentration_raw must appear in inputs_json.raw even at weight=0."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    rows = list(mod.compute_all_fits(conn, min_peers=5))
    for row in rows:
        inp = json.loads(_get(row, "inputs_json"))
        assert "concentration_raw" in inp["raw"], (
            "inputs_json.raw must persist concentration_raw for auditability"
        )


# ===========================================================================
# 12. SAMPLE STRENGTH formula check — sqrt-scale capped at n=100 (§4.2)
# ===========================================================================


def test_sample_strength_helper_formula():
    """§4.2: sample_strength(n) = min(sqrt(n) / sqrt(100), 1.0).

    n=25 → sqrt(25)/10 = 0.5
    n=100 → 1.0
    n=400 → capped at 1.0
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    fn = getattr(mod, "sample_strength", None)
    assert callable(fn), (
        "sniper.analysis.wallet_tag_fit must expose sample_strength(n_held)"
    )
    assert fn(25) == pytest.approx(0.5, abs=0.01), f"sample_strength(25) should be 0.5; got {fn(25)}"
    assert fn(100) == pytest.approx(1.0, abs=0.001), f"sample_strength(100) should be 1.0; got {fn(100)}"
    assert fn(400) == pytest.approx(1.0, abs=0.001), f"sample_strength(400) should be capped at 1.0; got {fn(400)}"
    assert fn(0) == pytest.approx(0.0, abs=0.001), f"sample_strength(0) should be 0.0; got {fn(0)}"


# ===========================================================================
# 13. READER — db.wallet_tag_fit_rows contract (§6)
# ===========================================================================


def _insert_fit_row(conn, wallet, tag, fit_score, fit_rank, peer_count, flag, inputs=None):
    conn.execute(
        """
        INSERT OR REPLACE INTO wallet_tag_fit
            (wallet, tag, fit_score, fit_rank, peer_count, confidence_flag,
             computed_at, inputs_json)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (wallet, tag, fit_score, fit_rank, peer_count, flag, NOW,
         inputs or json.dumps({"version": "v1"})),
    )


def test_db_reader_filter_by_wallet():
    """§6 reader: wallet= filter returns only rows for that wallet."""
    import db  # noqa: F401
    conn = _make_conn()

    _insert_fit_row(conn, W1, TAG_A, 0.9, 1, 35, "ok")
    _insert_fit_row(conn, W1, TAG_B, 0.7, 2, 35, "ok")
    _insert_fit_row(conn, W2, TAG_A, 0.5, 3, 35, "ok")
    conn.commit()

    rows = list(db.wallet_tag_fit_rows(conn, wallet=W1))
    assert len(rows) == 2, f"Expected 2 rows for W1; got {len(rows)}"
    tags = {_get(r, "tag") for r in rows}
    assert tags == {TAG_A, TAG_B}


def test_db_reader_filter_by_tag():
    """§6 reader: tag= filter returns only rows for that tag."""
    import db  # noqa: F401
    conn = _make_conn()

    _insert_fit_row(conn, W1, TAG_A, 0.9, 1, 35, "ok")
    _insert_fit_row(conn, W2, TAG_A, 0.5, 2, 35, "ok")
    _insert_fit_row(conn, W1, TAG_B, 0.7, 1, 35, "ok")
    conn.commit()

    rows = list(db.wallet_tag_fit_rows(conn, tag=TAG_A))
    assert len(rows) == 2, f"Expected 2 rows for {TAG_A}; got {len(rows)}"


def test_db_reader_no_filter_returns_all():
    """§6 reader: no filter returns all rows."""
    import db  # noqa: F401
    conn = _make_conn()

    _insert_fit_row(conn, W1, TAG_A, 0.9, 1, 35, "ok")
    _insert_fit_row(conn, W2, TAG_A, 0.5, 2, 35, "ok")
    _insert_fit_row(conn, W1, TAG_B, 0.7, 1, 35, "ok")
    conn.commit()

    rows = list(db.wallet_tag_fit_rows(conn))
    assert len(rows) == 3, f"Expected 3 rows; got {len(rows)}"


def test_db_reader_confidence_only_filter():
    """§6 reader: confidence_only=True returns only rows with confidence_flag='ok'."""
    import db  # noqa: F401
    conn = _make_conn()

    _insert_fit_row(conn, W1, TAG_A, 0.9, 1, 35, "ok")
    _insert_fit_row(conn, W2, TAG_A, 0.5, 2, 25, "low_confidence")
    conn.commit()

    rows = list(db.wallet_tag_fit_rows(conn, confidence_only=True))
    assert len(rows) == 1
    assert _get(rows[0], "confidence_flag") == "ok"


def test_db_reader_limit():
    """§6 reader: limit= parameter caps output rows."""
    import db  # noqa: F401
    conn = _make_conn()

    for i, w in enumerate([W1, W2, W3, W4, W5]):
        _insert_fit_row(conn, w, TAG_A, float(i) * 0.2, i + 1, 35, "ok")
    conn.commit()

    rows = list(db.wallet_tag_fit_rows(conn, limit=3))
    assert len(rows) == 3, f"Expected 3 rows with limit=3; got {len(rows)}"


# ===========================================================================
# 14. SCHEMA PERSISTENCE — wallet_tag_fit table DDL per spec §5
# These tests verify the module's own table-creation function (not the fixture).
# ===========================================================================


def _make_conn_no_fit_table() -> sqlite3.Connection:
    """Like _make_conn but does NOT pre-create wallet_tag_fit.
    Used to verify the module creates it itself."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE wallet_tag_pnl (
            wallet                   TEXT    NOT NULL,
            tag                      TEXT    NOT NULL,
            n_positions              INTEGER NOT NULL,
            realized_pnl             REAL    NOT NULL,
            win_rate                 REAL    NOT NULL,
            avg_hold_days            REAL    NOT NULL,
            conviction_concentration REAL    NOT NULL,
            computed_at              INTEGER NOT NULL,
            win_rate_held_only       REAL,
            n_held_to_resolution     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (wallet, tag)
        )
    """)
    conn.execute("""
        CREATE TABLE followed_wallet_activity (
            activity_key  TEXT PRIMARY KEY,
            wallet        TEXT    NOT NULL,
            condition_id  TEXT    NOT NULL DEFAULT '',
            activity_type TEXT    NOT NULL DEFAULT '',
            side          TEXT    NOT NULL DEFAULT '',
            outcome       TEXT    NOT NULL DEFAULT '',
            price         REAL    NOT NULL DEFAULT 0,
            size          REAL    NOT NULL DEFAULT 0,
            usdc_size     REAL    NOT NULL DEFAULT 0,
            activity_ts   INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE market_tags (
            condition_id TEXT NOT NULL,
            tag          TEXT NOT NULL,
            tag_label    TEXT,
            PRIMARY KEY (condition_id, tag)
        )
    """)
    return conn


def test_wallet_tag_fit_table_has_required_columns():
    """§5: compute_all_fits must create wallet_tag_fit with the required columns.

    We use a fresh connection WITHOUT wallet_tag_fit pre-created, then call
    compute_all_fits (which must create the table), and verify all spec §5
    columns are present. This tests the MODULE creates the right schema, not
    the test fixture.
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn_no_fit_table()

    # Insert enough data to trigger table creation
    _build_n_wallet_tag.__wrapped__(conn, "schema_test", 10) if hasattr(_build_n_wallet_tag, "__wrapped__") else None
    for i in range(10):
        w = f"0xsch{i:036d}"
        _insert_pnl_row(conn, wallet=w, tag="schema_test",
                        realized_pnl=float(i * 10),
                        win_rate_held_only=0.55,
                        n_held_to_resolution=5)
        _insert_tag(conn, f"0xcid_sch{i}", "schema_test")
        _insert_activity(conn, wallet=w, condition_id=f"0xcid_sch{i}",
                         activity_ts=NOW - 5 * SECONDS_PER_DAY)

    # This must create the table if it doesn't exist
    list(mod.compute_all_fits(conn, min_peers=10))

    info = conn.execute("PRAGMA table_info(wallet_tag_fit)").fetchall()
    assert info, "wallet_tag_fit table was not created by compute_all_fits"
    col_names = {row[1] for row in info}
    required = {
        "wallet", "tag", "fit_score", "fit_rank",
        "peer_count", "confidence_flag", "computed_at", "inputs_json",
    }
    missing = required - col_names
    assert not missing, f"wallet_tag_fit missing columns: {missing}"


def test_wallet_tag_fit_primary_key_is_wallet_tag():
    """§5: compute_all_fits idempotent — running twice on same data keeps one row per (wallet, tag).

    PRIMARY KEY (wallet, tag) means a second run must upsert, not duplicate.
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    # Run twice
    list(mod.compute_all_fits(conn, min_peers=5))
    list(mod.compute_all_fits(conn, min_peers=5))

    # For any (wallet, tag) pair, row count must be exactly 1
    rows = conn.execute("SELECT wallet, tag, COUNT(*) AS c FROM wallet_tag_fit GROUP BY wallet, tag HAVING c > 1").fetchall()
    assert rows == [], (
        f"Duplicate (wallet,tag) rows found after two runs: {[(r[0], r[1], r[2]) for r in rows]}. "
        "compute_all_fits must UPSERT, not INSERT."
    )


# ===========================================================================
# 15. COMPUTE_PEER_STATS returns correct summary (unit, §6 module spec)
# ===========================================================================


def test_compute_peer_stats_structure():
    """§6: compute_peer_stats(conn) returns a dict keyed by tag.

    Each value must expose tag, peer_count, peer_mean_wr_held,
    peer_median_n_held, k_prior_strength.
    """
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()
    _build_5x2_fixture(conn)

    stats = mod.compute_peer_stats(conn)
    assert isinstance(stats, dict), "compute_peer_stats must return a dict"
    assert TAG_A in stats or TAG_B in stats, (
        f"Expected {TAG_A} or {TAG_B} in peer_stats; got {list(stats.keys())}"
    )

    for tag, ps in stats.items():
        # Accept dict or typed object
        for field in ("peer_count", "peer_mean_wr_held", "peer_median_n_held", "k_prior_strength"):
            val = ps[field] if isinstance(ps, dict) else getattr(ps, field)
            assert val is not None, f"peer_stats.{field} is None for tag={tag}"


def test_compute_peer_stats_k_prior_floor():
    """§4.3: k = max(5, median peer n_held_to_resolution). With median=3, k must be 5."""
    mod = importlib.import_module("sniper.analysis.wallet_tag_fit")
    conn = _make_conn()

    # Build tag with 10 wallets, each having n_held=3 (median=3 → k=max(5,3)=5)
    for i in range(10):
        w = f"0xktest{i:035d}"
        _insert_pnl_row(conn, wallet=w, tag="ktest",
                        win_rate_held_only=0.60, n_held_to_resolution=3)
        _insert_tag(conn, f"0xcid_k{i}", "ktest")
        _insert_activity(conn, wallet=w, condition_id=f"0xcid_k{i}",
                         activity_ts=NOW - 10 * SECONDS_PER_DAY)

    stats = mod.compute_peer_stats(conn)
    assert "ktest" in stats, "ktest tag not in compute_peer_stats output"

    ps = stats["ktest"]
    k = ps["k_prior_strength"] if isinstance(ps, dict) else ps.k_prior_strength
    assert k == 5, (
        f"k_prior_strength must be max(5, median_n_held)=max(5,3)=5; got {k}"
    )
