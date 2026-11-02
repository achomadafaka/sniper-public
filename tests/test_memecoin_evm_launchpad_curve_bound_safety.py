"""The wrapped launchpad-curve token-safety function through the real, POST-54ffd4ec
`evm_real_entry` path (`sniper/services/memecoin_evm_leg.py`), flag on.

`54ffd4ec` ("Bind ARC exit safety to each actual entry quote") added a startup refusal: for any
chain in `MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS`, `evm_real_entry` now requires `execute_fn` to
support `quote_safety_fn` and `token_safety_fn` to support `bought_amount_atomic`, or it refuses
the entry outright (`status: "unsafe"`) before ever reserving. Chain 4663 is NOT a member of that
set (the curve rung's own clip check is deliberately implemented outside it -- see
`evm_launchpad_curve.wrap_token_safety_fn`'s docstring), so this gate must not fire here; these
tests prove that directly, on the real function, not by re-deriving the gate's logic.
"""
import sqlite3

from sniper.execution import evm_launchpad_curve as curve_mod
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_leg as leg
from sniper.settings import memecoin as cfg

MINT = "0x5e9ca04c6fe6397f287091130a63072a91afd14f"
CURVE = "0xd1255489d894452dd234a001f0fbcab6d4037040"
NATIVE = "0x" + "00" * 20


class _Ev:
    def __init__(self, mint=MINT):
        self.mint = mint
        self.source_user_id = "u1"
        self.network_id = "4663"


def _c(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    from sniper.services import memecoin_arming
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c, str(tmp_path / "kill")


def test_4663_is_not_in_the_quote_bound_safety_set_by_default():
    """The premise every other test here relies on -- pin it directly so a future change to the
    default set fails loudly here instead of silently changing what these tests actually cover."""
    assert "4663" not in getattr(cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", ())


def test_wrapped_safety_fn_reaches_evm_real_entry_and_fills_on_4663(monkeypatch, tmp_path):
    """The new 54ffd4ec startup gate must not fire for 4663 (not in the Arc-only set), so the
    PRE-existing parallel safety-worker path runs unchanged, calling the wrapped fn exactly as
    before -- and the wrapped fn's curve branch decides the entry."""
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", True)
    c, kill = _c(monkeypatch, tmp_path)

    state = {"coin": MINT, "curve": CURVE, "counter": NATIVE, "fee_bps": 100, "state": 0}
    state_fn = lambda chain, mint, rpc_url, http=None: state
    reserves_fn = lambda curve, rpc_url, http=None: (
        1689900000000000001, 994141665187289188709391089)

    def fallback(mint, *, deadline=None, usd=None, bought_amount_atomic=None, **kwargs):
        raise AssertionError("the curve branch must decide this mint, not the standard cascade")

    wrapped = curve_mod.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker="0x" + "9e" * 20, fallback_fn=fallback,
        state_fn=state_fn, reserves_fn=reserves_fn, native_usd_price_fn=lambda chain: 2000.0)

    def execute_fn(_conn, pid, _mint, _size, _now, safety_gate_fn=None):
        assert safety_gate_fn is not None                    # the parallel path, unchanged by 54ffd4ec
        assert safety_gate_fn() == (True, "launchpad_curve_exit_quotable_at_clip")
        return {"status": "filled", "position_id": pid}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:4663", day="2026-09-17", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-09-17T00:00:00Z", trade_size=50.0,
        token_safety_fn=wrapped, execute_fn=execute_fn, kill_path=kill,
    )
    assert result["status"] == "filled", result


def test_wrapped_safety_fn_refuses_and_never_reaches_the_executor(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", True)
    c, kill = _c(monkeypatch, tmp_path)

    state = {"coin": MINT, "curve": CURVE, "counter": NATIVE, "fee_bps": 100, "state": 0}
    state_fn = lambda chain, mint, rpc_url, http=None: state
    reserves_fn = lambda curve, rpc_url, http=None: None         # the curve cannot be read

    def fallback(mint, *, deadline=None, usd=None, bought_amount_atomic=None, **kwargs):
        raise AssertionError("must never fall through once the curve branch has an answer")

    wrapped = curve_mod.wrap_token_safety_fn(
        "4663", "https://rpc.example", taker="0x" + "9e" * 20, fallback_fn=fallback,
        state_fn=state_fn, reserves_fn=reserves_fn, native_usd_price_fn=lambda chain: 2000.0)
    calls = {"exec": 0, "wallet_mutation": 0}

    def execute_fn(_conn, pid, _mint, _size, _now, safety_gate_fn=None):
        calls["exec"] += 1
        # the real executor joins + checks the gate before its first wallet mutation (approval /
        # pre-tx / signing) and releases the reservation on an unsafe verdict -- mirror that here.
        safe, reason = safety_gate_fn()
        if not safe:
            return {"status": "unsafe", "reason": reason}
        calls["wallet_mutation"] += 1
        return {"status": "filled", "position_id": pid}

    result = leg.evm_real_entry(
        c, _Ev(), route="evm:4663", day="2026-09-17", usdc_balance=300.0,
        native_balance=1.0, now_iso="2026-09-17T00:00:00Z", trade_size=50.0,
        token_safety_fn=wrapped, execute_fn=execute_fn, kill_path=kill,
    )
    assert result["status"] == "unsafe"
    assert calls["exec"] == 1
    assert calls["wallet_mutation"] == 0
