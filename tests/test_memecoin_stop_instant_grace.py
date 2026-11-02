"""Instant-bypass grace window (2026-09-04, operator).

DEBTCOIN (pos 50664) b_stopped on the FIRST exact quote within seconds of fill on 2026-09-04, then
round-tripped: sold at -59.7% on the depth bypass (MEMECOIN_REAL_STOP_INSTANT_PCT,
tests/test_memecoin_stop_instant_depth.py), a paper mirror on the same mint priced it back to
0.0003967 -- ABOVE our own 0.0003865 entry -- three minutes later, then geckoterminal caught it at
-76.5% ninety seconds after that. A fresh fill on a thin/young pool can wick past the depth on its
first read and revert before a second one; that is the bad-tick case the debounce exists for, not
the FATCOIN/MEOW slow-bleed case the depth itself was built for one day earlier
(tests/test_memecoin_stop_instant_depth.py).

Operator: "how can we avoid getting instantly liquidated on a not-bad signal ... add a timer in
which we cannot sell ... somewhere after 20 seconds of entering the signal."

Narrower than a hard no-sell timer: a genuine continued drop still exits on the SECOND confirming
read (the pre-existing sweep-count debounce) -- it just cannot sell off a single wick-shaped read
in the position's first MEMECOIN_REAL_STOP_INSTANT_GRACE_S seconds.

Gamma review before deploy caught two defects in the first cut (both fixed here, before anything
shipped):

1. The grace gate is on MAGNITUDE past the frozen catastrophe floor, not the reason label. X (pos
   50341) is not a second launch-chaos case -- entry_token_age_s 609,811s (7.06 days), a genuine
   -90.9% death spiral. A standard funded 4663 contract freezes stop_pct=35 < catastrophe_pct=80,
   and b_stop's own check in alpha_b_real_exit_reason returns BEFORE catastrophe is ever
   considered -- so a move straight through -80% still reports reason="b_stop". Gating the grace
   window on that label alone would have deferred X's own instant sell.
2. Age is read from opened_recorded_at (stamped when open_position() runs, at/after the confirmed
   fill), not opened_at (the pre-quote/sign/send/confirm decision-start timestamp both executors
   reuse) -- same bug class as finding-opened-at-is-decision-start-not-the-fill-2026-09-03.
"""
import ast
import pathlib
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr
from sniper.settings import memecoin as cfg

RUNNER = pathlib.Path(rr.__file__)


def _sweep_source():
    from _source_guard import code_of
    tree = ast.parse(code_of(RUNNER))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "real_exit_sweep":
            return ast.unparse(node)
    raise AssertionError("real_exit_sweep not found")


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_real(c, mint="GRACEQ", entry_price=1.0, opened_at="2026-09-04T00:00:00+00:00",
               opened_recorded_at=None):
    # opened_recorded_at defaults to opened_at here, not to real wall-clock "now" (open_position()'s
    # own default) -- these tests run at a fixed fictional instant, and the grace window reads
    # opened_recorded_at, so leaving it unset would silently measure age against the real clock.
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="TKR",
                          entry_price=entry_price, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=0, their_size_usd=0.0, opened_at=opened_at,
                          opened_recorded_at=opened_recorded_at or opened_at,
                          strategy="alpha_b", execution_mode="real", network_id="solana")
    m.insert_fill(c, position_id=pid, kind="entry", price=entry_price,
                  qty=50.0 / entry_price, notional=50.0, fill_source="chain", fraction=1.0,
                  created_at=opened_at)
    return pid


def _sweep(c, price, now_iso, exits_log):
    ctx = {"exit_fn": lambda conn, pos, reason, now: exits_log.append(reason) or {"status": "closed"},
           "token_fn": lambda mint: 0.0,
           "preview_exit_fn": lambda pos: {
               "expected_usdc": price / pos["entry_price"] * pos["entry_notional"],
           }}
    rr.real_exit_sweep(c, {"solana": ctx},
                       price_fn=lambda mints: {"GRACEQ": {"price": price}},
                       now_fn=lambda: now_iso)


def _rails(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_UNQUOTABLE_EXIT_S", 86400.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 80.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SECONDS", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_GRACE_S", 20.0)


# ---------------------------------------------------------------- the default

def test_the_default_is_twenty_seconds_matching_the_operator_ask():
    assert cfg.MEMECOIN_REAL_STOP_INSTANT_GRACE_S == 20.0


# ---------------------------------------------------------------- behavior

def test_a_wick_inside_the_grace_window_does_not_sell_on_the_first_read(monkeypatch):
    _rails(monkeypatch)
    c = _c()
    opened = "2026-09-04T00:00:00+00:00"
    pid = _open_real(c, opened_at=opened)
    exits = []
    _sweep(c, 0.40, "2026-09-04T00:00:05+00:00", exits)   # 5s old, -60%: past depth, inside grace
    assert exits == []
    assert m.get_position(c, pid)["stop_hits"] == 1


def test_a_sustained_drop_still_exits_on_the_second_read_inside_the_grace_window(monkeypatch):
    """Not a hard no-sell timer: the normal 2-sweep debounce still governs and can fire well
    inside the 20s window, just not off a single read."""
    _rails(monkeypatch)
    c = _c()
    opened = "2026-09-04T00:00:00+00:00"
    _open_real(c, opened_at=opened)
    exits = []
    _sweep(c, 0.40, "2026-09-04T00:00:05+00:00", exits)
    assert exits == []
    _sweep(c, 0.40, "2026-09-04T00:00:07+00:00", exits)   # still 7s old, well inside the 20s grace
    assert exits == ["b_stop"]


def test_a_recovering_wick_releases_the_counter_same_as_outside_the_grace_window(monkeypatch):
    _rails(monkeypatch)
    c = _c()
    opened = "2026-09-04T00:00:00+00:00"
    pid = _open_real(c, opened_at=opened)
    exits = []
    _sweep(c, 0.40, "2026-09-04T00:00:05+00:00", exits)    # -60%: wick
    _sweep(c, 0.95, "2026-09-04T00:00:06+00:00", exits)    # -5%: recovers
    assert exits == [] and m.get_position(c, pid)["stop_hits"] == 0


def test_the_same_wick_sells_instantly_once_past_the_grace_window(monkeypatch):
    _rails(monkeypatch)
    c = _c()
    opened = "2026-09-04T00:00:00+00:00"
    pid = _open_real(c, opened_at=opened)
    exits = []
    _sweep(c, 0.40, "2026-09-04T00:00:25+00:00", exits)   # 25s old: past the 20s grace
    assert exits == ["b_stop"]
    assert m.get_position(c, pid)["stop_hits"] == 0        # instant path never bumps the counter


def test_grace_zero_restores_age_blind_instant_selling(monkeypatch):
    _rails(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_GRACE_S", 0.0)
    c = _c()
    opened = "2026-09-04T00:00:00+00:00"
    _open_real(c, opened_at=opened)
    exits = []
    _sweep(c, 0.40, opened, exits)   # same instant as fill, grace disabled
    assert exits == ["b_stop"]


def test_catastrophe_is_never_gated_when_it_is_also_the_reason_label(monkeypatch):
    """The -80% floor is a death-spiral backstop, not a wick guard. It must stay instant even at
    held == 0. Trivial case: stop disabled, so the reason itself is 'catastrophe'."""
    _rails(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 0.0)   # stop off; catastrophe is the only floor
    c = _c()
    opened = "2026-09-04T00:00:00+00:00"
    _open_real(c, opened_at=opened)
    exits = []
    _sweep(c, 0.15, opened, exits)   # -85%, same instant as fill
    assert exits == ["catastrophe"]


def test_a_fresh_b_stop_past_the_catastrophe_floor_keeps_its_instant_bypass(monkeypatch):
    """The regression Gamma caught before deploy. reason=='b_stop' is not proof the catastrophe
    floor wasn't breached: b_stop's own check in alpha_b_real_exit_reason returns BEFORE
    catastrophe is ever considered, so with a tighter stop frozen (here: stop=50 < catastrophe=80,
    the same shape as every standard funded 4663 contract at stop_pct=35/catastrophe_pct=80) a
    death spiral straight through the floor still reports reason="b_stop". Position 50341 (X):
    -90.9%, held 6.4s, exit_reason b_stop, entry_token_age_s 609,811s (7.06 days -- not a fresh
    thin pool). Gating the grace window on the label alone would have deferred its own instant
    sell. Fresh (held == 0) must not be a defense once the catastrophe floor is breached."""
    _rails(monkeypatch)   # stop=50, catastrophe=80: b_stop nominates first, same as live 4663
    c = _c()
    opened = "2026-09-04T00:00:00+00:00"
    _open_real(c, opened_at=opened)
    exits = []
    _sweep(c, 0.10, opened, exits)   # -90%: past catastrophe(80), b_stop still nominates first
    assert exits == ["b_stop"]        # instant despite held == 0


def test_a_fresh_wick_short_of_the_catastrophe_floor_still_gets_its_grace(monkeypatch):
    """The fix must not swallow the ORIGINAL protection: a fresh loss between the stop and the
    catastrophe floor (here -60%, between stop=50 and catastrophe=80) is exactly DEBTCOIN's shape
    and must still lose its instant bypass inside the grace window."""
    _rails(monkeypatch)
    c = _c()
    opened = "2026-09-04T00:00:00+00:00"
    pid = _open_real(c, opened_at=opened)
    exits = []
    _sweep(c, 0.40, "2026-09-04T00:00:05+00:00", exits)   # -60%: past stop, short of catastrophe
    assert exits == []
    assert m.get_position(c, pid)["stop_hits"] == 1


# ---------------------------------------------------------------- source shape

def test_grace_only_ever_applies_to_a_b_stop_nomination():
    src = _sweep_source()
    i = src.index("_in_grace = ")
    clause = src[i:i + 200]
    assert "reason == 'b_stop'" in clause, (
        "the grace window must only ever withhold the instant bypass from a b_stop nomination")


def test_grace_is_gated_on_magnitude_past_the_catastrophe_floor_not_the_reason_label():
    """The regression: a bare reason=='b_stop' check is not sufficient, because b_stop nominates
    before catastrophe is ever considered. The gate must read the frozen catastrophe_pct and
    compare it to the reading, not just branch on which string `reason` holds."""
    src = _sweep_source()
    assert "_effective_catastrophe_pct(pos, override)" in src
    i = src.index("_past_catastrophe_floor = ")
    clause = src[i:i + 200]
    assert "_grace_catastrophe_pct" in clause and "_instant_loss" in clause
    i = src.index("_in_grace = ")
    clause = src[i:i + 200]
    assert "_past_catastrophe_floor" in clause, (
        "a b_stop past the frozen catastrophe floor must keep its instant bypass")


def test_grace_reads_time_since_the_confirmed_fill_not_the_decision_start():
    src = _sweep_source()
    i = src.index("_in_grace = ")
    clause = src[i:i + 300]
    assert "_held_s(pos.get('opened_recorded_at') or pos.get('opened_at'), now_iso)" in clause, (
        "opened_at is stamped at decision start, before quote/sign/send/confirm -- "
        "opened_recorded_at is stamped when open_position() runs, at/after the confirmed fill")


def test_zero_disables_grace_without_dividing_or_gating_at_zero():
    src = _sweep_source()
    assert "_instant_grace_s > 0" in src


def test_grace_sits_before_sell_now_and_cannot_reassign_reason():
    src = _sweep_source()
    assert src.index("_in_grace = ") < src.index("_sell_now = ")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "_in_grace"):
            for sub in ast.walk(node.value):
                assert not (isinstance(sub, ast.Name) and sub.id == "reason"
                           and isinstance(sub.ctx, ast.Store)), "must never reassign reason"
            break
    else:
        raise AssertionError("_in_grace assignment not found")
