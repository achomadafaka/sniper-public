"""db_memecoin.py — tables + CRUD for the memecoin copy-mirror (paper).

Tables (all ensure_* idempotent, CREATE IF NOT EXISTS):
  memecoin_wallets   curated fomo-native traders (no copyable on-chain wallet)
  memecoin_events    append-only raw feed observations (audit/research)
  memecoin_positions paper positions
  memecoin_fills     fills incl. manual UI actions
"""
from __future__ import annotations
import logging

# MODULE LOGGER. Absent until 2026-08-28 while `logger.exception` was already called in
# `entry_retry` (line ~4876) — so that error path would have raised NameError instead of
# logging, turning a handled failure into an unhandled one. Found by a fail-closed test on
# the new exemption reader, which asserted the reader returns empty when its table is gone
# and instead got the NameError.
logger = logging.getLogger(__name__)

import json as _json
import re
import hashlib
import math
import sqlite3
import statistics
import uuid
import time
import threading
from itertools import groupby
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from sniper.util.clock import real_utc_now_iso


# One conservative network key for legacy rows. A missing 0x network is an unresolved EVM-family
# bucket, never a guessed concrete chain. Keep the SQL expression equivalent to
# normalized_coin_network(); expression indexes cannot depend on an application-registered function.
def coin_network_sql(prefix=""):
    """The canonical network normalization, with an optional table qualifier.

    The bare form is correct ONLY in a single-table query. In a JOIN where more than one table
    carries `network_id`/`mint`, the unqualified columns are ambiguous and SQLite refuses the whole
    statement — which is how the post-TP scheduler crashed on every pass for a day (546 tracebacks)
    while its green tests monkeypatched this exact query away.
    """
    n = prefix + "network_id"
    mt = prefix + "mint"
    return (
        "CASE "
        "WHEN lower(trim(COALESCE(" + n + ",'')))='1399811149' THEN 'solana' "
        "WHEN trim(COALESCE(" + n + ",''))<>'' THEN lower(trim(" + n + ")) "
        "WHEN substr(lower(trim(COALESCE(" + mt + ",''))),1,2)='0x' THEN 'evm-unknown' "
        "ELSE 'solana' END"
    )


COIN_NETWORK_SQL = coin_network_sql()

# Exclude terminal full-close history before checking de-risk fills under the wallet lock.
_REAL_EXIT_NONCE_CANDIDATE_SQL = (
    "execution_mode='real' AND ("
    "(is_open=1 AND closed_at IS NULL AND (close_claim_id IS NOT NULL OR close_sig IS NOT NULL "
    "OR (derisked_at IS NULL AND (derisk_claim_id IS NOT NULL OR derisk_pending_at IS NOT NULL)))) "
    "OR (derisked_at IS NOT NULL AND derisk_sig IS NOT NULL))"
)

# The one honest-copy filter. A position with ANY fomo-priced fill models a fill no real
# execution can hit (entry==their_price, exit==mid, ZERO slippage), so every metric we grade
# a wallet, a signal, or an exit ARM on must drop it. See alpha_b_copy_stats for the evidence.
HONEST_COPY_SQL = ("id NOT IN (SELECT position_id FROM memecoin_fills "
                   "WHERE fill_source='fomo')")

# --- TWO QUESTIONS, TWO PREDICATES (2026-08-27, operator order via Alpha) --------------------------
#
# The operator's manual picks were reading as system losses: 08-26 robot +$247.25 against manual
# -$24.42, 08-27 robot +$74.18 against manual -$64.08, with manual the MAJORITY of closes by count
# on the 27th. So performance surfaces must split by who decided. That is a DISPLAY split; the
# ledger is immutable and nothing is deleted or hidden.
#
# THE TRAP THIS AVOIDS, and it was live in the order as written. `daily_real_realized_pnl` is a bare
# SUM(realized_pnl) and feeds the daily loss BREAKER. Applying the performance filter to it would
# have stopped the breaker counting $64.08 of real losses on 08-27 alone, leaving it green through a
# drawdown it exists to stop. Real money lost is real money lost regardless of who decided it.
#
# So the predicates are named by the QUESTION THEY ANSWER, never by quality. "Honest" would read as
# "the good one" and someone would reach for it in a risk path.
#
#   robot_performance_sql()  -> "how is the SYSTEM doing"   headline PnL, digests, leaderboards
#   real_money_sql()         -> "how much money moved"      breaker, bankroll, exposure, equity,
#                                                           anything that reconciles to the wallet
#
# Manual is a THIRD censor, not part of HONEST_COPY_SQL. That constant means "this position has a
# fomo-priced fill" — a dishonest PRICE. A manual entry is a different DECISION-MAKER at an honest
# price. Folding them would make the name lie and tell a future reader that manual rows have fill
# problems. The file already keeps observed_exit_sql separate for exactly this reason: dishonest
# entry, unobserved exit, and now different decider are three conditions with three names, composed
# where a caller needs more than one.
MANUAL_SIGNAL_SOURCE = "manual"


def robot_decision_sql(table: str = "memecoin_positions") -> str:
    """SQL predicate: this position was chosen by the SYSTEM, not by the operator."""
    return f"COALESCE({table}.signal_source, '') <> '{MANUAL_SIGNAL_SOURCE}'"


def real_money_sql(table: str = "memecoin_positions") -> str:
    """Every funded dollar, whoever decided it. Risk and reconciliation read THIS."""
    return f"{table}.execution_mode = 'real'"


def robot_performance_sql(table: str = "memecoin_positions") -> str:
    """Funded rows the SYSTEM chose, at prices we could have hit. Performance surfaces read THIS.

    real money AND not fomo-priced AND not operator-chosen. Composed from the three named censors
    rather than restating them, so a change to any one reaches every surface.
    """
    return (f"{real_money_sql(table)}"
            f" AND {HONEST_COPY_SQL.replace('id NOT IN', table + '.id NOT IN')}"
            f" AND {robot_decision_sql(table)}")

def observed_exit_sql(table: str = "memecoin_positions") -> str:
    """SQL predicate: this position has at least one EXECUTABLE exit leg.

    A closed position whose exit legs are ALL non-executable (`noquote`, `cleanup`) was never
    observed at a price we could transact. `memecoin_paper_report` censors exactly these; any arm
    comparison that does not is ranking imaginary outcomes — and on a blind chain like 4663 that
    is EVERY close. Applied together with HONEST_COPY_SQL: fomo fills are a dishonest ENTRY,
    non-executable exits are an unobserved EXIT.

    Built from the same NON_EXECUTABLE_FILL_SOURCES / EXIT_KINDS the exit path and the report use,
    so the three censors cannot drift apart.
    """
    sources = "','".join(NON_EXECUTABLE_FILL_SOURCES)
    kinds = "','".join(EXIT_KINDS)
    exit_leg = (f"SELECT 1 FROM memecoin_fills f WHERE f.position_id={table}.id "
                f"AND f.kind IN ('{kinds}')")
    # Censor only a row that HAS exit legs and whose legs are all non-executable — exactly the
    # report's test (`exited > 0 and observed_exited <= 0`). A row with no exit legs at all is a
    # different condition (missing-fill), handled elsewhere; dropping it here would silently
    # delete legacy and fixture rows this predicate has no opinion about.
    return (f"(NOT EXISTS ({exit_leg}) OR "
            f"EXISTS ({exit_leg} AND f.fill_source NOT IN ('{sources}')))")


SIGNAL_SOURCE_MANUAL = "manual"
# The desk seat is a third decider: never copy, never the operator (spec 2026-10-21 §1.5).
SIGNAL_SOURCE_DESK = "desk"


def signal_source_sql(table: str = "memecoin_positions") -> str:
    """SQL expression: 'manual', 'desk', 'copy', or 'unknown' for a funded position.

    THE MIX MOVED, SO THE PARTITION IS NOT OPTIONAL. Funded entries ran 0% manual through
    2026-08-23 and 89% manual on 08-28 and 08-29. Over closes since 08-20 the two lanes point
    opposite ways: copy +$206.74 across 183 (mean +$1.13), manual -$217.90 across 237 (mean
    -$0.92). An aggregate over "recent funded closes" that does not split on this is averaging a
    positive lane with a negative one at a weight that inverted inside the window.

    THE CONTRACT IS THE ONLY WRITER, WHICH IS WHY THIS IS AN EXPRESSION AND NOT A COLUMN. Adding a
    `signal_source` column beside the contract would create a second writer for one fact, and this
    schema already carries three instances of that trap (`source_drift_pct`, the `by_role` quote
    counters, `executable_peak_price` vs `peak_price`). Read the contract.

    ABSENT IS 'unknown', NEVER 'copy'. A row predating the funded-contract regime has no contract
    and no opinion recorded. Inferring copy from absence would be an inference dressed as data, and
    the absence is not random: it tracks the regime boundary. Censor it or report it, do not fold
    it into either lane.

    NULL `source_user_id` is NOT the test. It is the CONSEQUENCE of a manual buy (there is no
    source wallet), and reading the consequence would also catch any future copy path that loses
    its attribution — two different facts under one label.
    """
    expr = f"json_extract({table}.funded_contract_json, '$.source.signal_source')"
    return (f"CASE WHEN {table}.funded_contract_json IS NULL THEN 'unknown'"
            f" WHEN {expr} IS NULL THEN 'unknown'"
            f" WHEN {expr}='{SIGNAL_SOURCE_MANUAL}' THEN 'manual'"
            f" WHEN {expr}='{SIGNAL_SOURCE_DESK}' THEN 'desk'"
            f" ELSE 'copy' END")


def copy_only_sql(table: str = "memecoin_positions") -> str:
    """SQL predicate: this funded position came from a copy signal, not an operator buy.

    Excludes 'unknown' as well as 'manual'. A study that wants the unknowns must say so and say
    how many they are.
    """
    return f"({signal_source_sql(table)})='copy'"


def measurable_outcome_sql(table: str = "memecoin_positions") -> str:
    """SQL predicate: this CLOSED position is comparable evidence.

    Mirrors `memecoin_paper_report._add_outcome` in full. An earlier version of the matrix used
    `observed_exit_sql()` alone, which is only ONE of the report's three censor conditions — so a
    position that sold PART of the bag at a real quote and then closed the REMAINDER on a noquote
    leg passed here while the report censored it. Live robin: all 16 matrix-input rows were exactly
    that shape, and the trader table published +$217.88 of evidence the report had already thrown
    away — which reached funded arm selection through `select_copy_arm`.

    THAT FIX WAS INCOMPLETE AND THIS IS THE REST OF IT (2026-08-29). The reason-string condition
    catches a close whose FINAL rail was noquote. It cannot catch a close that sold part of the bag
    at a real quote, hit a normal rail like `real_tp` on that part, and left an EARLIER leg
    unpriced — the reason describes the last rail, not the composition of the legs.

    71 such rows were still inside this population, mean +45.20 against a whole-population mean of
    2.44, and their unpriced remainder was a MEDIAN 50% of the exited quantity. Half the bag,
    presented as the outcome of the whole. Removing them moves the paper evidence mean 2.4354 ->
    2.2413. The remainder cannot be valued honestly: a `last_quote_price` exists for every one, but
    it is a chart mid running 0.16x to 2.64x of entry, and substituting it would inject the error
    this censor exists to keep out.

    THE STORED NUMBER IS NOT TOUCHED. It is true for the portion that actually sold, and nulling it
    would destroy a real value to fix a framing problem. What is wrong is that a PARTIAL outcome
    sat in a population defined as COMPLETE ones, so the fix belongs in the population, not the row.
    Being a read-time censor it applies to history, which is the point: a forward-only version
    would leave those 71 inside the evidence base permanently.

    The conditions, all of which must hold, plus fomo:
      * an executable exit leg exists (or no exit legs at all)  -> `observed_exit_sql`
      * NO exit leg is non-executable — the whole bag was priced, not just the part that sold
      * the reason does not end `_noquote`
      * it is not an `unquotable` close lacking a last quote
    `substr(...,-8)` rather than LIKE '%_noquote' because `_` is a LIKE wildcard and would also
    match "Xnoquote".
    """
    entry_kinds = "','".join(ENTRY_KINDS)
    kinds = "','".join(EXIT_KINDS)
    sources = "','".join(NON_EXECUTABLE_FILL_SOURCES)
    return (
        f"{table}.is_open=0 AND {table}.realized_pnl IS NOT NULL"
        # POSITIVE entry-fill quantity, mirroring `missing_entry_fill = entered <= 0`. Existence
        # alone was not enough: a zero-qty entry fill reads as measurable here and
        # invalid_missing_entry_fill there.
        f" AND (SELECT COALESCE(SUM(f.qty),0) FROM memecoin_fills f"
        f"      WHERE f.position_id={table}.id AND f.kind IN ('{entry_kinds}')) > 0"
        # EVERY exit leg must be priced, not merely one of them. `observed_exit_sql` asks whether
        # an executable leg EXISTS; that is satisfied by the sold half of a partial exit whose
        # remainder never got a quote. Keyed on the leg's `fill_source`, never on the exit reason:
        # the reason names the final rail and the legs carry what was actually observed.
        f" AND NOT EXISTS (SELECT 1 FROM memecoin_fills f"
        f"      WHERE f.position_id={table}.id AND f.kind IN ('{kinds}')"
        f"      AND f.fill_source IN ('{sources}'))"
        f" AND {HONEST_COPY_SQL.replace('id NOT IN', table + '.id NOT IN')}"
        # Fomo provenance is carried on the POSITION as well as the fills, and the report's match
        # layer rejects on either. Checking only the fills let an `entry_fill_source='fomo'` row
        # with no fomo-sourced fill through.
        f" AND COALESCE({table}.entry_fill_source,'') <> 'fomo'"
        # An EXECUTABLE exit leg must exist. `observed_exit_sql` deliberately tolerates a row with
        # no exit legs at all (it has no opinion on legacy/fixture rows), but a CLOSED position
        # with no exit fill is the orphan-close malformation and its stored realized_pnl has no
        # provenance — fail-closed for funded evidence, and the report now agrees
        # (`invalid_missing_exit_fill`).
        f" AND EXISTS (SELECT 1 FROM memecoin_fills f2 WHERE f2.position_id={table}.id"
        f"             AND f2.kind IN ('{kinds}') AND f2.fill_source NOT IN ('{sources}'))"
        f" AND substr(COALESCE({table}.exit_reason,''), -8) <> '_noquote'"
        # Python's test is `not last_quote_price`, which is falsy for 0 and 0.0 as well as NULL.
        # `IS NULL` alone disagreed on a stored zero mark.
        f" AND NOT (COALESCE({table}.exit_reason,'')='unquotable'"
        f"          AND COALESCE({table}.last_quote_price,0) <= 0)"
    )


def position_outcome_is_measurable(conn, position_id) -> bool:
    """Whether ONE position is comparable evidence, by the same predicate the matrix filters on."""
    row = conn.execute(
        "SELECT 1 FROM memecoin_positions WHERE id=? AND " + measurable_outcome_sql(),
        (position_id,)).fetchone()
    return row is not None


def normalized_coin_network(network_id, mint):
    """Canonical network portion of a coin key without guessing an EVM chain."""
    network = str(network_id or "").strip().lower()
    if network == "1399811149":
        return "solana"
    if network:
        return network
    return "evm-unknown" if str(mint or "").strip().lower().startswith("0x") else "solana"


def coin_identity(network_id, mint):
    """Return ``(normalized network_id, mint)``; mint case remains untouched."""
    return normalized_coin_network(network_id, mint), str(mint or "").strip()


def chain_scope_clause(network_id):
    """(sql_fragment, params) restricting a memecoin_positions query to ONE canonical chain.

    Chains are never pooled (2026-07-30 per-chain evidence doctrine). The caller's spelling is put
    through `canonical_chain_id` first because UI surfaces say 'robin'/'base' while stored rows say
    '4663'/'8453' — comparing the raw string silently matches zero rows, which renders as a clean
    empty board rather than an error."""
    from sniper.services.memecoin_chains import canonical_chain_id
    chain = canonical_chain_id(network_id) or normalized_coin_network(network_id, "")
    return f" AND {COIN_NETWORK_SQL}=?", [chain]


def _validate_funded_json(value):
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("funded JSON numbers must be finite")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_funded_json(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("funded JSON object keys must be strings")
            _validate_funded_json(item)
        return
    raise ValueError(f"unsupported funded JSON value: {type(value).__name__}")


def canonical_funded_json(value):
    """Canonical funded-decision payload and its SHA-256 digest."""
    if not isinstance(value, dict):
        raise ValueError("funded JSON root must be an object")
    _validate_funded_json(value)
    encoded = _json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _set_immutable_entry_attempt_json(
        conn, attempt_id, *, prefix, payload_json, payload_hash, payload_version, commit=True):
    try:
        decoded = _json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {prefix} JSON") from exc
    canonical, actual_hash = canonical_funded_json(decoded)
    if canonical != payload_json or actual_hash != str(payload_hash).lower():
        raise ValueError(f"{prefix} hash does not match canonical JSON")
    version_key = "classification_version" if prefix == "classification" else "contract_version"
    if isinstance(payload_version, bool) or not isinstance(payload_version, int) or payload_version < 1:
        raise ValueError(f"{prefix} version must be a positive integer")
    if decoded.get(version_key) != payload_version:
        raise ValueError(f"{prefix} version does not match JSON")
    cols = (f"{prefix}_version", f"{prefix}_hash", f"{prefix}_json")
    row = conn.execute(
        f"SELECT {','.join(cols)} FROM memecoin_entry_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown entry attempt: {attempt_id}")
    existing = tuple(row)
    desired = (payload_version, actual_hash, canonical)
    if existing == desired:
        return False
    if any(value is not None for value in existing):
        raise ValueError(f"{prefix} is immutable once written")
    conn.execute(
        f"UPDATE memecoin_entry_attempts SET {cols[0]}=?,{cols[1]}=?,{cols[2]}=? "
        "WHERE attempt_id=?",
        (*desired, attempt_id),
    )
    if commit:
        conn.commit()
    return True


def set_entry_attempt_classification(
        conn, attempt_id, *, classification_json, classification_hash,
        classification_version, commit=True):
    return _set_immutable_entry_attempt_json(
        conn,
        attempt_id,
        prefix="classification",
        payload_json=classification_json,
        payload_hash=classification_hash,
        payload_version=classification_version,
        commit=commit,
    )


def set_entry_attempt_funded_contract(
        conn, attempt_id, *, funded_contract_json, funded_contract_hash,
        funded_contract_version, commit=True):
    return _set_immutable_entry_attempt_json(
        conn,
        attempt_id,
        prefix="funded_contract",
        payload_json=funded_contract_json,
        payload_hash=funded_contract_hash,
        payload_version=funded_contract_version,
        commit=commit,
    )


def validated_pending_funded_contract(conn, pending):
    """Validate the frozen pending contract and return its original storage bytes.

    All-null rows are pre-contract reservations and remain compatible. A contract row must be
    canonical, complete, version-consistent, and byte-for-byte equal to its entry attempt.
    """
    keys = ("funded_contract_version", "funded_contract_hash", "funded_contract_json")
    values = tuple(pending.get(key) for key in keys)
    if all(value is None for value in values):
        return {}
    if any(value is None for value in values):
        raise ValueError("funded contract fields are incomplete")
    version, expected_hash, payload_json = values
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("funded contract version must be a positive integer")
    try:
        decoded = _json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("funded contract JSON is invalid") from exc
    canonical, actual_hash = canonical_funded_json(decoded)
    if canonical != payload_json or actual_hash != str(expected_hash).lower():
        raise ValueError("funded contract hash does not match canonical JSON")
    if decoded.get("contract_version") != version:
        raise ValueError("funded contract version does not match JSON")
    attempt_id = pending.get("attempt_id")
    if not attempt_id:
        raise ValueError("funded contract is missing entry attempt identity")
    attempt = conn.execute(
        "SELECT funded_contract_version,funded_contract_hash,funded_contract_json "
        "FROM memecoin_entry_attempts WHERE attempt_id=?", (attempt_id,),
    ).fetchone()
    if attempt is None or tuple(attempt) != values:
        raise ValueError("funded contract does not match entry attempt")
    return dict(zip(keys, values))


def accounting_mark_key(network_id, mint):
    """Canonical per-chain quote key. EVM quote providers normalize address case."""
    from sniper.execution.solana_quote import norm_mint
    return normalized_coin_network(network_id, mint), norm_mint(str(mint or "").strip())


_POSITION_COIN_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_pos_open_uniq_v3 ON memecoin_positions("
    f"source_user_id, {COIN_NETWORK_SQL}, mint, strategy, "
    "COALESCE(execution_mode,'paper')) WHERE is_open=1"
)
_PENDING_COIN_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_pending_coin_uniq_v2 ON memecoin_pending_entries("
    f"{COIN_NETWORK_SQL}, mint) WHERE status='reserved'"
)


def _index_definition(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


def _normalized_ddl(sql):
    return " ".join(str(sql or "").lower().replace("if not exists ", "").split())


def _create_and_verify_index(conn, name, sql):
    actual = _index_definition(conn, name)
    if actual is not None:
        if _normalized_ddl(actual) != _normalized_ddl(sql):
            raise RuntimeError(f"unexpected definition for {name}: {actual!r}")
        return
    groups = _coin_index_collisions(conn, name)
    if groups:
        raise sqlite3.IntegrityError(
            f"index={name}; colliding_groups={groups!r}"
        )
    try:
        conn.execute(sql)
    except sqlite3.IntegrityError as exc:
        # A concurrent writer may have inserted a collision after the preflight.
        groups = _coin_index_collisions(conn, name)
        raise sqlite3.IntegrityError(
            f"{exc}; index={name}; colliding_groups={groups!r}"
        ) from exc
    actual = _index_definition(conn, name)
    if _normalized_ddl(actual) != _normalized_ddl(sql):
        raise RuntimeError(f"unexpected definition for {name}: {actual!r}")


def _coin_index_collisions(conn, name):
    """Preflight deterministic conflicting groups before a chain-key index is installed."""
    if name == "idx_mc_pos_open_uniq_v3":
        return [tuple(row) for row in conn.execute(
            "SELECT source_user_id, " + COIN_NETWORK_SQL + ", mint, strategy, "
            "COALESCE(execution_mode,'paper'), COUNT(*) FROM memecoin_positions "
            "WHERE is_open=1 GROUP BY source_user_id, " + COIN_NETWORK_SQL + ", mint, "
            "strategy, COALESCE(execution_mode,'paper') HAVING COUNT(*)>1 "
            "ORDER BY source_user_id, 2, mint, strategy, 5"
        ).fetchall()]
    if name == "idx_mc_pending_coin_uniq_v2":
        return [tuple(row) for row in conn.execute(
            "SELECT " + COIN_NETWORK_SQL + ", mint, COUNT(*) FROM memecoin_pending_entries "
            "WHERE status='reserved' GROUP BY " + COIN_NETWORK_SQL + ", mint "
            "HAVING COUNT(*)>1 ORDER BY 1, mint"
        ).fetchall()]
    return []

# Per-trader analytics columns — single source of truth for the table DDL and the
# upsert. Order-independent; upsert writes only the keys present in a given row.
# Mirrors the keys emitted by sniper.analysis.fomo_account.metrics.compute_metrics.
TRADER_ANALYTICS_COLUMNS = [
    "source_user_id", "handle", "computed_at", "data_as_of",
    "swaps_analyzed", "coverage_ratio", "hit_full_history", "window_days",
    "oldest_swap", "newest_swap", "pct_off_platform", "pct_route_approx",
    "n_open_bags", "n_unmatched_sells", "unmatched_sell_ratio", "closed_coverage",
    "n_closed", "matched_lots", "distinct_exit_events", "distinct_closed_mints",
    "realized_pnl_usd", "total_buy_notional", "open_bag_cost_usd",
    "dollar_weighted_return_pct", "mean_pnl_pct", "median_pnl_pct", "win_rate",
    "profit_factor", "undefeated", "pnl_pct_p10", "pnl_pct_p25", "pnl_pct_p50",
    "pnl_pct_p75", "pnl_pct_p90", "best_pct", "worst_pct", "return_std",
    "pnl_pct_dispersion_ratio", "realized_max_drawdown_usd", "avg_win_pct",
    "avg_loss_pct", "top3_win_share", "top3_loss_share", "median_hold_s",
    "mean_hold_s", "hold_p10", "hold_p90", "pct_held_under_60s", "pct_held_over_1d",
    "avg_buy_usd", "buy_usd_p10", "buy_usd_p50", "buy_usd_p90", "max_buy_usd",
    "size_cv", "re_entry_rate", "avg_buys_per_token", "rebuy_lower_rate",
    "fee_drag_pct", "last_swap_age_h", "swaps_24h", "swaps_7d", "volume_7d",
    "large_buys_7d", "active_days", "longest_gap_h", "num_trades", "total_volume",
    "account_age_days", "followers", "following", "follower_ratio", "has_twitter",
    "is_restricted", "is_private", "profile_swap_count", "chain_mix",
    "provider_mix", "metrics_json",
    # selection-data upgrade (2026-07-07): live mark-to-market from /balances
    "live_holdings_value_usd", "live_holdings_count", "unrealized_pnl_usd",
    "unrealized_partial", "fomo_realized_held_usd", "live_perp_pnl", "other_pnl",
    # windowed realized PnL (span-gated) + fomo leaderboard lifetime PnL
    "realized_pnl_24h", "realized_24h_covered", "n_closed_24h",
    "realized_pnl_7d", "realized_7d_covered", "realized_pnl_30d", "realized_30d_covered",
    "fomo_total_pnl", "fomo_unrealized_pnl", "on_leaderboard", "leaderboard_rank",
    "fomo_unrealized_partial",
]

# Additive columns for the selection-data upgrade — migrated onto an existing
# memecoin_trader_analytics table (CREATE TABLE IF NOT EXISTS won't add them).
_ANALYTICS_MIGRATION_COLS = [
    ("matched_lots", "INTEGER"), ("distinct_exit_events", "INTEGER"),
    ("distinct_closed_mints", "INTEGER"),
    ("live_holdings_value_usd", "REAL"), ("live_holdings_count", "INTEGER"),
    ("unrealized_pnl_usd", "REAL"), ("unrealized_partial", "INTEGER"),
    ("fomo_realized_held_usd", "REAL"),
    ("live_perp_pnl", "REAL"), ("other_pnl", "REAL"),
    ("realized_pnl_24h", "REAL"), ("realized_24h_covered", "INTEGER"),
    ("n_closed_24h", "INTEGER"),
    ("realized_pnl_7d", "REAL"), ("realized_7d_covered", "INTEGER"),
    ("realized_pnl_30d", "REAL"), ("realized_30d_covered", "INTEGER"),
    ("fomo_total_pnl", "REAL"), ("fomo_unrealized_pnl", "REAL"),
    ("on_leaderboard", "INTEGER"), ("leaderboard_rank", "INTEGER"),
    ("fomo_unrealized_partial", "INTEGER"),
]


def _ensure_memecoin_tables_in_transaction(conn: sqlite3.Connection) -> None:
    # Keep additive DDL and the create-new/drop-old identity-index migration atomic. Existing callers
    # already rely on this function committing at the end.
    # MANUAL INTENT QUEUE (2026-08-26). The web server must not build funded contracts: its
    # settings are captured at import and deploys never restart it, so position 44706 opened under
    # rails a config change had already replaced 28 minutes earlier. The operator's request crosses
    # to the runner as an INTENT — inputs only, never a decision — and the runner, whose config the
    # deploy does refresh, builds the contract.
    #
    # DURABLE STATE, NOT A CURSOR, unlike onchain_wallet_detections. That lane drops a row whose
    # hook raised because "a detection is a moment"; an operator instruction is an obligation, and
    # a manual buy that vanishes because the runner blinked is the failure this design would
    # otherwise introduce. Every intent ends in a terminal state a person can read.
    # ANCHOR-CHECKED EVM EXIT (spec 2026-10-01 §6): one row per held tick and per held sale, with
    # the tick's rung ledger, so the forward grade (unit: a held episode) reads it directly.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_exit_anchor_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            network_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            decision TEXT NOT NULL,
            route TEXT,
            value_usd REAL,
            anchor_price REAL,
            qty REAL,
            held_s REAL,
            exit_reason TEXT,
            rungs_json TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memecoin_exit_anchor_audit_observed "
                 "ON memecoin_exit_anchor_audit(observed_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_manual_exit_intents (
            intent_id TEXT PRIMARY KEY,
            requested_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            position_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            fraction REAL,
            actor TEXT,
            state TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            not_before TEXT,
            claim_id TEXT,
            claimed_at TEXT,
            resolved_at TEXT,
            result_status TEXT,
            reason TEXT,
            realized REAL,
            sig TEXT
        )""")
    _source_request_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_manual_exit_intents)")}
    if "request_json" not in _source_request_cols:
        conn.execute("ALTER TABLE memecoin_manual_exit_intents ADD COLUMN request_json TEXT")
    # THE OPERATOR'S SELL IS AN INTENT TOO (2026-09-05). Same rule as the buy table above: the web
    # writes inputs, the runner sells with its own executors. See memecoin_manual_exit_intents.py.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_manual_intents (
            intent_id TEXT PRIMARY KEY,
            requested_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            mint TEXT NOT NULL,
            network_id TEXT,
            source_user_id TEXT,
            ticker TEXT,
            size_usd REAL,
            size_pct_equity REAL,
            resolved_size_usd REAL,
            origin_event_id TEXT,
            state TEXT NOT NULL DEFAULT 'queued',
            claim_id TEXT,
            claimed_at TEXT,
            resolved_at TEXT,
            position_id INTEGER,
            attempt_id TEXT,
            result_status TEXT,
            reason TEXT
        )""")
    # RESIDUAL TAIL STUDY (2026-08-27). Immutable evidence for "recover principal, then trail the
    # rest": what the KEPT fraction would have been worth at 2/5/10/20/30/60s after a funded TP.
    # Measurement only — the post-exit doctrine forbids any decision reading this. UNIQUE on
    # (position_id, horizon_s) so a restart can fill a gap but can never replace a measurement.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_residual_tail_samples (
            id INTEGER PRIMARY KEY,
            position_id INTEGER NOT NULL,
            mint TEXT, network_id TEXT, closed_at TEXT,
            horizon_s REAL NOT NULL,
            observed_at TEXT, elapsed_s REAL,
            status TEXT NOT NULL,
            residual_usdc REAL, unit_price REAL, detail TEXT,
            entry_usdc REAL, exit_usdc REAL,
            resolve_status TEXT, resolve_fraction REAL,
            residual_qty_atomic TEXT, sold_qty_atomic TEXT, sold_usdc REAL,
            resolve_probes INTEGER, resolve_ms REAL,
            UNIQUE(position_id, horizon_s)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_residual_tail_pos "
                 "ON memecoin_residual_tail_samples(position_id, horizon_s)")
    # PER-WALLET GATE EXEMPTIONS (2026-08-28, operator order). A wallet may be excused from named
    # SELECTION gates — never from a safety gate. Child table rather than a column on
    # memecoin_wallets so each grant carries its own actor and reason, revoke keeps history, and
    # clearing on exclusion is one UPDATE.
    #
    # UNKNOWN VALUES ARE INERT BY CONSTRUCTION. `gate_family` is read only by matching it against
    # the refusal-capture taxonomy, so a row naming a family that does not exist is never consulted
    # by anything. That satisfies "malformed values are ignored, never fail-open across a family"
    # structurally, rather than through a guard someone has to remember to write.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_wallet_gate_exemptions (
            id INTEGER PRIMARY KEY,
            source_user_id TEXT NOT NULL,
            gate_family TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            actor TEXT, reason TEXT,
            granted_at TEXT, revoked_at TEXT,
            UNIQUE(source_user_id, gate_family)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_wallet_exempt_active "
                 "ON memecoin_wallet_gate_exemptions(source_user_id, active)")
    # GLOBAL entry-knob overrides (2026-09-08). The per-wallet table above waives a whole gate for
    # one wallet; this one moves a THRESHOLD for every entry. `value` is JSON so a bool stays a
    # bool and a float stays a float — a bare TEXT column made "0" and "false" indistinguishable.
    # The registry in sniper/services/memecoin_entry_knobs.py decides which names are adjustable;
    # a row naming anything else is never read, exactly as the exemptions table works.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_entry_knob_overrides (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            actor TEXT, reason TEXT,
            updated_at TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_entry_knob_audit (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            old_value TEXT, new_value TEXT,
            action TEXT NOT NULL,
            actor TEXT, reason TEXT,
            at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_entry_knob_audit_name "
                 "ON memecoin_entry_knob_audit(name, at)")
    # A wallet cap raises one named chase ceiling. It is not a gate exemption: a waiver would
    # remove the ceiling entirely and could apply on another chain by mistake.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_wallet_chase_caps (
            id INTEGER PRIMARY KEY,
            source_user_id TEXT NOT NULL,
            network_id TEXT NOT NULL,
            coin_band TEXT NOT NULL,
            cap_pct REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            actor TEXT, reason TEXT,
            granted_at TEXT, revoked_at TEXT,
            UNIQUE(source_user_id, network_id, coin_band)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_wallet_chase_cap_active "
                 "ON memecoin_wallet_chase_caps(source_user_id, network_id, coin_band, active)")
    # PER-WALLET EXIT OVERRIDE (operator order 2026-09-27: "set different tp strategies for
    # different users through the UI"). A take-profit and optionally a stop per (wallet, chain),
    # read once at contract build and frozen into NEW contracts only. Its own audit table, not
    # memecoin_wallet_approvals: a research reader takes that table's latest chain row as the arm
    # state without filtering on action.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_wallet_exit_overrides (
            id INTEGER PRIMARY KEY,
            source_user_id TEXT NOT NULL,
            network_id TEXT NOT NULL,
            tp_pct REAL,
            stop_pct REAL,
            active INTEGER NOT NULL DEFAULT 1,
            actor TEXT, reason TEXT,
            set_at TEXT, revoked_at TEXT,
            UNIQUE(source_user_id, network_id)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_wallet_exit_override_audit (
            id INTEGER PRIMARY KEY,
            source_user_id TEXT NOT NULL,
            network_id TEXT NOT NULL,
            action TEXT NOT NULL,
            old_tp_pct REAL, new_tp_pct REAL,
            old_stop_pct REAL, new_stop_pct REAL,
            actor TEXT, reason TEXT,
            at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_wallet_exit_override_audit_wallet "
                 "ON memecoin_wallet_exit_override_audit(source_user_id, at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_manual_intent_state "
                 "ON memecoin_manual_intents(state, requested_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_manual_exit_intent_state "
                 "ON memecoin_manual_exit_intents(state, requested_at)")
    # OPERATOR RAILS ON THE INTENT (2026-08-27). These do NOT break "inputs, never a decision":
    # they carry what the operator TYPED, never a value the web read from its own config. A typed
    # number cannot be stale; a defaulted one is exactly the 44706 divergence wearing a hat. The
    # web must send NULL for anything the operator left blank, and `rails_mode` says which regime
    # the runner applies:
    #   NULL / 'default' -> the runner's own policy decides everything (unchanged behaviour)
    #   'custom'         -> ONLY these values are armed; every rail not named here is switched OFF
    # 'custom' is the operator's explicit choice (2026-08-27) and it is the configuration that went
    # 3 of 11 on per-position overrides. It is offered because manual entry is a discretionary pick
    # and the operator owns the exit, not because the record supports it.
    _mi_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_manual_intents)")}
    for _col in ("tp_pct", "tp_target_mcap", "sl_pct", "sl_target_mcap", "trail_pct", "derisk_pct"):
        if _col not in _mi_cols:
            conn.execute(f"ALTER TABLE memecoin_manual_intents ADD COLUMN {_col} REAL")
    if "rails_mode" not in _mi_cols:
        conn.execute("ALTER TABLE memecoin_manual_intents ADD COLUMN rails_mode TEXT")
    for _col, _ddl in (("size_pct_equity", "REAL"), ("resolved_size_usd", "REAL"),
                       ("origin_event_id", "TEXT")):
        if _col not in _mi_cols:
            conn.execute(f"ALTER TABLE memecoin_manual_intents ADD COLUMN {_col} {_ddl}")
    # OPERATOR OVERRIDE (2026-09-22, operator order). What the operator TYPED via "Enter anyway"
    # on a refused/failed card, same "inputs, never a decision" rule as size_usd above: this
    # column is 0/1 of intent, never itself a gate verdict. The runner resolves it into named
    # policy parameters (real_entry_hook / the legs), the same layering the manual signal source
    # already uses -- see tests/test_memecoin_manual_gate_bypass.py.
    if "override" not in _mi_cols:
        conn.execute(
            "ALTER TABLE memecoin_manual_intents ADD COLUMN override INTEGER NOT NULL DEFAULT 0")
    # DESK ORIGIN (2026-10-21, spec desk-execution-plumbing §2.4). Same queue, a second decider.
    # Additive with a 'manual' default, so a pre-desk drain reads every old row unchanged.
    if "signal_source" not in _mi_cols:
        conn.execute("ALTER TABLE memecoin_manual_intents "
                     "ADD COLUMN signal_source TEXT NOT NULL DEFAULT 'manual'")
    if "desk_ticket_id" not in _mi_cols:
        conn.execute("ALTER TABLE memecoin_manual_intents ADD COLUMN desk_ticket_id TEXT")
    if "desk_metadata_json" not in _mi_cols:
        conn.execute("ALTER TABLE memecoin_manual_intents ADD COLUMN desk_metadata_json TEXT")
    # One desk ticket, one executable intent (Codex audit #4). Partial: manual rows carry NULL.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_manual_intent_desk_ticket "
                 "ON memecoin_manual_intents(desk_ticket_id) WHERE desk_ticket_id IS NOT NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_user_id TEXT UNIQUE,
            handle TEXT,
            label TEXT,
            paused INTEGER DEFAULT 0,
            added_at TEXT
        )""")
    # THE J7 TRACKED LISTS, WHICH ARE THE WHOLE ADDRESSABLE ROSTER (2026-09-22). J7 caps each
    # account at 50 wallets and relays trades for those only; fomo is banned and the global stream
    # ended on 09-11 (1,411 signalling wallets on 09-09, 49 on 09-11). So a roster row that is not
    # on one of these lists can never produce a signal. The runner already received the lists on
    # every connect and counted them, then dropped them.
    #
    # `seen_at` is the freshness the prune script refuses to act without: acting on a stale list
    # would retire wallets because a socket was down, not because J7 stopped tracking them.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS j7_tracked_wallets (
            account_index INTEGER NOT NULL,
            kind TEXT NOT NULL,
            source_user_id TEXT NOT NULL,
            handle TEXT,
            display_name TEXT,
            added_at TEXT,
            seen_at TEXT NOT NULL,
            PRIMARY KEY (account_index, kind, source_user_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_j7_tracked_uid "
                 "ON j7_tracked_wallets(source_user_id)")
    # WHAT THE FEED SAID versus WHAT WE STORED (2026-09-22). The pump list arrived with 9 entries
    # and parsed to zero, and the absence read as "J7 tracks nobody on pump": the prune then listed
    # 7 ACTIVE pump wallets as untracked, one with 202 events in 7 days and two funded closes.
    # Storing the COUNT the frame reported makes that detectable — stored < reported means the
    # tracked set is UNKNOWN, which must refuse, not retire. `stored_count` NULL = could not parse.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS j7_tracked_meta (
            account_index INTEGER NOT NULL,
            kind TEXT NOT NULL,
            reported_count INTEGER,
            stored_count INTEGER,
            seen_at TEXT NOT NULL,
            account_fingerprint TEXT,
            PRIMARY KEY (account_index, kind)
        )""")
    if "account_fingerprint" not in {r[1] for r in conn.execute("PRAGMA table_info(j7_tracked_meta)")}:
        conn.execute("ALTER TABLE j7_tracked_meta ADD COLUMN account_fingerprint TEXT")
    # A LIST THAT NEVER ARRIVED AT ALL (Codex recheck 2026-09-23, A3). The rows above describe
    # lists that DID arrive; an account that has never delivered a frame produced no row, so
    # completeness had nothing to compare and one fresh account could certify the silence of
    # another. `unseen=1` is a REFUSAL MARKER, never evidence: `j7_tracked_incomplete` counts it
    # so the prune refuses, and `j7_tracked_seen_at` skips it so its stamp cannot pass as a fresh
    # observation.
    if "unseen" not in {r[1] for r in conn.execute("PRAGMA table_info(j7_tracked_meta)")}:
        conn.execute("ALTER TABLE j7_tracked_meta ADD COLUMN unseen INTEGER NOT NULL DEFAULT 0")
    # ONE ADDRESS REGISTRY FOR THE ON-CHAIN LANES (2026-09-26, spec
    # docs/superpowers/specs/2026-09-26-solana-address-registry-design.md). The slot lane keyed a
    # JSONL by HANDLE and the confirmed watcher read memecoin_wallet_addresses, whose primary key is
    # source_user_id alone and which is written with INSERT OR REPLACE: one address per profile, and
    # a refutation erased by the next address. Here a profile may hold several addresses per chain,
    # a refutation belongs to ONE address, and every status change is an event row.
    # `conflict_at`: verified evidence arrived for an address already refuted (refuted stands).
    ensure_address_registry_tables(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS onchain_wallet_detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signature TEXT UNIQUE NOT NULL,
            source_user_id TEXT NOT NULL,
            handle TEXT,
            address TEXT NOT NULL,
            network_id TEXT NOT NULL DEFAULT 'solana',
            mint TEXT NOT NULL,
            token_delta REAL,
            sol_spent REAL,
            source_size_usd REAL,
            source_price REAL,
            block_time INTEGER,
            detected_at TEXT NOT NULL,
            detect_latency_s REAL,
            feed_event_id TEXT,
            feed_lag_vs_onchain_s REAL,
            created_at TEXT NOT NULL
        )""")
    _onchain_cols = {r[1] for r in conn.execute("PRAGMA table_info(onchain_wallet_detections)")}
    for _col, _ddl in (("network_id", "TEXT NOT NULL DEFAULT 'solana'"),
                       ("source_size_usd", "REAL"), ("source_price", "REAL"),
                       ("source_valuation_json", "TEXT"),
                       ("token_amount_atomic", "TEXT"),
                       ("quote_amount_atomic", "TEXT"),
                       ("source_observation_key", "TEXT"),
                       ("source_observation_state", "TEXT"),
                       # Fill timing (2026-10-02): the source buy's Solana slot or EVM block, as the
                       # watcher held it. NULL = not captured, never 0.
                       ("source_slot", "INTEGER")):
        if _col not in _onchain_cols:
            conn.execute(f"ALTER TABLE onchain_wallet_detections ADD COLUMN {_col} {_ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_onchain_detect_network_id "
                 "ON onchain_wallet_detections(network_id, id)")
    # The funded source-buy floor reads a wallet's detections of one coin in a time window
    # (source_prior_net_buy_usd, 2026-09-30): an index range, never a scan of the table.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_onchain_detect_wallet_coin "
                 "ON onchain_wallet_detections(source_user_id, mint, detected_at)")
    ensure_onchain_sells_table(conn)
    ensure_onchain_evm_sell_shadow_table(conn)     # the EVM sell SHADOW's own table (2026-09-29)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_evm_wallet_addresses (
            source_user_id TEXT PRIMARY KEY,
            handle TEXT,
            address TEXT NOT NULL UNIQUE,
            profile_checked_at TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fomo_event_id TEXT UNIQUE,
            source_user_id TEXT, source_handle TEXT,
            mint TEXT, ticker TEXT, side TEXT,
            their_price REAL, their_market_cap REAL, their_size_usd REAL,
            is_first_buy INTEGER,
            created_at TEXT, received_at TEXT, lag_s REAL
        )""")
    # Fresh-pool re-quote queue (2026-08-14). A buy on a pool younger than any aggregator's index
    # aborts "no quote"/"no planned-quantity sell route"; the SAME coin is Jupiter/Raydium-routable
    # minutes later (proven: MOX/3Knru quoted 200 ten minutes after their one-shot refusal). The
    # entry reservation is released the instant a quote misses, so nothing is held across the wait —
    # the retry re-runs the funded hook on the stored event, and reserve_pending's UNIQUE(mint)
    # among un-terminal rows makes a double-open impossible even if the live feed lands the same
    # coin mid-window. event_json is the frozen CopyEvent; the row is deleted on fill, on a
    # non-transient verdict, or at window expiry.
    # ROUTE SERIES (2026-08-26). The exit path computes exact whole-bag route quotes constantly and
    # discards every value; route_quote_value is populated on 0 of 105 current-era funded rows. This
    # keeps them. It is the ONLY route-space price series that exists — memecoin_position_marks'
    # `executable_exit_value` is a reserved alias for `mid` on 100% of its 3.2M rows.
    # SEAM IS PART OF THE KEY, not decoration: a peak preview, an exit-decision read and an equity
    # mark price different quantities at different moments, and a consumer that pools them holds a
    # mixture.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_route_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            mint TEXT,
            network_id TEXT,
            seam TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            quoted_at TEXT,
            expected_usdc REAL NOT NULL,
            open_qty REAL,
            unit_price REAL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_route_series_pos "
                 "ON memecoin_route_series(position_id, seam, observed_at)")
    if "quote_evidence_json" not in {r[1] for r in conn.execute("PRAGMA table_info(memecoin_route_series)")}:
        conn.execute("ALTER TABLE memecoin_route_series ADD COLUMN quote_evidence_json TEXT")
    # GAPS ARE ROWS (2026-10-16). Why the price series has a hole: a quote miss (reason = failure
    # class | provider group), a buffer drop (count + first/last observed_at lost) or a runner
    # restart (one row per open real position, at the runner's start). Append-only; the price table
    # above never holds a NULL or zero in its place.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_route_series_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            network_id TEXT,
            mint TEXT,
            seam TEXT NOT NULL,
            kind TEXT NOT NULL,
            reason TEXT,
            observed_at TEXT NOT NULL,
            last_observed_at TEXT,
            count INTEGER NOT NULL DEFAULT 1
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_route_series_gaps_pos "
                 "ON memecoin_route_series_gaps(position_id, observed_at)")
    # EXECUTABLE EXTREMES (2026-09-30, operator orders 367d4ad6 / 86d053ce). The top and bottom of
    # the `exit_decision` route series per CLOSED funded position. No row means no positive-price
    # sample, never zeros. The backup table holds the display-derived max_gain_pct / time_to_peak_s
    # a funded row carried before the history script cleared it; a row is written once.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_position_exec_extremes (
            position_id INTEGER PRIMARY KEY,
            high_price REAL NOT NULL,
            high_at TEXT NOT NULL,
            low_price REAL NOT NULL,
            low_at TEXT NOT NULL,
            samples INTEGER NOT NULL,
            first_at TEXT NOT NULL,
            last_at TEXT NOT NULL,
            basis TEXT NOT NULL,
            computed_at TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_position_display_extremes_backup (
            position_id INTEGER PRIMARY KEY,
            old_max_gain_pct REAL,
            old_time_to_peak_s REAL,
            copied_at TEXT NOT NULL
        )""")
    # PAPER PATH (2026-09-27, operator). The paper twin of the route series: one bounded price path
    # per open PAPER position, from the price the paper bracket already fetched. `basis` is part of
    # the key because a mid and an executable quote are different quantities and must never pool.
    # t_offset_s runs from opened_recorded_at (the fill), the anchor the funded shape table uses.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_paper_path (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            network_id TEXT,
            mint TEXT,
            basis TEXT NOT NULL,
            source TEXT,
            t_offset_s REAL NOT NULL,
            observed_at TEXT NOT NULL,
            price REAL NOT NULL,
            kind TEXT NOT NULL,
            UNIQUE(position_id, basis, observed_at)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_paper_path_pos "
                 "ON memecoin_paper_path(position_id, basis, t_offset_s)")
    # SLOT-LANE CU DEMOTIONS (2026-10-09): one row per class-limited CU class demoted after a send ran
    # out of compute units, bound to the class's table entry (entry_key: n, max, token programs), so a
    # restart cannot re-arm it and only a reviewed table change clears it. Add-only.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_slot_cu_demotions (
            class_key TEXT NOT NULL,
            entry_key TEXT NOT NULL,
            sig TEXT,
            signed_limit INTEGER,
            reason TEXT,
            demoted_at TEXT NOT NULL,
            PRIMARY KEY(class_key, entry_key)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_evm_v4_pool_keys (
            network_id TEXT NOT NULL,
            pool_id TEXT NOT NULL,
            pool_manager TEXT NOT NULL,
            key_json TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            PRIMARY KEY(network_id, pool_id, pool_manager)
        )""")
    # MINT -> POOLS, the direction discovery actually asks in. Every stored key carries both
    # currencies, so this table is also the coin index, and an EXPRESSION index keeps that lookup
    # constant as the offline indexer grows the table from a few thousand rows to six figures.
    # The expressions must match `_stored_pools_for_mint`'s WHERE clause character for character or
    # SQLite silently falls back to a full scan.
    for _side in ("currency0", "currency1"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mc_v4_key_%s ON memecoin_evm_v4_pool_keys "
            "(network_id, pool_manager, lower(json_extract(key_json,'$.%s')))" % (_side, _side))
    # PAIR -> POOLS (Alpha GO 2026-10-17): the multihop's `_stored_pair_pools` filters BOTH
    # currencies, and with one-sided indexes SQLite walked the busier side (4663 ETH/USDG: 48,155
    # USDG rows for 357, 49.7 ms inside the entry quote's bridge_options). Same expressions,
    # character for character. Built once at the first ensure: 7.0 s cold / 1.8 s warm on a copy of
    # the 19.2 GB ledger (965k rows), at the new runner's start inside the deploy's entries-down window.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mc_v4_key_pair ON memecoin_evm_v4_pool_keys "
        "(network_id, pool_manager, lower(json_extract(key_json,'$.currency0')), "
        "lower(json_extract(key_json,'$.currency1')))")

    # JUPITER LOAD, per-minute (2026-08-30). JUPITER_LOAD counts in process memory and the runner
    # restarted twice on 2026-08-30, so that day's funded rate-limit record is gone. host_429 is
    # per HOST ATTEMPT (pressure) and rate_limited is per QUOTE after every host failed (a lost
    # decision), so host_429 >= rate_limited by construction and the two must never be summed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_jupiter_load (
            ts_minute TEXT NOT NULL,
            role TEXT NOT NULL,
            host TEXT NOT NULL,
            quotes INTEGER NOT NULL DEFAULT 0,
            host_429 INTEGER NOT NULL DEFAULT 0,
            rate_limited INTEGER NOT NULL DEFAULT 0,
            errors INTEGER NOT NULL DEFAULT 0,
            auth_failed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (ts_minute, role, host)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_jupload_minute "
                 "ON memecoin_jupiter_load(ts_minute)")

    # VOLUME-DEATH SHADOW (2026-08-26). Immutable would-fire observations for a rule that is NOT
    # armed. Joinable to the position's own final outcome, which is the whole question: what would
    # this rule have done to a real close. `blind_frozen` marks a row where the stream was down, so
    # the silence measures OUR outage and must never be counted as market quiet.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_volume_death_shadow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            mint TEXT,
            fired_at TEXT NOT NULL,
            silence_s REAL,
            thresholds TEXT,
            top_threshold_s INTEGER,
            mark_price REAL,
            stream_healthy INTEGER NOT NULL,
            blind_frozen INTEGER NOT NULL,
            observable INTEGER
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_voldeath_pos "
                 "ON memecoin_volume_death_shadow(position_id, top_threshold_s)")

    # REVIVAL WATCH outcomes (2026-08-26). Every watch ends exactly once: cleared, expired,
    # superseded, or dropped at capacity. Joining this to memecoin_entry_attempts on
    # origin_event_id is what makes the arm measurable later without archaeology — the re-injected
    # attempt carries the SAME origin event as the refusal that started the watch.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_revival_watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin_event_id TEXT,
            mint TEXT NOT NULL,
            network_id TEXT,
            refused_at TEXT NOT NULL,
            refused_mcap REAL,
            refused_need REAL,
            outcome TEXT NOT NULL,
            detail TEXT,
            polls INTEGER,
            finished_at TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_revival_outcome "
                 "ON memecoin_revival_watches(outcome, finished_at)")

    # REVIVAL WATCH per-poll evidence (2026-09-20, Gamma's pre-registered acceptance criteria).
    # `memecoin_revival_watches` holds `polls` as a TOTAL and one row per watch at conclusion, so it
    # cannot hold the bound each individual poll read. This is append-only, one row per poll of
    # `revival_watch_volume_cleared()`, joinable to the parent watch on
    # (origin_event_id, mint, network_id). `usd`/`reason`/`coverage`/`covered_s`/`measured_at` are
    # the on-chain bound, the SAME fields the entry gate records via
    # `trace.mark_substage("onchain_volume_m5", ...)` -- `reason` here is the bound's own reason
    # (window_incomplete, feed_stale, ...), never the decision's. `decision_reason` is what
    # `volume_alive_decision` returned for this poll; `allowed` is its verdict. `source_age_s` is
    # recorded exactly as passed to that decision (None today, since the revival call never passes
    # it) -- never computed here, so a later fix to that gap stays auditable against these rows.
    #
    # NULL SEMANTICS on covered_s and measured_at (2026-09-20). `gate_evidence()`
    # (memecoin_volume_writer.py:439) has three return paths and they do not carry the same keys:
    #   1. Chain not in MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS (:444) -> returns None outright. Every
    #      column here is NULL, including `reason` -- a NULL `reason` means THIS, not a measured
    #      empty reading.
    #   2. Unidentifiable source tx (:447) -> {'usd','reason','pools','coverage'}. `reason` is
    #      'source_tx_unknown'; covered_s and measured_at are absent from the dict, so both are
    #      NULL here because the key never existed, not because a value was measured as empty.
    #   3. `read_for_gate()` (:272) -> `{**measure(...), 'measured_at': at}`.
    # `reason` is the field to read first, but "does `reason` exist" does not sort cleanly into
    # "does covered_s exist". `measure()`'s own early returns (:227, :229, :236, :243) skip the
    # `out['covered_s']=covered` assignment at :250 entirely. Every reason `onchain_volume_m5()`
    # produces reaches :250 UNCONDITIONALLY -- there is no check between that call and the
    # assignment, success or not. The decode, verified against every return in both files:
    #
    #   covered_s NULL -> reason is one of: window_incomplete, feed_stale (measure()'s OWN check,
    #                     :229), header_missing, source_tx_unknown, volume_store_unavailable, or
    #                     reason itself is NULL (chain not enabled at all, case 1 above).
    #   covered_s SET  -> reason is one of: ok, no_known_pool, no_feed, feed_stale
    #                     (onchain_volume_m5's OWN check, memecoin_onchain_volume.py:60),
    #                     quote_not_priceable, protocol_lagging.
    #
    # `covered_s IS NOT NULL` does NOT mean a bound was measured -- `quote_not_priceable` is in
    # that row and `usd` is still None there. To ask "does a bound exist", filter on `usd`, never
    # on `covered_s`.
    #
    # `feed_stale` is the only reason that appears in BOTH rows above, and the two cases are
    # different faults, not the same one twice. covered_s NULL means OUR OBSERVER is behind -- no
    # fresh `volume_feed` row -- the reset/disconnect regime. covered_s SET **and
    # covered_gap_s = 0** means the observer is current and the TAPE ITSELF is quiet: a window of
    # known age whose newest block is stale. Attributing a feed_stale poll to a feed reset is
    # correct in the first case and WRONG in the second; do not read one as the other.
    #
    # ⚠️ covered_s IS THE WINDOW REQUESTED, NOT THE BLOCKS PRESENT, AND IT CANNOT FAIL
    # (2026-09-23). On 2026-09-23 the V4 subscription fell 652.9 s behind on a live socket and
    # then dropped 2,754 blocks; the three attempts inside that window each recorded
    # `covered_s = 300.0` with ZERO swaps, and the sentence above read as 'the tape was quiet'
    # when the tape was blind. `covered_gap_s` is how much of the window the coin's own
    # subscription had not delivered, so the coverage actually seen is
    # `covered_s - covered_gap_s`. It is a LOWER bound on the loss: a span delivered late and
    # then dropped reads 0 once delivery recovers, because the lag is healthy again while the
    # blocks stay missing. NULL means no covered pool was known for the mint, so no
    # subscription could be behind -- unknown, not zero.
    #
    # poll_index comes from `watch["polls"]`, incremented at memecoin_revival_watch.py:166 BEFORE
    # `clears_fn` runs at :175. A poll that returns early on invalid mcap
    # (revival_watch_volume_cleared's own guard) reads nothing and writes no row here. So a GAP in
    # poll_index for a given watch means "no read happened" on that poll, by design -- not a lost
    # row.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_revival_watch_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin_event_id TEXT,
            mint TEXT NOT NULL,
            network_id TEXT,
            poll_index INTEGER NOT NULL,
            polled_at TEXT NOT NULL,
            usd REAL,
            reason TEXT,
            coverage TEXT,
            covered_s REAL,
            measured_at REAL,
            allowed INTEGER NOT NULL,
            decision_reason TEXT,
            source_age_s REAL,
            covered_gap_s REAL
        )""")
    # Added 2026-09-23 beside `covered_s`, which cannot fail: see the decode above. Rows
    # written before this carry NULL, which reads as unknown and not as zero loss.
    if "covered_gap_s" not in {r[1] for r in conn.execute(
            "PRAGMA table_info(memecoin_revival_watch_polls)")}:
        conn.execute("ALTER TABLE memecoin_revival_watch_polls ADD COLUMN covered_gap_s REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_revival_poll_watch "
                 "ON memecoin_revival_watch_polls(origin_event_id, mint, network_id)")

    # POST-TP FOLLOW-UP (measurement only, 2026-08-26). After a funded take-profit we stop
    # observing the coin, so `executable_peak_price` stops ratcheting at our exit and the post-exit
    # tail is UNMEASURED rather than proven absent. These two tables price that tail with real exit
    # quotes for the exact quantity we sold. They are evidence and nothing else: no decision, gate,
    # policy, inventory or report of funded state may read them.
    # Schedule and result are separate so a restart can retry an unfinished schedule without ever
    # overwriting a result (the V6 post-exit doctrine, 2026-07-27).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_post_tp_schedules (
            followup_id TEXT PRIMARY KEY,
            position_id INTEGER NOT NULL,
            network_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            horizon_s INTEGER NOT NULL,
            due_at TEXT NOT NULL,
            closed_at TEXT NOT NULL,
            token_qty_atomic TEXT NOT NULL,
            token_decimals INTEGER,
            exit_usdc REAL,
            created_at TEXT NOT NULL,
            UNIQUE (position_id, horizon_s)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_post_tp_results (
            followup_id TEXT PRIMARY KEY,
            position_id INTEGER NOT NULL,
            horizon_s INTEGER NOT NULL,
            status TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            elapsed_s REAL,
            quote_out_usdc REAL,
            unit_price REAL,
            detail TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_post_tp_due "
                 "ON memecoin_post_tp_schedules(due_at, followup_id)")

    # Durable spend ledger for the automatic gas top-up. The DAILY CAP is the safety property of an
    # automated swapper, and a cap held in memory resets on every restart — which is exactly when a
    # misbehaving loop would be restarting. One row per confirmed top-up; the cap sums the UTC day.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_gas_topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            day TEXT NOT NULL,
            usdc_spent REAL NOT NULL,
            usdc_before REAL,
            sol_received REAL,
            sol_before REAL,
            tx_sig TEXT,
            status TEXT NOT NULL,
            purpose TEXT NOT NULL DEFAULT 'gas'
        )""")
    gas_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_gas_topups)")}
    if "usdc_before" not in gas_cols:
        conn.execute("ALTER TABLE memecoin_gas_topups ADD COLUMN usdc_before REAL")
    if "purpose" not in gas_cols:
        conn.execute("ALTER TABLE memecoin_gas_topups ADD COLUMN purpose TEXT NOT NULL DEFAULT 'gas'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_gas_topups_day "
                 "ON memecoin_gas_topups(day, status)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_evm_gas_topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            day TEXT NOT NULL,
            chain_id TEXT NOT NULL,
            native_amount_atomic TEXT NOT NULL,
            native_before_atomic TEXT NOT NULL,
            tx_hash TEXT NOT NULL UNIQUE,
            nonce INTEGER NOT NULL,
            status TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'unwrap'
        )""")
    evm_gas_cols = {row[1] for row in conn.execute("PRAGMA table_info(memecoin_evm_gas_topups)")}
    if "direction" not in evm_gas_cols:
        conn.execute("ALTER TABLE memecoin_evm_gas_topups "
                     "ADD COLUMN direction TEXT NOT NULL DEFAULT 'unwrap'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_evm_gas_topups_chain_status "
                 "ON memecoin_evm_gas_topups(chain_id, status, day)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_entry_retry_queue (
            fomo_event_id TEXT PRIMARY KEY,
            mint TEXT NOT NULL,
            network_id TEXT NOT NULL,
            first_queued_at TEXT NOT NULL,
            next_attempt_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            event_json TEXT NOT NULL
        )""")
    # A BLIND MISS AND AN UNINDEXED POOL WAIT ON DIFFERENT THINGS (2026-09-08).
    # The one retry window is calibrated for INDEX LAG — the comments cite Initialize surfacing
    # 38-61s after the block — and a provider outage is not that. HODL (0x110cfd84, 19:06:02Z)
    # retried six times over 53s while v4 discovery and the v2 read alternated failures, then the
    # window expired; discovery found its pool in 670ms once the endpoints came back. Recording
    # WHICH kind of miss a row has taken lets the reap give the two different budgets.
    _retry_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_entry_retry_queue)")}
    if "blind_misses" not in _retry_cols:
        conn.execute("ALTER TABLE memecoin_entry_retry_queue "
                     "ADD COLUMN blind_misses INTEGER NOT NULL DEFAULT 0")
    # DIP RETRY (2026-09-30): a chase-refused buy from a named wallet waits here for its price to
    # come back (sniper/services/memecoin_dip_retry.py). NULL on every other row. A dip row is never
    # due, reaped or paged by the blind lane; its own deadline, reference and cap live in here.
    if "dip_json" not in _retry_cols:
        conn.execute("ALTER TABLE memecoin_entry_retry_queue ADD COLUMN dip_json TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_dip_retry_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fomo_event_id TEXT NOT NULL,
            source_user_id TEXT,
            mint TEXT NOT NULL,
            hit_at TEXT NOT NULL,
            watch_premium_pct REAL,
            cap_pct REAL,
            status TEXT,
            reason TEXT,
            position_id INTEGER
        )""")
    # EVM extension + Gamma MEDIUM 2 (2026-09-30): which chain, and the watch's view of the row, so
    # every queued row ends in one terminal log row (hit, expired, source_sold, knob_off, ...) and
    # the dip-back share has a denominator. NULL network_id on older rows = solana.
    _dip_log_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_dip_retry_log)")}
    for column, kind in (("network_id", "TEXT"), ("min_watch_premium_pct", "REAL"),
                         ("watch_quotes", "INTEGER"), ("watch_unknown", "INTEGER"),
                         # the watch's executable ticks, tick 0 = the refusal's quote (86d053ce)
                         ("watch_series_json", "TEXT"),
                         # which cap a row ran under (2026-10-09, dip max premium knob)
                         ("gate_cap_pct", "REAL"), ("cap_source", "TEXT")):
        if column not in _dip_log_cols:
            conn.execute(f"ALTER TABLE memecoin_dip_retry_log ADD COLUMN {column} {kind}")
    # TRIAL ARM (2026-10-01, spec 2026-10-01-memecoin-trial-arm-design.md section 6). One open trial
    # per (wallet, chain). `positions` equals the rows of memecoin_trial_positions for the trial,
    # a link keyed by position id, so a retried or recovered booking can never count twice.
    # memecoin_trial_verdicts.checkpoint ('5', '10', '20', 'day14') makes each verdict write-once.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_trial_arms (
            id INTEGER PRIMARY KEY, source_user_id TEXT NOT NULL, chain TEXT NOT NULL,
            clip_usd REAL NOT NULL, rule TEXT NOT NULL, started_at TEXT NOT NULL,
            actor TEXT NOT NULL, positions INTEGER NOT NULL DEFAULT 0,
            stopped_at TEXT, ended_at TEXT, end_reason TEXT)""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_open "
                 "ON memecoin_trial_arms(source_user_id, chain) WHERE ended_at IS NULL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_trial_positions (
            position_id INTEGER PRIMARY KEY, trial_id INTEGER NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trial_positions_trial "
                 "ON memecoin_trial_positions(trial_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_trial_verdicts (
            id INTEGER PRIMARY KEY, trial_id INTEGER NOT NULL, checkpoint TEXT NOT NULL,
            checked_at TEXT NOT NULL, closes INTEGER NOT NULL, mean_r REAL,
            verdict TEXT NOT NULL, action TEXT, detail_json TEXT NOT NULL)""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trial_verdict_once "
                 "ON memecoin_trial_verdicts(trial_id, checkpoint)")
    # WHICH VENUE, WHICH RUNG, WHICH REASON (2026-09-06). `entry_quote_probe_fn` already builds the
    # per-rung ladder trace (hit/skipped/blind_reason/reason for v3/v2/pons_v2/v4/v4_multihop) on
    # every miss, then discarded everything but the one-word code -- PUNK aborted
    # `no_route_all_venues` and left nothing behind to say whether the real pool (fee=0,
    # hook=0xe5e702..., already in our own cache 33 minutes before the buy) actually got tried,
    # or why it came back empty. Same defect class HOOD named 2026-08: "NOTHING recorded whether
    # those misses were the token, the route or the provider. Those are three different fixes."
    # This does not decide which of the three it is -- it makes that question answerable next time.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_entry_route_misses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT NOT NULL,
            network_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            code TEXT NOT NULL,
            detail TEXT,
            rungs_json TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_entry_route_misses_mint "
                 "ON memecoin_entry_route_misses(mint, network_id, observed_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_user_id TEXT, source_handle TEXT,
            mint TEXT, ticker TEXT,
            entry_price REAL, entry_notional REAL, entry_fill_source TEXT,
            entry_lag_s REAL, their_size_usd REAL,
            entry_liquidity REAL, entry_token_age_s REAL,
            is_open INTEGER DEFAULT 1,
            opened_at TEXT, opened_recorded_at TEXT,
            closed_at TEXT, closed_recorded_at TEXT, realized_pnl REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            kind TEXT, price REAL, qty REAL, notional REAL,
            fill_source TEXT, fraction REAL, created_at TEXT
        )""")
    # idempotent migration: per-event copy outcome (opened/skipped_dup/exit/ignored)
    ev_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_events)")}
    if "action" not in ev_cols:
        conn.execute("ALTER TABLE memecoin_events ADD COLUMN action TEXT")
    # copy-logic migrations: exit_reason on positions; chase-guard counterfactual on events
    pos_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_positions)")}
    if "exit_reason" not in pos_cols:
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN exit_reason TEXT")
    if "tp_target_price" not in pos_cols:
        # Hot TP lane (2026-08-22): USD take-profit target, written once from entry_price and
        # the frozen contract's tp_pct, never updated.
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN tp_target_price REAL")
    if "cluster_decision_id" not in pos_cols:
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN cluster_decision_id TEXT")
    if "fdv_supply" not in pos_cols:
        # TOTAL supply in whole tokens (2026-09-25), pinned ONCE from the first market snapshot that
        # carries both fdv and price. Operator mcap targets read live price x this, the FDV basis
        # GMGN shows. NULL = unknown: the targets stay on the provider's market cap. Never 0.
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN fdv_supply REAL")
    if "display_total_supply" not in pos_cols:
        # DISPLAY ONLY (2026-10-02, operator order "a"): an EVM coin's on-chain totalSupply in whole
        # tokens, the supply GMGN's market cap uses, pinned once by the exit sweep. No gate, target or
        # band reads it; an operator mcap target saved off a card on this basis pins fdv_supply.
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN display_total_supply REAL")
    if "entry_token_age_source" not in pos_cols:
        # WHERE entry_token_age_s CAME FROM (2026-09-22). The value derives from DexScreener's
        # pairCreatedAt, and a pre-graduation LaunchLab curve is not indexed — 2 of 32 funded
        # LaunchLab closes carry an age against 93% on Raydium CPMM. A chain-derived source fills
        # the gap, and the two must never be read as the same fact: `mint_first_slot` is an
        # inference from the mint's oldest signature, `dexscreener_pair` is the venue's own
        # timestamp. NULL on a row that HAS an age means it predates this column and came from
        # the pair.
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN entry_token_age_source TEXT")
    if "executed_pool" not in pos_cols:
        # THE POOL THE FILL ACTUALLY TOUCHED (2026-09-22, Gamma). Nothing recorded it:
        # `quote_route_target` is the router we routed THROUGH, and `evm_route_json.pool_id` is the
        # pool the QUOTE chose — present on 42% of funded EVM fills since 09-15, absent on every
        # relay and aggregator route, and not the same fact. BUDDY (2026-09-06) hit a decoy pool
        # while the real one also quoted and marked -61% four seconds after the fill, so quoted and
        # executed must be separately readable. Filled from our own receipt at fill time.
        # NOT BACKFILLED: a NULL here means "we did not record it", never "there was no pool".
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN executed_pool TEXT")
    if "executed_pool_source" not in pos_cols:
        # WHICH WITNESS NAMED IT. `receipt_v4_swap` / `receipt_v3_swap` / `receipt_v2_swap` are
        # decoded from our transaction receipt; `sol_position_pools` is carried over from the
        # Solana tape's own resolution and is NOT a receipt decode. A reader must be able to tell
        # those apart before trusting the pool for anything.
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN executed_pool_source TEXT")
    if "tracking_paused_at" not in pos_cols:
        # ATTENTION SWITCH, NOT A CLOSE (2026-09-04, operator). A dust play whose frozen policy has
        # every rail at 0 can never produce an exit decision, yet it still cost 3,481 marks an hour
        # in quotes and RPC. Set this and the exit sweep skips it and the operator view hides it.
        # `is_open`, quantity and PnL are untouched, and every exposure/bankroll/slot calculation
        # keeps counting it, because the tokens are still in the wallet.
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN tracking_paused_at TEXT")
    if "lifi_dependent" not in pos_cols:
        conn.execute(
            "ALTER TABLE memecoin_positions ADD COLUMN lifi_dependent INTEGER NOT NULL DEFAULT 0"
        )
    if "evm_route_json" not in pos_cols:
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN evm_route_json TEXT")
    for _col in ("wouldbe_price", "chase_gap_pct"):
        if _col not in ev_cols:
            conn.execute(f"ALTER TABLE memecoin_events ADD COLUMN {_col} REAL")
    # WS2 (2026-07-15): the REAL-leg outcome for this event, kept SEPARATE from the paper `action`
    # (both legs read the same event row) so the Activity/Decisions surface shows what we actually
    # did on the real path (skipped_mcap, not_approved, unroutable_chain, filled, …) + a reason.
    for _col in ("real_decision", "real_decision_reason", "real_decision_at"):
        if _col not in ev_cols:
            conn.execute(f"ALTER TABLE memecoin_events ADD COLUMN {_col} TEXT")
    # follow-sync migration: wallet provenance (manual | follow) — existing rows -> manual
    wallet_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_wallets)")}
    if "source" not in wallet_cols:
        conn.execute("ALTER TABLE memecoin_wallets ADD COLUMN source TEXT DEFAULT 'manual'")
    # discovery provenance: where a promoted wallet was FOUND ('large_buy' feed | 'leaderboard' |
    # 'manual') — kept distinct from `source` (how it's maintained) so forward copy-PnL can be
    # split by discovery pool (the leaderboard-vs-feed study).
    if "discovery_source" not in wallet_cols:
        conn.execute("ALTER TABLE memecoin_wallets ADD COLUMN discovery_source TEXT")
    # operator EXCLUSION: a followed wallet the operator cut must stay cut — the follow-sync
    # re-adds (delete) AND un-pauses (pause) any wallet still in the fomo graph, so neither
    # alone sticks. excluded=1 is respected by the sync and curated_user_ids.
    if "excluded" not in wallet_cols:
        conn.execute("ALTER TABLE memecoin_wallets ADD COLUMN excluded INTEGER DEFAULT 0")
    # real-money allowlist (2026-07-10): only approved wallets' buys reach the real leg.
    # Default 0 = watch-only (paper + Telegram, no real entry).
    if "real_approved" not in wallet_cols:
        conn.execute("ALTER TABLE memecoin_wallets ADD COLUMN real_approved INTEGER DEFAULT 0")
    # per-chain arming (2026-07-30): `real_chains` is the AUTHORITY list — the chains a wallet's
    # buys may open real positions on. NULL/empty = armed NOWHERE (fail-closed); it is never read
    # as "every chain". This reverses the 2026-07-15 global-arming decision, under which the column
    # was vestigial and always NULL. See real_approved_user_ids.
    if "real_chains" not in wallet_cols:
        conn.execute("ALTER TABLE memecoin_wallets ADD COLUMN real_chains TEXT")
    # arming audit trail (2026-07-12): append-only log of every real-allowlist change with the
    # wallet's copy-PnL evidence at decision time. Added after the roster went 20->48 with no
    # record — the criterion was unreconstructable. Writes happen inside set_wallet_real_approved
    # so EVERY path (UI, auto-demote, scripts) is captured.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_wallet_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_user_id TEXT NOT NULL,
            action TEXT NOT NULL,            -- approve | approve_refused | revoke
            actor TEXT,                      -- ui | operator | auto_demote | backfill | ...
            reason TEXT,
            copy_pnl REAL,                   -- alpha_b cum realized at decision time
            copy_closes INTEGER,             -- alpha_b closed count at decision time
            forced INTEGER DEFAULT 0,        -- 1 = operator override below the gate
            created_at TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_approvals_wallet "
                 "ON memecoin_wallet_approvals(source_user_id, created_at DESC)")
    # per-chain arming (2026-07-30): which chain a chain_arm / chain_revoke row refers to.
    # NULL on legacy whole-wallet approve/revoke rows. Funded cohorts are computed per
    # (wallet, chain) from these rows, so a chain re-armed after a demotion starts clean.
    approval_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_wallet_approvals)")}
    if "chain" not in approval_cols:
        conn.execute("ALTER TABLE memecoin_wallet_approvals ADD COLUMN chain TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_first_source_buys (
            source_user_id TEXT NOT NULL,
            network_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            cohort_start TEXT NOT NULL,
            source_signature TEXT NOT NULL,
            source_slot INTEGER,
            observed_at TEXT NOT NULL,
            PRIMARY KEY(source_user_id, network_id, mint, cohort_start)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_source_buy_slots (
            source_signature TEXT PRIMARY KEY,
            source_slot INTEGER,
            observed_at TEXT NOT NULL
        )""")
    # operator mcap-band take-profit targets (2026-07-13): a standing exit the operator sets by
    # MARKET CAP ('sell TripleT between 22.5M and 24M'). The runner's own rails are %-based (TP/stop);
    # this adds a per-position mcap band that real_exit_sweep consults each cycle and full-sells when
    # live mcap first reaches the lower bound. At most one active target per position (partial index).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_exit_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            mcap_low REAL NOT NULL,
            mcap_high REAL,
            status TEXT NOT NULL DEFAULT 'active',   -- active | filled | cancelled
            actor TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_exit_target_active "
                 "ON memecoin_exit_targets(position_id) WHERE status='active'")
    # Per-token liquidity store (2026-07-18): the ONLY reliable 4663 liquidity is GT's reserve_in_usd,
    # and the web can't reach GT (runner saturates the per-IP limit). The runner captures that reserve
    # for free from the pool discovery it already makes and writes it here; the display reads it.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_token_liquidity (
            chain_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            reserve_usd REAL,
            updated_at TEXT,
            PRIMARY KEY (chain_id, mint)
        )""")
    # signal-source attribution: which pipeline surfaced the signal — 'global_feed'
    # (real-time /feed, front-runnable) vs 'follow_activity' (per-follow /swaps poll, late).
    # Carried onto positions so paper PnL can be compared by source.
    if "signal_source" not in ev_cols:
        conn.execute("ALTER TABLE memecoin_events ADD COLUMN signal_source TEXT DEFAULT 'global_feed'")
    if "delivery_lane" not in ev_cols:
        conn.execute("ALTER TABLE memecoin_events ADD COLUMN delivery_lane TEXT")
    if "signal_source" not in pos_cols:
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN signal_source TEXT DEFAULT 'global_feed'")
    # network provenance: fomo chain id ('solana' | '8453' Base | '56' BSC | ...). Persisted on
    # events (feed truth) AND positions (so per-chain forward eval works) so the real leg can route
    # by chain. The fomo /feed already carries networkId; we were dropping it.
    if "network_id" not in ev_cols:
        conn.execute("ALTER TABLE memecoin_events ADD COLUMN network_id TEXT")
    # copy-hardening: execution-strategy arm + momentum state + instrumentation
    for _col, _ddl in (("strategy", "TEXT DEFAULT 'control'"),
                       ("peak_price", "REAL"), ("peak_ts", "TEXT"),
                       # Exact full-balance high-water for funded trails. Nominal peak_price stays
                       # separate so an unsellable display spike cannot ratchet a real-money exit.
                       ("executable_peak_price", "REAL"),
                       # Last RESOLVED exact whole-balance unit price and when. The loss rails read
                       # the nominal mid whenever this sweep's exact quote fails, and on a thin pool
                       # that mark is ~2x what the bag fetches -- position 29763 sat at -62.8% real
                       # while its -50% stop measured -27.9% nominal and never nominated. Keeping the
                       # last resolved exact price lets the rails stay conservative through an
                       # outage instead of switching to the optimistic number.
                       ("last_executable_price", "REAL"),
                       ("last_executable_at", "TEXT"),
                       # Anchor-checked EVM exit (spec 2026-10-01): a sticky USD-per-token level
                       # the sweep moves on a passing preview and re-bases on a persistent low one.
                       # NULL reads as the entry price. last_executable_price stays the rails' input.
                       ("exit_anchor_price", "REAL"),
                       ("exit_anchor_at", "TEXT"),
                       # Consecutive sweeps the loss rails have run on the nominal mid because the
                       # exact price would not resolve. Alerts once at the threshold, not per sweep.
                       ("rails_blind_streak", "INTEGER DEFAULT 0"),
                       ("no_new_high_sweeps", "INTEGER DEFAULT 0"),
                       ("time_to_peak_s", "REAL"), ("max_gain_pct", "REAL"),
                       # The highest DISPLAY tick no second tick confirmed (2026-10-17, ANIMA 73930):
                       # the card marks the peak unconfirmed while this sits above peak_price.
                       ("peak_unconfirmed_price", "REAL"),
                       # Signal-time mcap frozen at entry. Feed mcap wins; the same-pass DEX mcap
                       # is a fallback. Never recompute this from a later quote.
                       ("entry_mcap", "REAL"),
                       # Execution-quality instrumentation (2026-07-15): the trader's entry price
                       # frozen at open + our fill vs it. fill_gap_pct = (our_fill − their_price) /
                       # their_price × 100 — how far our REAL fill drifted from the price we were
                       # copying (chase / slippage / latency cost). Feeds the speed roadmap; NULL on
                       # paper (modeled fills) and when the trader's price wasn't on record.
                       ("their_price", "REAL"), ("fill_gap_pct", "REAL"),
                       ("attempt_id", "TEXT"),
                       ("price_attribution", "TEXT"),
                       ("quote_price", "REAL"),
                       ("quote_observed_at", "TEXT"),
                       ("quote_age_ms", "REAL"),
                       ("quote_id", "TEXT"),
                       ("quote_route_target", "TEXT"),
                       ("source_drift_pct", "REAL"),
                       ("execution_slippage_pct", "REAL"),
                       ("total_copy_drift_pct", "REAL"),
                       ("funded_contract_version", "INTEGER"),
                       ("funded_contract_hash", "TEXT"),
                       ("funded_contract_json", "TEXT"),
                       # Exact policy frozen in the position row. Only experimental policy arms
                       # populate these; existing arms retain NULL and unchanged behavior.
                       ("assigned_policy_id", "TEXT"),
                       ("assigned_policy_version", "INTEGER"),
                       ("assigned_policy_json", "TEXT"),
                       # Forward paper evidence identity. Historical rows intentionally remain
                       # NULL and are reported as legacy_unversioned; never fabricate a backfill.
                       ("paper_cohort_id", "TEXT"),
                       ("paper_cohort_json", "TEXT"),
                       ("experiment_status", "TEXT"),
                       # last observed live mid + when — so a time cap can still fire for a
                       # position that has gone UNQUOTABLE (DexScreener returns nothing): we
                       # close at the last real price we saw instead of letting it ride forever.
                       ("last_quote_price", "REAL"), ("last_quote_ts", "TEXT"),
                       # originating feed event — so cross-arm reads can compare arms ONLY on
                       # events where ALL arms entered (a fast arm's extra re-buy, which slow
                       # arms skip as a dup while still holding, must not skew the A/B).
                       ("origin_event_id", "TEXT"),
                       # hold-band study: FREEZE the trader's band at open (median_hold_s is a
                       # live, re-computable value; joining it at read time would silently
                       # reclassify historical positions when the analyzer re-runs).
                       ("hold_band", "TEXT"),
                       # one-time transition marker for pre-real-UTC open positions whose
                       # opened_at/peak_ts/last_quote_ts were stamped on the fast host clock.
                       ("hold_clock_normalized_at", "TEXT"),
                       # Corrected local observation time when the position insert committed.
                       # This remains available if best-effort attempt tracing fails after fill.
                       ("opened_recorded_at", "TEXT"),
                       # Corrected local observation time when close bookkeeping committed. The
                       # strategy's closed_at may be sampled before a slow on-chain confirmation;
                       # telemetry windows use this field so late commits cannot disappear.
                       ("closed_recorded_at", "TEXT"),
                       # S1 real-money discriminator: 'paper' (default) | 'real'. The daily-loss
                       # breaker + real exposure sum count ONLY 'real'; S2/S5 populate it on real
                       # fills. A mixed ledger would let paper losses trip the real breaker.
                       ("execution_mode", "TEXT DEFAULT 'paper'"),
                       # WO-5 controller state — persisted exit-decision hysteresis that survives
                       # restarts by construction. All NULL/0 by default, so the engine's behavior is
                       # UNCHANGED until WO-7 registers the persistence-window + sizing constants that
                       # read them (structure break requires two decoded events or a persistence
                       # window; derisk sizes on route/flow staleness, not age).
                       ("structure_break_since", "TEXT"),
                       ("consecutive_break_events", "INTEGER DEFAULT 0"),
                       ("negative_flow_since", "TEXT"),
                       ("route_degradation_since", "TEXT"),
                       ("route_failure_streak", "INTEGER DEFAULT 0"),
                       ("infra_failure_streak", "INTEGER DEFAULT 0"),
                       # WO-5A deterministic controller reducer. controller_revision is the CAS guard
                       # (optimistic concurrency: a loser reloads and recomputes); the event id dedups
                       # chain-grounded break/flow advances; executable_peak_at pairs with the existing
                       # executable_peak_price so no-progress is measured from a real peak TIME, not
                       # position age; route_quote_value/at carry the last whole-bag quote so route
                       # observations dedup by quote identity. All NULL/0 -> inert until WO-5B/WO-7.
                       ("controller_revision", "INTEGER DEFAULT 0"),
                       ("controller_last_event_id", "TEXT"),
                       ("controller_last_event_block", "INTEGER"),
                       ("controller_last_event_log_index", "INTEGER"),
                       ("executable_peak_at", "TEXT"),
                       ("route_quote_value", "REAL"),
                       ("route_quote_at", "TEXT"),
                       # S3 stuck bag: a real position whose sell ladder exhausted (illiquid/rug) —
                       # stays open + flagged + alerting, NEVER a fake clean close.
                       ("stuck_bag", "INTEGER DEFAULT 0"),
                       # network provenance (mirrors memecoin_events) so per-chain eval + the real
                       # leg's exit routing know which venue a position lives on.
                       ("network_id", "TEXT"),
                       # one-shot de-risk marker (real leg): set when the recover-the-entry partial
                       # sell fires at +MEMECOIN_REAL_DERISK_PCT so it can never fire twice.
                       ("derisked_at", "TEXT"),
                       # unknown-confirm derisk recovery (2026-07-17, pos 6605): the partial sell's
                       # tx signature, persisted so a maybe-landed derisk can be verified later
                       # instead of silently burning the one-shot. Cleared once verified.
                       ("derisk_sig", "TEXT"),
                       # P0-A (Codex round 2, 2026-07-24): the de-risk CLAIM. Distinct from the
                       # settled derisked_at so a crash mid-executor leaves a PENDING row (not a
                       # false-confirmed one); stale pendings are released by the recovery pass.
                       ("derisk_pending_at", "TEXT"),
                       # Opaque ownership token for the pending/submitted de-risk state machine.
                       # Every destructive transition is qualified by this identity so a stale
                       # executor or recovery snapshot cannot mutate a replacement claim.
                       ("derisk_claim_id", "TEXT"),
                       # consecutive explicit not_found confirm reads for derisk_sig — three spaced
                       # reads past the blockhash window = provably dropped -> re-arm the rail.
                       ("derisk_recheck_hits", "INTEGER DEFAULT 0"),
                       # Real full-close state machine. is_open remains settled ledger state;
                       # these fields own the pre-broadcast claim and write-ahead tx identity.
                       ("close_pending_at", "TEXT"),
                       ("close_claim_id", "TEXT"),
                       ("close_submitted_at", "TEXT"),
                       ("close_sig", "TEXT"),
                       ("close_exit_reason", "TEXT"),
                       # Sell-speed measurement (2026-08-13). MONOTONIC stage durations for the
                       # exit ladder: quote/sign/submit are the pre-broadcast latency that costs
                       # money, confirm is post-broadcast patience. Needed because execute_exit
                       # reuses ONE frozen now_iso for the claim, the write-ahead and the
                       # finalization, so close_submitted_at - closed_at is always 0.
                       ("exit_timing_json", "TEXT"),
                       # WO-6: the immutable ExitIntent (JSON) bound at claim time — decision snapshot
                       # (action, fraction, controller_revision, balance, decision_state_id) plus the
                       # sign-time quote binding. Re-verified before signing so a superseded decision
                       # or a position closed mid-slippage-ladder is refused pre-sign, never signed.
                       ("exit_intent_json", "TEXT"),
                       # Paper exit ticks (spec 2026-10-08): the v1 engine's binding (which pool its
                       # ticks come from and why), the route a 56 bag quote reads, and the seconds held
                       # with and without a live tick. Paper rows only; NULL on every other row.
                       ("paper_tick_json", "TEXT"),
                       # The mark an exit decision read (source, age, tick kind, price), written once
                       # after the decision, never read by one (Gamma 2b: paper and funded alike).
                       ("exit_decision_json", "TEXT"),
                       # stop debounce (2026-07-10): consecutive sweeps the −stop condition has
                       # held. On thin memecoin books a single bad DexScreener tick (wick) read
                       # as −50% would market-sell a healthy coin into the air pocket — 2 of the
                       # 4 paper stop-outs were exactly this. Reset on any quoted non-stop sweep.
                       ("stop_hits", "INTEGER DEFAULT 0"),
                       # elapsed-time stop confirmation (2026-08-11): UTC ISO stamp of when the
                       # stop condition FIRST held, cleared on any quoted non-stop sweep. `stop_hits`
                       # above counts DETECTIONS and has no clock, so a window written in seconds
                       # and frozen into a hit count against a nominal tick became 38 minutes
                       # whenever the ~31s slow lane did the deciding — on a policy whose only other
                       # downside exit is the -80% catastrophe floor. A timestamp is the same
                       # duration on every lane.
                       ("stop_breach_started_at", "TEXT"),
                       # TP CONFIRMATION WINDOW (2026-08-29 operator). Its own column, never the
                       # stop's: the two rails confirm independently and a position can be
                       # breaching one while the other is clear. Sharing a stamp would let a stop
                       # breach re-arm a take-profit's clock.
                       ("tp_breach_started_at", "TEXT"),
                       # exact-quote failure streak (2026-07-24): consecutive sweeps on which the
                       # whole-bag executable quote could NOT be resolved while a display price
                       # still existed. One or two are a transient RPC/aggregator blip and must not
                       # blind the loss rails, which is why nomination falls back to the display
                       # mid. But a bag that is genuinely unsellable fails EVERY sweep, and the
                       # chart keeps showing a healthy price — measured 2026-07-24: a live 4663 pool
                       # returned $2.82 on a $300 chart-implied bag. Counting the streak is what
                       # separates a blip from a rug; reset on any successful exact quote.
                       ("exact_quote_misses", "INTEGER DEFAULT 0"),
                       # Infrastructure/provider failures are operational evidence, never token
                       # unsellability. Persist separately so restarts cannot erase an outage streak.
                       ("exact_quote_infra_misses", "INTEGER DEFAULT 0"),
                       # per-position operator override (2026-07-15): when 1, real_mirror_on_sell will
                       # NOT sell this position when its copied trader sells — the operator wants to
                       # hold past the trader's exit. Only the mirror copy-exit is suppressed; TP /
                       # stop / de-risk / timecap / unquotable rails still fire, so the bag is never
                       # left unprotected. Default 0 = mirror active (the copy edge).
                       ("mirror_disabled", "INTEGER DEFAULT 0")):
        if _col not in pos_cols:
            conn.execute(f"ALTER TABLE memecoin_positions ADD COLUMN {_col} {_ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pos_strat "
                 "ON memecoin_positions(source_user_id, mint, strategy, is_open)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pos_wallet_mode_open "
                 "ON memecoin_positions(source_user_id, execution_mode, is_open)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pos_open ON memecoin_positions(is_open)")
    # The cockpit SSE change signature aggregates REAL rows once a second per open stream; without
    # an execution_mode prefix that is a full scan of ~14k wide rows (~20 ms/tick, 2026-08-17).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pos_mode_open_closed "
                 "ON memecoin_positions(execution_mode, is_open, closed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_real_drawdown_replay "
                 "ON memecoin_positions(execution_mode, is_open, closed_at, realized_pnl) WHERE is_open=0 "
                 "AND execution_mode='real' AND realized_pnl IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_real_exit_nonce_candidates "
                 "ON memecoin_positions(" + COIN_NETWORK_SQL + ") WHERE "
                 + _REAL_EXIT_NONCE_CANDIDATE_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_fills_pos ON memecoin_fills(position_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_events_id ON memecoin_events(id)")
    # mint index: resolve_network_from_events / heal_open_evm_unknown_from_events filter events by
    # mint on every ~15s sweep. Without this it's a full scan of the never-pruned feed firehose,
    # which also delays the real exit sweep that runs right after bracket_pass in the same loop.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_events_mint ON memecoin_events(mint)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_events_user_time "
                 "ON memecoin_events(source_user_id, created_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_feed_observations_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            upstream_event_id TEXT NOT NULL UNIQUE,
            raw_type TEXT NOT NULL,
            normalized_side TEXT,
            source_user_id TEXT,
            source_handle TEXT,
            network_id TEXT,
            mint TEXT,
            ticker TEXT,
            their_price REAL,
            their_market_cap REAL,
            their_size_usd REAL,
            source_ts_raw TEXT,
            source_ts TEXT,
            observed_at TEXT NOT NULL,
            lag_s REAL,
            was_curated INTEGER,
            was_real_approved INTEGER,
            raw_json TEXT NOT NULL,
            capture_version INTEGER NOT NULL DEFAULT 2,
            clock_domain TEXT NOT NULL DEFAULT 'observed_real_utc'
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_feed_v2_coin_time "
                 "ON memecoin_feed_observations_v2(network_id, mint, source_ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_feed_v2_consensus "
                 "ON memecoin_feed_observations_v2(network_id, mint, normalized_side, "
                 "was_curated, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_feed_v2_user_time "
                 "ON memecoin_feed_observations_v2(source_user_id, source_ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_feed_v2_type_time "
                 "ON memecoin_feed_observations_v2(raw_type, source_ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_feed_v2_observed_at "
                 "ON memecoin_feed_observations_v2(observed_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_holder_evidence(
        network_id TEXT NOT NULL,
        mint TEXT NOT NULL,
        source_user_id TEXT NOT NULL,
        source_handle TEXT,
        evidence_state TEXT NOT NULL,
        remaining_usd REAL,
        observed_at TEXT NOT NULL,
        source TEXT NOT NULL,
        upstream_event_id TEXT,
        confirmation_state TEXT NOT NULL,
        successful_poll_at TEXT,
        invalidated_at TEXT,
        invalidation_reason TEXT,
        updated_at TEXT NOT NULL,
        clock_domain TEXT NOT NULL,
        source_event_at TEXT,
        source_timestamp_trust TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        PRIMARY KEY (network_id, mint, source_user_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_holder_evidence_fresh "
                 "ON memecoin_holder_evidence(network_id, mint, evidence_state, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_holder_evidence_user "
                 "ON memecoin_holder_evidence(source_user_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_holder_evidence_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        network_id TEXT NOT NULL,
        mint TEXT NOT NULL,
        source_user_id TEXT NOT NULL,
        source_handle TEXT,
        evidence_state TEXT NOT NULL,
        remaining_usd REAL,
        observed_at TEXT NOT NULL,
        source TEXT NOT NULL,
        upstream_event_id TEXT,
        confirmation_state TEXT NOT NULL,
        successful_poll_at TEXT,
        invalidated_at TEXT,
        invalidation_reason TEXT,
        recorded_at TEXT NOT NULL,
        clock_domain TEXT NOT NULL,
        source_event_at TEXT,
        source_timestamp_trust TEXT NOT NULL,
        evidence_hash TEXT NOT NULL,
        UNIQUE(network_id, mint, source_user_id, evidence_hash)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_holder_history_coin_time "
                 "ON memecoin_holder_evidence_history(network_id,mint,observed_at,source_user_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_capture_cohorts_v2 (
            network_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            contexts TEXT NOT NULL,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            priority INTEGER NOT NULL,
            next_due_at TEXT NOT NULL,
            skipped_sweep_count INTEGER NOT NULL DEFAULT 0,
            canonical_pair_address TEXT,
            pending_pair_address TEXT,
            pending_pair_count INTEGER NOT NULL DEFAULT 0,
            last_good_price REAL,
            PRIMARY KEY(network_id, mint)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_capture_v2_due "
                 "ON memecoin_capture_cohorts_v2(expires_at, next_due_at, priority)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_quote_sweeps_v2 (
            sweep_id TEXT PRIMARY KEY,
            observed_at TEXT NOT NULL,
            eligible_count INTEGER NOT NULL,
            requested_count INTEGER NOT NULL,
            captured_count INTEGER NOT NULL,
            budget_skipped_count INTEGER NOT NULL,
            capture_version INTEGER NOT NULL DEFAULT 2
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_quote_observations_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sweep_id TEXT NOT NULL,
            network_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            pair_address TEXT,
            dex_id TEXT,
            observed_at TEXT NOT NULL,
            source_ts TEXT,
            price REAL,
            liquidity REAL,
            market_cap REAL,
            volume_24h REAL,
            quality TEXT NOT NULL,
            contexts TEXT NOT NULL,
            capture_version INTEGER NOT NULL DEFAULT 2,
            UNIQUE(sweep_id, network_id, mint)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_quotes_v2_coin_time "
                 "ON memecoin_quote_observations_v2(network_id, mint, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_quotes_v2_quality_time "
                 "ON memecoin_quote_observations_v2(quality, observed_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_signal_outcomes_v2 (
            outcome_id TEXT PRIMARY KEY,
            upstream_event_id TEXT NOT NULL UNIQUE,
            entry_observation_id INTEGER,
            source_user_id TEXT,
            handle TEXT,
            network_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            ticker TEXT,
            signal_observed_at TEXT NOT NULL,
            ref_price REAL NOT NULL,
            target_horizons_json TEXT NOT NULL,
            expected_marks INTEGER NOT NULL,
            observed_marks INTEGER NOT NULL DEFAULT 0,
            missed_marks INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            capture_version INTEGER NOT NULL DEFAULT 2,
            clock_domain TEXT NOT NULL DEFAULT 'observed_real_utc',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_outcomes_v2_status "
                 "ON memecoin_signal_outcomes_v2(status, signal_observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_outcomes_v2_coin "
                 "ON memecoin_signal_outcomes_v2(network_id, mint, signal_observed_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_horizon_marks_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome_id TEXT NOT NULL,
            target_s INTEGER NOT NULL,
            target_at TEXT NOT NULL,
            observation_id INTEGER,
            observed_at TEXT,
            actual_age_s REAL,
            lateness_s REAL,
            price REAL,
            quality TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            UNIQUE(outcome_id, target_s),
            UNIQUE(outcome_id, observation_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_horizons_v2_pending "
                 "ON memecoin_horizon_marks_v2(status, target_at)")
    # Dup-open race guard: main-feed + follow-activity can check-then-insert concurrently. Coin
    # identity is (normalized network_id, mint); Robinhood 4663 remains distinct from Base/BSC/ETH.
    # Legacy paper duplicates are retired per exact coin key. Funded rows are never auto-closed: a
    # funded collision makes the new UNIQUE index fail loudly for operator reconciliation.
    # MODE-SCOPED (2026-07-10 P0): the key includes execution_mode — a real alpha_b fill lives
    # ALONGSIDE the paper alpha_b arm for the same (uid, mint). Without mode in the key the real
    # ledger insert raised IntegrityError AFTER the on-chain swap landed (orphaned funds), and
    # this dedup would have retired the real row as a "dup".
    conn.execute(
        "UPDATE memecoin_positions SET is_open=0, exit_reason='dup_race_dedup', "
        "realized_pnl=COALESCE(realized_pnl, 0.0), closed_at=COALESCE(closed_at, opened_at) "
        "WHERE is_open=1 AND COALESCE(execution_mode,'paper')!='real' AND id NOT IN ("
        "  SELECT MIN(id) FROM memecoin_positions WHERE is_open=1 "
        f"  GROUP BY source_user_id, {COIN_NETWORK_SQL}, mint, strategy, "
        "COALESCE(execution_mode,'paper'))")
    _create_and_verify_index(conn, "idx_mc_pos_open_uniq_v3", _POSITION_COIN_INDEX_SQL)
    # missed-sell coverage: one-time classification per trader-sell (by swap id) so the gauge
    # counts each sell once and 'missed' can't later flip to 'seen' across polling passes.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_firehose (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fomo_event_id TEXT UNIQUE,
            source_user_id TEXT, source_handle TEXT,
            mint TEXT, ticker TEXT, side TEXT,
            their_price REAL, their_market_cap REAL, their_size_usd REAL,
            is_first_buy INTEGER,
            created_at TEXT, received_at TEXT, network_id TEXT,
            was_curated INTEGER, was_real_approved INTEGER
        )""")
    # point-in-time cohort flags (2026-07-11, strategy-engine prereq): "tracked wallet" can NOT
    # be reconstructed from the mutable roster (it changed 3x today alone) — membership is
    # stamped at ingest. Pre-migration rows stay NULL = unknown; never backfill from a later roster.
    fh_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_firehose)")}
    for _col in ("was_curated", "was_real_approved"):
        if _col not in fh_cols:
            conn.execute(f"ALTER TABLE memecoin_firehose ADD COLUMN {_col} INTEGER")
    # dense quote ledger (2026-07-11, v1 exit-search prereq): every price observation the sweeps
    # already fetch for open-position mints, deduped to MINUTE resolution via UNIQUE(mint, minute).
    # Six horizon marks cannot order a +TP touch vs a -SL touch between marks; this ledger is the
    # "video" an honest exit-policy search requires. quality flags mark suspect prints.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_quote_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT NOT NULL,
            ts_minute TEXT NOT NULL,
            ts TEXT NOT NULL,
            price REAL NOT NULL,
            liquidity REAL,
            source TEXT,
            quality TEXT,
            UNIQUE(mint, ts_minute)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_quotes_mint_ts ON memecoin_quote_ledger(mint, ts)")
    # network provenance (2026-07-15): a 0x mint is not a coin identity across EVM chains. The sweep
    # price_map is already (network_id, mint)-keyed; record it so per-chain research reads can't
    # conflate chains. Nullable — NULL means unknown, never a coerced default chain. The mint-scoped
    # UNIQUE(mint, ts_minute) is left intact (rebuilding it would lock the DB vs the live 15s sweep);
    # the new covering index keeps a chain-scoped read indexed.
    _ql_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_quote_ledger)")}
    if "network_id" not in _ql_cols:
        conn.execute("ALTER TABLE memecoin_quote_ledger ADD COLUMN network_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_quotes_net_mint_ts "
                 "ON memecoin_quote_ledger(network_id, mint, ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_seen_sells (
            swap_id TEXT PRIMARY KEY,
            source_user_id TEXT, mint TEXT, seen INTEGER, classified_at TEXT
        )""")
    # S1 real-money risk envelope: pending-reservation ledger (submitted-but-unconfirmed entries
    # reserve exposure/concurrency headroom so quick signals can't double-spend the cap).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_pending_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT, source_user_id TEXT, strategy TEXT,
            reserved_notional_usd REAL,
            reserved_at TEXT, broadcast_at TEXT, tx_sig TEXT,
            status TEXT DEFAULT 'reserved',  -- reserved | confirmed | failed
            origin_event_id TEXT,
            attempt_id TEXT,
            signal_source TEXT,
            source_price REAL,
            source_market_cap REAL,
            observed_at TEXT,
            clock_domain TEXT
        )""")
    # venue tag on the reservation (S1-EVM) so per-chain gas routing knows the chain pre-fill;
    # nonce (S2-EVM) is written-ahead with the tx hash before broadcast — the EVM no-double-submit.
    pe_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_pending_entries)")}
    if "entry_fee_ceiling_lamports" not in {r[1] for r in conn.execute("PRAGMA table_info(memecoin_pending_entries)")}:
        # option B provenance (2026-10-22): priority + known tips the BOUND signed bytes can charge; NULL = unproven
        conn.execute("ALTER TABLE memecoin_pending_entries ADD COLUMN entry_fee_ceiling_lamports INTEGER")
    if "network_id" not in pe_cols:
        conn.execute("ALTER TABLE memecoin_pending_entries ADD COLUMN network_id TEXT")
    if "nonce" not in pe_cols:
        conn.execute("ALTER TABLE memecoin_pending_entries ADD COLUMN nonce INTEGER")
    # recovery marker for the orphaned-reservation sweep: a confirmed-but-position-less reservation
    # (tx landed, ledger never opened) is tagged 'orphaned' so it alerts ONCE and stays blocking.
    if "recovery_status" not in pe_cols:
        conn.execute("ALTER TABLE memecoin_pending_entries ADD COLUMN recovery_status TEXT")
    for _col, _ddl in (
        ("not_found_checks", "INTEGER DEFAULT 0"),
        ("first_not_found_at", "TEXT"),
        ("last_recovery_check_at", "TEXT"),
        ("resolution_reason", "TEXT"),
        ("origin_event_id", "TEXT"),
        ("attempt_id", "TEXT"),
        ("signal_source", "TEXT"),
        ("source_price", "REAL"),
        ("source_market_cap", "REAL"),
        ("source_liquidity", "REAL"),
        ("source_token_age_s", "REAL"),
        ("observed_at", "TEXT"),
        ("clock_domain", "TEXT"),
        ("funded_contract_version", "INTEGER"),
        ("funded_contract_hash", "TEXT"),
        ("funded_contract_json", "TEXT"),
        ("funding_asset", "TEXT"),
        ("funding_amount_atomic", "TEXT"),
        ("funding_overhead_atomic", "TEXT"),
        ("funding_sol_usd", "REAL"),
        ("evm_quote_asset_usd", "REAL"),
        ("cluster_decision_id", "TEXT"),
        ("cleanup_tx_sig", "TEXT"),
        ("cleanup_nonce", "INTEGER"),
        ("cleanup_amount_atomic", "TEXT"),
        ("cleanup_status", "TEXT"),
        ("cleanup_signed_at", "TEXT"),
        ("cleanup_signed_payload_sha256", "TEXT"),
    ):
        if _col not in pe_cols:
            conn.execute(f"ALTER TABLE memecoin_pending_entries ADD COLUMN {_col} {_ddl}")
    # Create the less-restrictive chain-aware guard before removing the legacy mint-only guard. The
    # old guard already prohibits every same-chain collision, so this order never opens a race gap.
    _create_and_verify_index(conn, "idx_mc_pending_coin_uniq_v2", _PENDING_COIN_INDEX_SQL)
    # Both replacements are now present and definition-verified. Only now remove every legacy guard.
    conn.execute("DROP INDEX IF EXISTS idx_mc_pending_mint_uniq")
    conn.execute("DROP INDEX IF EXISTS idx_mc_pos_open_uniq")   # pre-mode shape (uid,mint,strategy)
    conn.execute("DROP INDEX IF EXISTS idx_mc_pos_open_uniq_v2")
    # DESK daily halt (Codex audit #3, 2026-10-21): its own latch, so a later desk winner cannot
    # reopen the day, and the book-wide table keeps its one key shape (plain YYYY-MM-DD).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_desk_loss_trips (
            day TEXT PRIMARY KEY,          -- real-UTC YYYY-MM-DD
            tripped_at TEXT NOT NULL,
            realized_usd REAL NOT NULL,
            limit_usd REAL NOT NULL
        )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_pilot_admissions (
        cohort_id TEXT NOT NULL, desk_ticket_id TEXT NOT NULL, admitted_at TEXT NOT NULL,
        PRIMARY KEY(cohort_id,desk_ticket_id))""")
    for _action in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS pilot_admission_no_{_action.lower()} "
                     f"BEFORE {_action} ON memecoin_pilot_admissions "
                     "BEGIN SELECT RAISE(ABORT,'pilot admission is immutable'); END")
    for _ddl in ("""CREATE TABLE IF NOT EXISTS memecoin_desk_cohorts (
            cohort_id TEXT PRIMARY KEY, producer TEXT NOT NULL CHECK(producer IN ('H','U')),
            prompt_version TEXT NOT NULL, policy_hash TEXT NOT NULL, policy_json TEXT NOT NULL,
            created_at TEXT NOT NULL, first_fill_at TEXT, kill_limit_usd REAL NOT NULL,
            UNIQUE(producer, policy_hash))""",
        """CREATE TABLE IF NOT EXISTS memecoin_desk_cohort_trips (
            cohort_id TEXT PRIMARY KEY, tripped_at TEXT NOT NULL, net_usd REAL NOT NULL,
            cash_pnl_usd REAL NOT NULL, known_cost_usd REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS memecoin_desk_cohort_costs (
            cohort_id TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL,
            usd REAL, occurred_at TEXT NOT NULL, provenance TEXT NOT NULL,
            position_id INTEGER, resolved_at TEXT, PRIMARY KEY(cohort_id,event_id))""",
        """CREATE TABLE IF NOT EXISTS memecoin_desk_cost_evidence (
            cohort_id TEXT NOT NULL, event_id TEXT NOT NULL, evidence_json TEXT NOT NULL,
            evidence_hash TEXT NOT NULL, known_at TEXT NOT NULL, provenance TEXT NOT NULL,
            execution_usd REAL NOT NULL, refund_usd REAL NOT NULL,
            PRIMARY KEY(cohort_id,event_id))"""):
        conn.execute(_ddl)
    for _action in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS desk_cost_evidence_no_{_action.lower()} "
                     f"BEFORE {_action} ON memecoin_desk_cost_evidence "
                     "BEGIN SELECT RAISE(ABORT,'desk cost evidence is immutable'); END")
    _ensure_known_group_schema(conn)
    for _table in ("memecoin_positions", "memecoin_pending_entries"):
        _cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_table})")}
        for _col in ("desk_cohort_id", "desk_producer", "desk_opportunity_id", "desk_snapshot_hash"):
            if _col not in _cols:
                conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_col} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pos_desk_cohort "
                 "ON memecoin_positions(desk_cohort_id,is_open)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pending_desk_cohort "
                 "ON memecoin_pending_entries(desk_cohort_id) "
                 "WHERE signal_source='desk' AND tx_sig IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pending_pilot_capacity "
                 "ON memecoin_pending_entries(desk_cohort_id,status,desk_opportunity_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_intent_pilot_capacity "
                 "ON memecoin_manual_intents(signal_source, "
                 "CASE WHEN json_valid(desk_metadata_json) THEN "
                 "json_extract(desk_metadata_json,'$.cohort_id') END, state,desk_ticket_id)")
    # S1 latched daily-loss circuit breaker: once a day is in here it stays tripped for the day.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_daily_loss_trips (
            day TEXT PRIMARY KEY,          -- real-UTC YYYY-MM-DD
            tripped_at TEXT
        )""")
    # S2/S5 real fills carry the on-chain tx signature (chain-delta priced, fill_source='chain').
    fill_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_fills)")}
    if "tx_sig" not in fill_cols:
        conn.execute("ALTER TABLE memecoin_fills ADD COLUMN tx_sig TEXT")
    for _col, _ddl in (
        ("attempt_id", "TEXT"), ("origin_event_id", "TEXT"), ("network_id", "TEXT"),
        ("signal_source", "TEXT"), ("source_price", "REAL"),
        ("price_attribution", "TEXT"), ("quote_price", "REAL"),
        ("quote_observed_at", "TEXT"), ("quote_age_ms", "REAL"), ("quote_id", "TEXT"),
        ("quote_route_target", "TEXT"), ("source_drift_pct", "REAL"),
        ("execution_slippage_pct", "REAL"), ("total_copy_drift_pct", "REAL"),
        ("funding_asset", "TEXT"), ("funding_spent_atomic", "TEXT"),
        ("funding_sol_usd", "REAL"), ("network_fee_lamports", "INTEGER"),
        ("priority_fee_lamports", "INTEGER"), ("rent_lamports", "INTEGER"),
        ("returned_rent_lamports", "INTEGER"),
        # An EVM fill's native fee in wei, from its own receipt (gasUsed x effectiveGasPrice, plus
        # the OP-stack l1Fee): TEXT, a wei count overflows SQLite's integer. NULL = not recorded.
        ("network_fee_wei", "TEXT"),
        # EXIT ROUTE (operator 2026-10-21, for Gamma's exit-fill-vs-tape audit): the one pool an
        # exit swap traded through, when its signed route had exactly one leg. NULL = multi-hop,
        # unknown or recovered; never a guess. quote_route_target (above) keeps the whole route.
        ("executed_pool", "TEXT"),
    ):
        if _col not in fill_cols:
            conn.execute(f"ALTER TABLE memecoin_fills ADD COLUMN {_col} {_ddl}")
    # Tier-0 speed instrumentation: per-real-entry stage latencies (gate/safety/exec) so every
    # later speed decision has data instead of a guess.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_exec_latency (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT, pending_id INTEGER, status TEXT, sig TEXT,
            gate_ms REAL, safety_ms REAL, exec_ms REAL, total_ms REAL, at TEXT,
            impact_pct REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_exit_execution_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            network_id TEXT NOT NULL,
            exit_reason TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            status TEXT NOT NULL,
            sig TEXT,
            total_ms REAL,
            priority_fee_microlamports INTEGER,
            fee_paid_lamports INTEGER,
            depth_first_rung_pct REAL,
            tier_variant TEXT NOT NULL,
            failure_type TEXT
        )""")
    # WHY A CAUSE COLUMN (2026-09-02). The executor computes a cause for every stuck exit --
    # `token_balance_zero` at evm_exit_executor.py:171, the ladder-exhausted `failure_type` at
    # :360 -- and threw it away at the call site. Position 48595 produced THIRTEEN identical
    # `stuck` rows carrying no cause, so which of the two paths fired had to be inferred from a
    # Telegram message rather than read from the ledger. A status without its reason is the same
    # defect class as the shared `rpc_transport` label and the swallowed safety deadline.
    _exit_attempt_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(memecoin_exit_execution_attempts)")}
    if "failure_type" not in _exit_attempt_cols:
        conn.execute("ALTER TABLE memecoin_exit_execution_attempts ADD COLUMN failure_type TEXT")
    # The per-stage exit map was computed and then written to a LOG LINE only, while the
    # durable row one line below kept `total_ms` alone. Ten such lines survived across every
    # rotation, against 469 exit attempts on 4663 in 14 days, so exit time was
    # unattributable by construction. Same defect class as a `stuck` row with no cause.
    if "stage_ms_json" not in _exit_attempt_cols:
        conn.execute(
            "ALTER TABLE memecoin_exit_execution_attempts ADD COLUMN stage_ms_json TEXT")
    # WHAT RECONCILIATION LATER PROVED (2026-10-15). `status` is the executor's truth at the time
    # and stays; 9 of 12 'unknown' exit attempts in the 7 days to 10-15 had landed (73882's 3081
    # and 3083). landed = a fill on the same position carries the signature (an EVM hash in any
    # case, a Solana signature exactly); not_landed = that signature's exit intent ended failed or
    # dropped; unresolved otherwise; NULL for an unsigned attempt. Read-only; DROP VIEW undoes it.
    conn.execute("""
        CREATE VIEW IF NOT EXISTS memecoin_exit_attempt_outcomes AS
        SELECT a.*, CASE
            WHEN a.sig IS NULL OR TRIM(a.sig) = '' THEN NULL
            WHEN EXISTS (SELECT 1 FROM memecoin_fills f WHERE f.position_id = a.position_id
                         AND (f.tx_sig = a.sig OR (a.sig LIKE '0x%' AND lower(f.tx_sig) = lower(a.sig))))
                THEN 'landed'
            WHEN EXISTS (SELECT 1 FROM memecoin_exit_intents i WHERE i.position_id = a.position_id
                         AND i.status IN ('failed', 'dropped')
                         AND json_extract(i.intent_json, '$.tx_identity') = a.sig)
                THEN 'not_landed'
            ELSE 'unresolved' END AS outcome
        FROM memecoin_exit_execution_attempts a""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_exit_intents (
            claim_id TEXT PRIMARY KEY,
            position_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            intent_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'quoted',
            created_at TEXT,
            updated_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_exit_intent_position "
                 "ON memecoin_exit_intents(position_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_exit_intent_tx_identity ON memecoin_exit_intents"
                 "(CASE WHEN json_valid(intent_json) THEN json_extract(intent_json,'$.tx_identity') END)")
    _ensure_write_off_table(conn)            # operator write-offs' audit (2026-09-29)
    _ensure_wallet_grade_exclusions(conn)    # system-fault closes kept off wallet grades (2026-10-02)
    # OP-stack fees of our own funded transactions (Ink Task 8, 2026-09-29). One row per tx hash,
    # amounts in wei as decimal TEXT. status 'complete' or 'unknown:<parts>'; an unknown row is
    # upgraded when a later read completes it, a complete row is never rewritten.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_evm_tx_fees (
            tx_hash TEXT PRIMARY KEY,
            network_id TEXT NOT NULL,
            kind TEXT,
            block_number INTEGER,
            l2_execution_wei TEXT,
            l1_data_wei TEXT,
            operator_wei TEXT,
            total_wei TEXT,
            status TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )""")
    # Solana network fee of our own funded transactions (fee reconciler, 2026-10-21): one row per
    # signature: fee_lamports = meta.fee (base + priority); tip_lamports = SOL moved into Sender /
    # Nozomi tip accounts, a transfer outside meta.fee (Gamma 2026-10-21). status 'complete' or
    # 'unknown:<why>'; write-once like memecoin_evm_tx_fees.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_sol_tx_fees (
            tx_sig TEXT PRIMARY KEY,
            kind TEXT,
            slot INTEGER,
            fee_lamports INTEGER,
            tip_lamports INTEGER,
            status TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )""")
    # One shell row per funded buy handed to the real hook. Claimed upstream time is kept
    # separate from the local real-UTC anchor; every stage stamp is derived from that anchor and
    # one monotonic clock by EntryAttemptTrace. The row becomes immutable at terminal_status.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_entry_attempts (
            attempt_id TEXT PRIMARY KEY,
            origin_event_id TEXT,
            signal_source TEXT,
            delivery_lane TEXT,
            network_id TEXT,
            mint TEXT,
            source_user_id TEXT,
            source_handle TEXT,
            ticker TEXT,
            side TEXT,
            source_created_at TEXT,
            source_timestamp_trust TEXT NOT NULL DEFAULT 'claimed_untrusted',
            source_price REAL,
            source_market_cap REAL,
            observed_at TEXT,
            clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
            decision_started_at TEXT NOT NULL,
            cluster_decision_id TEXT,
            cluster_reason TEXT,
            cluster_metrics_json TEXT,
            cluster_completed_at TEXT,
            cluster_ms REAL,
            cluster_outcome_id TEXT,
            cluster_outcome_status TEXT,
            chain_resolution_completed_at TEXT,
            chain_resolution_ms REAL,
            market_data_completed_at TEXT,
            market_data_ms REAL,
            reservation_completed_at TEXT,
            reservation_ms REAL,
            safety_checks_completed_at TEXT,
            safety_checks_ms REAL,
            quote_completed_at TEXT,
            quote_ms REAL,
            signing_completed_at TEXT,
            signing_ms REAL,
            transaction_submitted_at TEXT,
            transaction_submitted_ms REAL,
            chain_confirmation_completed_at TEXT,
            chain_confirmation_ms REAL,
            balance_delta_completed_at TEXT,
            balance_delta_ms REAL,
            persistence_completed_at TEXT,
            persistence_ms REAL,
            quote_price REAL,
            quote_observed_at TEXT,
            quote_age_ms REAL,
            quote_id TEXT,
            quote_route_target TEXT,
            price_attribution TEXT,
            fill_price REAL,
            source_drift_pct REAL,
            execution_slippage_pct REAL,
            total_copy_drift_pct REAL,
            pending_id INTEGER,
            position_id INTEGER,
            fill_id INTEGER,
            tx_sig TEXT,
            total_ms REAL,
            terminal_status TEXT,
            terminal_reason TEXT,
            completed_at TEXT,
            trace_json TEXT,
            CHECK (
                (terminal_status IS NULL AND terminal_reason IS NULL AND completed_at IS NULL)
                OR
                (terminal_status IS NOT NULL AND length(trim(terminal_status)) > 0
                 AND terminal_reason IS NOT NULL AND length(trim(terminal_reason)) > 0
                 AND completed_at IS NOT NULL AND length(trim(completed_at)) > 0)
            )
        )""")
    _attempt_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_entry_attempts)")}
    for _col, _ddl in (
        ("delivery_lane", "TEXT"),
        ("cluster_decision_id", "TEXT"),
        ("cluster_reason", "TEXT"),
        ("cluster_metrics_json", "TEXT"),
        ("cluster_completed_at", "TEXT"),
        ("cluster_ms", "REAL"),
        ("cluster_outcome_id", "TEXT"),
        ("cluster_outcome_status", "TEXT"),
    ):
        if _col not in _attempt_cols:
            conn.execute(f"ALTER TABLE memecoin_entry_attempts ADD COLUMN {_col} {_ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_entry_attempt_coin "
                 "ON memecoin_entry_attempts(network_id, mint, decision_started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_entry_attempt_source "
                 "ON memecoin_entry_attempts(signal_source, decision_started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_entry_attempt_origin "
                 "ON memecoin_entry_attempts(origin_event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_entry_view_started "
                 "ON memecoin_entry_attempts(decision_started_at DESC, attempt_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_entry_view_completed "
                 "ON memecoin_entry_attempts(completed_at DESC, attempt_id)")
    for table in ("memecoin_pending_entries", "memecoin_positions", "memecoin_fills"):
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_view_attempt ON {table}(attempt_id)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_view_origin ON {table}(origin_event_id)")
    # quote price-impact %% per attempt (2026-07-11): filled AND aborted — the entry impact-cap
    # knob calibrates from this distribution, not from aborts alone.
    _el_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_exec_latency)")}
    if "impact_pct" not in _el_cols:
        conn.execute("ALTER TABLE memecoin_exec_latency ADD COLUMN impact_pct REAL")
    # chain provenance (2026-07-15): instrument rows carry a bare 0x mint; add the chain so a mint
    # join can't cross EVM chains. Nullable; backfilled unambiguous-only offline.
    if "network_id" not in _el_cols:
        conn.execute("ALTER TABLE memecoin_exec_latency ADD COLUMN network_id TEXT")
    for _col, _ddl in (
        ("attempt_id", "TEXT"), ("origin_event_id", "TEXT"), ("signal_source", "TEXT"),
        ("source_price", "REAL"), ("price_attribution", "TEXT"),
        ("quote_price", "REAL"), ("quote_observed_at", "TEXT"),
        ("quote_age_ms", "REAL"), ("quote_id", "TEXT"), ("quote_route_target", "TEXT"),
        ("fill_price", "REAL"), ("source_drift_pct", "REAL"),
        ("execution_slippage_pct", "REAL"), ("total_copy_drift_pct", "REAL"),
        # WO-7 execution tiering measurement: priority fee actually paid, the depth-aware first
        # slippage rung used, and a variant tag so the 20-fills-per-variant read can group.
        ("fee_paid_microlamports", "REAL"), ("depth_first_rung_pct", "REAL"),
        ("tier_variant", "TEXT"),
    ):
        if _col not in _el_cols:
            conn.execute(f"ALTER TABLE memecoin_exec_latency ADD COLUMN {_col} {_ddl}")
    _attempt_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_entry_attempts)")}
    for _col, _ddl in (
        ("quote_route_target", "TEXT"),
        ("price_attribution", "TEXT"),
        ("classification_version", "INTEGER"),
        ("classification_hash", "TEXT"),
        ("classification_json", "TEXT"),
        ("funded_contract_version", "INTEGER"),
        ("funded_contract_hash", "TEXT"),
        ("funded_contract_json", "TEXT"),
        # WHICH EXEMPTIONS APPLIED to this admitted attempt (2026-08-28). Its own column, NOT
        # classification_json: that blob is hashed into the funded contract, so adding a field
        # there would change contract bytes and every pinned prior release would fail to reconcile
        # positions written by this one. A column also makes "how do exempted entries perform" a
        # plain query instead of JSON extraction.
        ("exemptions_applied", "TEXT"),
        # THE CHASE DECISION, recorded so the refusal grading can split "the gate was right" from
        # "the gate charged the source his own footprint". Only the would-refuse path writes them.
        ("chase_naive_premium_pct", "REAL"),
        ("chase_source_impact_pct", "REAL"),
        ("chase_effective_cap_pct", "REAL"),
        ("chase_decided_by", "TEXT"),
        ("chase_base_cap_pct", "REAL"),
        ("chase_source_size_usd", "REAL"),
        ("chase_impact_formula", "TEXT"),
        ("chase_clamp_hit", "INTEGER"),
        ("chase_impact_skip_reason", "TEXT"),
        # MEASUREMENT ONLY (2026-09-02). A second, free impact estimate recorded beside the
        # quote-derived one, plus THE LIQUIDITY IT WAS COMPUTED FROM. The input is stored because a
        # derived value whose input was not stored cannot be re-graded when the model changes —
        # which is exactly why this data did not exist and every figure in the 4663 chase design
        # had to be reconstructed. No decision reads these.
        ("chase_liquidity_usd", "REAL"),
        ("chase_model_impact_pct", "REAL"),
        ("chase_model_formula", "TEXT"),
        # The exact inputs a market-data gate refused on. Written at refusal time so the
        # next threshold argument starts from hundreds of rows instead of the 24 that were
        # gradeable on 2026-08-30. See _volume_gate_inputs.
        ("gate_inputs_json", "TEXT"),
    ):
        if _col not in _attempt_cols:
            conn.execute(f"ALTER TABLE memecoin_entry_attempts ADD COLUMN {_col} {_ddl}")
    try:
        normalize_open_position_hold_stamps(conn)
    except Exception:
        pass
    try:
        backfill_network_id(conn)
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_trader_analytics (
            source_user_id TEXT PRIMARY KEY,
            handle TEXT, computed_at TEXT, data_as_of TEXT,
            swaps_analyzed INTEGER, coverage_ratio REAL, hit_full_history INTEGER,
            window_days REAL, oldest_swap TEXT, newest_swap TEXT,
            pct_off_platform REAL, pct_route_approx REAL,
            n_open_bags INTEGER, n_unmatched_sells INTEGER, unmatched_sell_ratio REAL,
            closed_coverage REAL,
            n_closed INTEGER, matched_lots INTEGER, distinct_exit_events INTEGER,
            distinct_closed_mints INTEGER, realized_pnl_usd REAL, total_buy_notional REAL,
            open_bag_cost_usd REAL, dollar_weighted_return_pct REAL,
            mean_pnl_pct REAL, median_pnl_pct REAL, win_rate REAL, profit_factor REAL,
            undefeated INTEGER,
            pnl_pct_p10 REAL, pnl_pct_p25 REAL, pnl_pct_p50 REAL, pnl_pct_p75 REAL,
            pnl_pct_p90 REAL, best_pct REAL, worst_pct REAL, return_std REAL,
            pnl_pct_dispersion_ratio REAL, realized_max_drawdown_usd REAL,
            avg_win_pct REAL, avg_loss_pct REAL, top3_win_share REAL, top3_loss_share REAL,
            median_hold_s REAL, mean_hold_s REAL, hold_p10 REAL, hold_p90 REAL,
            pct_held_under_60s REAL, pct_held_over_1d REAL,
            avg_buy_usd REAL, buy_usd_p10 REAL, buy_usd_p50 REAL, buy_usd_p90 REAL,
            max_buy_usd REAL, size_cv REAL, re_entry_rate REAL, avg_buys_per_token REAL,
            rebuy_lower_rate REAL, fee_drag_pct REAL,
            last_swap_age_h REAL, swaps_24h INTEGER, swaps_7d INTEGER, volume_7d REAL,
            large_buys_7d INTEGER, active_days INTEGER, longest_gap_h REAL,
            num_trades INTEGER, total_volume REAL, account_age_days REAL,
            followers INTEGER, following INTEGER, follower_ratio REAL,
            has_twitter INTEGER, is_restricted INTEGER, is_private INTEGER,
            profile_swap_count INTEGER,
            chain_mix TEXT, provider_mix TEXT, metrics_json TEXT
        )""")
    ana_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_trader_analytics)")}
    for _col, _ddl in _ANALYTICS_MIGRATION_COLS:
        if _col not in ana_cols:
            conn.execute(f"ALTER TABLE memecoin_trader_analytics ADD COLUMN {_col} {_ddl}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_candidates (
            source_user_id TEXT PRIMARY KEY,
            handle TEXT,
            sighting_count INTEGER DEFAULT 0,
            total_buy_usd REAL DEFAULT 0,
            max_buy_usd REAL DEFAULT 0,
            first_seen TEXT, last_seen TEXT,
            last_ticker TEXT, last_mint TEXT,
            status TEXT DEFAULT 'new'
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_cand_status ON memecoin_candidates(status)")
    cand_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_candidates)")}
    if "source" not in cand_cols:   # discovery provenance: 'large_buy' (feed) | 'leaderboard'
        conn.execute("ALTER TABLE memecoin_candidates ADD COLUMN source TEXT DEFAULT 'large_buy'")
    # fomo leaderboard snapshot (latest wins) — server-side lifetime totalPnL universe.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_leaderboard (
            source_user_id TEXT PRIMARY KEY,
            handle TEXT, rank INTEGER,
            total_pnl REAL, unrealized_pnl REAL, total_volume REAL,
            total_holdings INTEGER, num_trades INTEGER, swap_count INTEGER,
            followers INTEGER, snapshot_ts TEXT
        )""")
    # forward daily PnL snapshots — tile realized_24h into 7d/30d the ledger can't cover,
    # + fomo_total_pnl for a cadence-independent lifetime delta on leaderboard traders.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_trader_pnl_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_user_id TEXT, snapshot_ts TEXT,
            realized_24h REAL, n_closed_24h INTEGER,
            unrealized_pnl_usd REAL, live_holdings_value_usd REAL,
            fomo_total_pnl REAL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pnl_snap "
                 "ON memecoin_trader_pnl_snapshots(source_user_id, snapshot_ts)")
    # post-buy-pump: forward price-path capture per large-buy sighting -> per-trader pump profile.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_signal_outcomes (
            fomo_event_id TEXT PRIMARY KEY,
            source_user_id TEXT, handle TEXT, mint TEXT, ticker TEXT,
            buy_ts TEXT, ref_price REAL, their_size_usd REAL,
            marks_json TEXT DEFAULT '{}',
            max_gain_pct REAL, peak_horizon_s INTEGER, last_price REAL,
            n_marks INTEGER DEFAULT 0, status TEXT DEFAULT 'open',
            created_at TEXT, updated_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_outcome_status "
                 "ON memecoin_signal_outcomes(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_outcome_user "
                 "ON memecoin_signal_outcomes(source_user_id, status)")
    outcome_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_signal_outcomes)")}
    if "network_id" not in outcome_cols:
        conn.execute("ALTER TABLE memecoin_signal_outcomes ADD COLUMN network_id TEXT")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_exit_trigger_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            checked_minute TEXT NOT NULL,
            display_price REAL,
            preview_usdc REAL,
            allowed INTEGER NOT NULL,
            detail TEXT,
            checked_at TEXT NOT NULL,
            UNIQUE(position_id, reason, checked_minute)
        )""")
    # WHICH LOOP DECIDED (2026-08-29). The decision-latency map could name the p50 but not split
    # it by lane, because nothing recorded which code path evaluated the rail — and the mark's
    # `source` is not a substitute, since it says where the PRICE came from rather than who read
    # it. One string makes the wake-vs-sweep split measurable instead of inferable.
    audit_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(memecoin_exit_trigger_audit)")}
    if "lane" not in audit_cols:
        conn.execute("ALTER TABLE memecoin_exit_trigger_audit ADD COLUMN lane TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_exit_guard_pos "
                 "ON memecoin_exit_trigger_audit(position_id, checked_at)")
    # thesis capture: curated traders' posted reasoning (display/context ONLY). Stores raw
    # facts; `stance` is computed at read time so the classifier can evolve without migration.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_theses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fomo_thesis_id TEXT UNIQUE,
            comment_id TEXT,
            source_user_id TEXT, handle TEXT,
            mint TEXT, ticker TEXT,
            comment TEXT, has_link INTEGER,
            their_pnl_pct REAL, their_realized_pnl_pct REAL,
            their_notional_usd REAL, their_unrealized_pnl_usd REAL,
            market_cap REAL, fdv REAL, price REAL,
            tag TEXT, network_id TEXT, swap_id TEXT, trade_id TEXT,
            created_at TEXT, received_at TEXT, lag_s REAL,
            signal_source TEXT DEFAULT 'global_feed'
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_theses_user "
                 "ON memecoin_theses(source_user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_theses_mint "
                 "ON memecoin_theses(mint, received_at)")
    # telegram front-run latency instrument: one row per alerted curated large-buy. Stores OUR
    # side (detection lag + send latency); the fomo-push-arrival comparison is a manual phone read.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_alert_latency (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fomo_event_id TEXT UNIQUE,
            source_user_id TEXT, handle TEXT, mint TEXT, ticker TEXT,
            their_size_usd REAL, market_cap REAL,
            fomo_created_at TEXT, detected_at TEXT, lag_s REAL,
            alert_sent_at TEXT, send_ok INTEGER, send_ms REAL, local_ms REAL,
            signal_source TEXT DEFAULT 'global_feed'
        )""")
    al_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_alert_latency)")}
    if "local_ms" not in al_cols:   # detection->send-start gap (DB writes + thesis lookup + msg build)
        conn.execute("ALTER TABLE memecoin_alert_latency ADD COLUMN local_ms REAL")
    # chain provenance (2026-07-15): dedup is by fomo_event_id (1:1 with an event that has a chain),
    # so the alert row itself was chain-blind; add it. Nullable; backfilled unambiguous-only offline.
    if "network_id" not in al_cols:
        conn.execute("ALTER TABLE memecoin_alert_latency ADD COLUMN network_id TEXT")
    for column in ("rendered_text_redacted", "message_sha256"):
        if column not in al_cols:
            conn.execute(f"ALTER TABLE memecoin_alert_latency ADD COLUMN {column} TEXT")
    # Generic asynchronous Telegram delivery audit. The exact rendered text is represented by its
    # SHA-256; the readable copy is redacted before it reaches SQLite so signed one-tap URLs never
    # become credentials at rest.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_telegram_delivery_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_kind TEXT NOT NULL,
            source_id TEXT,
            queued_at TEXT NOT NULL,
            send_started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            send_ok INTEGER NOT NULL,
            send_ms REAL NOT NULL,
            rendered_text_redacted TEXT NOT NULL,
            message_sha256 TEXT NOT NULL,
            error_class TEXT,
            attempt INTEGER NOT NULL DEFAULT 1
        )""")
    tg_cols = {row[1] for row in conn.execute("PRAGMA table_info(memecoin_telegram_delivery_audit)")}
    if "attempt" not in tg_cols:
        conn.execute("ALTER TABLE memecoin_telegram_delivery_audit ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_tg_delivery_completed "
                 "ON memecoin_telegram_delivery_audit(completed_at, message_kind)")
    # Legacy clock recovery is additive: preserve the original stamp, classify its domain, and
    # store a separate normalized value only when the row is provable against its source time.
    for table, normalized_col in (
        ("memecoin_events", "received_at_normalized"),
        ("memecoin_firehose", "received_at_normalized"),
        ("memecoin_theses", "received_at_normalized"),
        ("memecoin_signal_outcomes", "buy_ts_normalized"),
    ):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in (
            (normalized_col, "TEXT"),
            ("capture_version", "INTEGER NOT NULL DEFAULT 1"),
            ("clock_domain", "TEXT NOT NULL DEFAULT 'legacy_unknown'"),
        ):
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # Adaptive-exit observation + replay foundation (SP#1 task 1): additive, no reads/writes
    # wired yet — these back the future episode/snapshot/mark/policy/arm-outcome pipeline.
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_token_episodes(
        episode_id TEXT PRIMARY KEY, chain_id TEXT, token_address TEXT, started_ts TEXT,
        ended_ts TEXT, first_signal_id TEXT, consensus_cluster_count INTEGER DEFAULT 1,
        status TEXT DEFAULT 'open', last_signal_ts TEXT, first_source_price REAL,
        first_executable_price REAL)""")
    episode_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_token_episodes)")}
    for column, ddl in (
        ("last_signal_ts", "TEXT"),
        ("first_source_price", "REAL"),
        ("first_executable_price", "REAL"),
    ):
        if column not in episode_cols:
            conn.execute(f"ALTER TABLE memecoin_token_episodes ADD COLUMN {column} {ddl}")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_episode_open
        ON memecoin_token_episodes(chain_id, token_address) WHERE status='open'""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_episode_signals(
        episode_id TEXT NOT NULL, upstream_event_id TEXT NOT NULL UNIQUE,
        observed_at TEXT NOT NULL, source_price REAL, executable_price REAL,
        PRIMARY KEY (episode_id, upstream_event_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_episode_signals_episode "
                 "ON memecoin_episode_signals(episode_id, observed_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_entry_snapshots(
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, upstream_event_id TEXT UNIQUE NOT NULL,
        episode_id TEXT, chain TEXT, mint TEXT, action TEXT, decided_at TEXT,
        mcap REAL, liquidity REAL, token_age_s REAL, consensus_n_60m INTEGER,
        wallet_trust_tier TEXT, their_size_usd REAL, regime_id INTEGER,
        features_json TEXT, feature_version INTEGER DEFAULT 1,
        data_quality_json TEXT, entry_latency_json TEXT, snapshot_hash TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_radar_ratings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id TEXT NOT NULL UNIQUE,
        upstream_event_id TEXT,
        snapshot_id INTEGER,
        episode_id TEXT,
        network_id TEXT,
        mint TEXT,
        signal_source TEXT,
        rating_version INTEGER NOT NULL,
        rating TEXT NOT NULL,
        reason_codes_json TEXT NOT NULL,
        facts_json TEXT NOT NULL,
        facts_hash TEXT NOT NULL,
        decided_at TEXT,
        created_at TEXT NOT NULL,
        clock_domain TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_radar_coin_time "
                 "ON memecoin_radar_ratings(network_id, mint, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_radar_event "
                 "ON memecoin_radar_ratings(upstream_event_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_shadow_signals(
        shadow_signal_id TEXT PRIMARY KEY,
        episode_id TEXT NOT NULL UNIQUE,
        origin_event_id TEXT NOT NULL,
        network_id TEXT NOT NULL,
        mint TEXT NOT NULL,
        source_user_id TEXT,
        source_handle TEXT,
        signal_source TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        claimed_source_lag_s REAL,
        source_price REAL,
        quote_observation_id INTEGER,
        quote_price REAL,
        quote_observed_at TEXT,
        quote_age_s REAL,
        market_cap REAL,
        liquidity REAL,
        consensus_at_signal INTEGER NOT NULL,
        wallet_ab_pnl REAL,
        wallet_ab_closes INTEGER,
        rating TEXT NOT NULL CHECK(rating IN ('candidate','watch','reject')),
        reason_codes_json TEXT NOT NULL,
        facts_json TEXT NOT NULL,
        facts_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        clock_domain TEXT NOT NULL,
        UNIQUE(network_id, mint, episode_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_shadow_signal_time "
                 "ON memecoin_shadow_signals(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_shadow_signal_rating "
                 "ON memecoin_shadow_signals(rating, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_shadow_signal_coin "
                 "ON memecoin_shadow_signals(network_id, mint, observed_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_position_marks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT, mint TEXT, sweep_ts TEXT,
        mid REAL, executable_exit_value REAL, liquidity REAL, mcap REAL,
        route_quote_id TEXT, source TEXT, quality TEXT)""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_marks_uniq
        ON memecoin_position_marks(chain_id, mint, sweep_ts)""")
    # OTHER HOLDINGS (2026-09-13, operator: "an other holdings section ... sell anything we can").
    # Tokens in the funded wallet that no open position or reservation owns: route leftovers and
    # airdrops. The runner rewrites the snapshot; sells are intents the runner executes under the
    # wallet lock, the same shape as the manual exit intents.
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_other_holdings(
        network_id TEXT NOT NULL, mint TEXT NOT NULL, symbol TEXT, amount REAL, amount_raw TEXT,
        decimals INTEGER, price_usd REAL, value_usd REAL, liquidity_usd REAL, observed_at TEXT,
        PRIMARY KEY (network_id, mint))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_other_holdings_meta(
        network_id TEXT PRIMARY KEY, observed_at TEXT, unpriced_count INTEGER, total_usd REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_holding_sell_intents(
        intent_id TEXT PRIMARY KEY, network_id TEXT NOT NULL, mint TEXT NOT NULL,
        requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
        claimed_at TEXT, resolved_at TEXT, reason TEXT, sig TEXT, usd_received REAL)""")
    # Funded nonce fencing must never scan the terminal holding-sell history on entry.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_holding_sell_unresolved "
                 "ON memecoin_holding_sell_intents(network_id) "
                 "WHERE state IN ('submitted','approval_submitted')")
    # OPERATOR WITHDRAWALS (2026-09-13). The dashboard queues an amount; the runner sends USDC to
    # the ONE configured address (MEMECOIN_WITHDRAW_SOLANA_ADDRESS) under the wallet lock.
    # QUOTE-ASSET RESIDUE (2026-09-28, design docs/superpowers/specs/2026-09-27-slot-lane-any-venue-
    # design.md section 8.1). An ExactIn quote leg leaves up to its slippage in the quote asset; one
    # append-only row per such fill. WRITER: memecoin_quote_residue.record_fill_residue, called only
    # after a filled LIVE venue buy spending a quote other than SOL/USDC (none today: every venue's
    # live list is empty). READER: memecoin_quote_residue.run_sweep_once, which enqueues the existing
    # holding sell (memecoin_holding_sell_intents) and marks the rows swept_by that intent.
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_quote_residue(
        id INTEGER PRIMARY KEY AUTOINCREMENT, network_id TEXT NOT NULL, quote_mint TEXT NOT NULL,
        residue_atomic TEXT NOT NULL, residue_usd REAL, venue TEXT, attempt_id TEXT, entry_sig TEXT,
        recorded_at TEXT NOT NULL, swept_by TEXT, swept_at TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memecoin_quote_residue_mint "
                 "ON memecoin_quote_residue(network_id, quote_mint)")
    # RESIDUE CREDITS (operator order 2026-10-07). Exit rows (kind='exit') come from the exit's own
    # receipt, read off the exit path by memecoin_quote_residue.read_exit_residue_once, and carry
    # their position directly; entry rows (kind NULL = 'entry') bind by attempt_id. source_at is the
    # fill's time, which orders a row against the holding sell that consumed it.
    _qr_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_quote_residue)")}
    for _col, _ddl in (("position_id", "INTEGER"), ("kind", "TEXT"), ("source_sig", "TEXT"),
                       ("source_at", "TEXT")):
        if _col not in _qr_cols:
            conn.execute(f"ALTER TABLE memecoin_quote_residue ADD COLUMN {_col} {_ddl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memecoin_quote_residue_source "
                 "ON memecoin_quote_residue(source_sig, quote_mint) WHERE source_sig IS NOT NULL")
    # One row per exit receipt read: 'read' (rows written, possibly none = a known zero), 'pending'
    # (unreadable so far; retried) or 'unreadable' (gave up: unknown, never zero).
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_quote_residue_reads(
        sig TEXT PRIMARY KEY, position_id INTEGER, fill_kind TEXT, state TEXT NOT NULL,
        tries INTEGER NOT NULL DEFAULT 0, negative_mints TEXT, read_at TEXT)""")
    # ORDER-PROOF DDL (Alpha 2026-10-07): 39c3fd840 deployed before fea99acd1 created this table with a
    # COLUMN UNIQUE on residue_id, which a second revert of a re-credited residue violates. Such a
    # table is swapped for the live-only UNIQUE in this same transaction: renamed aside, the new one
    # created, every row copied with its id, the old one dropped, then the indexes built (after the
    # drop, so IF NOT EXISTS cannot find the old table's index and skip the new one's).
    _old_credits = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                                "AND name='memecoin_quote_residue_credits'").fetchone()
    _swap_credits = bool(_old_credits and "residue_id INTEGER NOT NULL UNIQUE" in (_old_credits[0] or ""))
    if _swap_credits:
        conn.execute("ALTER TABLE memecoin_quote_residue_credits RENAME TO memecoin_quote_residue_credits_v1")
    # APPEND-ONLY. One row per residue row a holding sell consumed; realized_pnl moved by `usd` in the
    # same transaction (before/after kept). A revert subtracts it and stamps reverted_at. Amounts are
    # TEXT ints (a quote residue can exceed int64).
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_quote_residue_credits(
        id INTEGER PRIMARY KEY AUTOINCREMENT, residue_id INTEGER NOT NULL,
        position_id INTEGER NOT NULL, intent_id TEXT NOT NULL, quote_mint TEXT NOT NULL,
        residue_atomic TEXT NOT NULL, consumed_atomic TEXT NOT NULL, foreign_atomic TEXT NOT NULL,
        foreign_usd REAL, usd REAL NOT NULL, short INTEGER NOT NULL DEFAULT 0,
        realized_before REAL, realized_after REAL, credited_at TEXT NOT NULL,
        reverted_at TEXT, revert_reason TEXT)""")
    if _swap_credits:
        _cols = ("id, residue_id, position_id, intent_id, quote_mint, residue_atomic, consumed_atomic, "
                 "foreign_atomic, foreign_usd, usd, short, realized_before, realized_after, credited_at, "
                 "reverted_at, revert_reason")
        conn.execute(f"INSERT INTO memecoin_quote_residue_credits({_cols}) "
                     f"SELECT {_cols} FROM memecoin_quote_residue_credits_v1")
        conn.execute("DROP TABLE memecoin_quote_residue_credits_v1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memecoin_quote_residue_credits_position "
                 "ON memecoin_quote_residue_credits(position_id)")
    # One LIVE credit per residue row; reverted rows stay as history (a residue may be credited,
    # reverted and credited again any number of times).
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memecoin_quote_residue_credits_live "
                 "ON memecoin_quote_residue_credits(residue_id) WHERE reverted_at IS NULL")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_withdraw_intents(
        intent_id TEXT PRIMARY KEY, network_id TEXT NOT NULL, to_address TEXT NOT NULL,
        amount_usdc REAL NOT NULL, requested_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', claimed_at TEXT, resolved_at TEXT, reason TEXT,
        sig TEXT, last_valid_height INTEGER)""")
    # The two facts that prove a submitted sell can never land (see sig_provably_dead): the
    # signature's last valid block height, and the raw balance held when it was signed.
    _hs_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_holding_sell_intents)")}
    for _col, _ddl in (("last_valid_height", "INTEGER"), ("balance_raw", "TEXT"),
                       ("allow_small", "INTEGER NOT NULL DEFAULT 0"),
                       ("signed_payload_b64", "TEXT"),
                       ("transaction_phase", "TEXT NOT NULL DEFAULT 'swap'")):
        if _col not in _hs_cols:
            conn.execute(f"ALTER TABLE memecoin_holding_sell_intents ADD COLUMN {_col} {_ddl}")
    # LIVE ACTIVITY ON THE MARK (2026-09-13, operator: "can the position card show live volume").
    # Token-level 5-minute and 1-hour volume and 5-minute trade count, from the same stats read
    # that priced the mark. Additive and nullable: price-only writers leave them NULL.
    _mark_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_position_marks)")}
    # PAIR SELECTION SHADOW (2026-09-17): the stats read's `pair_selection` record, JSON, on marks
    # priced by the DexScreener selector. NULL for every other source.
    for _col, _ddl in (("volume_m5", "REAL"), ("volume_h1", "REAL"), ("txns_m5", "INTEGER"),
                       ("pair_selection_json", "TEXT")):
        if _col not in _mark_cols:
            conn.execute(f"ALTER TABLE memecoin_position_marks ADD COLUMN {_col} {_ddl}")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_policy_versions(
        policy_id TEXT, version INTEGER, template TEXT, parameters_json TEXT,
        code_commit TEXT, immutable_hash TEXT, activated_ts TEXT, retired_ts TEXT,
        PRIMARY KEY (policy_id, version))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_policy_assignments(
        episode_id TEXT, arm_id TEXT, policy_id TEXT, policy_version INTEGER DEFAULT 1,
        entry_snapshot_id INTEGER,
        assignment_reason TEXT, assigned_ts TEXT, PRIMARY KEY (episode_id, arm_id))""")
    assignment_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_policy_assignments)")}
    if "policy_version" not in assignment_cols:
        conn.execute("ALTER TABLE memecoin_policy_assignments "
                     "ADD COLUMN policy_version INTEGER DEFAULT 1")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_arm_outcomes(
        episode_id TEXT, arm_id TEXT, entry_cost REAL, net_exit_value REAL, net_pnl REAL,
        net_return REAL, max_executable_return REAL, max_drawdown REAL, exit_reason TEXT,
        route_failures INTEGER DEFAULT 0, closed_ts TEXT, PRIMARY KEY (episode_id, arm_id))""")
    # Per-position exit-control layer: operator manual overrides + system templates. One active row
    # per (position_id, source); effective exit = operator > template > default (see
    # effective_exit_override). TP/SL as % OR target mcap; the sweep folds via apply_overrides.
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_position_exit_overrides(
        id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL, source TEXT NOT NULL,
        tp_pct REAL, tp_target_mcap REAL, sl_pct REAL, sl_target_mcap REAL, trail_pct REAL,
        derisk_pct REAL, actor TEXT, note TEXT, active INTEGER DEFAULT 1, created_at TEXT,
        resolved_at TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_exit_override_active "
                 "ON memecoin_position_exit_overrides(position_id, active)")
    # idempotent migration (2026-07-16): per-position derisk threshold for the TP-vs-derisk-ride
    # A/B — an override arm needs its OWN derisk trigger, not the global cfg one.
    _ov_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_position_exit_overrides)")}
    if "derisk_pct" not in _ov_cols:
        conn.execute("ALTER TABLE memecoin_position_exit_overrides ADD COLUMN derisk_pct REAL")
    # RISK-live apply acks (2026-10-22, memecoin_desk_risk_apply): one row per watcher decision the
    # funded sweep acted on or refused. The watcher's own DB is read-only here; this is the runner's.
    # (source, decision_id) PK (2026-10-22): risk_live decision ids and attention event ids are separate
    # namespaces; decision_id is untyped so int ids stay ints (an older release's reads still match on rollback).
    # namespaces. A v1 table (decision_id PK) is migrated in place; its rows become source 'risk_live'.
    _ra_cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_desk_risk_applies)")}
    if _ra_cols and "source" not in _ra_cols:
        conn.execute("ALTER TABLE memecoin_desk_risk_applies RENAME TO memecoin_desk_risk_applies_v1")
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_desk_risk_applies(
        source TEXT NOT NULL DEFAULT 'risk_live', decision_id NOT NULL, position_id INTEGER NOT NULL,
        action TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, decided_at REAL, applied_at TEXT NOT NULL,
        lag_s REAL, rails_before_json TEXT, rails_after_json TEXT, call_id INTEGER, inputs_hash TEXT,
        detail_json TEXT, PRIMARY KEY (source, decision_id))""")
    if _ra_cols and "source" not in _ra_cols:
        conn.execute("INSERT OR IGNORE INTO memecoin_desk_risk_applies(source, decision_id, position_id, action, "
                     "status, reason, decided_at, applied_at, lag_s, rails_before_json, rails_after_json, call_id, "
                     "inputs_hash) SELECT 'risk_live', decision_id, position_id, action, status, reason, "
                     "decided_at, applied_at, lag_s, rails_before_json, rails_after_json, call_id, inputs_hash "
                     "FROM memecoin_desk_risk_applies_v1")
        conn.execute("DROP TABLE memecoin_desk_risk_applies_v1")

    # one-tap single-use ledger (2026-07-23 review P1): registered here in the bootstrap path
    # rather than created lazily on the first buy_token request.
    conn.execute("CREATE TABLE IF NOT EXISTS memecoin_action_tokens("
                 " token_id TEXT PRIMARY KEY, mint TEXT, used_at TEXT NOT NULL)")

    # Shared fast-submission durable binding (2026-07-25). Before the first network write the
    # entry reservation persists the exact signed payload + hash + validity + selected backend, so a
    # crash after broadcast stays reconcilable and (while the blockhash is valid) the SAME bytes may
    # be rebroadcast unchanged — never re-signed. Exits keep ExitIntent as their binding.
    for _col, _ddl in (
        ("signed_payload_b64", "TEXT"),
        ("signed_payload_sha256", "TEXT"),
        ("submission_blockhash", "TEXT"),
        ("submission_last_valid_height", "INTEGER"),
        ("submission_backend", "TEXT"),
        ("submission_bound_at", "TEXT"),
    ):
        if _col not in pe_cols:
            conn.execute(f"ALTER TABLE memecoin_pending_entries ADD COLUMN {_col} {_ddl}")
            pe_cols.add(_col)
    # Append-only provider-attempt audit for BOTH entries and exits — hashes only, never the payload.
    # Keyed to the owning reservation / claim; execution authority stays with the reservation/claim/
    # intent state, this is observability + forensic evidence of who accepted/rejected/timed-out.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_submission_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id TEXT NOT NULL,
            action TEXT NOT NULL,            -- entry | entry_approval | approval | pre_tx | exit
                                             --   | partial_exit  (entry_approval is reservation-
                                             --   keyed; bare 'approval' is the unaudited
                                             --   record=False fallback for non-entry approvals)
            owner_kind TEXT,                 -- reservation | close_claim | derisk_claim | approval
            owner_id TEXT,                   -- pending_id / claim_id (string)
            tx_identity TEXT,
            signed_payload_sha256 TEXT,      -- hash ONLY; the payload never lands here
            provider_id TEXT NOT NULL,
            state TEXT NOT NULL,             -- accepted | explicit_rejected | ambiguous | not_sent
            latency_ms REAL,
            response_code TEXT,
            reason_code TEXT,
            created_at TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_submit_attempts_owner "
                 "ON memecoin_submission_attempts(owner_kind, owner_id, created_at)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_lifi_authority_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lifecycle_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            pending_id INTEGER NOT NULL,
            position_id INTEGER,
            expected_prior_state TEXT,
            state TEXT NOT NULL,
            event_type TEXT NOT NULL,
            stage TEXT,
            chain_id TEXT NOT NULL,
            custody_wallet TEXT NOT NULL,
            token TEXT,
            spender TEXT,
            amount_atomic TEXT,
            input_atomic TEXT,
            output_min_atomic TEXT,
            fee_atomic TEXT,
            allowance_atomic TEXT,
            settlement_in_atomic TEXT,
            settlement_out_atomic TEXT,
            tx_hash TEXT,
            tx_identity TEXT,
            nonce TEXT,
            signed_payload_sha256 TEXT,
            block_number TEXT,
            block_hash TEXT,
            proof_sha256 TEXT,
            request_sha256 TEXT,
            response_sha256 TEXT,
            calldata_sha256 TEXT,
            decoded_semantics_sha256 TEXT,
            release_sha TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            tool TEXT,
            provider_failure_type TEXT,
            quota_limit TEXT,
            quota_remaining TEXT,
            quota_reset TEXT,
            detail_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(lifecycle_id, sequence)
        )""")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_lifi_pending_owner "
        "ON memecoin_lifi_authority_events(pending_id) WHERE sequence=0"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_lifi_position_link "
        "ON memecoin_lifi_authority_events(position_id) WHERE position_id IS NOT NULL"
    )
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mc_lifi_sequence_zero
        BEFORE INSERT ON memecoin_lifi_authority_events
        WHEN NEW.sequence=0 AND NEW.expected_prior_state IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'LI.FI sequence zero has a prior state');
        END""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mc_lifi_transition
        BEFORE INSERT ON memecoin_lifi_authority_events
        WHEN NEW.sequence>0 AND NOT EXISTS (
            SELECT 1 FROM memecoin_lifi_authority_events prev
            WHERE prev.lifecycle_id=NEW.lifecycle_id
              AND prev.sequence=NEW.sequence-1
              AND prev.state=NEW.expected_prior_state
              AND prev.pending_id=NEW.pending_id
              AND prev.chain_id=NEW.chain_id
              AND prev.custody_wallet=NEW.custody_wallet
              AND prev.release_sha=NEW.release_sha
              AND prev.manifest_hash=NEW.manifest_hash
        )
        BEGIN
            SELECT RAISE(ABORT, 'LI.FI transition does not match the prior event');
        END""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mc_lifi_no_update
        BEFORE UPDATE ON memecoin_lifi_authority_events
        BEGIN
            SELECT RAISE(ABORT, 'LI.FI authority events are append-only');
        END""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mc_lifi_no_delete
        BEFORE DELETE ON memecoin_lifi_authority_events
        BEGIN
            SELECT RAISE(ABORT, 'LI.FI authority events cannot be deleted');
        END""")
    activation_table_sql = """
        CREATE TABLE IF NOT EXISTS memecoin_lifi_activations (
            release_sha TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            chain_id TEXT NOT NULL,
            custody_wallet TEXT NOT NULL,
            canary_lifecycle_id TEXT NOT NULL,
            allowance_proof_sha256 TEXT NOT NULL,
            quota_limit TEXT NOT NULL,
            quota_remaining TEXT NOT NULL,
            quota_reset TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            PRIMARY KEY(release_sha, manifest_hash, chain_id, custody_wallet)
        )"""
    conn.execute(activation_table_sql)
    for index in conn.execute("PRAGMA index_list(memecoin_lifi_activations)").fetchall():
        columns = [row[0] for row in conn.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],),
        ).fetchall()]
        if index[2] and columns == ["canary_lifecycle_id"]:
            conn.execute(
                "ALTER TABLE memecoin_lifi_activations "
                "RENAME TO memecoin_lifi_activations_unique_canary"
            )
            conn.execute(activation_table_sql)
            conn.execute(
                "INSERT INTO memecoin_lifi_activations SELECT * "
                "FROM memecoin_lifi_activations_unique_canary"
            )
            conn.execute("DROP TABLE memecoin_lifi_activations_unique_canary")
            break
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mc_lifi_activation_no_update
        BEFORE UPDATE ON memecoin_lifi_activations
        BEGIN
            SELECT RAISE(ABORT, 'LI.FI activation is immutable');
        END""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mc_lifi_activation_no_delete
        BEFORE DELETE ON memecoin_lifi_activations
        BEGIN
            SELECT RAISE(ABORT, 'LI.FI activation cannot be deleted');
        END""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_mc_lifi_dependent_immutable
        BEFORE UPDATE OF lifi_dependent ON memecoin_positions
        WHEN NEW.lifi_dependent <> OLD.lifi_dependent
        BEGIN
            SELECT RAISE(ABORT, 'lifi_dependent is immutable');
        END""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_evm_slot_proofs (
            network_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            spender TEXT NOT NULL,
            balance_slot TEXT NOT NULL,
            allowance_slot TEXT NOT NULL,
            PRIMARY KEY(network_id, mint, spender)
        )""")
    # One row per token pre-approval attempt after a V4 entry (spec 2026-09-30 4663 approve at
    # entry). amount_atomic is TEXT: token atoms exceed 2**63. receipt_status NULL = never resolved
    # (the thread died); 'unknown' = no receipt inside the bounded wait.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_evm_preapprovals (
            id INTEGER PRIMARY KEY,
            position_id INTEGER NOT NULL,
            network_id TEXT NOT NULL,
            token TEXT,
            spender TEXT,
            amount_atomic TEXT,
            nonce INTEGER,
            tx_hash TEXT,
            send_status TEXT NOT NULL,
            receipt_status TEXT,
            gas_used INTEGER,
            effective_gas_price_wei INTEGER,
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )""")

    conn.commit()


def ensure_memecoin_tables(conn: sqlite3.Connection) -> None:
    """Apply schema changes atomically; refuse to touch a caller-owned transaction."""
    if conn.in_transaction:
        raise RuntimeError(
            "ensure_memecoin_tables must be invoked outside an active transaction; "
            "commit or roll back the caller transaction first"
        )
    # Reserve the writer slot before migration reads. A deferred BEGIN can read a
    # snapshot, lose the writer race, then fail immediately with BUSY_SNAPSHOT
    # when a later migration statement writes — despite busy_timeout.
    conn.execute("BEGIN IMMEDIATE")
    try:
        _ensure_memecoin_tables_in_transaction(conn)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def record_cu_demotion(conn, *, class_key, entry_key, sig, signed_limit, reason, demoted_at):
    """Persist one slot-lane CU class demotion; a repeat for the same entry is ignored."""
    conn.execute("INSERT OR IGNORE INTO memecoin_slot_cu_demotions (class_key, entry_key, sig, signed_limit, "
                 "reason, demoted_at) VALUES (?,?,?,?,?,?)",
                 (class_key, entry_key, sig, signed_limit, reason, demoted_at))


def cu_demotion_rows(conn):
    """{(class_key, entry_key)} of every persisted slot-lane CU demotion."""
    return {(r[0], r[1]) for r in conn.execute("SELECT class_key, entry_key FROM memecoin_slot_cu_demotions")}


def get_evm_v4_pool_key(conn, *, chain_id, pool_id, pool_manager):
    """Return one validated immutable V4 key, or None for a cache miss."""
    row = conn.execute(
        "SELECT key_json FROM memecoin_evm_v4_pool_keys "
        "WHERE network_id=? AND pool_id=? AND pool_manager=?",
        (str(chain_id), str(pool_id).lower(), str(pool_manager).lower()),
    ).fetchone()
    if row is None:
        return None
    try:
        key = _json.loads(row[0])
        fields = ("currency0", "currency1", "fee", "tick_spacing", "hooks")
        if not isinstance(key, dict) or set(key) != set(fields):
            return None
        if any(not isinstance(key[field], str) for field in ("currency0", "currency1", "hooks")):
            return None
        for field in ("currency0", "currency1", "hooks"):
            value = key[field]
            if len(value) != 42 or not value.startswith("0x"):
                return None
            int(value[2:], 16)
        int(key["fee"])
        int(key["tick_spacing"])
    except (TypeError, ValueError, _json.JSONDecodeError):
        return None
    return key


def put_evm_v4_pool_key(conn, *, chain_id, pool_id, pool_manager, key, now_iso=None):
    """Persist a PoolManager-verified immutable V4 key."""
    conn.execute(
        "INSERT OR REPLACE INTO memecoin_evm_v4_pool_keys "
        "(network_id, pool_id, pool_manager, key_json, verified_at) VALUES (?,?,?,?,?)",
        (str(chain_id), str(pool_id).lower(), str(pool_manager).lower(),
         _json.dumps(key, sort_keys=True, separators=(",", ":")), now_iso or real_utc_now_iso()),
    )
    conn.commit()


def _commit_trace_write(conn, write_fn):
    """Commit one trace-owned write without touching a caller-owned transaction."""
    caller_owned_transaction = bool(getattr(conn, "in_transaction", False))
    if caller_owned_transaction:
        raise RuntimeError("entry trace write refused inside caller-owned transaction")
    try:
        result = write_fn()
        conn.commit()
        return result
    except Exception:
        if not caller_owned_transaction and getattr(conn, "in_transaction", False):
            conn.rollback()
        raise


def start_entry_attempt(conn, *, attempt_id, decision_started_at, origin_event_id=None,
                        signal_source=None, network_id=None, mint=None, source_user_id=None,
                        source_handle=None, ticker=None, side=None, source_created_at=None,
                        source_timestamp_trust="claimed_untrusted", source_price=None,
                        source_market_cap=None, observed_at=None,
                        clock_domain="legacy_unknown", delivery_lane=None, trace_json=None):
    """Commit an unfinished real-entry trace before any routing or funded side effect."""
    def write():
        conn.execute(
            "INSERT INTO memecoin_entry_attempts("
            "attempt_id, origin_event_id, signal_source, network_id, mint, source_user_id, "
            "source_handle, ticker, side, source_created_at, source_timestamp_trust, source_price, "
            "source_market_cap, observed_at, clock_domain, delivery_lane, decision_started_at, trace_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, origin_event_id, signal_source, network_id, mint, source_user_id,
             source_handle, ticker, side, source_created_at, source_timestamp_trust, source_price,
             source_market_cap, observed_at, clock_domain, delivery_lane, decision_started_at, trace_json),
        )

    _commit_trace_write(conn, write)
    return attempt_id


def set_entry_attempt_cluster_decision(
        conn, attempt_id, *, decision_id, reason, metrics_json, completed_at,
        duration_ms, commit=True):
    """Store one immutable cluster decision on an unfinished entry attempt."""
    row = conn.execute(
        "SELECT cluster_decision_id,cluster_reason,cluster_metrics_json,"
        "cluster_completed_at,cluster_ms FROM memecoin_entry_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown entry attempt")
    if row[0] not in (None, decision_id):
        raise ValueError("cluster decision is immutable")
    if row[0] == decision_id:
        existing = tuple(row[1:])
        incoming = (reason, metrics_json, completed_at, duration_ms)
        if existing != incoming:
            raise ValueError("cluster decision is immutable")
        return False
    cur = conn.execute(
        "UPDATE memecoin_entry_attempts SET cluster_decision_id=?,cluster_reason=?,"
        "cluster_metrics_json=?,cluster_completed_at=?,cluster_ms=? "
        "WHERE attempt_id=? AND terminal_status IS NULL AND cluster_decision_id IS NULL",
        (
            decision_id,
            reason,
            metrics_json,
            completed_at,
            duration_ms,
            attempt_id,
        ),
    )
    if commit:
        conn.commit()
    return cur.rowcount == 1


def set_entry_attempt_cluster_outcome(
        conn, attempt_id, *, outcome_id, status, commit=True):
    """Store one blocked-outcome link without replacing a different link."""
    row = conn.execute(
        "SELECT cluster_outcome_id FROM memecoin_entry_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown entry attempt")
    if row[0] not in (None, outcome_id):
        raise ValueError("cluster outcome is immutable")
    cur = conn.execute(
        "UPDATE memecoin_entry_attempts SET cluster_outcome_id=?,cluster_outcome_status=? "
        "WHERE attempt_id=? AND (cluster_outcome_id IS NULL OR cluster_outcome_id=?)",
        (outcome_id, status, attempt_id, outcome_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount == 1


def entry_attempt_source_handle(conn, attempt_id):
    """The source handle an entry attempt recorded, or None."""
    row = conn.execute("SELECT source_handle FROM memecoin_entry_attempts WHERE attempt_id=?",
                       (attempt_id,)).fetchone()
    handle = row[0] if row else None
    return handle if isinstance(handle, str) and handle.strip() else None


def entry_attempt_source_evidence(conn, attempt_id):
    """Frozen source evidence for one attempt; unavailable or malformed evidence stays unknown."""
    row = conn.execute("SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                       (attempt_id,)).fetchone()
    try:
        trace = _json.loads(row[0]) if row and row[0] else {}
        evidence = trace.get('source_evidence') if isinstance(trace, dict) else None
        return evidence if isinstance(evidence, dict) else {}
    except (TypeError, ValueError):
        return {}


def link_entry_attempt_position(conn, attempt_id, position_id):
    """Name the position a finished attempt turned out to open (a confirmed-entry recovery after an
    anomaly). Never re-links and never touches the terminal status. No commit: the caller's."""
    return conn.execute("UPDATE memecoin_entry_attempts SET position_id=? WHERE attempt_id=? "
                        "AND position_id IS NULL", (int(position_id), attempt_id)).rowcount == 1


_ENTRY_ATTEMPT_FINISH_FIELDS = {
    "network_id", "quote_price", "quote_observed_at", "quote_age_ms", "quote_id",
    "quote_route_target", "price_attribution",
    "fill_price", "source_drift_pct", "execution_slippage_pct", "total_copy_drift_pct",
    "pending_id", "position_id", "fill_id", "tx_sig", "total_ms",
    "gate_inputs_json",
}

_ENTRY_ATTEMPT_STAGE_COLUMNS = {
    "chain_resolution": ("chain_resolution_completed_at", "chain_resolution_ms"),
    "market_data": ("market_data_completed_at", "market_data_ms"),
    "cluster": ("cluster_completed_at", "cluster_ms"),
    "reservation": ("reservation_completed_at", "reservation_ms"),
    "safety_checks": ("safety_checks_completed_at", "safety_checks_ms"),
    "quote": ("quote_completed_at", "quote_ms"),
    "signing": ("signing_completed_at", "signing_ms"),
    "transaction_submitted": ("transaction_submitted_at", "transaction_submitted_ms"),
    "chain_confirmation": ("chain_confirmation_completed_at", "chain_confirmation_ms"),
    "balance_delta": ("balance_delta_completed_at", "balance_delta_ms"),
    "persistence": ("persistence_completed_at", "persistence_ms"),
}


def _require_nonblank_terminal_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")


def close_interrupted_entry_attempts(conn, *, started_at, now_iso, lookback_s=3600):
    """Give a terminal status to entry attempts that a restart killed before any reservation.

    COINU (2026-09-17 08:04:49): a deploy stopped the runner 0.46 s into the decision, the attempt
    never finished, and its Telegram card stayed at EVALUATING forever. Only attempts that started
    before this process, inside `lookback_s`, have no terminal status, and own NO reservation are
    closed; a reservation has its own recovery and is never touched here. Timestamps are parsed,
    not string-compared, because stored ISO strings and SQLite's datetime() use different separators.
    """
    from datetime import datetime, timedelta, timezone
    def _dt(value):
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Legacy timestamps without an offset use UTC.
        return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at
    start = _dt(started_at)
    floor = start - timedelta(seconds=float(lookback_s))
    rows = conn.execute(
        "SELECT a.attempt_id, a.decision_started_at FROM memecoin_entry_attempts a "
        "WHERE a.terminal_status IS NULL AND a.completed_at IS NULL AND a.pending_id IS NULL "
        "AND a.decision_started_at IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM memecoin_pending_entries p WHERE p.attempt_id=a.attempt_id)"
    ).fetchall()
    closed = 0
    for attempt_id, decided in rows:
        try:
            at = _dt(decided)
        except (TypeError, ValueError):
            continue
        if floor <= at < start:
            closed += conn.execute(
                "UPDATE memecoin_entry_attempts SET terminal_status='aborted', "
                "terminal_reason=?, completed_at=? WHERE attempt_id=? AND terminal_status IS NULL",
                ("interrupted: the runner restarted before a decision", str(now_iso), attempt_id),
            ).rowcount
    conn.commit()
    return closed


def finish_entry_attempt(conn, attempt_id, terminal_status, terminal_reason, completed_at,
                         trace_json, *, stages=None, **fields):
    """Atomically persist reached stages and finalize once."""
    _require_nonblank_terminal_text(terminal_status, "terminal_status")
    _require_nonblank_terminal_text(terminal_reason, "terminal_reason")
    _require_nonblank_terminal_text(completed_at, "completed_at")
    unknown = set(fields) - _ENTRY_ATTEMPT_FINISH_FIELDS
    if unknown:
        raise ValueError(f"unsupported entry-attempt fields: {sorted(unknown)}")
    stage_values = stages or {}
    unknown_stages = set(stage_values) - _ENTRY_ATTEMPT_STAGE_COLUMNS.keys()
    if unknown_stages:
        raise ValueError(f"unsupported entry-attempt stages: {sorted(unknown_stages)}")
    assignments = ["terminal_status=?", "terminal_reason=?", "completed_at=?", "trace_json=?"]
    values = [terminal_status, terminal_reason, completed_at, trace_json]
    for stage, stage_data in stage_values.items():
        # `measured` is REQUIRED, not optional: it says whether `duration_ms` is this stage's own
        # work or the gap since the previous mark, and only one of the ten funded-entry stages
        # measures. A dict that omits it would silently reintroduce the ambiguity the flag exists
        # to remove, so it is rejected at the boundary rather than defaulted.
        if (not isinstance(stage_data, dict)
                or set(stage_data) != {"completed_at", "duration_ms", "measured"}):
            raise ValueError(f"invalid entry-attempt stage data: {stage}")
        if not isinstance(stage_data["measured"], bool):
            raise ValueError(f"entry-attempt stage `measured` must be a bool: {stage}")
        timestamp_column, duration_column = _ENTRY_ATTEMPT_STAGE_COLUMNS[stage]
        assignments.extend((f"{timestamp_column}=?", f"{duration_column}=?"))
        values.extend((stage_data["completed_at"], stage_data["duration_ms"]))
    for column, value in fields.items():
        assignments.append(f"{column}=?")
        values.append(value)
    values.append(attempt_id)
    def write():
        return conn.execute(
            f"UPDATE memecoin_entry_attempts SET {', '.join(assignments)} "
            "WHERE attempt_id=? AND terminal_status IS NULL",
            values,
        )

    cur = _commit_trace_write(conn, write)
    return cur.rowcount == 1


def slot_lane_traces_since(conn, since_iso):
    """trace_json of every SENT slot-lane attempt decided at or after `since_iso` (lane start only:
    memecoin_crowded_fee re-seeds today's spend from it)."""
    return [row[0] for row in conn.execute(
        "SELECT trace_json FROM memecoin_entry_attempts WHERE signal_source='slot_lane' "
        "AND tx_sig IS NOT NULL AND julianday(decision_started_at) >= julianday(?)", (str(since_iso),))]


def bsc_fast_lane_traces_since(conn, since_iso):
    """trace_json of every SENT 56 fast-lane attempt decided at or after `since_iso` (lane start only:
    memecoin_bsc_crowded_gas re-seeds today's spend from it)."""
    return [row[0] for row in conn.execute(
        "SELECT trace_json FROM memecoin_entry_attempts WHERE signal_source='bsc_fast_lane' "
        "AND network_id='56' AND tx_sig IS NOT NULL AND julianday(decision_started_at) >= julianday(?)",
        (str(since_iso),))]


def entry_attempt_for_origin(conn, origin_event_id):
    """Latest immutable real-entry attempt for one exact upstream event.

    This lookup never falls back to wallet, mint, or chain. Those dimensions can identify a
    different buy; only ``origin_event_id`` provides exact source-event attribution.
    """
    if not origin_event_id:
        return None
    row = conn.execute(
        "SELECT * FROM memecoin_entry_attempts WHERE origin_event_id=? "
        "ORDER BY decision_started_at DESC, attempt_id DESC LIMIT 1",
        (str(origin_event_id),),
    ).fetchone()
    return dict(row) if row else None


def record_policy_assignment(conn, *, episode_id, arm_id, policy_id, policy_version,
                             parameters_json, entry_snapshot_id, assigned_ts,
                             assignment_reason="entry_policy", code_commit=None):
    """Insert one immutable policy version and its episode/arm assignment.

    Reusing `(policy_id, version)` with different parameters is corruption, not an update.
    Replaying the same assignment is idempotent.
    """
    digest = hashlib.sha256(parameters_json.encode()).hexdigest()
    existing = conn.execute(
        "SELECT immutable_hash FROM memecoin_policy_versions WHERE policy_id=? AND version=?",
        (policy_id, policy_version),
    ).fetchone()
    if existing is not None and existing[0] != digest:
        raise ValueError(
            f"immutable policy collision for {policy_id} version {policy_version}",
        )
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_policy_versions("
        "policy_id, version, template, parameters_json, code_commit, immutable_hash, activated_ts) "
        "VALUES(?,?,?,?,?,?,?)",
        (policy_id, policy_version, arm_id, parameters_json, code_commit, digest, assigned_ts),
    )
    assigned = conn.execute(
        "SELECT policy_id, policy_version FROM memecoin_policy_assignments "
        "WHERE episode_id=? AND arm_id=?",
        (episode_id, arm_id),
    ).fetchone()
    if assigned is not None and (assigned[0], assigned[1]) != (policy_id, policy_version):
        raise ValueError(f"immutable assignment collision for {episode_id}/{arm_id}")
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_policy_assignments("
        "episode_id, arm_id, policy_id, policy_version, entry_snapshot_id, assignment_reason, "
        "assigned_ts) VALUES(?,?,?,?,?,?,?)",
        (episode_id, arm_id, policy_id, policy_version, entry_snapshot_id,
         assignment_reason, assigned_ts),
    )
    conn.commit()


def get_policy_assignment(conn, episode_id, arm_id):
    """Return the exact immutable policy assigned to an episode/arm, or None."""
    row = conn.execute(
        "SELECT a.policy_id, a.policy_version, v.parameters_json "
        "FROM memecoin_policy_assignments a JOIN memecoin_policy_versions v "
        "ON v.policy_id=a.policy_id AND v.version=a.policy_version "
        "WHERE a.episode_id=? AND a.arm_id=?",
        (episode_id, arm_id),
    ).fetchone()
    return dict(row) if row is not None else None


_EXIT_OVERRIDE_PRIORITY = ["operator", "desk_risk", "desk", "micro", "scalp", "ride", "trail", "template",
                           "auto-classify"]
# 'desk' (2026-10-21, spec desk-execution-plumbing §2.5 R2): the desk seat's bounds-checked rails.
# Only the operator outranks them; no cohort overlay may replace the seat's stop on a desk position.
# operator wins. 'micro' (2026-07-16, micro-ladder policy) outranks the tp-ab 'ride' row: a micro
# position is its OWN pre-registered cohort, never part of the tp-vs-ride A/B. 'auto-classify'
# (heuristic entry-archetype overlay) is LAST — it is only written when a position has no other
# override (see apply_entry_classification), and if a tested cohort policy is added later it must win.


def _desk_decision_id(source, decision_id):
    """One type per source in the untyped column (Gamma LOW): risk_live ids are watcher row INTEGERs (a non-integer
    raises), every other source a TEXT id, so 7 and '7' can never be two acks of one decision."""
    if source == "risk_live":
        if isinstance(decision_id, bool) or not (isinstance(decision_id, int) or str(decision_id).strip().isdigit()):
            raise ValueError("risk_live decision_id must be an integer: %r" % (decision_id,))
        return int(decision_id)
    return str(decision_id)


def record_desk_risk_apply(conn, *, decision_id, position_id, action, status, reason, decided_at,
                           applied_at, lag_s=None, rails_before=None, rails_after=None, call_id=None,
                           inputs_hash=None, source="risk_live", detail=None):
    """One ack per (source, decision); the first write wins (a decision is acted on at most once)."""
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_desk_risk_applies(source, decision_id, position_id, action, status, reason, "
        "decided_at, applied_at, lag_s, rails_before_json, rails_after_json, call_id, inputs_hash, detail_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, _desk_decision_id(source, decision_id), position_id, action, status, reason, decided_at, applied_at, lag_s,
         None if rails_before is None else _json.dumps(rails_before, sort_keys=True),
         None if rails_after is None else _json.dumps(rails_after, sort_keys=True), call_id, inputs_hash,
         None if detail is None else _json.dumps(detail, sort_keys=True)))


def desk_risk_acted_ids(conn, decision_ids, source="risk_live"):
    """The ids of `source` already acked. decision_id has no column affinity (Gamma LOW3 rollback): a risk_live
    id stays an INTEGER, an attention id a TEXT hash, so an older release's int IN (json_each) read still matches."""
    if not decision_ids:
        return set()
    seen = {r[0] for r in conn.execute(
        "SELECT decision_id FROM memecoin_desk_risk_applies WHERE source=? AND decision_id IN "
        "(SELECT value FROM json_each(?))", (source, _json.dumps([_desk_decision_id(source, d) for d in decision_ids])))}
    return {d for d in decision_ids if _desk_decision_id(source, d) in seen}


def set_position_exit_override(conn, position_id, source, *, tp_pct=None, tp_target_mcap=None,
                               sl_pct=None, sl_target_mcap=None, trail_pct=None, derisk_pct=None,
                               actor="operator", note="", now_iso=None, commit=True):
    """Set/replace the ACTIVE exit override for (position_id, source), superseding any prior active
    row of the same source (audit trail preserved via active=0 + resolved_at). TP/SL as % OR target
    mcap. Operator rows carry full authority (may loosen); templates set the per-coin initial policy.
    Returns the new row id. Authority is enforced at the API layer, not here."""
    if now_iso is None:
        now_iso = real_utc_now_iso()
    conn.execute("UPDATE memecoin_position_exit_overrides SET active=0, resolved_at=? "
                 "WHERE position_id=? AND source=? AND active=1", (now_iso, position_id, source))
    cur = conn.execute(
        "INSERT INTO memecoin_position_exit_overrides(position_id, source, tp_pct, tp_target_mcap, "
        "sl_pct, sl_target_mcap, trail_pct, derisk_pct, actor, note, active, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
        (position_id, source, tp_pct, tp_target_mcap, sl_pct, sl_target_mcap, trail_pct,
         derisk_pct, actor, note, now_iso))
    if commit:
        conn.commit()
    return cur.lastrowid


def open_micro_position_count(conn):
    """Open REAL nano/micro positions below the configured mid-band boundary.

    All ladder overrides intentionally use source='micro'; entry_mcap keeps the 1-2M mid band
    out of the nano/micro concurrency denominator.
    """
    from sniper.settings.memecoin import MEMECOIN_REAL_MICRO_MCAP_USD
    return conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions p WHERE p.execution_mode='real' AND p.is_open=1 "
        "AND (p.entry_mcap IS NULL OR p.entry_mcap < ?) "
        "AND EXISTS (SELECT 1 FROM memecoin_position_exit_overrides o "
        "WHERE o.position_id=p.id AND o.source='micro' AND o.active=1)",
        (MEMECOIN_REAL_MICRO_MCAP_USD,)).fetchone()[0]


def clear_position_exit_override(conn, position_id, source, now_iso=None, *, commit=True):
    """Deactivate the active override of one source (operator un-sets a manual override, or a
    template is cleared). Effective exit falls back to the next-priority source or the default rails."""
    if now_iso is None:
        now_iso = real_utc_now_iso()
    conn.execute("UPDATE memecoin_position_exit_overrides SET active=0, resolved_at=? "
                 "WHERE position_id=? AND source=? AND active=1", (now_iso, position_id, source))
    if commit:
        conn.commit()


def effective_exit_override(conn, position_id):
    """The override the exit sweep should honor for a position: operator wins, else the
    highest-priority template (scalp>ride>trail>template), else None. Returns a dict of tp/sl
    fields + source/actor (or None)."""
    rows = conn.execute(
        "SELECT source, tp_pct, tp_target_mcap, sl_pct, sl_target_mcap, trail_pct, derisk_pct, "
        "actor FROM memecoin_position_exit_overrides WHERE position_id=? AND active=1",
        (position_id,)).fetchall()
    if not rows:
        return None
    order = {s: i for i, s in enumerate(_EXIT_OVERRIDE_PRIORITY)}
    best = min(rows, key=lambda r: order.get(r["source"], 99))
    return {"source": best["source"], "tp_pct": best["tp_pct"],
            "tp_target_mcap": best["tp_target_mcap"], "sl_pct": best["sl_pct"],
            "sl_target_mcap": best["sl_target_mcap"], "trail_pct": best["trail_pct"],
            "derisk_pct": best["derisk_pct"], "actor": best["actor"]}


def add_wallet(conn, source_user_id, handle, label="", source="manual"):
    """Add a curated wallet. Manual adds UPSERT (win over a paused follow row:
    activate + claim as manual). Follow-sync adds are INSERT-OR-IGNORE so they
    never disturb an existing manual/paused row."""
    if source == "manual":
        conn.execute(
            "INSERT INTO memecoin_wallets(source_user_id, handle, label, paused, source, added_at) "
            "VALUES (?,?,?,0,'manual', strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
            "ON CONFLICT(source_user_id) DO UPDATE SET source='manual', paused=0, handle=excluded.handle",
            (source_user_id, handle, label))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO memecoin_wallets(source_user_id, handle, label, paused, source, added_at) "
            "VALUES (?,?,?,0,?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (source_user_id, handle, label, source))
        # REFRESH THE DISPLAY NAME (2026-08-19). `INSERT OR IGNORE` protects an existing row's
        # STATE — paused/excluded/real_approved must survive a follow sync, which is why this path
        # is not an upsert. But it also discarded the handle, so `handle` froze at whatever the
        # wallet was called when it was first added while fomo went on renaming the account.
        # Live: uid bfc5cd45… was added 2026-07-29 as `groguguy67` and the feed has since
        # delivered its events under BOTH `groguguy67` (n=80) and `ZonalItchyDeer` (n=35),
        # interleaved minutes apart. A roster lookup by the wallet's current name returned
        # "not on roster" and the operator could not find the row they had just armed.
        # Only the display name moves here — never state, and never a blank over a real name.
        if str(handle or "").strip():
            conn.execute(
                "UPDATE memecoin_wallets SET handle=? "
                "WHERE source_user_id=? AND COALESCE(handle,'') <> ?",
                (handle, source_user_id, handle))
    conn.commit()
    row = conn.execute("SELECT id FROM memecoin_wallets WHERE source_user_id=?",
                       (source_user_id,)).fetchone()
    return row[0] if row else None


def rename_wallet_handle(conn, source_user_id, *, expected_handle, new_handle, dry_run=False):
    """Offline metadata-only compare-and-set. Never inserts a wallet or changes its state.

    The caller owns a dedicated connection and has checked same-UID alias evidence. Refuse names
    bound to a handle-keyed funded policy; policy migration needs separate operator scope.
    """
    uid = str(source_user_id or "").strip()
    old = str(expected_handle or "")
    new = str(new_handle or "").strip()
    if not uid or not old.strip() or not new:
        return {"status": "refused", "reason": "blank_identity"}
    if conn.in_transaction:
        raise ValueError("transaction_active")
    from sniper.settings import memecoin as cfg
    bound_handles = {
        str(entry).rpartition(":")[0].strip().casefold()
        for entries in (cfg.MEMECOIN_FOLLOW3050_WALLETS, cfg.MEMECOIN_REAL_TP_ONLY_WALLETS)
        for entry in entries
    }
    with conn:
        conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        row = conn.execute("SELECT handle FROM memecoin_wallets WHERE source_user_id=?",
                           (uid,)).fetchone()
        if row is None:
            return {"status": "refused", "reason": "wallet_not_found"}
        if row[0] not in (old, new):
            return {"status": "refused", "reason": "unexpected_handle"}
        collision = conn.execute(
            "SELECT 1 FROM memecoin_wallets WHERE source_user_id<>? "
            "AND LOWER(TRIM(handle))=LOWER(?) LIMIT 1", (uid, new)).fetchone()
        if collision:
            return {"status": "refused", "reason": "handle_collision"}
        if row[0] == new:
            return {"status": "unchanged", "source_user_id": uid, "handle": new}
        if {old.strip().casefold(), new.casefold()} & bound_handles:
            return {"status": "refused", "reason": "handle_policy_bound"}
        if dry_run:
            return {"status": "planned", "source_user_id": uid, "old_handle": old,
                    "handle": new}
        changed = conn.execute(
            "UPDATE memecoin_wallets SET handle=? WHERE source_user_id=? AND handle=?",
            (new, uid, old)).rowcount
        if changed != 1:
            raise RuntimeError("wallet_handle_compare_and_set_failed")
    return {"status": "renamed", "source_user_id": uid, "old_handle": old, "handle": new}


def list_wallets(conn, include_paused=True):
    q = "SELECT * FROM memecoin_wallets"
    if not include_paused:
        q += " WHERE paused=0"
    return [dict(r) for r in conn.execute(q + " ORDER BY added_at")]


def set_wallet_paused(conn, source_user_id, paused):
    is_paused = 1 if paused else 0
    conn.execute(
        "UPDATE memecoin_wallets SET paused=?, "
        "real_approved=CASE WHEN ? THEN 0 ELSE real_approved END, "
        "real_chains=CASE WHEN ? THEN NULL ELSE real_chains END "
        "WHERE source_user_id=?",
        (is_paused, is_paused, is_paused, source_user_id),
    )
    conn.commit()


def replace_j7_tracked(conn, account_index, kind, rows, *, seen_at, reported_count=None,
                       account_fingerprint=None):
    """Replace one account's tracked list for `kind`, atomically.

    REPLACE, NEVER MERGE: the frame is the account's COMPLETE list, so a wallet J7 dropped must
    leave. `rows=None` means no list arrived and the stored one is left alone — an empty list is a
    real answer and does clear the account, but a missing frame is not evidence of anything.
    """
    kind = str(kind)
    index = int(account_index)
    if rows is None:
        # UNKNOWN, NOT EMPTY. Leave the stored list alone and record that we could not read this
        # one, so the prune can refuse instead of treating an unreadable list as an empty roster.
        if reported_count:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO j7_tracked_meta"
                    "(account_index,kind,reported_count,stored_count,seen_at,unseen,account_fingerprint) "
                    "VALUES(?,?,?,NULL,?,0,?)",
                    (index, kind, int(reported_count), str(seen_at), account_fingerprint))
        return None
    with conn:
        conn.execute("DELETE FROM j7_tracked_wallets WHERE account_index=? AND kind=?",
                     (index, kind))
        conn.executemany(
            "INSERT OR REPLACE INTO j7_tracked_wallets"
            "(account_index,kind,source_user_id,handle,display_name,added_at,seen_at) "
            "VALUES(?,?,?,?,?,?,?)",
            [(index, kind, str(r.get("source_user_id")), r.get("handle"),
              r.get("display_name"), r.get("added_at"), str(seen_at))
             for r in rows if r.get("source_user_id")])
        stored = sum(1 for r in rows if r.get("source_user_id"))
        conn.execute(
            # `unseen=0` IS NAMED, not left to `INSERT OR REPLACE` erasing it. The erasure would
            # be the right semantic by accident; naming it makes "a frame arrived, so this list is
            # no longer unseen" the statement's own claim.
            "INSERT OR REPLACE INTO j7_tracked_meta"
            "(account_index,kind,reported_count,stored_count,seen_at,unseen,account_fingerprint) "
            "VALUES(?,?,?,?,?,0,?)",
            (index, kind,
             (int(reported_count) if reported_count is not None else len(rows)),
             stored, str(seen_at), account_fingerprint))
    return len(rows)


def mark_j7_list_unseen(conn, account_index, kind, *, at, account_fingerprint=None):
    """Record that a CONFIGURED list has never delivered a frame. Never touches a stored list.

    Written for every expected account/kind that `expected_lists` reports without a frame. It is a
    refusal marker: `reported_count` and `stored_count` stay NULL because nothing was reported, and
    `seen_at` holds when we NOTICED the silence, which is why `j7_tracked_seen_at` must skip these
    rows -- a noticing time is not an observation of the list. A later frame overwrites the row
    through `replace_j7_tracked` with `unseen=0`, so recovery needs no operator action.

    An existing row is left alone: a list that HAS reported must not be demoted to unseen by a
    later heartbeat that happens to read a reconnecting socket.
    """
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO j7_tracked_meta"
                "(account_index,kind,reported_count,stored_count,seen_at,unseen,account_fingerprint) "
                "VALUES(?,?,NULL,NULL,?,1,?)",
                (int(account_index), str(kind), str(at), account_fingerprint))
    except sqlite3.Error:
        logger.exception("j7 unseen-list marker failed for account %s kind %s",
                         account_index, kind)


def recently_closed_real_positions(conn, *, within_s, now_iso=None, limit=None):
    """Funded positions closed within the last `within_s` seconds, newest first, bounded.

    The post-close mark window (2026-09-23, operator): the swap-mark lane keeps observing these
    mints so the minutes after a stop are on the tape. Marks only. Every decision path selects
    `is_open=1` itself and never sees these rows. The cutoff is built in Python and compared as
    an ISO string with the same 'T' shape the ledger writes; SQLite's datetime() renders a space
    and would silently miss every row.
    """
    try:
        within = float(within_s or 0)
    except (TypeError, ValueError):
        within = 0.0
    if within <= 0:
        return []
    from datetime import datetime, timedelta, timezone
    if now_iso:
        now_dt = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    else:
        from sniper.util.clock import real_utc_now_iso
        now_dt = datetime.fromisoformat(real_utc_now_iso())
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    cutoff = (now_dt - timedelta(seconds=within)).isoformat()
    if limit is None:
        from sniper.settings import memecoin as cfg
        limit = 2 * int(getattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7) or 7)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memecoin_positions WHERE is_open=0 AND execution_mode='real' "
            "AND closed_at IS NOT NULL AND closed_at >= ? ORDER BY closed_at DESC LIMIT ?",
            (cutoff, int(limit)))]
    except sqlite3.Error:
        logger.exception("recently-closed lookup failed")
        return []


def j7_tracked_ids(conn):
    """Every source_user_id J7 currently tracks, across all accounts and both kinds."""
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT source_user_id FROM j7_tracked_wallets")}
    except sqlite3.Error:
        return set()


def j7_tracked_identity_matches(conn, fingerprints):
    """Only complete account/kind ownership lets manual prune trust the positional lists."""
    if not fingerprints or any(not f for f in fingerprints) or len(set(fingerprints)) != len(fingerprints):
        return False
    expected = {(index, kind): fingerprint for index, fingerprint in enumerate(fingerprints)
                for kind in ("fomo", "pump")}
    try:
        rows = conn.execute("SELECT account_index,kind,account_fingerprint FROM j7_tracked_meta").fetchall()
    except sqlite3.Error:
        return False
    return (len(rows) == len(expected) and all(
        expected.get((row[0], row[1])) == row[2] for row in rows))


def j7_tracked_incomplete(conn):
    """Accounts/kinds where the feed reported more wallets than we stored. [] when all agree.

    A non-empty answer means the tracked set is UNKNOWN for that kind, so anything that decides a
    wallet is untracked must refuse. Returns [] on a read error only because the caller already
    refuses on an empty tracked set and a stale stamp; this is an extra guard, not the only one.
    """
    try:
        rows = conn.execute(
            # A CONFIGURED LIST THAT NEVER REPORTED counts here too (A3 recheck, 2026-09-23): the
            # tracked set is unknown for that kind in exactly the way an unparseable frame makes
            # it unknown, and both must refuse rather than let one account certify another's
            # silence. Its counts are NULL because nothing was ever reported.
            "SELECT account_index, kind, reported_count, stored_count FROM j7_tracked_meta "
            "WHERE unseen = 1 "
            "   OR (COALESCE(reported_count,0) > 0 "
            "       AND (stored_count IS NULL OR stored_count < reported_count)) "
            "ORDER BY account_index, kind").fetchall()
        if rows:
            return [dict(r) for r in rows]
        # NO META AT ALL, BUT LISTS ARE STORED: a writer that predates the counts, so completeness
        # cannot be checked. That is the state this guard exists for — on 2026-09-22 the stored
        # set held ONLY kind=fomo while the feed was tracking 9 pump wallets, and without the
        # counts nothing could tell that from a roster with no pump wallets on it.
        has_rows = conn.execute("SELECT 1 FROM j7_tracked_wallets LIMIT 1").fetchone()
        has_meta = conn.execute("SELECT 1 FROM j7_tracked_meta LIMIT 1").fetchone()
        if has_rows and not has_meta:
            return [{"account_index": None, "kind": "(all)", "reported_count": None,
                     "stored_count": None}]
    except sqlite3.Error:
        return []
    return []


def j7_tracked_seen_at(conn):
    """The OLDEST seen_at across the stored lists, or None when nothing is stored.

    The prune reads this to refuse a stale set. "Absent from every list" is a claim about EVERY
    list, so the evidence is only as fresh as the stalest one; an account that never reconnects
    blocks the prune until it does (Codex review 2026-09-23, A3 — the earlier newest-wins rule
    let one fresh account hide another's stale cache). The meta table carries the stamp of an
    empty list, which stores no wallet rows but is a real answer.
    """
    try:
        row = conn.execute(
            # `unseen` ROWS ARE EXCLUDED. Their stamp is when we noticed the silence, not when a
            # list was observed, so counting it here would report a fresh list that has never
            # spoken -- and counting it as the OLDEST would wedge the prune shut for good. The
            # refusal lives in `j7_tracked_incomplete`; this reader answers only about lists that
            # actually arrived.
            "SELECT MIN(seen_at) FROM (SELECT seen_at FROM j7_tracked_wallets "
            "UNION ALL SELECT seen_at FROM j7_tracked_meta WHERE COALESCE(unseen,0) = 0)"
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def wallets_not_tracked(conn):
    """Roster rows whose source_user_id is on NO J7 tracked list, newest identity fields included.

    Returns [] when nothing is stored, NOT the whole roster: an empty tracked set means the lists
    were never received, and reading that as "J7 tracks nobody" would retire all 553 wallets.
    The caller still has to check freshness with `j7_tracked_seen_at`.
    """
    tracked = j7_tracked_ids(conn)
    if not tracked:
        return []
    if j7_tracked_incomplete(conn):
        # A kind we could not read is a kind whose wallets would all look untracked. Refuse.
        return []
    rows = conn.execute(
        "SELECT source_user_id, handle, COALESCE(real_approved,0) AS real_approved, "
        "COALESCE(paused,0) AS paused, COALESCE(excluded,0) AS excluded "
        "FROM memecoin_wallets ORDER BY COALESCE(real_approved,0) DESC, handle").fetchall()
    return [dict(r) for r in rows if str(r["source_user_id"]) not in tracked]


def record_wallet_audit(conn, source_user_id, action, *, actor, reason, now_iso, chain=None):
    """One operator-attributable row in the wallet approval audit, with no copy statistics.

    `_audit_approval` needs a stats dict and only runs for approved wallets. A prune retires
    unapproved rows too, and those retirements must still be attributable.
    """
    conn.execute(
        "INSERT INTO memecoin_wallet_approvals(source_user_id, action, actor, reason, copy_pnl, "
        "copy_closes, forced, created_at, chain) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(source_user_id), str(action), str(actor), str(reason), None, None, 0,
         str(now_iso), chain))


def remove_wallet(conn, source_user_id):
    """Retire a wallet without erasing the roster identity or its evidence."""
    row = conn.execute(
        "SELECT COALESCE(real_approved,0) FROM memecoin_wallets WHERE source_user_id=?",
        (source_user_id,)).fetchone()
    if not row:
        return False
    stats = alpha_b_copy_stats(conn, source_user_id) if row[0] else None
    try:
        conn.execute(
            "UPDATE memecoin_wallets SET paused=1,excluded=1,real_approved=0,real_chains=NULL "
            "WHERE source_user_id=?", (source_user_id,))
        # Retire excludes with raw SQL rather than through set_wallet_excluded, so the exemption
        # clear has to be repeated here. Two exclusion paths, both must drop waivers.
        try:
            clear_wallet_gate_exemptions(conn, source_user_id, actor="wallet_retire",
                                         now_iso=real_utc_now_iso())
            clear_wallet_chase_caps(conn, source_user_id, actor="wallet_retire",
                                    now_iso=real_utc_now_iso())
            clear_wallet_exit_overrides(conn, source_user_id, actor="wallet_retire",
                                        now_iso=real_utc_now_iso())
        except Exception:
            logger.exception("could not clear wallet controls on retire for %s", source_user_id)
        if stats is not None:
            _audit_approval(
                conn, source_user_id, "revoke", actor="wallet_retire",
                reason="wallet retired to excluded", stats=stats, forced=False,
                now_iso=real_utc_now_iso())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def curated_user_ids(conn):
    return {r[0] for r in conn.execute(
        "SELECT source_user_id FROM memecoin_wallets WHERE paused=0 AND COALESCE(excluded,0)=0")}


def set_wallet_excluded(conn, source_user_id, excluded, *, actor="system", now_iso=None):
    """Exclude or re-include a wallet without retention of funded authority.

    EXCLUSION ALSO CLEARS GATE EXEMPTIONS (2026-08-28). An excluded wallet carries no waiver into a
    future re-arm: the re-arm decides fresh rather than silently inheriting a selection gate
    somebody excused weeks ago for a reason nobody re-examined. Same doctrine as real_approved and
    real_chains being dropped on the line above.
    """
    ex = 1 if excluded else 0
    # ONE TRANSACTION. The exclusion used to commit before the exemptions were cleared, with the
    # clear wrapped in a swallowing except — so a failure there left the wallet excluded AND still
    # waived, which is the one combination this function exists to make impossible. Excluded-but-
    # waived is worse than either state alone: nothing surfaces it, and a re-arm inherits it.
    # Either both land or neither does.
    conn.execute("SAVEPOINT wallet_exclusion")
    try:
        conn.execute(
            "UPDATE memecoin_wallets SET excluded=?,paused=?,real_approved=0,real_chains=NULL "
            "WHERE source_user_id=?",
            (ex, ex, source_user_id),
        )
        if ex:
            clear_wallet_gate_exemptions(
                conn, source_user_id, actor=actor,
                now_iso=now_iso or real_utc_now_iso(), commit=False)
            clear_wallet_chase_caps(
                conn, source_user_id, actor=actor,
                now_iso=now_iso or real_utc_now_iso(), commit=False)
            clear_wallet_exit_overrides(
                conn, source_user_id, actor=actor,
                now_iso=now_iso or real_utc_now_iso(), commit=False)
    except Exception:
        conn.execute("ROLLBACK TO wallet_exclusion")
        conn.execute("RELEASE wallet_exclusion")
        raise
    conn.execute("RELEASE wallet_exclusion")
    conn.commit()


def reobserve_wallet(conn, source_user_id):
    """Restore an excluded wallet to watch-only; never restore funded authority."""
    row = conn.execute(
        "SELECT COALESCE(real_approved,0) FROM memecoin_wallets WHERE source_user_id=?",
        (source_user_id,)).fetchone()
    if not row:
        return False
    stats = alpha_b_copy_stats(conn, source_user_id) if row[0] else None
    try:
        conn.execute(
            "UPDATE memecoin_wallets SET paused=0,excluded=0,real_approved=0,real_chains=NULL "
            "WHERE source_user_id=?", (source_user_id,))
        conn.execute(
            "UPDATE memecoin_candidates SET status='promoted' WHERE source_user_id=?",
            (source_user_id,))
        if stats is not None:
            _audit_approval(
                conn, source_user_id, "revoke", actor="wallet_reobserve",
                reason="re-observed as watch-only", stats=stats, forced=False,
                now_iso=real_utc_now_iso())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def ensure_address_registry_tables(conn):
    """The two registry tables alone, so the reconcile script can create them inside its own
    transaction (and roll them back in a dry run) without running every migration."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_address_registry (
            source_user_id TEXT NOT NULL,
            network_id TEXT NOT NULL,
            address TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence_kind TEXT NOT NULL,
            evidence_json TEXT,
            handle_seen TEXT,
            first_seen_at TEXT NOT NULL,
            status_at TEXT NOT NULL,
            conflict_at TEXT,
            PRIMARY KEY (source_user_id, network_id, address)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_address_registry_net_status "
                 "ON memecoin_address_registry(network_id, status)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_address_registry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_user_id TEXT NOT NULL,
            network_id TEXT NOT NULL,
            address TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            evidence_kind TEXT NOT NULL,
            evidence_json TEXT,
            actor TEXT NOT NULL,
            at TEXT NOT NULL
        )""")


_ADDRESS_STATUS_RANK = {"candidate": 0, "verified": 1, "refuted": 2}


def record_address_evidence(conn, *, source_user_id, network_id, address, status, evidence_kind,
                            now_iso, actor, evidence_json=None, handle=None):
    """Apply one piece of address evidence to memecoin_address_registry. Returns the new status
    when it changed (or the row is new), else None. Does not commit.

    Precedence: refuted > verified > candidate. Weaker or equal evidence never rewrites a row, so a
    re-run writes nothing. A refutation is never lifted automatically; verified evidence for a
    refuted address, or refuted evidence for a verified one, stamps `conflict_at` (once) for the
    coverage surface, whichever arrives first. Every change writes
    one memecoin_address_registry_events row; nothing is deleted."""
    if status not in _ADDRESS_STATUS_RANK:
        raise ValueError("unknown address status %r" % (status,))
    key = (str(source_user_id), str(network_id), str(address))
    row = conn.execute(
        "SELECT status, conflict_at FROM memecoin_address_registry "
        "WHERE source_user_id=? AND network_id=? AND address=?", key).fetchone()
    old = row[0] if row else None
    if old is not None and _ADDRESS_STATUS_RANK[status] <= _ADDRESS_STATUS_RANK[old]:
        if old == "refuted" and status == "verified" and row[1] is None:
            conn.execute(
                "UPDATE memecoin_address_registry SET conflict_at=? "
                "WHERE source_user_id=? AND network_id=? AND address=?", (now_iso, *key))
        return None
    if old is None:
        conn.execute(
            "INSERT INTO memecoin_address_registry (source_user_id, network_id, address, status, "
            "evidence_kind, evidence_json, handle_seen, first_seen_at, status_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (*key, status, evidence_kind, evidence_json, handle, now_iso, now_iso))
    else:
        # verified -> refuted is the same disagreement in the other order: flag it too.
        conflict = now_iso if (old == "verified" and status == "refuted") else None
        conn.execute(
            "UPDATE memecoin_address_registry SET status=?, evidence_kind=?, evidence_json=?, "
            "handle_seen=COALESCE(?, handle_seen), status_at=?, "
            "conflict_at=COALESCE(conflict_at, ?) "
            "WHERE source_user_id=? AND network_id=? AND address=?",
            (status, evidence_kind, evidence_json, handle, now_iso, conflict, *key))
    conn.execute(
        "INSERT INTO memecoin_address_registry_events (source_user_id, network_id, address, "
        "from_status, to_status, evidence_kind, evidence_json, actor, at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (*key, old, status, evidence_kind, evidence_json, actor, now_iso))
    return status


def address_registry_rows(conn, network_id):
    """Every registry row on one chain (verified, candidate and refuted)."""
    return [dict(zip(("source_user_id", "address", "status", "evidence_kind", "handle_seen",
                      "conflict_at"), r)) for r in conn.execute(
        "SELECT source_user_id, address, status, evidence_kind, handle_seen, conflict_at "
        "FROM memecoin_address_registry WHERE network_id=? ORDER BY source_user_id, address",
        (str(network_id),))]


def current_verified_relay_identity(conn, uid, origin, status_at, evm_address):
    """Current Solana identity and an EVM recipient unclaimed by any other trader."""
    return conn.execute(
        "SELECT 1 FROM memecoin_address_registry r WHERE r.source_user_id=? "
        "AND r.network_id=? AND r.address=? AND r.status='verified' "
        "AND r.conflict_at IS NULL AND r.status_at=? AND NOT EXISTS ("
        "SELECT 1 FROM memecoin_address_registry other WHERE other.network_id=r.network_id "
        "AND other.address=r.address AND other.source_user_id<>r.source_user_id "
        "AND other.status='verified') AND NOT EXISTS ("
        "SELECT 1 FROM axiom_verified_addresses a WHERE a.network_id<>'solana' "
        "AND lower(a.address)=? AND a.source_user_id<>r.source_user_id) AND NOT EXISTS ("
        "SELECT 1 FROM memecoin_wallet_addresses a WHERE a.network_id<>'solana' "
        "AND lower(a.address)=? AND a.source_user_id<>r.source_user_id "
        "AND a.verify_status='fomoscan_confirmed') AND NOT EXISTS ("
        "SELECT 1 FROM memecoin_address_registry a WHERE a.network_id<>'solana' "
        "AND lower(a.address)=? AND a.source_user_id<>r.source_user_id AND a.status='verified')",
        (uid, 'solana', origin, status_at, evm_address, evm_address, evm_address)).fetchone() is not None


def real_approved_user_ids(conn, network_id=None):
    """Wallets whose BUYS may open a real position ON `network_id`.

    Arming is PER CHAIN (2026-07-30), reversing the 2026-07-15 global decision.
    `memecoin_wallets.real_chains` is the authority list: a comma-separated set of network
    ids. NULL or empty means armed NOWHERE — it is never read as "all chains", because a
    silent default is exactly the confusion this removes.

    A caller that cannot name a chain gets the empty set. The venue-context guard in
    make_real_hook answers "can we execute on this chain at all" (capability); this answers
    "is this wallet authorised here" (authority). They are different questions, and the
    first never stood in for the second.

    A paused/excluded wallet is still never real-approved — curated gating comes first.
    """
    from sniper.services.memecoin_chains import canonical_chain_id
    chain = canonical_chain_id(network_id)
    if not chain:
        return set()
    return {row["source_user_id"] for row in conn.execute(
        "SELECT source_user_id, real_chains FROM memecoin_wallets "
        "WHERE COALESCE(real_approved,0)=1 AND paused=0 AND COALESCE(excluded,0)=0")
        if chain in _parse_real_chains(row["real_chains"])}


def _parse_real_chains(raw):
    """'solana,4663' -> {'solana','4663'}. NULL/blank -> empty set (armed nowhere).

    Every token is canonicalised to the FUNDED network id, so a legacy row storing the UI
    spelling ('robin') still resolves to the identity the entry gate compares ('4663').
    An unresolvable token is dropped rather than kept as a chain nothing can match.
    """
    from sniper.services.memecoin_chains import canonical_chain_id
    out = set()
    for token in str(raw or "").split(","):
        cid = canonical_chain_id(token)
        if cid:
            out.add(cid)
    return out



def armed_bought_mints(ledger_path, chain, since_iso):
    """The mints ANY lane decided on for `chain` since `since_iso` (memecoin_entry_attempts): the
    safety warm set, not fills (lambda + Beta, Alpha GO 2026-10-14). Lowercased. Read-only, 250 ms
    busy timeout; an unreadable ledger raises and the caller keeps its previous set."""
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection(str(ledger_path))
    try:
        conn.execute("PRAGMA busy_timeout=250")
        return {str(r[0]).lower() for r in conn.execute(
            "SELECT DISTINCT mint FROM memecoin_entry_attempts INDEXED BY idx_mc_entry_view_completed "
            "WHERE completed_at >= ? AND network_id = ? AND mint IS NOT NULL", (since_iso, str(chain)))}
    finally:
        conn.close()


def armed_sold_mints(ledger_path, chain, since_iso):
    """The mints wallets armed on `chain` sold since `since_iso` (memecoin_events side='sell'), one
    walk of idx_mc_events_user_time per armed wallet. Same read rules as armed_bought_mints."""
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection(str(ledger_path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=250")
        out = set()
        for uid in sorted(real_approved_user_ids(conn, network_id=chain)):
            out.update(str(r[0]).lower() for r in conn.execute(
                "SELECT DISTINCT mint FROM memecoin_events WHERE source_user_id = ? AND created_at >= ? "
                "AND side = 'sell' AND network_id = ? AND mint IS NOT NULL", (uid, since_iso, str(chain))))
        return out
    finally:
        conn.close()

def recent_armed_solana_route_targets(conn, *, now_iso=None, within_s=86400, limit=64,
                                     scan_limit=1024):
    """Untrusted executed-pool read targets from the newest 1024 position IDs, at most 64 coins.
    Only currently armed Solana wallets and aware causal opens within 24 h qualify. Paper and real
    rows both name read targets; this is our observed route coverage, never admission evidence.
    Missing/invalid/future timestamps are unknown; a failed read raises rather than becoming []."""
    limit, scan_limit = min(64, max(0, int(limit))), min(1024, max(0, int(scan_limit)))
    if not limit or not scan_limit:
        return []
    armed = real_approved_user_ids(conn, network_id="solana")
    if not armed:
        return []
    now = datetime.fromisoformat(now_iso or real_utc_now_iso())
    if now.tzinfo is None:
        raise ValueError("route target clock must be aware UTC")
    rows = conn.execute(
        "SELECT source_user_id,network_id,mint,executed_pool,opened_at FROM memecoin_positions "
        "WHERE id>(SELECT COALESCE(MAX(id),0) FROM memecoin_positions)-? "
        "ORDER BY id DESC LIMIT ?", (scan_limit, scan_limit)).fetchall()
    targets, seen = [], set()
    for row in rows:
        coin, pool = row["mint"], row["executed_pool"]
        if (row["source_user_id"] not in armed or not coin or not pool or coin in seen
                or normalized_coin_network(row["network_id"], coin) != "solana"):
            continue
        try:
            opened = datetime.fromisoformat(row["opened_at"])
            if opened.tzinfo is None:
                continue
            age_s = (now - opened).total_seconds()
        except (TypeError, ValueError):
            continue
        if not 0 <= age_s <= within_s:
            continue
        targets.append({"coin": coin, "pool": pool, "age_s": age_s})
        seen.add(coin)
        if len(targets) >= limit:
            break
    return targets


def real_armed_any_chain_user_ids(conn):
    """Wallets armed on AT LEAST ONE chain — for observation/roster lanes, never authority.

    Entry authority must always name a chain via real_approved_user_ids(conn, network_id=...).
    """
    return {row["source_user_id"] for row in conn.execute(
        "SELECT source_user_id, real_chains FROM memecoin_wallets "
        "WHERE COALESCE(real_approved,0)=1 AND paused=0 AND COALESCE(excluded,0)=0")
        if _parse_real_chains(row["real_chains"])}


def set_wallet_chain_armed(conn, source_user_id, network_id, armed, *, actor="operator",
                           reason="", force=False, min_closes=None, min_pnl=None, now_iso=None):
    """Arm or disarm ONE chain for one wallet, in ONE transaction.

    This is the only correct way to change funded authority (audit remediation 2026-07-30).
    The previous flow had the client send a whole `chains` list, so arming a second chain
    silently replaced the first, and it wrote the approval flag and the chain list in two
    separate commits — leaving `real_approved=1, real_chains=NULL` on a mid-write failure,
    which the pinned funded release reads as armed on EVERY chain.

    Semantics:
      - `network_id` is canonicalised to the funded identity ('robin' -> '4663').
      - ARM adds the chain, leaving every other armed chain untouched, and sets the approval
        flag. Subject to the same copy-PnL promote gate as a whole-wallet approve.
      - DISARM removes only that chain. Removing the LAST chain also clears the approval
        flag, so the roster never shows an approved wallet armed nowhere.
      - Flag, chain list and audit row commit together, or none of them do.
    Returns {ok, action, chains, stats, reason}.
    """
    from sniper.services.memecoin_chains import (
        canonical_chain_id, funded_chain_keys, resolve_chain)

    chain = canonical_chain_id(network_id)
    if not chain:
        return {"ok": False, "action": "unknown_chain", "chains": wallet_armed_chains(
            conn, source_user_id), "stats": {}, "reason": f"unknown chain {network_id!r}"}
    key = resolve_chain(network_id)
    # ARMING a chain the funded runner cannot execute on is latent authority: the venue guard
    # blocks it today, so it looks harmless, but enabling that venue later would silently
    # activate approvals nobody reviewed. Research chains and the `unresolved` data-quality
    # bucket are never armable. DISARM is always allowed, so a chain armed before this guard
    # can still be cleaned up.
    if armed and key not in funded_chain_keys():
        return {"ok": False, "action": "chain_not_funded",
                "chains": wallet_armed_chains(conn, source_user_id), "stats": {},
                "reason": (f"{key} is research-only — funded authority exists only for "
                           f"{', '.join(funded_chain_keys())}")}
    if now_iso is None:
        now_iso = real_utc_now_iso()

    row = conn.execute(
        "SELECT real_chains,COALESCE(excluded,0) FROM memecoin_wallets "
        "WHERE source_user_id=?",
        (source_user_id,),
    ).fetchone()
    current = _parse_real_chains(row[0] if row else None)
    if armed and row and bool(row[1]):
        return {
            "ok": False,
            "action": "excluded_requires_reobserve",
            "chains": sorted(current),
            "stats": {},
            "reason": "excluded wallet requires re-observe",
        }

    if armed:
        # Chain-scoped gate: the evidence that grants authority on this chain must come from
        # this chain (2026-08-03). Previously global, which armed Robin off Solana closes.
        preview = real_approval_preview(conn, source_user_id, reason=reason, force=force,
                                        min_closes=min_closes, min_pnl=min_pnl, chain=key)
        stats = preview["stats"]
        if not preview["ok"]:
            _audit_approval(conn, source_user_id, "approve_refused", actor=actor,
                            reason=preview["reason"], stats=stats, forced=False,
                            now_iso=now_iso, chain=chain)
            conn.commit()
            return {"ok": False, "action": "approve_refused", "chains": sorted(current),
                    "stats": stats, "reason": preview["reason"]}
        target = sorted(current | {chain})
        action = "chain_arm"
    else:
        stats = alpha_b_copy_stats(conn, source_user_id, chain=key)
        target = sorted(current - {chain})
        action = "chain_revoke"

    # One transaction: flag + authority list + audit row, or nothing.
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        pass          # already inside a transaction; the outer one owns the commit
    try:
        conn.execute(
            "UPDATE memecoin_wallets SET real_approved=?,real_chains=?,paused=0 "
            "WHERE source_user_id=?",
            (1 if target else 0, ",".join(target) or None, source_user_id))
        _audit_approval(conn, source_user_id, action, actor=actor,
                        reason=reason or f"{chain}: {'arm' if armed else 'revoke'}",
                        stats=stats, forced=bool(force and armed), now_iso=now_iso, chain=chain)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"ok": True, "action": action, "chains": target, "stats": stats, "reason": reason}


def wallet_armed_chains(conn, source_user_id):
    """Sorted chains this wallet may open real positions on. Empty when armed nowhere."""
    row = conn.execute("SELECT real_chains FROM memecoin_wallets WHERE source_user_id=?",
                       (source_user_id,)).fetchone()
    return sorted(_parse_real_chains(row["real_chains"] if row else None))


def set_wallet_real_chains(conn, source_user_id, chains, *, actor="operator", reason="",
                           now_iso=None):
    """Set a wallet's per-chain authority list. Audited like any other arming change.

    Non-funded chains are dropped here as well as in set_wallet_chain_armed (2026-08-03):
    this is the second write path to the same authority, and a guard on only one of them is
    not a guard. Dropped tokens are named in the audit reason, never removed silently.
    """
    if now_iso is None:
        now_iso = real_utc_now_iso()
    from sniper.services.memecoin_chains import (
        canonical_chain_id, funded_chain_keys, resolve_chain)
    requested = [c for c in (chains or [])]
    normalised = sorted({canonical_chain_id(c) for c in requested
                         if resolve_chain(c) in funded_chain_keys()} - {None})
    dropped = sorted({str(c) for c in requested
                      if resolve_chain(c) not in funded_chain_keys()})
    state = conn.execute(
        "SELECT COALESCE(real_approved,0),COALESCE(excluded,0) "
        "FROM memecoin_wallets WHERE source_user_id=?",
        (source_user_id,),
    ).fetchone()
    if not state:
        return {"ok": False, "action": "wallet_not_found", "chains": [], "dropped": dropped}
    if normalised and bool(state[1]):
        return {
            "ok": False,
            "action": "excluded_requires_reobserve",
            "chains": wallet_armed_chains(conn, source_user_id),
            "dropped": dropped,
        }
    if normalised and not bool(state[0]):
        return {
            "ok": False,
            "action": "approval_required",
            "chains": wallet_armed_chains(conn, source_user_id),
            "dropped": dropped,
        }
    if normalised:
        conn.execute(
            "UPDATE memecoin_wallets SET real_chains=?,paused=0 WHERE source_user_id=?",
            (",".join(normalised), source_user_id),
        )
    else:
        conn.execute(
            "UPDATE memecoin_wallets SET real_chains=NULL,real_approved=0,"
            "paused=CASE WHEN COALESCE(excluded,0)=1 THEN 1 ELSE 0 END "
            "WHERE source_user_id=?",
            (source_user_id,),
        )
    note = f"chains={normalised or 'none'}"
    if dropped:
        note += f"; dropped non-funded {','.join(dropped)}"
    _audit_approval(conn, source_user_id, "set_chains", actor=actor,
                    reason=reason or note,
                    stats=alpha_b_copy_stats(conn, source_user_id), forced=False,
                    now_iso=now_iso)
    conn.commit()
    return {"ok": True, "action": "set_chains", "chains": normalised, "dropped": dropped}


# The alpha_b copy-evidence filter, written ONCE. `alpha_b_copy_stats` (one wallet) and
# `alpha_b_copy_stats_all` (every wallet, one grouped pass) must answer the same question, so they
# share the predicate and the chain scope instead of each spelling it out. A second spelling is how
# a batched view and the gate that reads it drift apart without either one looking wrong.
def _alpha_b_base_sql():
    # Built on CALL, not at import: measurable_outcome_sql() reads module constants defined further
    # down the file, so a module-level constant here raises NameError at import time.
    return ("strategy='alpha_b' AND COALESCE(execution_mode,'paper')!='real' AND "
            + measurable_outcome_sql())


def _alpha_b_scope_sql(network_id, chain):
    """Chain scope for the alpha_b evidence queries: ('' | ' AND ...', params).

    `chain` is a canonical key and expands to EVERY network id the chain is stored under
    ('robin' -> '4663','robin'). A bare `network_id=` matches one exact spelling and would silently
    miss rows written under the other, so the two are not interchangeable.
    """
    if chain is not None:
        from sniper.services.memecoin_chains import network_ids_for, resolve_chain
        key = resolve_chain(chain)
        if key is None:
            raise ValueError(f"unknown chain {chain!r}; chains are never pooled")
        nets = tuple(network_ids_for(key))
        return (f" AND {COIN_NETWORK_SQL} IN (%s)" % ",".join("?" * len(nets))), list(nets)
    if network_id is not None:
        return f" AND {COIN_NETWORK_SQL}=?", [normalized_coin_network(network_id, "")]
    return "", []


def alpha_b_copy_stats(conn, source_user_id, network_id=None, *, chain=None):
    """OUR measurable copy evidence for a wallet: closed alpha_b PAPER positions.
    This — not the trader's own PnL — is the pre-registered arming criterion (2026-07-11 study:
    PrimeT own -20% -> copy +$529; techquant own +1970% -> copy -$78).

    MEASURABLE (2026-08-05): this uses the same measurable-outcome predicate as the paper report
    and arm matrix. Censored/noquote, missing-fill, unquotable-without-mark, and Fomo-tainted closes
    cannot qualify a funded wallet or change its evidence-based controls.

    FOMO-EXCLUDED (2026-07-15, the 0.4 governance fix): positions with ANY fill priced by the fomo
    no-impact fallback (fill_source='fomo': entry==their_price, exit==mid, ZERO slippage) are
    dropped. That price is a fantasy no real fill can hit — fomo-exit closes averaged +$12.91 vs
    +$0.97 for honest dexscreener exits, so they inflated the promote metric and armed proven real
    losers (Magneto 16/$108 all-fills -> 0/$0 honest; DearSheerLark, careful, _OHT_ flipped
    negative). Arming must reflect achievable real execution, so only positions HONEST ON BOTH
    LEGS count. Real fills never use the fomo source, so live real positions are unaffected.

    SCOPE (corrected 2026-08-03). Arming became PER CHAIN on 2026-07-30, but this metric and the
    gate that reads it were left global from the 2026-07-15 decision. That let a wallet qualify for
    one chain on another chain's evidence — Robin armed off 5 Solana closes with zero Robin closes,
    which is exactly the pooling the per-chain evidence doctrine forbids. Pass `chain` (a canonical
    key) to scope the gate; `network_id=None, chain=None` still aggregates, for the roster views
    that legitimately want a wallet-level total.

    `chain` expands to EVERY network id the chain is stored under ('robin' -> '4663','robin'),
    which a bare `network_id=` cannot do — that path matches one exact spelling and would silently
    miss rows written under the other."""
    q = ("SELECT COUNT(*) n, COALESCE(SUM(realized_pnl),0) pnl FROM memecoin_positions "
         "WHERE source_user_id=? AND " + _alpha_b_base_sql())
    params = [source_user_id]
    scope_sql, scope_params = _alpha_b_scope_sql(network_id, chain)
    r = conn.execute(q + scope_sql, params + scope_params).fetchone()
    return {"n_closes": int(r["n"] or 0), "pnl": round(float(r["pnl"] or 0.0), 2)}


def alpha_b_copy_stats_all(conn, network_id=None, *, chain=None, dense=False):
    """Batched alpha_b_copy_stats keyed by source_user_id — the SAME measurable metric (alpha_b-only,
    non-real, censored/fomo-excluded) computed in one grouped pass for the Traders/Performance UI views, so
    those views can show the arming criterion instead of a blended all-arms/fomo total. A wallet with
    zero honest closes is ABSENT from the dict (never silently rendered as a fat blended number);
    callers default the missing key to {n_closes:0, pnl:0.0}. See alpha_b_copy_stats for the why.

    `chain` scopes it exactly as the singular does, which is what lets the per-chain Armed board
    read this instead of calling the singular once per wallet (527 queries, 14.4s, 2026-09-08)."""
    q = ("SELECT source_user_id, COUNT(*) n, COALESCE(SUM(realized_pnl),0) pnl "
         "FROM memecoin_positions WHERE " + _alpha_b_base_sql())
    scope_sql, scope_params = _alpha_b_scope_sql(network_id, chain)
    q += scope_sql + " GROUP BY source_user_id"
    out = {r["source_user_id"]: {"n_closes": int(r["n"] or 0), "pnl": round(float(r["pnl"] or 0.0), 2)}
           for r in conn.execute(q, scope_params)}
    if dense:
        # ABSENT AND ZERO ARE DIFFERENT WORDS FOR THE SAME FACT, AND ONLY ONE IS SAFE TO READ.
        # Scoping to a chain drops most of the roster: on 2026-09-08 the unscoped dict held 191 of
        # 527 wallets, solana 176, robin 126, and base and bsc were EMPTY. An empty dict from a
        # chain with armed wallets reads like a failed query rather than "no evidence here", and
        # the singular answers that same question with an explicit zero. `dense=True` fills every
        # wallet so a caller cannot tell the two apart by accident. Default off: three existing
        # callers consume the sparse shape correctly.
        for row in conn.execute("SELECT source_user_id FROM memecoin_wallets"):
            out.setdefault(row[0], {"n_closes": 0, "pnl": 0.0})
    return out


def backfill_evm_unknown_events(conn, *, dry_run=False):
    """Resolve `memecoin_events` rows written with network_id='evm-unknown' to their real chain, using a
    same-mint row that carries a KNOWN chain (feed observations + other events). The same 0x mint can
    exist on more than one chain (Base vs Robinhood 4663), so ONLY unambiguous mints — those with exactly
    one known chain — are resolved; ambiguous and orphan mints are left as-is (chain genuinely unknown).
    Idempotent (touches only evm-unknown rows). Returns {resolved, updated, ambiguous, unresolved}."""
    unknown_mints = {r["mint"] for r in conn.execute(
        "SELECT DISTINCT mint FROM memecoin_events WHERE network_id='evm-unknown' AND COALESCE(mint,'')!=''")}
    if not unknown_mints:
        return {"resolved": 0, "updated": 0, "ambiguous": 0, "unresolved": 0}
    chains = {}                                          # mint -> set of known chains
    for src in ("SELECT DISTINCT mint, network_id FROM memecoin_feed_observations_v2 "
                "WHERE network_id NOT IN ('evm-unknown','')",
                "SELECT DISTINCT mint, network_id FROM memecoin_events "
                "WHERE network_id NOT IN ('evm-unknown','')"):
        for r in conn.execute(src):
            if r["mint"] in unknown_mints:
                chains.setdefault(r["mint"], set()).add(r["network_id"])
    resolve = {m: next(iter(cs)) for m, cs in chains.items() if len(cs) == 1}
    ambiguous = sum(1 for cs in chains.values() if len(cs) > 1)
    unresolved = len(unknown_mints) - len(resolve) - ambiguous
    updated = 0
    if not dry_run:
        for mint, chain in resolve.items():
            cur = conn.execute(
                "UPDATE memecoin_events SET network_id=? WHERE network_id='evm-unknown' AND mint=?",
                (chain, mint))
            updated += cur.rowcount
        conn.commit()
    return {"resolved": len(resolve), "updated": updated, "ambiguous": ambiguous,
            "unresolved": unresolved}


def prior_buyers_60m(conn, *, mint, network_id=None, source_user_id, before_iso, window_s=3600):
    """Distinct OTHER tracked wallets that bought this coin in the `window_s` before `before_iso`.

    Consensus context for the front-run alert: a coin several tracked wallets bought inside an hour
    hit +20% far more often than a lone buy (37.5% vs 10%, 2026-07-11 beat-trader study), so a lone
    late buy and a 4-wallet cluster are very different signals the operator should see distinguished.

    Coin identity is chain-scoped; the same 0x address may exist on Base and Robinhood 4663. Time
    diff is in the fomo createdAt domain — same clock as the current event, so the subtraction is
    honest (the host wall clock runs +7h and must NOT be mixed in). Returns None on missing inputs
    (unknown, not a claimed zero). Cheap: idx_mc_events_mint makes this a point lookup."""
    if not mint or not before_iso or not source_user_id:
        return None
    try:
        anchor = conn.execute("SELECT strftime('%s', ?)", (before_iso,)).fetchone()
        if not anchor or anchor[0] is None:
            return None
        row = conn.execute(
            "SELECT COUNT(DISTINCT CASE WHEN "
            "strftime('%s', ?) - strftime('%s', created_at) BETWEEN 0 AND ? "
            "AND strftime('%s', created_at) < strftime('%s', ?) THEN source_user_id END), "
            "SUM(CASE WHEN strftime('%s', created_at) IS NULL THEN 1 ELSE 0 END) "
            "FROM memecoin_events "
            f"WHERE mint=? AND {COIN_NETWORK_SQL}=? AND side='buy' "
            "AND source_user_id IS NOT NULL AND source_user_id<>?",
            (before_iso, int(window_s), before_iso, mint,
             normalized_coin_network(network_id, mint), source_user_id)).fetchone()
        if not row or int(row[1] or 0) > 0:
            return None
        return int(row[0]) if row[0] is not None else None
    except Exception:
        return None


def _audit_approval(conn, source_user_id, action, *, actor, reason, stats, forced, now_iso,
                    chain=None):
    conn.execute(
        "INSERT INTO memecoin_wallet_approvals(source_user_id, action, actor, reason, copy_pnl, "
        "copy_closes, forced, created_at, chain) VALUES (?,?,?,?,?,?,?,?,?)",
        (source_user_id, action, actor, reason, stats["pnl"], stats["n_closes"],
         1 if forced else 0, now_iso, chain))


def funded_source_is_identified(conn, source_user_id):
    """True only when the funded source has an operator-visible identity."""
    if not source_user_id:
        return False
    row = conn.execute(
        "SELECT handle,label FROM memecoin_wallets WHERE source_user_id=?",
        (source_user_id,),
    ).fetchone()
    if row is None:
        return False
    return bool(str(row["handle"] or "").strip() or str(row["label"] or "").strip())


def real_approval_preview(conn, source_user_id, *, reason="", force=False,
                          min_closes=None, min_pnl=None, chain=None):
    """Evaluate the real-approval gate without changing wallet or audit state.

    `chain` scopes the evidence to the chain being armed (2026-08-03). Authority is granted per
    chain, so the evidence that grants it must come from that chain — a global metric let one
    chain's paper record qualify a wallet on a chain it had never traded.
    """
    stats = alpha_b_copy_stats(conn, source_user_id, chain=chain)
    if not funded_source_is_identified(conn, source_user_id):
        return {
            "ok": False,
            "action": "identity_required",
            "stats": stats,
            "reason": "funded authority requires an identified source",
            "forced": False,
        }
    state = conn.execute(
        "SELECT COALESCE(excluded,0) FROM memecoin_wallets WHERE source_user_id=?",
        (source_user_id,),
    ).fetchone()
    if state and bool(state[0]):
        return {
            "ok": False,
            "action": "excluded_requires_reobserve",
            "stats": stats,
            "reason": "excluded wallet requires re-observe",
            "forced": False,
        }
    if min_closes is None or min_pnl is None:
        from sniper.settings import memecoin as _cfg
        min_closes = (_cfg.MEMECOIN_REAL_PROMOTE_MIN_CLOSES
                      if min_closes is None else min_closes)
        min_pnl = (_cfg.MEMECOIN_REAL_PROMOTE_MIN_PNL_USD
                   if min_pnl is None else min_pnl)
    if not force and not (stats["n_closes"] >= min_closes and stats["pnl"] >= min_pnl):
        where = f" on {chain}" if chain else ""
        why = (f"gate: {stats['n_closes']} alpha_b closes{where} (need >={min_closes}) "
               f"and ${stats['pnl']:.2f} copy PnL (need >=${min_pnl:.2f})"
               + (f"; {reason}" if reason else ""))
        return {"ok": False, "action": "approve_refused", "stats": stats,
                "reason": why, "forced": False}
    return {"ok": True, "action": "approve", "stats": stats,
            "reason": reason, "forced": bool(force)}


def record_wallet_real_follow_refusal(conn, source_user_id, *, actor="operator", reason="",
                                      force=False, stats=None, now_iso=None, chain=None):
    """Audit a qualified arm refused because its required Fomo follow could not be established.

    `chain` scopes the refusal (2026-08-05). Arming is per chain, so a refusal that does not name
    the chain cannot be told apart from a refusal on a different one when reading the trail back.
    """
    if now_iso is None:
        now_iso = real_utc_now_iso()
    stats = stats or alpha_b_copy_stats(conn, source_user_id, chain=chain)
    bounded_reason = str(reason or "fomo_follow_failed")[:500]
    action = "approve_refused_fomo_follow"
    _audit_approval(conn, source_user_id, action, actor=actor, reason=bounded_reason,
                    stats=stats, forced=force, now_iso=now_iso, chain=chain)
    conn.commit()
    return {"ok": False, "action": action, "stats": stats, "reason": bounded_reason}


def set_wallet_real_approved(conn, source_user_id, approved, *, chains=None, actor="operator",
                             reason="", force=False, min_closes=None, min_pnl=None, now_iso=None):
    """Real-allowlist change with an append-only audit trail (memecoin_wallet_approvals) and a
    copy-PnL promote gate. Governance fix for the 2026-07-12 finding: the roster went 20->48 with
    no record and 17/48 armed wallets had ZERO alpha_b closes.

    Arming is PER CHAIN as of 2026-07-30: approval is a flag, `chains` is the authority list.
    Passing `chains` on an approve makes arming ATOMIC — the flag and the authority list move
    together, so `real_approved=1` with no chains never persists as a half-armed state. An
    approve without `chains` sets the flag only and the wallet stays armed nowhere.
      - REVOKE: always allowed (auto-demote / operator safety must never be blocked). Clears the
        chain list too, so a later re-approval cannot silently inherit stale authority. Audited.
      - APPROVE: requires >= min_closes alpha_b paper closes AND >= min_pnl cum copy PnL
        (defaults from settings MEMECOIN_REAL_PROMOTE_*). Refusal writes an 'approve_refused'
        audit row and leaves the flag unchanged. `force=True` overrides (operator manual call)
        but the audit row is marked forced.
    Returns {ok, action, stats, reason}. Legacy positional call (conn, uid, False) still works."""
    if now_iso is None:
        now_iso = real_utc_now_iso()
    if not approved:
        stats = alpha_b_copy_stats(conn, source_user_id)
        conn.execute("UPDATE memecoin_wallets SET real_approved=0, real_chains=NULL "
                     "WHERE source_user_id=?", (source_user_id,))
        _audit_approval(conn, source_user_id, "revoke", actor=actor, reason=reason,
                        stats=stats, forced=False, now_iso=now_iso)
        conn.commit()
        return {"ok": True, "action": "revoke", "stats": stats, "reason": reason}
    # When chains are named, EVERY named chain must qualify on its own evidence — this legacy
    # multi-chain path is the second way to grant funded authority, and leaving its gate pooled
    # while the per-chain path was scoped would just move the hole (audit 2026-08-03).
    from sniper.services.memecoin_chains import resolve_chain as _resolve
    gate_chains = [k for k in ((_resolve(c) for c in (chains or []))) if k] or [None]
    preview = None
    for gate_chain in gate_chains:
        preview = real_approval_preview(
            conn, source_user_id, reason=reason, force=force,
            min_closes=min_closes, min_pnl=min_pnl, chain=gate_chain,
        )
        if not preview["ok"]:
            break
    stats = preview["stats"]
    if not preview["ok"]:
        _audit_approval(conn, source_user_id, "approve_refused", actor=actor,
                        reason=preview["reason"],
                        stats=stats, forced=False, now_iso=now_iso)
        conn.commit()
        return {"ok": False, "action": "approve_refused", "stats": stats,
                "reason": preview["reason"]}
    conn.execute("UPDATE memecoin_wallets SET real_approved=1 WHERE source_user_id=?",
                 (source_user_id,))
    _audit_approval(conn, source_user_id, "approve", actor=actor, reason=reason,
                    stats=stats, forced=force, now_iso=now_iso)
    conn.commit()
    if chains is not None:
        # Atomic arm: flag and authority list together, so no half-armed state persists.
        set_wallet_real_chains(conn, source_user_id, chains, actor=actor,
                               reason=reason, now_iso=now_iso)
    return {"ok": True, "action": "approve", "stats": stats, "reason": reason}


def resolve_source_evidence(conn, origin_event_id, *, network_id=None, mint=None,
                            source_user_id=None, attempt=None):
    """Exact source facts; proximity and feed aliases never establish transaction identity."""
    if not origin_event_id:
        return {}
    origin = str(origin_event_id)
    onchain = origin.startswith("onchain:")
    # The lanes that write their source facts into the attempt trace: the Solana slot lane
    # ("slotlane:<signature>") and the EVM fast lanes ("rhfast:<chain>:<tx>:<mint>",
    # "bscfast:<chain>:<tx>:<mint>"). `lane_sig` is the source transaction the facts must name.
    lane_sig = None
    if origin.startswith("slotlane:"):
        lane_sig = origin[len("slotlane:"):]
    elif origin.startswith(("rhfast:", "bscfast:")):
        parts = origin.split(":")
        lane_sig = parts[2] if len(parts) == 4 and parts[2] else ""
    slotlane = lane_sig is not None
    if (onchain or slotlane) and not all((network_id, mint, source_user_id)):
        return {}

    def sized(row):
        try:
            size = float(row.get("their_size_usd"))
        except (TypeError, ValueError, OverflowError):
            size = None
        row["their_size_usd"] = size if size is not None and math.isfinite(size) and size >= 0 else None
        return row

    def matches(row):
        if source_user_id is not None and row.get("source_user_id") != source_user_id:
            return False
        if mint is not None and accounting_mark_key(network_id, mint) != accounting_mark_key(
                row.get("network_id"), row.get("mint")):
            return False
        return network_id is None or normalized_coin_network(network_id, mint) == normalized_coin_network(
            row.get("network_id"), row.get("mint"))

    if onchain:
        try:
            row = conn.execute("SELECT * FROM onchain_wallet_detections WHERE signature=?",
                               (origin[len("onchain:"):],)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: onchain_wallet_detections" not in str(exc):
                raise
            return {}
        row = dict(row) if row else {}
        if not row or not matches(row):
            return {}
        # Historical created_at is detection time, never verified source block time.
        return sized(dict(row, id=None, source_detection_id=row.get("id"), their_size_usd=row.get("source_size_usd"),
                    their_price=row.get("source_price"), source_handle=row.get("handle"),
                    received_at_normalized=row.get("detected_at"),
                    created_at=row.get("detected_at"), source_timestamp_trust="observed_detection"))
    if slotlane:
        if attempt is None:
            attempt = entry_attempt_for_origin(conn, origin)
        if not attempt or attempt.get("origin_event_id") != origin or not matches(attempt):
            return {}
        try:
            facts = _json.loads(attempt.get("trace_json") or "{}").get("source_evidence", {})
        except (ValueError, TypeError, AttributeError):
            return {}
        if (not isinstance(facts, dict) or not lane_sig or facts.get("signature") != lane_sig
                or not matches(facts)):
            return {}
        return sized(facts)
    row = conn.execute("SELECT * FROM memecoin_events WHERE fomo_event_id=?", (origin,)).fetchone()
    row = dict(row) if row else {}
    return sized(row) if row and matches(row) else {}


def resolve_source_size_usd(conn, origin_event_id, *, network_id=None, mint=None, source_user_id=None):
    """Source swap USD at position-open time; absent/unusable stays unknown, recorded zero stays zero."""
    row = resolve_source_evidence(conn, origin_event_id, network_id=network_id,
                                  mint=mint, source_user_id=source_user_id)
    return row.get("their_size_usd")


def resolve_source_lag_s(conn, origin_event_id):
    """Seconds from the source trader's buy to OUR read of it, or None when unknown.

    Same shape and same row as resolve_source_size_usd. The funded executors wrote a literal -1.0
    for every position, so `memecoin_entry_quality` graded all 130 funded rows as missing-lag and
    the funded leg had no entry-speed measurement at all (2026-08-13). The paper copy trader has
    always stored `ev.lag_s` here; this makes the funded leg store the same quantity, so the two
    legs stay comparable.

    A small NEGATIVE lag is real (source clock ahead of ours, seen to -0.78s) and is kept as
    measured. Readers already grade `lag < 0` as missing; inventing a floor here would hide skew.
    """
    if not origin_event_id:
        return None
    row = conn.execute(
        "SELECT lag_s FROM memecoin_events "
        "WHERE fomo_event_id=? AND lag_s IS NOT NULL ORDER BY id LIMIT 1",
        (str(origin_event_id),),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        lag = float(row[0])
    except (TypeError, ValueError):
        return None
    return lag if lag == lag and lag not in (float("inf"), float("-inf")) else None


def resolve_mint_supply(conn, mint, *, network_id=None, lookback=25):
    """Circulating supply implied by the feed: median of market_cap/price over recent priced events.

    `entry_mcap` is NULL on 52% of 4663 real positions (37/71, measured 2026-08-13). The funded lane
    enters on a coin's FIRST signal, so when that event omitted the market cap there is no same-mint
    history for `resolve_mint_mcap` to fall back to, and the display's frozen-ratio derivation
    (mid * entry_mcap/entry_price) has no ratio either — the operator sees a blank market cap for
    the life of the position.

    Supply is the constant those derivations actually want, and ANY priced event for the mint
    yields it. Both sides must be positive: a 0 market cap means MISSING in this feed, never a
    zero-cap coin, and a 0 price would divide. None when the coin has never been priced anywhere.
    """
    if not mint:
        return None
    query = ("SELECT their_market_cap, their_price FROM memecoin_events "
             "WHERE mint=? AND their_market_cap>0 AND their_price>0 ")
    params = [mint]
    if network_id is not None:
        query += f"AND {COIN_NETWORK_SQL}=? "
        params.append(normalized_coin_network(network_id, mint))
    query += "ORDER BY id DESC LIMIT ?"
    params.append(int(lookback))
    ratios = []
    for mc, px in conn.execute(query, params):
        try:
            mc, px = float(mc), float(px)
        except (TypeError, ValueError):
            continue
        if mc > 0 and px > 0:
            ratios.append(mc / px)
    if not ratios:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


_TX_SIGNATURE_RE = None


def event_transaction_signature(event_id):
    """The transaction signature from a VALIDATED feed event format, or None.

    `swap:raybot:<sig>:...`, `onchain:<sig>` and `pump:<sig>:...` carry a base58 signature. Every other
    shape, including `swap:<uuid>`, has no transaction identity. Shared by the S1 feed query and the S2
    activity probe (docs/superpowers/specs/2026-09-17-solana-hourly-floor-activity-design.md).
    """
    global _TX_SIGNATURE_RE
    if _TX_SIGNATURE_RE is None:
        import re
        _TX_SIGNATURE_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")
    parts = str(event_id or "").split(":")
    candidate = None
    if len(parts) >= 3 and parts[0] == "swap" and parts[1] == "raybot":
        candidate = parts[2]
    elif len(parts) >= 2 and parts[0] in ("onchain", "pump"):
        candidate = parts[1]
    return candidate if candidate and _TX_SIGNATURE_RE.match(candidate) else None


_TRADE_ID_RE = None

FEED_DUPLICATE_WINDOW_S = 5.0
FEED_DUPLICATE_USD_RATIO = 0.01


def event_trade_identity(event_id):
    """The S1 trade identity of a feed event: (identity, kind), or None.

    kind is `signature` for a validated transaction signature, or `trade_id` for the J7 event id
    `swap:<uuid>` (spec addendum 2026-09-17). Every other shape has no identity.
    """
    global _TRADE_ID_RE
    signature = event_transaction_signature(event_id)
    if signature is not None:
        return signature, "signature"
    if _TRADE_ID_RE is None:
        import re
        _TRADE_ID_RE = re.compile(r"^swap:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    event_id = str(event_id or "")
    return (event_id, "trade_id") if _TRADE_ID_RE.match(event_id) else None


def solana_prior_feed_buys(conn, mint, *, decision_at, source_signature, source_event_id,
                           window_s=3600.0):
    """S1: eligible prior feed BUYS for one Solana mint, a PARTIAL hourly dollar-volume estimate.

    A row is eligible when it is a Solana buy of this exact mint by a known wallet, with a finite
    positive USD amount, traded in [T - window, T), received before T, with a trade identity
    (`event_trade_identity`). Rows of one identity are one trade; their amounts must be equal. A
    cross-lane duplicate is one trade too: a row pair with at least one trade id, the same wallet, and
    trade times within 5 s; their amounts must agree within 1%. A trade with contradictory amounts
    excludes all its rows. The source is excluded by its identity and by the same duplicate rule. No
    eligible row gives None.
    """
    import math
    from datetime import datetime, timedelta

    def parse(value):
        try:
            out = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
        except (TypeError, ValueError):
            return None
        return out if out.tzinfo is not None else None

    result = {"feed_buy_usd": None, "eligible_count": 0, "duplicate_count": 0,
              "excluded_source_count": 0, "unknown_row_count": 0, "contradictory_count": 0,
              "late_receipt_count": 0, "rows": []}
    decided = parse(decision_at)
    if decided is None or not mint:
        return result
    start = decided - timedelta(seconds=float(window_s))
    fmt = "%Y-%m-%dT%H:%M:%S"
    nodes = []
    for row in conn.execute(
            "SELECT fomo_event_id, source_user_id, source_handle, side, their_size_usd, created_at, "
            "received_at_normalized, received_at, network_id FROM memecoin_events "
            "WHERE mint=? AND created_at>=? AND created_at<? ORDER BY id",
            (mint, (start - timedelta(seconds=1)).strftime(fmt),
             (decided + timedelta(seconds=1)).strftime(fmt))):
        event_id, wallet, handle, side, usd, created_at, received_norm, received_raw, network = tuple(row)
        if normalized_coin_network(network, mint) != "solana":
            continue
        if side == "sell":
            continue
        identity = event_trade_identity(event_id)
        signature = identity[0] if identity and identity[1] == "signature" else None
        traded = parse(created_at)
        try:
            amount = float(usd)
        except (TypeError, ValueError):
            amount = float("nan")
        amount = amount if math.isfinite(amount) and amount > 0 else None
        node = {"event_id": event_id, "wallet": wallet, "handle": handle, "usd": amount,
                "traded": traded, "traded_at": created_at, "received_at": received_norm or received_raw,
                "identity": identity[0] if identity else None,
                "identity_kind": identity[1] if identity else None, "signature": signature,
                "source": False}
        if (source_event_id is not None and event_id == source_event_id) or (
                signature is not None and signature == source_signature):
            # The source: excluded here, kept as an anchor for the duplicate rule.
            result["excluded_source_count"] += 1
            nodes.append({**node, "source": True})
            continue
        if traded is not None and not start <= traded < decided:
            continue
        if side != "buy" or traded is None:
            result["unknown_row_count"] += 1
            continue
        received = parse(received_norm) or parse(received_raw)
        if received is None:
            result["unknown_row_count"] += 1
            continue
        if received >= decided:
            result["late_receipt_count"] += 1
            continue
        if identity is None or not wallet or amount is None:
            result["unknown_row_count"] += 1
            continue
        nodes.append(node)
    # One trade = the rows joined by one identity or by the cross-lane duplicate rule.
    parent = list(range(len(nodes)))
    contradictory = set()

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(nodes):
        for j in range(i + 1, len(nodes)):
            b = nodes[j]
            if a["identity"] is not None and a["identity"] == b["identity"]:
                agree = (a["usd"] is not None and b["usd"] is not None
                         and round(a["usd"], 6) == round(b["usd"], 6))
            elif ("trade_id" in (a["identity_kind"], b["identity_kind"]) and a["wallet"]
                  and a["wallet"] == b["wallet"] and a["traded"] is not None and b["traded"] is not None
                  and abs((a["traded"] - b["traded"]).total_seconds()) <= FEED_DUPLICATE_WINDOW_S):
                agree = (a["usd"] is not None and b["usd"] is not None and abs(a["usd"] - b["usd"])
                         <= FEED_DUPLICATE_USD_RATIO * max(a["usd"], b["usd"]))
            else:
                continue
            root = find(i)
            parent[find(j)] = root
            if not agree:
                contradictory.add(i)
    clusters = {}
    for i in range(len(nodes)):
        clusters.setdefault(find(i), []).append(i)
    bad_roots = {find(i) for i in contradictory}
    for root, members in clusters.items():
        rows = [nodes[i] for i in members]
        others = [r for r in rows if not r["source"]]
        if len(others) < len(rows):
            key = "contradictory_count" if root in bad_roots else "excluded_source_count"
            result[key] += len(others)
            continue
        if root in bad_roots:
            result["contradictory_count"] += len(rows)
            continue
        chosen = next((r for r in rows if r["identity_kind"] == "signature"), rows[0])
        result["eligible_count"] += 1
        result["duplicate_count"] += len(rows) - 1
        result["rows"].append({k: chosen[k] for k in (
            "event_id", "handle", "usd", "traded_at", "received_at", "signature", "identity",
            "identity_kind")} | {"lanes": len(rows)})
    if result["rows"]:
        result["feed_buy_usd"] = sum(r["usd"] for r in result["rows"])
    return result


def _median(values):
    values = sorted(values)
    n = len(values)
    if not n:
        return None
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2.0


def _supply_from_pairs(pairs):
    """Median supply implied by (market cap, price) readings, or None when they disagree.

    SUPPLY IS THE STABLE HALF OF A MARKET CAP (2026-09-18). COINU DDndHihss… was resolved at
    $126,036 while its own chart said $266,708 forty-five seconds earlier: the readings spanned a
    coin that had doubled, so the MEDIAN MCAP described an earlier price. Its implied supply across
    the same readings was 0.89-1.04B — a 15% spread — so price x supply is the number that tracks
    the coin. Readings that disagree by more than a third are refused rather than averaged.
    """
    ratios = []
    for mcap, price in pairs:
        try:
            mcap = float(mcap); price = float(price)
        except (TypeError, ValueError):
            continue
        if mcap > 0 and price > 0 and math.isfinite(mcap) and math.isfinite(price):
            ratios.append(mcap / price)
    if len(ratios) < 3:
        return None
    supply = _median(ratios)
    if not supply or supply <= 0:
        return None
    spread = _median([abs(r - supply) for r in ratios]) / supply
    return supply if spread <= 0.34 else None


# A reading's implied supply (mcap / price) outside this band is a broken reading, not a coin: a price
# in another unit, a mcap on another basis. It holds 94.9% of recent Solana feed readings and 92-100%
# on the EVM chains; the rest are almost all the low tail (2026-10-01, 300k rows).
PRICED_SUPPLY_MIN, PRICED_SUPPLY_MAX = 1e6, 1e13


def _priced_supply(pairs, *, band=True):
    """The median supply implied by the plausible (mcap, price) readings, from one reading up; None
    when none is usable (no price, or outside the band). `band=False` keeps every finite positive
    supply: off the own-mcap EVM chains a reading outside the band is mostly a real coin (4663,
    2026-10-06: 13 of 22 such mints read 3e3-3.6e4 tokens within 20% across readings, e.g. 6,000 at
    ~$1,000), and unknown there sends a blind-chain coin to assumed_nano, past the ceiling.
    No agreement check: three readings that disagree by more than a third are still priced at their
    median supply, where _supply_from_pairs falls back to the median mcap with the price ignored."""
    # ponytail: J7's implied supply is one reading's noise, 0.63e9..1.66e9 on pump coins of ~0.98e9
    # (Gamma, 2026-10-01): one reading can price a coin up to ~1.7x high (refuses) or ~0.6x low
    # (admits). The funded gate prices PROVEN pump mints at 1e9 instead; for other mints the exact
    # fix is the mint-read supply, which is I/O on the fill path (option b, not built).
    supplies = []
    for mcap, price in pairs:
        try:
            supply = float(mcap) / float(price)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if math.isfinite(supply) and supply > 0 and (
                not band or PRICED_SUPPLY_MIN <= supply <= PRICED_SUPPLY_MAX):
            supplies.append(supply)
    return _median(supplies)


def _priced_value(pairs, price, *, band=True):
    """`price` x the readings' priced supply, or None."""
    supply = _priced_supply(pairs, band=band)
    try:
        value = supply * float(price) if supply else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and math.isfinite(value) and value > 0 else None


def resolve_mint_mcap(conn, mint, *, network_id=None, lookback=25, since_iso=None, price=None,
                      priced=False, supply_band=True):
    """Median of recent NON-ZERO market-cap readings for a coin across the feed. Fills the gap
    when an individual event omits their_market_cap (~5% of events) — the same mint is almost always
    priced by other buyers (e.g. ANSEM: 1169 readings agree ~$277M). None if the coin has never been
    priced anywhere (truly unknown). Lets the real mcap gate apply to a resolved value instead of
    fail-closed-skipping a small coin just because THIS event lacked the field.

    `network_id` is mandatory for chain-aware callers: a 0x address is not a coin identity and may
    exist on several EVM chains. Omitting it preserves the legacy aggregate lookup only for old
    callers that do not have chain provenance.

    `priced=True` (the Solana ceiling shadow and the funded gate on every EVM chain): `price` x the
    supply the readings imply, from ONE reading up, or None -- never the median, which with fewer
    than 3 agreeing readings is a stale mcap with the price ignored. `supply_band=False` prices
    readings outside the plausible-supply band too (see _priced_supply). Every other caller gets
    today's rule, so executors' entry_mcap and the exit targets derived from it never move."""
    if not mint:
        return None
    # `since_iso` bounds how far back a reading may come from (operator 2026-08-25: the funded
    # path reads nothing older than a week). `lookback` bounds the ROW COUNT and cannot do this
    # job -- 25 rows on a quiet coin can reach back weeks, and a mcap from a different era is not
    # evidence about this entry.
    query = ("SELECT their_market_cap, their_price FROM memecoin_events "
             "WHERE mint=? AND their_market_cap>0 ")
    params = [mint]
    if since_iso:
        query += "AND created_at>=? "
        params.append(since_iso)
    if network_id is not None:
        query += f"AND {COIN_NETWORK_SQL}=? "
        params.append(normalized_coin_network(network_id, mint))
    query += "ORDER BY created_at DESC LIMIT ?"
    params.append(int(lookback))
    pairs = list(conn.execute(query, params))
    rows = [r[0] for r in pairs]
    if not rows:
        # A coin seen ONLY via follow_activity has no priced memecoin_events row to borrow from
        # (the sole row for that mint is the blank one this call is trying to fill), so the
        # events-only lookup above is circular and returned None for 94% of that lane. The capture
        # tables DO cover it: quote_observations_v2 carries a resolved market_cap for 89% of those
        # mints. Read-only fallback, still no network call on the latency-critical fast lane.
        return _resolve_mcap_from_capture(conn, mint, network_id=network_id,
                                          lookback=lookback, price=price, priced=priced,
                                          supply_band=supply_band)
    if priced:
        return _priced_value(pairs, price, band=supply_band)
    # PRICE x SUPPLY WHEN A PRICE IS GIVEN. A median market cap describes the median PRICE of the
    # window, which on a coin that just moved is a different coin. COINU entered at a resolved
    # $126,036 under the $250k ceiling while price x supply put it at $313,818, above it.
    if price:
        supply = _supply_from_pairs(pairs)
        if supply:
            try:
                derived = supply * float(price)
            except (TypeError, ValueError):
                derived = None
            if derived and math.isfinite(derived) and derived > 0:
                return derived
    return _median(rows)


def _resolve_mcap_from_capture(conn, mint, *, network_id=None, lookback=25, price=None, priced=False,
                               supply_band=True):
    """Median non-zero market cap for `mint` from the forward-capture tables.

    Ordered cheapest-first: quote_observations_v2 (populated by capture_v2 for every candidate
    cohort) then feed_observations_v2 (the full feed retention). Returns None when neither has
    ever priced the coin, so the caller still fails closed on a genuinely unknown mint.
    """
    exact = normalized_coin_network(network_id, mint) if network_id is not None else None
    for table, column, price_column in (
            ("memecoin_quote_observations_v2", "market_cap", "price"),
            ("memecoin_feed_observations_v2", "their_market_cap", "their_price")):
        query = f"SELECT {column}, {price_column} FROM {table} WHERE mint=? AND {column}>0 "
        params = [mint]
        if exact is not None:
            # The RAW-column equality is what lets SQLite use idx_*_coin_time
            # (network_id, mint, observed_at). Filtering only through COIN_NETWORK_SQL hides the
            # index's leading column behind a CASE expression, and `mint` alone is the second
            # column, so neither predicate is usable and the query FULL SCANS the table.
            #
            # Measured 2026-08-23: 576.9 ms per call against 8,471,247 quote rows, on the path the
            # armed fast lane runs PER SWAP and which is documented as DB-only precisely because it
            # is latency-critical. It was the whole of that lane's 0.62 s median dispatch cost.
            # With this predicate the same query plans as SEARCH and runs in 0.02 ms.
            #
            # It is not a semantic change. Both tables store the column already normalized: 0 of
            # 8,471,247 quote rows differ from COIN_NETWORK_SQL, and the only 122 feed rows that
            # differ are unpriced, so the `{column}>0` filter already excluded them. The expression
            # stays as the correctness guard if a future writer ever inserts a raw value.
            query += "AND network_id=? "
            params.append(exact)
            query += f"AND {COIN_NETWORK_SQL}=? "
            params.append(exact)
        query += "ORDER BY observed_at DESC LIMIT ?"
        params.append(int(lookback))
        try:
            pairs = list(conn.execute(query, params))
        except sqlite3.OperationalError:
            continue                      # table not created yet on a fresh DB — try the next one
        rows = [r[0] for r in pairs]
        if rows and priced:
            return _priced_value(pairs, price, band=supply_band)
        if rows and price:
            supply = _supply_from_pairs(pairs)
            if supply:
                try:
                    derived = supply * float(price)
                except (TypeError, ValueError):
                    derived = None
                if derived and math.isfinite(derived) and derived > 0:
                    return derived
        if rows:
            rows.sort()
            n = len(rows)
            return rows[n // 2] if n % 2 else (rows[n // 2 - 1] + rows[n // 2]) / 2.0
    return None


def position_display_supply(conn, position):
    """Token supply that turns a per-token price into a market cap, for DISPLAY only (operator,
    2026-09-28: "34k, not 0.0000343"). The pinned total supply when the position has one; else
    market cap over price from ONE stored mark (the same read), the rule the range bar uses live.
    None when neither exists: the caller then shows the price.

    NOT entry_mcap / their_price: those two come from different reads on most lanes. Against the
    pinned supply that ratio was off by more than 5% on 25 of 44 follow_activity positions and 3 of
    4 on-chain ones since 09-01 (p10 0.83, p90 1.13), and on closed rows without a pinned supply it
    ranged 0.26x to 2.9x the mark's own supply (Alpha's check, 2026-09-28).

    The pinned supply is position_cap_supply's: fdv_supply, then the display-only on-chain total
    (2026-10-02), so a 56 coin reads $221K as GMGN does, not $175K on a 796M implied supply."""
    position = position or {}
    pinned = position_cap_supply(position)[0]
    if pinned:
        return pinned
    if conn is None or not position.get("mint"):
        return None
    row = conn.execute(
        "SELECT mcap, mid FROM memecoin_position_marks WHERE chain_id=? AND mint=? "
        "AND mcap>0 AND mid>0 ORDER BY sweep_ts DESC LIMIT 1",
        (str(position.get("network_id") or "solana"), position["mint"])).fetchone()
    return (float(row[0]) / float(row[1])) if row else None


def resolve_mint_ticker(conn, mint, *, network_id, include_feed=True):
    """Latest non-empty local ticker for one exact ``(network_id, mint)`` identity.

    `include_feed=False` stops after memecoin_events (indexed on mint): the feed-observation
    fallback cannot use an index for this match and costs up to ~0.9 s on a miss, too slow for a
    display poll."""
    if not mint or network_id is None:
        return None
    row = conn.execute(
        "SELECT ticker FROM memecoin_events "
        f"WHERE mint=? AND {COIN_NETWORK_SQL}=? "
        "AND ticker IS NOT NULL AND trim(ticker)<>'' "
        "ORDER BY COALESCE(received_at_normalized,received_at,created_at) DESC, id DESC LIMIT 1",
        (mint, normalized_coin_network(network_id, mint)),
    ).fetchone()
    if row and str(row[0]).strip():
        return str(row[0]).strip()
    if not include_feed:
        return None
    # Same circularity as resolve_mint_mcap: a follow_activity-only coin has no ticker'd event
    # row to borrow. feed_observations_v2 retains the whole feed and carries the symbol.
    try:
        row = conn.execute(
            "SELECT ticker FROM memecoin_feed_observations_v2 "
            f"WHERE mint=? AND {COIN_NETWORK_SQL}=? "
            "AND ticker IS NOT NULL AND trim(ticker)<>'' "
            "ORDER BY observed_at DESC, id DESC LIMIT 1",
            (mint, normalized_coin_network(network_id, mint)),
        ).fetchone()
    except sqlite3.OperationalError:
        return None                       # table not created yet on a fresh DB
    return str(row[0]).strip() if row and str(row[0]).strip() else None


def resolve_mint_their_price(conn, mint, *, network_id=None, source_user_id=None):
    """Return the observed BUY price for the exact trader/chain being copied.

    A mint alone is not a coin identity on EVM, and another trader's later buy is not the pending
    entry's reference price. Optional filters preserve compatibility for research callers, while
    funded executors always provide both. Observation time wins over the source-claimed timestamp.
    """
    if not mint:
        return None
    query = ("SELECT their_price FROM memecoin_events "
             "WHERE mint=? AND side='buy' AND their_price>0 ")
    params = [mint]
    if network_id is not None:
        query += f"AND {COIN_NETWORK_SQL}=? "
        params.append(normalized_coin_network(network_id, mint))
    if source_user_id is not None:
        query += "AND source_user_id=? "
        params.append(str(source_user_id))
    query += ("ORDER BY COALESCE(received_at_normalized,received_at,created_at) DESC, id DESC "
              "LIMIT 1")
    row = conn.execute(query, params).fetchone()
    return (row[0] if row else None) or None


def latest_network_for_mint(conn, mint, source_user_id=None):
    """Return the feed network only when it is unambiguous, else None.

    A 0x address is not self-identifying and can exist on Base, Robinhood 4663, BSC, etc. Routing a
    manual real buy to whichever chain happened to post most recently can spend from the wrong
    wallet. Restrict to the requested source when provided, then accept exactly one distinct chain."""
    if not mint:
        return None
    query = ("SELECT DISTINCT network_id FROM memecoin_events "
             "WHERE mint=? AND COALESCE(network_id,'')!='' ")
    params = [mint]
    if source_user_id:
        query += "AND source_user_id=? "
        params.append(source_user_id)
    rows = [r[0] for r in conn.execute(query, params) if r[0]]
    return rows[0] if len(rows) == 1 else None


def claim_first_source_buy(conn, *, source_user_id, mint, source_signature, source_slot,
                           cohort_start, observed_at):
    """Keep the first observed source transaction even when its entry is refused or restarted."""
    key = (source_user_id, "solana", mint, cohort_start)
    sql = ("SELECT source_signature,source_slot,observed_at FROM memecoin_first_source_buys "
           "WHERE source_user_id=? AND network_id=? AND mint=? AND cohort_start=?")
    row = conn.execute(sql, key).fetchone()
    slot_sql = "SELECT source_slot FROM memecoin_source_buy_slots WHERE source_signature=?"
    candidate = conn.execute(slot_sql, (source_signature,)).fetchone()
    if row is not None and candidate is not None:
        return dict(zip(("source_signature", "source_slot", "observed_at"), row),
                    candidate_source_slot=candidate[0])
    if conn.in_transaction:
        raise RuntimeError("first source claim requires an idle connection")
    # Preserve known opportunities from before policy activation; the existing coin index bounds
    # the read. A historical unknown slot still consumes its first signature conservatively.
    previous = None
    if row is None:
        import json
        from types import SimpleNamespace
        from sniper.services.memecoin_source_initiation import source_initiation_refusal
        # Each indexed source contributes at most 16 proof checks plus a budget sentinel. If a
        # sentinel sorts before a verified candidate, unseen earlier history keeps admission held.
        history_limit = 16
        history = conn.execute(
            "SELECT * FROM ("
            "SELECT * FROM (SELECT signature,source_slot,detected_at AS seen,address AS wallet, "
            "CASE WHEN json_valid(source_valuation_json) THEN "
            "json_extract(source_valuation_json,'$.source_initiation') END AS proof_json, "
            "'onchain:'||signature AS origin,'onchain_watch' AS source, "
            "'observed_detection' AS trust,0 AS kind "
            "FROM onchain_wallet_detections WHERE source_user_id=? AND mint=? "
            "AND network_id='solana' AND detected_at>=? "
            "AND julianday(detected_at)>=julianday(?) ORDER BY detected_at,id LIMIT ?) "
            "UNION ALL SELECT * FROM (SELECT "
            "CASE WHEN origin_event_id LIKE 'swap:raybot:%' THEN substr(origin_event_id,13) "
            "WHEN origin_event_id LIKE 'slotlane:%' THEN substr(origin_event_id,10) "
            "ELSE substr(origin_event_id,9) END AS signature, "
            "CASE WHEN json_valid(trace_json) THEN COALESCE("
            "json_extract(trace_json,'$.source_evidence.source_slot'),"
            "json_extract(trace_json,'$.source_evidence.source_block_number')) END AS source_slot, "
            "decision_started_at AS seen, "
            "CASE WHEN json_valid(trace_json) THEN "
            "json_extract(trace_json,'$.source_evidence.source_wallet') END AS wallet, "
            "CASE WHEN json_valid(trace_json) THEN "
            "json_extract(trace_json,'$.source_evidence.source_initiation') END AS proof_json, "
            "origin_event_id AS origin, "
            "CASE WHEN origin_event_id LIKE 'slotlane:%' THEN 'slot_lane' ELSE 'onchain_watch' END AS source, "
            "source_timestamp_trust AS trust,1 AS kind FROM memecoin_entry_attempts "
            "WHERE network_id='solana' AND mint=? AND source_user_id=? "
            "AND decision_started_at>=? AND julianday(decision_started_at)>=julianday(?) "
            "AND (origin_event_id LIKE 'slotlane:%' OR origin_event_id LIKE 'onchain:%' "
            "OR origin_event_id LIKE 'swap:raybot:%') "
            "AND COALESCE(delivery_lane,'')<>'j7_shadow' "
            "AND COALESCE(terminal_status,'')<>'shadow' "
            "AND COALESCE(signal_source,'')<>'manual' "
            "ORDER BY decision_started_at,attempt_id LIMIT ?)) ORDER BY julianday(seen)",
            (source_user_id, mint, str(cohort_start)[:19], cohort_start, history_limit + 1,
             mint, source_user_id, str(cohort_start)[:19], cohort_start, history_limit + 1)).fetchall()
        checked = {0: 0, 1: 0}
        for signature, old_slot, seen, wallet, proof_json, origin, source, trust, kind in history:
            checked[kind] += 1
            if checked[kind] > history_limit:
                raise RuntimeError('first source historical proof budget exhausted')
            try:
                proof = json.loads(proof_json) if proof_json else None
                if not isinstance(proof, dict):
                    continue
                event = SimpleNamespace(source_user_id=source_user_id, source_wallet=wallet, mint=mint,
                    source_block_number=old_slot if old_slot is not None else proof.get('block_number'),
                    source_initiation=proof, fomo_event_id=origin, signal_source=source,
                    source_timestamp_trust=trust)
                if source_initiation_refusal(event, 'solana') is not None:
                    continue
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            # Proof may bind a missing historical slot, but must never upgrade its deadline.
            previous = (signature, old_slot if type(old_slot) is int and old_slot > 0 else None, seen)
            break
    signature, slot, seen = tuple(previous) if previous is not None else (
        source_signature, source_slot, observed_at)
    try:
        conn.execute("INSERT OR IGNORE INTO memecoin_source_buy_slots "
                     "(source_signature,source_slot,observed_at) VALUES(?,?,?)",
                     (source_signature, source_slot, observed_at))
        conn.execute(
            "INSERT OR IGNORE INTO memecoin_first_source_buys "
            "(source_user_id,network_id,mint,cohort_start,source_signature,source_slot,observed_at) "
            "VALUES(?,?,?,?,?,?,?)", (*key, signature, slot, seen))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(sql, key).fetchone()
    if row is None:
        raise RuntimeError("first source claim was not persisted")
    candidate = conn.execute(slot_sql, (source_signature,)).fetchone()
    return dict(zip(("source_signature", "source_slot", "observed_at"), row),
                candidate_source_slot=candidate[0])


def wallet_real_cohort_start(conn, source_user_id, network_id=None, *, include_initial=False):
    """When the wallet's CURRENT funded authority began, or None for 'never revoked -> lifetime'.

    A wallet that was demoted and re-approved starts a new funded cohort at the re-approval: the
    losses that caused the demotion are the reason it was revoked, and re-approving is the operator
    accepting that history. Without this, `wallet_real_record` kept summing them and the guardrail
    re-fired on the very NEXT close whatever its outcome — jester sat at -$101.23 against a -$100
    floor after its 2026-07-22 re-approval, so any close at all would have re-demoted it.

    Deliberately anchored to the first approve AFTER the latest revoke, never to the latest approve:
    a redundant approve on a wallet that was never demoted must NOT zero its loss counter, or the
    guardrail could be reset indefinitely by re-clicking approve. Never revoked -> None -> unchanged
    lifetime behaviour.

    Set `include_initial` to return the first arm when no revoke exists. The funded backlog gate
    uses this option. The demotion guard keeps the lifetime default."""
    from sniper.services.memecoin_chains import canonical_chain_id
    chain = canonical_chain_id(network_id) if network_id else None
    if chain:
        # Per-(wallet, chain) cohort (2026-07-30): a chain re-armed after its own demotion
        # starts clean, so the losses that caused it cannot re-fire the guardrail. A
        # whole-wallet revoke still ends every chain's cohort.
        revoke_sql = ("SELECT MAX(id) FROM memecoin_wallet_approvals WHERE source_user_id=? "
                      "AND ((action='chain_revoke' AND chain=?) OR action='revoke')")
        arm_sql = ("SELECT created_at FROM memecoin_wallet_approvals WHERE source_user_id=? "
                   "AND ((action='chain_arm' AND chain=?) OR action='approve') AND id>? "
                   "ORDER BY id LIMIT 1")
        last_revoke = conn.execute(revoke_sql, (source_user_id, chain)).fetchone()[0]
        if last_revoke is None and not include_initial:
            return None
        row = conn.execute(
            arm_sql, (source_user_id, chain, last_revoke or 0),
        ).fetchone()
    else:
        last_revoke = conn.execute(
            "SELECT MAX(id) FROM memecoin_wallet_approvals WHERE source_user_id=? "
            "AND action IN ('revoke','chain_revoke')",
            (source_user_id,)).fetchone()[0]
        if last_revoke is None and not include_initial:
            return None
        row = conn.execute(
            "SELECT created_at FROM memecoin_wallet_approvals WHERE source_user_id=? "
            "AND action IN ('approve','chain_arm') AND id>? ORDER BY id LIMIT 1",
            (source_user_id, last_revoke or 0)).fetchone()
    started = row["created_at"] if row else None
    if not started:
        return None
    # An unparseable anchor must not silently disable the guardrail -> fall back to lifetime.
    if conn.execute("SELECT julianday(?)", (started,)).fetchone()[0] is None:
        return None
    return started


# --- trial arm (spec docs/superpowers/specs/2026-10-01-memecoin-trial-arm-design.md) ---------------
# A malformed contract reads as "no marker", never as an error: these run on booking and grading.
_TRIAL_ID_SQL = ("(CASE WHEN json_valid(funded_contract_json) "
                 "THEN json_extract(funded_contract_json, '$.source.trial.trial_id') END)")


def _trial_chain(chain):
    from sniper.services.memecoin_chains import canonical_chain_id
    return canonical_chain_id(chain) or str(chain or "").strip().lower()


def _rows_as_dicts(cur):
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def start_trial(conn, *, source_user_id, chain, clip_usd, rule, actor, now_iso, commit=True):
    """Insert one open trial; the partial UNIQUE index refuses a second open one (IntegrityError)."""
    cur = conn.execute(
        "INSERT INTO memecoin_trial_arms (source_user_id, chain, clip_usd, rule, started_at, actor) "
        "VALUES (?,?,?,?,?,?)",
        (source_user_id, _trial_chain(chain), float(clip_usd), rule, now_iso, actor))
    if commit:
        conn.commit()
    return cur.lastrowid


def open_trial(conn, source_user_id, chain):
    """The open trial of (wallet, chain), or None. One read on the partial unique index."""
    rows = _rows_as_dicts(conn.execute(
        "SELECT * FROM memecoin_trial_arms WHERE source_user_id=? AND chain=? AND ended_at IS NULL",
        (source_user_id, _trial_chain(chain))))
    return rows[0] if rows else None


def open_trials(conn):
    return _rows_as_dicts(conn.execute(
        "SELECT * FROM memecoin_trial_arms WHERE ended_at IS NULL ORDER BY id"))


def delete_trial(conn, trial_id):
    """Undo a start whose arm was refused (spec 5.1 step 3). Only a trial with no position."""
    conn.execute("DELETE FROM memecoin_trial_arms WHERE id=? AND positions=0", (trial_id,))
    conn.commit()


def stop_trial(conn, trial_id, *, now_iso):
    """The trial takes no more entries; it is still open until ended."""
    conn.execute("UPDATE memecoin_trial_arms SET stopped_at=COALESCE(stopped_at, ?) "
                 "WHERE id=? AND ended_at IS NULL", (now_iso, trial_id))
    conn.commit()


def end_trial(conn, trial_id, *, now_iso, reason, commit=True):
    cur = conn.execute(
        "UPDATE memecoin_trial_arms SET ended_at=?, end_reason=?, stopped_at=COALESCE(stopped_at, ?) "
        "WHERE id=? AND ended_at IS NULL", (now_iso, reason, now_iso, trial_id))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def trial_book_count(conn):
    """Open trial positions plus unresolved trial reservations, across every trial. Reads only the
    open real positions and the reserved rows, the rows the capacity snapshot reads."""
    return conn.execute(
        "SELECT (SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
        "AND " + _TRIAL_ID_SQL + " IS NOT NULL) + (SELECT COUNT(*) FROM memecoin_pending_entries "
        "WHERE status='reserved' AND " + _TRIAL_ID_SQL + " IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM memecoin_positions p WHERE p.is_open=1 AND p.execution_mode='real' "
        "AND p.attempt_id=memecoin_pending_entries.attempt_id "
        "AND memecoin_pending_entries.attempt_id IS NOT NULL))").fetchone()[0]


def trial_reserved_count(conn, trial_id):
    """Unresolved reservations carrying this trial's marker."""
    return conn.execute(
        "SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
        "AND " + _TRIAL_ID_SQL + "=? AND NOT EXISTS (SELECT 1 FROM memecoin_positions p "
        "WHERE p.is_open=1 AND p.execution_mode='real' "
        "AND p.attempt_id=memecoin_pending_entries.attempt_id "
        "AND memecoin_pending_entries.attempt_id IS NOT NULL)", (int(trial_id),)).fetchone()[0]


def link_trial_position(conn, position_id, funded_contract_json):
    """Count one booked trial position, once (spec 5.2, Gamma CLEAN note). Keyed by position id, so
    a retried or recovered booking of the same position leaves the count unchanged. No commit: it
    rides the booking's own transaction. A contract without a marker does nothing."""
    try:
        trial = (_json.loads(funded_contract_json).get("source") or {}).get("trial") \
            if funded_contract_json else None
    except (TypeError, ValueError, AttributeError):
        return
    if not isinstance(trial, dict) or trial.get("trial_id") is None:
        return
    tid = int(trial["trial_id"])
    conn.execute("INSERT OR IGNORE INTO memecoin_trial_positions (position_id, trial_id) "
                 "VALUES (?,?)", (int(position_id), tid))
    conn.execute("UPDATE memecoin_trial_arms SET positions=(SELECT COUNT(*) FROM "
                 "memecoin_trial_positions WHERE trial_id=?) WHERE id=?", (tid, tid))


def trial_closes(conn, trial):
    """The trial's closes by the contract marker (spec section 3): real, closed, realized PnL known,
    not a manual entry. Manual EXITS stay in, marked by exit_reason."""
    return _rows_as_dicts(conn.execute(
        "SELECT id AS position_id, mint, realized_pnl, entry_notional, closed_at, exit_reason, "
        "opened_at, origin_event_id, network_id FROM memecoin_positions "
        "WHERE source_user_id=? AND execution_mode='real' AND is_open=0 "
        "AND realized_pnl IS NOT NULL AND COALESCE(signal_source,'')!='manual' "
        "AND " + _TRIAL_ID_SQL + "=? ORDER BY closed_at, id",
        (trial["source_user_id"], int(trial["id"]))))


def trial_closes_linked(conn, trial):
    """`trial_closes`, read through the trial's own position links (idx_trial_positions_trial)
    instead of every closed position of the wallet (Codex P2, 2026-10-15): the two-of-three rule
    asks this inside a reservation's BEGIN IMMEDIATE, where the scan held the writer lock 4.1 ms
    on a 10,000-close wallet against 0.010 ms. Same rows, same order, same filters: every booked
    position with a trial marker is linked in its own booking transaction (link_trial_position),
    and the marker and identity checks below still decide membership."""
    return _rows_as_dicts(conn.execute(
        "SELECT p.id AS position_id, p.mint, p.realized_pnl, p.entry_notional, p.closed_at, "
        "p.exit_reason, p.opened_at, p.origin_event_id, p.network_id "
        # CROSS JOIN fixes SQLite's join order (links first): left to choose, the planner drove
        # from idx_mc_pos_mode_open_closed over every closed real position, i.e. the scan again.
        "FROM memecoin_trial_positions t CROSS JOIN memecoin_positions p ON p.id=t.position_id "
        "WHERE t.trial_id=? AND p.source_user_id=? AND p.execution_mode='real' AND p.is_open=0 "
        "AND p.realized_pnl IS NOT NULL AND COALESCE(p.signal_source,'')!='manual' "
        "AND " + _TRIAL_ID_SQL + "=? ORDER BY p.closed_at, p.id",
        (int(trial["id"]), trial["source_user_id"], int(trial["id"]))))


def trial_checkpoints_done(conn, trial_id):
    return {int(c) if str(c).isdigit() else c for (c,) in conn.execute(
        "SELECT checkpoint FROM memecoin_trial_verdicts WHERE trial_id=?", (trial_id,))}


def record_trial_verdict(conn, *, trial_id, checkpoint, checked_at, closes, mean_r, verdict,
                         action, detail_json, commit=True):
    """Write a verdict once per (trial, checkpoint). True when this call wrote it."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_trial_verdicts (trial_id, checkpoint, checked_at, closes, "
        "mean_r, verdict, action, detail_json) VALUES (?,?,?,?,?,?,?,?)",
        (trial_id, str(checkpoint), checked_at, closes, mean_r, verdict, action, detail_json))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def source_buy_at(conn, origin_event_id):
    """The source's own buy time for a position's origin event, or None."""
    if not origin_event_id:
        return None
    row = conn.execute("SELECT created_at FROM memecoin_events WHERE fomo_event_id=?",
                       (origin_event_id,)).fetchone()
    return row[0] if row else None


def buys_of_mint(conn, network_id, mint):
    """[(source_user_id, created_at)] of every recorded buy of one coin (idx_mc_events_mint)."""
    return conn.execute(
        f"SELECT source_user_id, created_at FROM memecoin_events WHERE mint=? AND side='buy' "
        f"AND {COIN_NETWORK_SQL}=?", (mint, normalized_coin_network(network_id, mint))).fetchall()


def wallet_real_record(conn, source_user_id, network_id=None):
    """Real realized PnL + current consecutive-loss streak for ONE source wallet over its CURRENT
    funded cohort — the auto-demote guardrail's inputs (spec §4). Streak counts back from the latest
    close. Cohort per `wallet_real_cohort_start`: lifetime unless the wallet was revoked and
    re-approved, in which case only closes at/after the re-approval count.

    `network_id` scopes the cohort to ONE chain (2026-07-30). Authority callers must pass it, so a
    wallet losing on 4663 is judged — and demoted — on 4663 alone. Omitting it keeps the
    cross-chain lifetime view, which is display-only and must never drive a demotion."""
    # TRIAL CLOSES ARE NOT IN THE COHORT (trial spec section 5.5, operator 2026-10-01): the trial
    # rule is their demotion rule, and four straight losses at a 31-36% per-close SD would end a
    # trial before the rule's first check. After the trial, the cohort is the non-trial closes.
    q = ("SELECT realized_pnl FROM memecoin_positions WHERE source_user_id=? AND is_open=0 "
         "AND execution_mode='real' AND realized_pnl IS NOT NULL AND " + _TRIAL_ID_SQL + " IS NULL "
         "AND " + WALLET_GRADE_SQL)
    params = [source_user_id]
    if network_id:
        q += " AND network_id=?"
        params.append(str(network_id).strip().lower())
    cohort_start = wallet_real_cohort_start(conn, source_user_id, network_id=network_id)
    if cohort_start:
        # A missing/unparseable closed_at stays IN the cohort — the guardrail fails closed.
        q += " AND (julianday(closed_at)>=julianday(?) OR julianday(closed_at) IS NULL)"
        params.append(cohort_start)
    rows = conn.execute(q + " ORDER BY closed_at DESC, id DESC", params).fetchall()
    pnls = [float(r[0]) for r in rows]
    streak = 0
    for p in pnls:
        if p < 0:
            streak += 1
        else:
            break
    return {"realized_total": round(sum(pnls), 2), "consecutive_losses": streak,
            "closed_count": len(pnls)}


def set_wallet_discovery_source(conn, source_user_id, discovery_source):
    """Record where a wallet was discovered (leaderboard | large_buy | manual). Set at
    promotion time so forward copy-PnL can be attributed to a discovery pool. Only fills
    a NULL (first discovery wins; a manual re-add doesn't overwrite a real provenance)."""
    conn.execute("UPDATE memecoin_wallets SET discovery_source=? "
                 "WHERE source_user_id=? AND discovery_source IS NULL",
                 (discovery_source, source_user_id))
    conn.commit()


def get_wallet_by_handle(conn, handle):
    r = conn.execute(
        "SELECT * FROM memecoin_wallets WHERE handle=?", (handle,)).fetchone()
    return dict(r) if r else None


def upsert_trader_analytics(conn, row):
    """Insert-or-replace one trader's analytics row. Only known columns present in
    `row` are written (missing -> NULL/default). source_user_id is required."""
    cols = [c for c in TRADER_ANALYTICS_COLUMNS if c in row]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "source_user_id")
    conflict = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    sql = (f"INSERT INTO memecoin_trader_analytics ({','.join(cols)}) "
           f"VALUES ({placeholders}) "
           f"ON CONFLICT(source_user_id) {conflict}")
    conn.execute(sql, [row[c] for c in cols])
    conn.commit()


def list_trader_analytics(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_trader_analytics ORDER BY realized_pnl_usd DESC")]


def get_trader_analytics(conn, uid):
    r = conn.execute(
        "SELECT * FROM memecoin_trader_analytics WHERE source_user_id=?", (uid,)).fetchone()
    return dict(r) if r else None


def _lb_f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def ingest_leaderboard(conn, entries, now_iso):
    """Ingest fomo's top-N leaderboard: upsert the snapshot table, stamp each trader's
    analytics row with fomo lifetime PnL / rank, and seed non-curated traders into the
    discovery funnel (source='leaderboard'). Existing candidate rows keep their status
    and first-seen provenance. Returns {ranked, new_candidates}."""
    curated = curated_user_ids(conn)
    # clear stale flags first — a trader who dropped out of the top-N must not keep showing
    # "on leaderboard rank K" forever (the flag was write-once and never reset).
    conn.execute("UPDATE memecoin_trader_analytics SET on_leaderboard=0, leaderboard_rank=NULL "
                 "WHERE on_leaderboard=1")
    new_candidates = 0
    for rank, e in enumerate(entries, start=1):
        uid = e.get("id")
        if not uid:
            continue
        handle = e.get("userHandle") or e.get("displayName")
        top = e.get("topHoldings") or []
        unreal = sum(_lb_f(h.get("pnl")) for h in top)
        # topHoldings is a "top" slice vs the separate totalHoldings count — if the trader holds
        # more tokens than are listed, our unrealized sum is PARTIAL (understated), which could
        # false-GREEN the bag-check. Flag it so the gate doesn't silently trust the partial sum.
        unreal_partial = 1 if len(top) < int(_lb_f(e.get("totalHoldings"))) else 0
        conn.execute(
            """INSERT INTO memecoin_leaderboard
                 (source_user_id, handle, rank, total_pnl, unrealized_pnl, total_volume,
                  total_holdings, num_trades, swap_count, followers, snapshot_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_user_id) DO UPDATE SET
                 handle=excluded.handle, rank=excluded.rank, total_pnl=excluded.total_pnl,
                 unrealized_pnl=excluded.unrealized_pnl, total_volume=excluded.total_volume,
                 total_holdings=excluded.total_holdings, num_trades=excluded.num_trades,
                 swap_count=excluded.swap_count, followers=excluded.followers,
                 snapshot_ts=excluded.snapshot_ts""",
            (uid, handle, rank, _lb_f(e.get("totalPnL")), unreal, _lb_f(e.get("totalVolume")),
             int(_lb_f(e.get("totalHoldings"))), int(_lb_f(e.get("numTrades"))),
             int(_lb_f(e.get("swapCount"))), int(_lb_f(e.get("followers"))), now_iso))
        upsert_trader_analytics(conn, {
            "source_user_id": uid, "handle": handle,
            "fomo_total_pnl": _lb_f(e.get("totalPnL")), "fomo_unrealized_pnl": unreal,
            "fomo_unrealized_partial": unreal_partial,
            "on_leaderboard": 1, "leaderboard_rank": rank})
        if uid not in curated:
            cur = conn.execute(
                "INSERT OR IGNORE INTO memecoin_candidates(source_user_id, handle, "
                "sighting_count, total_buy_usd, max_buy_usd, first_seen, last_seen, "
                "status, source) VALUES (?,?,1,0,0,?,?, 'new','leaderboard')",
                (uid, handle, now_iso, now_iso))
            new_candidates += cur.rowcount
    conn.commit()
    return {"ranked": len(entries), "new_candidates": new_candidates}


def list_leaderboard(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_leaderboard ORDER BY rank")]


def get_leaderboard_entry(conn, uid):
    r = conn.execute(
        "SELECT * FROM memecoin_leaderboard WHERE source_user_id=?", (uid,)).fetchone()
    return dict(r) if r else None


def _parse_iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def _shift_iso(s, seconds):
    dt = _parse_iso(s)
    if not dt:
        return s
    return (dt + timedelta(seconds=seconds)).isoformat()


def normalize_open_position_hold_stamps(conn, *, offset_s=None, now_iso=None,
                                        min_offset_s=300.0, tolerance_s=3600.0):
    """One-time transition repair for positions opened before hold stamps moved to real-UTC.

    Old copy rows used host time for opened_at/peak_ts/last_quote_ts. The host->real offset is
    learned from fomo's Date header. We only shift OPEN rows not previously normalized, and only
    when event provenance shows opened_at is about `offset_s` away from the fomo created_at.
    Manual/no-provenance rows are left alone unless they are copy-like (entry_lag_s>=0).
    """
    if offset_s is None:
        from sniper.util.clock import offset_s as _offset_s
        offset_s = _offset_s()
    try:
        offset = float(offset_s or 0.0)
    except (TypeError, ValueError):
        return 0
    if abs(offset) < float(min_offset_s):
        return 0
    if now_iso is None:
        from sniper.util.clock import real_utc_now_iso
        now_iso = real_utc_now_iso()

    cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_positions)")}
    if "hold_clock_normalized_at" not in cols:
        conn.execute("ALTER TABLE memecoin_positions ADD COLUMN hold_clock_normalized_at TEXT")
    shifted = 0
    cur = conn.execute("""
        SELECT p.id, p.opened_at, p.peak_ts, p.last_quote_ts, p.origin_event_id,
               p.entry_lag_s, p.signal_source, e.created_at AS event_created_at
        FROM memecoin_positions p
        LEFT JOIN memecoin_events e ON e.fomo_event_id = p.origin_event_id
        WHERE p.is_open=1
          AND p.hold_clock_normalized_at IS NULL
    """)
    names = [d[0] for d in cur.description]
    for r in cur.fetchall():
        row = dict(r) if hasattr(r, "keys") else dict(zip(names, r))
        opened = _parse_iso(row.get("opened_at"))
        if not opened:
            continue
        event_created = _parse_iso(row.get("event_created_at"))
        is_legacy_host = False
        if event_created:
            gap = (opened - event_created).total_seconds()
            is_legacy_host = abs(gap - offset) <= tolerance_s
        elif row.get("origin_event_id") is None and (row.get("entry_lag_s") or -1) >= 0:
            # Legacy copy rows before origin_event_id existed have no event join, but manual buys
            # use entry_lag_s=-1. Treat only copy-like no-origin rows as host-domain candidates.
            is_legacy_host = True
        if not is_legacy_host:
            continue
        conn.execute(
            "UPDATE memecoin_positions SET opened_at=?, peak_ts=?, last_quote_ts=?, "
            "hold_clock_normalized_at=? WHERE id=? AND hold_clock_normalized_at IS NULL",
            (_shift_iso(row.get("opened_at"), -offset),
             _shift_iso(row.get("peak_ts"), -offset),
             _shift_iso(row.get("last_quote_ts"), -offset),
             now_iso, row["id"]))
        shifted += 1
    if shifted:
        conn.commit()
    return shifted


def record_pnl_snapshot(conn, uid, row, snapshot_ts):
    """Store one effective corrected-UTC calendar-day snapshot per trader."""
    values = (row.get("realized_pnl_24h"), row.get("n_closed_24h"),
              row.get("unrealized_pnl_usd"), row.get("live_holdings_value_usd"),
              row.get("fomo_total_pnl"))
    day = str(snapshot_ts)[:10]
    existing = conn.execute(
        "SELECT id FROM memecoin_trader_pnl_snapshots "
        "WHERE source_user_id=? AND substr(snapshot_ts,1,10)=? "
        "ORDER BY snapshot_ts DESC,id DESC LIMIT 1", (uid, day)).fetchone()
    if existing:
        conn.execute(
            "UPDATE memecoin_trader_pnl_snapshots SET snapshot_ts=?, "
            "realized_24h=COALESCE(?,realized_24h), "
            "n_closed_24h=COALESCE(?,n_closed_24h), "
            "unrealized_pnl_usd=COALESCE(?,unrealized_pnl_usd), "
            "live_holdings_value_usd=COALESCE(?,live_holdings_value_usd), "
            "fomo_total_pnl=COALESCE(?,fomo_total_pnl) WHERE id=?",
            (snapshot_ts, *values, existing[0]))
    else:
        conn.execute(
            "INSERT INTO memecoin_trader_pnl_snapshots(source_user_id, snapshot_ts, realized_24h, "
            "n_closed_24h, unrealized_pnl_usd, live_holdings_value_usd, fomo_total_pnl) "
            "VALUES (?,?,?,?,?,?,?)", (uid, snapshot_ts, *values))
    conn.commit()


def latest_snapshot_day(conn):
    row = conn.execute(
        "SELECT MAX(substr(snapshot_ts,1,10)) FROM memecoin_trader_pnl_snapshots"
    ).fetchone()
    return row[0] if row and row[0] else None


def batch_snapshots(conn, uids):
    """All PnL snapshots for a set of uids in ONE query -> {uid: [rows]} (asc by ts). Lets the
    combined/candidates/gated endpoints compute every trader's windows without an N+1 per row."""
    out = {}
    if not uids:
        return out
    uids = list(uids)
    qs = ",".join("?" for _ in uids)
    for r in conn.execute(
        f"SELECT source_user_id, snapshot_ts, realized_24h, n_closed_24h, fomo_total_pnl "
        f"FROM memecoin_trader_pnl_snapshots WHERE source_user_id IN ({qs}) ORDER BY snapshot_ts",
        uids):
        out.setdefault(r["source_user_id"], []).append(dict(r))
    return out


def batch_analytics(conn, uids):
    """Full analytics rows for a set of uids in ONE query -> {uid: row}."""
    out = {}
    if not uids:
        return out
    uids = list(uids)
    qs = ",".join("?" for _ in uids)
    for r in conn.execute(
        f"SELECT * FROM memecoin_trader_analytics WHERE source_user_id IN ({qs})", uids):
        out[r["source_user_id"]] = dict(r)
    return out


def windowed_realized(conn, uid, days, now_iso, snaps=None):
    """Sum the daily realized_24h tiles over the last `days`. One tile per CALENDAR DAY
    (latest snapshot that day) so a >1/day cadence never double-counts. saturated=1 if any
    counted day's round-trips consumed ~the whole 100-swap fetch window (>= SWAP_LIMIT/2
    round-trips ≈ >= SWAP_LIMIT swap legs), meaning older same-day trades are unseen and that
    day's realized_24h may undercount. (The old >=100 round-trip threshold was UNREACHABLE —
    n_closed is bounded by the 100-swap fetch cap — so the flag could never fire.)"""
    from sniper.settings.memecoin import MEMECOIN_ANALYZER_SWAP_LIMIT
    sat_floor = max(1, MEMECOIN_ANALYZER_SWAP_LIMIT // 2)
    now = _parse_iso(now_iso)
    cutoff = now - timedelta(days=days) if now else None
    rows = snaps if snaps is not None else conn.execute(
        "SELECT snapshot_ts, realized_24h, n_closed_24h FROM memecoin_trader_pnl_snapshots "
        "WHERE source_user_id=? ORDER BY snapshot_ts", (uid,)).fetchall()
    by_day = {}
    for r in rows:
        t = _parse_iso(r["snapshot_ts"])
        if not t or (cutoff and t < cutoff) or (now and t > now):
            continue
        if r["realized_24h"] is None:
            continue
        by_day[t.date()] = (r["realized_24h"], r["n_closed_24h"])   # latest per day wins (ordered asc)
    realized = sum((v[0] or 0.0) for v in by_day.values())
    saturated = 1 if any((v[1] or 0) >= sat_floor for v in by_day.values()) else 0
    return {"realized": round(realized, 2), "n_days": len(by_day),
            "saturated": saturated, "days": days}


def fomo_pnl_delta(conn, uid, days, now_iso, snaps=None):
    """Cadence-independent lifetime-PnL delta for leaderboard traders: latest fomo_total_pnl
    minus the earliest snapshot at/after (now-days). delta=None until history spans the window."""
    now = _parse_iso(now_iso)
    cutoff = now - timedelta(days=days) if now else None
    src = snaps if snaps is not None else conn.execute(
        "SELECT snapshot_ts, fomo_total_pnl FROM memecoin_trader_pnl_snapshots "
        "WHERE source_user_id=? AND fomo_total_pnl IS NOT NULL ORDER BY snapshot_ts",
        (uid,)).fetchall()
    rows = [r for r in src if r["fomo_total_pnl"] is not None]
    valid = [r for r in rows if _parse_iso(r["snapshot_ts"])
             and (not now or _parse_iso(r["snapshot_ts"]) <= now)]
    if len(valid) < 2:
        return {"delta": None, "from_pnl": None, "to_pnl": None, "days": days, "span_days": 0.0}
    to = valid[-1]
    # anchor = latest snapshot at/before the window start; else earliest available
    before = [r for r in valid if cutoff and _parse_iso(r["snapshot_ts"]) <= cutoff]
    frm = before[-1] if before else valid[0]
    if frm["snapshot_ts"] == to["snapshot_ts"]:
        return {"delta": None, "from_pnl": None, "to_pnl": None, "days": days, "span_days": 0.0}
    # span_days = the ACTUAL elapsed window the delta covers. When there's no anchor at/before
    # the window start, the delta spans LESS than `days` — the caller must not read it as a full
    # `days` figure (matches _win_realized's don't-guess honesty).
    span = (_parse_iso(to["snapshot_ts"]) - _parse_iso(frm["snapshot_ts"])).total_seconds() / 86400.0
    return {"delta": round(to["fomo_total_pnl"] - frm["fomo_total_pnl"], 2),
            "from_pnl": frm["fomo_total_pnl"], "to_pnl": to["fomo_total_pnl"],
            "days": days, "span_days": round(span, 2)}


def funded_quote_for_origin(conn, origin_event_id):
    """The route quote the FUNDED path bought for this event, or None.

    The paper fan-out runs AFTER the funded hook, so buying its own quote costs it the funded
    path's whole decision: measured on MANLETS, 2.738 s and 11.75% of entry price. The funded
    attempt row is persisted first — 229 of 230 origins since 2026-09-23T20:58Z, p50 gap 0.00 s —
    so the price is already on disk by the time the arm needs it, and reading it costs one indexed
    lookup instead of a second Jupiter call.

    ⚠️ MOST EVENTS HAVE NONE, AND THAT IS NOT AN ERROR. Only 6 of those 229 attempts carry a
    `quote_price`, because a refusal usually never reaches the quote stage — and refusals are the
    population this measurement is for. The caller falls back to the arm's own single quote.
    """
    if not origin_event_id:
        return None
    row = conn.execute(
        "SELECT quote_price, quote_observed_at FROM memecoin_entry_attempts "
        "WHERE origin_event_id=? AND quote_price IS NOT NULL "
        "ORDER BY decision_started_at DESC LIMIT 1", (str(origin_event_id),)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        price = float(row[0])
    except (TypeError, ValueError):
        return None
    return {"price": price, "observed_at": row[1]} if price > 0 else None


def funded_clip_for_origin(conn, origin_event_id):
    """The USD the FUNDED entry actually spent on this event, or None.

    The paper arm is flat at `MEMECOIN_TRADE_SIZE_USD` while funded re-sizes by bankroll inside the
    entry path: measured over 206 funded positions in 7 days, solana medians $175.00 against 4663's
    $39.81 and 56's $68.30. Same signal, different money, so dollar PnL was never comparable.

    ⚠️ READ FROM THE POSITION, NOT FROM THE FUNDED PATH. The clip appears in no result dict, no
    trace and no attempt column, so surfacing it at the source would mean editing the funded entry
    path for a measurement. The position row already holds what was spent and is written before the
    fan-out runs — 73 of 74 matched origins in the same window — so this costs one indexed read and
    touches nothing that decides money.

    ⚠️ ONLY A FILL HAS ONE. A refusal never sized, and refusals are most of this study, so the
    caller falls back to the configured base and labels which it used.
    """
    if not origin_event_id:
        return None
    row = conn.execute(
        "SELECT entry_notional FROM memecoin_positions "
        "WHERE origin_event_id=? AND execution_mode='real' AND entry_notional IS NOT NULL "
        "ORDER BY id DESC LIMIT 1", (str(origin_event_id),)).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        usd = float(row[0])
    except (TypeError, ValueError):
        return None
    return {"usd": usd} if usd > 0 else None


def open_paper_coin_identities(conn, *, strategy=None):
    """The distinct coins the paper lane holds open, as `coin_identity` tuples.

    This is what the open-coin cap counts, and it is deliberately coins and not positions: the exit
    sweep prices one batched map keyed by coin identity, so a second position on a coin already open
    costs no provider call.
    """
    sql = ("SELECT DISTINCT network_id, mint FROM memecoin_positions "
           "WHERE execution_mode='paper' AND is_open=1")
    args = []
    if strategy:
        sql += " AND strategy=?"
        args.append(str(strategy))
    return {coin_identity(row[0], row[1]) for row in conn.execute(sql, tuple(args))}


def open_position_user_ids(conn, mode="paper"):
    """Wallets that still hold an open position — eligible for mirror-exit even if
    paused (so an unfollowed wallet's positions still close when the trader sells)."""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT source_user_id FROM memecoin_positions WHERE is_open=1 AND execution_mode=?",
        (mode,))}


def insert_firehose_events(conn, events, curated_ids=None, armed_ids=None):
    """Append-only retention of the ENTIRE feed (every trader, every side, no size floor).
    Until 2026-07-11 we kept only curated trades + >$1k candidate buys and discarded the rest —
    which made lead-lag ranks biased (candidates only visible above the floor), made coin
    episodes unreconstructable, and blocked own-entry (cluster/consensus) research. Dedup by
    fomo_event_id; one executemany per poll. `curated_ids`/`armed_ids` stamp POINT-IN-TIME
    cohort membership (the roster mutates; a later join lies). Returns rows actually inserted."""
    if not events:
        return 0
    curated_ids = curated_ids if curated_ids is not None else set()
    armed_ids = armed_ids if armed_ids is not None else set()
    cur = conn.executemany(
        "INSERT OR IGNORE INTO memecoin_firehose(fomo_event_id, source_user_id, source_handle, "
        "mint, ticker, side, their_price, their_market_cap, their_size_usd, is_first_buy, "
        "created_at, received_at, network_id, was_curated, was_real_approved) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(e.get("fomo_event_id"), e.get("source_user_id"), e.get("source_handle"),
          e.get("mint"), e.get("ticker"), e.get("side"), e.get("their_price"),
          e.get("their_market_cap"), e.get("their_size_usd"),
          1 if e.get("is_first_buy") else 0, e.get("created_at"), e.get("received_at"),
          e.get("network_id"),
          1 if e.get("source_user_id") in curated_ids else 0,
          1 if e.get("source_user_id") in armed_ids else 0) for e in events])
    conn.commit()
    return cur.rowcount


def _feed_observation_v2_params(observation, was_curated, was_real_approved):
    row = observation if isinstance(observation, dict) else observation.__dict__
    raw = row.get("raw_payload") or {}
    return (
        row.get("upstream_event_id"), row.get("raw_type"), row.get("normalized_side"),
        row.get("source_user_id"), row.get("source_handle"), row.get("network_id"),
        row.get("mint"), row.get("ticker"), row.get("their_price"),
        row.get("their_market_cap"), row.get("their_size_usd"), row.get("source_ts_raw"),
        row.get("source_ts"), row.get("observed_at"), row.get("lag_s"),
        1 if was_curated else 0, 1 if was_real_approved else 0,
        _json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )


_FEED_OBSERVATION_V2_INSERT = (
    "INSERT OR IGNORE INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, "
    "normalized_side, source_user_id, source_handle, network_id, mint, ticker, their_price, "
    "their_market_cap, their_size_usd, source_ts_raw, source_ts, observed_at, lag_s, "
    "was_curated, was_real_approved, raw_json, capture_version, clock_domain) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,2,'observed_real_utc')"
)


def insert_feed_observation_v2(conn, observation, was_curated, was_real_approved):
    """Insert one pre-normalization upstream observation with point-in-time cohort flags."""
    cur = conn.execute(
        _FEED_OBSERVATION_V2_INSERT,
        _feed_observation_v2_params(observation, was_curated, was_real_approved),
    )
    conn.commit()
    return cur.rowcount > 0


def insert_feed_observations_v2(conn, observations, curated_ids, armed_ids):
    """Bulk v2 capture: one transaction per poll, with membership frozen at poll receipt."""
    params = [
        _feed_observation_v2_params(
            observation,
            observation.source_user_id in curated_ids,
            observation.source_user_id in armed_ids,
        )
        for observation in observations
    ]
    if not params:
        return 0
    cur = conn.executemany(_FEED_OBSERVATION_V2_INSERT, params)
    conn.commit()
    return cur.rowcount


def _explicit_remaining_usd(raw_json, raw_type):
    """Return an explicitly reported post-trade USD holding without unit fallbacks."""
    try:
        raw = _json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except (TypeError, ValueError):
        return None
    body = raw.get("body") if isinstance(raw, dict) else None
    if not isinstance(body, dict):
        return None
    if "currentSizeUsd" in body:
        value = body.get("currentSizeUsd")
    elif raw_type == "single_user_sell" and "humanUsdAmount" in body:
        value = body.get("humanUsdAmount")
    else:
        return None
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < -1e-6:
        return None
    return max(0.0, value)


def upsert_holder_evidence(conn, *, network_id, mint, source_user_id, source_handle,
                           evidence_state, remaining_usd, observed_at, source,
                           upstream_event_id, confirmation_state, successful_poll_at,
                           invalidated_at, invalidation_reason, updated_at, clock_domain,
                           source_event_at, source_timestamp_trust, evidence_hash, commit=True):
    if evidence_state not in ("positive", "zero", "unknown"):
        raise ValueError(f"invalid holder evidence state: {evidence_state}")
    # WO-16 point-in-time corpus. Positive/zero facts are retained append-only; unknown remains
    # fail-closed in the current mirror without manufacturing non-information history.
    if evidence_state != "unknown":
        conn.execute(
            "INSERT OR IGNORE INTO memecoin_holder_evidence_history(network_id,mint,source_user_id,"
            "source_handle,evidence_state,remaining_usd,observed_at,source,upstream_event_id,"
            "confirmation_state,successful_poll_at,invalidated_at,invalidation_reason,recorded_at,"
            "clock_domain,source_event_at,source_timestamp_trust,evidence_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (network_id, mint, source_user_id, source_handle, evidence_state, remaining_usd,
             observed_at, source, upstream_event_id, confirmation_state, successful_poll_at,
             invalidated_at, invalidation_reason, updated_at, clock_domain, source_event_at,
             source_timestamp_trust, evidence_hash),
        )
    conn.execute(
        "INSERT INTO memecoin_holder_evidence(network_id, mint, source_user_id, source_handle, "
        "evidence_state, remaining_usd, observed_at, source, upstream_event_id, "
        "confirmation_state, successful_poll_at, invalidated_at, invalidation_reason, updated_at, "
        "clock_domain, source_event_at, source_timestamp_trust, evidence_hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(network_id, mint, source_user_id) DO UPDATE SET "
        "source_handle=COALESCE(excluded.source_handle, memecoin_holder_evidence.source_handle), "
        "evidence_state=excluded.evidence_state, "
        "remaining_usd=excluded.remaining_usd, observed_at=excluded.observed_at, "
        "source=excluded.source, upstream_event_id=excluded.upstream_event_id, "
        "confirmation_state=excluded.confirmation_state, "
        "successful_poll_at=COALESCE(excluded.successful_poll_at, "
        "memecoin_holder_evidence.successful_poll_at), invalidated_at=excluded.invalidated_at, "
        "invalidation_reason=excluded.invalidation_reason, updated_at=excluded.updated_at, "
        "clock_domain=excluded.clock_domain, source_event_at=excluded.source_event_at, "
        "source_timestamp_trust=excluded.source_timestamp_trust, "
        "evidence_hash=excluded.evidence_hash "
        "WHERE julianday(excluded.observed_at) >= "
        "julianday(memecoin_holder_evidence.observed_at)",
        (network_id, mint, source_user_id, source_handle, evidence_state, remaining_usd,
         observed_at, source, upstream_event_id, confirmation_state, successful_poll_at,
         invalidated_at, invalidation_reason, updated_at, clock_domain, source_event_at,
         source_timestamp_trust, evidence_hash),
    )
    if commit:
        conn.commit()


def mark_holder_poll_success(conn, source_user_id, observed_at, *, commit=True):
    conn.execute(
        "UPDATE memecoin_holder_evidence SET successful_poll_at=?, updated_at=? "
        "WHERE source_user_id=?",
        (observed_at, observed_at, source_user_id),
    )
    if commit:
        conn.commit()


def source_remaining_usd_for_event(conn, upstream_event_id, *, source_user_id, network_id, mint):
    """Exact-coin sell-event holding, preserving explicit zero as a full exit."""
    if not upstream_event_id or not source_user_id or not mint:
        return None
    network = normalized_coin_network(network_id, mint)
    row = conn.execute(
        "SELECT raw_type, raw_json FROM memecoin_feed_observations_v2 "
        "WHERE upstream_event_id=? AND source_user_id=? AND network_id=? AND mint=?",
        (str(upstream_event_id), source_user_id, network, mint),
    ).fetchone()
    if row is None:
        return None
    return _explicit_remaining_usd(row["raw_json"], row["raw_type"])


def source_trades_for_coin(conn, source_user_id, mint, network_id=None):
    """One wallet's recorded buys and sells of one coin, oldest first: dicts with fomo_event_id,
    side, their_price, their_size_usd, created_at. Rows tagged with another chain are excluded;
    untagged rows are kept (the fomo-era feed often omitted the chain). Read-only."""
    if not source_user_id or not mint:
        return []
    network = normalized_coin_network(network_id, mint) if network_id else None
    rows = conn.execute(
        "SELECT fomo_event_id, side, their_price, their_size_usd, created_at, network_id "
        "FROM memecoin_events WHERE source_user_id=? AND mint=? AND side IN ('buy','sell') "
        "ORDER BY created_at, id", (source_user_id, mint)).fetchall()
    out = []
    for r in rows:
        tag = r["network_id"]
        if network and tag and normalized_coin_network(tag, mint) != network:
            continue
        out.append({"fomo_event_id": r["fomo_event_id"], "side": r["side"],
                    "their_price": r["their_price"], "their_size_usd": r["their_size_usd"],
                    "created_at": r["created_at"]})
    return out


def active_armed_holders_for_coin(conn, *, network_id, mint, exclude_user_id, observed_at,
                                  window_s, min_remaining_usd):
    """Latest fresh explicit holdings for other currently active armed wallets on one exact coin."""
    if not mint or not observed_at:
        return []
    try:
        window_s = float(window_s)
        min_remaining_usd = float(min_remaining_usd)
    except (TypeError, ValueError):
        return []
    if not (math.isfinite(window_s) and window_s > 0
            and math.isfinite(min_remaining_usd) and min_remaining_usd > 0):
        return []
    network = normalized_coin_network(network_id, mint)
    rows = conn.execute(
        "SELECT o.source_user_id, o.source_handle, o.raw_type, o.raw_json, o.observed_at "
        "FROM memecoin_feed_observations_v2 o "
        "JOIN memecoin_wallets w ON w.source_user_id=o.source_user_id "
        "WHERE o.network_id=? AND o.mint=? AND o.source_user_id<>? "
        "AND o.normalized_side IN ('buy','sell') "
        "AND w.real_approved=1 AND w.paused=0 AND w.excluded=0 "
        "AND julianday(o.observed_at)<=julianday(?) "
        "AND julianday(o.observed_at)>=julianday(?) - (? / 86400.0) "
        "ORDER BY o.source_user_id, julianday(o.observed_at) DESC, o.id DESC",
        (network, mint, exclude_user_id or "", observed_at, observed_at, window_s),
    ).fetchall()
    holders = []
    seen = set()
    for row in rows:
        uid = row["source_user_id"]
        if uid in seen:
            continue
        seen.add(uid)
        remaining = _explicit_remaining_usd(row["raw_json"], row["raw_type"])
        if remaining is None or remaining < min_remaining_usd:
            continue
        holders.append({
            "source_user_id": uid,
            "source_handle": row["source_handle"],
            "remaining_usd": remaining,
            "observed_at": row["observed_at"],
        })
    return holders


def paper_path_state(conn, position_ids, basis):
    """{position_id: (rows, last t_offset_s, high, low)} of each position's stored paper path."""
    ids = [int(p) for p in position_ids]
    if not ids:
        return {}
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute(
        "SELECT position_id, COUNT(*), MAX(t_offset_s), MAX(price), MIN(price) "
        "FROM memecoin_paper_path WHERE basis=? AND position_id IN (%s) GROUP BY position_id"
        % ",".join("?" * len(ids)), (basis, *ids))}


def insert_paper_path_rows(conn, rows):
    """rows: (position_id, network_id, mint, basis, source, t_offset_s, observed_at, price, kind).
    A second write of the same (position, basis, observed_at) is ignored. Returns rows written."""
    if not rows:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO memecoin_paper_path(position_id, network_id, mint, basis, source, "
        "t_offset_s, observed_at, price, kind) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return cur.rowcount


def record_quote_observations(conn, price_map, now_iso, source="dexscreener"):
    """Dense quote ledger writer: one row per (mint, minute) from a sweep's batched price map.
    UNIQUE(mint, ts_minute) dedupes the ~15s sweep cadence to minute resolution for free.
    quality='suspect' marks prints the pump guard would reject (>1000x moves are unit
    mismatches) so the exit search can filter without losing the row."""
    rows = []
    minute = (now_iso or "")[:16]
    for key, st in (price_map or {}).items():
        is_tuple = isinstance(key, tuple) and len(key) == 2
        mint = key[1] if is_tuple else key
        network_id = key[0] if is_tuple else None   # NULL for legacy bare-mint keys — never defaulted
        price = (st or {}).get("price")
        if not price or price <= 0:
            continue
        # Honest provenance: prefer the per-row source, including historical retired providers;
        # fall back to the scalar default for source-less rows.
        row_source = (st or {}).get("source") or source
        rows.append((mint, minute, now_iso, float(price), (st or {}).get("liquidity"),
                     row_source, None, network_id))
    if not rows:
        return 0
    cur = conn.executemany(
        "INSERT OR IGNORE INTO memecoin_quote_ledger(mint, ts_minute, ts, price, liquidity, "
        "source, quality, network_id) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return cur.rowcount


def set_paper_tick_json(conn, position_id, value, *, commit=True):
    """The paper tick engine's binding and hold accounting for one PAPER position. Never a real
    row (the WHERE says so)."""
    conn.execute("UPDATE memecoin_positions SET paper_tick_json=? WHERE id=? "
                 "AND execution_mode='paper'", (value, int(position_id)))
    if commit:
        conn.commit()


def record_exit_decision(conn, position_id, value, *, commit=True):
    """The latest exit decision's mark (JSON) for one position, so a closed row carries the one
    that closed it (a de-risk or a refused attempt is overwritten by the close that follows).
    Measurement only: no decision reads this column."""
    conn.execute("UPDATE memecoin_positions SET exit_decision_json=? WHERE id=?",
                 (value, int(position_id)))
    if commit:
        conn.commit()


def record_executed_pool(conn, position_id, pool, source, *, commit=True):
    """Stamp the pool a fill actually touched. WRITE-ONCE, descriptive, never a decision input.

    Write-once because the executed pool is a fact about ONE transaction: an exit through a
    different venue must not overwrite what the entry did. A row that already carries a pool is
    left alone, so a later read can never silently become a different pool's evidence.
    """
    if not position_id or not pool or not source:
        return False
    cur = conn.execute(
        "UPDATE memecoin_positions SET executed_pool=?, executed_pool_source=? "
        "WHERE id=? AND executed_pool IS NULL", (str(pool), str(source), int(position_id)))
    if commit:
        conn.commit()
    return cur.rowcount > 0


def record_exit_trigger_audit(conn, *, position_id, reason, display_price, preview_usdc,
                              allowed, detail, checked_at, lane=None, key=None, replace=True):
    """Keep one latest executable-trigger decision per position/reason/minute.

    `key` replaces the minute in that identity for a lane whose every decision must keep its own
    row (the source-sell mirror passes its decision time, 2026-09-27). The column is still named
    checked_minute. `replace=False` preserves a durable source intent another worker already owns.

    `lane` names the LOOP that decided, not the price source. The two are different questions and
    the mark's source cannot answer this one: a periodic sweep and a websocket wake can both read
    the same swap mark, and only the lane says which of them acted on it.
    """
    conn.execute(
        "INSERT INTO memecoin_exit_trigger_audit(position_id, reason, checked_minute, "
        "display_price, preview_usdc, allowed, detail, checked_at, lane) "
        "VALUES (?,?,?,?,?,?,?,?,?) " + (
        "ON CONFLICT(position_id, reason, checked_minute) DO UPDATE SET "
        "display_price=excluded.display_price, preview_usdc=excluded.preview_usdc, "
        "allowed=excluded.allowed, detail=excluded.detail, checked_at=excluded.checked_at, "
        "lane=excluded.lane" if replace else "ON CONFLICT DO NOTHING"),
        (position_id, reason, str(checked_at)[:16] if key is None else str(key), display_price,
         preview_usdc, 1 if allowed else 0, detail, checked_at, lane),
    )
    conn.commit()


def compare_exit_trigger_audit_detail(conn, *, position_id, reason, key, previous_detail,
                                      detail, checked_at, allowed=None):
    """Transition one exact durable audit item without overwriting a concurrent owner's state."""
    cur = conn.execute(
        "UPDATE memecoin_exit_trigger_audit SET detail=?,checked_at=?,allowed=COALESCE(?,allowed) "
        "WHERE position_id=? AND reason=? AND checked_minute=? AND detail=?",
        (detail, checked_at, None if allowed is None else int(bool(allowed)),
         position_id, reason, key, previous_detail))
    conn.commit()
    return cur.rowcount == 1


def pending_onchain_sell_mirrors(conn, network_id):
    """Source rows of new durable mirror intents, including a closed owner awaiting final audit."""
    return _rows(conn.execute(
        "WITH intents AS (SELECT *, CASE WHEN json_valid(detail) THEN detail ELSE '{}' END AS parsed "
        "FROM memecoin_exit_trigger_audit WHERE reason='mirror') "
        "SELECT s.*,a.position_id AS mirror_position_id FROM intents a "
        "JOIN memecoin_positions p ON p.id=a.position_id "
        "JOIN onchain_evm_sell_shadow s ON s.id=json_extract(a.parsed,'$.sell_row_id') "
        "WHERE s.network_id=? AND p.network_id=s.network_id AND p.execution_mode='real' "
        "AND p.source_user_id=s.source_user_id AND lower(p.mint)=lower(s.mint) "
        "AND a.checked_minute='onchain_sell:'||lower(s.tx_hash) "
        "AND json_extract(a.parsed,'$.mirror_state') IN ('pending','executing') "
        "ORDER BY s.id", (str(network_id),)))


def exit_decisions(conn, position_ids):
    """{position_id: decision} for CLOSED positions: what decided the exit, for display.

    The decision is the latest ALLOWED audit row of the position's own exit rail at or before its
    close. A price rail reports the exact whole-bag route PnL it confirmed (`decision_basis`
    "route"); vol_death reports its on-chain proof ("onchain"). The row's display_price is the
    market mid the screens showed at that instant (2026-09-27: 65922 showed -2.0% while the exact
    quote that stopped it read -35.4%), so it comes back labelled with its basis, source and age
    when the runner recorded them, and unlabelled (None) on rows written before it did. The
    `<rail>_execution` row, when present, adds the execution-time re-quote. Display only.
    """
    ids = list(position_ids)
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    pos = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT id, exit_reason, closed_at, entry_price FROM memecoin_positions "
        f"WHERE id IN ({marks}) AND is_open=0", ids)}
    out = {}
    for pid, reason, display_price, detail, checked_at, lane in conn.execute(
            "SELECT position_id, reason, display_price, detail, checked_at, lane "
            "FROM memecoin_exit_trigger_audit WHERE allowed=1 "
            f"AND position_id IN ({marks}) ORDER BY position_id, checked_at, id", ids):
        exit_reason, closed_at, entry_price = pos.get(pid, (None, None, None))
        if not exit_reason or (closed_at and str(checked_at) > str(closed_at)):
            continue
        try:
            parsed = _json.loads(detail or "{}")
        except (TypeError, ValueError):
            parsed = None
        parsed = parsed if isinstance(parsed, dict) else None
        if reason == f"{exit_reason}_execution":
            if pid in out and parsed and parsed.get("executable_unit_pnl_pct") is not None:
                out[pid]["execution_pnl_pct"] = parsed["executable_unit_pnl_pct"]
            continue
        if reason != exit_reason:
            continue
        exact = parsed.get("executable_unit_pnl_pct") if parsed else None
        shown = None
        try:
            if display_price and entry_price:
                shown = (float(display_price) / float(entry_price) - 1.0) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            shown = None
        out[pid] = {
            "rail": reason, "lane": lane, "decided_at": checked_at,
            "decision_basis": "route" if exact is not None else (
                "onchain" if reason == "vol_death" else None),
            "decision_pnl_pct": exact,
            "decision_detail": None if parsed else detail,
            "execution_pnl_pct": None,
            "display_pnl_pct": shown,
            **{k: (parsed or {}).get(k) for k in (
                "display_basis", "display_source", "display_changed_at", "display_checked_at",
                "display_age_s")},
        }
    return out


def latest_mirror_decisions(conn, position_ids):
    """The runner's latest copied-wallet sell decision per position.

    Returns {position_id: {**detail, "allowed", "checked_at"}}. A position with no decision has no
    key. A malformed detail keeps `allowed` and `checked_at` only.
    """
    ids = list(position_ids)
    if not ids:
        return {}
    out = {}
    for pid, allowed, detail, checked_at in conn.execute(
            "SELECT position_id, allowed, detail, checked_at FROM memecoin_exit_trigger_audit "
            "WHERE reason='mirror' AND lane='mirror_on_sell' "
            f"AND position_id IN ({','.join('?' * len(ids))}) "
            "ORDER BY position_id, checked_at, id", ids):
        try:
            parsed = _json.loads(detail or "{}")
        except (TypeError, ValueError):
            parsed = {}
        out[pid] = {**(parsed if isinstance(parsed, dict) else {}),
                    "allowed": bool(allowed), "checked_at": checked_at}
    return out


def insert_event(conn, ev):
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_events(fomo_event_id, source_user_id, source_handle, "
        "mint, ticker, side, their_price, their_market_cap, their_size_usd, is_first_buy, "
        "created_at, received_at, lag_s, signal_source, delivery_lane, network_id, "
        "received_at_normalized, clock_domain) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ev["fomo_event_id"], ev["source_user_id"], ev["source_handle"], ev["mint"],
         ev["ticker"], ev["side"], ev["their_price"], ev["their_market_cap"],
         ev["their_size_usd"], 1 if ev.get("is_first_buy") else 0,
         ev["created_at"], ev["received_at"], ev["lag_s"],
         ev.get("signal_source") or "global_feed", ev.get("delivery_lane"),
         ev.get("network_id") or None,
         ev.get("received_at_normalized"), ev.get("clock_domain") or "legacy_unknown"))
    handle = str(ev.get("source_handle") or "").strip()
    if handle:
        conn.execute(
            "UPDATE memecoin_wallets SET handle=? WHERE source_user_id=? "
            "AND TRIM(COALESCE(handle,''))=''",
            (handle, ev.get("source_user_id")),
        )
    conn.commit()
    return cur.rowcount > 0


def open_position(conn, *, source_user_id, source_handle, mint, ticker, entry_price,
                  entry_notional, entry_fill_source, entry_lag_s, their_size_usd,
                  entry_liquidity=None, entry_token_age_s=None, entry_mcap=None, opened_at,
                  signal_source="global_feed", strategy="control", origin_event_id=None,
                  execution_mode="paper", network_id=None, assigned_policy_id=None,
                  assigned_policy_version=None, assigned_policy_json=None,
                  their_price=None, fill_gap_pct=None, attempt_id=None,
                  price_attribution=None, quote_price=None, quote_observed_at=None,
                  quote_age_ms=None, quote_id=None, quote_route_target=None,
                  source_drift_pct=None, execution_slippage_pct=None,
                  total_copy_drift_pct=None, opened_recorded_at=None,
                  funded_contract_version=None, funded_contract_hash=None,
                  funded_contract_json=None, paper_cohort_id=None,
                  paper_cohort_json=None, experiment_status=None,
                  cluster_decision_id=None, lifi_dependent=False, evm_route_json=None,
                  commit=True):
    # execution_mode is the paper/real seam: the paper exit sweep and the real exit sweep read
    # DISJOINT sets (mode-scoped readers below). Validate at the write path so a bad tag can never
    # let the paper sweep modeled-close a real position.
    if execution_mode not in ("paper", "real"):
        raise ValueError(f"execution_mode must be 'paper' or 'real', got {execution_mode!r}")
    # freeze the trader's hold band as-of-open (point-in-time; reproducible across re-screens)
    from sniper.analysis.memecoin_holdband import band_of
    hrow = conn.execute("SELECT median_hold_s FROM memecoin_trader_analytics WHERE source_user_id=?",
                        (source_user_id,)).fetchone()
    hold_band = band_of(hrow[0] if hrow else None)
    opened_recorded_at = opened_recorded_at or real_utc_now_iso()
    if experiment_status is None and execution_mode == "paper":
        experiment_status = "legacy_unversioned"
    tp_target_price = None
    if execution_mode == "real" and funded_contract_json is not None:
        from sniper.services.memecoin_funded_policy import frozen_policy_from_position
        from sniper.services.memecoin_hot_tp import tp_target_from
        try:
            frozen = frozen_policy_from_position({
                "mint": mint,
                "network_id": network_id,
                "funded_contract_version": funded_contract_version,
                "funded_contract_hash": funded_contract_hash,
                "funded_contract_json": funded_contract_json,
            })
        except ValueError:
            frozen = None
        if frozen is not None:
            tp_target_price = tp_target_from(entry_price, frozen.tp_pct)
    desk_meta = (_desk_contract_metadata(funded_contract_json)
                 if signal_source == SIGNAL_SOURCE_DESK and execution_mode == "real" else None)
    values = (
        source_user_id, source_handle, mint, ticker, entry_price, entry_notional,
        entry_fill_source, entry_lag_s, their_size_usd, entry_liquidity,
        entry_token_age_s, entry_mcap, assigned_policy_id, assigned_policy_version,
        assigned_policy_json, 1, opened_at, signal_source, strategy, entry_price, opened_at, 0,
        hold_band, origin_event_id, execution_mode, network_id, their_price, fill_gap_pct,
        attempt_id, price_attribution, quote_price, quote_observed_at, quote_age_ms, quote_id,
        quote_route_target, source_drift_pct, execution_slippage_pct, total_copy_drift_pct,
        opened_recorded_at, funded_contract_version, funded_contract_hash, funded_contract_json,
        paper_cohort_id, paper_cohort_json, experiment_status, tp_target_price,
        cluster_decision_id, 1 if lifi_dependent else 0, evm_route_json,
        *(desk_meta.get(k) if desk_meta else None for k in
          ("cohort_id", "producer", "opportunity_id", "snapshot_hash")),
    )
    cur = conn.execute(
        "INSERT INTO memecoin_positions(source_user_id, source_handle, mint, ticker, "
        "entry_price, entry_notional, entry_fill_source, entry_lag_s, their_size_usd, "
        "entry_liquidity, entry_token_age_s, entry_mcap, assigned_policy_id, "
        "assigned_policy_version, assigned_policy_json, is_open, opened_at, signal_source, strategy, "
        "peak_price, peak_ts, no_new_high_sweeps, hold_band, origin_event_id, execution_mode, "
        "network_id, their_price, fill_gap_pct, attempt_id, price_attribution, quote_price, "
        "quote_observed_at, quote_age_ms, quote_id, quote_route_target, source_drift_pct, "
        "execution_slippage_pct, total_copy_drift_pct, opened_recorded_at, "
        "funded_contract_version, funded_contract_hash, funded_contract_json, "
        "paper_cohort_id, paper_cohort_json, experiment_status, tp_target_price, "
        "cluster_decision_id, lifi_dependent, evm_route_json, "
        "desk_cohort_id, desk_producer, desk_opportunity_id, desk_snapshot_hash) "
        f"VALUES ({','.join('?' for _ in values)})", values)
    if desk_meta:
        _mark_desk_execution_cost(conn, cur.lastrowid, f"entry:{cur.lastrowid}", opened_at)
        conn.execute("UPDATE memecoin_desk_cohorts SET first_fill_at=COALESCE(first_fill_at,?) "
                     "WHERE cohort_id=?", (opened_at, desk_meta["cohort_id"]))
    if execution_mode == "real" and funded_contract_json:
        # The trial positions counter (trial spec 5.2). Bookkeeping must never fail a booking.
        try:
            link_trial_position(conn, cur.lastrowid, funded_contract_json)
        except Exception:
            logger.exception("trial position link failed position=%s", cur.lastrowid)
    if commit:
        conn.commit()
    return cur.lastrowid


def first_seen_buy_price(conn, network_id, mint, *, since_iso=None):
    """The price at which OUR OWN feed first showed this exact (network_id, mint) as a buy —
    the anchor for the real path's first-seen chase guard. Earliest arrival (id order), positive
    prices only; sells never count. None = never seen (guard fails open).

    `since_iso` bounds how far back the anchor may come from. Unbounded, this drifted stale as the
    event history grew: measured 2026-08-06, the anchoring event behind 104 refusals was a median
    26.7 hours old (max 23 days), so the guard was comparing today's price to a price from another
    day and calling the difference a chase. An anchor older than the window means the move is not
    recent, so there is nothing to chase and the guard fails open — same contract as never-seen."""
    params = [mint, normalized_coin_network(network_id, mint)]
    window = ""
    if since_iso:
        window = "AND created_at >= ? "
        params.append(since_iso)
    row = conn.execute(
        f"SELECT their_price FROM memecoin_events WHERE mint=? AND {COIN_NETWORK_SQL}=? "
        f"AND side='buy' AND their_price > 0 {window}ORDER BY id LIMIT 1",
        tuple(params)).fetchone()
    return float(row[0]) if row else None


def recently_closed_real_coin_ids(conn, now_iso, cooldown_s, *, before_iso=None, closes_by=None,
                                  lifted=None):
    """Coin identities of REAL positions closed within the last cooldown_s — the re-entry
    cooldown set (one real campaign per coin per window; 07-09 study: no re-entry edge).
    Threshold is computed in Python and compared as ISO-UTC strings (same-format prefix compare;
    format divergence only matters at sub-second granularity, irrelevant for hour-scale windows).

    `closes_by` (cross-wallet re-entry, 2026-10-08): None keeps every coin. A source_user_id keeps
    only the coins that id closed itself inside the window; other wallets' closes and manual
    closes (source_user_id NULL) do not count. The caller decides who gets a scope. When `lifted`
    is a dict, each coin the scope removed gets its latest close in the window (`_close_marker`)."""
    if not cooldown_s or cooldown_s <= 0:
        return frozenset()
    from datetime import datetime, timedelta
    from sniper.settings import memecoin as _cfg
    anchor = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    threshold = (anchor - timedelta(seconds=float(cooldown_s))).strftime("%Y-%m-%dT%H:%M:%S")
    # WINNERS ARE NOT COOLED (2026-08-27, operator order). The cooldown exists because the 07-09
    # re-entry study found no edge in re-entering a coin we just left — but that study never split
    # the population by outcome, so it says nothing about re-entering a coin we just made money on.
    # This is a policy change on operator judgment with the ledger as the grader, not a finding.
    #
    # The MOST RECENT close decides. A coin can have several closes inside one window and the last
    # one is the only one that describes the state we would be re-entering from; letting an older
    # win unblock a coin we have since lost on would invert the gate.
    #
    # Losses and breakeven keep the cooldown, and `losing_real_coin_ids` is untouched — a coin that
    # won last but is on the losing list stays blocked by that separate set.
    # `before_iso` makes this READABLE AS OF A PAST INSTANT, for the promotion shadow, which must
    # judge a decision on what was knowable when it was made. Live callers omit it and the SQL is
    # unchanged; a close cannot happen after "now" anyway, so this can only ever remove rows a
    # replay must not see.
    # The scope's two columns are read only when there is a scope: without one the SQL is byte for
    # byte what every funded caller has always run (test_memecoin_promotion_lane pins it).
    last, closers = {}, {}
    for r in conn.execute(
            "SELECT network_id, mint, closed_at, realized_pnl"
            + (", id, source_user_id" if closes_by is not None else "")
            + " FROM memecoin_positions "
            "WHERE execution_mode='real' AND is_open=0 AND closed_at >= ? "
            + ("AND closed_at <= ? " if before_iso else "")
            + "ORDER BY closed_at",
            ((threshold, str(before_iso)) if before_iso else (threshold,))):
        cid = coin_identity(r["network_id"], r["mint"])
        last[cid] = r
        if closes_by is not None:
            closers.setdefault(cid, set()).add(r["source_user_id"])
    # CHAIN SCOPE (2026-09-28): a winner is unblocked only on a chain in
    # MEMECOIN_REENTRY_WINNER_CHAINS. Every other chain keeps today's blanket cooldown.
    winners_open = getattr(_cfg, "MEMECOIN_REENTRY_WINNER_UNBLOCKED", False)
    cooled = frozenset(
        cid for cid, r in last.items()
        if not (winners_open and r["realized_pnl"] is not None and r["realized_pnl"] > 0
                and _cfg.winner_reentry_chain_on(cid[0])))
    if closes_by is None:
        return cooled
    own = frozenset(cid for cid in cooled if closes_by in closers[cid])
    if lifted is not None:
        lifted.update((cid, _close_marker(last[cid])) for cid in cooled - own)
    return own


def _close_marker(row):
    """The close a cross-wallet re-entry lifted, for the attempt trace."""
    return {"position_id": row["id"], "source_user_id": row["source_user_id"],
            "realized_pnl": row["realized_pnl"], "closed_at": row["closed_at"]}


def winner_reentry_rank(conn, network_id, mint, now_iso, window_s):
    """(rank, last_won) for one coin: how many REAL positions on it closed inside the window before
    `now_iso`, and whether the most recent of them won (realized_pnl > 0; 0 and NULL do not).

    One range on idx_mc_pos_mode_open_closed (a test pins the plan): this runs on every funded
    classification once winner re-entry is on. The threshold is built in Python, never with SQLite
    datetime() (its space separator sorts below our 'T')."""
    from datetime import datetime, timedelta
    anchor = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    threshold = (anchor - timedelta(seconds=float(window_s))).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        "SELECT p.realized_pnl FROM memecoin_positions p WHERE p.execution_mode='real' "
        "AND p.is_open=0 AND p.closed_at >= ? AND p.mint = ? "
        "AND IFNULL(" + coin_network_sql("p.") + ", '') = IFNULL(?, '') ORDER BY p.closed_at",
        (threshold, mint, normalized_coin_network(network_id, mint))).fetchall()
    if not rows:
        return 0, False
    last = rows[-1][0]
    return len(rows), bool(last is not None and last > 0)


def winner_reentry_step(conn, network_id, mint, now_iso, *, signal_source=None):
    """The winner re-entry ladder step for an automatic entry, or None when it does not apply.

    None: manual, winner re-entry off or out of chain scope, a first entry (rank 0), or a last
    close that did not win (the cooldown and losing gates refuse those). Otherwise
    {"rank": k, "factor": f}: f = 1.0 with no ladder configured, the k-th ladder step, or None
    when the ladder is exhausted or malformed (the reservation refuses it)."""
    from sniper.settings import memecoin as _cfg
    from sniper.services.memecoin_origin import bypasses_selection
    if bypasses_selection(signal_source) or not getattr(_cfg, "MEMECOIN_REENTRY_WINNER_UNBLOCKED", False):
        return None
    if not _cfg.winner_reentry_chain_on(network_id):
        return None
    rank, last_won = winner_reentry_rank(
        conn, network_id, mint, now_iso, float(_cfg.MEMECOIN_REAL_REENTRY_COOLDOWN_S or 0))
    if rank <= 0 or not last_won:
        return None
    ladder = _cfg.winner_reentry_ladder()
    if ladder is None:
        return {"rank": rank, "factor": 1.0}
    return {"rank": rank, "factor": ladder[rank - 1] if rank <= len(ladder) else None}


def last_real_close_for_coin(conn, network_id, mint, *, before_iso=None):
    """The most recent REAL close for one coin: (won, exit_unit_price, closed_at) or None.

    `exit_unit_price` is ROUTE-REALIZED — `memecoin_fills.notional / memecoin_fills.qty` on the
    exit fill, which is what we actually received per token, and is the same quantity
    `memecoin_fills.price` stores. Returns None for the unit price when the fill cannot be divided,
    so a caller can tell "we won but cannot price the exit" from "we won at X".
    """
    row = conn.execute(
        "SELECT p.id, p.realized_pnl, p.closed_at, f.qty, f.notional "
        "FROM memecoin_positions p "
        "LEFT JOIN memecoin_fills f ON f.position_id = p.id AND f.kind LIKE '%exit%' "
        "WHERE p.execution_mode='real' AND p.is_open=0 AND p.mint = ? "
        "  AND IFNULL(" + coin_network_sql("p.") + ", '') = IFNULL(?, '') "
        + ("  AND p.closed_at <= ? " if before_iso else "")
        + "ORDER BY p.closed_at DESC, f.id DESC LIMIT 1",
        ((mint, network_id, str(before_iso)) if before_iso else (mint, network_id))).fetchone()
    if row is None:
        return None
    unit = None
    try:
        if row["qty"] and float(row["qty"]) > 0 and row["notional"] is not None:
            unit = float(row["notional"]) / float(row["qty"])
    except (TypeError, ValueError, ZeroDivisionError):
        unit = None
    pnl = row["realized_pnl"]
    return {"won": bool(pnl is not None and pnl > 0), "exit_unit_price": unit,
            "closed_at": row["closed_at"], "position_id": row["id"]}


def recent_confirmed_real_exit_for_coin(conn, network_id, mint, now_iso, *, max_age_s):
    """Return a recent chain-confirmed full REAL exit, or None."""
    from datetime import datetime
    try:
        anchor = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        age = float(max_age_s)
    except (TypeError, ValueError):
        return None
    if age <= 0:
        return None
    row = conn.execute(
        "SELECT p.id, p.closed_at, p.close_sig FROM memecoin_positions p "
        "JOIN memecoin_fills f ON f.position_id=p.id AND f.tx_sig=p.close_sig "
        "WHERE p.execution_mode='real' AND p.is_open=0 AND p.mint=? "
        "AND IFNULL(" + coin_network_sql("p.") + ", '')=IFNULL(?, '') "
        "AND p.close_sig IS NOT NULL "
        "AND f.kind IN ('mirror_exit','manual_close','bracket_exit') "
        "AND f.fill_source IN ('chain','chain_confirmed_unpriced') "
        "ORDER BY p.closed_at DESC LIMIT 1",
        (mint, network_id),
    ).fetchone()
    if row is None:
        return None
    try:
        closed_at = datetime.fromisoformat(str(row["closed_at"]).replace("Z", "+00:00"))
        elapsed = (anchor - closed_at).total_seconds()
    except (TypeError, ValueError):
        return None
    if elapsed < 0 or elapsed > age:
        return None
    return {"position_id": row["id"], "closed_at": row["closed_at"], "close_sig": row["close_sig"]}


def real_campaigns_today_for_coin(conn, network_id, mint, now_iso, *, before_iso=None):
    """How many REAL positions opened on this coin so far today (UTC).

    The cutoff is built in PYTHON. `datetime('now', ...)` renders a space separator and ' ' sorts
    below 'T', so a SQL-built day boundary silently reaches ~16 hours further back than intended
    (2026-08-23: a "24h" window returned 126,489 rows against a true 74,693).
    """
    from datetime import datetime
    try:
        anchor = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    start = anchor.strftime("%Y-%m-%dT00:00:00")
    return int(conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions p WHERE p.execution_mode='real' "
        "  AND p.mint = ? AND IFNULL(" + coin_network_sql("p.") + ", '') = IFNULL(?, '') "
        "  AND p.opened_at >= ?"
        + ("  AND p.opened_at <= ?" if before_iso else ""),
        ((mint, network_id, start, str(before_iso)) if before_iso
         else (mint, network_id, start))).fetchone()[0])


def losing_real_coin_ids(conn, *, since_iso=None, before_iso=None, closes_by=None, lifted=None):
    """Coin identities whose MOST RECENT closed real position lost money.

    `since_iso` bounds how far back a close may count (operator 2026-08-25: the funded path reads
    nothing older than a week). None = unbounded, the original behaviour. NOTE this converts the
    block below from permanent into expiring -- that is the point of the bound, and it is a real
    change to the rule described in the next paragraph.

    Operator 2026-08-25: this block is now scoped to 24 hours (MEMECOIN_REAL_LOSING_REENTRY_HOURS).
    The paragraph below describes the ORIGINAL permanent form and is kept because its evidence is
    unchanged -- only the duration moved. A coin that beat us yesterday is enterable again today.

    Operator 2026-08-23, after KIRK: position 44596 lost $15.27 on b_timecap at 15:54, and 43
    minutes later 44598 re-entered the SAME mint and lost $24.24 the same way -- $39.51 on one
    coin the book had already been beaten by. `MEMECOIN_REAL_REENTRY_COOLDOWN_S` exists but is 0,
    and a time window is the wrong shape anyway: it expires, and it blocks winners too.

    Measured over 309 closed real positions: an entry whose coin's previous close LOST returned
    -12.5% (n=15, 33% wins) against -4.8% for every other entry (n=294, 48% wins). Bootstrap on
    the difference is -7.6pp, 95% CI [-27.3, +13.3], P(worse)=0.77 -- directional, not proof. It
    is a behavioral rule about not re-fighting a coin, and it touches 4.9% of entries.

    SQLite returns the bare columns from the row that matched MAX(closed_at) in a GROUP BY, which
    is what makes 'the most recent close' readable in one pass rather than a correlated subquery.
    """
    # `closes_by` (cross-wallet re-entry, 2026-10-08): None keeps every coin. A source_user_id
    # keeps only the coins whose most recent close is that id's own loss; a manual-buy position
    # (source_user_id NULL) is never one. Never stricter than None. Two closes at the same instant
    # make the bare columns one of them, as they already do for realized_pnl. `lifted`: as in
    # recently_closed_real_coin_ids. Unscoped, the SQL is unchanged (as there).
    lost = {
        coin_identity(r["network_id"], r["mint"]): r
        for r in conn.execute(
            "SELECT network_id, mint, realized_pnl, "
            + ("id, source_user_id, MAX(closed_at) AS closed_at " if closes_by is not None
               else "MAX(closed_at) ")
            + "FROM memecoin_positions "
            "WHERE execution_mode='real' AND is_open=0 AND closed_at IS NOT NULL "
            + ("AND closed_at>=? " if since_iso else "")
            + ("AND closed_at<=? " if before_iso else "")
            + "GROUP BY network_id, mint",
            tuple(v for v in (since_iso, before_iso) if v))
        if (r["realized_pnl"] or 0.0) < 0}
    if closes_by is None:
        return frozenset(lost)
    own = frozenset(cid for cid, r in lost.items() if r["source_user_id"] == closes_by)
    if lifted is not None:
        lifted.update((cid, _close_marker(r)) for cid, r in lost.items() if cid not in own)
    return own


def concrete_event_networks(conn, mint):
    """Sorted concrete networks observed for ``mint``; empty and unknown tags are excluded.

    Keeping all distinct values preserves the difference between no evidence and conflicting
    evidence. Callers that require a scalar must accept only a one-element result.
    """
    rows = conn.execute(
        "SELECT DISTINCT network_id FROM memecoin_events "
        "WHERE mint=? AND network_id IS NOT NULL AND network_id NOT IN ('', 'evm-unknown') "
        "ORDER BY network_id",
        (mint,),
    ).fetchall()
    return tuple(row[0] for row in rows)


def resolve_network_from_events(conn, mint):
    """The feed's own concrete chain for a mint, from memecoin_events. This is the ONLY signal that
    can attribute a Robinhood 4663 mint delivered without a networkId — DexScreener has no 4663 pair,
    so resolve_evm_chain() can't. Returns the single concrete network_id, or None when the feed never
    carried a concrete chain for this mint OR carried conflicting chains (ambiguous -> refuse, never
    guess). Cheap indexed lookup; safe to call in the hot path and the sweep."""
    chains = concrete_event_networks(conn, mint)
    return chains[0] if len(chains) == 1 else None


def resolve_ledger_network(conn, mint):
    """Unambiguous-only chain for a bare-mint ledger/instrument row (quote_ledger, alert/exec
    latency). A non-0x mint is Solana-native (unambiguous). A 0x mint is resolved from the feed's
    own events (single concrete chain) or left None — never coerced to a default chain."""
    if not mint:
        return None
    if not str(mint).startswith("0x"):
        return "solana"
    return resolve_network_from_events(conn, mint)


def _backfill_table_network(conn, table, resolver, batch=5000):
    """Fill NULL network_id rows of `table` via resolver(conn, mint)->chain|None, scanning by an id
    watermark (NOT `WHERE network_id IS NULL LIMIT`, which would re-select the unresolvable rows
    forever). Batched + committed per batch so each write-lock is short vs the live 15s sweep (WAL:
    readers never block). Unresolvable rows stay NULL and are counted. Idempotent."""
    resolved = left_null = scanned = 0
    cache = {}
    last_id = 0
    while True:
        rows = conn.execute(
            f"SELECT id, mint FROM {table} WHERE id>? AND network_id IS NULL ORDER BY id LIMIT ?",
            (last_id, batch)).fetchall()
        if not rows:
            break
        for row in rows:
            rid, mint = row[0], row[1]
            scanned += 1
            last_id = rid
            if mint not in cache:
                cache[mint] = resolver(conn, mint)
            net = cache[mint]
            if net is None:
                left_null += 1
                continue
            conn.execute(f"UPDATE {table} SET network_id=? WHERE id=?", (net, rid))
            resolved += 1
        conn.commit()
    return {"resolved": resolved, "left_null": left_null, "scanned": scanned}


def backfill_quote_ledger_network(conn, batch=5000):
    """Backfill memecoin_quote_ledger.network_id, unambiguous-only. See _backfill_table_network."""
    return _backfill_table_network(conn, "memecoin_quote_ledger", resolve_ledger_network, batch)


def backfill_alert_latency_network(conn, batch=5000):
    """Backfill memecoin_alert_latency.network_id, unambiguous-only."""
    return _backfill_table_network(conn, "memecoin_alert_latency", resolve_ledger_network, batch)


def backfill_exec_latency_network(conn, batch=5000):
    """Backfill memecoin_exec_latency.network_id, unambiguous-only."""
    return _backfill_table_network(conn, "memecoin_exec_latency", resolve_ledger_network, batch)


class HealOpenNetworkResult(int):
    """Int-compatible heal count with deterministic conflicts for operator telemetry."""

    def __new__(cls, healed, collisions=()):
        result = int.__new__(cls, healed)
        result.collisions = tuple(collisions)
        return result


def heal_open_evm_unknown_from_events(conn, mode="paper"):
    """Re-attribute open positions stuck at network_id 'evm-unknown'/'' to the concrete chain the feed
    later carried for the same mint (via resolve_network_from_events — unambiguous only). Without this a
    4663 mint that opened before its chain was known is priced on 'evm-unknown' (nothing) forever and
    only leaves via the 24h unquotable guard, never its arm's exit policy. Returns the count healed.
    Used both by the periodic sweep (self-heal) and the one-time backfill."""
    rows = conn.execute(
        "SELECT id, mint, source_user_id, strategy, "
        "COALESCE(execution_mode,'paper') AS execution_mode FROM memecoin_positions "
        "WHERE is_open=1 AND execution_mode=? AND mint LIKE '0x%' "
        "AND (network_id IS NULL OR network_id IN ('', 'evm-unknown')) ORDER BY id", (mode,)).fetchall()
    healed = 0
    collisions = []

    def conflicting_position(row, resolved):
        return conn.execute(
            "SELECT id FROM memecoin_positions WHERE id<>? AND is_open=1 "
            "AND source_user_id IS ? AND mint=? AND strategy IS ? "
            "AND COALESCE(execution_mode,'paper')=? AND " + COIN_NETWORK_SQL + "=? "
            "ORDER BY id LIMIT 1",
            (row["id"], row["source_user_id"], row["mint"], row["strategy"],
             row["execution_mode"], resolved),
        ).fetchone()

    def record_collision(row, resolved, conflict):
        collisions.append({
            "position_id": row["id"],
            "conflicting_position_id": conflict["id"],
            "network_id": resolved,
            "mint": row["mint"],
            "reason": "open_coin_identity_collision",
        })

    for r in rows:
        resolved = resolve_network_from_events(conn, r["mint"])
        if not resolved:
            continue
        conflict = conflicting_position(r, resolved)
        if conflict:
            record_collision(r, resolved, conflict)
            continue
        conn.execute("SAVEPOINT heal_open_evm_unknown")
        try:
            conn.execute("UPDATE memecoin_positions SET network_id=? WHERE id=?",
                         (resolved, r["id"]))
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK TO heal_open_evm_unknown")
            conn.execute("RELEASE heal_open_evm_unknown")
            conflict = conflicting_position(r, resolved)
            if conflict is None:
                raise
            record_collision(r, resolved, conflict)
        else:
            conn.execute("RELEASE heal_open_evm_unknown")
            healed += 1
    if healed:
        conn.commit()
    return HealOpenNetworkResult(healed, collisions)


def backfill_network_id(conn):
    """One-time: derive network_id for pre-migration rows (NULL). An `0x`-prefixed mint is an EVM
    token whose exact chain we can't recover from the mint alone -> 'evm-unknown'; anything else is
    a Solana base58 mint -> 'solana'. NEVER overwrites a known value. Idempotent (only touches NULLs)."""
    for tbl in ("memecoin_events", "memecoin_positions"):
        conn.execute(
            f"UPDATE {tbl} SET network_id = CASE WHEN mint LIKE '0x%' THEN 'evm-unknown' "
            f"ELSE 'solana' END WHERE network_id IS NULL")
    conn.commit()


def get_open_position(conn, source_user_id, mint, strategy="control", mode="paper",
                      network_id=None):
    """Return the exact open coin row.

    Legacy omitted networks resolve only to the conservative family bucket: base58 -> Solana,
    0x -> evm-unknown. A bare 0x lookup can never select a concrete EVM-chain position.
    """
    r = conn.execute(
        "SELECT * FROM memecoin_positions WHERE source_user_id=? AND mint=? "
        f"AND {COIN_NETWORK_SQL}=? AND strategy=? AND is_open=1 AND execution_mode=?",
        (source_user_id, mint, normalized_coin_network(network_id, mint), strategy, mode)).fetchone()
    return dict(r) if r else None


def notification_held_position(conn, network_id, mint):
    """Worker-only display context: one currently open real position for this exact coin."""
    if not mint or not network_id:
        return None
    net, key = accounting_mark_key(network_id, mint)
    mint_sql = "LOWER(mint)" if str(mint).lower().startswith("0x") else "mint"
    row = conn.execute(
        "SELECT id,opened_at,opened_recorded_at FROM memecoin_positions "
        f"WHERE {mint_sql}=? AND {COIN_NETWORK_SQL}=? AND is_open=1 AND execution_mode='real' "
        "ORDER BY id DESC LIMIT 1", (key, net)).fetchone()
    return dict(row) if row else None


def open_positions_for_coin(conn, source_user_id, mint, strategy="control", mode="paper"):
    """ALL open rows for (uid, mint, strategy, mode) across network buckets.

    The entry path stores a RESOLVED identity (concrete chain when it can), but sell and
    follow events often omit or under-resolve the chain — an exact-bucket lookup alone
    misses the position (a funded EVM bag would ride through the trader's dump) and the
    open-side dedup double-opens during the evm-unknown -> concrete heal window. Callers
    use this as the network-agnostic fallback/dup view; ambiguity (>1 row, a genuine
    cross-chain address collision) is theirs to refuse."""
    rows = conn.execute(
        "SELECT * FROM memecoin_positions WHERE source_user_id=? AND mint=? "
        "AND strategy=? AND is_open=1 AND execution_mode=?",
        (source_user_id, mint, strategy, mode)).fetchall()
    return [dict(r) for r in rows]


def update_position_peak(conn, position_id, peak_price, peak_ts, no_new_high_sweeps):
    conn.execute("UPDATE memecoin_positions SET peak_price=?, peak_ts=?, no_new_high_sweeps=? "
                 "WHERE id=?", (peak_price, peak_ts, no_new_high_sweeps, position_id))
    conn.commit()


def update_position_peak_unconfirmed(conn, position_id, price):
    """Raise the unconfirmed display high (a lone tick above the confirmed peak). Display only."""
    conn.execute("UPDATE memecoin_positions SET peak_unconfirmed_price=? WHERE id=? "
                 "AND (peak_unconfirmed_price IS NULL OR peak_unconfirmed_price < ?)",
                 (float(price), position_id, float(price)))
    conn.commit()


def update_position_executable_peak(conn, position_id, peak_price, peak_at=None):
    """Advance the funded exact-balance high-water independently of the display peak.

    `peak_at` stamps WHEN the high was set. The copy-arm peak-stall rail measures elapsed time
    since the last new executable high, so it needs the timestamp rather than a sweep counter —
    the funded fast lane ticks up to 15x faster than the paper sweep the arm was measured on.
    Omitted (default) leaves the column untouched, so existing callers are byte-identical.
    """
    if peak_at is None:
        conn.execute("UPDATE memecoin_positions SET executable_peak_price=? WHERE id=?",
                     (float(peak_price), position_id))
    else:
        conn.execute(
            "UPDATE memecoin_positions SET executable_peak_price=?, executable_peak_at=? "
            "WHERE id=?", (float(peak_price), str(peak_at), position_id))
    conn.commit()


def set_position_rails_blind_streak(conn, position_id, streak):
    conn.execute("UPDATE memecoin_positions SET rails_blind_streak=? WHERE id=?",
                 (int(streak), position_id))
    conn.commit()


def recent_position_marks(conn, chain_id, mint, since_iso, until_iso=None, sources=None, limit=200):
    """Non-quarantined marks for one coin in [since_iso, until_iso], newest first: (sweep_ts, mid,
    liquidity, source, quality). `sources` keeps only those exact names, so other series can never
    crowd them out of `limit`. For the exit dust guard's market read (memecoin_exit_dust_guard)."""
    sql = ("SELECT sweep_ts, mid, liquidity, source, quality FROM memecoin_position_marks "
           "WHERE chain_id IS ? AND mint=? AND sweep_ts >= ? "
           "AND COALESCE(quality,'') != 'quarantined_spike'")
    params = [chain_id, mint, since_iso]
    if until_iso is not None:
        sql += " AND sweep_ts <= ?"
        params.append(until_iso)
    if sources:
        sql += " AND source IN (%s)" % ",".join("?" * len(sources))
        params.extend(sources)
    params.append(int(limit))
    return [dict(r) if hasattr(r, "keys") else dict(zip(("sweep_ts", "mid", "liquidity", "source",
                                                          "quality"), r))
            for r in conn.execute(sql + " ORDER BY sweep_ts DESC LIMIT ?", params)]


def set_position_last_executable(conn, position_id, price, observed_at, *, anchor=None):
    """Record the most recent RESOLVED exact whole-balance unit price.

    Only ever called with a real, positive exact quote. A failed quote must leave the previous
    value alone: that stale-but-real price is exactly what the loss rails fall back to.

    `anchor`, when given, moves the anchor-checked exit's sticky level in the SAME statement (spec
    2026-10-01 §3.1): one UPDATE, one commit per tick. None is today's statement unchanged.
    """
    if anchor is None:
        conn.execute(
            "UPDATE memecoin_positions SET last_executable_price=?, last_executable_at=? WHERE id=?",
            (float(price), str(observed_at), position_id))
    else:
        conn.execute(
            "UPDATE memecoin_positions SET last_executable_price=?, last_executable_at=?, "
            "exit_anchor_price=?, exit_anchor_at=? WHERE id=?",
            (float(price), str(observed_at), float(anchor), str(observed_at), position_id))
    conn.commit()


def record_exit_anchor_audit(conn, *, position_id, network_id, observed_at, decision, route,
                             value_usd, anchor_price, qty, held_s, exit_reason, rungs_json):
    """One row per held tick and per held sale (spec 2026-10-01 §6). A plain pass writes nothing."""
    conn.execute(
        "INSERT INTO memecoin_exit_anchor_audit(position_id, network_id, observed_at, decision, "
        "route, value_usd, anchor_price, qty, held_s, exit_reason, rungs_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(position_id), str(network_id), str(observed_at), str(decision), route, value_usd,
         anchor_price, qty, held_s, exit_reason, rungs_json))
    conn.commit()


def open_exit_anchor_holds(conn, *, since_iso):
    """{position_id: latest audit row} for positions whose latest row since `since_iso` is a hold:
    the Real Positions row shows it with the "sell at any price" button."""
    try:
        rows = conn.execute(
            "SELECT a.position_id, a.observed_at, a.value_usd, a.anchor_price, a.qty, a.held_s, "
            "a.route, a.exit_reason FROM memecoin_exit_anchor_audit a JOIN ("
            "SELECT position_id, MAX(id) AS id FROM memecoin_exit_anchor_audit "
            "WHERE observed_at >= ? GROUP BY position_id) last ON last.id = a.id "
            "WHERE a.decision = 'held'", (str(since_iso),)).fetchall()
    except sqlite3.OperationalError:           # a ledger the runner has not migrated yet
        return {}
    return {int(r[0]): {"observed_at": r[1], "value_usd": r[2], "anchor_price": r[3], "qty": r[4],
                        "held_s": r[5], "route": r[6], "exit_reason": r[7]} for r in rows}


def set_position_instrumentation(conn, position_id, *, time_to_peak_s, max_gain_pct):
    conn.execute("UPDATE memecoin_positions SET time_to_peak_s=?, max_gain_pct=? WHERE id=?",
                 (time_to_peak_s, max_gain_pct, position_id))
    conn.commit()


def pin_position_fdv_supply(conn, position_id, supply):
    """Write the position's total supply once and return the STORED value. A pinned value is never
    moved, so a target cannot change basis mid-position; a second writer gets the first one's."""
    conn.execute(
        "UPDATE memecoin_positions SET fdv_supply=? WHERE id=? AND fdv_supply IS NULL",
        (float(supply), int(position_id)))
    row = conn.execute("SELECT fdv_supply FROM memecoin_positions WHERE id=?",
                       (int(position_id),)).fetchone()
    return row[0] if row else None


def pin_position_display_total_supply(conn, position_id, supply):
    """Write the position's display-only total supply once; return the STORED value."""
    conn.execute("UPDATE memecoin_positions SET display_total_supply=? "
                 "WHERE id=? AND display_total_supply IS NULL", (float(supply), int(position_id)))
    row = conn.execute("SELECT display_total_supply FROM memecoin_positions WHERE id=?",
                       (int(position_id),)).fetchone()
    return row[0] if row else None


def position_cap_supply(position):
    """(supply, basis) the cards value market caps on, GMGN's total-supply basis, or (None, None).

    'fdv': the pinned fdv_supply, which the runner's operator mcap targets also read.
    'total': the display-only on-chain totalSupply; this position's operator targets still read the
    provider/entry-ratio basis until a save pins fdv_supply from it (set_real_position_exit)."""
    position = position or {}
    for column, basis in (("fdv_supply", "fdv"), ("display_total_supply", "total")):
        try:
            v = float(position.get(column) or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            return v, basis
    return None, None


def set_position_identity(conn, position_id, *, ticker=None, source_handle=None,
                          origin_event_id=None):
    """Backfill ticker/handle/origin_event_id onto an executor-created real position (executors only
    know the mint; the feed event knows who/what/which-signal). Never overwrites an existing non-empty
    value. WS3 (2026-07-15): origin_event_id lets real positions be matched to their real_shadow
    counterpart — it was 100% NULL on the real leg, blinding matched real-vs-shadow comparison."""
    conn.execute(
        "UPDATE memecoin_positions SET "
        "ticker=COALESCE(NULLIF(ticker,''), ?), "
        "source_handle=CASE WHEN source_handle IS NULL OR source_handle IN ('', '(real)') "
        "THEN COALESCE(?, source_handle) ELSE source_handle END, "
        "origin_event_id=COALESCE(NULLIF(origin_event_id,''), ?) "
        "WHERE id=?", (ticker, source_handle, origin_event_id, position_id))
    conn.commit()


def set_position_stop_breach_started(conn, position_id, started_at):
    """Stamp (or clear, with None) when a stop condition FIRST held for this position.

    Elapsed-time confirmation needs a breach start; the bare `stop_hits` counter has no clock, so
    a "seconds" window expressed as a hit count silently became 38 minutes whenever the ~31s slow
    lane did the deciding."""
    conn.execute("UPDATE memecoin_positions SET stop_breach_started_at=? WHERE id=?",
                 (started_at, position_id))
    conn.commit()


def set_position_tp_breach_started(conn, position_id, started_at):
    """Stamp (or clear, with None) when the take-profit condition FIRST held.

    Separate from the stop's stamp deliberately. The rails confirm independently, and a position
    can be breaching neither, one, or (transiently, across a wide swing) both — one shared column
    would let a stop breach reset a take-profit's window and vice versa.
    """
    conn.execute("UPDATE memecoin_positions SET tp_breach_started_at=? WHERE id=?",
                 (started_at, position_id))
    conn.commit()


def set_position_stop_hits(conn, position_id, hits):
    conn.execute("UPDATE memecoin_positions SET stop_hits=? WHERE id=?", (int(hits), position_id))
    conn.commit()


def set_experiment_status(conn, position_id, status):
    """Exclude a paper row before an operator-modeled mutation; funded rows are untouched."""
    allowed = {
        "eligible", "excluded_fill", "operator_modified", "manual", "legacy_unversioned",
        "invalid_policy",
    }
    if status not in allowed:
        raise ValueError(f"invalid experiment status: {status!r}")
    cur = conn.execute(
        "UPDATE memecoin_positions SET experiment_status="
        "CASE WHEN experiment_status='manual' THEN 'manual' ELSE ? END "
        "WHERE id=? AND execution_mode='paper'",
        (status, position_id),
    )
    conn.commit()
    return cur.rowcount == 1


def set_position_exact_quote_misses(conn, position_id, misses):
    """Consecutive sweeps the whole-bag executable quote failed to resolve. 0 = resolved this sweep."""
    conn.execute("UPDATE memecoin_positions SET exact_quote_misses=? WHERE id=?",
                 (int(misses), position_id))
    conn.commit()


# Reducer-writable controller columns (mirror sniper.analysis.exit_controller.WRITABLE_FIELDS).
# `controller_revision` is deliberately absent — it is the CAS guard, bumped only by
# persist_controller_state_cas, never a free field a caller can set directly.
CONTROLLER_STATE_COLS = (
    "structure_break_since", "consecutive_break_events", "negative_flow_since",
    "route_degradation_since", "route_failure_streak", "infra_failure_streak",
    "controller_last_event_id", "controller_last_event_block",
    "controller_last_event_log_index", "executable_peak_at", "executable_peak_price",
    "route_quote_value", "route_quote_at")


def set_position_controller_state(conn, position_id, **fields):
    """WO-5: persist exit-decision hysteresis on the position row so it survives restarts by
    construction (structure-break persistence, negative-flow/route-degradation onset timestamps,
    route/infra failure streaks). Accepts any subset of CONTROLLER_STATE_COLS; an unknown key raises
    (fail loud, never silently drop a money-path signal). No-op when `fields` is empty."""
    unknown = set(fields) - set(CONTROLLER_STATE_COLS)
    if unknown:
        raise ValueError(f"unknown controller-state field(s): {sorted(unknown)}")
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE memecoin_positions SET {sets} WHERE id=?",
                 (*fields.values(), position_id))
    conn.commit()


def persist_controller_state_cas(conn, position_id, prev_revision, **fields):
    """WO-5A: atomic optimistic-concurrency persist of one reduce() output. Writes the controller
    columns AND bumps controller_revision to prev_revision+1 ONLY if the row still holds
    prev_revision. Returns True if this writer won the CAS; False means a concurrent writer already
    advanced the revision — the caller must reload the row and recompute (never blind-overwrite a
    money-path signal). Unknown field -> ValueError (fail loud); controller_revision is CAS-managed
    and rejected as a free field."""
    unknown = set(fields) - set(CONTROLLER_STATE_COLS)
    if unknown:
        raise ValueError(f"unknown controller-state field(s): {sorted(unknown)}")
    assignments = [f"{k}=?" for k in fields] + ["controller_revision=?"]
    params = [*fields.values(), int(prev_revision) + 1, position_id, int(prev_revision)]
    cur = conn.execute(
        f"UPDATE memecoin_positions SET {', '.join(assignments)} "
        "WHERE id=? AND controller_revision=?", params)
    conn.commit()
    return cur.rowcount == 1


def set_position_exact_quote_infra_misses(conn, position_id, misses):
    conn.execute("UPDATE memecoin_positions SET exact_quote_infra_misses=? WHERE id=?",
                 (int(misses), position_id))
    conn.commit()


def set_position_mirror_disabled(conn, position_id, disabled):
    """Operator per-position mirror-exit override. disabled=True -> real_mirror_on_sell will NOT
    sell this position when its copied trader sells (the copy-exit is suppressed so the operator can
    hold past the trader's exit). TP / stop / de-risk / timecap / unquotable rails are unaffected —
    the position is never left unprotected. disabled=False reverts to the default (mirror active)."""
    conn.execute("UPDATE memecoin_positions SET mirror_disabled=? WHERE id=?",
                 (1 if disabled else 0, position_id))
    conn.commit()


def set_exit_target(conn, position_id, mcap_low, mcap_high, *, now_iso, actor="operator", note=None):
    """Set the active mcap-band take-profit target for a position (cancels any prior active target
    first, so there is always at most one active target per position). Full-sell fires when live mcap
    reaches mcap_low; mcap_high is the informational top of the intended zone."""
    conn.execute("UPDATE memecoin_exit_targets SET status='cancelled', resolved_at=? "
                 "WHERE position_id=? AND status='active'", (now_iso, position_id))
    conn.execute("INSERT INTO memecoin_exit_targets"
                 "(position_id, mcap_low, mcap_high, status, actor, note, created_at) "
                 "VALUES(?,?,?, 'active', ?, ?, ?)",
                 (position_id, float(mcap_low),
                  (float(mcap_high) if mcap_high is not None else None), actor, note, now_iso))
    conn.commit()


def active_exit_target(conn, position_id):
    """The active mcap target for a position, or None."""
    r = conn.execute("SELECT * FROM memecoin_exit_targets WHERE position_id=? AND status='active' "
                     "ORDER BY id DESC LIMIT 1", (position_id,)).fetchone()
    return dict(r) if r else None


def mark_exit_target_filled(conn, position_id, now_iso):
    conn.execute("UPDATE memecoin_exit_targets SET status='filled', resolved_at=? "
                 "WHERE position_id=? AND status='active'", (now_iso, position_id))
    conn.commit()


def set_token_liquidity(conn, chain_id, mint, reserve_usd, now_iso):
    """Persist a token's pool liquidity (reserve_in_usd). Written by the runner from its free GT
    pool-discovery capture; the web display reads it (GT is otherwise unreachable from the web)."""
    try:
        reserve_usd = float(reserve_usd)
    except (TypeError, ValueError):
        return
    if not (reserve_usd > 0):
        return
    conn.execute(
        "INSERT INTO memecoin_token_liquidity(chain_id, mint, reserve_usd, updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(chain_id, mint) DO UPDATE SET "
        "reserve_usd=excluded.reserve_usd, updated_at=excluded.updated_at",
        (str(chain_id), str(mint), reserve_usd, now_iso))
    conn.commit()


def token_liquidity_ages(conn, keys):
    """{(chain_id, mint): updated_at or None} for the given keys — the staleness index the
    blind-liquidity refresher rotates on (absent rows sort first as None)."""
    out = {}
    for key in keys or []:
        chain_id, mint = key
        row = conn.execute(
            "SELECT updated_at FROM memecoin_token_liquidity WHERE chain_id=? AND mint=?",
            (str(chain_id), str(mint))).fetchone()
        out[(chain_id, mint)] = row["updated_at"] if row else None
    return out


def get_token_liquidity(conn, chain_id, mint, *, now_iso=None, max_age_s=7200):
    """Last-known pool liquidity for a token, or None if unknown / older than max_age_s. Liquidity
    moves slowly, so a stale value is a fine display estimate — but a day-old reserve is dropped."""
    r = conn.execute("SELECT reserve_usd, updated_at FROM memecoin_token_liquidity "
                     "WHERE chain_id=? AND mint=?", (str(chain_id), str(mint))).fetchone()
    if not r or not r["reserve_usd"] or r["reserve_usd"] <= 0:
        return None
    if now_iso and max_age_s:
        from sniper.services.memecoin_exits import _parse
        a, b = _parse(r["updated_at"]), _parse(now_iso)
        if a and b and (b - a).total_seconds() > float(max_age_s):
            return None
    return r["reserve_usd"]


def ensure_external_discovery_table(conn):
    """Coin Radar Stage 2: coins surfaced by EXTERNAL discovery (DexScreener boosts, GT trending/new
    pools) — hot coins that may appear before any of our wallets touch them. One row per (chain, mint);
    upserts refresh the stats + last_seen."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memecoin_external_discovery ("
        "chain_id TEXT NOT NULL, mint TEXT NOT NULL, ticker TEXT, source TEXT, "
        "price REAL, market_cap REAL, liquidity REAL, volume_24h REAL, "
        "first_seen TEXT, last_seen TEXT, PRIMARY KEY (chain_id, mint))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_ext_disc_last "
                 "ON memecoin_external_discovery(last_seen)")
    conn.commit()


def upsert_external_discovery(conn, rows, now_iso):
    """Upsert normalized discovery rows [{network_id/chain_id, mint, ticker, source, price, market_cap,
    liquidity, volume_24h}]. first_seen is set once; last_seen + stats refresh each pass. Returns the
    count written."""
    ensure_external_discovery_table(conn)
    n = 0
    for r in rows or []:
        chain = str(r.get("network_id") or r.get("chain_id") or "")
        mint = r.get("mint")
        if not chain or not mint:
            continue
        conn.execute(
            "INSERT INTO memecoin_external_discovery"
            "(chain_id, mint, ticker, source, price, market_cap, liquidity, volume_24h, "
            " first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chain_id, mint) DO UPDATE SET "
            "  ticker=COALESCE(excluded.ticker, ticker), "
            "  source=COALESCE(excluded.source, source), "
            "  price=COALESCE(excluded.price, price), "
            "  market_cap=COALESCE(excluded.market_cap, market_cap), "
            "  liquidity=COALESCE(excluded.liquidity, liquidity), "
            "  volume_24h=COALESCE(excluded.volume_24h, volume_24h), "
            "  last_seen=excluded.last_seen",
            (chain, str(mint), r.get("ticker"), r.get("source"), r.get("price"),
             r.get("market_cap"), r.get("liquidity"), r.get("volume_24h"), now_iso, now_iso))
        n += 1
    conn.commit()
    return n


def prune_external_discovery(conn, *, before_iso):
    """Delete discovery rows last seen before `before_iso`. Invalid timestamps fail closed."""
    ensure_external_discovery_table(conn)
    if not before_iso:
        raise ValueError("before_iso is required")
    cur = conn.execute(
        "DELETE FROM memecoin_external_discovery "
        "WHERE julianday(last_seen) < julianday(?)",
        (str(before_iso),),
    )
    conn.commit()
    return cur.rowcount


def external_discovery_rows(
    conn, *, window_hours=48, chain=None, limit=300, now_iso=None, sort=None, desc=True
):
    """Recent external-discovery coins as universe-shaped rows (buyers/armed_buyers=0; they are external,
    not from our feed). Empty if the table doesn't exist yet."""
    try:
        ensure_external_discovery_table(conn)
    except Exception:
        return []
    where, params = [], []
    if window_hours and now_iso:
        from datetime import datetime, timedelta
        try:
            cut = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                   - timedelta(hours=float(window_hours))).isoformat()
            where.append("last_seen >= ?"); params.append(cut)
        except (ValueError, TypeError):
            pass
    if chain:
        where.append("chain_id = ?"); params.append(str(chain))
    wq = (" WHERE " + " AND ".join(where)) if where else ""
    sort_column = {
        "mcap": "market_cap", "liq": "liquidity", "vol": "volume_24h",
        "age": "first_seen", "recent": "last_seen",
    }.get(sort, "last_seen")
    direction = "DESC" if desc else "ASC"
    if sort == "age":
        direction = "ASC" if desc else "DESC"
    rows = conn.execute(
        "SELECT chain_id, mint, ticker, source, price, market_cap, liquidity, volume_24h, "
        "first_seen, last_seen FROM memecoin_external_discovery" + wq +
        f" ORDER BY ({sort_column}) IS NULL, {sort_column} {direction}, last_seen DESC LIMIT ?",
        params + [int(max(1, limit))],
    ).fetchall()
    from datetime import datetime
    try:
        now_dt = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00")) if now_iso else None
    except (ValueError, TypeError):
        now_dt = None
    out = []
    for r in rows:
        try:
            first_dt = datetime.fromisoformat(str(r["first_seen"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            first_dt = None
        try:
            last_dt = datetime.fromisoformat(str(r["last_seen"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            last_dt = None
        out.append({
            "network_id": r["chain_id"], "mint": r["mint"], "ticker": r["ticker"],
            "chain": r["chain_id"], "venue": "EVM" if str(r["mint"]).startswith("0x") else "SOL",
            "buyers": 0, "armed_buyers": 0, "price": r["price"], "market_cap": r["market_cap"],
            "liquidity": r["liquidity"], "volume_24h": r["volume_24h"],
            "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            "age_s": (now_dt - first_dt).total_seconds() if (now_dt and first_dt) else None,
            "recent_s": (now_dt - last_dt).total_seconds() if (now_dt and last_dt) else None,
            "rating": None, "real_open": False, "source": r["source"] or "external",
        })
    return out


def ensure_pool_depth_cache(conn):
    """Depth in USD per coin, computed from our own pool store off the hot path.

    The funded entry path reads this with ONE indexed SELECT when the market-data provider returns
    no pair (1,982 of 2,254 4663 attempts since 09-15). It is refreshed by a separate pass because
    finding a coin's live pool means reading dozens of mostly empty ones, which is RPC time a
    60-second-old coin's decision cannot afford. `depth_usd` is a FLOOR, not the provider's TVL —
    see sniper/services/memecoin_pool_depth for the measured gap.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memecoin_pool_depth("
        "chain_id TEXT NOT NULL, mint TEXT NOT NULL, pool TEXT, quote TEXT, "
        "depth_usd REAL, pools_read INTEGER, as_of TEXT NOT NULL, "
        "PRIMARY KEY(chain_id, mint))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_pool_depth_as_of "
                 "ON memecoin_pool_depth(chain_id, as_of)")
    # The impact gate (2026-09-23) needs the pool STATE, not just the derived depth, and the table
    # shipped before it existed. Added in place so an existing database is not rebuilt.
    have = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_pool_depth)")}
    for column, decl in (("liquidity", "TEXT"),            # uint128, stored as text to stay exact
                         ("sqrt_price_x96", "TEXT"),       # uint160, same reason
                         ("quote_decimals", "INTEGER"),
                         ("quote_is_token0", "INTEGER"),
                         ("quote_usd", "REAL")):
        if column not in have:
            conn.execute("ALTER TABLE memecoin_pool_depth ADD COLUMN %s %s" % (column, decl))
    conn.commit()


def record_pool_depth(conn, chain_id, mint, *, depth_usd, pool, quote, pools_read, now_iso,
                      state=None):
    """Upsert one coin's computed depth, and the pool STATE the impact gate needs.

    `state` is the deepest pool's own reading: liquidity, sqrt_price_x96, quote_decimals,
    quote_is_token0 and quote_usd. It is optional so an older caller keeps working; a row without it
    simply cannot answer the gate, which then fails open."""
    ensure_pool_depth_cache(conn)
    state = state or {}
    cur = conn.execute(
        "INSERT INTO memecoin_pool_depth(chain_id, mint, pool, quote, depth_usd, pools_read, as_of,"
        " liquidity, sqrt_price_x96, quote_decimals, quote_is_token0, quote_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(chain_id, mint) DO UPDATE SET "
        "pool=excluded.pool, quote=excluded.quote, depth_usd=excluded.depth_usd, "
        "pools_read=excluded.pools_read, as_of=excluded.as_of, liquidity=excluded.liquidity, "
        "sqrt_price_x96=excluded.sqrt_price_x96, quote_decimals=excluded.quote_decimals, "
        "quote_is_token0=excluded.quote_is_token0, quote_usd=excluded.quote_usd",
        (str(chain_id), str(mint), (str(pool) if pool else None), (str(quote) if quote else None),
         (float(depth_usd) if depth_usd is not None else None), int(pools_read or 0), str(now_iso),
         (str(state["liquidity"]) if state.get("liquidity") is not None else None),
         (str(state["sqrt_price_x96"]) if state.get("sqrt_price_x96") is not None else None),
         (int(state["quote_decimals"]) if state.get("quote_decimals") is not None else None),
         (1 if state.get("quote_is_token0") else (0 if "quote_is_token0" in state else None)),
         (float(state["quote_usd"]) if state.get("quote_usd") is not None else None)))
    conn.commit()
    return cur.rowcount


def pool_impact_inputs(conn, chain_id, mint, *, now_iso, max_age_s):
    """The cached pool state the impact gate needs, or None. Fail-closed on age, like the depth read.

    Returns None rather than a partial dict: a missing field means the gate cannot compute an
    impact, and the caller must fail OPEN on that, so an incomplete row must not look answerable.
    """
    try:
        row = conn.execute(
            "SELECT liquidity, sqrt_price_x96, quote_decimals, quote_is_token0, quote_usd, as_of "
            "FROM memecoin_pool_depth WHERE chain_id=? AND mint=?",
            (str(chain_id), str(mint))).fetchone()
    except Exception:
        return None
    if not row or any(row[i] is None for i in range(5)) or not row[5]:
        return None
    try:
        from datetime import datetime
        age = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
               - datetime.fromisoformat(str(row[5]).replace("Z", "+00:00"))).total_seconds()
        out = {"liquidity": int(row[0]), "sqrt_price_x96": int(row[1]),
               "quote_decimals": int(row[2]), "quote_is_token0": bool(row[3]),
               "quote_usd": float(row[4]), "as_of": str(row[5]), "age_s": age}
    except (TypeError, ValueError):
        return None
    if age < 0 or (max_age_s and age > float(max_age_s)):
        return None
    if out["liquidity"] <= 0 or out["sqrt_price_x96"] <= 0 or out["quote_usd"] <= 0:
        return None
    return out


def pool_depth_usd(conn, chain_id, mint, *, now_iso, max_age_s):
    """Cached depth for one coin when it is fresh enough, else None. Never raises.

    Fail-closed on ANY problem: the caller's alternative is the honest `unknown` it already had,
    so a stale or unreadable cache must never become a number a gate trusts.
    """
    try:
        row = conn.execute(
            "SELECT depth_usd, as_of FROM memecoin_pool_depth WHERE chain_id=? AND mint=?",
            (str(chain_id), str(mint))).fetchone()
    except Exception:
        return None
    if not row or row[0] is None or not row[1]:
        return None
    try:
        from datetime import datetime
        age = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
               - datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))).total_seconds()
        depth = float(row[0])
    except (TypeError, ValueError):
        return None
    if age < 0 or age > float(max_age_s) or not depth > 0:
        return None
    return depth


def ensure_radar_quote_cache(conn):
    """Coin Radar: background-refreshed quote cache (mcap/liq/vol per coin). A daemon keeps this fresh
    so the /universe endpoint reads it instantly instead of doing rate-limited live fetches per request."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memecoin_radar_quote("
        "chain_id TEXT NOT NULL, mint TEXT NOT NULL, price REAL, market_cap REAL, liquidity REAL, "
        "volume_24h REAL, price_change_h1 REAL, updated_at TEXT, PRIMARY KEY(chain_id, mint))")
    try:
        # additive column for existing caches: 1h price change, used as the classifier's entry momentum
        # (position marks don't exist yet inside the <45-min classify window).
        conn.execute("ALTER TABLE memecoin_radar_quote ADD COLUMN price_change_h1 REAL")
    except sqlite3.OperationalError:
        pass                                          # already present
    conn.commit()


def repair_radar_future_timestamps(conn, *, now_iso, host_skew_s, future_tolerance_s=300.0):
    """Shift impossible future Radar timestamps back by a measured positive host-clock skew.

    The first Radar daemon used host UTC on a machine whose wall clock runs ahead. Only timestamps
    beyond corrected-now plus `future_tolerance_s` are touched; normal rows fail closed unchanged.
    """
    ensure_external_discovery_table(conn)
    ensure_radar_quote_cache(conn)
    try:
        skew_s = float(host_skew_s)
        tolerance_s = float(future_tolerance_s)
        now_dt = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {"external": 0, "quotes": 0}
    if (not math.isfinite(skew_s) or not math.isfinite(tolerance_s)
            or skew_s <= tolerance_s or tolerance_s < 0):
        return {"external": 0, "quotes": 0}
    future_cut = now_dt + timedelta(seconds=tolerance_s)

    def _shift(ts):
        try:
            value = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return ts
        return (value - timedelta(seconds=skew_s)).isoformat() if value > future_cut else ts

    external = 0
    for row in conn.execute(
            "SELECT chain_id,mint,first_seen,last_seen FROM memecoin_external_discovery").fetchall():
        first_seen, last_seen = _shift(row["first_seen"]), _shift(row["last_seen"])
        if first_seen == row["first_seen"] and last_seen == row["last_seen"]:
            continue
        conn.execute(
            "UPDATE memecoin_external_discovery SET first_seen=?,last_seen=? "
            "WHERE chain_id=? AND mint=?",
            (first_seen, last_seen, row["chain_id"], row["mint"]),
        )
        external += 1

    quotes = 0
    for row in conn.execute(
            "SELECT chain_id,mint,updated_at FROM memecoin_radar_quote").fetchall():
        updated_at = _shift(row["updated_at"])
        if updated_at == row["updated_at"]:
            continue
        conn.execute(
            "UPDATE memecoin_radar_quote SET updated_at=? WHERE chain_id=? AND mint=?",
            (updated_at, row["chain_id"], row["mint"]),
        )
        quotes += 1
    conn.commit()
    return {"external": external, "quotes": quotes}


def upsert_radar_quotes(conn, rows, now_iso):
    """Upsert refreshed quotes [{network_id/chain_id, mint, price, market_cap, liquidity, volume_24h}].
    Only non-None fields overwrite (a source that misses liquidity won't wipe a known value)."""
    ensure_radar_quote_cache(conn)
    n = 0
    for r in rows or []:
        chain = str(r.get("network_id") or r.get("chain_id") or "")
        mint = r.get("mint")
        if not chain or not mint:
            continue
        conn.execute(
            "INSERT INTO memecoin_radar_quote(chain_id, mint, price, market_cap, liquidity, volume_24h, "
            " price_change_h1, updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(chain_id, mint) DO UPDATE "
            "SET price=COALESCE(excluded.price, price), market_cap=COALESCE(excluded.market_cap, market_cap), "
            "liquidity=COALESCE(excluded.liquidity, liquidity), "
            "volume_24h=COALESCE(excluded.volume_24h, volume_24h), "
            "price_change_h1=COALESCE(excluded.price_change_h1, price_change_h1), "
            "updated_at=excluded.updated_at",
            (chain, str(mint), r.get("price"), r.get("market_cap"), r.get("liquidity"),
             r.get("volume_24h"), r.get("price_change_h1"), now_iso))
        n += 1
    conn.commit()
    return n


def radar_quote_map(conn, *, now_iso=None, max_age_s=600.0):
    """(chain_id, mint) -> {price, market_cap, liquidity, volume_24h} from the background cache. {} if
    none yet. Rows older than `max_age_s` are excluded so a dead worker cannot serve stale stats as
    current. One query — the endpoint applies it without any live fetch."""
    try:
        ensure_radar_quote_cache(conn)
    except Exception:
        return {}
    try:
        max_age_s = float(max_age_s)
        if not math.isfinite(max_age_s) or max_age_s <= 0:
            return {}
        now_dt = datetime.fromisoformat(str(now_iso or real_utc_now_iso()).replace("Z", "+00:00"))
        cutoff = (now_dt - timedelta(seconds=max_age_s)).isoformat()
    except (TypeError, ValueError, OverflowError):
        return {}
    return {accounting_mark_key(r["chain_id"], r["mint"]): {
                "price": r["price"], "market_cap": r["market_cap"],
                "liquidity": r["liquidity"], "volume_24h": r["volume_24h"],
                "price_change_h1": r["price_change_h1"], "updated_at": r["updated_at"]}
            for r in conn.execute(
                "SELECT chain_id, mint, price, market_cap, liquidity, volume_24h, price_change_h1, "
                "updated_at FROM memecoin_radar_quote WHERE julianday(updated_at) >= julianday(?)",
                (cutoff,))}


def ensure_promotion_lane_table(conn):
    """Lane A — one row per disarmed-wallet signal the promotion shadow evaluated.

    NOTHING IN THE FUNDED BOOK READS THIS. The lane exists because the paper lanes opened a
    position for every signal regardless of the gates (2,242 of 2,346 paper rows in the week to
    2026-09-23 came from REFUSED attempts), so a wallet's paper record answered "were its coins
    good" and never "would we have taken them".

    A row is written for every signal evaluated, admitted or not, so the denominator is the
    population the gates saw rather than the population we chose to keep. `verdict` separates
    `unreadable` from `refused`: a market read we could not make is never the gates saying no.
    `horizon_s` with `censored_by` distinguishes a row the POLICY held to its timecap from one the
    price series ran out under, because a fixed short horizon would systematically censor the
    slower chain's winners.

    AN ADMITTED ROW IS PRICED LATER, NEVER AT EVALUATION. The gates are read a minute after the
    decision and the modelled hold runs for `horizon_target_s` beyond it, so at evaluation the whole
    exit window is still in the future: the first supervised pass (2026-09-23) wrote four admitted
    rows and every one of them found an empty series. `settled_at` is therefore the stamp of the
    SECOND pass, which prices the row once its horizon has elapsed, and `fill_basis` carries the
    three states apart -- `pending` (not due yet), `quoted` (a price series existed) and `unquoted`
    (the horizon passed and nothing ever quoted this mint). `none` is retired: it read as "no fill
    step ran" when it meant "no quote existed".
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memecoin_promotion_lane ("
        "id INTEGER PRIMARY KEY, attempt_id TEXT NOT NULL UNIQUE, origin_event_id TEXT, "
        "episode_id TEXT, network_id TEXT NOT NULL, mint TEXT NOT NULL, ticker TEXT, "
        "source_user_id TEXT, source_handle TEXT, lane_version TEXT, "
        "decision_started_at TEXT NOT NULL, evaluated_at TEXT NOT NULL, eval_lag_s REAL, "
        "verdict TEXT NOT NULL, refused_family TEXT, refused_reason TEXT, "
        "gate_states_json TEXT, gate_inputs_json TEXT, chase_lagged_json TEXT, "
        "market_data_source TEXT, market_data_age_s REAL, "
        "clip_usd REAL, fill_basis TEXT, price_at_signal REAL, fill_price REAL, "
        "fill_impact_pct REAL, quote_usd REAL, "
        "policy_id TEXT, policy_json TEXT, horizon_s REAL, censored_by TEXT, "
        "exit_series_n INTEGER, exit_series_source TEXT, "
        "exit_price REAL, exit_reason TEXT, exit_after_s REAL, pnl_usd REAL, pnl_pct REAL, "
        "horizons_json TEXT, horizon_target_s REAL, settled_at TEXT, created_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_promo_wallet "
                 "ON memecoin_promotion_lane (source_user_id, decision_started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_promo_verdict "
                 "ON memecoin_promotion_lane (verdict, evaluated_at)")
    have = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_promotion_lane)")}
    for column in ("horizon_target_s REAL", "settled_at TEXT", "fill_lag_s REAL",
                   "endpoint_delay_s REAL", "pnl_basis TEXT", "gap_after_s REAL"):
        if column.split()[0] not in have:
            conn.execute("ALTER TABLE memecoin_promotion_lane ADD COLUMN %s" % column)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_promo_settle "
                 "ON memecoin_promotion_lane (verdict, settled_at, decision_started_at)")
    conn.commit()


PROMOTION_LANE_COLUMNS = (
    "attempt_id", "origin_event_id", "episode_id", "network_id", "mint", "ticker",
    "source_user_id", "source_handle", "lane_version", "decision_started_at", "evaluated_at",
    "eval_lag_s", "verdict", "refused_family", "refused_reason", "gate_states_json",
    "gate_inputs_json", "chase_lagged_json", "market_data_source", "market_data_age_s",
    "clip_usd", "fill_basis", "price_at_signal", "fill_price", "fill_impact_pct", "quote_usd",
    "policy_id", "policy_json", "horizon_s", "censored_by", "exit_series_n", "exit_series_source",
    "exit_price", "exit_reason", "exit_after_s", "pnl_usd", "pnl_pct", "horizons_json",
    "horizon_target_s", "settled_at", "fill_lag_s", "endpoint_delay_s", "pnl_basis",
    "gap_after_s", "created_at",
)

# The columns the settle pass owns. Everything else on the row was decided by the gates and must
# never move afterwards: a settle that could rewrite a verdict would let a late price change what we
# said the gates did.
PROMOTION_LANE_SETTLE_COLUMNS = (
    "fill_basis", "fill_price", "fill_impact_pct", "fill_lag_s", "quote_usd", "horizon_s",
    "censored_by",
    "exit_series_n", "exit_series_source", "exit_price", "exit_reason", "exit_after_s",
    "pnl_usd", "pnl_pct", "horizons_json", "settled_at",
    # How late the endpoint reading was, what kind of number `pnl_usd` IS, and where the tape went
    # dark. All three are settle-time facts about the measurement, not about the gates.
    "endpoint_delay_s", "pnl_basis", "gap_after_s",
)


def record_promotion_lane_row(conn, row):
    """Append one evaluated signal. Never raises: a measurement must not disturb a funded sweep.

    `INSERT OR IGNORE` on `attempt_id`, so a worker that re-reads its cursor cannot double-count a
    signal into a promotion statistic.
    """
    try:
        marks = ",".join("?" * len(PROMOTION_LANE_COLUMNS))
        conn.execute(
            "INSERT OR IGNORE INTO memecoin_promotion_lane (%s) VALUES (%s)"
            % (",".join(PROMOTION_LANE_COLUMNS), marks),
            tuple(row.get(name) for name in PROMOTION_LANE_COLUMNS))
        conn.commit()
        return True
    except Exception:
        logger.exception("promotion lane: write failed for %s", row.get("attempt_id"))
        return False


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def promotion_lane_rollup(conn, *, chain=None, since=None):
    """Per-wallet promotion evidence plus the lane's own refusal funnel.

    THE DENOMINATOR IS EVERY EVALUATED ROW. A share computed over the rows we kept would describe
    our own behaviour rather than the wallet's, which is the failure this whole lane exists to
    remove. `unreadable` is its own bucket at every level: a market read we could not make is never
    the gates saying no.

    THREE THINGS AN ADMITTED ROW CAN BE, and they are not two. `closed` carries a price and a PnL.
    `censored` is an open-at-horizon or settled unquoted row, a decided result with no measured
    exit price. `pending` is an admitted row the
    settle pass has not priced yet, which is an UNKNOWN and nothing else; `promotion_lane_rows_
    unsettled` exists precisely because those rows are normal. Folding pending into censored would
    report every fresh row as a decided censoring and make the censored share track how recently
    the settle pass ran.

    ONE ROW PER WALLET PER CHAIN. Chains are never pooled, so the key is
    `(source_user_id, network_id)`: a wallet trading two chains is two rows, as the screen shows it.
    """
    import statistics
    from sniper.services.memecoin_chains import canonical_chain_id, network_ids_for, resolve_chain

    where, args = ["1=1"], []
    if chain:
        key = resolve_chain(chain)
        if key is None:
            raise ValueError(f"unknown chain {chain!r}; chains are never pooled")
        nets = tuple(network_ids_for(key))
        where.append("network_id IN (%s)" % ",".join("?" * len(nets)))
        args.extend(nets)
    if since:
        where.append("decision_started_at>=?")
        args.append(str(since))
    clause = " AND ".join(where)
    have = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_promotion_lane)")}
    settled = "settled_at" if "settled_at" in have else "NULL AS settled_at"
    basis = "pnl_basis" if "pnl_basis" in have else "NULL AS pnl_basis"
    rows = [dict(r) for r in conn.execute(
        "SELECT source_user_id, source_handle, network_id, mint, decision_started_at, evaluated_at, "
        f"       verdict, refused_family, exit_reason, fill_basis, {settled}, pnl_usd, {basis}, "
        f"       eval_lag_s FROM memecoin_promotion_lane WHERE {clause}", tuple(args))]

    by_wallet, families = {}, {}
    for row in rows:
        network_id = canonical_chain_id(row.get("network_id")) or row.get("network_id")
        key = (row.get("source_user_id"), network_id)
        bucket = by_wallet.setdefault(key, {
            "source_user_id": row.get("source_user_id"), "source_handle": row.get("source_handle"),
            "network_id": network_id, "seen": 0, "admitted": 0, "refused": 0,
            "unreadable": 0, "censored": 0, "pending": 0, "_closed_pnl": {},
            "_coins": set(), "_days": set(), "pnl_bases": {}, "_families": {}})
        bucket["seen"] += 1
        verdict = row.get("verdict")
        if verdict == "admitted":
            bucket["admitted"] += 1
            bucket["_coins"].add(row.get("mint"))
            bucket["_days"].add(str(row.get("decision_started_at") or "")[:10])
            if row.get("exit_reason") == "open_at_horizon" or (
                    row.get("settled_at") and row.get("fill_basis") == "unquoted"):
                bucket["censored"] += 1
            elif row.get("pnl_usd") is None:
                bucket["pending"] += 1
            else:
                # PER-COIN GRAIN: the promotion unit is one distinct coin per wallet and chain,
                # so repeated signals on one mint are summed into ONE outcome, never counted as
                # several (a few coins must not dominate the evidence).
                mint = row.get("mint")
                bucket["_closed_pnl"][mint] = bucket["_closed_pnl"].get(mint, 0.0) + float(row["pnl_usd"])
                basis = row.get("pnl_basis") or "unknown"
                bucket["pnl_bases"][basis] = bucket["pnl_bases"].get(basis, 0) + 1
        elif verdict == "unreadable":
            bucket["unreadable"] += 1
        else:
            bucket["refused"] += 1
            family = row.get("refused_family") or "unnamed"
            bucket["_families"][family] = bucket["_families"].get(family, 0) + 1
            families[family] = families.get(family, 0) + 1

    wallets = []
    for bucket in by_wallet.values():
        closed = list(bucket.pop("_closed_pnl").values())
        own = bucket.pop("_families")
        bucket["coins_admitted"] = len(bucket.pop("_coins"))
        bucket["days_admitted"] = len(bucket.pop("_days"))
        bucket["closed"] = len(closed)          # distinct coins with a modeled close
        bucket["median_pnl"] = statistics.median(closed) if closed else None
        bucket["mean_pnl"] = (sum(closed) / len(closed)) if closed else None
        bucket["sum_pnl"] = sum(closed) if closed else None
        bucket["win_pct"] = (100.0 * sum(1 for p in closed if p > 0) / len(closed)) if closed else None
        bucket["admit_pct"] = 100.0 * bucket["admitted"] / bucket["seen"] if bucket["seen"] else None
        # Deterministic on a tie: the biggest family, then the name. `max` alone would break ties by
        # insertion order, so the same data could name two different families on two reads.
        bucket["top_family"] = (sorted(own.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
                                if own else None)
        wallets.append(bucket)
    wallets.sort(key=lambda w: (-(w["admitted"] or 0), -(w["seen"] or 0),
                                str(w["source_handle"] or ""), str(w["network_id"] or "")))

    total_refused = sum(families.values())
    family_rows = [{"family": name, "n": n,
                    "share_pct": (100.0 * n / total_refused) if total_refused else None}
                   for name, n in sorted(families.items(), key=lambda kv: (-kv[1], kv[0]))]
    lags = [float(r["eval_lag_s"]) for r in rows if r.get("eval_lag_s") is not None]
    return {"wallets": wallets, "families": family_rows,
            "lag": {"p50_s": _percentile(lags, 0.5), "p90_s": _percentile(lags, 0.9),
                    "n": len(lags)},
            "window": {"chain": chain, "since": since,
                       "latest_evaluated_at": max((r["evaluated_at"] for r in rows
                                                   if r.get("evaluated_at")), default=None)}}


def promotion_lane_signals(conn, source_user_id, *, chain, since, limit=100):
    """Newest diagnostic rows for one wallet on one canonical chain."""
    from sniper.services.memecoin_chains import network_ids_for, resolve_chain
    key = resolve_chain(chain)
    if key is None:
        raise ValueError(f"unknown chain {chain!r}; chains are never pooled")
    nets = tuple(network_ids_for(key))
    have = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_promotion_lane)")}
    settled = "settled_at" if "settled_at" in have else "NULL AS settled_at"
    basis = "pnl_basis" if "pnl_basis" in have else "NULL AS pnl_basis"
    return [dict(r) for r in conn.execute(
        "SELECT attempt_id, network_id, mint, ticker, decision_started_at, evaluated_at, "
        "eval_lag_s, verdict, refused_family, refused_reason, fill_basis, exit_reason, "
        f"censored_by, pnl_usd, {basis}, {settled} "
        "FROM memecoin_promotion_lane WHERE source_user_id=? "
        f"AND network_id IN ({','.join('?' * len(nets))}) AND decision_started_at>=? "
        "ORDER BY decision_started_at DESC LIMIT ?",
        (str(source_user_id), *nets, str(since), int(limit)))]


def promotion_lane_rows_unsettled(conn, *, limit=200):
    """Admitted lane rows nobody has priced yet, oldest decision first.

    ⚠️ NO DATE ARITHMETIC IN SQL. `datetime()` renders a space where our stamps carry 'T', and
    'T' > ' ', so a `datetime()` bound silently excludes every row. WHICH of these is due is decided
    by the caller in Python, from the row's own `horizon_target_s`.

    `fill_basis IS NULL OR IN ('pending','none')` carries the four rows the first supervised pass
    wrote before the settle pass existed; `settled_at IS NULL` is what makes a second worker pass a
    no-op rather than a second opinion.
    """
    return [dict(r) for r in conn.execute(
        "SELECT attempt_id, network_id, mint, ticker, decision_started_at, horizon_target_s, "
        "       clip_usd, policy_json, price_at_signal, fill_price, fill_lag_s, exit_series_source "
        "  FROM memecoin_promotion_lane "
        " WHERE verdict='admitted' AND settled_at IS NULL "
        "   AND (fill_basis IS NULL OR fill_basis IN ('pending','none')) "
        " ORDER BY decision_started_at LIMIT ?", (int(limit),))]


def promotion_lane_rows_open_for_sampling(conn, *, limit=200, network_id=None):
    """Admitted rows nobody has priced yet, the ones that still need a FILL first.

    ⚠️ THE CHOICE HAPPENS BEFORE THE LIMIT (Codex review N7). This used to take the oldest 200 rows
    and let the caller prioritise afterwards, so a newly admitted row outside those 200 never
    reached the sort at all — and the fill IS the first priced sample, so an invisible row does not
    lose a price, it moves its fill hours past its decision and quietly changes what it measures.

    ⚠️ AN ATTEMPT IS NOT A FILL. The caller's old priority counted any sample row, so a row whose
    every quote had failed looked served. The order here is: rows with no PRICED sample first, then
    by how long ago we last ASKED (never asked sorts first), then oldest decision.

    Whether a row is still INSIDE its horizon is decided by the caller in Python, for the same
    reason the settle selector does it there: a `datetime()` bound compares a space-separated
    rendering against our 'T' stamps and is silently false for every row.
    """
    network_filter = " AND l.network_id=? " if network_id is not None else ""
    params = (str(network_id), int(limit)) if network_id is not None else (int(limit),)
    query = (
        "SELECT l.attempt_id, l.network_id, l.mint, l.ticker, l.decision_started_at, "
        "       l.horizon_target_s, l.clip_usd, l.policy_json, l.fill_price, "
        "       l.fill_lag_s, l.exit_series_source "
        "  FROM memecoin_promotion_lane l "
        "  LEFT JOIN (SELECT attempt_id, MAX(sampled_at) AS last_attempt_at, "
        "                    MAX(CASE WHEN price IS NOT NULL AND price > 0 THEN 1 ELSE 0 END) "
        "                      AS has_price "
        "               FROM memecoin_promotion_lane_samples GROUP BY attempt_id) s "
        "    ON s.attempt_id = l.attempt_id "
        " WHERE l.verdict='admitted' AND l.settled_at IS NULL "
        + network_filter +
        " ORDER BY COALESCE(s.has_price, 0), COALESCE(s.last_attempt_at, ''), "
        "          l.decision_started_at LIMIT ?")
    try:
        return [dict(r) for r in conn.execute(query, params)]
    except sqlite3.OperationalError:
        # The samples table has never been created on this database: every row is unpriced.
        return [dict(r) for r in conn.execute(
            "SELECT attempt_id, network_id, mint, ticker, decision_started_at, horizon_target_s, "
            "       clip_usd, policy_json, fill_price, fill_lag_s, exit_series_source "
            "  FROM memecoin_promotion_lane "
            " WHERE verdict='admitted' AND settled_at IS NULL "
            + network_filter.replace("l.network_id", "network_id") +
            " ORDER BY decision_started_at LIMIT ?", params)]


def claim_promotion_lane_fill(conn, attempt_id, *, source, price, lag_s):
    """Keep the first observed model fill identity before a later-stamped mark arrives."""
    cur = conn.execute(
        "UPDATE memecoin_promotion_lane SET exit_series_source=?, fill_price=?, fill_lag_s=? "
        "WHERE attempt_id=? AND verdict='admitted' AND settled_at IS NULL AND fill_price IS NULL",
        (source, price, lag_s, attempt_id))
    conn.commit()
    return cur.rowcount == 1


def settle_promotion_lane_row(conn, attempt_id, fields):
    """Write the priced half of one admitted row. Returns True only when THIS call priced it.

    Guarded on `settled_at IS NULL`, so two overlapping passes cannot both write and the second one
    reports False rather than silently replacing the first one's price. Never raises: a measurement
    must not disturb a funded sweep.
    """
    try:
        names = [name for name in PROMOTION_LANE_SETTLE_COLUMNS if name in fields]
        if "settled_at" not in names:
            return False
        assignment = ",".join("%s=?" % name for name in names)
        cur = conn.execute(
            "UPDATE memecoin_promotion_lane SET " + assignment +
            " WHERE attempt_id=? AND verdict='admitted' AND settled_at IS NULL",
            (*[fields.get(name) for name in names], str(attempt_id)))
        conn.commit()
        return bool(cur.rowcount)
    except Exception:
        logger.exception("promotion lane: settle failed for %s", attempt_id)
        return False


def ensure_promotion_lane_samples_table(conn):
    """Lane A's OWN price series for the coins it admitted. Its own table, on purpose.

    WHY THIS EXISTS. `memecoin_quote_ledger` is written by the scanner sweep over the mints it
    already prices, so it covers coins we took and almost nothing else: of 895 `not_approved`
    attempts in the 24 h to 2026-09-23, 178 of the 179 covered mints were mints we had opened a
    position on, against 1 of 113 we never held. Lane A admits exactly the coins we did NOT take, so
    pricing it from that ledger would measure only the subset we had already chosen -- the failure
    the promotion pre-registration forbids by name.

    ⚠️ THE PRICE HERE IS NOT IN THE LEDGER'S UNIT, AND THE TWO MAY NEVER BE CONCATENATED.
    `price_unit` says which: a Solana lane quote is `usd_per_atomic_token`, because converting to
    USD per whole token needs the mint's decimals and that is a Solana RPC read this worker is not
    allowed to make unattended. Ratios inside one source are arithmetically comparable buy marks;
    they do not establish an executable sell PnL. An
    absolute price is off by 10**decimals. The settle pass therefore picks ONE source per row and
    labels it, never a union of the two.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memecoin_promotion_lane_samples ("
        "id INTEGER PRIMARY KEY, attempt_id TEXT NOT NULL, network_id TEXT NOT NULL, "
        "mint TEXT NOT NULL, sampled_at TEXT NOT NULL, scheduled_at TEXT, price REAL, "
        "price_basis TEXT, price_unit TEXT, quote_usd REAL, out_atomic TEXT, rtt_ms REAL, "
        "source TEXT, reason TEXT, UNIQUE(attempt_id, sampled_at))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_promo_sample_attempt "
                 "ON memecoin_promotion_lane_samples (attempt_id, sampled_at)")
    # `sampled_at` is WHEN THE ANSWER CAME BACK; `scheduled_at` is when the pass decided to ask.
    # They were one number, taken before the request, so every row after the first in a pass was
    # stamped early by the sum of the preceding round trips. Rows written before this column
    # exists keep NULL: they were stamped the old way and cannot be repaired.
    have = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_promotion_lane_samples)")}
    if "scheduled_at" not in have:
        conn.execute("ALTER TABLE memecoin_promotion_lane_samples ADD COLUMN scheduled_at TEXT")
    conn.commit()


def record_promotion_lane_sample(conn, sample):
    """Append one lane quote, price or no price. Never raises.

    A failed quote is recorded WITH ITS REASON and a NULL price, never dropped and never a zero: a
    mint that stopped routing is a fact about the coin, and a table that only holds successes would
    make every coverage share describe the quotes that happened to work.
    """
    columns = ("attempt_id", "network_id", "mint", "sampled_at", "scheduled_at", "price",
               "price_basis", "price_unit", "quote_usd", "out_atomic", "rtt_ms", "source",
               "reason")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO memecoin_promotion_lane_samples (%s) VALUES (%s)"
            % (",".join(columns), ",".join("?" * len(columns))),
            tuple(sample.get(name) for name in columns))
        conn.commit()
        return True
    except Exception:
        logger.exception("promotion lane: sample write failed for %s", sample.get("attempt_id"))
        return False


def promotion_lane_samples(conn, attempt_id):
    """Every lane sample for one row, oldest first, PRICED AND FAILED ALIKE.

    ⚠️ THE FAILURES BELONG IN THE SERIES (Codex review N5). This reader used to filter
    `price IS NOT NULL AND price > 0`, so a rate-limited quote vanished and the replay's
    `bar_at_or_before` left the previous healthy price standing across the hole — which let ONE
    observation satisfy a two-sweep stop confirmation. A failed quote carries its `reason` and a
    NULL price; the caller stops the replay there rather than carrying a price it never saw.
    A caller that wants only prices must filter on `price`, and it must do so knowing what it drops.

    A missing table reads as no samples, not as an error: the settle pass runs against databases
    where the sampler has never written, and raising there would strand every row it could still
    have priced from the ledger.
    """
    try:
        return [dict(r) for r in conn.execute(
            "SELECT sampled_at, scheduled_at, price, price_unit, source, reason "
            "  FROM memecoin_promotion_lane_samples "
            " WHERE attempt_id=? ORDER BY sampled_at", (str(attempt_id),))]
    except sqlite3.OperationalError:
        return []


def ensure_gmgn_shadow_table(conn):
    """GMGN facts recorded beside our own, for the shadow comparison. NOTHING READS THIS.

    Own table, so the measurement cannot reach a gate, an exit or a mark by accident. One row per
    read; `facts_json` holds the whole fact dict verbatim rather than a chosen subset, because the
    point of the shadow is to find out which fields matter and a column list would decide that in
    advance.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memecoin_gmgn_shadow ("
        "id INTEGER PRIMARY KEY, chain_id TEXT NOT NULL, mint TEXT NOT NULL, ts TEXT NOT NULL, "
        "price REAL, market_cap REAL, liquidity REAL, volume_m5 REAL, volume_h1 REAL, "
        "holder_count REAL, launchpad TEXT, top_10_holder_rate REAL, "
        "pair_address TEXT, facts_json TEXT, rtt_ms REAL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_gmgn_shadow_mint "
                 "ON memecoin_gmgn_shadow (chain_id, mint, ts)")
    conn.commit()


def ensure_token_social_table(conn):
    """Coin Radar #4: cached token socials (twitter/telegram/website + DexScreener boost count) so
    'is anyone talking about this?' is a live column, not a manual lookup. Solana/Base only (4663 blind)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memecoin_token_social("
        "chain_id TEXT NOT NULL, mint TEXT NOT NULL, twitter TEXT, telegram TEXT, website TEXT, "
        "boosts INTEGER DEFAULT 0, updated_at TEXT, PRIMARY KEY(chain_id, mint))")
    conn.commit()


def upsert_token_socials(conn, socials, now_iso):
    """Upsert {(chain, mint): {twitter, telegram, website, boosts}}. Returns count written."""
    ensure_token_social_table(conn)
    n = 0
    for (chain, mint), s in (socials or {}).items():
        conn.execute(
            "INSERT INTO memecoin_token_social(chain_id, mint, twitter, telegram, website, boosts, "
            " updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(chain_id, mint) DO UPDATE SET "
            "twitter=excluded.twitter, telegram=excluded.telegram, website=excluded.website, "
            "boosts=excluded.boosts, updated_at=excluded.updated_at",
            (str(chain), str(mint), s.get("twitter"), s.get("telegram"), s.get("website"),
             int(s.get("boosts") or 0), now_iso))
        n += 1
    conn.commit()
    return n


def token_social_map(conn, *, now_iso=None, stale_h=48.0):
    """accounting_mark_key -> {twitter, telegram, website, boosts, has_socials, age_h, stale} from the
    cache. {} if none. `age_h`/`stale` let the UI flag socials that predate a possible rug (stale after
    `stale_h` — slower-moving than triage)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='memecoin_token_social'").fetchone():
        return {}
    out = {}
    for r in conn.execute("SELECT chain_id, mint, twitter, telegram, website, boosts, updated_at "
                          "FROM memecoin_token_social"):
        age = _age_hours(r["updated_at"], now_iso)
        out[accounting_mark_key(r["chain_id"], r["mint"])] = {
            "twitter": r["twitter"], "telegram": r["telegram"], "website": r["website"],
            "boosts": r["boosts"] or 0,
            "has_socials": bool(r["twitter"] or r["telegram"] or r["website"]),
            "age_h": round(age, 1) if age is not None else None,
            "stale": bool(age is not None and age > float(stale_h))}
    return out


def ensure_radar_triage_table(conn):
    """Coin Radar Stage 3: coin-level LLM triage verdicts (promising/watch/skip + reason). One row per
    (chain, mint); drafts for operator confirm, never auto-trades."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memecoin_radar_triage("
        "chain_id TEXT NOT NULL, mint TEXT NOT NULL, verdict TEXT, reason TEXT, model TEXT, "
        "created_at TEXT, PRIMARY KEY(chain_id, mint))")
    conn.commit()


def upsert_radar_triage(conn, verdicts, now_iso):
    """Upsert [{network_id/chain_id, mint, verdict, reason, model}]. Returns count written."""
    ensure_radar_triage_table(conn)
    n = 0
    for v in verdicts or []:
        raw_chain = v.get("network_id") or v.get("chain_id") or ""
        raw_mint = v.get("mint")
        if not raw_chain or not raw_mint:
            continue
        # Store under the canonical key so the PK dedups cross-source triage of the same coin (a
        # checksummed EVM mint and its lowercase feed twin must NOT become two rows), and so the
        # read map keys identically to socials/quotes.
        chain, mint = accounting_mark_key(raw_chain, raw_mint)
        conn.execute(
            "INSERT INTO memecoin_radar_triage(chain_id, mint, verdict, reason, model, created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(chain_id, mint) DO UPDATE SET "
            "verdict=excluded.verdict, reason=excluded.reason, model=excluded.model, "
            "created_at=excluded.created_at",
            (chain, str(mint), v.get("verdict"), v.get("reason"), v.get("model"), now_iso))
        n += 1
    conn.commit()
    return n


def _age_hours(created_at, now_iso):
    """Hours between an ISO `created_at` and now (real UTC), or None if unparseable."""
    try:
        now_dt = datetime.fromisoformat(str(now_iso or real_utc_now_iso()).replace("Z", "+00:00"))
        c_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return max(0.0, (now_dt - c_dt).total_seconds() / 3600.0)
    except (TypeError, ValueError):
        return None


def radar_triage_map(conn, *, now_iso=None, stale_h=12.0):
    """accounting_mark_key -> {verdict, reason, age_h, stale} for the latest triage of each coin. {} if
    none. `age_h`/`stale` let the UI dim a days-old verdict — an LLM 'promising' on a now-dead memecoin
    must not read as current (stale after `stale_h`)."""
    try:
        ensure_radar_triage_table(conn)
    except Exception:
        return {}
    # Keyed by accounting_mark_key (rows are stored canonically by upsert_radar_triage) so the /universe
    # route's accounting_mark_key lookup hits — previously raw keys silently dropped the badge on any
    # coin whose display row differed in mint-case/network-label from the triage-write row.
    out = {}
    for r in conn.execute("SELECT chain_id, mint, verdict, reason, created_at FROM memecoin_radar_triage"):
        age = _age_hours(r["created_at"], now_iso)
        out[accounting_mark_key(r["chain_id"], r["mint"])] = {
            "verdict": r["verdict"], "reason": r["reason"],
            "age_h": round(age, 1) if age is not None else None,
            "stale": bool(age is not None and age > float(stale_h))}
    return out


def ticker_for_mint(conn, mint):
    """Resolve a display ticker/name for a mint from the feed observations (which carry it ~always).
    The position row's `ticker` is blank for ~1/3 of entries (the copy event lacked it), yet the same
    mint has a named observation. Returns the most recent non-empty ticker, or None."""
    if not mint:
        return None
    try:
        r = conn.execute(
            "SELECT ticker FROM memecoin_feed_observations_v2 "
            "WHERE mint=? AND COALESCE(ticker,'')!='' ORDER BY id DESC LIMIT 1",
            (str(mint),)).fetchone()
    except Exception:
        return None
    return (r["ticker"] if r else None) or None


def set_position_ticker(conn, position_id, ticker):
    """Backfill a resolved ticker onto a position whose stored ticker was blank, so it persists for the
    UI, Telegram signals, and closed-trade history instead of re-resolving every render. No-op on empty."""
    ticker = (ticker or "").strip()
    if not ticker:
        return
    conn.execute("UPDATE memecoin_positions SET ticker=? WHERE id=? AND COALESCE(ticker,'')=''",
                 (ticker, position_id))
    conn.commit()


def cancel_exit_target(conn, position_id, now_iso, *, commit=True):
    """Cancel any active mcap-band target for a position. Used to enforce ONE take-profit control
    (2026-07-18): a mcap band and an override tp_target_mcap are both mcap take-profits, and the band
    has display + sweep precedence — so setting/clearing a TP override must supersede a stale band, or
    the operator's new TP is silently shadowed (KITTY: a 07-13 5.5-6.5M band outranked a 07-18 $4.6M
    TP). Returns the number of bands cancelled."""
    cur = conn.execute("UPDATE memecoin_exit_targets SET status='cancelled', resolved_at=? "
                       "WHERE position_id=? AND status='active'", (now_iso, position_id))
    if commit:
        conn.commit()
    return cur.rowcount


def update_position_last_quote(conn, position_id, price, ts):
    """Record the most recent live mid we saw for this open position, so a time cap can still
    close it at a real (if stale) price when the mint later goes unquotable."""
    conn.execute("UPDATE memecoin_positions SET last_quote_price=?, last_quote_ts=? WHERE id=?",
                 (price, ts, position_id))
    conn.commit()


def mark_stuck_bag(conn, position_id):
    """Flag a real position whose sell ladder exhausted (illiquid/rug). It stays OPEN (un-exited)
    so the sweep can retry and the operator is alerted — never a fake clean close."""
    conn.execute("UPDATE memecoin_positions SET stuck_bag=1 WHERE id=?", (position_id,))
    conn.commit()


def clear_stuck_bag(conn, position_id, *, now_iso=None, quiet_s=None):
    """Un-flag a position whose bag is demonstrably routable again. True when a row changed.

    AN EXECUTION FAILURE OUTRANKS A QUOTE (2026-09-23, Lambda). A stuck attempt younger than
    `quiet_s` (default MEMECOIN_STUCK_BACKOFF_CEILING_S) keeps the flag: RATBRAIN 64081's whole-bag
    quote came from the same Kyber route whose transactions were reverting, so a clear on every
    sweep would have flapped the flag and reset `stuck_backoff` each time, which is the 65-attempt
    loop of position 46888. With `now_iso` None the guard is skipped (older callers, tests).

    `mark_stuck_bag` was WRITE-ONCE: nothing in the tree ever set this column back to 0, so a
    position stayed flagged long after it became sellable. Three things ride on the flag, so a stale
    1 is not cosmetic -- it keeps paging the operator, it reads as DEAD in `stuck_bag_report`
    ("capital is effectively gone"), and `stuck_backoff` uses it to DELAY the next exit attempt on a
    position that can now exit. RATBRAIN 64081 on 2026-09-23 sat flagged at -13.4% against a -35%
    stop, with a whole-bag preview pricing it at $26.91 and no exit attempt for eleven minutes.

    ONLY A FRESH WHOLE-BAG ROUTE QUOTE MAY CLEAR IT. A mark, a mid or a display price must not: those
    are exactly the prices that still look healthy on a bag nobody can sell, which is the condition
    the flag exists to report. Clearing on a mid would delete the alert precisely when it is true.
    """
    if now_iso is not None:
        if quiet_s is None:
            from sniper.settings import memecoin as cfg
            quiet_s = float(getattr(cfg, "MEMECOIN_STUCK_BACKOFF_CEILING_S", 120.0) or 120.0)
        row = conn.execute(
            "SELECT MAX(attempted_at) FROM memecoin_exit_execution_attempts "
            "WHERE position_id=? AND status='stuck'", (position_id,)).fetchone()
        newest = row[0] if row else None
        if newest:
            try:
                from datetime import datetime
                age = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                       - datetime.fromisoformat(str(newest).replace("Z", "+00:00"))).total_seconds()
            except (TypeError, ValueError):
                age = None
            if age is not None and 0 <= age < float(quiet_s):
                return False
    cur = conn.execute("UPDATE memecoin_positions SET stuck_bag=0 WHERE id=? AND stuck_bag=1",
                       (position_id,))
    conn.commit()
    return cur.rowcount > 0


def close_submission_state(position):
    """Normalized real-close lifecycle state for recovery, tests, and operator surfaces."""
    if not position:
        return None
    if position.get("closed_at") is not None:
        return "settled"
    if position.get("close_sig"):
        return "submitted_unknown"
    if position.get("close_claim_id"):
        return "pending_unsigned"
    return "idle"


def claim_position_close(conn, position_id, at, exit_reason):
    """Own a real full close before signing; return an opaque token or None.

    Unlike claim_close(), this does not falsify settled ledger state by setting is_open=0. A loser
    cannot reach signing, and a submitted claim remains owned across process death.
    """
    claim_id = uuid.uuid4().hex
    cur = conn.execute(
        "UPDATE memecoin_positions SET close_pending_at=?, close_claim_id=?, close_exit_reason=? "
        "WHERE id=? AND is_open=1 AND close_claim_id IS NULL AND close_sig IS NULL "
        "AND derisk_claim_id IS NULL AND derisk_sig IS NULL "
        "AND (?='manual' OR COALESCE(tracking_paused_at,'')='') "
        "AND (? NOT IN ('mirror','mirror_source_sell') OR COALESCE(mirror_disabled,0)=0)",
        (at, claim_id, exit_reason, position_id, exit_reason, exit_reason))
    conn.commit()
    return claim_id if cur.rowcount == 1 else None


def release_position_close_claim(conn, position_id, claim_id):
    """Release only this owner's unsigned claim. A signed identity can never pass this predicate."""
    cur = conn.execute(
        "UPDATE memecoin_positions SET close_pending_at=NULL, close_claim_id=NULL, "
        "close_exit_reason=NULL WHERE id=? AND is_open=1 AND close_claim_id=? AND close_sig IS NULL",
        (position_id, claim_id))
    conn.commit()
    return cur.rowcount == 1


def release_dropped_close_claim(conn, position_id, claim_id, sig):
    """Release a SIGNED close claim whose transaction provably died.

    `release_position_close_claim` refuses a signed identity on purpose: a broadcast may have landed,
    so a blind release re-arms the exit and sells the same tokens twice. This variant exists for the
    one case where that ambiguity is resolved — the caller has proven the chain is past the intent's
    `last_valid_height` (it can never land) AND the full bag is still held (it never did).

    Without it a dropped sell strands the position for good: the rails stop retrying and the
    operator's close button answers `already_closing` forever. Live on 2026-08-14, position 37331
    (RIDEABULL) at +48%, freed only by a hand-written UPDATE.

    CAS on BOTH the claim id and the signature, so a stale recovery pass cannot release a claim that
    has since been replaced. Refuses a position that already carries terminal close bookkeeping."""
    try:
        cur = conn.execute(
            "UPDATE memecoin_positions SET close_pending_at=NULL, close_claim_id=NULL, close_sig=NULL, "
            "close_submitted_at=NULL, close_exit_reason=NULL, exit_intent_json=NULL "
            "WHERE id=? AND is_open=1 AND closed_at IS NULL AND close_claim_id=? AND close_sig=? "
            "RETURNING signal_source,execution_mode,desk_cohort_id",
            (position_id, claim_id, sig))
        changed = cur.fetchall()
        if changed and changed[0][0] == SIGNAL_SOURCE_DESK and changed[0][1] == "real" and changed[0][2]:
            _mark_desk_execution_cost(conn, position_id, f"exit:{position_id}:{sig}", real_utc_now_iso())
        conn.commit()
        return len(changed) == 1
    except Exception:
        conn.rollback()
        raise


def enqueue_entry_retry(conn, fomo_event_id, mint, network_id, event_json, now_iso,
                        *, retry_after_s, blind=False):
    """Queue a fresh-pool buy for re-quote, or refresh its next-attempt time if already queued.

    Idempotent on fomo_event_id: a coin that misses again just gets a later next_attempt_at and a
    bumped attempts count, never a duplicate row. first_queued_at is set once and anchors the
    window. Never raises inside the caller's flow — a failed enqueue simply means the miss was
    one-shot, exactly the old behaviour."""
    try:
        conn.execute(
            "INSERT INTO memecoin_entry_retry_queue "
            "(fomo_event_id, mint, network_id, first_queued_at, next_attempt_at, attempts, "
            "event_json, blind_misses) "
            "VALUES (?,?,?,?,?,0,?,?) "
            "ON CONFLICT(fomo_event_id) DO UPDATE SET "
            "next_attempt_at=excluded.next_attempt_at, attempts=memecoin_entry_retry_queue.attempts+1, "
            # A row that has EVER missed blindly keeps the longer budget: the outage it is waiting
            # out does not stop being an outage because one later attempt reached a real verdict.
            "blind_misses=memecoin_entry_retry_queue.blind_misses+excluded.blind_misses",
            (fomo_event_id, mint, network_id, now_iso,
             _shift_iso(now_iso, retry_after_s), event_json, 1 if blind else 0))
        conn.commit()
        return True
    except Exception:
        logger.exception("entry retry enqueue failed for %s", fomo_event_id)
        return False


def due_entry_retries(conn, now_iso, *, window_s, max_attempts, limit=16, network_id=None,
                      excluded_coins=()):
    """Rows whose next_attempt_at has passed AND that are still inside the window and under the
    attempt cap. Expired/over-cap rows are NOT returned here — reap_entry_retries removes them so
    a terminal verdict is recorded exactly once by the caller."""
    rows = (dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_entry_retry_queue "
        "WHERE next_attempt_at <= ? AND attempts < ? "
        "AND first_queued_at >= ? AND dip_json IS NULL "
        "AND (? IS NULL OR network_id = ?) "
        "ORDER BY first_queued_at LIMIT ?",
        (now_iso, max_attempts, _shift_iso(now_iso, -window_s), network_id, network_id,
         -1 if excluded_coins else limit)))
    if not excluded_coins:
        return list(rows)
    from itertools import islice
    # Filter before the eligible limit so held coins cannot starve unrelated retries.
    available = (r for r in rows if coin_identity(r["network_id"], r["mint"]) not in excluded_coins)
    return list(available if limit < 0 else islice(available, limit))


def reap_entry_retries(conn, now_iso, *, window_s, max_attempts, blind_window_s=None,
                       network_id=None):
    """Return and delete rows that are past the window or over the attempt cap — the caller stamps
    a final 'gave up' decision on each. One shot: a reaped row is gone.

    `blind_window_s` is the budget for a row that has missed because a PROVIDER could not be read,
    as opposed to a pool that is not indexed yet. Default None means one budget for both, exactly
    as before. The attempt cap is shared: a longer clock must not also buy more attempts.
    """
    blind_cut = _shift_iso(now_iso, -(window_s if blind_window_s is None
                                      else max(window_s, float(blind_window_s))))
    stale = [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_entry_retry_queue "
        "WHERE dip_json IS NULL AND (attempts >= ? "
        "   OR (COALESCE(blind_misses,0) = 0 AND first_queued_at < ?) "
        "   OR (COALESCE(blind_misses,0) > 0 AND first_queued_at < ?)) "
        "AND (? IS NULL OR network_id = ?)",
        (max_attempts, _shift_iso(now_iso, -window_s), blind_cut, network_id, network_id))]
    if stale:
        conn.executemany(
            "DELETE FROM memecoin_entry_retry_queue WHERE fomo_event_id=?",
            [(r["fomo_event_id"],) for r in stale])
        conn.commit()
    return stale


def enqueue_dip_retry(conn, fomo_event_id, mint, network_id, event_json, now_iso, dip_json):
    """Queue one dip row. A repeat refusal of the same event keeps the first row (its deadline is
    the signal's). Never raises: a failed enqueue means no dip retry, which is the old behaviour."""
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO memecoin_entry_retry_queue (fomo_event_id, mint, network_id, "
            "first_queued_at, next_attempt_at, attempts, event_json, blind_misses, dip_json) "
            "VALUES (?,?,?,?,?,0,?,0,?)",
            (fomo_event_id, mint, network_id, now_iso, now_iso, event_json, dip_json))
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        logger.exception("dip retry enqueue failed for %s", fomo_event_id)
        return False


def dip_retry_source_seen(conn, network_id, source_tx):
    """True when this chain's dip retry already queued or logged a signal about `source_tx`,
    whichever lane delivered it (J7 and the on-chain lane carry the same source transaction). Both
    tables are small; a LIKE over the event id is bounded by them."""
    needle = "%:" + str(source_tx) + ":%"
    for row in conn.execute(
            "SELECT 1 FROM memecoin_entry_retry_queue WHERE dip_json IS NOT NULL AND network_id=? "
            "AND fomo_event_id LIKE ? LIMIT 1", (str(network_id), needle)):
        return True
    for row in conn.execute(
            "SELECT 1 FROM memecoin_dip_retry_log WHERE COALESCE(network_id,'solana')=? "
            "AND fomo_event_id LIKE ? LIMIT 1", (str(network_id), needle)):
        return True
    return False


def dip_retry_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT fomo_event_id, mint, network_id, attempts, event_json, dip_json "
        "FROM memecoin_entry_retry_queue WHERE dip_json IS NOT NULL ORDER BY first_queued_at")]


def bump_dip_retry(conn, fomo_event_id):
    conn.execute("UPDATE memecoin_entry_retry_queue SET attempts=attempts+1 WHERE fomo_event_id=?",
                 (fomo_event_id,))
    conn.commit()


def record_dip_retry_hit(conn, *, fomo_event_id, source_user_id, mint, hit_at, watch_premium_pct,
                         cap_pct, status, reason, position_id, network_id=None, stats=None,
                         gate_cap_pct=None, cap_source=None):
    """One dip log row: a hit's verdict, or a row's terminal outcome. `stats` is the watch's
    {min_premium_pct, quotes, unknown, series} for the row; None when the watch never saw it
    (restart). `series` is its executable ticks, tick 0 being the refusal's own quote."""
    stats = stats or {}
    conn.execute(
        "INSERT INTO memecoin_dip_retry_log (fomo_event_id, source_user_id, mint, hit_at, "
        "watch_premium_pct, cap_pct, status, reason, position_id, network_id, "
        "min_watch_premium_pct, watch_quotes, watch_unknown, watch_series_json, gate_cap_pct, cap_source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fomo_event_id, source_user_id, mint, hit_at, watch_premium_pct, cap_pct, status, reason,
         position_id, network_id, stats.get("min_premium_pct"), stats.get("quotes"),
         stats.get("unknown"),
         _json.dumps(stats["series"], separators=(",", ":")) if stats.get("series") else None,
         gate_cap_pct, cap_source))
    conn.commit()


def source_sold_since(conn, source_user_id, mint, network_id, since_epoch):
    """True when this source sold this mint after `since_epoch`, on J7 (memecoin_events) or the
    watcher (onchain_wallet_sells). Times are parsed here: the two tables stamp ISO with 'Z' and
    '+00:00' respectively, and a string comparison across the two is wrong below the second."""
    from datetime import datetime

    def epoch(iso):
        try:
            return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return None
    for (created,) in conn.execute(
            "SELECT created_at FROM memecoin_events WHERE source_user_id=? AND mint=? "
            "AND side='sell' AND network_id=?", (source_user_id, mint, network_id)):
        at = epoch(created)
        if at is not None and at > since_epoch:
            return True
    for block_time, detected in conn.execute(
            "SELECT block_time, detected_at FROM onchain_wallet_sells WHERE source_user_id=? "
            "AND mint=? AND network_id=?", (source_user_id, mint, network_id)):
        at = float(block_time) if block_time else epoch(detected)
        if at is not None and at > since_epoch:
            return True
    if str(network_id) != "solana":
        # The EVM watcher's proven sales (2026-09-30). No block time is stored, so its detection
        # time: lag only moves a sale later, so a late-detected pre-signal sale stops the row too.
        for (detected,) in conn.execute(
                "SELECT detected_at FROM onchain_evm_sell_shadow WHERE source_user_id=? "
                "AND mint=? AND network_id=?", (source_user_id, mint, network_id)):
            at = epoch(detected)
            if at is not None and at > since_epoch:
                return True
    return False


def entry_tx_sig(conn, position_id):
    """The signature of a position's entry fill, or None."""
    row = conn.execute("SELECT tx_sig FROM memecoin_fills WHERE position_id=? AND kind='entry' "
                       "ORDER BY id LIMIT 1", (position_id,)).fetchone()
    return row[0] if row and row[0] else None


def dequeue_entry_retry(conn, fomo_event_id):
    """Remove one row (fill, or a non-transient verdict). Idempotent."""
    conn.execute("DELETE FROM memecoin_entry_retry_queue WHERE fomo_event_id=?", (fomo_event_id,))
    conn.commit()


def record_entry_route_miss(conn, *, mint, network_id, code, detail, rungs, observed_at):
    """Append one entry-quote-ladder miss: the code, the detail, and every rung's own trace.

    Append-only, one row per attempt (including retries) -- a coin that misses five times in its
    retry window leaves five rows, which is the point: the pattern across attempts (the same rung
    blind every time vs. a different one each time) is itself evidence. Never raises; a failed
    write must not touch the trading decision that already happened."""
    import json as _json
    try:
        conn.execute(
            "INSERT INTO memecoin_entry_route_misses "
            "(mint, network_id, observed_at, code, detail, rungs_json) VALUES (?,?,?,?,?,?)",
            (mint, network_id, observed_at, code, detail,
             _json.dumps(list(rungs or []), default=str)))
        conn.commit()
    except Exception:
        logger.exception("entry route miss record failed for %s/%s", network_id, mint)


def bump_entry_retry(conn, fomo_event_id, now_iso, *, retry_after_s):
    """A retry attempt missed again but is still in-window: schedule the next one."""
    conn.execute(
        "UPDATE memecoin_entry_retry_queue SET attempts=attempts+1, next_attempt_at=? "
        "WHERE fomo_event_id=?",
        (_shift_iso(now_iso, retry_after_s), fomo_event_id))
    conn.commit()


def bind_exit_intent(conn, position_id, claim_id, intent_json):
    """WO-6: persist the immutable ExitIntent for the CURRENT owner's claim. CAS-guarded on the live
    claim id (close or derisk) so a stale executor cannot overwrite a replacement claim's intent."""
    try:
        intent = _json.loads(intent_json)
        action = str(intent["action"])
        claim_col = {"close": "close_claim_id", "derisk": "derisk_claim_id", "partial": "derisk_claim_id"}.get(action)
        if (claim_col is None or str(intent.get("claim_id") or "") != str(claim_id)
                or int(intent.get("position_id")) != int(position_id)):
            conn.rollback()
            return False
        live = conn.execute(
            f"SELECT 1 FROM memecoin_positions WHERE id=? AND is_open=1 AND {claim_col}=?",
            (position_id, claim_id)).fetchone()
        if live is None:
            conn.rollback()
            return False
        cur = conn.execute(
            "INSERT OR IGNORE INTO memecoin_exit_intents("
            "claim_id,position_id,action,intent_json,status,created_at,updated_at) "
            "VALUES(?,?,?,?, 'quoted', ?, ?)",
            (claim_id, position_id, action, intent_json, intent.get("quote_time"),
             intent.get("quote_time")))
        if cur.rowcount != 1:
            conn.rollback()
            return False
        position_update = conn.execute(
            "UPDATE memecoin_positions SET exit_intent_json=? WHERE id=? AND is_open=1 "
            f"AND {claim_col}=?",
            (intent_json, position_id, claim_id))
        if position_update.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def set_exit_intent_status(conn, claim_id, status, at):
    """Advance the queryable execution lifecycle for one immutable claim intent."""
    allowed = {"quoted", "signed", "broadcast", "confirmed", "failed", "dropped",
               "submitted_unknown"}
    if status not in allowed:
        raise ValueError(f"invalid exit intent status: {status}")
    cur = conn.execute(
        "UPDATE memecoin_exit_intents SET status=?,updated_at=? WHERE claim_id=?",
        (status, at, claim_id))
    conn.commit()
    return cur.rowcount == 1


def record_exit_timing(conn, position_id, timing_json):
    """Attach measured exit stage durations to a position. Best-effort: a measurement must never
    fail a sell, so a write error is swallowed rather than raised into the exit path."""
    if not timing_json:
        return False
    try:
        conn.execute("UPDATE memecoin_positions SET exit_timing_json=? WHERE id=?",
                     (timing_json, position_id))
        conn.commit()
        return True
    except Exception:
        return False


def _exit_signature_owned_elsewhere(conn, claim_id, sig):
    """Indexed identity check inside the publication transaction; identical owner reoffers survive."""
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    return conn.execute(
        "SELECT 1 FROM memecoin_exit_intents WHERE (CASE WHEN json_valid(intent_json) "
        "THEN json_extract(intent_json,'$.tx_identity') END)=? AND claim_id<>? "
        "AND status NOT IN ('failed','dropped') LIMIT 1", (sig, claim_id)).fetchone() is not None


def mark_position_close_submitted(conn, position_id, claim_id, at, sig, *,
                                  expected_intent_json=None, submitted_intent_json=None,
                                  expected_controller_revision=None, commit=True):
    """Persist the signed full-exit identity before broadcast."""
    if _exit_signature_owned_elsewhere(conn, claim_id, sig):
        if commit:
            conn.rollback()
        return False
    cur = conn.execute(
        "UPDATE memecoin_positions SET close_sig=?, close_submitted_at=?, close_pending_at=NULL, "
        "exit_intent_json=COALESCE(?,exit_intent_json) "
        "WHERE id=? AND is_open=1 AND close_claim_id=? AND close_pending_at IS NOT NULL "
        "AND close_sig IS NULL AND (? IS NULL OR exit_intent_json=?) "
        "AND (? IS NULL OR controller_revision=?)",
        (sig, at, submitted_intent_json, position_id, claim_id,
         expected_intent_json, expected_intent_json,
         expected_controller_revision, expected_controller_revision))
    if cur.rowcount == 1 and submitted_intent_json is not None:
        changed = conn.execute(
            "UPDATE memecoin_exit_intents SET intent_json=?,status='submitted_unknown',updated_at=? "
            "WHERE claim_id=? AND intent_json=?",
            (submitted_intent_json, at, claim_id, expected_intent_json))
        if changed.rowcount != 1:
            if commit:
                conn.rollback()
            return False
    if commit:
        conn.commit()
    return cur.rowcount == 1


def swap_close_identity(conn, position_id, claim_id, current_sig, new_sig, *,
                        expected_intent_json, new_intent_json, at):
    """Move a held full exit's signed identity from `current_sig` to `new_sig` in ONE transaction:
    the position's close_sig/exit_intent_json and the claim's intent row, both compare-and-set.
    The claim is never released. Used by the chain-1 same-nonce replacement (both directions: the
    replacement, and back when its send is refused) and by recovery when a replaced hash landed.
    True only when both rows moved."""
    try:
        cur = conn.execute(
            "UPDATE memecoin_positions SET close_sig=?, exit_intent_json=? "
            "WHERE id=? AND is_open=1 AND close_claim_id=? AND close_sig=? AND exit_intent_json=?",
            (new_sig, new_intent_json, position_id, claim_id, current_sig, expected_intent_json))
        if cur.rowcount != 1:
            conn.rollback()
            return False
        changed = conn.execute(
            "UPDATE memecoin_exit_intents SET intent_json=?, status='submitted_unknown', "
            "updated_at=? WHERE claim_id=? AND intent_json=?",
            (new_intent_json, at, claim_id, expected_intent_json))
        if changed.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def reset_position_close_submission(conn, position_id, claim_id, sig, at, *, intent_status=None):
    """Atomically reset an explicitly rejected/failed send and its audit lifecycle."""
    if intent_status not in (None, "dropped", "failed"):
        raise ValueError(f"invalid reset intent status: {intent_status}")
    try:
        cur = conn.execute(
            "UPDATE memecoin_positions SET close_sig=NULL, close_submitted_at=NULL, "
            "close_pending_at=? WHERE id=? AND is_open=1 AND close_claim_id=? AND close_sig=? RETURNING signal_source,execution_mode,desk_cohort_id",
            (at, position_id, claim_id, sig))
        changed_position = cur.fetchall()
        if len(changed_position) != 1:
            conn.rollback()
            return False
        if intent_status is not None:
            changed = conn.execute(
                "UPDATE memecoin_exit_intents SET status=?,updated_at=? WHERE claim_id=?",
                (intent_status, at, claim_id))
            if changed.rowcount != 1:
                conn.rollback()
                return False
        if (changed_position[0][0] == SIGNAL_SOURCE_DESK and changed_position[0][1] == "real"
                and changed_position[0][2]):
            _mark_desk_execution_cost(conn, position_id, f"exit:{position_id}:{sig}", at)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def unverified_closes(conn):
    """Signed real full exits without terminal ledger bookkeeping.

    Recovery may inspect these, but never releases them automatically: signed identity means a
    broadcast may have landed, so safety requires operator/chain reconciliation rather than retry.
    """
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
        "AND closed_at IS NULL AND close_sig IS NOT NULL AND close_claim_id IS NOT NULL")]


def has_unresolved_real_exit(conn, network):
    """Chain-local nonce fence for new maintenance writes; ledger errors propagate.

    Unsigned open claims also fence while their executor may still sign. Closed full-exit
    identities are terminal history, but a signed de-risk without a booked fill remains debt
    even after the position closes, matching unverified_derisks().
    """
    return conn.execute(
        "SELECT 1 FROM memecoin_positions WHERE " + _REAL_EXIT_NONCE_CANDIDATE_SQL
        + " AND " + COIN_NETWORK_SQL + "=? AND ("
        "(is_open=1 AND closed_at IS NULL AND (close_claim_id IS NOT NULL OR close_sig IS NOT NULL)) "
        "OR NOT EXISTS (SELECT 1 FROM memecoin_fills f "
        "WHERE f.position_id=memecoin_positions.id AND f.kind='derisk')) "
        "LIMIT 1", (normalized_coin_network(network, ""),)).fetchone() is not None


def stale_close_pendings(conn, older_than_iso):
    """Unsigned close claims old enough to prove the executor died before submission."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
        "AND close_pending_at IS NOT NULL AND close_pending_at < ? AND close_claim_id IS NOT NULL "
        "AND close_sig IS NULL", (older_than_iso,))]


def release_stale_close_pending(conn, position_id, claim_id, pending_at):
    """CAS-release the exact unsigned close claim observed by recovery."""
    cur = conn.execute(
        "UPDATE memecoin_positions SET close_pending_at=NULL, close_claim_id=NULL, "
        "close_exit_reason=NULL, stuck_bag=0 WHERE id=? AND is_open=1 AND close_claim_id=? "
        "AND close_pending_at=? AND close_sig IS NULL",
        (position_id, claim_id, pending_at))
    conn.commit()
    return cur.rowcount == 1


def release_unsigned_close_claims_after_runner_stop(conn):
    """Release unsigned full-close claims after no runner can still sign them."""
    import sqlite3

    try:
        rows = conn.execute(
            "SELECT id, close_claim_id, close_pending_at FROM memecoin_positions "
            "WHERE execution_mode='real' AND is_open=1 AND close_pending_at IS NOT NULL "
            "AND close_claim_id IS NOT NULL AND close_sig IS NULL"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: memecoin_positions" in str(exc):
            return []
        raise
    released = []
    for row in rows:
        if release_stale_close_pending(conn, row["id"], row["close_claim_id"],
                                       row["close_pending_at"]):
            released.append(int(row["id"]))
    return released


def _fill_slippage_pct(price, quote_price):
    """Realized fill against the quote the route was signed at, in percent, or None if unknown.

    The ratio is fill/quote on BOTH sides, matching `entry_attribution.buy_drift`, so the
    direction of harm flips with the side: an entry above its quote is positive, an exit below
    its quote is negative. A missing or non-positive quote is unknown, never a zero — a 0.0 here
    would read as "this leg executed exactly at its quote" and would bias every aggregate toward
    the appearance of perfect execution.
    """
    try:
        if price is None or quote_price is None:
            return None
        fill, quote = float(price), float(quote_price)
        if fill <= 0 or quote <= 0:
            return None
        return (fill / quote - 1.0) * 100.0
    except (TypeError, ValueError):
        return None


def book_full_exit_and_finalize(
        conn, *, position_id, claim_id, sig, closed_at, realized_pnl, exit_reason,
        fill_kind, price, qty, notional, fill_source="chain", recorded_at=None,
        quote_price=None, quote_route_target=None, executed_pool=None, commit=True):
    """Atomically persist the confirmed full fill and settle the exact submitted claim.

    `quote_price` is the USD/token the signed route was quoted at. It is stored beside the
    realized `price` and reduced to `execution_slippage_pct` here, so exits carry the same
    quote-vs-fill column entries already carry (`entry_attribution.buy_drift`) and the formula
    exists in ONE place for the exit side. Sign convention is shared with entries — the ratio is
    always fill/quote — so the direction of harm flips with the side: an entry above its quote is
    positive, an exit below its quote is negative. Unknown stays NULL; a missing or non-positive
    quote is not a zero slippage.
    """
    recorded_at = recorded_at or real_utc_now_iso()
    slippage_pct = _fill_slippage_pct(price, quote_price)
    try:
        cur = conn.execute(
            "UPDATE memecoin_positions SET is_open=0, closed_at=?, closed_recorded_at=?, "
            "realized_pnl=?, exit_reason=?, close_pending_at=NULL, close_claim_id=NULL, stuck_bag=0 "
            "WHERE id=? AND is_open=1 AND close_claim_id=? AND close_sig=?",
            (closed_at, recorded_at, realized_pnl, exit_reason, position_id, claim_id, sig))
        if cur.rowcount != 1:
            if commit:
                conn.rollback()
            return False
        conn.execute(
            "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
            "fraction, created_at, tx_sig, quote_price, execution_slippage_pct, "
            "quote_route_target, executed_pool) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, fill_kind, price, qty, notional, fill_source, 1.0, closed_at, sig,
             quote_price, slippage_pct, quote_route_target, executed_pool))
        conn.execute(
            "UPDATE memecoin_exit_intents SET status='confirmed',updated_at=? WHERE claim_id=?",
            (closed_at, claim_id))
        latch_desk_day_on_close(conn, position_id, closed_at)
        if commit:
            conn.commit()
        return True
    except Exception:
        if commit:
            conn.rollback()
        raise


def reconcile_unpriced_full_exit(conn, *, position_id, sig, price, qty, notional, fill_source,
                                 quote_price=None):
    """Settle one closed, confirmed full exit that had no proceeds at recovery time."""
    try:
        price, qty, notional = float(price), float(qty), float(notional)
        if not all(math.isfinite(value) and value > 0 for value in (price, qty, notional)):
            return False
        if not math.isclose(price * qty, notional, rel_tol=1e-9, abs_tol=1e-9):
            return False
        fill = conn.execute(
            "SELECT id,qty FROM memecoin_fills WHERE position_id=? "
            "AND kind IN ('mirror_exit','manual_close','bracket_exit') "
            "AND tx_sig=? AND fill_source='chain_confirmed_unpriced' AND price IS NULL "
            "AND notional IS NULL",
            (position_id, sig),
        ).fetchall()
        if len(fill) != 1 or not math.isclose(float(fill[0]["qty"]), qty, rel_tol=1e-12):
            return False
        pos = conn.execute(
            "SELECT entry_notional FROM memecoin_positions WHERE id=? AND is_open=0 "
            "AND realized_pnl IS NULL AND close_sig=?",
            (position_id, sig),
        ).fetchone()
        if pos is None:
            return False
        realized = realized_after_exit(conn, position_id, notional, pos["entry_notional"])
        slippage_pct = _fill_slippage_pct(price, quote_price)
        conn.execute(
            "UPDATE memecoin_fills SET price=?,notional=?,fill_source=?,quote_price=?, "
            "execution_slippage_pct=? WHERE id=? AND price IS NULL AND notional IS NULL "
            "AND fill_source='chain_confirmed_unpriced'",
            (price, notional, fill_source, quote_price, slippage_pct, fill[0]["id"]),
        )
        cur = conn.execute(
            "UPDATE memecoin_positions SET realized_pnl=? WHERE id=? AND is_open=0 "
            "AND realized_pnl IS NULL AND close_sig=?",
            (realized, position_id, sig),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        _latch_desk_day_for_position(conn, position_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def unpriced_full_exits(conn):
    """Return closed funded exits that still need exact receipt accounting."""
    return [dict(row) for row in conn.execute(
        "SELECT p.*, f.qty AS unpriced_qty FROM memecoin_positions p "
        "JOIN memecoin_fills f ON f.position_id=p.id AND f.tx_sig=p.close_sig "
        "WHERE p.execution_mode='real' AND p.is_open=0 AND p.realized_pnl IS NULL "
        "AND p.close_sig IS NOT NULL "
        "AND f.kind IN ('mirror_exit','manual_close','bracket_exit') "
        "AND f.fill_source='chain_confirmed_unpriced' "
        "AND f.price IS NULL AND f.notional IS NULL ORDER BY p.id")]


def finalize_claimed_close_without_fill(
        conn, *, position_id, claim_id, closed_at, realized_pnl, exit_reason, recorded_at=None):
    """Settle a provably empty wallet under an unsigned close claim; no transaction was sent."""
    recorded_at = recorded_at or real_utc_now_iso()
    cur = conn.execute(
        "UPDATE memecoin_positions SET is_open=0, closed_at=?, closed_recorded_at=?, "
        "realized_pnl=?, exit_reason=?, close_pending_at=NULL, close_claim_id=NULL, "
        "close_exit_reason=NULL WHERE id=? AND is_open=1 AND close_claim_id=? AND close_sig IS NULL",
        (closed_at, recorded_at, realized_pnl, exit_reason, position_id, claim_id))
    if cur.rowcount == 1:
        latch_desk_day_on_close(conn, position_id, closed_at)
    conn.commit()
    return cur.rowcount == 1


WRITE_OFF_EXIT_REASON = "written_off"
# A sell whose outcome the chain has not answered. A write-off beside one would book the bag twice.
_AMBIGUOUS_INTENT_STATUSES = ("submitted", "submitted_unknown")
_EXIT_IN_FLIGHT_COLUMNS = ("close_claim_id", "close_sig", "close_pending_at",
                           "derisk_claim_id", "derisk_sig", "derisk_pending_at")


def _ensure_write_off_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_position_write_offs (
            position_id INTEGER PRIMARY KEY,
            network_id TEXT,
            mint TEXT,
            actor TEXT NOT NULL,
            reason TEXT NOT NULL,
            balance_at_write_off TEXT NOT NULL,
            balance_source TEXT,
            entry_notional REAL,
            derisk_proceeds REAL,
            realized_pnl REAL,
            written_off_at TEXT NOT NULL
        )""")


# WALLET-GRADE EXCLUSIONS (operator 2026-10-02, after DAODUN 68544). A close that lost money because
# of OUR fault (no exit route we could use, a garbage quote, a bug) is not evidence about the wallet
# we copied, so it must not move that wallet's funded grade: the demote guardrail
# (wallet_real_record) and the funded-authority board (real_stats_by_wallet). The ledger is untouched
# and the loss still counts everywhere money is counted (daily breaker, system totals, risk): those
# read real_money_sql / robot_performance_sql, which deliberately do NOT apply this. One explicit,
# audited row per position; deleting the row undoes it.
def _ensure_wallet_grade_exclusions(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_wallet_grade_exclusions (
            position_id INTEGER PRIMARY KEY,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")


WALLET_GRADE_SQL = ("id NOT IN (SELECT position_id FROM memecoin_wallet_grade_exclusions)")


def exclude_from_wallet_grade(conn, position_id, *, reason, actor):
    """Keep one CLOSED funded position off its wallet's funded grade. Returns the row written, or
    raises ValueError for an unknown, open or non-funded position, or an empty reason."""
    reason, actor = str(reason or "").strip(), str(actor or "").strip()
    if not reason or not actor:
        raise ValueError("a reason and an actor are required")
    row = conn.execute("SELECT id, is_open, execution_mode FROM memecoin_positions WHERE id=?",
                       (int(position_id),)).fetchone()
    if row is None:
        raise ValueError("no position %s" % position_id)
    if int(row[1] or 0) != 0:
        raise ValueError("position %s is still open" % position_id)
    if str(row[2] or "") != "real":
        raise ValueError("position %s is not a funded position" % position_id)
    from sniper.util.clock import real_utc_now_iso
    _ensure_wallet_grade_exclusions(conn)
    try:                                 # first reason stands: the audit row is never rewritten
        conn.execute("INSERT INTO memecoin_wallet_grade_exclusions "
                     "(position_id, reason, actor, created_at) VALUES (?,?,?,?)",
                     (int(position_id), reason, actor, real_utc_now_iso()))
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("position %s is already excluded" % position_id)
    conn.commit()
    return {"position_id": int(position_id), "reason": reason, "actor": actor}


def write_off_refusal(conn, position_id):
    """(position or None, reason or None) for an operator write-off. None reason means it may go."""
    row = conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (position_id,)).fetchone()
    if row is None:
        return None, "no such position"
    pos = dict(row)
    if pos.get("execution_mode") != "real":
        return pos, "not a funded position (execution_mode=%r)" % pos.get("execution_mode")
    if not pos.get("is_open") or pos.get("closed_at") is not None:
        return pos, "position is not open"
    for column in _EXIT_IN_FLIGHT_COLUMNS:
        if pos.get(column):
            return pos, "an exit is in flight (%s=%s)" % (column, pos[column])
    marks = ",".join("?" for _ in _AMBIGUOUS_INTENT_STATUSES)
    live = conn.execute(
        f"SELECT claim_id, status FROM memecoin_exit_intents WHERE position_id=? "
        f"AND status IN ({marks}) LIMIT 1", (position_id, *_AMBIGUOUS_INTENT_STATUSES)).fetchone()
    if live is not None:
        return pos, "an exit is in flight (intent %s is %s)" % (live["claim_id"], live["status"])
    entry = pos.get("entry_notional")
    if entry is None or not math.isfinite(float(entry)):
        return pos, "entry cost unknown, so the loss cannot be booked"
    return pos, None


def write_off_real_position(conn, position_id, *, actor, reason, balance_text,
                            balance_source=None, at=None):
    """Close an open funded position at $0 proceeds, operator only (2026-09-29).

    No chain action: the tokens stay in the wallet, where Other Holdings lists them once no open
    position owns the mint. realized = de-risk proceeds already booked - entry cost, the canonical
    close formula with a final leg of 0. No exit fill is written, because no sale happened.

    ONE transaction. The UPDATE repeats every refusal as its predicate, so a runner claim that
    lands between the read and the write makes this a refusal, never a double booking; a claim
    that arrives after it finds is_open=0 and cannot sign. A re-run returns the audit row and
    changes nothing.
    """
    if not str(reason or "").strip():
        raise ValueError("a write-off needs a reason")
    at = at or real_utc_now_iso()
    _ensure_write_off_table(conn)
    if conn.in_transaction:
        raise RuntimeError("write-off needs a clean transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = conn.execute("SELECT * FROM memecoin_position_write_offs WHERE position_id=?",
                            (position_id,)).fetchone()
        if done is not None:
            conn.rollback()
            return {"status": "already_written_off", **dict(done)}
        pos, why = write_off_refusal(conn, position_id)
        if why is not None:
            conn.rollback()
            return {"status": "refused", "reason": why}
        derisk = derisk_proceeds(conn, position_id)
        entry = float(pos["entry_notional"])
        realized = None if derisk is None else derisk - entry     # an unpriced partial: unknown
        marks = ",".join("?" for _ in _AMBIGUOUS_INTENT_STATUSES)
        cur = conn.execute(
            "UPDATE memecoin_positions SET is_open=0, closed_at=?, closed_recorded_at=?, "
            "realized_pnl=?, exit_reason=?, stuck_bag=0, "
            "time_to_peak_s=COALESCE(time_to_peak_s, CASE "
            "  WHEN peak_ts IS NOT NULL AND opened_at IS NOT NULL "
            "  THEN max(0.0, (julianday(peak_ts) - julianday(opened_at)) * 86400.0) END), "
            "max_gain_pct=COALESCE(max_gain_pct, CASE "
            "  WHEN entry_price > 0 AND peak_price IS NOT NULL "
            "  THEN (peak_price / entry_price - 1.0) * 100.0 END) "
            "WHERE id=? AND is_open=1 AND closed_at IS NULL AND execution_mode='real' "
            "AND close_claim_id IS NULL AND close_sig IS NULL AND close_pending_at IS NULL "
            "AND derisk_claim_id IS NULL AND derisk_sig IS NULL AND derisk_pending_at IS NULL "
            f"AND NOT EXISTS (SELECT 1 FROM memecoin_exit_intents WHERE position_id=? "
            f"AND status IN ({marks}))",
            (at, real_utc_now_iso(), realized, WRITE_OFF_EXIT_REASON, position_id, position_id,
             *_AMBIGUOUS_INTENT_STATUSES))
        if cur.rowcount != 1:
            conn.rollback()
            return {"status": "refused", "reason": "the position changed during the write-off"}
        conn.execute(
            "INSERT INTO memecoin_position_write_offs (position_id, network_id, mint, actor, "
            "reason, balance_at_write_off, balance_source, entry_notional, derisk_proceeds, "
            "realized_pnl, written_off_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, pos.get("network_id"), pos.get("mint"), str(actor), str(reason).strip(),
             str(balance_text), balance_source, entry, derisk, realized, at))
        latch_desk_day_on_close(conn, position_id, at)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"status": "written_off", "position_id": position_id, "realized_pnl": realized,
            "entry_notional": entry, "derisk_proceeds": derisk}


def claim_source_partial(conn, position_id, request_id, at):
    """Own one durable source request, sharing the existing partial exclusion slot."""
    claim_id = uuid.uuid4().hex
    try:
        request = conn.execute(
            "SELECT 1 FROM memecoin_manual_exit_intents x WHERE intent_id=? AND position_id=? "
            "AND action='source_partial' AND state='queued' AND request_json IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM memecoin_manual_exit_intents earlier "
            "WHERE earlier.position_id=x.position_id AND earlier.rowid<x.rowid "
            "AND earlier.action='source_partial' AND earlier.state IN ('queued','claimed','submitted_unknown'))",
            (request_id, position_id)).fetchone()
        if not request:
            return None
        cur = conn.execute(
            "UPDATE memecoin_positions SET derisk_pending_at=?,derisk_claim_id=? "
            "WHERE id=? AND is_open=1 AND closed_at IS NULL AND derisk_pending_at IS NULL "
            "AND derisk_claim_id IS NULL AND derisk_sig IS NULL AND close_claim_id IS NULL "
            "AND close_sig IS NULL AND COALESCE(tracking_paused_at,'')='' "
            "AND COALESCE(mirror_disabled,0)=0",
            (at, claim_id, position_id))
        if cur.rowcount != 1:
            conn.rollback()
            return None
        changed = conn.execute(
            "UPDATE memecoin_manual_exit_intents SET state='claimed',claim_id=?,claimed_at=?,"
            "attempts=attempts+1 WHERE intent_id=? AND state='queued'",
            (claim_id, at, request_id))
        if changed.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return claim_id
    except Exception:
        conn.rollback()
        raise


def release_source_partial(conn, position_id, claim_id):
    """Only an unsigned exact owner may return its source request to the queue."""
    try:
        cur = conn.execute(
            "UPDATE memecoin_positions SET derisk_claim_id=NULL,derisk_pending_at=NULL "
            "WHERE id=? AND derisk_claim_id=? AND derisk_sig IS NULL AND EXISTS "
            "(SELECT 1 FROM memecoin_manual_exit_intents x WHERE x.claim_id=? "
            "AND x.action='source_partial' AND x.state='claimed' AND x.sig IS NULL)",
            (position_id, claim_id, claim_id))
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.execute("UPDATE memecoin_manual_exit_intents SET state='queued',claim_id=NULL,"
                     "claimed_at=NULL WHERE claim_id=? AND action='source_partial'", (claim_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def finish_source_partial_noop(conn, position_id, claim_id, at):
    """Complete a proven zero-atom trim under its exact unsigned owner, without booking money."""
    try:
        pos = conn.execute(
            "UPDATE memecoin_positions SET derisk_claim_id=NULL,derisk_pending_at=NULL "
            "WHERE id=? AND is_open=1 AND derisk_claim_id=? AND derisk_sig IS NULL "
            "AND derisk_pending_at IS NOT NULL AND EXISTS "
            "(SELECT 1 FROM memecoin_manual_exit_intents x WHERE x.position_id=? AND x.claim_id=? "
            "AND x.action='source_partial' AND x.state='claimed' AND x.sig IS NULL)",
            (position_id, claim_id, position_id, claim_id))
        request = conn.execute(
            "UPDATE memecoin_manual_exit_intents SET state='done',resolved_at=?,"
            "result_status='rounded_to_zero',reason='partial_amount_zero' "
            "WHERE position_id=? AND claim_id=? AND action='source_partial' "
            "AND state='claimed' AND sig IS NULL", (at, position_id, claim_id))
        if pos.rowcount != 1 or request.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def mark_source_partial_submitted(conn, position_id, claim_id, at, sig, *,
                                  expected_intent_json=None, submitted_intent_json=None,
                                  expected_controller_revision=None, **_unused):
    """Signed identity, request state and active owner commit before broadcast. Never touch policy."""
    if conn.in_transaction:
        raise RuntimeError('source partial publication requires a clean transaction')
    try:
        # A repeated source fraction can produce identical Solana bytes within one blockhash.
        # Its old receipt belongs to the earlier claim, including after a close and re-entry.
        if _exit_signature_owned_elsewhere(conn, claim_id, sig):
            conn.rollback()
            return False
        cur = conn.execute(
            "UPDATE memecoin_positions SET derisk_sig=?,exit_intent_json=? WHERE id=? "
            "AND derisk_claim_id=? AND derisk_pending_at IS NOT NULL AND derisk_sig IS NULL "
            "AND exit_intent_json=? AND controller_revision=?",
            (sig, submitted_intent_json, position_id, claim_id, expected_intent_json,
             expected_controller_revision))
        row = conn.execute(
            "UPDATE memecoin_exit_intents SET intent_json=?,status='submitted_unknown',updated_at=? "
            "WHERE claim_id=? AND action='partial' AND intent_json=?",
            (submitted_intent_json, at, claim_id, expected_intent_json))
        request = conn.execute(
            "UPDATE memecoin_manual_exit_intents SET state='submitted_unknown',sig=? "
            "WHERE action='source_partial' AND claim_id=? AND state='claimed' AND sig IS NULL",
            (sig, claim_id))
        if any(r.rowcount != 1 for r in (cur, row, request)):
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def reset_source_partial_submission(conn, position_id, claim_id, sig, at, *, intent_status=None):
    """A proved failed/non-sent transaction may retry; the one-shot policy marker survives."""
    if intent_status not in ('failed', 'dropped'):
        raise ValueError('partial reset needs exact failure proof')
    try:
        cur = conn.execute(
            "UPDATE memecoin_positions SET derisk_sig=NULL WHERE id=? AND derisk_claim_id=? "
            "AND derisk_sig=? AND derisk_pending_at IS NOT NULL", (position_id, claim_id, sig))
        request = conn.execute(
            "UPDATE memecoin_manual_exit_intents SET state='claimed',sig=NULL "
            "WHERE action='source_partial' AND claim_id=? AND sig=? AND state='submitted_unknown'",
            (claim_id, sig))
        row = conn.execute("UPDATE memecoin_exit_intents SET status=?,updated_at=? "
                           "WHERE claim_id=? AND action='partial'", (intent_status, at, claim_id))
        if any(r.rowcount != 1 for r in (cur, request, row)):
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def book_source_partial_fill(conn, *, position_id, claim_id, tx_sig, at, sold_atomic,
                             sell_decimals, notional, fraction, quote_price=None, **_unused):
    """One actual receipt books once, releases active ownership, and preserves de-risk policy."""
    if (type(sold_atomic) is not int or sold_atomic <= 0 or type(sell_decimals) is not int
            or not 0 <= sell_decimals <= 255 or isinstance(notional, bool)
            or not math.isfinite(float(notional)) or float(notional) <= 0):
        raise ValueError('invalid partial receipt')
    try:
        row = conn.execute("SELECT intent_json FROM memecoin_exit_intents WHERE claim_id=? "
                           "AND action='partial'", (claim_id,)).fetchone()
        intent = _json.loads(row[0]) if row else {}
        if int(intent.get('sell_amount_atomic', 0)) != sold_atomic or intent.get('tx_identity') != tx_sig:
            raise ValueError('partial receipt identity/amount mismatch')
        cur = conn.execute(
            "UPDATE memecoin_positions SET derisk_claim_id=NULL,derisk_sig=NULL,derisk_pending_at=NULL "
            "WHERE id=? AND derisk_claim_id=? AND derisk_sig=?", (position_id, claim_id, tx_sig))
        request = conn.execute(
            "UPDATE memecoin_manual_exit_intents SET state='done',resolved_at=?,result_status='partial_sold' "
            "WHERE action='source_partial' AND claim_id=? AND sig=? AND state='submitted_unknown'",
            (at, claim_id, tx_sig))
        if cur.rowcount != 1 or request.rowcount != 1:
            conn.rollback()
            return False
        qty = sold_atomic / 10 ** sell_decimals
        price = float(notional) / qty
        conn.execute("INSERT INTO memecoin_fills(position_id,kind,price,qty,notional,fill_source,"
                     "fraction,created_at,tx_sig,quote_price,execution_slippage_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (position_id, 'derisk', price, qty, notional, 'chain', fraction, at, tx_sig,
                      quote_price, _fill_slippage_pct(price, quote_price)))
        intent.update(receipt_sold_atomic=str(sold_atomic), receipt_decimals=sell_decimals)
        conn.execute("UPDATE memecoin_exit_intents SET status='confirmed',intent_json=?,updated_at=? "
                     "WHERE claim_id=?", (_json.dumps(intent, sort_keys=True), at, claim_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def claim_position_derisk(conn, position_id, at):
    """Atomically claim the de-risk one-shot before submission. Return its opaque owner token,
    or None when another caller already owns/settled it.

    The claim is a DISTINCT PENDING state (`derisk_pending_at`), never the settled `derisked_at`
    (P0-A, Codex round 2): committing derisked_at before balance/quote/sign/submit meant an
    exception, process death, or host restart mid-executor left a row that READ as a confirmed
    de-risk (derisked_at set, derisk_sig NULL == derisk_confirmed True) while being excluded
    from the recovery queue — rail burned, trail armed on principal that was never recovered.
    A crash now leaves a PENDING row: derisk_confirmed stays False, nothing arms, and
    `stale_derisk_pendings` re-arms it after the bound."""
    claim_id = uuid.uuid4().hex
    cur = conn.execute(
        "UPDATE memecoin_positions SET derisk_pending_at=?, derisk_claim_id=? "
        "WHERE id=? AND is_open=1 AND derisk_pending_at IS NULL AND derisked_at IS NULL "
        "AND close_claim_id IS NULL AND close_sig IS NULL "
        "AND derisk_claim_id IS NULL AND derisk_sig IS NULL "
        "AND COALESCE(tracking_paused_at,'')=''",
        (at, claim_id, position_id))
    conn.commit()
    return claim_id if cur.rowcount > 0 else None


def stale_derisk_pendings(conn, older_than_iso):
    """Pending de-risk claims whose executor died without settling or releasing (no derisked_at,
    no sig, no booked fill) older than the bound — the crash-recovery work queue for P0-A.
    Resolution is clear-and-re-arm: nothing was provably submitted (any submit path stamps
    derisked_at+sig or books a fill in the same commit), so releasing is safe."""
    return [dict(r) for r in conn.execute(
        "SELECT p.* FROM memecoin_positions p WHERE p.derisk_pending_at IS NOT NULL "
        "AND p.derisk_pending_at < ? AND p.derisked_at IS NULL AND p.derisk_sig IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM memecoin_fills f WHERE f.position_id=p.id "
        "AND f.kind='derisk') AND NOT EXISTS (SELECT 1 FROM memecoin_manual_exit_intents x "
        "WHERE x.action='source_partial' AND x.claim_id=p.derisk_claim_id)", (older_than_iso,))]


def release_stale_derisk_pending(conn, position_id, claim_id, pending_at):
    """Release only the exact unsigned claim observed by the stale-work scan."""
    claim_predicate = "derisk_claim_id=?" if claim_id is not None else "derisk_claim_id IS NULL"
    params = [position_id]
    if claim_id is not None:
        params.append(claim_id)
    params.append(pending_at)
    cur = conn.execute(
        "UPDATE memecoin_positions SET derisk_pending_at=NULL, derisk_claim_id=NULL "
        f"WHERE id=? AND {claim_predicate} AND derisk_pending_at=? "
        "AND derisked_at IS NULL AND derisk_sig IS NULL",
        params)
    conn.commit()
    return cur.rowcount == 1


def release_position_derisk_claim(conn, position_id, claim_id):
    """Release this executor's claim only while it is still unsigned/unsubmitted."""
    cur = conn.execute(
        "UPDATE memecoin_positions SET derisk_pending_at=NULL, derisk_claim_id=NULL "
        "WHERE id=? AND derisk_claim_id=? AND derisked_at IS NULL AND derisk_sig IS NULL",
        (position_id, claim_id))
    conn.commit()
    return cur.rowcount == 1


def mark_position_derisk_submitted(conn, position_id, claim_id, at, sig, *,
                                   expected_intent_json=None, submitted_intent_json=None,
                                   expected_controller_revision=None, commit=True):
    """Persist signed transaction identity before broadcast.

    A process death after the subsequent send is therefore submitted-unknown, never an unsigned
    stale claim that recovery can release into a double-sell.
    """
    if _exit_signature_owned_elsewhere(conn, claim_id, sig):
        if commit:
            conn.rollback()
        return False
    cur = conn.execute(
        "UPDATE memecoin_positions SET derisked_at=?, derisk_sig=?, derisk_pending_at=NULL, "
        "derisk_recheck_hits=0, exit_intent_json=COALESCE(?,exit_intent_json) "
        "WHERE id=? AND derisk_claim_id=? AND derisk_pending_at IS NOT NULL "
        "AND derisked_at IS NULL AND derisk_sig IS NULL "
        "AND (? IS NULL OR exit_intent_json=?) "
        "AND (? IS NULL OR controller_revision=?)",
        (at, sig, submitted_intent_json, position_id, claim_id,
         expected_intent_json, expected_intent_json,
         expected_controller_revision, expected_controller_revision))
    if cur.rowcount == 1 and submitted_intent_json is not None:
        changed = conn.execute(
            "UPDATE memecoin_exit_intents SET intent_json=?,status='submitted_unknown',updated_at=? "
            "WHERE claim_id=? AND intent_json=?",
            (submitted_intent_json, at, claim_id, expected_intent_json))
        if changed.rowcount != 1:
            if commit:
                conn.rollback()
            return False
    if commit:
        conn.commit()
    return cur.rowcount == 1


def reset_position_derisk_submission(
        conn, position_id, claim_id, sig, at, *, intent_status=None):
    """Atomically reset this owner's rejected/failed submission and audit lifecycle."""
    if intent_status not in (None, "dropped", "failed"):
        raise ValueError(f"invalid reset intent status: {intent_status}")
    try:
        cur = conn.execute(
            "UPDATE memecoin_positions SET derisked_at=NULL, derisk_sig=NULL, "
            "derisk_pending_at=?, derisk_recheck_hits=0 WHERE id=? AND derisk_claim_id=? "
            "AND derisk_sig=? AND derisked_at IS NOT NULL RETURNING signal_source,execution_mode,desk_cohort_id",
            (at, position_id, claim_id, sig))
        changed_position = cur.fetchall()
        if len(changed_position) != 1:
            conn.rollback()
            return False
        if intent_status is not None:
            changed = conn.execute(
                "UPDATE memecoin_exit_intents SET status=?,updated_at=? WHERE claim_id=?",
                (intent_status, at, claim_id))
            if changed.rowcount != 1:
                conn.rollback()
                return False
        if (changed_position[0][0] == SIGNAL_SOURCE_DESK and changed_position[0][1] == "real"
                and changed_position[0][2]):
            _mark_desk_execution_cost(conn, position_id, f"partial:{position_id}:{sig}", at)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def set_position_derisked(conn, position_id, at):
    """One-shot marker: the recover-the-entry partial sell fired (or its confirm is unknown —
    marked anyway so a maybe-landed sell can never be repeated into a double-sell)."""
    conn.execute("UPDATE memecoin_positions SET derisked_at=?, derisk_pending_at=NULL, "
                 "derisk_claim_id=NULL "
                 "WHERE id=?", (at, position_id))
    conn.commit()


def set_position_derisk_unknown(conn, position_id, at, sig):
    """Unknown-confirm de-risk: stamp AND signature in ONE commit.

    These used to be two helpers with two commits (2026-07-22 review). A crash between them
    left `derisked_at` set with `derisk_sig` NULL — which is exactly the shape of a SETTLED
    de-risk, so `derisk_confirmed()` would read a never-landed sell as recovered principal and
    arm the trail on it. That is 9003's failure mode reintroduced by a power cut instead of a
    race. There must be no committed state where the stamp exists without its signature.
    """
    conn.execute("UPDATE memecoin_positions SET derisked_at=?, derisk_sig=?, "
                 "derisk_pending_at=NULL, derisk_claim_id=NULL, derisk_recheck_hits=0 WHERE id=?",
                 (at, sig, position_id))
    conn.commit()


def book_derisk_fill(conn, *, position_id, at, price, qty, notional, fraction, tx_sig,
                     fill_source="chain", claim_id=None, quote_price=None,
                     quote_route_target=None, executed_pool=None):
    """Confirmed de-risk: book the fill AND stamp the one-shot in ONE transaction.

    `quote_price` carries the same quote-vs-fill measurement the full exit records; see
    `book_full_exit_and_finalize` for the sign convention. A de-risk is a low-volume path today
    (5 funded fills in 30 days against 1230 full exits), but it is a funded write path, and an
    instrument that exists on one exit leg and not the other reads later as "de-risks never
    slip" rather than "de-risks were never measured".

    Booking the fill first and stamping second (two commits) left a window where a LANDED
    de-risk carried no stamp, so the one-shot could fire again into a bag that had already
    been partly sold. Both writes land together or neither does — and if the stamp statement
    itself raises, the fill INSERT must NOT survive: without the explicit rollback the failed
    stamp leaves the insert pending on the connection, and the next commit anywhere flushes an
    orphaned fill (2026-07-22 review). Roll back and re-raise so the caller's exit path decides,
    rather than silently persisting the very state this helper exists to prevent.
    """
    try:
        if claim_id is not None:
            cur = conn.execute(
                "UPDATE memecoin_positions SET derisked_at=?, derisk_sig=NULL, "
                "derisk_pending_at=NULL, derisk_claim_id=NULL, derisk_recheck_hits=0 "
                "WHERE id=? AND derisk_claim_id=? AND derisk_sig=? "
                "AND derisked_at IS NOT NULL RETURNING signal_source,execution_mode,desk_cohort_id",
                (at, position_id, claim_id, tx_sig))
        else:
            # Compatibility for historical callers/tests that atomically stamp a fill without
            # participating in the funded executor's claim-owned state machine.
            cur = conn.execute(
                "UPDATE memecoin_positions SET derisked_at=?, derisk_sig=NULL, "
                "derisk_pending_at=NULL, derisk_claim_id=NULL, derisk_recheck_hits=0 WHERE id=? "
                "RETURNING signal_source,execution_mode,desk_cohort_id",
                (at, position_id))
        booked = cur.fetchall()
        if len(booked) != 1:
            raise ValueError(
                f"derisk stamp matched {len(booked)} rows for position {position_id}, expected 1")
        conn.execute(
            "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
            "fraction, created_at, tx_sig, quote_price, execution_slippage_pct, "
            "quote_route_target, executed_pool) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, "derisk", price, qty, notional, fill_source, fraction, at, tx_sig,
             quote_price, _fill_slippage_pct(price, quote_price), quote_route_target,
             executed_pool))
        if claim_id is not None:
            conn.execute(
                "UPDATE memecoin_exit_intents SET status='confirmed',updated_at=? WHERE claim_id=?",
                (at, claim_id))
        if booked[0][0] == SIGNAL_SOURCE_DESK and booked[0][1] == "real" and booked[0][2] and tx_sig:
            _mark_desk_execution_cost(conn, position_id, f"partial:{position_id}:{tx_sig}", at)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def book_partial_and_stamp(conn, *, position_id, at, price, qty, notional, fraction,
                           kind="partial_exit", fill_source="dexscreener"):
    """PAPER one-shot partial: claim `derisked_at` AND book the partial fill in ONE transaction.

    The paper arms (combo_f, real_shadow) wrote the fill then stamped derisked_at as two separate
    commits — a crash between them duplicates the partial (fill booked, stamp not, so the next
    sweep re-fires it) and corrupts the paper evidence (2026-07-23 review P2). Here the stamp is
    the atomic CLAIM (guarded `derisked_at IS NULL`, like claim_close): if it matches 0 rows the
    one-shot already fired, so roll back and book nothing. Returns True iff this call booked it.
    """
    try:
        cur = conn.execute(
            "UPDATE memecoin_positions SET derisked_at=? WHERE id=? AND derisked_at IS NULL",
            (at, position_id))
        if cur.rowcount != 1:
            conn.rollback()                          # already derisked -> never a second partial
            return False
        conn.execute(
            "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
            "fraction, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (position_id, kind, price, qty, notional, fill_source, fraction, at))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def set_position_derisk_sig(conn, position_id, sig):
    """Persist the partial-sell tx signature alongside the one-shot marker so an
    unknown-confirm derisk can be verified later (2026-07-17: pos 6605's sig was dropped,
    making post-hoc confirmation impossible and burning the rail silently)."""
    conn.execute("UPDATE memecoin_positions SET derisk_sig=?, derisk_recheck_hits=0 WHERE id=?",
                 (sig, position_id))
    conn.commit()


def clear_position_derisk(conn, position_id, *, claim_id=None, sig=None, exact_claim=False):
    """Re-arm the derisk rail: the recovery pass proved the partial sell never landed
    (explicit on-chain failure, or repeated not_found past the blockhash window)."""
    sql = ("UPDATE memecoin_positions SET derisked_at=NULL, derisk_sig=NULL, "
           "derisk_pending_at=NULL, derisk_claim_id=NULL, derisk_recheck_hits=0 WHERE id=?")
    params = [position_id]
    if claim_id is not None:
        sql += " AND derisk_claim_id=?"
        params.append(claim_id)
    elif exact_claim:
        sql += " AND derisk_claim_id IS NULL"
    if sig is not None:
        sql += " AND derisk_sig=?"
        params.append(sig)
    else:
        # A new desk's paid identity cannot disappear unless this caller names it. Legacy/copy
        # callers retain their old unsigned-or-unscoped compatibility without another read.
        sql += (" AND NOT (signal_source IS 'desk' AND execution_mode IS 'real' "
                "AND desk_cohort_id IS NOT NULL AND derisk_sig IS NOT NULL)")
    try:
        cur = conn.execute(sql + " RETURNING signal_source,execution_mode,desk_cohort_id", params)
        changed = cur.fetchall()
        if (changed and changed[0][0] == SIGNAL_SOURCE_DESK and changed[0][1] == "real"
                and changed[0][2] and sig is not None):
            _mark_desk_execution_cost(conn, position_id, f"partial:{position_id}:{sig}", real_utc_now_iso())
        conn.commit()
        return len(changed) == 1
    except Exception:
        conn.rollback()
        raise


def settle_position_derisk_sig(conn, position_id, *, claim_id=None, sig=None, exact_claim=False):
    """The sig was verified (landed, or the operator resolved it): stop rechecking but KEEP
    derisked_at — the one-shot stands."""
    sql = ("UPDATE memecoin_positions SET derisk_sig=NULL, derisk_claim_id=NULL, "
           "derisk_recheck_hits=0 WHERE id=?")
    params = [position_id]
    if claim_id is not None:
        sql += " AND derisk_claim_id=?"
        params.append(claim_id)
    elif exact_claim:
        sql += " AND derisk_claim_id IS NULL"
    if sig is not None:
        sql += " AND derisk_sig=?"
        params.append(sig)
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.rowcount == 1


def bump_derisk_recheck(conn, position_id, *, claim_id=None, sig=None, exact_claim=False):
    sql = ("UPDATE memecoin_positions SET derisk_recheck_hits=derisk_recheck_hits+1 "
           "WHERE id=?")
    params = [position_id]
    if claim_id is not None:
        sql += " AND derisk_claim_id=?"
        params.append(claim_id)
    elif exact_claim:
        sql += " AND derisk_claim_id IS NULL"
    if sig is not None:
        sql += " AND derisk_sig=?"
        params.append(sig)
    cur = conn.execute(sql, params)
    conn.commit()
    if cur.rowcount != 1:
        return None
    r = conn.execute("SELECT derisk_recheck_hits FROM memecoin_positions WHERE id=?",
                     (position_id,)).fetchone()
    return int(r[0]) if r else 0


def unverified_derisks(conn, submitted_before_iso=None):
    """Real positions whose derisk one-shot fired with an UNVERIFIED tx (sig persisted, no
    kind='derisk' fill booked) — the recovery pass's work queue.

    Closed positions stay in the queue (2026-07-22, pos 9003): the queue used to filter
    is_open=1, so a position that closed before the 20-min blockhash window elapsed stranded
    its unverified signature forever — no recovery, no audit resolution, and a `derisked_at`
    stamp on the record that nothing ever confirmed. A closed row cannot re-arm a rail, but
    its debt must still be settled; the recovery pass branches on is_open for that.

    `submitted_before_iso` paces recovery checks; age cannot prove an executor has exited.
    The caller must acquire the executor's wallet lock before settling its exact claim.
    The bound is an ISO UTC string built by the caller and compared as text, the same format the
    column holds; never SQLite `datetime()`, which renders a space where the column has 'T'."""
    sql = ("SELECT p.* FROM memecoin_positions p WHERE p.derisked_at IS NOT NULL "
           "AND p.derisk_sig IS NOT NULL AND NOT EXISTS "
           "(SELECT 1 FROM memecoin_fills f WHERE f.position_id=p.id AND f.kind='derisk') "
           "AND NOT EXISTS (SELECT 1 FROM memecoin_manual_exit_intents x "
           "WHERE x.action='source_partial' AND x.claim_id=p.derisk_claim_id)")
    params = ()
    if submitted_before_iso is not None:
        sql += " AND p.derisked_at <= ?"
        params = (str(submitted_before_iso),)
    return [dict(r) for r in conn.execute(sql, params)]


def record_evm_tx_fee(conn, *, network_id, tx_hash, kind, fee, recorded_at=None):
    """Write-once fee row for one of our transactions. An unknown row may be completed later;
    a complete row is never rewritten. `fee` is evm_op_fees.tx_fee's dict (+ block_number)."""
    recorded_at = recorded_at or real_utc_now_iso()
    conn.execute(
        "INSERT INTO memecoin_evm_tx_fees (tx_hash, network_id, kind, block_number, "
        "l2_execution_wei, l1_data_wei, operator_wei, total_wei, status, recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tx_hash) DO UPDATE SET "
        "block_number=excluded.block_number, l2_execution_wei=excluded.l2_execution_wei, "
        "l1_data_wei=excluded.l1_data_wei, operator_wei=excluded.operator_wei, "
        "total_wei=excluded.total_wei, status=excluded.status, recorded_at=excluded.recorded_at "
        "WHERE memecoin_evm_tx_fees.status != 'complete'",
        (str(tx_hash).lower(), str(network_id), kind, fee.get("block_number"),
         fee.get("l2_execution_wei"), fee.get("l1_data_wei"), fee.get("operator_wei"),
         fee.get("total_wei"), fee["status"], recorded_at))
    conn.commit()


def evm_fee_backlog(conn, network_id, *, limit=20):
    """[(tx_hash, fill kind)] of funded fills on `network_id` with no complete fee row, oldest
    first. One entry per transaction even when it booked several fills."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT lower(f.tx_sig), MIN(f.kind) FROM memecoin_fills f "
        "JOIN memecoin_positions p ON p.id=f.position_id "
        "WHERE p.network_id=? AND p.execution_mode='real' AND f.tx_sig IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM memecoin_evm_tx_fees x WHERE x.tx_hash=lower(f.tx_sig) "
        "AND x.status='complete') "
        "GROUP BY lower(f.tx_sig) ORDER BY MIN(f.id) LIMIT ?",
        (str(network_id), int(limit)))]


def record_sol_tx_fee(conn, *, tx_sig, kind, fee, recorded_at=None):
    """Write-once Solana fee row (memecoin_sol_tx_fees). `fee` = {fee_lamports, tip_lamports, slot, status}. An
    unknown row may be completed later; a complete row is never rewritten. Unknown is never 0."""
    recorded_at = recorded_at or real_utc_now_iso()
    conn.execute(
        "INSERT INTO memecoin_sol_tx_fees (tx_sig, kind, slot, fee_lamports, tip_lamports, status, recorded_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(tx_sig) DO UPDATE SET slot=excluded.slot, "
        "fee_lamports=excluded.fee_lamports, tip_lamports=excluded.tip_lamports, status=excluded.status, "
        "recorded_at=excluded.recorded_at WHERE memecoin_sol_tx_fees.status != 'complete'",
        (str(tx_sig), kind, fee.get("slot"), fee.get("fee_lamports"), fee.get("tip_lamports"), fee["status"],
         recorded_at))
    conn.commit()


def sol_fee_backlog(conn, *, limit=20):
    """[(tx_sig, fill kind)] of funded Solana fills with no complete fee row, oldest first."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT f.tx_sig, MIN(f.kind) FROM memecoin_fills f "
        "JOIN memecoin_positions p ON p.id=f.position_id "
        "WHERE p.network_id='solana' AND p.execution_mode='real' AND f.tx_sig IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM memecoin_sol_tx_fees x WHERE x.tx_sig=f.tx_sig "
        "AND x.status='complete') "
        "GROUP BY f.tx_sig ORDER BY MIN(f.id) LIMIT ?", (int(limit),))]


def fee_missing_sql(conn):
    """SQL predicate on fill alias `f`: true when the fill has no known fee, i.e. neither fee column on
    the fill is set and no COMPLETE row exists in a per-tx fee table. A fill without a tx_sig counts:
    its fee is unknown and the reconciler cannot find it. A per-tx table absent
    from this ledger (read-only handle, runner not yet restarted on the DDL) means no reconciled fees
    there, not an error. Shared by fills_without_fee and the Desk copy-lane panel so they cannot
    disagree (Alpha 2026-10-21)."""
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('memecoin_sol_tx_fees', 'memecoin_evm_tx_fees')")}
    sql = "(f.network_fee_lamports IS NULL AND f.network_fee_wei IS NULL"
    if "memecoin_sol_tx_fees" in have:
        sql += (" AND NOT EXISTS (SELECT 1 FROM memecoin_sol_tx_fees s WHERE s.tx_sig=f.tx_sig "
                "AND s.status='complete')")
    if "memecoin_evm_tx_fees" in have:
        sql += (" AND NOT EXISTS (SELECT 1 FROM memecoin_evm_tx_fees e WHERE e.tx_hash=lower(f.tx_sig) "
                "AND e.status='complete')")
    return sql + ")"


def fills_without_fee(conn, *, since_iso=None):
    """{network_id: {kind: n}} of real fills with no known fee (fee_missing_sql): the counter
    that shows any fill path whose fee is not being recorded (2026-10-21).

    Known gap: approval and pre-transaction txs (EVM approve, WETH unwrap, SOL buffer swaps) are not
    fills, so their fees are not counted here or reconciled (Gamma 2026-10-21)."""
    sql = ("SELECT p.network_id, f.kind, COUNT(*) FROM memecoin_fills f "
           "JOIN memecoin_positions p ON p.id=f.position_id "
           "WHERE p.execution_mode='real' AND " + fee_missing_sql(conn))
    params = ()
    if since_iso is not None:
        sql += " AND f.created_at >= ?"
        params = (str(since_iso),)
    out = {}
    for network, kind, n in conn.execute(sql + " GROUP BY p.network_id, f.kind", params):
        out.setdefault(str(network), {})[kind] = n
    return out


def derisk_proceeds(conn, position_id):
    """Total USDC already recovered by 'derisk' partial fills — added back at final close so
    realized = (derisk + final received) - entry_notional, never just the final leg.

    None while ANY partial is still unpriced (Codex N14, Alpha 2026-10-07): a landed partial whose
    receipt is not yet proved is unknown money, and summing it as $0 would book a realized figure
    that looks known but is short by the partial, forever if the receipt never prices."""
    r = conn.execute("SELECT COALESCE(SUM(notional), 0.0), "
                     "COALESCE(SUM(price IS NULL OR notional IS NULL), 0) FROM memecoin_fills "
                     "WHERE position_id=? AND kind='derisk'", (position_id,)).fetchone()
    return None if r[1] else float(r[0])


def realized_after_exit(conn, position_id, received, entry_notional):
    """Realized PnL of a final exit: its proceeds plus every partial, minus entry. None (unknown)
    when the final proceeds or any partial is unpriced; never a number short by the partial."""
    derisk = derisk_proceeds(conn, position_id)
    if received is None or derisk is None:
        return None
    return float(received) + derisk - float(entry_notional or 0.0)


def exit_intent_for_tx(conn, position_id, action, tx_identity):
    """The persisted ExitIntent dict of THIS transaction (by claim row, not the position column a
    later exit overwrites), or None when no intent of that action carries that identity."""
    rows = conn.execute("SELECT intent_json FROM memecoin_exit_intents WHERE position_id=? "
                        "AND action=?", (position_id, action)).fetchall()
    found = []
    for (raw,) in rows:
        try:
            intent = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(intent, dict) and str(intent.get("tx_identity")) == str(tx_identity):
            found.append(intent)
    return found[0] if len(found) == 1 else None


def settle_confirmed_derisk_unpriced(conn, *, position_id, claim_id, sig, qty, fraction, at):
    """A partial the chain CONFIRMED but whose proceeds are not yet proved (Codex N14): clear the
    pending sig (so the full exit is never blocked) and book a kind='derisk' fill with price and
    notional NULL in ONE transaction. The fill is the durable work item: it takes the row out of
    `unverified_derisks` for good, so no later read can re-arm the rail into a second sale, and its
    qty (None when the intent cannot say) keeps the ledger quantity honest meanwhile."""
    try:
        cur = conn.execute(
            "UPDATE memecoin_positions SET derisk_sig=NULL, derisk_claim_id=NULL, "
            "derisk_recheck_hits=0 WHERE id=? AND derisk_claim_id IS ? AND derisk_sig=? "
            "AND derisked_at IS NOT NULL RETURNING signal_source,execution_mode,desk_cohort_id",
            (position_id, claim_id, sig))
        booked = cur.fetchall()
        if len(booked) != 1:
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
            "fraction, created_at, tx_sig) VALUES(?,?,?,?,?,?,?,?,?)",
            (position_id, "derisk", None, qty, None, "chain_confirmed_unpriced", fraction, at, sig))
        if claim_id is not None:
            conn.execute("UPDATE memecoin_exit_intents SET status='confirmed',updated_at=? "
                         "WHERE claim_id=?", (at, claim_id))
        if booked[0][0] == SIGNAL_SOURCE_DESK and booked[0][1] == "real" and booked[0][2]:
            _mark_desk_execution_cost(conn, position_id, f"partial:{position_id}:{sig}", at)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def unbooked_derisk_intents(conn, position_id):
    """De-risk intents of a funded position whose transaction has no fill on it: a partial that may
    have landed unbooked (pos 31099), or one stuck unresolved (MOONDENG 36517). For the operator's
    backfill tool only; each row carries the intent's own fields."""
    out = []
    for row in conn.execute(
            "SELECT i.claim_id, i.status, i.intent_json FROM memecoin_exit_intents i "
            "JOIN memecoin_positions p ON p.id=i.position_id "
            "WHERE i.position_id=? AND i.action='derisk' AND p.execution_mode='real' "
            "AND i.status IN ('confirmed','submitted_unknown','broadcast') ORDER BY i.created_at",
            (position_id,)):
        try:
            intent = _json.loads(row["intent_json"] or "{}")
        except ValueError:
            continue
        sig = intent.get("tx_identity")
        if not sig or conn.execute("SELECT 1 FROM memecoin_fills WHERE position_id=? AND tx_sig=?",
                                   (position_id, sig)).fetchone():
            continue
        out.append({"claim_id": row["claim_id"], "status": row["status"], "sig": sig,
                    "fraction": intent.get("fraction"),
                    "sell_amount_atomic": intent.get("sell_amount_atomic")})
    return out


def full_exit_qty_equals_entry_qty(conn, position_id):
    """True when the position's last full exit sold exactly the quantity it entered. A DIAGNOSTIC
    ONLY, never proof that a partial did not run: final = entry - partial + inflow, so equality
    shows partial == inflow (a transfer in, a distribution, a rebase), not partial == 0 (review
    C08, 2026-10-08)."""
    full = conn.execute(
        "SELECT qty FROM memecoin_fills WHERE position_id=? AND kind IN ('bracket_exit',"
        "'manual_close','mirror_exit') AND qty IS NOT NULL ORDER BY id DESC LIMIT 1",
        (position_id,)).fetchone()
    marks = ",".join("?" for _ in ENTRY_KINDS)
    entered = conn.execute(
        f"SELECT COALESCE(SUM(qty),0) FROM memecoin_fills WHERE position_id=? AND kind IN ({marks})",
        (position_id, *ENTRY_KINDS)).fetchone()[0]
    return (full is not None and entered > 0
            and abs(float(full[0]) - float(entered)) <= 1e-9 * float(entered))


def backfill_unbooked_derisk(conn, *, position_id, sig, qty, fraction, at):
    """Book a partial the chain shows landed but the ledger never recorded, as an unpriced derisk
    fill (the operator's backfill, --confirm only); reconcile_unpriced_derisk then prices it and
    settles realized. Returns the fill id, or None when the position is not funded, already has a
    fill for `sig`, or `qty` is not positive."""
    try:
        qty = float(qty)
        if not (math.isfinite(qty) and qty > 0):
            return None
        # CHECK AND INSERT AS ONE WRITE TRANSACTION (Codex CORE-01): the implicit transaction began
        # only at the INSERT, so two --confirm runs both saw no fill and both booked the same sale.
        # Operator recovery, never the entry or signing path, so taking the write lock up front is
        # free; the guarded INSERT also covers a caller already inside a transaction.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        real = conn.execute("SELECT signal_source,desk_cohort_id FROM memecoin_positions "
                            "WHERE id=? AND execution_mode='real'",
                            (position_id,)).fetchone()
        booked = conn.execute("SELECT 1 FROM memecoin_fills WHERE position_id=? AND tx_sig=?",
                              (position_id, sig)).fetchone()
        if not real or booked:
            conn.rollback()
            return None
        cur = conn.execute(
            "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
            "fraction, created_at, tx_sig) SELECT ?,?,?,?,?,?,?,?,? WHERE NOT EXISTS "
            "(SELECT 1 FROM memecoin_fills WHERE position_id=? AND tx_sig=?)",
            (position_id, "derisk", None, qty, None, "chain_confirmed_unpriced", fraction, at, sig,
             position_id, sig))
        if cur.rowcount != 1:
            conn.rollback()
            return None
        if real[0] == SIGNAL_SOURCE_DESK and real[1]:
            _mark_desk_execution_cost(conn, position_id, f"partial:{position_id}:{sig}", at)
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        raise


def set_unpriced_derisk_qty(conn, *, fill_id, position_id, sig, qty):
    """Give an unpriced partial booked without a quantity (its intent could not say) the quantity
    its receipt shows, once (Gamma LOW-2 on c91ac5689). False when it already has one."""
    try:
        qty = float(qty)
        if not (math.isfinite(qty) and qty > 0):
            return False
        cur = conn.execute(
            "UPDATE memecoin_fills SET qty=? WHERE id=? AND position_id=? AND tx_sig=? "
            "AND kind='derisk' AND qty IS NULL AND price IS NULL AND notional IS NULL "
            "AND fill_source='chain_confirmed_unpriced'", (qty, fill_id, position_id, sig))
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def unpriced_null_qty_derisk_fill(conn, fill_id):
    """The unpriced, quantity-less partial `fill_id` with its position, or None."""
    row = conn.execute(
        "SELECT f.id AS fill_id, f.tx_sig AS derisk_tx, f.fraction AS fill_fraction, p.* "
        "FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
        "WHERE f.id=? AND p.execution_mode='real' AND f.kind='derisk' AND f.qty IS NULL "
        "AND f.price IS NULL AND f.notional IS NULL AND f.fill_source='chain_confirmed_unpriced' "
        "AND f.tx_sig IS NOT NULL", (fill_id,)).fetchone()
    return dict(row) if row else None


def unpriced_derisk_fills(conn):
    """Confirmed partials still waiting for receipt proceeds, with their position."""
    return [dict(r) for r in conn.execute(
        "SELECT f.id AS fill_id, f.qty AS unpriced_qty, f.tx_sig AS derisk_tx, p.* "
        "FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
        "WHERE p.execution_mode='real' AND f.kind='derisk' AND f.price IS NULL "
        "AND f.notional IS NULL AND f.fill_source='chain_confirmed_unpriced' "
        "AND f.qty > 0 AND f.tx_sig IS NOT NULL ORDER BY f.id")]


def reconcile_unpriced_derisk(conn, *, fill_id, position_id, sig, notional, fill_source):
    """Price one unpriced partial from its receipt, once, and settle the realized it decides:
      open                      -> nothing more; the final close adds it via derisk_proceeds.
      closed, realized booked   -> that close predates the unpriced fill: realized += notional.
      closed, realized unknown  -> computed now only if every partial and full exit is priced."""
    try:
        notional = float(notional)
        if not (math.isfinite(notional) and notional > 0):
            return False
        cur = conn.execute(
            "UPDATE memecoin_fills SET price=?/qty, notional=?, fill_source=? WHERE id=? "
            "AND position_id=? AND tx_sig=? AND kind='derisk' AND price IS NULL "
            "AND notional IS NULL AND fill_source='chain_confirmed_unpriced' AND qty > 0",
            (notional, notional, fill_source, fill_id, position_id, sig))
        if cur.rowcount != 1:
            conn.rollback()
            return False
        pos = conn.execute("SELECT is_open, realized_pnl, entry_notional FROM memecoin_positions "
                           "WHERE id=?", (position_id,)).fetchone()
        if pos is not None and not pos["is_open"]:
            if pos["realized_pnl"] is not None:
                conn.execute("UPDATE memecoin_positions SET realized_pnl=realized_pnl+? WHERE id=?",
                             (notional, position_id))
            else:
                marks = ",".join("?" for _ in FULL_EXIT_KINDS)
                legs = conn.execute(
                    f"SELECT COALESCE(SUM(notional), 0.0), COALESCE(SUM(notional IS NULL), 0) "
                    f"FROM memecoin_fills WHERE position_id=? AND kind IN ({marks})",
                    (position_id, *FULL_EXIT_KINDS)).fetchone()
                realized = (None if legs[1] else
                            realized_after_exit(conn, position_id, legs[0], pos["entry_notional"]))
                if realized is not None:
                    conn.execute("UPDATE memecoin_positions SET realized_pnl=? WHERE id=? "
                                 "AND realized_pnl IS NULL", (realized, position_id))
            _latch_desk_day_for_position(conn, position_id)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def position_return_pct(conn, position_id, *, entry_notional, last_price, entry_price,
                       open_quantity=None):
    """Return on the POSITION's money, not on the token's price.

    `last/entry - 1` measures the TOKEN. After a partial de-risk the position no longer
    tracks it: the stake is partly back as USDC and a smaller bag rides on. The two
    diverge permanently from the moment the de-risk lands.

    Observed on 27035 (KIO, 2026-08-05): the price read +53.1% while the position was
    +46.2% ($49.68 recovered + $23.40 remaining on a $50 stake). The dangerous direction
    is the other one — had the coin gone to zero the price read would have shown -100%
    against an actual -0.6%, i.e. the display is most wrong exactly when the de-risk has
    done the thing it exists to do.

    Counts EVERY exit fill, not just the de-risk: on a closed row the final leg is a
    `bracket_exit`/`manual_close`, and summing only de-risk proceeds would report KIO's
    +36.8% as -0.6% (the recovered stake with the bag valued at nothing). Both callers
    pass open positions today, where the only exit so far is the de-risk, so this is
    also correct there — it just stops being a trap for the next caller.

    Reduces to the price return when nothing has been sold, so undederisked rows are
    unchanged. Falls back to the price return when the stake is unknown, because a
    position with no recorded notional has no denominator to be honest about.
    """
    if not last_price or not entry_price or entry_price <= 0:
        return None
    price_return = (last_price / entry_price - 1.0) * 100.0
    if not entry_notional or entry_notional <= 0:
        return price_return
    qty = open_qty(conn, position_id) if open_quantity is None else open_quantity
    marks = ",".join("?" for _ in EXIT_KINDS)
    recovered = float(conn.execute(
        f"SELECT COALESCE(SUM(notional),0) FROM memecoin_fills "
        f"WHERE position_id=? AND kind IN ({marks})",
        (position_id, *EXIT_KINDS)).fetchone()[0] or 0.0)
    if not recovered:
        return price_return
    value = recovered + max(0.0, float(qty)) * last_price
    return (value / float(entry_notional) - 1.0) * 100.0


def record_exec_latency(conn, *, mint, pending_id, status, sig, timing, at, impact_pct=None,
                        network_id=None, attempt_id=None, origin_event_id=None,
                        signal_source=None, source_price=None, price_attribution=None,
                        quote_price=None, quote_observed_at=None, quote_age_ms=None,
                        quote_id=None, quote_route_target=None, fill_price=None,
                        source_drift_pct=None, execution_slippage_pct=None,
                        total_copy_drift_pct=None, fee_paid_microlamports=None,
                        depth_first_rung_pct=None, tier_variant=None):
    """Persist one real-entry's Tier-0 stage latencies (gate/safety/exec/total ms) + the quote's
    price impact %% (filled AND aborted — the impact-cap knob calibrates from this distribution).
    `network_id` is the chain the entry routed on; NULL when the caller lacks it (backfilled).

    WO-7: fee_paid_microlamports / depth_first_rung_pct / tier_variant are the execution-tiering
    measurement fields — NULL until a variant is armed, so every tiering claim reads off real fills."""
    conn.execute(
        "INSERT INTO memecoin_exec_latency(mint, pending_id, status, sig, gate_ms, safety_ms, "
        "exec_ms, total_ms, at, impact_pct, network_id, attempt_id, origin_event_id, "
        "signal_source, source_price, price_attribution, quote_price, quote_observed_at, "
        "quote_age_ms, quote_id, quote_route_target, fill_price, source_drift_pct, "
        "execution_slippage_pct, total_copy_drift_pct, fee_paid_microlamports, "
        "depth_first_rung_pct, tier_variant) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mint, pending_id, status, sig, timing.get("gate_ms"), timing.get("safety_ms"),
         timing.get("exec_ms"), timing.get("total_ms"), at, impact_pct, network_id,
         attempt_id, origin_event_id, signal_source, source_price, price_attribution,
         quote_price, quote_observed_at, quote_age_ms, quote_id, quote_route_target, fill_price,
         source_drift_pct, execution_slippage_pct, total_copy_drift_pct, fee_paid_microlamports,
         depth_first_rung_pct, tier_variant))
    conn.commit()


def record_exit_execution(conn, *, position_id, network_id, exit_reason, attempted_at, status,
                          sig=None, total_ms=None, priority_fee_microlamports=0,
                          fee_paid_lamports=None, depth_first_rung_pct=None,
                          tier_variant="baseline", failure_type=None, stage_ms_json=None):
    """One funded EXIT attempt. Separate from entry latency; actual fee stays NULL until observed.

    `failure_type` is the CAUSE behind `status`. A stuck exit has two distinct causes -- an empty
    token balance and an exhausted sell ladder -- and without this they write identical rows.

    `stage_ms_json` is WHERE the time went: {"quote_ms":.., "quote_n":.., "approve_ms":.., ..}.
    Counts travel with the durations because a stage entered 4 times is a retry story that a mean
    hides. Telemetry only -- never read back into an exit decision."""
    conn.execute(
        "INSERT INTO memecoin_exit_execution_attempts(position_id,network_id,exit_reason,"
        "attempted_at,status,sig,total_ms,priority_fee_microlamports,fee_paid_lamports,"
        "depth_first_rung_pct,tier_variant,failure_type,stage_ms_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(position_id), str(network_id), str(exit_reason), str(attempted_at), str(status), sig,
         total_ms, int(priority_fee_microlamports or 0), fee_paid_lamports,
         depth_first_rung_pct, str(tier_variant or "baseline"),
         str(failure_type) if failure_type else None,
         str(stage_ms_json) if stage_ms_json else None))
    conn.commit()


def open_positions_by_strategy(conn, strategy, mode="paper", include_paused=False):
    """Open positions the exit sweep should WORK. Paused ones are excluded by default.

    A PAUSED POSITION IS STILL OPEN AND STILL OURS. `tracking_paused_at` says only "stop spending
    reads and quotes on this one" -- the row keeps `is_open=1`, the tokens are still in the wallet,
    and every exposure, bankroll and slot calculation must keep counting it. That is why this is
    filtered HERE, in what the sweep works, and NOT in `list_open_positions`, which accounting and
    the risk gates read.

    Operator-requested 2026-09-04 for a dust play whose frozen policy has every rail at 0 and which
    therefore could never produce an exit decision, while still costing 3,481 marks an hour.
    `include_paused=True` is for anything that must see the whole book.
    """
    sql = "SELECT * FROM memecoin_positions WHERE is_open=1 AND strategy=? AND execution_mode=?"
    if not include_paused:
        sql += " AND COALESCE(tracking_paused_at,'')=''"
    return [dict(r) for r in conn.execute(sql, (strategy, mode))]


def set_position_tracking_paused(conn, position_id, paused_at):
    """Pause or resume automatic tracking. `paused_at=None` resumes.

    Never touches `is_open`, quantity or PnL: this is an attention switch, not a close.
    """
    # The pause and close/de-risk claims are competing writes on the same row. If an executor
    # claimed first, report failure; if pause won, the claim predicates reject auto execution.
    pending = (" AND is_open=1 AND close_claim_id IS NULL AND close_sig IS NULL "
               "AND derisk_claim_id IS NULL AND derisk_sig IS NULL") if paused_at else ""
    cur = conn.execute("UPDATE memecoin_positions SET tracking_paused_at=? WHERE id=?" + pending,
                       (paused_at, int(position_id)))
    conn.commit()
    return cur.rowcount == 1


def get_position(conn, position_id):
    r = conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (position_id,)).fetchone()
    return dict(r) if r else None


# Fill kinds that add / remove open quantity. Partial exits keep a position open
# with reduced qty; both auto (mirror_exit) and manual (manual_close) fully retire it.
ENTRY_KINDS = ("entry", "manual_buy")
EXIT_KINDS = ("partial_exit", "derisk", "mirror_exit", "manual_close", "bracket_exit")


def full_exit_fill_kind(exit_reason):
    """Map the close trigger to the honest quantity-reducing ledger kind."""
    reason = str(exit_reason or "").strip().lower()
    if reason == "manual" or reason.startswith("manual_"):
        return "manual_close"
    if reason in ("mirror", "b_mirror", "mirror_exit", "mirror_source_sell"):
        return "mirror_exit"
    return "bracket_exit"


def shared_mint_open_real_count(conn, mint, network_id=None):
    """How many OPEN funded positions currently share this mint on this chain.

    Exits sell the WHOLE wallet balance, which is correct while one position owns the mint and
    silently wrong the moment two do: on 2026-08-13 two reservations raced into EUB1eZBt, both
    bought $32.01, and the first position's take-profit sold BOTH buys' tokens while being costed
    against one. Reported profit was inflated by exactly the untracked buy ($33.04 booked, $1.03
    true), and the second reservation orphaned and blocked deploys.

    That race became possible when MEMECOIN_REAL_ONE_POSITION_PER_COIN was turned off, so the
    exit has to know whether it is alone before it decides how much to sell.
    """
    if not mint:
        return 0
    net = normalized_coin_network(network_id, mint) if network_id is not None else None
    sql = ("SELECT COUNT(*) FROM memecoin_positions "
           "WHERE mint=? AND is_open=1 AND execution_mode='real'")
    params = [mint]
    if net is not None:
        sql += f" AND {COIN_NETWORK_SQL}=?"
        params.append(net)
    # IN-FLIGHT RESERVATIONS COUNT TOO. A reservation that has broadcast already owns tokens in the
    # wallet, and it is NOT a position yet — so counting positions alone missed the very race this
    # exists to stop. Measured 2026-08-13: position 37125 opened at 19:13:08, reservation 382
    # broadcast at 19:13:38, and 37125's take-profit at 19:13:41 sold 63,830 tokens having bought
    # 31,584. Booked +$44.42 on what was truly -$3.69.
    res_sql = ("SELECT COUNT(*) FROM memecoin_pending_entries "
               "WHERE mint=? AND status='reserved'")
    res_params = [mint]
    if net is not None:
        res_sql += " AND LOWER(COALESCE(network_id,''))=?"
        res_params.append(str(net).lower())
    try:
        positions = int(conn.execute(sql, params).fetchone()[0] or 0)
    except Exception:
        return 0
    try:
        pending = int(conn.execute(res_sql, res_params).fetchone()[0] or 0)
    except Exception:
        pending = 0
    return positions + pending


def open_qty(conn, position_id):
    """Remaining open quantity = entered − exited. Correct after partial sells."""
    def _sum(kinds):
        marks = ",".join("?" for _ in kinds)
        return float(conn.execute(
            f"SELECT COALESCE(SUM(qty),0) FROM memecoin_fills "
            f"WHERE position_id=? AND kind IN ({marks})",
            (position_id, *kinds)).fetchone()[0] or 0.0)
    return _sum(ENTRY_KINDS) - _sum(EXIT_KINDS)


def exit_qty_unknown(conn, position_id):
    """True when an exit fill on the position has no quantity (an unpriced partial whose intent
    could not say it). SUM skips NULL, so open_qty is then the quantity BEFORE that sale."""
    marks = ",".join("?" for _ in EXIT_KINDS)
    return conn.execute(
        f"SELECT 1 FROM memecoin_fills WHERE position_id=? AND kind IN ({marks}) "
        f"AND qty IS NULL LIMIT 1", (position_id, *EXIT_KINDS)).fetchone() is not None


def _chain_clause(chain):
    """('' , ()) when chain is None; else (' AND network_id IN (?,..)', ids).

    Raises on an unresolvable value — there is no pooled scope, so a typo must fail loudly
    rather than silently widening the read to every chain (spec 2026-07-30).
    """
    if chain is None:
        return "", ()
    from sniper.services.memecoin_chains import network_ids_for, resolve_chain
    key = resolve_chain(chain)
    if key is None:
        raise ValueError(f"unknown chain {chain!r}; chains are never pooled")
    ids = network_ids_for(key)
    return " AND network_id IN (%s)" % ",".join("?" * len(ids)), ids


def list_open_positions(conn, mode="paper", chain=None, *, include_paused=True):
    chain_sql, chain_params = _chain_clause(chain)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_positions WHERE is_open=1 AND execution_mode=?"
        + (" AND COALESCE(tracking_paused_at,'')=''" if not include_paused else "")
        + chain_sql + " ORDER BY opened_at DESC", (mode, *chain_params))]


def list_closed_positions(conn, mode="paper", limit=None, chain=None):
    """Closed positions for a single execution mode (default 'paper' — the A/B research board). Pass
    mode='real' for the real-money ledger; mode=None for both (audit only). Scoped so the paper board
    never shows real fills and vice versa.

    `limit` bounds the page (rows are closed_at DESC). Callers that display N rows must pass N —
    materialising all 22.5k paper rows at 85 columns each costs ~980ms, ~780ms of it turning
    sqlite3.Row into dict for rows that are then sliced away. Whole-ledger aggregates belong in
    closed_position_totals(), not in a discarded page."""
    chain_sql, chain_params = _chain_clause(chain)
    # The chain filter goes in the WHERE, BEFORE the limit: filtering a globally-limited window
    # client-side gives each chain an arbitrary slice of whichever chain was busiest.
    where = "is_open=0" + ("" if mode is None else " AND execution_mode=?") + chain_sql
    params = ((), (mode,))[mode is not None] + chain_params
    page = ""
    if limit is not None:
        page = " LIMIT ?"
        params = (*params, max(0, int(limit)))
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM memecoin_positions WHERE {where} ORDER BY closed_at DESC{page}", params)]


def closed_position_totals(conn, mode="paper", chain=None):
    """Whole-ledger closed aggregates: win rate, total PnL, PnL per source handle, entry-lag
    median/min. Reads three columns rather than all 85 — same arithmetic as scanning the full
    rows, ~40x cheaper, and it does not depend on the display page size.

    No ORDER BY: these are order-independent aggregates. Float addition is not associative, so
    per-wallet sums can differ from a closed_at-ordered scan in the last bits (measured max
    2.9e-11 on a -$11,728 wallet — identical to the cent). That is not a defect to chase; adding
    a sort here would buy bit-identity with a full 22.5k-row sort."""
    chain_sql, chain_params = _chain_clause(chain)
    where = "is_open=0" + ("" if mode is None else " AND execution_mode=?") + chain_sql
    params = ((), (mode,))[mode is not None] + chain_params
    rows = conn.execute(
        f"SELECT realized_pnl, entry_lag_s, source_handle FROM memecoin_positions WHERE {where}",
        params,
    ).fetchall()
    pnls = [row["realized_pnl"] for row in rows if row["realized_pnl"] is not None]
    by_wallet = {}
    for row in rows:
        by_wallet[row["source_handle"]] = (
            by_wallet.get(row["source_handle"], 0.0) + (row["realized_pnl"] or 0.0))
    lags = [row["entry_lag_s"] for row in rows if (row["entry_lag_s"] or -1) >= 0]
    return {
        "win_rate": (sum(1 for pnl in pnls if pnl > 0) / len(pnls)) if pnls else None,
        "total_pnl": sum(pnls),
        "by_wallet": by_wallet,
        "lag": {"median": statistics.median(lags) if lags else None,
                "min": min(lags) if lags else None},
    }


def entry_fill_for_attempt(conn, attempt_id, tx_sig):
    """The entry fill already booked for this attempt's transaction, or None. Looked up by
    attempt_id (indexed: about 10 us on the live ledger, where a scan by tx_sig costs about
    18 ms); the transaction hash must match too. Without an attempt id or a hash: None."""
    if not attempt_id or not tx_sig:
        return None
    row = conn.execute(
        "SELECT id, position_id, price, qty, notional, tx_sig FROM memecoin_fills "
        "WHERE attempt_id=? AND kind='entry' ORDER BY id", (attempt_id,)).fetchall()
    for fill_id, position_id, price, qty, notional, sig in row:
        if str(sig or "").lower() == str(tx_sig).lower():
            return {"id": fill_id, "position_id": position_id, "price": price, "qty": qty,
                    "notional": notional}
    return None


def insert_fill(conn, *, position_id, kind, price, qty, notional, fill_source,
                fraction, created_at, tx_sig=None, attempt_id=None, origin_event_id=None,
                network_id=None, signal_source=None, source_price=None,
                price_attribution=None, quote_price=None, quote_observed_at=None,
                quote_age_ms=None, quote_id=None, quote_route_target=None,
                source_drift_pct=None, execution_slippage_pct=None,
                total_copy_drift_pct=None, funding_asset=None, funding_spent_atomic=None,
                funding_sol_usd=None, network_fee_lamports=None,
                priority_fee_lamports=None, rent_lamports=None,
                returned_rent_lamports=None, network_fee_wei=None, commit=True):
    cur = conn.execute(
        "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
        "fraction, created_at, tx_sig, attempt_id, origin_event_id, network_id, signal_source, "
        "source_price, price_attribution, quote_price, quote_observed_at, quote_age_ms, quote_id, "
        "quote_route_target, source_drift_pct, execution_slippage_pct, total_copy_drift_pct, "
        "funding_asset, funding_spent_atomic, funding_sol_usd, network_fee_lamports, "
        "priority_fee_lamports, rent_lamports, returned_rent_lamports, network_fee_wei) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (position_id, kind, price, qty, notional, fill_source, fraction, created_at, tx_sig,
         attempt_id, origin_event_id, network_id, signal_source, source_price, price_attribution,
         quote_price, quote_observed_at, quote_age_ms, quote_id, quote_route_target,
         source_drift_pct, execution_slippage_pct, total_copy_drift_pct,
         funding_asset, str(funding_spent_atomic) if funding_spent_atomic is not None else None,
         funding_sol_usd, network_fee_lamports, priority_fee_lamports, rent_lamports,
         returned_rent_lamports, str(network_fee_wei) if network_fee_wei is not None else None))
    if commit:
        conn.commit()
    return cur.lastrowid


def close_position(
        conn, position_id, closed_at, realized_pnl, exit_reason=None, *, recorded_at=None):
    recorded_at = recorded_at or real_utc_now_iso()
    conn.execute(
        "UPDATE memecoin_positions SET is_open=0, closed_at=?, closed_recorded_at=?, "
        "realized_pnl=?, exit_reason=? WHERE id=?",
        (closed_at, recorded_at, realized_pnl, exit_reason, position_id))
    latch_desk_day_on_close(conn, position_id, closed_at)
    conn.commit()


def claim_close(conn, position_id):
    """Atomically claim a position for closing (is_open 1->0). Returns True iff THIS
    caller won the flip. The bracket sweep (runner proc) and manual close (web proc)
    both read open_qty then write; without this claim both could insert an exit fill
    and double-count. Insert the final exit fill + finalize_close ONLY when this is True."""
    cur = conn.execute(
        "UPDATE memecoin_positions SET is_open=0 WHERE id=? AND is_open=1 "
        "AND close_claim_id IS NULL AND close_sig IS NULL", (position_id,))
    conn.commit()
    return cur.rowcount > 0


def reopen_position(conn, position_id):
    """Undo a claim_close (is_open 0->1) for a PARTIAL exit that keeps a remainder open.
    Only reopens a position that has no close bookkeeping yet (closed_at IS NULL)."""
    conn.execute("UPDATE memecoin_positions SET is_open=1 WHERE id=? AND closed_at IS NULL",
                 (position_id,))
    conn.commit()


# The full-exit legs of a close (NOT the de-risk partials, which are recovered separately).
FULL_EXIT_KINDS = ("mirror_exit", "manual_close", "bracket_exit")

# Fill sources that are NOT executable evidence of an outcome — the price booked is one we could
# never have transacted at, so a close carried only by these was never observed:
#   noquote — no quote existed; the leg is booked at the entry price (0 PnL by construction).
#   cleanup — `paper_unquotable_cleanup` books the STALE last-known mark, possibly hours old.
# Single source of truth: the report censors on it and orphan recovery preserves it. Treating a
# non-executable leg as a real fill is how a censored outcome gets laundered into a measured one.
NON_EXECUTABLE_FILL_SOURCES = ("noquote", "cleanup")

# WHEN WE COULD NOT PRICE AN EXIT, THE LEDGER BOOKED THE ENTRY PRICE. The arithmetic then produced
# exactly 0.0 realized PnL and stored it as a fact — 2,823 paper closes indistinguishable from a
# real round trip at flat. `fill_source` alone cannot recover which those were: `noquote` always
# substitutes, but `cleanup` books a genuine stale quote when one exists and only falls back to the
# entry price when it does not, so the same source covers both a real price and a fabricated one.
# This marker says the price is a PLACEHOLDER rather than an observation, so no reader has to infer
# it from `price == entry_price`. Deriving a fact from an equality is the same class of mistake as
# parsing a label out of prose.
#
# FORWARD ONLY from 2026-08-29. Rows closed before that date carry the fabricated 0.0 and no
# marker, and are NOT backfilled — an era boundary you can see rather than a rewrite you cannot.
PLACEHOLDER_PRICE_ATTRIBUTION = "placeholder_entry_price"
PLACEHOLDER_ERA_START = "2026-08-29"


def placeholder_exit_rate_by_arm(conn, *, since=None, execution_mode="paper"):
    """Share of closes per strategy whose exit price was INVENTED rather than observed.

    WHY THIS IS REPORTED AND NOT JUST CENSORED. The registered censor protects the ESTIMAND; it
    does not protect a human reading a raw arm table. On 2026-08-29 the fabrication rate was 9.6%
    on combo_f, 15.5% on momentum_a, and 24% on BOTH funded_clone and profile_matched_v1 — the two
    smallest arms, and the two being compared against each other.

    A matched rate would cancel in a paired difference. Nothing constrains it to stay matched, and
    the moment the two arms diverge on this rate the fabrication becomes a DIFFERENTIAL bias in an
    estimand whose entire claim is that the arms differ only in policy. So the rate itself is
    evidence and belongs beside the comparison, not in an archaeology query after the fact.

    Returns {strategy: {"closed": n, "placeholder": k, "rate": k/n}}.
    """
    kinds = "','".join(EXIT_KINDS)
    where = "p.execution_mode=? AND p.is_open=0"
    args = [execution_mode]
    if since:
        where += " AND p.closed_at>=?"
        args.append(since)
    rows = conn.execute(
        f"SELECT p.strategy, COUNT(*) closed, SUM(CASE WHEN EXISTS ("
        f"  SELECT 1 FROM memecoin_fills f WHERE f.position_id=p.id"
        f"  AND f.kind IN ('{kinds}')"
        f"  AND (f.price_attribution=? OR f.fill_source='noquote')) THEN 1 ELSE 0 END) placeholder"
        f" FROM memecoin_positions p WHERE {where} GROUP BY p.strategy",
        (PLACEHOLDER_PRICE_ATTRIBUTION, *args)).fetchall()
    out = {}
    for r in rows:
        closed, k = int(r[1] or 0), int(r[2] or 0)
        out[r[0]] = {"closed": closed, "placeholder": k,
                     "rate": (k / closed) if closed else None}
    return out


def full_exit_legs(conn, position_id):
    """Full-exit legs of a close, split by whether they are EXECUTABLE evidence.

    `full_exit_proceeds` answers "did a sell get booked" — enough for the funded path, where the
    only question is whether to finalize or quarantine. Paper recovery needs more: a leg booked
    `noquote`/`cleanup` means the close happened but was never OBSERVED at a transactable price,
    so finalizing it as a measured outcome launders a censored row into evidence.

    Returns {count, notional, executable_count, last_at}. `last_at` is the newest leg's
    `created_at` — the truthful close time, versus whenever recovery happened to run.
    """
    marks = ",".join("?" for _ in FULL_EXIT_KINDS)
    nonexec = ",".join("?" for _ in NON_EXECUTABLE_FILL_SOURCES)
    r = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(notional), 0.0), "
        f"       COALESCE(SUM(CASE WHEN COALESCE(fill_source,'') NOT IN ({nonexec}) "
        f"                    THEN 1 ELSE 0 END), 0), "
        f"       MAX(created_at) "
        f"FROM memecoin_fills WHERE position_id=? AND kind IN ({marks})",
        (*NON_EXECUTABLE_FILL_SOURCES, position_id, *FULL_EXIT_KINDS)).fetchone()
    return {"count": int(r[0]), "notional": float(r[1]),
            "executable_count": int(r[2]), "last_at": r[3]}


def orphaned_closes(conn, mode="real"):
    """Positions claimed for close (is_open flipped to 0) but never finalized (closed_at NULL).

    A crash struck in the window between `claim_close` and `finalize_close`. In the FUNDED executor
    that window is sub-millisecond (WO-9 crash-injection surfaced it: no network I/O sits between
    the two commits, so only a hard kill / power loss can land here, but the money path warrants
    defence-in-depth). In the PAPER exit (`memecoin_exits._close`) it is NOT: a route-quote RPC
    (`_exit_quote_fn`) runs between the claim and the fill, so the window is seconds wide and any
    restart lands in it — worst on 4663, whose blind quote path is the slowest of any chain.

    Such a row is invisible to the open-position sweep (is_open=0) and has no close record. Left
    unrecovered it is laundered into the arm PnL as a clean $0.00 close, because `_accumulate_arm`
    reads `realized_pnl or 0.0` and a NULL exit_reason never matches the `_noquote` censor.

    `mode` scopes the queue to one ledger: the funded sweep owns 'real' (it QUARANTINES a no-fill
    orphan, since on-chain submission truth is unknown), the paper sweep owns 'paper' (it REOPENS,
    which is safe because nothing was ever sold)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_positions WHERE is_open=0 AND closed_at IS NULL "
        "AND execution_mode=?", (mode,))]


def full_exit_proceeds(conn, position_id):
    """(count, total_notional) of a position's booked FULL-exit legs — the USDC a claimed-but-
    unfinalized close already recorded receiving. Count distinguishes 'sell was booked, finalize
    didn't run' (finalize from these) from 'claim won, nothing sold yet' (reopen and retry)."""
    marks = ",".join("?" for _ in FULL_EXIT_KINDS)
    r = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(notional), 0.0) FROM memecoin_fills "
        f"WHERE position_id=? AND kind IN ({marks})",
        (position_id, *FULL_EXIT_KINDS)).fetchone()
    return int(r[0]), float(r[1])


def finalize_close(
        conn, position_id, closed_at, realized_pnl, exit_reason=None, *, recorded_at=None):
    """Write the close bookkeeping after claim_close won (is_open already 0).

    `time_to_peak_s` and `max_gain_pct` are derived HERE because this is the one statement every
    lane's close runs through. They were 0 of 1041 on funded positions: the only caller of
    `set_position_instrumentation` is the paper close in `memecoin_exits` and `_close_instrumentation`
    in the copy trader, and the funded runner calls neither. So the two fields that answer "how far
    did it run before it turned, and when" existed on every funded row and were empty on all of
    them — measured 2026-09-08, while trying to decompose the exit overshoot.

    COALESCE, so an explicit `set_position_instrumentation` written moments earlier by the paper
    lane wins and is not recomputed. The arithmetic is the same as that helper's: elapsed
    opened_at -> peak_ts, and peak over entry as a percentage. It is done in SQL rather than Python
    because this statement is on the funded money path and must not gain a way to raise; a NULL
    input yields a NULL column, never a wrong number.
    """
    recorded_at = recorded_at or real_utc_now_iso()
    conn.execute(
        "UPDATE memecoin_positions SET closed_at=?, closed_recorded_at=?, realized_pnl=?, "
        "exit_reason=?, "
        "time_to_peak_s=COALESCE(time_to_peak_s, CASE "
        "  WHEN peak_ts IS NOT NULL AND opened_at IS NOT NULL "
        "  THEN max(0.0, (julianday(peak_ts) - julianday(opened_at)) * 86400.0) END), "
        "max_gain_pct=COALESCE(max_gain_pct, CASE "
        "  WHEN entry_price > 0 AND peak_price IS NOT NULL "
        "  THEN (peak_price / entry_price - 1.0) * 100.0 END) "
        "WHERE id=?",
        (closed_at, recorded_at, realized_pnl, exit_reason, position_id))
    latch_desk_day_on_close(conn, position_id, closed_at)
    conn.commit()


def upsert_candidate(conn, ev):
    """Record/accumulate a non-curated large-buy sighting (the discovery funnel). Counters
    accumulate; status is PRESERVED on conflict so an ignored/promoted candidate stays so."""
    size = float(getattr(ev, "their_size_usd", 0) or 0)
    conn.execute(
        """INSERT INTO memecoin_candidates
             (source_user_id, handle, sighting_count, total_buy_usd, max_buy_usd,
              first_seen, last_seen, last_ticker, last_mint, status)
           VALUES (?,?,1,?,?,?,?,?,?,'new')
           ON CONFLICT(source_user_id) DO UPDATE SET
             handle=excluded.handle,
             sighting_count=sighting_count+1,
             total_buy_usd=total_buy_usd+excluded.total_buy_usd,
             max_buy_usd=MAX(max_buy_usd, excluded.max_buy_usd),
             last_seen=excluded.last_seen,
             last_ticker=excluded.last_ticker,
             last_mint=excluded.last_mint""",
        (ev.source_user_id, ev.source_handle, size, size,
         ev.created_at, ev.created_at, ev.ticker, ev.mint))
    conn.commit()


def get_candidate(conn, uid):
    r = conn.execute("SELECT * FROM memecoin_candidates WHERE source_user_id=?", (uid,)).fetchone()
    return dict(r) if r else None


def set_candidate_status(conn, uid, status):
    conn.execute("UPDATE memecoin_candidates SET status=? WHERE source_user_id=?", (status, uid))
    conn.commit()


def promote_candidate(conn, uid):
    """Atomically promote one existing, non-excluded candidate to observed."""
    cand = get_candidate(conn, uid)
    if not cand:
        return {"ok": False, "error": "candidate_not_found"}
    wallet = conn.execute(
        "SELECT COALESCE(excluded,0) excluded FROM memecoin_wallets WHERE source_user_id=?",
        (uid,)).fetchone()
    if wallet and wallet["excluded"]:
        return {"ok": False, "error": "excluded_requires_reobserve"}
    handle = cand.get("handle") or ""
    try:
        conn.execute(
            "INSERT INTO memecoin_wallets(source_user_id,handle,label,paused,source,added_at,"
            "discovery_source) VALUES (?,?, '',0,'manual',strftime('%Y-%m-%dT%H:%M:%SZ','now'),?) "
            "ON CONFLICT(source_user_id) DO UPDATE SET source='manual',paused=0,"
            "handle=excluded.handle,discovery_source=excluded.discovery_source",
            (uid, handle, cand.get("source") or "manual"))
        conn.execute(
            "UPDATE memecoin_candidates SET status='promoted' WHERE source_user_id=?", (uid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"ok": True, "handle": handle}


def recent_armed_signals(conn, *, since_iso, limit=15):
    """Pre-trade board (2026-07-18): recent large BUYS from ARMED wallets (real_approved, not
    paused/excluded — the wallets that actually trigger real entries), grouped by coin. One row per
    (network_id, mint): consensus = distinct armed buyers in-window, plus the latest handle / size /
    mcap / ticker / timestamp. Read-only; ordered newest first. This is 'what are my wallets buying
    right now', distinct from the full Radar firehose (all traders)."""
    rows = conn.execute(
        """
        SELECT o.network_id AS network_id, o.mint AS mint,
               MAX(o.ticker) AS ticker,
               COUNT(DISTINCT o.source_user_id) AS consensus,
               MAX(o.source_ts) AS last_ts,
               MAX(o.their_size_usd) AS max_size_usd,
               (SELECT source_handle FROM memecoin_feed_observations_v2 x
                 WHERE x.network_id=o.network_id AND x.mint=o.mint AND x.normalized_side='buy'
                 ORDER BY x.source_ts DESC LIMIT 1) AS last_handle,
               (SELECT their_market_cap FROM memecoin_feed_observations_v2 x
                 WHERE x.network_id=o.network_id AND x.mint=o.mint AND x.normalized_side='buy'
                   AND x.their_market_cap>0 ORDER BY x.source_ts DESC LIMIT 1) AS mcap
        FROM memecoin_feed_observations_v2 o
        WHERE o.normalized_side='buy' AND o.source_ts >= ?
          AND o.source_user_id IN (
            SELECT source_user_id FROM memecoin_wallets
            WHERE real_approved=1 AND COALESCE(paused,0)=0 AND COALESCE(excluded,0)=0)
        GROUP BY o.network_id, o.mint
        ORDER BY last_ts DESC
        LIMIT ?
        """,
        (since_iso, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def count_candidates(conn, min_sightings=1, min_usd=0.0, status="new", *, now_iso=None):
    if status != "excluded":
        return int(conn.execute(
            "SELECT COUNT(*) FROM memecoin_candidates WHERE status=? "
            "AND sighting_count>=? AND total_buy_usd>=?",
            (status, int(min_sightings), float(min_usd))).fetchone()[0])
    now_iso = now_iso or real_utc_now_iso()
    return int(conn.execute(
        "SELECT COUNT(*) FROM memecoin_candidates c "
        "JOIN memecoin_wallets w ON w.source_user_id=c.source_user_id "
        "JOIN (SELECT source_user_id,COUNT(*) n,COALESCE(SUM(their_size_usd),0) usd "
        "      FROM memecoin_events WHERE action='candidate' AND side='buy' "
        "      AND julianday(created_at)>=julianday(?,'-7 days') "
        "      AND julianday(created_at)<=julianday(?) GROUP BY source_user_id) r "
        "ON r.source_user_id=c.source_user_id "
        "WHERE COALESCE(w.excluded,0)=1 AND r.n>=? AND r.usd>=?",
        (now_iso, now_iso, int(min_sightings), float(min_usd))).fetchone()[0])


def list_candidates(conn, min_sightings=1, min_usd=0.0, limit=200, *, offset=0,
                    status="new", now_iso=None):
    """Candidate evidence ordered by recency; lifetime totals remain display-only."""
    now_iso = now_iso or real_utc_now_iso()
    if status == "excluded":
        where_sql = ("COALESCE(w.excluded,0)=1 AND "
                     "COALESCE(r.fresh_sightings_7d,0)>=? AND "
                     "COALESCE(r.fresh_buy_usd_7d,0)>=?")
        where_params = (int(min_sightings), float(min_usd))
    else:
        where_sql = "c.status=? AND c.sighting_count>=? AND c.total_buy_usd>=?"
        where_params = (status, int(min_sightings), float(min_usd))
    rows = conn.execute(
        f"""WITH recent AS (
               SELECT source_user_id, COUNT(*) AS fresh_sightings_7d,
                      COALESCE(SUM(their_size_usd),0) AS fresh_buy_usd_7d,
                      COUNT(DISTINCT COALESCE(network_id,'') || ':' || COALESCE(mint,''))
                          AS distinct_coins_7d
               FROM memecoin_events
               WHERE action='candidate' AND side='buy'
                 AND julianday(created_at)>=julianday(?,'-7 days')
                 AND julianday(created_at)<=julianday(?)
               GROUP BY source_user_id
           ), latest AS (
               SELECT source_user_id,network_id FROM (
                   SELECT source_user_id,network_id,
                          ROW_NUMBER() OVER (
                              PARTITION BY source_user_id
                              ORDER BY julianday(created_at) DESC,id DESC) AS rn
                   FROM memecoin_events
                   WHERE action='candidate'
               ) WHERE rn=1
           )
           SELECT c.*,
                  a.n_closed, a.realized_pnl_usd, a.win_rate, a.median_pnl_pct,
                  a.dollar_weighted_return_pct, a.top3_win_share, a.coverage_ratio,
                  a.unmatched_sell_ratio, a.computed_at AS analyzed_at,
                  a.fomo_total_pnl, a.fomo_unrealized_pnl, a.on_leaderboard, a.leaderboard_rank,
                  COALESCE(r.fresh_sightings_7d,0) AS fresh_sightings_7d,
                  COALESCE(r.fresh_buy_usd_7d,0) AS fresh_buy_usd_7d,
                  COALESCE(r.distinct_coins_7d,0) AS distinct_coins_7d,
                  l.network_id,
                  COALESCE(w.paused,0) AS wallet_paused,
                  COALESCE(w.excluded,0) AS wallet_excluded,
                  COALESCE(w.real_approved,0) AS wallet_real_approved
           FROM memecoin_candidates c
           LEFT JOIN memecoin_trader_analytics a ON a.source_user_id = c.source_user_id
           LEFT JOIN recent r ON r.source_user_id=c.source_user_id
           LEFT JOIN latest l ON l.source_user_id=c.source_user_id
           LEFT JOIN memecoin_wallets w ON w.source_user_id=c.source_user_id
           WHERE {where_sql}
           ORDER BY julianday(c.last_seen) DESC,
                    COALESCE(r.fresh_buy_usd_7d,0) DESC,c.source_user_id
           LIMIT ? OFFSET ?""",
        (now_iso, now_iso, *where_params, int(limit), int(offset)))
    return [dict(r) for r in rows]


def list_curated_with_stats(conn):
    """Curated wallets joined with (a) their analyzer screen, (b) our copy performance, and
    (c) last feed signal — the combined Traders+Analytics view. One row per curated wallet."""
    stats = wallet_stats(conn)
    # honest αb copy evidence (the arming criterion) alongside the blended cp·pnl — one batched pass
    ab = alpha_b_copy_stats_all(conn)
    ana = {r["source_user_id"]: r for r in list_trader_analytics(conn)}
    last_by_uid = {r["source_user_id"]: r["last_signal"] for r in conn.execute(
        "SELECT source_user_id,MAX(created_at) AS last_signal FROM memecoin_events "
        "GROUP BY source_user_id")}
    # Pause and exclusion remove all authority. Hide stale stored lists from old releases.
    configured_chains_by_uid = {
        r["source_user_id"]: sorted(_parse_real_chains(r["real_chains"]))
        for r in conn.execute(
            "SELECT source_user_id, real_chains FROM memecoin_wallets "
            "WHERE COALESCE(real_approved,0)=1 AND paused=0 AND COALESCE(excluded,0)=0")
    }
    out = []
    for w in list_wallets(conn):
        uid = w["source_user_id"]
        a = ana.get(uid) or {}
        s = stats.get(uid) or {}
        configured_chains = configured_chains_by_uid.get(uid, [])
        armed_chains = configured_chains
        out.append({
            "source_user_id": uid, "handle": w.get("handle"),
            "source": w.get("source"), "paused": w.get("paused"),
            "real_approved": w.get("real_approved") or 0,
            "excluded": w.get("excluded") or 0,
            "armed": 1 if armed_chains else 0,
            "armed_chains": armed_chains,
            "configured_chains": configured_chains,
            "last_signal": last_by_uid.get(uid),
            "copy_open": s.get("open_count"), "copy_closed": s.get("closed_count"),
            "copy_realized": s.get("realized_pnl"), "copy_wr": s.get("win_rate"),
            # honest αb (alpha_b-only, non-real, fomo-excluded) — the arm gate's own metric
            "copy_ab_closed": (ab.get(uid) or {}).get("n_closes", 0),
            "copy_ab_realized": (ab.get(uid) or {}).get("pnl", 0.0),
            "n_closed": a.get("n_closed"), "realized_pnl_usd": a.get("realized_pnl_usd"),
            "matched_lots": a.get("matched_lots"),
            "distinct_exit_events": a.get("distinct_exit_events"),
            "distinct_closed_mints": a.get("distinct_closed_mints"),
            "median_pnl_pct": a.get("median_pnl_pct"),
            "dollar_weighted_return_pct": a.get("dollar_weighted_return_pct"),
            "win_rate": a.get("win_rate"), "top3_win_share": a.get("top3_win_share"),
            "coverage_ratio": a.get("coverage_ratio"),
            "unmatched_sell_ratio": a.get("unmatched_sell_ratio"),
            "analyzed_at": a.get("computed_at"),
            # selection-data upgrade: fomo lifetime PnL + live unrealized + windowed realized
            "fomo_total_pnl": a.get("fomo_total_pnl"),
            "fomo_unrealized_pnl": a.get("fomo_unrealized_pnl"),
            "fomo_unrealized_partial": a.get("fomo_unrealized_partial"),
            "on_leaderboard": a.get("on_leaderboard"),
            "leaderboard_rank": a.get("leaderboard_rank"),
            "unrealized_pnl_usd": a.get("unrealized_pnl_usd"),
            "live_holdings_value_usd": a.get("live_holdings_value_usd"),
            "median_hold_s": a.get("median_hold_s"),
            "last_swap_age_h": a.get("last_swap_age_h"),
            "active_days": a.get("active_days"),
            "realized_pnl_7d": a.get("realized_pnl_7d"),
            "realized_7d_covered": a.get("realized_7d_covered"),
            "realized_pnl_30d": a.get("realized_pnl_30d"),
            "realized_30d_covered": a.get("realized_30d_covered"),
        })
    return out


def create_signal_outcome(conn, *, fomo_event_id, source_user_id, handle, mint, ticker,
                          buy_ts, ref_price, their_size_usd, now_iso, network_id=None):
    """Open a post-buy price-path capture for one sighting. Dedups on fomo_event_id.
    Returns True iff a new row was created."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_signal_outcomes(fomo_event_id, source_user_id, handle, "
        "mint, ticker, buy_ts, ref_price, their_size_usd, marks_json, status, created_at, updated_at, "
        "network_id) VALUES (?,?,?,?,?,?,?,?,'{}','open',?,?,?)",
        (fomo_event_id, source_user_id, handle, mint, ticker, buy_ts, ref_price,
         their_size_usd, now_iso, now_iso, network_id))
    conn.commit()
    return cur.rowcount > 0


def list_open_outcomes(conn):
    """Open capture rows with marks_json parsed into a `marks` dict (keys are str horizon_s)."""
    out = []
    for r in conn.execute("SELECT * FROM memecoin_signal_outcomes WHERE status='open'"):
        d = dict(r)
        d["marks"] = _json.loads(d.get("marks_json") or "{}")
        out.append(d)
    return out


# Rows per write transaction. See `save_outcomes` for the measured lock-hold curve this comes from.
SAVE_OUTCOME_CHUNK = 1000

_SAVE_OUTCOME_SQL = (
    "UPDATE memecoin_signal_outcomes SET marks_json=?, max_gain_pct=?, peak_horizon_s=?, "
    "last_price=?, n_marks=?, status=?, updated_at=? WHERE fomo_event_id=?")


def _outcome_params(fomo_event_id, marks, max_gain_pct, peak_horizon_s, last_price,
                    n_marks, status, now_iso):
    return (_json.dumps({str(k): v for k, v in marks.items()}), max_gain_pct, peak_horizon_s,
            last_price, n_marks, status, now_iso, fomo_event_id)


def save_outcomes(conn, updates, *, now_iso):
    """Persist MANY recomputed capture rows in ONE short write transaction.

    WHY THIS EXISTS. `save_outcome` committed per row and the caller looped over every open
    capture row, so one pass became one transaction per row. Each commit copies every dirty page
    into the WAL as its own frames, and the next row copies the SAME pages again — `status` is a
    three-value column over 104k rows, so every write lands on the same handful of
    `idx_mc_outcome_status` leaves. Measured on the live ledger: 4,072 WAL frames over 1,297
    distinct pages in eight seconds (990 commit frames, ~124 commits/s), with the hottest pages
    written 84 to 192 times each. That is a 4 KB page copied ~100 times because ~100 separate
    transactions each touched it, and it is why the runner wrote 5.2 MB/s to insert ~6 rows/s.

    TWO-PHASE, NOT ONE LONG TRANSACTION (Alpha, 2026-08-27). The caller does every read and every
    recompute FIRST and hands the finished rows here, so the write lock covers the applies only.
    The reason is lock contention, not WAL size: the web process writes manual intents to the same
    database and the funded exit sweep wants the same file, so a long write lock is paid by the
    operator's own buys.

    CHUNKED, AND THE BOUND COMES FROM A MEASUREMENT. On a replica of this table's real shape
    (104k rows, the same two indexes, the same marks_json payload) the lock hold for ONE
    transaction was 0.6 ms at 100 rows, 3.2 ms at 1,000, and 13.8 ms for the entire 4,400-row open
    set. Even unbounded it never approached a sweep interval, so the chunk is insurance against the
    open set growing rather than a fix for a measured stall. 1,000 keeps the hold near 3 ms with
    four times the headroom over today's open set.

    WHAT A LOST BATCH COSTS — NOT "NOTHING", AND NOT CORRUPTION (Beta, 2026-08-27, correcting me).
    Marks are ACCUMULATED, not recomputed: the caller starts from the stored marks_json and adds
    newly-due horizons from a live quote taken in that pass. That quote is not replayable, so a
    lost batch does NOT reproduce itself — the horizon refills from a later quote instead. It is
    still safe to batch because every mark carries its own `age_s` sampled at the real instant, so
    a refill records the larger age truthfully. A lost batch therefore DEGRADES RESOLUTION and
    never corrupts. Do not call this idempotent; it is not, and the distinction matters if anyone
    later leans on that word to justify a longer window.

    `updates` is an iterable of dicts with the same keys `save_outcome` takes. Returns the count
    written. Empty input commits nothing.
    """
    params = [
        _outcome_params(u["fomo_event_id"], u["marks"], u["max_gain_pct"], u["peak_horizon_s"],
                        u["last_price"], u["n_marks"], u["status"], now_iso)
        for u in updates
    ]
    if not params:
        return 0
    for i in range(0, len(params), SAVE_OUTCOME_CHUNK):
        try:
            conn.executemany(_SAVE_OUTCOME_SQL, params[i:i + SAVE_OUTCOME_CHUNK])
            conn.commit()
        except Exception:
            # ROLL BACK, DO NOT SWALLOW. `get_connection` is transactional (Python's default
            # isolation_level=""), which is what makes the batching real — and it also means a
            # raising `executemany` leaves an OPEN WRITE TRANSACTION on this connection. If the
            # caller catches and carries on, that lock is held until something else commits on the
            # same connection or the process dies, and it blocks the funded exit sweep and the
            # web's manual-intent writes. That is the exact contention this batching exists to
            # avoid, and the failure window is wider than the per-row version's ever was
            # (Beta, 2026-08-27, reproduced: in_transaction True with 5 of 10 rows applied).
            #
            # Losing the batch is the cheap side of the trade: per this function's loss semantics a
            # dropped batch degrades resolution and never corrupts. Holding the lock is not cheap.
            # Chunks already committed stay committed — each is a complete unit, and partial
            # progress across chunks carries the same degrades-resolution cost.
            conn.rollback()
            raise
    return len(params)


def save_outcome(conn, fomo_event_id, *, marks, max_gain_pct, peak_horizon_s, last_price,
                 n_marks, status, now_iso):
    """Persist recomputed marks + derived scalars for ONE capture row. `marks` maps
    horizon_s (int) -> {price, age_s}; stored as JSON (string keys).

    Single-row convenience over `save_outcomes`. A loop over this function is the shape that
    caused the amplification documented there — batch instead.
    """
    return save_outcomes(conn, [{
        "fomo_event_id": fomo_event_id, "marks": marks, "max_gain_pct": max_gain_pct,
        "peak_horizon_s": peak_horizon_s, "last_price": last_price, "n_marks": n_marks,
        "status": status,
    }], now_iso=now_iso)


def _median(v):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    n = len(v); mid = n // 2
    return v[mid] if n % 2 else (v[mid - 1] + v[mid]) / 2.0


def _pctile(v, q):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q; lo = int(k); hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _open_realized_pnl(conn, position_id, entry_price):
    """Booked PnL from partial exits while a position remains open."""
    if not entry_price:
        return 0.0
    rows = conn.execute(
        "SELECT price, qty FROM memecoin_fills WHERE position_id=? AND kind='partial_exit'",
        (position_id,)).fetchall()
    total = 0.0
    for price, qty in rows:
        if price is None or qty is None:
            continue
        total += (float(price) - float(entry_price)) * float(qty)
    return total


def trader_pump_profile(conn, min_signals=1, hit_pct=None, source_user_ids=None):
    """Per-trader front-run read over MATURED outcomes: does this trader's large buy pump?
    n_signals, median/mean/p90 max-gain, pump_hit_rate (>= hit_pct), median peak horizon,
    and early-move medians (1m/5m gain from the sampled marks). Only traders with
    n_signals >= min_signals are returned. Display/ranking signal — not a gate yet."""
    from sniper.settings import memecoin as _cfg
    hit_pct = _cfg.MEMECOIN_PUMP_HIT_PCT if hit_pct is None else hit_pct
    params = []
    user_sql = ""
    if source_user_ids is not None:
        source_user_ids = list(dict.fromkeys(source_user_ids))
        if not source_user_ids:
            return []
        user_sql = " AND source_user_id IN (%s)" % ",".join("?" for _ in source_user_ids)
        params.extend(source_user_ids)
    by = {}
    # PROJECT, DO NOT `SELECT *`. This reads 96% of a 104k-row table and the UI refetches it every
    # 4 seconds while the operator sits on the discovery, traders or performance subtab, so the
    # 14 unused columns are paid continuously on the funded host. Measured on the live ledger:
    # `SELECT *` 410.8 ms / 40.90 MB against this projection 150.2 ms / 17.51 MB — 2.7x faster for
    # the same output. `marks_json` stays because `_mark_gain` parses it for the 1m and 5m
    # horizons; the six below are exactly what this function reads and nothing more.
    for r in conn.execute(
            "SELECT source_user_id, handle, max_gain_pct, peak_horizon_s, ref_price, marks_json "
            "FROM memecoin_signal_outcomes WHERE status='matured'" + user_sql,
            params):
        by.setdefault(r["source_user_id"], []).append(dict(r))
    out = []
    for uid, rows in by.items():
        gains = [x["max_gain_pct"] for x in rows if x["max_gain_pct"] is not None]
        # n_signals = outcomes with a REAL gain (abandoned dust rows have None gain and must
        # not pad the sample past the thin-n grey gate); n_matured is the raw count for context.
        if len(gains) < min_signals:
            continue

        def _mark_gain(row, h):
            ref = row.get("ref_price")
            mk = _json.loads(row.get("marks_json") or "{}").get(str(h))
            if not mk or not mk.get("price") or not ref or ref <= 0:
                return None
            return (mk["price"] / ref - 1.0) * 100.0
        g1 = [g for g in (_mark_gain(x, 60) for x in rows) if g is not None]
        g5 = [g for g in (_mark_gain(x, 300) for x in rows) if g is not None]
        out.append({
            "source_user_id": uid, "handle": rows[0].get("handle"),
            "n_signals": len(gains), "n_matured": len(rows),
            "median_max_gain_pct": _median(gains),
            "mean_max_gain_pct": (round(sum(gains) / len(gains), 4) if gains else None),
            "p90_max_gain_pct": _pctile(gains, 0.90),
            "pump_hit_rate": (round(sum(1 for g in gains if g >= hit_pct) / len(gains), 4)
                              if gains else None),
            "median_peak_horizon_s": _median([x["peak_horizon_s"] for x in rows
                                              if x["peak_horizon_s"] is not None]),
            "median_1m_gain_pct": _median(g1),
            "median_5m_gain_pct": _median(g5),
        })
    out.sort(key=lambda x: (x["median_max_gain_pct"] is not None, x["median_max_gain_pct"] or 0),
             reverse=True)
    return out


def buy_event_times(conn, uid, mint, network_id=None):
    """createdAt of every buy event for (uid, network, mint) — for cross-source dedup (don't copy a
    per-follow-activity buy that the global feed already captured near the same time)."""
    return [r[0] for r in conn.execute(
        "SELECT created_at FROM memecoin_events WHERE source_user_id=? AND mint=? "
        "AND " + COIN_NETWORK_SQL + "=? AND side='buy'",
        (uid, mint, normalized_coin_network(network_id, mint)))]


def sell_event_times(conn, uid, mint, network_id=None):
    """createdAt of every SELL event for (uid, network, mint) — for the missed-sell coverage gauge (did
    the global feed deliver the trader's sell so a mirror exit could fire?)."""
    return [r[0] for r in conn.execute(
        "SELECT created_at FROM memecoin_events WHERE source_user_id=? AND mint=? "
        "AND " + COIN_NETWORK_SQL + "=? AND side='sell'",
        (uid, mint, normalized_coin_network(network_id, mint)))]


def mark_sell_classified(conn, swap_id, source_user_id, mint, seen, at):
    """Record — exactly ONCE per trader-sell (by swap id) — whether the global feed delivered it
    (seen) so a mirror exit could fire. Returns True iff this is the first classification, so the
    caller counts each sell once (fomo's newest-<=100 swaps window re-delivers the same sells)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_seen_sells(swap_id, source_user_id, mint, seen, classified_at) "
        "VALUES (?,?,?,?,?)", (str(swap_id), source_user_id, mint, 1 if seen else 0, at))
    conn.commit()
    return cur.rowcount == 1


def sell_coverage_summary(conn):
    """Cumulative sell-coverage: total large sells classified, how many the feed delivered (seen)
    vs missed (each sell counted once). A high missed rate means the b_timecap decay cluster may
    partly be 'trader sold, we never saw it' rather than real fade."""
    row = conn.execute("SELECT COUNT(*), COALESCE(SUM(seen), 0) FROM memecoin_seen_sells").fetchone()
    total, seen = row[0], row[1]
    return {"total": total, "seen": seen, "missed": total - seen}


# --- S1 real-money risk envelope: pending-reservation ledger ---

LIFI_TERMINAL_STATES = frozenset({"canary_complete", "entry_failed", "position_closed"})
_LIFI_ATOMIC_FIELDS = (
    "amount_atomic", "input_atomic", "output_min_atomic", "fee_atomic",
    "allowance_atomic", "settlement_in_atomic", "settlement_out_atomic",
)


def _canonical_atomic_text(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("LI.FI atomic amount must be a nonnegative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("LI.FI atomic amount must be a nonnegative integer") from exc
    if number < 0 or (isinstance(value, str) and value != str(number)):
        raise ValueError("LI.FI atomic amount must use canonical decimal text")
    return str(number)


def _lifi_begin(conn, commit):
    if not commit:
        if not conn.in_transaction:
            raise RuntimeError("LI.FI caller transaction is absent")
        return False
    if conn.in_transaction:
        raise RuntimeError("LI.FI write refused inside a caller transaction")
    conn.execute("BEGIN IMMEDIATE")
    return True


def start_lifi_lifecycle(
        conn, pending_id, *, chain_id, custody_wallet, token, release_sha, manifest_hash,
        created_at, detail=None, commit=True):
    """Create the immutable sequence-zero event for one reservation."""
    owned = _lifi_begin(conn, commit)
    lifecycle_id = uuid.uuid4().hex
    detail_json = (_json.dumps(detail, sort_keys=True, separators=(",", ":"), allow_nan=False)
                   if detail is not None else None)
    try:
        pending = conn.execute(
            "SELECT 1 FROM memecoin_pending_entries WHERE id=? AND status='reserved'",
            (pending_id,),
        ).fetchone()
        if pending is None:
            if owned:
                conn.rollback()
            return None
        conn.execute(
            "INSERT INTO memecoin_lifi_authority_events("
            "lifecycle_id,sequence,pending_id,expected_prior_state,state,event_type,stage,"
            "chain_id,custody_wallet,token,release_sha,manifest_hash,detail_json,created_at) "
            "VALUES (?,0,?,NULL,'reserved','lifecycle_started','entry',?,?,?,?,?,?,?)",
            (lifecycle_id, pending_id, str(chain_id), str(custody_wallet).lower(),
             str(token).lower(), str(release_sha).lower(), str(manifest_hash).lower(),
             detail_json, created_at),
        )
        if owned:
            conn.commit()
        return lifecycle_id
    except sqlite3.IntegrityError as exc:
        if owned:
            conn.rollback()
        if "memecoin_lifi_authority_events.pending_id" in str(exc):
            return None
        raise
    except Exception:
        if owned:
            conn.rollback()
        raise


def append_lifi_event(
        conn, lifecycle_id, *, expected_state, new_state, event_type, created_at,
        position_id=None, stage=None, token=None, spender=None, amount_atomic=None,
        input_atomic=None, output_min_atomic=None, fee_atomic=None, allowance_atomic=None,
        settlement_in_atomic=None, settlement_out_atomic=None, tx_hash=None, tx_identity=None,
        nonce=None, signed_payload_sha256=None, block_number=None, block_hash=None,
        proof_sha256=None, request_sha256=None, response_sha256=None, calldata_sha256=None,
        decoded_semantics_sha256=None, tool=None, provider_failure_type=None,
        quota_limit=None, quota_remaining=None, quota_reset=None, detail=None, commit=True):
    """Append one compare-and-set lifecycle event."""
    atomic_values = {
        name: _canonical_atomic_text(value)
        for name, value in {
            "amount_atomic": amount_atomic,
            "input_atomic": input_atomic,
            "output_min_atomic": output_min_atomic,
            "fee_atomic": fee_atomic,
            "allowance_atomic": allowance_atomic,
            "settlement_in_atomic": settlement_in_atomic,
            "settlement_out_atomic": settlement_out_atomic,
        }.items()
    }
    owned = _lifi_begin(conn, commit)
    try:
        previous = conn.execute(
            "SELECT * FROM memecoin_lifi_authority_events WHERE lifecycle_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (lifecycle_id,),
        ).fetchone()
        if previous is None or previous["state"] != expected_state:
            if owned:
                conn.rollback()
            return False
        prior_detail = _json.loads(previous["detail_json"] or "{}")
        merged_detail = dict(detail or {})
        if "balance_baseline" in prior_detail and "balance_baseline" not in merged_detail:
            merged_detail["balance_baseline"] = prior_detail["balance_baseline"]
        detail_json = (_json.dumps(
            merged_detail, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ) if merged_detail else None)
        values = {
            "lifecycle_id": lifecycle_id,
            "sequence": int(previous["sequence"]) + 1,
            "pending_id": previous["pending_id"],
            "position_id": position_id,
            "expected_prior_state": expected_state,
            "state": new_state,
            "event_type": event_type,
            "stage": stage,
            "chain_id": previous["chain_id"],
            "custody_wallet": previous["custody_wallet"],
            "token": str(token or previous["token"] or "").lower() or None,
            "spender": str(spender or previous["spender"] or "").lower() or None,
            **atomic_values,
            "tx_hash": tx_hash or previous["tx_hash"],
            "tx_identity": tx_identity or previous["tx_identity"],
            "nonce": (previous["nonce"] if nonce is None else str(nonce)),
            "signed_payload_sha256": (
                signed_payload_sha256 or previous["signed_payload_sha256"]),
            "block_number": (
                previous["block_number"] if block_number is None else str(block_number)),
            "block_hash": block_hash or previous["block_hash"],
            "proof_sha256": proof_sha256,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "calldata_sha256": calldata_sha256,
            "decoded_semantics_sha256": decoded_semantics_sha256,
            "release_sha": previous["release_sha"],
            "manifest_hash": previous["manifest_hash"],
            "tool": tool,
            "provider_failure_type": provider_failure_type,
            "quota_limit": None if quota_limit is None else str(quota_limit),
            "quota_remaining": None if quota_remaining is None else str(quota_remaining),
            "quota_reset": None if quota_reset is None else str(quota_reset),
            "detail_json": detail_json,
            "created_at": created_at,
        }
        columns = tuple(values)
        conn.execute(
            f"INSERT INTO memecoin_lifi_authority_events({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(values[name] for name in columns),
        )
        if owned:
            conn.commit()
        return True
    except Exception:
        if owned:
            conn.rollback()
        raise


def latest_lifi_lifecycle(conn, lifecycle_id):
    row = conn.execute(
        "SELECT * FROM memecoin_lifi_authority_events WHERE lifecycle_id=? "
        "ORDER BY sequence DESC LIMIT 1",
        (lifecycle_id,),
    ).fetchone()
    if row is None:
        return None
    value = dict(row)
    link = conn.execute(
        "SELECT position_id FROM memecoin_lifi_authority_events "
        "WHERE lifecycle_id=? AND position_id IS NOT NULL LIMIT 1",
        (lifecycle_id,),
    ).fetchone()
    value["position_id"] = link[0] if link else None
    return value


def activate_lifi_release(
        conn, *, release_sha, manifest_hash, chain_id, custody_wallet,
        canary_lifecycle_id, expected_allowances, quota_limit, quota_remaining,
        quota_reset, activated_at):
    """Store one immutable activation after a compatible terminal canary."""
    if conn.in_transaction:
        raise RuntimeError("LI.FI activation needs a clean transaction")
    latest = latest_lifi_lifecycle(conn, canary_lifecycle_id)
    custody = str(custody_wallet).lower()
    expected = {
        (str(token).lower(), str(spender).lower(), "0")
        for token, spender in expected_allowances
    }
    try:
        detail = _json.loads(latest["detail_json"] or "{}")
        observed_list = detail["allowances"]
        observed = {
            (str(item["token"]).lower(), str(item["spender"]).lower(),
             _canonical_atomic_text(item["amount_atomic"]))
            for item in observed_list
        }
        limit = int(quota_limit)
        remaining = int(quota_remaining)
        reset = int(quota_reset)
    except (KeyError, TypeError, ValueError):
        return None
    if not expected or len(observed_list) != len(expected) or observed != expected:
        return None
    if latest["state"] != "canary_complete" \
            or latest["manifest_hash"] != str(manifest_hash).lower() \
            or latest["chain_id"] != str(chain_id) \
            or latest["custody_wallet"] != custody \
            or str(detail.get("chain_id")) != str(chain_id) \
            or str(detail.get("custody_wallet") or "").lower() != custody:
        return None
    if limit <= 0 or remaining < 0 or remaining > limit or reset < 0:
        return None
    proof = _json.dumps(
        sorted(observed), sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    proof_sha = hashlib.sha256(proof.encode()).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO memecoin_lifi_activations("
            "release_sha,manifest_hash,chain_id,custody_wallet,canary_lifecycle_id,"
            "allowance_proof_sha256,quota_limit,quota_remaining,quota_reset,activated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(release_sha).lower(), str(manifest_hash).lower(), str(chain_id), custody,
             canary_lifecycle_id, proof_sha, str(limit), str(remaining), str(reset), activated_at),
        )
        row = active_lifi_release(
            conn, release_sha=release_sha, manifest_hash=manifest_hash,
            chain_id=chain_id, custody_wallet=custody,
        )
        if row is None or row["canary_lifecycle_id"] != canary_lifecycle_id:
            conn.rollback()
            return None
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise


def complete_lifi_canary(
        conn, *, lifecycle_id, allowances, quota_limit, quota_remaining,
        quota_reset, created_at):
    """Record one terminal canary only after each observed allowance is zero."""
    try:
        values = [
            {
                "token": str(token).lower(),
                "spender": str(spender).lower(),
                "amount_atomic": _canonical_atomic_text(amount),
            }
            for token, spender, amount in allowances
        ]
        limit = int(quota_limit)
        remaining = int(quota_remaining)
        reset = int(quota_reset)
    except (TypeError, ValueError):
        return False
    if not values or any(item["amount_atomic"] != "0" for item in values):
        return False
    if limit <= 0 or remaining < 0 or remaining > limit or reset < 0:
        return False
    events = conn.execute(
        "SELECT event_type FROM memecoin_lifi_authority_events "
        "WHERE lifecycle_id=? ORDER BY sequence",
        (lifecycle_id,),
    ).fetchall()
    if "exit_settled" not in {row["event_type"] for row in events}:
        return False
    return append_lifi_event(
        conn, lifecycle_id, expected_state="position_closed",
        new_state="canary_complete", event_type="canary_complete", stage="canary",
        quota_limit=limit, quota_remaining=remaining, quota_reset=reset,
        detail={"chain_id": "4663", "custody_wallet": (
            latest_lifi_lifecycle(conn, lifecycle_id) or {}).get("custody_wallet"),
            "allowances": values},
        created_at=created_at,
    )


def active_lifi_release(conn, *, release_sha, manifest_hash, chain_id, custody_wallet):
    row = conn.execute(
        "SELECT * FROM memecoin_lifi_activations WHERE release_sha=? AND manifest_hash=? "
        "AND chain_id=? AND custody_wallet=?",
        (str(release_sha).lower(), str(manifest_hash).lower(), str(chain_id),
         str(custody_wallet).lower()),
    ).fetchone()
    return dict(row) if row is not None else None


def unresolved_lifi_lifecycles(conn, chain_id, custody_wallet):
    rows = conn.execute(
        "SELECT e.* FROM memecoin_lifi_authority_events e "
        "JOIN (SELECT lifecycle_id,MAX(sequence) AS sequence "
        "      FROM memecoin_lifi_authority_events GROUP BY lifecycle_id) latest "
        "ON latest.lifecycle_id=e.lifecycle_id AND latest.sequence=e.sequence "
        "WHERE e.chain_id=? AND e.custody_wallet=? ORDER BY e.id",
        (str(chain_id), str(custody_wallet).lower()),
    ).fetchall()
    return [
        latest_lifi_lifecycle(conn, row["lifecycle_id"])
        for row in rows
        if row["state"] not in LIFI_TERMINAL_STATES
    ]


def lifi_slots_used(conn, chain_id, custody_wallet):
    keys = {
        row["lifecycle_id"]
        for row in unresolved_lifi_lifecycles(conn, chain_id, custody_wallet)
    }
    for position in conn.execute(
            "SELECT id FROM memecoin_positions WHERE execution_mode='real' "
            "AND network_id=? AND is_open=1 AND lifi_dependent=1",
            (str(chain_id),)):
        link = conn.execute(
            "SELECT lifecycle_id FROM memecoin_lifi_authority_events "
            "WHERE position_id=? LIMIT 1",
            (position["id"],),
        ).fetchone()
        keys.add(link[0] if link else f"position:{position['id']}")
    return len(keys)


def finalize_lifi_entry(
        conn, *, lifecycle_id, pending_id, position_kwargs, fill_kwargs, created_at):
    """Commit one confirmed entry and its lifecycle link."""
    if conn.in_transaction:
        raise RuntimeError("LI.FI entry finalization needs a clean transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        latest = latest_lifi_lifecycle(conn, lifecycle_id)
        if latest is None or latest["state"] != "entry_settled" \
                or int(latest["pending_id"]) != int(pending_id):
            conn.rollback()
            return None
        position_id = open_position(
            conn, **dict(position_kwargs), lifi_dependent=True, commit=False,
        )
        insert_fill(conn, position_id=position_id, **dict(fill_kwargs), commit=False)
        if not confirm_pending(conn, pending_id, commit=False):
            raise RuntimeError("LI.FI pending reservation did not confirm")
        if not append_lifi_event(
                conn,
                lifecycle_id,
                expected_state="entry_settled",
                new_state="position_open",
                event_type="position_linked",
                stage="entry",
                position_id=position_id,
                created_at=created_at,
                commit=False):
            raise RuntimeError("LI.FI lifecycle link failed")
        conn.commit()
        return position_id
    except Exception:
        conn.rollback()
        raise


def finalize_lifi_close(
        conn, *, lifecycle_id, expected_state, exit_kwargs, created_at,
        final_state="position_closed"):
    """Commit one terminal close and release its lifecycle slot."""
    if conn.in_transaction:
        raise RuntimeError("LI.FI close finalization needs a clean transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        latest = latest_lifi_lifecycle(conn, lifecycle_id)
        if latest is None or latest["state"] != expected_state:
            conn.rollback()
            return False
        if not book_full_exit_and_finalize(conn, **dict(exit_kwargs), commit=False):
            conn.rollback()
            return False
        if not append_lifi_event(
                conn,
                lifecycle_id,
                expected_state=expected_state,
                new_state=final_state,
                event_type=final_state,
                stage="exit",
                created_at=created_at,
                commit=False):
            raise RuntimeError("LI.FI terminal lifecycle event failed")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


ONCHAIN_SELLS_DDL = """
    CREATE TABLE IF NOT EXISTS onchain_wallet_sells (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signature TEXT UNIQUE NOT NULL,
        source_user_id TEXT NOT NULL,
        handle TEXT,
        address TEXT NOT NULL,
        network_id TEXT NOT NULL DEFAULT 'solana',
        mint TEXT NOT NULL,
        sold_atomic INTEGER NOT NULL,
        pre_atomic INTEGER,
        post_atomic INTEGER,
        sold_share REAL,
        slot INTEGER,
        block_time INTEGER,
        detected_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )"""


# ONE ROW PER SALE, NOT PER TRANSACTION (review S27-03, 2026-09-27). `onchain_wallet_sells` made
# the signature alone unique, so a transaction that sold two coins kept only the first. SQLite
# cannot drop that constraint without a rebuild, so the sale identity lives in this sibling table
# and the old one is left as it is, never erased. Expand/contract: the watcher writes BOTH while a
# release that reads the old table may still run; the mirror reads this one.
ONCHAIN_SELLS_V2_DDL = """
    CREATE TABLE IF NOT EXISTS onchain_wallet_sells_v2 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signature TEXT NOT NULL,
        source_user_id TEXT NOT NULL,
        handle TEXT,
        address TEXT NOT NULL,
        network_id TEXT NOT NULL DEFAULT 'solana',
        mint TEXT NOT NULL,
        sold_atomic INTEGER NOT NULL,
        pre_atomic INTEGER,
        post_atomic INTEGER,
        sold_share REAL,
        slot INTEGER,
        block_time INTEGER,
        detected_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(network_id, signature, address, mint)
    )"""


def ensure_onchain_sells_table(conn):
    """The confirmed watcher's source SELLS (source-sell mirror, 2026-09-27). One DDL for the
    runner and the watcher process, which writes it without the rest of the memecoin schema."""
    conn.execute(ONCHAIN_SELLS_DDL)
    conn.execute(ONCHAIN_SELLS_V2_DDL)


def record_onchain_sell(conn, *, signature, source_user_id, handle, address, mint, sold_atomic,
                        pre_atomic, post_atomic, slot, block_time, detected_at, created_at,
                        network_id="solana"):
    """One source sell with its exact share from the transaction's own balances. True when stored."""
    share = (int(sold_atomic) / int(pre_atomic)) if pre_atomic else None
    row = (signature, source_user_id, handle, address, network_id, mint, int(sold_atomic),
           None if pre_atomic is None else int(pre_atomic),
           None if post_atomic is None else int(post_atomic), share, slot, block_time,
           detected_at, created_at)
    columns = ("(signature, source_user_id, handle, address, network_id, mint, sold_atomic, "
               "pre_atomic, post_atomic, sold_share, slot, block_time, detected_at, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    conn.execute("INSERT OR IGNORE INTO onchain_wallet_sells " + columns, row)   # old readers
    cur = conn.execute("INSERT OR IGNORE INTO onchain_wallet_sells_v2 " + columns, row)
    conn.commit()
    return cur.rowcount == 1


# EVM SELL PATH, SHADOW (2026-09-29, Alpha GO). The watcher's detected EVM source sales with the
# proof that each IS a sale (a_erc20 / a_native: proceeds to the wallet in the same tx; b_relay: a
# Relay-envelope swap). Its OWN table on purpose: the Solana source-sell mirror reads
# onchain_wallet_sells_v2, and a shadow row must never reach a mirror. Atomic amounts are TEXT: an
# 18-decimal balance overflows SQLite's 64-bit INTEGER.
ONCHAIN_EVM_SELL_SHADOW_DDL = """
    CREATE TABLE IF NOT EXISTS onchain_evm_sell_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        network_id TEXT NOT NULL,
        tx_hash TEXT NOT NULL,
        address TEXT NOT NULL,
        source_user_id TEXT,
        handle TEXT,
        mint TEXT NOT NULL,
        sold_atomic TEXT NOT NULL,
        pre_atomic TEXT,
        post_atomic TEXT,
        proof TEXT NOT NULL,
        proceeds_token TEXT,
        proceeds_atomic TEXT,
        block_number INTEGER,
        detected_at TEXT NOT NULL,
        decoded_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(network_id, tx_hash, address, mint)
    )"""


def ensure_onchain_evm_sell_shadow_table(conn):
    conn.execute(ONCHAIN_EVM_SELL_SHADOW_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_onchain_evm_sell_shadow_detected "
                 "ON onchain_evm_sell_shadow(network_id, detected_at)")


def memecoin_events_after(conn, last_id, *, limit=500):
    """[(id, fomo_event_id, received_at, side, network_id, delivery_lane)] with id > `last_id`, oldest
    first, at most `limit` (the EVM watcher's J7 cross-check, 2026-09-29). `last_id` None returns one
    cursor row `(max id, None, None, None, None, None)`: the check starts at the newest event, never
    replays history. Read-only; the id range is indexed."""
    if last_id is None:
        row = conn.execute("SELECT max(id) FROM memecoin_events").fetchone()
        return [((row[0] if row and row[0] is not None else 0), None, None, None, None, None)]
    return [tuple(r) for r in conn.execute(
        "SELECT id, fomo_event_id, received_at, side, network_id, delivery_lane FROM memecoin_events "
        "WHERE id > ? ORDER BY id LIMIT ?", (int(last_id), int(limit))).fetchall()]


def record_onchain_evm_sell_shadow(conn, *, network_id, tx_hash, address, source_user_id, handle,
                                   mint, sold_atomic, pre_atomic, post_atomic, proof,
                                   proceeds_token, proceeds_atomic, block_number, detected_at,
                                   decoded_at, created_at):
    """One shadow EVM sale. True when stored; False when this (chain, tx, wallet, coin) was known."""
    text = lambda v: None if v is None else str(int(v))      # noqa: E731 -- exact, never float
    cur = conn.execute(
        "INSERT OR IGNORE INTO onchain_evm_sell_shadow (network_id, tx_hash, address, "
        "source_user_id, handle, mint, sold_atomic, pre_atomic, post_atomic, proof, proceeds_token, "
        "proceeds_atomic, block_number, detected_at, decoded_at, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(network_id), str(tx_hash).lower(), str(address).lower(), source_user_id, handle,
         str(mint).lower(), text(sold_atomic), text(pre_atomic), text(post_atomic), proof,
         proceeds_token, text(proceeds_atomic), block_number, detected_at, decoded_at, created_at))
    conn.commit()
    return cur.rowcount == 1


def onchain_sells_max_id(conn):
    return int(conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM onchain_wallet_sells_v2").fetchone()[0])


def evm_recovery_chains(conn, networks):
    """The subset of `networks` (EVM chain ids) with open real inventory or an unresolved
    reservation. Each needs an exit-and-recovery executor even while its entries are off (Gamma H3,
    2026-09-30): without one the exit sweep skips the position and recovery leaves the reservation
    reserved. Same table rules as `ink_recovery_required`: a missing table holds nothing, a failed
    read raises."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('memecoin_positions','memecoin_pending_entries')")}
    out = set()
    for network in networks:
        network = str(network)
        if ("memecoin_positions" in tables
                and real_open_snapshot(conn, network=network)["count"] > 0):
            out.add(network)
        elif ("memecoin_pending_entries" in tables
              and pending_totals(conn, network=network)[0] > 0):
            out.add(network)
    return out


def ink_recovery_required(conn):
    """Open Ink exposure requires an executor even when new entries are disabled.

    A table that does not exist holds no Ink state (a web-only or fresh DB): it counts as zero and
    the other table is still read. A read that FAILS raises and is never False, so a broken read
    cannot hide open Ink inventory. (2026-09-29: this raised when only one table existed, and every
    chain's context build aborted with it.)"""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('memecoin_positions','memecoin_pending_entries')")}
    if "memecoin_positions" in tables and real_open_snapshot(conn, network="57073")["count"] > 0:
        return True
    return ("memecoin_pending_entries" in tables
            and pending_totals(conn, network="57073")[0] > 0)


def onchain_sells_after(conn, cursor, *, limit=50):
    return [dict(r) for r in conn.execute(
        "SELECT id, signature, source_user_id, handle, address, mint, sold_atomic, pre_atomic, post_atomic, "
        "slot, block_time, detected_at FROM onchain_wallet_sells_v2 WHERE id > ? ORDER BY id "
        "LIMIT ?", (int(cursor), int(limit)))]


def record_onchain_detection(conn, *, signature, source_user_id, handle, address, mint, detected_at,
                             created_at, network_id="solana", token_delta=None, sol_spent=None,
                             source_size_usd=None, source_price=None, source_valuation_json=None,
                             block_time=None, token_amount_atomic=None, quote_amount_atomic=None,
                             source_observation_key=None, source_observation_state=None,
                             source_slot=None, commit=True):
    """Write one detected source buy for the on-chain lane. True when this call stored it; False
    when the signature already has a row (the watcher's or an earlier handoff's), which is kept.

    A table that predates `source_slot` (a process on new code before any ensure ran) still records
    the buy, without the slot: the detection is the signal itself (Gamma M2, 2026-10-02)."""
    columns = ("signature, source_user_id, handle, address, network_id, mint, token_delta, sol_spent, "
               "source_size_usd, source_price, detected_at, created_at, source_valuation_json, "
               "block_time, token_amount_atomic, quote_amount_atomic, source_observation_key, "
               "source_observation_state")
    values = (signature, source_user_id, handle, address, network_id, mint, token_delta, sol_spent,
              source_size_usd, source_price, detected_at, created_at, source_valuation_json, block_time,
              str(token_amount_atomic) if token_amount_atomic is not None else None,
              str(quote_amount_atomic) if quote_amount_atomic is not None else None,
              source_observation_key, source_observation_state)
    sql = "INSERT OR IGNORE INTO onchain_wallet_detections (%s) VALUES (%s)"
    try:
        cur = conn.execute(sql % (columns + ", source_slot", ",".join("?" * (len(values) + 1))),
                           values + (source_slot,))
    except sqlite3.OperationalError as exc:
        if "source_slot" not in str(exc):
            raise
        logger.warning("onchain detection %s: no source_slot column yet; stored without it", signature)
        cur = conn.execute(sql % (columns, ",".join("?" * len(values))), values)
    if commit:
        conn.commit()
    return cur.rowcount == 1


# ONE COIN, ONE LANE (spec desk-execution-plumbing §2.5 R3, operator 2026-10-21: refuse both ways
# until he rules otherwise). The partial UNIQUE on reserved rows already stops two reservations of
# one coin from any origin, including an unresolved one. What it cannot see is an OPEN position:
# the open index is per wallet and strategy, and a desk row has no wallet (NULL is distinct). So a
# desk reservation refuses on ANY open real position in the coin, and every other lane refuses on
# an open DESK position. Copy-on-copy stays exactly as it was. Same statement as the INSERT, so
# the check and the write cannot be split by another writer. Bound params: normalized network,
# mint twice (exact for Solana, case-blind for 0x), then 1 when the reserver is the desk.
_CROSS_LANE_HOLD_SQL = (
    "SELECT 1 FROM memecoin_positions p WHERE p.execution_mode='real' AND p.is_open=1 "
    f"AND {coin_network_sql('p.')}=? "
    "AND (p.mint=? OR (substr(lower(p.mint),1,2)='0x' AND lower(p.mint)=lower(?))) "
    f"AND (?=1 OR COALESCE(p.signal_source,'')='{SIGNAL_SOURCE_DESK}')"
)
# THE DESK CAP, INSIDE THE SAME INSERT (Codex audit #2): a check before the reservation lets two
# writers both pass it. Bound params: 1 when the reserver is the desk, then the cap. Open desk
# positions and reserved desk entries both count; copy lanes never do.
_DESK_CAP_SQL = (
    "(?=0 OR (SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
    f"AND signal_source='{SIGNAL_SOURCE_DESK}') + (SELECT COUNT(*) FROM memecoin_pending_entries "
    f"WHERE status='reserved' AND signal_source='{SIGNAL_SOURCE_DESK}') < ?)"
)
# THE DESK DAILY HALT, INSIDE THE SAME INSERT (Codex review of the latch fix): the pre-check in
# desk_book_refusal runs before quote and balance work, so another connection can trip the day in
# between. Bound params: 1 when the reserver is the desk, the UTC day, that day's 00:00, the limit.
# A latched day refuses; so does realized desk PnL at or below -limit; limit <= 0 admits nothing.
_DESK_HALT_SQL = (
    "(?=0 OR (NOT EXISTS (SELECT 1 FROM memecoin_desk_loss_trips WHERE day=?) "
    "AND (SELECT COALESCE(SUM(realized_pnl), 0.0) FROM memecoin_positions WHERE execution_mode='real' "
    f"AND is_open=0 AND signal_source='{SIGNAL_SOURCE_DESK}' AND realized_pnl IS NOT NULL "
    "AND closed_at>=?) > -? AND ? > 0))"
)


# A signed attempt may already have paid fees even if it never booked a position. A confirmed
# entry's own position expense replaces this provisional unknown; failed signatures keep a charge.
_DESK_PENDING_COST_GAP_SQL = (
    "p.signal_source='desk' AND p.desk_cohort_id={cohort} AND p.tx_sig IS NOT NULL "
    "AND NOT (p.status='confirmed' AND EXISTS (SELECT 1 FROM memecoin_positions booked "
    "WHERE booked.desk_cohort_id=p.desk_cohort_id AND booked.desk_opportunity_id=p.desk_opportunity_id "
    "AND booked.execution_mode='real' AND booked.signal_source='desk')) "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_desk_cohort_costs fee WHERE fee.cohort_id=p.desk_cohort_id "
    "AND fee.event_id='pending:' || p.id || ':' || p.tx_sig {priced})"
)
_DESK_POSITION_COST_GAP_SQL = (
    "p.desk_cohort_id={cohort} AND p.signal_source='desk' AND p.execution_mode='real' "
    "AND p.{column} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM memecoin_desk_cohort_costs fee "
    "WHERE fee.cohort_id=p.desk_cohort_id AND fee.event_id='{kind}:' || p.id || ':' || p.{column} {priced})"
)
from sniper.services.memecoin_known_group_canary import POLICY_HASH as _KNOWN_GROUP_POLICY_HASH, _POLICY_BODY as _KNOWN_GROUP_POLICY_BODY
from sniper.settings.known_group import KNOWN_GROUP_LAUNCH_V1 as _KNOWN_GROUP_POLICY,KNOWN_GROUP_SERVICE_CATALOGUE

_KNOWN_GROUP_SERVICE_JSON = _json.dumps(dict(KNOWN_GROUP_SERVICE_CATALOGUE),sort_keys=True,separators=(',',':'))

_KNOWN_GROUP_POLICY_SQL = ("(c.cohort_id='known-group-launch-v1' AND c.producer='H' "
    "AND c.stop_mode='manual' AND c.kill_limit_usd=0 AND c.admission_enabled=1 "
    "AND c.policy_hash='" + _KNOWN_GROUP_POLICY_HASH + "' AND c.policy_json='" + _KNOWN_GROUP_POLICY_BODY + "')")
_DESK_STOP_SQL = ("((c.stop_mode='loss_limit' AND c.kill_limit_usd>0) OR " + _KNOWN_GROUP_POLICY_SQL + ")")

_DESK_COHORT_SQL_LEGACY = (
    "(?=0 OR EXISTS (SELECT 1 FROM memecoin_desk_cohorts c WHERE c.cohort_id=? AND c.producer=? "
    "AND " + _DESK_STOP_SQL + " "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_desk_cohort_trips t WHERE t.cohort_id=c.cohort_id) "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_positions p WHERE p.desk_cohort_id=c.cohort_id "
    "AND p.execution_mode='real' AND p.signal_source='desk' AND p.is_open=0 AND p.realized_pnl IS NULL) "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_desk_cohort_costs x WHERE x.cohort_id=c.cohort_id AND x.usd IS NULL) "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_pending_entries p WHERE "
    + _DESK_PENDING_COST_GAP_SQL.format(cohort="c.cohort_id", priced="AND fee.usd IS NOT NULL") + ") "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_positions p WHERE "
    + _DESK_POSITION_COST_GAP_SQL.format(cohort="c.cohort_id", column="close_sig", kind="exit",
                                        priced="AND fee.usd IS NOT NULL") + ") "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_positions p WHERE "
    + _DESK_POSITION_COST_GAP_SQL.format(cohort="c.cohort_id", column="derisk_sig", kind="partial",
                                        priced="AND fee.usd IS NOT NULL") + ") "
    "AND (c.stop_mode='manual' OR (SELECT COALESCE(SUM(p.realized_pnl),0.0) FROM memecoin_positions p "
    "WHERE p.desk_cohort_id=c.cohort_id AND p.execution_mode='real' AND p.signal_source='desk' "
    "AND p.is_open=0) - (SELECT COALESCE(SUM(x.usd),0.0) FROM memecoin_desk_cohort_costs x "
    "WHERE x.cohort_id=c.cohort_id) > -c.kill_limit_usd)))"
)
# The reservation INSERT's copy of desk_cohort_gate (Gamma BLOCKER on 3f19c5853: the atomic WHERE must agree
# with the Python gate). Params: desk flag, cohort id, producer, bound (USD per unknown execution leg). Fail closed:
# an unpriced close, or a NULL cost that is neither an execution leg nor a flat-subscription/refused model row.
# Bounded: realized - priced costs - bound x unknown execution legs > -kill_limit.
# EXACT forms only (Codex 2026-10-22): the no-call fact, or the validated flat proof = prefix + 64 lowercase hex
_DESK_FLAT_PREFIX = "model:claude_sdk:subscription_flat:"
_DESK_FLAT_MODEL_SQL = ("(x.kind='model' AND (x.provenance='desk:no_model_call' OR (length(x.provenance)=%d "
                        "AND substr(x.provenance,1,%d)='%s' AND substr(x.provenance,%d) GLOB '%s')))"
                        % (len(_DESK_FLAT_PREFIX) + 64, len(_DESK_FLAT_PREFIX), _DESK_FLAT_PREFIX,
                           len(_DESK_FLAT_PREFIX) + 1, "[0-9a-f]" * 64))
# An unpriced ENTRY leg is bounded only through its OWN signed pending row's recorded fee footprint
# (entry_fee_ceiling_lamports, derived from the exact bound bytes at the write-ahead), never by a timestamp
# (Codex P1 on ffbaa9e43: a recovered entry's opened_at is the receipt time). The SQL twins below must match.
_DESK_ENTRY_PROVEN_PENDING_SQL = "(p.entry_fee_ceiling_lamports IS NOT NULL AND p.entry_fee_ceiling_lamports <= ?)"
# a NULL fee row is a provably capped entry leg when: pending:<id>:<sig> -> that pending row carries a footprint
# <= L; entry:<pid> -> its position's attempt has >= 1 signed pending row and EVERY signed row of it carries one.
_DESK_ENTRY_ROW_SQL = (
    "(x.kind='execution' AND ("
    "(x.event_id GLOB 'pending:*' AND EXISTS (SELECT 1 FROM memecoin_pending_entries p WHERE "
    "x.event_id = 'pending:' || p.id || ':' || p.tx_sig AND p.entry_fee_ceiling_lamports IS NOT NULL "
    "AND p.entry_fee_ceiling_lamports <= ?)) OR "
    "(x.event_id GLOB 'entry:*' AND EXISTS (SELECT 1 FROM memecoin_positions q JOIN memecoin_pending_entries p "
    "ON p.attempt_id = q.attempt_id WHERE q.id = x.position_id AND q.attempt_id IS NOT NULL AND p.tx_sig IS NOT NULL) "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_positions q JOIN memecoin_pending_entries p "
    "ON p.attempt_id = q.attempt_id WHERE q.id = x.position_id AND p.tx_sig IS NOT NULL "
    "AND (p.entry_fee_ceiling_lamports IS NULL OR p.entry_fee_ceiling_lamports > ?)))))")


# The reservation INSERT's copy of desk_cohort_gate. Params: desk flag, cohort id, producer, L, L (NULL rows),
# L (pending gaps), bound, L, L (bounded rows), L (bounded pending gaps); L = the bound's priority+tip lamports.
# Holds on: an unpriced close; any NULL cost that is not a proven entry leg or an exact zero-proof model row; any
# exit/partial signature without a fee row; a signed pending entry without a proven footprint and no fee row.
# Bounded: realized - priced costs - bound x proven unknown entry legs > -kill_limit.
_DESK_COHORT_SQL = (
    "(?=0 OR EXISTS (SELECT 1 FROM memecoin_desk_cohorts c WHERE c.cohort_id=? AND c.producer=? "
    "AND " + _DESK_STOP_SQL + " "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_desk_cohort_trips t WHERE t.cohort_id=c.cohort_id) "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_positions p WHERE p.desk_cohort_id=c.cohort_id "
    "AND p.execution_mode='real' AND p.signal_source='desk' AND p.is_open=0 AND p.realized_pnl IS NULL) "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_desk_cohort_costs x WHERE x.cohort_id=c.cohort_id AND x.usd IS NULL "
    "AND NOT " + _DESK_FLAT_MODEL_SQL + " AND NOT " + _DESK_ENTRY_ROW_SQL + ") "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_positions p WHERE "
    + _DESK_POSITION_COST_GAP_SQL.format(cohort="c.cohort_id", column="close_sig", kind="exit", priced="") + ") "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_positions p WHERE "
    + _DESK_POSITION_COST_GAP_SQL.format(cohort="c.cohort_id", column="derisk_sig", kind="partial", priced="") + ") "
    "AND NOT EXISTS (SELECT 1 FROM memecoin_pending_entries p WHERE "
    + _DESK_PENDING_COST_GAP_SQL.format(cohort="c.cohort_id", priced="") + " AND NOT "
    + _DESK_ENTRY_PROVEN_PENDING_SQL + ") "
    "AND (c.stop_mode='manual' OR (SELECT COALESCE(SUM(p.realized_pnl),0.0) FROM memecoin_positions p "
    "WHERE p.desk_cohort_id=c.cohort_id AND p.execution_mode='real' AND p.signal_source='desk' "
    "AND p.is_open=0) - (SELECT COALESCE(SUM(x.usd),0.0) FROM memecoin_desk_cohort_costs x "
    "WHERE x.cohort_id=c.cohort_id) - ? * ("
    "(SELECT COUNT(*) FROM memecoin_desk_cohort_costs x WHERE x.cohort_id=c.cohort_id AND x.usd IS NULL "
    "AND " + _DESK_ENTRY_ROW_SQL + ") "
    "+ (SELECT COUNT(*) FROM memecoin_pending_entries p WHERE "
    + _DESK_PENDING_COST_GAP_SQL.format(cohort="c.cohort_id", priced="") + " AND " + _DESK_ENTRY_PROVEN_PENDING_SQL
    + ")) > -c.kill_limit_usd)))"
)


def _desk_identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 200
            and re.fullmatch(r"[A-Za-z0-9_.:/-]+", value) is not None)


def _desk_aware_utc(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.utcoffset() != timedelta(0):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("desk timestamp must be aware UTC") from None
    return str(value)


def _ensure_known_group_schema(conn):
    from pathlib import Path
    migration = Path(__file__).resolve().parents[2] / 'migrations/20261101_known_group_canary.sql'
    columns = {r[1] for r in conn.execute('PRAGMA table_info(memecoin_desk_cohorts)')}
    sql = '\n'.join(line for line in migration.read_text().splitlines() if not line.lstrip().startswith('--'))
    for raw in sql.split(';'):
        statement = raw.strip()
        if not statement:
            continue
        match = re.match(r'ALTER TABLE memecoin_desk_cohorts ADD COLUMN (\w+) ', statement)
        if match and match[1] in columns:
            continue
        conn.execute(statement)


def _known_group_policy_refusal(conn, cohort_id='known-group-launch-v1'):
    from sniper.services.memecoin_known_group_canary import POLICY_HASH
    row = conn.execute('SELECT producer,prompt_version,policy_hash,policy_json,kill_limit_usd,stop_mode,'
                       'admission_enabled FROM memecoin_desk_cohorts WHERE cohort_id=?', (cohort_id,)).fetchone()
    if row is None or cohort_id != 'known-group-launch-v1':
        return 'known_group_unregistered'
    expected = (row[0] == 'H' and row[1] == 'known-group-launch-v1' and row[2] == POLICY_HASH
                and hashlib.sha256(row[3].encode()).hexdigest() == POLICY_HASH
                and row[4] == 0.0 and row[5] == 'manual')
    if not expected:
        return 'known_group_policy_mismatch'
    return None if row[6] == 1 else 'known_group_admission_disabled'


def set_known_group_admission(conn, *, enabled, policy_hash, now_iso):
    from sniper.services.memecoin_known_group_canary import POLICY_HASH
    _desk_aware_utc(now_iso)
    if policy_hash != POLICY_HASH:
        raise ValueError('known_group_policy_mismatch')
    refusal = _known_group_policy_refusal(conn)
    if refusal not in (None, 'known_group_admission_disabled'):
        raise ValueError(refusal)
    conn.execute('UPDATE memecoin_desk_cohorts SET admission_enabled=? WHERE cohort_id=? AND policy_hash=?',
                 (int(enabled), 'known-group-launch-v1', policy_hash))
    conn.commit()


def record_known_group_candidate(conn, *, request, proof_json, now_iso):
    meta = request['desk_metadata']
    proof = _json.loads(proof_json)
    creation = proof['creation']
    candidate = meta['opportunity_id']
    registered = conn.execute('SELECT created_at FROM memecoin_desk_cohorts WHERE cohort_id=?',
                              (meta['cohort_id'],)).fetchone()
    if registered is None or datetime.fromisoformat(registered[0]) > datetime.fromisoformat(creation['received_at_iso']):
        raise ValueError('known_group_registry_not_prior')
    values = (meta['cohort_id'], request['mint'], creation['signature'],
              proof_json, meta['snapshot_hash'], creation['received_at_iso'], meta['entry_deadline_utc'])
    conn.execute('INSERT OR IGNORE INTO memecoin_known_group_candidates(candidate_id,cohort_id,mint,'
                 'creation_signature,proof_json,proof_hash,received_at_utc,deadline_utc,known_at_utc,request_json) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?)', (candidate, *values, now_iso,
                    _json.dumps(request,sort_keys=True,separators=(',',':'),allow_nan=False)))
    old = conn.execute('SELECT cohort_id,mint,creation_signature,proof_json,proof_hash,received_at_utc,'
                       'deadline_utc FROM memecoin_known_group_candidates WHERE candidate_id=?', (candidate,)).fetchone()
    identical_source = (old is not None and tuple(old)[:3]==values[:3] and tuple(old)[5:]==values[5:]
        and _json.loads(old[3])['full_source_sha256']==proof['full_source_sha256']
        and hashlib.sha256(old[3].encode()).hexdigest()==old[4])
    if not identical_source:
        conn.rollback()
        raise ValueError('known_group_candidate_payload_mismatch')
    conn.execute("UPDATE memecoin_known_group_candidates SET eligible=0,invalidated_at=?,reason='known_prior_sell' "
        "WHERE candidate_id=? AND EXISTS(SELECT 1 FROM memecoin_known_group_sells s "
        "JOIN json_each(memecoin_known_group_candidates.proof_json,'$.members') member "
        "ON json_extract(member.value,'$.address')=s.owner WHERE s.mint=memecoin_known_group_candidates.mint)",
        (now_iso, candidate))
    conn.commit()
    return candidate


def known_group_candidate_refusal(conn, meta, *, now_iso):
    refusal = _known_group_policy_refusal(conn)
    if refusal:
        return refusal
    row = conn.execute('SELECT proof_json,proof_hash,eligible,deadline_utc,received_at_utc '
        'FROM memecoin_known_group_candidates WHERE candidate_id=? AND cohort_id=?',
        (meta['opportunity_id'], meta['cohort_id'])).fetchone()
    if (row is None or row[0] != _json.dumps(meta['known_group_proof'],sort_keys=True,separators=(',',':'),allow_nan=False)
            or row[1] != meta['snapshot_hash'] or row[3] != meta['entry_deadline_utc']):
        return 'known_group_candidate_mismatch'
    if not row[2]:
        return 'known_group_candidate_invalidated'
    frozen = known_group_candidate_request(conn, meta['opportunity_id'])['desk_metadata']
    if (any(meta.get(key) != value for key,value in frozen.items())
            or set(meta) - set(frozen) - {'ticket_id','rails','base_policy_id'}
            or ('rails' in meta and meta['rails'] != {'tp_pct':_KNOWN_GROUP_POLICY['tp_pct'],'sl_pct':_KNOWN_GROUP_POLICY['sl_pct']})):
        return 'known_group_candidate_metadata_mismatch'
    if not datetime.fromisoformat(row[4]) <= datetime.fromisoformat(now_iso) < datetime.fromisoformat(row[3]):
        return 'known_group_entry_expired'
    return None


def known_group_candidate_request(conn, candidate):
    row = conn.execute('SELECT request_json FROM memecoin_known_group_candidates WHERE candidate_id=?', (candidate,)).fetchone()
    if row is None:
        raise ValueError('known_group_candidate_missing')
    return _json.loads(row[0])


def desk_cohort_dashboard_rows(conn):
    return conn.execute('SELECT cohort_id,producer,prompt_version,created_at,kill_limit_usd,stop_mode,'
                        'admission_enabled FROM memecoin_desk_cohorts ORDER BY created_at').fetchall()


def known_group_tail_state(conn, source):
    row = conn.execute('SELECT * FROM memecoin_known_group_tail WHERE source=?', (source,)).fetchone()
    return dict(row) if row else None


def connect_known_group_observer(path):
    from pathlib import Path
    source = Path(path).resolve()
    conn = sqlite3.connect(source,timeout=0.1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    ensure_known_group_observer_tables(conn)
    return conn


def ensure_known_group_observer_tables(conn):
    from pathlib import Path
    migration = Path(__file__).resolve().parents[2]/'migrations/20261101_known_group_observer.sql'
    conn.executescript(migration.read_text())


def store_known_group_evidence(conn, proof_ref, profile):
    import zlib
    from sniper.services.wallet_identity import MAX_BYTES
    raw = _json.dumps(profile,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    if len(raw)>MAX_BYTES:
        raise ValueError('known_group_native_profile_oversized')
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute('INSERT OR IGNORE INTO memecoin_known_group_evidence VALUES(?,?,?,?)',
                 (proof_ref,digest,len(raw),zlib.compress(raw,1)))
    old = conn.execute('SELECT sha256 FROM memecoin_known_group_evidence WHERE proof_ref=?',(proof_ref,)).fetchone()
    if old[0]!=digest:
        conn.rollback()
        raise ValueError('known_group_native_profile_replaced')
    conn.commit()


def known_group_evidence(conn, proof_ref):
    import zlib
    from sniper.services.wallet_identity import MAX_BYTES
    row = conn.execute('SELECT sha256,raw_size,profile_zlib FROM memecoin_known_group_evidence WHERE proof_ref=?',
                       (proof_ref,)).fetchone()
    if row is None or not 0 < row[1] <= MAX_BYTES:
        raise ValueError('known_group_native_profile_missing')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(row[2],MAX_BYTES+1)
    if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or len(raw)!=row[1]
            or hashlib.sha256(raw).hexdigest()!=row[0]):
        raise ValueError('known_group_native_profile_corrupt')
    return _json.loads(raw)


def save_known_group_tail(conn, *, source, identity, cursor, anchor, baseline, started_at, last_host, state):
    conn.execute('INSERT INTO memecoin_known_group_tail VALUES(?,?,?,?,?,?,?,?) '
        'ON CONFLICT(source) DO UPDATE SET identity=excluded.identity,cursor=excluded.cursor,anchor=excluded.anchor,'
        'last_host=excluded.last_host,state_json=excluded.state_json',
        (source,identity,cursor,anchor,baseline,started_at,last_host,
         _json.dumps(state,sort_keys=True,separators=(',',':'),allow_nan=False)))
    conn.commit()


def invalidate_known_group_observer(conn, *, now_iso, reason):
    if conn.execute('SELECT 1 FROM memecoin_known_group_candidates WHERE eligible=1 LIMIT 1').fetchone() is None:
        return
    conn.execute('UPDATE memecoin_known_group_candidates SET eligible=0,invalidated_at=?,reason=? WHERE eligible=1',
                 (now_iso,reason))
    conn.commit()


def known_group_open_positions(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM memecoin_positions WHERE is_open=1 "
        "AND execution_mode='real' AND signal_source='desk' AND desk_cohort_id='known-group-launch-v1'")]


def known_group_candidate_mints(conn, *, now_iso):
    return {r[0] for r in conn.execute('SELECT DISTINCT mint FROM memecoin_known_group_candidates '
        'WHERE eligible=1 AND julianday(deadline_utc)>julianday(?)',(now_iso,))}


def read_known_group_raw_tail(path, *, cursor, pending, anchor, limit, now_host):
    """One short read-only capture snapshot. IDs are keys, never an inferred receipt rate."""
    from pathlib import Path
    source = Path(path).resolve()
    stat = source.stat()
    identity = f'{stat.st_dev}:{stat.st_ino}'
    capture = sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=0)
    capture.row_factory = sqlite3.Row
    try:
        capture.execute('BEGIN')
        maximum = capture.execute('SELECT flow_id,signature,slot,received_at FROM wallet_flow_transactions '
                                  'ORDER BY flow_id DESC LIMIT 1').fetchone()
        if anchor is not None:
            current = capture.execute('SELECT signature,slot,received_at FROM wallet_flow_transactions WHERE flow_id=?',
                                      (cursor,)).fetchone()
            if current is None or list(current) != _json.loads(anchor):
                raise ValueError('known_group_source_anchor_replaced')
        if maximum is not None and maximum['flow_id'] < cursor:
            raise ValueError('known_group_source_regressed')
        rows = capture.execute('SELECT * FROM wallet_flow_transactions WHERE flow_id>? ORDER BY flow_id LIMIT ?',
                               (cursor,limit)).fetchall()
        retry = []
        for flow_id in pending:
            row = capture.execute('SELECT * FROM wallet_flow_transactions WHERE flow_id=?', (int(flow_id),)).fetchone()
            if row is None:
                raise ValueError('known_group_pending_receipt_missing')
            retry.append(row)
        from sniper.settings.known_group import KNOWN_GROUP_TAIL_PENDING_MAX
        day = datetime.fromtimestamp(now_host,timezone.utc).date()
        gaps = capture.execute('SELECT reason,first_at,last_at,known_at FROM wallet_flow_gaps '
            'WHERE day>=? AND day<=? ORDER BY day DESC,reason LIMIT ?',
            ((day-timedelta(days=1)).isoformat(),day.isoformat(),KNOWN_GROUP_TAIL_PENDING_MAX+1)).fetchall()
        if len(gaps)>KNOWN_GROUP_TAIL_PENDING_MAX:
            raise ValueError('known_group_gap_page_overflow')
        for gap in gaps:
            if (any(type(gap[k]) not in (int,float) or not math.isfinite(gap[k]) for k in ('first_at','last_at','known_at'))
                    or not 0 < gap['first_at'] <= gap['last_at'] <= gap['known_at'] <= now_host):
                raise ValueError('known_group_gap_clock_unknown')
        return dict(identity=identity, maximum=dict(maximum) if maximum else None,
                    rows=[dict(r) for r in rows],retry=[dict(r) for r in retry],gaps=[dict(r) for r in gaps])
    finally:
        capture.close()


def known_group_mint_metadata(desk_dir, mint, *, now_host):
    from pathlib import Path
    from sniper.services.memecoin_desk_ps_metadata import read_metadata, OWNER_OF, SOURCE, _CANON_UINT
    path = Path(desk_dir).resolve()/'pumpswap/metadata.db'
    conn = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0)
    try:
        conn.execute('BEGIN')
        meta = read_metadata(conn,mint,as_of=now_host)
        payload = conn.execute('SELECT payload_json FROM mints WHERE mint=?',(mint,)).fetchone()
        if not meta or not payload:
            raise ValueError('known_group_mint_metadata_unknown')
        parsed = _json.loads(payload[0])
        if (meta['metadata_source'] != SOURCE or parsed['decimals'] != meta['decimals']
                or parsed['supply_atomic'] != meta['supply_atomic'] or parsed['slot'] != meta['metadata_slot']
                or parsed['token_program'] != meta['token_program'] or parsed['owner'] != OWNER_OF.get(parsed['token_program'])
                or type(parsed['decimals']) is not int or not 0 <= parsed['decimals'] <= 18
                or type(parsed['supply_atomic']) is not str or not _CANON_UINT.fullmatch(parsed['supply_atomic'])
                or not 0 < int(parsed['supply_atomic']) < 2**64):
            raise ValueError('known_group_mint_metadata_unproven')
        return meta
    finally:
        conn.close()


def known_group_watch_active(path, address):
    from pathlib import Path
    conn = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=0)
    try:
        row = conn.execute('SELECT removed_at FROM watch_list WHERE address=?',(address,)).fetchone()
        return row is not None and row[0] is None
    finally:
        conn.close()


def connect_known_group_ledger(path, *, readonly=False):
    from pathlib import Path
    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError('known_group_existing_ledger_required')
    conn = sqlite3.connect(source.as_uri()+('?mode=ro' if readonly else '?mode=rw'),uri=True,timeout=0.1)
    conn.row_factory = sqlite3.Row
    if not readonly:
        ensure_memecoin_tables(conn)
    return conn


def invalidate_known_group_candidates(conn, *, sell, now_iso):
    payload = _json.dumps(sell, sort_keys=True, separators=(',',':'), allow_nan=False)
    key = (sell['signature'], sell['owner'], sell['mint'])
    conn.execute('INSERT OR IGNORE INTO memecoin_known_group_sells VALUES(?,?,?,?,?)', (*key,payload,now_iso))
    previous = conn.execute('SELECT payload_json FROM memecoin_known_group_sells WHERE signature=? AND owner=? AND mint=?', key).fetchone()
    if previous[0] != payload:
        conn.rollback()
        raise ValueError('known_group_sell_payload_mismatch')
    changed = conn.execute("UPDATE memecoin_known_group_candidates SET eligible=0,invalidated_at=?,reason='known_prior_sell' "
        "WHERE mint=? AND eligible=1 AND EXISTS(SELECT 1 FROM json_each(proof_json,'$.members') member "
        "WHERE json_extract(member.value,'$.address')=?)", (now_iso,sell['mint'],sell['owner'])).rowcount
    conn.commit()
    return changed


def enqueue_known_group_exit(conn, *, position_id, payload, now_iso):
    sell = payload['sell']
    key = _json.dumps([position_id,payload['entry_contract_hash']],separators=(',',':'))
    intent = 'known-group-exit:' + hashlib.sha256(key.encode()).hexdigest()
    payload_json = _json.dumps(payload, sort_keys=True, separators=(',',':'), allow_nan=False)
    # Decision clocks belong to the first nomination; identical source replay retains that decision.
    previous = conn.execute('SELECT request_json FROM memecoin_manual_exit_intents WHERE intent_id=?', (intent,)).fetchone()
    if previous:
        saved = _json.loads(previous[0])
        if saved['sell']['signature'] != sell['signature']:
            return intent  # The first nomination owns this entry; later sales never add a second close.
        if any(saved.get(k) != payload.get(k) for k in ('position_id','sell','entry_contract_hash')):
            raise ValueError('known_group_exit_payload_mismatch')
        return intent
    from sniper.services.memecoin_manual_exit_intents import DEFAULT_TTL_S
    expires = (datetime.fromisoformat(now_iso) + timedelta(seconds=DEFAULT_TTL_S)).isoformat()
    conn.execute('INSERT OR IGNORE INTO memecoin_manual_exit_intents(intent_id,requested_at,expires_at,'
        'position_id,action,fraction,actor,state,attempts,request_json) VALUES(?,?,?,?,?,1.0,?,?,0,?)',
        (intent,now_iso,expires,position_id,'known_group_close','known-group-launch-v1','queued',payload_json))
    conn.commit()
    saved = conn.execute('SELECT request_json FROM memecoin_manual_exit_intents WHERE intent_id=?', (intent,)).fetchone()
    if saved[0] != payload_json:
        if _json.loads(saved[0])['sell']['signature'] == sell['signature']:
            raise ValueError('known_group_exit_payload_mismatch')
    from sniper.services.memecoin_manual_exit_intents import wake_address
    from sniper.services.memecoin_onchain_wake import send_wake
    send_wake(wake_address())
    return intent


def known_group_exit_source_matches(conn, payload):
    sell = payload['sell']
    row = conn.execute('SELECT payload_json FROM memecoin_known_group_sells WHERE signature=? AND owner=? AND mint=?',
                      (sell['signature'],sell['owner'],sell['mint'])).fetchone()
    return row is not None and row[0] == _json.dumps(sell,sort_keys=True,separators=(',',':'),allow_nan=False)


def register_desk_cohort(conn, *, producer, policy, prompt_version, created_at,
                         cohort_id=None, commit=True, stop_mode='loss_limit'):
    """Explicit provisioning, never per-emission rollover. A policy cannot be reset by renaming it."""
    if producer not in ("H", "U") or not _desk_identifier(prompt_version) or not isinstance(policy, dict):
        raise ValueError("invalid desk cohort policy")
    _desk_aware_utc(created_at)
    from sniper.services.memecoin_known_group_canary import _valid_policy, COHORT_ID
    manual = stop_mode == 'manual'
    if stop_mode not in ('loss_limit', 'manual') or (manual and (
            cohort_id != COHORT_ID or not _valid_policy(policy, producer, prompt_version))):
        raise ValueError('manual stop requires the exact known-group policy')
    if cohort_id == COHORT_ID and not manual:
        raise ValueError('known-group policy requires explicit manual stop')
    try:
        body = _json.dumps({"producer": producer, "prompt_version": prompt_version,
                            "policy": policy, "kill_limit_usd": 0.0 if manual else 200.0,
                            **({'stop_mode':'manual'} if manual else {})},
                           sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("invalid desk cohort policy") from None
    if len(body.encode()) > 65536:
        raise ValueError("desk cohort policy too large")
    digest = hashlib.sha256(body.encode()).hexdigest()
    cohort_id = cohort_id or f"desk-cohort:{producer}:{digest}"
    if not _desk_identifier(cohort_id):
        raise ValueError("invalid desk cohort identity")
    if conn.isolation_level is None and not conn.in_transaction:
        if not commit:
            raise ValueError("autocommit handle requires an explicit caller transaction")
        conn.execute("BEGIN IMMEDIATE")  # Explicit off-path provisioning only.
    # The INSERT obtains the writer slot before inspecting the winner, including ignored aliases.
    try:
        conn.execute("INSERT OR IGNORE INTO memecoin_desk_cohorts "
                     "(cohort_id,producer,prompt_version,policy_hash,policy_json,created_at,kill_limit_usd,stop_mode) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     (cohort_id, producer, prompt_version, digest, body, created_at,
                      0.0 if manual else 200.0, stop_mode))
        old = conn.execute("SELECT producer,policy_hash FROM memecoin_desk_cohorts WHERE cohort_id=?",
                           (cohort_id,)).fetchone()
        if old is None:
            raise ValueError("desk cohort policy is already registered under another alias")
        if tuple(old) != (producer, digest):
            raise ValueError("desk cohort already has a different policy")
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    return cohort_id


def desk_cohort_killed(conn, cohort_id):
    return conn.execute("SELECT 1 FROM memecoin_desk_cohort_trips WHERE cohort_id=?",
                        (cohort_id,)).fetchone() is not None


def desk_cohort_accounting(conn, cohort_id):
    """Trade cash PnL less separately recorded charges. Incurred unpriced amounts remain unknown."""
    cash, unpriced = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl),0.0), "
        "COALESCE(SUM(CASE WHEN realized_pnl IS NULL THEN 1 ELSE 0 END),0) "
        "FROM memecoin_positions WHERE desk_cohort_id=? AND execution_mode='real' "
        "AND signal_source='desk' AND is_open=0", (cohort_id,)).fetchone()
    costs, unknown = conn.execute(
        "SELECT COALESCE(SUM(usd),0.0),COALESCE(SUM(CASE WHEN usd IS NULL THEN 1 ELSE 0 END),0) "
        "FROM memecoin_desk_cohort_costs WHERE cohort_id=?", (cohort_id,)).fetchone()
    pending_unknown = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries p WHERE "
        + _DESK_PENDING_COST_GAP_SQL.format(cohort="?", priced=""), (cohort_id,)).fetchone()[0]
    unknown += pending_unknown
    for kind, column in (("exit", "close_sig"), ("partial", "derisk_sig")):
        unknown += conn.execute("SELECT COUNT(*) FROM memecoin_positions p WHERE "
            + _DESK_POSITION_COST_GAP_SQL.format(cohort="?", column=column, kind=kind, priced=""),
            (cohort_id,)).fetchone()[0]
    known_net = float(cash) - float(costs)
    return {"cash_pnl_usd": float(cash), "known_cost_usd": float(costs),
            "known_net_usd": known_net, "unknown_costs": int(unknown),
            "unpriced_closes": int(unpriced), "net_usd": None if unknown or unpriced else known_net}


_ATA_RENT_LAMPORTS = 2_039_280            # one new token account, paid at entry (refunded on close, counted anyway)
_BASE_FEE_LAMPORTS = 5_000
_SOLANA_MAX_CU = 1_400_000                # the protocol's per-transaction compute ceiling
_ENTRY_CU_PRICE_MAX = 50_000_000          # settings' validator ceiling for MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS
# Zero only for a validated fact about that exact call (Codex/Gamma/Alpha 2026-10-22); a generic model-invalid
# marker also covers malformed or conflicting BILLED receipts, so it is an unknown and fails closed until a
# caller-enforced per-call ceiling exists.


def _finite(value, name, *, positive):
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")) or (v <= 0 if positive else v < 0):
        raise ValueError("desk cost bound component %s=%r must be finite and %s"
                         % (name, value, "positive" if positive else "non-negative"))
    return v


def desk_execution_cost_bound():
    """Worst-case USD of ONE unpriced desk ENTRY leg signed under the unconditional entry fee clamp (option B,
    2026-10-22), x 1.5. The clamp (cap_entry_priority_fee, every entry, cache or not) caps priority + Nozomi tip at
    MAX_TOTAL_LAMPORTS, or at MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS on a crowded treated send; the Sender A/B
    tip guard caps the Sender tip at TIP_CAP_LAMPORTS; plus one ATA rent and the base fee, priced at
    MEMECOIN_DESK_BOUND_SOL_USD, plus the venue fee on the clip. Exits are NOT bounded here: no exit path enforces a
    fee ceiling at signing, so an unknown exit leg holds the cohort. Every component is validated first."""
    from sniper.settings import memecoin as cfg
    from sniper.execution.solana_entry_bids import MAX_TOTAL_LAMPORTS
    from sniper.execution.solana_sender_ab import TIP_CAP_LAMPORTS
    clip = max(_finite(cfg.MEMECOIN_DESK_CLIP_USD or 0.0, "MEMECOIN_DESK_CLIP_USD", positive=False), 25.0)
    fee_pct = _finite(cfg.MEMECOIN_DESK_VERDICT_FEE_PCT, "MEMECOIN_DESK_VERDICT_FEE_PCT", positive=True)
    sol_usd = _finite(cfg.MEMECOIN_DESK_BOUND_SOL_USD, "MEMECOIN_DESK_BOUND_SOL_USD", positive=True)
    crowded = int(_finite(cfg.MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS,
                          "MEMECOIN_SLOT_LANE_CROWDED_MAX_SEND_LAMPORTS", positive=False))
    lanes = {"entry_clamp": int(MAX_TOTAL_LAMPORTS), "entry_crowded": crowded}
    priority = max(lanes.values()) + int(TIP_CAP_LAMPORTS)
    lamports = priority + _ATA_RENT_LAMPORTS + _BASE_FEE_LAMPORTS
    venue = clip * fee_pct / 100.0
    usd = (venue + lamports / 1e9 * sol_usd) * 1.5
    if not (usd > 0 and math.isfinite(usd)):          # finite inputs can still overflow (Codex P2)
        raise ValueError("desk execution cost bound %r is not finite and positive" % usd)
    return {"usd": usd, "venue_usd": venue, "priority_lamports": priority, "lanes": lanes, "lamports": lamports,
            "sender_tip_cap": int(TIP_CAP_LAMPORTS), "sol_usd": sol_usd, "clip_usd": clip, "factor": 1.5}


def _desk_zero_model(provenance):
    """True only for the exact no-call fact or the exact validated flat proof (prefix + 64 lowercase hex)."""
    if provenance == "desk:no_model_call":
        return True
    p = str(provenance)
    return (p.startswith(_DESK_FLAT_PREFIX) and len(p) == len(_DESK_FLAT_PREFIX) + 64
            and all(c in "0123456789abcdef" for c in p[len(_DESK_FLAT_PREFIX):]))


def _desk_entry_row_proven(conn, event_id, position_id, limit):
    """Python twin of _DESK_ENTRY_ROW_SQL for one NULL execution row."""
    if str(event_id).startswith("pending:"):
        row = conn.execute("SELECT 1 FROM memecoin_pending_entries p WHERE ? = 'pending:' || p.id || ':' || p.tx_sig "
                           "AND p.entry_fee_ceiling_lamports IS NOT NULL AND p.entry_fee_ceiling_lamports <= ?",
                           (event_id, limit)).fetchone()
        return row is not None
    if str(event_id).startswith("entry:") and position_id is not None:
        rows = conn.execute("SELECT p.entry_fee_ceiling_lamports FROM memecoin_positions q JOIN "
                            "memecoin_pending_entries p ON p.attempt_id = q.attempt_id WHERE q.id = ? "
                            "AND q.attempt_id IS NOT NULL AND p.tx_sig IS NOT NULL", (position_id,)).fetchall()
        return bool(rows) and all(r[0] is not None and r[0] <= limit for r in rows)
    return False


def _desk_unknowns_by_kind(conn, cohort_id, limit):
    """({kind: count}, flat_model, oldest occurred_at) over every unknown the accounting counts. Kinds:
    'execution' = an ENTRY leg whose own signed bytes carry a recorded fee footprint <= `limit` (the bound's
    priority+tip lamports): bounded; 'entry_unbounded' = an entry leg without one (old runner, unproven bytes, NULL,
    missing or unmatched link, above the limit); 'exit_unbounded' = any exit/partial leg (no ceiling is enforced at
    exit signing); 'model_unbounded' = any NULL model row that is not the exact no-call or flat proof (counted 0 as
    flat_model); any other kind keeps its own name. All but 'execution' hold the cohort."""
    counts, invalid, oldest = {}, 0, None

    def add(key, at):
        nonlocal oldest
        counts[key] = counts.get(key, 0) + 1
        if at is not None:
            oldest = at if oldest is None or at < oldest else oldest

    for kind, provenance, at, event_id, position_id in conn.execute(
            "SELECT kind, provenance, occurred_at, event_id, position_id FROM memecoin_desk_cohort_costs "
            "WHERE cohort_id=? AND usd IS NULL", (cohort_id,)):
        if kind == "model" and _desk_zero_model(provenance):
            invalid += 1
        elif kind == "model":
            add("model_unbounded", at)
        elif kind == "execution":
            if str(event_id).startswith(("entry:", "pending:")):
                add("execution" if _desk_entry_row_proven(conn, event_id, position_id, limit) else "entry_unbounded",
                    at)
            else:
                add("exit_unbounded", at)              # exit:/partial: legs: no enforced ceiling at signing
        else:
            add(kind, at)
    for at, ceiling in conn.execute("SELECT p.reserved_at, p.entry_fee_ceiling_lamports FROM memecoin_pending_entries p "
                                    "WHERE " + _DESK_PENDING_COST_GAP_SQL.format(cohort="?", priced=""), (cohort_id,)):
        add("execution" if ceiling is not None and ceiling <= limit else "entry_unbounded", at)
    for kind, column, at_col in (("exit", "close_sig", "closed_at"), ("partial", "derisk_sig", "opened_at")):
        for (at,) in conn.execute("SELECT p.%s FROM memecoin_positions p WHERE " % at_col
                                  + _DESK_POSITION_COST_GAP_SQL.format(cohort="?", column=column, kind=kind,
                                                                       priced=""), (cohort_id,)):
            add("exit_unbounded", at)
    return counts, invalid, oldest


def desk_cohort_gate(conn, *, cohort_id, producer):
    """The cohort admission verdict with its arithmetic: {'refusal', 'known_net_usd', 'bounded_net_usd',
    'unknown_by_kind', 'flat_model', 'unpriced_closes', 'bound_usd', 'oldest_unknown_at', 'legacy'}.
    Refusal 'cohort_accounting_unknown' when an unknown cannot be bounded (an unpriced close, a metered model cost,
    any unlisted kind) or when known_net - sum(bounds) <= -kill_limit; LEGACY restores 'any unknown refuses'."""
    from sniper.settings import memecoin as cfg
    out = {"refusal": None, "known_net_usd": None, "bounded_net_usd": None, "unknown_by_kind": {},
           "flat_model": 0, "unpriced_closes": 0, "bound_usd": None, "oldest_unknown_at": None,
           "legacy": bool(cfg.MEMECOIN_DESK_COST_GATE_LEGACY)}
    if not _desk_identifier(cohort_id) or producer not in ("H", "U"):
        return {**out, "refusal": "desk_cohort_missing"}
    row = conn.execute("SELECT producer,kill_limit_usd,stop_mode FROM memecoin_desk_cohorts WHERE cohort_id=?",
                       (cohort_id,)).fetchone()
    if row is None:
        return {**out, "refusal": "desk_cohort_unregistered"}
    if row[0] != producer:
        return {**out, "refusal": "desk_cohort_mismatch"}
    if desk_cohort_killed(conn, cohort_id):
        return {**out, "refusal": "desk_cohort_killed"}
    manual = row[2] == 'manual'
    if manual:
        refusal = _known_group_policy_refusal(conn, cohort_id)
        if refusal:
            return {**out, 'refusal':refusal}
    elif row[2] != 'loss_limit' or float(row[1]) <= 0:
        return {**out, 'refusal':'desk_cohort_policy_invalid'}
    state = desk_cohort_accounting(conn, cohort_id)
    out["known_net_usd"], out["unpriced_closes"] = state["known_net_usd"], state["unpriced_closes"]
    if not manual and state["known_net_usd"] <= -float(row[1]):
        return {**out, "refusal": "desk_cohort_killed"}
    if out["legacy"]:
        if state["unknown_costs"] or state["unpriced_closes"]:
            out["refusal"] = "cohort_accounting_unknown"
        return out
    try:
        b = desk_execution_cost_bound()
    except ValueError as exc:                   # a bad bound component refuses (fail closed), never admits
        logger.warning("desk cost gate: bound invalid cohort=%s: %s", cohort_id, exc)
        return {**out, "refusal": "cohort_accounting_unknown", "bound_error": str(exc)}
    bound = b["usd"]
    counts, invalid, oldest = _desk_unknowns_by_kind(conn, cohort_id, b["priority_lamports"])
    out.update(unknown_by_kind=counts, flat_model=invalid, oldest_unknown_at=oldest, bound_usd=bound)
    out["bounded_net_usd"] = state["known_net_usd"] - bound * counts.get("execution", 0)
    if (state["unpriced_closes"] or set(counts) - {"execution"}
            or not math.isfinite(out["bounded_net_usd"]) or (not manual and out["bounded_net_usd"] <= -float(row[1]))):
        out["refusal"] = "cohort_accounting_unknown"
    return out


def desk_cohort_gates(conn):
    """desk_cohort_gate for every registered, not-killed cohort (the age page's input; tiny table)."""
    rows = conn.execute("SELECT c.cohort_id, c.producer FROM memecoin_desk_cohorts c WHERE NOT EXISTS "
                        "(SELECT 1 FROM memecoin_desk_cohort_trips t WHERE t.cohort_id=c.cohort_id)").fetchall()
    return [{"cohort_id": r[0], **desk_cohort_gate(conn, cohort_id=r[0], producer=r[1])} for r in rows]


def _latch_desk_cohort_if_tripped(conn, cohort_id, at_iso):
    row = conn.execute("SELECT kill_limit_usd,stop_mode FROM memecoin_desk_cohorts WHERE cohort_id=?",
                       (cohort_id,)).fetchone()
    if row is None or desk_cohort_killed(conn, cohort_id):
        return
    if row[1] == 'manual':
        return
    state = desk_cohort_accounting(conn, cohort_id)
    if state["known_net_usd"] <= -float(row[0]):
        conn.execute("INSERT OR IGNORE INTO memecoin_desk_cohort_trips "
                     "(cohort_id,tripped_at,net_usd,cash_pnl_usd,known_cost_usd) VALUES(?,?,?,?,?)",
                     (cohort_id, at_iso, state["known_net_usd"], state["cash_pnl_usd"],
                      state["known_cost_usd"]))


def get_desk_cohort_cost(conn, cohort_id, event_id):
    """Read one billing marker off the fill path without changing its valuation."""
    columns = ("kind", "usd", "occurred_at", "provenance")
    row = conn.execute("SELECT kind,usd,occurred_at,provenance FROM memecoin_desk_cohort_costs "
                       "WHERE cohort_id=? AND event_id=?", (cohort_id, event_id)).fetchone()
    return dict(zip(columns, row)) if row is not None else None


def record_desk_cohort_cost(conn, *, cohort_id, event_id, usd, kind, occurred_at, provenance,
                            position_id=None, commit=True):
    """One incurred charge, or its once-only NULL -> exact valuation. Refunds never clear a kill.

    Network totals already include priority fees. Callers must not charge that component twice.
    Execution charges contain costs outside trade notional, including paid tips and net rent.
    """
    if (not _desk_identifier(event_id) or not _desk_identifier(provenance)
            or kind not in ("execution", "model", "refund")):
        raise ValueError("invalid desk cost identity")
    _desk_aware_utc(occurred_at)
    if usd is not None and (isinstance(usd, bool) or not isinstance(usd, (int, float))
                            or not math.isfinite(usd) or (usd < 0 and kind != "refund")):
        raise ValueError("desk cost must be a finite USD number, or explicitly unknown")
    if conn.execute("SELECT 1 FROM memecoin_desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone() is None:
        raise ValueError("desk cohort not registered")
    if position_id is not None:
        pos = conn.execute("SELECT desk_cohort_id FROM memecoin_positions WHERE id=?", (position_id,)).fetchone()
        if pos is None or pos[0] != cohort_id:
            raise ValueError("desk cost position belongs to a different cohort")
    if conn.isolation_level is None and not conn.in_transaction:
        if not commit:
            raise ValueError("autocommit handle requires an explicit caller transaction")
        conn.execute("BEGIN IMMEDIATE")  # Cohort provisioning/billing only; never a fill-path lock.
    try:
        # Write first: concurrent replays/valuations inspect the actual winner while owning the writer slot.
        inserted = conn.execute("INSERT OR IGNORE INTO memecoin_desk_cohort_costs "
                     "(cohort_id,event_id,kind,usd,occurred_at,provenance,position_id,resolved_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (cohort_id, event_id, kind, usd, occurred_at,
                                                provenance, position_id,
                                                real_utc_now_iso() if usd is not None else None))
        if event_id.startswith("pending:"):
            identity = re.fullmatch(r"pending:([1-9][0-9]*):(.+)", event_id)
            pending = (conn.execute("SELECT signal_source,desk_cohort_id,status,tx_sig "
                                   "FROM memecoin_pending_entries WHERE id=?", (identity[1],)).fetchone()
                       if identity else None)
            refusal = None
            if (pending is None or pending[0] != SIGNAL_SOURCE_DESK or pending[1] != cohort_id
                    or kind != "execution" or position_id is not None):
                refusal = "invalid pending entry cost identity"
            else:
                current = pending[3] == identity[2]
                if ((current and pending[2] != "failed")
                        or (inserted.rowcount == 1 and not (current and pending[2] == "failed"))):
                    refusal = "pending entry cost requires a failed signature or durable reset marker"
            if refusal:
                # A successful position owns its entry:<pid> fee. Only a failed pending signature
                # or a durable reset marker may own a pending fee, even across later retries.
                if inserted.rowcount == 1:
                    conn.execute("DELETE FROM memecoin_desk_cohort_costs WHERE cohort_id=? AND event_id=?",
                                 (cohort_id, event_id))
                raise ValueError(refusal)
        old = conn.execute("SELECT kind,usd,occurred_at,provenance,position_id FROM memecoin_desk_cohort_costs "
                           "WHERE cohort_id=? AND event_id=?", (cohort_id, event_id)).fetchone()
        if (old[0], old[2], old[4]) != (kind, occurred_at, position_id):
            raise ValueError("desk cost already has a different identity")
        if old[1] is not None and (old[1] != usd or old[3] != provenance):
            raise ValueError("desk cost valuation is immutable")
        if old[1] is None and usd is None and old[3] != provenance:
            raise ValueError("desk cost unknown provenance is immutable")
        if old[1] is None and usd is not None:
            conn.execute("UPDATE memecoin_desk_cohort_costs SET usd=?,provenance=?,resolved_at=? "
                         "WHERE cohort_id=? AND event_id=? AND usd IS NULL",
                         (usd, provenance, real_utc_now_iso(), cohort_id, event_id))
        _latch_desk_cohort_if_tripped(conn, cohort_id, occurred_at)
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise


_DESK_COST_FILL_FIELDS = (
    "kind", "id", "tx_sig", "qty", "notional", "funding_asset", "funding_spent_atomic",
    "funding_sol_usd", "network_fee_lamports", "priority_fee_lamports", "rent_lamports",
    "returned_rent_lamports", "created_at",
)


def _desk_cost_signature(value, network):
    if not isinstance(value, str) or not value.strip():
        return None
    return value if network == "solana" else value.lower()


def _desk_execution_cost_request(conn, cohort_id, event_id):
    """Read one bounded owner/fill binding. A missing entry fill remains explicit work debt."""
    marker = conn.execute("SELECT kind,occurred_at,position_id FROM memecoin_desk_cohort_costs "
                          "WHERE cohort_id=? AND event_id=?", (cohort_id, event_id)).fetchone()
    if marker is not None and marker[0] != "execution":
        return None
    request = {"cohort_id": cohort_id, "event_id": event_id, "kind": "execution",
               "event_kind": event_id.split(":", 1)[0], "occurred_at": marker[1] if marker else None,
               "marker_exists": marker is not None, "position_id": None, "pending_id": None,
               "network_id": None, "mint": None, "tx_sig": None, "owner_status": None,
               "fill": None, "ownership_error": None}
    identity = re.fullmatch(r"(entry|partial|exit|pending):([1-9][0-9]*)(?::(.+))?", event_id)
    if not identity or (identity[1] == "entry") != (identity[3] is None):
        request["ownership_error"] = "event_identity"
        return request
    kind, owner_id, signature = identity[1], int(identity[2]), identity[3]
    if kind == "pending":
        row = conn.execute("SELECT signal_source,desk_cohort_id,network_id,mint,tx_sig,status,"
                           "broadcast_at,reserved_at FROM memecoin_pending_entries WHERE id=?",
                           (owner_id,)).fetchone()
        if row is not None and row[0] != SIGNAL_SOURCE_DESK:
            return None
        request["pending_id"] = owner_id
        if row is None or row[1] != cohort_id:
            request["ownership_error"] = "owner_missing_or_foreign"
            return request
        request.update(network_id=row[2], mint=row[3], owner_status=row[5])
        request["tx_sig"] = _desk_cost_signature(signature, row[2])
        request["occurred_at"] = request["occurred_at"] or row[6] or row[7]
        current = _desk_cost_signature(row[4], row[2]) == request["tx_sig"]
        if ((marker is not None and marker[2] is not None)
                or (current and row[5] != "failed") or (marker is None and not (current and row[5] == "failed"))):
            request["ownership_error"] = "pending_owner_changed"
    else:
        row = conn.execute("SELECT signal_source,execution_mode,desk_cohort_id,network_id,mint,"
                           "is_open,opened_at,close_sig,derisk_sig,closed_at,derisked_at,"
                           "close_pending_at,derisk_pending_at "
                           "FROM memecoin_positions WHERE id=?", (owner_id,)).fetchone()
        if row is not None and (row[0] != SIGNAL_SOURCE_DESK or row[1] != "real"):
            return None
        request["position_id"] = owner_id
        if row is None or row[2] != cohort_id:
            request["ownership_error"] = "owner_missing_or_foreign"
            return request
        request.update(network_id=row[3], mint=row[4], owner_status="open" if row[5] else "closed")
        fields = ",".join(_DESK_COST_FILL_FIELDS)
        if kind == "entry":
            fills = conn.execute(f"SELECT {fields},network_id FROM memecoin_fills "
                                 "WHERE position_id=? AND kind='entry' ORDER BY id LIMIT 2", (owner_id,)).fetchall()
            if len(fills) != 1 or _desk_cost_signature(fills[0][2], row[3]) is None:
                request["ownership_error"] = "entry_fill_missing_or_ambiguous"
            else:
                signature = fills[0][2]
        else:
            fill_kinds = ("derisk",) if kind == "partial" else FULL_EXIT_KINDS
            marks = ",".join("?" for _ in fill_kinds)
            comparison = "tx_sig=?" if row[3] == "solana" else "lower(tx_sig)=lower(?)"
            fills = conn.execute(f"SELECT {fields},network_id FROM memecoin_fills WHERE position_id=? "
                                 f"AND kind IN ({marks}) AND {comparison} ORDER BY id LIMIT 2",
                                 (owner_id, *fill_kinds, signature)).fetchall()
            if len(fills) > 1:
                request["ownership_error"] = "fill_ambiguous"
            pointer = row[8] if kind == "partial" else row[7]
            if marker is None and not fills and (_desk_cost_signature(pointer, row[3]) !=
                                                 _desk_cost_signature(signature, row[3])):
                request["ownership_error"] = "signature_owner_changed"
        request["tx_sig"] = _desk_cost_signature(signature, row[3])
        if len(fills) == 1:
            request["fill"] = dict(zip(_DESK_COST_FILL_FIELDS, fills[0][:-1]))
            if fills[0][-1] is not None and fills[0][-1] != row[3]:
                request["ownership_error"] = "fill_network_mismatch"
        request["occurred_at"] = (request["occurred_at"] or
            (request["fill"]["created_at"] if request["fill"] else None) or
            ((row[12] or row[10]) if kind == "partial" else
             (row[11] or row[9]) if kind == "exit" else row[6]))
        if marker is not None and marker[2] != owner_id:
            request["ownership_error"] = "marker_position_mismatch"
    if not request["network_id"] or not request["mint"] or not request["tx_sig"]:
        request["ownership_error"] = request["ownership_error"] or "ownership_identity_missing"
    if not request["occurred_at"]:
        request["ownership_error"] = request["ownership_error"] or "occurrence_missing"
    return request


def list_unpriced_desk_execution_costs(conn, *, after=None, limit=20):
    """Materialized, keyset-paged receipt requests. No write, provider I/O, or raw signed payload.

    Advance the cursor even for an unavailable owner/fill; wrap after the final page to retry it.
    Current pointers supplement durable markers, and settled fills retain old partial signatures.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("desk expense limit must be an integer from 1 to 100")
    if after is not None and (not isinstance(after, (tuple, list)) or len(after) != 2
                              or not all(_desk_identifier(v) for v in after)):
        raise ValueError("invalid desk expense cursor")
    cursor = tuple(after) if after is not None else ("", "")
    fills = ",".join("'" + k + "'" for k in ("entry", "derisk", *FULL_EXIT_KINDS))
    sql = (
        "WITH candidates(cohort_id,event_id) AS ("
        "SELECT x.cohort_id,x.event_id FROM memecoin_desk_cohort_costs x "
        "LEFT JOIN memecoin_positions owner ON owner.id=COALESCE(x.position_id,"
        "CAST(substr(x.event_id,instr(x.event_id,':')+1) AS INTEGER)) "
        "AND (x.event_id LIKE 'entry:%' OR x.event_id LIKE 'partial:%' OR x.event_id LIKE 'exit:%') "
        "LEFT JOIN memecoin_pending_entries pending ON x.event_id LIKE 'pending:%' "
        "AND pending.id=CAST(substr(x.event_id,9) AS INTEGER) "
        "WHERE x.kind='execution' AND x.usd IS NULL "
        "AND (owner.id IS NULL OR (owner.signal_source='desk' AND owner.execution_mode='real')) "
        "AND (pending.id IS NULL OR pending.signal_source='desk') "
        "UNION SELECT desk_cohort_id,'pending:'||id||':'||tx_sig FROM memecoin_pending_entries "
        "WHERE signal_source='desk' AND status='failed' AND tx_sig IS NOT NULL "
        "UNION SELECT desk_cohort_id,'exit:'||id||':'||close_sig FROM memecoin_positions "
        "INDEXED BY idx_mc_pos_desk_cohort WHERE desk_cohort_id IS NOT NULL "
        "AND signal_source='desk' AND execution_mode='real' AND close_sig IS NOT NULL "
        "UNION SELECT desk_cohort_id,'partial:'||id||':'||derisk_sig FROM memecoin_positions "
        "INDEXED BY idx_mc_pos_desk_cohort WHERE desk_cohort_id IS NOT NULL "
        "AND signal_source='desk' AND execution_mode='real' AND derisk_sig IS NOT NULL "
        "UNION SELECT p.desk_cohort_id,CASE WHEN f.kind='entry' THEN 'entry:'||p.id "
        "WHEN f.kind='derisk' THEN 'partial:'||p.id||':'||f.tx_sig ELSE 'exit:'||p.id||':'||f.tx_sig END "
        "FROM memecoin_positions p INDEXED BY idx_mc_pos_desk_cohort "
        "JOIN memecoin_fills f ON f.position_id=p.id "
        f"WHERE p.desk_cohort_id IS NOT NULL AND p.signal_source='desk' AND p.execution_mode='real' AND f.kind IN ({fills}) "
        "AND (f.kind='entry' OR f.tx_sig IS NOT NULL)) "
        "SELECT q.cohort_id,q.event_id FROM candidates q JOIN memecoin_desk_cohorts c ON c.cohort_id=q.cohort_id "
        "LEFT JOIN memecoin_desk_cohort_costs x ON x.cohort_id=q.cohort_id AND x.event_id=q.event_id "
        "WHERE x.usd IS NULL AND (x.kind IS NULL OR x.kind='execution') "
        "AND (q.cohort_id,q.event_id)>(?,?) ORDER BY q.cohort_id,q.event_id LIMIT ?")
    rows = conn.execute(sql, (*cursor, limit)).fetchall()
    return [request for cohort_id, event_id in rows
            if (request := _desk_execution_cost_request(conn, cohort_id, event_id)) is not None]


def resolve_desk_execution_cost(conn, *, request, execution_usd, refund_usd=0, evidence,
                                known_at, provenance, commit=True):
    """Off-path only: revalidate ownership, freeze evidence, and refund before charging atomically.

    commit=False requires a caller transaction. A savepoint preserves unrelated caller work when
    validation or pricing fails; provider/FX work must finish before this function is called.
    """
    if (not isinstance(request, dict) or not _desk_identifier(request.get("cohort_id"))
            or not _desk_identifier(request.get("event_id")) or not _desk_identifier(provenance)):
        raise ValueError("invalid desk expense request")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
           for v in (execution_usd, refund_usd)):
        raise ValueError("desk execution and refund must be finite nonnegative USD numbers")
    if not isinstance(evidence, dict) or not isinstance(known_at, str):
        raise ValueError("invalid desk expense evidence")
    _desk_aware_utc(known_at)
    try:
        encoded = _json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("invalid desk expense evidence") from None
    if len(encoded.encode()) > 65536:
        raise ValueError("desk expense evidence too large")
    if commit and conn.in_transaction:
        raise ValueError("desk expense resolution requires a clean transaction")
    if not commit and not conn.in_transaction:
        raise ValueError("desk expense resolution requires an explicit caller transaction")
    if commit:
        conn.execute("BEGIN IMMEDIATE")  # Dedicated off-path writer, never a funded fill-path lock.
    conn.execute("SAVEPOINT desk_execution_cost")
    try:
        cohort_id, event_id = request["cohort_id"], request["event_id"]
        current = _desk_execution_cost_request(conn, cohort_id, event_id)
        identity = ("cohort_id", "event_id", "kind", "event_kind", "occurred_at", "position_id",
                    "pending_id", "network_id", "mint", "tx_sig", "fill")
        if (current is None or current["ownership_error"] or request.get("ownership_error")
                or any(request.get(key) != current[key] for key in identity)):
            raise ValueError("desk expense ownership changed or fill signature is ambiguous")
        if conn.execute("SELECT 1 FROM memecoin_desk_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone() is None:
            raise ValueError("desk expense cohort is not registered")
        for key in ("cohort_id", "event_id", "network_id", "mint", "tx_sig"):
            if evidence.get(key) != current[key]:
                raise ValueError("foreign desk expense evidence")
        occurred_at = _desk_aware_utc(current["occurred_at"])
        for stamp in (occurred_at, evidence.get("event_at", occurred_at)):
            if not isinstance(stamp, str):
                raise ValueError("invalid desk expense evidence timestamp")
            _desk_aware_utc(stamp)
            if datetime.fromisoformat(stamp.replace("Z", "+00:00")) > datetime.fromisoformat(known_at.replace("Z", "+00:00")):
                raise ValueError("desk expense evidence is not known at known_at")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        expected = (encoded, digest, known_at, provenance, float(execution_usd), float(refund_usd))
        old = conn.execute("SELECT evidence_json,evidence_hash,known_at,provenance,execution_usd,refund_usd "
                           "FROM memecoin_desk_cost_evidence WHERE cohort_id=? AND event_id=?",
                           (cohort_id, event_id)).fetchone()
        if old is not None and tuple(old) != expected:
            raise ValueError("desk expense evidence or valuation is immutable")
        if old is None:
            conn.execute("INSERT INTO memecoin_desk_cost_evidence "
                         "(cohort_id,event_id,evidence_json,evidence_hash,known_at,provenance,execution_usd,refund_usd) "
                         "VALUES(?,?,?,?,?,?,?,?)", (cohort_id, event_id, *expected))
        if refund_usd:
            record_desk_cohort_cost(conn, cohort_id=cohort_id, event_id="refund:" + event_id,
                usd=-refund_usd, kind="refund", occurred_at=occurred_at, provenance=provenance,
                position_id=current["position_id"], commit=False)
        record_desk_cohort_cost(conn, cohort_id=cohort_id, event_id=event_id, usd=execution_usd,
            kind="execution", occurred_at=occurred_at, provenance=provenance,
            position_id=current["position_id"], commit=False)
        conn.execute("RELEASE SAVEPOINT desk_execution_cost")
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        else:
            conn.execute("ROLLBACK TO SAVEPOINT desk_execution_cost")
            conn.execute("RELEASE SAVEPOINT desk_execution_cost")
        raise
    return {"cohort_id": cohort_id, "event_id": event_id, "evidence": _json.loads(encoded),
            "evidence_hash": digest, "known_at": known_at, "provenance": provenance,
            "execution_usd": float(execution_usd), "refund_usd": float(refund_usd)}


def count_rejected_unsigned_pilot_admissions(conn, cohort_id):
    """Exclude proven unsigned refusals from capacity; retain immutable admission history."""
    return conn.execute("""
        SELECT count(*) FROM memecoin_pilot_admissions a
        JOIN memecoin_manual_intents i ON i.signal_source='desk' AND i.desk_ticket_id=a.desk_ticket_id
        WHERE a.cohort_id=? AND json_valid(i.desk_metadata_json)
        AND json_extract(i.desk_metadata_json,'$.cohort_id')=a.cohort_id
        AND i.state='refused' AND i.resolved_at IS NOT NULL AND i.position_id IS NULL
        AND (i.result_status IN ('rejected','refused') OR i.result_status GLOB 'skipped_*')
        AND (i.attempt_id IS NULL OR EXISTS (
            SELECT 1 FROM memecoin_entry_attempts e WHERE e.attempt_id=i.attempt_id))
        AND NOT EXISTS (SELECT 1 FROM memecoin_positions p WHERE
            (p.desk_cohort_id=a.cohort_id AND p.desk_opportunity_id=a.desk_ticket_id)
            OR p.origin_event_id=i.origin_event_id OR p.attempt_id=i.attempt_id)
        AND NOT EXISTS (SELECT 1 FROM memecoin_entry_attempts e WHERE
            (e.attempt_id=i.attempt_id OR e.origin_event_id=i.origin_event_id)
            AND (e.terminal_status IS NOT 'refused' OR e.completed_at IS NULL
                OR e.signing_completed_at IS NOT NULL OR e.transaction_submitted_at IS NOT NULL
                OR e.tx_sig IS NOT NULL OR e.position_id IS NOT NULL OR e.fill_id IS NOT NULL))
        AND NOT EXISTS (SELECT 1 FROM memecoin_pending_entries p WHERE
            ((p.desk_cohort_id=a.cohort_id AND p.desk_opportunity_id=a.desk_ticket_id)
                OR p.origin_event_id=i.origin_event_id OR p.attempt_id=i.attempt_id)
            AND (p.status IS NOT 'failed' OR p.tx_sig IS NOT NULL
                OR p.signed_payload_b64 IS NOT NULL OR p.signed_payload_sha256 IS NOT NULL
                OR p.broadcast_at IS NOT NULL OR p.submission_bound_at IS NOT NULL
                OR p.cleanup_tx_sig IS NOT NULL OR p.cleanup_signed_at IS NOT NULL
                OR p.cleanup_signed_payload_sha256 IS NOT NULL
                OR EXISTS (SELECT 1 FROM memecoin_submission_attempts s
                    WHERE s.owner_kind='reservation' AND s.owner_id=CAST(p.id AS TEXT))))
    """, (cohort_id,)).fetchone()[0]


def desk_cohort_refusal(conn, *, cohort_id, producer):
    return desk_cohort_gate(conn, cohort_id=cohort_id, producer=producer)["refusal"]


def _desk_contract_metadata(contract_json):
    from sniper.services.memecoin_origin import validate_desk_metadata
    try:
        value = _json.loads(contract_json) if isinstance(contract_json, str) else contract_json
        desk = value.get("source", {}).get("desk", {})
        meta = {key: desk.get(key) for key in ("cohort_id", "producer", "opportunity_id", "snapshot_hash")}
        if meta["cohort_id"] == "large-buyer-crowd-v1":
            meta.update({key: desk[key] for key in ("min_others_60s", "admission_proof", "crowd_proof", "time_stop_s", "history_basis") if key in desk})
        if meta['cohort_id'] == 'known-group-launch-v1':
            meta.update({key:desk[key] for key in ('known_group_proof','entry_deadline_utc','time_stop_s','verdict_price',
                'known_group_policy_hash','known_group_cut_host') if key in desk})
        return meta if validate_desk_metadata(meta)[0] else None
    except (TypeError, ValueError, AttributeError):
        return None


def _mark_desk_execution_cost(conn, position_id, event_id, at_iso):
    conn.execute("INSERT OR IGNORE INTO memecoin_desk_cohort_costs "
                 "(cohort_id,event_id,kind,occurred_at,provenance,position_id) "
                 "SELECT desk_cohort_id,?,?,?,?,id FROM memecoin_positions "
                 "WHERE id=? AND signal_source='desk' AND execution_mode='real' "
                 "AND desk_cohort_id IS NOT NULL", (event_id, "execution", at_iso, "receipt:unpriced", position_id))


def _latch_desk_day_for_position(conn, position_id):
    """A settled-PnL writer that knows only the position (receipt reconciliation): latch on the
    day of that position's own close, inside the writer's transaction."""
    row = conn.execute("SELECT closed_at FROM memecoin_positions WHERE id=?",
                       (position_id,)).fetchone()
    if row is not None and row[0]:
        latch_desk_day_on_close(conn, position_id, row[0])


def latch_desk_day_on_close(conn, position_id, closed_at):
    """At a full close, inside the writer's own transaction (no commit here): when the position is a
    REAL DESK position and the desk's realized PnL for that UTC day is at or below -limit, latch the
    day. This is what keeps a loss-then-winner sequence from reopening the day when no desk entry
    was checked in between (Codex review). A copy close pays one primary-key read."""
    row = conn.execute("SELECT signal_source, execution_mode, desk_cohort_id, close_sig "
                       "FROM memecoin_positions WHERE id=?",
                       (position_id,)).fetchone()
    if row is None or row[0] != SIGNAL_SOURCE_DESK or row[1] != "real":
        return
    _latch_desk_day_if_tripped(conn, closed_at, commit=False)
    if row[2]:
        if row[3]:
            _mark_desk_execution_cost(conn, position_id, f"exit:{position_id}:{row[3]}", closed_at)
        _latch_desk_cohort_if_tripped(conn, row[2], closed_at)


def _latch_desk_day_if_tripped(conn, at_iso, *, commit):
    day, halt = str(at_iso)[:10], _desk_halt()
    if halt <= 0 or desk_loss_latched(conn, day):
        return
    realized = desk_realized_pnl_since(conn, day + "T00:00:00+00:00")
    if realized <= -halt:
        latch_desk_loss(conn, day, realized_usd=realized, limit_usd=halt, tripped_at=str(at_iso),
                        commit=commit)


def _desk_halt():
    from sniper.settings import memecoin as _cfg
    try:
        return float(_cfg.MEMECOIN_DESK_DAILY_LOSS_USD or 0.0)   # 0 or unset admits no desk entry
    except (TypeError, ValueError):
        return 0.0


def _desk_cap():
    from sniper.settings import memecoin as _cfg
    try:
        return int(_cfg.MEMECOIN_DESK_MAX_CONCURRENT or 0)   # 0 or unset admits no desk entry
    except (TypeError, ValueError):
        return 0


def lost_reservation_reason(conn, *, network_id, mint, signal_source, at_iso,
                            funded_contract_json=None):
    """The truthful label for a reservation INSERT that wrote nothing (Codex review). reserve_pending
    returns None for every refusal; for a desk reservation this names the desk gate that refused
    (halt, then cap). Callers read it BEFORE committing the reservation transaction, so it sees the
    state the INSERT saw. Never a provider call."""
    default = "one position per coin: lost reservation race"
    if signal_source != SIGNAL_SOURCE_DESK:
        return default
    meta = _desk_contract_metadata(funded_contract_json)
    refusal = desk_cohort_refusal(conn, cohort_id=meta.get("cohort_id") if meta else None,
                                  producer=meta.get("producer") if meta else None)
    if refusal:
        return refusal.replace("_", " ") + ": refused inside the reservation"
    day, halt = str(at_iso)[:10], _desk_halt()
    if (halt <= 0 or desk_loss_latched(conn, day)
            or desk_realized_pnl_since(conn, day + "T00:00:00+00:00") <= -halt):
        return "desk daily loss halt: refused inside the reservation"
    if desk_open_count(conn) >= _desk_cap():
        return "desk max concurrent: refused inside the reservation"
    return default


def desk_open_count(conn):
    """Open real desk positions plus reserved desk entries (a reservation may still fill)."""
    return int(conn.execute(
        "SELECT (SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
        "AND signal_source=?) + (SELECT COUNT(*) FROM memecoin_pending_entries "
        "WHERE status='reserved' AND signal_source=?)",
        (SIGNAL_SOURCE_DESK, SIGNAL_SOURCE_DESK)).fetchone()[0])


def desk_loss_latched(conn, day):
    return conn.execute("SELECT 1 FROM memecoin_desk_loss_trips WHERE day=?",
                        (day,)).fetchone() is not None


def latch_desk_loss(conn, day, *, realized_usd, limit_usd, tripped_at, commit=True):
    """Latch the desk halt for `day`. First write wins; it is never cleared by code. commit=False
    joins the caller's transaction (the reservation gate), which owns the commit or rollback."""
    conn.execute("INSERT OR IGNORE INTO memecoin_desk_loss_trips(day, tripped_at, realized_usd, "
                 "limit_usd) VALUES (?,?,?,?)", (day, tripped_at, float(realized_usd), float(limit_usd)))
    if commit:
        conn.commit()


def desk_realized_pnl_since(conn, since_iso):
    """Realized PnL of real desk closes at/after `since_iso`. Unknown PnL is not counted as 0:
    a close with NULL realized_pnl is excluded here and stays visible in the position view."""
    return float(conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0.0) FROM memecoin_positions WHERE execution_mode='real' "
        "AND is_open=0 AND signal_source=? AND realized_pnl IS NOT NULL AND closed_at>=?",
        (SIGNAL_SOURCE_DESK, since_iso)).fetchone()[0])


def reserve_pending(conn, *, mint, source_user_id, strategy, notional, reserved_at, network_id=None,
                    origin_event_id=None, attempt_id=None, signal_source=None, source_price=None,
                    source_market_cap=None, source_liquidity=None, source_token_age_s=None,
                    observed_at=None, clock_domain=None,
                    funded_contract_version=None, funded_contract_hash=None,
                    funded_contract_json=None, cluster_decision_id=None, commit=True,
                    funding_asset=None, funding_amount_atomic=None, funding_sol_usd=None,
                    funding_overhead_atomic=None):
    """INSERT a sig-less reserved row; returns id, or None if the exact coin is already reserved
    (partial UNIQUE(normalized network_id,mint)). This is the atomic coin-dedup guard.
    network_id tags the venue ('solana' | '8453' | ...) so the exit path routes to the right leg."""
    import sqlite3
    if funding_asset not in (None, "usdc", "sol"):
        raise ValueError("unsupported entry funding asset")
    if funding_asset == "sol":
        import math
        if funding_overhead_atomic is None:
            funding_overhead_atomic = 0
        if (network_id != "solana" or isinstance(funding_amount_atomic, bool)
                or not isinstance(funding_amount_atomic, int) or funding_amount_atomic <= 0
                or isinstance(funding_overhead_atomic, bool)
                or not isinstance(funding_overhead_atomic, int) or funding_overhead_atomic < 0
                or funding_sol_usd is None or not math.isfinite(float(funding_sol_usd))
                or float(funding_sol_usd) <= 0):
            raise ValueError("SOL funding needs positive atomic amount and price")
    elif (funding_amount_atomic is not None or funding_sol_usd is not None):
        raise ValueError("USDC funding cannot carry SOL amount or price")
    elif funding_overhead_atomic is not None and (
            network_id != "solana" or funding_asset != "usdc"
            or type(funding_overhead_atomic) is not int or funding_overhead_atomic < 0):
        # Explicit Solana USDC funding may carry its frozen SOL tip. Legacy NULL remains unknown;
        # zero is a proven no-tip arm. This does not widen other chains or funding assets.
        raise ValueError("USDC SOL overhead needs explicit Solana funding and nonnegative atomic liability")
    meta = (_desk_contract_metadata(funded_contract_json) if signal_source == SIGNAL_SOURCE_DESK else None)
    projections = tuple(meta.get(k) if meta else None for k in
                        ("cohort_id", "producer", "opportunity_id", "snapshot_hash"))
    known_group = bool(meta and meta.get('cohort_id') == 'known-group-launch-v1')
    if known_group:
        from sniper.services.memecoin_known_group_canary import _admission_refusal
        if notional != _KNOWN_GROUP_POLICY['clip_usd'] or _admission_refusal(conn, meta, mint=mint, now_iso=reserved_at):
            return None
    from sniper.settings import memecoin as _cfg
    if _cfg.MEMECOIN_DESK_COST_GATE_LEGACY:
        cohort_sql, cohort_bound = _DESK_COHORT_SQL_LEGACY, ()
    else:   # the bound is pure config arithmetic: no I/O on the reservation
        cohort_sql = _DESK_COHORT_SQL
        try:
            b = (desk_execution_cost_bound() if signal_source == SIGNAL_SOURCE_DESK
                 else {"usd": 0.0, "priority_lamports": 0})
            lim = b["priority_lamports"]
            cohort_bound = (lim, lim, lim, b["usd"], lim, lim, lim)
        except ValueError:      # a bad bound refuses every desk reservation, as the gate does; never raises here
            logger.warning("desk reservation: cost bound invalid; refusing", exc_info=True)
            return None
    try:
        cur = conn.execute(
            "INSERT INTO memecoin_pending_entries(mint, source_user_id, strategy, "
            "reserved_notional_usd, reserved_at, status, network_id, origin_event_id, attempt_id, "
            "signal_source, source_price, source_market_cap, source_liquidity, "
            "source_token_age_s, observed_at, clock_domain, "
            "funded_contract_version, funded_contract_hash, funded_contract_json, "
            "cluster_decision_id, funding_asset, funding_amount_atomic, funding_sol_usd, "
            "funding_overhead_atomic,desk_cohort_id,desk_producer,desk_opportunity_id,desk_snapshot_hash) "
            "SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,? "
            "WHERE NOT EXISTS (" + _CROSS_LANE_HOLD_SQL + ") AND " + _DESK_CAP_SQL
            + " AND " + _DESK_HALT_SQL + " AND " + cohort_sql
            + " AND (?=0 OR EXISTS(SELECT 1 FROM memecoin_known_group_candidates kg "
              "WHERE kg.candidate_id=? AND kg.cohort_id=? AND kg.proof_hash=? AND kg.eligible=1 "
              "AND json_extract(kg.proof_json,'$.service_catalogue')=? "
              "AND julianday(kg.received_at_utc)<=julianday(?) AND julianday(kg.deadline_utc)>julianday(?)))",
            (mint, source_user_id, strategy, float(notional), reserved_at, "reserved", network_id,
             origin_event_id, attempt_id, signal_source, source_price, source_market_cap,
             source_liquidity, source_token_age_s,
             observed_at, clock_domain, funded_contract_version, funded_contract_hash,
             funded_contract_json, cluster_decision_id, funding_asset,
             str(funding_amount_atomic) if funding_amount_atomic is not None else None,
             funding_sol_usd,
             str(funding_overhead_atomic) if funding_overhead_atomic is not None else None,
             *projections,
             normalized_coin_network(network_id, mint), mint, mint,
             1 if signal_source == SIGNAL_SOURCE_DESK else 0,
             1 if signal_source == SIGNAL_SOURCE_DESK else 0, _desk_cap(),
             1 if signal_source == SIGNAL_SOURCE_DESK else 0, str(reserved_at)[:10],
             str(reserved_at)[:10] + "T00:00:00+00:00", _desk_halt(), _desk_halt(),
             1 if signal_source == SIGNAL_SOURCE_DESK else 0, projections[0], projections[1], *cohort_bound,
             int(known_group),projections[2],projections[0],projections[3],_KNOWN_GROUP_SERVICE_JSON,reserved_at,reserved_at))
        if cur.rowcount == 0:
            # Another funded lane holds this coin (§2.5 R3), the desk cap is full, or the desk day
            # is halted. A desk day found tripped here is latched deliberately, inside the caller's
            # transaction when it owns one, so a later winner cannot reopen it.
            if commit:
                conn.rollback()
            if signal_source == SIGNAL_SOURCE_DESK:
                _latch_desk_day_if_tripped(conn, reserved_at, commit=False)
                if meta:
                    _latch_desk_cohort_if_tripped(conn, meta["cohort_id"], reserved_at)
                if commit:
                    conn.commit()
            return None
        if signal_source == "slot_lane" and attempt_id:
            conn.execute(
                "UPDATE memecoin_entry_attempts SET pending_id=? WHERE attempt_id=? "
                "AND terminal_status IS NULL AND pending_id IS NULL",
                (cur.lastrowid, attempt_id))
        if commit:
            conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        if commit:
            conn.rollback()
        return None


def _signed_entry_fee(payload_b64):
    if not payload_b64:
        return None
    from sniper.execution.solana_sender_ab import signed_fee_footprint_lamports
    return signed_fee_footprint_lamports(payload_b64)


def set_pending_sig(conn, pending_id, tx_sig, broadcast_at, signed_payload_b64=None):
    """Write-ahead the signed tx signature BEFORE broadcast so a crashed reservation is
    reconcilable (has-sig -> resolve against chain; no-sig+old -> provably never broadcast).
    The fee footprint of THESE bytes rides the same UPDATE (NULL when not given or unproven)."""
    conn.execute("UPDATE memecoin_pending_entries SET tx_sig=?, broadcast_at=?, entry_fee_ceiling_lamports=? "
                 "WHERE id=?", (tx_sig, broadcast_at, _signed_entry_fee(signed_payload_b64), pending_id))
    _link_slot_entry_signature(conn, pending_id)
    conn.commit()


def _link_slot_entry_signature(conn, pending_id):
    """Project the slot reservation's durable signature in the SAME write-ahead transaction."""
    conn.execute(
        "UPDATE memecoin_entry_attempts SET tx_sig=(SELECT tx_sig FROM memecoin_pending_entries WHERE id=?) "
        "WHERE attempt_id=(SELECT attempt_id FROM memecoin_pending_entries WHERE id=? AND signal_source='slot_lane') "
        "AND terminal_status IS NULL AND EXISTS (SELECT 1 FROM memecoin_pending_entries p "
        "WHERE p.id=? AND p.signal_source='slot_lane' AND p.attempt_id=memecoin_entry_attempts.attempt_id "
        "AND p.origin_event_id=memecoin_entry_attempts.origin_event_id)", (pending_id, pending_id, pending_id))


def set_pending_broadcast(conn, pending_id, tx_sig, nonce, broadcast_at):
    """EVM write-ahead: persist the tx hash AND the nonce before broadcast. The nonce is the EVM
    no-double-submit key — a crashed reservation with a known nonce is resolved by reading whether
    that nonce landed on-chain, and a resubmit would reuse the same nonce (rejected), never spend twice."""
    conn.execute("UPDATE memecoin_pending_entries SET tx_sig=?, nonce=?, broadcast_at=? WHERE id=?",
                 (tx_sig, nonce, broadcast_at, pending_id))
    conn.commit()


def bind_pending_cleanup(conn, pending_id, *, tx_sig, nonce, amount_atomic, payload_sha256,
                         signed_at):
    """Store the cleanup transaction identity before the network write."""
    cur = conn.execute(
        "UPDATE memecoin_pending_entries SET cleanup_tx_sig=?, cleanup_nonce=?, "
        "cleanup_amount_atomic=?, cleanup_signed_payload_sha256=?, cleanup_signed_at=?, "
        "cleanup_status='signed' WHERE id=? AND status='reserved' AND cleanup_tx_sig IS NULL",
        (tx_sig, nonce, str(amount_atomic), payload_sha256, signed_at, pending_id))
    conn.commit()
    return cur.rowcount == 1


def mark_pending_cleanup_status(conn, pending_id, status):
    conn.execute(
        "UPDATE memecoin_pending_entries SET cleanup_status=? WHERE id=?",
        (status, pending_id))
    conn.commit()


def bind_entry_submission(conn, pending_id, *, tx_identity, signed_payload_b64,
                          signed_payload_sha256, blockhash, last_valid_height, nonce,
                          backend, bound_at, replace_unbound=False,
                          evm_quote_asset_usd=None):
    """Bind one immutable signed identity to a live reservation before network I/O.

    Rebinding the exact same bytes is allowed for same-bytes replay. A pipelined pre-transaction may
    replace only its exact unbound identity with the swap identity. All bound identities stay fixed.
    """
    replace_from = str(replace_unbound) if replace_unbound else None
    if evm_quote_asset_usd is not None:
        price = float(evm_quote_asset_usd)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("invalid EVM quote asset USD price")
    cur = conn.execute(
        "UPDATE memecoin_pending_entries SET "
        "tx_sig=CASE WHEN signed_payload_sha256 IS NULL AND tx_sig=? "
        "THEN ? ELSE COALESCE(tx_sig,?) END, "
        "signed_payload_b64=COALESCE(signed_payload_b64,?), "
        "signed_payload_sha256=COALESCE(signed_payload_sha256,?), "
        "submission_blockhash=COALESCE(submission_blockhash,?), "
        "submission_last_valid_height=COALESCE(submission_last_valid_height,?), "
        "nonce=CASE WHEN signed_payload_sha256 IS NULL AND tx_sig=? "
        "THEN ? ELSE COALESCE(nonce,?) END, "
        "submission_backend=COALESCE(submission_backend,?), "
        "submission_bound_at=COALESCE(submission_bound_at,?), "
        "evm_quote_asset_usd=COALESCE(evm_quote_asset_usd,?), "
        "entry_fee_ceiling_lamports=CASE WHEN signed_payload_sha256 IS NULL THEN ? ELSE entry_fee_ceiling_lamports END, "
        "broadcast_at=CASE WHEN signed_payload_sha256 IS NULL AND tx_sig=? "
        "THEN ? ELSE COALESCE(broadcast_at,?) END "
        "WHERE id=? AND status='reserved' "
        "AND ((signed_payload_sha256 IS NULL AND "
        "(tx_sig IS NULL OR tx_sig=? OR (? IS NOT NULL AND tx_sig=?))) OR "
        "(signed_payload_sha256=? AND tx_sig=? "
        "AND evm_quote_asset_usd IS ?))",
        (replace_from, tx_identity, tx_identity, signed_payload_b64,
         signed_payload_sha256, blockhash, last_valid_height, replace_from, nonce, nonce,
         backend, bound_at, evm_quote_asset_usd, _signed_entry_fee(signed_payload_b64),
         replace_from, bound_at, bound_at, pending_id,
         tx_identity, replace_from, replace_from, signed_payload_sha256, tx_identity,
         evm_quote_asset_usd))
    if cur.rowcount == 1:
        _link_slot_entry_signature(conn, pending_id)
    conn.commit()
    return cur.rowcount == 1


def entry_submission_binding(conn, pending_id):
    """Read back the durable entry submission binding (recovery / audit). None if no row exists.
    The current recovery pass uses these bytes as identity evidence; it does not auto-rebroadcast."""
    r = conn.execute(
        "SELECT tx_sig, nonce, signed_payload_b64, signed_payload_sha256, submission_blockhash, "
        "submission_last_valid_height, submission_backend, submission_bound_at, "
        "evm_quote_asset_usd "
        "FROM memecoin_pending_entries WHERE id=?", (pending_id,)).fetchone()
    return dict(r) if r else None


def reset_entry_submission_after_failed_receipt(conn, pending_id, tx_identity, *, release_reason=None):
    """Clear the exact mined failure for retry, or release it in the same terminal transition."""
    cur = conn.execute(
        "UPDATE memecoin_pending_entries SET tx_sig=NULL, nonce=NULL, broadcast_at=NULL, "
        "signed_payload_b64=NULL, signed_payload_sha256=NULL, submission_blockhash=NULL, "
        "submission_last_valid_height=NULL, submission_backend=NULL, submission_bound_at=NULL, "
        "evm_quote_asset_usd=NULL, "
        "status=CASE WHEN ? IS NULL THEN status ELSE 'failed' END, "
        "resolution_reason=COALESCE(?,resolution_reason) "
        "WHERE id=? AND status='reserved' AND tx_sig=? "
        "RETURNING signal_source,desk_cohort_id",
        (release_reason, release_reason, pending_id, tx_identity),
    )
    changed = cur.fetchall()
    if changed and changed[0][0] == SIGNAL_SOURCE_DESK and changed[0][1]:
        conn.execute("INSERT OR IGNORE INTO memecoin_desk_cohort_costs "
                     "(cohort_id,event_id,kind,occurred_at,provenance) VALUES(?,?,'execution',?,'receipt:unpriced')",
                     (changed[0][1], f"pending:{pending_id}:{tx_identity}", real_utc_now_iso()))
    conn.commit()
    return len(changed) == 1


def record_submission_attempts(conn, *, chain_id, action, owner_kind, owner_id, tx_identity,
                               signed_payload_sha256, attempts, created_at):
    """Append the per-provider fanout attempts to the audit log (caller thread only; hashes, never
    the payload). Rows are keyed to the owning reservation/claim; execution authority stays with the
    reservation/claim/intent state. Returns the number of rows written."""
    def _text(value):
        return None if value is None else str(value)
    rows = []
    for a in attempts or ():
        state = getattr(a, "state", None)
        state = getattr(state, "value", state)
        rows.append((str(chain_id), str(action), _text(owner_kind), _text(owner_id),
                     _text(tx_identity), _text(signed_payload_sha256),
                     _text(getattr(a, "provider_id", None)), _text(state),
                     getattr(a, "latency_ms", None), _text(getattr(a, "response_code", None)),
                     _text(getattr(a, "reason_code", None)), created_at))
    if not rows:
        return 0
    try:
        conn.executemany(
            "INSERT INTO memecoin_submission_attempts "
            "(chain_id, action, owner_kind, owner_id, tx_identity, signed_payload_sha256, "
            "provider_id, state, latency_ms, response_code, reason_code, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def recent_submission_attempts(conn, limit=2000):
    """The most recent submission attempts (newest first) for the sanitized status surface — provider
    label, state, reason_code, and latency only. Never returns a URL or payload (the audit table
    stores neither). Read-only; adds no authority."""
    rows = conn.execute(
        "SELECT provider_id, state, reason_code, latency_ms "
        "FROM memecoin_submission_attempts ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def pending_row(conn, pending_id):
    """The reserved entry's row (mint/source_user_id/strategy/status/...) — the real executor
    recovers source_user_id + strategy from here so the per-entry call contract stays (conn, pid,
    mint, usd, now_iso)."""
    r = conn.execute("SELECT * FROM memecoin_pending_entries WHERE id=?", (pending_id,)).fetchone()
    return dict(r) if r else None


def load_evm_slot_proof(conn, *, network_id, mint, spender):
    """Read one validated storage layout for a chain, mint, and approval spender."""
    row = conn.execute(
        "SELECT balance_slot,allowance_slot FROM memecoin_evm_slot_proofs "
        "WHERE network_id=? AND mint=? AND spender=?",
        (str(network_id), str(mint).lower(), str(spender).lower()),
    ).fetchone()
    return (row["balance_slot"], row["allowance_slot"]) if row else None


def store_evm_slot_proof(conn, *, network_id, mint, spender, proof):
    """Store one positive storage proof. This does not store a safety verdict."""
    try:
        balance_slot, allowance_slot = proof
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid EVM slot proof") from exc
    if not all(isinstance(slot, str) and slot.startswith("0x") and len(slot) == 66
               for slot in (balance_slot, allowance_slot)):
        raise ValueError("invalid EVM slot proof")
    conn.execute(
        "INSERT INTO memecoin_evm_slot_proofs("
        "network_id,mint,spender,balance_slot,allowance_slot) VALUES (?,?,?,?,?) "
        "ON CONFLICT(network_id,mint,spender) DO UPDATE SET "
        "balance_slot=excluded.balance_slot,allowance_slot=excluded.allowance_slot",
        (str(network_id), str(mint).lower(), str(spender).lower(), balance_slot, allowance_slot),
    )
    conn.commit()


PREAPPROVAL_COLUMNS = ("position_id", "network_id", "token", "spender", "amount_atomic", "nonce",
                       "tx_hash", "send_status", "created_at")


def record_evm_preapproval(conn, row):
    """Insert one pre-approval attempt and return its id. Missing keys are NULL, never zero."""
    cur = conn.execute(
        "INSERT INTO memecoin_evm_preapprovals (%s) VALUES (%s)"
        % (",".join(PREAPPROVAL_COLUMNS), ",".join("?" * len(PREAPPROVAL_COLUMNS))),
        tuple(row.get(col) for col in PREAPPROVAL_COLUMNS))
    conn.commit()
    return cur.lastrowid


def resolve_evm_preapproval(conn, row_id, *, receipt_status, gas_used, effective_gas_price_wei,
                            resolved_at):
    conn.execute(
        "UPDATE memecoin_evm_preapprovals SET receipt_status=?, gas_used=?, "
        "effective_gas_price_wei=?, resolved_at=? WHERE id=?",
        (receipt_status, gas_used, effective_gas_price_wei, resolved_at, row_id))
    conn.commit()


def stale_reserved_rows(conn, cutoff_iso):
    """Reservations still 'reserved' and created before cutoff_iso — candidates for chain-recovery
    (an unknown-confirm or a crash mid-executor leaves these blocking the mint + eating exposure)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_pending_entries WHERE status='reserved' AND reserved_at < ?",
        (cutoff_iso,))]


def mark_pending_orphaned(conn, pending_id):
    """Tag a confirmed-but-position-less reservation (funds at risk) so it alerts once and STAYS
    reserved (still holds the coin slot + exposure) until the operator manually reconciles."""
    conn.execute("UPDATE memecoin_pending_entries SET recovery_status='orphaned' WHERE id=?",
                 (pending_id,))
    conn.commit()


def mark_pending_recovery_status(conn, pending_id, status):
    """Persist a non-terminal recovery classification without freeing exposure."""
    conn.execute("UPDATE memecoin_pending_entries SET recovery_status=? WHERE id=?",
                 (status, pending_id))
    conn.commit()


def record_pending_recovery_check(conn, pending_id, status, checked_at, min_spacing_s=60.0):
    """Persist explicit Solana expiry evidence. Only spaced `not_found` responses increment;
    `pending` proves the signature exists and resets the expiry proof. Unavailable responses do
    not mutate proof state."""
    row = pending_row(conn, pending_id)
    if not row:
        return {}
    if status == "pending":
        conn.execute(
            "UPDATE memecoin_pending_entries SET not_found_checks=0, first_not_found_at=NULL, "
            "last_recovery_check_at=NULL WHERE id=?",
            (pending_id,),
        )
        conn.commit()
        return pending_row(conn, pending_id)
    if status != "not_found":
        return row
    try:
        checked_dt = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        last_raw = row.get("last_recovery_check_at")
        last_dt = datetime.fromisoformat(str(last_raw).replace("Z", "+00:00")) if last_raw else None
    except (TypeError, ValueError):
        return row
    if last_dt is not None and (checked_dt - last_dt).total_seconds() < float(min_spacing_s):
        return row
    count = int(row.get("not_found_checks") or 0) + 1
    first = row.get("first_not_found_at") or checked_at
    conn.execute(
        "UPDATE memecoin_pending_entries SET not_found_checks=?, first_not_found_at=?, "
        "last_recovery_check_at=? WHERE id=?",
        (count, first, checked_at, pending_id),
    )
    conn.commit()
    return pending_row(conn, pending_id)


def confirm_pending(conn, pending_id, commit=True):
    cur = conn.execute(
        "UPDATE memecoin_pending_entries SET status='confirmed', recovery_status=NULL "
        "WHERE id=? AND status='reserved'",
        (pending_id,),
    )
    if commit:
        conn.commit()
    return cur.rowcount == 1


def finalize_pending_entry(conn, *, pending_id, position_kwargs, fill_kwargs):
    """Atomically store one confirmed entry position, fill, and reservation state."""
    if conn.in_transaction:
        raise RuntimeError("entry finalization needs a clean transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        position_id = open_position(conn, **dict(position_kwargs), commit=False)
        fill_id = insert_fill(
            conn, position_id=position_id, **dict(fill_kwargs), commit=False)
        if not confirm_pending(conn, pending_id, commit=False):
            raise RuntimeError("pending reservation did not confirm")
        conn.commit()
        return position_id, fill_id
    except Exception:
        conn.rollback()
        raise


def clear_pending_signed_payload(conn, pending_id, commit=False):
    """Destroy the raw signed bytes of a reservation so nothing can ever put them on the wire.

    An EVM signature stays valid until its nonce is consumed, so a row released while its payload
    is still on disk can be resurrected -- by a later code path, a recovery script, or a human --
    and would buy a coin no ledger row expects. The hash is kept for audit; only the broadcastable
    form goes. Callers release in the SAME transaction, so this defaults to not committing.
    """
    conn.execute("UPDATE memecoin_pending_entries SET signed_payload_b64=NULL WHERE id=?",
                 (pending_id,))
    if commit:
        conn.commit()


def release_pending(conn, pending_id, reason, commit=True):
    """Free a reservation on CONFIRMED failure only (never on timeout — that stays reserved,
    fail-closed). The row is kept for audit.

    FIRST WRITER WINS (peer review 2026-08-25). The UPDATE was unconditional, so a second caller
    could overwrite `resolution_reason` on an already-released row. Recovery can release a row while
    its executor is still in flight; that executor then reaches its own release with a reason
    describing what IT saw, and the audit column silently became the later, less true one --
    'dropped_blockhash_expired_proven' replaced by 'broadcast_rejected'. Both callers are honest; the
    FIRST one is the one that actually freed the slot. The `status='reserved'` guard makes the column
    say what really happened. A second release is then a no-op, which is correct: the row is already
    terminal with a reason. Nothing branches on the rowcount, so no caller changes behaviour."""
    cur = conn.execute(
        "UPDATE memecoin_pending_entries SET status='failed', resolution_reason=? "
        "WHERE id=? AND status='reserved'",
        (reason, pending_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount == 1


def _unsigned_reservation_where():
    """Select reservations with no signature or submission metadata."""
    return """status='reserved'
        AND tx_sig IS NULL AND broadcast_at IS NULL AND nonce IS NULL
        AND signed_payload_b64 IS NULL AND signed_payload_sha256 IS NULL
        AND submission_blockhash IS NULL AND submission_last_valid_height IS NULL
        AND submission_backend IS NULL AND submission_bound_at IS NULL
        AND cleanup_tx_sig IS NULL AND cleanup_nonce IS NULL
        AND cleanup_amount_atomic IS NULL AND cleanup_status IS NULL
        AND cleanup_signed_at IS NULL AND cleanup_signed_payload_sha256 IS NULL"""


def release_unsigned_reservations_after_runner_stop(conn):
    """Release only rows that cannot have reached signing before a proven runner stop."""
    import sqlite3

    where = _unsigned_reservation_where()
    try:
        rows = conn.execute(f"SELECT id FROM memecoin_pending_entries WHERE {where}").fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: memecoin_pending_entries" in str(exc):
            return []
        raise
    released = []
    for row in rows:
        cur = conn.execute(
            "UPDATE memecoin_pending_entries SET status='failed', "
            "resolution_reason='runner_stopped_before_signing' "
            f"WHERE id=? AND {where}",
            (row[0],),
        )
        if cur.rowcount == 1:
            lifecycle = conn.execute(
                "SELECT lifecycle_id,state FROM memecoin_lifi_authority_events "
                "WHERE pending_id=? ORDER BY sequence DESC LIMIT 1",
                (row[0],),
            ).fetchone()
            if lifecycle is not None and lifecycle["state"] == "reserved":
                append_lifi_event(
                    conn, lifecycle["lifecycle_id"], expected_state="reserved",
                    new_state="entry_failed", event_type="entry_failed", stage="entry",
                    created_at=real_utc_now_iso(),
                    detail={"reason": "runner_stopped_before_signing"}, commit=False,
                )
            released.append(int(row[0]))
    if released:
        conn.commit()
    return released


def record_jupiter_load(conn, rows):
    """Accumulate per-minute Jupiter load buckets. ADDITIVE upsert, never a replace.

    A restart mid-minute produces a second partial bucket for the same key; `+ excluded` sums
    them instead of letting the later write erase the earlier one. That is the exact data loss
    this table exists to prevent.
    """
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO memecoin_jupiter_load "
        "(ts_minute, role, host, quotes, host_429, rate_limited, errors, auth_failed) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ts_minute, role, host) DO UPDATE SET "
        "  quotes = quotes + excluded.quotes, "
        "  host_429 = host_429 + excluded.host_429, "
        "  rate_limited = rate_limited + excluded.rate_limited, "
        "  errors = errors + excluded.errors, "
        "  auth_failed = auth_failed + excluded.auth_failed",
        [(r.get("ts_minute"), r.get("role"), r.get("host"), int(r.get("quotes") or 0),
          int(r.get("host_429") or 0), int(r.get("rate_limited") or 0),
          int(r.get("errors") or 0), int(r.get("auth_failed") or 0)) for r in rows])
    conn.commit()
    return len(rows)


def entry_attempt_notional(conn, attempt_id):
    """Read the exact reservation size for one entry attempt."""
    row = conn.execute(
        "SELECT p.reserved_notional_usd FROM memecoin_entry_attempts a "
        "JOIN memecoin_pending_entries p ON p.id=a.pending_id AND p.attempt_id=a.attempt_id "
        "WHERE a.attempt_id=?", (attempt_id,)).fetchone()
    return row[0] if row else None


def max_route_series_id(conn):
    """The newest memecoin_route_series id, 0 on an empty table."""
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM memecoin_route_series").fetchone()[0])


def latest_exit_decision_quote(conn, position_id):
    """Newest retained whole-bag decision observation; caller validates identity, size and both clocks."""
    row = conn.execute(
        "SELECT * FROM memecoin_route_series WHERE position_id=? AND seam='exit_decision' "
        "ORDER BY observed_at DESC, id DESC LIMIT 1", (int(position_id),)).fetchone()
    return dict(row) if row else None


def exit_decision_series_after(conn, network_id, after_id, limit=500):
    """Exit-decision quotes of REAL positions on one chain with id > after_id, oldest first, each
    with its position's stored entry route. Read-only; the exit-venue shadow's cursor read."""
    return [dict(r) for r in conn.execute(
        "SELECT s.id AS series_id, s.position_id, s.mint, s.network_id, s.quoted_at, "
        "s.observed_at, s.quote_evidence_json, p.evm_route_json "
        "FROM memecoin_route_series s JOIN memecoin_positions p ON p.id = s.position_id "
        "WHERE s.id > ? AND s.seam = 'exit_decision' AND s.network_id = ? "
        "AND p.execution_mode = 'real' ORDER BY s.id LIMIT ?",
        (int(after_id), str(network_id), int(limit)))]


def closed_position_ids(conn, position_ids):
    """Which of these positions are no longer open. Read-only; a primary-key lookup."""
    ids = [int(i) for i in position_ids]
    if not ids:
        return []
    return [int(r[0]) for r in conn.execute(
        "SELECT id FROM memecoin_positions WHERE is_open = 0 AND id IN (%s) ORDER BY id"
        % ",".join("?" * len(ids)), ids)]


def _route_price(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def record_route_series(conn, rows, gaps=(), *, on_lock=None):
    """Append route observations and gap rows in one transaction. Append-only.

    A price row without a positive finite expected_usdc is refused here, the last line before the
    table, whatever path built it. On an autocommit connection (the writer thread's) the
    transaction is one explicit BEGIN IMMEDIATE ... COMMIT, timed for `on_lock(wait_ms, hold_ms)`;
    on a default connection (the maintenance flush) it is the implicit transaction and commit."""
    rows = [r for r in rows if _route_price(r.get("expected_usdc"))]
    gaps = list(gaps)
    if not rows and not gaps:
        return 0
    own = conn.isolation_level is None
    t0 = time.perf_counter()
    if own:
        conn.execute("BEGIN IMMEDIATE")
    t1 = time.perf_counter()
    try:
        if rows:
            conn.executemany(
                "INSERT INTO memecoin_route_series "
                "(position_id, mint, network_id, seam, observed_at, quoted_at, expected_usdc, "
                " open_qty, unit_price, quote_evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(r.get("position_id"), r.get("mint"), r.get("network_id"), r.get("seam"),
                  r.get("observed_at"), r.get("quoted_at"), float(r.get("expected_usdc")),
                  r.get("open_qty"), r.get("unit_price"), r.get("quote_evidence_json"))
                 for r in rows])
        if gaps:
            conn.executemany(
                "INSERT INTO memecoin_route_series_gaps "
                "(position_id, network_id, mint, seam, kind, reason, observed_at, last_observed_at, "
                " count) VALUES (?,?,?,?,?,?,?,?,?)",
                [(g.get("position_id"), g.get("network_id"), g.get("mint"), g.get("seam"),
                  g.get("kind"), g.get("reason"), g.get("observed_at"), g.get("last_observed_at"),
                  int(g.get("count") or 1)) for g in gaps])
        if own:
            conn.execute("COMMIT")
        else:
            conn.commit()
    except Exception:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if on_lock is not None:
        t2 = time.perf_counter()
        on_lock((t1 - t0) * 1000.0, (t2 - t1) * 1000.0)
    return len(rows)


def open_real_position_keys(conn):
    """(id, network_id, mint) of every open real position, for the route series restart marker."""
    return [{"id": r[0], "network_id": r[1], "mint": r[2]} for r in conn.execute(
        "SELECT id, network_id, mint FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=1 ORDER BY id")]


EXEC_EXTREMES_BASIS = "exact_route:exit_decision"
_EXEC_EXTREMES_SERIES = ("FROM memecoin_route_series WHERE position_id=? AND seam='exit_decision' "
                         "AND unit_price > 0")
_EXEC_EXTREMES_COLUMNS = ("position_id", "high_price", "high_at", "low_price", "low_at", "samples",
                          "first_at", "last_at", "basis")


def exec_extremes_live(conn, position_id):
    """The executable top and bottom of one position from its `exit_decision` route series, or
    None when the series holds no positive-price sample.

    `unit_price` is the whole-bag sell quote per token, net of our own exit impact and fees. Only
    seam `exit_decision` counts: `exit_audit`, `peak_preview` and `equity_mark` price other moments
    and quantities. `high_at` / `low_at` are the FIRST sample at that price. Three reads on
    idx_mc_route_series_pos; read-only, so it serves an open position too."""
    agg = conn.execute(
        "SELECT MAX(unit_price), MIN(unit_price), COUNT(*), MIN(observed_at), MAX(observed_at) "
        + _EXEC_EXTREMES_SERIES, (position_id,)).fetchone()
    if not agg or not agg[2]:
        return None
    first_at = {}
    for name, price in (("high", agg[0]), ("low", agg[1])):
        first_at[name] = conn.execute(
            "SELECT MIN(observed_at) " + _EXEC_EXTREMES_SERIES + " AND unit_price=?",
            (position_id, price)).fetchone()[0]
    return {"position_id": position_id, "high_price": agg[0], "high_at": first_at["high"],
            "low_price": agg[1], "low_at": first_at["low"], "samples": agg[2],
            "first_at": agg[3], "last_at": agg[4], "basis": EXEC_EXTREMES_BASIS}


def store_exec_extremes(conn, live, computed_at):
    """Write one computed extremes row (the dict `exec_extremes_live` returns). A pure write."""
    conn.execute(
        "INSERT INTO memecoin_position_exec_extremes (%s, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(position_id) DO UPDATE SET high_price=excluded.high_price, "
        "high_at=excluded.high_at, low_price=excluded.low_price, low_at=excluded.low_at, "
        "samples=excluded.samples, first_at=excluded.first_at, last_at=excluded.last_at, "
        "basis=excluded.basis, computed_at=excluded.computed_at" % ", ".join(_EXEC_EXTREMES_COLUMNS),
        (*(live[c] for c in _EXEC_EXTREMES_COLUMNS), computed_at))


def upsert_exec_extremes(conn, position_id, computed_at):
    """Compute and store one position's executable extremes. True when a row was written; False,
    and nothing stored, when the series has no positive-price sample. Does not commit."""
    return backfill_exec_extremes(conn, [position_id], computed_at) == 1


def backfill_exec_extremes(conn, position_ids, computed_at):
    """Compute, then store, the extremes of several positions. Returns the rows written. Does not
    commit. EVERY READ COMES BEFORE THE FIRST WRITE: the ledger has one writer at a time, the
    funded exit and entry paths write to it, and the first write takes the lock until the caller
    commits. Reading inside that window would hold the lock for the reads too."""
    lives = [exec_extremes_live(conn, position_id) for position_id in position_ids]
    for live in lives:
        if live is not None:
            store_exec_extremes(conn, live, computed_at)
    return sum(live is not None for live in lives)


_FUNDED_DISPLAY_SET = ("WHERE id=? AND execution_mode='real' AND is_open=0 "
                       "AND (max_gain_pct IS NOT NULL OR time_to_peak_s IS NOT NULL)")


def clear_funded_display_extremes(conn, position_id, copied_at):
    """Set the display-derived `max_gain_pct` / `time_to_peak_s` to NULL on one CLOSED funded row.
    The old values are copied to `memecoin_position_display_extremes_backup` first, in the caller's
    transaction, so every clear can be undone; the first copy of a position is never overwritten.
    True when the row held a value. A paper or open row never matches. Does not commit."""
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_position_display_extremes_backup "
        "(position_id, old_max_gain_pct, old_time_to_peak_s, copied_at) "
        "SELECT id, max_gain_pct, time_to_peak_s, ? FROM memecoin_positions " + _FUNDED_DISPLAY_SET,
        (copied_at, position_id))
    return conn.execute(
        "UPDATE memecoin_positions SET max_gain_pct=NULL, time_to_peak_s=NULL "
        + _FUNDED_DISPLAY_SET, (position_id,)).rowcount > 0


def exec_extremes_pass(conn, now_iso, *, limit=20):
    """One bounded pass over RECENT funded closes. Returns {visited, written, cleared}.

    Order under the cap: every funded position closed in the last 10 minutes, newest first
    (recomputed each pass, because the route-series flush can land after the close); then funded
    positions closed in the last 24 hours that have no row, newest first (a restart gap). Older
    rows belong to the operator's history script.

    Every visited position loses its display-derived columns, with or without a series. The
    cutoffs are ISO 'T' strings built here, never SQLite datetime('now'), whose ' ' separator
    sorts below every 'T' stamp of the same day.

    THE WRITE LOCK. Every read comes first. BEGIN IMMEDIATE then takes the ledger's write lock, and
    the one transaction holds it for the writes and the commit only. When another writer holds the
    lock past this connection's busy timeout, the pass writes nothing and returns busy=True; the
    next pass does the work. `lock_wait_ms` is the time spent WAITING for the lock (it delays
    nobody); `write_ms` is the time the lock was HELD, which is what another writer can wait on.
    A pass with nothing to visit takes no lock."""
    import time
    from datetime import datetime, timedelta
    started = time.perf_counter()
    now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    recent, day = ((now - timedelta(**span)).isoformat() for span in ({"minutes": 10}, {"hours": 24}))
    closed = "FROM memecoin_positions p WHERE p.execution_mode='real' AND p.is_open=0 AND p.closed_at "
    ids = [r[0] for r in conn.execute(
        "SELECT p.id " + closed + ">= ? ORDER BY p.closed_at DESC, p.id DESC LIMIT ?",
        (recent, int(limit)))]
    if len(ids) < int(limit):
        ids += [r[0] for r in conn.execute(
            "SELECT p.id " + closed + ">= ? AND p.closed_at < ? AND NOT EXISTS ("
            "SELECT 1 FROM memecoin_position_exec_extremes x WHERE x.position_id=p.id) "
            "ORDER BY p.closed_at DESC, p.id DESC LIMIT ?", (day, recent, int(limit) - len(ids)))]
    lives = [exec_extremes_live(conn, position_id) for position_id in ids]
    read_done = time.perf_counter()
    out = {"visited": len(ids), "written": 0, "cleared": 0, "busy": False,
           "read_ms": (read_done - started) * 1000.0, "lock_wait_ms": 0.0, "write_ms": 0.0}
    if not ids:
        return out                                   # nothing to write: no lock, no transaction
    locked = None
    try:
        conn.execute("BEGIN IMMEDIATE")              # take the write lock now; waits the busy timeout at most
        locked = time.perf_counter()
        for position_id, live in zip(ids, lives):
            if live is not None:
                store_exec_extremes(conn, live, now_iso)
                out["written"] += 1
            out["cleared"] += clear_funded_display_extremes(conn, position_id, now_iso)
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        out["busy"] = True                           # another writer holds the ledger; nothing was written
    done = time.perf_counter()
    out["lock_wait_ms"] = ((locked or done) - read_done) * 1000.0
    out["write_ms"] = (done - locked) * 1000.0 if locked is not None else 0.0
    return out


def exec_extremes_history_targets(conn):
    """What the operator's history script would do, as counts and position ids. Read-only.

    `to_backfill`: closed funded positions with a positive-price exit_decision sample and no
    extremes row. `to_clear`: closed funded rows that hold a display-derived value. `no_series`:
    closed funded positions with no positive-price sample (they get no extremes row, ever)."""
    closed = "FROM memecoin_positions p WHERE p.execution_mode='real' AND p.is_open=0"
    series = ("EXISTS (SELECT 1 FROM memecoin_route_series s WHERE s.position_id=p.id "
              "AND s.seam='exit_decision' AND s.unit_price > 0)")
    has_row = "EXISTS (SELECT 1 FROM memecoin_position_exec_extremes x WHERE x.position_id=p.id)"

    def count(where="1"):
        return conn.execute("SELECT COUNT(*) %s AND %s" % (closed, where)).fetchone()[0]

    def ids(where):
        return [r[0] for r in conn.execute("SELECT p.id %s AND %s ORDER BY p.id" % (closed, where))]
    return {
        "closed_funded": count(),
        "extremes_rows": count(has_row),
        "to_backfill": ids(series + " AND NOT " + has_row),
        "no_series": count("NOT " + series),
        "to_clear": ids("(p.max_gain_pct IS NOT NULL OR p.time_to_peak_s IS NOT NULL)"),
        "backed_up": display_extremes_backup_count(conn),
    }


def display_extremes_backup_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM memecoin_position_display_extremes_backup").fetchone()[0]


def restore_funded_display_extremes(conn):
    """Copy every backed-up display value back onto its closed funded row. Returns the number of
    rows written. The backup table is left in place. Does not commit."""
    backup = "(SELECT %s FROM memecoin_position_display_extremes_backup b WHERE b.position_id=memecoin_positions.id)"
    return conn.execute(
        "UPDATE memecoin_positions SET max_gain_pct=%s, time_to_peak_s=%s "
        "WHERE execution_mode='real' AND is_open=0 AND id IN "
        "(SELECT position_id FROM memecoin_position_display_extremes_backup)"
        % (backup % "old_max_gain_pct", backup % "old_time_to_peak_s")).rowcount


def funded_exec_extremes(conn, position_ids):
    """{position_id: extremes row} for the given positions; a position with no row is absent.
    The one reader for research and grades."""
    ids, out = [int(i) for i in position_ids], {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute(
                "SELECT %s, computed_at FROM memecoin_position_exec_extremes WHERE position_id IN (%s)"
                % (", ".join(_EXEC_EXTREMES_COLUMNS), ",".join("?" for _ in chunk)), chunk):
            out[row[0]] = dict(zip((*_EXEC_EXTREMES_COLUMNS, "computed_at"), row))
    return out


def exec_extremes_pct(row, entry_price, *, opened_at=None):
    """Percent against the fill, derived at read time: {high_pct, low_pct, time_to_high_s,
    time_to_low_s}; every value None when it cannot be known.

    `entry_price` is the fill cost per token; the extremes are whole-bag SELL quotes per token, net
    of our own exit impact and fees. So a fresh position reads below zero by its round-trip cost,
    and the best executable gain of a hold can be negative. Pass `opened_at` as the fill stamp
    (`opened_recorded_at`) to get the two durations."""
    from datetime import datetime
    out = {"high_pct": None, "low_pct": None, "time_to_high_s": None, "time_to_low_s": None}
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        return out
    if not row or not entry > 0:
        return out
    for name in ("high", "low"):
        if row.get(name + "_price") is not None:
            out[name + "_pct"] = (float(row[name + "_price"]) / entry - 1.0) * 100.0
        try:
            out["time_to_%s_s" % name] = (
                datetime.fromisoformat(str(row[name + "_at"]).replace("Z", "+00:00"))
                - datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))).total_seconds()
        except (KeyError, TypeError, ValueError):
            pass
    return out


def record_volume_death_shadow(conn, *, position_id, mint, fired_at, silence_s, thresholds,
                               mark_price, stream_healthy, blind_frozen, observable=None):
    """Append one would-fire observation. Append-only; this rule is not armed and writes no state
    any decision reads."""
    conn.execute(
        "INSERT INTO memecoin_volume_death_shadow "
        "(position_id, mint, fired_at, silence_s, thresholds, top_threshold_s, mark_price, "
        " stream_healthy, blind_frozen, observable) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (int(position_id), mint, fired_at, silence_s,
         ",".join(str(x) for x in (thresholds or [])),
         (max(thresholds) if thresholds else None), mark_price,
         1 if stream_healthy else 0, 1 if blind_frozen else 0,
         (None if observable is None else (1 if observable else 0))))
    conn.commit()


def record_revival_watch(conn, *, origin_event_id, mint, network_id, refused_at, refused_mcap,
                         refused_need, outcome, detail, polls, finished_at):
    """Append one finished revival watch. Append-only; a watch ends exactly once."""
    conn.execute(
        "INSERT INTO memecoin_revival_watches "
        "(origin_event_id, mint, network_id, refused_at, refused_mcap, refused_need, outcome, "
        " detail, polls, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (origin_event_id, str(mint), network_id, refused_at, refused_mcap, refused_need,
         str(outcome), detail, polls, finished_at))
    conn.commit()


def record_revival_watch_poll(conn, *, origin_event_id, mint, network_id, poll_index, polled_at,
                              usd, reason, coverage, covered_s, measured_at, allowed,
                              decision_reason, source_age_s, covered_gap_s=None):
    """Append one poll's on-chain bound and the decision it fed. Append-only; every poll of
    `revival_watch_volume_cleared()` writes exactly one row here, in addition to whatever
    `record_revival_watch` writes once the watch concludes."""
    conn.execute(
        "INSERT INTO memecoin_revival_watch_polls "
        "(origin_event_id, mint, network_id, poll_index, polled_at, usd, reason, coverage, "
        " covered_s, measured_at, allowed, decision_reason, source_age_s, covered_gap_s) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (origin_event_id, str(mint), network_id, int(poll_index), polled_at, usd, reason,
         coverage, covered_s, measured_at, 1 if allowed else 0, decision_reason, source_age_s,
         covered_gap_s))
    conn.commit()


def revival_watch_tally(conn, *, since_iso):
    """Counts by outcome since `since_iso` — the arm's own scoreboard."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT outcome, COUNT(*) FROM memecoin_revival_watches WHERE finished_at>=? "
        "GROUP BY outcome", (since_iso,))}


POST_TP_HORIZONS_S = (60, 300, 900, 1800)


def schedule_post_tp_followups(conn, *, position_id, network_id, mint, closed_at,
                               token_qty_atomic, token_decimals, exit_usdc, now_iso,
                               horizons=POST_TP_HORIZONS_S):
    """Create the follow-up schedule for one closed take-profit. Idempotent per (position, horizon).

    NEVER BACKFILLED. A quote taken today cannot reconstruct the executable route of a past exit,
    so only closes seen after this lands get schedules (the V6 post-exit doctrine, 2026-07-27).
    """
    from datetime import datetime, timedelta
    try:
        anchor = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0
    written = 0
    for horizon in horizons:
        fid = "%s:%s" % (position_id, int(horizon))
        cur = conn.execute(
            "INSERT OR IGNORE INTO memecoin_post_tp_schedules "
            "(followup_id, position_id, network_id, mint, horizon_s, due_at, closed_at, "
            " token_qty_atomic, token_decimals, exit_usdc, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, int(position_id), str(network_id or ""), str(mint), int(horizon),
             (anchor + timedelta(seconds=int(horizon))).isoformat(), str(closed_at),
             str(token_qty_atomic), token_decimals, exit_usdc, now_iso))
        written += cur.rowcount or 0
    conn.commit()
    return written


def due_post_tp_followups(conn, now_iso, *, limit=1, horizons=None):
    """Schedules past due with no result yet, oldest first. Default one at a time so an old backlog
    cannot flood the quote provider. `horizons` limits the queue to those horizons, so one
    instrument never drains another's backlog."""
    only = ""
    args = [now_iso]
    if horizons:
        only = " AND s.horizon_s IN (%s)" % ",".join("?" * len(horizons))
        args.extend(int(h) for h in horizons)
    return [dict(r) for r in conn.execute(
        "SELECT s.* FROM memecoin_post_tp_schedules s "
        "LEFT JOIN memecoin_post_tp_results r ON r.followup_id=s.followup_id "
        "WHERE r.followup_id IS NULL AND s.due_at <= ?" + only +
        " ORDER BY s.due_at LIMIT ?", (*args, int(limit)))]


def record_post_tp_result(conn, *, followup_id, position_id, horizon_s, status, observed_at,
                          elapsed_s=None, quote_out_usdc=None, unit_price=None, detail=None):
    """Write one IMMUTABLE follow-up result. Returns False if one already exists.

    A restart may retry an unfinished schedule; it may never overwrite a recorded observation.
    `elapsed_s` is the ACTUAL age at observation, not the target horizon — a late sample must be
    reported by when it really happened.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_post_tp_results "
        "(followup_id, position_id, horizon_s, status, observed_at, elapsed_s, quote_out_usdc, "
        " unit_price, detail) VALUES (?,?,?,?,?,?,?,?,?)",
        (followup_id, int(position_id), int(horizon_s), str(status), observed_at,
         elapsed_s, quote_out_usdc, unit_price, detail))
    conn.commit()
    return bool(cur.rowcount)


# THE FINAL CLOSE, NOT EVERY EXIT FILL (2026-09-20, BR4). Both queries below joined every fill
# whose kind contains `exit`. That is a ONE-TO-MANY join with no ordering: a position with a
# partial returns BOTH rows and whichever the consumer reads first wins. Position 44568 returns
# `partial_exit` 2,444,816.78 tokens for $32.00 AND `bracket_exit` the same quantity for $44.87,
# six minutes apart.
#
# Measured read-only on the funded ledger 2026-09-20: 1,350 of 35,800 positions carry more than
# one exit fill (1,474 `partial_exit` rows). The chronologically last fill is `bracket_exit` on
# 995 and `mirror_exit` on 355, and `partial_exit` on ZERO of them, so binding to the last exit
# fill by (created_at, id) is the confirmed final close.


def recent_real_tp_closes(conn, *, since_iso, limit=50, exit_reasons=("real_tp",)):
    """Funded closes for `exit_reasons` (take-profit by default) at/after `since_iso`, with the exit
    fill quantity.

    Scanning after the fact keeps this collector entirely off the exit path: nothing here can delay,
    alter, or fail a real exit.
    """
    return [dict(r) for r in conn.execute(
        "SELECT p.id AS position_id, p.mint, p.closed_at, "
        "       " + coin_network_sql("p.") + " AS network_id, "
        "       f.qty AS exit_qty, f.notional AS exit_usdc "
        "FROM memecoin_positions p "
        "JOIN memecoin_fills f ON f.id = ("
        "    SELECT x.id FROM memecoin_fills x "
        "    WHERE x.position_id = p.id AND x.kind LIKE '%exit%' "
        "    ORDER BY x.created_at DESC, x.id DESC LIMIT 1) "
        "WHERE p.execution_mode='real' AND p.is_open=0 "
        "  AND p.exit_reason IN (" + ",".join("?" * len(exit_reasons)) + ") "
        "  AND p.closed_at >= ? "
        "ORDER BY p.closed_at LIMIT ?", (*exit_reasons, since_iso, int(limit)))]


def recent_real_tp_closes_with_principal(conn, *, since_iso, limit=50):
    """Funded take-profit closes at/after `since_iso`, WITH the principal the tail study needs.

    Separate from `recent_real_tp_closes` rather than an extra column on it: that one feeds the
    live post-TP follow-up, and widening a query the funded runner already calls every 30s to serve
    a second study is how one consumer's need becomes another's regression. Read-only, after the
    fact, entirely off the exit path.
    """
    return [dict(r) for r in conn.execute(
        "SELECT p.id AS position_id, p.mint, p.closed_at, p.entry_notional AS entry_usdc, "
        "       " + coin_network_sql("p.") + " AS network_id, "
        "       f.qty AS exit_qty, f.notional AS exit_usdc "
        "FROM memecoin_positions p "
        "JOIN memecoin_fills f ON f.id = ("
        "    SELECT x.id FROM memecoin_fills x "
        "    WHERE x.position_id = p.id AND x.kind LIKE '%exit%' "
        "    ORDER BY x.created_at DESC, x.id DESC LIMIT 1) "
        "WHERE p.execution_mode='real' AND p.is_open=0 AND p.exit_reason='real_tp' "
        "  AND p.closed_at >= ? AND p.entry_notional > 0 "
        "ORDER BY p.closed_at LIMIT ?", (since_iso, int(limit)))]


def record_residual_tail_sample(conn, *, position_id, mint, network_id, closed_at, horizon_s,
                                observed_at, elapsed_s, status, residual_usdc=None,
                                unit_price=None, detail=None, entry_usdc=None, exit_usdc=None,
                                resolve_status=None, resolve_fraction=None,
                                residual_qty_atomic=None, sold_qty_atomic=None, sold_usdc=None,
                                resolve_probes=None, resolve_ms=None):
    """Append one immutable residual-tail observation. INSERT OR IGNORE on (position, horizon).

    Never overwrites: a restart may only fill a horizon that has no row. A second quote for a
    horizon already recorded would silently replace evidence with a later, different measurement.
    """
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_residual_tail_samples("
        "position_id, mint, network_id, closed_at, horizon_s, observed_at, elapsed_s, status, "
        "residual_usdc, unit_price, detail, entry_usdc, exit_usdc, resolve_status, "
        "resolve_fraction, residual_qty_atomic, sold_qty_atomic, sold_usdc, resolve_probes, "
        "resolve_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (position_id, mint, network_id, closed_at, horizon_s, observed_at, elapsed_s, status,
         residual_usdc, unit_price, detail, entry_usdc, exit_usdc, resolve_status,
         resolve_fraction, str(residual_qty_atomic) if residual_qty_atomic is not None else None,
         str(sold_qty_atomic) if sold_qty_atomic is not None else None, sold_usdc,
         resolve_probes, resolve_ms))
    conn.commit()


def gas_topup_spent_today(conn, day, *, purpose=None):
    """USDC spent on CONFIRMED gas top-ups on `day` (UTC date string). Unknown-status rows count
    too: an ambiguous submit may well have spent the money, and a cap that ignores what it cannot
    confirm is not a cap."""
    if purpose == "buffer":
        # Only a chain-proven failed buffer swap frees its write-ahead spend. Unknown sends still
        # count, and confirmed sends retain the original submitted amount in the daily cap.
        row = conn.execute(
            "SELECT COALESCE(SUM(t.usdc_spent),0) FROM memecoin_gas_topups t "
            "WHERE t.day=? AND t.purpose='buffer' AND t.status='submitted' AND NOT EXISTS ("
            "SELECT 1 FROM memecoin_gas_topups done WHERE done.tx_sig=t.tx_sig "
            "AND done.purpose=t.purpose AND done.status='failed')", (day,)).fetchone()
        return float(row[0] or 0.0)
    row = conn.execute(
        "SELECT COALESCE(SUM(usdc_spent),0) FROM memecoin_gas_topups "
        "WHERE day=? AND status!='refused'" + (" AND purpose=?" if purpose else ""),
        (day, purpose) if purpose else (day,)).fetchone()
    return float(row[0] or 0.0)


def unresolved_gas_topup(conn, *, purpose="buffer"):
    """One signed top-up without a terminal ledger row; never start another while it may land."""
    row = conn.execute(
        "SELECT t.tx_sig FROM memecoin_gas_topups t WHERE t.purpose=? AND t.status='submitted' "
        "AND NOT EXISTS (SELECT 1 FROM memecoin_gas_topups done WHERE done.purpose=t.purpose "
        "AND done.tx_sig=t.tx_sig AND done.status IN ('confirmed','failed','dropped')) "
        "ORDER BY t.id LIMIT 1", (purpose,)).fetchone()
    return row[0] if row else None


def record_gas_topup(conn, *, now_iso, day, usdc_spent, sol_received, sol_before, tx_sig, status,
                     purpose="gas", usdc_before=None):
    """APPEND one gas top-up row. Written BEFORE the cap is next consulted, so a crash between the
    swap and the record cannot hand the next pass a fresh budget.

    THIS APPENDS, IT NEVER UPDATES, and the cap math depends on that. The write-ahead row carries
    the full usdc_spent; the later confirmation row carries usdc_spent=0.0 and only records the SOL
    received. Turning the pair into one UPDATE would look tidier and would break the ledger: an
    UPDATE that lands after a crash-and-retry can lose a spend the cap must still count."""
    conn.execute(
        "INSERT INTO memecoin_gas_topups "
        "(created_at, day, usdc_spent, usdc_before, sol_received, sol_before, tx_sig, status, purpose) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now_iso, day, float(usdc_spent), usdc_before, sol_received, sol_before, tx_sig,
         status, purpose))
    conn.commit()


def _atomic_text(value, *, name):
    """Validate a non-negative atomic amount and return its canonical decimal text."""
    try:
        amount = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(name + " must be an integer atomic amount") from exc
    if amount < 0:
        raise ValueError(name + " must not be negative")
    return str(amount)


def record_evm_gas_topup_submission(conn, *, now_iso, day, chain_id,
                                    native_amount_atomic, native_before_atomic, tx_hash, nonce,
                                    direction="unwrap"):
    """Write a signed EVM gas transfer before the network send.

    The amount stays in this row through every later status. A restart therefore counts a send that
    may have reached the network and blocks a second gas transfer until recovery has chain evidence.
    """
    amount = _atomic_text(native_amount_atomic, name="native_amount_atomic")
    before = _atomic_text(native_before_atomic, name="native_before_atomic")
    if int(amount) <= 0:
        raise ValueError("native_amount_atomic must be positive")
    if not isinstance(tx_hash, str) or not tx_hash.startswith("0x"):
        raise ValueError("tx_hash must be a transaction hash")
    if direction not in {"unwrap", "wrap"}:
        raise ValueError("invalid EVM gas transfer direction")
    cur = conn.execute(
        "INSERT INTO memecoin_evm_gas_topups "
        "(created_at, day, chain_id, native_amount_atomic, native_before_atomic, tx_hash, nonce, direction, status) "
        "VALUES (?,?,?,?,?,?,?,?, 'submitted')",
        (str(now_iso), str(day), str(chain_id), amount, before, tx_hash.lower(), int(nonce), direction),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_evm_gas_topup(conn, *, row_id, status):
    """Set one submitted EVM gas transfer state. Return false when another recovery changed it."""
    if status not in {"confirmed", "failed", "submitted_unknown", "not_sent"}:
        raise ValueError("invalid EVM gas top-up status")
    cur = conn.execute(
        "UPDATE memecoin_evm_gas_topups SET status=? "
        "WHERE id=? AND status IN ('submitted', 'submitted_unknown')",
        (status, int(row_id)),
    )
    conn.commit()
    return bool(cur.rowcount)


def evm_gas_topup_spent_today(conn, chain_id, day):
    """Return the atomic unwrap budget that can still have left the WETH balance."""
    rows = conn.execute(
        "SELECT native_amount_atomic FROM memecoin_evm_gas_topups "
        "WHERE chain_id=? AND day=? AND direction='unwrap' "
        "AND status IN ('submitted', 'submitted_unknown', 'confirmed')",
        (str(chain_id), str(day)),
    ).fetchall()
    return sum(int(row[0]) for row in rows)


def unresolved_evm_gas_topups(conn, chain_id):
    """Return signed EVM gas transfers that need receipt recovery before another transfer starts."""
    return [dict(row) for row in conn.execute(
        "SELECT id, tx_hash, status, nonce FROM memecoin_evm_gas_topups "
        "WHERE chain_id=? AND status IN ('submitted', 'submitted_unknown') ORDER BY id",
        (str(chain_id),),
    )]


def has_unresolved_evm_gas_topup(conn, chain_id):
    """True when a signed EVM unwrap has no terminal receipt state."""
    return bool(unresolved_evm_gas_topups(conn, chain_id))


def native_gas_refusal_since(conn, network_id, since_iso):
    """True when a funded entry on `network_id` refused for native gas after `since_iso`.

    For the EVM GAS-LOW page (memecoin_real_runner.check_evm_gas_alert). A range read on
    idx_mc_entry_view_started from the page's own cursor, about 15 s of rows per call."""
    row = conn.execute(
        "SELECT 1 FROM memecoin_entry_attempts WHERE decision_started_at > ? AND network_id = ? "
        "AND terminal_reason LIKE 'insufficient native gas%' LIMIT 1",
        (str(since_iso), str(network_id))).fetchone()
    return row is not None


def pending_totals(conn, network=None, *, include_manual=True, own_pending_id=None):
    """(count, reserved USD) of unresolved reservations, excluding a matching open position.

    No network returns the global portfolio-cap view. A network returns the chain-local
    encumbrance. Set ``include_manual=False`` only for the automatic slot count.
    """
    unresolved = (
        " AND NOT EXISTS (SELECT 1 FROM memecoin_positions p "
        "WHERE p.is_open=1 AND p.execution_mode='real' "
        "AND p.attempt_id=memecoin_pending_entries.attempt_id "
        "AND memecoin_pending_entries.attempt_id IS NOT NULL)"
    )
    # The automatic COPY slot count excludes manual and desk reservations: the desk has its own cap
    # (_DESK_CAP_SQL, MEMECOIN_DESK_MAX_CONCURRENT), operator 2026-10-22 (1a).
    manual = ("" if include_manual else
              " AND COALESCE(signal_source,'') NOT IN ('manual', '%s')" % SIGNAL_SOURCE_DESK)
    own = ""
    params = (normalized_coin_network(network, ""),)
    if network is not None and type(own_pending_id) is int and own_pending_id > 0:
        # Its prospective +1 owns this unsigned candidate; every other liability stays counted.
        own = " AND NOT (id=? AND (" + _unsigned_reservation_where() + "))"
        params += (own_pending_id,)
    if network is not None:
        r = conn.execute("SELECT COUNT(*), COALESCE(SUM(reserved_notional_usd), 0.0) "
                         "FROM memecoin_pending_entries WHERE status='reserved' "
                         f"AND {COIN_NETWORK_SQL}=?" + manual + unresolved + own,
                         params).fetchone()
    else:
        r = conn.execute("SELECT COUNT(*), COALESCE(SUM(reserved_notional_usd), 0.0) "
                         "FROM memecoin_pending_entries WHERE status='reserved'" + manual + unresolved
                         ).fetchone()
    return (r[0], float(r[1]))


def _snapshot_debit_ids(snapshot_debits):
    if (not isinstance(snapshot_debits, (tuple, list, set, frozenset))
            or len(snapshot_debits) > 256
            or any(type(pid) is not int or pid <= 0 for pid in snapshot_debits)):
        raise ValueError("invalid snapshot debit reservation IDs")
    return tuple(dict.fromkeys(snapshot_debits))


def settled_snapshot_usdc_debit(conn, network, *, snapshot_debits=()):
    """USDC from retained IDs already removed from the pending book. Manual sizing reads this
    and its open/pending book in one read transaction, so cash and inventory share a snapshot."""
    debits = _snapshot_debit_ids(snapshot_debits)
    if not debits:
        return 0.0
    network = normalized_coin_network(network, "")
    marks = ",".join("?" for _ in debits)
    rows = conn.execute(
        "SELECT e.id, e.funding_asset, e.reserved_notional_usd, e.status, "
        "EXISTS (SELECT 1 FROM memecoin_positions p WHERE p.is_open=1 "
        "AND p.execution_mode='real' AND p.attempt_id=e.attempt_id "
        "AND e.attempt_id IS NOT NULL) FROM memecoin_pending_entries e "
        "WHERE e.id IN (" + marks + ") AND " + coin_network_sql("e.") + "=?",
        (*debits, network)).fetchall()
    if set(debits) != {row[0] for row in rows}:
        raise ValueError("snapshot debit reservation unavailable on this chain")
    debit = 0.0
    for _pid, asset, notional, status, linked_open in rows:
        if asset not in (None, "usdc", "sol"):
            raise ValueError("unknown reservation funding asset")
        if asset in (None, "usdc") and (status != "reserved" or linked_open):
            value = float(notional)
            if not math.isfinite(value) or value < 0:
                raise ValueError("malformed snapshot debit USDC reservation")
            debit += value
    return debit


def pending_funding_totals(conn, network, *, snapshot_debits=()):
    """(reserved USDC dollars, native lamports), including dispatched IDs whose settlement is
    newer than the caller's balance read. Those IDs affect cash only, never book exposure."""
    debits = _snapshot_debit_ids(snapshot_debits)
    network = normalized_coin_network(network, "")
    fields = ("SELECT e.id, e.funding_asset, e.reserved_notional_usd, e.funding_amount_atomic, "
              "e.funding_overhead_atomic FROM memecoin_pending_entries e ")
    sql = (fields + "WHERE e.status='reserved' AND " + coin_network_sql("e.")
           + "=? AND NOT EXISTS (SELECT 1 FROM memecoin_positions p WHERE p.is_open=1 "
           "AND p.execution_mode='real' AND p.attempt_id=e.attempt_id "
           "AND e.attempt_id IS NOT NULL)")
    params = (network,)
    if debits:
        marks = ",".join("?" for _ in debits)
        # Separate indexed branches avoid a full historical-table scan from an OR on status/id.
        sql += " AND e.id NOT IN (" + marks + ") UNION ALL " + fields
        sql += "WHERE e.id IN (" + marks + ") AND " + coin_network_sql("e.") + "=?"
        params += (*debits, *debits, network)
    rows = conn.execute(sql, params).fetchall()
    debit_ids = set(debits)
    if not debit_ids.issubset(row[0] for row in rows):
        raise ValueError("snapshot debit reservation unavailable on this chain")
    usdc, sol = 0.0, 0
    for pid, asset, notional, atomic, overhead in rows:
        if asset in (None, "usdc"):
            if pid in debit_ids and (not math.isfinite(float(notional)) or float(notional) < 0):
                raise ValueError("malformed snapshot debit USDC reservation")
            usdc += float(notional)
            if overhead is not None:
                if (network != "solana" or asset != "usdc" or not str(overhead).isdigit()):
                    raise ValueError("malformed USDC SOL reservation overhead")
                sol += int(overhead)
        elif asset == "sol":
            if atomic is None or not str(atomic).isdigit() or int(atomic) <= 0:
                raise ValueError("malformed SOL reservation amount")
            if overhead is not None and (not str(overhead).isdigit() or int(overhead) < 0):
                raise ValueError("malformed SOL reservation overhead")
            sol += int(atomic) + int(overhead or 0)
        else:
            raise ValueError("unknown reservation funding asset")
    if normalized_coin_network(network, "") == "solana":
        # Buffer sends keep their write-ahead spend until a terminal ledger row exists. Legacy gas
        # sends only need this protection while their recent blockhash could still land; days-old
        # gas signatures remain unresolved in the ledger but cannot spend today's fresh balance.
        gas_cutoff = (datetime.fromisoformat(real_utc_now_iso())
                      - timedelta(minutes=10)).isoformat()
        row = conn.execute(
            "SELECT COALESCE(SUM(t.usdc_spent),0) FROM memecoin_gas_topups t "
            "LEFT JOIN memecoin_gas_topups done ON done.tx_sig=t.tx_sig "
            "AND done.purpose=t.purpose AND done.status IN ('confirmed','failed','dropped') "
            "WHERE t.status='submitted' AND (t.purpose='buffer' OR t.created_at>=?) "
            "AND done.tx_sig IS NULL",
            (gas_cutoff,)).fetchone()
        usdc += float(row[0] or 0.0)
    return usdc, sol


def unresolved_signed_pending_count(conn, network):
    """Count unresolved entry identities and manual EVM sells on the same nonce domain."""
    network = normalized_coin_network(network, "")
    row = conn.execute(
        "SELECT COUNT(*) FROM memecoin_pending_entries e "
        "WHERE e.status='reserved' AND " + coin_network_sql("e.") + "=? "
        "AND (e.tx_sig IS NOT NULL OR e.cleanup_tx_sig IS NOT NULL "
        "OR e.signed_payload_sha256 IS NOT NULL "
        "OR e.broadcast_at IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM memecoin_positions p "
        "WHERE p.is_open=1 AND p.execution_mode='real' "
        "AND p.attempt_id=e.attempt_id AND e.attempt_id IS NOT NULL)",
        (network,),
    ).fetchone()
    return int(row[0]) + (unresolved_holding_sell_count(conn, network)
                          if network != "solana" else 0)


def pending_summary(conn):
    """Reserved-entry rollup for the operator cockpit: count, reserved USD, orphaned (confirmed-but-
    position-less, needs manual reconcile), and a per-network breakdown."""
    rows = conn.execute(
        "SELECT network_id, mint, recovery_status, reserved_notional_usd "
        "FROM memecoin_pending_entries WHERE status='reserved'").fetchall()
    by_net, orphaned, notional = {}, 0, 0.0
    for r in rows:
        net = normalized_coin_network(r[0], r[1])
        by_net[net] = by_net.get(net, 0) + 1
        notional += (r[3] or 0.0)
        if r[2] == "orphaned":
            orphaned += 1
    return {"count": len(rows), "notional": notional, "orphaned": orphaned, "by_network": by_net}


def pending_open_mints(conn):
    return {r[0] for r in conn.execute(
        "SELECT mint FROM memecoin_pending_entries WHERE status='reserved'")}


def reserved_pending_sources(conn, network):
    """[(pending_id, source_user_id, mint, reserved_at)] of unresolved reservations on `network`."""
    want = normalized_coin_network(network, "")
    return [(r[0], r[1], r[3], r[4]) for r in conn.execute(
        "SELECT id, source_user_id, network_id, mint, reserved_at FROM memecoin_pending_entries "
        "WHERE status='reserved'") if normalized_coin_network(r[2], r[3]) == want]


def pending_open_coin_ids(conn, *, blockers=None):
    """Exact identities holding a funded reservation slot."""
    rows = conn.execute(
        "SELECT network_id, mint, id, attempt_id, signal_source FROM memecoin_pending_entries "
        "WHERE status='reserved'")
    identities = set()
    for row in rows:
        identity = coin_identity(row[0], row[1])
        identities.add(identity)
        if blockers is not None:
            blocker = {"kind": "reservation", "reservation_id": row[2], "attempt_id": row[3]}
            if row[4] == "relay_prebuy":
                # Named only for the 4663 Relay pre-buy: its refusal is a retry transient (memecoin_real_runner).
                blocker["signal_source"] = "relay_prebuy"
            blockers.setdefault(identity, blocker)
    return identities


# --- S1 real-money risk envelope: latched daily-loss circuit breaker ---

def set_risk_reset(conn, day, *, actor, reason, now_iso):
    """Start this day's risk count from NOW: releases an active drawdown pause and the daily latch.

    THE RAIL STAYS ARMED (operator, 2026-09-18: "I just need a button like the clear switch"). This
    is the drawdown equivalent of clearing the kill switch, not of turning it off. It moves the
    lower bound of the day's risk replay to this instant, so the pause and the latch that fired
    before it are released and the very next trigger pauses again. Expires on its own at midnight
    UTC, because the row is keyed by day.

    The alternative — editing MEMECOIN_RISK_DAY_START_AT — needs a restart of a release-pinned
    real-money process, which is why the operator could not use it while watching the system.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS memecoin_risk_resets ("
                 "day TEXT PRIMARY KEY, reset_at TEXT NOT NULL, actor TEXT, reason TEXT)")
    conn.execute("INSERT INTO memecoin_risk_resets(day, reset_at, actor, reason) VALUES (?,?,?,?) "
                 "ON CONFLICT(day) DO UPDATE SET reset_at=excluded.reset_at, actor=excluded.actor, "
                 "reason=excluded.reason",
                 (day, now_iso, actor or "operator", reason or ""))
    conn.commit()
    return {"day": day, "reset_at": now_iso}


def risk_reset_at(conn, day):
    """This day's operator risk reset, or "". Unreadable means none: a reset is never invented."""
    try:
        r = conn.execute("SELECT reset_at FROM memecoin_risk_resets WHERE day=?", (day,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(r[0]) if r and r[0] else ""


def risk_day_since(day, conn=None, *, resets=None):
    """Lower bound for the day's risk count: MEMECOIN_RISK_DAY_START_AT when it falls on `day`
    (operator restart, 2026-09-15: "start it from now"), else the day itself, which bounds nothing.
    Another day ignores the value, so it expires on its own at midnight UTC.

    With a connection, an operator reset from the cockpit is honoured too, and the LATER of the two
    wins: both are "count from here", so the more recent one is the live instruction. Without a
    connection the answer is the environment's alone — every funded caller passes one.
    Multi-day readers may pass their fresh per-call `resets` snapshot instead of repeating SQL.
    """
    from sniper.settings import memecoin as _cfg
    start = (getattr(_cfg, "MEMECOIN_RISK_DAY_START_AT", "") or "").strip()
    env = start if start[:10] == day else day
    if conn is None and resets is None:
        return env
    reset = risk_reset_at(conn, day) if resets is None else str(resets.get(day) or "")
    return max(env, reset or day)


def daily_real_realized_pnl(conn, day, *, since=None):
    """Sum realized PnL of closed real positions on the UTC day.

    The optional lower bound serves the risk reader. Accounting uses the full day.
    """
    r = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0.0) FROM memecoin_positions "
        "WHERE is_open=0 AND execution_mode='real' AND substr(closed_at,1,10)=? AND closed_at>=?",
        (day, since or day)).fetchone()
    return float(r[0])


def real_realized_between(conn, start_iso, end_iso):
    """(realized PnL, closes) of real closes with closed_at in (start_iso, end_iso]: the equity change
    breakdown's Trading term. PnL is None if any close is unpriced. Bounds are ISO strings in the
    ledger's own closed_at format."""
    r = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0.0), COUNT(*), COUNT(realized_pnl) FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=0 AND closed_at>? AND closed_at<=?",
        (start_iso, end_iso)).fetchone()
    return (float(r[0]) if r[1] == r[2] else None), int(r[1])


def latest_position_mids(conn, positions, since_iso=None):
    """{accounting_mark_key: newest positive own-source mid} for `positions`: the accounting route's
    last-resort mark (memecoin_position_marks), read for the equity recorder without a provider.
    `since_iso` (whole seconds, "+00:00") drops older marks: a stopped sweep is no mark."""
    from sniper.services.memecoin_candles import OWN_MARK_SOURCES
    sources_sql = ",".join("?" for _ in OWN_MARK_SOURCES)
    out = {}
    for p in positions:
        key = accounting_mark_key(p["network_id"], p["mint"])
        row = conn.execute("SELECT mid FROM memecoin_position_marks WHERE chain_id=? AND mint=? "
                           "AND sweep_ts>=? AND mid>0 AND COALESCE(quality,'')!='quarantined_spike' "
                           f"AND source IN ({sources_sql}) ORDER BY sweep_ts DESC LIMIT 1",
                           (key[0], key[1], since_iso or "", *OWN_MARK_SOURCES)).fetchone()
        if row and row[0] and float(row[0]) > 0:
            out[key] = float(row[0])
    return out


def daily_risk_realized_pnl(conn, day):
    """Return realized PnL since the operator's risk reset on this day."""
    return daily_real_realized_pnl(conn, day, since=risk_day_since(day, conn))


def is_daily_loss_tripped(conn, day):
    """Whether the breaker is latched for `day`, AFTER the operator's risk reset.

    A latch is a row, and a row outlives the count that produced it. Releasing the day from the
    cockpit has to release the latch too, or the button reports success and the entries stay
    blocked by a trip the operator just cleared.
    """
    r = conn.execute("SELECT tripped_at FROM memecoin_daily_loss_trips WHERE day=?",
                     (day,)).fetchone()
    if r is None:
        return False
    reset = risk_reset_at(conn, day)
    return not (reset and str(r[0] or "") < reset)


def check_and_latch_daily_loss(conn, day, limit_usd, *, commit=True):
    """Latched circuit breaker: trip once real realized PnL for `day` <= -limit_usd, and STAY
    tripped for the day even if a later fill revises PnL back up. Returns whether `day` is latched.
    limit_usd <= 0 = breaker DISABLED (never latches; -abs(0)=0 would otherwise trip on any loss)."""
    if limit_usd is None or float(limit_usd) <= 0:
        return False
    if is_daily_loss_tripped(conn, day):
        return True
    if daily_risk_realized_pnl(conn, day) <= -abs(limit_usd):
        conn.execute("INSERT OR IGNORE INTO memecoin_daily_loss_trips(day, tripped_at) VALUES (?, ?)",
                     (day, day))
        if commit:
            conn.commit()
        return True
    return False


_DRAWDOWN_REPLAY = threading.local()


def _drawdown_day_pause(rows, until, reset, close_day, drop_usd, pause_s, *, now):
    real = peak = 0.0
    started = False
    for closed_at, pnl in rows:
        t = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        if t > now:
            return until, True
        if not started:
            if reset != close_day:
                until = None
            started = True
        if str(closed_at) < reset:
            continue
        real += float(pnl)
        peak = max(peak, real)
        if (until is None or t >= until) and peak - real >= float(drop_usd):
            until, peak = t + timedelta(seconds=float(pause_s)), real
    return until, False


def drawdown_pause_until(conn, day, drop_usd, pause_s, now_iso):
    """Return the active drawdown pause end, or None.

    A drop of `drop_usd` from the daily realized peak starts a `pause_s` pause.
    The peak resets to the realized amount at each trigger. Midnight resets the daily peak, but preserves an active pause.
    An explicit risk reset clears the earlier history and pause. Replay preserves the result after a restart.
    Rows and resets are reread every call; only unchanged historical-day arithmetic is reused.
    """
    if not drop_usd or float(drop_usd) <= 0:
        return None
    now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
    until = None
    try:
        resets = dict(conn.execute("SELECT day, reset_at FROM memecoin_risk_resets"))
    except sqlite3.OperationalError:
        resets = {}  # Same conservative missing-reset behavior as risk_reset_at.
    reset = risk_day_since(day, resets=resets)
    since = reset if reset != day else ""
    rows = conn.execute(
            "SELECT closed_at, realized_pnl FROM memecoin_positions WHERE is_open=0 "
            "AND execution_mode='real' AND realized_pnl IS NOT NULL "
            "AND closed_at>=? AND julianday(closed_at)<=julianday(?) ORDER BY closed_at",
            (since, now_iso)).fetchall()
    cache = getattr(_DRAWDOWN_REPLAY, "days", None)
    if cache is None:
        cache = _DRAWDOWN_REPLAY.days = {}
    # ISO close dates older than two UTC days cannot reach `now`, even with a UTC offset.
    cache_before = (now.astimezone(timezone.utc) - timedelta(days=2)).date().isoformat()
    for close_day, group in groupby(rows, key=lambda row: str(row[0])[:10]):
        reset = risk_day_since(close_day, resets=resets)
        inputs = (tuple(group), until, reset, close_day, drop_usd, pause_s)
        previous = cache.get(close_day) if close_day < cache_before else None
        if previous is not None and previous[0] == inputs:
            until = previous[1]
            continue
        until, stopped = _drawdown_day_pause(*inputs, now=now)
        if stopped:
            break
        # Fresh rows/reset/settings and incoming pause are the key; no shared state or new lock.
        # Keep one version per day and only the newest 128 days; older days still replay exactly.
        if close_day < cache_before and (len(cache) < 128 or close_day in cache or close_day > min(cache)):
            cache[close_day] = (inputs, until)
            if len(cache) > 128:
                del cache[min(cache)]
    if until is None or now >= until:
        return None
    return until.isoformat()


def record_network_drop(conn, network_id, mint, reason, at):
    """Count a real-leg signal we did NOT trade because of its venue: reason='excluded' (chain not
    allowlisted / off) or 'evm_unarmed' (allowlisted but the EVM leg isn't built yet). A counter,
    not an alert — telemetry for 'how much are we passing on', read via network_drop_counts."""
    conn.execute("CREATE TABLE IF NOT EXISTS memecoin_network_drops ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, network_id TEXT, mint TEXT, reason TEXT, at TEXT)")
    conn.execute("INSERT INTO memecoin_network_drops(network_id, mint, reason, at) VALUES (?,?,?,?)",
                 (network_id or "", mint, reason, at))
    conn.commit()


def network_drop_counts(conn):
    """{(network_id, reason): count} of real-leg venue drops. Empty if the table doesn't exist yet."""
    try:
        rows = conn.execute("SELECT network_id, reason, COUNT(*) FROM memecoin_network_drops "
                            "GROUP BY network_id, reason").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {(r[0], r[1]): r[2] for r in rows}


def _mark_value_usd(entry_notional, entry_price, last_price, last_at, paused_at, now_iso,
                    max_age_s, opened_at=None):
    """What one open position is worth NOW, or 0.0 when we cannot honestly say.

    A FRESH MARK WINS. Otherwise, two different unknowns that must not be treated alike:

      A POSITION TOO NEW TO HAVE A MARK IS WORTH ITS COST. We just paid that price, so cost IS the
      most recent executable evidence we hold. MEASURED on funded positions since 2026-08-30: the
      time from open to a first executable mark is p50 90 s and p90 701 s, and 23 of 247 consecutive
      entry pairs opened within 120 s of each other — so about one entry in ten is sized while its
      predecessor is plausibly still unmarked. Zeroing those understated equity by a whole clip and
      shrank the very next entry for no reason.

      A POSITION OLD ENOUGH TO HAVE BEEN MARKED, AND ISN'T, IS WORTH ZERO. Past the bound the
      absence is evidence, not latency.

    ZERO on every other doubt: paused tracking, a non-positive cost, a missing or non-positive price
    on either side of a mark we do have, or an unparseable timestamp. The one-way rule is intact —
    the dead-bag case this exists to fix is BY DEFINITION old, and the cost fallback is reachable
    only inside the freshness bound, so an unknown can still only shrink a clip.
    """
    if paused_at:
        return 0.0
    try:
        cost = float(entry_notional or 0.0)
        entry = float(entry_price or 0.0)
        last = float(last_price or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if cost <= 0:
        return 0.0
    mark_age = _iso_age_seconds(last_at, now_iso)
    if (entry > 0 and last > 0 and mark_age is not None
            and 0 <= mark_age <= float(max_age_s)):
        return cost * (last / entry)
    # No usable mark. Only its own age can still speak for it.
    open_age = _iso_age_seconds(opened_at, now_iso)
    if open_age is not None and 0 <= open_age <= float(max_age_s):
        return cost
    return 0.0


def _iso_age_seconds(stamp, now_iso):
    """Seconds between an ISO stamp and `now_iso`, or None when either cannot be read."""
    from datetime import datetime, timezone
    def _parse(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    then, now = _parse(stamp), _parse(now_iso)
    if then is None or now is None:
        return None
    return (now - then).total_seconds()


def real_open_snapshot(conn, network=None, *, include_manual=True, now_iso=None, max_age_s=None):
    """Confirmed real-money open positions including exact ``coin_ids``. Pass
    network ('solana' | chain id) to scope to one venue (per-venue gas reserve — a Solana gas check
    must not count EVM positions, which exit with ETH, not SOL).
    Set ``include_manual=False`` only for the automatic slot count.

    TWO VALUATIONS, BECAUSE THERE ARE TWO QUESTIONS (2026-09-06). Computed here, once, so no caller
    invents its own and they cannot drift apart:

      ``notional``      COST BASIS. What we committed. This is what the EXPOSURE cap
                        (`MEMECOIN_MAX_TOTAL_EXPOSURE_USD`) must read: the cap bounds money
                        COMMITTED, not money recoverable, and a dead bag has spent its capital and
                        must keep occupying the cap until it is actually closed. If marks fed the
                        cap it would LOOSEN exactly when the book is full of unsellable bags —
                        the worst possible moment to allow another entry — and a fixed dollar limit
                        would drift with price rather than with our decisions.
      ``mark_notional`` LAST FRESH EXECUTABLE MARK, zero when stale, paused or unreadable. This is
                        what SIZING must read: the next clip should be a share of what we could
                        actually get back, not of what we once paid.

    Do not collapse these. `test_a_dead_bag_still_consumes_exposure` exists to stop it.
    """
    from sniper.settings import memecoin as _cfg
    if max_age_s is None:
        max_age_s = getattr(_cfg, "MEMECOIN_SIZING_MARK_MAX_AGE_S", 300.0)
    if now_iso is None:
        from sniper.util.clock import real_utc_now_iso
        now_iso = real_utc_now_iso()
    manual = "" if include_manual else " AND COALESCE(signal_source,'')!='manual'"
    columns = ("network_id, mint, entry_notional, entry_price, last_executable_price, "
               "last_executable_at, tracking_paused_at, opened_at")
    if network is not None:
        rows = conn.execute(
            f"SELECT {columns} FROM memecoin_positions WHERE is_open=1 "
            f"AND execution_mode='real' AND {COIN_NETWORK_SQL}=?" + manual,
            (normalized_coin_network(network, ""),)).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {columns} FROM memecoin_positions "
            "WHERE is_open=1 AND execution_mode='real'" + manual).fetchall()
    return {"count": len(rows),
            "notional": float(sum((r[2] or 0.0) for r in rows)),
            "mark_notional": float(sum(
                _mark_value_usd(r[2], r[3], r[4], r[5], r[6], now_iso, max_age_s, r[7])
                for r in rows)),
            "mints": {r[1] for r in rows},
            "coin_ids": {coin_identity(r[0], r[1]) for r in rows}}


def funded_book_cost_snapshot(conn):
    """Global funded cost exposure without executable-mark valuation or coin identities."""
    opened = conn.execute(
        "SELECT entry_notional FROM memecoin_positions "
        "WHERE is_open=1 AND execution_mode='real'").fetchall()
    open_notional = float(sum((r[0] or 0.0) for r in opened))
    pending_count, pending_notional = pending_totals(conn)
    return {
        "count": len(opened) + pending_count,
        "notional": open_notional + pending_notional,
        "open_count": len(opened),
        "open_notional": open_notional,
        "pending_count": pending_count,
        "pending_notional": pending_notional,
    }


def funded_book_snapshot(conn):
    """Global funded cap state: open real positions plus unresolved reservations exactly once."""
    opened = real_open_snapshot(conn)
    pending_count, pending_notional = pending_totals(conn)
    return {
        "count": opened["count"] + pending_count,
        "notional": opened["notional"] + pending_notional,
        # A RESERVATION IS ALWAYS AT COST in both views: it has no position and therefore no mark,
        # and the cash behind it is already committed.
        "mark_notional": opened["mark_notional"] + pending_notional,
        "open_count": opened["count"],
        "open_notional": opened["notional"],
        "open_mark_notional": opened["mark_notional"],
        "pending_count": pending_count,
        "pending_notional": pending_notional,
    }


def evm_canary_book(conn, network):
    """(closes, realized PnL, deployed clip) over one chain's AUTOMATIC real closes, for the canary
    stop (eth-mainnet spec §7). A close whose PnL or notional is unknown is LEFT OUT of all three
    numbers, never counted as a zero: unknown is not break-even. Manual entries are the operator's
    own and are not part of the canary."""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0.0), COALESCE(SUM(entry_notional), 0.0) "
        "FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 AND network_id=? "
        "AND COALESCE(signal_source,'') NOT IN ('manual', 'desk') AND realized_pnl IS NOT NULL "
        "AND entry_notional IS NOT NULL AND entry_notional > 0", (str(network),)).fetchone()
    return int(row[0]), float(row[1]), float(row[2])


def automatic_funded_count(conn, network=None):
    """Count non-manual open positions and reservations for the automatic slot cap.

    ``network`` scopes it to one chain for the PER-CHAIN slot limit; the default global view is
    what the total cap reads. The manual exclusion is deliberate and must survive any tidying:
    an operator's own buy does not consume an automatic slot, while it DOES consume gas — which
    is why the gas reserve counts manual positions and this does not. The two counts differ on
    purpose (see `evm_entry_allowed`).
    """
    from sniper.settings import memecoin as _cfg
    from sniper.util.clock import real_utc_now_iso
    pending_count, _ = pending_totals(conn, network=network, include_manual=False)
    # A LONG HOLD IS DUST, NOT A SLOT (operator 2026-10-07). A position open at least
    # MEMECOIN_SLOT_FREE_AFTER_S stops counting here, and only here: exposure (cost basis), the gas
    # reserve and one-position-per-coin still see it. 0 = off. An unknown, unparseable or future
    # opened_at still takes its slot; a reservation always does.
    free_after = float(getattr(_cfg, "MEMECOIN_SLOT_FREE_AFTER_S", 0.0) or 0.0)
    # Desk positions count only in their own MEMECOIN_DESK_MAX_CONCURRENT (operator 2026-10-22, 1a), so
    # an open desk book never takes a copy slot; exposure and the gas reserve still see them.
    where = ("is_open=1 AND execution_mode='real' "
             "AND COALESCE(signal_source,'') NOT IN ('manual', '%s')" % SIGNAL_SOURCE_DESK)
    params = ()
    if network is not None:
        where += f" AND {COIN_NETWORK_SQL}=?"
        params = (normalized_coin_network(network, ""),)
    rows = conn.execute(f"SELECT opened_at FROM memecoin_positions WHERE {where}", params).fetchall()
    if free_after <= 0:
        return len(rows) + pending_count
    try:
        now = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return len(rows) + pending_count

    def held_long(opened_at):
        try:
            opened = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            return (now - opened).total_seconds() >= free_after
        except (TypeError, ValueError):
            return False
    return sum(1 for (opened_at,) in rows if not held_long(opened_at)) + pending_count


def real_open_coin_ids(conn, *, blockers=None):
    """Exact identities of confirmed funded positions."""
    identities = set()
    for row in conn.execute(
            "SELECT network_id, mint, id, attempt_id FROM memecoin_positions "
            "WHERE is_open=1 AND execution_mode='real'"):
        identity = coin_identity(row[0], row[1])
        identities.add(identity)
        if blockers is not None:
            blockers.setdefault(identity, {
                "kind": "position", "position_id": row[2], "attempt_id": row[3]})
    return identities


def real_accounting(conn, *, price_map, day):
    """Operator-truth rollup of the real book (spec §7).

    ``price_map`` is keyed by ``(normalized network_id, normalized mint)``. A legacy bare-mint
    mark is accepted only for an unambiguous Solana coin in the current book; a bare EVM mark is
    never shared across chains. The map comes from one injected batch call, so this stays pure.

    Per network: deployed = remaining cost basis of open positions (open_qty × entry, so a
    de-risked position counts only its un-recovered slice) + reserved pending; unrealized =
    Σ open_qty × (mark − entry) over QUOTABLE positions only — unquotable ones are counted in
    `unquotable_n`, never silently marked 0. Realized = cumulative + today's (real-UTC day)."""
    by_net = {}

    def _net(n, mint):
        return by_net.setdefault(normalized_coin_network(n, mint), {
            "deployed": 0.0, "reserved": 0.0, "unrealized": 0.0,
            "open_n": 0, "reserved_n": 0, "unquotable_n": 0})

    open_rows = conn.execute(
        "SELECT id, mint, entry_price, network_id FROM memecoin_positions "
        "WHERE is_open=1 AND execution_mode='real'").fetchall()
    keys_by_mint = {}
    for row in open_rows:
        key = accounting_mark_key(row["network_id"], row["mint"])
        keys_by_mint.setdefault(key[1], set()).add(key)
    for r in open_rows:
        b = _net(r["network_id"], r["mint"])
        entry = float(r["entry_price"] or 0.0)
        qty = open_qty(conn, r["id"])
        b["deployed"] += qty * entry
        b["open_n"] += 1
        key = accounting_mark_key(r["network_id"], r["mint"])
        mark = (price_map or {}).get(key)
        if (mark is None and key[0] == "solana" and len(keys_by_mint[key[1]]) == 1):
            mark = (price_map or {}).get(key[1])
        if mark and mark > 0 and entry > 0:
            b["unrealized"] += qty * (float(mark) - entry)
        else:
            b["unquotable_n"] += 1
    reserved = conn.execute(
        "SELECT network_id, mint, reserved_notional_usd FROM memecoin_pending_entries "
        "WHERE status='reserved'").fetchall()
    for r in reserved:
        b = _net(r["network_id"], r["mint"])
        amt = float(r["reserved_notional_usd"] or 0.0)
        b["deployed"] += amt
        b["reserved"] += amt   # tracked separately: reserved USDC is still in the wallet's cash
        b["reserved_n"] += 1   # balance (not yet spent), so equity must not add it via deployed too
    for b in by_net.values():
        b["deployed"] = round(b["deployed"], 2)
        b["reserved"] = round(b["reserved"], 2)
        b["unrealized"] = round(b["unrealized"], 2)
    # THREE NUMBERS, AND THE FIRST TWO ARE NOT INTERCHANGEABLE. `realized_*` stay ALL-MONEY because
    # accounting reconciles to the wallet and the daily breaker reads the same quantity; a manual
    # loss is a real loss. `robot_realized_*` is the system's own performance, and the difference
    # between them is the operator's own picks — 08-27 was +$10.10 all-money against +$74.18 robot.
    # A surface must say which it is showing; an unlabelled pair is how the operator read his own
    # losses as system losses in the first place.
    def _sum(where, *params):
        return round(float(conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM memecoin_positions WHERE " + where,
            params).fetchone()[0]), 2)
    day_clause = "is_open=0 AND substr(closed_at,1,10)=?"
    return {"by_network": by_net,
            "realized_total": _sum("is_open=0 AND " + real_money_sql()),
            "realized_today": round(daily_real_realized_pnl(conn, day), 2),
            "robot_realized_total": _sum("is_open=0 AND " + robot_performance_sql()),
            "robot_realized_today": _sum(day_clause + " AND " + robot_performance_sql(), day),
            "manual_realized_total": _sum(
                "is_open=0 AND " + real_money_sql() + " AND NOT (" + robot_decision_sql() + ")"),
            "manual_realized_today": _sum(
                day_clause + " AND " + real_money_sql()
                + " AND NOT (" + robot_decision_sql() + ")", day),
            "open_count": len(open_rows), "reserved_count": len(reserved),
            "unrealized_total": round(sum(b["unrealized"] for b in by_net.values()), 2),
            "deployed_total": round(sum(b["deployed"] for b in by_net.values()), 2)}


# ---------------------------------------------------------------------------
# Thesis capture (display/context only — never a copy signal)
# ---------------------------------------------------------------------------

_THESIS_COLUMNS = [
    "fomo_thesis_id", "comment_id", "source_user_id", "handle", "mint", "ticker",
    "comment", "has_link", "their_pnl_pct", "their_realized_pnl_pct",
    "their_notional_usd", "their_unrealized_pnl_usd", "market_cap", "fdv", "price",
    "tag", "network_id", "swap_id", "trade_id", "created_at", "received_at", "lag_s",
    "signal_source",
]


def record_thesis(conn, th):
    """Insert one curated thesis. Dedups on fomo_thesis_id (keep-first on re-delivery /
    edit). Accepts a ThesisEvent.__dict__. Returns True iff a new row was created."""
    row = dict(th)
    row["has_link"] = 1 if row.get("has_link") else 0
    cols = [c for c in _THESIS_COLUMNS if c in row]
    placeholders = ",".join("?" for _ in cols)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO memecoin_theses ({','.join(cols)}) VALUES ({placeholders})",
        [row[c] for c in cols])
    conn.commit()
    return cur.rowcount > 0


def list_theses(conn, uid=None, mint=None, limit=50):
    """Recent curated theses, newest-first (by created_at), optional uid/mint filter."""
    q = "SELECT * FROM memecoin_theses"
    where, args = [], []
    if uid:
        where.append("source_user_id=?"); args.append(uid)
    if mint:
        where.append("mint=?"); args.append(mint)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY created_at DESC, id DESC LIMIT ?"
    args.append(int(limit))
    return [dict(r) for r in conn.execute(q, args)]


def latest_thesis_by_trader(conn, uids):
    """Latest thesis per uid in ONE query -> {uid: row}. For decorating the Traders table
    with each trader's last posted reasoning without an N+1."""
    out = {}
    if not uids:
        return out
    uids = list(uids)
    qs = ",".join("?" for _ in uids)
    # newest per trader: order asc so the last write per uid wins the dict slot
    for r in conn.execute(
        f"SELECT * FROM memecoin_theses WHERE source_user_id IN ({qs}) "
        f"ORDER BY created_at ASC, id ASC", uids):
        out[r["source_user_id"]] = dict(r)
    return out


def token_thesis_counts(conn, mints, within_hours=24, now_iso=None):
    """How many curated theses on each mint within the last `within_hours` (confluence).
    Windowed on corrected real-UTC received_at — our own consistent reference, avoids
    fomo-clock mixing. Returns {mint: count} with 0 for requested mints that have none."""
    out = {mm: 0 for mm in mints}
    if not mints:
        return out
    if now_iso is None:
        from sniper.util.clock import real_utc_now_iso
        now_iso = real_utc_now_iso()
    now = _parse_iso(now_iso)
    cutoff = now - timedelta(hours=within_hours) if now else None
    qs = ",".join("?" for _ in mints)
    for r in conn.execute(
        f"SELECT mint, received_at FROM memecoin_theses WHERE mint IN ({qs})", list(mints)):
        t = _parse_iso(r["received_at"])
        if cutoff and (not t or t < cutoff):
            continue
        out[r["mint"]] = out.get(r["mint"], 0) + 1
    return out


# ---------------------------------------------------------------------------
# Telegram front-run latency instrument
# ---------------------------------------------------------------------------

def record_alert(conn, fields):
    """Log an alert intent for a curated large-buy. Dedups on fomo_event_id. Returns True
    iff a new row was created (i.e. this is the first alert for the event)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO memecoin_alert_latency(fomo_event_id, source_user_id, handle, "
        "mint, ticker, their_size_usd, market_cap, fomo_created_at, detected_at, lag_s, "
        "signal_source, network_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (fields["fomo_event_id"], fields.get("source_user_id"), fields.get("handle"),
         fields.get("mint"), fields.get("ticker"), fields.get("their_size_usd"),
         fields.get("market_cap"), fields.get("fomo_created_at"), fields.get("detected_at"),
         fields.get("lag_s"), fields.get("signal_source") or "global_feed",
         fields.get("network_id")))
    conn.commit()
    return cur.rowcount > 0


def update_alert_sent(conn, fomo_event_id, sent_at, ok, ms, local_ms=None, *,
                      rendered_text_redacted=None, message_sha256=None):
    conn.execute(
        "UPDATE memecoin_alert_latency SET alert_sent_at=?, send_ok=?, send_ms=?, local_ms=?, "
        "rendered_text_redacted=?, message_sha256=? WHERE fomo_event_id=?",
        (sent_at, 1 if ok else 0, ms, local_ms, rendered_text_redacted, message_sha256,
         fomo_event_id))
    conn.commit()


def record_telegram_delivery(conn, fields):
    """Append one completed primary-chat delivery attempt; never stores raw one-tap URLs."""
    cur = conn.execute(
        "INSERT INTO memecoin_telegram_delivery_audit("
        "message_kind, source_id, queued_at, send_started_at, completed_at, send_ok, send_ms, "
        "rendered_text_redacted, message_sha256, error_class, attempt) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (str(fields.get("message_kind") or "generic")[:80],
         str(fields["source_id"])[:200] if fields.get("source_id") is not None else None,
         fields["queued_at"], fields["send_started_at"], fields["completed_at"],
         1 if fields.get("send_ok") else 0, float(fields.get("send_ms") or 0.0),
         str(fields.get("rendered_text_redacted") or ""), fields["message_sha256"],
         str(fields["error_class"])[:80] if fields.get("error_class") else None,
         int(fields.get("attempt", 1))))
    conn.commit()
    return cur.lastrowid


def alert_latency_summary(conn, recent_limit=50):
    """Our detection->send profile over logged alerts. Three disjoint measured segments, NOT a
    verdict: median_lag_s (fomo feed -> our receipt, real-UTC), median_local_ms (our receipt ->
    send start: DB writes + thesis lookup + msg build), median_send_ms (the Telegram POST,
    SUCCESSFUL sends only). Their sum is our instrumented latency; the fomo-push comparison is a
    manual phone read (out of band). send-time metrics condition on send_ok=1 so a timed-out
    send (~3s) never inflates them."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_alert_latency ORDER BY id DESC")]
    sent = [r for r in rows if r.get("alert_sent_at")]
    ok_sent = [r for r in sent if r.get("send_ok")]
    lags = [r["lag_s"] for r in rows if r.get("lag_s") is not None and r["lag_s"] >= 0]
    local = [r["local_ms"] for r in sent if r.get("local_ms") is not None]
    send_ok = [r["send_ms"] for r in ok_sent if r.get("send_ms") is not None]
    total_ok = [(r.get("local_ms") or 0.0) + r["send_ms"] for r in ok_sent
                if r.get("send_ms") is not None]
    oks = [r["send_ok"] for r in sent if r.get("send_ok") is not None]
    return {
        "n": len(rows),
        "n_sent": len(sent),
        "n_sent_ok": len(ok_sent),
        "median_lag_s": _median(lags),
        "median_local_ms": _median(local),
        "median_send_ms": _median(send_ok),        # successful sends only
        "median_total_ms": _median(total_ok),      # local + send, successful only
        "send_ok_rate": (sum(1 for x in oks if x) / len(oks)) if oks else None,
        "recent": rows[:recent_limit],
    }


def fomo_priced_position_ids(conn):
    """Position ids carrying at least one fomo-priced fill — the set HONEST_COPY_SQL removes."""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT position_id FROM memecoin_fills WHERE fill_source='fomo'")}


def _arm_bucket(out, strat):
    return out.setdefault(strat, {"strategy": strat, "opened": 0, "closed": 0, "wins": 0,
                                  "realized_pnl": 0.0, "open_count": 0, "unrealized_pnl": 0.0,
                                  "open_realized_pnl": 0.0, "unrealized_partial": 0,
                                  "noquote_closes": 0, "stale_closes": 0,
                                  "noquote_shadow_pnl": 0.0, "noquote_shadow_adjustment": 0.0,
                                  "fomo_priced_closes": 0, "fomo_priced_pnl": 0.0,
                                  "_holds": [], "_peaked_at_entry": 0, "_ttp_n": 0,
                                  "_events": set()})


def _disclose_fomo(out, p):
    """Record a dropped fomo-priced position against its arm WITHOUT letting it move the PnL.
    The bucket stays visible so the honest read can never be mistaken for the whole tape."""
    a = _arm_bucket(out, p.get("strategy") or "control")
    if not p["is_open"]:
        a["fomo_priced_closes"] += 1
        a["fomo_priced_pnl"] += p.get("realized_pnl") or 0.0


def _accumulate_arm(conn, out, p, open_qty=None):
    """Fold one position row into its arm's running aggregate (shared by the full and matched
    strategy summaries). Open positions are marked to their last observed quote (unrealized_pnl)
    so an arm that lets losers ride open is not flattered vs one that force-realizes at a cap;
    an open position we could never price is counted unrealized_partial (excluded, not marked 0)."""
    from datetime import datetime as _dt
    a = _arm_bucket(out, p.get("strategy") or "control")
    a["opened"] += 1
    if p.get("origin_event_id") is not None:
        a["_events"].add(p["origin_event_id"])
    if not p["is_open"]:
        a["closed"] += 1
        rp = p.get("realized_pnl") or 0.0
        a["realized_pnl"] += rp
        reason = p.get("exit_reason") or ""
        if reason.endswith("_noquote"):
            a["noquote_closes"] += 1
            shadow = -float(p.get("entry_notional") or 0.0)
            a["noquote_shadow_pnl"] += shadow
            a["noquote_shadow_adjustment"] += shadow - rp
        if reason.endswith("_stale"):
            a["stale_closes"] += 1
        if rp > 0:
            a["wins"] += 1
        ttp = p.get("time_to_peak_s")
        if ttp is not None:
            a["_ttp_n"] += 1
            if ttp <= 15.0:
                a["_peaked_at_entry"] += 1
        try:
            h = (_dt.fromisoformat(p["closed_at"]) - _dt.fromisoformat(p["opened_at"])).total_seconds()
            if h >= 0:
                a["_holds"].append(h)
        except (ValueError, TypeError, KeyError):
            pass
    else:
        a["open_count"] += 1
        a["open_realized_pnl"] += _open_realized_pnl(conn, p["id"], p.get("entry_price"))
        lq = p.get("last_quote_price")
        if lq and lq > 0 and open_qty and p.get("entry_price"):
            a["unrealized_pnl"] += (lq - p["entry_price"]) * open_qty
        else:
            a["unrealized_partial"] += 1   # never priced -> unknown, excluded from the mark


def _arm_rows(out, *, with_matched_events=False):
    import statistics as _st
    res = []
    for a in out.values():
        realized = round(a["realized_pnl"], 2)
        open_realized = round(a["open_realized_pnl"], 2)
        unreal = round(a["unrealized_pnl"], 2)
        noquote_shadow = round(a["noquote_shadow_pnl"], 2)
        noquote_adjustment = round(a["noquote_shadow_adjustment"], 2)
        total = round(realized + open_realized + unreal, 2)
        row = {
            "strategy": a["strategy"], "opened": a["opened"], "closed": a["closed"],
            "realized_pnl": realized,
            "open_realized_pnl": open_realized,
            "open_count": a["open_count"], "unrealized_pnl": unreal,
            "unrealized_partial": a["unrealized_partial"],
            "noquote_closes": a["noquote_closes"], "stale_closes": a["stale_closes"],
            "noquote_shadow_pnl": noquote_shadow,
            "noquote_shadow_adjustment": noquote_adjustment,
            # dropped by the honest filter — disclosed, never folded into the PnL above
            "fomo_priced_closes": a["fomo_priced_closes"],
            "fomo_priced_pnl": round(a["fomo_priced_pnl"], 2),
            # realized + marked-open — PARTIAL when unrealized_partial>0 (some opens unpriceable)
            "total_pnl": total,
            "conservative_total_pnl": round(total + noquote_adjustment, 2),
            "win_rate": (a["wins"] / a["closed"]) if a["closed"] else None,
            "median_hold_s": _st.median(a["_holds"]) if a["_holds"] else None,
            "exit_liquidity_rate": (a["_peaked_at_entry"] / a["_ttp_n"]) if a["_ttp_n"] else None,
        }
        if with_matched_events:
            row["matched_events"] = len(a["_events"])
        res.append(row)
    return res


def strategy_summary(conn, mode="paper", honest_only=True, network_id=None):
    """Copy performance by execution arm — the A/B/control read. exit_liquidity_rate =
    fraction of closes whose peak was within one sweep (~15s) of entry (we were the bag).
    Open positions are marked to their last observed quote (unrealized_pnl / total_pnl).
    Scoped to `mode` (default paper) so real-money rows never pollute the paper A/B verdict.

    HONEST BY DEFAULT (2026-07-27): fomo-priced positions are dropped, exactly as the arming
    gate drops them (HONEST_COPY_SQL). They are not a harmless minority — they invert the
    ranking. scalp_c reads -$39,896 with them and +$6,926 without, and on 2026-07-26 the
    contaminated number got it cut as a "deep loser". The dropped bucket stays visible in
    fomo_priced_closes / fomo_priced_pnl. Pass honest_only=False only to inspect that bucket.

    NOTE: this counts EVERY position, including a fast arm's re-buys that slow arms skipped as
    a dup while still holding — so the arms' entry sets can differ. Use strategy_summary_matched
    for the apples-to-apples cross-arm read."""
    out = {}
    drop = fomo_priced_position_ids(conn) if honest_only else set()
    sql, params = "SELECT * FROM memecoin_positions WHERE execution_mode=?", [mode]
    if network_id is not None:
        frag, extra = chain_scope_clause(network_id)
        sql += frag
        params += extra
    for p in [dict(r) for r in conn.execute(sql, params)]:
        if p["id"] in drop:
            _disclose_fomo(out, p)
            continue
        oq = open_qty(conn, p["id"]) if p["is_open"] else None
        _accumulate_arm(conn, out, p, open_qty=oq)
    return _arm_rows(out)


def strategy_summary_matched(conn, arms=None, mode="paper", honest_only=True, network_id=None):
    """Cross-arm A/B restricted to events where ALL arms entered (origin_event_id), so a fast
    arm's extra re-buy — which slow arms skip as a dup while still holding the first entry —
    can't skew the comparison. Same shape as strategy_summary plus matched_events. Arms default
    to the configured MEMECOIN_ARMS; positions with no origin_event_id (legacy / manual) are
    excluded since their arm coverage is unknown. Scoped to `mode` (default paper).

    The honest filter is applied BEFORE the match, not after. A fomo exit can hit one mirror arm
    and not another on the same event, so a post-match filter would compare one arm's 1 close
    against another arm's 2 and call it apples-to-apples."""
    if arms is None:
        from sniper.settings.memecoin import MEMECOIN_ARMS
        arms = MEMECOIN_ARMS
    armset = set(arms)
    drop = fomo_priced_position_ids(conn) if honest_only else set()
    rows, dropped = [], []
    sql, params = "SELECT * FROM memecoin_positions WHERE execution_mode=?", [mode]
    if network_id is not None:
        frag, extra = chain_scope_clause(network_id)
        sql += frag
        params += extra
    for r in conn.execute(sql, params):
        p = dict(r)
        (dropped if p["id"] in drop else rows).append(p)
    ev_arms = {}
    for p in rows:
        eid = p.get("origin_event_id")
        if eid is not None:
            ev_arms.setdefault(eid, set()).add(p.get("strategy") or "control")
    matched = {eid for eid, s in ev_arms.items() if armset <= s}
    out = {}
    for p in rows:
        if p.get("origin_event_id") in matched and (p.get("strategy") or "control") in armset:
            oq = open_qty(conn, p["id"]) if p["is_open"] else None
            _accumulate_arm(conn, out, p, open_qty=oq)
    for p in dropped:
        if (p.get("strategy") or "control") in armset:
            _disclose_fomo(out, p)
    return _arm_rows(out, with_matched_events=True)


def signal_source_summary(conn, mode="paper"):
    """Copy performance grouped by signal_source — the A/B: does the real-time global feed
    beat the late per-follow-activity poll? Open positions counted separately (unrealized).
    Scoped to `mode` (default paper)."""
    out = {}
    for r in conn.execute("SELECT * FROM memecoin_positions WHERE execution_mode=?", (mode,)):
        p = dict(r)
        src = p.get("signal_source") or "global_feed"
        a = out.setdefault(src, {"signal_source": src, "opened": 0, "closed": 0,
                                 "wins": 0, "realized_pnl": 0.0})
        a["opened"] += 1
        if not p["is_open"]:
            a["closed"] += 1
            rp = p.get("realized_pnl") or 0.0
            a["realized_pnl"] += rp
            if rp > 0:
                a["wins"] += 1
    for a in out.values():
        a["win_rate"] = (a["wins"] / a["closed"]) if a["closed"] else None
    return list(out.values())


def delete_event(conn, fomo_event_id):
    """Remove a feed event so a failed copy re-processes on the next poll (the dedup
    row is committed before the copy runs; deleting it on failure makes copy at-least-once)."""
    conn.execute("DELETE FROM memecoin_events WHERE fomo_event_id=?", (fomo_event_id,))
    conn.commit()


def set_event_action(conn, fomo_event_id, action):
    """Record the copy outcome (opened/skipped_dup/exit/ignored) for a feed event."""
    conn.execute("UPDATE memecoin_events SET action=? WHERE fomo_event_id=?",
                 (action, fomo_event_id))
    conn.commit()


def set_event_real_decision(conn, fomo_event_id, decision, reason=None, now_iso=None):
    """Record the REAL-leg outcome (+ human reason) for a feed event, in columns disjoint from the
    paper `action` so neither leg clobbers the other. Makes every real-path skip/fill VISIBLE on the
    Activity/Decisions surface instead of the raw fomo firehose implying it was 'what we did'.
    A no-op if the event row is absent (e.g. a follow-activity event never inserted) — never raises."""
    if not fomo_event_id:
        return
    now_iso = now_iso or real_utc_now_iso()
    conn.execute(
        "UPDATE memecoin_events SET real_decision=?, real_decision_reason=?, real_decision_at=? "
        "WHERE fomo_event_id=?", (decision, reason, now_iso, fomo_event_id))
    conn.commit()


def set_event_chase(conn, fomo_event_id, wouldbe_price, chase_gap_pct):
    """Record the chase-guard counterfactual so we can later judge if skipping was right."""
    conn.execute(
        "UPDATE memecoin_events SET wouldbe_price=?, chase_gap_pct=? WHERE fomo_event_id=?",
        (wouldbe_price, chase_gap_pct, fomo_event_id))
    conn.commit()


def list_recent_events(conn, limit=50):
    """Most-recent COPY feed observations (curated), newest first, with outcome. Excludes
    action='candidate' rows — those are the discovery firehose, surfaced separately."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_events WHERE action IS NULL OR action != 'candidate' "
        "ORDER BY id DESC LIMIT ?", (int(limit),))]


def trader_performance(conn, mode="paper"):
    """Per-trader copy performance — the payoff table: which curated wallets are
    actually +EV to copy. Sorted by closed PnL desc. Holds parsed in Python (ISO).
    Scoped to `mode` (default paper)."""
    import statistics as _st
    from datetime import datetime as _dt
    ab = alpha_b_copy_stats_all(conn)   # honest αb copy PnL (arm criterion) beside the blended total
    from sniper.analysis.memecoin_entry_quality import wallet_candidates_from_db
    entry_quality = wallet_candidates_from_db(conn) if mode == "paper" else {}
    agg = {}
    for r in conn.execute("SELECT * FROM memecoin_positions WHERE execution_mode=?", (mode,)):
        p = dict(r)
        uid = p["source_user_id"]
        a = agg.setdefault(uid, {"source_user_id": uid, "handle": p.get("source_handle"),
                                 "entries": 0, "closed": 0, "wins": 0, "closed_pnl": 0.0,
                                 "_lags": [], "_holds": []})
        a["entries"] += 1
        if a["handle"] is None:
            a["handle"] = p.get("source_handle")
        if p.get("entry_lag_s") is not None and p["entry_lag_s"] >= 0:
            a["_lags"].append(p["entry_lag_s"])
        if not p["is_open"]:
            a["closed"] += 1
            rp = p.get("realized_pnl") or 0.0
            a["closed_pnl"] += rp
            if rp > 0:
                a["wins"] += 1
            try:
                hold = (_dt.fromisoformat(p["closed_at"]) - _dt.fromisoformat(p["opened_at"])).total_seconds()
                if hold >= 0:
                    a["_holds"].append(hold)
            except (ValueError, TypeError):
                pass
    out = []
    for a in agg.values():
        h = ab.get(a["source_user_id"]) or {}
        out.append({
            "source_user_id": a["source_user_id"], "handle": a["handle"],
            "entries": a["entries"], "closed": a["closed"],
            "closed_pnl": a["closed_pnl"],
            # honest αb copy PnL (alpha_b-only, non-real, fomo-excluded) — never blend this into
            # closed_pnl; the UI shows both, labeled, so a losing armed wallet can't hide.
            "ab_closed": h.get("n_closes", 0), "ab_pnl": h.get("pnl", 0.0),
            "entry_quality_candidate": entry_quality.get(a["source_user_id"]),
            "win_rate": (a["wins"] / a["closed"]) if a["closed"] else None,
            "avg_entry_lag_s": (sum(a["_lags"]) / len(a["_lags"])) if a["_lags"] else None,
            "median_hold_s": _st.median(a["_holds"]) if a["_holds"] else None,
        })
    out.sort(key=lambda r: (r["closed_pnl"] if r["closed"] else 0.0), reverse=True)
    return out


def trader_arm_matrix(conn, mode="paper", min_n=3, chain=None):
    """Honest wallet x arm matrix over each wallet's exact shared origin set, scoped to ONE chain.

    `chain` is a canonical key from sniper.services.memecoin_chains. There is NO pooled scope:
    a value that does not resolve raises rather than silently returning every chain. Passing
    None keeps the cross-chain view for legacy callers, which must not be used to compare chains.
    """
    from sniper.services.memecoin_chains import network_ids_for, resolve_chain

    ARM_ORDER = {"control": 0, "real_shadow": 1, "scalp_c2": 2, "scalp_c": 3,
                 "alpha_b": 4, "momentum_a": 5, "trail_d": 6}
    chain_sql, chain_params = "", ()
    if chain is not None:
        key = resolve_chain(chain)
        if key is None:
            raise ValueError(f"unknown chain {chain!r}; chains are never pooled")
        chain_params = network_ids_for(key)
        chain_sql = " AND network_id IN (%s)" % ",".join("?" * len(chain_params))
    raw = {}
    for r in conn.execute(
        "SELECT id,source_user_id,source_handle,strategy,origin_event_id,realized_pnl "
        "FROM memecoin_positions WHERE execution_mode=? AND is_open=0 "
        "AND realized_pnl IS NOT NULL AND closed_at IS NOT NULL "
        "AND trim(COALESCE(origin_event_id,''))<>'' "
        # Comparable evidence only (2026-08-04 review). This is the REPORT's full measurable-
        # outcome test, not a partial re-derivation of it: the first attempt used
        # `observed_exit_sql()` alone and admitted partial-executable-then-noquote closes the
        # report censors, publishing them as arm evidence and feeding them to funded selection.
        "AND " + measurable_outcome_sql() + " "
        "AND source_user_id IN (SELECT source_user_id FROM memecoin_wallets "
        "WHERE COALESCE(excluded,0)=0)" + chain_sql, (mode, *chain_params)):
        uid = r["source_user_id"]
        arm = r["strategy"] or "control"
        wallet = raw.setdefault(uid, {"handle": r["source_handle"], "events": {}})
        if wallet["handle"] is None:
            wallet["handle"] = r["source_handle"]
        event = wallet["events"].setdefault(r["origin_event_id"], {})
        event.setdefault(arm, []).append(r)

    # A wallet needs >=2 arms over a SHARED origin event for its cells to be comparable
    # (same coin, same entry, different exit rule). Wallets that fail that are reported in
    # `dropped` with the reason rather than silently vanishing — the operator asked for every
    # wallet copied on the chain, and a silent omission reads as "no such wallet".
    agg, arms, dropped = {}, set(), []
    for uid, wallet in raw.items():
        wallet_arms = {
            arm for event in wallet["events"].values() for arm in event
        }
        if len(wallet_arms) < 2:
            dropped.append({"source_user_id": uid, "handle": wallet["handle"],
                            "reason": "only 1 arm", "signals": len(wallet["events"])})
            continue
        shared = {
            event_id for event_id, by_arm in wallet["events"].items()
            if all(len(by_arm.get(arm, [])) == 1 for arm in wallet_arms)
        }
        if not shared:
            dropped.append({"source_user_id": uid, "handle": wallet["handle"],
                            "reason": "no shared origin", "signals": len(wallet["events"])})
            continue
        a = {"source_user_id": uid, "handle": wallet["handle"], "by_arm": {},
             "total_pnl": 0.0, "total_closed": 0, "shared_origins": len(shared),
             # `signals` = distinct origin EVENTS, not positions: one call fanned to six arms
             # is one signal. total_closed counts the positions.
             "signals": len(shared)}
        for arm in wallet_arms:
            values = [float(wallet["events"][event][arm][0]["realized_pnl"])
                      for event in shared]
            cell = {"pnl": sum(values), "closed": len(values),
                    "wins": sum(value > 0 for value in values)}
            a["by_arm"][arm] = cell
            a["total_pnl"] += cell["pnl"]
            a["total_closed"] += cell["closed"]
            arms.add(arm)
        agg[uid] = a
    for a in agg.values():
        a["status"] = "comparable"
    wallets = sorted(agg.values(), key=lambda w: w["total_pnl"], reverse=True)
    for d in sorted(dropped, key=lambda d: -d["signals"]):
        wallets.append({"source_user_id": d["source_user_id"], "handle": d["handle"],
                        "by_arm": {}, "total_pnl": 0.0, "total_closed": 0,
                        "shared_origins": 0, "signals": d["signals"],
                        "status": "not_comparable", "note": d["reason"]})

    # EVERY trader in the system appears, even with no signal on this chain — the roster IS
    # the evidence surface, and a wallet that is silent here is a fact worth seeing rather
    # than an absence the operator has to infer. Zeroes, never fabricated numbers.
    listed = {w["source_user_id"] for w in wallets}
    for row in conn.execute(
            "SELECT source_user_id, handle FROM memecoin_wallets "
            "WHERE COALESCE(excluded,0)=0 ORDER BY handle"):
        if row["source_user_id"] in listed:
            continue
        wallets.append({"source_user_id": row["source_user_id"], "handle": row["handle"],
                        "by_arm": {}, "total_pnl": 0.0, "total_closed": 0,
                        "shared_origins": 0, "signals": 0,
                        "status": "no_signal", "note": "no closed copy on this chain"})

    arms_sorted = sorted(arms, key=lambda x: ARM_ORDER.get(x, 9))
    return {"mode": mode, "min_n": min_n, "arms": arms_sorted, "wallets": wallets,
            "chain": chain, "dropped": sorted(dropped, key=lambda d: -d["signals"]),
            "copied_wallets": len(raw), "roster_wallets": len(wallets),
            "exact_paired": True, "honest_only": True, "current_wallets_only": True}


def wallet_stats(conn, mode="paper"):
    """Per-curated-wallet copy rollup keyed by source_user_id: open/closed counts, realized PnL,
    win rate. Powers the inline 'who is worth keeping' decision. Scoped to `mode` (default 'paper',
    the research board) so real fills never inflate the paper copy stats; mode=None for both."""
    mode_sql = "" if mode is None else " AND p.execution_mode=?"
    params = () if mode is None else (mode,)
    out = {}
    for row in conn.execute(
        "SELECT w.source_user_id,w.handle,"
        "SUM(CASE WHEN p.id IS NOT NULL AND p.is_open=1 THEN 1 ELSE 0 END) open_count,"
        "SUM(CASE WHEN p.is_open=0 AND p.realized_pnl IS NOT NULL THEN 1 ELSE 0 END) closed_count,"
        "COALESCE(SUM(CASE WHEN p.is_open=0 THEN p.realized_pnl ELSE 0 END),0) realized_pnl,"
        "SUM(CASE WHEN p.is_open=0 AND p.realized_pnl>0 THEN 1 ELSE 0 END) wins "
        "FROM memecoin_wallets w LEFT JOIN memecoin_positions p "
        "ON p.source_user_id=w.source_user_id" + mode_sql + " "
        "GROUP BY w.source_user_id,w.handle", params):
        closed = int(row["closed_count"] or 0)
        out[row["source_user_id"]] = {
            "source_user_id": row["source_user_id"], "handle": row["handle"],
            "open_count": int(row["open_count"] or 0), "closed_count": closed,
            "realized_pnl": float(row["realized_pnl"] or 0.0),
            "win_rate": (int(row["wins"] or 0) / closed) if closed else None,
        }
    return out


def real_stats_by_wallet(conn, chain):
    """Funded (execution_mode='real') record per wallet on ONE chain, keyed by source_user_id.

    Chains are never pooled (2026-07-30): an unresolvable `chain` raises rather than
    silently returning every chain, exactly like trader_arm_matrix.
    """
    from sniper.services.memecoin_chains import network_ids_for, resolve_chain
    key = resolve_chain(chain)
    if key is None:
        raise ValueError(f"unknown chain {chain!r}; chains are never pooled")
    nets = tuple(network_ids_for(key))
    # ROLLING WINDOWS (2026-09-22, operator): the funded-authority board shows 24h / 7d / 30d
    # beside all-time so a wallet that was good and started to degrade reads as such. Same
    # robot-only, same chain scope, same closed-row definition as the all-time columns.
    from datetime import datetime, timedelta, timezone
    _now = datetime.now(timezone.utc)
    cut24h, cut7d, cut30d = ((_now - timedelta(hours=24)).isoformat(),
                             (_now - timedelta(days=7)).isoformat(),
                             (_now - timedelta(days=30)).isoformat())
    out = {}
    for row in conn.execute(
        "SELECT source_user_id,"
        " SUM(CASE WHEN is_open=1 THEN 1 ELSE 0 END) AS open_count,"
        " SUM(CASE WHEN is_open=0 AND realized_pnl IS NOT NULL THEN 1 ELSE 0 END) AS closed_count,"
        " COALESCE(SUM(CASE WHEN is_open=0 THEN realized_pnl ELSE 0 END),0) AS realized_pnl,"
        " SUM(CASE WHEN is_open=0 AND realized_pnl>0 THEN 1 ELSE 0 END) AS wins,"
        " MAX(opened_at) AS last_entry,"
        " SUM(CASE WHEN is_open=0 AND realized_pnl IS NOT NULL AND closed_at>=? THEN 1 ELSE 0 END) AS n24h,"
        " COALESCE(SUM(CASE WHEN is_open=0 AND closed_at>=? THEN realized_pnl ELSE 0 END),0) AS pnl24h,"
        " SUM(CASE WHEN is_open=0 AND realized_pnl IS NOT NULL AND closed_at>=? THEN 1 ELSE 0 END) AS n7d,"
        " COALESCE(SUM(CASE WHEN is_open=0 AND closed_at>=? THEN realized_pnl ELSE 0 END),0) AS pnl7d,"
        " SUM(CASE WHEN is_open=0 AND realized_pnl IS NOT NULL AND closed_at>=? THEN 1 ELSE 0 END) AS n30d,"
        " COALESCE(SUM(CASE WHEN is_open=0 AND closed_at>=? THEN realized_pnl ELSE 0 END),0) AS pnl30d,"
        # RETURN ON NOTIONAL (2026-09-22, operator): a dollar PnL depends on the clip, and the clip
        # differs on almost every entry, so two wallets' dollars do not compare. pnl / notional does.
        " COALESCE(SUM(CASE WHEN is_open=0 AND realized_pnl IS NOT NULL THEN entry_notional ELSE 0 END),0) AS notional_all,"
        " COALESCE(SUM(CASE WHEN is_open=0 AND realized_pnl IS NOT NULL AND closed_at>=? THEN entry_notional ELSE 0 END),0) AS notional24h,"
        " COALESCE(SUM(CASE WHEN is_open=0 AND realized_pnl IS NOT NULL AND closed_at>=? THEN entry_notional ELSE 0 END),0) AS notional7d,"
        " COALESCE(SUM(CASE WHEN is_open=0 AND realized_pnl IS NOT NULL AND closed_at>=? THEN entry_notional ELSE 0 END),0) AS notional30d"
        # ROBOT ONLY (2026-08-27). A manual entry can carry a source_user_id — naming a trader is
        # how the operator buys the mirror-close — so an operator pick lands on that trader's
        # record. This is not display: `real_demote` reads realized_total against
        # MEMECOIN_REAL_DEMOTE_LOSS_USD, so a manual loss could demote a wallet for a trade it
        # never signalled. One attributed manual close exists today (-$10.17), which moved
        # DegenCapitalLLC to $185.19 against a true $195.36.
        + (" FROM memecoin_positions WHERE execution_mode='real' AND network_id IN (%s)"
           " AND " + robot_decision_sql() + " AND " + WALLET_GRADE_SQL +
           " GROUP BY source_user_id") % ",".join("?" * len(nets)),
            (cut24h, cut24h, cut7d, cut7d, cut30d, cut30d, cut24h, cut7d, cut30d) + nets):
        closed = int(row["closed_count"] or 0)

        def _window(n_key, pnl_key, notional_key):
            pnl = float(row[pnl_key] or 0.0)
            notional = float(row[notional_key] or 0.0)
            return {"closed": int(row[n_key] or 0), "pnl": pnl, "notional": notional,
                    "roi_pct": (pnl / notional * 100.0) if notional > 0 else None}

        notional_all = float(row["notional_all"] or 0.0)
        out[row["source_user_id"]] = {
            "open_count": int(row["open_count"] or 0), "closed_count": closed,
            "realized_pnl": float(row["realized_pnl"] or 0.0),
            "notional": notional_all,
            "roi_pct": (float(row["realized_pnl"] or 0.0) / notional_all * 100.0) if notional_all > 0 else None,
            "win_rate": (int(row["wins"] or 0) / closed) if closed else None,
            "windows": {
                "24h": _window("n24h", "pnl24h", "notional24h"),
                "7d": _window("n7d", "pnl7d", "notional7d"),
                "30d": _window("n30d", "pnl30d", "notional30d"),
            },
            "last_entry": row["last_entry"],
        }
    return out


DETECTION_BLOCK_TIMES_SQL = ("SELECT signature, block_time FROM onchain_wallet_detections "
                             "WHERE source_user_id=? AND mint IN (%s)")


def detection_block_times(conn, source_user_id, mints):
    """{signature: block_time (epoch s) or None} of one wallet's on-chain detections on `mints`: the
    chain time of its buys (S2, Codex review 2026-10-07), one read on idx_mc_onchain_detect_wallet_coin,
    bounded by the caller's mint list."""
    mints = list(dict.fromkeys(mt for mt in mints if mt))
    if not mints:
        return {}
    return {sig: bt for sig, bt in conn.execute(
        DETECTION_BLOCK_TIMES_SQL % ",".join("?" * len(mints)), (source_user_id, *mints))}


def real_positions_for_wallet(conn, source_user_id, chain):
    """ONE wallet's funded positions on ONE chain, under real_stats_by_wallet's own filters (funded,
    robot-only, grade exclusions), oldest first. One indexed read (idx_mc_pos_wallet_mode_open), for
    the Wallet Map's supporting coins (D3)."""
    from sniper.services.memecoin_chains import network_ids_for, resolve_chain
    key = resolve_chain(chain)
    if key is None:
        raise ValueError(f"unknown chain {chain!r}; chains are never pooled")
    nets = tuple(network_ids_for(key))
    return [dict(r) for r in conn.execute(
        "SELECT id, mint, network_id, opened_at, closed_at, entry_notional, entry_lag_s, fill_gap_pct, "
        "total_copy_drift_pct, realized_pnl, exit_reason, is_open, origin_event_id, attempt_id "
        "FROM memecoin_positions "
        "WHERE source_user_id=? AND execution_mode='real' AND network_id IN (%s) AND %s AND %s "
        "ORDER BY opened_at, id" % (",".join("?" * len(nets)), robot_decision_sql(), WALLET_GRADE_SQL),
        (source_user_id,) + nets)]


def entry_attempts_for_coins(conn, source_user_id, network_id, mints):
    """{mint: [attempt rows, oldest first]} of ONE wallet's FUNDED entry attempts on these coins: one
    read on idx_mc_entry_attempt_coin, the trace columns only (never trace_json). A j7_shadow row is
    non-funded by doctrine (brain 137fb844) and is never returned (Gamma HOLD on Wallet Map D3)."""
    mints = sorted({str(x) for x in mints if x})
    out = {}
    for i in range(0, len(mints), 500):
        chunk = mints[i:i + 500]
        for r in conn.execute(
                "SELECT attempt_id, origin_event_id, mint, decision_started_at, terminal_status, terminal_reason, "
                "delivery_lane FROM memecoin_entry_attempts WHERE network_id=? AND mint IN (%s) AND source_user_id=? "
                "AND COALESCE(delivery_lane, '') <> 'j7_shadow' AND COALESCE(terminal_status, '') <> 'shadow' "
                "ORDER BY decision_started_at, attempt_id" % ",".join("?" * len(chunk)),
                (network_id, *chunk, source_user_id)):
            out.setdefault(r["mint"], []).append(dict(r))
    return out


def update_entry_attempt_fields(conn, attempt_id, **fields):
    """Set named columns on one in-flight attempt row. Returns True iff a row was updated.

    Column names come from CALLERS IN THIS REPO ONLY and are validated against the live table, so
    this can never become a path for arbitrary SQL. It deliberately does NOT touch terminal_status
    or completed_at: those belong to the trace's own finish, and writing them here would race it.
    """
    if not attempt_id or not fields:
        return False
    allowed = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_entry_attempts)")}
    allowed -= {"attempt_id", "terminal_status", "completed_at"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("unknown entry-attempt column(s): %s" % ", ".join(sorted(unknown)))
    cols = sorted(fields)
    cur = conn.execute(
        "UPDATE memecoin_entry_attempts SET %s WHERE attempt_id=?"
        % ", ".join("%s=?" % c for c in cols),
        [fields[c] for c in cols] + [attempt_id])
    conn.commit()
    return cur.rowcount == 1


# --- per-wallet gate exemptions -------------------------------------------------------------------
#
# SELECTION ONLY. The exemptible set is exactly the families a MANUAL entry already bypasses, which
# is the same selection-versus-safety line drawn everywhere else in this system: a gate that judges
# whether the coin is a GOOD BUY may be waived for a wallet the operator trusts; a gate that
# protects the capital may not. Safety families — execution_premium and route_over_mid included,
# both being cost gates — are absent from this set and a row naming one is never read.
EXEMPTIBLE_GATE_FAMILIES = frozenset({
    "chase_past_entry", "chase_first_seen",
    "m5_txns_under_floor", "m5_txns_unreadable",
    "m5_turnover_under_floor", "m5_turnover_over_ceiling", "turnover_under_floor",
    "coin_age_ceiling", "coin_band_unreservable", "sell_share",
    "winner_reentry_price", "winner_reentry_daily_cap",
    "reentry_cooldown", "losing_reentry",
    # SOURCE-BUY FLOOR (2026-08-28). Selection: it judges whether the COPIED trader's buy was
    # conviction or bag work, which is a judgement about the signal, not a protection of capital.
    "source_buy_floor",
})

WALLET_CHASE_CAP_MAX_PCT = 75.0
WALLET_CHASE_CAP_NETWORKS = frozenset({"4663"})
WALLET_CHASE_CAP_BANDS = frozenset({"nano", "assumed_nano"})


def set_entry_knob_override(conn, name, value, *, active, actor, reason, now_iso):
    """Set or clear ONE global entry-knob override, and write the audit row.

    The registry decides what is adjustable, so this refuses a locked or unknown name here rather
    than letting an unreadable row sit in the table. `value` is stored as JSON; `active=False`
    clears the override and the knob reverts to its `.env` default on the runner's next entry.
    """
    import json as _json
    from sniper.services.memecoin_entry_knobs import KNOBS, ADJUSTABLE
    knob = KNOBS.get(name)
    if knob is None:
        raise ValueError(f"unknown entry knob {name!r}")
    if name not in ADJUSTABLE:
        raise ValueError(f"{name} is locked: {knob.locked_reason}")
    old = conn.execute("SELECT value FROM memecoin_entry_knob_overrides WHERE name=? AND active=1",
                       (name,)).fetchone()
    old_value = old[0] if old else None
    if not active:
        conn.execute("UPDATE memecoin_entry_knob_overrides SET active=0, actor=?, reason=?, "
                     "updated_at=? WHERE name=?", (actor, reason, now_iso, name))
        stored = None
        action = "clear"
    else:
        stored = _json.dumps(knob.coerce(value))     # ValueError here means it never gets written
        conn.execute(
            "INSERT INTO memecoin_entry_knob_overrides(name,value,active,actor,reason,updated_at) "
            "VALUES(?,?,1,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "value=excluded.value, active=1, actor=excluded.actor, reason=excluded.reason, "
            "updated_at=excluded.updated_at",
            (name, stored, actor, reason, now_iso))
        action = "set"
    conn.execute("INSERT INTO memecoin_entry_knob_audit(name,old_value,new_value,action,actor,"
                 "reason,at) VALUES(?,?,?,?,?,?,?)",
                 (name, old_value, stored, action, actor, reason, now_iso))
    conn.commit()
    return {"name": name, "action": action, "value": stored}


def entry_knob_audit(conn, limit=50):
    """Most recent knob changes, newest first — the cockpit's own record of what was moved."""
    try:
        rows = conn.execute(
            "SELECT name, old_value, new_value, action, actor, reason, at "
            "FROM memecoin_entry_knob_audit ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    except Exception:
        logger.exception("entry knob audit unreadable")
        return []
    return [dict(r) for r in rows]


_SOURCE_TX_EVM = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SOURCE_TX_SOLANA = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{80,90}$")


# ---- entry feature capture reads (spec 2026-09-29-memecoin-entry-feature-capture-design.md) ----
# Read-only, for the capture process's read-only ledger connection. Nothing on the entry path
# calls these.

def terminal_attempts_completed_since(conn, since_iso, *, after=None, limit=200):
    """Terminal entry attempts completed at or after `since_iso`, oldest first. `after` is the
    (completed_at, attempt_id) of the previous page's last row: the next page starts strictly after
    it, so equal completed_at stamps neither repeat nor drop across a page boundary."""
    if after is None:
        where, args = "completed_at >= ?", (since_iso,)
    else:
        where = "completed_at >= ? AND (completed_at > ? OR attempt_id > ?)"
        args = (after[0], after[0], after[1])
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_entry_attempts WHERE terminal_status IS NOT NULL "
        f"AND completed_at IS NOT NULL AND {where} "
        "ORDER BY completed_at, attempt_id LIMIT ?", (*args, int(limit)))]


def source_record_before(conn, source_user_id, before_iso):
    """(n, pnl_sum, wins) of this source's funded closes that closed before `before_iso`.
    pnl_sum is None when n is 0 (no record, not a zero record)."""
    row = conn.execute(
        "SELECT COUNT(*), SUM(realized_pnl), SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) "
        "FROM memecoin_positions WHERE execution_mode='real' AND source_user_id=? "
        "AND closed_at IS NOT NULL AND realized_pnl IS NOT NULL AND closed_at < ?",
        (source_user_id, before_iso)).fetchone()
    n = int(row[0] or 0)
    return n, (float(row[1]) if n else None), int(row[2] or 0)


def source_buys_between(conn, source_user_id, since_iso, before_iso, *, received_before_iso):
    """This source's feed buys with created_at in [since_iso, before_iso) that WE HAD RECEIVED by
    `received_before_iso` (the decision): (mint, network_id, their_size_usd, created_at).
    created_at is the relay's clock, which can run ahead of our receipt (4663 J7: +23 s seen on
    09-29), so the causal bound is received_at, our own observed_real_utc stamp. created_at
    carries 'Z' and '+00:00' spellings; both sides are normalised."""
    norm = lambda iso: str(iso).replace("Z", "+00:00")
    return [dict(r) for r in conn.execute(
        "SELECT mint, network_id, their_size_usd, created_at FROM memecoin_events "
        "WHERE source_user_id=? AND side='buy' "
        "AND replace(created_at,'Z','+00:00') >= ? AND replace(created_at,'Z','+00:00') < ? "
        "AND received_at IS NOT NULL AND replace(received_at,'Z','+00:00') <= ?",
        (source_user_id, norm(since_iso), norm(before_iso), norm(received_before_iso)))]


def prior_buyers_60m_causal(conn, *, mint, network_id, source_user_id, before_iso,
                            received_before_iso, window_s=3600):
    """prior_buyers_60m, restricted to buys WE HAD RECEIVED by `received_before_iso`: the entry
    feature capture recomputes consensus after the decision and must not count a buy that
    arrived later. None on missing inputs or an unparseable stamp."""
    if not (mint and before_iso and source_user_id and received_before_iso):
        return None
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT CASE WHEN "
            "strftime('%s', ?) - strftime('%s', created_at) BETWEEN 0 AND ? "
            "AND strftime('%s', created_at) < strftime('%s', ?) "
            "AND received_at IS NOT NULL AND replace(received_at,'Z','+00:00') <= ? "
            "THEN source_user_id END), "
            "SUM(CASE WHEN strftime('%s', created_at) IS NULL THEN 1 ELSE 0 END) "
            "FROM memecoin_events "
            f"WHERE mint=? AND {COIN_NETWORK_SQL}=? AND side='buy' "
            "AND source_user_id IS NOT NULL AND source_user_id<>?",
            (before_iso, int(window_s), before_iso, str(received_before_iso).replace("Z", "+00:00"),
             mint, normalized_coin_network(network_id, mint), source_user_id)).fetchone()
        if not row or int(row[1] or 0) > 0:
            return None
        return int(row[0]) if row[0] is not None else None
    except Exception:
        return None


def entry_attempt_by_id(conn, attempt_id):
    """One entry attempt by its id, or None."""
    row = conn.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?",
                       (str(attempt_id),)).fetchone()
    return dict(row) if row else None


def real_positions_open_at(conn, network_id, at_iso):
    """How many real positions on `network_id` were open at `at_iso` (opened at or before it and not
    closed by then). The fill stamps (`*_recorded_at`) decide where present, as is_open follows the
    fill and `closed_at` is often the exit decision. An open position with tracking paused is not
    counted, as in solana_book_flat. Both halves search idx_mc_pos_mode_open_closed on
    (execution_mode, is_open); the closed half scans the closed real rows (1,780, 2.3 ms on 10-02)."""
    still = conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions WHERE "
        "execution_mode='real' AND is_open=1 AND network_id=? "
        "AND COALESCE(opened_recorded_at, opened_at) <= ? AND COALESCE(tracking_paused_at,'')=''",
        (str(network_id), at_iso)).fetchone()[0]
    closed_later = conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions WHERE "
        "execution_mode='real' AND is_open=0 AND COALESCE(closed_recorded_at, closed_at) > ? "
        "AND network_id=? AND COALESCE(opened_recorded_at, opened_at) <= ?",
        (at_iso, str(network_id), at_iso)).fetchone()[0]
    return int(still) + int(closed_later)


def solana_book_flat(conn):
    """The research-read rule: no open real Solana position AND no reserved Solana entry. A caller
    treats an error as NOT flat (a failed check never unlocks a research read)."""
    open_n = conn.execute(
        "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' AND is_open=1 "
        "AND network_id='solana' AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]
    reserved = conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries WHERE status='reserved' "
                            "AND network_id='solana'").fetchone()[0]
    return open_n == 0 and reserved == 0


def own_tx_hashes(conn, network_id, mint):
    """Every transaction hash of OUR OWN trades in one coin (entry attempts, the fills of the
    positions they opened, reservations), lowercased: our own trades are not the market's order
    flow. Exact (network_id, mint) on indexed paths; ledger mints are stored in their canonical
    case (EVM lowercase)."""
    out, positions = set(), set()
    for tx, pid in conn.execute(
            "SELECT tx_sig, position_id FROM memecoin_entry_attempts WHERE network_id=? AND mint=?",
            (str(network_id), mint)):
        if tx:
            out.add(str(tx).lower())
        if pid is not None:
            positions.add(pid)
    for pid in positions:
        out.update(str(r[0]).lower() for r in conn.execute(
            "SELECT tx_sig FROM memecoin_fills WHERE position_id=? AND tx_sig IS NOT NULL", (pid,)))
    out.update(str(r[0]).lower() for r in conn.execute(
        "SELECT tx_sig FROM memecoin_pending_entries WHERE network_id=? AND mint=? "
        "AND tx_sig IS NOT NULL", (str(network_id), mint)))
    return out


def source_event_size_usd(conn, origin_event_id):
    """The copied trade's own USD size, from the event it came from: the feed event
    (memecoin_events.fomo_event_id) or, for an on-chain origin, the detection. None when neither
    holds it."""
    if not origin_event_id:
        return None
    row = conn.execute("SELECT their_size_usd FROM memecoin_events WHERE fomo_event_id=?",
                       (origin_event_id,)).fetchone()
    if row and row[0] is not None:
        return float(row[0])
    sig = source_tx_of(origin_event_id)
    if sig:
        row = conn.execute("SELECT source_size_usd FROM onchain_wallet_detections WHERE signature=?",
                           (sig,)).fetchone()
        if row and row[0] is not None:
            return float(row[0])
    return None


def source_tx_of(event_id):
    """The source transaction an event id carries, found by SHAPE (an EVM 0x+64-hex hash or a
    base58 Solana signature), or None. The forms differ by lane: 'swap:raybot:<tx>:<owner>:<mint>',
    'pump:<sig>:...', 'onchain:<sig>', 'onchain:<chain>:<tx>:<mint>', 'slotlane:<sig>...'; J7's
    'swap:<uuid>' and manual ids carry none."""
    for part in str(event_id or "").split(":"):
        if _SOURCE_TX_EVM.match(part) or _SOURCE_TX_SOLANA.match(part):
            return part
    return None


PRIOR_PAIR_S = 120.0                 # a uuid report and its transaction record are this close
PRIOR_PAIR_PCT = 0.05                # and their sizes this close


def _prior_stamp(raw):
    from datetime import datetime
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()


def _prior_tx_key(tx):
    return tx.lower() if tx.startswith("0x") else tx


def _prior_block_t(raw):
    """A detection's chain time, or None when it carries none. The EVM watcher stores whatever the
    node's log holds, so 0 is 'no blockTimestamp', never 1970."""
    try:
        t = float(raw)
    except (TypeError, ValueError):
        return None
    return t if t > 0 else None


def prior_net_from_rows(events, detections, *, judged_id, judged_usd, before_t, window_s):
    """The net USD (buys minus sells) of the wallet's trades of one coin EARLIER than the judged trade,
    from `events` rows (fomo_event_id, side, their_size_usd, created_at) and `detections` rows
    (signature, source_size_usd, block_time, detected_at). Pure; the caller read the rows.

    ONE TRADE, HOWEVER MANY REPORTS (operator 2026-09-30). A trade is keyed by its transaction when a
    report carries one: J7's 'swap:raybot:<tx>:...', a 'pump:<sig>' or lane id, or the watcher's
    detection (so a buy only the on-chain lane saw counts). Its size is the event's, else the
    detection's. J7's 'swap:<uuid>' report carries no transaction; it IS a tx-carrying trade of the
    same side within PRIOR_PAIR_S and PRIOR_PAIR_PCT, each transaction taken once, the nearest in
    time first; a report that pairs with none is a trade of its own.

    TWO CLOCKS. A uuid report is stamped with Fomo's app time, a median 18 s BEFORE the chain time of
    its transaction. A trade is earlier than the judged one on the judged trade's own clock when it
    has a stamp on that clock, else on the stamp it has. `before_t` is the judged event's own stamp;
    for an on-chain lane event that is the moment the watcher SAW the trade, about a second after
    its block. When the judged trade's own record gives its chain time, that is the reference for
    transactions, so another buy in the same chain second is not an earlier buy (their order inside
    the second is unknown, and counting it would admit both of two simultaneous slices).
    A detection's chain time is its transaction's time whoever else reported it: J7 stamps a
    transaction 0.2 to 2.4 s after its block. A block time of 0 or less is no chain time (the EVM
    watcher stores 0 when the node's log has none); that record is timed by when the watcher saw it.

    KNOWN LIMITS. Two buys in one chain second never count each other (6 attempts in 7 days on
    2026-09-30); their slot would order them and is not read here. An earlier buy that only J7
    reported keeps J7's stamp, so one in the last 2.4 s before a trade judged on chain time can read
    as not earlier.

    An unknown size adds nothing and pairs with nothing; it is never a known 0."""
    judged_tx = source_tx_of(judged_id)
    trades = {}                                        # tx key -> {usd, side, tx_t, app_t}
    for fid, side, usd, created in events:
        tx = source_tx_of(fid)
        if tx is not None:
            trades[_prior_tx_key(tx)] = {"usd": usd, "side": side, "tx_t": _prior_stamp(created), "app_t": None}
    for sig, usd, block_time, detected in detections:
        tx = source_tx_of(sig)
        if tx is None:
            continue
        block_t = _prior_block_t(block_time)
        known = trades.setdefault(_prior_tx_key(tx), {"usd": usd, "side": "buy", "tx_t": None, "app_t": None})
        if known["usd"] is None:
            known["usd"] = usd
        if block_t is not None:                        # chain time beats J7's stamp of the same transaction
            known["tx_t"] = block_t
        elif known["tx_t"] is None:
            known["tx_t"] = _prior_stamp(detected)
    judged = None
    if judged_tx is not None:                          # the judged trade pairs too, in the ledger or not
        judged = trades.setdefault(_prior_tx_key(judged_tx),
                                   {"usd": judged_usd, "side": "buy", "tx_t": before_t, "app_t": None})
        if judged["usd"] is None:
            judged["usd"] = judged_usd
    reports = [(_prior_stamp(created), fid, side, usd) for fid, side, usd, created in events
               if source_tx_of(fid) is None]
    if judged_tx is None and not any(fid == judged_id for _t, fid, _s, _u in reports):
        reports.append((before_t, judged_id, "buy", judged_usd))
    lone, used = [], set()
    for app_t, fid, side, usd in sorted(reports, key=lambda r: r[0]):
        twin = None
        if usd:
            near = [(abs(tr["tx_t"] - app_t), key) for key, tr in trades.items()
                    if key not in used and tr["side"] == side and tr["usd"]
                    and abs(tr["tx_t"] - app_t) <= PRIOR_PAIR_S
                    and abs(usd / tr["usd"] - 1.0) <= PRIOR_PAIR_PCT + 1e-9]
            if near:
                twin = trades[min(near)[1]]
                used.add(min(near)[1])
                twin["app_t"] = app_t
        if twin is None and fid != judged_id:
            lone.append({"usd": usd, "side": side, "tx_t": None, "app_t": app_t})
    # The judged trade itself is never earlier than its own stamp, on either clock: no exclusion
    # by identity is needed, only the reference it is compared against.
    by_app = judged_tx is None                         # the judged event is a uuid report: app clock
    tx_ref = before_t if judged is None else min(before_t, judged["tx_t"])
    net = 0.0
    for tr in list(trades.values()) + lone:
        if not tr["usd"]:
            continue
        on_app = tr["tx_t"] is None or (by_app and tr["app_t"] is not None)
        t, ref = (tr["app_t"], before_t) if on_app else (tr["tx_t"], tx_ref)
        if ref - float(window_s) <= t < ref:
            net += tr["usd"] if tr["side"] == "buy" else -tr["usd"]
    return net


def source_prior_net_buy_usd(conn, *, source_user_id, network_id, mint, before_iso, window_s,
                             exclude_event_id=None, judged_usd=None):
    """The copied trader's net USD into one coin (buys minus sells) in the `window_s` before
    `before_iso`, EXCLUDING the trade being judged. 0.0 when there is nothing; None when unreadable.

    Operator 2026-09-13: a trader who scales in with spaced buys (APU: $200 then $100 four seconds
    apart) was judged buy by buy against a floor that only the total clears.

    ONE TRADE IS COUNTED ONCE (2026-09-29, 2026-09-30). The judged trade and other reports of it are
    not earlier trades: J7's tx-id report of the same transaction (CARDANO e3c706bf: $1,000 judged
    plus J7's $998.50), and J7's 'swap:<uuid>' report of it, which carries no transaction and is
    stamped seconds BEFORE it (2841a5c0: a $270 buy judged as $540). `judged_usd` is the judged
    trade's own size; without it nothing pairs with the judged trade by amount.
    A BUY ONLY THE ON-CHAIN LANE SAW COUNTS (2026-09-30): onchain_wallet_detections is read beside
    memecoin_events, deduped by transaction. The rule is `prior_net_from_rows`.

    WHAT STILL STOPS A DRIP BUYER (replay 2026-09-30, docs/audits/2026-09-30-source-buy-prior-sum.md).
    With on-chain buys in the sum, the ratio leg ALONE would have passed 230 more attempts in 7 days,
    214 of them buys under $50 by wallets that drip-buy one coin (pump:4vw, 191). Only the absolute
    band on the SINGLE buy still refuses those: MEMECOIN_REAL_MIN_SOURCE_BUY_USD, $197 in the traces
    of that day and 0 (off) by code default. Turning that band off, deleting its `.env` line, or exempting a
    chain or a wallet from the source_buy_floor family (MEMECOIN_GATE_EXEMPT_FAMILIES_<chain>) turns
    them into admissions.

    TWO READS, EACH A RANGE ON AN INDEX: memecoin_events on (source_user_id, created_at) and
    onchain_wallet_detections on (source_user_id, mint, detected_at). created_at carries both 'Z' and
    '+00:00' spellings; the SQL bound is the second-resolution prefix both share, and the exact
    window is applied to the parsed stamps.
    """
    if not source_user_id or not mint or not before_iso or not window_s or float(window_s) <= 0:
        return 0.0
    try:
        from datetime import datetime, timedelta
        end = datetime.fromisoformat(str(before_iso).replace("Z", "+00:00"))
        lo = (end - timedelta(seconds=float(window_s) + PRIOR_PAIR_S + 1)).strftime("%Y-%m-%dT%H:%M:%S")
        hi = (end + timedelta(seconds=PRIOR_PAIR_S + 2)).strftime("%Y-%m-%dT%H:%M:%S")
        events = conn.execute(
            "SELECT fomo_event_id, side, their_size_usd, created_at FROM memecoin_events "
            "WHERE source_user_id=? AND created_at > ? AND created_at < ? AND mint=? "
            "AND COALESCE(network_id,'')=COALESCE(?, COALESCE(network_id,''))",
            (source_user_id, lo, hi, mint, network_id)).fetchall()
        # A detection is read by when the watcher saw it, a little after its chain time.
        detections = conn.execute(
            "SELECT signature, source_size_usd, block_time, detected_at FROM onchain_wallet_detections "
            "WHERE source_user_id=? AND mint=? AND detected_at > ? AND detected_at < ? "
            "AND network_id=COALESCE(?, network_id)",
            (source_user_id, mint, lo, (end + timedelta(seconds=PRIOR_PAIR_S + 600)).strftime(
                "%Y-%m-%dT%H:%M:%S"), network_id)).fetchall()
        return prior_net_from_rows(
            [tuple(r) for r in events], [tuple(r) for r in detections], judged_id=exclude_event_id,
            judged_usd=judged_usd, before_t=end.timestamp(), window_s=float(window_s))
    except Exception:
        logger.exception("source prior net buy unreadable for %s / %s", source_user_id, mint)
        return None


def holding_candidates(conn, network_id, *, limit=501):
    """Bounded known inventory, including closed bags and previously discovered airdrops."""
    limit = max(1, int(limit))
    known = {}
    for mint, symbol, decimals in conn.execute(
            "SELECT mint, symbol, decimals FROM memecoin_other_holdings WHERE network_id=? "
            "ORDER BY mint LIMIT ?", (network_id, limit)):
        mint = str(mint).lower()
        known[mint] = {"mint": mint, "symbol": symbol, "decimals": decimals}
    for mint, symbol in conn.execute(
            "SELECT p.mint, p.ticker FROM memecoin_positions p JOIN "
            "(SELECT MAX(id) id FROM memecoin_positions WHERE network_id=? "
            "AND execution_mode='real' AND mint IS NOT NULL GROUP BY LOWER(mint) "
            "ORDER BY MAX(id) DESC LIMIT ?) latest ON latest.id=p.id ORDER BY p.id DESC",
            (network_id, limit)):
        mint = str(mint).lower()
        if mint not in known:
            known[mint] = {"mint": mint, "symbol": symbol, "decimals": None}
        if len(known) >= limit:
            break
    return list(known.values())[:limit]


def protected_holding_mints(conn, network_id):
    """Chain-local funded inventory; unreadable protection means no scan or sale."""
    try:
        return {str(r[0]).lower() for r in conn.execute(
            "SELECT mint FROM memecoin_positions WHERE network_id=? "
            "AND execution_mode='real' AND is_open=1 AND mint IS NOT NULL UNION "
            "SELECT mint FROM memecoin_pending_entries WHERE network_id=? "
            "AND status='reserved' AND mint IS NOT NULL", (network_id, network_id))}
    except sqlite3.Error:
        logger.exception("holding protection unreadable on %s", network_id)
        return None


def replace_other_holdings(conn, network_id, rows, *, observed_at, unpriced_count):
    """Replace one chain's other-holdings snapshot in one transaction."""
    conn.execute("DELETE FROM memecoin_other_holdings WHERE network_id=?", (network_id,))
    total = 0.0
    for r in rows:
        conn.execute(
            "INSERT INTO memecoin_other_holdings(network_id, mint, symbol, amount, amount_raw, decimals,"
            " price_usd, value_usd, liquidity_usd, observed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (network_id, r["mint"], r.get("symbol"), r.get("amount"), str(r.get("amount_raw") or ""),
             r.get("decimals"), r.get("price_usd"), r.get("value_usd"), r.get("liquidity_usd"),
             observed_at))
        total += float(r.get("value_usd") or 0.0)
    conn.execute("INSERT OR REPLACE INTO memecoin_other_holdings_meta(network_id, observed_at, "
                 "unpriced_count, total_usd) VALUES(?,?,?,?)",
                 (network_id, observed_at, int(unpriced_count or 0), round(total, 2)))
    conn.commit()


def other_holdings(conn):
    """{network_id: {"observed_at", "unpriced_count", "total_usd", "rows": [...]}} plus the newest
    sell intent per mint. Read-only."""
    out = {}
    for net, obs, unpriced, total in conn.execute(
            "SELECT network_id, observed_at, unpriced_count, total_usd FROM memecoin_other_holdings_meta"):
        out[net] = {"observed_at": obs, "unpriced_count": unpriced, "total_usd": total, "rows": []}
    latest = {}
    for row in conn.execute(
            "SELECT intent_id, network_id, mint, state, reason, usd_received, resolved_at, requested_at "
            "FROM memecoin_holding_sell_intents ORDER BY requested_at"):
        latest[(row[1], row[2])] = {"intent_id": row[0], "state": row[3], "reason": row[4],
                                    "usd_received": row[5], "resolved_at": row[6]}
    for r in conn.execute(
            "SELECT network_id, mint, symbol, amount, price_usd, value_usd, liquidity_usd "
            "FROM memecoin_other_holdings ORDER BY value_usd DESC"):
        net = out.setdefault(r[0], {"observed_at": None, "unpriced_count": 0, "total_usd": 0.0, "rows": []})
        net["rows"].append({"mint": r[1], "symbol": r[2], "amount": r[3], "price_usd": r[4],
                            "value_usd": r[5], "liquidity_usd": r[6], "sell": latest.get((r[0], r[1]))})
    return out


def enqueue_holding_sell(conn, network_id, mint, *, now_iso, ttl_s=3600, allow_small=False):
    """Queue one operator sell of a whole leftover balance. Refuses a second live intent per mint."""
    import uuid
    from datetime import datetime, timedelta
    if not isinstance(allow_small, bool):
        raise ValueError("allow_small must be a boolean")
    live = conn.execute(
        "SELECT intent_id FROM memecoin_holding_sell_intents WHERE network_id=? AND mint=? "
        "AND state IN ('queued','claimed','submitted','approval_submitted')", (network_id, mint)).fetchone()
    if live:
        raise ValueError("a sell for this token is already in progress")
    intent_id = uuid.uuid4().hex
    expires = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
               + timedelta(seconds=float(ttl_s))).isoformat()
    conn.execute("INSERT INTO memecoin_holding_sell_intents(intent_id, network_id, mint, requested_at,"
                 " expires_at, state, allow_small) VALUES(?,?,?,?,?,'queued',?)",
                 (intent_id, network_id, mint, now_iso, expires, int(allow_small)))
    conn.commit()
    return intent_id


_WITHDRAW_LIVE = ("queued", "claimed", "submitted")


def enqueue_withdraw(conn, network_id, to_address, amount_usdc, *, now_iso, ttl_s=600,
                     request_id=None):
    """Queue one withdrawal. The request identity remains valid after its terminal state."""
    import uuid
    from datetime import datetime, timedelta
    if request_id is not None:
        try:
            if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError("request_id must be a canonical UUID") from None
    intent_id = request_id or str(uuid.uuid4())
    if conn.in_transaction:
        raise RuntimeError("withdrawal refused inside a caller transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute(
            "SELECT network_id, to_address, amount_usdc FROM memecoin_withdraw_intents "
            "WHERE intent_id=?", (intent_id,)).fetchone()
        if prior:
            if tuple(prior) != (network_id, to_address, float(amount_usdc)):
                raise ValueError("request_id already has a different withdrawal payload")
            conn.commit()
            return intent_id
        live = conn.execute("SELECT intent_id FROM memecoin_withdraw_intents WHERE network_id=? "
                            "AND state IN (?,?,?)", (network_id, *_WITHDRAW_LIVE)).fetchone()
        if live:
            raise ValueError("a withdrawal is already in progress")
        expires = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                   + timedelta(seconds=float(ttl_s))).isoformat()
        conn.execute("INSERT INTO memecoin_withdraw_intents(intent_id, network_id, to_address, "
                     "amount_usdc, requested_at, expires_at, state) VALUES(?,?,?,?,?,?,'queued')",
                     (intent_id, network_id, to_address, float(amount_usdc), now_iso, expires))
        conn.commit()
        return intent_id
    except Exception:
        conn.rollback()
        raise


def release_withdraw_blocked_entry(conn, pending_id):
    """Release only an unsigned reservation that an unresolved withdrawal blocks."""
    return release_unsigned_blocked_entry(conn, pending_id, "withdrawal_unresolved")


def release_unsigned_blocked_entry(conn, pending_id, reason):
    """Release a blocked entry only before any signing/submission metadata exists."""
    cur = conn.execute(
        "UPDATE memecoin_pending_entries SET status='failed', "
        "resolution_reason=? WHERE id=? AND " + _unsigned_reservation_where(),
        (reason, pending_id))
    conn.commit()
    return cur.rowcount == 1


def withdraw_intent(conn, intent_id, *, expected_payload=None):
    """Return one request. An expected payload is (network, address, amount)."""
    row = conn.execute(
        "SELECT intent_id, state, sig, network_id, to_address, amount_usdc "
        "FROM memecoin_withdraw_intents WHERE intent_id=?", (intent_id,)).fetchone()
    if row and expected_payload is not None and tuple(row[3:]) != tuple(expected_payload):
        raise ValueError("request_id already has a different withdrawal payload")
    return dict(zip(("intent_id", "state", "sig"), row[:3])) if row else None


def recent_withdraws(conn, network_id, limit=5):
    return [dict(zip(("intent_id", "amount_usdc", "state", "reason", "sig", "requested_at",
                      "resolved_at", "to_address"), r))
            for r in conn.execute(
                "SELECT intent_id, amount_usdc, state, reason, sig, requested_at, resolved_at, "
                "to_address FROM memecoin_withdraw_intents WHERE network_id=? "
                "ORDER BY requested_at DESC LIMIT ?", (network_id, int(limit)))]


def withdraw_pending(conn, network_id):
    """Return the active state, including a claim that needs crash recovery."""
    row = conn.execute("SELECT state FROM memecoin_withdraw_intents WHERE network_id=? AND "
                       "state IN ('queued','claimed','submitted') ORDER BY requested_at LIMIT 1",
                       (network_id,)).fetchone()
    return row[0] if row else None


def recover_withdraw_claims(conn, network_id, *, now_iso):
    """Expire queued requests and unsigned stale claims under the wallet lock."""
    from datetime import datetime, timedelta
    conn.execute("UPDATE memecoin_withdraw_intents SET state='expired', resolved_at=?, "
                 "reason='expired before the runner could send it' WHERE state='queued' AND "
                 "expires_at<? AND network_id=?", (now_iso, now_iso, network_id))
    stale = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
             - timedelta(seconds=600)).isoformat()
    conn.execute("UPDATE memecoin_withdraw_intents SET state='failed', resolved_at=?, "
                 "reason='the runner stopped before sending' WHERE state='claimed' AND claimed_at<? "
                 "AND network_id=? AND sig IS NULL", (now_iso, stale, network_id))
    conn.commit()


def claim_withdraw(conn, network_id, *, now_iso):
    """Claim the oldest queued request after unsigned crash recovery."""
    recover_withdraw_claims(conn, network_id, now_iso=now_iso)
    row = conn.execute("SELECT intent_id, to_address, amount_usdc FROM memecoin_withdraw_intents "
                       "WHERE network_id=? AND state='queued' ORDER BY requested_at LIMIT 1",
                       (network_id,)).fetchone()
    if not row:
        conn.commit()
        return None
    cur = conn.execute("UPDATE memecoin_withdraw_intents SET state='claimed', claimed_at=? "
                       "WHERE intent_id=? AND state='queued'", (now_iso, row[0]))
    conn.commit()
    return ({"intent_id": row[0], "to_address": row[1], "amount_usdc": float(row[2])}
            if cur.rowcount == 1 else None)


def resolve_withdraw(conn, intent_id, state, *, now_iso, reason=None, sig=None,
                     last_valid_height=None):
    conn.execute("UPDATE memecoin_withdraw_intents SET state=?, reason=?, sig=COALESCE(?, sig), "
                 "last_valid_height=COALESCE(?, last_valid_height), resolved_at=CASE WHEN ? IN "
                 "('sent','failed') THEN ? ELSE resolved_at END WHERE intent_id=?",
                 (state, reason, sig, last_valid_height, state, now_iso, intent_id))
    conn.commit()


def unresolved_withdraws(conn, network_id):
    return [dict(zip(("intent_id", "sig", "last_valid_height", "amount_usdc", "to_address"), r))
            for r in conn.execute(
                "SELECT intent_id, sig, last_valid_height, amount_usdc, to_address FROM "
                "memecoin_withdraw_intents WHERE network_id=? AND state='submitted'",
                (network_id,))]


def holding_sell_queued(conn, network_id):
    """True when a sell intent waits. One indexed-size read, so the runner can skip the lock."""
    return conn.execute("SELECT 1 FROM memecoin_holding_sell_intents WHERE network_id=? AND "
                        "state='queued' LIMIT 1", (network_id,)).fetchone() is not None


def claim_holding_sell(conn, network_id, *, now_iso):
    """Expire stale intents, then claim the oldest queued one for `network_id`, or None.
    A claim older than 10 minutes means the runner stopped mid-sell; it is released as failed so
    the mint can be sold again (the next sell reads the exact balance first)."""
    from datetime import datetime, timedelta
    conn.execute("UPDATE memecoin_holding_sell_intents SET state='expired', resolved_at=?, "
                 "reason='expired before the runner could sell' WHERE state='queued' AND expires_at<?",
                 (now_iso, now_iso))
    stale = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
             - timedelta(seconds=600)).isoformat()
    conn.execute("UPDATE memecoin_holding_sell_intents SET state='failed', resolved_at=?, "
                 "reason='the runner stopped during the sell; check the wallet scan' "
                 "WHERE state='claimed' AND claimed_at<? AND sig IS NULL", (now_iso, stale))
    row = conn.execute(
        "SELECT intent_id, mint, allow_small FROM memecoin_holding_sell_intents WHERE network_id=? AND "
        "state='queued' ORDER BY requested_at LIMIT 1", (network_id,)).fetchone()
    if not row:
        conn.commit()
        return None
    cur = conn.execute("UPDATE memecoin_holding_sell_intents SET state='claimed', claimed_at=? "
                       "WHERE intent_id=? AND state='queued'", (now_iso, row[0]))
    conn.commit()
    return {"intent_id": row[0], "mint": row[1], "allow_small": bool(row[2])} if cur.rowcount == 1 else None


def resolve_holding_sell(conn, intent_id, state, *, now_iso, reason=None, sig=None,
                         usd_received=None, last_valid_height=None, balance_raw=None,
                         signed_payload_b64=None, transaction_phase=None):
    """Record a sell's write-ahead ('submitted') or its outcome. The write-ahead carries the sig
    and the two expiry facts; an outcome never clears them."""
    if transaction_phase not in (None, "approval", "swap"):
        raise ValueError("invalid holding transaction phase")
    where = "intent_id=?"
    if state in ("submitted", "approval_submitted"):
        where += " AND state IN ('queued','claimed','submitted','approval_submitted')"
    cur = conn.execute("UPDATE memecoin_holding_sell_intents SET state=?, reason=?, sig=COALESCE(?, sig), "
                 "usd_received=COALESCE(?, usd_received), "
                 "last_valid_height=COALESCE(?, last_valid_height), "
                 "signed_payload_b64=COALESCE(?, signed_payload_b64), "
                 "transaction_phase=COALESCE(?, transaction_phase), "
                 "balance_raw=COALESCE(?, balance_raw), resolved_at=CASE WHEN ? IN ('sold','failed') "
                 "THEN ? ELSE resolved_at END WHERE " + where,
                 (state, reason, sig, usd_received, last_valid_height, signed_payload_b64, transaction_phase,
                  None if balance_raw is None else str(balance_raw), state, now_iso, intent_id))
    if state in ("submitted", "approval_submitted") and cur.rowcount != 1:
        conn.rollback()
        raise ValueError("holding transaction was not durably bound to a live intent")
    if state == "sold":
        # A SOLD MINT LEAVES THE SNAPSHOT NOW, not at the next 300 s scan (2026-09-14, GOOGLx).
        # The sell took the whole balance; the stale row kept a Sell button, a second sell was
        # queued 30 s later on an empty balance, and it failed "no route". Rewards that refill
        # the mint come back with the next scan.
        conn.execute("DELETE FROM memecoin_other_holdings WHERE (network_id, mint) IN "
                     "(SELECT network_id, mint FROM memecoin_holding_sell_intents WHERE intent_id=?)",
                     (intent_id,))
        conn.execute("UPDATE memecoin_other_holdings_meta SET total_usd=(SELECT COALESCE(SUM("
                     "value_usd), 0) FROM memecoin_other_holdings h WHERE h.network_id="
                     "memecoin_other_holdings_meta.network_id)")
    conn.commit()


def record_quote_residue(conn, *, network_id, quote_mint, residue_atomic, residue_usd, venue,
                         attempt_id, entry_sig, recorded_at):
    """Append one fill's quote-asset residue (see the table's comment). Positive amounts only."""
    if int(residue_atomic) <= 0:
        raise ValueError("a residue row needs a positive amount")
    conn.execute("INSERT INTO memecoin_quote_residue(network_id, quote_mint, residue_atomic, residue_usd,"
                 " venue, attempt_id, entry_sig, recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                 (network_id, quote_mint, str(int(residue_atomic)), residue_usd, venue, attempt_id,
                  entry_sig, recorded_at))
    conn.commit()


def unswept_quote_residue(conn, network_id):
    """{quote_mint: {"atomic", "usd", "oldest", "newest", "ids"}} over rows no live or sold holding
    sell has claimed: swept_by is empty, or its intent failed or expired (the residue is still there)."""
    out = {}
    for rid, mint, atomic, usd, recorded in conn.execute(
            "SELECT r.id, r.quote_mint, r.residue_atomic, r.residue_usd, r.recorded_at "
            "FROM memecoin_quote_residue r LEFT JOIN memecoin_holding_sell_intents i "
            "ON i.intent_id = r.swept_by WHERE r.network_id=? AND (r.swept_by IS NULL "
            "OR i.state IN ('failed', 'expired')) ORDER BY r.id", (network_id,)):
        agg = out.setdefault(mint, {"atomic": 0, "usd": 0.0, "oldest": recorded, "newest": recorded,
                                    "ids": []})
        agg["atomic"] += int(atomic)
        agg["usd"] += float(usd or 0.0)
        agg["oldest"], agg["newest"] = min(agg["oldest"], recorded), max(agg["newest"], recorded)
        agg["ids"].append(rid)
    return out


def mark_quote_residue_swept(conn, ids, intent_id, *, now_iso):
    """Tie residue rows to the holding sell that will take them."""
    conn.executemany("UPDATE memecoin_quote_residue SET swept_by=?, swept_at=? WHERE id=?",
                     [(intent_id, now_iso, rid) for rid in ids])
    conn.commit()


QUOTE_RESIDUE_READ_MAX_TRIES = 3


@contextmanager
def _immediate(conn):
    """One BEGIN IMMEDIATE ... COMMIT; rollback on error. Refuses a caller's open transaction."""
    if conn.in_transaction:
        raise RuntimeError("needs a clean transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _rows(cur):
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def unread_exit_fills(conn, *, since_iso, limit):
    """Real Solana exit fills (every kind but 'entry') since `since_iso` whose receipt the residue
    read has not settled: no read row, or a 'pending' one. Oldest first. Positions are reached
    through idx_mc_pos_mode_open_closed (open, or closed since the cutoff)."""
    return _rows(conn.execute(
        "SELECT DISTINCT f.tx_sig AS sig, f.position_id, f.kind, f.created_at, p.mint, p.attempt_id, "
        "COALESCE(r.tries, 0) AS tries FROM memecoin_positions p "
        "JOIN memecoin_fills f ON f.position_id = p.id "
        "LEFT JOIN memecoin_quote_residue_reads r ON r.sig = f.tx_sig "
        "WHERE p.execution_mode='real' AND (p.is_open=1 OR p.closed_at>=?) "
        "AND p.network_id='solana' AND f.kind <> 'entry' AND f.tx_sig IS NOT NULL "
        "AND f.created_at >= ? AND (r.sig IS NULL OR r.state='pending') "
        "ORDER BY f.created_at LIMIT ?", (since_iso, since_iso, int(limit))))


def record_exit_residue_read(conn, fill, *, rows, negative, now_iso):
    """Settle one exit receipt read. `rows` None = the receipt was not readable: the read stays
    'pending' and becomes 'unreadable' at the try cap (unknown, never zero). Otherwise one residue
    row per mint the exit left (none = a known zero) and the read is 'read'. One transaction."""
    tries = int(fill.get("tries") or 0) + 1
    if rows is None:
        state = "unreadable" if tries >= QUOTE_RESIDUE_READ_MAX_TRIES else "pending"
    else:
        state = "read"
    with _immediate(conn):
        for row in rows or ():
            conn.execute(
                "INSERT OR IGNORE INTO memecoin_quote_residue(network_id, quote_mint, residue_atomic,"
                " residue_usd, venue, attempt_id, entry_sig, recorded_at, position_id, kind, source_sig,"
                " source_at) VALUES('solana',?,?,?,?,?,NULL,?,?,'exit',?,?)",
                (row["mint"], str(int(row["atomic"])), row.get("usd"), fill.get("kind"),
                 fill.get("attempt_id"), now_iso, fill["position_id"], fill["sig"], fill["created_at"]))
        conn.execute(
            "INSERT INTO memecoin_quote_residue_reads(sig, position_id, fill_kind, state, tries,"
            " negative_mints, read_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(sig) DO UPDATE SET"
            " state=excluded.state, tries=excluded.tries, negative_mints=excluded.negative_mints,"
            " read_at=excluded.read_at",
            (fill["sig"], fill["position_id"], fill.get("kind"), state, tries,
             ",".join(negative) if negative else None, now_iso))
    return state


def quote_residue_sell_candidates(conn, *, since_iso):
    """Sold Solana holding sells (proceeds read, signature known, settled since `since_iso`) of a
    mint that still has uncredited residue recorded before the sell settled. Oldest first."""
    return _rows(conn.execute(
        "SELECT i.intent_id, i.mint, i.sig, i.usd_received, i.balance_raw, i.resolved_at "
        "FROM memecoin_holding_sell_intents i WHERE i.network_id='solana' AND i.state='sold' "
        "AND i.usd_received IS NOT NULL AND i.sig IS NOT NULL AND i.resolved_at >= ? "
        "AND EXISTS (SELECT 1 FROM memecoin_quote_residue r LEFT JOIN memecoin_quote_residue_credits c "
        "ON c.residue_id = r.id AND c.reverted_at IS NULL "
        "WHERE r.network_id='solana' AND r.quote_mint = i.mint AND c.id IS NULL "
        "AND COALESCE(r.source_at, r.recorded_at) < i.resolved_at) ORDER BY i.resolved_at",
        (since_iso,)))


def other_sold_holding_sells(conn, mint, *, intent_id):
    """(sig, resolved_at) of every OTHER sold holding sell of `mint`. Their order is decided on the
    chain (receipt slot); local `resolved_at` is recovery time, used only as an upper bound on an
    unreadable sell's landing (review C06, 2026-10-08)."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT sig, resolved_at FROM memecoin_holding_sell_intents WHERE network_id='solana' AND mint=? "
        "AND state='sold' AND intent_id<>? AND sig IS NOT NULL ORDER BY resolved_at",
        (mint, intent_id))]


def quote_residue_owners(conn, mint, *, before_iso, intent_id, after_iso=""):
    """The residue a sell of `mint` that landed at `before_iso` consumed: rows recorded before it,
    not credited to another sell, and not swept by a different intent that is still live or sold.
    Each with its position (exit rows carry it; entry rows bind by attempt_id), whether that
    position is closed and priced, and whether it is already credited by this intent."""
    return _rows(conn.execute(
        "SELECT r.id AS residue_id, r.residue_atomic AS atomic, COALESCE(r.source_at, r.recorded_at) AS source_at, "
        "COALESCE(r.position_id, p.id) AS position_id, "
        "(pp.is_open = 0 AND pp.realized_pnl IS NOT NULL) AS priced, c.id AS credit_id "
        "FROM memecoin_quote_residue r "
        "LEFT JOIN memecoin_positions p ON r.position_id IS NULL AND p.attempt_id = r.attempt_id "
        "AND r.attempt_id IS NOT NULL AND p.execution_mode='real' "
        "LEFT JOIN memecoin_positions pp ON pp.id = COALESCE(r.position_id, p.id) "
        "LEFT JOIN memecoin_quote_residue_credits c ON c.residue_id = r.id AND c.reverted_at IS NULL "
        "LEFT JOIN memecoin_holding_sell_intents s ON s.intent_id = r.swept_by "
        "WHERE r.network_id='solana' AND r.quote_mint=? AND COALESCE(r.source_at, r.recorded_at) < ? "
        "AND COALESCE(r.source_at, r.recorded_at) > ? "
        "AND (c.id IS NULL OR c.intent_id = ?) "
        "AND (r.swept_by IS NULL OR r.swept_by = ? OR s.state IN ('failed', 'expired')) "
        "ORDER BY r.id", (mint, before_iso, after_iso, intent_id, intent_id)))


def exit_reads_unsettled_before(conn, *, since_iso, before_iso):
    """Exit fills before `before_iso` whose receipt read is not settled yet (unread, or pending under
    the try cap). A sell after any of them waits: that residue may be part of the balance sold.
    An 'unreadable' read does NOT hold sells (Gamma LOW, Alpha 2026-10-07): it pages once when it
    gives up, and any residue it left falls to the sell's foreign share, never booked."""
    unread = unread_exit_fills(conn, since_iso=since_iso, limit=1_000_000)
    return sum(1 for f in unread if f["created_at"] < before_iso)


def apply_quote_residue_credits(conn, *, intent_id, mint, credits, consumed_atomic, foreign_atomic,
                                foreign_usd, short, now_iso):
    """Book one sell's credits: for each, realized_pnl += usd on a closed priced position, the
    append-only credit row (UNIQUE residue_id: never twice) and the residue row tied to the sell.
    A position that is open or unpriced at the time is skipped (no row) and retried later.
    Returns the number booked. One transaction."""
    booked = 0
    with _immediate(conn):
        for c in credits:
            before = conn.execute("SELECT realized_pnl FROM memecoin_positions WHERE id=? AND is_open=0 "
                                  "AND realized_pnl IS NOT NULL", (c["position_id"],)).fetchone()
            done = conn.execute("SELECT 1 FROM memecoin_quote_residue_credits WHERE residue_id=? "
                                "AND reverted_at IS NULL", (c["residue_id"],)).fetchone()
            if before is None or done is not None:
                continue
            conn.execute("UPDATE memecoin_positions SET realized_pnl = realized_pnl + ? WHERE id=?",
                         (float(c["usd"]), c["position_id"]))
            conn.execute(
                "INSERT INTO memecoin_quote_residue_credits(residue_id, position_id, intent_id, quote_mint,"
                " residue_atomic, consumed_atomic, foreign_atomic, foreign_usd, usd, short,"
                " realized_before, realized_after, credited_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c["residue_id"], c["position_id"], intent_id, mint, str(int(c["atomic"])),
                 str(int(consumed_atomic)), str(int(foreign_atomic)), foreign_usd, float(c["usd"]),
                 int(bool(short)), float(before[0]), float(before[0]) + float(c["usd"]), now_iso))
            conn.execute("UPDATE memecoin_quote_residue SET swept_by=?, swept_at=COALESCE(swept_at, ?) "
                         "WHERE id=?", (intent_id, now_iso, c["residue_id"]))
            booked += 1
    return booked


def revert_quote_residue_credit(conn, credit_id, *, now_iso, reason):
    """Undo one credit: realized_pnl -= usd and stamp reverted_at (the row stays). The residue row
    is released (swept_by cleared) so a corrected credit can book it again. False when there is no
    such unreverted credit."""
    with _immediate(conn):
        row = conn.execute("SELECT position_id, usd, residue_id FROM memecoin_quote_residue_credits "
                           "WHERE id=? AND reverted_at IS NULL", (credit_id,)).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE memecoin_positions SET realized_pnl = realized_pnl - ? WHERE id=?",
                     (float(row[1]), row[0]))
        _latch_desk_day_for_position(conn, row[0])
        # Stamped, never deleted: the live-credit UNIQUE index covers unreverted rows only.
        conn.execute("UPDATE memecoin_quote_residue_credits SET reverted_at=?, revert_reason=? WHERE id=?",
                     (now_iso, reason, credit_id))
        conn.execute("UPDATE memecoin_quote_residue SET swept_by=NULL, swept_at=NULL WHERE id=?", (row[2],))
    return True


def quote_residue_by_position(conn, position_ids=None):
    """{position_id: {"pending_usd", "credited_usd"}}: residue not yet credited (at its record-time
    price; not money until a sell lands) apart from residue credited into realized_pnl."""
    out = {}
    for pid, pending, credited in conn.execute(
            "SELECT COALESCE(r.position_id, p.id) AS pid, "
            "SUM(CASE WHEN c.id IS NULL THEN COALESCE(r.residue_usd, 0) ELSE 0 END), "
            "SUM(CASE WHEN c.id IS NOT NULL THEN c.usd ELSE 0 END) FROM memecoin_quote_residue r "
            "LEFT JOIN memecoin_positions p ON r.position_id IS NULL AND p.attempt_id = r.attempt_id "
            "AND r.attempt_id IS NOT NULL AND p.execution_mode='real' "
            "LEFT JOIN memecoin_quote_residue_credits c ON c.residue_id = r.id AND c.reverted_at IS NULL "
            "GROUP BY COALESCE(r.position_id, p.id)"):
        if pid is None or (position_ids is not None and pid not in set(position_ids)):
            continue
        out[pid] = {"pending_usd": round(float(pending or 0.0), 6),
                    "credited_usd": round(float(credited or 0.0), 6)}
    return out


def live_holding_sell_mints(conn, network_id):
    """Mints with a holding sell queued, claimed or submitted (not yet settled)."""
    return {r[0] for r in conn.execute(
        "SELECT mint FROM memecoin_holding_sell_intents WHERE network_id=? "
        "AND state IN ('queued','claimed','submitted','approval_submitted')", (network_id,))}


def other_holding_balance(conn, network_id, mint):
    """(amount_raw int or None, snapshot observed_at or None) from the other-holdings snapshot. The
    snapshot is replaced per chain, so its time is the chain's meta row, not a MAX over all chains.
    A mint absent from a snapshot is held at zero or owned by a position; both read as None."""
    meta = conn.execute("SELECT observed_at FROM memecoin_other_holdings_meta WHERE network_id=?",
                        (network_id,)).fetchone()
    row = conn.execute("SELECT amount_raw FROM memecoin_other_holdings WHERE network_id=? AND mint=?",
                       (network_id, mint)).fetchone()
    raw = None
    if row and str(row[0] or "").isdigit():
        raw = int(row[0])
    return raw, (meta[0] if meta else None)


def recently_sold_holdings(conn, network_id, *, now_iso, within_s=60):
    """Mints whose holding sell confirmed within `within_s` seconds of `now_iso`."""
    since = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
             - timedelta(seconds=float(within_s))).isoformat()
    return {r[0] for r in conn.execute(
        "SELECT mint FROM memecoin_holding_sell_intents WHERE network_id=? AND state='sold' "
        "AND resolved_at>=?", (network_id, since))}


def unresolved_holding_sell_count(conn, network_id):
    """Cheap chain-local nonce fence; an absent signature never clears an unresolved state."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM memecoin_holding_sell_intents WHERE network_id=? "
        "AND state IN ('submitted','approval_submitted')",
        (normalized_coin_network(network_id, ""),)).fetchone()[0])


def unresolved_holding_sells(conn, network_id):
    """Signed sells whose chain result is not yet known."""
    return [dict(zip(("intent_id", "mint", "sig", "last_valid_height", "balance_raw",
                      "signed_payload_b64", "transaction_phase"), r))
            for r in conn.execute(
                "SELECT intent_id, mint, sig, last_valid_height, balance_raw, signed_payload_b64, "
                "transaction_phase FROM "
                "memecoin_holding_sell_intents WHERE network_id=? "
                "AND state IN ('submitted','approval_submitted') "
                "ORDER BY requested_at", (network_id,))]


def position_live_activity(conn, network_id, mint, *, now_iso, max_age_s=900):
    """Latest stored market activity for an open position, for the card. Display only.

    `volume_m5`/`volume_h1`/`txns_m5` come from the newest mark that carries them (the runner's
    DexScreener sweep, token-level across pools since 2026-09-12). `last_trade_at` is the newest
    live swap on the Solana fast lane -- the clock the volume-death exit reads. Missing = None.
    """
    out = {"volume_m5": None, "volume_h1": None, "txns_m5": None, "volume_at": None,
           "last_trade_at": None}
    if not mint:
        return out
    chain = str(network_id or "solana")
    try:
        from datetime import datetime, timedelta
        since = (datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
                 - timedelta(seconds=float(max_age_s))).isoformat()
        row = conn.execute(
            "SELECT volume_m5, volume_h1, txns_m5, sweep_ts FROM memecoin_position_marks "
            "WHERE chain_id=? AND mint=? AND sweep_ts>=? AND volume_m5 IS NOT NULL "
            "ORDER BY sweep_ts DESC LIMIT 1", (chain, mint, since)).fetchone()
        if row:
            out.update(volume_m5=row[0], volume_h1=row[1], txns_m5=row[2], volume_at=row[3])
        if chain == "solana":
            swap = conn.execute(
                "SELECT MAX(sweep_ts) FROM memecoin_position_marks "
                "WHERE chain_id=? AND mint=? AND sweep_ts>=? AND source='fastlane_sol_swap'",
                (chain, mint, since)).fetchone()
            out["last_trade_at"] = swap[0] if swap else None
    except Exception:
        logger.exception("live activity unreadable for %s / %s", chain, mint)
    return out


def wallet_gate_exemptions(conn, source_user_id):
    """The ACTIVE, EXEMPTIBLE families for one wallet. Never raises; unknown families are dropped.

    Filtering against EXEMPTIBLE_GATE_FAMILIES on READ is what makes a stale or hand-written row
    naming a safety gate inert — the caller cannot act on something this never returns.
    """
    if not source_user_id:
        return frozenset()
    try:
        rows = conn.execute(
            "SELECT gate_family FROM memecoin_wallet_gate_exemptions "
            "WHERE source_user_id=? AND active=1", (source_user_id,)).fetchall()
    except Exception:
        logger.exception("wallet exemptions unreadable for %s — treating as none", source_user_id)
        return frozenset()
    return frozenset(r[0] for r in rows if r[0] in EXEMPTIBLE_GATE_FAMILIES)


def entry_gate_exemptions(conn, source_user_id, network_id):
    """The wallet's own exemptions plus configured waivers for this chain/account.

    CHAIN-WIDE EXEMPTIONS (2026-09-13, operator: "remove all the refusal reasons that made money").
    MEMECOIN_GATE_EXEMPT_FAMILIES_<CHAIN> names selection families to waive for every wallet on
    that chain. Filtered against EXEMPTIBLE_GATE_FAMILIES like a wallet row, so a safety gate
    named there is inert. Waivers are recorded on the attempt exactly as a wallet's are.
    """
    from sniper.settings.memecoin import gate_exempt_families_for
    chain = frozenset(f for f in gate_exempt_families_for(network_id, source_user_id=source_user_id)
                      if f in EXEMPTIBLE_GATE_FAMILIES)
    return wallet_gate_exemptions(conn, source_user_id) | chain


def set_wallet_gate_exemption(conn, source_user_id, gate_family, *, active, actor, reason,
                              now_iso):
    """Grant or revoke ONE family for one wallet, and write the approvals audit row.

    Refuses a family that is not exemptible rather than storing it: an inert row would look granted
    in the UI and do nothing at the gate, which is worse than a refusal.
    """
    if gate_family not in EXEMPTIBLE_GATE_FAMILIES:
        raise ValueError("%r is not an exemptible selection gate" % (gate_family,))
    conn.execute(
        "INSERT INTO memecoin_wallet_gate_exemptions"
        "(source_user_id, gate_family, active, actor, reason, granted_at, revoked_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(source_user_id, gate_family) DO UPDATE SET "
        "active=excluded.active, actor=excluded.actor, reason=excluded.reason, "
        "granted_at=CASE WHEN excluded.active=1 THEN excluded.granted_at ELSE granted_at END, "
        "revoked_at=CASE WHEN excluded.active=0 THEN excluded.revoked_at ELSE NULL END",
        (source_user_id, gate_family, 1 if active else 0, actor, reason,
         now_iso if active else None, None if active else now_iso))
    conn.execute(
        "INSERT INTO memecoin_wallet_approvals"
        "(source_user_id, action, actor, reason, created_at) VALUES (?,?,?,?,?)",
        (source_user_id, "exempt_grant" if active else "exempt_revoke", actor,
         "%s: %s" % (gate_family, reason or ""), now_iso))
    conn.commit()


def wallet_chase_cap(conn, source_user_id, *, network_id, coin_band):
    """Return one active bounded chase cap, or None when no scoped cap exists."""
    if not source_user_id:
        return None
    try:
        row = conn.execute(
            "SELECT cap_pct FROM memecoin_wallet_chase_caps "
            "WHERE source_user_id=? AND network_id=? AND coin_band=? AND active=1",
            (source_user_id, str(network_id), str(coin_band)),
        ).fetchone()
        cap = float(row[0]) if row else None
    except Exception:
        logger.exception("wallet chase cap unreadable for %s", source_user_id)
        return None
    if cap is None or not math.isfinite(cap) or not 0 < cap <= WALLET_CHASE_CAP_MAX_PCT:
        return None
    return cap


def set_wallet_chase_cap(conn, source_user_id, *, network_id, coin_band, cap_pct,
                         actor, reason, now_iso):
    """Set one bounded wallet chase cap. This cannot create a blanket waiver."""
    network = str(network_id)
    band = str(coin_band)
    try:
        cap = float(cap_pct)
    except (TypeError, ValueError):
        raise ValueError("cap_pct must be a finite number") from None
    if network not in WALLET_CHASE_CAP_NETWORKS:
        raise ValueError("network_id is not supported for a wallet chase cap")
    if band not in WALLET_CHASE_CAP_BANDS:
        raise ValueError("coin_band is not supported for a wallet chase cap")
    if not math.isfinite(cap) or not 0 < cap <= WALLET_CHASE_CAP_MAX_PCT:
        raise ValueError("cap_pct must be greater than 0 and at most %.0f" % WALLET_CHASE_CAP_MAX_PCT)
    if not conn.execute("SELECT 1 FROM memecoin_wallets WHERE source_user_id=?", (source_user_id,)).fetchone():
        raise ValueError("wallet_not_found")
    conn.execute(
        "INSERT INTO memecoin_wallet_chase_caps"
        "(source_user_id,network_id,coin_band,cap_pct,active,actor,reason,granted_at,revoked_at) "
        "VALUES(?,?,?,?,1,?,?,?,NULL) "
        "ON CONFLICT(source_user_id,network_id,coin_band) DO UPDATE SET "
        "cap_pct=excluded.cap_pct,active=1,actor=excluded.actor,reason=excluded.reason,"
        "granted_at=excluded.granted_at,revoked_at=NULL",
        (source_user_id, network, band, cap, actor, reason, now_iso),
    )
    conn.execute(
        "INSERT INTO memecoin_wallet_approvals"
        "(source_user_id,action,actor,reason,created_at) VALUES(?,?,?,?,?)",
        (source_user_id, "chase_cap_set", actor,
         "%s/%s %.0f%%: %s" % (network, band, cap, reason or ""), now_iso),
    )
    conn.commit()


WALLET_EXIT_OVERRIDE_TP_RANGE = (5.0, 500.0)      # +5% .. +500%
WALLET_EXIT_OVERRIDE_STOP_RANGE = (10.0, 80.0)    # -10% .. -80%; the catastrophe floor stays 80


def _exit_override_network(network_id):
    """One stored spelling per chain: 'robin' and '4663' are the same row."""
    from sniper.services.memecoin_chains import canonical_chain_id
    return normalized_coin_network(canonical_chain_id(network_id) or network_id, "")


def _exit_override_value(value, bounds):
    """A finite number inside `bounds`, else None. Never 0 by accident."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not bounds[0] <= number <= bounds[1]:
        return None
    return number


def wallet_exit_override(conn, source_user_id, *, network_id):
    """The active exit override for (wallet, chain), or None. Read-side guarded: a value outside
    the write bounds (a hand-edited row) is dropped, so a bad row falls back to the base rail."""
    if not source_user_id:
        return None
    row = conn.execute(
        "SELECT id, tp_pct, stop_pct, actor, reason, set_at FROM memecoin_wallet_exit_overrides "
        "WHERE source_user_id=? AND network_id=? AND active=1",
        (source_user_id, _exit_override_network(network_id))).fetchone()
    if row is None:
        return None
    tp = _exit_override_value(row[1], WALLET_EXIT_OVERRIDE_TP_RANGE)
    stop = _exit_override_value(row[2], WALLET_EXIT_OVERRIDE_STOP_RANGE)
    if tp is None and stop is None:
        return None
    return {"id": row[0], "tp_pct": tp, "stop_pct": stop, "actor": row[3], "reason": row[4],
            "set_at": row[5]}


def _exit_override_audit(conn, source_user_id, network, action, old, new, actor, reason, now_iso):
    conn.execute(
        "INSERT INTO memecoin_wallet_exit_override_audit(source_user_id,network_id,action,"
        "old_tp_pct,new_tp_pct,old_stop_pct,new_stop_pct,actor,reason,at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (source_user_id, network, action, (old or {}).get("tp_pct"), (new or {}).get("tp_pct"),
         (old or {}).get("stop_pct"), (new or {}).get("stop_pct"), actor, reason, now_iso))


def set_wallet_exit_override(conn, source_user_id, *, network_id, tp_pct, stop_pct, actor,
                             reason, now_iso):
    """Set the (wallet, chain) take-profit and/or stop. A None rail keeps the base policy's."""
    network = _exit_override_network(network_id)
    if not network:
        raise ValueError("network_id is required")
    tp = None if tp_pct is None else _exit_override_value(tp_pct, WALLET_EXIT_OVERRIDE_TP_RANGE)
    stop = (None if stop_pct is None
            else _exit_override_value(stop_pct, WALLET_EXIT_OVERRIDE_STOP_RANGE))
    if tp_pct is not None and tp is None:
        raise ValueError("tp_pct must be between %.0f and %.0f" % WALLET_EXIT_OVERRIDE_TP_RANGE)
    if stop_pct is not None and stop is None:
        raise ValueError("stop_pct must be between %.0f and %.0f"
                         % WALLET_EXIT_OVERRIDE_STOP_RANGE)
    if tp is None and stop is None:
        raise ValueError("set tp_pct, stop_pct, or both")
    if not conn.execute("SELECT 1 FROM memecoin_wallets WHERE source_user_id=?",
                        (source_user_id,)).fetchone():
        raise ValueError("wallet_not_found")
    old = wallet_exit_override(conn, source_user_id, network_id=network)
    conn.execute(
        "INSERT INTO memecoin_wallet_exit_overrides"
        "(source_user_id,network_id,tp_pct,stop_pct,active,actor,reason,set_at,revoked_at) "
        "VALUES(?,?,?,?,1,?,?,?,NULL) "
        "ON CONFLICT(source_user_id,network_id) DO UPDATE SET tp_pct=excluded.tp_pct,"
        "stop_pct=excluded.stop_pct,active=1,actor=excluded.actor,reason=excluded.reason,"
        "set_at=excluded.set_at,revoked_at=NULL",
        (source_user_id, network, tp, stop, actor, reason, now_iso))
    new = {"tp_pct": tp, "stop_pct": stop}
    _exit_override_audit(conn, source_user_id, network, "set", old, new, actor, reason, now_iso)
    conn.commit()
    return {"network_id": network, **new}


def reset_wallet_exit_override(conn, source_user_id, *, network_id, actor, reason, now_iso):
    """Back to the base policy for (wallet, chain). Returns 1 when an override was active."""
    network = _exit_override_network(network_id)
    old = wallet_exit_override(conn, source_user_id, network_id=network)
    cur = conn.execute(
        "UPDATE memecoin_wallet_exit_overrides SET active=0,actor=?,reason=?,revoked_at=? "
        "WHERE source_user_id=? AND network_id=? AND active=1",
        (actor, reason, now_iso, source_user_id, network))
    if cur.rowcount:
        _exit_override_audit(conn, source_user_id, network, "reset", old, None, actor, reason,
                             now_iso)
    conn.commit()
    return cur.rowcount


def clear_wallet_exit_overrides(conn, source_user_id, *, actor, now_iso, commit=True):
    """Drop every active exit override of an excluded or retired wallet, with an audit row each."""
    rows = conn.execute(
        "SELECT network_id, tp_pct, stop_pct FROM memecoin_wallet_exit_overrides "
        "WHERE source_user_id=? AND active=1", (source_user_id,)).fetchall()
    for network, tp, stop in rows:
        _exit_override_audit(conn, source_user_id, network, "cleared_on_exclusion",
                             {"tp_pct": tp, "stop_pct": stop}, None, actor, None, now_iso)
    conn.execute(
        "UPDATE memecoin_wallet_exit_overrides SET active=0,actor=?,revoked_at=? "
        "WHERE source_user_id=? AND active=1", (actor, now_iso, source_user_id))
    if commit:
        conn.commit()
    return len(rows)


def wallet_exit_override_audit(conn, source_user_id, limit=20):
    """Newest-first audit rows of one wallet's exit overrides."""
    return [dict(zip(("network_id", "action", "old_tp_pct", "new_tp_pct", "old_stop_pct",
                      "new_stop_pct", "actor", "reason", "at"), r)) for r in conn.execute(
        "SELECT network_id, action, old_tp_pct, new_tp_pct, old_stop_pct, new_stop_pct, actor, "
        "reason, at FROM memecoin_wallet_exit_override_audit WHERE source_user_id=? "
        "ORDER BY id DESC LIMIT ?", (source_user_id, int(limit)))]


def clear_wallet_chase_caps(conn, source_user_id, *, actor, now_iso, commit=True):
    """Revoke every active numeric chase cap for an excluded wallet."""
    cur = conn.execute(
        "UPDATE memecoin_wallet_chase_caps SET active=0,actor=?,reason='cleared on exclusion',"
        "revoked_at=? WHERE source_user_id=? AND active=1",
        (actor, now_iso, source_user_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount


def clear_wallet_gate_exemptions(conn, source_user_id, *, actor, now_iso, commit=True):
    """Revoke every exemption for a wallet. Called on EXCLUSION: an excluded wallet carries none
    into a future re-arm, so the re-arm decides fresh rather than inheriting a waiver nobody
    re-examined. Returns how many were active.

    `commit=False` lets the caller keep this inside its own transaction. set_wallet_excluded needs
    that: an exclusion that commits before the waivers clear can leave a wallet excluded AND
    exempt.
    """
    rows = [r[0] for r in conn.execute(
        "SELECT gate_family FROM memecoin_wallet_gate_exemptions "
        "WHERE source_user_id=? AND active=1", (source_user_id,)).fetchall()]
    if not rows:
        return 0
    # REVOKE DIRECTLY, not through set_wallet_gate_exemption. That setter validates the family, and
    # validation belongs on the GRANT path only: a stored row naming a non-exemptible family is
    # inert but real, and refusing to clear it would leave it behind on every excluded wallet
    # forever. Caught by the first run of this function against a hand-written safety-gate row.
    conn.execute(
        "UPDATE memecoin_wallet_gate_exemptions SET active=0, actor=?, "
        "reason='cleared on exclusion', revoked_at=? WHERE source_user_id=? AND active=1",
        (actor, now_iso, source_user_id))
    for family in rows:
        conn.execute(
            "INSERT INTO memecoin_wallet_approvals"
            "(source_user_id, action, actor, reason, created_at) VALUES (?,?,?,?,?)",
            (source_user_id, "exempt_revoke", actor,
             "%s: cleared on exclusion" % family, now_iso))
    if commit:
        conn.commit()
    return len(rows)


def funded_entry_prior_rows(conn, *, source_user_id, network_id, decision_at,
                            current_position_id, exclude_mint=None, strict_copy=False):
    """Read closed funded history available at the entry decision."""
    decision = _parse_iso(decision_at)
    if decision is None or decision.tzinfo is None:
        raise ValueError("An aware decision time is required")
    population = copy_only_sql() if strict_copy else "COALESCE(signal_source,'')!='manual'"
    rows = conn.execute(
        "SELECT id,realized_pnl,entry_notional,closed_recorded_at FROM memecoin_positions "
        "WHERE source_user_id=? AND network_id=? AND execution_mode='real' AND is_open=0 "
        f"AND ({population}) AND closed_recorded_at IS NOT NULL AND id!=? "
        "AND (? IS NULL OR mint!=?) ORDER BY closed_recorded_at,id",
        (source_user_id, network_id, current_position_id, exclude_mint, exclude_mint),
    ).fetchall()
    return [row for row in rows if (closed := _parse_iso(row["closed_recorded_at"]))
            is not None and closed.tzinfo is not None and closed <= decision]


# Operator projections read bounded evidence without changing execution state.
@contextmanager
def operator_read_snapshot(conn):
    """Keep related reads in one snapshot without changing caller ownership."""
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        yield conn
    finally:
        if owned:
            conn.rollback()


@contextmanager
def operator_read_connection(conn):
    """Use one short read-only snapshot for a file-backed operator request."""
    from sniper.persistence.db_core import _connection_db_path, get_readonly_connection
    path = _connection_db_path(conn)
    if path is None:
        yield conn
        return
    observer = get_readonly_connection(path)
    try:
        observer.execute("BEGIN")
        yield observer
    finally:
        observer.close()


def entry_attempt_source_identity(conn, attempt):
    """Exact retained source tuple, never a wallet/coin/time approximation."""
    facts = resolve_source_evidence(conn, attempt.get("origin_event_id"),
        network_id=attempt.get("network_id"), mint=attempt.get("mint"),
        source_user_id=attempt.get("source_user_id"), attempt=attempt)
    signature = (facts.get("signature") or facts.get("tx_hash")
                 or (event_transaction_signature(attempt.get("origin_event_id")) if facts else None))
    net = normalized_coin_network(attempt.get("network_id"), attempt.get("mint"))
    origin_actor = None
    if net != "solana":
        if not facts or not net.isdigit() or int(net) <= 0:
            return None
        parts = str(attempt.get("origin_event_id") or "").split(":")
        token = accounting_mark_key(net, attempt.get("mint"))
        if len(parts) == 5 and parts[:2] == ["swap", "raybot"]:
            origin_actor = parts[3]
            if accounting_mark_key(net, parts[4]) != token:
                return None
        elif len(parts) == 4 and parts[0] in {"onchain", "rhfast", "bscfast"}:
            if parts[1] != net or accounting_mark_key(net, parts[3]) != token:
                return None
        elif not (len(parts) == 2 and parts[0] == "onchain"):
            return None
        source_hash = source_tx_of(attempt.get("origin_event_id"))
        if (not _SOURCE_TX_EVM.fullmatch(source_hash or "")
                or (signature and str(source_tx_of(signature) or "").lower() != source_hash.lower())
                or not re.fullmatch(r"0x[0-9a-fA-F]{40}", token[1])):
            return None
        signature = source_hash
    if not signature or not all(attempt.get(k) for k in ("network_id", "mint", "source_user_id")):
        return None
    actor = facts.get("address") or facts.get("wallet") or facts.get("owner")
    if not actor:
        try:
            valuation = _json.loads(facts.get("source_valuation_json") or "{}")
            actor = valuation.get("owner") if isinstance(valuation, dict) else None
        except (ValueError, TypeError):
            actor = None
    fill = facts.get("source_fill")
    if not actor and net != "solana" and isinstance(fill, dict) and (
            str(fill.get("tx_hash") or "").lower() == str(signature).lower()):
        # A Relay-routed EVM lane buy names its buyer only in the proven fill: the payer is Relay's
        # EOA. Without this the rh lane's duplicate paged "another entry attempt in progress" and
        # hid the owning refusal (OmakaseOnly 72019cd9, 2026-10-14).
        actor = fill.get("wallet")
    if net != "solana":
        if origin_actor and actor and origin_actor.lower() != str(actor).lower():
            return None
        actor = actor or origin_actor
        if not isinstance(actor, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", actor):
            return None
    if not actor and net == "solana":
        from types import SimpleNamespace
        from sniper.services.memecoin_source_initiation import source_initiation_refusal
        proof = facts.get("source_initiation")
        if isinstance(proof, dict):
            ev = SimpleNamespace(signal_source=attempt.get("signal_source"),
                fomo_event_id=attempt.get("origin_event_id"), mint=attempt["mint"],
                source_user_id=attempt["source_user_id"], source_wallet=proof.get("payer"),
                source_block_number=facts.get("source_slot"), source_initiation=proof)
            if source_initiation_refusal(ev, net) is None:
                actor = proof.get("payer")
    if not isinstance(actor, str) or not actor:
        return None
    return (net, attempt["source_user_id"], actor if net == "solana" else actor.lower(),
            accounting_mark_key(net, attempt["mint"]),
            signature if net == "solana" else str(signature).lower())


def native_owned_secondary_refusal(conn, attempt):
    """Bounded exact native proof lookup on the notification reader, after execution."""
    if (attempt.get("network_id") != "solana" or attempt.get("signal_source") != "onchain_watch"
            or attempt.get("terminal_status") not in {"source_initiation_unknown", "refused"}
            or attempt.get("terminal_reason") != "missing_receipt_proof"):
        return False
    identity = entry_attempt_source_identity(conn, attempt)
    if not identity:
        return False
    watcher = resolve_source_evidence(conn, attempt.get("origin_event_id"), network_id="solana",
        mint=attempt.get("mint"), source_user_id=attempt.get("source_user_id"), attempt=attempt)
    if not watcher.get("address"):
        return False
    from types import SimpleNamespace
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    for raw in conn.execute(
            "SELECT * FROM memecoin_entry_attempts WHERE origin_event_id=? "
            "AND network_id='solana' AND mint=? AND source_user_id=? AND signal_source='slot_lane' "
            "ORDER BY decision_started_at DESC LIMIT 20",
            ('slotlane:' + identity[-1], attempt['mint'], attempt['source_user_id'])):
        native = dict(raw)
        if (not str(native.get('delivery_lane') or '').startswith('slot_lane_')
                or entry_attempt_source_identity(conn, native) != identity):
            continue
        facts = resolve_source_evidence(conn, native['origin_event_id'], network_id='solana',
            mint=native['mint'], source_user_id=native['source_user_id'], attempt=native)
        proof = facts.get('source_initiation')
        if not isinstance(proof, dict):
            continue
        ev = SimpleNamespace(signal_source='slot_lane', fomo_event_id=native['origin_event_id'],
            mint=native['mint'], source_user_id=native['source_user_id'], source_wallet=watcher['address'],
            source_block_number=facts.get('source_slot'), source_initiation=proof)
        if source_initiation_refusal(ev, 'solana') is not None:
            continue
        if native.get('tx_sig') or native.get('transaction_submitted_at') or conn.execute(
                "SELECT 1 FROM memecoin_pending_entries WHERE attempt_id=? AND "
                "(tx_sig IS NOT NULL OR signed_payload_sha256 IS NOT NULL OR submission_bound_at IS NOT NULL) LIMIT 1",
                (native['attempt_id'],)).fetchone():
            return True
    return False


def shadow_fast_lane_results(conn, attempt):
    """Off-path operator evidence for the same Solana signature, wallet and mint only."""
    parts = str(attempt.get("origin_event_id") or "").split(":")
    if (attempt.get("delivery_lane") != "j7_shadow" or attempt.get("network_id") != "solana"
            or len(parts) != 5 or parts[:2] != ["swap", "raybot"] or not parts[2]
            or not attempt.get("source_user_id") or not attempt.get("mint")):
        return []
    return [dict(row) for row in conn.execute(
        "SELECT attempt_id FROM memecoin_entry_attempts WHERE origin_event_id IN (?,?) "
        "AND network_id='solana' AND mint=? AND source_user_id=? "
        "AND signal_source IN ('slot_lane','onchain_watch') "
        "ORDER BY decision_started_at DESC LIMIT 20",
        ('onchain:' + parts[2], 'slotlane:' + parts[2], attempt['mint'], attempt['source_user_id']))]


def _detection_times(row):
    block_time = row[0] if type(row[0]) is int and row[0] > 0 else None
    return {"block_time": block_time, "detected_at": row[1],
            "block_ms": _header_block_ms(row[2] if len(row) > 2 else None, block_time)}


def _header_block_ms(valuation_json, block_time):
    """DISPLAY ONLY (7eb5c92c, 2026-10-17): the source block's header milliTimestamp (BSC), from the
    watcher's push_head (head_at*1000 - head_minus_block_ms), else None. Only a stamp inside the row's
    own block_time second is this block's; anything else, or a malformed valuation, is None."""
    if block_time is None or not valuation_json:
        return None
    try:
        head = _json.loads(valuation_json).get("push_head") or {}
        head_at, lag = head.get("head_at"), head.get("head_minus_block_ms")
        if type(head_at) not in (int, float) or type(lag) not in (int, float):
            return None
        block_ms = round(head_at * 1000.0 - lag)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None
    return block_ms if block_ms // 1000 == block_time else None


def onchain_detection_by_signature(conn, *, signature, source_user_id):
    """DISPLAY ONLY (D-a, Gamma B1 2026-10-16): our detection row of exactly this source transaction,
    as {"block_time": epoch s or None, "detected_at": ISO}, else None. Identity, never proximity: the
    signature is the detection's unique key ('<chain>:<tx>:<mint>' on EVM, the bare signature on
    Solana). A read error is None."""
    if not signature or not source_user_id:
        return None
    try:
        row = conn.execute(
            "SELECT block_time, detected_at, source_valuation_json FROM onchain_wallet_detections "
            "WHERE signature=? AND source_user_id=?", (str(signature), str(source_user_id))).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else _detection_times(row)


def sole_onchain_detection_near(conn, *, source_user_id, network_id, mint, around, window_s=30.0):
    """DISPLAY ONLY (D-a, Gamma B1 2026-10-16): our on-chain detection of this wallet buying this coin
    within `window_s` of `around` (ISO UTC), only when it is the ONLY one there, as `_detection_times`;
    else None. Proximity, never identity: a drip buyer's second buy in the window makes the match
    ambiguous, so two or more return None, and nothing that decides may read this. One indexed read
    (idx_mc_onchain_detect_wallet_coin); a read error is None."""
    if not source_user_id or not mint or not around:
        return None
    try:
        at = datetime.fromisoformat(str(around).replace("Z", "+00:00")).astimezone(timezone.utc)
        lo, hi = ((at + timedelta(seconds=d)).isoformat() for d in (-window_s, window_s))
        rows = conn.execute(
            "SELECT block_time, detected_at, source_valuation_json FROM onchain_wallet_detections "
            "WHERE source_user_id=? AND mint IN (?, ?) AND detected_at BETWEEN ? AND ? "
            "AND network_id=? LIMIT 2",
            (str(source_user_id), str(mint), str(mint).lower(), lo, hi,
             normalized_coin_network(network_id, mint))).fetchall()
    except (sqlite3.Error, TypeError, ValueError, OverflowError):
        return None
    return _detection_times(rows[0]) if len(rows) == 1 else None


def entry_view_evidence(conn, *, origin_event_id=None, attempt_id=None):
    """Read exact event and execution identities. Never return signed bytes."""
    chosen = None
    if attempt_id:
        row = conn.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?",
                           (str(attempt_id),)).fetchone()
        chosen = dict(row) if row else None
        if chosen:
            if origin_event_id and origin_event_id != chosen.get("origin_event_id"):
                raise ValueError("attempt and origin identities disagree")
            origin_event_id = chosen.get("origin_event_id")
    elif origin_event_id:
        chosen = entry_attempt_for_origin(conn, origin_event_id)
    event = None
    if origin_event_id:
        row = conn.execute("SELECT * FROM memecoin_events WHERE fomo_event_id=?",
                           (str(origin_event_id),)).fetchone()
        event = dict(row) if row else None
        attempts = [dict(r) for r in conn.execute(
            "SELECT * FROM memecoin_entry_attempts WHERE origin_event_id=? "
            "ORDER BY decision_started_at DESC, attempt_id DESC LIMIT 201", (str(origin_event_id),))]
    else:
        attempts = [chosen] if chosen else []
    if event is None and chosen:
        event = resolve_source_evidence(
            conn, origin_event_id, network_id=chosen.get("network_id"), mint=chosen.get("mint"),
            source_user_id=chosen.get("source_user_id"), attempt=chosen) or None
    target = "attempt_id=?" if attempt_id else "origin_event_id=?"
    identity = str(attempt_id or origin_event_id or "")
    reservations = [dict(r) for r in conn.execute(
        "SELECT id, attempt_id, origin_event_id, network_id, mint, reserved_notional_usd, "
        "reserved_at, status, tx_sig, broadcast_at, submission_bound_at, recovery_status, "
        "last_recovery_check_at, resolution_reason, cleanup_tx_sig, cleanup_status, "
        "(signed_payload_sha256 IS NOT NULL) AS has_signed_payload "
        f"FROM memecoin_pending_entries WHERE {target} ORDER BY id DESC LIMIT 201", (identity,))]
    positions = [dict(r) for r in conn.execute(
        "SELECT id, attempt_id, origin_event_id, network_id, mint, ticker, is_open, "
        "entry_price, entry_notional, opened_at, opened_recorded_at, funded_contract_json "
        f"FROM memecoin_positions WHERE execution_mode='real' AND {target} ORDER BY id DESC LIMIT 201",
        (identity,))]
    column = "attempt_id" if attempt_id else "origin_event_id"
    fill_select = (
        "SELECT f.id, f.position_id, COALESCE(f.attempt_id,p.attempt_id) AS attempt_id, "
        "COALESCE(f.origin_event_id,p.origin_event_id) AS origin_event_id, "
        "COALESCE(f.network_id,p.network_id) AS network_id, f.tx_sig, "
        "f.price, f.qty, f.notional, f.created_at, p.mint "
    )
    fills = [dict(r) for r in conn.execute(
        fill_select + "FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
        f"WHERE p.execution_mode='real' AND f.kind IN ('entry','manual_buy') AND f.{column}=? "
        "ORDER BY f.id DESC LIMIT 201", (identity,))]
    if positions:
        ids = [p["id"] for p in positions]
        marks = ",".join("?" for _ in ids)
        legacy = conn.execute(
            fill_select + "FROM memecoin_fills f INDEXED BY idx_mc_fills_pos "
            "JOIN memecoin_positions p ON p.id=f.position_id "
            f"WHERE f.position_id IN ({marks}) AND f.{column} IS NULL "
            "AND f.kind IN ('entry','manual_buy') ORDER BY f.id DESC LIMIT 201", ids)
        fills = sorted(fills + [dict(r) for r in legacy], key=lambda r: r["id"], reverse=True)[:201]
    retry = None
    if origin_event_id:
        row = conn.execute(
            "SELECT fomo_event_id, network_id, mint, first_queued_at, next_attempt_at, attempts "
            "FROM memecoin_entry_retry_queue WHERE fomo_event_id=?", (str(origin_event_id),)).fetchone()
        retry = dict(row) if row else None
    receipt = None
    if origin_event_id:
        try:
            row = conn.execute("SELECT received_at FROM j7_receipts WHERE fomo_event_id=?",
                               (str(origin_event_id),)).fetchone()
            receipt = row[0] if row else None
        except sqlite3.OperationalError as exc:
            if "no such table: j7_receipts" not in str(exc):
                raise
    return {"event": event, "chosen": chosen, "attempts": attempts, "reservations": reservations,
            "positions": positions, "fills": fills, "retry": retry, "socket_received_at": receipt}


def recent_entry_identities(conn, *, limit=100, chain=None):
    """Read bounded recent origins plus current reservation and retry owners."""
    limit = min(200, max(1, int(limit)))
    chain_sql, chain_args = _chain_clause(chain)
    rows = []
    queries = (
        ("SELECT fomo_event_id AS origin_event_id, NULL AS attempt_id, network_id, "
         "COALESCE(received_at_normalized,created_at) AS at FROM memecoin_events "
         # A source SELL is never an entry: 41 of the last 100 "entry results" were ignored sells
         # reading "Source received" (2026-09-11).
         "WHERE (action IS NULL OR action!='candidate') AND (side IS NULL OR side!='sell')"
         + chain_sql + " ORDER BY id DESC LIMIT ?"),
        ("SELECT origin_event_id, attempt_id, network_id, decision_started_at AS at "
         "FROM memecoin_entry_attempts WHERE 1=1" + chain_sql
         + " ORDER BY decision_started_at DESC, attempt_id DESC LIMIT ?"),
        ("SELECT origin_event_id, attempt_id, network_id, COALESCE(last_recovery_check_at,reserved_at) AS at "
         "FROM memecoin_pending_entries WHERE status='reserved'" + chain_sql + " ORDER BY id DESC LIMIT ?"),
        ("SELECT fomo_event_id AS origin_event_id, NULL AS attempt_id, network_id, first_queued_at AS at "
         "FROM memecoin_entry_retry_queue WHERE 1=1" + chain_sql + " ORDER BY first_queued_at DESC LIMIT ?"),
        ("SELECT origin_event_id, attempt_id, network_id, created_at AS at FROM memecoin_fills "
         "WHERE kind IN ('entry','manual_buy') AND attempt_id IS NOT NULL" + chain_sql
         + " ORDER BY id DESC LIMIT ?"),
    )
    for sql in queries:
        rows.extend(dict(r) for r in conn.execute(sql, (*chain_args, limit)))
    seen, out = set(), []
    for row in sorted(rows, key=lambda r: (r.get("at") or "", r.get("attempt_id") or ""), reverse=True):
        key = (normalized_coin_network(row.get("network_id"), ""),
               row.get("origin_event_id") or row.get("attempt_id"))
        if key[1] and key not in seen:
            seen.add(key)
            out.append(row)
        if len(out) == limit:
            break
    return out


def entry_view_revision(conn):
    """Hash bounded result evidence; price-tape writes do not invalidate entry views."""
    queries = (
        "SELECT attempt_id,terminal_status,completed_at,pending_id,position_id,fill_id,tx_sig "
        "FROM memecoin_entry_attempts ORDER BY decision_started_at DESC LIMIT 200",
        "SELECT attempt_id,terminal_status,completed_at FROM memecoin_entry_attempts "
        "WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 200",
        "SELECT id,real_decision,real_decision_reason,real_decision_at FROM memecoin_events ORDER BY id DESC LIMIT 200",
        "SELECT id,status,tx_sig,broadcast_at,recovery_status,last_recovery_check_at,resolution_reason "
        "FROM memecoin_pending_entries ORDER BY id DESC LIMIT 200",
        "SELECT id,status,tx_sig,broadcast_at,recovery_status,last_recovery_check_at,resolution_reason "
        "FROM memecoin_pending_entries WHERE status='reserved' ORDER BY id DESC LIMIT 200",
        "SELECT fomo_event_id,next_attempt_at,attempts FROM memecoin_entry_retry_queue "
        "ORDER BY first_queued_at DESC LIMIT 200",
        "SELECT id,attempt_id,position_id FROM memecoin_fills WHERE kind IN ('entry','manual_buy') "
        "ORDER BY id DESC LIMIT 200",
        "SELECT intent_id,state,attempt_id,result_status,resolved_at FROM memecoin_manual_intents "
        "ORDER BY requested_at DESC LIMIT 200",
    )
    evidence = [[tuple(r) for r in conn.execute(sql)] for sql in queries]
    from sniper.persistence.db_memecoin_notifications import notification_delivery_revision
    evidence.append(notification_delivery_revision(conn))
    return hashlib.sha256(_json.dumps(evidence, separators=(",", ":")).encode()).hexdigest()[:24]


def position_value_view(conn, position_id, *, last_price, entry_notional, open_quantity=None):
    """Keep remaining token value separate from cash already recovered."""
    qty = open_qty(conn, position_id) if open_quantity is None else open_quantity
    has_entry = conn.execute("SELECT 1 FROM memecoin_fills WHERE position_id=? "
                             "AND kind IN ('entry','manual_buy') LIMIT 1", (position_id,)).fetchone()
    placeholders = ",".join("?" for _ in EXIT_KINDS)
    recovered = float(conn.execute(
        f"SELECT COALESCE(SUM(notional),0) FROM memecoin_fills WHERE position_id=? "
        f"AND kind IN ({placeholders})", (position_id, *EXIT_KINDS)).fetchone()[0])
    valid_price = last_price is not None and math.isfinite(float(last_price)) and float(last_price) >= 0
    remaining = max(0, float(qty)) * float(last_price) if valid_price and has_entry else None
    total = remaining + recovered - float(entry_notional) if (
        remaining is not None and entry_notional is not None) else None
    pct = total / float(entry_notional) * 100 if total is not None and float(entry_notional or 0) > 0 else None
    return {"open_qty": qty if has_entry else None, "remaining_value_usd": remaining, "recovered_usd": recovered,
            "total_pnl_usd": total, "total_pnl_pct": pct, "value_basis": "selected_exit",
            "cost_basis": "ledger_notional"}


def operator_position_fills(conn, position_id):
    """Read bounded public fill evidence for one exact position."""
    return [dict(row) for row in conn.execute(
        "SELECT id,position_id,kind,price,qty,notional,created_at,tx_sig,attempt_id,origin_event_id,network_id,"
        "fill_source FROM memecoin_fills WHERE position_id=? ORDER BY id DESC LIMIT 200", (position_id,))]


# Raw historical measurements remain readable; hypothetical conclusions are ineligible.
from sniper.research.wallet_execution.publication import diagnostic_result as _diagnostic_result
strategy_summary = _diagnostic_result(strategy_summary)
strategy_summary_matched = _diagnostic_result(strategy_summary_matched)
signal_source_summary = _diagnostic_result(signal_source_summary)
trader_performance = _diagnostic_result(trader_performance)
trader_arm_matrix = _diagnostic_result(trader_arm_matrix)
wallet_stats = _diagnostic_result(wallet_stats)
promotion_lane_rollup = _diagnostic_result(promotion_lane_rollup)


def wallet_entry_cap_evidence(conn, *, since):
    """Background-only recorded funded closes; never read on an entry or reserve thread."""
    conn.execute("PRAGMA busy_timeout=250")  # private, short-lived read-only handle
    return [dict(r) for r in conn.execute(
        "SELECT source_user_id, network_id, mint, realized_pnl, entry_notional, "
        "entry_fill_source, signal_source, closed_recorded_at "
        "FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
        "AND (closed_recorded_at >= ? OR (closed_recorded_at IS NULL AND closed_at >= ?)) "
        "AND COALESCE(signal_source, '') NOT IN ('manual', 'manual-unmanaged', 'desk')",
        (since, since)).fetchall()]


def funded_fill_signatures(conn, mint, network_id="solana"):
    """Every on-chain signature of OUR funded fills on one mint: each fill's tx_sig plus the positions' derisk_sig / close_sig, for
    every execution_mode='real' row. Read-only (pass get_readonly_connection). The market-structure and attention readers learn our
    wallet from these transactions and drop its swaps, so our own entry or exit never reads as demand or supply. Raises on a read
    error: a caller that cannot name our fills must not assume there are none."""
    sigs = set()
    for derisk, close in conn.execute("SELECT derisk_sig, close_sig FROM memecoin_positions "
                                      "WHERE mint=? AND network_id=? AND execution_mode='real'", (mint, network_id)):
        sigs.update(s for s in (derisk, close) if isinstance(s, str) and s)
    for (sig,) in conn.execute("SELECT f.tx_sig FROM memecoin_fills f JOIN memecoin_positions p ON p.id=f.position_id "
                               "WHERE p.mint=? AND p.network_id=? AND p.execution_mode='real' AND f.tx_sig IS NOT NULL "
                               "AND f.tx_sig<>''", (mint, network_id)):
        sigs.add(sig)
    return tuple(sorted(sigs))


def roster_state_rows(conn):
    """{source_user_id: (paused, excluded, real_approved, real_chains)}: the roster's CURRENT state, read-only,
    for the desk roster-history snapshot worker (scripts/memecoin_desk_roster_history_worker.py)."""
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute(
        "SELECT source_user_id, paused, excluded, real_approved, real_chains FROM memecoin_wallets "
        "WHERE source_user_id IS NOT NULL")}


def desk_buyer_ledger(conn, network_id="solana"):
    """Read-only inputs of the desk buyers fact (cohort C prereg b90ddeb59 1a), as-of filtered by the caller:
    registry: [(address, source_user_id, to_status, at)] every address-registry event on the network;
    closes:   {source_user_id: [(closed_at, mint, realized_pnl)]} robot real closes on the network that grade wallets."""
    registry = [tuple(r) for r in conn.execute(
        "SELECT address, source_user_id, to_status, at FROM memecoin_address_registry_events WHERE network_id=? "
        "ORDER BY at, id", (network_id,))]
    closes = {}
    for uid, closed_at, mint, pnl in conn.execute(
            "SELECT source_user_id, closed_at, mint, realized_pnl FROM memecoin_positions WHERE execution_mode='real' "
            "AND is_open=0 AND realized_pnl IS NOT NULL AND closed_at IS NOT NULL AND network_id=? AND "
            + robot_decision_sql() + " AND " + WALLET_GRADE_SQL, (network_id,)):
        closes.setdefault(uid, []).append((closed_at, mint, float(pnl)))
    return {"registry": registry, "closes": closes}
