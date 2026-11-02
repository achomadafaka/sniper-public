"""watch-list-v1 step 1 (operator 10-26, Gamma prereg 6c9b1f37f): an H answer is a WATCH; arm W (bit 2 of sha256(root))
re-looks it on a frozen cadence with a fresh JUDGE sheet, arm C keeps it final; bounded looks and life."""
from datetime import datetime, timedelta

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk as service
from sniper.settings import memecoin as cfg
from test_memecoin_desk_contract import candidate, evidence, policy, verdict
from test_memecoin_desk_pipeline import AT, POLICY, RESPONSE, api, databases, decide  # noqa: F401

RULE = {"version": "watch-list-v1", "max_looks": 3, "life_s": 900, "cadence_s": [60, 180], "relook_seats_per_h": 20}
WL = {**POLICY, "watch_list": RULE}
H = {**verdict(v="H", r=None)}


def coin(ticket_id="desk:U:fx1", tail="W"):                    # fx1: watch_arm 1 (W); fixture: 0 (C)
    c = candidate()
    return {**c, "ticket_id": ticket_id, "mint": c["mint"][:-1] + tail}


def ticket(databases, c, pol=WL):
    cohort = api().provision_cohort(*databases, producer="U", policy=pol, prompt_version="desk-judge-v25", created_at=AT)
    t = api().ingest_candidate(databases[0], c, cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], t, evidence=evidence(c), policy=policy(), now_iso=AT)
    return t


def vet(db, t):
    return [s for s in store.get_ticket(db[0], t)["stages"] if s["stage"] == "vet"][0]


def look(databases, t, raw, at):
    c = store.get_ticket(databases[0], t)["candidate"]
    return decide(databases, t, {**raw, "k": t}, asof={"candidate": c, "meta": {}}, call_started_at=at, response_at=at,
                  now_iso=at)


def test_arm_w_watch_is_logged_with_its_look_and_all_three_bits(databases):
    c = coin()
    t = ticket(databases, c)
    out = look(databases, t, H, RESPONSE)
    assert out["action"] == "WATCH"
    w = vet(databases, t)["inputs"]["watch"]
    assert w["look_index"] == 1 and w["trigger_reason"] == "band_entry" and w["watch_arm"] == 1 and w["verdict"] == "WATCH"
    assert {"limit_arm", "standing_arm", "sheet_sha", "mcap_usd_at_look", "seconds_since_band_entry", "look_cost_usd"} <= set(w)
    assert store.get_ticket(databases[0], t)["state"] == "waiting"


def test_arm_c_watch_is_final(databases):
    t = ticket(databases, candidate())                         # desk:U:fixture: watch_arm 0
    out = look(databases, t, {**H, "k": "desk:U:fixture"}, RESPONSE)
    assert out["action"] == "SKIP" and out["reason"] == "watch_final_arm_c"


def test_h_outside_a_watch_cohort_is_still_refused(databases):
    t = ticket(databases, coin(), pol=POLICY)
    assert look(databases, t, H, RESPONSE)["reason"] == "verdict_enum"


def test_kill_knob_off_makes_every_watch_final(databases, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_WATCH_LIST", False)
    t = ticket(databases, coin())
    assert look(databases, t, H, RESPONSE)["reason"] == "watch_off"


def test_cadence_relooks_until_max_looks_then_final(databases):
    t = ticket(databases, coin())
    now = datetime.fromisoformat(RESPONSE)
    look(databases, t, H, now.isoformat())
    assert service.expire_watches(databases[0], now_iso=(now + timedelta(seconds=30)).isoformat()) == []   # not standing_off
    assert service.relook_watches(databases[0], now_iso=(now + timedelta(seconds=30)).isoformat()) == []    # cadence 60 s
    now += timedelta(seconds=61)
    [l2] = service.relook_watches(databases[0], now_iso=now.isoformat())
    assert l2 == "desk:U:fx1:look2" and store.get_ticket(databases[0], t)["state"] == "expired"
    assert look(databases, l2, H, now.isoformat())["action"] == "WATCH"
    w = vet(databases, l2)["inputs"]["watch"]
    assert w["look_index"] == 2 and w["trigger_reason"] == "cadence_60" and w["watch_arm"] == 1
    now += timedelta(seconds=181)                              # look 2 waits cadence_s[1] = 180
    [l3] = service.relook_watches(databases[0], now_iso=now.isoformat())
    assert l3 == "desk:U:fx1:look3"
    out = look(databases, l3, H, now.isoformat())
    assert out["action"] == "SKIP" and out["reason"] == "watch_max_looks"        # max_looks 3: the last WATCH is final
    assert service.relook_watches(databases[0], now_iso=(now + timedelta(seconds=999)).isoformat()) == []


def test_an_enter_on_a_later_look_is_an_ordinary_enter(databases):
    t = ticket(databases, coin())
    now = datetime.fromisoformat(RESPONSE)
    look(databases, t, H, now.isoformat())
    now += timedelta(seconds=61)
    [l2] = service.relook_watches(databases[0], now_iso=now.isoformat())
    assert look(databases, l2, verdict(), now.isoformat())["action"] == "ENTER"
    assert vet(databases, l2)["inputs"]["watch"]["verdict"] == "ENTER"


def test_watch_life_ends_the_watch(databases):
    t = ticket(databases, coin())
    look(databases, t, H, RESPONSE)
    late = (datetime.fromisoformat(AT) + timedelta(seconds=901)).isoformat()
    service.relook_watches(databases[0], now_iso=late)
    head = store.get_ticket(databases[0], t)
    assert head["state"] == "expired" and [s["reason"] for s in head["stages"] if s["stage"] == "expire"] == ["watch_life"]


@pytest.mark.parametrize("bad", [{"max_looks": 1}, {"cadence_s": [180, 60]}, {"relook_seats_per_h": -1}, {"extra": 1}])
def test_malformed_watch_rule_refuses_provisioning(databases, bad):
    with pytest.raises(ValueError):
        api().provision_cohort(*databases, producer="U", policy={**WL, "watch_list": {**RULE, **bad}},
                               prompt_version="desk-judge-v25", created_at=AT)


def _two_looks_waiting(databases, pol=WL):
    out = []
    for tid, tail in (("desk:U:fx1", "W"), ("desk:U:fx6", "X")):              # both watch_arm 1
        t = ticket(databases, coin(tid, tail), pol=pol)
        look(databases, t, H, RESPONSE)
        out.append(t)
    service.relook_watches(databases[0], now_iso=(datetime.fromisoformat(RESPONSE) + timedelta(seconds=61)).isoformat())
    return out


def test_looks_are_seated_inside_the_frozen_relook_cap(databases, monkeypatch):
    from sniper.services import memecoin_desk_judge as J
    _two_looks_waiting(databases, pol={**WL, "watch_list": {**RULE, "relook_seats_per_h": 1}})
    now = datetime.fromisoformat(RESPONSE) + timedelta(seconds=62)
    picks, skipped = J._watch_looks(databases[0], now=now, calls_per_hour=40, fresh=0, limit=10)
    assert len(picks) == 1 and [s["state"] for s in skipped] == ["look_skipped_seat"] and skipped[0]["reason"] == "relook_cap"


def test_fresh_candidates_take_the_hourly_seats_first(databases):
    from sniper.services import memecoin_desk_judge as J
    _two_looks_waiting(databases)
    now = datetime.fromisoformat(RESPONSE) + timedelta(seconds=62)
    used = store.judge_calls_since(databases[0], since_iso=(now - timedelta(hours=1)).isoformat())
    picks, skipped = J._watch_looks(databases[0], now=now, calls_per_hour=used + 3, fresh=2, limit=10)
    assert len(picks) == 1 and skipped[0]["reason"] == "hourly_cap"


def test_an_unseated_look_dies_at_watch_life(databases):
    [t, _] = _two_looks_waiting(databases)
    late = (datetime.fromisoformat(AT) + timedelta(seconds=901)).isoformat()
    service.relook_watches(databases[0], now_iso=late)
    child = store.get_ticket(databases[0], "desk:U:fx1:look2")
    assert child["state"] == "expired" and [s["reason"] for s in child["stages"] if s["stage"] == "expire"] == ["watch_life"]


def _enter_look(databases):
    t = ticket(databases, coin())
    now = datetime.fromisoformat(RESPONSE)
    look(databases, t, H, now.isoformat())
    now += timedelta(seconds=61)
    [l2] = service.relook_watches(databases[0], now_iso=now.isoformat())
    look(databases, l2, verdict(), now.isoformat())
    return l2, now


def test_a_later_look_enter_waits_for_its_fresh_filter_then_queues(databases):
    l2, now = _enter_look(databases)
    assert store.list_outbox(databases[0]) == []                                     # nothing queued before the filter
    assert [h["ticket_id"] for h in service.list_pending_refilters(databases[0])] == [l2]
    service.record_refilter(databases[0], l2, evidence=evidence(coin()), now_iso=(now + timedelta(seconds=1)).isoformat(),
                            timing={"refilter_ms": 600.0})
    assert len(store.list_outbox(databases[0])) == 1


def test_a_failed_look_filter_is_a_kill_and_never_queues(databases):
    l2, now = _enter_look(databases)
    ev = evidence(coin())
    ev["checks"] = {k: {**v, "status": "unknown", "reason": "rpc_failed"} for k, v in ev["checks"].items()}
    row = service.record_refilter(databases[0], l2, evidence=ev, now_iso=(now + timedelta(seconds=1)).isoformat(), timing={})
    assert row["verdict"] == "SKIP" and store.get_ticket(databases[0], l2)["state"] == "refused"
    service.dispatch_outbox(*databases, now_iso=(now + timedelta(seconds=2)).isoformat())
    assert store.list_outbox(databases[0]) == []


def test_a_look_enter_without_a_filter_inside_its_ttl_times_out(databases):
    l2, now = _enter_look(databases)
    service.dispatch_outbox(*databases, now_iso=(now + timedelta(seconds=21)).isoformat())     # verdict enter_ttl_s 20
    head = store.get_ticket(databases[0], l2)
    assert head["state"] == "refused" and [s["reason"] for s in head["stages"] if s["stage"] == "size"] == ["refilter_timeout"]


def test_a_look_1_enter_is_still_instant(databases):
    t = ticket(databases, coin())
    look(databases, t, verdict(), RESPONSE)
    assert len(store.list_outbox(databases[0])) == 1 and service.list_pending_refilters(databases[0]) == []


def _watching(databases):
    """An arm-W WATCH with a known last-look reference (as-of price, 4 % noise, 60 s regular buys 100 USD)."""
    c = coin()
    t = ticket(databases, c)
    asof = {"candidate": c, "meta": {"rank": {"price_now": 1e-6}}}
    decide(databases, t, {**H, "k": t}, asof=asof,
           structure={"version": "structure-v1", "volatility": {"median_5s_swing_pct": 4.0, "bars": 30},
                      "price_usd": 2.0e-4, "windows": {"60s": {"state": "ok", "buy_usd": 100.0}, "300s": {"high": 2.2e-4}}},
           call_started_at=RESPONSE, response_at=RESPONSE, now_iso=RESPONSE, sheet_sha256="ab" * 32)
    return t, datetime.fromisoformat(RESPONSE).timestamp()


def _book(databases):
    from sniper.services.memecoin_desk_universe import WatchBook
    b = WatchBook()
    b.load(databases[0].execute(WatchBook.SQL).fetchall(), 0.0)
    return b


@pytest.mark.parametrize("prints,kind", [
    ([(1, 1.19e-6, True, "a")], "new_high"),     # last-look high 1.1e-6 SOL (USD high/price x p0); +8.2 % > 2 x 4 % noise
    ([(1, 0.95e-6, False, "a"), (2, 1.0e-6, True, "b")], "pullback_reclaim"),     # dip > 1 x noise, back to the look
    ([(i, 1.0e-6, True, "w%d" % i) for i in range(5)], "fresh_buyers"),
])
def test_change_triggers_emit_one_request(databases, prints, kind):
    t, t0 = _watching(databases)
    b = _book(databases)
    mint = store.get_ticket(databases[0], t)["mint"]
    for dt, px, buy, actor in prints:
        b.on_trade(mint=mint, price_sol=px, received_at=t0 + dt, is_buy=buy, actor=actor, agents=frozenset(),
                   gap_since_load=False, recent=[])
    [(tid, req)] = b.take()
    assert tid == t and req["trigger"] == kind
    b.commit(t)
    b.on_trade(mint=mint, price_sol=2e-6, received_at=t0 + 9, is_buy=True, actor="z", agents=frozenset(),
               gap_since_load=False, recent=[])
    assert b.take() == []                                                            # one request per look


def test_agent_buyers_and_unknown_reference_never_trigger(databases):
    t, t0 = _watching(databases)
    b = _book(databases)
    mint = store.get_ticket(databases[0], t)["mint"]
    for i in range(6):
        b.on_trade(mint=mint, price_sol=1.0e-6, received_at=t0 + i, is_buy=True, actor="agent", agents=frozenset({"agent"}),
                   gap_since_load=False, recent=[])
    assert b.take() == []
    b.by_mint[mint][0]["noise"] = None                                               # unknown noise: no price trigger
    b.on_trade(mint=mint, price_sol=5e-6, received_at=t0 + 7, is_buy=False, actor="x", agents=frozenset(),
               gap_since_load=False, recent=[])
    assert b.take() == []


def test_volume_burst_needs_a_full_minute_after_the_look(databases):
    t, t0 = _watching(databases)
    b = _book(databases)
    mint = store.get_ticket(databases[0], t)["mint"]
    ring = [(t0 + 30 + i, True, 50.0, "r%d" % (i % 3), 1.0e-6) for i in range(5)]       # 250 USD >= 2 x 100
    b.on_trade(mint=mint, price_sol=1.0e-6, received_at=t0 + 40, is_buy=True, actor="r0", agents=frozenset(),
               gap_since_load=False, recent=ring)
    assert b.take() == []                                                            # < 60 s since the look
    b.on_trade(mint=mint, price_sol=1.0e-6, received_at=t0 + 61, is_buy=True, actor="r0", agents=frozenset(),
               gap_since_load=False, recent=ring)
    assert [r["trigger"] for _, r in b.take()] == ["volume_burst"]


def test_writer_turns_a_trigger_into_the_next_look(databases):
    t, t0 = _watching(databases)
    now = datetime.fromisoformat(RESPONSE) + timedelta(seconds=5)
    service.record_relook(databases[0], t, {"trigger": "new_high", "change_score": 4}, now_iso=now.isoformat())
    [l2] = service.relook_watches(databases[0], now_iso=now.isoformat())
    assert store.get_ticket(databases[0], l2)["candidate"]["watch_look"]["trigger"] == "new_high"
    with pytest.raises(ValueError):
        service.record_relook(databases[0], t, {"trigger": "new_high"}, now_iso=now.isoformat())   # no longer waiting


def _end(databases, t):
    rows = [s["output"] for s in store.get_ticket(databases[0], t)["stages"] if s["stage"] == "watch_end"]
    return rows[0] if rows else None


def test_every_finished_watch_gets_one_end_row_on_its_last_look(databases):
    t = ticket(databases, coin())
    now = datetime.fromisoformat(RESPONSE)
    look(databases, t, H, now.isoformat())
    now += timedelta(seconds=61)
    [l2] = service.relook_watches(databases[0], now_iso=now.isoformat())
    look(databases, l2, {**verdict(v="S", r=None)}, now.isoformat())             # DROP at look 2
    service.record_watch_ends(databases[0], now_iso=now.isoformat())
    assert _end(databases, t) is None                                            # look 1 continued
    row = _end(databases, l2)
    assert row["end_reason"] == "drop" and row["root"] == t and row["look_index"] == 2 and row["looks_judged"] == 2
    assert row["watch_arm"] == 1
    assert service.record_watch_ends(databases[0], now_iso=now.isoformat()) == []  # once
    assert store.get_ticket(databases[0], l2)["state"] == "skipped"              # the record never moves the head


def test_arm_c_and_life_ends_are_named(databases):
    tc = ticket(databases, candidate())
    look(databases, tc, {**H, "k": "desk:U:fixture"}, RESPONSE)
    tw = ticket(databases, coin())
    look(databases, tw, H, RESPONSE)
    service.relook_watches(databases[0], now_iso=(datetime.fromisoformat(AT) + timedelta(seconds=901)).isoformat())
    assert _end(databases, tc)["end_reason"] == "watch_final_arm_c" and _end(databases, tc)["watch_arm"] == 0
    assert _end(databases, tw)["end_reason"] == "watch_life"


def test_new_high_is_measured_from_the_last_look_high_not_its_price(databases):
    t, t0 = _watching(databases)                    # look price 1.0e-6 SOL, look high 1.1e-6 SOL, noise 4 %
    b = _book(databases)
    mint = store.get_ticket(databases[0], t)["mint"]
    b.on_trade(mint=mint, price_sol=1.12e-6, received_at=t0 + 1, is_buy=False, actor="a", agents=frozenset(),
               gap_since_load=False, recent=[])     # +12 % over the price, +1.8 % over the high: not a new high
    assert b.take() == []


def test_per_look_log_binds_sheet_hash_change_score_and_prev_high(databases):
    t, _ = _watching(databases)
    w = vet(databases, t)["inputs"]["watch"]
    assert w["sheet_sha"] == "ab" * 32 and w["look_high"] == 2.2e-4 and w["prev_look_high"] is None
    service.record_relook(databases[0], t, {"trigger": "new_high", "change_score": 4}, now_iso=RESPONSE)
    [child] = service.relook_watches(databases[0], now_iso=RESPONSE)
    look(databases, child, H, RESPONSE)
    cw = vet(databases, child)["inputs"]["watch"]
    assert cw["look_index"] == 2 and cw["change_score"] == 4 and cw["prev_look_high"] == 2.2e-4
    assert cw["sheet_sha"] is None                  # not recorded = unknown, never the candidate snapshot hash
