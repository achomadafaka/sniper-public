"""A round-trip refusal the one-bank simulation could not confirm is re-run once on a fresh quote.

SHORK (attempt 7fa44410, 2026-09-28): Jupiter's buy quote gave 27.9% fewer tokens than a quote
3.0 s later and than every on-chain trade in its slot; the sequential simulation could not run
(transaction_limits), so one bad reading refused a coin worth +61% at 13 minutes.
"""
import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.persistence import db_memecoin as m
from test_memecoin_entry_roundtrip_cap import _c, _Deps


class _Sequence(_Deps):
    """Each reverse quote returns the next USD value in `backs` (the last one repeats)."""
    def __init__(self, backs):
        super().__init__(quote_impact=0.05)
        self._backs = list(backs)

    def reverse_quote_fn(self, mint, token_atomic):
        self._usd_back = self._backs.pop(0) if len(self._backs) > 1 else self._backs[0]
        return super().reverse_quote_fn(mint, token_atomic)


def _run(c, pid, d, **kw):
    return ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b",
        source_user_id="u1", now_iso="2026-09-28T09:39:16Z",
        quote_fn=d.quote_fn, reverse_quote_fn=d.reverse_quote_fn, sign_fn=d.sign_fn,
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_fn=d.token_fn,
        impact_cap_pct=5.0, **kw)          # 5% one-way cap -> 10% round-trip cap


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE", True)
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_ROUNDTRIP_RESIZE", False)


def test_a_bad_first_quote_that_a_fresh_quote_clears_fills():
    c, pid = _c(); d = _Sequence([7.08, 9.40])        # 29.2% then 6.0%
    r = _run(c, pid, d)
    assert r["status"] == "filled"
    assert d.calls.count("quote") == 2 and d.calls.count("roundtrip") == 2
    assert d.calls.index("sign") > [i for i, x in enumerate(d.calls) if x == "roundtrip"][1]


def test_a_fresh_quote_that_confirms_the_refusal_still_refuses_and_releases():
    c, pid = _c(); d = _Sequence([7.08, 7.10])
    r = _run(c, pid, d)
    assert r["status"] == "aborted" and r["reason"].startswith("round trip ")
    assert "sign" not in d.calls
    assert d.calls.count("quote") == 2, "exactly one re-run, never a third quote"
    assert m.pending_totals(c) == (0, 0.0)


def test_a_simulation_measured_refusal_is_not_re_quoted():
    c, pid = _c(); d = _Sequence([7.08, 9.40])
    measured = {"version": "solana_sequential_roundtrip_v1", "status": "measured",
                "buy_spent_atomic": "10000000", "sell_received_atomic": "8000000"}
    r = _run(c, pid, d, roundtrip_measure_fn=lambda b, s: measured)
    assert r["status"] == "aborted" and "20.00%" in r["reason"]
    assert d.calls.count("quote") == 1


def test_an_unmeasured_simulation_is_re_quoted():
    c, pid = _c(); d = _Sequence([7.08, 9.40])
    r = _run(c, pid, d, roundtrip_measure_fn=lambda b, s: {"status": "unknown",
                                                           "reason": "transaction_limits"})
    assert r["status"] == "filled"
    assert d.calls.count("quote") == 2


def test_the_knob_off_refuses_on_the_first_reading(monkeypatch):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_ENTRY_ROUNDTRIP_REQUOTE", False)
    c, pid = _c(); d = _Sequence([7.08, 9.40])
    r = _run(c, pid, d)
    assert r["status"] == "aborted"
    assert d.calls.count("quote") == 1
