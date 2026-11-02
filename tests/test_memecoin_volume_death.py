"""The volume-death shadow measures a rule that is not armed. It must never act, and never lie.

Two ways it could lie, both from real incidents:
  - reading a BATCH timestamp as event time (pool_ticks.observed_at: 3.8% distinct, nine exactly
    300.000s "silences" that were the poller's own fetch interval)
  - counting OUR outage as the market's quiet (2026-08-24: 926s with no marks at all)
"""
from __future__ import annotations

import ast
import json
import pathlib
import sqlite3

import pytest

from _source_guard import code_of
from sniper.services import memecoin_volume_death as vd
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)

NOW = "2026-08-26T12:00:00+00:00"
OPENED = "2026-08-26T11:55:00+00:00"
LOOP = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py"
RUNNER = (pathlib.Path(__file__).resolve().parents[1] / "sniper" / "services"
          / "memecoin_real_runner.py")


@pytest.fixture(autouse=True)
def _clean():
    vd._RECORDED.clear()
    yield
    vd._RECORDED.clear()


def _conn(marks=()):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_position_marks "
              "(chain_id TEXT, mint TEXT, sweep_ts TEXT, source TEXT, mid REAL)")
    for row in marks:
        mint, ts, source = row[0], row[1], row[2]
        chain = row[3] if len(row) > 3 else "solana"
        mid = row[4] if len(row) > 4 else 1.0
        c.execute("INSERT INTO memecoin_position_marks VALUES (?,?,?,?,?)",
                  (chain, mint, ts, source, mid))
    return c


def _pos(pid=1, mint="MINT", network_id="solana"):
    return {"id": pid, "mint": mint, "opened_at": OPENED, "network_id": network_id}


def _swap(mint, ts, chain="solana"):
    return (mint, ts, vd.SWAP_SOURCE, chain)


def _state(mint, ts, mid, chain="solana"):
    """A pool-reserve mark. The MID is the payload — the witness reads value, never count."""
    return (mint, ts, vd.STATE_SOURCE, chain, mid)


# ------------------------------------------------------------------ the instrument

def test_only_swap_evidence_counts_as_a_trade():
    """A polled mid is not a trade. Its gaps measure our polling cadence."""
    c = _conn([("MINT", "2026-08-26T11:59:55+00:00", "dexscreener"),
               ("MINT", "2026-08-26T11:59:55+00:00", "geckoterminal"),
               _swap("MINT", "2026-08-26T11:59:00+00:00")])
    assert vd.newest_swap_age_s(c, mint="MINT", since_iso=OPENED, now_iso=NOW, chain_id="solana") == pytest.approx(60.0)


def test_a_swap_from_a_previous_hold_cannot_make_this_one_look_alive():
    c = _conn([_swap("MINT", "2026-08-26T11:54:00+00:00")])     # before the open
    assert vd.newest_swap_age_s(c, mint="MINT", since_iso=OPENED, now_iso=NOW, chain_id="solana") is None


def test_a_batch_stamped_tape_fixture_produces_no_would_fire():
    """THE 300s ARTIFACT. Rows sharing one poller timestamp are not evidence of anything, and the
    evaluator must not source silence from them — only fastlane_sol_swap is per-event."""
    batch = "2026-08-26T11:55:00+00:00"
    c = _conn([("MINT", batch, "poll_batch"), ("OTHER", batch, "poll_batch"),
               ("THIRD", batch, "poll_batch")])
    # no swap-source rows at all -> stream reads unhealthy, nothing fires
    assert vd.stream_healthy(c, now_iso=NOW, chain_id="solana") is False
    rows, tally = vd.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                              has_loss_rail_fn=lambda _p: True)
    assert tally["blind_frozen"] == 1
    assert all(not r["thresholds"] for r in rows), "a batch stamp must never produce a would-fire"


# ------------------------------------------------------------------ quiet vs blind

def test_a_dead_stream_freezes_the_clock_rather_than_firing_on_everything():
    """THE 2026-08-24 CASE. 926s with no marks at all. A naive rule fires on every position at
    once, with total confidence, having learned nothing."""
    c = _conn([_swap("OTHER", "2026-08-24T11:38:34+00:00")])     # last swap, long ago
    now = "2026-08-24T11:54:00+00:00"
    rows, tally = vd.evaluate(c, now_iso=now,
                              open_positions=[_pos(1, "A"), _pos(2, "B"), _pos(3, "C")],
                              has_loss_rail_fn=lambda _p: True)
    assert tally["blind_frozen"] == 3 and tally["quiet"] == 0
    assert all(r["blind_frozen"] and r["thresholds"] == [] for r in rows)
    assert all(r["silence_s"] is None for r in rows), "unmeasurable, not zero and not huge"


def test_silence_counts_when_other_coins_are_still_trading():
    """The discriminator: swaps arriving elsewhere prove the stream is alive, so quiet on THIS coin
    is a real observation about this coin."""
    c = _conn([_swap("OTHER", "2026-08-26T11:59:50+00:00"),
               _swap("MINT", "2026-08-26T11:59:35+00:00")])
    rows, tally = vd.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                              has_loss_rail_fn=lambda _p: True)
    assert tally["quiet"] == 1 and tally["blind_frozen"] == 0
    assert rows[0]["silence_s"] == pytest.approx(25.0)
    assert rows[0]["thresholds"] == [10, 15, 20]


def test_a_coin_never_swapped_in_this_hold_is_unobserved_not_silent():
    """31% of funded holds have no swap evidence. That is missing coverage, not a quiet market."""
    c = _conn([_swap("OTHER", "2026-08-26T11:59:50+00:00")])
    rows, tally = vd.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                              has_loss_rail_fn=lambda _p: True, hold_age_fn=lambda _p: 300.0)
    assert tally["no_swap_ever"] == 1 and tally["quiet"] == 0
    assert rows[0]["thresholds"] == [], "an unobserved coin must never produce a would-fire"


# ------------------------------------------------------------------ thresholds

def test_every_threshold_lands_in_ONE_row():
    """So the cutoff is chosen from one live dataset, not five reruns of the same window."""
    assert vd.crossed(4.0) == []
    assert vd.crossed(21.0) == [10, 15, 20]
    assert vd.crossed(75.0) == [10, 15, 20, 30, 60]


def test_a_quiet_position_records_once_per_escalation_not_once_per_pass():
    c = _conn([_swap("OTHER", "2026-08-26T11:59:50+00:00")])
    written = []
    rows = [{"position_id": 1, "mint": "M", "silence_s": 21.0, "thresholds": [10, 15, 20],
             "blind_frozen": False, "stream_healthy": True}]
    assert vd.record_new_escalations(c, rows, now_iso=NOW,
                                     record_fn=lambda r, n: written.append(r)) == 1
    assert vd.record_new_escalations(c, rows, now_iso=NOW,
                                     record_fn=lambda r, n: written.append(r)) == 0
    rows[0]["thresholds"] = [10, 15, 20, 30]          # escalated
    assert vd.record_new_escalations(c, rows, now_iso=NOW,
                                     record_fn=lambda r, n: written.append(r)) == 1
    assert len(written) == 2


def test_a_closed_position_clears_its_state_so_a_later_quiet_spell_is_new():
    c = _conn()
    rows = [{"position_id": 1, "mint": "M", "silence_s": 21.0, "thresholds": [10, 15, 20],
             "blind_frozen": False, "stream_healthy": True}]
    vd.record_new_escalations(c, rows, now_iso=NOW, record_fn=lambda r, n: None)
    vd.record_new_escalations(c, [], now_iso=NOW, record_fn=lambda r, n: None)      # gone
    assert vd.record_new_escalations(c, rows, now_iso=NOW, record_fn=lambda r, n: None) == 1


# ------------------------------------------------------------------ scope and safety

def test_a_position_with_no_loss_rail_is_out_of_scope():
    c = _conn([_swap("OTHER", "2026-08-26T11:59:50+00:00")])
    rows, tally = vd.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                              has_loss_rail_fn=lambda _p: False)
    assert rows == [] and tally["checked"] == 0


def test_a_flat_book_evaluates_nothing():
    rows, tally = vd.evaluate(_conn(), now_iso=NOW, open_positions=[],
                              has_loss_rail_fn=lambda _p: True)
    assert rows == [] and tally["checked"] == 0


def test_evaluate_never_raises_into_the_loop():
    """The property is that the maintenance pass survives, and that a row which could not be read
    produces NO observation. `checked` counting the attempt is honest — it was attempted."""
    class Broken(dict):
        def get(self, *_a, **_k):
            raise sqlite3.OperationalError("boom")
    rows, tally = vd.evaluate(_conn(), now_iso=NOW, open_positions=[Broken()],
                              has_loss_rail_fn=lambda _p: True)
    assert rows == [], "a position that cannot be read must not become an observation"
    assert tally["quiet"] == 0 and tally["no_swap_ever"] == 0


def test_a_recording_failure_does_not_stop_the_others():
    c = _conn()
    rows = [{"position_id": 1, "mint": "A", "silence_s": 21.0, "thresholds": [20],
             "blind_frozen": False, "stream_healthy": True},
            {"position_id": 2, "mint": "B", "silence_s": 21.0, "thresholds": [20],
             "blind_frozen": False, "stream_healthy": True}]
    def flaky(row, _now):
        if row["position_id"] == 1:
            raise RuntimeError("db hiccup")
    assert vd.record_new_escalations(c, rows, now_iso=NOW, record_fn=flaky) == 1


# ------------------------------------------------------------------ isolation

def test_the_service_can_exit_nothing():
    src = pathlib.Path(vd.__file__).read_text()
    for forbidden in ("exit_fn", "execute_fn", "derisk_fn", "release_pending", "reserve_entry",
                      "UPDATE memecoin_positions", "wallet_exec_lock", "dexscreener"):
        assert forbidden not in src, f"a shadow evaluator must not reference {forbidden}"


def test_the_wiring_arms_nothing():
    body = LOOP.read_text()
    start = body.index("# VOLUME-DEATH SHADOW")
    block = body[start:body.index("[err] volume-death shadow failed", start)]
    for forbidden in ("exit_fn", "real_exit", "derisk", "wallet_exec_lock"):
        assert forbidden not in block, f"the shadow wiring must not touch {forbidden}"
    assert "record_volume_death_shadow" in block


# ------------------------------------------------------------------ the denominator

def test_every_window_resolves_to_one_of_three_states():
    """Without this the would-fire RATE reads biased low exactly in the blind case, because
    unobserved windows silently leave the denominator."""
    # 1. observed + quiet enough to fire
    c = _conn([_swap("OTHER", "2026-08-26T11:59:50+00:00"),
               _swap("MINT", "2026-08-26T11:59:35+00:00")])
    rows, _t = vd.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                           has_loss_rail_fn=lambda _p: True)
    assert rows[0]["observable"] is True and rows[0]["thresholds"]

    # 2. observed by a POLLED source but never swapped — watched, and genuinely no trades
    c2 = _conn([_swap("OTHER", "2026-08-26T11:59:50+00:00"),
                ("MINT", "2026-08-26T11:58:00+00:00", "dexscreener")])
    rows2, t2 = vd.evaluate(c2, now_iso=NOW, open_positions=[_pos()],
                            has_loss_rail_fn=lambda _p: True, hold_age_fn=lambda _p: 300.0)
    assert t2["no_swap_ever"] == 1
    assert rows2[0]["observable"] is True, "a polled mark proves something was watching"

    # 3. NOT OBSERVABLE — nothing was watching this coin at all
    c3 = _conn([_swap("OTHER", "2026-08-26T11:59:50+00:00")])
    rows3, _t3 = vd.evaluate(c3, now_iso=NOW, open_positions=[_pos()],
                             has_loss_rail_fn=lambda _p: True, hold_age_fn=lambda _p: 300.0)
    assert rows3[0]["observable"] is False, "no mark of any kind = no information either way"


def test_a_blind_frozen_window_is_never_marked_observable():
    c = _conn([_swap("OTHER", "2026-08-24T11:38:34+00:00")])
    rows, _t = vd.evaluate(c, now_iso="2026-08-24T11:54:00+00:00",
                           open_positions=[_pos()], has_loss_rail_fn=lambda _p: True)
    assert rows[0]["blind_frozen"] is True and rows[0]["observable"] is False


# ------------------------------------------------------ the ARMED rail (2026-08-26)

ARM_CUT = "2026-08-26T14:00:00+00:00"
ARM_OPEN = "2026-08-26T14:10:00+00:00"      # in-cohort open
ARM_NOW = "2026-08-26T14:15:00+00:00"       # held 300s


def _armed(conn, pos, **kw):
    args = dict(now_iso=ARM_NOW, held_s=300.0, exit_s=60.0,
                min_opened_at=ARM_CUT, require_onchain=False)
    args.update(kw)
    return vd.armed_exit_decision(conn, pos, **args)


def _arm_pos(mint="MINT", opened=ARM_OPEN, network_id="solana"):
    return {"id": 9, "mint": mint, "opened_at": opened, "network_id": network_id}


def test_armed_fires_on_measured_silence_past_the_threshold():
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),      # last swap 180s ago
               _swap("OTHER", "2026-08-26T14:14:50+00:00")])    # stream healthy
    fire, silence, why = _armed(c, _arm_pos())
    # UPDATED 2026-08-29. This coin has swap coverage and 180s of measured silence, but the reserve
    # lane never watched it, so there is no second witness and the rail FREEZES. It used to fire
    # here on swap silence alone; the operator reported that behaviour as broken and all 24 armed
    # fires took this path. The silence is still measured and reported — only the verdict changed.
    # REFINED 2026-09-02, WITHDRAWN 2026-09-22. The 09-02 label called this the pool-CANNOT-answer
    # case. A swap mark replaces a state mark in the mark cache, so "never emitted" measures the
    # sample. The verdict never changed; the label is one word again.
    assert fire is False and why == "state_uncovered_frozen"
    assert silence == pytest.approx(180.0, abs=1.0)


def test_armed_holds_below_the_threshold():
    c = _conn([_swap("MINT", "2026-08-26T14:14:30+00:00"),      # 30s ago
               _swap("OTHER", "2026-08-26T14:14:50+00:00")])
    fire, silence, _ = _armed(c, _arm_pos())
    assert fire is False and silence == pytest.approx(30.0, abs=1.0)


def test_armed_FREEZES_on_a_blind_stream_rather_than_firing():
    """A dead stream is unbounded silence about US, not about the coin. Selling on our own
    blindness would exit healthy coins on every websocket hiccup."""
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00")])      # nothing anywhere in 60s window
    fire, silence, why = _armed(c, _arm_pos())
    assert fire is False and why == "blind_frozen" and silence is None


def test_armed_pre_cutover_positions_are_untouched():
    """Cohort discipline: existing positions keep their frozen contract."""
    c = _conn([_swap("OTHER", "2026-08-26T14:14:50+00:00")])
    fire, _s, why = _armed(c, _arm_pos(opened="2026-08-26T13:00:00+00:00"))
    assert fire is False and why == "cohort_excluded"


def test_armed_no_cutoff_configured_is_inert():
    c = _conn([_swap("OTHER", "2026-08-26T14:14:50+00:00")])
    fire, _s, why = _armed(c, _arm_pos(), min_opened_at="")
    assert fire is False and why == "cohort_excluded"


def test_armed_a_coin_the_swap_lane_never_watched_is_not_silent_it_is_UNOBSERVED():
    """REVERSED 2026-08-29, and this is the defect the operator reported.

    Zero swaps since entry used to mean "silent for the whole hold". But the swap lane has never
    recorded ANY swap for this mint, so we are not watching this pool — and `silence = held_s`
    turned that unknown into a measured silence equal to the position's whole life. On 16 of the 24
    armed fires that produced a close at 60-61s at 1.00x: a time-stop, not a liquidity signal.
    """
    c = _conn([_swap("OTHER", "2026-08-26T14:14:50+00:00")])
    fire, silence, why = _armed(c, _arm_pos())
    assert fire is False and why == "swap_lane_uncovered"
    assert silence is None, "an unobserved pool has no silence to report"


def test_armed_nonpositive_or_broken_exit_s_disarms():
    c = _conn([_swap("OTHER", "2026-08-26T14:14:50+00:00")])
    assert _armed(c, _arm_pos(), exit_s=0)[2] == "disarmed"
    assert _armed(c, _arm_pos(), exit_s="x")[2] == "misconfigured"


def test_armed_wiring_sits_below_the_price_rails_and_above_the_engine():
    """vol_death may only claim a sweep the envelope left unclaimed, and must outrank the engine."""
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "sniper" / "services" / "memecoin_real_runner.py").read_text()
    i_reason = src.index("reason = alpha_b_real_exit_reason(")
    i_vd = src.index('reason = "vol_death"')
    i_engine = src.index("if reason is None and pos[\"id\"] in engine_ids:")
    assert i_reason < i_vd < i_engine
    assert "MEMECOIN_VOLDEATH_ARMED" in src[i_reason:i_vd]


def test_armed_vol_death_passes_executable_confirmation_like_timecap():
    from sniper.services.memecoin_real_runner import _EXECUTABLE_PRICE_REASONS
    assert "vol_death" not in _EXECUTABLE_PRICE_REASONS
    assert "b_timecap" not in _EXECUTABLE_PRICE_REASONS


# --------------------------------------------- chain scoping (Beta pre-deploy review, 2026-08-26)

def test_armed_a_4663_position_cannot_borrow_solanas_health():
    """REGRESSION, executed proof from review: SWAP_SOURCE rows exist for solana ONLY. Unscoped,
    a 4663 position read Solana's stream as its own health, found no swaps for its mint, fell back
    to silence-since-entry, and fired at exit_s on a coin with 40 fresh marks and price up 40%.
    Chain-scoped, an uncovered chain is BLIND -> frozen, and the rail self-arms per chain as swap
    coverage appears."""
    c = _conn([_swap("SOLCOIN", "2026-08-26T14:14:50+00:00", chain="solana")])
    fire, silence, why = _armed(c, _arm_pos(mint="0xEvmCoin", network_id="4663"))
    assert fire is False and why == "blind_frozen" and silence is None


def test_armed_4663_uses_confirmed_robinhood_pool_silence():
    state = vd.RobinhoodVolumeState(health_window_s=5.0)
    state.replace_bindings(
        {"0x" + "ab" * 32: "0xEvmCoin"}, now_ts=100.0, covered=True)
    state.record_head(1000, now_ts=100.0)
    state.record_head(1300, now_ts=130.0)
    pos = _arm_pos(
        mint="0xEvmCoin", network_id="4663",
        opened="1970-01-01T00:01:40+00:00")

    fire, silence, why = _armed(
        _conn([]), pos, now_iso="1970-01-01T00:02:10+00:00", held_s=30.0,
        exit_s=30.0, min_opened_at="1970-01-01T00:00:00+00:00",
        robinhood_state=state,
        robinhood_confirm_fn=lambda _pool, _first, _last: [],
    )

    assert fire is True
    assert silence == pytest.approx(30.0)
    assert why == "measured_state_still"


def test_robinhood_activity_resets_the_silence_clock():
    pool = "0x" + "ab" * 32
    state = vd.RobinhoodVolumeState(health_window_s=5.0)
    state.replace_bindings({pool: "0xEvmCoin"}, now_ts=100.0, covered=True)
    state.record_head(1000, now_ts=100.0)
    state.record_pool_activity(pool, now_ts=125.0)
    state.record_head(1300, now_ts=130.0)

    fire, silence, why = state.decision(
        {"mint": "0xEvmCoin", "opened_at": "1970-01-01T00:01:40+00:00"},
        now_iso="1970-01-01T00:02:10+00:00", exit_s=30.0,
        confirm_fn=lambda _pool, _first, _last: [],
    )

    assert fire is False
    assert silence == pytest.approx(5.0)
    assert why == "measured"


def test_head_age_s_is_none_before_any_head_and_measures_after():
    """The reconnect loop's staleness check reads this — before it existed, a stalled-but-open
    subscription raised nothing and nothing else here would ever notice."""
    state = vd.RobinhoodVolumeState(health_window_s=5.0)
    assert state.head_age_s(100.0) is None
    state.record_head(1, now_ts=90.0)
    assert state.head_age_s(100.0) == pytest.approx(10.0)


def test_coverage_snapshot_reports_bound_covered_pools_and_head_age():
    pool = "0x" + "ab" * 32
    state = vd.RobinhoodVolumeState(health_window_s=5.0)
    state.replace_bindings({pool: "0xEvmCoin"}, now_ts=100.0, covered=True)
    state.record_head(5, now_ts=110.0)

    assert state.coverage_snapshot(120.0) == {
        "pools_bound": 1, "pools_covered": 1,
        "head_age_s": pytest.approx(10.0), "heads_seen": 1,
    }


def test_robinhood_http_witness_blocks_a_missed_ws_event():
    pool = "0x" + "ab" * 32
    state = vd.RobinhoodVolumeState(health_window_s=5.0)
    state.replace_bindings({pool: "0xEvmCoin"}, now_ts=100.0, covered=True)
    state.record_head(1000, now_ts=100.0)
    state.record_head(1300, now_ts=130.0)

    result = state.decision(
        {"mint": "0xEvmCoin", "opened_at": "1970-01-01T00:01:40+00:00"},
        now_iso="1970-01-01T00:02:10+00:00", exit_s=30.0,
        confirm_fn=lambda _pool, _first, _last: [{"topics": ["swap"]}],
    )

    assert result == (False, 30.0, "state_moving")


def test_robinhood_pool_binding_uses_the_frozen_entry_pair():
    contract = {
        "classification": {"facts": {
            "liquidity_pair_address": "0x" + "ab" * 32,
        }}
    }
    pos = {"network_id": "4663", "mint": "0xTOKEN",
           "funded_contract_json": json.dumps(contract)}

    assert vd.robinhood_pool_bindings([pos]) == {
        "0x" + "ab" * 32: "0xtoken",
    }


def test_robinhood_pool_binding_falls_back_to_the_filled_route():
    pool = "0x" + "cd" * 32
    pos = {
        "network_id": "4663", "mint": "0xTOKEN",
        "funded_contract_json": json.dumps({"classification": {"facts": {}}}),
        "evm_route_json": json.dumps({"protocol": "uniswap_v4", "pool_id": pool}),
    }

    assert vd.robinhood_pool_bindings([pos]) == {pool: "0xtoken"}


def test_robinhood_v4_subscription_covers_swaps_and_liquidity_changes():
    pool = "0x" + "ab" * 32
    request = vd.robinhood_log_subscription(7, pool)

    assert request["method"] == "eth_subscribe"
    assert request["params"][0] == "logs"
    assert request["params"][1]["topics"][1] == pool
    assert len(request["params"][1]["topics"][0]) == 2


def test_robinhood_ws_delivery_updates_pool_activity():
    pool = "0x" + "ab" * 32
    state = vd.RobinhoodVolumeState(health_window_s=5.0)
    state.replace_bindings({pool: "0xEvmCoin"}, now_ts=100.0)
    pending = {2: pool}
    vd._handle_robinhood_message(
        state, {"jsonrpc": "2.0", "id": 2, "result": "sub"}, pending, now_ts=100.0)
    vd._handle_robinhood_message(
        state, {"params": {"result": {"number": "0x3e8"}}}, pending, now_ts=100.0)
    vd._handle_robinhood_message(
        state, {"params": {"result": {
            "address": vd._robinhood_log_filter(pool)["address"],
            "topics": [vd._robinhood_log_filter(pool)["topics"][0][0], pool],
        }}}, pending, now_ts=125.0)
    state.record_head(1300, now_ts=130.0)

    result = state.decision(
        {"mint": "0xEvmCoin", "opened_at": "1970-01-01T00:01:40+00:00"},
        now_iso="1970-01-01T00:02:10+00:00", exit_s=30.0,
        confirm_fn=lambda _pool, _first, _last: [],
    )
    assert result == (False, 5.0, "measured")


def test_robinhood_http_witness_uses_an_address_filter():
    pool = "0x" + "ab" * 32
    seen = {}

    def http(method, url, *, body):
        seen.update(method=method, url=url, body=json.loads(body))
        return 200, {"result": []}, None

    assert vd.robinhood_activity_logs(
        pool, 100, 200, rpc_url="https://official", http=http) == []
    flt = seen["body"]["params"][0]
    assert seen["url"] == "https://official"
    assert flt["address"]
    assert flt["fromBlock"] == "0x64"
    assert flt["toBlock"] == "0xc8"


def test_robinhood_http_witness_uses_the_verified_rpc_pool(monkeypatch):
    from sniper.execution import evm_wallet
    seen = {}

    def rpc(url, method, params, *, http=None):
        seen.update(url=url, method=method, params=params, http=http)
        return {"result": []}

    monkeypatch.setattr(evm_wallet, "_rpc", rpc)
    monkeypatch.setattr(
        evm_wallet, "_http",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("direct RPC read")),
    )
    pool = "0x" + "ab" * 32

    assert vd.robinhood_activity_logs(pool, 100, 200, rpc_url="https://official") == []
    assert seen["method"] == "eth_getLogs"
    assert seen["params"][0]["address"]
    assert seen["params"][0]["topics"][1] == pool


def test_runner_starts_the_robinhood_volume_stream():
    source = code_of(LOOP)
    assert "run_robinhood_volume_stream(" in source
    assert "MEMECOIN_ROBINHOOD_VOLUME_WS_URL" in source


def test_robinhood_stream_uses_supported_sync_connect_arguments():
    seen = {}
    pool = "0x" + "ab" * 32
    position = {
        "network_id": "4663", "mint": "0xTOKEN",
        "funded_contract_json": json.dumps({
            "classification": {"facts": {"liquidity_pair_address": pool}},
        }),
    }

    def connect(url, **kwargs):
        seen.update(url=url, kwargs=kwargs)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        vd.run_robinhood_volume_stream(
            ws_url="wss://node", bindings_fn=lambda: [position], now_fn=lambda: 100.0,
            connect_fn=connect, sleep_fn=lambda _seconds: None,
        )

    assert seen == {"url": "wss://node", "kwargs": {"open_timeout": 10}}


def test_a_stalled_newheads_subscription_forces_a_reconnect_without_any_exception():
    """A subscription that stays OPEN but stops delivering raises nothing — a real free-RPC
    failure mode. Before the staleness check, this loop would sit in `ws.recv` forever, and the
    armed 4663 rail would freeze on the stale read silently for the rest of the process's life."""
    pool = "0x" + "ab" * 32
    position = {
        "network_id": "4663", "mint": "0xTOKEN",
        "funded_contract_json": json.dumps({
            "classification": {"facts": {"liquidity_pair_address": pool}},
        }),
    }

    class _StalledWs:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def send(self, _payload):
            pass

        def recv(self, timeout=None):
            raise TimeoutError    # never delivers a single newHeads message

    connects = []

    def connect(url, **_kwargs):
        connects.append(url)
        return _StalledWs()

    ticks = iter(float(i) for i in range(10_000))

    class _Stop(Exception):
        pass

    sleeps = []

    def sleep_fn(seconds):
        sleeps.append(seconds)
        raise _Stop

    with pytest.raises(_Stop):
        vd.run_robinhood_volume_stream(
            ws_url="wss://node", bindings_fn=lambda: [position], now_fn=lambda: next(ticks),
            connect_fn=connect, sleep_fn=sleep_fn, stale_after_s=15.0,
        )

    assert len(connects) == 1     # broke out of ONE connection on staleness, not on an exception
    assert sleeps == [1.0]        # the reconnect backoff, not the exception-path backoff


def test_a_binding_refresh_failure_does_not_kill_the_stream_loop():
    """The one seam in this loop that used to sit outside every try/except. Its current caller
    (open_real_positions) already catches its own exceptions, so this is defense in depth, not a
    response to an observed failure."""
    calls = {"n": 0}

    def bindings_fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db is locked")
        return []

    class _Stop(Exception):
        pass

    sleeps = []

    def sleep_fn(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _Stop

    def connect(*_a, **_k):
        raise AssertionError("must never connect with no bindings")

    with pytest.raises(_Stop):
        vd.run_robinhood_volume_stream(
            ws_url="wss://node", bindings_fn=bindings_fn, now_fn=lambda: 100.0,
            connect_fn=connect, sleep_fn=sleep_fn,
        )

    assert calls["n"] == 2        # survived the raise and retried rather than dying
    assert sleeps == [1.0, 1.0]   # backoff after the failure, then the no-bindings idle sleep


def test_armed_an_unknown_chain_is_blind_not_dead():
    c = _conn([_swap("SOLCOIN", "2026-08-26T14:14:50+00:00")])
    fire, _s, why = _armed(c, _arm_pos(network_id=None))
    assert fire is False and why == "blind_frozen"


def test_armed_a_precomputed_stream_ok_skips_the_health_read():
    """The sweep memoizes health once per chain per sweep; the decision must honour it."""
    class NoHealthRead:
        def execute(self, sql, args=()):
            # Discriminate on the QUESTION, not the SQL shape. The swap-health read is the one
            # that asks about a chain with no mint and SWAP_SOURCE; the reserve-witness reads have
            # the same shape and are a different question, so a shape-matching guard would fire on
            # them and did.
            if vd.SWAP_SOURCE in tuple(args) and "mint=?" not in sql:
                raise AssertionError("swap health must not be re-read")
            class R:
                def fetchone(self):
                    return None
            return R()
    fire, _s, why = _armed(NoHealthRead(), _arm_pos(), stream_ok=True)
    # Every read on this stub answers empty, so the swap lane has never seen this pool and the
    # rail stops before the threshold. The SUBJECT of this test is that health was not re-read —
    # the stub raises if it is — and that still holds on the earlier exit.
    assert fire is False and why == "swap_lane_uncovered"
    fire, _s, why = _armed(NoHealthRead(), _arm_pos(), stream_ok=False)
    assert fire is False and why == "blind_frozen"


def test_shadow_evaluate_is_also_chain_scoped():
    """The same borrow existed in the shadow: a 4663 position must record blind_frozen even while
    Solana streams, or shadow evidence is contaminated by exactly the defect the armed rail had."""
    c = _conn([_swap("SOLCOIN", "2026-08-26T11:59:55+00:00")])
    rows, tally = vd.evaluate(
        c, now_iso=NOW,
        open_positions=[_pos(pid=1, mint="0xEvm", network_id="4663")],
        has_loss_rail_fn=lambda _p: True)
    assert tally["blind_frozen"] == 1
    assert rows and rows[0]["blind_frozen"] is True


def test_newest_swap_age_filters_by_chain():
    """Same mint string on two chains must not cross-contaminate ((network_id, mint) identity)."""
    c = _conn([_swap("MINT", "2026-08-26T11:58:00+00:00", chain="4663"),
               _swap("MINT", "2026-08-26T11:56:00+00:00", chain="solana")])
    age_sol = vd.newest_swap_age_s(c, mint="MINT", since_iso=OPENED, now_iso=NOW, chain_id="solana")
    assert age_sol == pytest.approx(240.0)          # its OWN 11:56 swap, not 4663's 11:58


# ------------------------------------------------------------------ the second witness
# Position 44813 (2026-08-28) exited vol_death while the coin traded both sides. The silence clock
# reads only the swap stream; that stream ran 112s/45s/61s gaps across the hold while the reserve
# lane logged 12 marks spanning 0.00346-0.00503 in the final "silent" 60 seconds. The rail measured
# our own packet loss as market death and sold a live position.


def test_a_moving_pool_HOLDS_even_when_the_swap_stream_is_silent():
    """THE 44813 CASE. Swap silence past the threshold, reserves moving inside the same window."""
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),        # last swap 180s ago
               _swap("OTHER", "2026-08-26T14:14:50+00:00"),       # swap stream healthy
               _state("MINT", "2026-08-26T14:14:20+00:00", 0.00346),
               _state("MINT", "2026-08-26T14:14:40+00:00", 0.00503)])
    fire, silence, why = _armed(c, _arm_pos())
    assert fire is False and why == "state_moving"
    assert silence == pytest.approx(180.0, abs=1.0)


def test_a_still_pool_FIRES_because_that_is_what_a_dead_coin_looks_like():
    """The witness must not become a veto. Reserves present and unchanging corroborate death."""
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00"),
               _state("MINT", "2026-08-26T14:14:20+00:00", 0.0042),
               _state("MINT", "2026-08-26T14:14:40+00:00", 0.0042),   # identical: no movement
               _state("OTHER", "2026-08-26T14:14:55+00:00", 9.0)])    # reserve lane alive
    fire, _silence, why = _armed(c, _arm_pos())
    assert fire is True and why == "measured_state_still"


def test_a_dead_pool_emitting_NOTHING_still_fires():
    """Robust to how the frame is emitted, which is why the fix does not depend on settling it.

    If a sync frame arrives only on a reserve change, a dead pool produces no marks in the window;
    if it arrives regardless, it produces marks that are all equal. Both must land on `still`.
    """
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00"),
               _state("MINT", "2026-08-26T14:10:30+00:00", 0.0042),   # covered, but BEFORE window
               _state("OTHER", "2026-08-26T14:14:55+00:00", 9.0)])    # reserve lane alive now
    fire, _silence, why = _armed(c, _arm_pos())
    assert fire is True and why == "measured_state_still"


def test_a_dark_reserve_lane_cannot_corroborate_anything():
    """Two witnesses sharing one failure are one witness. If the reserve lane is quiet chain-wide,
    its agreement is our blindness agreeing with itself."""
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00"),
               _state("MINT", "2026-08-26T14:10:30+00:00", 0.0042)])  # covered; lane now silent
    fire, _silence, why = _armed(c, _arm_pos())
    assert fire is False and why == "state_reader_dark"


def test_an_uncovered_pool_FREEZES_instead_of_firing_on_one_witness():
    """REVERSED 2026-08-29. 63 of 135 swap-covered funded Solana holds carry NO reserve marks, and
    this branch used to fire on swap silence alone for all of them — which made the second witness
    decoration, since every one of the 24 armed fires took exactly this path.

    The lane is alive but never watched THIS pool, so "did the pool go quiet" has no second answer.
    The rail freezes on not-knowing everywhere else (blind stream, uncovered chain, reader dark);
    this is the same condition under a different name. It self-arms per pool as coverage appears.
    """
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00"),
               _state("OTHER", "2026-08-26T14:14:55+00:00", 9.0)])    # lane alive, not this coin
    fire, _silence, why = _armed(c, _arm_pos())
    assert fire is False and why == "state_uncovered_frozen"


def test_the_witness_reads_VALUE_not_COUNT():
    """Getting this backwards inverts the defect: a healthy reader on a genuinely dead coin would
    block the rail forever. Measured on the live tape, 12.4% of consecutive reserve marks on one
    mint carry an identical mid, so the source does emit without a price change and a count is not
    a proxy for movement."""
    many_identical = [_state("MINT", "2026-08-26T14:14:%02d+00:00" % sec, 0.0042)
                      for sec in (10, 20, 30, 40, 50)]
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00"),
               _state("OTHER", "2026-08-26T14:14:55+00:00", 9.0)] + many_identical)
    fire, _silence, why = _armed(c, _arm_pos())
    assert fire is True and why == "measured_state_still", "five marks is not five price changes"


def test_an_uncovered_chain_still_blind_freezes_before_the_witness_runs():
    """CHAIN SCOPE UNCHANGED. The 08-26 behaviour for chains the swap stream never covered is
    decided before the second witness is consulted, so this change cannot alter it."""
    c = _conn([_swap("SOLMINT", "2026-08-26T14:14:50+00:00", "solana")])
    fire, silence, why = _armed(c, _arm_pos(mint="EVMMINT", network_id="4663"))
    assert fire is False and silence is None and why == "blind_frozen"


def test_the_fire_true_silence_none_regression_stays_dead():
    """A fire must always carry the silence that justified it. Every path that returns fire=True
    returns a float."""
    # Only the two-witness path fires now, so that is the only shape that can carry a silence.
    # The single-witness case moved to `state_uncovered_frozen` and is asserted in its own test.
    for marks in (
        [_swap("MINT", "2026-08-26T14:12:00+00:00"), _swap("OTHER", "2026-08-26T14:14:50+00:00"),
         _state("MINT", "2026-08-26T14:14:20+00:00", 0.0042),
         _state("OTHER", "2026-08-26T14:14:55+00:00", 9.0)],
    ):
        fire, silence, _why = _armed(_conn(marks), _arm_pos())
        assert fire is True
        assert isinstance(silence, float) and silence > 0


def test_the_witness_can_be_turned_off_for_a_rollback():
    """One flag, so a rollback does not need a code edit on the funded exit path."""
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00"),
               _state("MINT", "2026-08-26T14:14:20+00:00", 0.00346),
               _state("MINT", "2026-08-26T14:14:40+00:00", 0.00503)])
    assert _armed(c, _arm_pos())[0] is False                       # witness on: HOLD
    assert _armed(c, _arm_pos(), require_state_witness=False)[0] is True


def test_the_witness_is_on_by_default_and_the_runner_passes_the_knob():
    """A rollback must be an env edit, not a code change on the funded exit path."""
    from sniper.settings import memecoin as cfg
    from _source_guard import code_of
    from sniper.services import memecoin_real_runner as RR

    assert cfg.MEMECOIN_VOLDEATH_STATE_WITNESS is True
    code = code_of(RR.maintain_real_positions) if hasattr(RR, "maintain_real_positions") else ""
    if not code:
        code = pathlib.Path(RR.__file__).read_text()
    assert "MEMECOIN_VOLDEATH_STATE_WITNESS" in code, "the runner must read the knob"


def test_a_replay_cannot_read_marks_from_after_the_moment_it_judges():
    """REGRESSION (Codex review 2026-08-28). Mark reads had no upper bound, so a replay passing a
    PAST now_iso against a table that has since grown read FUTURE swaps and returned a negative
    silence — a full-database replay of 44813 gave -2050.69s. Production cannot read the future,
    so the live rail was never affected, but every grader and replay of this rail inherited it,
    and a negative silence silently reads as "not silent" rather than as an error.
    """
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),        # last swap before `now`
               _swap("OTHER", "2026-08-26T14:14:50+00:00"),       # stream healthy
               _swap("MINT", "2026-08-26T14:40:00+00:00"),        # 25 min AFTER the judged moment
               # flat reserves, so the rail's two-witness requirement is met and this test still
               # exercises a FIRE rather than an early freeze — the subject here is the bound on
               # the read, not the verdict
               _state("MINT", "2026-08-26T14:14:10+00:00", 0.0042),
               _state("MINT", "2026-08-26T14:14:40+00:00", 0.0042),
               _state("OTHER", "2026-08-26T14:14:55+00:00", 9.0)])
    fire, silence, _why = _armed(c, _arm_pos())
    assert silence == pytest.approx(180.0, abs=1.0), "the future swap must not be visible"
    assert fire is True


def test_the_state_witness_is_also_bounded_at_now():
    """The same defect, on the second witness: a later price move must not retroactively hold a
    decision that was correct when it was made."""
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00"),
               _state("MINT", "2026-08-26T14:14:20+00:00", 0.0042),
               _state("OTHER", "2026-08-26T14:14:55+00:00", 9.0),
               _state("MINT", "2026-08-26T14:40:00+00:00", 0.0099)])   # after the judged moment
    fire, _silence, why = _armed(c, _arm_pos())
    assert why == "measured_state_still", "a future price cannot make the pool retroactively alive"
    assert fire is True


def _post_threshold_labels():
    """Every `why` armed_exit_decision can return AFTER the threshold is crossed, from the source.

    NOT from a list written here. The property being guarded is "a label added later cannot be
    missed", and a fixed set intersected against the source can only ever report labels the set
    already knows — the exact case it advertises catching is the one it cannot see. Codex caught
    that in the first version of this guard.

    THE BOUNDARY IS THE WITNESS CALL, which is structural and is exactly the scope the audit
    covers. Returns above it never consulted the second witness — the below-threshold exit, the
    `require_state_witness=False` rollback, and the window-parse failure — so recording them would
    claim evidence that was never gathered. Returns BELOW it are witness verdicts, and every one
    must reach the audit or the operator's split silently loses a case.
    """
    def _constant_labels(tree, above=0):
        return {n.value.elts[2].value for n in ast.walk(tree)
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
                and len(n.value.elts) == 3 and isinstance(n.value.elts[2], ast.Constant)
                and n.lineno > above}

    tree = ast.parse(code_of(vd._armed_exit_decision))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "state_witness"]
    assert len(calls) == 1, (
        "this guard splits the taxonomy at the single state_witness call; it moved or multiplied, "
        "so the split can no longer be located and the guard must not pass quietly")
    labels = _constant_labels(tree, above=calls[0].lineno)
    assert labels, "no witness verdicts found below the witness call"
    # THE WRAPPER IS PART OF THE TAXONOMY (2026-09-03). `armed_exit_decision` now applies the
    # executable-move veto after the venue witnesses have already decided to fire, so its refusals
    # are post-threshold verdicts exactly like the ones below the witness call. Reading only the
    # private function would let a veto label decide an exit and never reach the grading split —
    # the precise failure this guard exists to catch, reintroduced by the split into two functions.
    # Returns that pass `why` through are variables, not constants, so they are skipped here.
    labels |= _constant_labels(ast.parse(code_of(vd.armed_exit_decision)))
    # 4663 DECIDES IN ITS OWN METHOD (2026-09-16). `_armed_exit_decision` returns
    # `RobinhoodVolumeState.decision(...)` whole, so its verdicts never appeared below the witness
    # call; they matched only because they shared Solana's label strings. Once 4663 named which
    # read went dark, that coincidence ended. Its boundary is the silence-threshold check.
    robin = next(n for n in ast.walk(ast.parse(code_of(vd.RobinhoodVolumeState)))
                 if isinstance(n, ast.FunctionDef) and n.name == "decision")
    gates = [n for n in ast.walk(robin)
             if isinstance(n, ast.If) and ast.unparse(n.test) == "silence < threshold"]
    assert len(gates) == 1, "the 4663 decision's threshold check moved or multiplied"
    labels |= _constant_labels(robin, above=gates[0].end_lineno)
    # THE TAPE RAIL AUDITS ITS BLINDNESS (operator order 2026-10-01), so every constant it returns
    # is a recorded verdict except the per-minute clock and the unparsable-input exit. Its
    # unvouched labels come from one table, indexed by the subscription family.
    labels |= _constant_labels(ast.parse(code_of(vd.tape_exit_decision))) - {
        "measured", "unmeasurable"}
    labels |= set(vd.TAPE_UNVOUCHED.values())
    # THE POLL RAIL (56, 2026-10-01) audits its blindness like the tape: every constant its decision
    # returns is a recorded verdict. Its non-blind verdicts come from the 4663 method above.
    poll = next(n for n in ast.walk(ast.parse(code_of(vd.BscPollState)))
                if isinstance(n, ast.FunctionDef) and n.name == "decision")
    labels |= _constant_labels(poll)
    return labels


def _audit_write_statement():
    """The `if` statement that guards the audit write, as CODE — comments removed.

    Bounded to that statement rather than to the file or to a character window. The runner carries
    an explainer comment naming these labels in prose two lines above, so a whole-file `in` check
    stays green when a label is deleted from the tuple and the comment survives. That is the
    documented failure of this exact guard class.
    """
    tree = ast.parse(code_of(pathlib.Path(RUNNER)))
    # EIGHT `if` statements in this file contain a record_exit_trigger_audit call — the table is
    # shared by several rails — so "the first one containing the call" picked the rails-blind rail
    # and the guard failed against code that was correct. Select on the vol_death guard's own
    # test, which is the only one that branches on `_vd_why`, and require exactly one.
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.If) and "_vd_why in" in ast.unparse(n.test)
            and any(isinstance(c, ast.Call)
                    and getattr(c.func, "attr", "") == "record_exit_trigger_audit"
                    for c in ast.walk(n))]
    assert len(hits) == 1, (
        "expected exactly one vol_death witness audit write branching on _vd_why; found "
        f"{len(hits)}. The runner no longer records the witness where this guard can see it.")
    return ast.unparse(hits[0])


def test_the_witness_label_is_QUERYABLE_not_only_logged():
    """The operator ruled that covered and uncovered holds are graded separately and never pooled.
    That ruling needs a COLUMN. `exit_reason` is 'vol_death' for both, so before this the split
    lived only in a log line and no grading query could honour it.
    """
    stmt = _audit_write_statement()
    assert "record_exit_trigger_audit" in stmt
    assert "vol_death" in stmt


def test_every_post_threshold_label_reaches_the_audit_write():
    """The advertised guarantee: a label added to the decision later cannot decide an exit and
    quietly leave the grading split one case short.

    UPDATED 2026-08-29. The runner used to inline the label tuple, and this guard compared label
    literals against that statement as text. The inline list then drifted for real — the decision
    function stopped returning `measured_no_state_witness` and started returning
    `state_uncovered_frozen`, and the runner recorded neither, so a decision was being MADE and not
    RECORDED. This guard caught it, which is the only reason it was found.

    The runner now reads one exported constant, so a text check cannot see the labels any more.
    Resolving the constant instead is strictly stronger: it compares SETS rather than substrings,
    so a label that merely appears somewhere in the statement no longer counts as handled.
    """
    labels = _post_threshold_labels()
    assert len(labels) >= 4, f"expected the four known decisions at least, found {labels}"

    exported = set(vd.POST_THRESHOLD_REASONS)
    missing = sorted(lab for lab in labels if lab not in exported)
    assert not missing, (
        "decided but never recorded, so the operator's split loses them: %s" % missing)

    stale = sorted(exported - set(labels))
    assert not stale, (
        "POST_THRESHOLD_REASONS names verdicts the decision function no longer returns: %s. "
        "A dead label hides a live one — the list must track the returns." % stale)

    # and the runner must actually route through that constant rather than re-hardcoding a list
    stmt = _audit_write_statement()
    assert "POST_THRESHOLD_REASONS" in stmt, (
        "the runner's audit branch does not read the exported constant, so the two can drift "
        "again exactly as they did on 2026-08-29: %s" % stmt)


def test_the_held_reasons_are_a_subset_of_the_recorded_ones():
    """A decision that is logged as HELD but never recorded is the same gap in the other lane."""
    assert set(vd.HELD_WITNESS_REASONS) <= set(vd.POST_THRESHOLD_REASONS)


# --- the 2026-09-02 uncovered split, withdrawn 2026-09-22 ----------------------------------------
#
# The split labelled a watched pool with no state mark `structural`: it "cannot ever answer". The
# premise was that a pool answers only when its program emits a `sol_sync` we decode.
# `SolanaSwapMarkCache` keeps ONE mark per mint, so a swap mark replaces a state mark before the
# fast lane persists either. Measured on 601 Solana mints, the SWAP-only share falls from 96% at
# one mark to 4% above 300, and every program shows both labels. The label read the sample.


def test_a_watched_pool_with_no_state_mark_is_plain_UNCOVERED():
    """The exact `structural` signature: swap marks on this mint, no state mark, ever. It freezes
    as one uncovered fact and must not claim the pool is incapable of answering."""
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00")])
    assert not hasattr(vd, "_uncovered_kind"), "the withdrawn split must not come back"
    fire, _silence, why = _armed(c, _arm_pos())
    assert fire is False and why == "state_uncovered_frozen"


def test_the_withdrawn_label_is_gone_from_the_audit_lists():
    """A label the decision cannot return must not sit in the operator's split pretending to."""
    assert "state_uncovered_structural" not in vd.POST_THRESHOLD_REASONS
    assert "state_uncovered_structural" not in vd.HELD_WITNESS_REASONS


def test_an_UNKNOWN_witness_freezes_instead_of_firing(monkeypatch):
    """THE FALL-THROUGH USED TO FIRE. The tail of armed_exit_decision was a bare
    `return True, silence, "measured_state_still"`, so any witness value a later change introduced
    would have SOLD the position rather than frozen it — on the ~50% of funded Solana holds that
    reach this branch. The one firing value is named explicitly now; everything else freezes."""
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00")])
    monkeypatch.setattr(vd, "state_witness", lambda *a, **k: "a_value_nobody_has_written_yet")
    fire, silence, why = _armed(c, _arm_pos())
    assert fire is False and why == "state_witness_unknown"
    assert silence == pytest.approx(180.0, abs=1.0)


def test_a_still_pool_STILL_fires():
    """The refactor must not cost the rail its one firing path."""
    c = _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),
               _swap("OTHER", "2026-08-26T14:14:50+00:00")])
    monkeypatch_free = vd.state_witness
    try:
        vd.state_witness = lambda *a, **k: "still"
        fire, _s, why = _armed(c, _arm_pos())
    finally:
        vd.state_witness = monkeypatch_free
    assert fire is True and why == "measured_state_still"


def test_a_bonding_curve_position_binds_no_pool_so_the_rail_freezes():
    """REGRESSION, position 49013 (2026-09-03). It exited `vol_death` at 92.2s on
    `measured_state_still` while the coin was alive — 25 distinct executable mids inside the hold,
    moving over a 20% range. The exact quote prices a pons token off the curve's own
    `getReserves()`, so a moving mid is moving reserves.

    Its route is `pons_v2`, ungraduated, `pairToken() == 0`. The binding took
    `liquidity_pair_address` from the classification facts — the V4 poolId DexScreener reports for
    the token, holding $5.01 and never traded — and `eth_getLogs` truthfully answered "no swaps"
    about a pool nobody uses. The rail read a stranger's stillness as this coin's death, and
    `vol_death` is the one exit that sells with no percent recheck behind it.

    A curve is not a pool, so no pool can witness it. Binding nothing lands on
    `swap_lane_uncovered`, which is how this module already answers every other unwatchable venue.
    """
    pos = {
        "network_id": "4663", "mint": "0x56A0",
        "funded_contract_json": json.dumps({"classification": {"facts": {
            "liquidity_pair_address": "0x" + "cc" * 32}}}),
        "evm_route_json": json.dumps({
            "protocol": "pons_v2", "curve": "0x68bbbfe9b069582f90fb2a3c9e6befef006ad794"}),
    }

    assert vd.robinhood_pool_bindings([pos]) == {}

    # A HEALTHY head stream, so the freeze below is the POOL check and not the blind-tape check.
    now_iso = "2026-09-03T11:36:42+00:00"
    now_ts = vd._epoch(now_iso)
    state = vd.RobinhoodVolumeState(health_window_s=5.0)
    state.replace_bindings(vd.robinhood_pool_bindings([pos]), now_ts=now_ts, covered=True)
    state.record_head(1000, now_ts=now_ts - 1.0)
    pos["opened_at"] = "2026-09-03T11:35:03+00:00"
    fire, _silence, why = state.decision(
        pos, now_iso=now_iso, exit_s=60.0, confirm_fn=lambda pool, first, last: [])

    assert (fire, why) == (False, "swap_lane_uncovered")


def _veto_conn(mids):
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE memecoin_position_marks (id INTEGER PRIMARY KEY, chain_id TEXT,
      mint TEXT, sweep_ts TEXT, mid REAL, source TEXT)""")
    for i, mid in enumerate(mids):
        c.execute("INSERT INTO memecoin_position_marks (chain_id, mint, sweep_ts, mid, source) "
                  "VALUES ('4663','0x56A0',?,?,?)",
                  (f"2026-09-03T11:36:{i:02d}+00:00", mid, vd.EXECUTABLE_SOURCE))
    c.commit()
    return c


def _veto(mids, pct=5.0):
    return vd.executable_price_moved(
        _veto_conn(mids), mint="0x56A0", chain_id="4663",
        window_start_iso="2026-09-03T11:35:42+00:00", now_iso="2026-09-03T11:36:42+00:00",
        min_range_pct=pct)


def test_the_executable_move_veto_separates_the_live_closes_from_the_dead_one():
    """The three funded 4663 vol_death closes that sold a moving coin, against the one that did
    not. Ranges are the measured ones: LOCK 16.3%, CARDPAD 38.9%, DUET 43.1%, GTAVI 1.7%.

    GTAVI is the reason this is a range and not a distinct-value count. It carried 23 distinct
    mids on a pool that really was dead — the EVM quote is denominated through ETH/USD, so a static
    pool still prints small differences. A count would freeze this rail on every EVM coin forever.
    """
    assert _veto([1.0, 1.163]) is True            # 49013 LOCK
    assert _veto([1.0, 1.389]) is True            # 48336 CARDPAD
    assert _veto([1.0, 1.431]) is True            # 47633 DUET, a genuine uniswap_v4 pool
    assert _veto([1.0 + i * 0.0008 for i in range(23)]) is False   # 48590 GTAVI, 1.7%, truly dead


def test_the_move_veto_never_fires_the_rail_and_never_guesses():
    """Veto only, and silent on absence. An empty tape, a zero mid or a broken read must leave the
    decision exactly as the venue witnesses made it — our missing query is not evidence that the
    coin traded."""
    assert _veto([]) is False
    assert _veto([0.0, 0.0]) is False
    assert _veto([1.0, 9.0], pct=0) is True       # 0 pct here is a threshold, not the disable knob

    class _Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such table")

    assert vd.executable_price_moved(
        _Broken(), mint="0x56A0", chain_id="4663", window_start_iso="a", now_iso="b",
        min_range_pct=5.0) is False


def test_a_moving_coin_cannot_be_sold_as_dead_however_the_witnesses_voted():
    """End to end through the wrapper: the venue witnesses fire, the price tape overrules them.

    The label is a post-threshold verdict, so it must also be one the audit write records —
    `test_every_post_threshold_label_reaches_the_audit_write` enforces that from the source.
    """
    conn = _veto_conn([1.0, 1.163])
    pos = {"opened_at": "2026-09-03T11:35:03+00:00", "mint": "0x56A0", "network_id": "4663"}
    state = vd.RobinhoodVolumeState(health_window_s=5.0)
    now_iso = "2026-09-03T11:36:42+00:00"
    now_ts = vd._epoch(now_iso)
    pool = "0x" + "cc" * 32
    # covered from the moment the position opened, so the silence clock really has crossed 60s
    state.replace_bindings({pool: "0x56a0"}, now_ts=vd._epoch(pos["opened_at"]), covered=True)
    state.record_head(1000, now_ts=now_ts - 1.0)

    fire, _silence, why = vd.armed_exit_decision(
        conn, pos, now_iso=now_iso, held_s=99.0, exit_s=60.0,
        min_opened_at="2026-01-01T00:00:00+00:00", robinhood_state=state,
        robinhood_confirm_fn=lambda p, f, l: [], move_veto_pct=5.0)

    assert (fire, why) == (False, "executable_price_moved")
    assert "executable_price_moved" in vd.POST_THRESHOLD_REASONS


def test_a_chain_with_no_executable_tape_is_uncovered_not_confirmed(caplog):
    """PINS THE BLIND SPOT so nobody reads "Solana is unaffected" as "Solana is protected".

    Only 4663 writes `fastlane_rpc`: 10,438 marks over 71 mints since 2026-08-25, none of them
    Solana. Solana prices funded positions from `fastlane_mid`, the batched DexScreener lane — the
    same nominal price the exit decision stopped trusting — so this witness must not read it and
    has nothing to read instead. All 25 Solana vol_death closes replay unchanged.

    The veto stays fail-open there, because freezing it would disable a rail that works on that
    chain. What it must not do is stay quiet about which fires it could not check.
    """
    conn = _veto_conn([1.0, 1.5])          # a 4663 tape...
    with caplog.at_level("WARNING"):
        moved = vd.executable_price_moved(
            conn, mint="0x56A0", chain_id="solana",   # ...read as solana: no marks match
            window_start_iso="2026-09-03T11:35:42+00:00",
            now_iso="2026-09-03T11:36:42+00:00", min_range_pct=5.0)

    assert moved is False
    assert "could not check this fire" in caplog.text


def test_one_mark_is_not_a_range_and_does_not_read_as_checked(caplog):
    """GAMMA, Protocol C review 2026-09-03. A range needs two points. At exactly one mark MIN
    equals MAX, the range is 0.0%, and the veto returns False having proved nothing — identical in
    the audit to a coin it checked and cleared. The first guard tested `not marks`, so the
    single-mark case skipped the warning as well and reported as checked-and-clear.

    3 of the 7 funded 4663 vol_death fires carried exactly one mark (47462, 47961, 48020) and a
    fourth carried exactly two (47633). This rail fires on coins nobody is quoting, which is the
    same condition that leaves the tape sparse — so this is the normal case here, not the tail.
    """
    with caplog.at_level("WARNING"):
        assert _veto([1.0]) is False
    assert "need 2 for a range" in caplog.text
    assert "1 executable mark(s)" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING"):
        assert _veto([1.0, 1.163]) is True          # two marks: a real range, and it vetoes
    assert caplog.text == ""


def test_a_multihop_position_binds_its_cached_pools(monkeypatch):
    """A multi-hop entry has no single `pool_id`, so it used to bind NOTHING and sat at
    `swap_lane_uncovered` with no rail at all. Multi-hop is now a common route, and this runner's
    decisions ran 57 blind_frozen / 22 state_reader_dark against 4 measured."""
    from sniper.services import memecoin_volume_death as vd
    pools = ["0x" + "a" * 64, "0x" + "b" * 64]
    monkeypatch.setattr("sniper.execution.evm_v4_quote._stored_pools_for_mint",
                        lambda *_a, **_k: pools)
    pos = {
        "network_id": "4663", "mint": "0x" + "cd" * 20,
        "funded_contract_json": "{}",
        "evm_route_json": '{"bridge":"0x' + "ee" * 20 + '"}',   # multi-hop: no pool_id
    }
    bindings = vd.robinhood_pool_bindings([pos])
    assert set(bindings) == set(pools), "every cached pool must be watched"
    assert set(bindings.values()) == {pos["mint"]}


def test_a_single_pool_route_also_gains_the_coins_other_pools(monkeypatch):
    """Position 49013 exited on a pool holding $5.01 that never traded while the coin moved 20%
    elsewhere. Watching every pool makes silence mean silence, not one stranger's stillness."""
    from sniper.services import memecoin_volume_death as vd
    routed = "0x" + "11" * 32
    others = ["0x" + "22" * 32]
    monkeypatch.setattr("sniper.execution.evm_v4_quote._stored_pools_for_mint",
                        lambda *_a, **_k: others)
    pos = {"network_id": "4663", "mint": "0x" + "cd" * 20,
           "funded_contract_json": "{}", "evm_route_json": '{"pool_id":"%s"}' % routed}
    bindings = vd.robinhood_pool_bindings([pos])
    assert routed in bindings and others[0] in bindings


def test_a_bonding_curve_still_binds_nothing(monkeypatch):
    """A curve is not a pool, so no pool can witness it. That exclusion must survive."""
    from sniper.services import memecoin_volume_death as vd
    monkeypatch.setattr("sniper.execution.evm_v4_quote._stored_pools_for_mint",
                        lambda *_a, **_k: ["0x" + "a" * 64])
    pos = {"network_id": "4663", "mint": "0x" + "cd" * 20,
           "funded_contract_json": "{}", "evm_route_json": '{"curve":true}'}
    assert vd.robinhood_pool_bindings([pos]) == {}


def test_a_cache_failure_never_breaks_the_binding(monkeypatch):
    from sniper.services import memecoin_volume_death as vd

    def boom(*_a, **_k):
        raise RuntimeError("db locked")
    monkeypatch.setattr("sniper.execution.evm_v4_quote._stored_pools_for_mint", boom)
    routed = "0x" + "11" * 32
    pos = {"network_id": "4663", "mint": "0x" + "cd" * 20,
           "funded_contract_json": "{}", "evm_route_json": '{"pool_id":"%s"}' % routed}
    assert vd.robinhood_pool_bindings([pos]) == {routed: pos["mint"]}


# ------------------------------------------ the exact-quote witness (2026-09-13, position 56828)
#
# Exact quotes veto movement. Flat quotes do not replace missing pool-state evidence.

@pytest.fixture(autouse=True)
def _empty_exact_tape():
    vd._EXACT_TAPE.clear()
    yield
    vd._EXACT_TAPE.clear()


def _uncovered():
    return _conn([_swap("MINT", "2026-08-26T14:12:00+00:00"),      # 180 s of swap silence
                  _swap("OTHER", "2026-08-26T14:14:50+00:00")])    # healthy stream


def _quotes(prices, stamps=("14:14:02", "14:14:30", "14:14:58")):
    for ts, px in zip(stamps, prices):
        vd.record_exact_quote(9, quoted_at="2026-08-26T%s+00:00" % ts, price=px)


@pytest.mark.parametrize("prices", [[1.00, 1.01, 0.99], [181.306661, 181.398528, 181.797927]])
def test_flat_quotes_cannot_confirm_death_without_pool_state(prices):
    _quotes(prices)
    fire, silence, why = _armed(_uncovered(), _arm_pos())
    assert fire is False and why == "state_uncovered_frozen"
    assert silence == pytest.approx(180.0, abs=1.0)


def test_an_uncovered_pool_holds_when_the_exact_quotes_moved():
    _quotes([1.00, 1.08, 1.02])                                  # 8% range >= the 5% cut
    fire, _silence, why = _armed(_uncovered(), _arm_pos())
    assert fire is False and why == "exact_quote_moving"
    assert why in vd.HELD_WITNESS_REASONS and why in vd.POST_THRESHOLD_REASONS


@pytest.mark.parametrize("stamps", [
    ("14:14:30", "14:14:58"),                    # two quotes: not enough
    ("14:14:40", "14:14:50", "14:14:58"),        # the window's first 40 s is not covered
    ("14:14:02", "14:14:20", "14:14:40"),        # nothing in the last 20 s
])
def test_sparse_exact_quotes_keep_the_old_freeze(stamps):
    _quotes([1.0] * len(stamps), stamps=stamps)
    fire, _silence, why = _armed(_uncovered(), _arm_pos())
    assert fire is False and why == "state_uncovered_frozen"


def test_the_exact_witness_has_an_env_rollback(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_EXACT_WITNESS", False)
    _quotes([1.0, 1.0, 1.0])
    assert _armed(_uncovered(), _arm_pos())[2] == "state_uncovered_frozen"


def test_the_exact_witness_never_answers_for_a_pool_the_state_lane_covers(monkeypatch):
    """A covered pool keeps its own reserve witness: moving quotes cannot override 'still', and
    still quotes cannot fire a pool whose reserves moved."""
    _quotes([1.0, 1.0, 1.0])
    monkeypatch.setattr(vd, "state_witness", lambda *a, **k: "moving")
    assert _armed(_uncovered(), _arm_pos())[:3:2] == (False, "state_moving")


def test_the_tape_ignores_a_cached_preview_re_read_and_forgets_closed_positions():
    vd.record_exact_quote(9, quoted_at="2026-08-26T14:14:02+00:00", price=1.0)
    vd.record_exact_quote(9, quoted_at="2026-08-26T14:14:02+00:00", price=2.0)   # same quote
    assert vd._EXACT_TAPE[9] == [(vd._epoch("2026-08-26T14:14:02+00:00"), 1.0)]
    vd.record_exact_quote(10, quoted_at="2026-08-26T14:30:00+00:00", price=1.0)  # 16 min later
    assert 9 not in vd._EXACT_TAPE and 10 in vd._EXACT_TAPE


# ------------------------------------------ state_reader_dark on 4663 (2026-09-16, positions 58831)
# 58831 X held `state_reader_dark` 36 times over ~5 min while quotes fell from -5.6% to -19.4%. On
# 4663 that day: 135 dark holds, 3 fires, 0 `state_moving`. The rail bound 6-11 pools per coin and
# made ONE eth_getLogs per pool in sequence, so one failed read out of N held the rail: at a ~26%
# per-read failure rate, 11 pools come back complete about 4% of the time.
def _dark_state(pools, *, health_window_s=5.0, head_at=130.0):
    state = vd.RobinhoodVolumeState(health_window_s=health_window_s)
    state.replace_bindings({p: "0xEvmCoin" for p in pools}, now_ts=100.0, covered=True)
    state.record_head(1000, now_ts=100.0)
    state.record_head(1300, now_ts=head_at)
    return state


_DARK_POS = {"mint": "0xEvmCoin", "opened_at": "1970-01-01T00:01:40+00:00"}
_DARK_NOW = "1970-01-01T00:02:10+00:00"


def test_reader_dark_names_the_read_that_failed():
    pools = ["0x" + "ab" * 32]
    assert _dark_state(pools).decision(_DARK_POS, now_iso=_DARK_NOW, exit_s=30.0,
                                       confirm_fn=lambda *_a: None)[2] \
        == "state_reader_dark:getlogs_failed"
    assert _dark_state(pools).decision(_DARK_POS, now_iso=_DARK_NOW, exit_s=30.0,
                                       confirm_fn=None)[2] == "state_reader_dark:no_confirm_fn"

    def boom(*_a):
        raise TimeoutError("read timed out")
    assert _dark_state(pools).decision(_DARK_POS, now_iso=_DARK_NOW, exit_s=30.0,
                                       confirm_fn=boom)[2] == "state_reader_dark:getlogs_failed"


def test_reader_dark_without_a_head_after_the_quiet_start_says_so():
    """Unreachable with the 5 s health window (the newest head is always inside the silence); a
    wide window reaches it, and the label must still be specific."""
    state = _dark_state(["0x" + "ab" * 32], health_window_s=1000.0, head_at=90.0)
    state._heads.clear()
    state.record_head(1300, now_ts=90.0)
    assert state.decision(_DARK_POS, now_iso=_DARK_NOW, exit_s=30.0,
                          confirm_fn=lambda *_a: [])[2] == "state_reader_dark:no_head"


def test_every_split_reader_dark_label_is_held_and_audited():
    labels = {"state_reader_dark:no_head", "state_reader_dark:no_confirm_fn",
              "state_reader_dark:getlogs_failed"}
    assert labels <= set(vd.HELD_WITNESS_REASONS)
    assert labels <= set(vd.POST_THRESHOLD_REASONS)
    assert "state_reader_dark" in vd.HELD_WITNESS_REASONS, "the Solana witness still returns it"


def test_one_confirm_read_covers_every_bound_pool():
    pools = ["0x%064x" % i for i in range(1, 12)]
    calls = []
    fire, _silence, why = _dark_state(pools).decision(
        _DARK_POS, now_iso=_DARK_NOW, exit_s=30.0,
        confirm_fn=lambda p, f, l: calls.append((sorted(p), f, l)) or [])
    assert (fire, why) == (True, "measured_state_still")
    assert calls == [(sorted(pools), 1000, 1300)]


def test_a_log_on_any_bound_pool_holds_and_records_that_pools_activity():
    from sniper.services.memecoin_evm_v4_events import V4_SWAP
    pools = ["0x%064x" % i for i in range(1, 4)]
    state = _dark_state(pools)
    manager = vd._robinhood_log_filter(pools[0])["address"]
    log = {"address": manager, "topics": [V4_SWAP, pools[2]]}
    assert state.decision(_DARK_POS, now_iso=_DARK_NOW, exit_s=30.0,
                          confirm_fn=lambda *_a: [log]) == (False, 30.0, "state_moving")
    assert state._activity_at == {pools[2]: pytest.approx(130.0)}


def test_the_batched_http_witness_sends_one_v4_read_and_one_address_list_read():
    v4 = ["0x" + "ab" * 32, "0x" + "cd" * 32]
    v2 = ["0x" + "12" * 20, "0x" + "34" * 20]
    sent = []

    def http(method, url, *, body):
        sent.append(json.loads(body)["params"][0])
        return 200, {"result": []}, None

    assert vd.robinhood_activity_logs(v4 + v2, 100, 200, rpc_url="https://official", http=http) == []
    assert len(sent) == 2
    by_kind = {len(f["address"]) if isinstance(f["address"], str) else "list": f for f in sent}
    assert by_kind[42]["topics"][1] == v4
    assert sorted(by_kind["list"]["address"]) == sorted(v2)
    assert all(f["fromBlock"] == "0x64" and f["toBlock"] == "0xc8" for f in sent)


@pytest.mark.parametrize("first", [
    RuntimeError("EVM RPC provider HTTP failure"),
    {"error": {"code": -32000, "message": "request timed out"}},
])
def test_a_failed_witness_read_is_retried_once(monkeypatch, first):
    from sniper.execution import evm_wallet
    answers = [first, {"result": []}]
    naps = []

    def rpc(url, method, params, *, http=None):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(evm_wallet, "_rpc", rpc)
    patch_local(monkeypatch, "sleep", naps.append)
    assert vd.robinhood_activity_logs(["0x" + "ab" * 32], 1, 2, rpc_url="https://official") == []
    assert naps == [pytest.approx(0.2)] and answers == []


def test_a_witness_read_that_fails_twice_is_none_and_its_cause_is_logged(monkeypatch, caplog):
    from sniper.execution import evm_wallet

    def rpc(url, method, params, *, http=None):
        raise RuntimeError("EVM RPC provider HTTP failure")

    monkeypatch.setattr(evm_wallet, "_rpc", rpc)
    monkeypatch.setattr(vd.time, "sleep", lambda _s: None)
    vd._READER_DARK_LOGGED.clear()
    with caplog.at_level("WARNING"):
        assert vd.robinhood_activity_logs(["0x" + "ab" * 32], 1, 2,
                                          rpc_url="https://official") is None
    assert "RuntimeError" in caplog.text and "HTTP failure" in caplog.text
