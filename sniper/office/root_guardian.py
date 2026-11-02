"""Guardian policy core for reviewed root installation; no running service bindings.

Publisher has only guard+shared-lock mounts. The guardian never reads/writes signer
key, issuance or replay/consume state. Installed policy/telemetry/kill probes MUST
be immutable root bindings, not Office JSON/config/callbacks.
"""
import math
import time

from .planned_host import atomic

POLICY='office-host-guard-pilot-v3'
AUTO_REASONS=frozenset({'funded_heartbeat','cpu_pressure','io_pressure','memory_available'})


def finite(value):
    return type(value)in(int,float) and math.isfinite(value) and value>=0


def unrelated_fault(sample,reason):
    if reason is not None and reason!='funded_heartbeat':return reason
    if isinstance(sample,dict):
        for key,name in (('cpu_psi_some_avg10','cpu_pressure'),('io_psi_some_avg10','io_pressure')):
            if finite(sample.get(key)) and sample[key]>.20:return name
        if finite(sample.get('mem_available_bytes')) and sample['mem_available_bytes']<4294967296:
            return 'memory_available'
    return None


class Guardian:
    def __init__(self,publisher,guard,*,terminate,population,kill_available,clock=time.monotonic):
        self.publisher,self.guard=publisher,guard
        self.terminate,self.population,self.clock=terminate,population,clock
        self.kill_available=kill_available
        self.next_attempt=0.

    def tick(self,sample,*,now):
        """Real normal checks, ordinary900s recovery only; no marker suppression.

        Protected lock covers fresh authority read, decision, publication and the
        short Office-only kill syscall. No five-second recovery observation holds it.
        Lock conflict raises; no caller may acknowledge a failed publication.
        """
        reason=self.guard.check(sample,now=now)
        if reason is None:
            try:
                if self.kill_available()is not True:raise ValueError("kill_control_unknown")
            except (OSError,ValueError):
                reason=self.guard._fail("kill_control_unavailable")
        with self.publisher.locked():
            try:old=self.publisher.read(self.publisher.guard)
            except (OSError,ValueError):old={}
            revision=old.get('revision')
            revision=revision if type(revision)is int and revision>=0 else 0
            paused=(old.get('state')=='paused' and type(old.get('reason'))is str and bool(old['reason']))
            fresh=finite(old.get('at')) and finite(now) and 0<=now-old['at']<=5
            sequence=old.get('unrelated_fault_seq')
            sequence_valid=type(sequence)is int and 0<=sequence<2**63-1
            valid=(old.get('guard_policy_version')==POLICY and fresh and sequence_valid)
            ready=old.get('state')=='ready' and old.get('reason')is None
            malformed=not valid or not(paused or ready)
            latch_reason=None
            if not paused and not(ready and valid):
                latch_reason='guard_stale' if ready and finite(old.get('at')) else 'guard_unknown'
            conflicting=(old.get('auto_resume_blocked')is True
                         or 'auto_resume_blocked'in old and type(old['auto_resume_blocked'])is not bool)
            termination=old.get('termination')
            if 'termination'in old and (type(termination)is not dict or termination.get('state')!='sent'):
                conflicting=True
            blocked=malformed or conflicting
            if ready and (conflicting or not sequence_valid or old.get('guard_policy_version')!=POLICY):
                latch_reason='guard_unknown'
            sequence=sequence if sequence_valid else None
            last_fault=old.get('last_unrelated_fault')
            def note_fault(fault):
                nonlocal sequence,last_fault
                detail=dict(reason=fault,at=now)
                if sequence is not None and detail!=last_fault:
                    sequence+=1
                last_fault=detail
            fault=unrelated_fault(sample,reason)
            if fault is None and conflicting:fault='guard_unknown'
            if fault is None and paused and old['reason']not in AUTO_REASONS:fault=old['reason']
            if fault is not None:note_fault(fault)
            ordinary=(paused and old['reason']in AUTO_REASONS and reason is None
                      and self.guard.resume_eligible and not blocked)
            if reason is None and not blocked and ((ready and valid) or ordinary):
                out=dict(state='ready',reason=None,at=now,revision=revision+1,mode='pilot',
                         guard_policy_version=POLICY,healthy_since=self.guard.healthy_since,
                         resume_eligible=self.guard.resume_eligible,unrelated_fault_seq=sequence,
                         last_unrelated_fault=last_fault)
                if ordinary:
                    out.update(previous_pause_reason=old['reason'],recovery_basis='ordinary_900s')
                elif old.get('recovery_basis')is not None:
                    out['recovery_basis']=old['recovery_basis']
                atomic(self.publisher.guard,out)
                self.next_attempt=0.
                return out
            out=dict(old,state='paused',reason=old['reason'] if paused else reason or latch_reason,
                     at=now,revision=revision+1,guard_policy_version=POLICY,
                     healthy_since=self.guard.healthy_since,resume_eligible=self.guard.resume_eligible,
                     unrelated_fault_seq=sequence,last_unrelated_fault=last_fault)
            if blocked:out['auto_resume_blocked']=True
            atomic(self.publisher.guard,out) # revoke readiness BEFORE termination
            try:
                populated=self.population()
                if type(populated)is not bool:raise ValueError('population_unknown')
                out['population']=dict(state='populated' if populated else 'empty',at=now)
            except (OSError,ValueError):
                populated=None
                out['population']=dict(state='unknown',at=now)
                out['auto_resume_blocked']=True
                note_fault('population_unknown')
            tick=self.clock()
            if populated is not False and tick>=self.next_attempt:
                try:self.terminate()
                except (OSError,ValueError) as exc:
                    out['termination']=dict(state='failed',at=now,error=type(exc).__name__)
                    out['auto_resume_blocked']=True
                    note_fault('termination_failed')
                else:out['termination']=dict(state='sent',at=now)
                self.next_attempt=tick+5.
            out['revision']+=1
            out.update(unrelated_fault_seq=sequence,last_unrelated_fault=last_fault)
            atomic(self.publisher.guard,out)
            return out
