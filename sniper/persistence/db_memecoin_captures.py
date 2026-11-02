"""Append-only storage for refused-mint forward captures.

Its OWN database file, deliberately, not `sniper_tracker.db`. The funded ledger already has one
writable handle it must trust, and this is research: a research writer that stalls, bloats a WAL,
or holds a checkpoint lock must not be able to reach the funded ledger to do it. Same reasoning
that keeps the Solana tape in `memecoin_sol_tape.db`.

Capture rows are immutable. The operator table has one row for each refusal. Its clocks and
decision use first-write rules.

READING THE SERIES BACK. `(age_s, quote_price)` ordered by `age_s` is exactly the shape
`sniper.analysis.memecoin_rails_model.rails_outcome` consumes, and `idx_refcap_series` makes that
read an index scan. Two things the caller MUST declare, because the model refuses to infer either:

  price_space   read `quote_price_basis`. 'exact_route' (and NULL, every row before 2026-09-13)
                is a Jupiter route quote for a fixed USD clip: route-priced, never a chart mid.
                'display' is a DexScreener priceUsd mark, the EVM lane's only read-only source:
                chart space. Chart and route differ by -33% to +76% on the funded book, so a
                series read in the wrong space returns a confident wrong answer, not an error.
                Never pool the two bases in one series or one estimate.
  entry_price   the first observation of the capture (`age_s` ~ 0), which is the refusal-time
                route price -- what we would have paid had the gate admitted.

EVM EXACT ROWS (2026-09-28, capture id suffix `#exact`, provider `evm_*`). `quote_price` is USD per
token, the same space as Solana's exact rows. The UNIT of `quote_output_atomic` on a SELL is not:
Solana sells return USDC atoms (6 decimals), EVM sells return the chain quote token's atoms
(WETH on 4663/8453, WBNB on 56, 18 decimals). `quote_asset_usd` is the USD rate each EVM row used
to price that token; it is NULL on Solana rows, where the asset is USDC and it does not apply.
Divide sell atoms by 1e6 only on `network_id='solana'`.

Rails outcomes are NOT stored here and must not be. They are a pure function of a series plus a
policy, policies change inside a single cohort, and a materialized outcome is a snapshot against
one policy that goes stale on the next contract. Recompute from the series instead; history then
rescores itself for free. Note also that `outcome` on this table already means capture QUALITY
(complete / gapped / truncated_worker / not_captured_stale), which is a different fact from the
model's `undetermined` -- same word, different meaning, and the column belongs to the first one.
"""
from __future__ import annotations

import sqlite3

_CAPTURES_TABLE = """
CREATE TABLE IF NOT EXISTS memecoin_refusal_captures (
    id INTEGER PRIMARY KEY,
    capture_id TEXT NOT NULL,
    origin_event_id TEXT,
    network_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    gate TEXT,
    refused_at TEXT NOT NULL,
    observed_at TEXT,
    age_s REAL,
    horizon_s REAL,
    quote_price REAL,
    quote_input_usd REAL,
    quote_route_target TEXT,
    quote_id TEXT,
    provider TEXT,
    endpoint_index INTEGER,
    latency_ms REAL,
    http_status INTEGER,
    quality TEXT,
    expected_obs INTEGER,
    n_obs INTEGER,
    max_gap_s REAL,
    cadence_s REAL,
    quote_side TEXT,
    token_qty_atomic TEXT,
    token_decimals INTEGER,
    quote_output_atomic TEXT,
    quote_min_output_atomic TEXT,
    quote_price_basis TEXT,
    outcome TEXT,
    quote_asset_usd REAL,
    -- slippage_fail only: tokens the failed buy would have got over its signed minimum (<1).
    slippage_available_over_min REAL
);
"""
# DIP SHADOW (2026-09-30): rows whose capture id ends `#dip` go to their own table with the same
# columns. Readers of memecoin_refusal_captures select by origin_event_id and take the first buy, or
# group by capture_id; a dip row there would be read as the entry, or as a second refusal.
DIP_SUFFIX = "#dip"
DIP_TABLE = "memecoin_dip_shadow"
SCHEMA = _CAPTURES_TABLE + _CAPTURES_TABLE.replace("memecoin_refusal_captures", DIP_TABLE) + """
CREATE INDEX IF NOT EXISTS idx_dip_shadow_series ON memecoin_dip_shadow (capture_id, age_s);
CREATE INDEX IF NOT EXISTS idx_refcap_mint ON memecoin_refusal_captures
    (network_id, mint, refused_at);
CREATE INDEX IF NOT EXISTS idx_refcap_series ON memecoin_refusal_captures (capture_id, age_s);
-- The reaper scans for captures whose window has passed. Without `outcome` IN the index this is a
-- full scan of an append-only table that only ever grows: finding-sqlite-unindexed-filter-scans-
-- the-tape-2026-08-23, where the predicate missing from the SEARCH line WAS the scan.
CREATE INDEX IF NOT EXISTS idx_refcap_outcome ON memecoin_refusal_captures
    (outcome, refused_at, capture_id);

-- POST-TAKE-PROFIT FOLLOW-UP (2026-08-29, operator hypothesis). We exit a take-profit sub-second,
-- so the funded ledger contains ZERO observations of what the coin did next. Without them the
-- question "would a confirmation window have paid" has no data on either side of it, and the
-- answer cannot be recovered later: an unobserved second is gone.
--
-- MEASUREMENT ONLY. Nothing reads these rows into a decision. The V6 post-exit doctrine applies
-- verbatim — an exit that could be influenced by what happens after it is not an exit rule, it is
-- a feedback loop wearing one.
--
-- `age_s` IS MEASURED FROM THE EXIT, NEVER FROM THE CAPTURE START. A poller detects a close some
-- seconds after it happens, so the first observations of every follow-up are missing. Anchoring on
-- the exit makes that coverage floor VISIBLE in the data — a series that starts at age 2.4s says
-- so — where anchoring on capture start would relabel a late start as t=0 and silently move every
-- observation earlier than it happened.
CREATE TABLE IF NOT EXISTS memecoin_post_tp_followups (
    id INTEGER PRIMARY KEY,
    followup_id TEXT NOT NULL,
    position_id INTEGER NOT NULL,
    close_sig TEXT,
    network_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    exit_reason TEXT,
    exited_at TEXT NOT NULL,
    exit_qty_atomic TEXT,
    exit_value_usd REAL,
    exit_price REAL,
    token_decimals INTEGER,
    observed_at TEXT,
    age_s REAL,
    quote_price REAL,
    quote_value_usd REAL,
    quality TEXT,
    provider TEXT,
    latency_ms REAL,
    detection_lag_s REAL,
    -- OWN IMPACT IS A JOIN, NOT A COLUMN (2026-08-29). The pre-exit anchor the prereg requires
    -- already exists: `memecoin_route_series` carries a pre-exit observation for 100% of real_tp
    -- closes since 2026-08-29 and 97% since it began, keyed on position_id. The post-exit side is
    -- this table's own first observation. Copying the pre-exit mark in here would be a second
    -- copy of a number that already has an owner, and the two would drift.
    censored INTEGER,
    horizon_s REAL,
    cadence_s REAL,
    expected_obs INTEGER,
    n_obs INTEGER,
    first_age_s REAL,
    max_gap_s REAL,
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS idx_posttp_series ON memecoin_post_tp_followups (followup_id, age_s);
CREATE UNIQUE INDEX IF NOT EXISTS idx_posttp_once ON memecoin_post_tp_followups
    (position_id, age_s) WHERE age_s IS NOT NULL;

CREATE TABLE IF NOT EXISTS memecoin_operator_refusal_slots (
    attempt_id TEXT PRIMARY KEY,
    origin_event_id TEXT,
    network_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    ticker TEXT,
    source_user_id TEXT,
    source_handle TEXT,
    gate_family TEXT NOT NULL,
    refusal_reason TEXT NOT NULL,
    refused_at TEXT NOT NULL,
    refusal_price REAL,
    slot_shown_at TEXT NOT NULL,
    slot_visible_at TEXT,
    operator_decision TEXT,
    decided_at TEXT,
    decided_size_pct REAL,
    intent_id TEXT,
    quote_price_at_decision REAL,
    slot_drift_from_refusal_pct REAL,
    CHECK (operator_decision IS NULL OR operator_decision IN
           ('enter','dismiss','expired_tab_visible','expired_tab_hidden','untouched'))
);
CREATE INDEX IF NOT EXISTS idx_operator_refusal_decision ON memecoin_operator_refusal_slots
    (operator_decision, refused_at);
-- SHADOW OF THE IMPACT CAP (2026-09-28): our own round trip at our clip, and a $1 probe on the same
-- route, measured after an impact-cap refusal. One row per refusal; never a capture series, so the
-- capture reaper (which groups memecoin_refusal_captures by capture_id) never sees it.
CREATE TABLE IF NOT EXISTS memecoin_impact_shadow (
    id INTEGER PRIMARY KEY,
    attempt_id TEXT,
    origin_event_id TEXT,
    network_id TEXT NOT NULL,
    mint TEXT NOT NULL,
    refused_at TEXT,
    quote_observed_at TEXT,
    observed_at TEXT NOT NULL,
    age_s REAL,
    clip_usd REAL,
    jupiter_impact_pct REAL,
    impact_cap_pct REAL,
    roundtrip_cap_pct REAL,
    buy_out_atomic TEXT,
    rt_sell_usdc_atomic TEXT,
    rt_cost_pct REAL,
    probe_usd REAL,
    probe_buy_out_atomic TEXT,
    probe_sell_usdc_atomic TEXT,
    probe_rt_cost_pct REAL,
    size_cost_pct REAL,
    would_pass_roundtrip INTEGER,
    hops_json TEXT,
    rt_sell_route_json TEXT,
    latency_ms REAL,
    quality TEXT,
    gate TEXT,
    entry_rt_cost_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_impact_shadow_origin ON memecoin_impact_shadow (origin_event_id);
-- SHADOW OF A SLOT-END CHASE REFERENCE (2026-09-29, memecoin_slot_end_shadow): one row per Solana
-- chase refusal. would_admit is NULL whenever anything is unknown (reason says what), never 1.
-- coin_pre/post/end: the pool's coin reserve before and after the source, and at the slot end (the
-- pump curve's is virtual); prices are ratios of these alone. vault_program: the programs whose
-- instructions carry the (largest) paying coin vault; n_coin_legs: how many coin accounts paid the
-- buyer (2+ is a split route, unknown).
CREATE TABLE IF NOT EXISTS memecoin_slot_end_shadow (
    id INTEGER PRIMARY KEY,
    attempt_id TEXT UNIQUE,
    origin_event_id TEXT,
    mint TEXT,
    refused_at TEXT,
    computed_at TEXT,
    source_sig TEXT,
    slot INTEGER,
    venue TEXT,
    pool_vault TEXT,
    vault_program TEXT,
    n_coin_legs INTEGER,
    in_slot_after INTEGER,
    coin_pre REAL,
    coin_post REAL,
    coin_end REAL,
    source_price REAL,
    quote_price REAL,
    cap_pct REAL,
    gap_pct REAL,
    gate_reference_price REAL,
    gate_reference_basis TEXT,
    ref_post_usd REAL,
    ref_end_slot REAL,
    gap_end_pct REAL,
    would_admit INTEGER,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_slot_end_shadow_refused ON memecoin_slot_end_shadow (refused_at);
-- EXIT-VENUE SHADOW (2026-09-30, memecoin_exit_venue_shadow): one row per sampled exit-decision
-- quote of a funded EVM position. series_id is memecoin_route_series.id in the funded ledger. The
-- winner_* columns are the runner's own stored quote (the venue its priority ladder took); the
-- entry_* and sim_* columns are the position's ENTRY venue, read on the shadow's host at blocks
-- BEFORE quoted_at, so the runner's own sell is never in the state that is read. The block the
-- runner's quote saw is not stored (measured 0.4 to 0.9 s before quoted_at), so the entry pool is
-- quoted at two blocks that bracket it: early_block (quoted_at - 1.0 s, entry_quote_early_atomic)
-- and pin_block (quoted_at - 0.3 s, entry_quote_atomic). Equal: the pool did not move across the
-- window and the answer is exact. Different: a bound. The allowance and the simulations are read
-- at pin_block. second_block is the first block of quoted_at's own second (found by a search on
-- block timestamps); block_time_s is the chain's measured seconds per block; head_time is the
-- head's chain timestamp in whole seconds. winner_pool_id equal to entry_pool_id is the control:
-- there a stable row must reproduce winner_output_atomic.
-- Atomic amounts are decimal text in the chain's quote token. sim_min_atomic is the entry quote
-- less winner_slippage_bps (the executor's own minimum). sim_ok: 1 the whole-bag sell simulates
-- at sim_min_atomic, 0 it reverts (sim_revert holds the data), NULL not simulated (outcome says
-- why). sim99_ok is the same sell asked for 99% of the entry quote, run only after sim_ok = 1:
-- sim_ok 1 with sim99_ok 0 is a loss between quote and proceeds (a transfer tax). A V4
-- simulation cannot return its output, so the value is the quote, bounded below by
-- sim_min_atomic when sim_ok is 1.
CREATE TABLE IF NOT EXISTS memecoin_exit_venue_shadow (
    id INTEGER PRIMARY KEY,
    series_id INTEGER UNIQUE,
    network_id TEXT,
    position_id INTEGER,
    mint TEXT,
    quoted_at TEXT,
    winner_venue TEXT,
    winner_pool_id TEXT,
    winner_route_target TEXT,
    input_atomic TEXT,
    winner_output_atomic TEXT,
    winner_min_output_atomic TEXT,
    winner_slippage_bps INTEGER,
    quote_asset_usd REAL,
    entry_protocol TEXT,
    entry_pool_id TEXT,
    entry_hooked INTEGER,
    observed_at TEXT,
    lag_s REAL,
    head_block INTEGER,
    head_time INTEGER,
    block_time_s REAL,
    second_block INTEGER,
    early_block INTEGER,
    pin_block INTEGER,
    entry_quote_early_atomic TEXT,
    entry_quote_atomic TEXT,
    entry_quote_ms REAL,
    allowance_standing INTEGER,
    sim_min_atomic TEXT,
    sim_ok INTEGER,
    sim_revert TEXT,
    sim_ms REAL,
    sim99_ok INTEGER,
    sim99_revert TEXT,
    calls INTEGER,
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS idx_exit_venue_shadow_position
    ON memecoin_exit_venue_shadow (position_id, quoted_at);
"""

EXIT_VENUE_SHADOW_COLUMNS = (
    "series_id", "network_id", "position_id", "mint", "quoted_at", "winner_venue",
    "winner_pool_id", "winner_route_target", "input_atomic", "winner_output_atomic",
    "winner_min_output_atomic", "winner_slippage_bps", "quote_asset_usd", "entry_protocol",
    "entry_pool_id", "entry_hooked", "observed_at", "lag_s", "head_block", "head_time",
    "block_time_s", "second_block", "early_block", "pin_block", "entry_quote_early_atomic",
    "entry_quote_atomic", "entry_quote_ms", "allowance_standing", "sim_min_atomic",
    "sim_ok", "sim_revert", "sim_ms", "sim99_ok", "sim99_revert", "calls", "outcome")


def record_exit_venue_shadow(conn, row, *, replace=False):
    """One row per stored exit-decision quote: the first write of a series_id wins. `replace` is
    for the read of a closed position's last quote, which may follow a row that says the tick
    was not read (a pause, a full ceiling). Missing keys are NULL, never zero."""
    conn.execute("INSERT OR %s INTO memecoin_exit_venue_shadow (%s) VALUES (%s)"
                 % ("REPLACE" if replace else "IGNORE",
                    ",".join(EXIT_VENUE_SHADOW_COLUMNS), ",".join("?" * len(EXIT_VENUE_SHADOW_COLUMNS))),
                 tuple(row.get(c) for c in EXIT_VENUE_SHADOW_COLUMNS))
    conn.commit()

SLOT_END_SHADOW_COLUMNS = (
    "attempt_id", "origin_event_id", "mint", "refused_at", "computed_at", "source_sig", "slot",
    "venue", "pool_vault", "vault_program", "n_coin_legs", "in_slot_after", "coin_pre", "coin_post", "coin_end",
    "source_price", "quote_price", "cap_pct", "gap_pct", "gate_reference_price",
    "gate_reference_basis", "ref_post_usd", "ref_end_slot", "gap_end_pct", "would_admit", "reason")


def record_slot_end_shadow(conn, row):
    """One slot-end shadow row per refusal (a re-run of the same attempt replaces it). Missing keys
    are NULL, never zero."""
    conn.execute("INSERT OR REPLACE INTO memecoin_slot_end_shadow (%s) VALUES (%s)"
                 % (",".join(SLOT_END_SHADOW_COLUMNS), ",".join("?" * len(SLOT_END_SHADOW_COLUMNS))),
                 tuple(row.get(c) for c in SLOT_END_SHADOW_COLUMNS))
    conn.commit()

IMPACT_SHADOW_COLUMNS = (
    "attempt_id", "origin_event_id", "network_id", "mint", "refused_at", "quote_observed_at",
    "observed_at", "age_s", "clip_usd", "jupiter_impact_pct", "impact_cap_pct", "roundtrip_cap_pct",
    "buy_out_atomic", "rt_sell_usdc_atomic", "rt_cost_pct", "probe_usd", "probe_buy_out_atomic",
    "probe_sell_usdc_atomic", "probe_rt_cost_pct", "size_cost_pct", "would_pass_roundtrip",
    "hops_json", "rt_sell_route_json", "latency_ms", "quality", "gate", "entry_rt_cost_pct")


def record_impact_shadow(conn, row):
    """Append one impact-cap shadow row (2026-09-28). Missing keys are NULL, never zero."""
    conn.execute("INSERT INTO memecoin_impact_shadow (%s) VALUES (%s)"
                 % (",".join(IMPACT_SHADOW_COLUMNS), ",".join("?" * len(IMPACT_SHADOW_COLUMNS))),
                 tuple(atomic_text(row.get(c)) if c.endswith("_atomic") and row.get(c) is not None
                       else row.get(c) for c in IMPACT_SHADOW_COLUMNS))
    conn.commit()


def _add_missing_columns(conn):
    """Additive-only migration for a table created before a column existed."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_refusal_captures)")}
    for name, decl in (("n_obs", "INTEGER"), ("max_gap_s", "REAL"), ("cadence_s", "REAL"),
                       ("quote_side", "TEXT"), ("token_qty_atomic", "TEXT"),
                       ("token_decimals", "INTEGER"), ("quote_output_atomic", "TEXT"),
                       ("quote_min_output_atomic", "TEXT"), ("quote_price_basis", "TEXT"),
                       ("quote_asset_usd", "REAL"), ("slippage_available_over_min", "REAL")):
        if name not in have:
            conn.execute("ALTER TABLE memecoin_refusal_captures ADD COLUMN %s %s" % (name, decl))
    shadow = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_impact_shadow)")}
    if shadow:        # a table created before the round-trip gate joined the shadow (2026-09-28)
        for name, decl in (("gate", "TEXT"), ("entry_rt_cost_pct", "REAL")):
            if name not in shadow:
                conn.execute("ALTER TABLE memecoin_impact_shadow ADD COLUMN %s %s" % (name, decl))
    ft = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_post_tp_followups)")}
    if ft:
        for name, decl in (("censored", "INTEGER"), ("token_decimals", "INTEGER")):
            if name not in ft:
                conn.execute(
                    "ALTER TABLE memecoin_post_tp_followups ADD COLUMN %s %s" % (name, decl))


def connect(path):
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Without this the WAL has no ceiling and a stalled reader pins it unboundedly --
    # finding-sqlite-wal-highwater-reclaim-2026-07-29.
    conn.execute("PRAGMA journal_size_limit=67108864")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    return conn


def atomic_text(value):
    """Preserve integer quantities as decimal text; invalid quantities remain unknown."""
    text = str(value)
    return text if text.isascii() and text.isdecimal() else None


def record_observation(conn, row):
    """Append ONE immutable observation. `observed_at` is stamped by the caller per request and is
    never inherited from a retry -- an inherited stamp cannot separate a repeat from a duplicate."""
    # `n_obs` is its OWN column. An earlier version stashed the observation count in
    # `quote_input_usd` on terminal rows, so one column meant dollars on some rows and a count on
    # others -- `SUM()` over it would add counts to dollars. That is the `source_drift_pct` trap
    # (two writers, two meanings, one name) rebuilt in a fresh table on day one. Caught in review by
    # Beta, 2026-08-26.
    cols = ("capture_id", "origin_event_id", "network_id", "mint", "gate", "refused_at",
            "observed_at", "age_s", "horizon_s", "quote_price", "quote_input_usd",
            "quote_route_target", "quote_id", "provider", "endpoint_index", "latency_ms",
            "http_status", "quality", "expected_obs", "n_obs", "max_gap_s", "cadence_s",
            "quote_side", "token_qty_atomic", "token_decimals", "quote_output_atomic",
            "quote_min_output_atomic", "quote_price_basis", "outcome", "quote_asset_usd",
            "slippage_available_over_min")
    table = (DIP_TABLE if str(row.get("capture_id") or "").endswith(DIP_SUFFIX)
             else "memecoin_refusal_captures")
    conn.execute(
        "INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), ",".join("?" * len(cols))),
        tuple(atomic_text(row.get(c)) if c.endswith("_atomic") else row.get(c) for c in cols))
    conn.commit()


def terminal_row(conn, capture_id, outcome, **kw):
    """One per-capture row carrying a terminal `outcome` and no observation.

    The chain comes from the capture id (`network:mint:origin`). A fixed 'solana' default put every
    EVM capacity refusal and every EVM reaper stamp on the wrong chain."""
    network = str(capture_id).split(":", 1)[0] if ":" in str(capture_id) else "solana"
    base = {"capture_id": capture_id, "outcome": outcome, "network_id": network}
    base.update(kw)
    record_observation(conn, base)


def observation_counts(conn, *, before_iso):
    """`capture_id -> (mint, refused_at, horizon_s, expected_obs, n_obs, terminal, reach_s,
    cadence_s, max_gap_s)`
    for every capture whose refusal is older than `before_iso`. The reaper's input.

    `reach_s` is the largest `age_s` the series actually reached and `max_gap_s` is its largest
    hole. Reach alone answers "did it get to the end"; the gap answers "was it observed
    THROUGHOUT", and those differ -- see `reap`. The leading interval counts as a gap, because a
    series whose first observation lands at 300s has a 300-second hole at the front.
    """
    gaps = dict(conn.execute(
        "SELECT capture_id, MAX(d) FROM ("
        "  SELECT capture_id, age_s - LAG(age_s, 1, 0.0) OVER "
        "    (PARTITION BY capture_id ORDER BY age_s) d"
        "  FROM memecoin_refusal_captures WHERE observed_at IS NOT NULL AND age_s IS NOT NULL"
        ") GROUP BY capture_id").fetchall())
    rows = conn.execute(
        "SELECT capture_id, MAX(mint), MAX(refused_at), MAX(horizon_s), MAX(expected_obs), "
        "SUM(CASE WHEN observed_at IS NOT NULL THEN 1 ELSE 0 END), "
        "MAX(CASE WHEN observed_at IS NULL THEN outcome END), MAX(age_s), MAX(cadence_s) "
        "FROM memecoin_refusal_captures WHERE refused_at <= ? GROUP BY capture_id",
        (before_iso,)).fetchall()
    return {r[0]: tuple(r[1:]) + (gaps.get(r[0]),) for r in rows}


# ---------------------------------------------------------------- post-TP follow-up

def record_followup_observation(conn, row):
    """Append ONE immutable post-exit observation.

    The unique index on (position_id, age_s) is the idempotency guard: a restarted worker that
    re-observes the same second writes nothing rather than a duplicate the series would read as
    two ticks. `observed_at` is stamped per request by the caller and never inherited from a
    retry — an inherited stamp cannot separate a repeat from a duplicate.
    """
    cols = ("followup_id", "position_id", "close_sig", "network_id", "mint", "exit_reason",
            "exited_at", "exit_qty_atomic", "exit_value_usd", "exit_price", "token_decimals",
            "observed_at",
            "age_s", "quote_price", "quote_value_usd", "quality", "provider", "latency_ms",
            "detection_lag_s")
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_post_tp_followups (%s) VALUES (%s)"
        % (",".join(cols), ",".join("?" * len(cols))),
        tuple(row.get(c) for c in cols))
    conn.commit()


def record_followup_terminal(conn, row):
    """One per follow-up, carrying coverage rather than a price.

    Coverage is a FACT ABOUT THE SERIES and belongs beside it: `first_age_s` says how much of the
    early window the detection lag cost, `max_gap_s` says whether the middle is continuous, and
    `n_obs` against `expected_obs` says how much of the horizon exists. A read that assumed a full
    60 observations would silently average over holes.
    """
    cols = ("followup_id", "position_id", "close_sig", "network_id", "mint", "exit_reason",
            "exited_at", "exit_qty_atomic", "exit_value_usd", "exit_price", "detection_lag_s",
            "horizon_s", "cadence_s", "expected_obs", "n_obs", "first_age_s", "max_gap_s",
            "outcome", "censored")
    conn.execute(
        "INSERT INTO memecoin_post_tp_followups (%s) VALUES (%s)"
        % (",".join(cols), ",".join("?" * len(cols))),
        tuple(row.get(c) for c in cols))
    conn.commit()


def followup_watermark(conn):
    """The newest exit already captured, so a restart resumes instead of re-walking history."""
    row = conn.execute("SELECT MAX(exited_at) FROM memecoin_post_tp_followups").fetchone()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------- operator refusal slots

_SLOT_FACTS = (
    "attempt_id", "origin_event_id", "network_id", "mint", "ticker",
    "source_user_id", "source_handle", "gate_family", "refusal_reason",
    "refused_at", "refusal_price",
)
_OPERATOR_DECISIONS = frozenset({
    "enter", "dismiss", "expired_tab_visible", "expired_tab_hidden", "untouched",
})


def operator_slot(conn, attempt_id):
    row = conn.execute(
        "SELECT * FROM memecoin_operator_refusal_slots WHERE attempt_id=?",
        (str(attempt_id),),
    ).fetchone()
    return dict(row) if row else None


def show_operator_slot(conn, row, *, now_iso):
    """Create one operator row. A repeat keeps the first clock and can fill one missing price."""
    values = tuple(row.get(name) for name in _SLOT_FACTS)
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_operator_refusal_slots ("
        + ",".join(_SLOT_FACTS) + ",slot_shown_at) VALUES ("
        + ",".join("?" for _ in range(len(_SLOT_FACTS) + 1)) + ")",
        values + (now_iso,),
    )
    if row.get("refusal_price") is not None:
        conn.execute(
            "UPDATE memecoin_operator_refusal_slots SET refusal_price=? "
            "WHERE attempt_id=? AND refusal_price IS NULL",
            (row["refusal_price"], str(row.get("attempt_id"))),
        )
    conn.commit()
    return operator_slot(conn, row.get("attempt_id"))


def mark_operator_slot_visible(conn, attempt_id, *, now_iso):
    conn.execute(
        "UPDATE memecoin_operator_refusal_slots "
        "SET slot_visible_at=COALESCE(slot_visible_at, ?) WHERE attempt_id=?",
        (now_iso, str(attempt_id)),
    )
    conn.commit()
    return operator_slot(conn, attempt_id)


def decide_operator_slot(conn, attempt_id, *, decision, now_iso, decided_size_pct=None,
                         intent_id=None, quote_price_at_decision=None):
    if decision not in _OPERATOR_DECISIONS:
        raise ValueError("invalid operator decision")
    row = operator_slot(conn, attempt_id)
    if row is None:
        raise KeyError(attempt_id)
    refusal_price = row.get("refusal_price")
    drift = None
    if refusal_price and quote_price_at_decision is not None:
        drift = round((float(quote_price_at_decision) / float(refusal_price) - 1.0) * 100.0, 6)
    conn.execute(
        "UPDATE memecoin_operator_refusal_slots SET operator_decision=?, decided_at=?, "
        "decided_size_pct=?, intent_id=?, quote_price_at_decision=?, "
        "slot_drift_from_refusal_pct=? WHERE attempt_id=? AND operator_decision IS NULL",
        (decision, now_iso, decided_size_pct, intent_id, quote_price_at_decision, drift,
         str(attempt_id)),
    )
    conn.commit()
    return operator_slot(conn, attempt_id)


def operator_slots(conn, attempt_ids):
    ids = [str(value) for value in attempt_ids if value]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM memecoin_operator_refusal_slots WHERE attempt_id IN ({marks})", ids,
    ).fetchall()
    return {row["attempt_id"]: dict(row) for row in rows}


def quote_prices_for_origin(conn, origin_event_id):
    """Return the first and latest exact-route prices for one refused origin."""
    if not origin_event_id:
        return None, None
    rows = conn.execute(
        "SELECT quote_price FROM memecoin_refusal_captures "
        "WHERE origin_event_id=? AND observed_at IS NOT NULL AND quote_price>0 "
        "AND COALESCE(quote_price_basis, 'exact_route')='exact_route' "
        "ORDER BY age_s, id", (str(origin_event_id),),
    ).fetchall()
    if not rows:
        return None, None
    return float(rows[0][0]), float(rows[-1][0])
