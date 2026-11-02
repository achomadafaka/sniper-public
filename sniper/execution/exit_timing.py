"""Stage timing for a real exit, so sell speed is a measured number instead of a guess.

`execute_exit` receives ONE frozen `now_iso` and reuses it for the claim, the pre-broadcast
write-ahead and the finalization. Every exit timestamp is therefore the same instant, and
`closed_at - close_submitted_at` read 0.00s on all five funded closes on 2026-08-13 — the column
pair is structurally incapable of measuring anything. Entries have `memecoin_entry_attempts` stage
traces; exits had nothing.

This measures with a MONOTONIC clock and never writes a wall-clock timestamp, so it cannot be
mistaken for, or drift against, the ledger's `now_iso` facts. Nothing here changes an existing
column: it only adds a record of how long each stage took.

The split that matters is the same one entries taught us: quote/sign/submit happen BEFORE the sell
is broadcast and are the latency that costs money, while confirm happens after the transaction is
already on-chain and costs nothing but patience.
"""
from __future__ import annotations

import json
import time

_PRE_BROADCAST = ("quote", "approve", "sign", "submit")


class ExitTiming:
    """Accumulates per-stage durations for one exit attempt. Never raises into the exit path."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        # Guarded like every other read here. An unguarded first tick raised straight into the exit
        # path, which is the one thing this module must never do — a sell is money, the timing is
        # not. A timer that cannot read its clock reports nothing instead of failing the sell.
        self._start = self._read()
        self._mark = self._start
        self.stages: dict[str, float] = {}
        # SUB-MARKS (2026-09-28, Alpha GO "Step 0"): where the time INSIDE a stage went -- the
        # balance reads, the quote call, the /swap build. Each is timed by its own caller, so a
        # sub-mark never moves a stage boundary and the stage numbers keep their meaning. They
        # accumulate across rungs like the stages do.
        self.subs: dict[str, float] = {}
        self.attempts = 0
        # E10 (2026-09-29): the V4 discovery ranking behind the quote this exit sold on, and its pool.
        self.v4 = {}
        # The pump_native curve leg the exit sold on (2026-10-07, native-exit stranding map).
        self.native = {}

    def _read(self):
        try:
            return self._clock()
        except Exception:
            return None

    def stage(self, name):
        """Close the stage `name` at the current instant and start the next one."""
        try:
            now = self._read()
            if now is None or self._mark is None:
                return self
            ms = (now - self._mark) * 1000.0
            self._mark = now
            # A ladder retries quote/sign/submit, so stages ACCUMULATE rather than overwrite —
            # reporting only the last rung would hide the cost of the retries entirely.
            self.stages[name] = self.stages.get(name, 0.0) + ms
        except Exception:
            pass
        return self

    def sub(self, name, ms):
        """Add `ms` to the sub-mark `name`. Never raises; a None or non-numeric value is skipped."""
        try:
            if ms is not None:
                self.subs[name] = self.subs.get(name, 0.0) + float(ms)
        except Exception:
            pass
        return self

    def note_quote(self, quote):
        """Keep the E10 ranking the quote carries (`_sniper_v4_ranking`) and the V4 pool it was won
        on. The last quote noted is the one the exit signed. Never raises."""
        try:
            ranking = quote.get("_sniper_v4_ranking") if isinstance(quote, dict) else None
            route = (quote.get("route") if isinstance(quote, dict) else None) or {}
            self.v4 = {} if ranking is None else {
                "v4_ranking": ranking,
                "won_pool": route.get("pool_id") if route.get("protocol") == "uniswap_v4" else None}
        except Exception:
            self.v4 = {}
        return self

    def note_native_leg(self, quote):
        """The curve leg of a native pump exit (pump_native_exit.build_native_exit): expected and
        minimum quote-asset out, its slippage, and the tail's expected and minimum USDC, atomic
        amounts as strings. Actual curve out = min_out + the exit's memecoin_quote_residue
        kind='exit' row. Any other venue, or a leg that does not parse, leaves nothing. Never
        raises."""
        try:
            leg = quote.get("_curve_leg") if isinstance(quote, dict) and quote.get("_venue") == "pump_native" else None
            self.native = {} if not isinstance(leg, dict) else {"native_leg": {
                "quote_mint": leg.get("quote_mint"), "curve": leg.get("curve"),
                "expected_out_atomic": str(int(leg["expected_out"])),
                "min_out_atomic": str(int(leg["min_out"])), "slippage_bps": quote.get("slippageBps"),
                "tail_out_atomic": str(int(quote["outAmount"])),
                "tail_min_atomic": str(int(quote["otherAmountThreshold"]))}}
        except Exception:
            self.native = {}
        return self

    def rung(self):
        self.attempts += 1
        return self

    def total_ms(self):
        now = self._read()
        if now is None or self._start is None:
            return None
        return (now - self._start) * 1000.0

    def pre_broadcast_ms(self):
        """The part that costs money: everything up to and including submission."""
        return sum(v for k, v in self.stages.items() if k in _PRE_BROADCAST)

    def to_json(self):
        try:
            total = self.total_ms()
            if total is None:
                return None
            return json.dumps({
                "stages_ms": {k: round(v, 2) for k, v in self.stages.items()},
                "pre_broadcast_ms": round(self.pre_broadcast_ms(), 2),
                "total_ms": round(total, 2),
                "ladder_rungs": self.attempts,
                **({"sub_ms": {k: round(v, 2) for k, v in self.subs.items()}} if self.subs else {}),
                **self.v4,
                **self.native,
            }, sort_keys=True, separators=(",", ":"))
        except Exception:
            return None
