"""Phase 2 Unit A — bounded open-position quote fast lane.

Contract:
  * every unpaused OPEN REAL position is refreshed unless the caller supplies a limit;
  * prices come from the position's OWN funded `preview_exit_fn` — never GeckoTerminal
    (Global Constraint 8) and never a re-implemented quote. That seam reads the EXACT raw
    on-chain balance and walks the full V3 -> V2 -> V4 ladder; an earlier draft rebuilt an
    atomic amount from the ledger's human float, which evm_real_seams explicitly warns
    mis-sizes bags above 2^53, and quoted V3 only;
  * unit price = expected_usdc / open_qty, byte-for-byte the runner's own
    `_executable_unit_price`, so the tape cannot drift from what the rails judge. This is
    the number 11984 was judged on (+48.296% executable vs a +50.489% tape mid);
  * no context, no preview, or zero quantity => NO price. Missing is missing; a fabricated
    mark is worse than none (the RUSSELL +$785.66 lesson).
"""
import sqlite3
import threading

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_fastlane as F


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _pos(c, *, network_id="4663", mint="0xtok", mode="real", qty=1000.0, price=0.05,
         user="u"):
    pid = m.open_position(c, source_user_id=user, source_handle="h", mint=mint, ticker="T",
                          entry_price=price, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-22T10:00:00+00:00",
                          strategy="alpha_b", execution_mode=mode, network_id=network_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=price, qty=qty,
                  notional=price * qty, fill_source="chain", fraction=1.0,
                  created_at="2026-07-22T10:00:00+00:00")
    return pid



class _preview:
    """Stand-in for execution.exit_preview.ExitPreview (only expected_usdc is read here)."""
    def __init__(self, expected_usdc):
        self.expected_usdc = expected_usdc

# ── selection ────────────────────────────────────────────────────────────────

def test_only_open_real_positions_are_refreshed():
    c = _c()
    open_real = _pos(c, mint="0xopen")
    _pos(c, mint="0xpaper", mode="paper")
    closed = _pos(c, mint="0xclosed")
    m.claim_close(c, closed)
    m.finalize_close(c, closed, "2026-07-22T10:05:00+00:00", -1.0, exit_reason="b_stop")
    got = [p["id"] for p in F.open_real_positions(c)]
    assert got == [open_real]


def test_selection_honors_an_explicit_limit():
    c = _c()
    for i in range(12):
        _pos(c, mint=f"0x{i}")
    assert len(F.open_real_positions(c, limit=7)) == 7


@pytest.mark.parametrize("limit,want", [(None, ["CopyA", "CopyB", "DeskA", "DeskB"]),
                                       (0, []), (2, ["CopyA", "CopyB"]),
                                       (-1, ["CopyA", "CopyB", "DeskA", "DeskB"])])
def test_open_work_set_covers_the_combined_book_without_changing_explicit_limits(monkeypatch, limit, want):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_TOTAL", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 2)
    c = _c()
    for mint in ("CopyA", "CopyB", "DeskA", "DeskB"):
        pid = _pos(c, network_id="solana", mint=mint)
        if mint.startswith("Desk"):
            c.execute("UPDATE memecoin_positions SET signal_source='desk', source_user_id=NULL WHERE id=?", (pid,))
    paused = _pos(c, network_id="solana", mint="PausedDesk")
    c.execute("UPDATE memecoin_positions SET signal_source='desk', tracking_paused_at='paused' WHERE id=?", (paused,))
    _pos(c, network_id="solana", mint="PaperDesk", mode="paper")
    closed = _pos(c, network_id="solana", mint="ClosedDesk")
    m.claim_close(c, closed)
    m.finalize_close(c, closed, "2026-07-22T10:05:00+00:00", -1.0, exit_reason="b_stop")

    assert [p["mint"] for p in F.open_real_positions(c, limit=limit)] == want
    c.close()


def test_newer_desk_positions_reach_marks_vault_work_set_and_rail_wake(monkeypatch):
    from types import SimpleNamespace
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_TOTAL", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_POST_CLOSE_MARK_S", 0.0)
    c = _c()
    _pos(c, network_id="solana", mint="Copy")
    desk = _pos(c, network_id="solana", mint="Desk")
    c.execute("UPDATE memecoin_positions SET signal_source='desk', source_user_id=NULL WHERE id=?", (desk,))

    def batch(keys):
        return {k: {"price": 1.0} for k in keys}

    marks = F.refresh_open_positions(c, {}, context_fn=lambda *a: {}, mid_batch_fn=batch)
    assert set(marks) == {("solana", "Copy"), ("solana", "Desk")}
    assert [p["mint"] for p in F.open_swap_mark_positions(c, paper_mint_limit=0)] == ["Copy", "Desk"]
    c.execute("UPDATE memecoin_positions SET last_executable_price=entry_price, last_executable_at=?",
              ("2026-07-22T10:01:00+00:00",))
    armed = {}
    F.rearm_rail_wake(c, SimpleNamespace(rearm=lambda rails, *, keep: armed.update(rails=rails, keep=keep)))
    assert armed["keep"] == {"Copy", "Desk"}
    assert set(armed["rails"]) == {"Copy", "Desk"}
    assert armed["rails"]["Desk"][2] == {desk}
    c.close()


@pytest.mark.parametrize("policy_id", ["follow-30-50-v3", "follow-30-50-v4"])
def test_a_pending_4663_tp_breach_gets_the_priority_pass(policy_id):
    import json
    import test_memecoin_real_rails_v2 as h

    c = _c()
    frozen = h._frozen_pos(derisk=0.0, trail=0.0, trail_arm=0.0, stop=90.0,
                           catastrophe=95.0, debounce=1, max_hold=999999.0, tp=60.0)
    contract = json.loads(frozen["funded_contract_json"])
    contract["identity"]["network_id"] = "4663"
    contract["policy"]["policy_id"] = policy_id
    contract["policy"]["mirror_on_sell"] = False
    payload, digest = m.canonical_funded_json(contract)
    frozen.update(network_id="4663", funded_contract_json=payload, funded_contract_hash=digest)
    pid = h._persist_frozen_position(c, frozen)

    assert F.has_priority_tp_confirmation(c) is False
    c.execute("UPDATE memecoin_positions SET tp_breach_started_at=? WHERE id=?",
              ("2026-08-31T00:00:00+00:00", pid))
    c.commit()
    assert F.has_priority_tp_confirmation(c) is True


def test_a_position_closed_inside_the_post_close_window_stays_on_the_tape(monkeypatch):
    """2026-09-23, operator: keep reading every entered token for at least 1 min 30 s after the
    close. The swap-mark work set keeps a funded position closed within the window and drops it
    after; the knob at 0 keeps today's behaviour."""
    from sniper.settings import memecoin as cfg
    c = _c()
    fresh = _pos(c, network_id="solana", mint="MintFresh")
    stale = _pos(c, network_id="solana", mint="MintStale")
    for pid, at in ((fresh, "2026-09-23T19:00:00+00:00"), (stale, "2026-09-23T18:50:00+00:00")):
        m.claim_close(c, pid)
        m.finalize_close(c, pid, at, -1.0, exit_reason="b_stop")
    now = "2026-09-23T19:01:00+00:00"
    monkeypatch.setattr(cfg, "MEMECOIN_POST_CLOSE_MARK_S", 120.0, raising=False)
    monkeypatch.setattr("sniper.util.clock.real_utc_now_iso", lambda: now)
    got = [p["mint"] for p in F.open_swap_mark_positions(c, real_limit=7, paper_mint_limit=16)]
    assert got == ["MintFresh"]
    assert [p["mint"] for p in F.open_real_positions(c)] == [], "a closed row is never open work"
    monkeypatch.setattr(cfg, "MEMECOIN_POST_CLOSE_MARK_S", 0.0, raising=False)
    assert F.open_swap_mark_positions(c, real_limit=7, paper_mint_limit=16) == []


def test_a_closed_evm_position_is_listed_for_marks_but_never_previewed(monkeypatch):
    """N13 (Codex review 2026-09-23): the post-close list carries a closed 4663 row, but the
    funded exit preview -- the only EVM price source in this lane -- must never quote a bag that
    was sold. Absent, not a balance-based price of nothing."""
    from types import SimpleNamespace
    from sniper.settings import memecoin as cfg
    c = _c()
    closed = _pos(c, network_id="4663", mint="0xclosed")
    _pos(c, network_id="4663", mint="0xopen")
    m.claim_close(c, closed)
    m.finalize_close(c, closed, "2026-09-23T12:00:00+00:00", -1.0, exit_reason="b_stop")
    monkeypatch.setattr(cfg, "MEMECOIN_POST_CLOSE_MARK_S", 120.0, raising=False)
    monkeypatch.setattr("sniper.util.clock.real_utc_now_iso",
                        lambda: "2026-09-23T12:00:10+00:00")
    listed = [p["mint"] for p in F.open_swap_mark_positions(c, paper_mint_limit=0)]
    assert listed == ["0xopen", "0xclosed"]
    asked = []

    def preview(pos):
        asked.append(pos["mint"])
        return SimpleNamespace(expected_usdc=50.0)

    marks = F.refresh_open_positions(
        c, {}, context_fn=lambda *a: {"preview_exit_fn": preview}, mid_batch_fn=lambda *a: {})
    assert asked == ["0xopen"]
    assert ("4663", "0xclosed") not in marks


def test_the_post_close_window_is_floored_at_ninety_seconds():
    from sniper.settings.memecoin import _post_close_mark_s
    assert _post_close_mark_s("0") == 0.0
    assert _post_close_mark_s("30") == 90.0
    assert _post_close_mark_s("120") == 120.0
    assert _post_close_mark_s("junk") == 120.0


def test_swap_mark_selection_keeps_a_paper_cohort_after_the_real_close():
    c = _c()
    real = _pos(c, network_id="solana", mint="MintA")
    paper = _pos(c, network_id="solana", mint="MintA", mode="paper")
    c.execute(
        "UPDATE memecoin_positions SET origin_event_id='event-a' WHERE id IN (?,?)",
        (real, paper),
    )
    m.claim_close(c, real)
    m.finalize_close(
        c, real, "2026-07-22T10:00:30+00:00", 1.0, exit_reason="real_tp"
    )

    selected = F.open_swap_mark_positions(c, real_limit=7, paper_mint_limit=16)

    assert [pos["id"] for pos in selected] == [real]


def test_swap_mark_paper_selection_is_newest_first_and_mint_bounded():
    c = _c()
    selected_real_ids = []
    for index in range(3):
        mint = f"Mint{index}"
        real = _pos(c, network_id="solana", mint=mint)
        paper = _pos(c, network_id="solana", mint=mint, mode="paper")
        event = f"event-{index}"
        opened = f"2026-07-22T10:0{index}:00+00:00"
        c.execute(
            "UPDATE memecoin_positions SET origin_event_id=?, opened_at=? WHERE id IN (?,?)",
            (event, opened, real, paper),
        )
        m.claim_close(c, real)
        m.finalize_close(c, real, opened, 1.0, exit_reason="real_tp")
        selected_real_ids.append(real)

    selected = F.open_swap_mark_positions(c, real_limit=7, paper_mint_limit=2)

    assert [pos["id"] for pos in selected] == selected_real_ids[2:0:-1]


def test_swap_mark_limit_counts_unique_mints_not_repeated_origins():
    c = _c()
    expected = []
    rows = (
        ("MintB", "event-b", "2026-07-22T10:00:00+00:00"),
        ("MintA", "event-a1", "2026-07-22T10:01:00+00:00"),
        ("MintA", "event-a2", "2026-07-22T10:02:00+00:00"),
    )
    for index, (mint, event, opened) in enumerate(rows):
        user = f"u{index}"
        real = _pos(c, network_id="solana", mint=mint, user=user)
        paper = _pos(c, network_id="solana", mint=mint, mode="paper", user=user)
        c.execute(
            "UPDATE memecoin_positions SET origin_event_id=?, opened_at=? WHERE id IN (?,?)",
            (event, opened, real, paper),
        )
        m.claim_close(c, real)
        m.finalize_close(c, real, opened, 1.0, exit_reason="real_tp")
        expected.append(real)

    selected = F.open_swap_mark_positions(c, real_limit=7, paper_mint_limit=2)

    assert [pos["id"] for pos in selected] == [expected[2], expected[0]]


def test_recent_swap_marks_exclude_other_fastlane_prices_and_expire():
    marks = F.RecentSwapMarks()
    marks.publish(
        {
            ("solana", "Swap"): {
                "price": 2.0,
                "source": "fastlane_sol_swap",
            },
            ("solana", "State"): {
                "price": 3.0,
                "source": "fastlane_sol_state",
            },
            ("solana", "Mid"): {"price": 4.0, "source": "fastlane_mid"},
        },
        now_ts=100.0,
    )

    assert set(marks.snapshot(now_ts=119.0, max_age_s=20.0)) == {
        ("solana", "Swap"),
        ("solana", "State"),
    }
    assert marks.snapshot(now_ts=121.0, max_age_s=20.0) == {}


# ── arming contract ──────────────────────────────────────────────────────────

def test_both_fastlane_flags_default_off():
    """Nothing here reaches real money by default. ENABLED alone is shadow (marks only);
    DECIDES is reserved for the not-yet-implemented flip that would let rails consume these
    quotes — nothing reads it today."""
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_FASTLANE_ENABLED is False
    assert cfg.MEMECOIN_FASTLANE_DECIDES is False


def test_solana_swap_mark_lane_defaults_on_but_needs_the_fastlane():
    from sniper.settings import memecoin as cfg

    assert cfg.MEMECOIN_SOLANA_SWAP_MARK_ENABLED is True
    assert cfg.MEMECOIN_FASTLANE_ENABLED is False


# ── executable unit price ────────────────────────────────────────────────────

def test_unit_price_is_expected_usdc_over_open_qty():
    """$50 of proceeds on a 1000-token bag = $0.05/token — the runner's own convention."""
    c = _c(); pid = _pos(c, qty=1000.0)
    px = F.executable_unit_price(c, dict(m.get_position(c, pid)),
                                 preview_fn=lambda p: _preview(50.0))
    assert px == pytest.approx(0.05)


def test_preview_dict_shape_is_accepted():
    c = _c(); pid = _pos(c, qty=1000.0)
    px = F.executable_unit_price(c, dict(m.get_position(c, pid)),
                                 preview_fn=lambda p: {"expected_usdc": 60.0})
    assert px == pytest.approx(0.06)


def test_no_preview_yields_no_mark():
    """Unquotable this tick -> missing, never zero and never a substituted weaker price."""
    c = _c(); pid = _pos(c, qty=1000.0)
    assert F.executable_unit_price(c, dict(m.get_position(c, pid)),
                                   preview_fn=lambda p: None) is None


def test_zero_open_quantity_yields_no_mark():
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xz", ticker="T",
                          entry_price=0.05, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-22T10:00:00+00:00", strategy="alpha_b",
                          execution_mode="real", network_id="4663")
    assert F.executable_unit_price(c, dict(m.get_position(c, pid)),
                                   preview_fn=lambda p: _preview(50.0)) is None


def test_nonpositive_expected_yields_no_mark():
    c = _c(); pid = _pos(c, qty=1000.0)
    assert F.executable_unit_price(c, dict(m.get_position(c, pid)),
                                   preview_fn=lambda p: _preview(0.0)) is None


def test_preview_error_never_raises_into_the_caller():
    c = _c(); pid = _pos(c, qty=1000.0)
    def boom(p):
        raise RuntimeError("rpc down")
    assert F.executable_unit_price(c, dict(m.get_position(c, pid)), preview_fn=boom) is None


# ── refresh ──────────────────────────────────────────────────────────────────

def _ctxs(preview_fn):
    return {"any": {"preview_exit_fn": preview_fn}}


def _ctx_fn(contexts, pos):
    return contexts.get("any")


def test_refresh_returns_a_write_marks_shaped_map():
    c = _c(); _pos(c, network_id="4663", mint="0xtok", qty=1000.0)
    pm = F.refresh_open_positions(c, _ctxs(lambda p: _preview(50.0)), context_fn=_ctx_fn)
    assert list(pm) == [("4663", "0xtok")]
    st = pm[("4663", "0xtok")]
    assert st["price"] == pytest.approx(0.05)
    assert st["source"] == "fastlane_rpc"        # segments new-cadence era from old
    assert st["quality"] == "exact_route"        # earned now: exact balance, full route ladder


def test_refresh_retains_successful_preview_for_same_tick_sweep():
    c = _c(); pid = _pos(c, network_id="4663", mint="0xtok", qty=1000.0)
    preview = _preview(50.0)
    previews = {}

    F.refresh_open_positions(
        c, _ctxs(lambda _p: preview), context_fn=_ctx_fn,
        preview_by_position=previews,
    )

    assert previews == {pid: preview}


def test_refresh_runs_independent_evm_previews_in_parallel():
    """A second open EVM bag must not wait for the first route preview."""
    c = _c()
    _pos(c, network_id="4663", mint="0xone", qty=1000.0)
    _pos(c, network_id="4663", mint="0xtwo", qty=1000.0)
    ready = threading.Barrier(2, timeout=0.5)

    def preview(_pos):
        ready.wait()
        return _preview(50.0)

    result = F.refresh_open_positions(c, _ctxs(preview), context_fn=_ctx_fn)

    assert set(result) == {("4663", "0xone"), ("4663", "0xtwo")}


def test_position_without_a_funded_seam_is_skipped():
    """No context => no substitute price. One definition of executable, or nothing."""
    c = _c(); _pos(c, network_id="solana", mint="SOLMINT")
    assert F.refresh_open_positions(c, {}, context_fn=lambda ctxs, pos: None) == {}


def test_refresh_is_keyed_on_network_and_mint():
    c = _c()
    _pos(c, network_id="4663", mint="SAME", qty=1000.0)
    _pos(c, network_id="8453", mint="SAME", qty=1000.0)
    pm = F.refresh_open_positions(c, _ctxs(lambda p: _preview(50.0)), context_fn=_ctx_fn)
    assert set(pm) == {("4663", "SAME"), ("8453", "SAME")}


def test_refresh_survives_one_bad_position():
    """One unquotable coin must not deny the tape to the others."""
    c = _c()
    _pos(c, network_id="4663", mint="0xgood", qty=1000.0)
    _pos(c, network_id="4663", mint="0xbad", qty=1000.0)
    def preview(p):
        if p["mint"] == "0xbad":
            raise RuntimeError("no pool")
        return _preview(50.0)
    pm = F.refresh_open_positions(c, _ctxs(preview), context_fn=_ctx_fn)
    assert list(pm) == [("4663", "0xgood")]


def test_context_lookup_failure_is_contained():
    c = _c(); _pos(c, network_id="4663", mint="0xtok", qty=1000.0)
    def boom(ctxs, pos):
        raise RuntimeError("context blew up")
    assert F.refresh_open_positions(c, {}, context_fn=boom) == {}


# ── scope: EVM only, proven against the PRODUCTION context resolver ──────────
#
# 2026-07-22 review blocker 2. The spec assumed Solana had no exit-preview seam. It does:
# build_real_context exposes preview_exit_fn (memecoin_real_runner.py:423) and the solana
# context is included in build_all_contexts (line 452). So "skip when there is no context"
# silently priced Solana too — a Jupiter balance+quote round trip per open position every
# 5s, for a chain whose tape gap is a 30s RECORDING floor and has no freshness to buy.
#
# The earlier test injected context_fn=lambda ...: None, which cannot catch this. These use
# the real resolver.

def test_solana_is_out_of_scope_under_the_production_resolver():
    from sniper.services.memecoin_real_runner import context_for_position
    c = _c(); _pos(c, network_id="solana", mint="SOLMINT", qty=1000.0)
    called = []
    contexts = {"solana": {"preview_exit_fn": lambda p: (called.append(p), _preview(50.0))[1]}}
    pm = F.refresh_open_positions(c, contexts, context_fn=context_for_position)
    assert pm == {}
    assert called == [], "Solana must not be quoted by the fast lane"


def test_evm_chain_is_in_scope_under_the_production_resolver():
    from sniper.services.memecoin_real_runner import context_for_position
    c = _c(); _pos(c, network_id="4663", mint="0xtok", qty=1000.0)
    contexts = {"4663": {"preview_exit_fn": lambda p: _preview(50.0)}}
    pm = F.refresh_open_positions(c, contexts, context_fn=context_for_position)
    assert list(pm) == [("4663", "0xtok")]


def test_evm_fastlane_leaves_a_budget_skip_for_the_exit_sweep():
    c = _c(); pid = _pos(c, network_id="4663", mint="0xtok", qty=1000.0)
    c.execute("UPDATE memecoin_positions SET last_executable_price=? WHERE id=?", (0.04, pid))
    c.commit()
    called = []
    demand = []

    def preview(_pos):
        called.append(_pos["id"])
        return _preview(50.0)

    def demand_fn(pos, **kwargs):
        demand.append((pos["id"], kwargs))
        return False

    previews = {}
    pm = F.refresh_open_positions(
        c, {"4663": {"preview_exit_fn": preview}},
        context_fn=lambda contexts, _pos: contexts["4663"],
        demand_fn=demand_fn, preview_by_position=previews,
    )

    assert pm == {}
    assert called == []
    assert demand == [(pid, {
        "display_mid": 0.04, "trail_pct": 0.0, "derisk_pct": 0.0, "trail_armed": False,
    })]
    assert pid not in previews


def test_scope_is_explicit_not_a_side_effect_of_missing_context():
    """Even handed a context WITH a preview seam, a non-EVM chain is refused outright."""
    c = _c(); _pos(c, network_id="solana", mint="SOLMINT", qty=1000.0)
    assert F.refresh_open_positions(
        c, {"any": {"preview_exit_fn": lambda p: _preview(50.0)}},
        context_fn=lambda ctxs, pos: ctxs["any"]) == {}
