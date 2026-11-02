"""The dip watch keeps watching premium refusals made on the post-trade reference (Alpha, 2026-10-08,
on Gamma's LOW for 5ee4deadd: "don't switch a working feature off").

With E11 on-chain the execution premium refuses as "route quote is Rx the post-trade price (cap
Cx)". The watch tracks OUR QUOTE OVER THE SOURCE PRICE (q), so the cap is restated on that basis:
quote/post <= C  <=>  quote/source <= C x post/source. The post-trade price rides on the refusal
(`premium_reference_price`), the source price is the refusal's own drift basis. Since 10-01 the
watch saw 25 eligible source-basis premium refusals (4663 18, 56 7); 9 hits and 1 fill on 4663.
"""
import inspect

import pytest

from sniper.execution import evm_swap_executor as X
from test_memecoin_dip_retry import (  # noqa: F401 (fixtures)
    _cfg, _conn, _dip, _enqueue, _evm_ev, _premium, evm_on)


def _post_trade_refusal(conn, ev, *, cap="1.20", ratio=1.28, post=8.0e-5, quote_price=1.0e-4,
                        drift=60.0):
    res = _premium(conn, ev, ratio=ratio, cap=cap, basis="post-trade price", drift=drift,
                   quote_price=quote_price)
    res.update(premium_reference_price=post, premium_reference_basis="post_trade")
    return res


def test_a_post_trade_premium_refusal_queues_with_its_cap_restated_on_the_source(evm_on):
    c = _conn()
    ev = _evm_ev()
    # quote 1.0e-4 is +60% over the source 6.25e-5; post-trade 8.0e-5 is 1.28x the source.
    assert _enqueue(c, ev, _post_trade_refusal(c, ev))
    [row] = _dip(c)
    source = 1.0e-4 / 1.60
    assert row["dip"]["reference_price"] == pytest.approx(source, rel=1e-9)
    assert row["dip"]["gate"] == "execution_premium_post_trade"
    assert row["dip"]["post_over_source"] == pytest.approx(8.0e-5 / source, rel=1e-9)
    assert row["dip"]["cap_pct"] == pytest.approx((1.20 * 8.0e-5 / source - 1) * 100, rel=1e-9)


@pytest.mark.parametrize("missing", ["premium_reference_price", "source_drift_pct"])
def test_without_its_post_trade_price_or_source_basis_nothing_is_queued(evm_on, missing):
    c = _conn()
    ev = _evm_ev()
    res = _post_trade_refusal(c, ev)
    res[missing] = None
    assert not _enqueue(c, ev, res)
    assert _dip(c) == []


def test_both_premium_refusal_sites_carry_the_reference_the_watch_needs():
    src = inspect.getsource(X.execute_entry_evm)
    assert src.count('"premium_reference_price": premium.reference_price') == 2
    assert src.count('"premium_reference_basis": premium.reference_basis') == 2
