"""Degen exit trial, policy V4 (operator YES 2026-10-01).

Spec docs/superpowers/specs/2026-10-01-memecoin-degen-exit-trial-v4-design.md; read pre-registered
in docs/audits/2026-10-01-degen-exit-trial-preregistration.md. One uid on Solana, from a stamped
cohort time, freezes exit-trial-v4-v1: TP +100, floor -60, cap 800 s, mirror on the source's FIRST
sell (any size), no volume death. Contracts are built by the production builder and the exits run
through the production sweep and mirror deciders.
"""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_funded_policy as fp
from sniper.services import memecoin_real_runner as RR
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_route_series as rs
from sniper.services import memecoin_source_sell_mirror as ssm
from sniper.services.memecoin_market_state import EntryClassification
from sniper.settings import memecoin as cfg

DEGEN = "fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb"
OTHER = "aaaaaaaa-0000-0000-0000-000000000001"
FROM = "2026-10-01T18:00:00+00:00"
AFTER = "2026-10-01T18:00:05+00:00"
BEFORE = "2026-10-01T17:59:59+00:00"
_ATTEMPTS = iter(range(1, 100_000))


@pytest.fixture(autouse=True)
def _live_shape(monkeypatch):
    """Production resolves every Solana wallet to follow-30-50 (TP 25 / stop 35) with the timecap
    lift on; the trial knobs on for Degen from FROM."""
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_ALL_WALLETS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_WALLETS", ())
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_TP_PCT", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_STOP_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BAND_POLICY_BANDS", ())
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_ONLY_WALLETS", ())
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SECONDS", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_TRIAL_V4_WALLETS", frozenset({DEGEN}))
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_TRIAL_V4_FROM", FROM)


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    for uid, handle in ((DEGEN, "DegenCapitalLLC"), (OTHER, "other")):
        c.execute("INSERT INTO memecoin_wallets(source_user_id, handle) VALUES (?,?)", (uid, handle))
    c.commit()
    return c


def _classification(mint="MINT", decision_at=AFTER, network="solana"):
    return EntryClassification(
        classification_version=1, network_id=network, mint=mint, signal_source="follow_activity",
        decision_at=decision_at, coin_band="nano", lifecycle="young", liquidity_class="unknown",
        momentum_phase="climbing", market_regime="hot_narrow", data_quality="partial", risk_scale=1.0,
        reason_codes=("coin_band:nano",),
        facts={"network_id": network, "mint": mint, "effective_mcap": 30_000.0, "liquidity": 5_000.0,
               "base_clip_usd": None})


def _contract(c, uid=DEGEN, mint="MINT", decision_at=AFTER, network="solana", **kw):
    """A NEW reservation through the production seam every funded lane crosses."""
    attempt = "attempt-%d" % next(_ATTEMPTS)
    m.start_entry_attempt(c, attempt_id=attempt, decision_started_at=decision_at, network_id=network,
                          mint=mint)
    frozen = risk.prepare_funded_contract(
        c, classification=_classification(mint, decision_at, network), network_id=network, mint=mint,
        attempt_id=attempt, origin_event_id="event-1", base_clip_usd=100.0, approved_clip_usd=100.0,
        source_user_id=uid, **kw)
    c.commit()                                   # the seam writes with commit=False, as in reserve_entry
    return frozen["funded_contract_json"]


# ---------------------------------------------------------------- the contract

def test_degen_after_the_cohort_time_freezes_v4_and_records_the_trial():
    c = _conn()
    contract = json.loads(_contract(c))
    p = contract["policy"]
    assert (p["policy_id"], p["tp_pct"], p["stop_pct"], p["catastrophe_pct"], p["max_hold_s"],
            p["mirror_on_sell"], p["trail_pct"], p["derisk_pct"]) == (
        "exit-trial-v4-v1", 100.0, 0.0, 60.0, 800.0, True, 0.0, 0.0)
    trial = contract["source"]["exit_trial"]
    assert (trial["name"], trial["source_user_id"], trial["cohort_from"]) == ("degen-v4", DEGEN, FROM)
    assert trial["replaced_policy_id"] == "follow-30-50-v2" and trial["replaced_copy_arm"] is False
    assert "copy_arm" not in contract["source"] and "exit_override" not in contract["source"]
    fp._validated_policy_data(p)                     # a pinned prior release's strict parse


def test_before_the_cohort_time_and_with_the_knob_off_degen_is_byte_identical():
    c = _conn()
    before = _contract(c, decision_at=BEFORE)
    assert json.loads(before)["policy"]["policy_id"] == "follow-30-50-v2"
    cfg.MEMECOIN_EXIT_TRIAL_V4_WALLETS = frozenset()       # restored by the autouse fixture
    off = _contract(c, decision_at=BEFORE)
    assert off == before
    off_after = _contract(c, decision_at=AFTER)
    assert json.loads(off_after)["policy"]["policy_id"] == "follow-30-50-v2"


def test_another_wallet_is_byte_identical_with_the_knob_on_and_off():
    c = _conn()
    on = _contract(c, uid=OTHER)
    cfg.MEMECOIN_EXIT_TRIAL_V4_WALLETS = frozenset()
    assert _contract(c, uid=OTHER) == on
    assert "exit_trial" not in json.loads(on)["source"]


@pytest.mark.parametrize("bad", ["", "not a time", "2026-10-01T18:00:00"])   # no offset: off too
def test_an_empty_or_unparseable_cohort_time_is_off(bad):
    cfg.MEMECOIN_EXIT_TRIAL_V4_FROM = bad
    c = _conn()
    assert json.loads(_contract(c))["policy"]["policy_id"] == "follow-30-50-v2"


def test_an_evm_entry_and_a_manual_entry_never_get_v4():
    c = _conn()
    evm = json.loads(_contract(c, mint="0x" + "ab" * 20, network="4663"))
    assert evm["policy"]["policy_id"] != fp.EXIT_TRIAL_V4_POLICY_ID
    manual = json.loads(_contract(c, funded_policy=fp._follow3050_policy(coin_band="nano")))
    assert "exit_trial" not in manual["source"]


def test_v4_wins_over_the_ui_override_and_says_which_one_it_replaced():
    c = _conn()
    row = m.set_wallet_exit_override(c, DEGEN, network_id="solana", tp_pct=40.0, stop_pct=None,
                                     actor="test", reason="unit", now_iso=AFTER)
    contract = json.loads(_contract(c))
    assert contract["policy"]["tp_pct"] == 100.0 and "exit_override" not in contract["source"]
    assert contract["source"]["exit_trial"]["replaced_override_id"] is not None
    del row


# ---------------------------------------------------------------- the exit sweep

def _position(c, contract_json, uid=DEGEN, opened_at=AFTER, mint="MINT"):
    payload, digest = m.canonical_funded_json(json.loads(contract_json))
    pid = m.open_position(
        c, source_user_id=uid, source_handle="h", mint=mint, ticker="T", entry_price=1.0,
        entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0.0, their_size_usd=0.0,
        opened_at=opened_at, strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
        network_id="solana", funded_contract_version=1, funded_contract_hash=digest,
        funded_contract_json=payload)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=50.0, notional=50.0,
                  fill_source="chain", fraction=1.0, created_at=opened_at)
    return pid


def _sweep(c, price, now, previews=None):
    """One sweep; the exact preview follows the mark (coherent, so the executable confirmation
    agrees with the nomination)."""
    exits = []
    contexts = {"solana": {
        "preview_exit_fn": lambda pos: (previews.append(pos["id"]) if previews is not None else None)
        or {"expected_usdc": price * 50.0},
        "exit_fn": lambda _c, pos, reason, _now: exits.append(reason) or {"status": "failed"},
    }}
    RR.real_exit_sweep(c, contexts, price_fn=lambda _mints: {"MINT": {"price": price}},
                       now_fn=lambda: now)
    return exits


def _at(seconds):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(AFTER) + timedelta(seconds=seconds)).isoformat()


@pytest.mark.parametrize("price,want", [(2.0, ["real_tp"]), (1.99, []), (0.40, ["catastrophe"]),
                                        (0.41, []), (0.60, [])])     # -40%: today's 35 stop would fire
def test_v4_takes_profit_at_100_floors_at_60_and_has_no_35_stop(price, want):
    c = _conn()
    _position(c, _contract(c))
    assert _sweep(c, price, _at(10)) == want


def test_v4_caps_at_800_seconds_not_at_the_lifted_four_hours():
    c = _conn()
    pid = _position(c, _contract(c))
    assert _sweep(c, 1.1, _at(799)) == []
    assert _sweep(c, 1.1, _at(801)) == ["b_timecap"]
    del pid


def test_the_same_marks_on_todays_policy_still_run_todays_rails():
    c = _conn()
    _position(c, _contract(c, uid=OTHER), uid=OTHER)
    assert _sweep(c, 1.30, _at(10)) == ["real_tp"]            # TP 25
    c3 = _conn()
    _position(c3, _contract(c3, uid=OTHER), uid=OTHER)
    assert _sweep(c3, 0.60, _at(10)) == ["b_stop"]            # stop 35
    c2 = _conn()
    _position(c2, _contract(c2, uid=OTHER), uid=OTHER)
    assert _sweep(c2, 1.1, _at(801)) == []                    # 4 h cap, not 800 s


def test_every_sweep_takes_an_exact_preview_and_keeps_the_exit_decision_quote():
    """Gamma constraint 2: the V0 grade needs the exit_decision route quote every sweep."""
    c = _conn()
    pid = _position(c, _contract(c))
    rs.reset_state()
    previews = []
    for s in (2, 12, 22):
        _sweep(c, 1.1, _at(s), previews)
    assert previews.count(pid) >= 3
    seams = [r for r in rs._BUFFER if r.get("seam") == "exit_decision" and r.get("position_id") == pid]
    assert len(seams) >= 3


def test_volume_death_never_runs_on_v4(monkeypatch):
    """Alpha D1 = NO: the trial measures the policy Gamma replayed, which had no volume death."""
    from sniper.services import memecoin_volume_death as vd
    seen = []
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_ARMED", 1)
    monkeypatch.setattr(vd, "armed_exit_decision",
                        lambda conn, pos, **k: seen.append(pos["id"]) or (False, None, "test"))
    c = _conn()
    v4 = _position(c, _contract(c))
    control = _position(c, _contract(c, uid=OTHER, mint="MINT"), uid=OTHER)
    _sweep(c, 1.1, _at(40))
    assert v4 not in seen and control in seen


# ---------------------------------------------------------------- the mirror

def _sell_event(uid, mint="MINT"):
    return SimpleNamespace(source_user_id=uid, mint=mint, network_id="solana", side="sell",
                           fomo_event_id="sell-%s" % uid, signal_source="follow_activity",
                           their_price=1.0, their_size_usd=5.0, created_at=_at(30))


def _mirror(c, ev, monkeypatch):
    exits = []
    monkeypatch.setattr(RR, "_alert_exit_result", lambda *a, **k: None)
    monkeypatch.setattr(RR, "check_wallet_demotion", lambda *a, **k: False)
    contexts = {"solana": {"exit_fn": lambda _c, pos, reason, _now: exits.append(
        (pos["id"], reason)) or {"status": "closed"}}}
    RR.real_mirror_on_sell(c, ev, contexts, now_fn=lambda: _at(31))
    return exits


def test_legacy_fractional_mode_small_degen_sell_closes_v4_on_j7(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.30)
    c = _conn()
    pid = _position(c, _contract(c))
    assert _mirror(c, _sell_event(DEGEN), monkeypatch) == [(pid, "mirror")]
    row = c.execute("SELECT detail, allowed FROM memecoin_exit_trigger_audit WHERE position_id=? "
                    "AND lane='mirror_on_sell'", (pid,)).fetchone()
    assert row["allowed"] == 1 and json.loads(row["detail"])["code"] == "first_sell"


def test_the_same_sell_holds_a_position_on_todays_policy(monkeypatch):
    c = _conn()
    _position(c, _contract(c, uid=OTHER), uid=OTHER)
    assert _mirror(c, _sell_event(OTHER), monkeypatch) == []        # size unknown: held


def test_another_wallets_sell_never_closes_a_v4_position(monkeypatch):
    c = _conn()
    _position(c, _contract(c))
    assert _mirror(c, _sell_event(OTHER), monkeypatch) == []


def test_the_operators_mirror_hold_still_holds_a_v4_position(monkeypatch):
    c = _conn()
    pid = _position(c, _contract(c))
    m.set_position_mirror_disabled(c, pid, True)
    assert _mirror(c, _sell_event(DEGEN), monkeypatch) == []


def test_a_small_degen_sell_closes_a_v4_position_on_the_slot_lane_path(monkeypatch):
    """The slot-lane mirror: a 20% trim, below today's 30% rule, closes V4 (first sell)."""
    import test_memecoin_source_sell_mirror as T
    monkeypatch.setattr(RR, "_alert_exit_result", lambda *a, **k: None)
    monkeypatch.setattr(RR, "check_wallet_demotion", lambda *a, **k: False)
    fx = T.FX["65826"]
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_TRIAL_V4_WALLETS", frozenset({fx["source_user_id"].lower()}))
    c = _conn()
    c.execute("INSERT INTO memecoin_wallets(source_user_id, handle) VALUES (?,?)",
              (fx["source_user_id"], fx["handle"]))
    c.commit()
    contract = _contract(c, uid=fx["source_user_id"], mint=fx["mint"])
    assert json.loads(contract)["policy"]["policy_id"] == fp.EXIT_TRIAL_V4_POLICY_ID
    pid = _position(c, contract, uid=fx["source_user_id"], opened_at=T.OPENED, mint=fx["mint"])
    calls, lock = [], threading.Lock()
    mirror = ssm.SourceSellMirror(enabled=True, shadow=False, min_fraction=0.30,
                                  clock=lambda: 100.0, real_now_fn=lambda: T.AFTER)
    [whole] = T._sells("65826")
    mirror.offer_buy(SimpleNamespace(owner=whole.owner, mint=whole.mint, pool=None,
                                     token_amount=whole.sold_atomic * 5))
    trim = whole.__class__(**{**whole.__dict__, "signature": "T1", "slot": whole.slot + 1})
    mirror.offer(trim)
    [rec] = mirror.run(c, {"solana": {"exit_fn": lambda conn, pos, reason, now: calls.append(
        (pos["id"], reason)) or {"status": "closed"}}}, exit_lock=lock)
    assert rec["outcome"] == "mirrored" and rec["share"] == pytest.approx(0.2)
    assert calls == [(pid, ssm.REASON)]
    detail = json.loads(c.execute("SELECT detail FROM memecoin_exit_trigger_audit WHERE position_id=?",
                                  (pid,)).fetchone()[0])
    assert detail["code"] == "first_sell"


def test_the_trial_check_adds_no_io_to_the_entry_path():
    """The entry path's added work is a set lookup and two datetime parses: measured here so a
    regression that adds I/O shows up as time."""
    import time
    samples = []
    for _ in range(20_000):
        t = time.perf_counter_ns()
        fp.exit_trial_v4_applies(source_user_id=DEGEN, network_id="solana", decision_at=AFTER)
        samples.append(time.perf_counter_ns() - t)
    samples.sort()
    assert samples[len(samples) // 2] < 50_000                      # p50 under 50 us


def test_the_cohort_starts_exactly_at_its_stamp():
    """Gamma L2: a decision stamped exactly at the cohort time is in; one microsecond before is out."""
    assert fp.exit_trial_v4_applies(source_user_id=DEGEN, network_id="solana", decision_at=FROM)
    assert fp.exit_trial_v4_applies(source_user_id=DEGEN, network_id="solana",
                                    decision_at="2026-10-01T17:59:59.999999+00:00") is None


def test_a_zero_share_event_never_closes_a_v4_position_on_the_slot_lane(monkeypatch):
    """Gamma L1: first sell means a SALE. A decided event whose share is exactly 0 holds; the share is
    fixed on the SourceSell (share_basis), so the V4 condition itself is what is tested."""
    monkeypatch.setattr(RR, "_alert_exit_result", lambda *a, **k: None)
    monkeypatch.setattr(RR, "check_wallet_demotion", lambda *a, **k: False)
    c = _conn()
    pid = _position(c, _contract(c), opened_at=AFTER)
    calls = []
    mirror = ssm.SourceSellMirror(enabled=True, shadow=False, min_fraction=0.30,
                                  clock=lambda: 100.0, real_now_fn=lambda: _at(30))
    sell = ssm.SourceSell(path=ssm.PATH_SLOT, signature="Z1", slot=10, source_user_id=DEGEN,
                          handle="DegenCapitalLLC", owner="Owner", venue="pumpswap", mint="MINT",
                          pool=None, sold_atomic=1, pre_atomic=None, seen_at=_at(20),
                          share_basis=(0.0, "event_pre_balance"))
    rec = mirror._handle(c, {"solana": {"exit_fn": lambda conn, pos, reason, now: calls.append(reason)
                                        or {"status": "closed"}}}, sell,
                         exit_lock=threading.Lock(), now_iso_fn=lambda: _at(30))
    assert rec["position_id"] == pid and rec["outcome"] == "held_below" and calls == []
