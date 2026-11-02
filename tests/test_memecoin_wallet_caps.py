"""Operator ceilings: downward only, chain scoped, cached and common to all lanes."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_wallet_caps as C
from sniper.settings import memecoin as cfg

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)

def row(i, *, uid="u", chain="solana", pnl=5, mint=None, **kw):
    return dict(source_user_id=uid, network_id=chain, mint=mint or f"coin-{i}",
                realized_pnl=pnl, entry_notional=100, entry_fill_source="chain",
                signal_source="follow_activity", closed_recorded_at=NOW.isoformat(), **kw)

@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_WALLET_CAPS_ENABLED", True)

@pytest.mark.parametrize("n,pnl,expected", [(9,5,50),(10,0,50),(10,-1,50),(10,5,175)])
def test_evidence_threshold(n,pnl,expected):
    cache=C.WalletCapCache(clock=lambda: 10)
    cache.publish([row(i,pnl=pnl) for i in range(n)], NOW)
    assert cache.cap("u","solana") == expected

def test_single_coin_and_lucky_outlier_do_not_prove_wallet():
    cache=C.WalletCapCache(clock=lambda: 10)
    cache.publish([row(i,mint="same") for i in range(10)],NOW)
    assert cache.cap("u","solana") == 50
    cache.publish([row(i,pnl=100 if i==0 else -1) for i in range(10)],NOW)
    assert cache.cap("u","solana") == 50

def test_chain_identity_missing_evidence_and_unknown_pnl():
    cache=C.WalletCapCache(clock=lambda: 10)
    cache.publish([row(i) for i in range(10)],NOW)
    assert cache.cap("u","56") == cache.cap(None,"solana") == 50
    rows=[row(i) for i in range(10)]+[row(10,pnl=None)]
    cache.publish(rows,NOW)
    assert cache.cap("u","solana") == 50

@pytest.mark.parametrize("field,value", [("signal_source","manual"),("entry_fill_source","fomo"),
                                       ("closed_recorded_at",None),
                                       ("closed_recorded_at",(NOW+timedelta(seconds=1)).isoformat()),
                                       ("closed_recorded_at",(NOW-timedelta(days=31)).isoformat())])
def test_manual_nonchain_and_unavailable_or_future_close_cannot_prove_wallet(field,value):
    rows=[row(i) for i in range(10)]
    for r in rows: r[field]=value
    cache=C.WalletCapCache(clock=lambda:10);cache.publish(rows,NOW)
    assert cache.cap("u","solana") == 50

def test_stale_cache_failure_and_atomic_snapshot():
    ticks=[10]
    cache=C.WalletCapCache(clock=lambda:ticks[0])
    assert cache.cap("u","solana")==50
    cache.publish([row(i) for i in range(10)],NOW)
    previous=cache.snapshot
    assert cache.cap("u","solana")==175
    cache.publish([row(i,pnl=0) for i in range(10)],NOW)
    assert previous[1][("u","solana")]["proven"] is True
    assert cache.cap("u","solana")==50
    cache.publish([row(i) for i in range(10)],NOW)
    ticks[0]+=181
    assert cache.cap("u","solana")==50
    cache.invalidate("db_error")
    assert cache.cap("u","solana")==50

def test_caps_do_not_set_target_or_bypass_lower_ceiling(monkeypatch):
    monkeypatch.setattr(C,"CACHE",C.WalletCapCache(clock=lambda:10))
    assert C.entry_cap_usd("u","56",existing_cap=20)==20
    assert C.entry_cap_usd("u","56",existing_cap=500)==50
    C.CACHE.publish([row(i,chain="56") for i in range(10)],NOW)
    assert C.entry_cap_usd("u","56")==175
    assert C.entry_cap_usd("u","56",existing_cap=100)==100


@pytest.fixture
def trial_caps(monkeypatch, tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as m

    path = tmp_path / "trial-caps.db"
    writer = sqlite3.connect(path)
    writer.executescript("""
        CREATE TABLE memecoin_trial_arms (
            id INTEGER PRIMARY KEY, source_user_id TEXT, chain TEXT, rule TEXT,
            ended_at TEXT, end_reason TEXT);
        CREATE TABLE memecoin_trial_verdicts (
            trial_id INTEGER, verdict TEXT, action TEXT);
    """)
    ticks = [10.0]
    cache = C.WalletCapCache(clock=lambda: ticks[0])
    monkeypatch.setattr(cache, "refresh_operator_overrides", lambda: True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FULL_CAP_WALLET_IDS", frozenset())
    monkeypatch.setattr(m, "wallet_entry_cap_evidence", lambda conn, **kw: [])

    def refresh():
        def connect():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            return conn
        cache.refresh(connect, lambda: NOW.isoformat())

    def trial(*, uid="selected", chain="solana", rule="tp-two-of-three-v1",
              ended=True, reason="promote", verdict="PROMOTE", action="ended_kept_armed"):
        tid = writer.execute(
            "INSERT INTO memecoin_trial_arms(source_user_id,chain,rule,ended_at,end_reason) "
            "VALUES(?,?,?,?,?)", (uid, chain, rule, NOW.isoformat() if ended else None, reason)).lastrowid
        if verdict is not None:
            writer.execute("INSERT INTO memecoin_trial_verdicts VALUES(?,?,?)", (tid, verdict, action))
        writer.commit()
        return tid

    yield cache, ticks, trial, refresh, writer
    writer.close()


def test_trial_promotion_raises_only_its_wallet_chain_cap(trial_caps, monkeypatch):
    cache, _ticks, trial, refresh, _writer = trial_caps
    trial()
    refresh()
    monkeypatch.setattr(C, "CACHE", cache)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CROSS_WALLET_REENTRY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS", "")
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS", "another")
    assert C.entry_cap_usd("selected", "solana") == 175
    assert C.entry_cap_usd("selected", "solana", ceiling_usd=500) == 175
    assert C.entry_cap_usd("selected", "solana", existing_cap=50) == 50
    assert C.entry_cap_usd("selected", "solana", existing_cap=100) == 100
    assert C.entry_cap_usd("selected", "56") == C.entry_cap_usd("another", "solana") == 50
    assert cache.full_cap_listed("selected", "solana") is False
    assert C.cross_wallet_reentry_closes_by("selected", "solana") is None
    assert C.raised_clip_wallet_allowed("selected", "solana") is False
    assert cache.status()["operator_full_cap_wallet_ids"] == []
    assert cache.status()["proven_cells"] == []
    assert cache.status()["trial_promoted_cells"] == [
        {"source_user_id": "selected", "network_id": "solana"}]


@pytest.mark.parametrize("change", [
    {"rule": "3a0b141ee"}, {"ended": False}, {"reason": "operator_keep_armed"},
    {"reason": "demote"}, {"verdict": None}, {"verdict": "CONTINUE"}, {"action": "stopped"},
])
def test_only_the_exact_terminal_promotion_grants_full_size(trial_caps, change):
    cache, _ticks, trial, refresh, _writer = trial_caps
    trial(**change)
    refresh()
    assert cache.cap("selected", "solana") == 50


@pytest.mark.parametrize("newer", [
    {"ended": False, "verdict": None},
    {"reason": "demote", "verdict": "DEMOTE", "action": "ended_disarmed"},
    {"rule": "3a0b141ee", "reason": "operator_keep_armed", "verdict": None},
])
def test_a_newer_trial_revokes_the_previous_trial_grant(trial_caps, newer):
    cache, _ticks, trial, refresh, _writer = trial_caps
    trial()
    refresh()
    assert cache.cap("selected", "solana") == 175
    previous = cache.snapshot
    trial(**newer)
    refresh()
    assert cache.cap("selected", "solana") == 50
    assert previous[1][("selected", "solana")]["trial_promoted"] is True
    assert cache.status()["trial_promoted_cells"] == []


def test_a_verdict_on_another_trial_cannot_complete_an_unfinished_promotion(trial_caps):
    cache, _ticks, trial, refresh, _writer = trial_caps
    trial(uid="another")
    trial(verdict=None)
    refresh()
    assert cache.cap("selected", "solana") == 50
    assert cache.cap("another", "solana") == 175


def test_trial_grant_expires_or_fails_closed_with_its_shared_snapshot(trial_caps):
    cache, ticks, trial, refresh, writer = trial_caps
    trial()
    refresh()
    assert cache.cap("selected", "solana") == 175
    ticks[0] += C.MAX_AGE_S + 1
    assert cache.cap("selected", "solana") == 50
    assert cache.status()["trial_promoted_cells"] == []
    refresh()
    assert cache.cap("selected", "solana") == 175
    writer.execute("DROP TABLE memecoin_trial_verdicts")
    writer.commit()
    refresh()
    assert cache.cap("selected", "solana") == 50
    assert cache.status()["state"] == "refresh_failed"


@pytest.mark.parametrize("chain", ["solana", "56", "4663", "8453", "1", "5042", "57073"])
@pytest.mark.parametrize("state", ["missing", "ready", "stale", "failed"])
def test_operator_full_cap_is_exact_wallet_identity_and_survives_unknown_evidence(
        monkeypatch, chain, state):
    ticks = [10]
    cache = C.WalletCapCache(clock=lambda: ticks[0])
    monkeypatch.setattr(C, "CACHE", cache)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FULL_CAP_WALLET_IDS", frozenset({"selected"}), raising=False)
    if state in {"ready", "stale"}:
        cache.publish([row(0, uid="selected", chain=chain, pnl=0)], NOW)
    if state == "stale":
        ticks[0] += 181
    if state == "failed":
        cache.invalidate("refresh_failed")
    assert C.entry_cap_usd("selected", chain) == 175
    assert C.entry_cap_usd("selected", chain, existing_cap=20) == 20
    assert C.entry_cap_usd("selected", chain, existing_cap=100) == 100
    assert C.entry_cap_usd("selected", chain, existing_cap=500) == 175
    assert C.entry_cap_usd("selected-other", chain) == 50
    assert C.entry_cap_usd(None, chain) == 50
    assert C.entry_cap_usd("", chain) == 50


def test_operator_cap_status_is_separate_from_percentage_qualification_and_is_revocable(monkeypatch):
    cache = C.WalletCapCache(clock=lambda: 10)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FULL_CAP_WALLET_IDS", frozenset({"selected"}), raising=False)
    cache.publish([row(0, uid="selected", pnl=0)], NOW)
    assert cache.status().get("operator_full_cap_wallet_ids") == ["selected"]
    assert cache.status()["proven_cells"] == []
    assert cache.cap("selected", "solana") == 175
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FULL_CAP_WALLET_IDS", frozenset())
    assert cache.cap("selected", "solana") == 50


@pytest.mark.parametrize("chain", ["56", "4663"])
def test_operator_full_cap_can_be_scoped_to_the_winning_chain(monkeypatch, tmp_path, chain):
    cache = C.WalletCapCache(clock=lambda: 10)
    monkeypatch.setattr(C, "CACHE", cache)
    (tmp_path / ".env").write_text(f"MEMECOIN_REAL_FULL_CAP_WALLET_IDS=selected@{chain}\n")
    assert cache.refresh_operator_overrides(root=tmp_path)
    assert C.entry_cap_usd("selected", chain) == 175
    assert C.entry_cap_usd("selected", chain, existing_cap=100) == 100
    for other in {"solana", "56", "4663", "8453"} - {chain}:
        assert C.entry_cap_usd("selected", other) == 50
    assert C.entry_cap_usd("selected-other", chain) == 50
    assert C.entry_cap_usd(None, chain) == 50


def test_operator_overrides_reload_without_restart_or_global_env_mutation(monkeypatch, tmp_path):
    import os
    key = "MEMECOIN_REAL_FULL_CAP_WALLET_IDS"
    monkeypatch.setattr(cfg, key, frozenset())
    monkeypatch.setenv(key, "shell-value")
    ticks = [10]
    cache = C.WalletCapCache(clock=lambda: ticks[0])
    (tmp_path / ".env").write_text(key + "='selected, second,selected'\n")
    (tmp_path / ".fomo.env").write_text(key + "=later-file\n")
    assert cache.cap("selected", "solana") == 50
    assert cache.refresh_operator_overrides(root=tmp_path)
    assert cache.cap("selected", "solana") == cache.cap("second", "56") == 175
    assert cache.cap("later-file", "56") == 50
    previous = cache.operator_snapshot
    ticks[0] += 30
    assert cache.status()["operator_config_age_s"] == 30
    (tmp_path / ".env").write_text(key + "=\n")
    assert cache.refresh_operator_overrides(root=tmp_path)
    assert cache.cap("selected", "solana") == 50
    assert cache.status()["operator_config_age_s"] == 0
    assert previous[0] == frozenset({"selected", "second"})
    assert cfg.MEMECOIN_REAL_FULL_CAP_WALLET_IDS == frozenset()
    assert os.environ[key] == "shell-value"


@pytest.mark.parametrize("failure", ["missing", "permission", "encoding"])
def test_operator_file_failure_keeps_last_authorized_value(monkeypatch, tmp_path, failure):
    from sniper.util import envfiles
    cache = C.WalletCapCache(clock=lambda: 10)
    path = tmp_path / ".env"
    path.write_text("MEMECOIN_REAL_FULL_CAP_WALLET_IDS=selected\n")
    assert cache.refresh_operator_overrides(root=tmp_path)
    if failure == "missing":
        path.unlink()
    elif failure == "encoding":
        path.write_bytes(b"\xff")
    else:
        def denied(*args, **kwargs):
            raise PermissionError("fixture")
        monkeypatch.setattr(envfiles, "load_env_files", denied)
    assert not cache.refresh_operator_overrides(root=tmp_path)
    assert cache.cap("selected", "solana") == 175
    assert cache.cap("other", "solana") == 50
    assert cache.status()["operator_config_state"] == "refresh_failed"


def test_operator_refresh_is_independent_of_failed_performance_read(monkeypatch, tmp_path):
    from sniper.util import state_paths
    monkeypatch.setattr(state_paths, "state_root", lambda: tmp_path)
    (tmp_path / ".env").write_text("MEMECOIN_REAL_FULL_CAP_WALLET_IDS=selected\n")
    cache = C.WalletCapCache(clock=lambda: 10)
    def db_down():
        raise RuntimeError("fixture database unavailable")
    cache.refresh(db_down, lambda: NOW.isoformat())
    assert cache.cap("selected", "solana") == 175
    assert cache.cap("other", "solana") == 50
    assert cache.status()["operator_config_state"] == "ready"
    assert cache.status()["state"] == "refresh_failed"


def test_cap_lookup_never_reloads_operator_files(monkeypatch, tmp_path):
    from sniper.util import envfiles
    cache = C.WalletCapCache(clock=lambda: 10)
    monkeypatch.setattr(C, "CACHE", cache)
    (tmp_path / ".env").write_text("MEMECOIN_REAL_FULL_CAP_WALLET_IDS=selected\n")
    assert cache.refresh_operator_overrides(root=tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("entry must not read configuration files")
    monkeypatch.setattr(envfiles, "load_env_files", forbidden)
    assert C.entry_cap_usd("selected", "solana", existing_cap=20) == 20
    assert C.entry_cap_usd("selected", "56") == 175
    assert C.entry_cap_usd("other", "56") == 50

@pytest.mark.parametrize("chain",["solana","56","4663","8453","1","5042","57073"])
@pytest.mark.parametrize("computed",[12,500])
def test_context_common_hard_ceiling_and_lower_amounts(monkeypatch,chain,computed):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_risk as S, memecoin_evm_risk as E
    conn=sqlite3.connect(":memory:");conn.row_factory=sqlite3.Row;m.ensure_memecoin_tables(conn)
    if chain=="solana":
        ctx=S.build_memecoin_risk_context(conn,usdc_balance=1000,sol_balance=10,day="2026-10-04",
             size_override=computed,override_risk_caps=True)
    else:
        ctx=E.build_evm_risk_context(conn,usdc_balance=1000,native_balance=10,day="2026-10-04",
             network_id=chain,size_override=computed,override_risk_caps=True)
    assert ctx.trade_size_usd==min(computed,175)

def test_sol_funding_is_derived_after_wallet_cap():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_risk as S
    conn=sqlite3.connect(":memory:");conn.row_factory=sqlite3.Row;m.ensure_memecoin_tables(conn)
    ctx=S.build_memecoin_risk_context(conn,usdc_balance=1000,sol_balance=10,day="2026-10-04",
        size_override=500,size_cap_usd=C.entry_cap_usd("unknown","solana"),
        funding_asset="sol",funding_sol_usd=100,sol_buffer_cap_usd=175)
    assert ctx.trade_size_usd==50
    assert ctx.funding_amount_atomic==500_000_000

@pytest.mark.parametrize("chain",["solana","56","4663","8453","1","5042","57073"])
@pytest.mark.parametrize("proven",[False,True])
@pytest.mark.parametrize("source",["follow_activity","slot_lane","rh_fast_lane","bsc_fast_lane","manual"])
def test_every_reservation_path_enforces_wallet_ceiling(monkeypatch,chain,proven,source):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_risk as S, memecoin_evm_risk as E
    conn=sqlite3.connect(":memory:");conn.row_factory=sqlite3.Row;m.ensure_memecoin_tables(conn)
    cache=C.WalletCapCache(clock=lambda:10)
    cache.publish([row(i,chain=chain) for i in range(10)] if proven else [],NOW)
    monkeypatch.setattr(C,"CACHE",cache)
    monkeypatch.setattr(cfg,"MEMECOIN_REAL_ENABLED",True)
    monkeypatch.setattr(cfg,"MEMECOIN_REAL_SIZING","fixed")
    monkeypatch.setattr(cfg,"MEMECOIN_REAL_SIZING_BY_CHAIN",{})
    monkeypatch.setattr(cfg,"MEMECOIN_TRADE_SIZE_USD",500)
    monkeypatch.setattr(cfg,"MEMECOIN_EVM_MAX_CLIP_USD",0)
    monkeypatch.setattr(cfg,"MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN",{})
    monkeypatch.setattr(cfg,"evm_fixed_clip_usd_for",lambda _chain: 0)
    monkeypatch.setattr(cfg,"evm_gas_natives_unset",lambda _chain: ())
    args=dict(mint="Mint" if chain=="solana" else "0x1111111111111111111111111111111111111111",
              source_user_id="u",strategy="alpha_b",day="2026-10-04",usdc_balance=1000,
              now_iso=NOW.isoformat(),signal_source=source,override_risk_caps=True,
              enforce_concurrency=source!="manual")
    if chain=="solana":
        decision,pid=S.reserve_entry(conn,sol_balance=10,**args)
    else:
        decision,pid=E.reserve_entry_evm(conn,network_id=chain,native_balance=10,**args)
    assert decision.allowed,decision.reason
    assert decision.approved_size_usdc==(175 if proven else 50)
    assert m.pending_row(conn,pid)["reserved_notional_usd"]==(175 if proven else 50)


def test_wallet_cap_slot_lane_uses_native_funding_at_capped_amount(tmp_path,monkeypatch):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from test_slot_lane_scaled_sol import _lane,_buy
    conn=sqlite3.connect(":memory:");conn.row_factory=sqlite3.Row;m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(C,"CACHE",C.WalletCapCache(clock=lambda:10))
    ln,rec=_lane(conn,tmp_path,monkeypatch,leg_scale=1,coin_scale=1,sol=2,shadow=100)
    result=ln.handle(_buy())
    assert result["status"]=="filled",result
    assert rec["reserve"]==["sol"]
    assert rec["built"]==[(50,500_000_000)]
    assert rec["sent"][0]["reserved_notional_usd"]==50


def test_wallet_cap_cash_leg_is_warm_without_replacing_full_and_regime_legs(monkeypatch):
    from sniper.execution.pump_curve_buy import refresh_cash_legs,matching_cash_leg
    calls=[]
    builder=SimpleNamespace(_leg=None,_cash_legs=(),clock=lambda:10,refresh_after_s=5,max_leg_age_s=30,
            leg_slippage_bps=100,user="u",jup_call=None,rpc=None,_alt_cache={})
    def quote(atoms,*args,**kwargs):
        calls.append(atoms)
        return dict(usdc_atomic=atoms,fetched_at=10)
    refresh_cash_legs(builder,175,131.25,extra_clip_usds=(50,),prefetch_fn=quote)
    assert calls==[175_000_000,131_250_000,50_000_000]
    assert matching_cash_leg(builder,50)["usdc_atomic"]==50_000_000
    refresh_cash_legs(builder,175,131.25,extra_clip_usds=(50,),prefetch_fn=quote)
    assert len(calls)==3


def test_failed_background_read_invalidates_larger_caps_and_closes_handle():
    cache=C.WalletCapCache(clock=lambda:10);cache.publish([row(i) for i in range(10)],NOW)
    closed=[]
    class Broken:
        def set_progress_handler(self,*args):pass
        def execute(self,*args):raise RuntimeError("timeout")
        def close(self):closed.append(True)
    cache.refresh(Broken,lambda:NOW.isoformat())
    assert cache.cap("u","solana")==50
    assert closed==[True]

@pytest.mark.parametrize("large_pnl,small_pnl,expected",[(500,-1,50),(-500,1,175)])
def test_percentage_performance_ignores_dollar_profit_from_unequal_bets(large_pnl,small_pnl,expected):
    rows=[row(i,pnl=small_pnl) for i in range(9)]
    for r in rows:r['entry_notional']=10
    large=row(9,pnl=large_pnl);large['entry_notional']=10000
    rows.append(large)
    cache=C.WalletCapCache(clock=lambda:10);cache.publish(rows,NOW)
    assert cache.cap('u','solana')==expected


def test_repeated_trades_on_one_coin_cannot_dominate_percentage_average():
    rows=[row(i,pnl=-1) for i in range(10)]
    rows += [row(i,mint='winner',pnl=10) for i in range(10,110)]
    cache=C.WalletCapCache(clock=lambda:10);cache.publish(rows,NOW)
    assert cache.cap('u','solana')==50
    assert cache.snapshot[1][('u','solana')]['mean_return_pct']==pytest.approx(0)

@pytest.mark.parametrize('cap',[float('nan'),float('inf'),0,-1])
def test_malformed_smaller_cap_is_not_silently_replaced_with_wallet_ceiling(cap):
    with pytest.raises(ValueError):C.entry_cap_usd('u','56',existing_cap=cap)


def test_rh_detector_uses_capped_amount(monkeypatch,tmp_path):
    from sniper.services import memecoin_rh_fast_decide as D
    from fractions import Fraction
    seen=[]
    monkeypatch.setattr(C,'CACHE',C.WalletCapCache(clock=lambda:10))
    def decide(*args,**kwargs):
        seen.append(kwargs['clip_usd'])
        return dict(reason='test',cap_price=Fraction(1))
    monkeypatch.setattr(D,'decide',decide)
    # The post-decision evidence writer is deliberately incomplete; it cannot affect this check.
    D.handle_signal(dict(tx_hash='0x1',seq=1,wallet='0x1',mint='0x2',source_user_id='u'),
                    0,tape=SimpleNamespace(rank=lambda *args:None),live={'clip_usd':175},
                    db_path=str(tmp_path/'decisions.db'),now_fn=lambda:1)
    assert seen==[50]

@pytest.mark.parametrize('chain',['solana','56','4663','8453','5042','57073'])
def test_frozen_contract_seals_capped_size(monkeypatch,chain):
    import json,sqlite3
    from test_memecoin_funded_contract_storage import _classification
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_risk as S,memecoin_evm_risk as E
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;m.ensure_memecoin_tables(conn)
    monkeypatch.setattr(C,'CACHE',C.WalletCapCache(clock=lambda:10))
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_ENABLED',True)
    mint='MINT' if chain=='solana' else '0x1111111111111111111111111111111111111111'
    m.start_entry_attempt(conn,attempt_id='attempt',decision_started_at=NOW.isoformat(),network_id=chain,mint=mint)
    args=dict(mint=mint,source_user_id='u',strategy='alpha_b',day='2026-10-04',usdc_balance=1000,
        now_iso=NOW.isoformat(),signal_source='follow_activity',size_override=500,attempt_id='attempt',
        override_risk_caps=True,classification=_classification(network_id=chain,mint=mint,risk_scale=1,
            signal_source='follow_activity'))
    decision,pid=(S.reserve_entry(conn,sol_balance=10,**args) if chain=='solana' else
                  E.reserve_entry_evm(conn,network_id=chain,native_balance=10,**args))
    assert decision.allowed,decision.reason
    pending=m.pending_row(conn,pid)
    contract=json.loads(pending['funded_contract_json'])
    assert contract['sizing']['approved_clip_usd']==50
    attempt=m.entry_attempt_by_id(conn,'attempt')
    assert attempt['funded_contract_json']==pending['funded_contract_json']


def test_rh_preflight_uses_wallet_cap_before_float_check(monkeypatch):
    from sniper.services import memecoin_rh_fast_lane as L
    from test_memecoin_rh_fast_entry import _payload
    monkeypatch.setattr(C,'CACHE',C.WalletCapCache(clock=lambda:10))
    checks=L._LaneChecks()
    for key,value in dict(clip_usd=175,require_exact_state=False,taker='0x'+'1'*40,
                         generation_fn=lambda:0,low_wei=0,max_snapshot_age_s=60,
                         eth_usd_max_age_s=60,generation_margin_s=0).items():setattr(checks,key,value)
    got=[]
    monkeypatch.setattr(L,'inputs_reason',lambda payload,**kwargs:got.append(kwargs['clip_wei']))
    # Stop immediately after the amount and float validation; later safety checks remain intact.
    class Seen(Exception):pass
    def stop(payload,**kwargs):got.append(kwargs['clip_wei']);raise Seen
    monkeypatch.setattr(L,'inputs_reason',stop)
    with pytest.raises(Seen):checks._precheck(_payload(),{},__import__('time').time(),None,None)
    assert got==[L.clip_wei_for(50,2500)]
