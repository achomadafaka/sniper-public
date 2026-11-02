"""fwlm-v0 controls (§9 C2-C3).

Control 2 (shuffle-side): flipped direction over the SAME recorded path — close to a
pure drift test; never sufficient alone. Control 3 (trend-matched): nearest same-asset
non-whale moment from the continuously-captured market_state pool (pre-treatment-blind,
excluding candidate-adjacent moments); inherits the treatment's direction + D. Both share
far_whale_sim.simulate_path so the cost/exit model cannot diverge between treatment and
control. All table access goes through far_whale_store (no raw SQL here)."""
from __future__ import annotations

import math
import sqlite3

from sniper.hyperlive.far_whale import HORIZON_MS
from sniper.hyperlive import far_whale_store as S
from sniper.hyperlive.far_whale_sim import simulate_path

ROUND_MS = 180_000
LOOKBACK_MS = 30 * 60_000
FEATURE_KEYS = ("ret_30m_bps", "vol_30m_bps", "spread_bps", "oi_delta_30m_pct")


def features_at(conn: sqlite3.Connection, asset: str, ts_ms: int) -> dict | None:
    """30-min-lookback market features at ts, or None if the lookback is incomplete
    (so an unmatchable moment is dropped, not silently filled)."""
    rows = S.market_state_window(conn, asset, ts_ms - LOOKBACK_MS, ts_ms)
    if len(rows) < 5 or rows[0]["ts_ms"] > ts_ms - LOOKBACK_MS + 2 * ROUND_MS:
        return None
    mids = [r["mid"] for r in rows]
    rets = [(mids[i + 1] - mids[i]) / mids[i] * 1e4 for i in range(len(mids) - 1)]
    mean_r = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / max(len(rets) - 1, 1))
    oi0, oi1 = rows[0]["oi"], rows[-1]["oi"]
    oi_delta = ((oi1 - oi0) / oi0 * 100.0) if (oi0 and oi1) else 0.0
    return {
        "ret_30m_bps": (mids[-1] - mids[0]) / mids[0] * 1e4,
        "vol_30m_bps": vol,
        "spread_bps": rows[-1]["spread_bps"] or 0.0,
        "oi_delta_30m_pct": oi_delta,
        "hour_of_day": (int(ts_ms) // 3_600_000) % 24,   # local-clock hour, self-consistent
    }


def _entry_path(conn, asset: str, entry_ts_ms: int) -> list:
    return S.read_market_series(conn, asset, entry_ts_ms,
                                entry_ts_ms + HORIZON_MS + 2 * ROUND_MS)


def shuffle_control(conn, entry: dict) -> dict | None:
    """§9 C2: same recorded path, flipped direction. A real magnet must beat this — it
    isolates 'price was drifting that way anyway'."""
    flipped = "up" if entry["direction_to_liquidation"] == "down" else "down"
    out = simulate_path(flipped, entry["d_bps"],
                        _entry_path(conn, entry["asset"], entry["entry_ts_ms"]),
                        spread_bps=entry["spread_bps"] or 0.0)
    if out is None or out.get("exit_reason") == "unresolved":
        return None
    return {"control_type": "shuffle", "matched_asset": entry["asset"],
            "matched_ts_ms": entry["entry_ts_ms"], "direction_to_liquidation": flipped,
            "d_bps": entry["d_bps"], **out}


def trend_matched_control(conn, entry: dict, *, exclusion_ms: int = 3_600_000) -> dict | None:
    """§9 C3: nearest z-scored same-asset non-whale moment, ≥ exclusion_ms from ANY
    candidate signal for that asset (pre-treatment-blind), inheriting the treatment's
    direction + D. A real magnet must beat trend, not just shuffle."""
    target = features_at(conn, entry["asset"], entry["entry_ts_ms"])
    if target is None:
        return None
    cand_ts = S.candidate_signal_ts(conn, entry["asset"])
    pool_ts = S.market_state_ts(conn, entry["asset"])
    eligible = [t for t in pool_ts if all(abs(t - c) >= exclusion_ms for c in cand_ts)]
    feats = [(t, features_at(conn, entry["asset"], t)) for t in eligible]
    feats = [(t, f) for t, f in feats
             if f is not None and len(_entry_path(conn, entry["asset"], t)) >= 3]
    if not feats:
        return None
    # z-score each feature over the eligible pool, nearest Euclidean neighbor
    stats = {}
    for k in FEATURE_KEYS:
        vals = [f[k] for _, f in feats]
        mu = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mu) ** 2 for v in vals) / max(len(vals) - 1, 1)) or 1.0
        stats[k] = (mu, sd)

    def dist(f):
        d = sum(((f[k] - target[k]) / stats[k][1]) ** 2 for k in FEATURE_KEYS)
        d += 0.25 * min(abs(f["hour_of_day"] - target["hour_of_day"]),
                        24 - abs(f["hour_of_day"] - target["hour_of_day"]))
        return d

    t_best, _ = min(feats, key=lambda tf: dist(tf[1]))
    out = simulate_path(entry["direction_to_liquidation"], entry["d_bps"],
                        _entry_path(conn, entry["asset"], t_best),
                        spread_bps=entry["spread_bps"] or 0.0)
    if out is None or out.get("exit_reason") == "unresolved":
        return None
    return {"control_type": "trend", "matched_asset": entry["asset"],
            "matched_ts_ms": int(t_best),
            "direction_to_liquidation": entry["direction_to_liquidation"],
            "d_bps": entry["d_bps"], **out}


def generate_controls(conn) -> dict:
    """For every settled PRIMARY entry lacking controls, write one shuffle + one trend row.
    Idempotent via UNIQUE(entry_id, control_type). Runs only after treatment outcomes are
    fixed, but the market_state pool exists from round one — pre-treatment-blind (§9)."""
    done = S.existing_control_keys(conn)
    counts = {"shuffle": 0, "trend": 0, "unmatched": 0}
    for e in S.primary_entries_with_outcomes(conn):
        for kind, fn in (("shuffle", shuffle_control), ("trend", trend_matched_control)):
            if (e["entry_id"], kind) in done:
                continue
            ctl = fn(conn, e)
            if ctl is None:
                counts["unmatched"] += 1
                continue
            S.insert_control(conn, {"entry_id": e["entry_id"], **ctl})
            counts[kind] += 1
    return counts


# --- Verdict (§12/§13): episode-clustered bootstrap, dose-response, concentration ---------
import random  # noqa: E402

from sniper.algos.polymarket_crypto5m.window_bootstrap import boot_means, pctile  # noqa: E402
from sniper.hyperlive.far_whale import CAMPAIGN_CAP_DAYS, MAX_EVENT_SHARE  # noqa: E402

FWLM_BOOT_N = 10_000
FWLM_BOOT_SEED = 20260704
PASS_MIN_EPISODES = 50       # §12 candidate episodes
PASS_MIN_OUTCOMES = 30       # §12 completed ITT primary outcomes
PASS_MIN_WHALES = 10
PASS_MIN_ASSETS = 3


def episode_ci(by_episode: dict, *, nb: int = FWLM_BOOT_N, seed: int = FWLM_BOOT_SEED):
    """Episode-clustered bootstrap 95% CI — resamples EPISODES, never rows (spec §12).
    Reuses the repo's shared clustered-bootstrap path (boot_means/pctile)."""
    means = boot_means(by_episode, random.Random(seed), nb)
    if not means:
        return None
    grand = sum(sum(v) for v in by_episode.values()) / sum(len(v) for v in by_episode.values())
    return grand, pctile(means, 0.025), pctile(means, 0.975)


def dose_response(rows: list) -> dict:
    """Control 1: migration rate by tercile of notional/distance. exists = top > bottom."""
    scored = sorted(rows, key=lambda r: r["position_notional"] / max(r["distance_bps"], 1e-9))
    n = len(scored)
    if n < 6:
        return {"terciles": [], "exists": False, "n": n}
    k = n // 3
    tiers = [scored[:k], scored[k:n - k], scored[n - k:]]
    rates = [round(sum(r["migrated_toward_liq"] for r in t) / len(t), 3) for t in tiers]
    return {"terciles": rates, "exists": rates[2] > rates[0], "n": n}


def concentration(rows: list) -> dict:
    """Control 5 on positive pnl AND candidate counts, per whale (both <= MAX_EVENT_SHARE)."""
    pos = [r for r in rows if (r.get("paper_pnl") or 0) > 0]
    total_pos = sum(r["paper_pnl"] for r in pos)
    by_whale_pnl: dict = {}
    by_whale_n: dict = {}
    for r in rows:
        w = r["whale_address"]
        by_whale_n[w] = by_whale_n.get(w, 0) + 1
        if (r.get("paper_pnl") or 0) > 0:
            by_whale_pnl[w] = by_whale_pnl.get(w, 0.0) + r["paper_pnl"]
    pnl_share = max((v / total_pos for v in by_whale_pnl.values()), default=0.0) \
        if total_pos > 0 else 0.0
    cand_share = max((v / len(rows) for v in by_whale_n.values()), default=0.0) if rows else 0.0
    return {"max_pnl_share": round(pnl_share, 3), "max_candidate_share": round(cand_share, 3),
            "ok": pnl_share <= MAX_EVENT_SHARE and cand_share <= MAX_EVENT_SHARE}


def pnl_breakdown(rows: list) -> dict:
    """§9 Control 5 reporting: net paper pnl grouped by asset, whale, and episode (event) —
    the diagnostic breakdown behind the concentration gate (which gates on whale share)."""
    def _by(key):
        out: dict = {}
        for r in rows:
            out[r[key]] = out.get(r[key], 0.0) + (r.get("paper_pnl") or 0.0)
        return {k: round(v, 2) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}
    return {"by_asset": _by("asset"), "by_whale": _by("whale_address"),
            "by_event": _by("episode_id")}


def _verdict_status(reasons: list, ci_pnl) -> str:
    """Map the §12/§13 reason set + net-pnl CI to a terminal status. Pure so the decision
    is testable without a bar-clearing fixture. Precedence: clean pass; else a hard KILL if
    the net-pnl CI upper < 0 OR a structural control fails (trend C3, dose-response C1,
    concentration C5) — C3 failure kills regardless of C2 (§9); otherwise DECAYED
    (point-positive but not separated from zero, or only the shuffle control unmet)."""
    if not reasons:
        return "pass"
    if ci_pnl and ci_pnl[0] > 0 and ci_pnl[1] <= 0 <= ci_pnl[2] and \
            reasons == ["net_pnl_ci_not_positive"]:
        return "decayed"        # point-positive but CI spans 0 — NOT a pass (§12)
    if ci_pnl and ci_pnl[2] < 0:
        return "kill"           # CI upper < 0 — hard kill (§13)
    if ("trend_delta_ci_not_positive" in reasons
            or "no_dose_response" in reasons
            or "concentration_breach" in reasons):
        return "kill"           # structural control failure — no partial credit (§9)
    return "decayed"


def evaluate(conn, *, now_ms: int) -> dict:
    """§12/§13 verdict. status in {no_freeze, insufficient, pass, decayed, kill,
    sparsity_killed}. Control-3 (trend) failure forces kill regardless of Control-2."""
    freeze = S.load_freeze(conn)
    if freeze is None:
        return {"status": "no_freeze",
                "note": "no frozen thresholds — sweep (§4) not completed; no entries exist"}
    rows = S.primary_outcome_rows(conn)              # ITT primary set — NEVER persistence-filtered
    n_episodes_total = S.count_episodes(conn)
    n_whales = len({r["whale_address"] for r in rows})
    n_assets = len({r["asset"] for r in rows})
    counts = {"candidate_episodes": n_episodes_total, "completed_outcomes": len(rows),
              "distinct_whales": n_whales, "assets": n_assets}
    days_elapsed = (now_ms - freeze["campaign_start_ts_ms"]) / 86_400_000.0
    bar_met = (n_episodes_total >= PASS_MIN_EPISODES and len(rows) >= PASS_MIN_OUTCOMES
               and n_whales >= PASS_MIN_WHALES and n_assets >= PASS_MIN_ASSETS)
    if not bar_met:
        if days_elapsed > CAMPAIGN_CAP_DAYS:
            return {"status": "sparsity_killed", "counts": counts,
                    "days_elapsed": round(days_elapsed, 1),
                    "note": f"§12 counts unmet after CAMPAIGN_CAP_DAYS={CAMPAIGN_CAP_DAYS};"
                            " no silent extension (spec §13)"}
        return {"status": "insufficient", "counts": counts,
                "days_elapsed": round(days_elapsed, 1)}

    ctl = {r["entry_id"]: {} for r in rows}
    for c in S.all_controls(conn):
        if c["entry_id"] in ctl:
            ctl[c["entry_id"]][c["control_type"]] = c

    pnl_by_ep: dict = {}
    d_shuffle: dict = {}
    d_trend: dict = {}
    for r in rows:
        pnl_by_ep.setdefault(r["episode_id"], []).append(r["paper_pnl"])
        cs, ct = ctl[r["entry_id"]].get("shuffle"), ctl[r["entry_id"]].get("trend")
        if cs:
            d_shuffle.setdefault(r["episode_id"], []).append(
                r["migrated_toward_liq"] - cs["migrated_toward_liq"])
        if ct:
            d_trend.setdefault(r["episode_id"], []).append(
                r["migrated_toward_liq"] - ct["migrated_toward_liq"])

    ci_pnl = episode_ci(pnl_by_ep)
    ci_shuffle = episode_ci(d_shuffle)
    ci_trend = episode_ci(d_trend)
    dr = dose_response(rows)
    conc = concentration(rows)
    attrition = S.episode_status_counts(conn)
    persisted_rate = (sum(r["position_persisted"] or 0 for r in rows) / len(rows))

    reasons = []
    if ci_pnl is None or ci_pnl[1] <= 0:
        reasons.append("net_pnl_ci_not_positive")
    if ci_shuffle is None or ci_shuffle[1] <= 0:
        reasons.append("shuffle_delta_ci_not_positive")
    if ci_trend is None or ci_trend[1] <= 0:
        reasons.append("trend_delta_ci_not_positive")   # C3 fail kills regardless of C2 (§9)
    if not dr["exists"]:
        reasons.append("no_dose_response")
    if not conc["ok"]:
        reasons.append("concentration_breach")

    status = _verdict_status(reasons, ci_pnl)

    return {"status": status, "reasons": reasons, "counts": counts,
            "days_elapsed": round(days_elapsed, 1),
            "ci_net_pnl": ci_pnl, "ci_delta_shuffle": ci_shuffle, "ci_delta_trend": ci_trend,
            "dose_response": dr, "concentration": conc, "pnl_breakdown": pnl_breakdown(rows),
            "persistence_diagnostic": {
                "itt_n": len(rows), "persisted_rate": round(persisted_rate, 3),
                "episode_attrition": attrition,
                "note": "persistence-conditioned rate is a diagnostic UPPER BOUND, "
                        "not achievable live (§9 Control 4)"},
            "boot": {"n": FWLM_BOOT_N, "seed": FWLM_BOOT_SEED, "cluster": "episode"}}
