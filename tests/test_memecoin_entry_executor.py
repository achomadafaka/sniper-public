"""Item 14 — real entry executor (DISARMED; injected seams). Verifies the money-safety invariants:
write-ahead sig BEFORE broadcast, chain-delta fill truth (not the quote), confirmed->open real
position, failed->release reservation, unknown/timeout->LEAVE reserved + NO resubmit, slippage
abort before signing. NOT integration-tested — a live smoke test must precede any arm."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.execution import solana_swap_executor as ex


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    return c, pid


def _contract_c(*, pending_hash=None, post_peak=False):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    attempt_id = "attempt-contract-sol"
    m.start_entry_attempt(
        c, attempt_id=attempt_id, decision_started_at="2026-07-09T00:00:00Z",
        network_id="solana", mint="MINT",
    )
    contract = {
        "contract_version": 1,
        "identity": {"network_id": "solana", "mint": "MINT"},
        "policy": {"policy_id": "nano_v1"},
    }
    if post_peak:
        contract["classification"] = {
            "momentum_phase": "post_peak",
            "facts": {
                "peak_price": 0.04, "display_price": 0.02,
                "runup_pct": 100.0, "giveback_pct": 50.0,
            },
        }
    payload, digest = m.canonical_funded_json(contract)
    m.set_entry_attempt_funded_contract(
        c, attempt_id, funded_contract_json=payload, funded_contract_hash=digest,
        funded_contract_version=1,
    )
    pid = m.reserve_pending(
        c, mint="MINT", source_user_id="u1", strategy="alpha_b", notional=10.0,
        reserved_at="t", network_id="solana", attempt_id=attempt_id,
        funded_contract_version=1, funded_contract_hash=pending_hash or digest,
        funded_contract_json=payload,
    )
    return c, pid, attempt_id


class _Deps:
    """Injected seams; each test tweaks one branch. Balances: USDC 100->90 (spent 10), token 0->500."""
    def __init__(self, confirm="confirmed", quote_impact=0.01, sign_ok=True):
        self.calls = []
        self._confirm = confirm
        self._impact = quote_impact
        self._sign_ok = sign_ok
        self._usdc = [100.0, 90.0]      # pre, post
        self._tok = [0.0, 500.0]
    def quote_fn(self, mint, usd):
        self.calls.append("quote")
        return {
            "inAmount": "10000000",
            "outAmount": "500",
            "otherAmountThreshold": "470",
            "priceImpactPct": self._impact,
        }
    def reverse_quote_fn(self, mint, amount_atomic):
        self.calls.append(("reverse_quote", mint, amount_atomic))
        return {
            "inAmount": str(amount_atomic),
            "outAmount": "9000000",
            "otherAmountThreshold": "8100000",
        }
    def usdc_fn(self):
        return self._usdc[min(len(self.calls) and self.calls.count("post_bal"), 1)] if False else \
               (self._usdc[1] if "sent" in self.calls else self._usdc[0])
    def token_fn(self, mint):
        return self._tok[1] if "sent" in self.calls else self._tok[0]
    def sign_fn(self, quote):
        self.calls.append("sign"); return (b"signedtx", "SIG123") if self._sign_ok else None
    def send_fn(self, signed):
        self.calls.append("send"); self.calls.append("sent"); return True
    def confirm_fn(self, sig):
        self.calls.append("confirm"); return self._confirm


def _run(c, pid, d):
    return ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-07-09T00:00:00Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn)


def test_reverse_quote_uses_entry_floor_before_signature():
    c, pid = _c()
    d = _Deps()

    result = _run(c, pid, d)

    reverse_call = ("reverse_quote", "MINT", 470)
    assert result["status"] == "filled"
    assert reverse_call in d.calls
    assert d.calls.index(reverse_call) < d.calls.index("sign")


@pytest.mark.parametrize("reverse_quote", [
    None,
    {"inAmount": "470", "outAmount": "0", "otherAmountThreshold": "1"},
    {"inAmount": "469", "outAmount": "1", "otherAmountThreshold": "1"},
    {"inAmount": "470", "outAmount": "bad", "otherAmountThreshold": "1"},
    {"inAmount": "470", "outAmount": "1", "otherAmountThreshold": "0"},
])
def test_invalid_reverse_route_releases_before_balance_or_signature(reverse_quote):
    c, pid = _c()
    d = _Deps()
    d.reverse_quote_fn = lambda mint, amount: reverse_quote
    d.usdc_fn = lambda: pytest.fail("balance read must not run")
    d.token_fn = lambda mint: pytest.fail("balance read must not run")

    result = _run(c, pid, d)

    assert result["status"] == "route_failed"
    assert result["reason"] == "no planned-quantity sell route"
    assert "sign" not in d.calls
    assert m.pending_totals(c) == (0, 0.0)


def test_reverse_route_exception_fails_closed_before_balance_or_signature():
    c, pid = _c()
    d = _Deps()

    def failed_quote(mint, amount):
        raise TimeoutError("provider timeout")

    d.reverse_quote_fn = failed_quote
    d.usdc_fn = lambda: pytest.fail("balance read must not run")
    d.token_fn = lambda mint: pytest.fail("balance read must not run")

    result = _run(c, pid, d)

    assert result["status"] == "route_failed"
    assert "sign" not in d.calls
    assert m.pending_totals(c) == (0, 0.0)


def test_confirmed_opens_real_position_priced_from_chain_delta():
    c, pid = _c(); d = _Deps(confirm="confirmed")
    r = _run(c, pid, d)
    assert r["status"] == "filled"
    pos = m.get_open_position(c, "u1", "MINT", "alpha_b", mode="real")
    assert pos is not None
    # chain delta: spent $10 for 500 tokens -> fill 0.02 (NOT derived from the quote)
    assert abs(pos["entry_price"] - 0.02) < 1e-9 and abs(pos["entry_notional"] - 10.0) < 1e-9
    assert m.pending_totals(c) == (0, 0.0)          # reservation confirmed/cleared
    fill = c.execute("SELECT fill_source, tx_sig FROM memecoin_fills WHERE position_id=?", (pos["id"],)).fetchone()
    assert fill["fill_source"] == "chain" and fill["tx_sig"] == "SIG123"


def test_confirmed_copies_funded_contract_byte_for_byte_to_position():
    c, pid, attempt_id = _contract_c()
    result = _run(c, pid, _Deps(confirm="confirmed"))

    pending = dict(c.execute(
        "SELECT funded_contract_version,funded_contract_hash,funded_contract_json "
        "FROM memecoin_pending_entries WHERE id=?", (pid,),
    ).fetchone())
    attempt = dict(c.execute(
        "SELECT funded_contract_version,funded_contract_hash,funded_contract_json "
        "FROM memecoin_entry_attempts WHERE attempt_id=?", (attempt_id,),
    ).fetchone())
    position = dict(c.execute(
        "SELECT funded_contract_version,funded_contract_hash,funded_contract_json "
        "FROM memecoin_positions WHERE id=?", (result["position_id"],),
    ).fetchone())
    assert position == pending == attempt


def test_confirmed_contract_mismatch_raises_and_leaves_reconciliation_visible():
    c, pid, _attempt_id = _contract_c(pending_hash="0" * 64)

    with pytest.raises(ValueError, match="funded contract"):
        _run(c, pid, _Deps(confirm="confirmed"))

    assert m.pending_totals(c) == (1, 10.0)
    assert m.get_open_position(c, "u1", "MINT", "alpha_b", mode="real") is None


def test_exact_confirmed_post_peak_aborts_before_sign_or_broadcast():
    c, pid, _attempt_id = _contract_c(post_peak=True)
    d = _Deps(confirm="confirmed")
    d.quote_fn = lambda mint, usd: (
        d.calls.append("quote") or {
            "inAmount": "10000000", "outAmount": "500",
            "otherAmountThreshold": "470", "priceImpactPct": 0.01,
        }
    )
    d.token_fn = lambda mint: ((500.0, 0) if "sent" in d.calls else (0.0, 0))

    result = _run(c, pid, d)

    assert result["status"] == "aborted"
    assert result["reason"] == "post_peak_confirmed"
    assert result["exact_giveback_pct"] == pytest.approx(50.0)
    assert "sign" not in d.calls and "send" not in d.calls
    assert m.pending_totals(c) == (0, 0.0)


def test_post_peak_display_nomination_without_exact_price_keeps_existing_path():
    c, pid, _attempt_id = _contract_c(post_peak=True)
    d = _Deps(confirm="confirmed")

    result = _run(c, pid, d)

    assert result["status"] == "filled"
    assert "sign" in d.calls and "send" in d.calls


def test_sig_persisted_before_broadcast():
    c, pid = _c(); d = _Deps()
    _run(c, pid, d)
    # set_pending_sig must be called before send_fn (write-ahead protocol)
    assert d.calls.index("sign") < d.calls.index("send")   # signed before sent
    # and the executor writes the sig to the ledger before broadcasting
    row = c.execute("SELECT tx_sig FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()
    assert row is None or row["tx_sig"] == "SIG123"        # (row cleared on confirm; sig was written)


def test_failed_tx_releases_reservation_no_position():
    c, pid = _c(); d = _Deps(confirm="failed")
    r = _run(c, pid, d)
    assert r["status"] == "failed"
    assert m.get_open_position(c, "u1", "MINT", "alpha_b", mode="real") is None
    assert m.pending_totals(c) == (0, 0.0)          # released


def test_unknown_leaves_reserved_and_never_resubmits():
    c, pid = _c(); d = _Deps(confirm="timeout")
    r = _run(c, pid, d)
    assert r["status"] == "unknown"
    assert m.get_open_position(c, "u1", "MINT", "alpha_b", mode="real") is None
    assert m.pending_totals(c) == (1, 10.0)         # STILL reserved (fail-closed, no resubmit)
    assert d.calls.count("send") == 1               # sent exactly once


def test_slippage_impact_aborts_before_signing():
    c, pid = _c(); d = _Deps(quote_impact=0.10)     # 10% impact > 3% cap
    r = _run(c, pid, d)
    assert r["status"] == "aborted"
    assert "sign" not in d.calls                     # never signed/sent
    assert m.pending_totals(c) == (0, 0.0)           # reservation released


def test_confirmed_with_lagging_balance_read_retries_then_fills():
    """A just-confirmed tx can lag the balance surface — the first post-confirm delta reads 0.
    Regression: a real $5 fill was booked 'anomaly' on this (2026-07-10 smoke test)."""
    c, pid = _c(); d = _Deps(confirm="confirmed")
    post_reads = {"n": 0}
    def lagging_usdc():
        if "sent" not in d.calls:
            return 100.0
        post_reads["n"] += 1
        return 100.0 if post_reads["n"] <= 1 else 90.0   # first post-confirm read is stale
    sleeps = []
    r = ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-07-09T00:00:00Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=lagging_usdc, token_fn=d.token_fn, sleep_fn=sleeps.append)
    assert r["status"] == "filled"
    assert len(sleeps) == 1                              # exactly one bounded re-read needed
    pos = m.get_open_position(c, "u1", "MINT", "alpha_b", mode="real")
    assert abs(pos["entry_notional"] - 10.0) < 1e-9      # priced from the FRESH delta


def test_confirmed_with_permanently_zero_delta_is_bounded_anomaly():
    c, pid = _c(); d = _Deps(confirm="confirmed")
    d._usdc = [100.0, 100.0]; d._tok = [0.0, 0.0]        # delta never appears
    sleeps = []
    r = ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-07-09T00:00:00Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn, sleep_fn=sleeps.append)
    assert r["status"] == "anomaly"
    assert len(sleeps) == 3                              # bounded, not infinite
    assert m.pending_totals(c) == (1, 10.0)              # reservation LEFT for recovery


def test_confirmed_with_missing_balance_delta_uses_verified_receipt_fill():
    c, pid = _c(); d = _Deps(confirm="confirmed")
    d._usdc = [100.0, 100.0]; d._tok = [0.0, 0.0]
    receipt_calls = []

    def receipt_fill(sig, mint, maximum):
        receipt_calls.append((sig, mint, maximum))
        return 10.0, 500.0, 0

    result = ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-07-09T00:00:00Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn, sleep_fn=lambda _s: None,
        receipt_fill_fn=receipt_fill,
    )

    assert result["status"] == "filled"
    assert receipt_calls == [("SIG123", "MINT", 10.0)]
    assert m.pending_totals(c) == (0, 0.0)


def test_receipt_fill_requires_the_signed_wallet_and_exact_token_deltas():
    owner = "Owner111"
    tx = {
        "transaction": {
            "signatures": ["SIG123"],
            "message": {"accountKeys": [{"pubkey": owner, "signer": True}]},
        },
        "meta": {
            "err": None,
            "preTokenBalances": [
                {"mint": ex.USDC_MINT, "owner": owner,
                 "uiTokenAmount": {"amount": "27267186", "decimals": 6}},
            ],
            "postTokenBalances": [
                {"mint": ex.USDC_MINT, "owner": owner,
                 "uiTokenAmount": {"amount": "7186", "decimals": 6}},
                {"mint": "MINT", "owner": owner,
                 "uiTokenAmount": {"amount": "9602688071", "decimals": 6}},
            ],
        },
    }

    assert ex.receipt_entry_fill(
        tx, signature="SIG123", owner=owner, mint="MINT", maximum=27.26,
    ) == pytest.approx((27.26, 9602.688071, 6))
    tx["transaction"]["message"]["accountKeys"][0]["signer"] = False
    assert ex.receipt_entry_fill(
        tx, signature="SIG123", owner=owner, mint="MINT", maximum=27.26,
    ) is None


def test_a_native_exit_check_that_crashes_is_not_a_native_exit_that_said_no():
    """A CHECK THAT COULD NOT RUN IS NOT A CHECK THAT SAID NO (2026-09-18, DAWGINU ref 0d66a2a1).

    The unsellable-trap escape hatch caught every exception and recorded the same
    `available: False` as a native sell that genuinely does not build. Both refuse — that is the
    right direction — but they ask for completely different repairs, and the trace could not tell
    them apart. Same class as the vol_death reader that fails closed to zero.
    """
    from sniper.execution import solana_swap_executor as ex
    import inspect
    src = inspect.getsource(ex)
    body = src[src.index("if _unbuildable and native_exit_available_fn is not None:"):]
    body = body[:body.index("if _unbuildable:")]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
    assert "check_failed:" in code, "the crash path must record a distinct reason"
    assert "did_not_build_or_fit" in code, "the genuine refusal must record its own reason"
    assert "logger.exception" in code, "a swallowed exception must still reach the log"
    # and the two must not collapse into one string
    assert code.index("check_failed:") != code.index("did_not_build_or_fit")


def _trap_guard(answer):
    """Drive the unsellable-trap escape hatch with one native-exit answer, and report the verdict."""
    from sniper.execution import solana_swap_executor as ex
    import inspect
    src = inspect.getsource(ex)
    body = src[src.index("if _unbuildable and native_exit_available_fn is not None:"):]
    body = body[:body.index("if _unbuildable:")]
    ns = {"_unbuildable": "QUBTAD8C", "native_exit_available_fn": lambda m, a: answer,
          "mint": "M", "planned_atomic": 1, "_trace_substage": lambda *a, **k: None,
          "trace": None, "logger": ex.logger}
    exec(body, ns)
    return ns["_unbuildable"], ns["_native_reason"]


def test_a_native_exit_that_fits_clears_the_trap_guard():
    """Quant ref 7f6e294e built at 1,028 bytes against a 1,232 limit and was refused anyway."""
    unbuildable, reason = _trap_guard((True, "fits:1028"))
    assert unbuildable is None, "a curve whose native sell fits must not be refused"
    assert reason == "fits:1028"


def test_a_native_exit_that_is_too_big_still_refuses():
    """DAWGINU built at 1,504 bytes — 272 over. That is the trap this guard exists for."""
    unbuildable, reason = _trap_guard((False, "too_big:1504>1232"))
    assert unbuildable == "QUBTAD8C"
    assert reason == "too_big:1504>1232"


def test_a_tuple_answer_is_never_read_as_truthy():
    """A 2-tuple is ALWAYS truthy: `bool(answer)` would admit the exact trap the guard catches."""
    unbuildable, _ = _trap_guard((False, "incomplete_after_retry"))
    assert unbuildable == "QUBTAD8C", "the tuple form must be unpacked, not coerced"


def test_a_bare_boolean_answer_still_works():
    """The legacy contract stays accepted, so an older caller cannot silently invert."""
    assert _trap_guard(True)[0] is None
    assert _trap_guard(False)[0] == "QUBTAD8C"


def _drive_native_exit_retry(once_answers):
    """Drive solana_real_seams._native_exit_available's retry decision with a scripted sequence of
    `_once()` answers, isolating the retry-vs-keep logic from the packet-size measurement it wraps
    (same source-slice-and-exec technique as `_trap_guard` above, dedented since this slice has
    multiple sibling statements rather than one self-contained `if` block)."""
    import inspect
    import textwrap
    from sniper.execution import solana_real_seams as S
    src = inspect.getsource(S)
    start = src.index("        ok, reason = _once()")
    end = src.index("        return ok, reason", start)
    body = textwrap.dedent(src[start:end])
    calls = iter(once_answers)
    ns = {"_once": lambda: next(calls)}
    exec(body, ns)
    return ns["ok"], ns["reason"]


def test_a_retry_that_measures_too_big_keeps_that_reason():
    """EX1 review, 2026-09-20: DAWGINU-shaped case. The first build produced no payload
    ("incomplete"); the retry completed and measured 1,504 bytes against the 1,232 limit. The real
    measurement must survive, not the generic incomplete_after_retry label."""
    ok, reason = _drive_native_exit_retry([(False, "incomplete"), (False, "too_big:1504>1232")])
    assert ok is False
    assert reason == "too_big:1504>1232"


def test_a_retry_that_fits_clears_and_keeps_that_reason():
    ok, reason = _drive_native_exit_retry([(False, "incomplete"), (True, "fits:1028")])
    assert ok is True
    assert reason == "fits:1028"


def test_a_retry_that_is_still_incomplete_is_the_only_case_that_gets_the_generic_label():
    ok, reason = _drive_native_exit_retry([(False, "incomplete"), (False, "incomplete")])
    assert ok is False
    assert reason == "incomplete_after_retry"


def test_a_first_attempt_that_is_already_measured_is_never_retried():
    """`too_big` on the first try means the build completed; retrying it spends a request to
    measure the same size again, so the guard must not call `_once` a second time."""
    ok, reason = _drive_native_exit_retry([(False, "too_big:1504>1232")])
    assert ok is False
    assert reason == "too_big:1504>1232"
