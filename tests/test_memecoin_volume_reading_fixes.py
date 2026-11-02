"""Volume reading fixes 1 and 2 (2026-09-12, operator: "do all three").

WMR (4663, 22:59Z): DexScreener's 5-minute volume said $64 while GeckoTerminal showed $300+ in the
same pool for the five minutes before the copied $994 buy; the gate subtracted the buy anyway and
refused on "$0 organic", and the 30 s revival watch expired before the provider caught up.
"""
import inspect

import sniper.services.memecoin_real_runner as R
import sniper.services.memecoin_revival_watch as rw
from sniper.settings import memecoin as cfg


def test_a_volume_below_the_buy_is_not_reduced_by_it():
    assert R.organic_volume(64.25, 994.44) == 64.25, "the buy is provably not in the number"
    assert R.organic_volume(1500.0, 994.0) == 506.0, "at or above the buy the subtraction stays"
    assert R.organic_volume(994.0, 994.0) == 0.0
    assert R.organic_volume(None, 994.0) is None
    assert R.organic_volume(50.0, None) == 50.0


def test_the_gate_and_the_render_share_the_rule():
    """The PROPERTY, not the spelling: neither the gate nor the render does its own subtraction.

    This used to pin the exact call text and broke the moment organic_volume took the index-lag
    arguments (2026-09-16). A guard that fails on a legitimate edit teaches people to edit the
    guard.
    """
    src = inspect.getsource(R.volume_alive_decision)
    assert "organic_volume(vol_m5" in src and "organic_volume(vol_h1" in src
    assert "organic_volume(raw" in inspect.getsource(R._volume_notification_evidence)
    for name in ("volume_alive_decision", "_volume_notification_evidence"):
        body = inspect.getsource(getattr(R, name))
        assert "- _sig" not in body and "- signal_usd" not in body, (
            "%s subtracts the source buy itself instead of asking organic_volume" % name)


def test_the_revival_window_and_poll_are_per_chain(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REVIVAL_WATCH_WINDOW_S", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REVIVAL_WATCH_POLL_S", 1.0)
    monkeypatch.setenv("MEMECOIN_REVIVAL_WATCH_WINDOW_S_4663", "150")
    monkeypatch.setenv("MEMECOIN_REVIVAL_WATCH_POLL_S_4663", "5")
    monkeypatch.delenv("MEMECOIN_REVIVAL_WATCH_WINDOW_S_solana", raising=False)
    monkeypatch.delenv("MEMECOIN_REVIVAL_WATCH_POLL_S_solana", raising=False)
    assert cfg.revival_watch_window_s("4663") == 150.0 and cfg.revival_watch_poll_s("4663") == 5.0
    assert cfg.revival_watch_window_s("solana") == 30.0 and cfg.revival_watch_poll_s("solana") == 1.0


def test_a_cleared_watch_marks_its_reask_and_the_hook_extends_the_age_budget():
    class Ev:
        mint = "M"
        fomo_event_id = "e"
    rw.WATCHES.clear(); rw._SPENT.clear()
    ev = Ev()
    rw.enqueue(rw.WATCHES, event=ev, mint="M", origin_event_id="e", refused_at="t", mcap=1.0,
               needed=1.0, now=0.0, window_s=150.0, poll_s=5.0, network_id="4663")
    seen = []
    rw.poll(rw.WATCHES, now=5.5, floor=0.0, stats_fn=lambda m, network_id=None: {},
            reinject_fn=seen.append, clears_fn=lambda w, st: True)
    assert seen == [ev] and ev.revival_reask is True
    rw.WATCHES.clear(); rw._SPENT.clear()
    hook_src = inspect.getsource(R.make_real_hook)
    assert 'if getattr(ev, "revival_reask", False) and age_cap:' in hook_src
    assert "age_cap = float(age_cap) + cfg.revival_watch_window_s(chain)" in hook_src
