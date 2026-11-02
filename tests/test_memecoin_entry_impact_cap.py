"""mcap-tiered entry price-impact cap. A flat impact cap refused exactly the small-cap pumps that
are the edge — a thin sub-$1M pool structurally quotes higher impact for the same clip, and a coin
that can 2-3x absorbs more entry impact than a $20M coin that barely moves (SOLdiers $677.7k mcap
aborted at 9.37% vs a 5% flat cap, 2026-07-15). The cap is now a function of the signal-time mcap;
the executor honors a caller-supplied cap and falls back to the flat global when none is given.
Solana-only: the EVM leg has no impact gate (0x bounds slippage at request time)."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.execution import solana_swap_executor as ex
from sniper.settings.memecoin import (
    MEMECOIN_ENTRY_IMPACT_CAP_PCT,
    entry_impact_cap_for_mcap,
)


# --- tier policy --------------------------------------------------------------------------------

def test_sub_1m_mcap_gets_loosest_cap():
    # the SOLdiers case: $677.7k mcap must clear a 9.37% quote
    assert entry_impact_cap_for_mcap(677_700) == 12.0


def test_mid_mcap_tiers():
    assert entry_impact_cap_for_mcap(3_000_000) == 8.0
    assert entry_impact_cap_for_mcap(20_000_000) == 6.0


def test_large_mcap_falls_back_to_base_cap():
    assert entry_impact_cap_for_mcap(50_000_000) == MEMECOIN_ENTRY_IMPACT_CAP_PCT


def test_unknown_mcap_never_loosened():
    # None / non-numeric / non-positive -> conservative base cap, never a looser tier
    assert entry_impact_cap_for_mcap(None) == MEMECOIN_ENTRY_IMPACT_CAP_PCT
    assert entry_impact_cap_for_mcap("nan") == MEMECOIN_ENTRY_IMPACT_CAP_PCT
    assert entry_impact_cap_for_mcap(0) == MEMECOIN_ENTRY_IMPACT_CAP_PCT
    assert entry_impact_cap_for_mcap(-5) == MEMECOIN_ENTRY_IMPACT_CAP_PCT


def test_tiers_are_monotonic_smaller_mcap_never_tighter():
    caps = [entry_impact_cap_for_mcap(x) for x in
            (500_000, 2_000_000, 10_000_000, 40_000_000)]
    assert caps == sorted(caps, reverse=True)   # smaller mcap -> looser-or-equal cap


# --- executor honors the supplied cap -----------------------------------------------------------

_AUTO_REVERSE = object()

def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    return c, pid


class _Deps:
    def __init__(self, quote_impact=0.01, confirm="confirmed", reverse_out=_AUTO_REVERSE):
        self.calls = []
        self._impact = quote_impact
        self._confirm = confirm
        self._reverse_out = reverse_out
    def quote_fn(self, mint, usd):
        self.calls.append("quote")
        return {"outAmount": "500", "otherAmountThreshold": "470",
                "priceImpactPct": self._impact}
    def reverse_quote_fn(self, mint, amount):
        if self._reverse_out is None:
            return None
        # price-neutral: 500 tokens are the $10 clip, so a token is worth 20,000 USDC atomic.
        # `outAmount: "1"` meant one atomic back on the whole clip, and the round-trip cap
        # reads this stub now.
        out = (str(int(amount) * 20_000) if self._reverse_out is _AUTO_REVERSE
               else str(self._reverse_out))
        return {"inAmount": str(amount), "outAmount": out, "otherAmountThreshold": out}
    def usdc_fn(self):
        return 90.0 if "sent" in self.calls else 100.0
    def token_fn(self, mint):
        return 500.0 if "sent" in self.calls else 0.0
    def sign_fn(self, quote):
        self.calls.append("sign"); return (b"signedtx", "SIG123")
    def send_fn(self, signed):
        self.calls.append("send"); self.calls.append("sent"); return True
    def confirm_fn(self, sig):
        self.calls.append("confirm"); return self._confirm


def _run(c, pid, d, impact_cap_pct=None):
    return ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-07-15T00:00:00Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn,
        sign_fn=d.sign_fn, send_fn=d.send_fn, confirm_fn=d.confirm_fn,
        usdc_fn=d.usdc_fn, token_fn=d.token_fn, impact_cap_pct=impact_cap_pct)


def test_small_cap_impact_passes_under_tiered_cap():
    # 9.37% impact clears a 12% small-cap cap -> proceeds to sign/send/fill
    c, pid = _c(); d = _Deps(quote_impact=0.0937)
    r = _run(c, pid, d, impact_cap_pct=12.0)
    assert r["status"] == "filled"
    assert "sign" in d.calls


def test_same_impact_aborts_under_base_cap():
    # identical quote, tight cap -> aborted, reason carries the ACTUAL cap number, never signs
    c, pid = _c(); d = _Deps(quote_impact=0.0937)
    r = _run(c, pid, d, impact_cap_pct=5.0)
    assert r["status"] == "aborted"
    assert "5.0% cap" in r["reason"] and "sign" not in d.calls
    assert m.pending_totals(c) == (0, 0.0)   # reservation released


def test_catastrophic_impact_still_aborts_under_loose_cap():
    # 25% impact is dead-pool liquidity — even the loosest tier refuses it
    c, pid = _c(); d = _Deps(quote_impact=0.25)
    r = _run(c, pid, d, impact_cap_pct=12.0)
    assert r["status"] == "aborted"
    assert "12.0% cap" in r["reason"] and "sign" not in d.calls


def test_default_no_cap_uses_global_flat_cap():
    # unchanged legacy behavior when no cap is threaded (manual path / older callers)
    c, pid = _c(); d = _Deps(quote_impact=(MEMECOIN_ENTRY_IMPACT_CAP_PCT + 5) / 100.0)
    r = _run(c, pid, d, impact_cap_pct=None)
    assert r["status"] == "aborted"
    assert "%.1f%% cap" % MEMECOIN_ENTRY_IMPACT_CAP_PCT in r["reason"]


def test_one_hundred_percent_sentinel_uses_safe_sell_back_quote():
    c, pid = _c()
    d = _Deps(quote_impact=1.0, reverse_out="8460000")
    result = _run(c, pid, d, impact_cap_pct=12.0)
    assert result["status"] == "filled"
    assert result["roundtrip_cost_pct"] == pytest.approx(10.0)
    assert "sign" in d.calls


def test_roundtrip_trace_preserves_independent_quote_amounts(monkeypatch):
    rows = []
    monkeypatch.setattr(ex, "_trace_substage", lambda trace, name, **data: rows.append((name, data)))
    c, pid = _c()
    d = _Deps(quote_impact=1.0, reverse_out="8460000")
    result = _run(c, pid, d, impact_cap_pct=12.0)
    assert result["status"] == "filled"
    cost = next(data for name, data in rows if name == "roundtrip_cap")
    assert cost["basis"] == "independent_pre_entry_quotes"
    assert cost["buy_expected_atomic"] == "500"
    assert cost["sell_input_atomic"] == "470"
    assert cost["sell_expected_atomic"] == "8460000"
    assert cost["scaled_sell_usd"] == pytest.approx(9.0)


def test_one_hundred_percent_sentinel_refuses_costly_sell_back_quote():
    c, pid = _c()
    d = _Deps(quote_impact=1.0, reverse_out="4700000")
    result = _run(c, pid, d, impact_cap_pct=12.0)
    assert result["status"] == "aborted"
    assert "round trip 50.00% over 24.0% cap" in result["reason"]
    assert "sign" not in d.calls


def test_one_hundred_percent_sentinel_refuses_missing_sell_back_quote():
    c, pid = _c()
    d = _Deps(quote_impact=1.0, reverse_out=None)
    result = _run(c, pid, d, impact_cap_pct=12.0)
    assert result == {"status": "route_failed", "reason": "no planned-quantity sell route",
                      "attempt_id": None, "origin_event_id": None,
                      "network_id": "solana", "signal_source": None}
    assert "sign" not in d.calls


def test_malformed_impact_cannot_use_the_sentinel_path():
    c, pid = _c()
    d = _Deps(quote_impact="invalid", reverse_out="9000000")
    result = _run(c, pid, d, impact_cap_pct=12.0)
    assert result["status"] == "aborted"
    assert result["reason"] == "price impact 100.00% over 12.0% cap"
    assert "sign" not in d.calls
