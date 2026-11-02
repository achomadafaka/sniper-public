"""Consensus entry signals — surface fresh setups, never auto-buy.

The strongest documented entry signal is CONSENSUS: >=2 tracked/armed wallets buying the same coin in a
tight window hit-20% 37.5% vs 10% for a single buyer. BUT our own backtest showed a naive "wait for
consensus then enter" trigger LOST 20/20 — the edge is a post-entry conviction/sizing signal and an
operator-surfacing aid, NOT an auto-trigger. So this ranks live consensus setups (freshness x wallet
count x liquidity x mcap headroom) for a human decision; it does not place orders.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_W_ARMED, _W_FRESH, _W_LIQ, _W_MCAP = 0.40, 0.25, 0.20, 0.15
_NEG_EVIDENCE_MIN = 3          # min alpha_b closes before we call a wallet a proven copy LOSER (caution)


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _log_band(x, lo, hi):
    if not x or x <= 0:
        return 0.0
    return _clamp((math.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * 100.0)


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _setup_score(armed, fresh_min, liq, mcap, window_min):
    armed_s = _clamp(min(armed, 5) / 5.0 * 100.0)
    fresh_s = _clamp((window_min - (fresh_min or window_min)) / window_min * 100.0)
    liq_s = _log_band(liq, 3_000.0, 80_000.0)                      # need real depth to enter+exit
    mcap_s = 50.0 if not mcap else _clamp(100.0 - (math.log10(mcap) - 6.0)
                                          / (math.log10(30_000_000.0) - 6.0) * 100.0)
    return round(_W_ARMED * armed_s + _W_FRESH * fresh_s + _W_LIQ * liq_s + _W_MCAP * mcap_s, 1)


def consensus_setups(conn, *, window_min=90, min_armed=2, cluster_min=60, held_keys=None, quote_map=None,
                     limit=15, now_iso=None, quote_fill_fn=None):
    """Coins with >= min_armed distinct armed BUYS in the last `window_min`, NOT already held, ranked by
    setup_score. `quote_map` = {(net,mint): {liquidity, market_cap, volume_24h}} (from the radar cache).
    Returns rows with {network_id, mint, ticker, armed, armed_60m, last_buy_min, liquidity, market_cap,
    volume_24h, setup_score, hot}.

    `armed` counts the full `window_min`; `armed_60m` counts only the tight `cluster_min` sub-window.
    `hot` requires the CLUSTER (>= 2 armed within cluster_min), not just 2 buys anywhere in the window —
    the validated edge is "≥2 armed in 60min → hit-20 37.5% vs 10%", so one buy 85m ago plus one 5m ago
    is NOT a consensus cluster even though it's 2 armed over 90m.

    Each armed buyer is graded by its HONEST copy-PnL (closed alpha_b paper positions = their entry +
    OUR exit, fomo-excluded — the same metric arming uses): `armed_proven` clears the promote gate
    (>=MIN_CLOSES closes AND >=MIN_PNL cum copy-PnL), `armed_negative` is a proven copy LOSER. Two
    proven wallets in a cluster is a very different signal from two negative-EV wallets (the alert-reality
    audit found 11/40 armed wallets are honest-negative), so setup_score rewards proven / penalizes
    negative. Still surfacing-only — it never auto-buys."""
    from sniper.util.clock import real_utc_now_iso
    from sniper.settings import memecoin as cfg
    now_iso = now_iso or real_utc_now_iso()
    now_dt = _parse(now_iso) or datetime.now(timezone.utc)
    cut = (now_dt - timedelta(minutes=float(window_min))).isoformat()
    cluster_cut = (now_dt - timedelta(minutes=float(min(cluster_min, window_min)))).isoformat()
    min_closes = int(getattr(cfg, "MEMECOIN_REAL_PROMOTE_MIN_CLOSES", 5))
    min_pnl = float(getattr(cfg, "MEMECOIN_REAL_PROMOTE_MIN_PNL_USD", 25.0))
    held_keys = held_keys or set()
    quote_map = dict(quote_map or {})   # copy: the fill below must not mutate the caller's cache
    rows = conn.execute(
        # cp = honest per-wallet copy-PnL (closed alpha_b PAPER positions, fomo-priced fills excluded).
        "WITH cp AS ("
        "  SELECT source_user_id, COUNT(*) n_closes, COALESCE(SUM(realized_pnl),0) copy_pnl "
        "  FROM memecoin_positions "
        "  WHERE strategy='alpha_b' AND is_open=0 AND COALESCE(execution_mode,'paper')!='real' "
        "  AND id NOT IN (SELECT position_id FROM memecoin_fills WHERE fill_source='fomo') "
        "  GROUP BY source_user_id) "
        "SELECT fo.network_id, fo.mint, MAX(NULLIF(fo.ticker,'')) tk, "
        "  COUNT(DISTINCT fo.source_user_id) armed, "
        "  COUNT(DISTINCT CASE WHEN fo.observed_at >= ? THEN fo.source_user_id END) armed_cluster, "
        "  COUNT(DISTINCT CASE WHEN fo.observed_at >= ? AND cp.n_closes >= ? AND cp.copy_pnl >= ? "
        "                      THEN fo.source_user_id END) armed_proven, "
        "  COUNT(DISTINCT CASE WHEN fo.observed_at >= ? AND cp.n_closes >= ? AND cp.copy_pnl < 0 "
        "                      THEN fo.source_user_id END) armed_negative, "
        "  MAX(fo.observed_at) last_buy "
        "FROM memecoin_feed_observations_v2 fo "
        "JOIN memecoin_wallets w ON w.source_user_id = fo.source_user_id "
        "LEFT JOIN cp ON cp.source_user_id = fo.source_user_id "
        "WHERE fo.normalized_side='buy' AND fo.observed_at >= ? AND w.real_approved=1 "
        "AND COALESCE(w.paused,0)=0 AND COALESCE(w.excluded,0)=0 "
        "GROUP BY fo.network_id, fo.mint HAVING armed >= ? "
        "ORDER BY armed_proven DESC, armed_cluster DESC, armed DESC, last_buy DESC",
        (cluster_cut, cluster_cut, min_closes, min_pnl, cluster_cut, _NEG_EVIDENCE_MIN,
         cut, int(min_armed))).fetchall()
    from sniper.persistence.db_memecoin import accounting_mark_key
    # Normalize held-set + quote key to the SAME canonical key (EVM lowercased, network canonicalized).
    # Held-filtering previously compared raw (net,mint): a held EVM/4663 position with a checksummed
    # mint vs the lowercase feed row missed the filter and re-surfaced as a fresh "HOT" setup — a
    # real-money double-buy path via the operator's manual /real/buy.
    held_norm = {accounting_mark_key(n, mm) for (n, mm) in held_keys}
    # CANDIDATE LIQUIDITY. `hot` below requires liquidity >= 5,000, and `liq` can only come from
    # quote_map. The radar quote daemon that filled that cache has been stopped since 2026-08-21, so
    # every candidate read liquidity=None, hot was pinned False for all of them, the badge went dark
    # and stuck-bags could never name a blocked setup. `quote_fill_fn` prices the SURVIVING
    # candidates -- at most `limit` coins, already past the armed threshold and the held filter --
    # from one bounded live batch. It is optional and fail-open: no fn, or a failed fetch, leaves
    # liquidity unknown and `hot` False, exactly as before.
    if quote_fill_fn is not None:
        want = []
        for r in rows:
            mk = accounting_mark_key(r["network_id"], r["mint"])
            if mk in held_norm or (quote_map.get(mk) or {}).get("liquidity"):
                continue
            want.append((r["network_id"], r["mint"]))
        if want:
            try:
                for mk, q in (quote_fill_fn(want) or {}).items():
                    if q:
                        quote_map[mk] = {**(quote_map.get(mk) or {}), **q}
            except Exception:
                logger.exception("consensus setups: candidate quote fill failed (liquidity stays unknown)")
    out = []
    for r in rows:
        mk = accounting_mark_key(r["network_id"], r["mint"])
        if mk in held_norm:
            continue
        q = quote_map.get(mk) or {}
        last = _parse(r["last_buy"])
        fresh_min = (now_dt - last).total_seconds() / 60.0 if last else None
        liq, mcap = q.get("liquidity"), q.get("market_cap")
        armed_cluster = r["armed_cluster"]
        proven, negative = r["armed_proven"], r["armed_negative"]
        base = _setup_score(r["armed"], fresh_min, liq, mcap, window_min)
        # quality adjustment (surfacing aid): reward proven copiers, penalize proven losers. Modest +/-
        # so it re-ranks within a tier without overriding the validated freshness/liquidity base.
        score = round(_clamp(base + min(12.0, proven * 6.0) - min(16.0, negative * 8.0)), 1)
        quality = ("proven" if proven and not negative else "mixed" if proven and negative
                   else "caution" if negative else "unproven")
        # hot requires the tight CLUSTER (>=2 armed within cluster_min), not just 2 buys over the window.
        hot = bool(armed_cluster >= 2 and (fresh_min is not None and fresh_min <= 30)
                   and (liq or 0) >= 5_000 and (mcap is None or mcap <= 5_000_000))
        out.append({"network_id": r["network_id"], "mint": r["mint"], "ticker": r["tk"],
                    "armed": r["armed"], "armed_60m": armed_cluster,
                    "armed_proven": proven, "armed_negative": negative, "quality": quality,
                    "last_buy_min": round(fresh_min) if fresh_min is not None else None,
                    "liquidity": liq, "market_cap": mcap, "volume_24h": q.get("volume_24h"),
                    "setup_score": score, "hot": hot})
    out.sort(key=lambda x: (x["hot"], x["armed_proven"] > 0, x["setup_score"]), reverse=True)
    return out[:int(limit)]
