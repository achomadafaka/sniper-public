"""Compute layer — rolling per-asset state from HL WS feeds (bbo/trades/activeAssetCtx) →
MarketSnapshot with every feature the setups need. Reuses the leadlag primitives."""
from __future__ import annotations

import math
import time
from collections import defaultdict, deque

from sniper.hyperlive.models import MarketSnapshot
from sniper.research.leadlag.measurement_audit import microprice, ofi_step
from sniper.research.leadlag.event_reactor import classify_trade, detect_dislocation

WINDOW_MS = 320_000


class RollingState:
    def __init__(self):
        self.bbo = defaultdict(lambda: deque())     # asset -> (ts, bid, ask, bsz, asz)
        self.trades = defaultdict(lambda: deque())  # asset -> (ts, notional, is_liq, liq_side)
        self.ctx = defaultdict(dict)                # asset -> latest ctx dict
        self.ctx_hist = defaultdict(lambda: deque())  # asset -> (ts, oi)
        self.cluster = {}                            # asset -> (side, distance_bps) from cluster_map lane
        self.l2_depth = {}                           # asset -> latest top-5 depth notional (USDC)
        self.depth_hist = defaultdict(lambda: deque())  # asset -> (ts, depth) baseline for collapse detect
        self.binance = {}                            # asset -> binance mid (cross-venue lead)

    def _trim(self, dq, now):
        while dq and dq[0][0] < now - WINDOW_MS:
            dq.popleft()

    def on_bbo(self, asset, ts, bid, ask, bsz, asz):
        d = self.bbo[asset]; d.append((ts, bid, ask, bsz, asz)); self._trim(d, ts)

    def on_trade(self, asset, ts, price, size, side, raw):
        is_liq, liq_side = classify_trade(raw)
        d = self.trades[asset]; d.append((ts, price * size, is_liq, liq_side)); self._trim(d, ts)

    def on_ctx(self, asset, ts, ctx):
        self.ctx[asset] = ctx
        try:
            oi = float(ctx.get("openInterest"))
            h = self.ctx_hist[asset]; h.append((ts, oi)); self._trim(h, ts)
        except (TypeError, ValueError):
            pass

    def on_l2book(self, asset, ts, bids, asks):
        # bids/asks: lists of (px, sz). Top-5 notional depth per side -> thin-book detector.
        depth = sum(px * sz for px, sz in (bids or [])[:5]) + sum(px * sz for px, sz in (asks or [])[:5])
        if depth <= 0:
            return
        self.l2_depth[asset] = depth
        h = self.depth_hist[asset]; h.append((ts, depth)); self._trim(h, ts)

    def _mid_at(self, asset, ts):
        best = None
        for t, b, a, *_ in self.bbo[asset]:
            if t <= ts:
                best = (b + a) / 2
            else:
                break
        return best

    def snapshot(self, asset: str, ts_ms: int) -> MarketSnapshot:
        d = self.bbo[asset]
        if not d:
            return MarketSnapshot(asset, ts_ms, 0, 0, 0, 0, {}, "no_book")
        _, bid, ask, bsz, asz = d[-1]
        mid = (bid + ask) / 2
        spread_bps = (ask - bid) / mid * 1e4 if mid else 0
        # vol + returns (bps)
        mids = [(t, (b + a) / 2) for t, b, a, *_ in d]
        rets = [math.log(m2 / m1) for (_, m1), (_, m2) in zip(mids, mids[1:]) if m1 > 0 and m2 > 0]
        recent = rets[-60:]
        vol_bps = (math.sqrt(sum(r * r for r in recent) / len(recent)) * 1e4) if recent else 0.0
        m60, m300 = self._mid_at(asset, ts_ms - 60_000), self._mid_at(asset, ts_ms - 300_000)
        ret60 = (mid - m60) / m60 * 1e4 if m60 else 0.0
        ret300 = (mid - m300) / m300 * 1e4 if m300 else 0.0
        # OFI over last 5 bbo
        snaps = [(b, bs, a, az) for _, b, a, bs, az in list(d)[-6:]]
        ofi = sum(x for x in (ofi_step(snaps[i - 1], snaps[i]) for i in range(1, len(snaps))) if x)
        # liquidations + dislocation
        liqs = [t for t in self.trades[asset] if t[2]]
        liq_rate = len([t for t in liqs if t[0] >= ts_ms - 60_000])
        event_side = liqs[-1][3] if liqs and liqs[-1][0] >= ts_ms - 30_000 else None
        disl = detect_dislocation(ret60, vol_bps)
        if event_side is None and disl is not None:
            event_side = disl.side
        event_strength = abs(ret60)
        # positioning
        cx = self.ctx[asset]
        funding = _f(cx.get("funding")); basis = _basis(cx)
        oi_d = self._oi_delta(asset, ts_ms)
        # cluster lane
        cl = self.cluster.get(asset)
        # thin-book / depth-collapse detector
        depth_total = self.l2_depth.get(asset, 0.0)
        dh = sorted(x[1] for x in self.depth_hist[asset])
        depth_base = dh[len(dh) // 2] if dh else 0.0          # rolling median baseline
        depth_ratio = (depth_total / depth_base) if depth_base > 0 else 1.0
        # cross-venue lead: binance mid vs HL mid (HL is a follower venue)
        b_mid = self.binance.get(asset)
        b_lead_bps = (b_mid - mid) / mid * 1e4 if (b_mid and mid) else 0.0
        b_side = ("long" if b_lead_bps > 0 else "short") if b_mid else None
        # OI-price confirmation: +1 = OI confirms the move (fueled), -1 = diverges (covering)
        oi_confirm = (1 if (ret60 > 0) == (oi_d > 0) else -1) if (ret60 and oi_d) else 0
        # funding-settlement proximity (HL funds hourly; local clock skew is ~whole-hour so
        # the minute-within-hour ≈ real time)
        mins_to_funding = round((3600 - (time.time() % 3600)) / 60.0, 1)
        feats = {
            "vol_bps": round(vol_bps, 2), "ret60_bps": round(ret60, 2), "ret300_bps": round(ret300, 2),
            "ofi": round(ofi, 2), "liq_rate": liq_rate, "oi_delta": round(oi_d, 5),
            "funding": funding, "basis": basis,
            "event_side": event_side, "event_strength_bps": round(event_strength, 1),
            "cluster_side": cl[0] if cl else None, "cluster_distance_bps": cl[1] if cl else 0.0,
            "binance_lead_side": b_side, "binance_lead_bps": round(b_lead_bps, 2), "catalyst": None,
            "depth_total": round(depth_total, 0), "depth_ratio": round(depth_ratio, 3),
            "oi_confirm": oi_confirm, "mins_to_funding": mins_to_funding,
            "microprice_dev_bps": round((microprice(bid, ask, bsz, asz) - mid) / mid * 1e4, 2) if mid and microprice(bid, ask, bsz, asz) else 0.0,
        }
        return MarketSnapshot(asset, ts_ms, bid, ask, mid, round(spread_bps, 3), feats, "ok")

    def _oi_delta(self, asset, ts):
        h = self.ctx_hist[asset]
        if len(h) < 2:
            return 0.0
        now = h[-1][1]; past = None
        for t, oi in h:
            if t <= ts - 300_000:
                past = oi
        if past is None or past == 0:
            past = h[0][1]
        return (now - past) / past if past else 0.0


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _basis(cx):
    mark, orac = _f(cx.get("markPx")), _f(cx.get("oraclePx"))
    return (mark - orac) / orac if (mark is not None and orac) else None
