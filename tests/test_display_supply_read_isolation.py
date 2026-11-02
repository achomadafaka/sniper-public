"""A display-supply read must never outlive the test that started it (rh_fast_entry flake, 2026-10-07).

`memecoin_real_runner.pin_display_total_supply` runs its read on a MODULE-GLOBAL one-thread pool, by
design (it never waits on the sweep). Under pytest a read started by one test (the blind-4663 trail
sweeps in test_memecoin_real_runner.py) ran during the NEXT test in the same xdist worker and called
`evm_wallet._http` through that test's network guard: tests/test_memecoin_rh_fast_entry.py then saw
('evm_wallet._http', False) in its `net.calls` and failed ~1 run in 5 in a combined run, never alone.

The two tests below are that sequence, made deterministic: the first starts a read that calls
`evm_wallet._http` 0.3 s later; the second guards `_http` straight away. The autouse
`_no_leaked_display_supply_read` (tests/conftest.py) waits for the first test's read at its own
teardown, so the second test sees nothing. Without it the second fails. In one process (-n 0) the
order is fixed; under xdist the two may land on different workers, which can only pass."""
import time

from sniper.execution import evm_wallet
from sniper.services import memecoin_evm_price as ep
from sniper.services import memecoin_real_runner as rr
from sniper.settings import memecoin as cfg

CALLS_SEEN_BY_THE_NEXT_TEST = []


def test_a_sweep_starts_a_display_supply_read(monkeypatch):
    from sniper.persistence import db_memecoin as m
    from test_memecoin_cap_supply import _ledger, _open56

    def slow_read(chain, mint):
        time.sleep(0.3)
        try:
            evm_wallet._http("POST", "", body="{}")    # looked up at call time, as _rpc_impl does
        except Exception:                             # noqa: BLE001 - this test's own call fails offline
            pass
        return None, False, 300.0
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56"}, raising=False)
    monkeypatch.setattr(ep, "evm_total_supply", slow_read)
    monkeypatch.setattr(ep, "_supply_cache", {})
    c = _ledger()
    _open56(c)
    assert rr.pin_display_total_supply(c, dict(m.get_position(c, 1))) is None   # started, not waited on
    assert rr._DISPLAY_SUPPLY_READS


def test_the_next_test_never_sees_that_read(monkeypatch):
    monkeypatch.setattr(evm_wallet, "_http", lambda *a, **k: CALLS_SEEN_BY_THE_NEXT_TEST.append(a))
    time.sleep(0.6)
    assert CALLS_SEEN_BY_THE_NEXT_TEST == []
    assert not rr._DISPLAY_SUPPLY_READS
