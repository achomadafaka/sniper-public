"""SINS 54M6qVo9, DegenCapitalLLC $1.1k at 06:27:09Z (operator: "insane money missed", 2026-10-07).

The slot lane landed S+1 and failed on chain (BuySlippageBelowMinTokensOut, receipt +39.1% over the
source); its one on-chain fallback quote passed the chase cap (30% over the source's post-trade
price) but not the hard copy cap (MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT, 35% over the source's AVERAGE
fill), and refused `copy price minimum cannot be built`. Degen is a dip-retry wallet, yet the dip
retry logged `not queued (gate=False ...)`: the refusal named no cap and carried no reference, so
`_reference_and_cap` had nothing to watch.

Both signed-floor refusals now say what they measured: the premium over the gate's own reference
and its cap, in the reason and as gate_* fields. A scoped refusal of either becomes a dip row whose
reference is that gate's (the source average for the hard cap), so the watch fires only where the
rerun decision can fill.
"""
import json
from dataclasses import asdict

import pytest

from sniper.execution import solana_swap_executor as ex
from sniper.services import memecoin_dip_retry as D
from test_memecoin_entry_chase_quote_recheck import _c, _Deps
from test_memecoin_dip_retry import _ev, _cfg  # noqa: F401 (fixture)


def _sins_entry(monkeypatch, *, out="6500000"):
    """Source average 1.0, post-trade reference 1.3, chase cap 30% (ceiling 1.69), hard cap 35%
    (ceiling 1.35). $10 for 6.5 tokens = 1.538: inside the chase cap, over the hard cap."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0, raising=False)
    c, pid = _c(source_price=1.0)
    d, signed = _Deps(), []
    result = ex.execute_entry(
        c, pending_id=pid, mint="MINT", usd_amount=10.0, strategy="alpha_b", source_user_id="u1",
        now_iso="2026-10-07T06:27:09+00:00",
        quote_fn=lambda *_: {"inAmount": "10000000", "outAmount": out, "otherAmountThreshold": out,
                             "slippageBps": 1500, "priceImpactPct": "0.001"},
        reverse_quote_fn=lambda _, amount: {"inAmount": str(amount), "outAmount": "9900000",
                                            "otherAmountThreshold": "9900000"},
        usdc_fn=d.usdc_fn, token_fn=lambda _: (0.0, 6), sign_fn=lambda q: signed.append(q),
        send_fn=d.send_fn, confirm_fn=d.confirm_fn, chase_max_pct=30.0, chase_reference_price=1.3)
    return c, result, signed, d


def test_the_hard_cap_refusal_names_its_reference_cap_and_premium(monkeypatch):
    c, result, signed, d = _sins_entry(monkeypatch)
    assert result["status"] == "skipped_chase" and not signed and "send" not in d.calls
    assert result["reason"] == ("copy price minimum cannot be built: exact quote is +54% above "
                                "source average fill (cap 35%)")
    assert result["gate_reference_price"] == 1.0 and result["gate_cap_pct"] == 35.0
    assert result["gate_premium_pct"] == pytest.approx(10 / 6.5 * 100 - 100)
    c.close()


def test_the_sins_refusal_becomes_a_dip_row_on_the_source_average(monkeypatch, _cfg):  # noqa: F811
    c, result, _signed, _d = _sins_entry(monkeypatch)
    pid = c.execute("SELECT id FROM memecoin_pending_entries").fetchone()[0]
    c.execute("UPDATE memecoin_pending_entries SET attempt_id='A-sins' WHERE id=?", (pid,))
    c.execute("INSERT INTO memecoin_entry_attempts (attempt_id, mint, network_id, pending_id, "
              "decision_started_at) VALUES ('A-sins','MINT','solana',?,'2026-10-07T06:27:09+00:00')",
              (pid,))
    result = dict(result, attempt_id="A-sins")
    ev = _ev(fid="onchain:solana:4Vni4fSX:degen:MINT", mint="MINT", created="2026-10-07T06:27:09Z")
    assert D.maybe_enqueue(c, ev, result, "2026-10-07T06:27:10+00:00", event_json=json.dumps(asdict(ev)))
    dip = json.loads(c.execute("SELECT dip_json FROM memecoin_entry_retry_queue").fetchone()[0])
    assert dip["reference_price"] == 1.0 and dip["cap_pct"] == 35.0
    assert dip["refused_premium_pct"] == pytest.approx(10 / 6.5 * 100 - 100)
    c.close()


def test_a_refusal_without_a_named_gate_is_still_not_watched():
    """The pre-fix wording (no cap, no reference) stays unknown: never a guessed reference."""
    assert D._reference_and_cap("solana", {"status": "skipped_chase", "quote_price": 1.5,
                                           "reason": "copy price minimum cannot be built"}) is None


@pytest.mark.parametrize("chase_ref, avg, want", [
    (1.3, 1.0, (1.0, 35.0, "source average fill")),     # SINS: hard ceiling 1.35 < chase 1.69
    (1.0, 1.0, (1.0, 30.0, "source price")),            # chase ceiling 1.30 < hard 1.35
    (1.0, None, (1.0, 30.0, "source price")),           # no average: the chase gate alone
])
def test_the_watch_follows_the_lower_ceiling(chase_ref, avg, want):
    gate = ex.watched_gate(chase_ref, 30.0, avg, 35.0)
    assert (gate["gate_reference_price"], gate["gate_cap_pct"], gate["gate_basis"]) == want


def test_a_plain_chase_refusal_watches_the_hard_cap_when_it_binds(monkeypatch):
    """Quote 1.75 vs post-trade 1.3 (+35%, over the 30% chase cap): the chase refuses, but a dip to
    1.6 would still fail the hard cap (35% over the 1.0 average). The row watches 1.35."""
    c, result, signed, _d = _sins_entry(monkeypatch, out=str(int(10 / 1.75 * 1e6)))
    assert result["status"] == "skipped_chase" and not signed
    assert result["reason"].startswith("exact quote is +35% above source price (cap 30%)")
    assert D._reference_and_cap("solana", result) == pytest.approx((1.0, 35.0, 75.0), rel=1e-3)
    c.close()


def test_a_floor_refusal_already_within_its_cap_is_not_watched():
    """Gamma on bde257c60: the floor's except also catches a malformed quote, where the premium can
    sit at or under the cap. Such a row would fire at once and spend a decision on nothing."""
    res = {"status": "skipped_chase", "reason": "copy price minimum cannot be built: exact quote is "
           "+20% above source average fill (cap 35%)", "gate_reference_price": 1.0,
           "gate_cap_pct": 35.0, "gate_premium_pct": 20.0, "quote_price": 1.2}
    assert D._reference_and_cap("solana", res) is None
    assert D._reference_and_cap("solana", dict(res, gate_premium_pct=35.0)) is None
    assert D._reference_and_cap("solana", dict(res, gate_premium_pct=35.1)) == (1.0, 35.0, 35.1)


# --- EVM (operator ~14:40Z 2026-10-08, "Dip should be for all top traders"; design e7244500c) ---
# SOCKET 8346cfef (badabeepp, 4663) was refused "copy price minimum is +40.5% above source fill (cap
# 35.0%)" and never queued. The watch's basis stays OUR QUOTE's premium q over the source average; the
# copy floor caps the SIGNED minimum, worst = quote x k with k = buy_atomic / min_atomic, so it maps to
# q <= (1 + cap) / k - 1.

from test_memecoin_dip_retry import EVM_MINT, _chase, _conn, _dip, _enqueue, _evm_ev  # noqa: E402

K_8 = 1 / 0.92            # 4663 attempt 0 signs at the 8% retry slippage


@pytest.fixture
def evm4663(_cfg):  # noqa: F811
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana", "4663", "8453"))
    _cfg.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0, raising=False)
    _cfg.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT_BY_WALLET", {}, raising=False)
    _cfg.setattr(cfg, "quote_over_source_max_pct_for", lambda network: 30.0)
    return _cfg


def _copy_floor(conn, ev, *, signed=40.5, cap=35.0, k=K_8, quote_price=6.4e-5, min_atomic=True):
    res = _chase(conn, ev)
    res.pop("quote_chase_pct")
    q0 = (1 + signed / 100) / k - 1                       # the refused quote's own premium
    buy = 1_000_000
    res.update(reason="copy price minimum is %+.1f%% above source fill (cap %.1f%%)" % (signed, cap),
               quote_price=quote_price, source_drift_pct=q0 * 100,
               dip_route={"rung": "v4", "sell_atomic": str(10 ** 16), "buy_atomic": str(buy),
                          "pool_id": "0xpool", "legs_path": None,
                          **({"min_atomic": str(round(buy / k))} if min_atomic else {})})
    return res


def test_a_4663_copy_floor_refusal_becomes_a_dip_row_on_the_quote_basis(evm4663):
    c = _conn()
    ev = _evm_ev()
    res = _copy_floor(c, ev)
    assert _enqueue(c, ev, res)
    [row] = _dip(c)
    dip = row["dip"]
    assert dip["reference_price"] == pytest.approx(6.4e-5 / (1 + res["source_drift_pct"] / 100))
    assert dip["refused_premium_pct"] == pytest.approx(29.26, abs=0.01)       # 1.405 x 0.92 - 1
    assert dip["cap_pct"] == pytest.approx(24.2, abs=0.01)                    # 1.35 x 0.92 - 1 < 30
    assert dip["gate"] == "copy_signed_floor" and dip["gate_cap_pct"] == 35.0
    # the quote-basis cap IS the signed cap through k: the hit lands exactly where the floor passes
    assert (1 + dip["cap_pct"] / 100) * dip["min_ratio"] - 1 == pytest.approx(0.35, abs=1e-4)
    assert dip["dip_route"]["min_atomic"] == res["dip_route"]["min_atomic"]


def test_a_wallet_override_keeps_its_own_cap_and_over_source_binds_first(evm4663):
    """397397 at 50: the copy bound is 1.50 x 0.92 - 1 = 38%, so the decision's 30% over-source cap
    is the one the watch must respect."""
    c = _conn()
    ev = _evm_ev()
    assert _enqueue(c, ev, _copy_floor(c, ev, signed=55.0, cap=50.0))
    [row] = _dip(c)
    assert row["dip"]["gate_cap_pct"] == 50.0 and row["dip"]["cap_pct"] == pytest.approx(30.0)


def test_a_copy_floor_refusal_without_its_minimum_is_not_queued(evm4663):
    c = _conn()
    ev = _evm_ev()
    assert not _enqueue(c, ev, _copy_floor(c, ev, min_atomic=False))
    assert _dip(c) == []


@pytest.mark.parametrize("reason", ["copy price minimum unavailable",
                                    "copy price signed minimum unproved for Relay route"])
def test_a_copy_floor_that_measured_nothing_is_not_queued(evm4663, reason):
    c = _conn()
    ev = _evm_ev()
    res = _copy_floor(c, ev)
    res["reason"] = reason
    assert not _enqueue(c, ev, res)


def test_a_4663_premium_row_never_hits_where_the_copy_floor_refuses(evm4663):
    """The 30% signed bound with 8% slippage permits a quote premium of at most 19.6%."""
    from test_memecoin_dip_retry import _premium
    c = _conn()
    ev = _evm_ev()
    res = _premium(c, ev, drift=60.0)
    res["dip_route"]["min_atomic"] = str(round(int(res["dip_route"]["buy_atomic"]) * 0.92))
    assert _enqueue(c, ev, res)
    [row] = _dip(c)
    assert row["dip"]["cap_pct"] == pytest.approx(1.30 * 0.92 * 100 - 100, abs=0.2)
    assert row["dip"]["gate"] == "execution_premium"


def test_at_3_pct_slippage_the_copy_bound_never_binds_before_over_source(evm4663):
    c = _conn()
    from test_memecoin_dip_retry import _ev, _premium
    ev = _ev(fid="x1", mint=EVM_MINT, network="8453")
    res = _premium(c, ev, drift=60.0)
    res["dip_route"]["min_atomic"] = str(round(int(res["dip_route"]["buy_atomic"]) * 0.97))
    assert _enqueue(c, ev, res)
    assert _dip(c)[0]["dip"]["cap_pct"] == pytest.approx(30.0)               # 1.35 x 0.97 - 1 = 31%


def test_the_evm_copy_floor_refusal_carries_its_refused_quote():
    """The watch's tick 0 and k come from the refusal itself (no stored quote was carried before)."""
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    from test_memecoin_evm_entry_executor import _QUOTE, _c, _seams
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-10-08T14:40:00Z", network_id="4663",
                            source_price=1.0)
    q = {**_QUOTE, "buyAmount": "7575758", "minBuyAmount": "7000000"}           # worst 10/7 = 1.43
    seams, _calls = _seams(quote=q, token=((0.0, 6), (7.575758, 6)))
    old = cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT
    cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT = 35.0
    try:
        res = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
                                strategy="alpha_b", source_user_id="trader1", now_iso="t", **seams)
    finally:
        cfg.MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT = old
    c.close()
    assert res["status"] == "skipped_chase" and res["reason"].startswith("copy price minimum is +")
    assert res["dip_route"]["buy_atomic"] == "7575758" and res["dip_route"]["min_atomic"] == "7000000"
