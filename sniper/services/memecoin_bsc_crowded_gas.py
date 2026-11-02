"""Crowded gas bid on the 56 fast lane (operator YES 2026-10-16 via Alpha, a live test).

WHY. Gamma's read of the lane's 18 sends (10-09..10-16): 11 of 13 reverts were fillable at the front of
S+1, where the copiers that crossed our floor paid 6-21 gwei against our 0.05. On BSC there is no base
fee, so gasPrice is the priority fee: the bid is that one number, nothing else in the signed bytes moves.

THE RULE ("early" on 56). The watcher pairs each source with the newHeads entry of its own block S on
the same socket; the datagram carries S's header milliTimestamp (`source_block_ms`) and when that head
reached us (`source_head_at`). A send is early when, at arm time (after the build, before the wallet
lock), host wall ms - source_block_ms <= MEMECOIN_BSC_FAST_LANE_CROWDED_EARLY_MS: our bytes can still
make S+1's cutoff. No pairing, or a pairing whose head arrived before its own block or more than
HEAD_SKEW_MAX_MS after it (host clock skew, a stale socket), is NOT early: the static bid, fail closed.

THE ARMS. Treated (the bid) or holdout (the static price), by memecoin_crowded_fee.holdout(attempt_id),
the slot lane's quarter rule, so one grader reads both tests. Every other send is "not eligible" with
its reason, decided before the arm is drawn so both arms see one population; the daily cap is one of
those reasons, so both arms stop together.

THE MONEY. Scope: BscLive's own private-only sends (V2 native, cap-floor; never public endpoints, never
v3_shared/flap or another chain). gasPrice = max(static, min(target, ceiling)); min_out, clip, route,
member set, gas limit, nonce and identity are untouched. A treated send counts its bound (gas limit x
price) when its bytes are signed, then its receipt's fee (gasUsed x effectiveGasPrice) replaces the
bound; the day's count is seeded from today's ledger traces at lane start. At the cap the day pages
once and both arms bid static until UTC midnight. A treated send needs the snapshot float to cover
clip + gas at the bid (float_for_bid): a node's "insufficient funds" reads ambiguous and holds the
reservation. One object per lane, its own lock: lane handles may overlap before the wallet lock.
"""
from __future__ import annotations

import json
import logging
import threading

from sniper.services.memecoin_crowded_fee import holdout

logger = logging.getLogger(__name__)

RULE_VERSION = "bsc-crowded-early-sha256-q1-v1"
SUBSTAGE = "bsc_crowded_gas"            # every in-scope decision: arm or reason, signed price, bound
SUBSTAGE_PAID = "bsc_crowded_gas_paid"  # a treated send's receipt fee, replacing its bound
HEAD_SKEW_MAX_MS = 2000


class BscCrowdedGas:
    def __init__(self, *, now_iso_fn, alert_fn=None):
        if alert_fn is None:
            from sniper.services.memecoin_alerts import send_alert as alert_fn
        self.now_iso_fn, self.alert_fn = now_iso_fn, alert_fn
        self._lock = threading.Lock()
        self.day, self.spent, self.paged, self._bounds = None, 0, False, {}
        self.status = dict(rule_version=RULE_VERSION, target_wei=None, ceiling_wei=None, early_ms=None,
                           day=None, spent_wei=0, daily_cap_wei=None, capped=False, treated=0, holdout=0,
                           sent_treated=0, not_eligible={}, bound_at=None)

    def _roll(self, now_iso):
        day = str(now_iso)[:10]
        if day != self.day:
            self.day, self.spent, self.paged, self._bounds = day, 0, False, {}
            self.status.update(day=day, spent_wei=0, capped=False, bound_at=None)

    def seed(self, conn):
        """Today's treated sends already out, at their receipt fee where it was recorded, else their
        bound: a restart must not reset the cap. Lane start only."""
        from sniper.persistence import db_memecoin as m
        with self._lock:
            self._roll(self.now_iso_fn())
            try:
                for trace_json in m.bsc_fast_lane_traces_since(conn, self.day + "T00:00:00+00:00"):
                    events = json.loads(trace_json or "{}").get("substage_events") or ()
                    armed = next((e for e in events if e.get("name") == SUBSTAGE
                                  and e.get("arm") == "treated"), None)
                    if armed is None:
                        continue
                    paid = next((e for e in events if e.get("name") == SUBSTAGE_PAID), None)
                    wei = (paid or {}).get("fee_wei")
                    if type(wei) is not int or wei < 0:
                        wei = armed.get("fee_bound_wei")
                    if type(wei) is int and wei > 0:
                        self.spent += wei
            except Exception:
                logger.exception("[memecoin] bsc crowded gas: seed from the ledger failed; counting from 0")
            self.status["spent_wei"] = self.spent
            return self.spent

    def arm(self, attempt_id, *, private_only, source_block_ms, source_head_at, now_ms, static_wei,
            gas_limit, float_ok_fn):
        """{'arm': 'treated'|'holdout'|None, 'reason', 'gas_price_wei' (what to sign), ...}."""
        from sniper.settings import memecoin as cfg
        target, ceiling = cfg.MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_WEI, cfg.MEMECOIN_BSC_FAST_LANE_CROWDED_GAS_MAX_WEI
        cap, early = cfg.MEMECOIN_BSC_FAST_LANE_CROWDED_DAILY_WEI, cfg.MEMECOIN_BSC_FAST_LANE_CROWDED_EARLY_MS
        bid = None if target is None or ceiling is None else max(int(static_wei), min(target, ceiling))
        early_ms = skew_ms = None
        if type(source_block_ms) is int and source_block_ms > 0:
            early_ms = float(now_ms) - source_block_ms
            if isinstance(source_head_at, (int, float)) and not isinstance(source_head_at, bool):
                skew_ms = float(source_head_at) * 1000.0 - source_block_ms
        reason = None
        if not target or ceiling is None or cap is None or early is None:
            reason = "off"
        elif not private_only:
            reason = "not_private"
        elif not attempt_id:
            reason = "no_attempt_id"
        elif (early_ms is None or skew_ms is None or early_ms < 0
              or not 0 <= skew_ms <= HEAD_SKEW_MAX_MS):
            reason = "head_unknown"
        elif early_ms > early:
            reason = "late"
        elif bid <= int(static_wei):
            reason = "target_below_static"
        elif not float_ok_fn(bid):
            reason = "float_for_bid"
        with self._lock:
            self._roll(self.now_iso_fn())
            self.status.update(target_wei=target, ceiling_wei=ceiling, early_ms=early, daily_cap_wei=cap)
            if reason is None and self.spent >= cap:
                reason = "daily_cap"
            out = dict(rule_version=RULE_VERSION, early_ms=early_ms, head_skew_ms=skew_ms,
                       source_block_ms=source_block_ms, target_wei=target, ceiling_wei=ceiling,
                       static_wei=int(static_wei), arm=None, gas_price_wei=int(static_wei))
            if reason is not None:
                self.status["not_eligible"][reason] = self.status["not_eligible"].get(reason, 0) + 1
                return dict(out, reason=reason, fee_bound_wei=int(gas_limit) * int(static_wei))
            arm = "holdout" if holdout(attempt_id) else "treated"
            self.status[arm] += 1
            price = bid if arm == "treated" else int(static_wei)
        return dict(out, arm=arm, reason="eligible", gas_price_wei=price,
                    fee_bound_wei=int(gas_limit) * price)

    def sent(self, tx_hash, bound_wei):
        """A treated send's bytes were signed: count its bound now (failed or not, it may land)."""
        if type(bound_wei) is not int or bound_wei <= 0 or not tx_hash:
            return
        with self._lock:
            self._roll(self.now_iso_fn())
            self._bounds[str(tx_hash).lower()] = bound_wei
            self.spent += bound_wei
            self.status.update(spent_wei=self.spent, sent_treated=self.status["sent_treated"] + 1)
            page = self._capped_locked()
        self._page(page)

    def paid(self, tx_hash, fee_wei):
        """That send's receipt: its fee replaces its bound (same UTC day only)."""
        if type(fee_wei) is not int or fee_wei < 0:
            return
        with self._lock:
            bound = self._bounds.pop(str(tx_hash).lower(), None)
            if bound is None:
                return
            self.spent += fee_wei - bound
            self.status["spent_wei"] = self.spent

    def _capped_locked(self):
        from sniper.settings import memecoin as cfg
        cap = cfg.MEMECOIN_BSC_FAST_LANE_CROWDED_DAILY_WEI
        if cap is None or self.spent < cap or self.paged:
            return None
        self.paged = True
        self.status.update(capped=True, bound_at=self.now_iso_fn())
        return ("56 crowded gas bid: today's cap reached, %.4f BNB over %d treated sends (bounds until "
                "their receipts book); static bids until UTC midnight. In memory, re-seeded from the "
                "ledger on restart." % (self.spent / 1e18, self.status["sent_treated"]))

    def _page(self, text):
        if text is None:
            return
        logger.warning("[memecoin] %s", text)
        try:
            self.alert_fn("BSC CROWDED GAS CAP", text)
        except Exception:
            logger.exception("[memecoin] bsc crowded gas page failed")
