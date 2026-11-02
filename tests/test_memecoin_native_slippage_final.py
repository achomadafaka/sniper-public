"""SHAV #74109: a native minimum-out failure must not authorize a fresh-price buy."""
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_onchain_lane as O
from sniper.services.memecoin_j7_backup import native_refusal
from sniper.services.memecoin_refusal_capture import SLIPPAGE_FAIL_REASONS
from sniper.settings import memecoin as cfg
from test_memecoin_slot_fallback_classes import (
    SIG, DETECTED, _conn, _row, _sweep, fresh_registries,
)


@pytest.mark.parametrize('reason', sorted(SLIPPAGE_FAIL_REASONS))
@pytest.mark.parametrize('fast_only', [True, False])
def test_native_slippage_is_final_across_ordinary_and_j7_paths(monkeypatch, reason, fast_only):
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_FAST_LANE_ONLY', fast_only)
    conn = _conn()
    _row(conn, terminal='failed', pending='failed', reason=reason, tx='native-signed-tx')
    # The durable release remains authoritative after process-local skip notes disappear.
    assert O.slot_lane_verdict(conn, SIG, DETECTED) == ('decided', None)
    seen = []
    _sweep(conn, seen)
    assert seen == []
    for strict in (False, True):
        ev = SimpleNamespace(fomo_event_id='onchain:' + SIG, created_at=DETECTED,
                             j7_native_failure_backup=strict)
        assert native_refusal(conn, ev) == 'j7_native_decided'
