"""4663 research-key rotation (operator 2026-10-02 via Alpha: another free QuickNode key).

The base key stays MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663; spares sit in numbered slots
MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663_2, _3, ... (data/add_research_qn_key_4663.sh fills the next
free one after an eth_chainId == 0x1237 check). Every research consumer keeps naming the BASE URL as
its host; `send` swaps in the active slot at send time. A QUOTA answer (QuickNode's -32003 "daily
request limit reached", or an error naming a daily/credit/quota/monthly limit) marks that slot spent
until the UTC day rolls and the same read is retried once on the next slot. A plain RPS limit (a 429,
QuickNode's -32007/-32008 per-second/per-minute limits whatever their text, or an error that says per second / rps / rate limit) is returned untouched, so each caller keeps its
own pause. With no spares the active slot is always the base: exactly today's behaviour.

Spares pass the lane's safety_host_refusal (never the custody RPC, the inline key, another role's
URL, a funded read-pool member or a free-submission endpoint, never a duplicate). The base key is
not re-checked: it is today's host. Only reads go through here (every research consumer reads);
nothing signs or submits through a research host.

A TRANSPORT failure (TLS alert, connect, reset, timeout: no answer at all) never spends a slot:
UNUSABLE_AFTER in a row make it unusable for UNUSABLE_S, and reads move to the next usable slot (Alpha
2026-10-02, after a QuickNode host answered every call with a TLS alert). Any answer resets the run.

One Rotation per consumer (the lane cache, the feed's float poll, the exit follow-up), never a module
global: its state is a set of spent slots that only grows within a UTC day and resets at the roll,
plus per-slot unusable marks, under one lock never held across network I/O. Each consumer learns a spent key from its own first
quota answer: one failed read per consumer per day, no shared file.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

BASE = "MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663"
_SLOT = re.compile(r"^%s_(\d+)$" % BASE)
_RPS = re.compile(r"per second|\brps\b|rate limit|too many requests", re.I)
_QUOTA = re.compile(r"daily|request limit reached|credit|quota|monthly", re.I)


def is_quota_answer(reply):
    """True for a provider's spent-quota answer, False for anything else (an RPS limit included).
    Only a JSON-RPC error body can say which: a bare 429 is an RPS limit as far as anyone can tell."""
    if isinstance(reply, list):
        return any(is_quota_answer(row) for row in reply)
    err = reply.get("error") if isinstance(reply, dict) else None
    if not isinstance(err, dict):
        return False
    message = str(err.get("message") or "")
    if err.get("code") in (-32007, -32008) or _RPS.search(message):   # QuickNode per-second, per-minute
        return False
    return err.get("code") == -32003 or bool(_QUOTA.search(message))


def _norm(url):
    return str(url or "").strip().rstrip("/").lower()


def _utc_day(now):
    return datetime.fromtimestamp(now, timezone.utc).date()


class Rotation:
    UNUSABLE_AFTER, UNUSABLE_S = 3, 600.0

    def __init__(self, env=None, *, clock=None, log=None, drpc_fallback=False):
        import time
        env = os.environ if env is None else env
        self.clock = clock or time.time
        self.log = log or logger.warning
        self._lock = threading.Lock()
        base = str(env.get(BASE) or "").strip()
        self.slots = [(1, base)] if base else []          # (slot number, url); slot 1 is the base
        self._preferred = None
        self._identity_slots, self._verified = set(), set()
        spares = sorted((int(m.group(1)), name) for name in env
                        if (m := _SLOT.match(str(name))) and str(env.get(name) or "").strip())
        if spares:
            from sniper.services.memecoin_rh_fast_lane import safety_host_refusal
            for number, name in spares:
                url = str(env[name]).strip()
                why = safety_host_refusal(url, env, own_name=name)
                if why is not None:           # never the URL: a key rides in its path
                    self.log("[research-rotation] %s refused (%s): not in the rotation" % (name, why))
                    continue
                self.slots.append((number, url))
        # Funded snapshot and safety preparation prefer the working dedicated dRPC account.
        # Keep legacy slot IDs/aliases and its identity/pacing gates; other consumers do not opt in.
        drpc_name = "MEMECOIN_RESEARCH_DRPC_RPC_URL_4663"
        if drpc_fallback and base and (url := str(env.get(drpc_name) or "").strip()):
            from sniper.services.memecoin_rh_fast_lane import safety_host_refusal
            why = safety_host_refusal(url, env, own_name=drpc_name)
            if why is None:
                number = max(n for n, _ in self.slots) + 1
                self.slots.append((number, url))
                self._identity_slots.add(number)
                self._preferred = (number, url)
            else:
                self.log("[research-rotation] %s refused (%s): not in the rotation" % (drpc_name, why))
        self._spent, self._day = set(), _utc_day(self.clock())
        self._runs, self._down = {}, {}            # slot -> transport failures in a row / unusable until
        self._provider_errors = 0
        self.counts = {n: {"calls": 0, "quota": 0, "transport": 0} for n, _u in self.slots}

    def _roll(self):
        day = _utc_day(self.clock())
        if day != self._day:
            if self._spent:
                self.log("[research-rotation] UTC day rolled: every 4663 research slot is back")
            self._day, self._spent = day, set()

    def _usable(self, number):
        return number not in self._spent and self.clock() >= self._down.get(number, 0.0)

    def _active(self):
        if self._preferred is not None and self._usable(self._preferred[0]):
            return self._preferred
        for number, url in self.slots:
            if self._usable(number):
                return number, url
        return self.slots[0] if self.slots else (None, "")    # none usable: the base, as today

    def is_research(self, url):
        want = _norm(url)
        return bool(want) and any(_norm(u) == want for _n, u in self.slots)

    def active(self):
        with self._lock:
            self._roll()
            return self._active()

    def stats(self):
        """`(active slot, [{slot, calls, quota, transport, spent, unusable}])`: slot numbers and
        counts, never a URL."""
        with self._lock:
            self._roll()
            now = self.clock()
            return self._active()[0], [dict(slot=n, spent=n in self._spent,
                                            unusable=now < self._down.get(n, 0.0), **self.counts[n])
                                       for n, _u in self.slots]

    def provider_errors(self):
        with self._lock:
            return self._provider_errors

    def _observe(self, out, *, allow_batch=False):
        """Answered failures also matter when quote readers swallow them into no route.
        A clean contract revert is token evidence; no answer/partial/server errors are not."""
        status, reply = out[:2]
        rows = reply if allow_batch and isinstance(reply, list) else [reply]
        failed = status is None or status >= 400 or not rows
        for row in rows:
            if not isinstance(row, dict):
                failed = True
                continue
            err = row.get("error")
            revert = (isinstance(err, dict) and (err.get("code") == 3
                      or "execution reverted" in str(err.get("message") or "").lower()))
            failed |= bool(err) and not revert
            if not err:
                result = row.get("result")
                failed |= result is None or (isinstance(result, str)
                    and not re.fullmatch(r"0x[0-9a-fA-F]*", result))
        if failed:
            with self._lock:
                self._provider_errors += 1
        return out

    def _note(self, number, quota, transport=None):
        """Count one read on slot `number`; True when it was a quota answer and an unspent slot is
        left to retry on (another thread may already have moved past `number`). `transport` True is
        a transport failure, False an answer, None a raise that is neither (the run is untouched)."""
        with self._lock:
            self._roll()
            self.counts[number]["calls"] += 1
            if transport is not None:
                self._runs[number] = self._runs.get(number, 0) + 1 if transport else 0
            if transport:
                self.counts[number]["transport"] += 1
                if self._runs[number] >= self.UNUSABLE_AFTER and self._usable(number):
                    self._down[number] = self.clock() + self.UNUSABLE_S
                    nxt = self._active()[0]
                    self.log("[research-rotation] 4663 research slot %s unusable for %ds after %d "
                             "transport failures (never spent): slot %s" % (
                                 number, self.UNUSABLE_S, self._runs[number], nxt))
                return False
            if not quota:
                return False
            self.counts[number]["quota"] += 1
            fresh_spend = number not in self._spent
            self._spent.add(number)
            nxt = self._active()[0]
            left = nxt not in self._spent
        if fresh_spend:
            self.log("[research-rotation] 4663 research slot %s spent its quota: %s" % (
                number, "slot %s until the UTC day rolls" % nxt if left else "no slot left today"))
        return left

    def send(self, url, do, *, identity=None, allow_batch=False, admission_fn=None):
        """`do(target_url) -> (status, reply, ...)` on the active slot when `url` is a research slot,
        else on `url` itself. A quota answer moves to the next slot and retries the read, once per
        slot left; the last answer is returned as it came. A raise is counted and passed on."""
        from sniper.execution.evm_rpc_pool import RequestNotSent
        def admitted():
            if admission_fn is not None and not admission_fn():
                raise RequestNotSent('speculative research admission expired')
        admitted()
        if not self.is_research(url):
            return self._observe(do(url), allow_batch=allow_batch)
        number, target = self.active()
        while True:
            try:
                admitted()
                # All opted-in dRPC snapshot calls share the existing research account pacer.
                # Rotation is used by background readers; the inline host bypasses it.
                from sniper.services.memecoin_research_pacer import drpc_paced_http
                def paced(read):
                    return drpc_paced_http(lambda _method, endpoint, **_kwargs: read(endpoint),
                                           admission_fn=admission_fn)("POST", target)
                with self._lock:
                    needs_identity = number in self._identity_slots and number not in self._verified
                if needs_identity:
                    answer = paced(identity) if identity is not None else None
                    reply = answer[1] if answer and len(answer) > 1 else None
                    try:
                        chain = (int(reply.get("result"), 16)
                                 if answer and answer[0] == 200 and isinstance(reply, dict)
                                 and not reply.get("error") else None)
                    except (TypeError, ValueError):
                        chain = None
                    if chain != 4663:
                        raise OSError("research fallback chain identity unavailable or mismatched")
                    with self._lock:
                        self._verified.add(number)
                out = self._observe(paced(do), allow_batch=allow_batch)
            except RequestNotSent:
                raise                         # no physical read; not a provider failure
            except Exception as exc:
                from sniper.services.memecoin_rh_fast_lane import is_transport_failure
                self._note(number, False, transport=True if is_transport_failure(exc) else None)
                raise
            reply = out[1] if len(out) > 1 else None
            err = reply.get("error") if isinstance(reply, dict) else None
            # urllib's float reader returns transport errors rather than raising them.
            # An HTTP response is still an answer, even if its body names a transport error.
            transport = out[0] is None and isinstance(err, dict) and bool(err.get("transport"))
            if not self._note(number, is_quota_answer(reply), transport=transport):
                return out
            number, target = self.active()

    def http_seam(self, call):
        """An http seam `(method, url, headers=None, body=None, timeout=10) -> (status, reply,
        headers)` that sends research reads through `send`; `call` is a zero-argument function
        returning today's seam, looked up per request (so a test's patch of it holds).
        Bodyless external GETs are REST: their payload is validated by the caller, not as RPC."""
        def http(method, url, headers=None, body=None, timeout=10, *, admission_fn=None):
            import json
            if str(method).upper() == "GET" and body is None and not self.is_research(url):
                # Binance/FMP prices and market-data reads are not JSON-RPC. A successful
                # response has no RPC result; an unavailable read must still prevent a
                # swallowed price failure from becoming a cached token-safety refusal.
                if admission_fn is not None and not admission_fn():
                    from sniper.execution.evm_rpc_pool import RequestNotSent
                    raise RequestNotSent('speculative research admission expired')
                try:
                    out = call()(method, url, headers=headers, body=body, timeout=timeout)
                except Exception:
                    with self._lock:
                        self._provider_errors += 1
                    raise
                if out[0] is None or out[0] >= 400:
                    with self._lock:
                        self._provider_errors += 1
                return out
            return self.send(url, lambda target: call()(method, target, headers=headers, body=body,
                                                        timeout=timeout),
                             identity=lambda target: call()("POST", target,
                                 headers={"Content-Type": "application/json"},
                                 body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId",
                                                  "params": []}).encode(), timeout=timeout),
                             admission_fn=admission_fn)
        http.background_call = http           # invocation-owned admission; ordinary calls unchanged
        return http

    def default_seam(self):
        """evm_wallet._http through this rotation (looked up per request)."""
        def today():
            from sniper.execution import evm_wallet
            return evm_wallet._http
        return self.http_seam(today)
