"""Lifecycle manager for Hyperliquid watch wallets.

The screen finds candidates; this module decides which watched wallets stay active,
which are culled, and which paper-copy tier is executable. It never places orders.
"""
from __future__ import annotations

import logging
import math
from collections import Counter

from sniper.hyperlive import intel_store as IS

logger = logging.getLogger(__name__)


WATCH_STALE_MS = 7 * 86_400_000
FOLLOW_ACTIVITY_STALE_MS = 14 * 86_400_000
MATURE_CULL_MIN_CLOSES = 30
EARLY_CULL_MIN_CLOSES = 10
EARLY_CULL_MAX_MEAN_BPS = -25.0


def _open_follow_wallets(conn) -> set[str]:
    return {r["wallet"] for r in conn.execute(
        "SELECT DISTINCT wallet FROM hl_follow_positions WHERE status='open'").fetchall()}


def _latest_screened(conn) -> dict[str, dict]:
    return {r["wallet"]: dict(r) for r in conn.execute("SELECT * FROM hl_screened_traders").fetchall()}


def _is_archetype_pass(row: dict) -> bool:
    return row.get("archetype_pass") in (1, True) or row.get("verdict") == "copyable"


EVIDENCE_TIEBREAK_CAP = 50.0     # max upward nudge from positive forward evidence
EVIDENCE_PENALTY_SCALE = 1_000.0  # negative forward evidence strongly demotes


def _evidence_adjust(n: int, mean: float) -> float:
    """Forward-follow evidence as an asymmetric tiebreak. A positive result nudges up but is capped
    (`EVIDENCE_TIEBREAK_CAP`) so it can only break ties, never override a real quality gap. A
    negative result strongly demotes — we don't rank a wallet that loses money when copied. Zero
    closes carry no signal."""
    if n <= 0:
        return 0.0
    if mean > 0.0:
        return min(mean, EVIDENCE_TIEBREAK_CAP)
    return mean * EVIDENCE_PENALTY_SCALE


def _score(row: dict, follow: dict | None, copy: dict | None) -> float:
    """Quality-led ranking (2026-07-09). Realized PnL leads, then PF / month ROI / win rate;
    forward-follow evidence is only a bounded tiebreak (`_evidence_adjust`). Incumbency no longer
    buys a slot — the best wallet ranks first whether or not it is already being copied."""
    f = follow or {}
    c = copy or {}
    realized = float(row.get("realized", 0.0) or 0.0)
    if math.isnan(realized):        # malformed stored PnL must not make sort order nondeterministic
        realized = 0.0
    win_rate = float(row.get("win_rate", 0.0) or 0.0)
    pf = min(float(row.get("profit_factor", 0.0) or 0.0), 50.0)
    month_roi = float(row.get("month_roi", 0.0) or 0.0)
    quality = realized + win_rate * 100.0 + pf * 2.0 + month_roi
    tiebreak = (_evidence_adjust(int(c.get("n_closed", 0) or 0), float(c.get("mean_pnl_bps", 0.0) or 0.0))
                + _evidence_adjust(int(f.get("n_closed", 0) or 0), float(f.get("mean_net_bps", 0.0) or 0.0)))
    return quality + tiebreak


def _cull_reason(row: dict, latest: dict | None, follow: dict | None, copy: dict | None, *,
                 now_ms: int, open_follows: set[str]) -> str | None:
    wallet = row["wallet"]
    if latest and not _is_archetype_pass(latest):
        latest_ts = int(latest.get("ts_ms") or 0)
        last_seen = int(row.get("last_seen_ts_ms") or row.get("updated_ts_ms") or 0)
        if latest_ts >= last_seen:
            verdict = IS.display_verdict(latest.get("verdict")) or "unknown"
            reason = latest.get("reason") or "not archetype-pass"
            return f"latest screen rejected: {verdict} ({reason})"

    f = follow or {}
    n_closed = int(f.get("n_closed", 0) or 0)
    mean = float(f.get("mean_net_bps", 0.0) or 0.0)
    if n_closed >= MATURE_CULL_MIN_CLOSES and mean <= 0.0:
        return f"{n_closed} closed follows mean {mean:+.2f}bps <= 0"
    if n_closed >= EARLY_CULL_MIN_CLOSES and mean <= EARLY_CULL_MAX_MEAN_BPS:
        return f"{n_closed} closed follows mean {mean:+.2f}bps <= {EARLY_CULL_MAX_MEAN_BPS:g}"

    copy_disqualified = IS.copy_disqualification_reason(copy)
    if copy_disqualified:
        return copy_disqualified

    # Archetype-pass wallets are NOT culled for staleness alone (2026-07-09). The screen re-screens
    # tracked wallets every run (screen --refresh-tracked), so a stale gap is a screen-coverage
    # artifact, not evidence the wallet is bad. They are removed only by a re-screen that downgrades
    # the verdict (handled above) or by negative forward evidence (handled above). Staleness only
    # culls wallets that are no longer archetype-pass.
    effective_ap = _is_archetype_pass(latest) if latest is not None else _is_archetype_pass(row)
    last_seen = int(row.get("last_seen_ts_ms") or row.get("updated_ts_ms") or row.get("added_ts_ms") or 0)
    last_close = int((follow or {}).get("last_close_ts", 0) or 0)
    recent_follow_close = last_close and now_ms - last_close <= FOLLOW_ACTIVITY_STALE_MS
    if (not effective_ap and last_seen and now_ms - last_seen > WATCH_STALE_MS
            and not recent_follow_close and wallet not in open_follows):
        return "stale watch wallet (>7d since archetype-pass screen)"
    return None


def maintain_watchlist(conn, *, now_ms: int, max_watch_targets: int | None = None) -> dict:
    """Cull bad watch wallets and activate the current paper-copy target tier.

    `max_watch_targets=None` (default, 2026-07-09) means no cap — every eligible archetype-pass
    wallet is followed ("if it's good, it's good"). Pass a positive int to re-impose a ceiling."""
    IS.ensure_schema(conn)
    fstats = IS.follow_stats_by_wallet(conn)
    cstats = IS.copy_stats_by_wallet(conn)
    latest_by_wallet = _latest_screened(conn)
    open_follows = _open_follow_wallets(conn)
    counts = Counter()
    proven, active_watch = [], []

    for row in IS.get_tracked(conn, include_culled=True):
        wallet = row["wallet"]
        latest = latest_by_wallet.get(wallet)
        follow = fstats.get(wallet)
        copy = cstats.get(wallet)
        reason = _cull_reason(row, latest, follow, copy, now_ms=now_ms,
                              open_follows=open_follows)
        if reason:
            IS.set_tracked_lifecycle(conn, wallet, status="culled", reason=reason, ts_ms=now_ms)
            IS.set_copy_target(conn, wallet, active=False,
                               copy_tier=row.get("copy_tier") or "watch_fallback",
                               reason=reason, ts_ms=now_ms)
            counts["culled"] += 1
            continue

        status = IS.copy_target_status(conn, wallet, now_ms=now_ms, follow_stats=fstats,
                                       copy_stats=cstats, copy_tier="proven")
        if status["effective_verdict"] == "proven":
            IS.set_tracked_lifecycle(conn, wallet, status="active", reason=status["reason"], ts_ms=now_ms)
            proven.append((row, follow, _score(row, follow, None)))
            counts["proven"] += 1
        elif _is_archetype_pass(row):
            IS.set_tracked_lifecycle(conn, wallet, status="active", reason="active watch", ts_ms=now_ms)
            active_watch.append((row, follow, _score(row, follow, copy)))
            counts["active_watch"] += 1
        else:
            reason = row.get("lifecycle_reason") or "not archetype-pass"
            IS.set_tracked_lifecycle(conn, wallet, status="culled", reason=reason, ts_ms=now_ms)
            IS.set_copy_target(conn, wallet, active=False, copy_tier="watch_fallback", reason=reason, ts_ms=now_ms)
            counts["culled"] += 1

    selected: dict[str, str] = {}
    if proven:
        ranked = sorted(proven, key=lambda item: item[2], reverse=True)[:max_watch_targets]
        for idx, (row, _follow, _score_val) in enumerate(ranked, start=1):
            selected[row["wallet"]] = "proven"
            IS.set_copy_target(conn, row["wallet"], active=True, copy_tier="proven",
                               reason=f"proven rank {idx}", ts_ms=now_ms)
        if active_watch:
            logger.warning(
                "proven wallet(s) present — disabling %d archetype-pass fallback target(s); "
                "follow list collapses to %d proven%s. (The phase-2 proven gate is scoped out of "
                "the no-cap expansion; revisit if proven becomes reachable.)",
                len(active_watch), len(ranked),
                " despite no-cap" if max_watch_targets is None else "")
        for row, _follow, _score_val in active_watch:
            IS.set_copy_target(conn, row["wallet"], active=False, copy_tier="watch_fallback",
                               reason="proven wallet exists; fallback disabled", ts_ms=now_ms)
    else:
        ranked = sorted(active_watch, key=lambda item: item[2], reverse=True)[:max_watch_targets]
        for idx, (row, _follow, _score_val) in enumerate(ranked, start=1):
            selected[row["wallet"]] = "watch_fallback"
            IS.set_copy_target(conn, row["wallet"], active=True, copy_tier="watch_fallback",
                               reason=f"fallback basket rank {idx}; no proven wallets", ts_ms=now_ms)
        selected_wallets = set(selected)
        for row, _follow, _score_val in active_watch:
            if row["wallet"] not in selected_wallets:
                IS.set_copy_target(conn, row["wallet"], active=False, copy_tier="watch_fallback",
                                   reason="outside fallback basket cap", ts_ms=now_ms)

    for target in IS.get_copy_targets(conn, active_only=True):
        if target["wallet"] not in selected:
            IS.set_copy_target(conn, target["wallet"], active=False,
                               copy_tier=target.get("copy_tier") or "proven",
                               reason="not selected by lifecycle", ts_ms=now_ms)

    return {
        "active_watch": counts["active_watch"],
        "proven": counts["proven"],
        "culled": counts["culled"],
        "selected_targets": selected,
    }
