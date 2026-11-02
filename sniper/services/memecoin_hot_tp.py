"""Exit-rail wake for the funded fastlane (hot TP lane 2026-08-22; every rail 2026-09-27).

Spec: docs/superpowers/specs/2026-08-22-memecoin-hot-tp-lane-design.md

The funded rails are decided by the fastlane sweep on a 1 s tick. This module does NOT add exit
logic. It adds a wake: when a Solana mark for an open real position says a rail has probably been
crossed, the fastlane worker runs a FOCUS pass for that position immediately instead of waiting for
the tick. The pass is the same audited path (exact preview -> rails -> claim -> exit intent ->
executor), and the exact whole-balance quote still decides.

How a mark is read (2026-09-27, operator order "funded exits must see price changes at
sub-second speed"). After every sweep, a mint whose persisted exact quote is new is re-anchored:
the anchor is that exact unit price E and the latest mark quote_per_token q0. A later mark q
estimates the executable price as E x q/q0, against the anchor of the mark's OWN source. That is a ratio inside one pool's own quote
unit, so there is no USD conversion and no spot-versus-executable gap to guess (the anchor carries
the gap). The rail levels are passed in by the fastlane from the sweep's own helpers, as ratios to
E: a mark wakes when the estimate crosses one of them, or when it moved `big_move_pct` since the
anchor, which covers every rail this module cannot see (market-cap targets, a peak that ratcheted).

Wakes are coalesced per mint (`min_interval_s`) and capped globally (`max_per_minute`), because a
wake costs one exact quote and one provider 429 shuts every exact quote for its cooldown. A mint
with no anchor yet never wakes: that is the tick, exactly as before.

`tp_target_price` is still written once at open (db_memecoin.open_position, via tp_target_from);
the wake does not read it, it reads the sweep's own thresholds.
"""
from __future__ import annotations

import threading
import time


def tp_target_from(entry_price, tp_pct):
    """USD target, or None when either input is missing or not positive."""
    try:
        entry, pct = float(entry_price), float(tp_pct)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or pct <= 0:
        return None
    return entry * (1.0 + pct / 100.0)


class HotTpTrigger:
    """Thread-safe per-mint rail anchors + wake event.

    `rearm()` runs on the fastlane thread after each sweep. `on_mark()` runs on the swap-stream
    thread for every stored mark and must stay cheap: one lock, dict reads, one divide, compares,
    no DB, no network, no logging. Wake records are drained and printed by the fastlane thread.
    """

    def __init__(self, *, min_interval_s=0.25, big_move_pct=5.0, max_per_minute=60.0,
                 shadow=False, clock=None, wall=None):
        from sniper.services.memecoin_quote_demand import QuoteBudget
        self.min_interval_s = float(min_interval_s)
        self.big_move = float(big_move_pct) / 100.0
        self.shadow = bool(shadow)
        self._clock = clock or time.monotonic
        self._wall = wall or time.time
        self._budget = QuoteBudget(per_minute=float(max_per_minute), clock=self._clock)
        self._lock = threading.Lock()
        self._rails = {}          # mint -> {"q0", "down", "up", "ids", "stamp", "last_wake"}
        self._last_q = {}         # mint -> {mark source -> latest quote_per_token}
        self._woken = set()       # position ids waiting for a focus pass
        self._records = []        # wake records for the fastlane thread to print
        self.wake = threading.Event()
        self.wakes = 0
        self.passes = 0
        self.suppressed = 0
        self.last_wake_ts = None

    def rearm(self, rails, *, keep):
        """Replace the anchors, on the fastlane thread after a sweep.

        rails: {mint: (down_ratio | None, up_ratio | None, position_ids, stamp)}, ratios to the
        position's last exact unit price E. `stamp` names that quote; while it is unchanged the
        mark anchor q0 is kept, and when it moves q0 becomes the latest mark (None until one
        arrives, and the first mark then becomes q0 without waking).
        keep: mints still held. Anchors and remembered marks for any other mint are dropped.
        """
        keep = {str(m) for m in keep} | {str(m) for m in rails}
        with self._lock:
            old_rails, self._rails = self._rails, {}
            self._last_q = {m: q for m, q in self._last_q.items() if m in keep}
            for mint, (down, up, ids, stamp) in rails.items():
                mint = str(mint)
                old = old_rails.get(mint) or {}
                q0 = (old.get("q0") if old.get("stamp") == stamp
                      else dict(self._last_q.get(mint) or {}))
                self._rails[mint] = {"q0": q0, "down": down, "up": up, "ids": frozenset(ids),
                                     "stamp": stamp, "last_wake": old.get("last_wake")}

    def on_mark(self, mark):
        """Return the wake reason when this mark woke (or, in shadow, would wake) a focus pass."""
        mint = getattr(mark, "mint", None)
        try:
            q = float(getattr(mark, "quote_per_token", None))
        except (TypeError, ValueError):
            return None
        if mint is None or not q > 0:
            return None
        mint = str(mint)
        now = self._clock()
        with self._lock:
            # ONE ANCHOR PER MARK SOURCE (2026-09-27, first live data): pool-state marks and swap
            # marks sit 4-10% apart on a thin coin (a swap carries its own impact), so comparing a
            # state mark with a swap anchor read every source switch as a big move. 81 wakes and 52
            # focus passes in 47 s on one position; 28 of the wakes were a switch of source.
            source = str(getattr(mark, "source", None) or "")
            self._last_q.setdefault(mint, {})[source] = q
            st = self._rails.get(mint)
            if st is None:
                return None
            q0 = st["q0"].get(source)
            if q0 is None:
                st["q0"][source] = q
                return None
            ratio = q / q0
            if st["down"] is not None and ratio <= st["down"]:
                reason = "down_rail"
            elif st["up"] is not None and ratio >= st["up"]:
                reason = "up_rail"
            elif abs(ratio - 1.0) >= self.big_move:
                reason = "big_move"
            else:
                return None
            if st["last_wake"] is not None and now - st["last_wake"] < self.min_interval_s:
                self.suppressed += 1
                return None
            if not self._budget.take():
                self.suppressed += 1
                return None
            st["last_wake"] = now
            self.wakes += 1
            self.last_wake_ts = self._wall()
            if len(self._records) < 256:
                self._records.append({
                    "mint": mint, "reason": reason, "ratio": ratio, "ids": sorted(st["ids"]),
                    "slot": getattr(mark, "slot", None), "source": getattr(mark, "source", None),
                    "mark_ts": getattr(mark, "observed_ts", None), "woke_mono": now,
                    "woke_ts": self.last_wake_ts, "shadow": self.shadow})
            if not self.shadow:
                self._woken |= st["ids"]
        if not self.shadow:
            self.wake.set()
        return reason

    def wait(self, timeout_s):
        """Block until a wake or the timeout. Returns True when woken by a mark."""
        woke = self.wake.wait(timeout_s)
        if woke:
            self.wake.clear()
        return woke

    def take_woken(self):
        """Position ids to run a focus pass for, cleared."""
        with self._lock:
            ids, self._woken = self._woken, set()
        if ids:
            self.passes += 1
        return ids

    def take_records(self):
        with self._lock:
            records, self._records = self._records, []
        return records

    def snapshot(self):
        return {"wakes": self.wakes, "passes": self.passes, "suppressed": self.suppressed,
                "last_wake_ts": self.last_wake_ts, "anchors": len(self._rails),
                "shadow": self.shadow}
