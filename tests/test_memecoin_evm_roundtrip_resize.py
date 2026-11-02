"""EVM shrink-instead-of-refuse (operator 2026-10-14: "make the blocker smart, not a higher bar").

73849 (56 Flap, china_sniper_1): the $50 clip priced a 15.80% round trip against the budget and
was refused; a smaller clip fits the SAME budget because only our size's impact scales with the
clip. A $1 probe prices the fixed part (fees, tax); the clip shrinks to land at 80% of the budget;
one re-quote at that clip decides. The budget itself never moves."""
import sqlite3

import pytest

from sniper.execution import evm_swap_executor as X
from sniper.settings import memecoin as cfg

TOKEN = "0x2222222222222222222222222222222222222222"
MISS = {"code": "route_over_budget_marginal",
        "detail": "flap_curve priced this route and declined it: round_trip_over_budget_marginal:15.8",
        "rungs": [{"rung": "flap_curve", "reason": "round_trip_over_budget_marginal:15.8"}]}


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_ROUNDTRIP_RESIZE", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_ROUNDTRIP_PROBE_USD", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_TARGET_FRACTION", 0.8)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_ROUNDTRIP_RESIZE_MIN_FRACTION", 0.25)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 5.0)
    monkeypatch.setattr(X, "_round_trip_budget", lambda chain: 15.0)


def _quotes(probe_pct, requote):
    calls = []

    def quote(usd):
        calls.append(usd)
        if usd == 1.0:
            return None if probe_pct is None else {"route": {"round_trip_pct": -probe_pct}}
        return requote
    return quote, calls


def test_a_size_driven_miss_shrinks_to_eighty_percent_of_the_same_budget():
    quote, calls = _quotes(2.0, {"route": {"round_trip_pct": -11.9}, "id": "q"})
    out = X._evm_roundtrip_resize(quote, TOKEN, 50.0, MISS, "56", None)
    # (12 - 2) / (15.8 - 2) x 50 = 36.23: fixed 2% kept, size part bought down to land at 12%
    assert calls == [1.0, 36.23]
    assert out == ({"route": {"round_trip_pct": -11.9}, "id": "q"}, 36.23)


@pytest.mark.parametrize("probe_pct,why", [(12.5, "fixed cost at or over the target"),
                                           (None, "the probe itself did not price in budget")])
def test_a_fixed_cost_trap_is_refused_at_any_size(probe_pct, why):
    quote, calls = _quotes(probe_pct, {"id": "never"})
    assert X._evm_roundtrip_resize(quote, TOKEN, 50.0, MISS, "56", None) is None, why
    assert calls == [1.0]


def test_a_shrink_below_a_quarter_of_the_clip_is_refused():
    miss = dict(MISS, rungs=[{"reason": "round_trip_over_budget:60.0"}], code="route_over_budget")
    quote, calls = _quotes(2.0, {"id": "never"})       # (12-2)/(60-2) x 50 = 8.62 < 12.5
    assert X._evm_roundtrip_resize(quote, TOKEN, 50.0, miss, "56", None) is None
    assert calls == [1.0]


def test_the_re_quote_still_decides_and_other_misses_are_untouched(monkeypatch):
    quote, calls = _quotes(2.0, None)                 # the smaller clip still did not fit
    assert X._evm_roundtrip_resize(quote, TOKEN, 50.0, MISS, "56", None) is None
    assert calls == [1.0, 36.23]
    quote, calls = _quotes(2.0, {"id": "q"})
    for code in ("no_route", "provider_timeout", "route_capacity"):
        assert X._evm_roundtrip_resize(quote, TOKEN, 50.0, dict(MISS, code=code), "56", None) is None
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_ROUNDTRIP_RESIZE", False)   # rollback
    assert X._evm_roundtrip_resize(quote, TOKEN, 50.0, MISS, "56", None) is None
    assert calls == []


def test_the_entry_takes_the_resized_quote_instead_of_refusing(monkeypatch):
    """The wiring: an over-budget miss on the first ladder calls the resize, and its quote replaces
    the refusal (the rest of the entry then judges it with every gate). The 73849 shape."""
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.reserve_pending(conn, mint=TOKEN, source_user_id="wallet", strategy="control",
                            notional=50, reserved_at="2026-10-14T09:46:33+00:00", network_id="56")
    seen = []

    def resize(call_quote, mint, usd, miss, chain, trace):
        seen.append((mint, usd, miss["code"], chain))
        return None                                     # refusal stands: the entry must abort
    monkeypatch.setattr(X, "_evm_roundtrip_resize", resize)
    out = X.execute_entry_evm(
        conn, pending_id=pid, mint=TOKEN, network_id="56", usd_amount=50, strategy="control",
        source_user_id="wallet", now_iso="2026-10-14T09:46:33+00:00",
        quote_fn=lambda *a, **k: None, quote_miss_fn=lambda: dict(MISS),
        approve_fn=lambda *a: True, sign_fn=lambda *a: ("0xraw", "0xhash", 1),
        send_fn=lambda *a: True, confirm_fn=lambda *a: "confirmed", usdc_fn=lambda: 50,
        token_fn=lambda _m: (0, 18))
    assert seen == [(TOKEN, 50, "route_over_budget_marginal", "56")]
    assert out["status"] == "aborted" and "route_over_budget_marginal" in out["reason"]


def test_the_entry_proceeds_on_the_resized_quote_and_clip(monkeypatch):
    """Gamma BLOCK on cb4c129ef: the success path. The first ladder misses over budget, the resize
    returns a quote at the smaller clip, and the entry signs, sends and books on THAT quote, with
    every later gate (here the gas guard) judging the resized clip, not the original."""
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _reserve, _seams
    c = _c()
    pid = _reserve(c, network_id="56")
    deps, calls = _seams(quote=None)
    resized_quote = dict(_QUOTE, _resized=True)
    asked = []

    def quote_fn(mint, usd, **kw):                # over budget at $50; the post-approval re-quote
        asked.append(usd)                         # asks again at the RESIZED clip
        return resized_quote if usd == 36.23 else None
    deps["quote_fn"] = quote_fn
    monkeypatch.setattr(X, "_evm_roundtrip_resize",
                        lambda call_quote, mint, usd, miss, chain, trace: (resized_quote, 36.23))
    seen_usd, signed = [], []

    def guard(usd):
        seen_usd.append(usd)
    real_sign = deps["sign_fn"]

    def sign_fn(tx):
        signed.append(tx)
        return real_sign(tx)
    deps["sign_fn"] = sign_fn
    out = X.execute_entry_evm(
        c, pending_id=pid, mint="0xtok", network_id="56", usd_amount=50.0, strategy="alpha_b",
        source_user_id="trader1", now_iso="2026-10-14T09:46:33Z", gas_guard_fn=guard,
        quote_miss_fn=lambda: dict(MISS), **deps)
    assert out["status"] == "filled", out
    assert seen_usd and set(seen_usd) == {36.23}
    assert calls["sign"] >= 1 and calls["send"] == 1
    assert asked[0] == 50.0 and set(asked[1:]) == {36.23}
    assert signed and signed[-1].get("data") == "0xdead"
