#!/usr/bin/env python3
"""Phase 1D — frozen-rule live forward PAPER harness (READ-ONLY).

Per BTC 5m market: discover -> buffer Binance prices -> at the 240s checkpoint (+500ms latency) snapshot
the per-token books -> run the frozen rules C0/C1/R1/R2/R3 -> execute the selected side on its OWN ask
ladder (paper, fee-exclusive $10 gross) -> persist -> settle on the resolved Polymarket winner -> compute
fee scenarios + PnL. Restart-idempotent (decision_key). NO orders, NO keys, NO authenticated channels, NO
production DB, NO service. The decision/execution/settlement logic is the unit-tested forward_* modules.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.algos.polymarket_crypto5m import forward_harness as FH      # noqa: E402
from sniper.algos.polymarket_crypto5m import forward_persistence as FPP  # noqa: E402
from sniper.algos.polymarket_crypto5m import forward_rules as FR         # noqa: E402
from sniper.algos.polymarket_crypto5m import market_resolver as MR       # noqa: E402
from sniper.algos.polymarket_crypto5m import persistence as P            # noqa: E402
from sniper.algos.polymarket_crypto5m import settlement as S             # noqa: E402

log = logging.getLogger("crypto5m_forward")
UA = {"User-Agent": "sniper-research/0.1"}
GAMMA = "https://gamma-api.polymarket.com/events?slug="
SLUG_PREFIX = "btc-updown-5m-"
CLOB_BOOK = "https://clob.polymarket.com/book?token_id="
BINANCE_TIME = "https://api.binance.com/api/v3/time"
BINANCE_PRICE = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
SETTLE_TIMEOUT_S = 1800


def _get(url, timeout=15):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())


def server_now():
    return int(_get(BINANCE_TIME)["serverTime"]) // 1000


def binance_price():
    return float(_get(BINANCE_PRICE)["price"])


def git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def clob_book(token_id):
    """Fetch a CLOB book; compute best from the price map (REST ordering is not guaranteed). 'usable' on a
    populated two-sided book, else None."""
    try:
        b = _get(f"{CLOB_BOOK}{token_id}")
    except Exception as e:  # noqa: BLE001
        log.warning("book fetch failed %s: %s", str(token_id)[:12], e)
        return None
    bids = [[float(x["price"]), float(x["size"])] for x in (b.get("bids") or []) if float(x["size"]) > 0]
    asks = [[float(x["price"]), float(x["size"])] for x in (b.get("asks") or []) if float(x["size"]) > 0]
    if not bids or not asks:
        return None
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    return {"best_bid": bids[0][0], "best_ask": asks[0][0], "bids": bids, "asks": asks,
            "book_hash": b.get("hash"), "book_integrity_status": "usable"}


def resolve(slug):
    try:
        ev = _get(f"{GAMMA}{slug}")
    except Exception:  # noqa: BLE001
        return None, None
    if not ev:
        return None, None
    spec, _ = MR.parse_event(ev[0])
    if spec is None:
        return None, None
    st = S.from_gamma(spec)
    return st.winning_outcome, st.reconciliation_status


def _price_at(buf, target_ts):
    best = None
    for ts, px in buf:
        if ts <= target_ts:
            best = px
        else:
            break
    return best


def decision_binance_prices(buf, decision_ts, lookback_s):
    """(binance_now, binance_lookback_ago) BOTH anchored to decision_ts via _price_at — never a
    post-checkpoint sample. Prevents R1/R3 momentum from reading price movement AFTER the checkpoint
    (the buffer keeps appending live prices while we wait out the modeled latency)."""
    return _price_at(buf, decision_ts), _price_at(buf, decision_ts - lookback_s)


def run_forward(conn, *, campaign_id, poll_interval=5, max_iterations=None, max_decisions=None):
    cfg = FR.CONFIG
    checkpoint, latency_ms = cfg["primary_checkpoint_s"], cfg["latency_ms"]
    gross, min_order = cfg["gross_notional_usdc"], 5
    FPP.start_campaign(conn, campaign_id=campaign_id, manifest=FR.RULE_MANIFEST,
                       manifest_hash=FR.manifest_hash(), git_commit=git_commit(), now_ts=server_now(),
                       feature_version=FR.RULE_VERSION, host=os.uname().nodename)
    log.info("[forward] campaign %s manifest=%s started", campaign_id, FR.manifest_hash()[:12])
    buf = collections.deque(maxlen=600)
    windows = {}
    # Restart-recovery: re-track decided-but-unsettled (and previously quarantined) windows so a restart
    # does not orphan in-flight bets. They settle on the next loop once gamma reports the winner.
    for p in FPP.pending_settlements(conn, campaign_id, checkpoint=checkpoint):
        windows[p["condition_id"]] = {"spec": None, "slug": p["slug"],
                                      "window_end_ts": p["window_end_ts"], "decided": True,
                                      "settled": False}
    if windows:
        log.info("[forward] recovered %d pending window(s) for settlement", len(windows))
    decisions = 0
    it = 0
    while True:
        it += 1
        now = server_now()
        try:
            buf.append((now, binance_price()))
        except Exception:  # noqa: BLE001
            pass
        cur = now - (now % 300)
        for ws in (cur, cur + 300):
            try:
                ev = _get(f"{GAMMA}{SLUG_PREFIX}{ws}")
            except Exception:  # noqa: BLE001
                continue
            if not ev:
                continue
            spec, verdict = MR.parse_event(ev[0])
            if spec and verdict.rule_supported and spec.condition_id not in windows:
                windows[spec.condition_id] = {"spec": spec, "slug": spec.slug,
                                              "window_end_ts": spec.window_end_ts, "decided": False,
                                              "settled": False}
        for cid, stt in list(windows.items()):
            spec = stt["spec"]
            if not stt["decided"] and spec is not None:
                decision_ts = spec.window_start_ts + checkpoint
                if now >= decision_ts + latency_ms / 1000.0:
                    up_book, down_book = clob_book(spec.up_token_id), clob_book(spec.down_token_id)
                    quality = "usable" if (up_book and down_book) else "missing"
                    # known-at-decision: both momentum legs anchored to decision_ts (no post-checkpoint leak)
                    binance_now, binance_ago = decision_binance_prices(
                        buf, decision_ts, cfg["momentum_lookback_s"])
                    ctx = FH.build_context(binance_now=binance_now, binance_30s_ago=binance_ago,
                                           up_book=up_book, down_book=down_book)
                    recs = FH.decide_market(condition_id=cid, ctx=ctx, up_book=up_book, down_book=down_book,
                                            up_token=spec.up_token_id, down_token=spec.down_token_id,
                                            decision_ts=decision_ts, fill_book_ts=now,
                                            gross=gross, min_order=(spec.min_order_size or min_order),
                                            latency_ms=latency_ms, book_quality=quality)
                    FH.persist_records(conn, campaign_id=campaign_id, condition_id=cid,
                                       rule_version=FR.RULE_VERSION, checkpoint=checkpoint,
                                       decision_ts=decision_ts, records=recs, now_ts=now)
                    stt["decided"] = True
                    decisions += 1
                    entries = [r["rule_id"] for r in recs if r["status"] == "paper_entry"]
                    log.info("[forward] decided %s entries=%s", spec.slug, entries or "none")
            if stt["decided"] and not stt["settled"] and now >= stt["window_end_ts"] + 30:
                wo, rs = resolve(stt["slug"])
                if wo in ("Up", "Down") or now >= stt["window_end_ts"] + SETTLE_TIMEOUT_S:
                    FH.settle_market(conn, campaign_id=campaign_id, condition_id=cid, winning_outcome=wo,
                                     reconciliation_status=(rs or "unresolved"), now_ts=now)
                    stt["settled"] = True
                    log.info("[forward] settled %s winner=%s", stt["slug"], wo)
        windows = {k: v for k, v in windows.items() if not v["settled"]}
        if (max_iterations and it >= max_iterations) or (max_decisions and decisions >= max_decisions):
            return decisions
        time.sleep(poll_interval)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="crypto5m frozen-rule forward PAPER harness (read-only)")
    ap.add_argument("--paper-db", default="data/crypto5m/btc5m_forward_v0.sqlite")
    ap.add_argument("--campaign-id", default=FR.CAMPAIGN_ID)
    ap.add_argument("--continuous", action="store_true",
                    help="no-op: the harness always runs continuously; accepted for launcher compatibility")
    ap.add_argument("--poll-interval", type=int, default=5)
    ap.add_argument("--max-iterations", type=int, default=None, help="smoke: stop after N loop iterations")
    ap.add_argument("--max-decisions", type=int, default=None)
    a = ap.parse_args(argv)
    os.makedirs(os.path.dirname(a.paper_db) or ".", exist_ok=True)
    conn = P.connect(a.paper_db)
    FPP.ensure_forward_schema(conn)
    n = run_forward(conn, campaign_id=a.campaign_id, poll_interval=a.poll_interval,
                    max_iterations=a.max_iterations, max_decisions=a.max_decisions)
    log.info("[forward] exit: %s decisions made", n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
