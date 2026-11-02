import importlib.util
import json
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location('sheet_stats', Path(__file__).resolve().parents[2] / 'scripts/office_sheet_read_stats.py')
M = importlib.util.module_from_spec(spec); spec.loader.exec_module(M)


def dbs():
    o = sqlite3.connect(':memory:'); d = sqlite3.connect(':memory:')
    o.execute('CREATE TABLE office_v2_events(seq INTEGER PRIMARY KEY,actor_id TEXT,corrected_at REAL,kind TEXT,refs_json TEXT)')
    d.execute('CREATE TABLE desk_tickets(ticket_id TEXT,emitted_at TEXT)')
    d.execute("INSERT INTO desk_tickets VALUES('t1','1970-01-01T00:16:40+00:00')")        # emitted at 1000
    d.execute("INSERT INTO desk_tickets VALUES('t2','1970-01-01T00:16:40+00:00')")
    return o, d


def read(o, actor, at, tid, state, extra=None, result_id=True):
    o.execute("INSERT INTO office_v2_events(actor_id,corrected_at,kind,refs_json) VALUES(?,?,?,?)", (actor, at, 'tool_call', json.dumps(dict(tool='sheet_read', ticket_id=tid) if tid else dict(tool='sheet_read'))))
    o.execute("INSERT INTO office_v2_events(actor_id,corrected_at,kind,refs_json) VALUES(?,?,?,?)", (actor, at + 1, 'tool_result', json.dumps(dict(tool='sheet_read', sheet_state=state, **(dict(ticket_id=tid) if (tid and result_id) else {}), **(extra or {})))))


def test_first_read_per_ticket_is_binned_by_age_and_repeat_reads_do_not_inflate():
    o, d = dbs()
    read(o, 'a', 1030, 't1', 'known')                                    # 30 s old
    read(o, 'a', 1400, 't1', 'partial', dict(sheet_class='tape_loss_since_cut'))        # a repeat read of t1: counted in reads, not in distinct tickets
    read(o, 'b', 5000, 't2', 'partial', dict(sheet_class='mint_receipt_too_old_or_invalid'))        # 4000 s old
    read(o, 'b', 5100, None, 'unknown')                                  # old event without a ticket id: not attributed
    out = M.stats(o, d)
    assert out['reads_by_state'] == {'known': 1, 'partial': 2, 'unknown': 1} and out['distinct_tickets'] == 2 and out['reads_without_ticket_id'] == 1
    assert out['first_read_by_age_bin'] == {'under_60s': {'known': 1}, 'over_1h': {'partial': 1}}
    assert out['first_read_failures_by_reason'] == {'partial:mint_receipt_too_old_or_invalid': 1}


def test_overlapping_reads_of_one_seat_match_by_ticket_id_and_legacy_overlaps_are_ambiguous():
    o, d = dbs()
    for at, kind, refs in [(1030, 'tool_call', dict(tool='sheet_read', ticket_id='t1')), (1031, 'tool_call', dict(tool='sheet_read', ticket_id='t2')),
                           (1032, 'tool_result', dict(tool='sheet_read', sheet_state='known', ticket_id='t1')), (1033, 'tool_result', dict(tool='sheet_read', sheet_state='partial', ticket_id='t2', sheet_class='x'))]:
        o.execute('INSERT INTO office_v2_events(actor_id,corrected_at,kind,refs_json) VALUES(?,?,?,?)', ('a', at, kind, json.dumps(refs)))
    out = M.stats(o, d)
    assert out['distinct_tickets'] == 2 and out['ambiguous_reads'] == 0 and out['first_read_by_age_bin'] == {'under_60s': {'known': 1, 'partial': 1}}      # t2 (31 s old) is partial, t1 known
    o, d = dbs()
    for at, kind, refs in [(1030, 'tool_call', dict(tool='sheet_read', ticket_id='t1')), (1031, 'tool_call', dict(tool='sheet_read', ticket_id='t2')),
                           (1032, 'tool_result', dict(tool='sheet_read', sheet_state='known')), (1033, 'tool_result', dict(tool='sheet_read', sheet_state='partial'))]:
        o.execute('INSERT INTO office_v2_events(actor_id,corrected_at,kind,refs_json) VALUES(?,?,?,?)', ('a', at, kind, json.dumps(refs)))
    out = M.stats(o, d)
    assert out['distinct_tickets'] == 0 and out['ambiguous_reads'] == 2                      # no id on the results: never guessed


def ev(o, actor, at, kind, **refs):
    o.execute('INSERT INTO office_v2_events(actor_id,corrected_at,kind,refs_json) VALUES(?,?,?,?)', (actor, at, kind, json.dumps(dict(tool='sheet_read', **refs))))


def test_same_actor_same_ticket_overlap_pairs_exactly_by_call_id_and_without_it_stays_ambiguous():
    o, d = dbs()
    ev(o, 'a', 1030, 'tool_call', ticket_id='t1', call_id='c1'); ev(o, 'a', 1031, 'tool_call', ticket_id='t1', call_id='c2')
    ev(o, 'a', 1032, 'tool_result', ticket_id='t1', call_id='c2', sheet_state='known'); ev(o, 'a', 1033, 'tool_result', ticket_id='t1', call_id='c1', sheet_state='partial', sheet_class='x')
    out = M.stats(o, d)
    assert out['ambiguous_reads'] == 0 and out['first_read_by_age_bin'] == {'under_60s': {'partial': 1}}                      # c1 (the earlier call) is t1's first read: partial
    o, d = dbs()
    ev(o, 'a', 1030, 'tool_call', ticket_id='t1'); ev(o, 'a', 1031, 'tool_call', ticket_id='t1')
    ev(o, 'a', 1032, 'tool_result', ticket_id='t1', sheet_state='known'); ev(o, 'a', 1033, 'tool_result', ticket_id='t1', sheet_state='partial')
    out = M.stats(o, d)
    assert out['distinct_tickets'] == 0 and out['ambiguous_reads'] == 2                                                      # no call id, two identical pending calls: never guessed


def test_the_first_read_of_a_ticket_is_the_earliest_call_not_the_first_completion():
    o, d = dbs()
    ev(o, 'a', 1030, 'tool_call', ticket_id='t1', call_id='ca'); ev(o, 'b', 1031, 'tool_call', ticket_id='t1', call_id='cb')
    ev(o, 'b', 1032, 'tool_result', ticket_id='t1', call_id='cb', sheet_state='known'); ev(o, 'a', 1033, 'tool_result', ticket_id='t1', call_id='ca', sheet_state='partial', sheet_class='y')
    out = M.stats(o, d)
    assert out['distinct_tickets'] == 1 and out['first_read_by_age_bin'] == {'under_60s': {'partial': 1}}                   # A called first; B finished first
