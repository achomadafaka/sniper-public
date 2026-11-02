"""Cross-process wake for the on-chain lane (operator 2026-09-27: remove detection->execution delay).

The confirmed watcher is a separate process that commits detections to onchain_wallet_detections;
the runner's on-chain lane found them by polling every MEMECOIN_ONCHAIN_LANE_POLL_S (1 s). Measured
on 468 Solana first attempts 09-25..09-27: commit -> attempt start p50 0.427 s, p90 0.860 s, the
shape of that poll. Now the watcher sends ONE datagram after its commit, and the lane waits in
select() on an abstract AF_UNIX datagram socket, so it sweeps at once. The poll stays: it is the
recovery for a lost datagram, a watcher that predates this, and a runner that could not bind.

THE DATAGRAM IS A HINT, NEVER DATA. Its payload is ignored and the sweep reads the ledger, so any
sender (an abstract name carries no file permissions) can only cause an idempotent cursor sweep,
rate-limited to one per MIN_SWEEP_INTERVAL_S. A lost datagram costs at most one poll.

The name is derived from the state root, as the live-marks snapshot is, so a test or another
checkout never wakes production. An abstract socket vanishes with its process, so nothing goes
stale on disk. During a deploy overlap the old runner still holds the name: the new lane polls and
retries the bind on every wait until the old process lets go.
"""
from __future__ import annotations

import errno
import hashlib
import select
import socket
import threading
import time

MIN_SWEEP_INTERVAL_S = 0.02
_PREFIX = "\0sniper-onchain-wake-"


def wake_address(root):
    """The lane's abstract socket name for this state root."""
    return _PREFIX + hashlib.sha256(str(root).encode()).hexdigest()[:12]


def default_address():
    from sniper.util.state_paths import state_root
    return wake_address(state_root())


def send_wake(address):
    """One datagram to the lane. Never raises; True when the kernel accepted it. Nothing bound
    (ECONNREFUSED), a full receive queue (EAGAIN: a wake is already pending) and anything else are
    all False: the lane's poll covers every one of them."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(b"w", socket.MSG_DONTWAIT, address)
        return True
    except Exception:  # noqa: BLE001 - a wake must never fail its caller
        return False


class LaneWake:
    """The on-chain lane's wait, with the threading.Event interface its callers already use
    (set / wait / clear / is_set). Owned and waited on by the lane thread only; `set` may be called
    from any thread (the in-process routed and slot-lane handoffs) and sends a datagram to itself,
    so it also wakes a wait that is blocked in select()."""

    def __init__(self, address):
        self.address = address
        self._sock = None
        self._event = threading.Event()
        self._last_wake = None
        self.stats = {"bound": False, "datagrams": 0, "bind_failures": 0, "last_error": None}

    def bind(self):
        if self._sock is not None:
            return True
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.bind(self.address)
        except OSError as exc:
            s.close()
            self.stats["bind_failures"] += 1
            self.stats["last_error"] = errno.errorcode.get(exc.errno, type(exc).__name__)
            return False
        s.setblocking(False)
        self._sock = s
        self.stats.update(bound=True, last_error=None)
        return True

    def close(self):
        if self._sock is not None:
            self._sock.close()
            self._sock = None
            self.stats["bound"] = False

    def set(self):
        self._event.set()
        if self._sock is not None:
            send_wake(self.address)

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout):
        """True when woken (a datagram or `set`) before `timeout`, else False. Never raises."""
        if self._sock is None and not self.bind():
            return self._event.wait(timeout)
        woke = self._event.is_set()
        if not woke:
            try:
                ready, _w, _x = select.select([self._sock], [], [], max(0.0, float(timeout)))
            except (OSError, ValueError):
                return self._event.wait(timeout)
            woke = bool(ready) or self._event.is_set()
        if woke:
            # One sweep per MIN_SWEEP_INTERVAL_S at most, whoever is sending.
            if self._last_wake is not None:
                gap = MIN_SWEEP_INTERVAL_S - (time.monotonic() - self._last_wake)
                if gap > 0:
                    time.sleep(gap)
            self._last_wake = time.monotonic()
        return woke

    def clear(self):
        """Drain every queued datagram. A row committed before its datagram was drained is swept
        by the sweep that follows this call; a datagram sent after it wakes the next wait."""
        self._event.clear()
        if self._sock is None:
            return
        while True:
            try:
                self._sock.recv(1024)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            self.stats["datagrams"] += 1


def lane_wake(address=None):
    """The lane's wake object: a LaneWake, or, under MEMECOIN_ONCHAIN_LANE_WAKE=0 (the rollback),
    the plain in-process threading.Event the lane used before, which binds nothing."""
    from sniper.settings import memecoin as cfg
    if not getattr(cfg, "MEMECOIN_ONCHAIN_LANE_WAKE", True):
        return threading.Event()
    lane = LaneWake(address or default_address())
    lane.bind()
    return lane
