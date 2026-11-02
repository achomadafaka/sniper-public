"""Bounded synchronous admission; adapter deadlines and supervisor child cancellation remain authoritative."""
from dataclasses import replace
import math
import threading
import time

from sniper.office.models import Envelope


class GuardRefused(ValueError):
    def __init__(self,reason,detail):
        super().__init__(reason)
        self.detail=detail


class Scheduler:
    def __init__(self, caps: dict[str,int], host_children_max: int, *, max_pending=64,
                 backoff_s=30., external_guard=None):
        if (not caps or any(k not in ('claude','codex') or type(v) is not int or v<1 for k,v in caps.items())
                or type(host_children_max) is not int or host_children_max<1
                or type(max_pending) is not int or max_pending<0
                or type(backoff_s) not in (int,float) or not math.isfinite(backoff_s) or backoff_s<=0):
            raise ValueError('scheduler_configuration')
        self.caps=dict(caps)
        self.host_children_max=host_children_max
        self.max_pending=max_pending
        self.backoff_s=float(backoff_s)
        self.external_guard=external_guard
        self._condition=threading.Condition()
        self._active={k:0 for k in caps}
        self._pending=0
        self._live_pending=0
        self._paused={}
        self._until={}

    def pause(self, provider, reason, *, account_id=None):
        if provider not in self.caps or not isinstance(reason,str) or not reason:
            raise ValueError('pause_configuration')
        with self._condition:
            self._paused[(provider,account_id)]=reason
            self._condition.notify_all()

    def resume(self, provider, *, account_id=None):
        with self._condition:
            self._paused.pop((provider,account_id),None)
            self._until.pop((provider,account_id),None)
            self._condition.notify_all()

    def _guard(self):
        if self.external_guard is None:
            return None
        try:
            reason=self.external_guard()
            return reason if isinstance(reason,str) and reason else None if reason is None else 'guard_unknown'
        except GuardRefused as exc:
            return exc
        except Exception:
            return 'guard_unknown'

    def live_backlogged(self):
        with self._condition:
            return self._live_pending > 0

    def submit(self, provider, fn, *, account_id=None, timeout_s=30., priority='live') -> Envelope:
        """Bound queue wait only. fn must use its own bounded adapter/child deadline.

        No implicit retries or model changes. Account failures isolate that account; provider
        guards pause every account. One supervisor owns capacity; existing Codex slot still
        arbitrates its subscription across processes.
        """
        begun=time.monotonic()
        waited=throttled=0.
        key=(provider,account_id)
        def refused(outcome,reason):
            return Envelope(requested_provider=provider,requested_model='',outcome=outcome,
                            account_id=account_id,error_reason=str(reason),waiting_s=waited,throttled_s=throttled,
                            guard_error=reason.detail if isinstance(reason,GuardRefused) else None)
        if priority not in ('live','replay') or provider not in self.caps or type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or timeout_s<0:
            return refused('refused','configuration')
        guard=self._guard()
        if guard:
            return refused('refused',guard)
        queued=False
        with self._condition:
            try:
                while True:
                    if priority=='replay' and self._live_pending:
                        return refused('refused','live_backlog')
                    guard=self._guard()
                    if guard:
                        return refused('refused',guard)
                    reason=self._paused.get((provider,None)) or self._paused.get(key)
                    if reason:
                        return refused('auth_revoked' if reason=='auth_revoked' else 'refused',reason)
                    now=time.monotonic()
                    until=max(self._until.get((provider,None),0.),self._until.get(key,0.))
                    capacity=(self._active[provider]<self.caps[provider]
                              and sum(self._active.values())<self.host_children_max)
                    if capacity and now>=until:
                        self._active[provider]+=1
                        break
                    if not queued:
                        if self._pending>=self.max_pending:
                            return refused('refused','queue_full')
                        self._pending+=1
                        if priority=='live': self._live_pending+=1
                        queued=True
                    remaining=timeout_s-(now-begun)
                    if remaining<=0:
                        return refused('rate_limited' if now<until else 'timeout',
                                       'provider_backoff' if now<until else 'queue_deadline')
                    before=time.monotonic()
                    self._condition.wait(min(remaining,.05))
                    duration=time.monotonic()-before
                    if now<until:
                        throttled+=duration
                    else:
                        waited+=duration
            finally:
                if queued:
                    self._pending-=1
                    if priority=='live': self._live_pending-=1
        try:
            try:
                result=fn()
                if not isinstance(result,Envelope) or result.requested_provider!=provider:
                    result=Envelope(requested_provider=provider,requested_model='',outcome='invalid',error_reason='adapter_result')
            except Exception:
                result=Envelope(requested_provider=provider,requested_model='',outcome='invalid',error_reason='adapter_exception')
            with self._condition:
                if result.outcome=='rate_limited':
                    self._until[key]=time.monotonic()+self.backoff_s
                elif result.outcome=='auth_revoked':
                    self._paused[key]='auth_revoked'
            return replace(result,account_id=account_id if account_id is not None else result.account_id,
                           waiting_s=result.waiting_s+waited,throttled_s=result.throttled_s+throttled)
        finally:
            with self._condition:
                self._active[provider]-=1
                self._condition.notify_all()
