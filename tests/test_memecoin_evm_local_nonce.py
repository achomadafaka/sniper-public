"""Offline regressions for local unbroadcast EVM nonce cleanup."""
import sqlite3

import pytest


@pytest.mark.parametrize("later_allocation", [False, True])
def test_affordability_discard_releases_unbroadcast_nonce(monkeypatch, later_allocation):
    from eth_account import Account
    from sniper.execution import chain1_protect as C, evm_real_seams as S, evm_wallet
    from sniper.settings import memecoin as cfg
    from test_memecoin_chain1_entry_tip_floor import _rpc, _history, KEY, SWAP
    monkeypatch.setenv('MEMECOIN_CHAIN1_FEE_HEADROOM', '1')
    monkeypatch.setenv('MEMECOIN_EVM_TYPE2_CHAINS', '1')
    monkeypatch.setenv('MEMECOIN_EVM_PREAPPROVE_CHAINS', '1')
    monkeypatch.setenv('MEMECOIN_EVM_TIP_GWEI_1', '0.05')
    monkeypatch.setenv('MEMECOIN_EVM_TIP_CAP_GWEI_1', '2')
    monkeypatch.setenv('MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1', '1')
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_GAS_ENTRY_NATIVE_BY_CHAIN', {'1': 0.001})
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_GAS_EXIT_NATIVE_BY_CHAIN', {'1': 0.001})
    h = C.FeeHeadroom(lambda: _history(0.18, [0.1]))
    h.refresh()
    monkeypatch.setattr(C, '_HEADROOM', {'1': h})
    monkeypatch.setattr(C, 'start_refresher', lambda *_args, **_kw: None)
    monkeypatch.setattr(evm_wallet, 'native_balance_raw_or_none', lambda *_args, **_kw: 9 * 10**14)
    ex = S.build_real_evm_executor(acct=Account.from_key(KEY), chain_id='1', rpc_url='fake', http=_rpc())
    real_sign = S.sign_tx
    allocated = []
    def fee_change(tx, acct, **kw):
        h._state = (h._state[0], 4 * 10**8, h._state[2])
        signed = real_sign(tx, acct, **kw)
        if later_allocation and not allocated:
            allocated.append(True)
            assert ex['_sign'](dict(SWAP, value=0))[2] == 8
        return signed
    monkeypatch.setattr(S, 'sign_tx', fee_change)
    assert ex['_make_exit_sign_fn'](sqlite3.connect(':memory:'), 1, 'close_claim')(dict(SWAP, value=0)) is None
    assert ex['_sign'](dict(SWAP, value=0))[2] == (9 if later_allocation else 7)
