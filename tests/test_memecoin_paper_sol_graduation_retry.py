"""C03 (engineering review 2026-10-08): one early pool miss must not strand a graduating paper curve.

resolve_pass consumed the completed-curve set, marked the binding 'graduating' and re-resolved
once; a miss left it 'graduating' forever, because the next pass saw a binding and skipped it.
Now a pending graduation retries from the resolver loop every GRAD_RETRY_EVERY_S until
GRAD_RETRY_FOR_S after the curve completed, then ends in the explicit terminal state
'graduation_unresolved'. A miss ('not_found') and a failed read ('unreadable') stay apart.
"""
from sniper.services import memecoin_paper_sol_ticks as SOL
from sniper.services import memecoin_slot_graduation as grad
from test_memecoin_paper_sol_ticks import MINT, ORIGIN, SIG, T0, _Rpc, _resolver, _src

CANON = "PumpSwapPool11111111111111111111111111111"


class _Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _graduating(monkeypatch, answers, wanted=((1, ORIGIN),)):
    """A source bound on CurveA for each wanted pid, then the curve completes. `answers` scripts the
    canonical-pool resolves: a pool string, None (not found) or an Exception (unreadable)."""
    monkeypatch.setattr(grad, "canonical_pool", lambda mint, quote_mint=None: CANON)
    _resolver(monkeypatch, {SIG: "CurveA"})
    mono = _Mono()
    src = _src(_Rpc(), mono=mono)
    src.wanted = [(pid, MINT, "CurveA", origin) for pid, origin in wanted]
    src.resolve_pass()
    calls = []
    script = list(answers)

    def resolve(mint, pool, origin):
        calls.append(pool)
        got = script.pop(0) if script else None
        if isinstance(got, Exception):
            raise got
        return (None, None) if got is None else ("entry_route", (got, "cv_" + got, "So111", "qv_" + got))
    monkeypatch.setattr(src, "_resolve", resolve)
    src._graduated(src._bindings[wanted[0][0]]["quote_vault"])
    return src, mono, calls


def test_a_first_miss_then_a_hit_rebinds_on_a_later_pass(monkeypatch):
    src, mono, calls = _graduating(monkeypatch, [None, CANON])
    src.resolve_pass()
    assert src.binding(1)["binding"] == "graduating" and src._bindings[1]["grad_last"] == "not_found"
    src.resolve_pass()                                   # inside the retry interval: no read
    assert len(calls) == 1
    mono.t += SOL.GRAD_RETRY_EVERY_S
    src.resolve_pass()
    assert src.binding(1)["binding"] == "graduated" and src.binding(1)["pool"] == CANON
    assert calls == [CANON, CANON]


def test_an_unreadable_read_then_a_hit_rebinds_and_is_named_apart_from_a_miss(monkeypatch):
    src, mono, calls = _graduating(monkeypatch, [TimeoutError("rpc"), CANON])
    src.resolve_pass()                                   # the raise must not escape the pass
    assert src._bindings[1]["grad_last"] == "unreadable"
    mono.t += SOL.GRAD_RETRY_EVERY_S
    src.resolve_pass()
    assert src.binding(1)["binding"] == "graduated"


def test_a_pool_absent_for_the_whole_budget_ends_unresolved_and_stops_reading(monkeypatch):
    src, mono, calls = _graduating(monkeypatch, [])
    src.resolve_pass()
    while mono.t < 1000.0 + SOL.GRAD_RETRY_FOR_S + SOL.GRAD_RETRY_EVERY_S:
        mono.t += SOL.GRAD_RETRY_EVERY_S
        src.resolve_pass()
    assert src.binding(1)["binding"] == "graduation_unresolved"
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "graduation_unresolved")
    reads = len(calls)
    mono.t += 10 * SOL.GRAD_RETRY_EVERY_S
    src.resolve_pass()
    assert len(calls) == reads <= SOL.GRAD_RETRY_FOR_S / SOL.GRAD_RETRY_EVERY_S + 2


def test_a_position_closed_while_its_graduation_is_pending_is_dropped(monkeypatch):
    src, mono, calls = _graduating(monkeypatch, [None])
    src.resolve_pass()
    src.wanted = []
    mono.t += SOL.GRAD_RETRY_EVERY_S
    src.resolve_pass()
    assert 1 not in src._bindings and len(calls) == 1


def test_two_positions_on_one_graduating_curve_both_rebind(monkeypatch):
    src, mono, calls = _graduating(monkeypatch, [None, None, CANON, CANON],
                                   wanted=((1, ORIGIN), (2, ORIGIN)))
    src.resolve_pass()
    mono.t += SOL.GRAD_RETRY_EVERY_S
    src.resolve_pass()
    assert src.binding(1)["pool"] == CANON and src.binding(2)["pool"] == CANON


def test_a_rate_limited_pool_read_is_unreadable_and_keeps_that_cause_when_it_ends(monkeypatch):
    """Gamma on C03: _resolve swallowed a getSignaturesForAddress failure as an empty pool, so a
    429 and a pool with no swaps yet both read 'not_found'. The real _resolve now says so."""
    monkeypatch.setattr(grad, "canonical_pool", lambda mint, quote_mint=None: CANON)
    _resolver(monkeypatch, {SIG: "CurveA"})
    mono = _Mono()
    src = _src(_Rpc(), mono=mono)
    src.wanted = [(1, MINT, "CurveA", ORIGIN)]
    src.resolve_pass()

    def limited(method, params):
        raise RuntimeError("HTTP 429 Too Many Requests")
    src._rpc_call = limited
    src._graduated(src._bindings[1]["quote_vault"])
    src.resolve_pass()
    assert src._bindings[1]["grad_last"] == "unreadable"
    while mono.t < 1000.0 + SOL.GRAD_RETRY_FOR_S + SOL.GRAD_RETRY_EVERY_S:
        mono.t += SOL.GRAD_RETRY_EVERY_S
        src.resolve_pass()
    assert src._bindings[1]["binding"] == "graduation_unresolved"
    assert src._bindings[1]["grad_last"] == "unreadable"


def test_an_empty_pool_page_is_not_found_not_unreadable(monkeypatch):
    monkeypatch.setattr(grad, "canonical_pool", lambda mint, quote_mint=None: CANON)
    _resolver(monkeypatch, {SIG: "CurveA"})
    src = _src(_Rpc(), mono=_Mono())
    src.wanted = [(1, MINT, "CurveA", ORIGIN)]
    src.resolve_pass()
    src._graduated(src._bindings[1]["quote_vault"])
    src.resolve_pass()
    assert src._bindings[1]["grad_last"] == "not_found"


def test_listed_signatures_whose_tx_reads_fail_are_unreadable(monkeypatch):
    """Gamma on C03: getSignaturesForAddress answers, every getTransaction 429s. get_transaction_fn
    logs and returns None, so the miss must still read 'unreadable', not 'not_found'."""
    monkeypatch.setattr(grad, "canonical_pool", lambda mint, quote_mint=None: CANON)
    _resolver(monkeypatch, {SIG: "CurveA"})
    src = _src(_Rpc(), mono=_Mono())
    src.wanted = [(1, MINT, "CurveA", ORIGIN)]
    src.resolve_pass()

    def tx_limited(method, params):
        if method == "getSignaturesForAddress":
            return [{"signature": "g%d" % i} for i in range(3)]
        raise RuntimeError("HTTP 429 Too Many Requests")
    src._rpc_call = tx_limited
    src._graduated(src._bindings[1]["quote_vault"])
    src.resolve_pass()
    assert src._bindings[1]["grad_last"] == "unreadable"
