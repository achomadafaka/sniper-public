#!/usr/bin/env python3
"""Phase 2B — btc5m-quant-shadow-v4 UNIFIED quant engine (LIVE shadow, READ-ONLY, NO ORDERS).

Multi-checkpoint snapshots (120/180/240/270s) for lead-lag; decorrelated ensemble (market + structural;
residual NOT wired pending a trained model; implied-vol & MC are diagnostics); both-books q_market with a
crossed-book guard; real per-source latency; Chainlink from the capture DB in fusion.

v4 fixes from the adversarial review:
  - klines strictly BEFORE the decision instant (no sub-second lookahead)
  - widened GRACE + explicit MISSED-checkpoint logging (slow fetches no longer silently drop windows)
  - gate: no actionable side when the ensemble is market-only (degraded) or the books are incoherent
  - gross (F0) net edge recorded alongside documented (F1) so the fee conflict can't hide an edge

Per checkpoint: snapshot both books, the Binance 1s path (structural), live cross-venue prices + DVOL +
Chainlink (fusion), build the signal panel + decorrelated ensemble + executable matrix, record it. The
actionable SHADOW prediction (no order) is recorded only at the primary 240s checkpoint; the others are
lead-lag snapshots. Settle on the real outcome. Frozen on config hash; restart-safe. NO orders/keys/prod
DB / frozen-forward-campaign contact.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.polymarket_crypto5m import market_resolver as MR        # noqa: E402
from sniper.algos.polymarket_crypto5m import persistence as P             # noqa: E402
from sniper.algos.polymarket_crypto5m import quant_ensemble as QE         # noqa: E402
from sniper.algos.polymarket_crypto5m import quant_matrix as QM           # noqa: E402
from sniper.algos.polymarket_crypto5m import quant_persistence as QPP     # noqa: E402
from sniper.algos.polymarket_crypto5m import quant_probability as QP      # noqa: E402
from sniper.algos.polymarket_crypto5m import quant_signals as QSG         # noqa: E402
from sniper.algos.polymarket_crypto5m import quant_sources as QSRC        # noqa: E402
from sniper.algos.polymarket_crypto5m import settlement as S             # noqa: E402

log = logging.getLogger("crypto5m_quant_shadow")
UA = {"User-Agent": "sniper-research/0.1"}
GAMMA = "https://gamma-api.polymarket.com/events?slug="
SLUG_PREFIX = "btc-updown-5m-"
CLOB_BOOK = "https://clob.polymarket.com/book?token_id="
KLINES = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s"
BINANCE_TIME = "https://api.binance.com/api/v3/time"
BINANCE_PRICE = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
KRAKEN = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
DERIBIT_INDEX = "https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd"
DERIBIT_DVOL = ("https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC"
                "&resolution=3600&start_timestamp={s}&end_timestamp={e}")
CAPTURE_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "crypto5m_capture.sqlite")
WINDOW = 300
SETTLE_TIMEOUT_S = 1800
GRACE_S = 35            # record a checkpoint only within this window after it opens (else book is stale);
                        # widened from 20 so slow source fetches don't silently drop busy/volatile windows

CAMPAIGN_ID = "btc5m-quant-shadow-v4"
CONFIG = {
    "campaign": CAMPAIGN_ID, "model_version": "v4", "policy_version": "v4",
    "checkpoints_s": [120, 180, 240, 270], "primary_checkpoint_s": 240, "latency_ms": 500,
    "documented_fee_rate": 0.07, "gross_notional_usdc": 10.0, "min_order_size": 5, "safety_margin": 0.01,
    "sources": ["binance_live", "coinbase", "kraken", "deribit_index", "chainlink_capture"],
    "implied_vol": "deribit_dvol", "structural": "variant_b_within_source", "mc_paths": 20000,
    "ensemble": "log_opinion_pool(market + structural); residual NOT wired yet (needs trained model "
                "on captured data); implied-vol & MC are diagnostics only",
    "residual": "not_wired_pending_trained_model",
    "q_trade": "uncertainty_shrunk_conservative", "shrink_k": 2.0,
    "gating": "no actionable side when ensemble is market-only (degraded) or books are incoherent",
    "note": "v4: fixes lookahead (klines strictly pre-decision), widened GRACE+missed logging, "
            "degraded/incoherent gating, both-books q_market w/ crossed-book guard, gross+documented "
            "net edge; shadow-only; no orders",
}


def _get(url, timeout=12):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _timed(url):
    t = time.time()
    r = _safe(lambda: _get(url))
    return r, int((time.time() - t) * 1000)


def server_now():
    # Transient HTTP errors (RemoteDisconnected, timeouts) must not crash the shadow daemon.
    # Retry a few times before giving up; callers in the hot loop additionally guard with _safe.
    last = None
    for attempt in range(3):
        try:
            return int(_get(BINANCE_TIME)["serverTime"]) // 1000
        except Exception as e:  # noqa: BLE001 — transient network/HTTP; retry before surfacing
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise last


def git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def clob_book(token_id):
    b = _safe(lambda: _get(f"{CLOB_BOOK}{token_id}"))
    if not b:
        return None
    bids = [[float(x["price"]), float(x["size"])] for x in (b.get("bids") or []) if float(x["size"]) > 0]
    asks = [[float(x["price"]), float(x["size"])] for x in (b.get("asks") or []) if float(x["size"]) > 0]
    if not bids or not asks:
        return None
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    return {"best_bid": bids[0][0], "best_ask": asks[0][0], "bids": bids, "asks": asks}


def window_klines(ws, upto_ts):
    kl = _safe(lambda: _get(f"{KLINES}&startTime={ws*1000}&endTime={upto_ts*1000}&limit=1000"))
    if not kl:
        return None
    out = {}
    for bar in kl:
        off = bar[0] // 1000 - ws
        if 0 <= off <= WINDOW:
            out[off] = float(bar[4])
    return out or None


# a Chainlink reference older than this at decision time is not decision-grade (feed gap, not a flat price)
CHAINLINK_STALE_MS = 30_000


def chainlink_from_capture(window_start, now_ts, db_path=CAPTURE_DB):
    """Latest KNOWN-AT-DECISION Chainlink BTC/USD tick for this window from the capture DB (read-only).
    Only ticks sourced at-or-before the checkpoint AND arrived (recv_ts) at-or-before it — a replayed/
    backfilled DB must never leak a later tick into an earlier checkpoint. ``stale``/``gap`` reflect the
    real age so the fusion can down-weight a stale reference instead of trusting it as fresh."""
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT value, source_ts_ms, recv_ts FROM crypto5m_chainlink_ticks "
            "WHERE window_start_ts=? AND source_ts_ms<=? AND recv_ts IS NOT NULL AND recv_ts<=? "
            "ORDER BY source_ts_ms DESC LIMIT 1",
            (window_start, now_ts * 1000, now_ts)).fetchone()
        c.close()
    except sqlite3.Error:
        return None
    if not row or not row["value"]:
        return None
    age = max(0, now_ts * 1000 - int(row["source_ts_ms"])) if row["source_ts_ms"] else CHAINLINK_STALE_MS
    return {"source": "chainlink", "price": float(row["value"]), "source_age_ms": age,
            "stale": age > CHAINLINK_STALE_MS, "gap": age > CHAINLINK_STALE_MS}


def resolve_actionable(matrix, panel):
    """Fail-closed actionable side. Blocked when the ensemble is market-only (degraded) or the books are
    NOT coherent (``False`` OR ``None`` — an invalid/one-sided book is ``None`` and must not pass). Ranked
    on the fee-EXCLUSIVE (F0 gross) net edges: the documented fee is in CONFLICT (Gamma 0.07 vs realized 0,
    see finding-polymarket-crypto5m-fee-conflict), so it is NEVER asserted as known — the gross regime is
    the explicit honest basis; the documented-fee net edges are recorded alongside for analysis."""
    if panel.get("ensemble_degraded"):
        return None, "degraded_ensemble"
    if panel.get("market_coherent") is not True:
        return None, "incoherent_books"
    cands = [(s, e) for s, e in (("Up", matrix.get("up_net_edge_gross")),
                                 ("Down", matrix.get("down_net_edge_gross"))) if e is not None and e > 0]
    if not cands:
        return None, "no_edge"
    return max(cands, key=lambda x: x[1])[0], "edge"


def fetch_sources(now_ts, window_start):
    """Live cross-venue prices with REAL per-source receive latency for fusion freshness."""
    out = []
    r, ms = _timed(BINANCE_PRICE)
    bp = float(r["price"]) if (r and r.get("price")) else None
    if bp:
        out.append({"source": "binance_live", "price": bp, "source_age_ms": ms, "stale": False, "gap": False})
    for name, url, parser in (("coinbase", COINBASE, QSRC.parse_coinbase_ticker),
                              ("kraken", KRAKEN, QSRC.parse_kraken_ticker),
                              ("deribit_index", DERIBIT_INDEX, QSRC.parse_deribit_index)):
        r, ms = _timed(url)
        px = parser(r)
        if px:
            out.append({"source": name, "price": px, "source_age_ms": ms, "stale": False, "gap": False})
    cl = chainlink_from_capture(window_start, now_ts)
    if cl:
        out.append(cl)
    return out


def fetch_implied_vol_per_s(now_ts):
    resp = _safe(lambda: _get(DERIBIT_DVOL.format(s=(now_ts - 2 * 86400) * 1000, e=now_ts * 1000)))
    return QSRC.dvol_to_vol_per_s(QSRC.parse_dvol(resp))


def _vol_per_s(closes, upto):
    seq = [closes[o] for o in range(0, upto + 1) if o in closes]
    if len(seq) < 5:
        return None
    rets = [math.log(seq[i] / seq[i - 1]) for i in range(1, len(seq)) if seq[i - 1] > 0]
    if len(rets) < 4:
        return None
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))


def _price_at(closes, off):
    for o in range(off, -1, -1):
        if o in closes:
            return closes[o]
    return None


def resolve(slug):
    ev = _safe(lambda: _get(f"{GAMMA}{slug}"))
    if not ev:
        return None, None
    spec, _ = MR.parse_event(ev[0])
    if spec is None:
        return None, None
    st = S.from_gamma(spec)
    return st.winning_outcome, st.reconciliation_status


def _record_checkpoint(conn, *, campaign_id, cid, spec, checkpoint, is_primary, now, implied_vol):
    decision_ts = spec.window_start_ts + checkpoint
    up_book, down_book = clob_book(spec.up_token_id), clob_book(spec.down_token_id)
    if not up_book or not down_book:
        return False
    # klines STRICTLY before the decision instant (endTime=decision_ts-1) — the bar opening at decision_ts
    # closes 1s later, so including it would be a sub-second lookahead.
    closes = window_klines(spec.window_start_ts, decision_ts - 1) or {}
    binance_start, binance_now = _price_at(closes, 0), _price_at(closes, checkpoint)
    realized_vol = _vol_per_s(closes, checkpoint)
    secs_rem = spec.window_end_ts - decision_ts
    sources = fetch_sources(now, spec.window_start_ts)
    q_mc = None
    if binance_now and binance_start and realized_vol and secs_rem > 0:
        mc = QP.monte_carlo_up_probability(current_price=binance_now, price_to_beat=binance_start,
                                           vol_per_s=realized_vol, seconds_remaining=secs_rem,
                                           n_paths=CONFIG["mc_paths"], seed=spec.window_start_ts % (2 ** 32))
        q_mc = mc.get("q_structural_up")

    panel = QSG.build_signal_panel(
        condition_id=cid, sources=sources, up_book=up_book, down_book=down_book,
        binance_start=binance_start, binance_now=binance_now, realized_vol_per_s=realized_vol,
        seconds_remaining=secs_rem, implied_vol_per_s=implied_vol, q_mc=q_mc,
        hour=(spec.window_start_ts // 3600) % 24)

    ens = QE.ensemble_probability(panel["ensemble_components"], shrink_k=CONFIG["shrink_k"])
    q_ens, q_trade = ens["q_ensemble"], ens["q_conservative"]
    matrix = QM.build_comparison_matrix(
        q_components=panel["components"], q_ensemble=q_ens, q_trade=q_trade, up_book=up_book,
        # fee is in CONFLICT -> never assert it is known; rank actionability on the gross edge below
        down_book=down_book, fee_rate=CONFIG["documented_fee_rate"], fee_known=False,
        latent_btc_price=panel["fusion"]["latent_btc_price"],
        source_prices={s["source"]: s["price"] for s in sources},
        gross_usdc=CONFIG["gross_notional_usdc"],
        min_order_size=(spec.min_order_size or CONFIG["min_order_size"]), safety_margin=CONFIG["safety_margin"])
    matrix["panel"] = panel
    matrix["predictive_uncertainty"] = ens["predictive_uncertainty"]
    matrix["c0_side"], matrix["c1_side"] = panel["c0_side"], panel["c1_side"]
    matrix["is_primary"] = is_primary
    matrix["n_ensemble_components"] = panel.get("n_ensemble_components")
    # gross-fee (F0) net edges alongside the documented-fee (F1) ones, so the fee conflict can't hide an edge
    if q_trade is not None:
        up_g, dn_g = matrix.get("up_executable_price"), matrix.get("down_executable_price")
        matrix["up_net_edge_gross"] = (round(q_trade - up_g - CONFIG["safety_margin"], 6) if up_g is not None else None)
        matrix["down_net_edge_gross"] = (round((1.0 - q_trade) - dn_g - CONFIG["safety_margin"], 6) if dn_g is not None else None)

    # fail-closed actionable side: block a degraded ensemble or non-coherent (False OR None) books, and
    # rank on the gross (fee-exclusive) edge since the fee is in conflict (see resolve_actionable).
    side, gate = resolve_actionable(matrix, panel)
    matrix["actionable_side"], matrix["gate_reason"] = side, gate
    matrix["actionable_fee_regime"] = "F0_gross"

    QPP.record_matrix(conn, campaign_id=campaign_id, condition_id=cid, checkpoint=checkpoint,
                      decision_ts=decision_ts, matrix=matrix, now_ts=now)
    if is_primary:
        net = matrix.get(f"{side.lower()}_net_edge_vs_executable") if side else None
        QPP.record_decision(conn, campaign_id=campaign_id, condition_id=cid, checkpoint=checkpoint,
                            decision_ts=decision_ts, actionable_side=side, q_trade=q_trade, net_edge=net,
                            status="shadow_prediction", reason=(gate or ("edge" if side else "no_edge")),
                            now_ts=now)
        log.info("[quant] %s cp=%d* q_mkt=%s q_ens=%s q_trade=%s unc=%s side=%s net=%s srcs=%d",
                 spec.slug, checkpoint, panel["q_market"], (round(q_ens, 3) if q_ens else None),
                 (round(q_trade, 3) if q_trade else None),
                 round(ens["predictive_uncertainty"], 3) if ens["predictive_uncertainty"] is not None else None,
                 side, (round(net, 4) if net is not None else None), len(sources))
    else:
        log.info("[quant] %s cp=%d snapshot q_mkt=%s struct-mkt=%s srcs=%d",
                 spec.slug, checkpoint, panel["q_market"], panel["structural_minus_market"], len(sources))
    return True


def run_shadow(conn, *, campaign_id, poll_interval=5, max_iterations=None, max_decisions=None):
    checkpoints = CONFIG["checkpoints_s"]
    primary, latency_ms = CONFIG["primary_checkpoint_s"], CONFIG["latency_ms"]
    QPP.start_campaign(conn, campaign_id=campaign_id, config=CONFIG, git_commit=git_commit(),
                       now_ts=server_now(), model_version=CONFIG["model_version"],
                       policy_version=CONFIG["policy_version"], host=os.uname().nodename)
    log.info("[quant] campaign %s started (unified v4, multi-checkpoint)", campaign_id)
    windows = {}
    for p in QPP.pending_settlements(conn, campaign_id, checkpoint=primary):
        windows[p["condition_id"]] = {"spec": None, "slug": p["slug"], "window_end_ts": p["window_end_ts"],
                                      "processed": set(checkpoints), "settled": False, "dvol": None}
    if windows:
        log.info("[quant] recovered %d pending window(s)", len(windows))
    decided, it = 0, 0
    while True:
        it += 1
        now = _safe(server_now)
        if now is None:                         # all retries failed this poll; try again next interval
            log.warning("[quant] server_now() unavailable this iteration; retrying after poll")
            time.sleep(poll_interval)
            continue
        cur = now - (now % WINDOW)
        for ws in (cur, cur + WINDOW):
            ev = _safe(lambda ws=ws: _get(f"{GAMMA}{SLUG_PREFIX}{ws}"))
            if not ev:
                continue
            spec, verdict = MR.parse_event(ev[0])
            if spec and verdict.rule_supported and spec.condition_id not in windows:
                windows[spec.condition_id] = {"spec": spec, "slug": spec.slug,
                                              "window_end_ts": spec.window_end_ts, "processed": set(),
                                              "settled": False, "dvol": None}
        for cid, stt in list(windows.items()):
            spec = stt["spec"]
            if spec is not None:
                for cp in checkpoints:
                    if cp in stt["processed"]:
                        continue
                    cp_ts = spec.window_start_ts + cp
                    if now < cp_ts + latency_ms / 1000.0:
                        continue                       # not yet
                    if now > cp_ts + GRACE_S:
                        stt["processed"].add(cp)        # missed (book would be stale) — skip + flag
                        log.warning("[quant] MISSED checkpoint %s cp=%d (now=%d > open+grace=%d)%s",
                                    spec.slug, cp, now, cp_ts + GRACE_S,
                                    "  <-- PRIMARY (lost this window's decision)" if cp == primary else "")
                        continue
                    if stt["dvol"] is None:
                        stt["dvol"] = fetch_implied_vol_per_s(now)
                    if _record_checkpoint(conn, campaign_id=campaign_id, cid=cid, spec=spec, checkpoint=cp,
                                          is_primary=(cp == primary), now=now, implied_vol=stt["dvol"]):
                        stt["processed"].add(cp)
                        if cp == primary:
                            decided += 1
            if not stt["settled"] and now >= stt["window_end_ts"] + 30:
                wo, rs = resolve(stt["slug"])
                if wo in ("Up", "Down") or now >= stt["window_end_ts"] + SETTLE_TIMEOUT_S:
                    QPP.record_settlement(conn, campaign_id=campaign_id, condition_id=cid,
                                          winning_outcome=wo, reconciliation_status=(rs or "unresolved"),
                                          now_ts=now)
                    stt["settled"] = True
                    log.info("[quant] settled %s winner=%s", stt["slug"], wo)
        windows = {k: v for k, v in windows.items() if not v["settled"]}
        if (max_iterations and it >= max_iterations) or (max_decisions and decided >= max_decisions):
            return decided
        time.sleep(poll_interval)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="crypto5m UNIFIED quant SHADOW cohort v3 (read-only, no orders)")
    ap.add_argument("--paper-db", default="data/crypto5m/btc5m_quant_shadow_v4.sqlite")
    ap.add_argument("--campaign-id", default=CAMPAIGN_ID)
    ap.add_argument("--continuous", action="store_true")
    ap.add_argument("--poll-interval", type=int, default=5)
    ap.add_argument("--max-iterations", type=int, default=None)
    ap.add_argument("--max-decisions", type=int, default=None)
    a = ap.parse_args(argv)
    os.makedirs(os.path.dirname(a.paper_db) or ".", exist_ok=True)
    conn = P.connect(a.paper_db, check_same_thread=False)
    QPP.ensure_quant_schema(conn)
    n = run_shadow(conn, campaign_id=a.campaign_id, poll_interval=a.poll_interval,
                   max_iterations=a.max_iterations, max_decisions=a.max_decisions)
    log.info("[quant] exit: %s decisions recorded", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
