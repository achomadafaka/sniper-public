"""POSITIVE CONTROL: a rate-limited paper quote must actually land a `rate_limited` label.

`memecoin_entry_snapshots` holds ZERO `rate_limited` labels in 19,047 all-time attempts — including
the hour the runner ran 40% rate-limited. A counter that has never counted cannot tell 0% from 40%,
so its zero is not evidence. Gamma's registration therefore reads the UPPER BOUND R+U rather than
trusting the label, and this test exists so the label can eventually be trusted on its own.

Same discipline as the vol_death guard: show the thing can FIRE, not just that it is wired.
"""
import pytest

from sniper.services import memecoin_quotes as V


class _Resp429(Exception):
    code = 429


def _http_all_429(method, url, headers=None):
    raise _Resp429("429 Too Many Requests")


def _http_status_429(method, url, headers=None):
    return 429, {"error": "rate limited"}, None


def test_a_raised_429_is_labelled_rate_limited_not_provider_error():
    """The exception form. `lite-api` bans by IP and raises rather than returning a status."""
    payload, reason, host = V.jupiter_quote_detailed(
        V.USDC_MINT, "MINT", 1_000_000, 300, http=_http_all_429)
    assert payload is None
    assert reason == V.QUOTE_RATE_LIMITED, f"a 429 reported as {reason!r}"
    assert host, "the host must travel with the reason or no per-tier baseline is possible"


def test_a_429_STATUS_is_labelled_rate_limited_too():
    """The non-raising form. Both must reach the same label — a 429 that arrives as a status and a
    429 that arrives as an exception are the same fact about capacity."""
    payload, reason, host = V.jupiter_quote_detailed(
        V.USDC_MINT, "MINT", 1_000_000, 300, http=_http_status_429)
    assert payload is None and reason == V.QUOTE_RATE_LIMITED


def test_the_reason_reaches_the_exception_the_paper_lane_reads():
    """`executable_entry_fill` reads `exc.quote_reason`/`exc.quote_host`. If `quote_entry` does not
    attach them the label degrades to `quote_unavailable`, which is the 4,976-row bucket."""
    from types import SimpleNamespace
    with pytest.raises(Exception) as ei:
        V.quote_entry(SimpleNamespace(mint="MINT", network_id="solana"), 100.0,
                      solana_rpc_url="http://rpc.invalid",
                      jupiter_entry_fn=V.jupiter_entry_quote, http=_http_all_429)
    exc = ei.value
    assert getattr(exc, "quote_reason", None) == V.QUOTE_RATE_LIMITED, (
        f"quote_reason={getattr(exc, 'quote_reason', None)!r} — the label cannot reach the snapshot")


def test_the_label_the_snapshot_would_carry():
    """End of the chain: `executable_entry_fill` turns reason+host into `reason@host`, and the
    caller writes `skipped_quote_<that>`. This is the exact string a baseline read greps for."""
    from sniper.services.memecoin_copy_trader import executable_entry_fill

    class _Boom(Exception):
        quote_reason = V.QUOTE_RATE_LIMITED
        quote_host = "api.jup.ag"

    import sniper.services.memecoin_quotes as mod
    real = mod.quote_entry
    try:
        mod.quote_entry = lambda *a, **k: (_ for _ in ()).throw(_Boom())
        price, source = executable_entry_fill("MINT", "solana", 100.0)
    finally:
        mod.quote_entry = real
    assert price is None
    assert source == "rate_limited@api.jup.ag"
    assert ("skipped_quote_%s" % source) == "skipped_quote_rate_limited@api.jup.ag"


def test_a_no_route_is_NOT_labelled_rate_limited():
    """NEGATIVE CONTROL. An honest empty route and a capacity refusal must never collapse — that
    collapse is what made 95% of buy events read as unpriceable coins."""
    def _empty(method, url, headers=None):
        return 200, {"routePlan": []}, None
    _payload, reason, _host = V.jupiter_quote_detailed(
        V.USDC_MINT, "MINT", 1_000_000, 300, http=_empty)
    assert reason != V.QUOTE_RATE_LIMITED


def test_an_unclassified_failure_still_names_its_exception_type():
    """The 5,002-row bucket exists because one raise path attaches a reason and the others do not.
    The exception type was already computed for a log line the runner never emits, so recording it
    costs nothing and makes the bucket decomposable from here."""
    from sniper.services.memecoin_copy_trader import executable_entry_fill
    import sniper.services.memecoin_quotes as mod

    class _Weird(RuntimeError):
        pass

    real = mod.quote_entry
    try:
        mod.quote_entry = lambda *a, **k: (_ for _ in ()).throw(_Weird("boom"))
        price, source = executable_entry_fill("MINT", "solana", 100.0)
    finally:
        mod.quote_entry = real
    assert price is None
    assert source == "quote_unavailable__Weird", source


def test_a_CLASSIFIED_failure_is_unchanged_by_that():
    """The type suffix must not displace a reason Jupiter actually gave us."""
    from sniper.services.memecoin_copy_trader import executable_entry_fill
    import sniper.services.memecoin_quotes as mod

    class _Boom(Exception):
        quote_reason = V.QUOTE_RATE_LIMITED
        quote_host = "api.jup.ag"

    real = mod.quote_entry
    try:
        mod.quote_entry = lambda *a, **k: (_ for _ in ()).throw(_Boom())
        _price, source = executable_entry_fill("MINT", "solana", 100.0)
    finally:
        mod.quote_entry = real
    assert source == "rate_limited@api.jup.ag"
