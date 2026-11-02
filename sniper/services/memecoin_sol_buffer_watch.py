"""Visibility for the Solana native-SOL execution buffer (Alpha, 2026-10-21).

The buffer worker's quiet refusals ('insufficient_free_usdc', 'buffer_cap', 'wallet_busy') are not logged,
so on 2026-10-19 the buffer stopped refilling at the USDC floor and native-SOL entries ended without a
trace. BufferWatch keeps the worker's last state, free USDC and native SOL value (from the reads the
worker already makes, no new RPC), publishes them in the heartbeat, and pages once when native SOL has
stayed under one clip without a refill for MEMECOIN_SOL_BUFFER_STALL_PAGE_S, and once on recovery. The
paged state is kept on disk, so a runner restart never re-pages (trap b6c22073). Off the fill path: it
only reads the worker's tally.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

REFILL_STATES = ("submitted", "confirmed", "confirmed_balance_pending", "unknown", "prior_confirmed")


class BufferWatch:
    def __init__(self, state_path, *, below_usd, stall_s, clip_fn=None, alert=None, clock=time.time):
        # below_usd > 0 is a fixed override; otherwise clip_fn() (the slot lane's current clip), the
        # amount prefer_sol's sol_funds() needs, so 'short' is exactly when entries switch to USDC (Gamma)
        self.state_path, self.below_usd, self.stall_s = Path(state_path), float(below_usd or 0), float(stall_s)
        self.clip_fn = clip_fn
        self.alert, self.clock, self.lock = alert, clock, threading.Lock()
        self._status = {"state": None, "at": None, "last_refill_at": None, "free_usdc": None,
                        "native_usd": None, "short_since": None, "threshold_usd": None, "paged": self._paged()}

    def threshold(self):
        if self.below_usd > 0:
            return self.below_usd
        try:
            clip = self.clip_fn() if self.clip_fn else None
            return float(clip) if clip else None
        except (TypeError, ValueError):
            return None

    def _paged(self):
        try:
            return bool(json.loads(self.state_path.read_text()).get("paged"))
        except (OSError, ValueError, AttributeError):
            return False

    def _save(self, paged):
        try:
            tmp = Path(str(self.state_path) + ".tmp")
            tmp.write_text(json.dumps({"paged": paged, "at": self.clock()}))
            tmp.replace(self.state_path)
        except OSError:
            pass                                       # a restart may re-page once; never raise

    def _page(self, text):
        alert = self.alert
        if alert is None:
            from sniper.services.memecoin_alerts import send_alert
            alert = send_alert
        alert("SOL-BUFFER", text)

    def observe(self, tally):
        """One worker pass. Returns the page text sent, or None. Never raises."""
        try:
            now = self.clock()
            tally = tally or {}
            with self.lock:
                s = dict(self._status)
                s.update(state=tally.get("state"), at=now)
                for key in ("free_usdc", "native_usd"):
                    if tally.get(key) is not None:
                        s[key] = round(float(tally[key]), 2)
                refilled = tally.get("state") in REFILL_STATES
                if refilled:
                    s["last_refill_at"] = now
                # short = native SOL known and under one clip, with no refill this pass. An unknown native
                # value or an unknown clip is not short.
                below = self.threshold()
                s["threshold_usd"] = below
                short = (not refilled and below is not None and tally.get("native_usd") is not None
                         and float(tally["native_usd"]) < below)
                text = None
                if below is None and not refilled:
                    self._status = s                   # clip unknown: no decision either way
                    return None
                s["short_since"] = (s["short_since"] or now) if short else None
                if short and not s["paged"] and now - s["short_since"] >= self.stall_s:
                    text = ("SOL execution buffer cannot refill: native SOL $%.2f < one clip ($%.0f) for %.0f min, "
                            "last refusal %s, free USDC %s. Native-SOL entries are off; entries fall back to USDC."
                            % (float(tally["native_usd"]), below, (now - s["short_since"]) / 60,
                               tally.get("state"), s["free_usdc"]))
                    s["paged"] = True
                elif not short and s["paged"] and tally.get("native_usd") is not None:
                    text = "SOL execution buffer recovered: native SOL $%.2f, state %s" % (
                        float(tally["native_usd"]), tally.get("state"))
                    s["paged"] = False
                self._status = s
            if text is not None:
                self._save(s["paged"])
                self._page(text)
            return text
        except Exception:
            return None

    def status(self):
        with self.lock:
            s = dict(self._status)
        if s["short_since"] is not None:
            s["short_for_s"] = round(self.clock() - s["short_since"], 1)
        return s
