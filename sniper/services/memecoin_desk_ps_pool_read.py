"""Desk-side seam to the exact PumpSwap pool read. The Office must not import sniper.execution (tests/office/test_ledger.py
test_no_funded_imports); this module owns that one read-only import so the Office depends on a desk service instead.
read_pool_state does no signing, sending or accounting: it decodes the Pool account and both vault balances at one slot."""


def read_pool_state(pool, *, rpc):
    from sniper.execution.pumpswap_sell import read_pool_state as read
    return read(pool, rpc=rpc)
