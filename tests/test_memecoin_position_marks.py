import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_exits import write_marks

def test_write_marks_one_row_per_chain_mint_sweep():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    price_map = {("solana", "MINTA"): {"price": 0.001, "liquidity": 8000.0, "mcap": 5e5},
                 ("4663", "0xabc"):    {"price": 2.0,   "liquidity": 3000.0, "mcap": 2e6}}
    write_marks(c, price_map, "2026-07-12T10:00:00+00:00")
    write_marks(c, price_map, "2026-07-12T10:00:00+00:00")   # same sweep_ts -> no dup
    rows = c.execute("SELECT chain_id, mint, mid FROM memecoin_position_marks ORDER BY mint").fetchall()
    assert len(rows) == 2
    assert rows[0]["chain_id"] == "4663" and rows[0]["mid"] == 2.0


def test_write_marks_dedupes_per_coin_within_min_interval():
    """2026-07-17: memecoin_position_marks grew ~20k rows/2h on 4663 — the ~15s sweep re-wrote a
    mark per coin per sweep even when the (TTL-cached) price hadn't been refetched. Writes now
    dedupe per (chain, mint): a mark younger than MEMECOIN_MARK_WRITE_MIN_INTERVAL_S skips."""
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    pm = {("4663", "0xabc"): {"price": 2.0, "liquidity": 3000.0, "mcap": 2e6}}
    write_marks(c, pm, "2026-07-17T10:00:00+00:00", min_interval_s=30.0)
    write_marks(c, pm, "2026-07-17T10:00:15+00:00", min_interval_s=30.0)   # 15s later -> deduped
    n = c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0]
    assert n == 1
    write_marks(c, pm, "2026-07-17T10:00:31+00:00", min_interval_s=30.0)   # 31s later -> written
    n = c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0]
    assert n == 2


def test_write_marks_dedupe_is_per_coin_not_global():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    write_marks(c, {("4663", "0xa"): {"price": 1.0}}, "2026-07-17T10:00:00+00:00",
                min_interval_s=30.0)
    # a DIFFERENT coin 5s later must still be written
    write_marks(c, {("4663", "0xb"): {"price": 1.0}}, "2026-07-17T10:00:05+00:00",
                min_interval_s=30.0)
    n = c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0]
    assert n == 2


# ── open-position tape floor (Phase 2 Unit B, 2026-07-22) ───────────────────────
#
# The 30s universe dedupe is a RECORDING floor, and on Solana the tape is pinned
# against it (24h histogram: 64.8% of gaps in 30-35s, 0.1% below 30s). That floor is
# right for thousands of tracked coins and wrong for the <=7 open funded positions,
# whose rows are the only ones any rail post-mortem ever reads. Open positions get
# their own, much lower floor.

def _open_pos(c, network_id, mint):
    return m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                           entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                           entry_lag_s=-1.0, their_size_usd=0.0,
                           opened_at="2026-07-22T10:00:00+00:00",
                           strategy="alpha_b", execution_mode="real", network_id=network_id)


def test_open_position_marks_use_the_lower_floor():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    _open_pos(c, "4663", "0xopen")
    pm = {("4663", "0xopen"): {"price": 2.0}}
    write_marks(c, pm, "2026-07-22T10:00:00+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    write_marks(c, pm, "2026-07-22T10:00:03+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 1  # <5s
    write_marks(c, pm, "2026-07-22T10:00:06+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 2  # >5s


def test_a_coin_closed_inside_the_post_close_window_keeps_the_lower_floor(monkeypatch):
    """2026-09-23, operator: the minutes after a stop must be on the tape at swap resolution, so a
    coin closed within MEMECOIN_POST_CLOSE_MARK_S keeps the open floor, not the 30 s one."""
    from sniper.settings import memecoin as cfg
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    pid = _open_pos(c, "solana", "MintDone")
    from sniper.persistence import db_memecoin as m
    m.claim_close(c, pid)
    m.finalize_close(c, pid, "2026-07-22T09:59:50+00:00", -1.0, exit_reason="b_stop")
    monkeypatch.setattr(cfg, "MEMECOIN_POST_CLOSE_MARK_S", 120.0, raising=False)
    monkeypatch.setattr("sniper.util.clock.real_utc_now_iso", lambda: "2026-07-22T10:00:00+00:00")
    pm = {("solana", "MintDone"): {"price": 2.0}}
    write_marks(c, pm, "2026-07-22T10:00:00+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    write_marks(c, pm, "2026-07-22T10:00:06+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 2


def test_non_open_coin_keeps_the_universe_floor():
    """The row-growth guard the 30s dedupe exists for must survive: a coin with no open
    position is still deduped at 30s even when the open floor is 5s."""
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    pm = {("4663", "0xidle"): {"price": 2.0}}
    write_marks(c, pm, "2026-07-22T10:00:00+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    write_marks(c, pm, "2026-07-22T10:00:06+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 1


def test_closed_position_coin_falls_back_to_universe_floor():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    pid = _open_pos(c, "4663", "0xshut")
    m.claim_close(c, pid)
    m.finalize_close(c, pid, "2026-07-22T10:00:00+00:00", -1.0, exit_reason="b_stop")
    pm = {("4663", "0xshut"): {"price": 2.0}}
    write_marks(c, pm, "2026-07-22T10:00:00+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    write_marks(c, pm, "2026-07-22T10:00:06+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 1


def test_open_floor_is_keyed_on_network_and_mint():
    """(network_id, mint) identity — the 07-15 keying finding. The same mint string on a
    different chain must NOT inherit the open floor."""
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    _open_pos(c, "solana", "SAMEMINT")
    pm = {("4663", "SAMEMINT"): {"price": 2.0}}
    write_marks(c, pm, "2026-07-22T10:00:00+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    write_marks(c, pm, "2026-07-22T10:00:06+00:00", min_interval_s=30.0, open_min_interval_s=5.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 1


def test_fastlane_source_label_is_preserved():
    """Research must be able to segment old-cadence from new-cadence eras (the 4663 paper
    slippage convention already changed once at e524ef70 and blended reads resulted)."""
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    write_marks(c, {("4663", "0xf"): {"price": 2.0, "source": "fastlane_rpc",
                                      "quality": "exact_route"}},
                "2026-07-22T10:00:00+00:00")
    row = c.execute("SELECT source, quality FROM memecoin_position_marks").fetchone()
    assert row["source"] == "fastlane_rpc" and row["quality"] == "exact_route"


def test_write_marks_dedupe_disabled_at_zero():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    pm = {("4663", "0xabc"): {"price": 2.0}}
    write_marks(c, pm, "2026-07-17T10:00:00+00:00", min_interval_s=0)
    write_marks(c, pm, "2026-07-17T10:00:01+00:00", min_interval_s=0)
    n = c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0]
    assert n == 2


# ── mid and executable writers must not suppress each other (review blocker 3) ──
#
# The dedupe keys on (chain_id, mint) alone, so a pool-mid mark at t=0 silenced a fast-lane
# EXECUTABLE mark at t=1 and vice versa. The chart grew two series but the tape could only
# ever record whichever writer got there first — which would have made the executable series
# look sparse or absent rather than divergent, i.e. the exact conclusion the chart exists to
# prevent. Dedupe is per SOURCE CLASS.

def test_executable_mark_is_not_suppressed_by_a_recent_mid():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    write_marks(c, {("4663", "0xt"): {"price": 1.0, "source": "dexscreener"}},
                "2026-07-22T10:00:00+00:00", min_interval_s=30.0)
    write_marks(c, {("4663", "0xt"): {"price": 1.02, "source": "fastlane_rpc"}},
                "2026-07-22T10:00:01+00:00", min_interval_s=30.0)
    rows = c.execute("SELECT source, mid FROM memecoin_position_marks ORDER BY id").fetchall()
    assert [r["source"] for r in rows] == ["dexscreener", "fastlane_rpc"]


def test_mid_mark_is_not_suppressed_by_a_recent_executable():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    write_marks(c, {("4663", "0xt"): {"price": 1.02, "source": "fastlane_rpc"}},
                "2026-07-22T10:00:00+00:00", min_interval_s=30.0)
    write_marks(c, {("4663", "0xt"): {"price": 1.0, "source": "geckoterminal"}},
                "2026-07-22T10:00:01+00:00", min_interval_s=30.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 2


def test_dedupe_still_applies_within_a_source_class():
    """Two mid sources are one class: GT must still be deduped against DexScreener."""
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    write_marks(c, {("4663", "0xt"): {"price": 1.0, "source": "dexscreener"}},
                "2026-07-22T10:00:00+00:00", min_interval_s=30.0)
    write_marks(c, {("4663", "0xt"): {"price": 1.01, "source": "geckoterminal"}},
                "2026-07-22T10:00:05+00:00", min_interval_s=30.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 1


def test_executable_dedupes_against_executable():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    pm = {("4663", "0xt"): {"price": 1.0, "source": "fastlane_rpc"}}
    write_marks(c, pm, "2026-07-22T10:00:00+00:00", min_interval_s=30.0)
    write_marks(c, pm, "2026-07-22T10:00:05+00:00", min_interval_s=30.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 1


# ── per-class dedupe must retain the NEWEST timestamp in the class (review 4) ──
#
# GROUP BY chain,mint,source yields one MAX(sweep_ts) per RAW source; the dict comprehension
# then collapses several raw sources into one class, so the LAST SQL row wins rather than the
# newest across the class. Two mid sources 40s apart could leave the OLDER stamp stored, and a
# fresh mid within the floor of the newer one gets wrongly admitted.

def test_class_dedupe_keeps_the_newest_stamp_across_sources_in_a_class():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    for ts, src in (("2026-07-22T10:00:00+00:00", "geckoterminal"),
                    ("2026-07-22T10:00:40+00:00", "dexscreener")):
        c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
                  "executable_exit_value, source, quality) VALUES('4663','0xt',?,?,?,?,?)",
                  (ts, 1.0, 1.0, src, "live"))
    c.commit()
    # a third mid 5s after the NEWEST (dexscreener 10:00:40) must be deduped under the 30s floor
    write_marks(c, {("4663", "0xt"): {"price": 1.1, "source": "other_mid"}},
                "2026-07-22T10:00:45+00:00", min_interval_s=30.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 2


def test_class_dedupe_admits_when_past_the_floor_of_the_newest():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    for ts, src in (("2026-07-22T10:00:00+00:00", "geckoterminal"),
                    ("2026-07-22T10:00:40+00:00", "dexscreener")):
        c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
                  "executable_exit_value, source, quality) VALUES('4663','0xt',?,?,?,?,?)",
                  (ts, 1.0, 1.0, src, "live"))
    c.commit()
    # 31s after the newest -> genuinely past the floor -> written
    write_marks(c, {("4663", "0xt"): {"price": 1.1, "source": "other_mid"}},
                "2026-07-22T10:01:11+00:00", min_interval_s=30.0)
    assert c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0] == 3
