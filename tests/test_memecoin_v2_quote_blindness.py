"""A V2 read that failed must not be recorded as "no route".

Chain 56 has never filled. Token 0x7997a69c22fb4e0e76bffe0e809c172c32e87777 was refused
`no route on v3/v2/pons_v2` at 2026-09-08T12:13:26Z. Its token/WBNB PancakeSwap V2 pair was created
TWENTY HOURS earlier, holds $30,852, and quotes 400557106031790997479723 for 0.05 WBNB from the
command line. The rung recorded `blind_reason: null` — it claimed it could see.

`_make_call_fn` returned None for a transport failure, a 429, a malformed body AND a genuine revert
alike, `_quote_path` read None as "no path", and the ladder wrote a hard `no_route` that the
re-quote lane treats as terminal. Fifth instance of the family cleared on 2026-09-08:
`("failed", None)`, `_program_error_name`, `_owned_token_account`, `_invokes_program`.

THE DISTINCTION THAT MATTERS, and it is not "did the call error":
`getAmountsOut` REVERTS when the pair does not exist. A revert is the chain ANSWERING, and must
stay a genuine no-route. Only a failure to reach the chain — transport, timeout, rate limit, a
non-revert JSON-RPC error — is blindness. Treating every error as blindness would make a coin with
no pool retry forever.

Blindness is reported through a per-call dict rather than module state: the rungs run in a
ThreadPoolExecutor, so a module-level "last read failed" flag would cross-contaminate rungs and
concurrent attempts.
"""
import pytest

from sniper.execution import evm_v2_exit_quote as Q

ROUTER = "0x" + "aa" * 20
TOKEN = "0x" + "11" * 20
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
TAKER = "0x" + "22" * 20
AMT = 10 ** 18


def _cfg():
    return {"router": ROUTER, "connectors": ()}


def test_a_revert_is_a_real_no_route_not_blindness():
    """THE GUARD AGAINST OVER-CORRECTING. No pair -> revert -> refuse, and do NOT retry."""
    blind = {}
    def call_fn(to, data):
        raise Q.V2Reverted("execution reverted: 0x")
    out = Q.dex_v2_exit_quote("56", WBNB, TOKEN, AMT, TAKER, cfg_router=ROUTER,
                              connector_tokens=(), slippage_bps=300, tax_buffer_bps=0,
                              call_fn=call_fn, blind_out=blind)
    assert out is None
    assert blind.get("reason") is None, "a revert is the chain answering, not a failed read"


@pytest.mark.parametrize("boom,reason", [
    (Q.V2ReadUnavailable("timeout"), "rpc_transport"),
    (Q.V2ReadUnavailable("429"), "rpc_rate_limit"),
    (Q.V2ReadUnavailable("{'code': 429, 'message': 'Too Many Requests'}"), "rpc_rate_limit"),
])
def test_an_unreadable_call_reports_blindness(boom, reason):
    """THE REGRESSION: this returned None and the rung called it no_route."""
    blind = {}
    def call_fn(to, data):
        raise boom
    out = Q.dex_v2_exit_quote("56", WBNB, TOKEN, AMT, TAKER, cfg_router=ROUTER,
                              connector_tokens=(), slippage_bps=300, tax_buffer_bps=0,
                              call_fn=call_fn, blind_out=blind)
    assert out is None
    assert blind.get("reason") == reason


def test_one_readable_path_beats_an_unreadable_one():
    """A real answer anywhere means we were not blind, even if another path failed to read."""
    blind = {}
    calls = {"n": 0}
    def call_fn(to, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Q.V2ReadUnavailable("timeout")
        return "0x" + format(AMT, "064x") + format(5000, "064x")
    out = Q.dex_v2_exit_quote("56", WBNB, TOKEN, AMT, TAKER, cfg_router=ROUTER,
                              connector_tokens=("0x" + "33" * 20,), slippage_bps=300,
                              tax_buffer_bps=0, call_fn=call_fn, blind_out=blind)
    assert blind.get("reason") is None


def test_blindness_is_per_call_not_module_state():
    """The rungs run in a thread pool; module state would leak between concurrent attempts."""
    import inspect
    src = inspect.getsource(Q)
    assert "blind_out" in src
    for bad in ("_LAST_READ_FAILED", "global _last", "_last_read_failed ="):
        assert bad not in src, "module-level blindness state is not thread-safe here: " + bad


def test_the_classifier_separates_revert_from_transport():
    assert Q._classify_rpc_error({"error": {"code": 3, "message": "execution reverted: 0x"}}) == "revert"
    assert Q._classify_rpc_error({"error": {"code": -32005, "message": "limit exceeded"}}) == "unavailable"
    assert Q._classify_rpc_error({"error": {"message": "header not found"}}) == "unavailable"
    assert Q._classify_rpc_error({"result": "0x00"}) is None


def test_the_v2_rung_passes_blindness_to_the_ladder():
    """Source guard: the fix is worthless if the rung still answers a bare dict."""
    import pathlib
    src = pathlib.Path("sniper/execution/evm_real_seams.py").read_text()
    i = src.index('routes.append(("v2"')
    window = src[max(0, i - 900):i + 400]
    assert "blind_out" in window, "the v2 rung does not collect blindness"
