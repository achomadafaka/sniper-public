"""Immutable pre-gate entry snapshot: computed at decision time from a CopySignal + market context,
written for EVERY eligible signal (including gate-rejected ones). Feature promoted JSON->column only
after a dedup-corrected study; JSON is the lab, columns are the shelf."""
from __future__ import annotations
import hashlib, json, math


def build_snapshot(sig, *, mcap, liquidity, token_age_s, consensus_n_60m,
                   wallet_trust_tier, regime_id, entry_latency, decided_at,
                   extra_features=None):
    promoted = {
        "upstream_event_id": sig.source_tx, "chain": sig.chain_id, "mint": sig.token_address,
        "mcap": mcap, "liquidity": liquidity, "token_age_s": token_age_s,
        "consensus_n_60m": consensus_n_60m, "wallet_trust_tier": wallet_trust_tier,
        "their_size_usd": sig.notional_estimate, "regime_id": regime_id,
    }
    features = {
        **_json_safe(extra_features or {}),
        **promoted,
        "source_entry_price": sig.source_entry_price,
        "confirmation_state": sig.confirmation_state,
    }
    features_json = json.dumps(features, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        **promoted,
        "decided_at": decided_at,
        "features_json": features_json, "feature_version": 1,
        "data_quality_json": json.dumps({"mcap": mcap is not None, "liquidity": liquidity is not None}),
        "entry_latency_json": json.dumps(entry_latency or {}),
        "snapshot_hash": hashlib.sha1(features_json.encode()).hexdigest(),
    }


def _json_safe(value):
    """Preserve unknown as null and reject JSON-invalid NaN/Infinity without losing a snapshot."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def trader_profile(conn, source_user_id):
    """Point-in-time descriptive strategy facts from the local analytics row.

    The label is a capture feature, not a trading verdict. Raw metrics remain beside the coarse,
    versioned label so offline work can ignore or replace it.
    """
    if not source_user_id:
        return None
    row = conn.execute(
        "SELECT computed_at, data_as_of, median_hold_s, pct_held_under_60s, pct_held_over_1d, "
        "avg_buy_usd, size_cv, re_entry_rate FROM memecoin_trader_analytics "
        "WHERE source_user_id=?",
        (source_user_id,),
    ).fetchone()
    if row is None:
        return None
    vals = dict(row)
    med = vals.get("median_hold_s")
    under = vals.get("pct_held_under_60s")
    over = vals.get("pct_held_over_1d")
    if over is not None and float(over) >= 0.5:
        archetype = "bagholder"
    elif ((under is not None and float(under) >= 0.5)
          or (med is not None and float(med) <= 300.0)):
        archetype = "scalper"
    elif med is not None and float(med) >= 3600.0:
        archetype = "swing"
    else:
        archetype = "mixed"
    return {"version": 1, "archetype": archetype, **vals}


def consensus_n_60m(conn, sig, observed_at):
    """Prior distinct curated buyers captured on our real-UTC observation clock.

    If the current event has no v2 capture row, coverage is unknown and the feature stays null.
    This avoids turning an absent capture lane into a known zero-consensus claim.
    """
    current = conn.execute(
        "SELECT 1 FROM memecoin_feed_observations_v2 "
        "WHERE upstream_event_id=? AND clock_domain='observed_real_utc'",
        (sig.source_tx,),
    ).fetchone()
    if current is None:
        return None
    row = conn.execute(
        "SELECT COUNT(DISTINCT source_user_id) FROM memecoin_feed_observations_v2 "
        "WHERE normalized_side='buy' AND was_curated=1 AND clock_domain='observed_real_utc' "
        "AND network_id=? AND mint=? AND upstream_event_id<>? AND source_user_id<>? "
        "AND strftime('%s', observed_at) < strftime('%s', ?) "
        "AND strftime('%s', ?) - strftime('%s', observed_at) BETWEEN 0 AND 3600",
        (sig.chain_id, sig.token_address, sig.source_tx, sig.source_wallet,
         observed_at, observed_at),
    ).fetchone()
    return int(row[0]) if row is not None else None


def write_snapshot(conn, snapshot: dict, action: str) -> int:
    """Idempotent on upstream_event_id — the first write (immutable) wins. Never raises on a
    duplicate signal being re-observed."""
    cols = ["upstream_event_id", "episode_id", "chain", "mint", "action", "decided_at", "mcap",
            "liquidity", "token_age_s", "consensus_n_60m", "wallet_trust_tier", "their_size_usd",
            "regime_id", "features_json", "feature_version", "data_quality_json",
            "entry_latency_json", "snapshot_hash"]
    row = {**snapshot, "action": action, "episode_id": snapshot.get("episode_id")}
    placeholders = ",".join("?" * len(cols))
    conn.execute(f"INSERT OR IGNORE INTO memecoin_entry_snapshots({','.join(cols)}) "
                 f"VALUES({placeholders})", [row.get(c) for c in cols])
    conn.commit()
    saved = conn.execute(
        "SELECT snapshot_id FROM memecoin_entry_snapshots WHERE upstream_event_id=?",
        (row["upstream_event_id"],),
    ).fetchone()
    if saved is None:
        raise RuntimeError("entry snapshot insert did not produce a row")
    return int(saved[0])
