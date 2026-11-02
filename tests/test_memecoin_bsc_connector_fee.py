"""Use the verified deep WBNB/USDT bridge, without extending the quote batch."""
import json
import os
import subprocess
import sys

import pytest
from eth_abi import decode

from sniper.execution import evm_dex_quote as quote
from sniper.settings import memecoin as cfg

WBNB = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'
USDT = '0x55d398326f99059ff775485246999027b3197955'
BASE = '0x94be0bba8e1e303fe998c9360b57b826f1a4f828'


def _settings(**overrides):
    env = dict(os.environ)
    for key in ('MEMECOIN_EVM_V3_CONNECTOR_56', 'MEMECOIN_EVM_V3_CONNECTOR_FEE_56'):
        env.pop(key, None)
    env.update(overrides)
    return json.loads(subprocess.check_output([sys.executable, '-c',
        'import json; from sniper.settings import memecoin as c; '
        'print(json.dumps(c.memecoin_evm_v3_connectors("56")))'], env=env, text=True))


def test_canonical_bridge_default_is_verified_point_zero_one_percent_pool():
    assert _settings() == [[USDT, 100]]


@pytest.mark.parametrize('fee', ['500', '10000'])
def test_operator_fee_override_remains_authoritative(fee):
    assert _settings(MEMECOIN_EVM_V3_CONNECTOR_FEE_56=fee) == [[USDT, int(fee)]]


def test_custom_connector_keeps_legacy_fee_unless_operator_overrides_it():
    assert _settings(MEMECOIN_EVM_V3_CONNECTOR_56=BASE) == [[BASE, 10000]]


@pytest.mark.parametrize('sell,buy,fees', [
    (WBNB, BASE, [100, 10000]), (BASE, WBNB, [10000, 100]),
])
def test_forward_and_reverse_bridge_use_same_single_batch(sell, buy, fees):
    batches = []

    def batch(calls):
        batches.append(calls)
        answers = []
        for _, data in calls:
            if data[2:10] != quote.QUOTE_EXACT_INPUT:
                answers.append(None)
                continue
            path, _ = decode(['bytes', 'uint256'], bytes.fromhex(data[10:]))
            actual_fees = [int.from_bytes(path[20:23], 'big'),
                           int.from_bytes(path[43:46], 'big')]
            answers.append('0x' + format(1000, '064x') if actual_fees == fees else None)
        return answers

    def call(*_):
        pytest.fail('Unexpected serial quoter fallback')
    call.batch = batch
    q = quote.dex_v3_quote('56', sell, buy, 1000, '0x' + 'ab' * 20,
        cfg_addr=cfg.memecoin_evm_v3_addresses('56'), call_fn=call,
        slippage_bps=300, pair_tokens_fn=lambda *_: ())
    assert q is not None and q['route']['fees'] == fees
    assert len(batches) == 1
    assert len(batches[0]) == 2 * len(cfg.memecoin_evm_v3_fee_tiers('56'))
