import json
import pytest
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import memecoin_desk_ps_metadata_worker as W  # noqa: E402
from sniper.services import memecoin_desk_ps_metadata as MD  # noqa: E402

WSOL = W.WSOL


def tape(path, pools, *, without_rowid=False):
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE swaps_ps(pool TEXT, mint TEXT, quote_mint TEXT, map_status TEXT, map_known_at REAL, received_at REAL, signature TEXT NOT NULL, ordinal INTEGER NOT NULL" + (", PRIMARY KEY(signature,ordinal)) WITHOUT ROWID" if without_rowid else ")"))
    c.execute("CREATE INDEX swaps_ps_received ON swaps_ps(received_at)")
    for pool, mint, q, status, known in pools:
        c.execute("INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?)", (pool, mint, q, status, known, known, pool, 0))
    c.commit()
    c.close()


def mint_result(dec=6, supply="1000000000000000", slot=500):
    return {"context": {"slot": slot}, "value": {"owner": MD.OWNER_OF["spl-token"], "data": {"program": "spl-token", "parsed": {
        "type": "mint", "info": {"decimals": dec, "supply": supply, "mintAuthority": None, "freezeAuthority": None}}}}}


class Rpc:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, params, gate=None):
        if gate is not None and not gate():
            return None, "budget_exhausted"
        self.calls.append((method, params))
        return self.replies.pop(0)


def setup(tmp, pools, t0=1000.0):
    tape(tmp / "tape.db", pools)
    c = MD.connect(tmp / "m.db")
    W.set_state(c, "read_not_before", t0)                              # the deploy marker
    c.close()
    clock = [t0]
    return clock, (lambda: clock[0])


def test_parse_rejects_partial_and_malformed():
    assert MD.parse_mint_account({"value": None, "context": {"slot": 1}}) == (None, "account_missing")
    bad = mint_result()
    bad["value"]["data"]["parsed"]["info"]["supply"] = "12x"
    assert MD.parse_mint_account(bad)[1] == "malformed"
    bad = mint_result()
    del bad["value"]["data"]["parsed"]["info"]["decimals"]
    assert MD.parse_mint_account(bad)[1] == "malformed"
    bad = mint_result(dec=True)
    assert MD.parse_mint_account(bad)[1] == "malformed"
    row, err = MD.parse_mint_account(mint_result())
    assert err is None and row["decimals"] == 6 and row["supply_atomic"] == "1000000000000000" and row["slot"] == 500


def test_one_read_write_once_same_response_and_receipt_time(tmp_path):
    clock, now = setup(tmp_path, [("P1", "M1", WSOL, "mapped", 1500.0), ("P1b", "M1", WSOL, "mapped", 1600.0)], t0=1000.0)
    # first start at t=1000 sets the marker; mapping at 1500 is AFTER it
    rpc = Rpc([(mint_result(), None)])
    clock[0] = 2000.0
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)
    assert s["read"] == 1 and len(rpc.calls) == 1                      # ONE read per mint despite two pools
    conn = MD.connect(tmp_path / "m.db")
    assert conn.execute("SELECT decimals, supply_atomic, slot, metadata_known_at FROM mints").fetchall() == [(6, "1000000000000000", 500, 2000.0)]
    W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)
    assert len(rpc.calls) == 1                                          # write-once: never re-read


def test_pre_deploy_pools_never_read(tmp_path):
    clock, now = setup(tmp_path, [("P0", "M0", WSOL, "mapped", 500.0)], t0=1000.0)
    rpc = Rpc([])
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)
    assert rpc.calls == [] and s["read"] == 0 and s["pre_deploy_unread"] is None and s["pre_deploy_unread_observed_lower_bound"] == 1


def test_quote_mint_unmapped_and_non_wsol_skipped(tmp_path):
    clock, now = setup(tmp_path, [("P1", "M1", "USDC", "mapped", 1500.0), ("P2", "M2", WSOL, "unmapped", 1500.0)], t0=1000.0)
    rpc = Rpc([])
    W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)
    clock[0] = 2000.0
    W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)
    assert rpc.calls == []


def test_first_429_stops_writes_gap_unknown_not_zero_and_stop_survives_restart(tmp_path):
    clock, now = setup(tmp_path, [("P1", "M1", WSOL, "mapped", 1500.0), ("P2", "M2", WSOL, "mapped", 1501.0)], t0=1000.0)
    clock[0] = 2000.0
    rpc = Rpc([(None, "rate_limited")])
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)
    assert s["stopped"] == "rate_limited" and len(rpc.calls) == 1       # M2 never tried
    conn = MD.connect(tmp_path / "m.db")
    assert conn.execute("SELECT COUNT(*) FROM mints").fetchone()[0] == 0   # unknown, not a zero row
    assert conn.execute("SELECT kind, key, error FROM failures").fetchall() == [("mint", "M1", "rate_limited")]
    assert conn.execute("SELECT cause FROM gaps WHERE cause LIKE 'stopped:%'").fetchall() == [("stopped:rate_limited",)]
    clock[0] = 2100.0                                                    # "restart" inside the cool-down
    rpc2 = Rpc([])
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc2, now_fn=now)
    assert s["stopped"] == "cool_down" and rpc2.calls == []
    clock[0] = 2000.0 + W.STOP_429_S + 1
    rpc3 = Rpc([(mint_result(), None), (mint_result(dec=9), None)])
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc3, now_fn=now)
    assert s["read"] == 2


def test_malformed_answer_is_unknown_and_stops(tmp_path):
    clock, now = setup(tmp_path, [("P1", "M1", WSOL, "mapped", 1500.0)], t0=1000.0)
    clock[0] = 2000.0
    bad = mint_result()
    bad["value"]["data"]["parsed"]["info"]["supply"] = None
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", Rpc([(bad, None)]), now_fn=now)
    assert s["stopped"] == "malformed"
    assert MD.connect(tmp_path / "m.db").execute("SELECT COUNT(*) FROM mints").fetchone()[0] == 0


def test_caps_persist_across_restart(tmp_path, monkeypatch):
    pools = [("P%d" % i, "M%d" % i, WSOL, "mapped", 1500.0 + i) for i in range(5)]
    clock, now = setup(tmp_path, pools, t0=1000.0)
    monkeypatch.setattr(W, "PER_MIN", 2)
    clock[0] = 2000.0
    rpc = Rpc([(mint_result(), None)] * 5)
    assert W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)["read"] == 2
    assert W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)["read"] == 0   # "restart": still capped
    assert len(rpc.calls) == 2
    clock[0] = 2061.0
    assert W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)["read"] == 2


def test_day_cap(tmp_path, monkeypatch):
    clock, now = setup(tmp_path, [("P%d" % i, "M%d" % i, WSOL, "mapped", 1500.0 + i) for i in range(4)], t0=1000.0)
    monkeypatch.setattr(W, "PER_DAY", 1)
    clock[0] = 2000.0
    rpc = Rpc([(mint_result(), None)] * 4)
    assert W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)["read"] == 1


def test_pool_account_default_off_and_on(tmp_path):
    clock, now = setup(tmp_path, [("P1", "M1", WSOL, "mapped", 1500.0)], t0=1000.0)
    clock[0] = 2000.0
    rpc = Rpc([(mint_result(), None)])
    W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)
    assert len(rpc.calls) == 1                                          # default: mint only
    clock[0] = 3000.0
    clock2, now2 = setup(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, [("P1", "M1", WSOL, "mapped", 1500.0)], t0=1000.0)
    clock2[0] = 2000.0
    acct = {"context": {"slot": 7}, "value": {"owner": "pAMM", "lamports": 5, "data": ["AAEC", "base64"]}}
    rpc2 = Rpc([(mint_result(), None), (acct, None)])
    W.run_once(tmp_path / "x" / "tape.db", tmp_path / "x" / "m.db", rpc2, now_fn=now2, pool_account=True)
    assert len(rpc2.calls) == 2
    assert MD.connect(tmp_path / "x" / "m.db").execute("SELECT pool, data_b64, slot FROM pool_accounts").fetchall() == [("P1", "AAEC", 7)]


def test_reader_is_causal(tmp_path):
    conn = MD.connect(tmp_path / "m.db")
    conn.execute("INSERT INTO mints VALUES('M1',6,'9','spl-token',NULL,NULL,2000.0,5,?,NULL)", (MD.SOURCE,))
    conn.commit()
    assert MD.read_metadata(conn, "M1", as_of=1999.9) is None           # read not yet received: unknown
    assert MD.read_metadata(conn, "M1", as_of=2000.0) is None          # equality excluded: received_at < as_of strictly
    assert MD.read_metadata(conn, "M1", as_of=2000.1)["decimals"] == 6
    assert MD.read_metadata(conn, "nope", as_of=9999.0) is None


def test_tape_opened_read_only(tmp_path):
    tape(tmp_path / "tape.db", [])
    conn = W.ro(tmp_path / "tape.db")
    try:
        conn.execute("INSERT INTO swaps_ps VALUES('a','b','c','d',1,1,'a',0)")
        assert False, "write succeeded"
    except sqlite3.OperationalError:
        pass
    assert W.ro(tmp_path / "missing.db") is None


def test_disabled_by_default_exits_without_touching_anything(monkeypatch, capsys):
    monkeypatch.delenv("MEMECOIN_DESK_PS_METADATA_ENABLED", raising=False)
    assert W.main(["--once"], rpc=Rpc([])) == 0
    assert "exiting" in capsys.readouterr().out


def test_no_runner_imports():
    src = Path(W.__file__).read_text()
    assert "memecoin_runner" not in src and "executor" not in src


def test_write_once_even_if_candidate_race(tmp_path, monkeypatch):
    clock, now = setup(tmp_path, [("P1", "M1", WSOL, "mapped", 1500.0)], t0=1000.0)
    conn = MD.connect(tmp_path / "m.db")
    conn.execute("INSERT INTO mints VALUES('M1',6,'111','spl-token',NULL,NULL,1700.0,5,?,NULL)", (MD.SOURCE,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(W, "candidates", lambda *a: [("M1", "P1", 1500.0)])       # another writer won the race
    clock[0] = 2000.0
    W.run_once(tmp_path / "tape.db", tmp_path / "m.db", Rpc([(mint_result(dec=9, supply="222"), None)]), now_fn=now)
    assert MD.connect(tmp_path / "m.db").execute("SELECT decimals, supply_atomic, metadata_known_at FROM mints").fetchall() == [(6, "111", 1700.0)]


def test_trust_boundary_slot_owner_uint64_canonical():
    def with_(**kw):
        r = mint_result()
        for k, v in kw.items():
            if k == "slot":
                r["context"] = {"slot": v} if v != "gone" else {}
            elif k == "owner":
                r["value"]["owner"] = v
            elif k == "supply":
                r["value"]["data"]["parsed"]["info"]["supply"] = v
            elif k == "program":
                r["value"]["data"]["program"] = v
        return MD.parse_mint_account(r)[1]
    assert with_(slot="gone") == "malformed" and with_(slot=None) == "malformed" and with_(slot=-1) == "malformed"
    assert with_(owner="SomeoneElse") == "owner_mismatch"
    assert with_(program="spl-token-2022") == "owner_mismatch"            # parse names 2022, owner is classic
    assert with_(supply=str(2**64)) == "malformed" and with_(supply="007") == "malformed" and with_(supply="-1") == "malformed" and with_(supply="1\n") == "malformed"
    assert with_(supply=str(2**64 - 1)) is None and with_(supply="0") is None


def test_read_pool_facts_composite_own_clocks_strict(tmp_path):
    conn = MD.connect(tmp_path / "m.db")
    conn.execute("INSERT INTO mints VALUES('M1',6,'9','spl-token',NULL,NULL,2000.0,5,?,NULL)", (MD.SOURCE,))
    conn.execute("INSERT INTO pool_accounts VALUES('P1','pAMM',1,'AAEC',2500.0,9,'s')")
    conn.commit()
    f = MD.read_pool_facts(conn, mint="M1", pool="P1", as_of=2001.0)
    assert f["mint"]["metadata_known_at"] == 2000.0 and f["pool_account"] is None   # pool read not yet received
    f = MD.read_pool_facts(conn, mint="M1", pool="P1", as_of=2500.0)
    assert f["pool_account"] is None                                                # equality excluded
    f = MD.read_pool_facts(conn, mint="M1", pool="P1", as_of=2600.0)
    assert f["pool_account"]["metadata_known_at"] == 2500.0 and f["pool_account"]["metadata_slot"] == 9
    assert MD.read_pool_facts(conn, mint="X", pool="Y", as_of=9e9) == {"mint": None, "pool_account": None}


def test_cached_mint_pool_read_not_lost_and_independent(tmp_path):
    clock, now = setup(tmp_path, [("P1", "M1", WSOL, "mapped", 1500.0), ("P2", "M1", WSOL, "mapped", 1501.0)], t0=1000.0)
    conn = MD.connect(tmp_path / "m.db")
    conn.execute("INSERT INTO mints VALUES('M1',6,'9','spl-token',NULL,NULL,1600.0,5,?,NULL)", (MD.SOURCE,))
    conn.commit()
    conn.close()
    clock[0] = 2000.0
    acct = {"context": {"slot": 7}, "value": {"owner": "pAMM", "lamports": 5, "data": ["AAEC", "base64"]}}
    rpc = Rpc([(acct, None), (acct, None)])
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now, pool_account=True)
    assert s["read"] == 0 and s["pool_read"] == 2                        # mint cached, BOTH pools (same mint) still read
    assert [c[1][0] for c in rpc.calls] == ["P1", "P2"]


def test_pool_read_survives_minute_cap(tmp_path, monkeypatch):
    clock, now = setup(tmp_path, [("P1", "M1", WSOL, "mapped", 1500.0)], t0=1000.0)
    monkeypatch.setattr(W, "PER_MIN", 1)
    clock[0] = 2000.0
    acct = {"context": {"slot": 7}, "value": {"owner": "pAMM", "lamports": 5, "data": ["AAEC", "base64"]}}
    rpc = Rpc([(mint_result(), None), (acct, None)])
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now, pool_account=True)
    assert s["read"] == 1 and s["pool_read"] == 0 and s["skipped_budget"] == 1
    clock[0] = 2061.0
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now, pool_account=True)
    assert s["pool_read"] == 1 and len(rpc.calls) == 2                   # pending pool read picked up next minute


def test_budget_checked_after_pacer_delay_not_before(tmp_path, monkeypatch):
    clock, now = setup(tmp_path, [("P%d" % i, "M%d" % i, WSOL, "mapped", 1500.0 + i) for i in range(2)], t0=1000.0)
    monkeypatch.setattr(W, "PER_MIN", 1)
    clock[0] = 2000.0

    class Delayed(Rpc):
        def __call__(self, method, params, gate=None):
            clock[0] += 0.5                                              # the pacer sleep happens BEFORE the gate
            return super().__call__(method, params, gate)
    rpc = Delayed([(mint_result(), None), (mint_result(), None)])
    s = W.run_once(tmp_path / "tape.db", tmp_path / "m.db", rpc, now_fn=now)
    assert len(rpc.calls) == 1 and s["skipped_budget"] == 1


def test_jsonrpc_429_pauses_shared_pacer(monkeypatch):
    paused = []

    class Pacer:
        def acquire(self): pass
        def pause(self, s): paused.append(s)

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"jsonrpc": "2.0", "error": {"code": 429, "message": "rate"}}).encode()
    monkeypatch.setattr(W.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert W.Rpc("http://x", Pacer())("getAccountInfo", []) == (None, "rate_limited") and paused == [60.0]


def test_real_rpc_gate_runs_after_acquire_and_blocks_send(monkeypatch):
    order = []

    class Pacer:
        def acquire(self): order.append("acquire")
        def pause(self, s): pass
    monkeypatch.setattr(W.urllib.request, "urlopen", lambda *a, **k: order.append("SEND"))
    assert W.Rpc("http://x", Pacer())("getAccountInfo", [], lambda: order.append("gate") or False) == (None, "budget_exhausted")
    assert order == ["acquire", "gate"]


def test_metadata_scan_uses_received_range_and_durable_pending(tmp_path):
    clock,now=setup(tmp_path, [('old','old',WSOL,'mapped',1.),('P1','M1',WSOL,'mapped',1900.)])
    clock[0]=2000.
    source=W.ro(tmp_path/'tape.db');meta=MD.connect(tmp_path/'m.db')
    W.refresh_mappings(source,meta,now=now())
    plan=source.execute('EXPLAIN QUERY PLAN '+W.SCAN_SQL,(0.,now(),0.,'',-1,10)).fetchall()
    assert any('SEARCH' in r[3] and 'swaps_ps_received' in r[3] and 'received_at>' in r[3] and 'received_at<' in r[3] for r in plan)
    assert W.candidates(source,meta,1000.)==[('M1','P1',1900.)]
    # A restart reads the persisted candidate even when its source row ages out.
    source.close();meta.close();clock[0]=10000.
    rpc=Rpc([(mint_result(),None)])
    assert W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now)['read']==1


@pytest.mark.parametrize("without_rowid",[False,True])
def test_cursor_pages_equal_times_and_late_append_without_skipping(tmp_path,monkeypatch,without_rowid):
    clock,now=setup(tmp_path, [('P1','M1',WSOL,'mapped',1900.),('P2','M2',WSOL,'mapped',1900.),('P3','M3',WSOL,'mapped',1900.)])
    if without_rowid:
        (tmp_path/'tape.db').unlink()
        tape(tmp_path/'tape.db', [('P1','M1',WSOL,'mapped',1900.),('P2','M2',WSOL,'mapped',1900.),('P3','M3',WSOL,'mapped',1900.)],without_rowid=True)
    clock[0]=2000.;monkeypatch.setattr(W,'SCAN_BATCH',2)
    source=W.ro(tmp_path/'tape.db');meta=MD.connect(tmp_path/'m.db')
    plan=source.execute('EXPLAIN QUERY PLAN '+W.SCAN_SQL,(0.,now(),0.,'',-1,2)).fetchall()
    assert any('SEARCH' in r[3] and 'swaps_ps_received' in r[3] and 'received_at>' in r[3] and 'received_at<' in r[3] for r in plan)
    assert W.refresh_mappings(source,meta,now=now())==2
    assert W.refresh_mappings(source,meta,now=now())==1
    writer=sqlite3.connect(tmp_path/'tape.db');writer.execute('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?)',('P4','M4',WSOL,'mapped',1885.,1985.,'P4',0));writer.commit();writer.close()
    traced=[];source.set_trace_callback(traced.append)
    assert W.refresh_mappings(source,meta,now=now())==1
    assert any('received_at>=1970.0' in sql for sql in traced if 'FROM swaps_ps' in sql)
    assert len(W.candidates(source,meta,1000.))==4
    source.close();meta.close()


def test_bootstrap_window_unknown_history_and_missing_index_no_rpc(tmp_path):
    clock,now=setup(tmp_path,[('ancient','old',WSOL,'mapped',500.)]);clock[0]=10000.
    out=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',Rpc([]),now_fn=now)
    assert out['pre_deploy_unread'] is None and out['pre_deploy_unread_observed_lower_bound']==0
    writer=sqlite3.connect(tmp_path/'tape.db');writer.execute('DROP INDEX swaps_ps_received');writer.commit();writer.close()
    out=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',Rpc([]),now_fn=now)
    assert out['stopped']=='tape_unreadable'


def test_scan_failure_cannot_commit_partial_pending_or_cursor(tmp_path):
    clock,now=setup(tmp_path,[('P1','M1',WSOL,'mapped',1900.),('P2','M2',WSOL,'mapped',1901.)]);clock[0]=2000.
    writer=sqlite3.connect(tmp_path/'tape.db');writer.execute('UPDATE swaps_ps SET ordinal=-1 WHERE pool=?',('P2',));writer.commit();writer.close()
    out=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',Rpc([]),now_fn=now)
    assert out['stopped']=='tape_unreadable'
    meta=MD.connect(tmp_path/'m.db')
    assert meta.execute('SELECT COUNT(*) FROM observed_mappings').fetchone()[0]==0
    assert meta.execute('SELECT COUNT(*) FROM mapping_cursor').fetchone()[0]==0
    meta.close()


def test_original_map_time_is_not_replaced_by_recent_receipt(tmp_path):
    clock,now=setup(tmp_path,[('P1','M1',WSOL,'mapped',500.)]);clock[0]=10000.
    writer=sqlite3.connect(tmp_path/'tape.db');writer.execute('UPDATE swaps_ps SET received_at=9999');writer.commit();writer.close()
    rpc=Rpc([]);out=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now)
    assert rpc.calls==[] and out['pre_deploy_unread'] is None
    assert out['pre_deploy_unread_observed_lower_bound']==1


@pytest.mark.parametrize("has_cursor",[False,True])
def test_scan_budget_interrupt_keeps_delivered_prefix_and_retries(tmp_path,monkeypatch,has_cursor):
    clock,now=setup(tmp_path,[('P%d'%i,'M%d'%i,WSOL,'mapped',1900.+i/1000) for i in range(200)])
    clock[0]=2000.
    meta=MD.connect(tmp_path/'m.db')
    if has_cursor:
        monkeypatch.setattr(W,'SCAN_BATCH',1)
        source=W.ro(tmp_path/'tape.db');W.refresh_mappings(source,meta,now=now());source.close()
        monkeypatch.setattr(W,'SCAN_BATCH',2000)
    cursor_before=meta.execute('SELECT * FROM mapping_cursor').fetchall() if has_cursor else []
    meta.close()
    # Exhaust the reader's own budget while executing real SQLite reads.
    monkeypatch.setattr(W,'SCAN_BUDGET_S',-1.)
    rpc=Rpc([])
    out=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',rpc,now_fn=now)
    assert out['stopped'] is None and out['scan_partial'] is True and out['budget_interrupts']==1
    assert out['budget_interrupts_total']==1 and rpc.calls==[]
    meta=MD.connect(tmp_path/'m.db')
    cursor_after=meta.execute('SELECT * FROM mapping_cursor').fetchall()
    if out['source_rows']:
        assert cursor_after and (not cursor_before or cursor_after[0][1:4]>cursor_before[0][1:4])
    else:
        assert cursor_after==cursor_before
    assert meta.execute('SELECT COUNT(*) FROM observed_mappings').fetchone()[0]==int(has_cursor)+out['source_rows']
    assert meta.execute("SELECT COUNT(*) FROM gaps WHERE cause='scan_failed'").fetchone()[0]==0
    meta.close()
    delivered=out['source_rows']
    monkeypatch.setattr(W,'SCAN_BUDGET_S',.25)
    clock[0]+=1
    # A next poll can consume the exact same page; no watchdog/restart required.
    out=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',Rpc([(mint_result(),None)]*200),now_fn=now)
    assert out['stopped'] is None and 0<out['source_rows']<=200-int(has_cursor)-delivered and out['read']>0
    assert out['budget_interrupts']==0 and out['budget_interrupts_total']==1


@pytest.mark.parametrize("without_rowid",[False,True])
def test_partial_prefix_is_committed_and_page_size_adapts(tmp_path,monkeypatch,without_rowid):
    import threading
    pools=[('P%04d'%i,'M%04d'%i,WSOL,'mapped',1900.+(0 if without_rowid else i/1000)) for i in range(3000)]
    clock,now=setup(tmp_path,pools);clock[0]=2000.
    if without_rowid:
        (tmp_path/'tape.db').unlink()
        tape(tmp_path/'tape.db',pools,without_rowid=True)
    real=W.time.monotonic
    me=threading.current_thread();ticks=[0.]
    def bounded_clock():
        if threading.current_thread() is not me:return real()
        ticks[0]+=.01
        return ticks[0]
    with monkeypatch.context() as patch:
        patch.setattr(W.time,'monotonic',bounded_clock)
        out=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',Rpc([]),now_fn=now)
    assert out['scan_partial'] is True and 0<out['source_rows']<W.SCAN_BATCH
    meta=MD.connect(tmp_path/'m.db')
    assert meta.execute('SELECT COUNT(*) FROM observed_mappings').fetchone()[0]==out['source_rows']
    before=meta.execute('SELECT received_at,signature,ordinal FROM mapping_cursor').fetchone()
    assert W.get_state(meta,'scan_batch_size')==W.SCAN_BATCH//2
    meta.close()
    # A restart uses the smaller limit and advances from the delivered prefix.
    out2=W.run_once(tmp_path/'tape.db',tmp_path/'m.db',Rpc([(mint_result(),None)]*20),now_fn=now)
    assert out2['source_rows']==W.SCAN_BATCH//2
    meta=MD.connect(tmp_path/'m.db')
    after=meta.execute('SELECT received_at,signature,ordinal FROM mapping_cursor').fetchone()
    assert after>before and W.get_state(meta,'scan_batch_size')==W.SCAN_BATCH
    assert meta.execute('SELECT COUNT(*) FROM observed_mappings').fetchone()[0]==out['source_rows']+out2['source_rows']
    meta.close()
