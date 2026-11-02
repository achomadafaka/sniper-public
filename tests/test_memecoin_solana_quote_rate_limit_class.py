"""A raised HTTP 429 is a RATE_LIMIT, not retryable transport.

`_http` uses `urllib.request.urlopen`, which RAISES `HTTPError` on 4xx/5xx and never returns a
`(429, ...)` tuple. Both Solana quote classifiers checked `status == 429` only on the tuple path,
so that branch was dead for the live transport and every Jupiter rate limit typed as
`RPC_TRANSPORT` — which is in the exit path's `retryable` set.

That inverts the quote-infra contract: "retry only rpc_transport/provider_timeout once after
200 ms ... never retry signing, submission, RATE LIMITS, malformed data, or token failures". We
retried into the throttle.

Live 2026-08-19: funded position 40653 (RWA, entry $39.28) went rails-blind for 21 sweeps while
lite-api.jup.ag returned 429 with a PLAIN-TEXT body. A manual read needed four backoff attempts
before it quoted; the exact whole-balance value was $30.33 (-22.8%) against a nominal mid of
-22.7%, so the rails were blind but not materially misled.

Only 429 is reclassified. 5xx and codeless transport errors keep RPC_TRANSPORT, which is already
correct — a genuine transport blip SHOULD get its one retry.
"""
import urllib.error

import pytest

from sniper.analysis.memecoin_quote_failures import QuoteFailure, is_infra
from sniper.execution import solana_real_seams as S


MINT = "nAPC5renY7b7HEQgKNe4eBZaX3npy33W2yvBJ144UMS"
AMOUNT = 746957876483
RETRYABLE = {QuoteFailure.RPC_TRANSPORT, QuoteFailure.PROVIDER_TIMEOUT}


def _raises(exc):
    def http(_method, _url, **_kw):
        raise exc
    return http


@pytest.mark.parametrize("exc,expected", [
    (urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None), QuoteFailure.RATE_LIMIT),
    (urllib.error.HTTPError("u", 500, "Internal Server Error", {}, None), QuoteFailure.RPC_TRANSPORT),
    (urllib.error.HTTPError("u", 503, "Unavailable", {}, None), QuoteFailure.RPC_TRANSPORT),
    (urllib.error.HTTPError("u", 400, "Bad Request", {}, None), QuoteFailure.RPC_TRANSPORT),
    (urllib.error.URLError("connection reset"), QuoteFailure.RPC_TRANSPORT),
    (TimeoutError(), QuoteFailure.PROVIDER_TIMEOUT),
])
def test_a_raised_http_error_is_classified_by_its_code(exc, expected):
    assert S._typed_jupiter_exit_quote(MINT, AMOUNT, 3.0, http=_raises(exc))[1] is expected


def test_a_rate_limit_is_never_retried():
    """The whole point. RPC_TRANSPORT gets one retry; a 429 must not."""
    failure = S._typed_jupiter_exit_quote(
        MINT, AMOUNT, 3.0,
        http=_raises(urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)))[1]
    assert failure not in RETRYABLE


def test_a_rate_limit_still_never_condemns_the_token():
    """RATE_LIMIT stays INFRA, so it advances the ops streak and never `exact_quote_misses` —
    the alert correctly reported 'token unsellable counters unchanged'."""
    assert is_infra(QuoteFailure.RATE_LIMIT)


def test_a_genuine_transport_blip_keeps_its_retry():
    """Guard against over-correcting: only 429 changes class."""
    assert S._typed_jupiter_exit_quote(
        MINT, AMOUNT, 3.0, http=_raises(urllib.error.URLError("reset")))[1] in RETRYABLE


@pytest.mark.parametrize("payload", ["Rate limit exceeded, please try again later.", {}, None])
def test_the_tuple_path_is_unchanged(payload):
    """A transport that RETURNS a 429 tuple (plain-text body included) was already right."""
    assert S._typed_jupiter_exit_quote(
        MINT, AMOUNT, 3.0, http=lambda *_a, **_k: (429, payload, None))[1] is QuoteFailure.RATE_LIMIT


def test_the_balance_boundary_classifier_agrees():
    """`_infra_failure` serves the balance/Jupiter boundary the QUOTE-INFRA alert names; it had
    the identical dead branch."""
    rate = S._infra_failure(
        {"exception": urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)})
    blip = S._infra_failure({"exception": urllib.error.URLError("reset")})
    assert rate is QuoteFailure.RATE_LIMIT and rate not in RETRYABLE
    assert blip is QuoteFailure.RPC_TRANSPORT


def test_a_codeless_exception_does_not_crash_the_classifier():
    class Weird(Exception):
        code = "not-an-int"
    assert S._infra_failure({"exception": Weird()}) is QuoteFailure.RPC_TRANSPORT
