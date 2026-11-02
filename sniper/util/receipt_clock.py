"""Identify the host and boot for local monotonic receipt clocks."""
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timezone
from itertools import count
import math
import time
import uuid


@lru_cache(maxsize=1)
def clock_domain():
    """Return the durable host and boot identity, or None when unavailable."""
    try:
        host = Path('/etc/machine-id').read_text().strip()
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return None
    if not host or not boot:
        return None
    return 'linux:' + sha256((host + ':' + boot).encode()).hexdigest()


class _ReceiptStamp(dict):
    """Flat immutable metadata that existing JSON/IPC encoders can serialize."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("receipt clock is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def freeze_receipt_clock(value):
    """Validate additive IPC metadata; missing/malformed anchors remain unknown."""
    if not isinstance(value, Mapping):
        return None
    try:
        if value.get("history_unknown", False) is not False:
            return None
        for key in ("monotonic_ns", "sequence"):
            if type(value[key]) is not int or value[key] < 0:
                return None
        if value["version"] != 1 or not isinstance(value["producer_id"], str) or not value["producer_id"]:
            return None
        if value["boot_id"] is not None and not isinstance(value["boot_id"], str):
            return None
        if not math.isfinite(float(value["correction_s"])):
            return None
        for key in ("raw_utc", "corrected_utc"):
            parsed = datetime.fromisoformat(value[key].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
        keys = ("version", "raw_utc", "corrected_utc", "correction_s", "monotonic_ns",
                "boot_id", "producer_id", "sequence")
        return value if isinstance(value, _ReceiptStamp) else _ReceiptStamp((key, value[key]) for key in keys)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def elapsed_receipt_ms(value, monotonic_ns, *, boot_id=None):
    """A complete same-boot monotonic interval, or None; never repair/clamp old clocks."""
    value = freeze_receipt_clock(value)
    boot_id = clock_domain() if boot_id is None else boot_id
    if (value is None or not boot_id or value["boot_id"] != boot_id
            or type(monotonic_ns) is not int or monotonic_ns < value["monotonic_ns"]):
        return None
    return (monotonic_ns - value["monotonic_ns"]) / 1_000_000


class ReceiptClock:
    """Producer-owned stamps; construct at startup, refresh correction only off-path."""

    def __init__(self, producer_id, *, raw_time_fn=None, cap=4096):
        self.boot_id = clock_domain()
        self.producer_id = str(producer_id) + ":" + uuid.uuid4().hex
        self._raw_time = raw_time_fn or time.time
        self._sequence = count(1)
        self._anchors, self._cap, self.evicted = OrderedDict(), cap, 0
        self.refresh_correction()

    def refresh_correction(self):
        from sniper.util.clock import offset_s
        value = offset_s()
        self._correction = value if math.isfinite(value) else 0.0

    def capture(self, *, monotonic_ns=None, corrected_utc=None):
        mono = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
        raw = self._raw_time()
        corrected = (datetime.fromtimestamp(raw - self._correction, timezone.utc).isoformat()
                     if corrected_utc is None else corrected_utc)
        correction = (self._correction if corrected_utc is None else
                      raw - datetime.fromisoformat(corrected.replace("Z", "+00:00")).timestamp())
        return _ReceiptStamp(version=1, raw_utc=datetime.fromtimestamp(raw, timezone.utc).isoformat(),
                             corrected_utc=corrected, correction_s=correction, monotonic_ns=mono,
                             boot_id=self.boot_id, producer_id=self.producer_id,
                             sequence=next(self._sequence))

    def known(self, exact_key, stamp):
        """Read one immutable earlier anchor without mutating the producer's map."""
        previous = self._anchors.get(exact_key)
        unknown = (stamp.get("history_unknown", False)
                   or (previous is None and self.evicted > 0)
                   or (previous is not None and previous.get("history_unknown", False)))
        if previous is not None and (not stamp["boot_id"] or stamp["boot_id"] != previous["boot_id"]
                                     or stamp["monotonic_ns"] >= previous["monotonic_ns"]):
            stamp = previous
        # Retain raw immutable fields for direct metadata consumers, but never grade an
        # absent key as first receipt after this bounded map has forgotten any history.
        if unknown and not stamp.get("history_unknown", False):
            return _ReceiptStamp(stamp, history_unknown=True)
        return stamp

    def remember(self, exact_key, stamp):
        """Retain earliest same-boot receipt. Caller owns existing synchronization."""
        previous = self._anchors.get(exact_key)
        stamp = self.known(exact_key, stamp)
        if previous is stamp:
            return stamp
        self._anchors[exact_key] = stamp
        if len(self._anchors) > self._cap:
            self._anchors.popitem(last=False)
            self.evicted += 1
        return stamp
