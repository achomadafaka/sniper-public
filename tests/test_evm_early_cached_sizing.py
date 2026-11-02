"""HUSH (56, attempt 2c0ab7cf, Alpha GO 2026-10-07): the early EVM quote launched in 5 of 34 entries
on 56. Its sizing blocked on the early balance read (p50 286 / p90 335 ms), so it was ready about
when the main thread's own quote began, and take() won the launch token (`not_started`). Token
safety then started at ~el 327 and ran serial inside (sell quote -> slot discovery -> sim).

With MEMECOIN_EVM_EARLY_CACHED_SIZING (code default off): the early sizing uses the chain's cached
pre-gate balance from the CURRENT spend generation, so the early quote launches at once; the same
worker starts token safety beside it with that clip. take() still binds mint + clip + age; the leg
reuses only a PASSED early safety bound to mint + usd + its own deadline, and re-runs anything else.
The worker's own outcome rides the evm.early_quote mark even with the knob off."""
import threading
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_evm_leg as leg
from sniper.services import memecoin_real_runner as runner
from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
from sniper.settings import memecoin as cfg

CHAIN = "8453"


class Marks:
    def __init__(self):
        self.marks = []

    def mark_substage(self, name, **kw):
        self.marks.append((name, kw))

    def last(self, name):
        return [kw for n, kw in self.marks if n == name][-1]


# --- the early sizing's balance source ---------------------------------------------------------

@pytest.fixture
def sizing(monkeypatch, tmp_path, request):
    """_early_evm_quote_clip with every gate stubbed but the balance source; returns (call, pre)."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_arming as arming
    from sniper.services import memecoin_evm_risk as risk, memecoin_wallet_caps as caps
    from sniper.services.memecoin_risk import real_trade_size
    from test_memecoin_entry_identity import _approve, _event
    chain = getattr(request, "param", CHAIN)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", (*cfg.MEMECOIN_EVM_ALLOWLIST, chain))
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    _approve(conn, chains=(chain,))
    conn.close()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ROSTER_GATE_ENABLED", True)
    monkeypatch.setattr(arming, "real_entries_allowed", lambda *a: True)
    monkeypatch.setattr(arming, "in_quiet_hours", lambda: False)
    monkeypatch.setattr(runner, "_trial_for_entry", lambda *a: None)
    monkeypatch.setattr(caps, "entry_cap_usd", lambda *a, **k: pre.clip_cap)
    seen = {}

    def context(c, **kw):
        seen["usdc"] = kw["usdc_balance"]
        return SimpleNamespace(trade_size_usd=min(kw["size_cap_usd"], real_trade_size(
            usdc_balance=kw["usdc_balance"], open_notional_usd=0, max_concurrent=1,
            max_clip_usd=kw["size_cap_usd"], mode="bankroll")))
    monkeypatch.setattr(risk, "build_evm_risk_context", context)
    monkeypatch.setattr(risk, "evm_entry_allowed", lambda *a, **kw: SimpleNamespace(allowed=True))
    monkeypatch.setattr(runner, "_gate_balance_cache", {})
    monkeypatch.setattr(runner, "_gate_balance_generation", {chain: 7})
    fresh = []
    pre = runner._EntryPreReads.__new__(runner._EntryPreReads)
    pre.generation, pre.chain, pre.context, pre.clip_cap = 7, chain, {}, 25
    pre.balances = lambda: fresh.append(1) or (300.0, 0.02, False, 290.0)

    def call():
        out = runner._early_evm_quote_clip(_event(network_id=chain), pre, str(db), pre.context, chain)
        return out, seen, fresh
    return call, pre


def _cache(quote, native=0.02, age_s=1.0):
    runner._gate_balance_cache[CHAIN] = {"quote": (quote, time.monotonic() - age_s),
                                         "native": (native, time.monotonic() - age_s)}


def test_a_cached_balance_of_this_generation_sizes_without_waiting_for_the_read(monkeypatch, sizing):
    call, _pre = sizing
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", True, raising=False)
    _cache(120.0)
    (clip, refusal, facts), seen, fresh = call()
    assert (clip, refusal, facts["balance_source"]) == (25.0, None, "cache")
    assert seen["usdc"] == 120.0 and fresh == [], "the fresh read was never awaited"


@pytest.mark.parametrize("sizing", ["56"], indirect=True)
def test_bsc_uses_its_existing_fresh_preread_so_the_early_clip_matches_the_gate(monkeypatch, sizing):
    call, pre = sizing
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", True)
    runner._gate_balance_cache["56"] = {
        "quote": (121.3, time.monotonic()), "native": (0.1711082791625889, time.monotonic())}
    pre.clip_cap = 175
    pre.balances = lambda: (121.32330330034091, 0.1711082791625889, False, 20.397282)
    pre.context["early_quote_balances_fn"] = lambda _price: pytest.fail("no speculative snapshot")
    trace, quoted = Marks(), []
    early = EarlyEvmQuote(mint="coin", trace=trace, sizing_fn=lambda: call()[0],
                          quote_fn=lambda _mint, clip: quoted.append(clip) or {"quote": {"buyAmount": "123"}})
    early.worker.join(2)
    assert quoted == [121.32]
    assert early.take("coin", 121.32)["quote"]["buyAmount"] == "123"
    assert trace.last("evm.early_quote")["balance_source"] == "fresh"


@pytest.mark.parametrize("sizing", ["56"], indirect=True)
@pytest.mark.parametrize("balances,stale,reason", [
    (None, False, "balance_unavailable"), ((None, 0.02), False, "balance_unavailable"),
    ((0.0, 0.02), False, "no_clip"),
    ((121.32, 0.02), True, "balance_stale_generation")])
def test_bsc_never_uses_cached_sizing_when_the_existing_preread_is_unusable(
        monkeypatch, sizing, balances, stale, reason):
    call, pre = sizing
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", True)
    runner._gate_balance_cache["56"] = {
        "quote": (121.3, time.monotonic()), "native": (0.02, time.monotonic())}
    pre.balances = lambda: balances
    if stale:
        runner._gate_balance_generation["56"] = 8
    early = EarlyEvmQuote(mint="coin", trace=None, sizing_fn=lambda: call()[0],
                          quote_fn=lambda *_: pytest.fail("no quote from an unusable preread"))
    early.worker.join(2)
    assert early.error == reason and early.balance_source == "fresh"


@pytest.mark.parametrize("case", ["stale_generation", "cached_zero", "too_old", "knob_off", "no_cache"])
def test_anything_short_of_a_usable_cached_balance_is_todays_fresh_read(monkeypatch, sizing, case):
    call, pre = sizing
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", case != "knob_off", raising=False)
    if case != "no_cache":
        _cache(0.0 if case == "cached_zero" else 120.0,
               age_s=cfg.MEMECOIN_EVM_EARLY_SIZING_CACHE_S + 5 if case == "too_old" else 1.0)
    if case == "stale_generation":
        runner._gate_balance_generation[CHAIN] = 8          # one of our spends began since
    (clip, refusal, facts), seen, fresh = call()
    if case == "stale_generation":
        # the cache is never used across a spend; today's path then refuses on its stale read
        assert (clip, refusal, facts["balance_source"]) == (None, "balance_stale_generation", "fresh")
        assert fresh == [1]
        return
    assert facts["balance_source"] == "fresh" and fresh == [1] and seen["usdc"] == 300.0


# --- the worker's own outcome on the mark (ships with the knob off) ----------------------------

def test_a_sizing_refusal_is_named_on_the_mark_not_hidden_as_not_started():
    trace = Marks()
    early = EarlyEvmQuote(mint="coin", trace=trace, sizing_fn=lambda: (None, "not_approved"),
                          quote_fn=lambda *a: pytest.fail("no quote without a clip"))
    early.worker.join(1)
    assert early.take("coin", 25) is None
    mark = trace.last("evm.early_quote")
    assert mark["reason"] == "not_started" and mark["worker"] == "not_approved"
    assert mark["sizing_ms"] is not None


def test_a_clip_mismatch_discards_the_early_quote_and_names_its_balance_source():
    trace = Marks()
    early = EarlyEvmQuote(mint="coin", trace=trace,
                          sizing_fn=lambda: (25.0, None, {"balance_source": "cache"}),
                          quote_fn=lambda *a: {"quote": {"buyAmount": "123"}})
    early.worker.join(1)
    assert early.take("coin", 20.0) is None
    mark = trace.last("evm.early_quote")
    assert mark["reason"] == "clip_mismatch" and mark["balance_source"] == "cache"
    assert mark["worker"] == "done"


def test_a_matching_cached_clip_is_used():
    trace = Marks()
    early = EarlyEvmQuote(mint="coin", trace=trace,
                          sizing_fn=lambda: (25.0, None, {"balance_source": "cache"}),
                          quote_fn=lambda *a: {"quote": {"buyAmount": "123"}})
    early.worker.join(1)
    assert early.take("coin", 25.0)["quote"]["buyAmount"] == "123"
    assert trace.last("evm.early_quote")["used"] is True


# --- early token safety beside the early quote ------------------------------------------------

def test_the_worker_runs_safety_beside_the_quote_with_the_same_clip():
    calls = []

    def safety(mint, clip, deadline, timings):
        calls.append((mint, clip))
        timings["sell_quote_ms"] = 5.0
        return True, "ok"
    early = EarlyEvmQuote(mint="coin", trace=None, sizing_fn=lambda: (25.0, None),
                          quote_fn=lambda *a: {"quote": {"buyAmount": "1"}}, safety_fn=safety)
    early.worker.join(1)
    got = early.safety_for("coin", 25.0)
    assert got is not None and got.wait() == (True, "ok") and got.timings["sell_quote_ms"] == 5.0
    assert early.safety_for("coin", 20.0) is None and early.safety_for("other", 25.0) is None
    assert calls == [("coin", 25.0)]


def _leg_run(monkeypatch, tmp_path, early_verdict, *, early_usd=10.0, fresh=(True, "fresh_ok")):
    from test_memecoin_evm_leg import _c, _Ev
    c, kill = _c(monkeypatch, tmp_path)
    fresh_calls = []

    def safety_fn(mint, *, tax_cap=None):
        fresh_calls.append(mint)
        return fresh

    class EarlySafety:
        timings, duration_ms = {"sell_quote_ms": 7.0}, 12.0

        def wait(self):
            return early_verdict

    class Early:
        trace = None

        def safety_for(self, mint, usd):
            return EarlySafety() if usd == early_usd else None

        def take(self, *a, **k):
            return None

    def execute_fn(conn, pid, mint, size, now_iso, safety_gate_fn=None, early_quote=None):
        safe, reason = safety_gate_fn()
        if not safe:
            from sniper.persistence import db_memecoin as mm
            mm.release_pending(conn, pid, "token_safety")
            return {"status": "unsafe_gate", "reason": reason}
        return {"status": "filled", "position_id": pid}
    trace = Marks()
    trace.mark = lambda *a, **k: None
    r = leg.evm_real_entry(c, _Ev(), route="evm:8453", day="2026-07-09", usdc_balance=300.0,
                           native_balance=1.0, now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
                           token_safety_fn=safety_fn, execute_fn=execute_fn, kill_path=kill,
                           early_quote=Early(), trace=trace)
    return r, fresh_calls, trace


def test_the_leg_reuses_an_early_pass_bound_to_its_usd(monkeypatch, tmp_path):
    r, fresh_calls, trace = _leg_run(monkeypatch, tmp_path, (True, "early_ok"))
    assert r["status"] == "filled" and fresh_calls == []
    mark = trace.last("evm.token_safety")
    assert mark["reason"] == "early_ok" and mark["early"] is True


@pytest.mark.parametrize("verdict", [(False, "sell tax 40%"), None], ids=["refusal", "no_verdict"])
def test_an_early_refusal_or_no_verdict_is_re_run_fresh(monkeypatch, tmp_path, verdict):
    r, fresh_calls, trace = _leg_run(monkeypatch, tmp_path, verdict)
    assert r["status"] == "filled" and fresh_calls == ["0xtok"]
    assert trace.last("evm.token_safety")["reason"] == "fresh_ok"


def test_an_early_safety_for_another_usd_is_never_used(monkeypatch, tmp_path):
    r, fresh_calls, _trace = _leg_run(monkeypatch, tmp_path, (True, "early_ok"), early_usd=25.0)
    assert fresh_calls == ["0xtok"]


@pytest.mark.parametrize('invalidator_chain,own', [(CHAIN, True), (CHAIN, False), ('1', False)])
def test_shared_balance_paused_then_invalidation_has_typed_generation_evidence(monkeypatch, sizing, invalidator_chain, own):
    call, pre = sizing
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_EARLY_CACHED_SIZING', False)
    got_tuple, resume = threading.Event(), threading.Event()
    def balances():
        result = (300., .02, False, 1.)
        got_tuple.set()
        assert resume.wait(2)
        return result
    pre.balances = balances
    trace = Marks()
    early = EarlyEvmQuote(mint='coin', trace=trace, sizing_fn=lambda: call()[0],
                          quote_fn=lambda *a: {'quote': {'buyAmount': '123'}})
    try:
        assert got_tuple.wait(1)
        generation, stamp = runner._gate_balance_invalidate(invalidator_chain)
        if own:
            early.note_invalidation(invalidator_chain, generation, stamp)
        resume.set()
        early.worker.join(2)
        result = early.take('coin',25.)
        mark = trace.last('evm.early_quote')
        if invalidator_chain == CHAIN:
            assert result is None and mark['worker'] == 'balance_stale_generation'
            assert mark['sizing_facts']['pre_generation'] == 7
            assert mark['sizing_facts']['current_generation'] == 8
        else:
            assert result['quote']['buyAmount'] == '123'
        assert mark['sizing_done_at'] is not None
        assert mark['own_invalidation_generation'] == (8 if own else None)
    finally:
        resume.set()
        early.worker.join(2)


@pytest.mark.parametrize('balance,reason', [(None,'balance_unavailable'), ((None,.02,False,1.),'balance_unavailable'), ((0.,.02,False,1.),'no_clip')])
def test_missing_balance_and_real_zero_are_not_stale_generation(monkeypatch,sizing,balance,reason):
    call,pre=sizing
    monkeypatch.setattr(cfg,'MEMECOIN_EVM_EARLY_CACHED_SIZING',False)
    pre.balances=lambda:balance
    trace=Marks()
    early=EarlyEvmQuote(mint='coin',trace=trace,sizing_fn=lambda:call()[0],quote_fn=lambda *a:pytest.fail('invalid clip'))
    early.worker.join(2)
    assert early.take('coin',25) is None
    assert trace.last('evm.early_quote')['worker']==reason


def test_generation_telemetry_failure_does_not_change_quote_reuse():
    class BrokenTrace:
        def mark_substage(self,*a,**k):
            raise RuntimeError('telemetry unavailable')
    early=EarlyEvmQuote(mint='coin',trace=BrokenTrace(),sizing_fn=lambda:(25.,None,{'pre_generation':7,'current_generation':7}),
                        quote_fn=lambda *a:{'quote':{'buyAmount':'123'}})
    early.worker.join(1)
    early.note_invalidation(CHAIN,8,time.monotonic())
    assert early.take('coin',25.)['quote']['buyAmount']=='123'
    assert early.take('coin',25.) is None


@pytest.mark.parametrize('mint,advance,reason', [('other',0.,'mint_mismatch'),('coin',4.,'stale')])
def test_generation_diagnostics_preserve_mint_and_age_fences(mint,advance,reason):
    now=[100.]
    trace=Marks()
    early=EarlyEvmQuote(mint='coin',trace=trace,clock=lambda:now[0],sizing_fn=lambda:(25.,None),
                        quote_fn=lambda *a:{'quote':{'buyAmount':'123'}})
    early.worker.join(1)
    now[0]+=advance
    early.note_invalidation(CHAIN,8,now[0])
    assert early.take(mint,25.) is None
    assert trace.last('evm.early_quote')['reason']==reason


def test_generation_diagnostics_preserve_original_ladder_deadline_and_late_result():
    began,release=threading.Event(),threading.Event()
    def quote(mint,clip,*,on_started):
        assert on_started(time.monotonic(),.02,{'mint':mint,'clip':clip})
        began.set()
        assert release.wait(2)
        return {'quote':{'buyAmount':'123'}}
    quote.early_deadline=True
    early=EarlyEvmQuote(mint='coin',trace=Marks(),sizing_fn=lambda:(25.,None),quote_fn=quote)
    try:
        assert began.wait(1)
        early.note_invalidation(CHAIN,8,time.monotonic())
        result=early.take('coin',25.)
        assert result['quote'] is None and result['rungs'][0]['blind_reason']=='ladder_deadline'
        deadline=early.deadline
        release.set()
        early.worker.join(1)
        assert early.deadline==deadline and early.take('coin',25.) is None
    finally:
        release.set()
        early.worker.join(2)


@pytest.mark.parametrize('quote,reason', [(None,'no_quote'),({'quote':{'buyAmount':'0'}},None)])
def test_missing_quote_and_known_zero_do_not_become_generation_failure(quote,reason):
    trace=Marks()
    early=EarlyEvmQuote(mint='coin',trace=trace,sizing_fn=lambda:(25.,None),quote_fn=lambda *a:quote)
    early.worker.join(1)
    assert early.take('coin',25.)==quote
    assert trace.last('evm.early_quote')['reason']==reason


def test_invalidation_clock_failure_still_invalidates_the_real_generation(monkeypatch):
    monkeypatch.setattr(runner,'_gate_balance_cache',{CHAIN:{'quote':(120.,0.)}})
    monkeypatch.setattr(runner,'_gate_balance_generation',{CHAIN:7})
    def broken():
        raise RuntimeError('timestamp unavailable')
    monkeypatch.setattr(runner.time,'monotonic',broken)
    assert runner._gate_balance_invalidate(CHAIN)==(8,None)
    assert CHAIN not in runner._gate_balance_cache
    assert runner._gate_balance_generation[CHAIN]==8
