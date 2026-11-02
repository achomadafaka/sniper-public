"""Codex audit #12 (35948d32..d7ef0f2c): `venue_quote_with_v2_fallback` emits a rung with
`blind_reason="ladder_deadline"` (evm_real_seams.py ~741) when the whole quote ladder times out
waiting on `as_completed`. `classify_quote_miss` turns that into `code="provider_ladder_deadline"`
(the `"provider_%s" % blind_reason` prefix rule). Two downstream lookups did not know that exact
string:

  1. the exit-preview failure map inside `preview_exit_fn` (evm_real_seams.py ~2622-2647) --
     `.get(code, QuoteFailure.MALFORMED_RESPONSE)` fell through to MALFORMED_RESPONSE, which reads
     as "the provider answered garbage" instead of "the provider never answered in time".
  2. the immediate entry-retry code set in `memecoin_real_runner.py` (~1563-1580) -- a ladder
     timeout on entry got no in-process re-quote, unlike every other typed provider miss.

Both should treat a ladder timeout as the existing PROVIDER_TIMEOUT class; the raw detail text
(naming the rung and "ladder_deadline") is untouched.

This drives the REAL ladder through `build_real_evm_executor`'s `preview_exit_fn`, not
`classify_quote_miss` with a hand-built rung list, so it fails if the map (not the classifier)
regresses. `MEMECOIN_EVM_LADDER_WAIT_S` is shrunk to force the deadline to its 0.5s floor
(`max(0.5, ...)` in the ladder), and the injected `venue_quote_fn` ("v3", the first rung) sleeps
past that floor so the ladder genuinely times out waiting on it -- the other configured rungs (v2,
v4) answer within the floor on their own real (mocked-http) misses, so "v3" is the rung that
carries `blind_reason="ladder_deadline"` and, being first in `classify_quote_miss`'s priority
order, is the one that decides the code.
"""
import time

from eth_account import Account

from sniper.execution import evm_real_seams as S


def test_a_ladder_deadline_on_exit_preview_is_a_provider_timeout_not_malformed(monkeypatch):
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.execution import evm_wallet
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LADDER_WAIT_S", 0.05)   # floors to 0.5s in the ladder
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 987)

    def slow_quote(*_a, **_k):
        time.sleep(0.8)
        return {"buyAmount": "1"}

    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="8453", rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {}, None),
        venue_quote_fn=slow_quote, usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0,
    )

    result = ex["preview_exit_fn"]({"mint": "0xtok"})

    assert result.failure == QuoteFailure.PROVIDER_TIMEOUT, (
        "a ladder that timed out waiting on every rung is an infrastructure timeout, "
        "not a malformed response — got %r" % (result.failure,))


def _immediate_retry_codes():
    """The literal `quote_miss_code` set the immediate entry-retry checks (memecoin_real_runner.py
    ~1578), parsed from the real AST rather than the source text -- a comment mentioning a code is
    not membership."""
    import ast
    import inspect
    from sniper.services import memecoin_real_runner as R
    tree = ast.parse(inspect.getsource(R))
    for node in ast.walk(tree):
        if isinstance(node, ast.Set):
            values = {e.value for e in node.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            if "provider_ladder_deadline" in values:
                return values
    raise AssertionError("the immediate-retry code set was not found")


def test_provider_search_limit_gets_the_immediate_retry_no_route_does_not():
    """Codex EX3, 2026-09-20 (decided by the operator's delegate after a measurement pass, not an
    incident: provider_search_limit fired zero times in 51,023 recorded entry attempts back to
    2026-07-15). Item 2 this file's own docstring names but never tested: the immediate-retry set
    omitted provider_search_limit -- the code two producers share, evm_v4_multihop's own
    deadline/request budget (E4) and an unreadable decision block (EX1) -- while the scheduled
    retry lane already retried it through its "no quote" prefix. Both lanes must now agree.

    The negative half is the point: this set exists specifically to exclude no_route, a rung that
    PRICED a route and answered. Widening the membership check alone would not catch a future edit
    that accidentally added no_route back in; asserting its absence does.
    """
    codes = _immediate_retry_codes()
    assert "provider_search_limit" in codes
    assert "no_route" not in codes, "no_route is an answer, not a provider miss -- it must stay out"
