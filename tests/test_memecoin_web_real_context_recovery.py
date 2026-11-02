"""A failed startup build of the web's funded executor heals on the next manual action.

Live 2026-08-14: the web server built its real context once at import, hit a bad moment (RPC
flake / mid-edit env), and cached the failure forever — four manual sells answered 503 "real
execution not configured" while a position needed out, until a service restart. The installer
retries lazily, cooldown-bounded, and stays fail-closed until a build actually succeeds.
"""
import pytest

from sniper.services.memecoin_real_runner import make_real_fns_installer


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_a_later_call_heals_a_failed_startup(monkeypatch):
    ctx = {}
    calls = []

    def build():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("rpc flake at boot")
        return {"solana": {"exit_fn": lambda *a: None}}

    clock = Clock()
    ensure = make_real_fns_installer(ctx, build_fn=build, cooldown_s=30.0, clock=clock)
    assert ensure() is False and "real_mark_fn" not in ctx
    clock.t += 31.0
    assert ensure() is True
    # 2026-09-05: the recovery door installs INTENT writers, never executors (that re-opened 44706)
    assert callable(ctx["real_exit_intent_fn"]) and callable(ctx["real_buy_fn"]) and callable(ctx["real_mark_fn"])
    assert "real_exit_fn" not in ctx and "real_partial_exit_fn" not in ctx


def test_present_fns_are_a_noop_and_never_rebuilt():
    ctx = {"real_mark_fn": lambda *a: "orig"}
    ensure = make_real_fns_installer(ctx, build_fn=lambda: (_ for _ in ()).throw(AssertionError),
                                     clock=Clock())
    assert ensure() is True
    assert ctx["real_mark_fn"](1) == "orig"


def test_cooldown_bounds_the_retry_storm():
    ctx = {}
    calls = []

    def build():
        calls.append(1)
        raise RuntimeError("still down")

    clock = Clock()
    ensure = make_real_fns_installer(ctx, build_fn=build, cooldown_s=30.0, clock=clock)
    for _ in range(10):
        ensure()                       # a click storm while broken
    assert len(calls) == 1, "one build attempt per cooldown window"
    clock.t += 31.0
    ensure()
    assert len(calls) == 2


def test_an_empty_context_is_disarmed_not_installed():
    ctx = {}
    ensure = make_real_fns_installer(ctx, build_fn=lambda: {}, cooldown_s=0.0, clock=Clock())
    assert ensure() is False
    assert "real_mark_fn" not in ctx and "real_exit_intent_fn" not in ctx, "disarmed/no-creds must keep the loud 503"
