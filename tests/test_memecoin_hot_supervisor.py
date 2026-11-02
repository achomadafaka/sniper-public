"""WO-1 Phase B — hot supervisor: fail-closed pull fallback, fabricated-notification rejection,
pull-lane verification of dropped WS events, pre-warmed exits, latency measurement."""
from sniper.services.memecoin_hot_supervisor import (DecisionGapTracker, HotSupervisor,
                                                     PrewarmedExits, PullVerifier, build_hot_book)


class _FakeEngines:
    """Stand-in for the (already-tested) HotEngineSet: decides for held pools, ignores the rest."""
    def __init__(self, held, decision="CUT"):
        self.held = set(held); self.decision = decision
    def on_tick(self, key, row, *, now_ts, cfg=None):
        return self.decision if key in self.held else None


class _Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t


def _row(pool="POOL", slot=100, notified=None):
    return {"chain_id": "solana", "pool": pool, "kind": "sol_swap", "block_no": slot,
            "amount0": "1", "amount1": "-1", "notified_ts": notified}


def test_ws_outage_degrades_to_pull_within_60s():
    clock = _Clock(1000.0)
    sup = HotSupervisor(_FakeEngines({("solana", "POOL")}), now_fn=clock, max_decision_gap_s=60.0)
    assert sup.on_ws_row(_row()) == "CUT"            # a live WS decision
    assert sup.gap.needs_pull_fallback(clock.t) is False
    clock.t += 61                                    # WS goes silent past the bound
    assert sup.gap.needs_pull_fallback(clock.t) is True   # fail-closed -> pull must cover
    sup.record_pull_decision(clock.t)                # the 30s pull lane produces a decision
    assert sup.gap.needs_pull_fallback(clock.t) is False  # gap reset inside the bound, no >60s hole


def test_fabricated_or_unheld_pool_makes_no_decision():
    clock = _Clock()
    sup = HotSupervisor(_FakeEngines({("solana", "POOL")}), now_fn=clock)
    # a row for a pool we do not hold (fabricated / wrong mint) -> no decision, gap untouched
    assert sup.on_ws_row(_row(pool="EVIL")) is None
    assert sup.gap.last_decision_ts is None


def test_pull_verifier_catches_dropped_ws_event():
    clock = _Clock()
    sup = HotSupervisor(_FakeEngines({("solana", "POOL")}), now_fn=clock)
    sup.on_ws_row(_row(slot=100)); sup.on_ws_row(_row(slot=101))
    dropped = sup.verify_against_pull({100, 101, 102})   # pull saw 102, WS never delivered it
    assert dropped == [102] and sup.verifier.divergences == 1


def test_prewarmed_exit_is_fresh_then_stale():
    pw = PrewarmedExits(max_age_s=5.0)
    pw.refresh(("solana", "POOL"), {"expected_usdc": 42.0}, now_ts=1000.0)
    assert pw.get_fresh(("solana", "POOL"), 1004.0)["expected_usdc"] == 42.0   # fresh
    assert pw.get_fresh(("solana", "POOL"), 1006.0) is None                    # aged out -> re-quote
    assert pw.get_fresh(("solana", "OTHER"), 1000.0) is None                   # never warmed


def test_latency_measured_from_notification():
    clock = _Clock(1000.5)
    sup = HotSupervisor(_FakeEngines({("solana", "POOL")}), now_fn=clock)
    sup.on_ws_row(_row(notified=1000.1))             # 400ms notification->decision
    assert abs(sup.decision_latency_p50_ms() - 400.0) < 1.0


def test_notification_time_is_stamped_at_receipt_not_fabricated_from_block_time():
    clock = _Clock(1000.5)
    sup = HotSupervisor(_FakeEngines({("solana", "POOL")}), now_fn=clock)
    sup.on_ws_row(_row(notified=None))
    assert sup.decision_latency_p50_ms() == 0.0


def test_fallback_is_executed_not_only_reported_due():
    clock = _Clock(1000.0)
    calls = []
    sup = HotSupervisor(_FakeEngines({("solana", "POOL")}), now_fn=clock)
    assert sup.run_pull_fallback(lambda: calls.append(1) or ["held"], now_ts=clock.t) == ["held"]
    assert calls == [1] and sup.gap.needs_pull_fallback(clock.t) is False
    assert sup.run_pull_fallback(lambda: calls.append(2), now_ts=clock.t + 1) is None
    assert calls == [1]


def test_cut_callback_receives_fresh_prewarmed_preview():
    clock = _Clock(1000.0)
    seen = []
    sup = HotSupervisor(_FakeEngines({("solana", "POOL")}), now_fn=clock,
                        on_decision=lambda key, decision, row, preview: seen.append(preview))
    sup.prewarm.refresh(("solana", "POOL"), {"expected_usdc": 42}, now_ts=clock.t)
    sup.on_ws_row(_row())
    assert seen == [{"expected_usdc": 42}]


def test_gap_tracker_never_decided_needs_pull():
    g = DecisionGapTracker(60.0)
    assert g.needs_pull_fallback(0.0) is True and g.gap_s(0.0) is None


def test_hot_book_uses_persisted_two_vault_identity_and_warms_history(monkeypatch):
    import sqlite3
    from datetime import datetime, timezone
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    from sniper.services import memecoin_solana_tape as st
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); t.ensure_block_times(c); ensure_feature_tables(c); st.ensure_sol_cursor(c)
    c.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at) "
              "VALUES('solana','CV','MINT','Q',1,'t')")
    c.execute("INSERT INTO sol_pool_vaults (pool,mint,coin_vault,quote_vault,quote_mint,resolved_at) VALUES('CV','MINT','CV','QV','Q','t')")
    c.execute("INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before) "
              "VALUES('CV','s','t',NULL)")
    base = 1_800_000_000
    for n in range(1, 7):
        iso = datetime.fromtimestamp(base + n, tz=timezone.utc).isoformat()
        t.insert_ticks(c, [{"chain_id": "solana", "pool": "CV", "kind": "sol_swap",
                            "block_no": n, "tx_sig": f"s{n}", "log_index": 0, "maker": None,
                            "amount0": "-100", "amount1": "200", "observed_at": iso}])
        t.backfill_block_times(c, "solana", [n], lambda _b, n=n: base + n, iso)
    policy = type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano",
                             "decision_engine_version": "v5"})()
    monkeypatch.setattr("sniper.services.memecoin_funded_policy.frozen_policy_from_position",
                        lambda _pos: policy)
    engines, vaults, entries = build_hot_book(
        [{"id": 7, "network_id": "solana", "mint": "MINT", "derisked_at": None,
          "opened_at": datetime.fromtimestamp(base - 100, tz=timezone.utc).isoformat()}], c,
        now_fn=lambda: base + 7)
    assert engines.keys() == {("solana", "CV")}
    assert set(vaults) == {"CV", "QV"}
    assert entries[("solana", "CV")]["engine"].ticks_seen == 6
    # WO-2 wiring: the book carries per-pool vault identity so decode corroboration can resolve it.
    assert entries[("solana", "CV")]["coin_vault"] == "CV"
    assert entries[("solana", "CV")]["coin_mint"] == "MINT"
    # Swap history without a reserve sync is not enough to authorize a hot-lane action. WO-1: the
    # state is no longer collapsed — depth is simply untrusted, so the depth-gated LP CUT can't fire.
    hot = entries[("solana", "CV")]["engine"]
    assert all(e.get("tx_sig") for e in hot._events)  # pull-decoded history stays authoritative
    assert hot.contract["held_s"] == 107
    assert hot.last_state.candidate_capable(("depth",)) is False
    assert hot.last_state.provenance["depth"].quality == "untrusted"
    assert hot.last_decision.action == "DEFER_TO_LEGACY"

    # Completeness can regress after the 30s book build. The per-event decision must re-read the
    # frontier and fail closed immediately, not keep acting on the old complete snapshot.
    c.execute("UPDATE sol_sig_cursor SET frontier_before='enum:older' WHERE vault='CV'")
    c.commit()
    engines.on_tick(("solana", "CV"), {
        "kind": "sol_sync", "block_ts": base + 8, "amount0": "1000000", "amount1": "2000000",
    }, now_ts=base + 8)
    # Frontier regressed -> tape incomplete -> EVERY family untrusted; no rule can act.
    assert all(p.quality == "untrusted" for p in hot.last_state.provenance.values())
    assert hot.last_state.candidate_capable(("depth",)) is False
    assert hot.last_decision.action == "DEFER_TO_LEGACY"


def test_hot_book_uses_position_pool_binding_when_a_mint_has_multiple_venues(monkeypatch):
    import sqlite3
    from datetime import datetime, timezone
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    from sniper.services import memecoin_solana_tape as st
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); t.ensure_block_times(c); ensure_feature_tables(c); st.ensure_sol_cursor(c)
    for pool in ("AAA-STALE", "ZZZ-CURRENT"):
        c.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at) "
                  "VALUES('solana',?,'MINT','Q',1,'t')", (pool,))
        c.execute("INSERT INTO sol_pool_vaults (pool,mint,coin_vault,quote_vault,quote_mint,resolved_at) VALUES(?, 'MINT', ?, ?, 'Q', 't')",
                  (pool, pool, f"{pool}-Q"))
        c.execute("INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before) "
                  "VALUES(?,'s','t',NULL)", (pool,))
    c.execute("INSERT INTO sol_position_pools "
              "VALUES(7,'MINT','ZZZ-CURRENT','amm','entry-sig','t')")
    base = 1_800_000_000
    iso = datetime.fromtimestamp(base, tz=timezone.utc).isoformat()
    t.insert_ticks(c, [{"chain_id": "solana", "pool": "ZZZ-CURRENT", "kind": "sol_swap",
                        "block_no": 1, "tx_sig": "s1", "log_index": 0, "maker": None,
                        "amount0": "-100", "amount1": "200", "observed_at": iso}])
    t.backfill_block_times(c, "solana", [1], lambda _b: base, iso)
    policy = type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano",
                             "decision_engine_version": "v5"})()
    monkeypatch.setattr("sniper.services.memecoin_funded_policy.frozen_policy_from_position",
                        lambda _pos: policy)

    engines, vaults, _entries = build_hot_book(
        [{"id": 7, "network_id": "solana", "mint": "MINT", "derisked_at": None,
          "opened_at": datetime.fromtimestamp(base - 10, tz=timezone.utc).isoformat()}], c,
        now_fn=lambda: base + 1)

    assert engines.keys() == {("solana", "ZZZ-CURRENT")}
    assert set(vaults) == {"ZZZ-CURRENT", "ZZZ-CURRENT-Q"}

def test_hot_lane_setting_is_default_off():
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_HOT_EXIT_ENABLED is False


def test_hot_book_selects_v5_for_history_and_future_ticks(monkeypatch):
    import sqlite3
    from datetime import datetime, timezone
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    from sniper.services import memecoin_solana_tape as st
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); t.ensure_block_times(c); ensure_feature_tables(c); st.ensure_sol_cursor(c)
    c.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at) "
              "VALUES('solana','CV','MINT','Q',1,'t')")
    c.execute("INSERT INTO sol_pool_vaults (pool,mint,coin_vault,quote_vault,quote_mint,resolved_at) VALUES('CV','MINT','CV','QV','Q','t')")
    c.execute("INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before) "
              "VALUES('CV','s','t',NULL)")
    base = 1_800_000_000
    for n in range(1, 8):
        iso = datetime.fromtimestamp(base + n, tz=timezone.utc).isoformat()
        kind = "sol_sync" if n == 1 else "sol_swap"
        t.insert_ticks(c, [{"chain_id": "solana", "pool": "CV", "kind": kind,
                            "block_no": n, "tx_sig": f"s{n}", "log_index": 0, "maker": None,
                            "amount0": "-100", "amount1": "200", "observed_at": iso}])
        t.backfill_block_times(c, "solana", [n], lambda _b, n=n: base + n, iso)
    policy = type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano",
                             "decision_engine_version": "v5"})()
    monkeypatch.setattr("sniper.services.memecoin_funded_policy.frozen_policy_from_position",
                        lambda _pos: policy)
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_EXIT_ENGINE_VERSION", "v3")
    engines, _vaults, entries = build_hot_book(
        [{"id": 7, "network_id": "solana", "mint": "MINT", "derisked_at": None,
          "opened_at": datetime.fromtimestamp(base - 2000, tz=timezone.utc).isoformat()}], c,
        now_fn=lambda: base + 7)
    hot = entries[("solana", "CV")]["engine"]
    assert hot.engine_version == "exit-brain-v5"
    assert hot.contract["coin_band"] == "nano"
    # History warming used v5, but age alone has no authority before WO-5 supplies persisted
    # executable no-progress evidence.
    assert hot.last_decision.action == "DEFER_TO_LEGACY"
    assert engines.keys() == {("solana", "CV")}


def test_hot_book_warmup_does_not_block_decoded_burn(monkeypatch):
    """Four swap blocks remain WARMUP, but structure warmup is irrelevant to trusted LP depth."""
    import sqlite3
    from datetime import datetime, timezone
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    from sniper.services import memecoin_solana_tape as st
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); t.ensure_block_times(c); ensure_feature_tables(c); st.ensure_sol_cursor(c)
    c.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at) "
              "VALUES('solana','CV','MINT','Q',1,'t')")
    c.execute("INSERT INTO sol_pool_vaults (pool,mint,coin_vault,quote_vault,quote_mint,resolved_at) VALUES('CV','MINT','CV','QV','Q','t')")
    c.execute("INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before) "
              "VALUES('CV','s','t',NULL)")
    base = 1_800_000_000
    kinds = ("sol_swap", "sol_swap", "sol_swap", "sol_swap", "sol_sync", "sol_burn")
    for n, kind in enumerate(kinds, 1):
        iso = datetime.fromtimestamp(base + n, tz=timezone.utc).isoformat()
        t.insert_ticks(c, [{"chain_id": "solana", "pool": "CV", "kind": kind,
                            "block_no": n, "tx_sig": f"s{n}", "log_index": 0, "maker": None,
                            "amount0": "-100", "amount1": "200", "observed_at": iso}])
        t.backfill_block_times(c, "solana", [n], lambda _b, n=n: base + n, iso)
    policy = type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano",
                             "decision_engine_version": "v5"})()
    monkeypatch.setattr("sniper.services.memecoin_funded_policy.frozen_policy_from_position",
                        lambda _pos: policy)
    _engines, _vaults, entries = build_hot_book(
        [{"id": 7, "network_id": "solana", "mint": "MINT", "derisked_at": None,
          "opened_at": datetime.fromtimestamp(base - 100, tz=timezone.utc).isoformat()}], c,
        now_fn=lambda: base + 7)
    hot = entries[("solana", "CV")]["engine"]
    assert hot.last_state.state.value == "warmup"
    assert hot.last_decision.action == "CUT"


def test_hot_book_rejects_stale_history_like_pull_state(monkeypatch):
    import sqlite3
    from datetime import datetime, timezone
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    from sniper.services import memecoin_solana_tape as st
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); t.ensure_block_times(c); ensure_feature_tables(c); st.ensure_sol_cursor(c)
    c.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at) "
              "VALUES('solana','CV','MINT','Q',1,'t')")
    c.execute("INSERT INTO sol_pool_vaults (pool,mint,coin_vault,quote_vault,quote_mint,resolved_at) VALUES('CV','MINT','CV','QV','Q','t')")
    c.execute("INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before) "
              "VALUES('CV','s','t',NULL)")
    base = 1_800_000_000
    for n in range(1, 7):
        kind = "sol_sync" if n == 1 else "sol_swap"
        iso = datetime.fromtimestamp(base + n, tz=timezone.utc).isoformat()
        t.insert_ticks(c, [{"chain_id": "solana", "pool": "CV", "kind": kind,
                            "block_no": n, "tx_sig": f"s{n}", "log_index": 0, "maker": None,
                            "amount0": "-100", "amount1": "200", "observed_at": iso}])
        t.backfill_block_times(c, "solana", [n], lambda _b, n=n: base + n, iso)
    policy = type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano",
                             "decision_engine_version": "v5"})()
    monkeypatch.setattr("sniper.services.memecoin_funded_policy.frozen_policy_from_position",
                        lambda _pos: policy)
    _engines, _vaults, entries = build_hot_book(
        [{"id": 7, "network_id": "solana", "mint": "MINT", "derisked_at": None,
          "opened_at": datetime.fromtimestamp(base - 100, tz=timezone.utc).isoformat()}], c,
        now_fn=lambda: base + 500)
    hot = entries[("solana", "CV")]["engine"]
    assert hot.last_state.state.value == "data_stale"
    assert hot.last_decision.action == "DEFER_TO_LEGACY"


def test_hot_book_rejects_stale_swap_family_and_future_events(monkeypatch):
    """A fresh Sync cannot launder stale price/flow/vol, and future event time is corrupt."""
    import sqlite3
    from datetime import datetime, timezone
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    from sniper.services import memecoin_solana_tape as st
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); t.ensure_block_times(c); ensure_feature_tables(c); st.ensure_sol_cursor(c)
    c.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at) "
              "VALUES('solana','CV','MINT','Q',1,'t')")
    c.execute("INSERT INTO sol_pool_vaults (pool,mint,coin_vault,quote_vault,quote_mint,resolved_at) VALUES('CV','MINT','CV','QV','Q','t')")
    c.execute("INSERT INTO sol_sig_cursor(vault,last_sig,updated_at,frontier_before) "
              "VALUES('CV','s','t',NULL)")
    base = 1_800_000_000
    for n in range(1, 6):
        iso = datetime.fromtimestamp(base + n, tz=timezone.utc).isoformat()
        t.insert_ticks(c, [{"chain_id": "solana", "pool": "CV", "kind": "sol_swap",
                            "block_no": n, "tx_sig": f"s{n}", "log_index": 0, "maker": None,
                            "amount0": "-100", "amount1": "200", "observed_at": iso}])
        t.backfill_block_times(c, "solana", [n], lambda _b, n=n: base + n, iso)
    iso = datetime.fromtimestamp(base + 500, tz=timezone.utc).isoformat()
    t.insert_ticks(c, [{"chain_id": "solana", "pool": "CV", "kind": "sol_sync",
                        "block_no": 6, "tx_sig": "s6", "log_index": 0, "maker": None,
                        "amount0": "1000000", "amount1": "2000000", "observed_at": iso}])
    t.backfill_block_times(c, "solana", [6], lambda _b: base + 500, iso)
    policy = type("P", (), {"policy_id": ENGINE_ENVELOPE_POLICY_ID, "coin_band": "nano",
                             "decision_engine_version": "v5"})()
    monkeypatch.setattr("sniper.services.memecoin_funded_policy.frozen_policy_from_position",
                        lambda _pos: policy)
    engines, _vaults, entries = build_hot_book(
        [{"id": 7, "network_id": "solana", "mint": "MINT", "derisked_at": None,
          # young position: isolates the stale-swap-family check from the age-based time-DERISK
          # (which correctly needs no market family and is covered by the engine unit tests).
          "opened_at": datetime.fromtimestamp(base + 400, tz=timezone.utc).isoformat()}], c,
        now_fn=lambda: base + 500)
    hot = entries[("solana", "CV")]["engine"]
    # WO-1: a fresh Sync no longer launders stale swaps by collapsing the state — the swap-derived
    # families stay untrusted, so flow-/price-/vol-gated rules can't fire and the engine defers.
    assert hot.last_state.candidate_capable(("flow",)) is False
    assert hot.last_state.provenance["flow"].quality == "untrusted"
    assert hot.last_decision.action == "DEFER_TO_LEGACY"

    engines.on_tick(("solana", "CV"), {
        "kind": "sol_swap", "block_ts": base + 501, "amount0": "-1", "amount1": "2",
    }, now_ts=base + 500)
    # A future-dated event (block_ts > now) is corrupt time -> fail closed to DATA_INSUFFICIENT.
    assert hot.last_state.state.value == "data_insufficient"
    assert hot.last_decision.action == "DEFER_TO_LEGACY"


def test_hot_supervisor_passes_funded_cfg_to_unsellable_cut():
    """Catches the fast lane dropping cfg and silently disabling the exact-quote-miss CUT."""
    from sniper.analysis.exit_state_types import MarketState, Provenance, StateLabel
    from sniper.services.exit_engine_hot import HotEngineSet, HotPositionEngine

    families = ("price", "flow", "vol", "depth")
    provenance = {f: Provenance("pool_ticks", quality="trusted") for f in families}

    def state_fn(_features, *, now_ts):
        return MarketState(
            chain_id="solana", mint="M", pool="POOL", position_id=1,
            engine_version="market-state-v2", feature_version="v2",
            state=StateLabel.TRENDING, required_families=families, provenance=provenance)

    class DecisionCfg:
        MEMECOIN_REAL_UNSELLABLE_SWEEPS = 4

    engines = HotEngineSet()
    engines.track(("solana", "POOL"), HotPositionEngine(
        1, {"trail_pct": 30.0, "exact_quote_misses": 4}, quote_side=0,
        state_fn=state_fn))
    supervisor = HotSupervisor(engines, now_fn=_Clock(), decision_cfg=DecisionCfg())
    decision = supervisor.on_ws_row(_row())
    assert decision.action == "CUT" and "unsellable" in decision.reason_codes
