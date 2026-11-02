"""Tests for the Solana exact-quote chase check.

A read-only audit found 154 display-route samples. The median time difference was 0.496 seconds.
The route quote exceeded the display price in 133 samples. The 30% route limit blocks 37 of 153 entries.
Coal had an 18.4% route gap, so the 30% limit allows it.
"""
import sqlite3

import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.persistence import db_memecoin as m


_AUTO_REVERSE = object()


def _c(source_price=None):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    if source_price is not None:
        c.execute("UPDATE memecoin_pending_entries SET source_price=? WHERE id=?",
                  (source_price, pid))
        c.commit()
    return c, pid


class _Bal:
    """Balance snapshot with token decimals."""
    def __init__(self, human, decimals=6):
        self.human, self.decimals = human, decimals
        self.atomic = int(round(human * (10 ** decimals)))


class _Deps:
    """Return a $0.02 expected route price."""
    def __init__(self):
        self.calls = []
    def quote_fn(self, mint, usd):
        self.calls.append("quote")
        return {"inAmount": "10000000", "outAmount": "500000000",
                "otherAmountThreshold": "470000000", "priceImpactPct": 0.01}
    def reverse_quote_fn(self, mint, amount):
        return {"inAmount": str(amount), "outAmount": "9800000",
                "otherAmountThreshold": "9800000"}
    def usdc_fn(self):
        return 90.0 if "sent" in self.calls else 100.0
    def token_fn(self, mint):
        return _Bal(500.0) if "sent" in self.calls else _Bal(0.0)
    def sign_fn(self, quote):
        self.calls.append("sign"); return (b"signedtx", "SIG123")
    def send_fn(self, signed):
        self.calls.append("send"); self.calls.append("sent"); return True
    def confirm_fn(self, sig):
        self.calls.append("confirm"); return "confirmed"


def _run(c, pid, d, chase_max_pct=None):
    return ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-08-19T00:00:00Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn, impact_cap_pct=12.0,
        chase_max_pct=chase_max_pct)


def test_a_quote_past_the_cap_is_refused_before_signing():
    """Quote 0.02 against a source price of 0.01 is +100% past the trader."""
    c, pid = _c(source_price=0.01); d = _Deps()
    r = _run(c, pid, d, chase_max_pct=30.0)
    assert r["status"] == "skipped_chase"
    assert r["quote_chase_pct"] == pytest.approx(100.0)
    assert "exact quote" in r["reason"]
    assert "display" not in r["reason"]
    assert "sign" not in d.calls and "send" not in d.calls
    assert m.pending_totals(c) == (0, 0.0)          # reservation released, slot freed


def test_coal_passes_thirty_percent_and_fails_only_at_fifteen_percent():
    """The Coal route gap was 18.4%."""
    for cap, expected in ((15.0, "skipped_chase"), (30.0, "filled")):
        c, pid = _c(source_price=0.0169)           # quote 0.02 -> +18.3%
        d = _Deps()
        assert _run(c, pid, d, chase_max_pct=cap)["status"] == expected


def test_a_thirty_one_percent_nano_quote_passes_the_forty_percent_cap():
    c, pid = _c(source_price=0.01524)       # quote 0.02 -> +31.2%
    d = _Deps()
    assert _run(c, pid, d, chase_max_pct=40.0)["status"] == "filled"


def test_buying_below_the_trader_is_never_refused():
    c, pid = _c(source_price=0.05); d = _Deps()    # quote 0.02 -> -60%
    assert _run(c, pid, d, chase_max_pct=30.0)["status"] == "filled"


def test_a_manual_operator_entry_is_never_chase_gated():
    """A manual entry has no source price, so this check does not apply."""
    c, pid = _c(source_price=None); d = _Deps()
    assert _run(c, pid, d, chase_max_pct=30.0)["status"] == "filled"


@pytest.mark.parametrize("cap", [None, 0.0])
def test_an_absent_or_disabled_cap_leaves_the_entry_alone(cap):
    """An absent or disabled limit keeps the fail-open policy."""
    c, pid = _c(source_price=0.001); d = _Deps()   # +1900%, would refuse under any live cap
    assert _run(c, pid, d, chase_max_pct=cap)["status"] == "filled"


def test_the_refusal_is_counted_as_a_refusal_not_an_error():
    """The digest records `skipped_chase` as a refusal."""
    from sniper.services.memecoin_real_runner import _TRACE_REFUSALS, _trace_terminal
    assert "skipped_chase" in _TRACE_REFUSALS
    assert _trace_terminal({"status": "skipped_chase", "reason": "x"})[0] == "refused"


def test_an_execution_premium_refusal_is_retryable():
    """The premium compares OUR quote to the CHART, so a refusal describes the routes visible at
    that instant, not the coin. This chain does not surface a new pool's Initialize for 38-61s, so
    a coin whose only route at t+5s is a thin two-hop can have a sane direct pool minutes later.
    Re-queueing is safe because the retry re-runs the whole decision and the cap must pass again."""
    from sniper.services.memecoin_real_runner import _is_transient_entry_miss
    res = {"status": "rejected",
           "reason": "exact quote is 7.32x above source price 0.000131289"}
    assert _is_transient_entry_miss(res) is True


def test_the_chase_premium_retry_is_knobbed_and_off_makes_it_terminal(monkeypatch):
    """The operator can turn OFF the one retryable class that is about price, not visibility.

    STOOK 64575 (solana, 2026-09-24) refused `exact quote is +47% above source price (cap 35%)` at
    14:24:37Z and `+38%` at 14:24:48Z, then the retry lane filled it at 14:24:59Z for -$101.57. The
    strings below are those ledger rows verbatim, so a reword of the refusal breaks this test
    rather than silently un-knobbing the branch.

    The knob is read from the settings module at call time, which is the only reading the funded
    path does; a test injecting its own cfg object would prove nothing about production.
    """
    from sniper.services.memecoin_real_runner import _is_transient_entry_miss
    from sniper.settings import memecoin as cfg

    live = [{"status": "skipped_chase",
             "reason": "exact quote is +47% above source price (cap 35%)"},
            {"status": "skipped_chase",
             "reason": "exact quote is +38% above source price (cap 35%)"}]

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_RETRY_ENABLED", True)
    for res in live:
        assert _is_transient_entry_miss(res) is True, res["reason"]

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_RETRY_ENABLED", False)
    for res in live:
        assert _is_transient_entry_miss(res) is False, res["reason"]

    # AND ONLY THAT CLASS. Every other transient verdict keeps retrying with the knob off.
    for other in ({"status": "aborted", "reason": "no_route_all_venues"},
                  {"status": "unsafe", "reason": "sell_sim_rpc_unknown: eth_call returned None"},
                  {"status": "unsafe", "reason": "mint account not visible yet (fail-closed)"},
                  {"status": "failed", "reason": "custom program error: slippage tolerance"}):
        assert _is_transient_entry_miss(other) is True, other["reason"]


def test_a_non_premium_rejection_is_still_terminal():
    from sniper.services.memecoin_real_runner import _is_transient_entry_miss
    assert _is_transient_entry_miss(
        {"status": "rejected", "reason": "source buy under mcap floor 0.500%"}) is False
    assert _is_transient_entry_miss({"status": "unsafe", "reason": "honeypot_sim_reverted"}) is False


def test_no_route_all_venues_is_transient_not_terminal():
    """Every venue asked and none had a route AT THAT INSTANT is a statement about pool
    VISIBILITY on a fresh 4663 coin, not about the coin.

    GPU refused `no_route_all_venues` at 10:21 and the same route quoted fine fourteen minutes
    later (evm_v4_multihop's own note). CHILL (0x5c3a0e5f) repeated it 2026-09-04: aborted at
    18:10:51, and its native-paired 10% pool 0x91e4bccd quoted 0.01 ETH into 111,801 tokens
    minutes afterwards.
    """
    from sniper.services.memecoin_real_runner import _is_transient_entry_miss
    assert _is_transient_entry_miss(
        {"status": "aborted", "reason": "no_route_all_venues"}) is True


def test_a_named_venue_route_failure_stays_terminal():
    # Only the all-venues instant miss is retried; a specific provider's typed failure is not
    # silently swept into the same bucket.
    from sniper.services.memecoin_real_runner import _is_transient_entry_miss
    assert _is_transient_entry_miss(
        {"status": "aborted", "reason": "lifi_insufficient_liquidity"}) is False
