"""Trial arm wiring: the marker travels from the runner's entry hook to the reservation on both legs.

The reservation validates the marker again inside its transaction (test_memecoin_trial_reserve);
these tests pin that each hop forwards it, because a dropped keyword would not fail anything else:
the reservation would just refuse every trial entry as `trial_unmarked`.
"""
import inspect
import sqlite3
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services import memecoin_evm_leg as evm_leg
from sniper.services import memecoin_real_leg as sol_leg
from sniper.services import memecoin_real_runner as RR
from sniper.settings import memecoin as cfg

MARKER = {"trial_id": 7, "rule": "3a0b141ee", "clip_usd": 20.0}
STOP = SimpleNamespace(allowed=False, approved_size_usdc=0.0, reason="stop here",
                       clamped=False, duplicate_blocker=None)


def _conn(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _capture(seen):
    def reserve(*_a, **kw):
        seen.update(kw)
        return STOP, None
    return reserve


def test_the_solana_leg_hands_the_marker_to_the_reservation(monkeypatch, tmp_path):
    c, seen = _conn(monkeypatch), {}
    monkeypatch.setattr(sol_leg, "reserve_entry", _capture(seen))
    ev = SimpleNamespace(mint="MINT", source_user_id="u1", created_at="t")
    sol_leg.real_entry(c, ev, day="2026-10-01", usdc_balance=300.0, sol_balance=1.0,
                       now_iso="2026-10-01T00:00:00Z", trade_size=20.0,
                       token_safety_fn=lambda mint, **kw: (True, "ok"),
                       execute_fn=lambda *a, **k: {}, kill_path=str(tmp_path / "kill"),
                       trial=MARKER)
    assert seen.get("trial") == MARKER


def test_the_solana_leg_hands_the_marker_to_the_evm_route(monkeypatch, tmp_path):
    c, seen = _conn(monkeypatch), {}
    ev = SimpleNamespace(mint="0x" + "ab" * 20, source_user_id="u1", created_at="t",
                         network_id="4663")
    sol_leg.real_entry(c, ev, day="2026-10-01", usdc_balance=300.0, sol_balance=1.0,
                       now_iso="2026-10-01T00:00:00Z", trade_size=20.0,
                       token_safety_fn=lambda mint, **kw: (True, "ok"),
                       execute_fn=lambda *a, **k: {}, kill_path=str(tmp_path / "kill"),
                       evm_allowlist={"4663"},
                       resolved_identity=SimpleNamespace(network_id="4663"),
                       evm_entry_fn=lambda *a, **kw: seen.update(kw) or {"status": "x"},
                       trial=MARKER)
    assert seen.get("trial") == MARKER


def test_the_runner_evm_dispatch_hands_the_marker_to_the_evm_leg(monkeypatch):
    seen = {}
    monkeypatch.setattr(evm_leg, "evm_real_entry", lambda *a, **kw: seen.update(kw) or {"status": "x"})
    ctx = {"usdc_fn": lambda: 300.0, "native_fn": lambda: 1.0, "token_safety_fn": None,
           "execute_fn": None}
    fn = RR._make_evm_entry_fn({"4663": ctx})
    ev = SimpleNamespace(mint="0x" + "ab" * 20, source_user_id="u1", network_id="4663")
    fn(None, ev, route="evm:4663", day="2026-10-01", now_iso="2026-10-01T00:00:00Z",
       trade_size=20.0, trial=MARKER)
    assert seen.get("trial") == MARKER


def test_the_evm_leg_hands_the_marker_to_the_reservation(monkeypatch, tmp_path):
    c, seen = _conn(monkeypatch), {}
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    monkeypatch.setattr(evm_leg, "reserve_entry_evm", _capture(seen))
    ev = SimpleNamespace(mint="0x" + "ab" * 20, source_user_id="u1", network_id="4663")
    evm_leg.evm_real_entry(c, ev, route="evm:4663", day="2026-10-01", usdc_balance=300.0,
                           native_balance=1.0, now_iso="2026-10-01T00:00:00Z", trade_size=20.0,
                           token_safety_fn=lambda _m: (True, "ok"), execute_fn=lambda *a: {},
                           kill_path=str(tmp_path / "kill"), trial=MARKER)
    assert seen.get("trial") == MARKER


def test_the_runner_finds_the_open_trial_for_the_entry(monkeypatch):
    c = _conn(monkeypatch)
    ev = SimpleNamespace(source_user_id="u1")
    assert RR._trial_for_entry(c, ev, "solana") is None
    tid = m.start_trial(c, source_user_id="u1", chain="solana", clip_usd=20.0, rule="3a0b141ee",
                        actor="t", now_iso="2026-10-01T00:00:00+00:00")
    assert RR._trial_for_entry(c, ev, "solana") == {**MARKER, "trial_id": tid}
    assert RR._trial_for_entry(c, ev, "4663") is None
    c.close()
    assert RR._trial_for_entry(c, ev, "solana") is None     # an unreadable ledger: no marker


def test_the_hook_sizes_a_trial_entry_and_passes_its_marker_unless_manual():
    src = inspect.getsource(RR.real_entry_hook)
    call = src.index("res = real_entry(")
    assert "_trial = None if _manual_entry else _trial_for_entry(" in src[:call]
    assert "trade_size = min(float(trade_size), float(_trial[\"clip_usd\"]))" in src[:call]
    assert "trial=_trial" in src[call:src.index(")", src.index("trial=_trial", call))]
