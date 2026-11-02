"""Sweep closure measurement on a synthetic PumpSwap tape: exact integer closure, residuals, and the frozen exclusions."""
import importlib.util
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location('sweep_closure', Path(__file__).resolve().parents[1] / 'scripts/office_sweep_closure.py')
M = importlib.util.module_from_spec(spec); spec.loader.exec_module(M)
WSOL = M.WSOL


def tape(tmp_path, *, amounts=(30, 5), mint=WSOL, extra=lambda c: None):
    c = sqlite3.connect(tmp_path / 'tape.db')
    c.execute('CREATE TABLE swaps_ps(pool TEXT,slot INTEGER,tx_index INTEGER,ordinal INTEGER,pre_base_reserve_atomic TEXT,pre_quote_reserve_atomic TEXT,post_base_reserve_atomic TEXT,post_quote_reserve_atomic TEXT,received_at REAL,signature TEXT)')
    c.execute('CREATE TABLE ps_sweep_events(signature TEXT,ordinal INTEGER,pool TEXT,base_mint TEXT,quote_mint TEXT,recipient TEXT,payer TEXT,amount TEXT,bucket INTEGER,event_time INTEGER,slot INTEGER,tx_index INTEGER,received_at REAL,recorder_version TEXT)')
    c.execute('CREATE TABLE ps_loss_attribution(signature TEXT,cause TEXT,reason TEXT,scope TEXT,pools_json TEXT,invocations INTEGER,discriminator TEXT,event_length INTEGER,idl_sha256 TEXT,received_at REAL,slot INTEGER,protocol INTEGER)')
    c.execute('CREATE TABLE gaps_ps(at REAL,cause TEXT,last_slot INTEGER)')
    c.execute('CREATE TABLE ps_gap_spans(start_at REAL,end_at REAL,cause TEXT,ended_by TEXT)')
    c.execute('CREATE TABLE ps_capture_intervals(session TEXT,start_at REAL,through_at REAL,protocol INTEGER)')
    # prev print slot 10: post base 500 / quote 1000. Sweep tx in slot 12 (two buckets). A stored buy in slot 12 (+100 quote, -20 base). next print slot 15: pre base 480 / quote 1065.
    c.execute("INSERT INTO swaps_ps VALUES('P',10,1,0,'510','900','500','1000',100.0,'prev')")
    c.execute("INSERT INTO swaps_ps VALUES('P',12,7,0,'500','965','480','1065',120.0,'buy12')")
    c.execute("INSERT INTO swaps_ps VALUES('P',15,2,0,'480','1065','470','1100',150.0,'next')")
    for i, a in enumerate(amounts):
        c.execute("INSERT INTO ps_sweep_events VALUES('sw',?,'P','b',?,'r','p',?,?,1,12,3,110.0,'sweep-rec-v1')", (i, mint, str(a), i))
    c.execute("INSERT INTO ps_loss_attribution VALUES('sw','virtual_quote_unproven','x','complete','[\"P\"]',1,?,185,'i',110.0,12,1)", (M.SWEEP_DISC,))
    extra(c)
    c.commit()
    return c


def test_a_gap_with_both_buckets_and_a_same_slot_swap_closes_exactly(tmp_path):
    out = M.measure(tape(tmp_path), limit=1)
    assert (out['selected'], out['closed'], out['residuals'], out['accounting_closure_500_of_500']) == (1, 1, [], False)      # one closure at limit 1 is not a 500
    assert out['validation_note'] == 'limit_below_500, not a validation' and out['virtual_quote_continuity'] == 'unchecked, not stored' and any('completeness' in r for r in out['r7_not_ready_because'])
    

def test_a_missing_bucket_event_leaves_a_residual_not_a_closure(tmp_path):
    out = M.measure(tape(tmp_path, amounts=(30,)), limit=1)
    assert out['closed'] == 0 and out['residuals'][0]['quote_residual'] == -5 and out['residuals'][0]['base_residual'] == 0


def test_a_sweep_loss_with_no_recorded_event_fails_closure(tmp_path):
    def more(c): c.execute("INSERT INTO ps_loss_attribution VALUES('old','virtual_quote_unproven','x','complete','[\"P\"]',1,?,185,'i',111.0,13,1)", (M.SWEEP_DISC,))
    out = M.measure(tape(tmp_path, extra=more), limit=1)
    assert out['closed'] == 0 and out['sweep_event_unrecorded'] == 1 and out['residuals'][0]['reason'] == 'sweep_event_unrecorded'


def test_the_frozen_exclusions_are_listed_not_counted(tmp_path):
    cases = {
        'not_wsol_quote': dict(mint='Other'),
        'loss_scope_null': dict(extra=lambda c: c.execute("INSERT INTO ps_loss_attribution VALUES('s','decode_loss','x',NULL,'[\"P\"]',1,'67f4521f2cf57777',515,'i',112.0,13,1)")),
        'R7e_unknown_loss_kind': dict(extra=lambda c: c.execute("INSERT INTO ps_loss_attribution VALUES('n','decode_loss','x','complete','[\"P\"]',1,NULL,NULL,'i',112.0,13,1)")),
        'loss_scope_unreadable': dict(extra=lambda c: c.execute("INSERT INTO ps_loss_attribution VALUES('m','decode_loss','x','complete','broken',1,'67f4521f2cf57777',515,'i',112.0,13,1)")),
        'R7e_other_loss_kind': dict(extra=lambda c: c.execute("INSERT INTO ps_loss_attribution VALUES('b','decode_loss','x','complete','[\"P\"]',1,'67f4521f2cf57777',515,'i',112.0,13,1)")),
        'R7e_capture_stop_or_point': dict(extra=lambda c: c.execute("INSERT INTO gaps_ps VALUES(130.0,'worker_start',13)")),
        'missing_neighbour': dict(extra=lambda c: c.execute("DELETE FROM swaps_ps WHERE signature='next'")),
        'in_gap_swap_post_unproven': dict(extra=lambda c: c.execute("UPDATE swaps_ps SET post_quote_reserve_atomic=NULL WHERE signature='buy12'")),
    }
    for reason, kw in cases.items():
        out = M.measure(tape(_fresh(tmp_path, reason), **kw), limit=1)
        assert out['selected'] == 0 and out['excluded'].get(reason, 0) >= 1, (reason, out)


def _fresh(tmp_path, name):
    d = tmp_path / name; d.mkdir()
    return d


def test_a_session_hole_inside_the_gap_is_a_capture_exclusion(tmp_path):
    def holes(c):
        c.execute("INSERT INTO ps_capture_intervals VALUES('a',0,125.0,1)"); c.execute("INSERT INTO ps_capture_intervals VALUES('b',130.0,NULL,1)")
    out = M.measure(tape(tmp_path, extra=holes), limit=1)
    assert out['selected'] == 0 and out['excluded'] == {'R7e_capture_stop_or_point': 1}


def test_the_limit_counts_distinct_measured_gaps_only(tmp_path):
    out = M.measure(tape(tmp_path), limit=500)
    assert out['selected'] == 1 and out['accounting_closure_500_of_500'] is False          # 1 closure is not 500 of 500


def test_pool_membership_is_exact_not_a_substring(tmp_path):
    def other_pool(c): c.execute("INSERT INTO ps_loss_attribution VALUES('o','decode_loss','x','complete','[\"PP\",\"XP\"]',1,'67f4521f2cf57777',515,'i',112.0,13,1)")
    out = M.measure(tape(tmp_path, extra=other_pool), limit=1)                         # pools PP and XP both CONTAIN the text of pool P; neither is P
    assert out['selected'] == 1 and out['closed'] == 1 and 'R7e_other_loss_kind' not in out['excluded']


def test_a_full_500_of_numeric_closure_is_reported_as_numeric_only(tmp_path):
    c = tape(tmp_path)
    for i in range(1, 500):                                                            # 499 more independent pools shaped like the first
        p = 'Q%d' % i
        c.execute("INSERT INTO swaps_ps VALUES(?,10,1,0,'510','900','500','1000',100.0,?)", (p, 'prev' + p))
        c.execute("INSERT INTO swaps_ps VALUES(?,12,7,0,'500','965','480','1065',120.0,?)", (p, 'buy' + p))
        c.execute("INSERT INTO swaps_ps VALUES(?,15,2,0,'480','1065','470','1100',150.0,?)", (p, 'next' + p))
        for k, a in enumerate((30, 5)):
            c.execute("INSERT INTO ps_sweep_events VALUES(?,?,?, 'b',?, 'r','p',?,?,1,12,3,?,'sweep-rec-v1')", ('sw' + p, k, p, WSOL, str(a), k, 111.0 + i))
        c.execute("INSERT INTO ps_loss_attribution VALUES(?,'virtual_quote_unproven','x','complete',?,1,?,185,'i',111.0,12,1)", ('sw' + p, '["%s"]' % p, M.SWEEP_DISC))
    c.commit()
    out = M.measure(c, limit=500)
    assert out['selected'] == 500 and out['closed'] == 500 and out['accounting_closure_500_of_500'] is True and 'grants an exemption' in out['use_gate'] and out['virtual_quote_continuity'] == 'unchecked, not stored'      # accounting closure never grants the rule


def test_the_snapshot_is_released_on_an_exception_too(tmp_path):
    c = tape(tmp_path)
    c.execute('DROP TABLE ps_loss_attribution')                                        # a missing table raises inside the measurement
    try: M.measure(c, limit=1)
    except sqlite3.Error: pass
    assert c.in_transaction is False


def test_a_gap_with_an_unknown_cause_capture_row_is_excluded_not_ignored(tmp_path):
    out = M.measure(tape(tmp_path, extra=lambda c: c.execute("INSERT INTO gaps_ps VALUES(130.0,NULL,13)")), limit=1)
    assert out['selected'] == 0 and out['excluded'] == {'R7e_capture_stop_or_point': 1}
