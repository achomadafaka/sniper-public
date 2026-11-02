"""Item 10 — two-sided chase guard: the real path skips entries that are >X% BELOW the trader's
price (a dump in progress / falling knife), not just >X% above. Opt-in so the paper A/B is
unchanged (the below-guard only fires when chase_below_pct is set — the real leg sets it)."""
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_feed import CopyEvent
from sniper.services.memecoin_copy_trader import copy_event_to_arms


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _buy(their_price=1.0):
    return CopyEvent("u1", "alice", "MINT", "T", "buy", their_price, 1e6, 3000.0,
                     True, "e1", "t", "t", 0.3)


def test_below_guard_skips_falling_knife_when_enabled():
    c = _c()
    fill = lambda *a, **k: (0.8, "dexscreener")   # our fill 20% BELOW their 1.0 -> dump
    out = copy_event_to_arms(c, _buy(1.0), 100.0, "t", arms=["control"],
                             fill_fn=fill, chase_below_pct=15.0)
    assert out["control"] == "skipped_dump"


def test_below_guard_off_by_default_paper_unchanged():
    c = _c()
    fill = lambda *a, **k: (0.8, "dexscreener")   # 20% below, but below-guard disabled
    out = copy_event_to_arms(c, _buy(1.0), 100.0, "t", arms=["control"], fill_fn=fill)
    assert out["control"] == "opened"             # paper: entry still taken


def test_above_guard_still_fires():
    c = _c()
    # 50% above their 1.0. The gate now applies FUNDED's cap — 30% above the nano band, 35% inside
    # it — instead of paper's retired flat 15%, so this still fires and does so under the family
    # that names which cap refused it.
    fill = lambda *a, **k: (1.5, "dexscreener")
    out = copy_event_to_arms(c, _buy(1.0), 100.0, "t", arms=["control"],
                             fill_fn=fill, chase_below_pct=15.0)
    assert out["control"] == "skipped_chase_over_funded_cap"
