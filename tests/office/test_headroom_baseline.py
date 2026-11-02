"""Baseline collection writes only its own append-only sample artifact."""
import importlib.util
import json
from pathlib import Path

spec=importlib.util.spec_from_file_location('office_headroom',Path(__file__).resolve().parents[2]/'scripts/office_measure_headroom.py')
H=importlib.util.module_from_spec(spec);spec.loader.exec_module(H)


def test_baseline_append_deduplicates_the_same_minute(tmp_path):
    path=tmp_path/'baseline.jsonl'
    sample={'sample_id':'utc-minute:1','office_off':True,'at_host':100.,'corrected_at':100.}
    assert H.append_baseline(path,sample) is True
    assert H.append_baseline(path,sample) is False
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    assert rows==[sample]


def test_baseline_marks_live_office_and_unreadable_proc_unknown(tmp_path):
    proc=tmp_path/'proc';proc.mkdir()
    p=proc/'123';p.mkdir();(p/'cmdline').write_bytes(b'python3\0/release/scripts/office_supervisor.py\0')
    (p/'cgroup').write_text('0::/user.slice/office.slice')
    assert H.office_off(proc_root=proc) is False
    (p/'cmdline').write_bytes(b'python3\0other.py\0');(p/'cgroup').write_text('0::/user.slice/app.slice')
    assert H.office_off(proc_root=proc) is True
    (p/'cmdline').unlink()
    assert H.office_off(proc_root=proc) is None


def test_baseline_missing_clock_or_heartbeat_is_unknown_not_zero(tmp_path):
    proc=tmp_path/'proc';proc.mkdir()
    clock=tmp_path/'clock.json';beat=tmp_path/'beat.json'
    got=H.baseline_sample(clock_file=clock,funded_heartbeat=beat,funded_ledger=tmp_path/'missing.db',
                          now_host=100.,proc_root=proc,headroom={'cpu_pressure':.01})
    assert got['corrected_at'] is None and got['funded_heartbeat_age_s'] is None
    assert got['latency'] is None and got['office_off'] is True
    assert not (tmp_path/'missing.db').exists()
    clock.write_text(json.dumps({'offset_s':2.,'at':99.}))
    beat.write_text(json.dumps({'ts':98.,'bracket_ts':97.,'auth_ok':True}))
    got=H.baseline_sample(clock_file=clock,funded_heartbeat=beat,funded_ledger=tmp_path/'missing.db',
                          now_host=100.,proc_root=proc,headroom={'cpu_pressure':.01})
    assert got['corrected_at']==98. and got['funded_heartbeat_age_s']==2.
    assert got['funded_bracket_age_s']==3. and got['latency'] is None


def test_latency_tail_counts_submitted_unique_attempts_not_sample_windows(tmp_path):
    import sqlite3
    c=sqlite3.connect(tmp_path/'traces.db')
    c.execute('CREATE TABLE memecoin_entry_attempts(attempt_id,network_id,signal_source,terminal_status,completed_at,total_ms,transaction_submitted_at)')
    c.execute('CREATE TABLE memecoin_exit_execution_attempts(id,network_id,tier_variant,status,attempted_at,total_ms,sig)')
    stamp='1970-01-01T00:16:30+00:00'
    c.execute('INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?)',('e1','solana','slot_lane','filled',stamp,100.,stamp))
    c.execute('INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,NULL)',('reject','solana','slot_lane','refused',stamp,900.))
    c.execute('INSERT INTO memecoin_exit_execution_attempts VALUES(?,?,?,?,?,?,?)',(1,'solana','baseline','confirmed',stamp,50.,'sig'))
    c.commit()
    a=H.trace_tail(c,now=1000.)
    b=H.trace_tail(c,now=1001.)
    assert {r['trace_id'] for r in a['observations']}=={'entry:e1','exit:1'}
    assert {r['trace_id'] for r in b['observations']}=={'entry:e1','exit:1'}
    assert a['total_unique_attempts']==2 and len(a['groups'])==2
    assert all(r['known_at']==1000. and r['source_at']==990. for r in a['observations'])
    assert all('status' not in group for group in a['groups'])
    c.close()
