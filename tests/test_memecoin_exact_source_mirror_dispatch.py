"""Only receipt-sized source sales create repeatable exact mirror requests."""
import contextlib
import json
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_funded_policy as fp
from sniper.services import memecoin_manual_exit_intents as mei
from sniper.services import memecoin_real_runner as rr
from sniper.services import memecoin_source_sell_mirror as ssm
from sniper.settings import memecoin as cfg
from test_memecoin_onchain_mirror_pending import world
from test_memecoin_bsc_sniper_arm import UID, NOW, _position, _sale


@pytest.fixture
def exact(monkeypatch):
    requests = []
    monkeypatch.setattr(fp, "is_source_mirror_tp100_position", lambda pos: bool((pos or {}).get("exact_profile")))
    monkeypatch.setattr(mei, "enqueue_source_partial", lambda conn, **kw: requests.append(kw) or "source-request")
    return requests


def test_evm_partial_is_queued_even_while_previous_partial_owns_position(world, exact, monkeypatch):
    pid = _position(world.c)
    orig = m.get_position
    monkeypatch.setattr(m, "get_open_position", lambda *a, **kw: {**orig(world.c, pid),
                        "exact_profile": True, "derisk_claim_id": "prior-partial"})
    row = _sale(world.c, sold=25, pre=100)
    assert world.run(row)["status"] == "source_partial_queued"
    assert world.calls == []
    assert exact[0]["source_sold_atomic"] == 25 and exact[0]["source_pre_atomic"] == 100
    assert exact[0]["source_owner"] == row["address"]
    assert world.run(row)["status"] == "already_decided"
    assert len(exact) == 1


@pytest.mark.parametrize("sold,pre,post", [(0,100,100),(25,None,75),(101,100,0),(25,100,None),(25,100,70)])
def test_evm_unknown_or_inconsistent_ratios_never_guess(world, exact, monkeypatch, sold, pre, post):
    pid = _position(world.c)
    orig = m.get_position
    monkeypatch.setattr(m, "get_open_position", lambda *a, **kw: {**orig(world.c,pid),"exact_profile":True})
    row = {**_sale(world.c), "sold_atomic": sold, "pre_atomic": pre, "post_atomic": post}
    assert world.run(row)["status"] == "mirror_held"
    assert exact == [] and world.calls == []


def test_evm_full_source_sale_is_journaled_behind_pending_partial(world, exact, monkeypatch):
    pid = _position(world.c)
    orig = m.get_position
    monkeypatch.setattr(m, "get_open_position", lambda *a, **kw: {**orig(world.c,pid),
        "exact_profile":True,"derisk_sig":"unknown-partial"})
    assert world.run(_sale(world.c,sold=100,pre=100))["status"] == "source_partial_queued"
    assert exact[0]["source_sold_atomic"] == exact[0]["source_pre_atomic"] == 100


@pytest.mark.parametrize("path,pre,post,expected", [
    (ssm.PATH_WATCHER,100,75,"mirrored"),
    (ssm.PATH_SLOT,100,None,"exact_ratio_unproven"),
    (ssm.PATH_ACCOUNT,100,75,"exact_ratio_unproven"),
    (ssm.PATH_WATCHER,None,75,"exact_ratio_unproven"),
    (ssm.PATH_WATCHER,100,70,"exact_ratio_unproven"),
])
def test_solana_requires_signed_receipt_ratio_and_preserves_exact_atoms(exact, monkeypatch,path,pre,post,expected):
    pos={"id":12,"source_user_id":"ton","network_id":"solana","mint":"COIN",
         "is_open":1,"exact_profile":True,"opened_at":"2026-10-20T10:00:00+00:00",
         "derisk_sig":"pending-prior"}
    monkeypatch.setattr(m,"get_open_position",lambda *a,**kw:pos)
    sale=ssm.SourceSell(path=path,signature="source-sig",slot=12,source_user_id="ton",handle="tontheneko",
        owner="OWNER",venue="watcher",mint="COIN",pool=None,sold_atomic=25,pre_atomic=pre,
        post_atomic=post,seen_at="2026-10-20T10:01:00+00:00")
    monkeypatch.setattr(m,"record_exit_trigger_audit",lambda *a,**kw:None)
    mirror=ssm.SourceSellMirror(enabled=True,shadow=False,min_fraction=1)
    rec=mirror._handle(None,{},sale,exit_lock=contextlib.nullcontext(),now_iso_fn=lambda:NOW)
    assert rec["outcome"]==expected
    assert len(exact)==(expected=="mirrored")


def test_generic_partial_routes_full_source_sale_through_existing_close():
    calls=[]
    ctx={"56":{"source_partial_fn":lambda *a:calls.append("partial") or {"status":"derisked"},
               "exit_fn":lambda *a:calls.append("full") or {"status":"closed"}}}
    fn=rr.make_venue_source_partial_exit_fn(ctx)
    pos={"network_id":"56","mint":"0x"+"ab"*20}
    assert fn(None,pos,{"request_json":json.dumps({"source_sold_atomic":"25","source_pre_atomic":"100"})},NOW)["status"]=="derisked"
    assert fn(None,pos,{"request_json":json.dumps({"source_sold_atomic":"100","source_pre_atomic":"100"})},NOW)["status"]=="closed"
    assert calls==["partial","full"]


def test_profile_holds_forever_on_loss_missing_quotes_and_stalled_peaks():
    from test_memecoin_source_mirror_tp100_policy import _contract, _position
    pos={**_position(_contract()), "entry_price":1.0,"opened_at":"2020-01-01T00:00:00+00:00",
        "executable_peak_price":1.9,"executable_peak_at":"2020-01-01T00:00:00+00:00"}
    for price in (None,0,.01,.5,1.99):
        assert rr.alpha_b_real_exit_reason(pos,price,"2026-10-20T12:00:00+00:00") is None
    assert rr.alpha_b_real_exit_reason(pos,2,"2026-10-20T12:00:00+00:00")=="real_tp"


def test_profile_sweep_suppresses_volume_death_even_after_days(monkeypatch):
    from test_memecoin_source_mirror_tp100_policy import _contract, UID as profile_uid
    from test_memecoin_degen_exit_trial_v4 import _conn, _position as insert_position, _sweep
    from sniper.services import memecoin_volume_death as vd
    monkeypatch.setattr(cfg,"MEMECOIN_VOLDEATH_ARMED",1)
    calls=[]
    def observe(*a,**kw):
        calls.append(1)
        return False,None,"test_hold"
    monkeypatch.setattr(vd,"armed_exit_decision",observe)
    c=_conn()
    insert_position(c,json.dumps(_contract()),uid=profile_uid,opened_at="2026-10-20T12:00:00+00:00")
    assert _sweep(c,.01,"2026-10-25T12:00:00+00:00")==[]
    assert calls==[]


@pytest.mark.parametrize("hold",["mirror_disabled","tracking_paused_at"])
def test_queued_full_source_sale_respects_a_new_operator_hold(hold):
    calls=[]
    fn=rr.make_venue_source_partial_exit_fn({"56":{"exit_fn":lambda *a:calls.append(1)}})
    request={"request_json":json.dumps({"source_sold_atomic":"100","source_pre_atomic":"100"})}
    result=fn(None,{"network_id":"56","mint":"0x"+"ab"*20,hold:1},request,NOW)
    assert result["status"]==hold and calls==[]


@pytest.mark.parametrize("minimum",[.3,1.0])
@pytest.mark.parametrize("late",[False,True])
def test_confirmed_exact_sale_survives_an_earlier_processed_hint(exact,monkeypatch,minimum,late):
    from dataclasses import replace
    pos={"id":12,"source_user_id":"ton","network_id":"solana","mint":"COIN",
        "is_open":1,"exact_profile":True,"opened_at":"2026-10-20T10:00:00+00:00"}
    ready=[not late]
    monkeypatch.setattr(m,"get_open_position",lambda *a,**kw:pos if ready[0] else None)
    monkeypatch.setattr(m,"record_exit_trigger_audit",lambda *a,**kw:None)
    sale=ssm.SourceSell(path=ssm.PATH_SLOT,signature="source-sig",slot=12,source_user_id="ton",
        handle="tontheneko",owner="OWNER",venue="curve",mint="COIN",pool=None,sold_atomic=25,
        pre_atomic=None,seen_at="2026-10-20T10:01:00+00:00")
    mirror=ssm.SourceSellMirror(enabled=True,shadow=False,min_fraction=minimum)
    batches=iter([[sale],[replace(sale,path=ssm.PATH_WATCHER,pre_atomic=100,post_atomic=75)],[]])
    monkeypatch.setattr(mirror,"_watcher_rows",lambda conn:next(batches))
    mirror.run(None,{},exit_lock=contextlib.nullcontext(),now_iso_fn=lambda:NOW)
    mirror.run(None,{},exit_lock=contextlib.nullcontext(),now_iso_fn=lambda:NOW)
    if late:
        ready[0]=True
        mirror.run(None,{},exit_lock=contextlib.nullcontext(),now_iso_fn=lambda:NOW)
    assert len(exact)==1


@pytest.mark.parametrize("source_slot,sold_at,want",[(10,"2026-10-20T09:59:50+00:00","mirrored"),
    (13,"2026-10-20T10:01:00+00:00","before_entry")])
def test_source_buy_slot_bounds_solana_mirror_before_our_decision(exact,monkeypatch,source_slot,sold_at,want):
    pos={"id":12,"source_user_id":"ton","network_id":"solana","mint":"COIN","attempt_id":"a",
        "is_open":1,"exact_profile":True,"opened_at":"2026-10-20T10:00:00+00:00"}
    monkeypatch.setattr(m,"get_open_position",lambda *a,**kw:pos)
    monkeypatch.setattr(m,"entry_attempt_source_evidence",lambda *a:{"source_slot":source_slot,
        "source_user_id":"ton","network_id":"solana","mint":"COIN","source_wallet":"OWNER"})
    monkeypatch.setattr(m,"record_exit_trigger_audit",lambda *a,**kw:None)
    sale=ssm.SourceSell(path=ssm.PATH_WATCHER,signature="source-sig",slot=12,source_user_id="ton",
        handle="tontheneko",owner="OWNER",venue="watcher",mint="COIN",pool=None,sold_atomic=25,
        pre_atomic=100,post_atomic=75,seen_at=sold_at)
    mirror=ssm.SourceSellMirror(enabled=True,shadow=False,min_fraction=1)
    rec=mirror._handle(None,{},sale,exit_lock=contextlib.nullcontext(),now_iso_fn=lambda:NOW)
    assert rec["outcome"]==want
    assert len(exact)==(want=="mirrored")


def test_source_adds_change_only_the_next_captured_sell_ratio():
    from test_memecoin_source_partials import setup,request,execute
    c,pid=setup()
    first,atoms=execute(c,pid,request(c,pid,sold="1",pre="4"),atomic=1000000,sig="first")
    # The source added inventory before this next sell; our owned remainder did not increase.
    second,next_atoms=execute(c,pid,request(c,pid,"next",sold="1",pre="8"),atomic=750000,sig="second")
    assert first["status"]==second["status"]=="partial_sold"
    assert atoms==[250000] and next_atoms==[93750]
