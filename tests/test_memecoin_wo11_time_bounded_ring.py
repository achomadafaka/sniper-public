"""WO-11 (rev 2) — ring retention as a COMPLETENESS CONTRACT, not a formula tweak.

Four corrections over the naive "largest window x margin":
1. Time-eviction is DISABLED until a finite structure-evidence window is REGISTERED — pivots consume
   the whole retained path, so bounding it silently changes prior_low/prior_high (structure CUT).
2. REQUIRED_HISTORY_S = max(VOL, 2xARRIVAL, DEPTH, REGISTERED_STRUCTURE). The 2x on arrival is
   structural (arrival acceleration compares rate_now vs the adjacent rate_prev), not a safety margin.
3. Evict against the MAX OBSERVED event-time watermark — a late corroborating row with an older
   timestamp must never advance or corrupt eviction.
4. _MAX_EVENTS is a memory backstop ONLY (no deque(maxlen) silent truncation). Overflow sets
   `history_incomplete`; while incomplete the hot engine has NO sale authority and the pull lane is
   authoritative; only a pull-complete rebuild (truncation-detecting) clears it.
"""
from sniper.services.exit_engine_hot import (
    _ARRIVAL_WINDOW_S, _DEPTH_WINDOW_S, _VOL_WINDOW_S, HotPositionEngine, required_history_s)


def _engine(*, structure_window_s=None, max_events=100000):
    return HotPositionEngine(1, {"chain_id": "solana", "pool": "P"}, quote_side=1,
                             state_fn=lambda feats, *, now_ts: None,
                             decision_fn=lambda contract, state, cfg: None,
                             structure_window_s=structure_window_s, max_events=max_events)


def _swap(block_no, ts, tx_sig="s"):
    return {"chain_id": "solana", "pool": "P", "kind": "sol_swap", "block_no": block_no,
            "tx_sig": (f"{tx_sig}{block_no}" if tx_sig is not None else None), "log_index": 0,
            "amount0": "-100", "amount1": "200", "block_ts": float(ts)}


# --- 1. the formula is a max of distinct terms; unregistered structure window disables eviction ----

def test_required_history_formula():
    assert required_history_s(None) is None            # unregistered -> eviction DISABLED
    assert required_history_s(0) is None               # non-positive -> disabled (fail closed)
    assert required_history_s(-5) is None
    # every term is present; the 2x on arrival is structural (rate_now vs rate_prev), not margin
    assert required_history_s(10) == max(_VOL_WINDOW_S, 2 * _ARRIVAL_WINDOW_S, _DEPTH_WINDOW_S, 10.0)
    assert required_history_s(1e6) == 1e6              # the structure term can dominate the max


def test_eviction_disabled_until_structure_window_registered():
    eng = _engine(structure_window_s=None, max_events=1000)
    base = 1_000_000.0
    for i in range(50):
        eng.add_tick(_swap(i, base + i * 100.0))       # 4900s span, over every window
    assert len(eng._events) == 50                      # NOTHING time-evicted while unregistered
    assert eng.history_incomplete is False             # still under the count backstop


# --- 2/3. per-window starvation with the structure window registered -------------------------------

def test_vol_window_not_starved():
    struct = 100.0
    eng = _engine(structure_window_s=struct, max_events=100000)   # req = max(300,120,60,100)=300
    req = required_history_s(struct)
    base = 1_000_000.0
    n = int(req) + 200
    for i in range(n + 1):
        eng.add_tick(_swap(i, base + i))
    wm = base + n
    kept = [e["block_ts"] for e in eng._events]
    assert min(kept) >= wm - req - 0.5                 # never older than REQUIRED_HISTORY
    assert min(kept) <= wm - _VOL_WINDOW_S             # the 300s vol window is fully covered


def test_both_adjacent_arrival_windows_retained():
    eng = _engine(structure_window_s=50.0, max_events=100000)
    base = 1_000_000.0
    n = 400
    for i in range(n + 1):
        eng.add_tick(_swap(i, base + i))
    wm = int(base) + n
    kept = {e["block_ts"] for e in eng._events}
    for t in range(wm - 2 * int(_ARRIVAL_WINDOW_S), wm + 1):       # rate_now + rate_prev both intact
        assert float(t) in kept


def test_depth_window_retained():
    eng = _engine(structure_window_s=50.0, max_events=100000)
    base = 1_000_000.0
    n = 400
    for i in range(n + 1):
        eng.add_tick(_swap(i, base + i))
    wm = int(base) + n
    kept = {e["block_ts"] for e in eng._events}
    for t in range(wm - int(_DEPTH_WINDOW_S), wm + 1):
        assert float(t) in kept


def test_registered_structure_window_not_starved():
    struct = 600.0                                     # structure dominates the max -> req = 600
    eng = _engine(structure_window_s=struct, max_events=100000)
    base = 1_000_000.0
    n = 800
    for i in range(n + 1):
        eng.add_tick(_swap(i, base + i))
    wm = base + n
    kept = [e["block_ts"] for e in eng._events]
    assert min(kept) <= wm - _VOL_WINDOW_S - 100       # retains well past the vol window (structure)
    assert min(kept) >= wm - struct - 0.5


# --- 3. older corroboration must not advance eviction ----------------------------------------------

def test_older_corroboration_does_not_advance_eviction():
    eng = _engine(structure_window_s=300.0, max_events=100000)
    base = 1_000_000.0
    for i in range(400):
        eng.add_tick(_swap(i, base + i))               # watermark = base + 399
    wm_before = eng._max_watermark
    # a decoded row for an in-window slot, carrying an OLDER timestamp, is inserted by corroboration
    eng.corroborate([390], [{"chain_id": "solana", "pool": "P", "kind": "sol_swap", "block_no": 390,
                             "tx_sig": "decoded", "log_index": 0, "amount0": "-1", "amount1": "2",
                             "block_ts": base + 390}], now_ts=base + 399)
    assert eng._max_watermark == wm_before             # older row did NOT advance the watermark/cutoff


# --- 4. backstop overflow -> history_incomplete -> no hot sale authority ---------------------------

def test_backstop_overflow_sets_incomplete_no_silent_truncation():
    eng = _engine(structure_window_s=None, max_events=5)           # eviction off; tiny backstop
    base = 1_000_000.0
    for i in range(5):
        eng.add_tick(_swap(i, base + i))
    assert eng.history_incomplete is False and len(eng._events) == 5
    eng.add_tick(_swap(5, base + 5))                              # 6th overruns memory
    assert eng.history_incomplete is True                        # flagged, not silently truncated
    assert len(eng._events) == 5 and 0 not in {e["block_no"] for e in eng._events}   # memory bounded


def test_incomplete_engine_has_no_hot_sale_authority():
    import types

    from sniper.services.memecoin_hot_supervisor import HotSupervisor

    grounded_cut = {"chain_id": "4663", "pool": "P", "kind": "v2_burn", "block_no": 1, "log_index": 0,
                    "tx_sig": "0xh", "block_hash": "0xb", "block_time_true": True,
                    "transaction_decoded": True, "amount0": "1", "amount1": "-1"}

    class _Engines:
        def __init__(self, incomplete):
            self.incomplete = incomplete
        def on_tick(self, key, row, *, now_ts, cfg=None):
            return types.SimpleNamespace(action="CUT", reason_codes=("cut", "break_confirmed"))
        def engine(self, key):
            return types.SimpleNamespace(history_incomplete=self.incomplete)

    # complete engine: a grounded CUT authorizes directly
    ok = []
    HotSupervisor(_Engines(False), now_fn=lambda: 1000.0,
                  on_decision=lambda *a: ok.append(a)).on_ws_row(dict(grounded_cut))
    assert len(ok) == 1 and ok[0][1].action == "CUT"

    # incomplete engine: SAME grounded CUT is NOT authorized -> pull lane covers it
    seen = []
    sup = HotSupervisor(_Engines(True), now_fn=lambda: 1000.0,
                        on_decision=lambda *a: seen.append(a))
    sup.gap.record_decision(999.0)
    sup.on_ws_row(dict(grounded_cut))
    assert seen == []
    assert sup.gap.needs_pull_fallback(1000.0)                   # pull is forced immediately


def test_incomplete_engine_cannot_resolve_ride_or_persist_structure_pending():
    """A non-sale still cannot reset the gap, and truncated structure cannot enter the durable
    controller through the special hysteresis-pending callback."""
    import types

    from sniper.services.memecoin_hot_supervisor import HotSupervisor

    row = {"chain_id": "4663", "pool": "P", "kind": "v2_swap", "block_no": 1,
           "log_index": 0, "tx_sig": "0xh", "block_hash": "0xb", "block_time_true": True,
           "transaction_decoded": True, "amount0": "1", "amount1": "-1"}

    class _Engines:
        def __init__(self, decision):
            self.decision = decision
        def on_tick(self, key, event, *, now_ts, cfg=None):
            return self.decision
        def engine(self, key):
            return types.SimpleNamespace(history_incomplete=True)

    for decision in (
        types.SimpleNamespace(action="RIDE", reason_codes=()),
        types.SimpleNamespace(action="RIDE", reason_codes=("structure_hysteresis_pending",)),
    ):
        seen = []
        sup = HotSupervisor(_Engines(decision), now_fn=lambda: 1000.0,
                            on_decision=lambda *args: seen.append(args))
        sup.gap.record_decision(999.0)
        sup.on_ws_row(dict(row))
        assert seen == []
        assert sup.gap.needs_pull_fallback(1000.0)


# --- 4. rebuild: complete restores authority, truncated does not ----------------------------------

def _evm_ticks_conn(n_rows, base):
    import sqlite3

    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    t.ensure_block_times(c)
    ensure_feature_tables(c)
    c.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,"
              "token0_decimals,token1_decimals,resolved_at) VALUES('4663','0xp','0xm','0xq',1,18,18,'t')")
    for i in range(1, n_rows + 1):
        t.insert_ticks(c, [{"chain_id": "4663", "pool": "0xp", "kind": "v2_swap", "block_no": i,
                            "tx_sig": f"0x{i}", "log_index": 0, "amount0": str(100 + i),
                            "amount1": str(-(40 + i)), "observed_at": "i"}])
        t.backfill_block_times(c, "4663", [i], lambda _b, i=i: base + i, "i")
    return c


def _evm_position(base):
    from datetime import datetime, timezone
    return {"id": 7, "network_id": "4663", "mint": "0xm", "quote_route_target": "0xr",
            "derisked_at": None, "exact_quote_misses": 0,
            "opened_at": datetime.fromtimestamp(base - 100, tz=timezone.utc).isoformat()}


def _patch_engine_policy(monkeypatch):
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID
    policy = type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano",
                            "decision_engine_version": "v5"})()
    monkeypatch.setattr("sniper.services.memecoin_funded_policy.frozen_policy_from_position",
                        lambda _pos: policy)


def test_complete_pull_rebuild_restores_authority(monkeypatch):
    from sniper.services.memecoin_hot_supervisor import build_evm_hot_book
    monkeypatch.setattr("sniper.services.exit_engine_hot._MAX_EVENTS", 5)
    _patch_engine_policy(monkeypatch)
    base = 1_800_000_000
    conn = _evm_ticks_conn(3, base)                              # 3 rows < cap 5 -> complete
    _engines, entries, _held = build_evm_hot_book([_evm_position(base)], conn, now_fn=lambda: base + 4)
    assert entries[("4663", "0xp")]["engine"].history_incomplete is False


def test_truncated_rebuild_does_not_restore_authority(monkeypatch):
    from sniper.services.memecoin_hot_supervisor import build_evm_hot_book
    monkeypatch.setattr("sniper.services.exit_engine_hot._MAX_EVENTS", 5)
    _patch_engine_policy(monkeypatch)
    base = 1_800_000_000
    conn = _evm_ticks_conn(12, base)                            # 12 rows > cap 5 -> LIMIT cap+1 detects
    _engines, entries, _held = build_evm_hot_book([_evm_position(base)], conn, now_fn=lambda: base + 13)
    assert entries[("4663", "0xp")]["engine"].history_incomplete is True


# --- restart-warm / live equivalence + corroboration still green ----------------------------------

def test_restart_warm_equals_live_time_bounded():
    base = 1_000_000.0
    tape = [_swap(i, base + i * 3.0) for i in range(300)]        # 0..897s, every 3s
    warm = _engine(structure_window_s=300.0, max_events=100000)
    for tk in tape:
        warm.add_tick(dict(tk))
    live = _engine(structure_window_s=300.0, max_events=100000)
    for tk in tape:
        live.add_tick(dict(tk))
        live.features(now_ts=tk["block_ts"])
    assert [e["block_no"] for e in warm._events] == [e["block_no"] for e in live._events]
    assert warm.history_incomplete == live.history_incomplete
