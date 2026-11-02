"""Versioned, chain-aware forward quote capture with explicit coverage gaps."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sniper.execution.solana_quote import FOMO_TO_DEX, norm_mint, quote_key
from sniper.persistence import db_memecoin

logger = logging.getLogger(__name__)


CONTEXTS = {"signal", "candidate", "control", "position"}
RATIO_MIN = 1e-4
RATIO_MAX = 1e3
CLUSTER_BLOCK_HORIZONS_S = (15, 30, 60, 300)


@dataclass(frozen=True)
class CaptureSweepResult:
    sweep_id: str
    eligible: int
    requested: int
    captured: int
    budget_skipped: int
    price_map: dict = field(repr=False)


@dataclass(frozen=True)
class HorizonResult:
    observed: int = 0
    missed: int = 0
    abandoned: int = 0
    completed: int = 0


def _iso_add(iso, seconds):
    parsed = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=float(seconds))).isoformat()


def _blind_networks():
    """Chains DexScreener cannot price; their tape comes from the blind-price worker."""
    from sniper.settings import memecoin as cfg
    return set(cfg.MEMECOIN_DEX_BLIND_CHAINS or ())


def _supported_capture_networks():
    """Networks an outcome/candidate can be captured on: DexScreener chains + blind-probe chains."""
    return set(FOMO_TO_DEX) | _blind_networks()


def _contexts(value):
    return {item for item in str(value or "").split(",") if item}


def register_capture_cohort(conn, network_id, mint, context, observed_at, expires_at, priority,
                            *, commit=True):
    """Upsert one chain/mint episode, merging contexts and extending its useful lifetime."""
    if context not in CONTEXTS:
        raise ValueError(f"unsupported capture context: {context}")
    network = str(network_id or ("solana" if not str(mint).lower().startswith("0x") else ""))
    if not network:
        raise ValueError("network_id required for EVM capture")
    normalized = norm_mint(mint)
    row = conn.execute(
        "SELECT * FROM memecoin_capture_cohorts_v2 WHERE network_id=? AND mint=?",
        (network, normalized),
    ).fetchone()
    if row:
        current = dict(row)
        contexts = ",".join(sorted(_contexts(current["contexts"]) | {context}))
        conn.execute(
            "UPDATE memecoin_capture_cohorts_v2 SET contexts=?, last_observed_at=?, "
            "expires_at=?, priority=?, next_due_at=MIN(next_due_at, ?) WHERE network_id=? AND mint=?",
            (contexts, max(str(current["last_observed_at"]), str(observed_at)),
             max(str(current["expires_at"]), str(expires_at)),
             max(int(current["priority"]), int(priority)), observed_at, network, normalized),
        )
    else:
        conn.execute(
            "INSERT INTO memecoin_capture_cohorts_v2(network_id, mint, contexts, first_observed_at, "
            "last_observed_at, expires_at, priority, next_due_at) VALUES (?,?,?,?,?,?,?,?)",
            (network, normalized, context, observed_at, observed_at, expires_at, int(priority), observed_at),
        )
    if commit:
        conn.commit()


def create_outcome_v2(conn, *, upstream_event_id, source_user_id, handle, network_id, mint,
                      ticker, signal_observed_at, ref_price, horizons, created_at):
    """Create immutable horizon targets for one actionable signal observation."""
    try:
        ref_price = float(ref_price)
    except (TypeError, ValueError):
        raise ValueError("ref_price must be positive") from None
    if ref_price <= 0 or not math.isfinite(ref_price):
        raise ValueError("ref_price must be positive")
    for name, value in (("signal_observed_at", signal_observed_at), ("created_at", created_at)):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be timezone-aware ISO-8601") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware ISO-8601")
    try:
        raw_targets = list(horizons)
    except TypeError:
        raise ValueError("horizons must be positive whole seconds") from None
    targets = []
    for horizon in raw_targets:
        if isinstance(horizon, bool):
            raise ValueError("horizons must be positive whole seconds")
        try:
            numeric = float(horizon)
        except (TypeError, ValueError):
            raise ValueError("horizons must be positive whole seconds") from None
        if not math.isfinite(numeric) or numeric <= 0 or not numeric.is_integer():
            raise ValueError("horizons must be positive whole seconds")
        targets.append(int(numeric))
    targets = sorted(set(targets))
    if not targets:
        raise ValueError("horizons must be positive whole seconds")
    if not upstream_event_id:
        raise ValueError("upstream_event_id required")
    if not mint:
        raise ValueError("mint required")
    network = str(network_id or ("solana" if not str(mint).lower().startswith("0x") else ""))
    if network not in _supported_capture_networks():
        raise ValueError("supported network_id required")
    normalized = norm_mint(mint)
    targets_json = json.dumps(targets, separators=(",", ":"))
    existing = conn.execute(
        "SELECT outcome_id, network_id, mint, signal_observed_at, ref_price, "
        "target_horizons_json FROM memecoin_signal_outcomes_v2 WHERE upstream_event_id=?",
        (upstream_event_id,),
    ).fetchone()
    if existing:
        same = (
            existing["network_id"] == network
            and existing["mint"] == normalized
            and existing["signal_observed_at"] == signal_observed_at
            and float(existing["ref_price"]) == ref_price
            and existing["target_horizons_json"] == targets_json
        )
        if not same:
            raise ValueError("conflicting retry for immutable upstream event")
        return existing[0]
    digest = hashlib.sha256(
        f"{upstream_event_id}|{network}|{normalized}|{signal_observed_at}".encode(),
    ).hexdigest()[:24]
    outcome_id = "mc2-" + digest
    observation = conn.execute(
        "SELECT id FROM memecoin_feed_observations_v2 WHERE upstream_event_id=?",
        (upstream_event_id,),
    ).fetchone()
    conn.execute("SAVEPOINT create_memecoin_outcome_v2")
    try:
        conn.execute(
            "INSERT INTO memecoin_signal_outcomes_v2(outcome_id, upstream_event_id, "
            "entry_observation_id, source_user_id, handle, network_id, mint, ticker, "
            "signal_observed_at, ref_price, target_horizons_json, expected_marks, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (outcome_id, upstream_event_id, observation[0] if observation else None, source_user_id,
             handle, network, normalized, ticker, signal_observed_at, ref_price,
             targets_json, len(targets), created_at),
        )
        conn.executemany(
            "INSERT INTO memecoin_horizon_marks_v2(outcome_id, target_s, target_at, status) "
            "VALUES (?,?,?,'pending')",
            [(outcome_id, target, _iso_add(signal_observed_at, target)) for target in targets],
        )
        conn.execute("RELEASE SAVEPOINT create_memecoin_outcome_v2")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT create_memecoin_outcome_v2")
        conn.execute("RELEASE SAVEPOINT create_memecoin_outcome_v2")
        raise
    conn.commit()
    return outcome_id


def create_cluster_block_outcome(
        conn, *, attempt_id: str, mint: str, observed_at: str, ref_price: float,
        source_user_id: str | None = None, handle: str | None = None) -> str:
    """Open the fixed V2 path for one blocked funded opportunity."""
    return create_outcome_v2(
        conn,
        upstream_event_id=f"cluster-block:{attempt_id}",
        source_user_id=source_user_id,
        handle=handle,
        network_id="solana",
        mint=mint,
        ticker=None,
        signal_observed_at=observed_at,
        ref_price=ref_price,
        horizons=CLUSTER_BLOCK_HORIZONS_S,
        created_at=observed_at,
    )


def record_cluster_block_outcome(conn, **kwargs) -> str:
    """Record a typed missing reference or open one measured V2 outcome."""
    attempt_id = kwargs["attempt_id"]
    ref_price = kwargs.pop("ref_price", None)
    try:
        valid_price = float(ref_price)
    except (TypeError, ValueError):
        valid_price = 0.0
    if not math.isfinite(valid_price) or valid_price <= 0:
        db_memecoin.set_entry_attempt_cluster_outcome(
            conn,
            attempt_id,
            outcome_id=None,
            status="coverage_missing_ref_price",
        )
        return "coverage_missing_ref_price"
    outcome_id = create_cluster_block_outcome(
        conn, ref_price=valid_price, **kwargs
    )
    db_memecoin.set_entry_attempt_cluster_outcome(
        conn, attempt_id, outcome_id=outcome_id, status="open"
    )
    return "open"


def _number(value, *, positive=False):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or (positive and value <= 0):
        return None
    return value


def _classify_and_update(conn, cohort, stats):
    if not stats:
        return "missing_pair", None
    expected_chain = FOMO_TO_DEX.get(cohort["network_id"])
    if stats.get("chain") != expected_chain:
        return "wrong_chain", None
    if norm_mint(stats.get("base_address") or "") != cohort["mint"]:
        return "wrong_base", None
    pair = str(stats.get("pair_address") or "")
    if not pair:
        return "missing_pair", None
    price = _number(stats.get("price"), positive=True)
    if price is None:
        raw = _number(stats.get("price"))
        return ("nonpositive" if raw is not None else "nonfinite"), None

    canonical = cohort.get("canonical_pair_address")
    pending = cohort.get("pending_pair_address")
    pending_count = int(cohort.get("pending_pair_count") or 0)
    quality = "ok"
    if canonical is None:
        canonical = pair
        pending = None
        pending_count = 0
    elif pair != canonical:
        if pair == pending:
            pending_count += 1
        else:
            pending = pair
            pending_count = 1
        if pending_count >= 2:
            canonical = pair
            pending = None
            pending_count = 0
        else:
            quality = "pair_changed"
    else:
        pending = None
        pending_count = 0

    last_good = _number(cohort.get("last_good_price"), positive=True)
    if quality == "ok" and last_good is not None:
        ratio = price / last_good
        if ratio < RATIO_MIN or ratio > RATIO_MAX:
            quality = "ratio_outlier"
    if quality == "ok" and _number(stats.get("liquidity"), positive=True) is None:
        quality = "liquidity_missing"
    conn.execute(
        "UPDATE memecoin_capture_cohorts_v2 SET canonical_pair_address=?, "
        "pending_pair_address=?, pending_pair_count=?, last_good_price=? "
        "WHERE network_id=? AND mint=?",
        (canonical, pending, pending_count,
         price if quality == "ok" else cohort.get("last_good_price"),
         cohort["network_id"], cohort["mint"]),
    )
    return quality, price


def capture_v2_sweep(conn, now_iso, stats_batch_fn, max_mints, *, sweep_id=None, period_s=30.0,
                     exclude_networks=None):
    """Capture one ordered, budgeted quote sweep and persist missing/suspect observations.

    Blind chains (DexScreener has no pair — 4663 measured 545k/545k missing_pair) are excluded
    from eligibility entirely: sweeping them here can only spam missing_pair rows and burn
    budget slots. Their tape comes from `capture_blind_pass` instead."""
    sweep_id = sweep_id or uuid.uuid4().hex
    excluded = _blind_networks() if exclude_networks is None else set(exclude_networks)
    eligible_rows = [dict(row) for row in conn.execute(
        "SELECT * FROM memecoin_capture_cohorts_v2 WHERE expires_at>=? AND next_due_at<=? "
        "ORDER BY priority DESC, next_due_at ASC, network_id ASC, mint ASC",
        (now_iso, now_iso),
    ) if row["network_id"] not in excluded]
    limit = max(0, int(max_mints))
    selected = eligible_rows[:limit]
    skipped = eligible_rows[limit:]
    for cohort in skipped:
        conn.execute(
            "UPDATE memecoin_capture_cohorts_v2 SET skipped_sweep_count=skipped_sweep_count+1 "
            "WHERE network_id=? AND mint=?",
            (cohort["network_id"], cohort["mint"]),
        )
    keys = [(cohort["network_id"], cohort["mint"]) for cohort in selected]
    # Never wait on the network holding the ledger's write lock: every other writer (funded
    # threads, the web) queues behind it for the whole fetch.
    conn.commit()
    try:
        price_map = stats_batch_fn(keys) if keys else {}
    except Exception:
        price_map = {}
    captured = 0
    for cohort in selected:
        key = quote_key(cohort["network_id"], cohort["mint"])
        stats = (price_map or {}).get(key)
        quality, price = _classify_and_update(conn, cohort, stats)
        if price is not None:
            captured += 1
        conn.execute(
            "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, pair_address, "
            "dex_id, observed_at, source_ts, price, liquidity, market_cap, volume_24h, quality, "
            "contexts, capture_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,2)",
            (sweep_id, cohort["network_id"], cohort["mint"], (stats or {}).get("pair_address"),
             (stats or {}).get("dex_id"), now_iso, (stats or {}).get("source_ts"), price,
             _number((stats or {}).get("liquidity")), _number((stats or {}).get("market_cap")),
             _number((stats or {}).get("volume_24h")), quality, cohort["contexts"]),
        )
        conn.execute(
            "UPDATE memecoin_capture_cohorts_v2 SET next_due_at=? WHERE network_id=? AND mint=?",
            (_iso_add(now_iso, period_s), cohort["network_id"], cohort["mint"]),
        )
    conn.execute(
        "INSERT INTO memecoin_quote_sweeps_v2(sweep_id, observed_at, eligible_count, requested_count, "
        "captured_count, budget_skipped_count, capture_version) VALUES (?,?,?,?,?,?,2)",
        (sweep_id, now_iso, len(eligible_rows), len(selected), captured, len(skipped)),
    )
    conn.commit()
    return CaptureSweepResult(
        sweep_id, len(eligible_rows), len(selected), captured, len(skipped), price_map or {},
    )


def _blind_capture_rows(conn, now_iso, blind):
    """Deadline-aware blind-chain cohorts without changing the provider request budget.

    A stored cohort priority cannot express point-in-time funded eligibility: candidate capture
    deliberately registers every coin, while the scarce 4663 quote slots must first cover positions
    and signals whose source wallet was armed when observed. Pending horizon truth is joined by exact
    `(network_id, mint)`; address shape never selects a chain.
    """
    if not blind:
        return []
    placeholders = ",".join("?" for _ in blind)
    rows = [dict(row) for row in conn.execute(
        f"SELECT * FROM memecoin_capture_cohorts_v2 WHERE expires_at>=? AND next_due_at<=? "
        f"AND network_id IN ({placeholders})",
        (now_iso, now_iso, *sorted(blind)),
    )]
    pending = {}
    for row in conn.execute(
        f"SELECT o.network_id, o.mint, MIN(h.target_at) AS pending_target_at, "
        f"MAX(COALESCE(f.was_real_approved,0)) AS has_approved_pending "
        f"FROM memecoin_signal_outcomes_v2 o "
        f"JOIN memecoin_horizon_marks_v2 h ON h.outcome_id=o.outcome_id "
        f"LEFT JOIN memecoin_feed_observations_v2 f ON f.id=o.entry_observation_id "
        f"WHERE o.status='open' AND h.status='pending' AND o.network_id IN ({placeholders}) "
        f"GROUP BY o.network_id, o.mint",
        tuple(sorted(blind)),
    ):
        pending[(row["network_id"], row["mint"])] = {
            "pending_target_at": row["pending_target_at"],
            "has_approved_pending": bool(row["has_approved_pending"]),
        }

    def order(row):
        key = (row["network_id"], row["mint"])
        horizon = pending.get(key) or {}
        contexts = _contexts(row.get("contexts"))
        if "position" in contexts:
            tier = 0
        elif horizon.get("has_approved_pending"):
            tier = 1
        elif horizon.get("pending_target_at"):
            tier = 2
        else:
            tier = 3
        deadline = horizon.get("pending_target_at") or row["next_due_at"]
        return (tier, -int(row.get("priority") or 0), deadline, row["next_due_at"],
                row["network_id"], row["mint"])

    return sorted(rows, key=order)


def capture_blind_pass(conn, now_iso, *, price_batch_fn=None, max_mints=None, blind_chains=None,
                       sweep_id=None, period_s=None):
    """One bounded GeckoTerminal sweep over blind-chain cohorts (runs in a background worker).

    DexScreener-blind chains (4663) get their forward tape here: probe price = proceeds of
    GeckoTerminal indexing. Quality vocabulary is probe-distinct —
    `probe_ok` / `probe_ratio_outlier` / `probe_unpriced` — NEVER `ok`, so every downstream
    consumer of DexScreener-grade marks has to opt in explicitly. Liquidity is never fabricated
    (a swap quote does not know it); an unpriceable mint gets an explicit `probe_unpriced` gap
    row, matching this module's coverage-gap philosophy. A mint absent from the probe map may be
    parked by evm_price_batch's fail-backoff — that is still an honest "no price this sweep"."""
    from sniper.settings import memecoin as cfg
    sweep_id = sweep_id or uuid.uuid4().hex
    blind = _blind_networks() if blind_chains is None else set(blind_chains)
    period_s = cfg.MEMECOIN_CAPTURE_BLIND_PERIOD_S if period_s is None else period_s
    max_mints = cfg.MEMECOIN_CAPTURE_BLIND_MAX_MINTS if max_mints is None else max_mints
    if not blind:
        return CaptureSweepResult(sweep_id, 0, 0, 0, 0, {})
    placeholders = ",".join("?" for _ in blind)
    eligible_rows = _blind_capture_rows(conn, now_iso, blind)
    # The real-position sweeper already obtains corrected-UTC GeckoTerminal marks for many 4663
    # coins. Reuse those exact observations before spending this worker's network budget. This is
    # v2-only: the legacy outcome table remains untouched.
    cutoff_iso = _iso_add(now_iso, -2.0 * float(period_s))
    eligible_keys = {(row["network_id"], row["mint"]) for row in eligible_rows}
    fresh_marks = {}
    mark_rows = conn.execute(
        f"SELECT pm.chain_id, pm.mint, pm.sweep_ts, pm.mid, pm.liquidity, pm.mcap, "
        f"pm.source, pm.quality FROM memecoin_capture_cohorts_v2 c "
        f"JOIN memecoin_position_marks pm ON pm.chain_id=c.network_id AND pm.mint=c.mint "
        f"WHERE c.expires_at>=? AND c.next_due_at<=? AND c.network_id IN ({placeholders}) "
        "AND pm.quality='live' AND julianday(pm.sweep_ts)>=julianday(?) "
        "AND julianday(pm.sweep_ts)<=julianday(?) ORDER BY pm.sweep_ts DESC, pm.id DESC",
        (now_iso, now_iso, *sorted(blind), cutoff_iso, now_iso),
    )
    for raw in mark_rows:
        row = dict(raw)
        key = (str(row["chain_id"]), norm_mint(row["mint"]))
        if key not in eligible_keys or key in fresh_marks:
            continue
        price = _number(row["mid"], positive=True)
        if price is None:
            continue
        fresh_marks[key] = {
            "price": price,
            "liquidity": _number(row["liquidity"]),
            "mcap": _number(row["mcap"]),
            "source": str(row["source"] or "position_mark"),
            "observed_at": row["sweep_ts"],
            "source_ts": row["sweep_ts"],
            "position_mark": True,
        }

    reused = [row for row in eligible_rows
              if (row["network_id"], row["mint"]) in fresh_marks]
    remaining = [row for row in eligible_rows
                 if (row["network_id"], row["mint"]) not in fresh_marks]
    limit = max(0, int(max_mints))
    probed = remaining[:limit]
    selected = reused + probed
    skipped = remaining[limit:]
    for cohort in skipped:
        conn.execute(
            "UPDATE memecoin_capture_cohorts_v2 SET skipped_sweep_count=skipped_sweep_count+1 "
            "WHERE network_id=? AND mint=?",
            (cohort["network_id"], cohort["mint"]),
        )
    if price_batch_fn is None:
        price_batch_fn = _default_blind_price_batch_fn(blind)
    keys = [(cohort["network_id"], cohort["mint"]) for cohort in probed]
    conn.commit()        # same rule as capture_v2_sweep: never hold the write lock across a fetch
    try:
        probed_prices = price_batch_fn(keys) if keys else {}
    except Exception:
        probed_prices = {}
    price_map = dict(fresh_marks)
    price_map.update(probed_prices or {})
    captured = 0
    for cohort in selected:
        stats = (price_map or {}).get((cohort["network_id"], cohort["mint"]))
        price = _number((stats or {}).get("price"), positive=True)
        if price is None:
            quality = "probe_unpriced"
        else:
            quality = "probe_ok"
            last_good = _number(cohort.get("last_good_price"), positive=True)
            if last_good is not None:
                ratio = price / last_good
                if ratio < RATIO_MIN or ratio > RATIO_MAX:
                    quality = "probe_ratio_outlier"   # spike never anchors or feeds marks
            if quality == "probe_ok":
                captured += 1
                conn.execute(
                    "UPDATE memecoin_capture_cohorts_v2 SET last_good_price=? "
                    "WHERE network_id=? AND mint=?",
                    (price, cohort["network_id"], cohort["mint"]),
                )
        # Provenance is the true source. An unpriced gap has no source -> a neutral 'probe' tag.
        source = str((stats or {}).get("source") or "probe")
        from_position_mark = bool((stats or {}).get("position_mark"))
        observed_at = ((stats or {}).get("observed_at") if from_position_mark else now_iso)
        source_ts = ((stats or {}).get("source_ts") if from_position_mark else None)
        pair_address = ("position_mark:" + str(cohort["network_id"])
                        if from_position_mark else source + ":" + str(cohort["network_id"]))
        conn.execute(
            "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, pair_address, "
            "dex_id, observed_at, source_ts, price, liquidity, market_cap, volume_24h, quality, "
            "contexts, capture_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,2)",
            (sweep_id, cohort["network_id"], cohort["mint"], pair_address, source,
             observed_at, source_ts, price if quality != "probe_unpriced" else None,
             _number((stats or {}).get("liquidity")) if from_position_mark else None,
             _number((stats or {}).get("mcap")), None, quality, cohort["contexts"]),
        )
        conn.execute(
            "UPDATE memecoin_capture_cohorts_v2 SET next_due_at=? WHERE network_id=? AND mint=?",
            (_iso_add(now_iso, period_s), cohort["network_id"], cohort["mint"]),
        )
    conn.execute(
        "INSERT INTO memecoin_quote_sweeps_v2(sweep_id, observed_at, eligible_count, requested_count, "
        "captured_count, budget_skipped_count, capture_version) VALUES (?,?,?,?,?,?,2)",
        (sweep_id, now_iso, len(eligible_rows), len(selected), captured, len(skipped)),
    )
    conn.commit()
    return CaptureSweepResult(
        sweep_id, len(eligible_rows), len(selected), captured, len(skipped), price_map or {},
    )


def _default_blind_price_batch_fn(blind, *, gt_batch_fn=None):
    """Bind blind-chain pricing to GeckoTerminal only.

    Missing keys remain unpriced. `gt_batch_fn` is the test seam."""
    from sniper.settings import memecoin as cfg
    _ = blind  # caller already filters the input keys by this set
    if gt_batch_fn is None and cfg.MEMECOIN_GT_ENABLED:
        from sniper.execution.geckoterminal_quote import gt_stats_batch_keyed
        gt_batch_fn = gt_stats_batch_keyed

    def price_batch_fn(keys):
        out = {}
        if gt_batch_fn is not None:
            try:
                out.update(gt_batch_fn(keys) or {})
            except Exception:
                logger.exception("geckoterminal blind batch failed")
        return out

    return price_batch_fn


def register_candidate_cohorts(conn, events, now_iso, *, ttl_s=None):
    """Open low-priority capture coverage for every concrete-chain coin the feed shows us.

    Corpus study 2026-07-16: capture gated behind entry/curation made every lifecycle and
    consensus dataset survivorship-only (entered coins showed ~3.5x higher MFE). This registers
    a `candidate` cohort per (network, mint) BUY sighting — including sub-discovery-floor buys —
    so the forward tape starts at first sighting, not at entry. Priority 50 keeps positions(400)
    and live outcomes(100) first in the sweep budget; `evm-unknown` stays out until identity
    heals (a probe against a guessed chain would fabricate prices)."""
    from sniper.settings import memecoin as cfg
    if not cfg.MEMECOIN_CAPTURE_CANDIDATES_ENABLED:
        return 0
    ttl_s = cfg.MEMECOIN_CAPTURE_CANDIDATE_TTL_S if ttl_s is None else ttl_s
    supported = _supported_capture_networks()
    expires = _iso_add(now_iso, ttl_s)
    coins = {}
    for event in events or []:
        row = event if isinstance(event, dict) else event.__dict__
        mint = row.get("mint")
        if row.get("side") != "buy" or not mint:
            continue
        # same inference register_capture_cohort applies: an omitted chain on a non-0x mint is
        # Solana; an omitted/ambiguous chain on an 0x mint is never guessed (evm-unknown skipped).
        network = str(row.get("network_id")
                      or ("solana" if not str(mint).lower().startswith("0x") else ""))
        if network not in supported:
            continue
        coins[(network, norm_mint(mint))] = mint
    for (network, _normalized), mint in coins.items():
        register_capture_cohort(
            conn, network, mint, "candidate", now_iso, expires, 50, commit=False,
        )
    if coins:
        conn.commit()
    return len(coins)


def _seconds_between(later, earlier):
    later_dt = datetime.fromisoformat(str(later).replace("Z", "+00:00"))
    earlier_dt = datetime.fromisoformat(str(earlier).replace("Z", "+00:00"))
    return (later_dt - earlier_dt).total_seconds()


def assign_due_horizons(conn, now_iso, *, tolerance_s=None, abandon_s=None):
    """Assign eligible observations once, then make overdue missingness permanent."""
    from sniper.settings import memecoin as cfg

    tolerance_s = max(0.0, float(
        max(90.0, cfg.MEMECOIN_PUMP_CAPTURE_PERIOD_S * 2)
        if tolerance_s is None else tolerance_s
    ))
    abandon_s = max(0.0, float(
        cfg.MEMECOIN_PUMP_ABANDON_S if abandon_s is None else abandon_s
    ))
    observed = missed = abandoned = completed = 0
    outcomes = [dict(row) for row in conn.execute(
        "SELECT * FROM memecoin_signal_outcomes_v2 WHERE status='open' "
        "ORDER BY signal_observed_at, outcome_id",
    )]
    for outcome in outcomes:
        marks = [dict(row) for row in conn.execute(
            "SELECT * FROM memecoin_horizon_marks_v2 WHERE outcome_id=? ORDER BY target_s",
            (outcome["outcome_id"],),
        )]
        final_target = max((int(mark["target_s"]) for mark in marks), default=0)
        for mark in marks:
            if mark["status"] != "pending" or _seconds_between(now_iso, mark["target_at"]) < 0:
                continue
            window_end = _iso_add(mark["target_at"], tolerance_s)
            quote = conn.execute(
                "SELECT id, observed_at, price, quality "
                "FROM memecoin_quote_observations_v2 "
                # probe_ok is the blind-chain good grade — without it, 4663 horizon
                # marks can never observe. Outlier/unpriced probe rows stay excluded.
                "WHERE network_id=? AND mint=? AND quality IN ('ok','probe_ok') "
                "AND observed_at>=? AND observed_at<=? "
                "AND id NOT IN (SELECT observation_id FROM memecoin_horizon_marks_v2 "
                "                   WHERE outcome_id=? AND observation_id IS NOT NULL) "
                "ORDER BY observed_at, id LIMIT 1",
                (outcome["network_id"], outcome["mint"], mark["target_at"], window_end,
                 outcome["outcome_id"]),
            ).fetchone()
            if quote:
                actual_age = _seconds_between(quote["observed_at"], outcome["signal_observed_at"])
                lateness = _seconds_between(quote["observed_at"], mark["target_at"])
                conn.execute(
                    "UPDATE memecoin_horizon_marks_v2 SET observation_id=?, observed_at=?, "
                    "actual_age_s=?, lateness_s=?, price=?, quality=?, status='observed' "
                    "WHERE id=? AND status='pending'",
                    (quote["id"], quote["observed_at"], actual_age, lateness, quote["price"],
                     quote["quality"], mark["id"]),
                )
                observed += 1
                continue
            overdue = _seconds_between(now_iso, mark["target_at"])
            if int(mark["target_s"]) == final_target and overdue >= abandon_s:
                conn.execute(
                    "UPDATE memecoin_horizon_marks_v2 SET status='abandoned' "
                    "WHERE id=? AND status='pending'", (mark["id"],),
                )
                abandoned += 1
            elif int(mark["target_s"]) != final_target and overdue > tolerance_s:
                conn.execute(
                    "UPDATE memecoin_horizon_marks_v2 SET status='missed' "
                    "WHERE id=? AND status='pending'", (mark["id"],),
                )
                missed += 1

        counts = dict(conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status='observed' THEN 1 ELSE 0 END) AS observed, "
            "SUM(CASE WHEN status IN ('missed','abandoned') THEN 1 ELSE 0 END) AS missing, "
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending "
            "FROM memecoin_horizon_marks_v2 WHERE outcome_id=?",
            (outcome["outcome_id"],),
        ).fetchone())
        terminal = int(counts["pending"] or 0) == 0
        status = "open"
        completed_at = None
        if terminal:
            status = "complete" if int(counts["observed"] or 0) == int(counts["total"] or 0) else "partial"
            completed_at = now_iso
            completed += 1
        conn.execute(
            "UPDATE memecoin_signal_outcomes_v2 SET observed_marks=?, missed_marks=?, "
            "status=?, completed_at=? WHERE outcome_id=?",
            (int(counts["observed"] or 0), int(counts["missing"] or 0), status,
             completed_at, outcome["outcome_id"]),
        )
    conn.commit()
    return HorizonResult(observed, missed, abandoned, completed)


def register_active_capture_cohorts(conn, now_iso, *, period_s=30.0):
    """Refresh active signal and position episodes before a sweep."""
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    for position in m.list_open_positions(conn):
        register_capture_cohort(
            conn, position.get("network_id") or "solana", position["mint"], "position", now_iso,
            _iso_add(now_iso, max(90.0, period_s * 3)), 400, commit=False,
        )
    horizon_tail = max(cfg.MEMECOIN_PUMP_HORIZONS_S) + cfg.MEMECOIN_PUMP_ABANDON_S
    for outcome in m.list_open_outcomes(conn):
        try:
            expires = _iso_add(outcome["buy_ts"], horizon_tail)
        except (TypeError, ValueError):
            continue
        if _seconds_between(expires, now_iso) < 0:
            continue
        register_capture_cohort(
            conn, outcome.get("network_id") or "solana", outcome["mint"], "signal", now_iso,
            expires, 100, commit=False,
        )
    for row in conn.execute(
        "SELECT network_id, mint, signal_observed_at, target_horizons_json "
        "FROM memecoin_signal_outcomes_v2 WHERE status='open'",
    ):
        try:
            targets = [int(value) for value in json.loads(row["target_horizons_json"])]
            expires = _iso_add(row["signal_observed_at"], max(targets) + cfg.MEMECOIN_PUMP_ABANDON_S)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if _seconds_between(expires, now_iso) < 0:
            continue
        register_capture_cohort(
            conn, row["network_id"], row["mint"], "signal", now_iso, expires, 100,
            commit=False,
        )
    conn.commit()


def capture_v2_pass(conn, now_iso, *, stats_batch_fn=None, max_mints=None, sweep_id=None,
                    period_s=None):
    from sniper.execution.solana_quote import dexscreener_stats_batch_keyed
    from sniper.settings import memecoin as cfg
    period_s = cfg.MEMECOIN_PUMP_CAPTURE_PERIOD_S if period_s is None else period_s
    max_mints = cfg.MEMECOIN_CAPTURE_V2_MAX_MINTS if max_mints is None else max_mints
    stats_batch_fn = stats_batch_fn or dexscreener_stats_batch_keyed
    register_active_capture_cohorts(conn, now_iso, period_s=period_s)
    conn.commit()
    result = capture_v2_sweep(
        conn, now_iso, stats_batch_fn, max_mints, sweep_id=sweep_id, period_s=period_s,
    )
    assign_due_horizons(conn, now_iso)
    return result
