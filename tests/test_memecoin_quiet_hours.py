"""Operator quiet window (2026-09-11): automatic funded entries stand down 00-06 UTC."""
import calendar, time
from sniper.services import memecoin_arming as a


def _epoch(hhmm):
    return calendar.timegm(time.strptime("2026-09-11T%s:00" % hhmm, "%Y-%m-%dT%H:%M:%S"))


def test_window_parses_and_is_off_when_empty_or_malformed():
    assert a.quiet_hours_window("0-6") == (0, 6)
    assert a.quiet_hours_window("22-4") == (22, 4)
    assert a.quiet_hours_window("") is None
    assert a.quiet_hours_window("six") is None
    assert a.quiet_hours_window("6-6") is None
    assert a.in_quiet_hours(_epoch("03:00"), spec="") is False


def test_membership_end_exclusive_and_wraparound():
    assert a.in_quiet_hours(_epoch("00:00"), spec="0-6") is True
    assert a.in_quiet_hours(_epoch("05:59"), spec="0-6") is True
    assert a.in_quiet_hours(_epoch("06:00"), spec="0-6") is False
    assert a.in_quiet_hours(_epoch("23:30"), spec="22-4") is True
    assert a.in_quiet_hours(_epoch("03:30"), spec="22-4") is True
    assert a.in_quiet_hours(_epoch("12:00"), spec="22-4") is False


def test_legs_stand_down_automatic_and_pass_manual(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_real_leg as sol, memecoin_evm_leg as evm
    monkeypatch.setattr(a, "in_quiet_hours", lambda *k, **kw: True)
    monkeypatch.setattr(sol, "real_entries_allowed", lambda *k, **kw: True)
    monkeypatch.setattr(evm, "real_entries_allowed", lambda *k, **kw: True)
    auto = SimpleNamespace(signal_source="follow_activity", mint="So1", network_id="solana")
    sol_kw = dict(day="2026-09-11", usdc_balance=100.0, sol_balance=1.0, now_iso="2026-09-11T03:00:00+00:00",
                  trade_size=10.0, kill_path="/nonexistent", token_safety_fn=None, execute_fn=None)
    evm_kw = dict(route={"chain_id": "4663"}, day="2026-09-11", usdc_balance=100.0, native_balance=1.0,
                  now_iso="2026-09-11T03:00:00+00:00", trade_size=10.0, kill_path="/nonexistent", token_safety_fn=None, execute_fn=None)
    assert sol.real_entry(None, auto, **sol_kw) == {"status": "disarmed", "reason": "quiet_hours"}
    assert evm.evm_real_entry(None, auto, **evm_kw) == {"status": "disarmed", "reason": "quiet_hours"}
    # a manual buy never consults the window
    manual = SimpleNamespace(signal_source="manual", mint="So1", network_id="solana")
    consulted = []
    monkeypatch.setattr(a, "in_quiet_hours", lambda *k, **kw: consulted.append(1) or True)
    try:
        sol.real_entry(None, manual, **sol_kw)
    except Exception:
        pass   # past the gate; the rest of the leg needs a real ledger and is not under test
    assert not consulted
