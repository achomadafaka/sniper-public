"""An oversized signed swap is re-quoted once with a capped account count.

A Solana packet is capped at 1232 bytes. Three funded refusals on 2026-08-23 measured 1346, 1248
and 1259 bytes; two of the three missed by 16 and 27. Across 8 oversized payloads since 2026-08-10
the overshoot was +3, +11, +16, +21, +114, +121, +124 and +239 bytes, so a saving of 31 to 206
bytes (measured for maxAccounts=48 on three live routes) recovers roughly half of them.

The retry must run BEFORE any write-ahead: bind_entry_submission refuses a second, different
payload for the same reservation, so a retry after the bind would be a fail-closed dead end.
"""
import base64

from sniper.execution import solana_swap_executor as X
from sniper.execution.submission_wiring import SOLANA_PACKET_LIMIT


def _payload(nbytes):
    return base64.b64encode(b"\x01" * nbytes).decode()


class _Harness:
    """Minimal seam set. The first quote builds an oversized tx; the capped re-quote fits."""

    def __init__(self, first_bytes, retry_bytes):
        self.first_bytes, self.retry_bytes = first_bytes, retry_bytes
        self.quote_calls, self.sent, self.bound = [], [], []

    def quote_fn(self, mint, usd, max_accounts=None):
        self.quote_calls.append(max_accounts)
        return {"outAmount": "1000", "otherAmountThreshold": "900",
                "priceImpactPct": "0.01", "_max_accounts": max_accounts}

    def reverse_quote_fn(self, mint, atomic):
        # usd_back = (outAmount / 1e6) * (expected / planned) = 27.0 * (1000/900) = $30.00 on a
        # $30 buy, i.e. a 0% round trip. The cap restored on 2026-08-23 is 30%, so a careless
        # fixture aborts at `roundtrip_cap` long before the sign step this test is about.
        return {"inAmount": str(atomic), "outAmount": "27000000",
                "otherAmountThreshold": "26000000"}

    def sign_fn(self, quote):
        n = self.retry_bytes if quote.get("_max_accounts") else self.first_bytes
        return (_payload(n), "sig-%d" % n, {"blockhash": "bh", "last_valid_height": 1})

    def send_fn(self, signed_b64, validity=None):
        self.sent.append(len(base64.b64decode(signed_b64)))
        return "sig-ok"


def _run(h, monkeypatch, retry_accounts=48):
    import sniper.settings.memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", retry_accounts)
    calls = {"release": []}
    monkeypatch.setattr(X.m, "release_pending", lambda c, p, r: calls["release"].append(r))
    monkeypatch.setattr(X.m, "pending_row", lambda c, p: {"network_id": "solana"})
    monkeypatch.setattr(X.m, "validated_pending_funded_contract", lambda c, p: {})
    monkeypatch.setattr(X.m, "set_pending_sig", lambda *a, **k: None)
    monkeypatch.setattr(X, "_post_peak_confirmation", lambda *a, **k: None)
    res = X.execute_entry(
        None, pending_id=1, mint="M", usd_amount=30.0, strategy="s", source_user_id="u",
        now_iso="2026-08-23T00:00:00+00:00", quote_fn=h.quote_fn,
        reverse_quote_fn=h.reverse_quote_fn, sign_fn=h.sign_fn, send_fn=h.send_fn,
        confirm_fn=lambda sig: "failed", usdc_fn=lambda: 100.0,
        token_fn=lambda m_: (0.0, 6), pending={"network_id": "solana"},
        sleep_fn=lambda s: None)
    return res, calls


def test_oversized_build_triggers_one_capped_requote(monkeypatch):
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT + 27, retry_bytes=SOLANA_PACKET_LIMIT - 200)
    _run(h, monkeypatch)
    assert h.quote_calls == [None, 48]                 # exactly one retry, capped
    assert h.sent == [SOLANA_PACKET_LIMIT - 200]       # only the fitting payload was sent


def test_oversized_payload_is_never_sent(monkeypatch):
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT + 27, retry_bytes=SOLANA_PACKET_LIMIT - 200)
    _run(h, monkeypatch)
    assert all(n <= SOLANA_PACKET_LIMIT for n in h.sent), h.sent


def test_the_retry_does_not_recurse_twice(monkeypatch):
    """If the capped route is STILL oversized, the send seam's guard owns it — no second retry."""
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT + 114, retry_bytes=SOLANA_PACKET_LIMIT + 10)
    _run(h, monkeypatch)
    assert h.quote_calls == [None, 48]                 # not [None, 48, 48]


def test_a_fitting_build_never_requotes(monkeypatch):
    """The 97% of entries that already fit must send no maxAccounts key and keep today's route."""
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT - 100, retry_bytes=1)
    _run(h, monkeypatch)
    assert h.quote_calls == [None]
    assert h.sent == [SOLANA_PACKET_LIMIT - 100]


def test_zero_disables_the_retry(monkeypatch):
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT + 27, retry_bytes=1)
    _run(h, monkeypatch, retry_accounts=0)
    assert h.quote_calls == [None]


def test_both_byte_counts_are_traced(monkeypatch):
    """Route size is time-varying, so the canary needs pre AND post to tell 'maxAccounts worked'
    from 'the route happened to be smaller this time'."""
    seen = []
    monkeypatch.setattr(X, "_trace_substage",
                        lambda trace, name, **kw: seen.append((name, kw)))
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT + 27, retry_bytes=SOLANA_PACKET_LIMIT - 200)
    _run(h, monkeypatch)
    names = {n for n, _ in seen}
    assert "oversize_retry" in names and "oversize_retry_rebuild" in names
    pre = next(kw for n, kw in seen if n == "oversize_retry")
    post = next(kw for n, kw in seen if n == "oversize_retry_rebuild")
    assert pre["pre_bytes"] == SOLANA_PACKET_LIMIT + 27
    assert post["post_bytes"] == SOLANA_PACKET_LIMIT - 200
    assert post["fits"] is True


def test_stage_latency_is_attributed_to_the_retry_not_the_discarded_build(monkeypatch):
    """`mark()` is idempotent, so without the retry-aware marker the p50/p95 stage columns would
    describe attempt 1 -- the build that was thrown away -- while `transaction_submitted` silently
    absorbed the whole retry. `_REPEATABLE_SUBMISSION_STAGES` exists for exactly this."""
    marks = []
    monkeypatch.setattr(X, "trace_mark_submission",
                        lambda trace, stage, attempt_no, **kw: marks.append((stage, attempt_no)))
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT + 27, retry_bytes=SOLANA_PACKET_LIMIT - 200)
    _run(h, monkeypatch)
    assert ("quote", 1) in marks and ("signing", 1) in marks        # attempt 1 kept as history
    assert ("quote", 2) in marks and ("signing", 2) in marks        # attempt 2 owns the columns
    # the submitted stage belongs to the attempt that actually broadcast
    assert ("transaction_submitted", 2) in marks
    assert ("transaction_submitted", 1) not in marks


def test_no_retry_marks_everything_as_attempt_one(monkeypatch):
    marks = []
    monkeypatch.setattr(X, "trace_mark_submission",
                        lambda trace, stage, attempt_no, **kw: marks.append((stage, attempt_no)))
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT - 100, retry_bytes=1)
    _run(h, monkeypatch)
    assert {a for _, a in marks} == {1}


def test_an_unrelated_TypeError_from_the_quote_seam_propagates(monkeypatch):
    """Catching TypeError around a call whose body can also raise it must not swallow a real bug
    and silently re-quote uncapped, which would make the retry a no-op that still looks like it ran."""
    import pytest
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT + 27, retry_bytes=1)

    def exploding_quote(mint, usd, max_accounts=None):
        h.quote_calls.append(max_accounts)
        if max_accounts:
            raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")
        return {"outAmount": "1000", "otherAmountThreshold": "900", "priceImpactPct": "0.01"}

    h.quote_fn = exploding_quote
    with pytest.raises(TypeError, match="unsupported operand"):
        _run(h, monkeypatch)


def test_a_legacy_seam_without_the_parameter_degrades_to_one_quote(monkeypatch):
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT + 27, retry_bytes=1)

    def legacy_quote(mint, usd):                      # predates the retry: no max_accounts
        h.quote_calls.append("legacy")
        return {"outAmount": "1000", "otherAmountThreshold": "900", "priceImpactPct": "0.01"}

    h.quote_fn = legacy_quote
    _run(h, monkeypatch)
    assert h.quote_calls == ["legacy", "legacy"]      # retried, uncapped, no crash


def test_a_refused_build_says_so_instead_of_sign_failed(monkeypatch):
    """Both "sign failed" aborts on 2026-09-11 were Raydium refusing a Token-2022 buy before any
    key was used. The seam notes the cause; the executor reports it."""
    h = _Harness(first_bytes=100, retry_bytes=100)
    def refused(quote):
        return None
    refused.refusal_reason = lambda: "Raydium simulation refused the Token-2022 buy"
    h.sign_fn = refused
    res, _ = _run(h, monkeypatch)
    assert res["status"] == "aborted"
    assert res["reason"] == "route build refused: Raydium simulation refused the Token-2022 buy"
    h.sign_fn = lambda quote: None  # a seam with no note keeps the old words
    res, _ = _run(h, monkeypatch)
    assert res["reason"] == "sign failed"


def test_the_seam_note_is_cleared_per_attempt():
    from sniper.execution import solana_real_seams as S
    S._note_build_refusal("x")
    assert S.take_build_refusal() == "x"
    assert S.take_build_refusal() is None


def _route(*labels):
    return [{"percent": 100, "swapInfo": {"label": label}} for label in labels]


def test_the_retry_excludes_the_first_hop_venue(monkeypatch):
    """2026-09-14 ("human" ref 7b9a3454): USDC -> SOL through HumidiFi built 1,473 bytes and the
    maxAccounts=48 rebuild kept HumidiFi (1,442). The retry must also exclude that first hop."""
    h = _Harness(first_bytes=SOLANA_PACKET_LIMIT + 241, retry_bytes=SOLANA_PACKET_LIMIT - 108)
    seen = []

    def quote_fn(mint, usd, max_accounts=None, exclude_dexes=()):
        seen.append((max_accounts, tuple(exclude_dexes)))
        return {"outAmount": "1000", "otherAmountThreshold": "900", "priceImpactPct": "0.01",
                "_max_accounts": max_accounts, "routePlan": _route("HumidiFi", "Pump.fun")}

    h.quote_fn = quote_fn
    wrapped, forwarded = X.execute_entry, []

    def spy(*a, **kw):
        forwarded.append(kw)
        return wrapped(*a, **kw)

    monkeypatch.setattr(X, "execute_entry", spy)
    restricted, native = (lambda mint: False), (lambda mint, atomic: True)
    import sniper.settings.memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS", 48)
    monkeypatch.setattr(X.m, "release_pending", lambda c, p, r: None)
    monkeypatch.setattr(X.m, "pending_row", lambda c, p: {"network_id": "solana"})
    monkeypatch.setattr(X.m, "validated_pending_funded_contract", lambda c, p: {})
    monkeypatch.setattr(X.m, "set_pending_sig", lambda *a, **k: None)
    monkeypatch.setattr(X, "_post_peak_confirmation", lambda *a, **k: None)
    wrapped(None, pending_id=1, mint="M", usd_amount=30.0, strategy="s", source_user_id="u",
            now_iso="2026-09-14T00:00:00+00:00", quote_fn=h.quote_fn,
            reverse_quote_fn=h.reverse_quote_fn, sign_fn=h.sign_fn, send_fn=h.send_fn,
            confirm_fn=lambda sig: "failed", usdc_fn=lambda: 100.0,
            token_fn=lambda m_: (0.0, 6), pending={"network_id": "solana"},
            sleep_fn=lambda s: None, quote_asset_restricted_fn=restricted,
            native_exit_available_fn=native)
    assert seen == [(None, ()), (48, ("HumidiFi",))]
    assert h.sent == [SOLANA_PACKET_LIMIT - 108]
    # the retry re-runs the same gates with the same seams, not with the fail-closed defaults
    assert forwarded[0]["quote_asset_restricted_fn"] is restricted
    assert forwarded[0]["native_exit_available_fn"] is native


def test_the_coins_own_pool_is_never_excluded():
    ex = X._oversize_retry_exclusions
    assert ex({"routePlan": _route("HumidiFi", "Pump.fun")}) == ("HumidiFi",)
    assert ex({"routePlan": _route("HumidiFi", "Raydium CP", "Raydium Launchlab")}) == ("HumidiFi",)
    assert ex({"routePlan": _route("Raydium CP", "Raydium CP")}) == ()      # shares the coin's label
    assert ex({"routePlan": _route("Pump.fun")}) == ()                      # single hop
    assert ex({"_venue": "raydium"}) == () and ex(None) == ()               # no Jupiter route


def test_the_seam_sends_the_exclusion_to_jupiter(monkeypatch):
    from sniper.execution import solana_real_seams as S
    paths = []
    monkeypatch.setattr(S, "_jup_call", lambda method, path, http=None: (paths.append(path) or
                                                                         (200, {"outAmount": "1"})))
    S._jup_quote_status("IN", "OUT", 1, 100, None, 48, ("HumidiFi",))
    assert "maxAccounts=48" in paths[0] and "excludeDexes=HumidiFi" in paths[0]
    S._jup_quote_status("IN", "OUT", 1, 100, None)
    assert "excludeDexes" not in paths[1]
