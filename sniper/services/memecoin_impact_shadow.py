"""SHADOW of the Solana impact cap: our own round trip at our own clip (2026-09-28, operator order).

The impact cap refuses on Jupiter's `priceImpactPct`, which values the output at Jupiter's reference
price, and that reference lags a coin that a source buy has just moved. On 137 attempts with both
numbers (09-21..09-28), impact 20-30% came with our own measured round trip at p50 15%, and 30.1%
came with a round trip that sold back for MORE. The candidate replacement measures what WE lose: the
exact tokens our buy quote returns, sold straight back (round trip at our clip), plus a $1 probe
bought and sold on the same pair, so the round trip splits into a fixed part and our size's part.

Nothing here decides anything. The executor refuses exactly as before and hands the refused quote to
`submit`, which never blocks: a bounded queue, one daemon worker, drop-when-full. The worker quotes on
the CAPTURE key with its own HTTP client. It never uses the funded Jupiter seam, whose process-wide
host cooldown a 429 from here would trip for the funded path (a lesson paid for on 2026-09-27). Rows
go to their own table in data/memecoin_refusal_captures.db, never the funded ledger.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
PROBE_USD = 1.0
SLIPPAGE_BPS = 5000          # quote-only: bounds the route search, never signed


def _default_http(url, headers, timeout):
    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


class ImpactShadow:
    def __init__(self, *, db_path, api_key, http=None, clock=time.time, queue_size=16,
                 timeout_s=5.0, record_fn=None):
        self._db_path, self._key = db_path, api_key
        self._http, self._clock = http or _default_http, clock
        self._timeout = float(timeout_s)
        self._record = record_fn or self.record
        self._queue = queue.Queue(maxsize=int(queue_size))
        self._thread, self._lock = None, threading.Lock()
        self.dropped = 0

    # --- the executor's side: never blocks, never raises ------------------------------------
    def submit(self, job):
        try:
            self._queue.put_nowait(dict(job))
        except queue.Full:
            self.dropped += 1
            return False
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="impact-shadow")
                self._thread.start()
        return True

    def _run(self):
        while True:
            job = self._queue.get()
            try:
                row = self.measure(job)
                if row is not None:
                    self._record(row)
            except Exception:
                logger.warning("impact shadow failed attempt=%s", job.get("attempt_id"), exc_info=True)

    # --- the measurement -----------------------------------------------------------------------
    def _quote(self, input_mint, output_mint, amount):
        url = ("%s?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d"
               % (QUOTE_URL, input_mint, output_mint, int(amount), SLIPPAGE_BPS))
        status, body = self._http(url, {"x-api-key": self._key, "User-Agent": "sniper-impact-shadow/1"},
                                  self._timeout)
        if status != 200 or not isinstance(body, dict) or not body.get("outAmount"):
            raise ValueError("no quote (HTTP %s)" % status)
        return body

    def measure(self, job):
        started = time.monotonic()
        now = self._clock()
        row = {k: job.get(k) for k in ("attempt_id", "origin_event_id", "network_id", "mint",
                                       "refused_at", "quote_observed_at", "clip_usd",
                                       "jupiter_impact_pct", "impact_cap_pct", "roundtrip_cap_pct",
                                       "buy_out_atomic", "gate", "entry_rt_cost_pct")}
        row["network_id"] = row.get("network_id") or "solana"
        row["observed_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
        try:
            refused = datetime.fromisoformat(str(job["refused_at"]).replace("Z", "+00:00"))
            row["age_s"] = now - refused.timestamp()
        except (KeyError, TypeError, ValueError):
            row["age_s"] = None
        row["hops_json"] = json.dumps(job.get("hops") or [], separators=(",", ":"))
        row["probe_usd"] = PROBE_USD
        mint, clip = job["mint"], float(job["clip_usd"])
        quality = "ok"
        # 1. Our clip's own tokens, sold straight back: the round trip at OUR size.
        try:
            sell = self._quote(mint, USDC, int(job["buy_out_atomic"]))
            row["rt_sell_usdc_atomic"] = str(sell["outAmount"])
            row["rt_cost_pct"] = (1.0 - int(sell["outAmount"]) / 1e6 / clip) * 100.0
            row["rt_sell_route_json"] = json.dumps(
                [(h.get("swapInfo") or {}).get("label") for h in sell.get("routePlan") or []])
        except Exception:
            row["rt_cost_pct"], quality = None, "rt_unavailable"
        # 2. A $1 probe bought and sold: the fixed part of the round trip (fees, spreads, legs).
        try:
            pbuy = self._quote(USDC, mint, int(PROBE_USD * 1e6))
            psell = self._quote(mint, USDC, int(pbuy["outAmount"]))
            row["probe_buy_out_atomic"] = str(pbuy["outAmount"])
            row["probe_sell_usdc_atomic"] = str(psell["outAmount"])
            row["probe_rt_cost_pct"] = (1.0 - int(psell["outAmount"]) / 1e6 / PROBE_USD) * 100.0
        except Exception:
            row["probe_rt_cost_pct"] = None
            quality = quality if quality != "ok" else "probe_unavailable"
        if row.get("rt_cost_pct") is not None and row.get("probe_rt_cost_pct") is not None:
            row["size_cost_pct"] = row["rt_cost_pct"] - row["probe_rt_cost_pct"]
        cap = job.get("roundtrip_cap_pct")
        row["would_pass_roundtrip"] = (None if row.get("rt_cost_pct") is None or cap is None
                                       else int(row["rt_cost_pct"] <= float(cap)))
        row["latency_ms"] = (time.monotonic() - started) * 1000.0
        row["quality"] = quality
        return row

    def record(self, row):
        from sniper.persistence import db_memecoin_captures as store
        conn = store.connect(self._db_path)
        try:
            store.record_impact_shadow(conn, row)
        finally:
            conn.close()


def from_env(env=None, *, db_path=None):
    """The shadow, or None: no capture key, or MEMECOIN_IMPACT_SHADOW=0 (default 1)."""
    env = os.environ if env is None else env
    key = str(env.get("MEMECOIN_CAPTURE_JUPITER_API_KEY") or "").strip()
    if not key or str(env.get("MEMECOIN_IMPACT_SHADOW", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return None
    if db_path is None:
        from sniper.util.state_paths import state_path
        db_path = state_path("data", "memecoin_refusal_captures.db")
    return ImpactShadow(db_path=str(db_path), api_key=key)
