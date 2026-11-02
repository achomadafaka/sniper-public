"""A multihop exit must be able to afford its own WRAP_ETH.

FROGE 50786 was un-exitable for 34 minutes across five attempts because the gas limit was the
literal "900000" and the transaction consumed 899,687 of it. `debug_traceTransaction` shows the V4
swap succeeding through the PoolManager and only the trailing WETH call failing:

    CALL UniversalRouter            ERROR=execution reverted
      CALL PoolManager              <- every swap leg fine
      CALL WETH gas=17432           ERROR=execution reverted
        DELEGATECALL 0xc6b81b4297   ERROR=out of gas

This chain's WETH is a PROXY, so deposit() delegatecalls and costs far more than a standard WETH9.
Replaying the exact reverted calldata: 900,000 reverts, 1,200,000 succeeds.
"""
from sniper.execution.evm_v4_multihop_tx import _multihop_gas


def test_the_limit_clears_the_measured_failure():
    """899,687 was consumed by the swap alone; the wrap needs room after it."""
    assert int(_multihop_gas()) > 899_687


def test_it_clears_the_replay_threshold():
    # 900,000 reverts and 1,200,000 succeeds on the exact FROGE calldata.
    assert int(_multihop_gas()) >= 1_200_000


def test_it_is_a_decimal_string_like_the_literal_it_replaced():
    g = _multihop_gas()
    assert isinstance(g, str) and g.isdigit()


def test_the_operator_can_retune_it(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_GAS", 2_000_000, raising=False)
    assert _multihop_gas() == "2000000"
