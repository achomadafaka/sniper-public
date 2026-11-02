"""One publication embargo for unsupported memecoin performance conclusions.

Legacy diagnostics require an explicit per-context opt-in. ContextVar isolation
prevents one diagnostic reader from enabling another thread's publication.
There is no bypass for policy consumers or candidate promotion.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import sys
from .evidence import PROTOCOL, study_gate

_DIAGNOSTIC = ContextVar('wallet_execution_diagnostic', default=False)

class UnsupportedResearch(ValueError):
    pass


def legacy_eligibility():
    return {'eligible':False,'protocol':PROTOCOL,'codes':['protocol_missing_or_legacy'],
            'basis':'legacy_sampled_diagnostic','funded_authority':False}


def diagnostic_active():
    return _DIAGNOSTIC.get()


@contextmanager
def diagnostic_mode():
    token=_DIAGNOSTIC.set(True)
    try: yield
    finally: _DIAGNOSTIC.reset(token)


def require_study(manifest=None, episodes=()):
    g=study_gate(manifest,episodes)
    if not g.eligible:
        raise UnsupportedResearch('unsupported memecoin performance finding: '+','.join(g.codes))
    return g


def legacy_report(fn):
    @wraps(fn)
    def guarded(*args,**kwargs):
        if not diagnostic_active():
            raise UnsupportedResearch('legacy hypothetical performance report is ineligible; use --diagnostic-only for raw historical diagnostics')
        return fn(*args,**kwargs)
    guarded.research_quarantined=True
    return guarded


def quarantine_exports(namespace, names):
    # This runs only in legacy standalone research modules, never funded code.
    if namespace.get('__name__')=='__main__' and '--diagnostic-only' in sys.argv:
        sys.argv.remove('--diagnostic-only')
        _DIAGNOSTIC.set(True)
        print('DIAGNOSTIC ONLY: legacy sampled paths; no supported strategy, filter, wallet ranking or arming finding.',file=sys.stderr)
    for name in names:
        fn=namespace.get(name)
        if callable(fn): namespace[name]=legacy_report(fn)


def brain_claim_errors(*, domain, kind, scope=None, manifest=None, episodes=()):
    if domain != 'memecoin' or kind not in ('finding','measurement','decision'):
        return []
    if scope in ('operational','actual_execution','relationship','normative'):
        return []
    try: require_study(manifest,episodes)
    except (UnsupportedResearch,ValueError,TypeError) as exc:
        return [str(exc)]
    return []


def diagnostic_result(fn):
    """Label legacy API aggregates; preserve actual funded rows and raw metrics."""
    from inspect import signature
    sig=signature(fn)
    @wraps(fn)
    def guarded(*args,**kwargs):
        result=fn(*args,**kwargs)
        bound=sig.bind_partial(*args,**kwargs)
        if bound.arguments.get('mode')=='real': return result
        meta=legacy_eligibility()
        if isinstance(result,dict): return {**result,'research_eligibility':meta}
        if isinstance(result,list):
            return [{**row,'research_eligibility':meta} if isinstance(row,dict) else row for row in result]
        return result
    return guarded


def memory_claim_guard(name, content, *, metadata=None, entry_type=None):
    """Typed legacy brain ingress; untyped memecoin findings cannot become active."""
    meta=metadata or {}
    domain=meta.get('domain')
    if domain is None and 'memecoin' in (str(name)+' '+str(content)).lower(): domain='memecoin'
    kind=entry_type or ('finding' if str(name).startswith(('finding/','research:','research/')) else None)
    if kind is None and domain=='memecoin':
        text=(str(name)+' '+str(content)).lower()
        if any(word in text for word in ('paper','counterfactual','hypothetical','replay','shadow','strategy rank','win rate','promote wallet')):
            kind='finding'
    errors=brain_claim_errors(domain=domain, kind=kind, scope=meta.get('claim_scope'),
                              manifest=meta.get('execution_study'), episodes=meta.get('execution_episodes') or ())
    if errors: raise UnsupportedResearch('; '.join(errors))


def legacy_launch(module_name):
    """Reject a bare legacy CLI before imports or module-level database work."""
    if module_name!='__main__': return
    if '--diagnostic-only' in sys.argv:
        sys.argv.remove('--diagnostic-only'); _DIAGNOSTIC.set(True)
        print('DIAGNOSTIC ONLY: legacy sampled paths; no supported performance finding.',file=sys.stderr)
        return
    if '--help' in sys.argv or '-h' in sys.argv:
        print('Legacy hypothetical research is quarantined. Add --diagnostic-only to inspect historical raw diagnostics. No strategy conclusions or promotion are supported.')
        raise SystemExit(0)
    raise SystemExit('Research blocked: unsupported hypothetical performance evidence. Use --diagnostic-only for historical diagnostics.')
