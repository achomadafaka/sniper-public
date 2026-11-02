"""Re-send a slot-lane entry's IDENTICAL signed bytes through the TPU sidecar (2026-09-28, Alpha GO).

Measured 2026-09-28 on 17 slot-lane sends: PumpSwap landed S+1 on 4/10 (curve 6/7) at the same fee,
CU limit and size, and in every slot we missed cheaper untipped transactions on the same mint landed.
The transaction was not in that leader's buffer: the keyless sidecar delivers exactly once, and an
unstaked stream is the first one throttled under load.

The first send is untouched. This runs on its own daemon thread and re-submits the SAME request (same
signed bytes, so the same signature and blockhash: the network processes it at most once and a
re-send can never fill twice) at each offset after the first send, unless the lane has already
recorded the outcome (`stop`) or the bytes no longer hash to the bound
identity. A busy provider defers that scheduled resend, rechecking every 5 ms until strictly
before the next configured offset; the final offset gets no grace. Other failures are recorded and
skipped; nothing here raises into the lane, and `stop` never waits for a send in flight.

Head-only entry requests retain fresh local head checks. Explicit legacy caps never rebase to
dispatch. These checks gate new submissions, never already-sent landing.
"""
from __future__ import annotations

import threading
import time

from sniper.execution.submission_transport import sha256_hex, _entry_request_slot_refusal

class SlotResender:
    def __init__(self, submit_fn, request_fn, *, expected_sha256, offsets_ms, source_slot,
                 head_fn=None, clock=time.monotonic, dispatch_slot=None):
        self._submit, self._request_fn = submit_fn, request_fn
        self._expected = str(expected_sha256)
        self._offsets = tuple(float(o) for o in offsets_ms)[:2]
        self._source = source_slot
        # Retain the observed dispatch slot for diagnostics; fixed offsets bound uncapped resends.
        self._window_slot = max(int(source_slot), int(dispatch_slot)) if (
            source_slot is not None and dispatch_slot is not None) else source_slot
        self._head_fn, self._clock = head_fn, clock
        self._stop = threading.Event()
        self._sends, self._in_flight = [], [0]
        self._thread = None

    def start(self, t0):
        self._thread = threading.Thread(target=self._run, args=(float(t0),), daemon=True,
                                        name="slot-lane-resend")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def report(self):
        sends = [dict(s) for s in self._sends]
        return {"sent": sum(1 for s in sends if s["state"] == "accepted"), "sends": sends,
                "in_flight": self._in_flight[0], "window_start_slot": self._window_slot}

    def _head(self):
        try:
            head = self._head_fn() if self._head_fn is not None else None
            return int(head) if head is not None else None
        except Exception:
            return None

    def _run(self, t0):
        for index, offset in enumerate(self._offsets):
            if self._stop.wait(max(0.0, t0 + offset / 1000.0 - self._clock())):
                return
            next_at = (t0 + self._offsets[index + 1] / 1000.0
                       if index + 1 < len(self._offsets) else None)
            while True:
                record = {"offset_ms": offset, "at_ms": (self._clock() - t0) * 1000.0,
                          "head_slot": self._head()}
                try:
                    request = self._request_fn()
                    if (request is None or sha256_hex(request.signed_payload) != self._expected
                            or request.signed_payload_sha256 != self._expected):
                        self._sends.append({**record, "state": "identity_mismatch"})
                        return
                    guarded = (request.chain_id == "solana" and request.action == "entry"
                               and (request.entry_max_slot is not None or request.entry_head_fn is not None))
                    if guarded:
                        self._window_slot = self._source  # The original source cap cannot rebase to dispatch.
                        refused = _entry_request_slot_refusal(request, clock=self._clock)
                    else:
                        refused = None
                    if refused:
                        self._sends.append({**record, "state": "window_ended", "reason": refused})
                        return
                    self._in_flight[0] += 1
                    attempt = self._submit(request)
                    self._in_flight[0] -= 1
                except Exception as exc:                  # a resend must never reach the lane
                    self._in_flight[0] = max(0, self._in_flight[0] - 1)
                    self._sends.append({**record, "state": "error", "reason": type(exc).__name__})
                    break
                if attempt is None:
                    self._sends.append({**record, "state": "no_provider"})
                    return
                state = str(getattr(attempt.state, "value", attempt.state))
                if state == "not_sent" and attempt.reason_code == "provider_busy" and next_at is not None:
                    remaining = next_at - self._clock()
                    if remaining > 0:
                        if self._stop.wait(min(.005, remaining)):
                            return
                        if self._clock() < next_at:
                            continue
                self._sends.append({**record, "state": state, "reason": attempt.reason_code,
                                    "leaders": attempt.response_code, "latency_ms": attempt.latency_ms})
                break
