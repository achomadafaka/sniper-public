"""ONE pacer per research key, shared across processes (Alpha 2026-10-02, entry-capture P2 option (a)).

The research Helius key is read by several processes (the slot-end shadow in the refusal-capture
process, the wallet graph and the research scripts through funded_exit_truth.rpc, and entry-capture
T1). Each used to pace itself, so their gaps did not add up and a 429 seen by one did not slow the
others. This module keeps one state file per key, `{"last": <the newest reserved call time>,
"paused_until": <a 429's pause end>}`, under an exclusive fcntl lock:

- acquire() RESERVES the next slot under the lock (max(last + gap, granted + gap, now)), writes it as
  `last`, and sleeps outside the lock until it. After the sleep it GRANTS under the lock, against
  `granted`, the time the previous caller was actually released: a sleep that resumed late (host
  suspend, scheduler stall, clock step) leaves its reserved slot in the past, and without the grant
  check it would send together with whoever reserved "now" meanwhile (Codex W12, 2026-10-06: actual
  gap 0.0 s against 1.1 s configured). A 429 seen by anyone while this caller waited sends it back
  to wait the pause out.
- pause(seconds) is what a caller does on a 429: everyone's next slot is after it.

Wall clock (`time.time`), the only clock processes share. A state further in the future than any
real reservation or pause could be (a clock step back, a corrupt file) is discarded as fresh, never
trusted to block."""
from __future__ import annotations

import fcntl
import json
import os
import re
import time

GAP_S = 1.1                 # the research key 429d at 0.13 s spacing (2026-09-27)
MAX_PAUSE_S = 600.0


class SharedPacer:
    def __init__(self, path, *, gap_s=GAP_S, clock=time.time, sleep=time.sleep):
        self.path, self.gap_s, self.clock, self.sleep = str(path), float(gap_s), clock, sleep

    @classmethod
    def for_url(cls, url, *, gap_s=GAP_S):
        """The pacer of the research key `url`, keyed by its non-secret key id."""
        from sniper.execution.solana_rpc_pool import key_id
        return cls.named(key_id(url), gap_s=gap_s)

    @classmethod
    def named(cls, name, *, gap_s=GAP_S):
        """The pacer `name` in ONE per-user directory: never under a checkout or a release, since a
        script run from a worktree must share the runtime's file (MEMECOIN_RESEARCH_PACER_DIR
        overrides it, for tests)."""
        root = (os.getenv("MEMECOIN_RESEARCH_PACER_DIR")
                or os.path.join(os.path.expanduser("~"), ".cache", "sniper", "research_pacer"))
        return cls(os.path.join(root, "%s.json" % name), gap_s=gap_s)

    def _locked(self, fn, *, nonblocking=False):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)   # on first use, not construction
        fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError:
                return False
            state = self._read()
            out, new = fn(state)
            if new is not None:
                tmp = "%s.%d.tmp" % (self.path, os.getpid())
                with open(tmp, "w") as f:
                    json.dump(new, f)
                os.replace(tmp, self.path)
            return out
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self):
        try:
            with open(self.path) as f:
                state = json.load(f)
            last, paused = float(state.get("last") or 0.0), float(state.get("paused_until") or 0.0)
            granted = float(state.get("granted") or 0.0)
        except (OSError, ValueError, TypeError, AttributeError):
            last = paused = granted = 0.0
        ceiling = self.clock() + self.gap_s + MAX_PAUSE_S
        return {"last": last if last <= ceiling else 0.0,
                "paused_until": paused if paused <= ceiling else 0.0,
                "granted": granted if granted <= ceiling else 0.0}

    def acquire(self):
        """Block until this caller may send one request; returns the reserved slot time."""
        while True:
            def reserve(state):
                now = self.clock()
                if state["paused_until"] > now:
                    return ("wait", state["paused_until"] - now), None
                slot = max(state["last"] + self.gap_s, state["granted"] + self.gap_s, now)
                return ("slot", slot), dict(state, last=slot)
            kind, value = self._locked(reserve)
            if kind == "wait":
                self.sleep(value)
                continue
            delay = value - self.clock()
            if delay > 0:
                self.sleep(delay)
            while True:
                def grant(state, slot=value):
                    now = self.clock()
                    if state["paused_until"] > now:      # a 429 seen by anyone while we slept
                        return ("paused", None), None
                    # This caller's send time: its slot, or later if it woke late.
                    at = max(now, slot)
                    early = state["granted"] + self.gap_s - at
                    if early > 0:                        # another caller was released less than a gap ago
                        return ("early", early), None
                    return ("granted", None), dict(state, granted=at)
                outcome, wait = self._locked(grant)
                if outcome != "early":
                    break
                self.sleep(wait)
            if outcome == "paused":
                continue
            return value

    def pause(self, seconds):
        """A 429: no caller of this key sends before now + seconds."""
        until = self.clock() + min(float(seconds), MAX_PAUSE_S)
        self._locked(lambda s: (None, dict(s, paused_until=max(s["paused_until"], until))))
        return until

    def try_acquire(self):
        """Research-only: grant NOW or skip, never reserve a future slot or wait for its lock."""
        def grant(state):
            now = self.clock()
            if max(state["paused_until"], state["last"] + self.gap_s,
                   state["granted"] + self.gap_s) > now:
                return False, None
            return True, dict(state, last=now, granted=now)
        return self._locked(grant, nonblocking=True)

    def paused_until(self):
        return self._locked(lambda s: (s["paused_until"], None))


# THE dRPC ACCOUNT (Gamma probe 2026-10-02): the free plan allows ~120 requests per rolling 60 s
# ACCOUNT-WIDE, every network counted, and answers HTTP 429 with code 15 "free plan rate limit",
# gone in ~50 s. The keyless <chain>.drpc.org hosts are a separate budget and are not paced here.
DRPC_GAP_S = 1 / 1.5
DRPC_LIMIT_PAUSE_S = 60.0


def _drpc_limit(error):
    if isinstance(error, dict):
        return error.get("code") == 15 or bool(_RATE_LIMIT.search(str(error.get("message") or "")))
    # A raised error's text: an HTTP 429, or a JSON-RPC error dict rendered into the message.
    text = str(error or "")
    return "429" in text or bool(_LIMIT_TEXT.search(text))


# dRPC also answers -32000 "rate-limited due to unusually high traffic" (Gamma 2026-10-14, 56 getLogs):
# hyphenated, so "rate limit" alone missed it and the shared pacer never paused on it.
_RATE_LIMIT = re.compile(r"rate[- ]limit", re.I)
_LIMIT_TEXT = re.compile(r"rate[- ]limit|too many|'code': 15\b|\"code\": 15\b", re.I)


def drpc_paused_s(url):
    """Seconds left on the dRPC account's shared limit pause for `url` (0 for any other host, or
    none in force): a caller on a shared worker skips instead of blocking in `acquire`."""
    from sniper.services.memecoin_evm_exact_capture import budget
    if budget(url) != "drpc.org (keyed)":
        return 0.0
    return max(0.0, SharedPacer.named("drpc-keyed", gap_s=DRPC_GAP_S).paused_until() - time.time())


def _drpc_send(url, send, limited, *, admission_fn=None):
    """`send()` behind the dRPC account's ONE shared pacer when `url` is a keyed dRPC URL
    (`budget(url) == "drpc.org (keyed)"`, whatever its network), else `send()` alone. A limit
    answer (`limited(out)`, or a raised 429) pauses every user of the account for
    DRPC_LIMIT_PAUSE_S."""
    from sniper.services.memecoin_evm_exact_capture import budget
    from sniper.execution.evm_rpc_pool import RequestNotSent
    def admitted():
        if admission_fn is not None and not admission_fn():
            raise RequestNotSent('speculative research admission expired')
    admitted()
    if budget(url) != "drpc.org (keyed)":
        return send()
    pacer = SharedPacer.named("drpc-keyed", gap_s=DRPC_GAP_S)
    if admission_fn is None:
        pacer.acquire()
    else:
        try:
            granted = pacer.try_acquire()
        except OSError as exc:
            raise RequestNotSent('speculative shared research capacity unavailable') from exc
        if not granted:
            raise RequestNotSent('speculative shared research capacity unavailable')
    admitted()
    try:
        out = send()
    except Exception as exc:
        if (getattr(exc, "code", None) == 429
                or getattr(exc.__cause__, "code", None) == 429):
            pacer.pause(DRPC_LIMIT_PAUSE_S)
        raise
    if limited(out):
        pacer.pause(DRPC_LIMIT_PAUSE_S)
    return out


def drpc_paced(call):
    """`call(url, method, params) -> (result, error)` behind the dRPC account's pacer."""
    def paced(url, method, params):
        return _drpc_send(url, lambda: call(url, method, params), lambda out: _drpc_limit(out[1]))
    return paced


def drpc_paced_http(http, *, admission_fn=None):
    """An http seam `(method, url, headers=None, body=None, timeout=10) -> (status, reply,
    headers)` behind the dRPC account's pacer: a 429 status or a limit error in the reply pauses."""
    def limited(out):
        rows = out[1] if isinstance(out[1], list) else [out[1]]
        return out[0] == 429 or any(isinstance(row, dict) and row.get("error") is not None
                                   and _drpc_limit(row["error"]) for row in rows)

    def paced(method, url, headers=None, body=None, timeout=10):
        send = lambda: http(method, url, headers=headers, body=body, timeout=timeout)
        if admission_fn is None:
            return _drpc_send(url, send, limited)
        return _drpc_send(url, send, limited, admission_fn=admission_fn)
    return paced


def drpc_paced_json(fetch):
    """JSON fetch, including RPC batches, behind the same research account budget."""
    def limited(reply):
        rows = reply if isinstance(reply, list) else [reply]
        return any(isinstance(row, dict) and row.get("error") is not None
                   and _drpc_limit(row["error"]) for row in rows)

    def paced(url, **kwargs):
        return _drpc_send(url, lambda: fetch(url, **kwargs), limited)
    return paced
