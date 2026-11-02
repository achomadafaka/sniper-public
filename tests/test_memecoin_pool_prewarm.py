"""Pre-entry pool discovery lane.

The two things that can silently go wrong here are the candidate SELECTION (resolving coins we
already have, or missing coins we do not) and the TIME BOUND (the SQLite space-separator trap).
Both are pure functions and both are tested. The RPC walk itself is `resolve_position_pool`,
which has its own coverage — this lane only feeds it a synthetic no-fill position.
"""
import sqlite3

import pytest

from scripts.memecoin_pool_prewarm import (
    _iso_minutes_ago, prewarm_once, unresolved_recent_mints,
)


@pytest.fixture
def ledger():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_events (mint TEXT, side TEXT, received_at TEXT, "
              "network_id TEXT, source_user_id TEXT)")
    c.execute("CREATE TABLE memecoin_wallets (source_user_id TEXT PRIMARY KEY, handle TEXT, "
              "real_approved INTEGER, excluded INTEGER)")
    return c


def _arm(c, uid, approved=1, excluded=0):
    c.execute("INSERT OR REPLACE INTO memecoin_wallets (source_user_id, handle, real_approved, "
              "excluded) VALUES (?,?,?,?)", (uid, uid, approved, excluded))


@pytest.fixture
def tape():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE sol_pool_vaults (pool TEXT PRIMARY KEY, mint TEXT NOT NULL, "
              "coin_vault TEXT, quote_vault TEXT, quote_mint TEXT, resolved_at TEXT)")
    return c


def _ev(c, mint, ts, side="buy", net="solana", uid="W1"):
    c.execute("INSERT INTO memecoin_events (mint, side, received_at, network_id, source_user_id) "
              "VALUES (?,?,?,?,?)", (mint, side, ts, net, uid))


def _resolved(c, mint):
    c.execute("INSERT INTO sol_pool_vaults (pool, mint) VALUES (?,?)", ("pool-" + mint, mint))


# --- the time bound -------------------------------------------------------------------

def test_cutoff_is_iso_with_a_T_separator():
    """A SQLite-rendered bound uses a space, and ' ' < 'T' makes a string compare reach further
    back than asked. Building it in Python is the fix, so assert the shape."""
    out = _iso_minutes_ago(30, now_iso="2026-08-25T12:00:00+00:00")
    assert out.startswith("2026-08-25T11:30:00")
    assert " " not in out.split("+")[0]


def test_cutoff_subtracts_the_right_amount():
    assert _iso_minutes_ago(90, now_iso="2026-08-25T12:00:00+00:00").startswith("2026-08-25T10:30")


# --- candidate selection --------------------------------------------------------------

def test_picks_up_an_unresolved_recent_mint(ledger, tape):
    _ev(ledger, "MINT_A", "2026-08-25T11:59:00+00:00")
    todo, dropped = unresolved_recent_mints(
        ledger, tape, since_iso="2026-08-25T11:30:00+00:00", limit=10, armed_only=False)
    assert [m for m, _ in todo] == ["MINT_A"] and dropped == 0
    # first_seen rides along because the tape draw needs it as its backward-walk floor
    assert todo[0][1] == "2026-08-25T11:59:00+00:00"


def test_skips_a_mint_whose_pool_is_already_resolved(ledger, tape):
    """The whole point of the lane is coins we do NOT have. Re-resolving is wasted RPC."""
    _ev(ledger, "MINT_A", "2026-08-25T11:59:00+00:00")
    _resolved(tape, "MINT_A")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00", limit=10,
                                      armed_only=False)
    assert todo == []


def test_ignores_events_older_than_the_window(ledger, tape):
    _ev(ledger, "OLD", "2026-08-25T09:00:00+00:00")
    _ev(ledger, "NEW", "2026-08-25T11:59:00+00:00")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00", limit=10,
                                      armed_only=False)
    assert [m for m, _ in todo] == ["NEW"]


def test_ignores_sells(ledger, tape):
    """A sell is not a first sighting of interest — the lane exists to pre-warm entries."""
    _ev(ledger, "SELLONLY", "2026-08-25T11:59:00+00:00", side="sell")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00", limit=10,
                                      armed_only=False)
    assert todo == []


def test_ignores_non_solana_chains(ledger, tape):
    _ev(ledger, "BASECOIN", "2026-08-25T11:59:00+00:00", net="8453")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00", limit=10,
                                      armed_only=False)
    assert todo == []


def test_null_network_is_treated_as_solana(ledger, tape):
    """Older rows predate the column being populated; excluding them would blind the lane."""
    _ev(ledger, "LEGACY", "2026-08-25T11:59:00+00:00", net=None)
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00", limit=10,
                                      armed_only=False)
    assert [m for m, _ in todo] == ["LEGACY"]


def test_one_mint_seen_many_times_is_one_candidate(ledger, tape):
    for i in range(5):
        _ev(ledger, "REPEAT", "2026-08-25T11:5%d:00+00:00" % i)
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00", limit=10,
                                      armed_only=False)
    assert [m for m, _ in todo] == ["REPEAT"]


def test_the_cap_reports_what_it_dropped(ledger, tape):
    """No silent caps. A dropped count that is not returned reads as full coverage."""
    for i in range(7):
        _ev(ledger, "MINT_%d" % i, "2026-08-25T11:5%d:00+00:00" % i)
    todo, dropped = unresolved_recent_mints(
        ledger, tape, since_iso="2026-08-25T11:30:00+00:00", limit=3, armed_only=False)
    assert len(todo) == 3 and dropped == 4


def test_newest_first_so_the_cap_keeps_the_freshest(ledger, tape):
    """Under a cap the freshest sighting has the most lead time left to be worth resolving."""
    _ev(ledger, "OLDER", "2026-08-25T11:31:00+00:00")
    _ev(ledger, "NEWER", "2026-08-25T11:59:00+00:00")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00", limit=1,
                                      armed_only=False)
    assert [m for m, _ in todo] == ["NEWER"]


# --- the pass -------------------------------------------------------------------------

def test_pass_counts_resolved_and_unresolved(ledger, tape, monkeypatch):
    _ev(ledger, "GOOD", "2026-08-25T11:59:00+00:00")
    _ev(ledger, "BAD", "2026-08-25T11:58:00+00:00")
    calls = []

    def fake_resolve(tc, mconn, pos, get_tx, **kw):
        calls.append(pos)
        return ("vault", 3) if pos["mint"] == "GOOD" else (None, 2)

    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool", fake_resolve)
    s = prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                     max_fetch=4, armed_only=False,
                     now_iso="2026-08-25T12:00:00+00:00")
    assert s["resolved"] == 1 and s["unresolved"] == 1 and s["rpc_calls"] == 5
    # the synthetic position is the documented no-fill shape
    assert all(p["id"] is None for p in calls)


def test_one_bad_mint_does_not_abort_the_pass(ledger, tape, monkeypatch):
    """A background lane that dies on one coin stops pre-warming every later coin in the run."""
    _ev(ledger, "BOOM", "2026-08-25T11:59:00+00:00")
    _ev(ledger, "FINE", "2026-08-25T11:58:00+00:00")

    def fake_resolve(tc, mconn, pos, get_tx, **kw):
        if pos["mint"] == "BOOM":
            raise RuntimeError("rpc exploded")
        return ("vault", 1)

    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool", fake_resolve)
    s = prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                     max_fetch=4, armed_only=False,
                     now_iso="2026-08-25T12:00:00+00:00")
    assert s["errors"] == 1 and s["resolved"] == 1


def test_pass_is_a_noop_with_no_candidates(ledger, tape, monkeypatch):
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool",
                        lambda *a, **k: pytest.fail("must not resolve with no candidates"))
    s = prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                     max_fetch=4, armed_only=False,
                     now_iso="2026-08-25T12:00:00+00:00")
    assert s == {"candidates": 0, "dropped": 0, "resolved": 0, "unresolved": 0,
                 "errors": 0, "rpc_calls": 0, "taped": 0, "tape_calls": 0}


# --- the tape draw --------------------------------------------------------------------
# Resolving a pool alone leaves it with ZERO ticks, because solana_tape_pass only taps vaults
# reached from open positions. A gate reading that pool finds an empty window and fails open,
# which is indistinguishable from having no lane. These cover the half that makes it useful.

def test_a_resolved_pool_gets_its_tape_drawn(ledger, tape, monkeypatch):
    _ev(ledger, "GOOD", "2026-08-25T11:59:00+00:00")
    drawn = []
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool",
                        lambda *a, **k: ("VAULT_1", 3))
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.tape_pass_for_vault",
                        lambda tc, mint, vault, gs, gt, now, **k: (drawn.append((mint, vault)), 5)[1])
    s = prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                     max_fetch=4, armed_only=False,
                     tape_budget=6, now_iso="2026-08-25T12:00:00+00:00")
    assert drawn == [("GOOD", "VAULT_1")]
    assert s["taped"] == 1 and s["tape_calls"] == 5


def test_an_unresolved_mint_is_never_taped(ledger, tape, monkeypatch):
    """No vault means nothing to tape. Calling with a None vault would be a crash or worse."""
    _ev(ledger, "BAD", "2026-08-25T11:59:00+00:00")
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool",
                        lambda *a, **k: (None, 4))
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.tape_pass_for_vault",
                        lambda *a, **k: pytest.fail("must not tape an unresolved mint"))
    s = prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                     max_fetch=4, armed_only=False,
                     tape_budget=6, now_iso="2026-08-25T12:00:00+00:00")
    assert s["unresolved"] == 1 and s["taped"] == 0


def test_tape_budget_zero_skips_the_draw(ledger, tape, monkeypatch):
    """The knob must be able to turn the draw off without disabling discovery."""
    _ev(ledger, "GOOD", "2026-08-25T11:59:00+00:00")
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool",
                        lambda *a, **k: ("VAULT_1", 3))
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.tape_pass_for_vault",
                        lambda *a, **k: pytest.fail("must not tape when the budget is 0"))
    s = prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                     max_fetch=4, armed_only=False,
                     tape_budget=0, now_iso="2026-08-25T12:00:00+00:00")
    assert s["resolved"] == 1 and s["taped"] == 0


def test_a_failing_tape_draw_does_not_lose_the_resolution(ledger, tape, monkeypatch):
    """The pool identity is durable and useful on its own; a tape failure must not discard it
    or abort the remaining mints."""
    _ev(ledger, "GOOD", "2026-08-25T11:59:00+00:00")
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool",
                        lambda *a, **k: ("VAULT_1", 3))

    def boom(*a, **k):
        raise RuntimeError("tape draw failed")

    monkeypatch.setattr("scripts.memecoin_pool_prewarm.tape_pass_for_vault", boom)
    s = prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                     max_fetch=4, armed_only=False,
                     tape_budget=6, now_iso="2026-08-25T12:00:00+00:00")
    assert s["resolved"] == 1 and s["errors"] == 1 and s["taped"] == 0


def test_an_aborted_draw_is_not_counted_as_taped(ledger, tape, monkeypatch):
    """tape_pass_for_vault returns -1 on abort. Counting that as taped would overstate coverage."""
    _ev(ledger, "GOOD", "2026-08-25T11:59:00+00:00")
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool",
                        lambda *a, **k: ("VAULT_1", 3))
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.tape_pass_for_vault", lambda *a, **k: -1)
    s = prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                     max_fetch=4, armed_only=False,
                     tape_budget=6, now_iso="2026-08-25T12:00:00+00:00")
    assert s["resolved"] == 1 and s["taped"] == 0 and s["tape_calls"] == 0


def test_watches_wider_than_we_trade(ledger, tape, monkeypatch):
    """A sub-floor coin MUST still be resolved. The mcap floor is a judgement the operator may
    revisit, and a lane that only watches what today's gate admits cannot produce the evidence
    to revisit it. This lane therefore has no mcap filter at all — assert it stays that way."""
    _ev(ledger, "TINY", "2026-08-25T11:59:00+00:00")
    seen = []
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool",
                        lambda tc, mc, pos, gt, **k: (seen.append(pos["mint"]), ("v", 1))[1])
    prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                 max_fetch=4, armed_only=False,
                 now_iso="2026-08-25T12:00:00+00:00")
    assert seen == ["TINY"]


# --- the backward-walk floor ----------------------------------------------------------
# `floor_ts=None` does NOT mean "take the head". It means NO floor, so a fresh vault (cursor
# last_sig='') tries to drain its entire history and records nothing. Measured live: n=0 and
# 0 ticks across four consecutive passes while the frontier advanced each time.

def test_floor_is_an_epoch_bounded_by_the_rolling_window():
    from scripts.memecoin_pool_prewarm import _floor_for
    f = _floor_for("2026-08-25T11:59:00+00:00", now_iso="2026-08-25T12:00:00+00:00")
    now = 1787659200.0  # 2026-08-25T12:00:00Z
    assert isinstance(f, float)
    assert f < now, "the floor must sit BEFORE now or the walk has nothing to cover"
    assert f >= now - 86400, "an unbounded floor is the bug this guards"


def test_a_stale_sighting_cannot_ask_for_an_unbounded_drain():
    from scripts.memecoin_pool_prewarm import _floor_for
    old = _floor_for("2020-01-01T00:00:00+00:00", now_iso="2026-08-25T12:00:00+00:00")
    fresh = _floor_for("2026-08-25T11:59:00+00:00", now_iso="2026-08-25T12:00:00+00:00")
    assert old == fresh, "the rolling floor must cap an ancient first sighting"


def test_an_unparseable_sighting_still_gets_a_floor():
    from scripts.memecoin_pool_prewarm import _floor_for
    assert _floor_for("not-a-timestamp", now_iso="2026-08-25T12:00:00+00:00") is not None


def test_the_draw_is_given_a_real_floor_not_none(ledger, tape, monkeypatch):
    """The regression. Passing None here is what produced 0 ticks on a live pool."""
    _ev(ledger, "GOOD", "2026-08-25T11:59:00+00:00")
    got = {}
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.resolve_position_pool",
                        lambda *a, **k: ("VAULT_1", 3))
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.tape_pass_for_vault",
                        lambda tc, m, v, gs, gt, now, **k: (got.update(k), 4)[1])
    prewarm_once(ledger, tape, None, None, None, lookback_min=30, max_mints=10,
                 max_fetch=4, armed_only=False,
                 tape_budget=6, now_iso="2026-08-25T12:00:00+00:00")
    assert got.get("floor_ts") is not None
    assert isinstance(got["floor_ts"], float)


# --- keeping the tape warm ------------------------------------------------------------
# Discovery alone is not enough. A pre-warmed pool is taped ONCE and then nothing re-taps it,
# so it drops out of the 300s flow window within minutes and reads as UNKNOWN — the gate then
# fails open exactly as if the lane had never run. Measured live: a pool with 10 ticks and a
# 12-minute-old head returned None from observe_pre_entry_flow.

@pytest.fixture
def tape_with_binds():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE sol_pool_vaults (pool TEXT PRIMARY KEY, mint TEXT NOT NULL, "
              "coin_vault TEXT, quote_vault TEXT, quote_mint TEXT, resolved_at TEXT)")
    c.execute("CREATE TABLE sol_position_pools (position_id INTEGER PRIMARY KEY, mint TEXT, "
              "pool TEXT, venue_kind TEXT, resolution_tx TEXT, resolved_at TEXT)")
    return c


def _vault(c, pool, mint, resolved_at):
    c.execute("INSERT INTO sol_pool_vaults (pool, mint, resolved_at) VALUES (?,?,?)",
              (pool, mint, resolved_at))


def test_a_recently_prewarmed_pool_is_a_refresh_candidate(tape_with_binds):
    from scripts.memecoin_pool_prewarm import stale_prewarmed_pools
    _vault(tape_with_binds, "POOL_A", "MINT_A", "2026-08-25T11:55:00+00:00")
    pools, dropped = stale_prewarmed_pools(
        tape_with_binds, since_iso="2026-08-25T11:15:00+00:00", limit=10)
    assert pools == [("POOL_A", "MINT_A")] and dropped == 0


def test_a_pool_owned_by_a_position_is_left_alone(tape_with_binds):
    """The tape worker already re-taps position-bound vaults. Doing it here would double-spend."""
    from scripts.memecoin_pool_prewarm import stale_prewarmed_pools
    _vault(tape_with_binds, "POOL_A", "MINT_A", "2026-08-25T11:55:00+00:00")
    tape_with_binds.execute(
        "INSERT INTO sol_position_pools (position_id, mint, pool, venue_kind, resolved_at) "
        "VALUES (1,'MINT_A','POOL_A','amm','2026-08-25T11:56:00+00:00')")
    pools, _ = stale_prewarmed_pools(
        tape_with_binds, since_iso="2026-08-25T11:15:00+00:00", limit=10)
    assert pools == []


def test_an_old_pool_stops_being_kept_warm(tape_with_binds):
    """Warmth is bounded — a coin nobody has mentioned for an hour is no longer a candidate."""
    from scripts.memecoin_pool_prewarm import stale_prewarmed_pools
    _vault(tape_with_binds, "POOL_OLD", "MINT_OLD", "2026-08-25T09:00:00+00:00")
    pools, _ = stale_prewarmed_pools(
        tape_with_binds, since_iso="2026-08-25T11:15:00+00:00", limit=10)
    assert pools == []


def test_refresh_cap_reports_its_drops(tape_with_binds):
    from scripts.memecoin_pool_prewarm import stale_prewarmed_pools
    for i in range(6):
        _vault(tape_with_binds, "POOL_%d" % i, "MINT_%d" % i, "2026-08-25T11:5%d:00+00:00" % i)
    pools, dropped = stale_prewarmed_pools(
        tape_with_binds, since_iso="2026-08-25T11:15:00+00:00", limit=2)
    assert len(pools) == 2 and dropped == 4


def test_refresh_retaps_and_counts(tape_with_binds, monkeypatch):
    from scripts.memecoin_pool_prewarm import refresh_once
    _vault(tape_with_binds, "POOL_A", "MINT_A", "2026-08-25T11:55:00+00:00")
    seen = []
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.tape_pass_for_vault",
                        lambda tc, m, v, gs, gt, now, **k: (seen.append((m, v)), 3)[1])
    r = refresh_once(tape_with_binds, None, None, keep_warm_min=45, max_pools=10, tape_budget=6,
                     now_iso="2026-08-25T12:00:00+00:00")
    assert seen == [("MINT_A", "POOL_A")]
    assert r["refreshed"] == 1 and r["refresh_calls"] == 3


def test_refresh_is_off_when_its_budget_is_zero(tape_with_binds, monkeypatch):
    from scripts.memecoin_pool_prewarm import refresh_once
    _vault(tape_with_binds, "POOL_A", "MINT_A", "2026-08-25T11:55:00+00:00")
    monkeypatch.setattr("scripts.memecoin_pool_prewarm.tape_pass_for_vault",
                        lambda *a, **k: pytest.fail("must not re-tap with a zero budget"))
    r = refresh_once(tape_with_binds, None, None, keep_warm_min=45, max_pools=10, tape_budget=0,
                     now_iso="2026-08-25T12:00:00+00:00")
    assert r["refreshed"] == 0


def test_one_failing_refresh_does_not_abort_the_rest(tape_with_binds, monkeypatch):
    from scripts.memecoin_pool_prewarm import refresh_once
    _vault(tape_with_binds, "POOL_BAD", "MINT_BAD", "2026-08-25T11:56:00+00:00")
    _vault(tape_with_binds, "POOL_OK", "MINT_OK", "2026-08-25T11:55:00+00:00")

    def maybe(tc, m, v, gs, gt, now, **k):
        if m == "MINT_BAD":
            raise RuntimeError("nope")
        return 2

    monkeypatch.setattr("scripts.memecoin_pool_prewarm.tape_pass_for_vault", maybe)
    r = refresh_once(tape_with_binds, None, None, keep_warm_min=45, max_pools=10, tape_budget=6,
                     now_iso="2026-08-25T12:00:00+00:00")
    assert r["refresh_errors"] == 1 and r["refreshed"] == 1


# --- armed-wallet scoping -------------------------------------------------------------
# Only an armed wallet can fill, so an unarmed wallet's coin yields no paired observation.
# Measured 2026-08-25: scoping drops 68-91% of candidates AND improves the experiment —
# armed attempts run 742 refused to 51 filled over 311 mints, a 14.5:1 matched control whose
# refused rows carry no fill and therefore no footprint of ours.

def test_armed_only_keeps_an_armed_wallets_coin(ledger, tape):
    _arm(ledger, "W_ARMED")
    _ev(ledger, "MINT_A", "2026-08-25T11:59:00+00:00", uid="W_ARMED")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00",
                                      limit=10, armed_only=True)
    assert [m for m, _ in todo] == ["MINT_A"]


def test_armed_only_drops_an_unarmed_wallets_coin(ledger, tape):
    _arm(ledger, "W_ARMED")
    _ev(ledger, "MINT_B", "2026-08-25T11:59:00+00:00", uid="W_UNARMED")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00",
                                      limit=10, armed_only=True)
    assert todo == []


def test_a_disarmed_wallet_is_not_armed(ledger, tape):
    _arm(ledger, "W_OFF", approved=0)
    _ev(ledger, "MINT_C", "2026-08-25T11:59:00+00:00", uid="W_OFF")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00",
                                      limit=10, armed_only=True)
    assert todo == []


def test_an_excluded_wallet_is_not_armed(ledger, tape):
    _arm(ledger, "W_EX", approved=1, excluded=1)
    _ev(ledger, "MINT_D", "2026-08-25T11:59:00+00:00", uid="W_EX")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00",
                                      limit=10, armed_only=True)
    assert todo == []


def test_an_empty_roster_prewarms_nothing(ledger, tape):
    """Nothing armed means nothing can fill, so there is nothing worth pre-warming. This must NOT
    be confused with a failed lookup — conflating them inverts the lane into resolving everything
    at the exact moment the roster is empty."""
    _arm(ledger, "W_OFF", approved=0)
    _ev(ledger, "MINT_X", "2026-08-25T11:59:00+00:00", uid="W_OFF")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00",
                                      limit=10, armed_only=True)
    assert todo == []


def test_an_unreadable_roster_falls_back_to_UNSCOPED_not_blind(ledger, tape):
    """A roster lookup FAILURE must not silently blind the lane. Failing open costs RPC; failing
    closed would stop all evidence collection without saying so."""
    ledger.execute("DROP TABLE memecoin_wallets")
    _ev(ledger, "MINT_E", "2026-08-25T11:59:00+00:00", uid="W_ANY")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00",
                                      limit=10, armed_only=True)
    assert [m for m, _ in todo] == ["MINT_E"]


def test_scoping_is_off_when_the_knob_says_so(ledger, tape):
    _arm(ledger, "W_ARMED")
    _ev(ledger, "MINT_F", "2026-08-25T11:59:00+00:00", uid="W_UNARMED")
    todo, _ = unresolved_recent_mints(ledger, tape, since_iso="2026-08-25T11:30:00+00:00",
                                      limit=10, armed_only=False)
    assert [m for m, _ in todo] == ["MINT_F"]


def test_refresh_honours_the_same_scope_as_discovery(tape_with_binds):
    """Scoping discovery alone is not enough: measured live, discovery fell to 0 calls under
    armed-only while refresh still spent 28 warming pools no armed wallet had touched. The
    refresh must carry the same scope or it silently becomes the entire bill."""
    from scripts.memecoin_pool_prewarm import stale_prewarmed_pools
    _vault(tape_with_binds, "POOL_KEEP", "MINT_KEEP", "2026-08-25T11:55:00+00:00")
    _vault(tape_with_binds, "POOL_DROP", "MINT_DROP", "2026-08-25T11:56:00+00:00")
    pools, _ = stale_prewarmed_pools(tape_with_binds, since_iso="2026-08-25T11:15:00+00:00",
                                     limit=10, keep_mints={"MINT_KEEP"})
    assert pools == [("POOL_KEEP", "MINT_KEEP")]


def test_refresh_with_an_empty_keep_set_warms_nothing(tape_with_binds):
    """Roster empty -> nothing can fill -> nothing is worth keeping warm."""
    from scripts.memecoin_pool_prewarm import stale_prewarmed_pools
    _vault(tape_with_binds, "POOL_A", "MINT_A", "2026-08-25T11:55:00+00:00")
    pools, _ = stale_prewarmed_pools(tape_with_binds, since_iso="2026-08-25T11:15:00+00:00",
                                     limit=10, keep_mints=set())
    assert pools == []


def test_refresh_with_no_keep_set_is_unscoped(tape_with_binds):
    from scripts.memecoin_pool_prewarm import stale_prewarmed_pools
    _vault(tape_with_binds, "POOL_A", "MINT_A", "2026-08-25T11:55:00+00:00")
    pools, _ = stale_prewarmed_pools(tape_with_binds, since_iso="2026-08-25T11:15:00+00:00",
                                     limit=10, keep_mints=None)
    assert pools == [("POOL_A", "MINT_A")]
