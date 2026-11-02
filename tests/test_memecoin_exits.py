import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_exits import sweep_exits, _quarantine_spikes, last_accepted_mid



def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open(c, strat, entry=1.0):
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="M", ticker="T",
                          entry_price=entry, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-07T10:00:00+00:00",
                          strategy=strat)
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=100.0 / entry,
                  notional=100.0, fill_source="j", fraction=1.0, created_at="2026-07-07T10:00:00+00:00")
    return pid


def _pm(price, liq=1e15):
    return {"M": {"price": price, "liquidity": liq}}


def test_momentum_a_rides_new_high_then_exits_on_rollover():
    c = _conn(); pid = _open(c, "momentum_a")
    assert sweep_exits(c, _pm(1.5), "2026-07-07T10:00:15+00:00") == []
    assert m.get_position(c, pid)["peak_price"] == 1.5
    sweep_exits(c, _pm(1.4), "2026-07-07T10:00:30+00:00")
    sweep_exits(c, _pm(1.4), "2026-07-07T10:00:45+00:00")
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "a_rollover"
    assert p["max_gain_pct"] is not None and abs(p["max_gain_pct"] - 50.0) < 1e-6
    assert p["time_to_peak_s"] is not None


def test_alpha_b_ignores_momentum_holds_until_wide_stop():
    c = _conn(); pid = _open(c, "alpha_b")
    for t in ("10:00:15", "10:00:30", "10:00:45"):
        sweep_exits(c, _pm(0.9), f"2026-07-07T{t}+00:00")
    assert m.get_position(c, pid)["is_open"] == 1
    sweep_exits(c, _pm(0.45), "2026-07-07T10:01:00+00:00")
    assert m.get_position(c, pid)["exit_reason"] == "b_stop"


def test_control_uses_sl_tp():
    c = _conn(); pid = _open(c, "control")
    sweep_exits(c, _pm(0.35), "2026-07-07T10:00:15+00:00")
    assert m.get_position(c, pid)["exit_reason"] == "stop_loss"


def test_held_s_recovers_true_age_for_host_stamped_legacy_position(monkeypatch):
    # CRITICAL (platform review): after the real-UTC clock switch, a position opened PRE-deploy
    # has a host-clock opened_at (+7h ahead of real), so held_s = real_now - host_open goes
    # NEGATIVE for anything younger than the offset -> its time cap silently would not fire for
    # ~7h. _held_s now recovers true age via the learned host->real offset when the naive diff is
    # negative. Correction happens ONLY when negative, so a same-clock position never over-corrects
    # and nothing is closed prematurely.
    import sniper.services.memecoin_exits as ex
    monkeypatch.setattr("sniper.util.clock.offset_s", lambda *a, **k: 25200.0)   # +7h host skew
    # real now 10:00; real_open 08:00 (2h ago); host_open = 08:00 + 7h = 15:00 (in the FUTURE)
    held = ex._held_s("2026-07-08T15:00:00+00:00", "2026-07-08T10:00:00+00:00")
    assert abs(held - 7200.0) < 1.0        # true age 2h, not the naive -5h
    # a same-clock (real-UTC) position: naive positive -> NO correction
    assert abs(ex._held_s("2026-07-08T09:00:00+00:00", "2026-07-08T10:00:00+00:00") - 3600.0) < 1.0


def test_close_finalizes_zero_qty_claim_instead_of_half_closing():
    # Hardening: if a malformed/pre-entry position reaches _close with no remaining fill qty,
    # claim_close has already flipped is_open=0. The close must still get final bookkeeping so it
    # does not become an ambiguous half-closed row.
    import sniper.services.memecoin_exits as ex
    c = _conn()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at="2026-07-07T10:00:00+00:00", strategy="alpha_b")
    pos = m.get_position(c, pid)
    r = ex._close(c, pos, 0.5, 1e15, "2026-07-07T10:05:00+00:00", "b_timecap")
    closed = m.get_position(c, pid)
    assert r["exit_reason"] == "zero_qty_close"
    assert closed["is_open"] == 0
    assert closed["closed_at"] == "2026-07-07T10:05:00+00:00"
    # A zero-qty claim has no exit legs to price, so the outcome is unknown, not a flat zero.
    assert closed["realized_pnl"] is None
    assert closed["exit_reason"] == "zero_qty_close"


def test_alpha_b_timecap_fires_without_live_quote_never_priced():
    # REGRESSION (deep review P0 #3): an unquotable mint (DexScreener returns nothing) must NOT
    # ride open past its time cap forever (zombie leak). A never-priced position closes at entry
    # (0 PnL, no fabricated loss/gain) tagged b_timecap_noquote so it is filterable.
    c = _conn(); pid = _open(c, "alpha_b", entry=1.0)
    sweep_exits(c, {}, "2026-07-07T23:00:00+00:00")     # opened 10:00 -> 13h > 12h cap; empty map
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "b_timecap_noquote"
    assert abs((p["realized_pnl"] or 0.0)) < 1e-9        # entry-priced -> exactly 0 PnL


def test_alpha_b_timecap_uses_last_known_price_when_unquotable():
    # A position priced earlier then gone dark closes at the LAST observed price (with
    # conservative slippage), not at entry — the real market we last saw, tagged _stale.
    c = _conn(); pid = _open(c, "alpha_b", entry=1.0)
    sweep_exits(c, _pm(0.8), "2026-07-07T11:00:00+00:00")   # -20% (< 60% stop), <12h -> holds; last_quote=0.8
    assert m.get_position(c, pid)["is_open"] == 1
    sweep_exits(c, {}, "2026-07-07T23:30:00+00:00")         # 13.5h > cap, now unquotable
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "b_timecap_stale"
    assert (p["realized_pnl"] or 0.0) < 0                   # last-known 0.8 < entry 1.0 -> a loss


def test_momentum_a_maxhold_fires_without_live_quote():
    # momentum_a max_hold must also fire when unquotable (its exit sweep skipped on no-mid too).
    c = _conn(); pid = _open(c, "momentum_a", entry=1.0)
    sweep_exits(c, {}, "2026-07-07T10:20:00+00:00")     # opened 10:00 -> 20m > 15m A_MAX_HOLD; empty map
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "a_timecap_noquote"


def test_control_peak_tracked_so_instrumentation_is_real():
    # REGRESSION (review Critical #3): control/alpha_b must track peak so time_to_peak_s /
    # max_gain_pct are REAL, not fabricated 0 (which faked exit_liquidity_rate=1.0).
    c = _conn(); pid = _open(c, "control")
    sweep_exits(c, _pm(1.4), "2026-07-07T10:00:15+00:00")   # pumped to 1.4 -> peak
    sweep_exits(c, _pm(1.4), "2026-07-07T10:00:30+00:00")   # still up, no exit
    sweep_exits(c, _pm(0.3), "2026-07-07T10:00:45+00:00")   # -70% -> SL close
    p = m.get_position(c, pid)
    assert p["exit_reason"] == "stop_loss"
    assert abs(p["max_gain_pct"] - 40.0) < 1e-6            # peaked at 1.4 vs entry 1.0 (REAL, not 0)
    assert p["time_to_peak_s"] is not None and p["time_to_peak_s"] > 0   # peaked at sweep 1, not entry


def test_bracket_pass_normalizes_legacy_host_open_before_timecap(monkeypatch):
    # Transition risk: after the real-UTC switch, an already-open host-clock position can have
    # true age > 12h while naive real_now - opened_at is only 6h. The pre-sweep normalizer must
    # shift that open row before timecap logic runs, or it stays open 7h too long.
    from sniper.services.memecoin_runner import bracket_pass

    monkeypatch.setattr("sniper.util.clock.offset_s", lambda *a, **k: 25200.0)
    c = _conn()
    ev = {"fomo_event_id": "legacy-buy", "source_user_id": "u1", "source_handle": "h",
          "mint": "M", "ticker": "T", "side": "buy", "their_price": 1.0,
          "their_market_cap": 100.0, "their_size_usd": 1000.0, "is_first_buy": 1,
          "created_at": "2026-07-08T08:59:58+00:00",
          "received_at": "2026-07-08T16:00:00+00:00", "lag_s": 2.0}
    m.insert_event(c, ev)
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0,
                          entry_fill_source="dexscreener", entry_lag_s=2.0,
                          their_size_usd=1000.0,
                          opened_at="2026-07-08T16:00:00+00:00",
                          strategy="alpha_b", origin_event_id="legacy-buy")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source="dexscreener", fraction=1.0,
                  created_at="2026-07-08T16:00:00+00:00")

    bracket_pass(c, price_fn=lambda _mints: {}, now_fn=lambda: "2026-07-08T22:00:00+00:00")
    p = m.get_position(c, pid)
    assert p["is_open"] == 0
    assert p["opened_at"] == "2026-07-08T09:00:00+00:00"
    assert p["exit_reason"] == "b_timecap_noquote"


# --- Bad-tick spike guard (Jotchua 07-14 regression) --------------------------------------------
#
# A dexscreener glitch returned a phantom $17.099 tick for a coin whose real price was $0.003
# during the exit sweep at 2026-07-14T15:24:37. Both `control` and `real_shadow` arms took TP on
# it, each booking +$376,483.56 of phantom paper PnL. The fingerprint of the glitch was always the
# same — price AND liquidity both jumped ~10x in a single 15s sweep, as if a different pool had
# been returned. Guard: any incoming tick with mid >= 10x the best-known prior mid for the same
# (chain, mint) among open positions is quarantined (written to marks with quality='quarantined_
# spike' for forensics, then dropped from the map every downstream exit reads).


def test_spike_guard_quarantines_the_jotchua_style_glitch():
    """Real-world reproduction: prior $0.003402 -> new $17.099 = ~5028x. Must NOT close on it."""
    c = _conn(); pid = _open(c, "control", entry=0.003531)
    sweep_exits(c, _pm(0.003402), "2026-07-14T15:24:22+00:00")   # baseline realistic tick
    assert m.get_position(c, pid)["last_quote_price"] and \
           abs(m.get_position(c, pid)["last_quote_price"] - 0.003402) < 1e-9
    # The phantom spike sweep — 5000x up in 15s
    closed = sweep_exits(c, _pm(17.099, liq=2_632_867), "2026-07-14T15:24:37+00:00")
    p = m.get_position(c, pid)
    assert closed == []                                # exit path never saw the spike
    assert p["is_open"] == 1                           # position stays open
    assert p["exit_reason"] is None
    # last_quote_price must not be poisoned by the glitch
    assert p["last_quote_price"] and p["last_quote_price"] < 1.0
    # Quarantined mark is still on the audit trail with the right quality tag
    rows = c.execute("SELECT sweep_ts, mid, quality FROM memecoin_position_marks "
                     "WHERE mint='M' ORDER BY sweep_ts").fetchall()
    qualities = [r["quality"] for r in rows]
    assert "quarantined_spike" in qualities
    # The clean tick that came before is 'live', the spike sits alongside as 'quarantined_spike'
    spike_row = [r for r in rows if r["quality"] == "quarantined_spike"][0]
    assert abs(spike_row["mid"] - 17.099) < 1e-6


def test_spike_guard_recovers_after_one_bad_tick():
    """A single bad tick must not stall the position forever — the next plausible tick must be
    acted on normally. Verified by firing a stop_loss on the tick immediately after the glitch."""
    c = _conn(); pid = _open(c, "control", entry=1.0)
    sweep_exits(c, _pm(0.9), "2026-07-07T10:00:15+00:00")           # baseline
    sweep_exits(c, _pm(50.0), "2026-07-07T10:00:30+00:00")          # 55x spike -> quarantined
    assert m.get_position(c, pid)["is_open"] == 1
    sweep_exits(c, _pm(0.3), "2026-07-07T10:00:45+00:00")           # -70% -> stop_loss fires
    p = m.get_position(c, pid)
    assert p["exit_reason"] == "stop_loss"


def test_spike_guard_lets_a_plausible_pump_through():
    """A within-limit pump (up to ~10x) must NOT be quarantined — verified at the filter layer so
    the assertion doesn't depend on whichever TP threshold `control` happens to be configured to."""
    c = _conn(); pid = _open(c, "control", entry=1.0)
    sweep_exits(c, _pm(2.0), "2026-07-07T10:00:15+00:00")           # baseline: last_quote = 2.0
    # Feed a 4x pump (2.0 -> 8.0 = 4x, still below the 10x limit) — must sail through the guard
    clean = _quarantine_spikes(c, {"M": {"price": 8.0, "liquidity": 1e12}},
                               "2026-07-07T10:00:30+00:00")
    assert clean == {"M": {"price": 8.0, "liquidity": 1e12}}
    # And the tick right on the boundary — 2.0 * 10 = 20.0 — DOES quarantine (>= is the rule)
    boundary = _quarantine_spikes(c, {"M": {"price": 20.0, "liquidity": 1e12}},
                                  "2026-07-07T10:00:31+00:00")
    assert boundary == {}


def test_spike_guard_does_not_touch_down_moves():
    """A 10x DOWN move can be a real rug. The stop_loss must still fire — only up-moves guarded."""
    c = _conn(); pid = _open(c, "alpha_b", entry=1.0)
    sweep_exits(c, _pm(0.9), "2026-07-07T10:00:15+00:00")           # baseline
    sweep_exits(c, _pm(0.05), "2026-07-07T10:00:30+00:00")          # 18x DOWN — real rug
    p = m.get_position(c, pid)
    assert p["exit_reason"] == "b_stop"                             # stop fired as intended


def test_spike_guard_ignores_fresh_mints_with_no_prior():
    """A brand-new (chain,mint) with no open positions has no prior baseline — passes through.
    The very first tick isn't guardable and shouldn't be quarantined out of paranoia."""
    c = _conn()
    # No positions in DB at all
    clean = _quarantine_spikes(c, {"NEW_MINT": {"price": 5.0, "liquidity": 1e12}},
                               "2026-07-07T10:00:00+00:00")
    assert clean == {"NEW_MINT": {"price": 5.0, "liquidity": 1e12}}


def test_spike_guard_uses_entry_price_when_last_quote_missing():
    """A position that just opened (last_quote_price=NULL) still has entry_price — the first sweep
    on that mint must be guarded, or a first-sweep glitch slips through."""
    c = _conn(); pid = _open(c, "control", entry=1.0)
    # Simulate: last_quote_price is NULL (never sweeped), only entry_price=1.0 present
    c.execute("UPDATE memecoin_positions SET last_quote_price=NULL, last_quote_ts=NULL WHERE id=?",
              (pid,))
    c.commit()
    clean = _quarantine_spikes(c, {"M": {"price": 50.0, "liquidity": 1e12}},
                               "2026-07-07T10:00:15+00:00")
    assert clean == {}                                              # 50x on entry=1.0 quarantined


# --- trail_d: mirror-sell + trailing stop that arms after +activate%, exits trail% off peak ---

def test_trail_d_exit_reason_pure():
    from sniper.services.memecoin_brackets import trail_d_exit_reason
    kw = dict(activate_pct=30, trail_pct=25, stop_pct=50, max_hold_s=43200)
    # not yet activated (peak +20% < 30%) -> hold even on a draw
    assert trail_d_exit_reason(entry_price=1.0, mid=1.0, peak_price=1.2, held_s=100, **kw) is None
    # activated (peak +50%), drawn 26.7% off peak (1.5 -> 1.1) -> trail fires
    assert trail_d_exit_reason(entry_price=1.0, mid=1.1, peak_price=1.5, held_s=100, **kw) == "d_trail"
    # activated but shallow draw (1.5 -> 1.4 = 6.7% off) -> hold, ride the winner
    assert trail_d_exit_reason(entry_price=1.0, mid=1.4, peak_price=1.5, held_s=100, **kw) is None
    # catastrophe stop is live even before activation (-60% <= -50%)
    assert trail_d_exit_reason(entry_price=1.0, mid=0.4, peak_price=1.0, held_s=100, **kw) == "d_stop"
    # stop takes precedence over a would-be trail
    assert trail_d_exit_reason(entry_price=1.0, mid=0.4, peak_price=1.5, held_s=100, **kw) == "d_stop"
    # wide timecap backstops a flat, never-activated bag
    assert trail_d_exit_reason(entry_price=1.0, mid=1.0, peak_price=1.0, held_s=50000, **kw) == "d_timecap"


def test_trail_d_rides_up_then_trails_off_peak():
    c = _conn(); pid = _open(c, "trail_d")
    # ride to +50% — activates the trail, no exit
    assert sweep_exits(c, _pm(1.5), "2026-07-07T10:00:15+00:00") == []
    assert m.get_position(c, pid)["peak_price"] == 1.5
    # shallow pullback to 1.4 (6.7% off peak) — still holding
    assert sweep_exits(c, _pm(1.4), "2026-07-07T10:00:30+00:00") == []
    assert m.get_position(c, pid)["is_open"] == 1
    # draw 26.7% off the peak — trailing stop fires
    sweep_exits(c, _pm(1.1), "2026-07-07T10:00:45+00:00")
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "d_trail"
    assert p["max_gain_pct"] is not None and abs(p["max_gain_pct"] - 50.0) < 1e-6


def test_trail_d_catastrophe_stop_before_activation():
    c = _conn(); pid = _open(c, "trail_d")
    sweep_exits(c, _pm(0.45), "2026-07-07T10:00:15+00:00")   # -55%, never activated -> d_stop
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "d_stop"


def test_trail_d_is_mirror_and_default_arm():
    from sniper.services.memecoin_copy_trader import MIRROR_ARMS
    from sniper.settings.memecoin import DEFAULT_MEMECOIN_ARMS
    assert "trail_d" in MIRROR_ARMS               # trader-sell mirror-closes it (the profit engine)
    assert "trail_d" in DEFAULT_MEMECOIN_ARMS      # runs paper by default alongside control/alpha_b


# --- peak_lock_e: paper-only peak activation + no-new-high harvest ---

def test_peak_lock_e_exit_reason_pure():
    from sniper.services.memecoin_brackets import peak_lock_e_exit_reason

    kw = dict(activate_pct=30, stall_sweeps=2, stop_pct=50, max_hold_s=43200)
    assert peak_lock_e_exit_reason(
        entry_price=1.0, mid=1.0, peak_price=1.2, no_new_high_sweeps=20,
        held_s=100, **kw,
    ) is None
    assert peak_lock_e_exit_reason(
        entry_price=1.0, mid=1.2, peak_price=1.3, no_new_high_sweeps=2,
        held_s=100, **kw,
    ) == "e_peak_stall"
    assert peak_lock_e_exit_reason(
        entry_price=1.0, mid=0.4, peak_price=1.3, no_new_high_sweeps=2,
        held_s=100, **kw,
    ) == "e_stop"
    assert peak_lock_e_exit_reason(
        entry_price=1.0, mid=1.0, peak_price=1.0, no_new_high_sweeps=20,
        held_s=50000, **kw,
    ) == "e_timecap"


def test_peak_lock_e_resets_on_high_then_harvests_after_two_stall_sweeps():
    c = _conn(); pid = _open(c, "peak_lock_e")
    assert sweep_exits(c, _pm(1.3), "2026-07-07T10:00:15+00:00") == []
    assert m.get_position(c, pid)["no_new_high_sweeps"] == 0
    assert sweep_exits(c, _pm(1.29), "2026-07-07T10:00:30+00:00") == []
    assert m.get_position(c, pid)["no_new_high_sweeps"] == 1
    # A new high resets the stall count.
    assert sweep_exits(c, _pm(1.4), "2026-07-07T10:00:45+00:00") == []
    assert m.get_position(c, pid)["no_new_high_sweeps"] == 0
    assert sweep_exits(c, _pm(1.39), "2026-07-07T10:01:00+00:00") == []
    closed = sweep_exits(c, _pm(1.38), "2026-07-07T10:01:15+00:00")
    assert closed[0]["exit_reason"] == "e_peak_stall"
    assert m.get_position(c, pid)["is_open"] == 0


def test_peak_lock_e_is_paper_mirror_and_default_arm():
    from sniper.services.memecoin_copy_trader import MIRROR_ARMS
    from sniper.settings.memecoin import DEFAULT_MEMECOIN_ARMS

    assert "peak_lock_e" in MIRROR_ARMS
    assert "peak_lock_e" in DEFAULT_MEMECOIN_ARMS


# ---- uniform max-hold backstop (2026-07-21) --------------------------------------------------
# control has no timecap by design, so a coin that never trips SL/TP rides open forever
# (13-day stragglers observed). One arm-agnostic cap bounds every paper position uniformly, so
# cross-arm comparisons stay fair. Default 14d; 0 disables.

def test_max_hold_cap_closes_an_ancient_control_position(monkeypatch):
    from sniper.settings import memecoin as S
    monkeypatch.setattr(S, "MEMECOIN_PAPER_MAX_HOLD_D", 14.0, raising=False)
    c = _conn(); pid = _open(c, "control")
    # price pinned between SL and TP -> control's own rails never fire
    sweep_exits(c, _pm(1.01), "2026-07-22T10:00:00+00:00")   # 15 days after open
    p = m.get_position(c, pid)
    assert p["is_open"] == 0
    assert p["exit_reason"] == "max_hold_cap"


def test_max_hold_cap_dark_position_closes_at_last_mid(monkeypatch):
    from sniper.settings import memecoin as S
    monkeypatch.setattr(S, "MEMECOIN_PAPER_MAX_HOLD_D", 14.0, raising=False)
    c = _conn(); pid = _open(c, "control")
    sweep_exits(c, _pm(1.01), "2026-07-07T10:00:15+00:00")   # one live quote recorded
    sweep_exits(c, {}, "2026-07-22T10:00:00+00:00")          # dark 15 days later
    p = m.get_position(c, pid)
    assert p["is_open"] == 0
    assert p["exit_reason"].startswith("max_hold_cap")


def test_max_hold_cap_leaves_young_positions_and_respects_off(monkeypatch):
    from sniper.settings import memecoin as S
    monkeypatch.setattr(S, "MEMECOIN_PAPER_MAX_HOLD_D", 14.0, raising=False)
    c = _conn(); pid = _open(c, "control")
    sweep_exits(c, _pm(1.01), "2026-07-10T10:00:00+00:00")   # 3 days -> stays open
    assert m.get_position(c, pid)["is_open"] == 1
    monkeypatch.setattr(S, "MEMECOIN_PAPER_MAX_HOLD_D", 0.0, raising=False)
    sweep_exits(c, _pm(1.01), "2026-07-30T10:00:00+00:00")   # 23 days but cap OFF
    assert m.get_position(c, pid)["is_open"] == 1


def test_max_hold_cap_never_touches_real_positions(monkeypatch):
    from sniper.settings import memecoin as S
    monkeypatch.setattr(S, "MEMECOIN_PAPER_MAX_HOLD_D", 14.0, raising=False)
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="M", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="chain",
                          entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at="2026-07-07T10:00:00+00:00",
                          strategy="alpha_b", execution_mode="real")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source="chain", fraction=1.0, created_at="2026-07-07T10:00:00+00:00")
    sweep_exits(c, _pm(1.01), "2026-08-07T10:00:00+00:00")   # 31 days
    assert m.get_position(c, pid)["is_open"] == 1, "real exits belong to real_exit_sweep only"


# ---- blind-chain liquidity enrichment (2026-07-22) -------------------------------------------
# 4663 quote sources carry no liquidity, so every paper exit there booked modeled_fill's
# unknown-liquidity CAP (0.5) — halving each close vs mid and distorting all arm evidence
# (found 2026-07-21 via the replay reconciliation). sweep_exits now fills missing liquidity
# from the runner-persisted pool reserves (memecoin_token_liquidity); no stored reserve keeps
# the conservative cap.

def _open_4663(c, strat="control", entry=1.0):
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint="0xdead", ticker="T",
                          entry_price=entry, entry_notional=100.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at="2026-07-07T10:00:00+00:00",
                          strategy=strat, network_id="4663")
    m.insert_fill(c, position_id=pid, kind="entry", price=entry, qty=100.0 / entry,
                  notional=100.0, fill_source="j", fraction=1.0,
                  created_at="2026-07-07T10:00:00+00:00")
    return pid


def test_sweep_enriches_missing_liquidity_from_token_store():
    c = _conn(); pid = _open_4663(c)
    m.set_token_liquidity(c, "4663", "0xdead", 70_000.0, "2026-07-07T09:59:00+00:00")
    pm = {("4663", "0xdead"): {"price": 0.35}}          # GT-style: price, NO liquidity
    sweep_exits(c, pm, "2026-07-07T10:00:15+00:00")
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "stop_loss"
    # slippage_frac(35, 70000) ~ 0.0005 -> realized ~ -65%, NOT the 0.5-cap's ~ -82.5%
    assert p["realized_pnl"] > -70.0, f"stored liquidity must beat the cap: {p['realized_pnl']}"
    mark = c.execute("SELECT liquidity FROM memecoin_position_marks WHERE chain_id='4663' "
                     "AND mint='0xdead' ORDER BY sweep_ts DESC LIMIT 1").fetchone()
    assert mark and mark["liquidity"] == 70_000.0, "marks must inherit the enriched liquidity"


def test_sweep_keeps_cap_when_no_stored_liquidity():
    c = _conn(); pid = _open_4663(c)
    sweep_exits(c, {("4663", "0xdead"): {"price": 0.35}}, "2026-07-07T10:00:15+00:00")
    p = m.get_position(c, pid)
    assert p["is_open"] == 0
    assert p["realized_pnl"] < -80.0, "unknown liquidity must stay fail-conservative (cap)"


def test_refresh_blind_liquidity_persists_stalest_coin():
    from sniper.services.memecoin_runner import refresh_blind_liquidity
    c = _conn()
    m.set_token_liquidity(c, "4663", "0xaaa", 5_000.0, "2026-07-07T09:00:00+00:00")
    calls = []

    def pool_stats(chain, mint):
        calls.append((chain, mint))
        return {"liquidity": 42_000.0}

    keys = [("4663", "0xaaa"), ("4663", "0xbbb"), ("solana", "SoMint")]
    target = refresh_blind_liquidity(c, keys, "2026-07-07T10:00:00+00:00",
                                     pool_stats_fn=pool_stats)
    assert target == ("4663", "0xbbb"), "absent row must be refreshed before a stale one"
    assert calls == [("4663", "0xbbb")], "exactly ONE pool call per invocation"
    assert m.get_token_liquidity(c, "4663", "0xbbb",
                                 now_iso="2026-07-07T10:30:00+00:00") == 42_000.0


# --- combo_f (2026-07-23): partial de-risk 50% at +20%, remainder trails 30% off peak. The P1/P2
#     combination from the stop/derisk redesign study — counterfactual on the n=29 funded cohort
#     recovered +$85-92 of the -$167 bleed with bounded tail cost (scripts/research/
#     memecoin_partial_derisk_counterfactual.py). PAPER challenger, matched-pair vs control. ---

def test_combo_f_exit_reason_pure():
    from sniper.services.memecoin_brackets import combo_f_exit_reason
    kw = dict(trigger_pct=20.0, trail_pct=30.0, stop_pct=60.0, max_hold_s=0.0)
    # below trigger, not derisked: nothing
    assert combo_f_exit_reason(entry_price=1.0, mid=1.1, peak_price=1.1, derisked=False,
                               held_s=100, **kw) is None
    # crosses trigger, not derisked: partial fires
    assert combo_f_exit_reason(entry_price=1.0, mid=1.25, peak_price=1.25, derisked=False,
                               held_s=100, **kw) == "f_partial"
    # derisked, drawdown below trail: hold
    assert combo_f_exit_reason(entry_price=1.0, mid=1.1, peak_price=1.3, derisked=True,
                               held_s=100, **kw) is None
    # derisked, drawdown >= trail% off peak: trail exit
    assert combo_f_exit_reason(entry_price=1.0, mid=0.9, peak_price=1.3, derisked=True,
                               held_s=100, **kw) == "f_trail"
    # NO trail before the partial (differs from trail_d): deep drawdown pre-derisk is not a trail
    assert combo_f_exit_reason(entry_price=1.0, mid=0.75, peak_price=1.15, derisked=False,
                               held_s=100, **kw) is None
    # catastrophe stop beats everything, derisked or not
    assert combo_f_exit_reason(entry_price=1.0, mid=0.35, peak_price=1.3, derisked=True,
                               held_s=100, **kw) == "f_stop"
    assert combo_f_exit_reason(entry_price=1.0, mid=0.35, peak_price=1.0, derisked=False,
                               held_s=100, **kw) == "f_stop"
    # timecap only when configured on
    assert combo_f_exit_reason(entry_price=1.0, mid=1.0, peak_price=1.0, derisked=False,
                               held_s=99999, **kw) is None
    kw2 = dict(kw, max_hold_s=43200.0)
    assert combo_f_exit_reason(entry_price=1.0, mid=1.0, peak_price=1.0, derisked=False,
                               held_s=50000, **kw2) == "f_timecap"


def test_combo_f_partial_fires_once_then_remainder_trails():
    c = _conn(); pid = _open(c, "combo_f")
    # +25% crossing: 50% partial, position STAYS OPEN, derisked stamp set. The sweep REPORTS the
    # partial event (was `== [] or True`, which proved nothing — and hid that the sweep does emit
    # the f_partial event).
    res = sweep_exits(c, _pm(1.25), "2026-07-07T10:00:15+00:00")
    assert [(r["exit_reason"], r["position_id"]) for r in res] == [("f_partial", pid)]
    p = m.get_position(c, pid)
    assert p["is_open"] == 1
    assert p["derisked_at"] is not None
    fills = c.execute("SELECT kind, fraction, qty FROM memecoin_fills WHERE position_id=? "
                      "AND kind='partial_exit'", (pid,)).fetchall()
    assert len(fills) == 1
    assert abs(fills[0]["fraction"] - 0.5) < 1e-6
    # second sweep above trigger must NOT partial again
    sweep_exits(c, _pm(1.30), "2026-07-07T10:00:30+00:00")
    fills = c.execute("SELECT COUNT(*) AS n FROM memecoin_fills WHERE position_id=? "
                      "AND kind='partial_exit'", (pid,)).fetchone()
    assert fills["n"] == 1
    # drawdown 30%+ off the 1.30 peak -> remainder closes as f_trail
    sweep_exits(c, _pm(0.90), "2026-07-07T10:00:45+00:00")
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "f_trail"
    # realized = partial leg (+25% on half, minus modeled slippage) + trail leg; must be positive
    assert p["realized_pnl"] is not None and p["realized_pnl"] > 0


def test_combo_f_below_trigger_takes_catastrophe_stop():
    c = _conn(); pid = _open(c, "combo_f")
    sweep_exits(c, _pm(1.10), "2026-07-07T10:00:15+00:00")   # never crosses +20
    sweep_exits(c, _pm(0.38), "2026-07-07T10:00:30+00:00")   # -62% <= -60 stop
    p = m.get_position(c, pid)
    assert p["is_open"] == 0 and p["exit_reason"] == "f_stop"
    fills = c.execute("SELECT COUNT(*) AS n FROM memecoin_fills WHERE position_id=? "
                      "AND kind='partial_exit'", (pid,)).fetchone()
    assert fills["n"] == 0


def test_combo_f_enrolled_in_arm_tuples():
    from sniper.services import memecoin_copy_trader as ct
    from sniper.settings import memecoin as cfg
    assert "combo_f" in ct.MIRROR_ARMS      # remainder mirrors the trader's sell, like control
    assert "combo_f" in cfg.DEFAULT_MEMECOIN_ARMS


def test_paper_orphan_close_with_no_fill_is_reopened():
    """claim_close commits is_open=0, then the PAPER close does a route-quote RPC before it books
    any fill. A death in that window strands the row is_open=0 / closed_at NULL / no exit fill.

    orphaned_closes() is scoped `execution_mode='real'`, so paper rows were never recovered:
    _accumulate_arm then laundered them as a clean $0.00 close (`realized_pnl or 0.0`), and a NULL
    exit_reason never matches the `_noquote` censor, so it escaped censoring too.

    Nothing was ever sold in paper, so reopening cannot double-sell (unlike the funded path, which
    must quarantine because on-chain submission truth is unknown)."""
    from sniper.services.memecoin_exits import recover_orphaned_paper_closes
    c = _conn(); pid = _open(c, "momentum_a")
    assert m.claim_close(c, pid) is True
    assert m.get_position(c, pid)["is_open"] == 0

    assert recover_orphaned_paper_closes(c, "2026-07-07T10:05:00+00:00") == 1

    p = m.get_position(c, pid)
    assert p["is_open"] == 1                     # handed back to the sweep, not booked as flat
    assert p["closed_at"] is None and p["realized_pnl"] is None and p["exit_reason"] is None


def test_paper_orphan_close_with_booked_fill_is_finalized_from_proceeds():
    """The other half of the window: the exit fill landed but finalize_close never ran. The sell
    is real, so finalize from booked proceeds rather than reopening."""
    from sniper.services.memecoin_exits import recover_orphaned_paper_closes
    c = _conn(); pid = _open(c, "control")
    assert m.claim_close(c, pid) is True
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=1.2, qty=100.0, notional=120.0,
                  fill_source="route_quote", fraction=1.0, created_at="2026-07-07T10:04:00+00:00")

    assert recover_orphaned_paper_closes(c, "2026-07-07T10:05:00+00:00") == 1

    p = m.get_position(c, pid)
    assert p["is_open"] == 0
    assert p["exit_reason"] == "recovered_orphan_close"
    assert abs(p["realized_pnl"] - 20.0) < 1e-9   # 120 received - 100 entry


def test_paper_orphan_recovery_leaves_real_rows_to_the_funded_sweep():
    """Funded orphans must stay with recover_orphaned_closes, which quarantines instead of
    reopening — reopening a real position that may already have sold on-chain double-sells."""
    from sniper.services.memecoin_exits import recover_orphaned_paper_closes
    c = _conn(); pid = _open(c, "alpha_b")
    c.execute("UPDATE memecoin_positions SET execution_mode='real' WHERE id=?", (pid,)); c.commit()
    assert m.claim_close(c, pid) is True

    assert recover_orphaned_paper_closes(c, "2026-07-07T10:05:00+00:00") == 0
    assert m.get_position(c, pid)["is_open"] == 0


def test_paper_orphan_with_only_a_noquote_fill_stays_censored():
    """AUDIT 2026-08-03 finding 1. Recovery must not launder a censored exit into a measured one.

    `full_exit_proceeds` counts every full-exit leg regardless of `fill_source`, so an orphan whose
    ONLY booked leg was `noquote` (priced at entry because no quote ever landed) took the
    "fill booked -> finalize" branch and was stamped `recovered_orphan_close`. That reason does not
    end in `_noquote`, so the report classified it as a KNOWN outcome — the exact censored->measured
    laundering this recovery path was written to stop.
    """
    from sniper.services.memecoin_exits import recover_orphaned_paper_closes
    c = _conn(); pid = _open(c, "control", entry=1.0)
    assert m.claim_close(c, pid) is True
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=1.0, qty=100.0, notional=100.0,
                  fill_source="noquote", fraction=1.0, created_at="2026-07-07T10:04:00+00:00")

    assert recover_orphaned_paper_closes(c, "2026-07-07T10:30:00+00:00") == 1

    p = m.get_position(c, pid)
    assert p["is_open"] == 0
    assert p["exit_reason"].endswith("_noquote"), p["exit_reason"]
    # close time is the booked fill's time, not when recovery happened to run (a 26-min inflation
    # of the hold here) — hold length feeds median_hold_s and the timecap analyses.
    assert p["closed_at"] == "2026-07-07T10:04:00+00:00"


def test_paper_orphan_with_an_executable_fill_is_measured():
    """The other side: a genuinely executable booked leg must still finalize as a real outcome."""
    from sniper.services.memecoin_exits import recover_orphaned_paper_closes
    c = _conn(); pid = _open(c, "control", entry=1.0)
    assert m.claim_close(c, pid) is True
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=1.2, qty=100.0, notional=120.0,
                  fill_source="route_quote", fraction=1.0, created_at="2026-07-07T10:04:00+00:00")

    assert recover_orphaned_paper_closes(c, "2026-07-07T10:05:00+00:00") == 1

    p = m.get_position(c, pid)
    assert p["exit_reason"] == "recovered_orphan_close"
    assert abs(p["realized_pnl"] - 20.0) < 1e-9


# --- collapse guard (down-side), added after UNIFROG 27097 on 2026-08-05 --------

def test_collapse_guard_quarantines_the_unifrog_tick():
    """The real incident: 0.0002514992 -> 0.0000006323 in ONE sweep (397x), held 7s, recovered.

    It cost money twice: `catastrophe` crossed 80% on the corrupted price when the honest decline
    had only reached 74%, and the exit quote taken inside the window set min_output to ~$0.02, so
    the slippage floor authorised selling a ~$6 bag for $0.02.
    """
    c = _conn(); _open(c, "alpha_b", entry=0.0009669320197528678)
    sweep_exits(c, _pm(0.0002514992), "2026-08-05T16:01:27+00:00")   # honest -74%, accepted
    clean = _quarantine_spikes(c, {"M": {"price": 0.0000006323, "liquidity": None}},
                               "2026-08-05T16:01:28+00:00")
    assert clean == {}                                               # never reaches a decision
    row = c.execute("SELECT quality FROM memecoin_position_marks WHERE mid < 1e-6").fetchone()
    assert row and row[0] == "quarantined_spike"                     # evidence kept for forensics


def test_collapse_guard_still_lets_a_real_rug_fire_the_stop():
    """A genuine collapse walks the reference down sweep by sweep and stays admissible.

    This is why the down reference is the last ACCEPTED tape mark and not max(last_quote, entry):
    pinning it at the entry would quarantine every tick of a real rug and the stop would never fire.
    """
    c = _conn(); pid = _open(c, "alpha_b", entry=1.0)
    for price, ts in ((0.9, "10:00:15"), (0.35, "10:00:30"), (0.12, "10:00:45"), (0.04, "10:01:00")):
        sweep_exits(c, _pm(price), f"2026-07-07T{ts}+00:00")
    p = m.get_position(c, pid)
    assert p["is_open"] == 0
    assert p["exit_reason"] == "b_stop"


def test_collapse_guard_boundary_is_inclusive():
    c = _conn(); _open(c, "control", entry=1.0)
    sweep_exits(c, _pm(2.0), "2026-07-07T10:00:15+00:00")            # accepted baseline 2.0
    # 2.0 / 20 = 0.1 exactly -> quarantined (>= is the rule, same as the up side)
    assert _quarantine_spikes(c, {"M": {"price": 0.1, "liquidity": 1e12}},
                              "2026-07-07T10:00:30+00:00") == {}
    # just inside the bound -> admitted
    admitted = _quarantine_spikes(c, {"M": {"price": 0.11, "liquidity": 1e12}},
                                  "2026-07-07T10:00:31+00:00")
    assert admitted == {"M": {"price": 0.11, "liquidity": 1e12}}


def test_collapse_guard_ignores_a_mint_with_no_accepted_tape():
    """No accepted mark yet -> nothing to measure against; the tick passes rather than being
    quarantined out of paranoia. Matches the up-side's fresh-mint behaviour."""
    c = _conn()
    clean = _quarantine_spikes(c, {"NEW_MINT": {"price": 1e-9, "liquidity": 1e12}},
                               "2026-07-07T10:00:00+00:00")
    assert clean == {"NEW_MINT": {"price": 1e-9, "liquidity": 1e12}}


def test_collapse_guard_can_be_disabled_by_config():
    c = _conn(); _open(c, "alpha_b", entry=1.0)
    sweep_exits(c, _pm(0.9), "2026-07-07T10:00:15+00:00")
    passed = _quarantine_spikes(c, {"M": {"price": 1e-9, "liquidity": None}},
                                "2026-07-07T10:00:30+00:00", collapse_limit=1.0)
    assert passed == {"M": {"price": 1e-9, "liquidity": None}}


# --- review findings, 2026-08-05 -----------------------------------------------

def test_write_then_decide_order_disarms_the_guard():
    """The BLOCKER. Reproduces the funded fast lane's real order: persist the map, THEN validate.

    Once the corrupted tick is on the tape as an accepted mark it becomes its own reference, the
    ratio collapses to 1x and the guard cannot fire. This is why the fast lane must filter first.
    """
    from sniper.services.memecoin_exits import write_marks
    c = _conn(); _open(c, "alpha_b", entry=1.0)
    # Production shape verbatim: source='fastlane_rpc', quality='exact_route'. Omitting quality
    # defaults to 'live', which is NOT what the funded fast lane writes.
    base = {("solana", "M"): {"price": 1.0, "source": "fastlane_rpc",
                              "quality": "exact_route"}}
    write_marks(c, base, "2026-07-07T10:00:00+00:00", min_interval_s=0, open_min_interval_s=0)
    bad = {("solana", "M"): {"price": 0.001, "liquidity": None, "source": "fastlane_rpc",
                             "quality": "exact_route"}}
    # Production shape: write_marks stamps its own real_utc_now_iso(), then real_exit_sweep
    # evaluates at a LATER instant — so the corrupted mark is strictly prior and IS selected as
    # the reference. (The strict `<` bound alone only defends the equal-timestamp case.)
    write_marks(c, bad, "2026-07-07T10:00:02+00:00",
                min_interval_s=0, open_min_interval_s=0)   # the production mistake
    assert c.execute(
        "SELECT quality FROM memecoin_position_marks ORDER BY sweep_ts DESC LIMIT 1"
    ).fetchone()[0] == "exact_route"                                 # the shape production writes
    leaked = _quarantine_spikes(c, bad, "2026-07-07T10:00:03+00:00")
    assert leaked == bad                                             # guard is disarmed


def test_filter_before_write_keeps_the_guard_armed():
    """The fix: validate first, then persist. Same tick, opposite outcome."""
    from sniper.services.memecoin_exits import filter_implausible_ticks, write_marks
    c = _conn(); _open(c, "alpha_b", entry=1.0)
    # tuple key throughout — the shape the funded fast lane actually passes and write_marks stores
    write_marks(c, {("solana", "M"): {"price": 1.0, "source": "fastlane_rpc",
                                      "quality": "exact_route"}},
                "2026-07-07T10:00:00+00:00", min_interval_s=0, open_min_interval_s=0)
    bad = {("solana", "M"): {"price": 0.001, "liquidity": None, "source": "fastlane_rpc",
                             "quality": "exact_route"}}
    clean = filter_implausible_ticks(c, bad, "2026-07-07T10:00:02+00:00")
    assert clean == {}
    write_marks(c, clean, "2026-07-07T10:00:02+00:00")               # nothing to persist
    assert last_accepted_mid(c, "solana", "M", "2026-07-07T10:00:03+00:00") == 1.0


def test_accepted_mid_never_reads_the_future():
    """A backdated or replayed sweep must not price itself from marks it has not reached."""
    from sniper.services.memecoin_exits import write_marks
    c = _conn(); _open(c, "alpha_b", entry=1.0)
    write_marks(c, {("solana", "M"): {"price": 1.0}}, "2026-07-07T09:59:00+00:00",
                min_interval_s=0, open_min_interval_s=0)
    write_marks(c, {("solana", "M"): {"price": 99.0}}, "2026-07-07T11:00:00+00:00",
                min_interval_s=0, open_min_interval_s=0)
    assert last_accepted_mid(c, "solana", "M", "2026-07-07T10:00:00+00:00") == 1.0


def test_accepted_mid_excludes_the_candidate_sweep_itself():
    """Strict `<`: a mark stamped with this very sweep's timestamp is not its own reference."""
    from sniper.services.memecoin_exits import write_marks
    c = _conn(); _open(c, "alpha_b", entry=1.0)
    write_marks(c, {("solana", "M"): {"price": 1.0}}, "2026-07-07T10:00:00+00:00",
                min_interval_s=0, open_min_interval_s=0)
    write_marks(c, {("solana", "M"): {"price": 0.001}}, "2026-07-07T10:00:02+00:00",
                min_interval_s=0, open_min_interval_s=0)
    assert last_accepted_mid(c, "solana", "M", "2026-07-07T10:00:02+00:00") == 1.0


def test_a_genuine_one_tick_collapse_releases_at_the_configured_boundary():
    """A real >=20x rug in ONE sweep is quarantined at first — we cannot tell it from a glitch.

    It must not be quarantined forever: once the pre-rug mark ages past
    MEMECOIN_ACCEPTED_MID_WINDOW_S there is no reference, the tick is admitted and the floor
    check has nothing to block against. Asserted just INSIDE and just OUTSIDE the configured
    boundary, so the window itself is protected rather than an arbitrary overshoot.
    """
    from datetime import timedelta

    from sniper.services.memecoin_exits import (
        _ACCEPTED_MID_WINDOW_S, _parse, write_marks)
    c = _conn(); _open(c, "alpha_b", entry=1.0)
    base_ts = _parse("2026-07-07T10:00:00+00:00")
    write_marks(c, {("solana", "M"): {"price": 1.0}}, base_ts.isoformat(),
                min_interval_s=0, open_min_interval_s=0)
    rug = {("solana", "M"): {"price": 0.001, "liquidity": None}}

    inside = (base_ts + timedelta(seconds=_ACCEPTED_MID_WINDOW_S - 1)).isoformat()
    assert _quarantine_spikes(c, dict(rug), inside) == {}            # reference live -> blocked
    assert last_accepted_mid(c, "solana", "M", inside) == 1.0

    outside = (base_ts + timedelta(seconds=_ACCEPTED_MID_WINDOW_S + 1)).isoformat()
    assert _quarantine_spikes(c, dict(rug), outside) == rug          # aged out -> released
    assert last_accepted_mid(c, "solana", "M", outside) is None       # floor unblocked too


def test_accepted_mid_lookup_is_index_shaped_not_a_table_scan():
    """The unfiltered form scanned 1.88M marks at ~0.2s per call against a 1s funded cadence."""
    c = _conn(); _open(c, "alpha_b", entry=1.0)
    plan = [r[3] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT mid FROM memecoin_position_marks "
        "WHERE chain_id=? AND mint=? AND quality != 'quarantined_spike' AND mid > 0 "
        "AND sweep_ts >= ? AND sweep_ts < ? ORDER BY sweep_ts DESC LIMIT 1",
        ("solana", "M", "a", "b"))]
    assert any("idx_mc_marks_uniq" in step or "USING INDEX" in step for step in plan), plan
    assert not any(step.startswith("SCAN memecoin_position_marks") for step in plan), plan


def test_write_marks_uses_a_valid_source_observation_time():
    from sniper.services.memecoin_exits import write_marks

    c = _conn()
    _open(c, "alpha_b", entry=1.0)
    write_marks(
        c,
        {
            ("solana", "M"): {
                "price": 1.1,
                "source": "fastlane_sol_swap",
                "quality": "live",
                "observed_at": "2026-07-07T10:00:01+00:00",
            }
        },
        "2026-07-07T10:00:02+00:00",
        min_interval_s=0,
        open_min_interval_s=0,
    )
    row = c.execute(
        "SELECT sweep_ts,source FROM memecoin_position_marks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(row) == (
        "2026-07-07T10:00:01+00:00",
        "fastlane_sol_swap",
    )


def test_write_marks_rejects_a_future_source_observation_time():
    from sniper.services.memecoin_exits import write_marks

    c = _conn()
    _open(c, "alpha_b", entry=1.0)
    write_marks(
        c,
        {
            ("solana", "M"): {
                "price": 1.1,
                "observed_at": "2026-07-07T10:00:03+00:00",
            }
        },
        "2026-07-07T10:00:02+00:00",
        min_interval_s=0,
        open_min_interval_s=0,
    )
    count = c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0]
    assert count == 0


def test_swap_mark_is_not_blocked_by_the_dex_mid_write_floor():
    from sniper.services.memecoin_exits import write_marks

    c = _conn()
    write_marks(
        c,
        {
            ("solana", "M"): {
                "price": 1.0,
                "source": "fastlane_mid",
            }
        },
        "2026-07-07T10:00:00.500000+00:00",
        min_interval_s=1,
        open_min_interval_s=1,
    )
    write_marks(
        c,
        {
            ("solana", "M"): {
                "price": 1.1,
                "source": "fastlane_sol_swap",
                "observed_at": "2026-07-07T10:00:01+00:00",
            }
        },
        "2026-07-07T10:00:01+00:00",
        min_interval_s=1,
        open_min_interval_s=1,
    )
    count = c.execute("SELECT COUNT(*) FROM memecoin_position_marks").fetchone()[0]
    assert count == 2


# ---- pair-identity alarm (position 40687 / PURR copycat, 2026-08-19) ----
# 661 of 694 marks sat quarantined for 25 minutes at a near-constant 0.1114 while the
# accepted tape moved at 2.2-2.6e-05: the mark source priced a hostile pool, not a
# glitch. A coherent quarantine run must raise an alarm; the guard itself must not change.

def _ts(i):
    return f"2026-08-19T19:{27 + i // 60:02d}:{i % 60:02d}+00:00"


def _poison(c, n, mids=(0.1114,)):
    import sniper.services.memecoin_exits as ex
    for i in range(n):
        out = _quarantine_spikes(c, _pm(mids[i % len(mids)]), _ts(i))
        assert out == {}          # the guard still quarantines every poisoned tick
    return ex


def test_pair_identity_alarm_fires_on_a_coherent_quarantine_run(caplog):
    import logging
    import sniper.services.memecoin_exits as ex
    ex._PAIR_ALARM_LAST.clear()
    c = _conn(); _open(c, "alpha_b", entry=2.6e-05)
    with caplog.at_level(logging.ERROR, logger="sniper.services.memecoin_exits"):
        _poison(c, ex._PAIR_IDENTITY_STREAK)
    alarms = [r for r in caplog.records if "pair-identity" in r.getMessage()]
    assert len(alarms) == 1
    assert "M" in alarms[0].getMessage()


def test_pair_identity_alarm_needs_the_full_streak(caplog):
    import logging
    import sniper.services.memecoin_exits as ex
    ex._PAIR_ALARM_LAST.clear()
    c = _conn(); _open(c, "alpha_b", entry=2.6e-05)
    with caplog.at_level(logging.ERROR, logger="sniper.services.memecoin_exits"):
        _poison(c, ex._PAIR_IDENTITY_STREAK - 1)
    assert not [r for r in caplog.records if "pair-identity" in r.getMessage()]


def test_pair_identity_alarm_ignores_incoherent_glitch_spam(caplog):
    import logging
    import sniper.services.memecoin_exits as ex
    ex._PAIR_ALARM_LAST.clear()
    c = _conn(); _open(c, "alpha_b", entry=2.6e-05)
    with caplog.at_level(logging.ERROR, logger="sniper.services.memecoin_exits"):
        _poison(c, ex._PAIR_IDENTITY_STREAK, mids=(0.05, 3.7, 210.0))
    assert not [r for r in caplog.records if "pair-identity" in r.getMessage()]


def test_pair_identity_alarm_resets_on_an_accepted_mark(caplog):
    import logging
    from sniper.services.memecoin_exits import write_marks
    import sniper.services.memecoin_exits as ex
    ex._PAIR_ALARM_LAST.clear()
    c = _conn(); _open(c, "alpha_b", entry=2.6e-05)
    with caplog.at_level(logging.ERROR, logger="sniper.services.memecoin_exits"):
        _poison(c, 15)
        write_marks(c, {("solana", "M"): {"price": 2.5e-05, "liquidity": 20_000.0}},
                    _ts(15), min_interval_s=0, open_min_interval_s=0)
        for i in range(16, 16 + ex._PAIR_IDENTITY_STREAK - 1):
            assert _quarantine_spikes(c, _pm(0.1114), _ts(i)) == {}
    assert not [r for r in caplog.records if "pair-identity" in r.getMessage()]
