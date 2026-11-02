import sqlite3

import pytest

from sniper.services import memecoin_large_buyer as B, memecoin_manual_intents as I
from sniper.services.memecoin_funded_policy import apply_desk_rails
from sniper.services.memecoin_origin import desk_lane_open, validate_desk_metadata
import sniper.settings.memecoin as cfg

NOW = 1793500000.0
T = dict(pool="P", wallet="W", mint="M", signature="S", trigger_at=NOW - 10, entry_due=NOW, pool_quote_before=str(50 * 10**9), post_quote=str(100 * 10**9), post_base=str(10**15))


class Arm:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *a):
        return self

    def fetchall(self):
        return self.rows


@pytest.fixture
def ledger():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_positions(desk_cohort_id,execution_mode)")
    c.execute("CREATE TABLE memecoin_pending_entries(desk_cohort_id,status)")
    c.execute("CREATE TABLE memecoin_manual_intents(desk_ticket_id,state)")
    return c


def run(ledger, t=T, **kw):
    sent, counts = [], {}
    args = dict(cohort_id=B.COHORT_ID, decimals_fn=lambda m: 6, sol_fn=lambda n: 100.0, enqueue=lambda conn, **r: sent.append(r), now=NOW, max_fills=30, seen=set(), counts=counts)
    args.update(kw)
    B.cycle(Arm([t]), ledger, **args)
    return sent, counts


def test_request_is_valid_desk_metadata_with_frozen_rails_and_a_real_cap(ledger):
    sent, _ = run(ledger)
    r = sent[0]
    assert r["rails"] == {"tp_pct": 40.0, "sl_pct": 30.0} and r["size_usd"] == 25 and r["signal_source"] == "desk"
    assert validate_desk_metadata(r["desk_metadata"]) == (True, None)
    vp = r["desk_metadata"]["verdict_price"]
    assert vp["ref_price_usd"] == pytest.approx(100 / (10**15 / 10**6) * 100)      # USD per whole token, 6 decimals
    assert vp["cap_pct"] > 15


@pytest.mark.parametrize("change,why", [
    (dict(pool_quote_before=str(39 * 10**9)), "pool_under_floor_or_unknown"),
    (dict(pool_quote_before=None), "pool_under_floor_or_unknown"),
    (dict(post_base=None), "reserves_unknown"),
    (dict(entry_due=NOW - 6), "late"),
])
def test_absent_or_stale_data_skips_by_name(ledger, change, why):
    sent, counts = run(ledger, dict(T, **change))
    assert sent == [] and counts == {why: 1}


def test_unknown_decimals_or_fx_never_buys(ledger):
    assert run(ledger, decimals_fn=lambda m: None)[1] == {"decimals_unknown": 1}
    assert run(ledger, sol_fn=lambda n: None)[1] == {"sol_unknown": 1}


def test_the_fill_cap_counts_inflight_and_stops(ledger):
    ledger.execute("INSERT INTO memecoin_positions VALUES('large-buyer-v1','real')")
    ledger.execute("INSERT INTO memecoin_pending_entries VALUES('large-buyer-v1','reserved')")
    ledger.execute("INSERT INTO memecoin_manual_intents VALUES('lb:x','queued')")
    assert run(ledger, max_fills=3)[1] == {"max_fills": 1}
    assert run(ledger, max_fills=4)[1] == {"sent": 1}


def test_one_decision_per_trigger(ledger):
    seen = set()
    assert run(ledger, seen=seen)[1] == {"sent": 1}
    assert run(ledger, seen=seen)[1] == {}


def test_knobs_are_separate_and_default_off(monkeypatch):
    assert cfg.MEMECOIN_LARGE_BUYER_ENABLED is False
    monkeypatch.setattr(cfg, "MEMECOIN_LARGE_BUYER_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", False)
    pilot, llm = dict(cohort_id="large-buyer-v1"), dict(cohort_id="desk-cohort:U:x")
    assert desk_lane_open(pilot) and not desk_lane_open(llm)
    monkeypatch.setattr(cfg, "MEMECOIN_LARGE_BUYER_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    assert not desk_lane_open(pilot) and desk_lane_open(llm)


def test_time_stop_is_only_for_the_pilot_producer():
    meta = dict(cohort_id="large-buyer-v1", producer="H", opportunity_id="o", snapshot_hash="0" * 64, time_stop_s=300.0)
    assert validate_desk_metadata(meta)[0]
    assert not validate_desk_metadata(dict(meta, cohort_id="desk-cohort:U:x"))[0]
    assert not validate_desk_metadata(dict(meta, time_stop_s=0))[0]


def test_time_stop_is_frozen_into_the_policy_and_a_desk_entry_without_keeps_its_band():
    from sniper.services.memecoin_funded_policy import resolve_funded_policy
    base = resolve_funded_policy(coin_band="micro", network_id="solana")
    railed = apply_desk_rails(base, dict(rails=dict(B.RAILS), time_stop_s=300.0))
    assert railed.max_hold_s == 300.0 and railed.tp_pct == 40.0 and railed.stop_pct == 30.0 and railed.trail_pct == 0.0 and railed.catastrophe_pct == base.catastrophe_pct
    assert apply_desk_rails(base, dict(rails=dict(B.RAILS))).max_hold_s == base.max_hold_s


def test_a_pilot_intent_end_to_end_opens_with_the_frozen_contract_and_the_llm_desk_stays_shut(monkeypatch):
    import json
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_arming, memecoin_real_runner as runner
    from sniper.util.clock import real_utc_now_iso
    for k, v in dict(MEMECOIN_REAL_ENABLED=True, MEMECOIN_DESK_ENABLED=False, MEMECOIN_LARGE_BUYER_ENABLED=True, MEMECOIN_DESK_CLIP_USD=25.0, MEMECOIN_DESK_CHAINS=frozenset({"solana"}),
                     MEMECOIN_DESK_MAX_CONCURRENT=2, MEMECOIN_DESK_DAILY_LOSS_USD=50.0, MEMECOIN_DESK_RAIL_BOUNDS={"tp_pct": (15, 150), "sl_pct": (10, 40), "trail_pct": (8, 30)}).items():
        monkeypatch.setattr(cfg, k, v)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *a, **k: None)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    mint = "So11111111111111111111111111111111111111112"[:-1] + "3"
    cohort = m.register_desk_cohort(c, producer=B.PRODUCER, policy=B.POLICY, prompt_version=B.PROMPT_VERSION, created_at=real_utc_now_iso(), cohort_id=B.COHORT_ID)
    caps = []

    def execute(conn, pending_id, mint, size, now_iso, impact_cap_pct=None, desk_price_cap=None, **kw):
        caps.append(desk_price_cap)
        row = dict(conn.execute("SELECT * FROM memecoin_pending_entries WHERE id=?", (pending_id,)).fetchone())
        pid = m.open_position(conn, source_user_id=row["source_user_id"], source_handle="(real)", mint=mint, ticker="LB", entry_price=0.0001, entry_notional=size,
                              entry_fill_source="chain", entry_lag_s=0.0, their_size_usd=0.0, opened_at=now_iso, strategy=row["strategy"], execution_mode="real", network_id="solana",
                              signal_source=row["signal_source"], funded_contract_json=row["funded_contract_json"])
        conn.execute("UPDATE memecoin_pending_entries SET status='confirmed' WHERE id=?", (pending_id,))
        return {"status": "filled", "position_id": pid}

    contexts = {"solana": {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0, "token_safety_fn": lambda mint, **kw: (True, "ok"), "execute_fn": execute, "kill_path": None}}
    stats = lambda mint, network_id=None: {"market_cap": 60000.0, "price": 0.0001, "liquidity_usd": 20000.0}
    t = dict(T, mint=mint)
    request = B.request_for(t, cohort_id=cohort, decimals=6, sol_usd=100.0, now=NOW)
    request.pop("ttl_s")
    iid = I.enqueue(c, now_iso=real_utc_now_iso(), **request)
    done = I.drain(c, runner.make_real_buy_fn(contexts, stats_fn=stats), now_fn=real_utc_now_iso)
    status = I.read_status(c, iid)
    assert done == [(iid, I.FILLED)], status
    policy = json.loads(c.execute("SELECT funded_contract_json FROM memecoin_positions").fetchone()[0])["policy"]
    assert policy["max_hold_s"] == 300.0 and policy["tp_pct"] == 40.0 and policy["stop_pct"] == 30.0 and float(policy["catastrophe_pct"]) > 0
    assert caps[0]["ref_price_usd"] > 0
    with pytest.raises(ValueError, match="desk disabled"):
        I.enqueue(c, now_iso=real_utc_now_iso(), **dict(request, desk_ticket_id="llm", origin_event_id="desk:llm", desk_metadata={k: v for k, v in {**request["desk_metadata"], "cohort_id": "desk-cohort:U:x"}.items() if k != "time_stop_s"}))


def test_decimals_only_for_pump_mints_and_the_risk_seat_never_touches_the_pilot():
    from sniper.services import memecoin_desk_risk_apply as R
    assert B.pump_decimals("abcpump") is None and B.pump_decimals("abc") is None
    assert R._fixed_rule(dict(desk_cohort_id="large-buyer-v1")) and not R._fixed_rule(dict(desk_cohort_id="desk-cohort:U:x"))
    assert R.apply_orders(None, dict(signal_source="desk", desk_cohort_id="large-buyer-v1", id=1), now_epoch=1., now_iso="x", orders=[dict(action="EXIT")]) is None


def test_a_pilot_position_and_reservation_take_no_copy_slot():
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.open_position(c, source_user_id=None, source_handle="(real)", mint="M", ticker="LB", entry_price=0.1, entry_notional=25.0, entry_fill_source="chain", entry_lag_s=0.0,
                    their_size_usd=0.0, opened_at="2026-11-01T00:00:00+00:00", strategy="s", execution_mode="real", network_id="solana", signal_source="desk")
    assert m.automatic_funded_count(c) == 0 and m.automatic_funded_count(c, network="solana") == 0
    m.open_position(c, source_user_id="u", source_handle="h", mint="N", ticker="C", entry_price=0.1, entry_notional=25.0, entry_fill_source="chain", entry_lag_s=0.0,
                    their_size_usd=0.0, opened_at="2026-11-01T00:00:00+00:00", strategy="s", execution_mode="real", network_id="solana", signal_source="slot_lane")
    assert m.automatic_funded_count(c) == 1


def test_min_others_compares_and_unknown_fails_only_when_required(ledger):
    assert run(ledger, dict(T, others_60s=0), min_others=1)[1] == {"too_few_others": 1}
    assert run(ledger, dict(T), min_others=1)[1] == {"too_few_others": 1}
    assert run(ledger, dict(T, others_60s=1), min_others=1)[1] == {"sent": 1}
    assert run(ledger, dict(T), min_others=0)[1] == {"sent": 1}
    assert cfg.MEMECOIN_LARGE_BUYER_MIN_OTHERS == 1
