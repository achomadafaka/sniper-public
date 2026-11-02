"""A native-route disagreement cannot override a confirmed operator market-cap stop."""

from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_live_marks as lm
from sniper.services import memecoin_native_exit_rung as rung
from sniper.services import memecoin_real_runner as runner
from sniper.settings import memecoin as cfg
from test_memecoin_native_exit_rung import (
    ATOMIC, ENTRY_PRICE, JUPITER_USD, MINT, POOL, QUOTE_ATOMS, SPCX, SPCX_USD, Cache, _ledger,
)


@pytest.mark.parametrize("sl_mcap,display_ratio,sell_ratio,want_exit", [
    (17000.0, 0.82, 0.45, True),
    (17000.0, 0.85, 0.45, True),
    (17000.0, 0.90, 0.45, True),
    (17000.0, 0.82, 0.90, False),
    (None, 0.82, 0.45, False),
])
def test_only_a_crossed_operator_mcap_stop_bypasses_the_native_hold(
        monkeypatch, sl_mcap, display_ratio, sell_ratio, want_exit):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_RUNG", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    monkeypatch.setattr(runner, "exact_price_relevant", lambda *a, **k: True)
    monkeypatch.setattr(rung, "_VIRTUAL_QUOTE", {POOL: 0})
    monkeypatch.setattr(rung, "_HELD_SINCE", {})
    monkeypatch.setattr(lm, "fresh_quote_usd", lambda *a, **k: {SPCX: SPCX_USD})
    conn, pid = _ledger()
    conn.execute("UPDATE memecoin_positions SET entry_mcap=20000 WHERE id=?", (pid,))
    conn.commit()
    if sl_mcap is not None:
        m.set_position_exit_override(conn, pid, "operator", sl_target_mcap=sl_mcap,
                                     now_iso="2026-10-17T15:00:00+00:00")
    calls = []
    context = {
        "preview_exit_fn": lambda _p: SimpleNamespace(
            expected_usdc=JUPITER_USD * sell_ratio / 0.45, amount_atomic=ATOMIC, route_id="r",
            quoted_at="2026-10-17T15:59:54+00:00"),
        "exit_fn": lambda _c, _p, reason, _n: calls.append(reason) or {"status": "unknown"},
        "decimals_fn": lambda _mint: 6,
    }
    # The pessimistic native pool is ~-18%, above the default 35% loss rail. Jupiter is ~-55%.
    runner.real_exit_sweep(
        conn, {"solana": context}, now_fn=lambda: "2026-10-17T15:59:55+00:00",
        price_fn=lambda _m: {MINT: {"price": ENTRY_PRICE * display_ratio}},
        reserve_witness_cache=Cache(quote_atoms=int(QUOTE_ATOMS * 0.85)))
    assert calls == (["b_stop"] if want_exit else [])
    conn.close()
