"""A 429 and an unroutable coin must not share a label.

`_jupiter_quote` returned `None` on ANY non-200, so a rate limit and a genuinely routeless coin both
surfaced as `skipped_no_executable_quote`. 95% of curated-time buy events landed in that bucket and
were read as unpriceable coins, when they may be substantially our own keyless rate limit — the
paper lane runs on `lite-api`, which bans by IP, from the same host the funded runner quotes Jupiter
at ~162 calls per RPC call.

The split lands BEFORE the paper API key so the key's effect is attributable rather than assumed.
"""
from __future__ import annotations

import urllib.error

import pytest

from sniper.services import memecoin_quotes as q


def _http(seq):
    """Replays a scripted sequence of (status, payload) or an exception, per host tried."""
    calls = []

    def http(method, url, headers=None):
        calls.append((url.split("/")[2], (headers or {}).get("x-api-key")))
        item = seq[min(len(calls) - 1, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item
        return item[0], item[1], None

    http.calls = calls
    return http


class _JsonHTTPError(urllib.error.HTTPError):
    """An HTTPError whose body reads back, which is how a real 4xx arrives."""

    def __init__(self, code, payload):
        import io
        import json
        super().__init__("u", code, "err", {}, io.BytesIO(json.dumps(payload).encode()))


def test_no_route_is_distinguished_from_rate_limited():
    """THE REAL SHAPE. Verified live 2026-08-28: an untradable mint is HTTP 400 with
    `errorCode: TOKEN_NOT_TRADABLE`, NOT a 200 carrying an error. Classifying on status alone filed
    every genuinely unroutable coin as `provider_error` — useless in the exact direction the split
    exists to measure. An earlier version of this test used the invented 200 shape and passed."""
    http = _http([_JsonHTTPError(400, {"error": "The token X is not tradable",
                                       "errorCode": "TOKEN_NOT_TRADABLE"})])
    _p, reason, host = q.jupiter_quote_detailed("A", "B", 1000, 300, http=http)
    assert reason == q.QUOTE_NO_ROUTE, "the provider ANSWERED: that is evidence, not a failure"
    assert host
    assert len(http.calls) == 1, "no route is terminal — the second host cannot make it tradable"

    http = _http([(429, {}), (429, {})])
    _p, reason, _h = q.jupiter_quote_detailed("A", "B", 1000, 300, http=http)
    assert reason == q.QUOTE_RATE_LIMITED
    assert len(http.calls) == 2, "a 429 on the first host must try the second"


def test_a_raised_429_is_also_rate_limited():
    """urlopen RAISES on 4xx — a status-only check would miss every real rate limit."""
    err = urllib.error.HTTPError("u", 429, "too many", {}, None)
    http = _http([err, err])
    _p, reason, _h = q.jupiter_quote_detailed("A", "B", 1000, 300, http=http)
    assert reason == q.QUOTE_RATE_LIMITED


def test_a_good_quote_returns_no_reason():
    http = _http([(200, {"outAmount": "123", "inAmount": "456"})])
    payload, reason, host = q.jupiter_quote_detailed("A", "B", 1000, 300, http=http)
    assert payload and reason is q.QUOTE_OK and host


def test_the_portal_host_is_tried_first_only_when_keyed(monkeypatch):
    monkeypatch.setenv("MEMECOIN_PAPER_JUPITER_API_KEY", "")
    assert q._jupiter_hosts()[0][0] == q.JUPITER_LITE_URL, "keyless order is unchanged"
    monkeypatch.setenv("MEMECOIN_PAPER_JUPITER_API_KEY", "k")
    first, second = q._jupiter_hosts()
    assert first == (q.JUPITER_PORTAL_URL, "k"), "the key is issued for the portal tier"
    assert second[1] == "", "lite-api honours no key, so sending one there buys nothing"


def test_the_key_is_sent_as_a_header_and_only_to_the_portal(monkeypatch):
    monkeypatch.setenv("MEMECOIN_PAPER_JUPITER_API_KEY", "secret")
    http = _http([(429, {}), (200, {"outAmount": "1", "inAmount": "2"})])
    q.jupiter_quote_detailed("A", "B", 1000, 300, http=http)
    assert http.calls[0] == ("api.jup.ag", "secret")
    assert http.calls[1] == ("lite-api.jup.ag", None)


def test_it_reads_the_paper_key_never_the_funded_one(monkeypatch):
    """Sharing MEMECOIN_JUPITER_API_KEY would spend the funded quota on research traffic."""
    monkeypatch.setenv("MEMECOIN_JUPITER_API_KEY", "FUNDED")
    monkeypatch.delenv("MEMECOIN_PAPER_JUPITER_API_KEY", raising=False)
    assert q._paper_jupiter_key() == ""
    assert q._jupiter_hosts()[0][0] == q.JUPITER_LITE_URL


def test_the_legacy_shim_still_returns_payload_or_none():
    http = _http([(200, {"outAmount": "9", "inAmount": "8"})])
    assert q._jupiter_quote("A", "B", 1, 300, http=http)["outAmount"] == "9"
    http = _http([_JsonHTTPError(400, {"errorCode": "COULD_NOT_FIND_ANY_ROUTE"})])
    assert q._jupiter_quote("A", "B", 1, 300, http=http) is None


def test_a_generic_4xx_is_provider_error_not_no_route():
    """Only the provider SAYING the token is untradable counts as no_route. A 400 for a malformed
    request is our bug, and filing it as evidence about the coin would be a fabricated observation.
    """
    http = _http([_JsonHTTPError(400, {"error": "bad request"}),
                  _JsonHTTPError(400, {"error": "bad request"})])
    _p, reason, _h = q.jupiter_quote_detailed("A", "B", 1000, 300, http=http)
    assert reason == q.QUOTE_PROVIDER_ERROR
    assert len(http.calls) == 2, "a non-terminal error still tries the second host"


def test_a_failed_quote_costs_ONE_host_sequence_not_two():
    """The first version called the legacy quote and then re-ran the whole host sequence to recover
    the reason: four HTTP calls per failure (lite -> portal -> lite -> portal), doubling load in
    exactly the 429 condition the split exists to measure."""
    from types import SimpleNamespace
    calls = []

    def http(method, url, headers=None):
        calls.append(url.split("/")[2])
        raise _JsonHTTPError(429, {})

    orig = q._solana_http
    q._solana_http = http
    try:
        with pytest.raises(Exception) as ei:
            q.quote_entry(SimpleNamespace(mint="M", network_id="solana"), 20.0)
    finally:
        q._solana_http = orig
    assert len(calls) == 2, f"one failure must cost one host sequence, got {calls}"
    assert getattr(ei.value, "quote_reason", None) == q.QUOTE_RATE_LIMITED
    assert getattr(ei.value, "quote_host", None), "the host must survive to the caller"


def test_the_host_reaches_the_consumer_not_just_the_log():
    """A log line is not a record. `quote_host` was read into a local and discarded, so no per-host
    baseline was possible — the JUPITER_LOAD gap reintroduced."""
    from sniper.services import memecoin_copy_trader as ctr

    def boom(*a, **k):
        exc = RuntimeError("x")
        exc.quote_reason, exc.quote_host = "rate_limited", "api.jup.ag"
        raise exc

    import sniper.services.memecoin_quotes as mod
    orig = mod.quote_entry
    mod.quote_entry = boom
    try:
        price, source = ctr.executable_entry_fill("M", "solana", 20.0)
    finally:
        mod.quote_entry = orig
    assert price is None
    assert source == "rate_limited@api.jup.ag", "reason AND host must reach the snapshot"


def test_the_key_state_is_announced_once_per_process(monkeypatch, caplog):
    """A key set in .env but absent from THIS process reads exactly like no key: getenv returns
    None, the host order reverts to keyless, and every downstream number looks like a capacity
    result when it is a configuration miss. The runner loads .env itself, but a research script or
    a smoke harness does not, and nothing would have said so."""
    import logging
    monkeypatch.setattr(q, "_KEY_ANNOUNCED", [False])
    monkeypatch.delenv("MEMECOIN_PAPER_JUPITER_API_KEY", raising=False)
    with caplog.at_level(logging.INFO):
        q._jupiter_hosts()
        q._jupiter_hosts()
    lines = [r.getMessage() for r in caplog.records if "jupiter(paper)" in r.getMessage()]
    assert len(lines) == 1, "announced once per process, not per call"
    assert "configured=False" in lines[0] and "KEYLESS" in lines[0]


def test_the_announcement_never_logs_the_key_value(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(q, "_KEY_ANNOUNCED", [False])
    monkeypatch.setenv("MEMECOIN_PAPER_JUPITER_API_KEY", "s3cr3t-value")
    with caplog.at_level(logging.INFO):
        q._jupiter_hosts()
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "s3cr3t-value" not in joined, "the value must never reach a log"
    assert "configured=True" in joined and "portal-first" in joined
