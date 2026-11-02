"""The slot lane's skip reasons for fill timing (spec rev 2, 2.2, decision C): one deque append per
routed decision on the lane's thread, drained every second by a runner thread into the timing file,
never the ledger."""
import sqlite3
import threading
from collections import OrderedDict

import pytest

from sniper.services import memecoin_fill_timing as ft
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_slot_signal as s
from sniper.services import memecoin_slot_skips as skips
from test_slot_lane_routed_pump import TXS, Handoff, Pools, _pools, _routed


@pytest.fixture(autouse=True)
def empty_ring():
    skips.drain()
    with skips._LAST_LOCK:
        skips._LAST.clear()
    yield
    skips.drain()


def _route(tx, *, pump_on, lane_live, pools, handoff=None):
    lane.route_routed_buy(_routed(tx), handoff=handoff, log=lambda line: None, put=lambda buy: None,
                          amm_pools=pools, cpmm_on=False, pump_on=pump_on, lane_live=lane_live)
    return [(sig, outcome, reason) for sig, outcome, reason, _t in skips.drain()]


def test_a_routed_buy_the_lane_takes_notes_lane():
    tx = TXS[1]
    assert _route(tx, pump_on=True, lane_live=True, pools=_pools(tx)) == [(tx["signature"], "lane", "pump_amm")]


def test_a_handed_off_buy_notes_why():
    tx = TXS[1]
    assert _route(tx, pump_on=True, lane_live=True, pools=Pools({}), handoff=Handoff()) == [
        (tx["signature"], "handoff", "pool_unknown")]
    assert _route(tx, pump_on=False, lane_live=True, pools=Pools({}), handoff=Handoff()) == [
        (tx["signature"], "handoff", "no_slot_venue")]
    assert _route(tx, pump_on=True, lane_live=False, pools=_pools(tx), handoff=Handoff()) == [
        (tx["signature"], "handoff", "lane_shadow")]


def test_an_expired_pair_notes_each_signature():
    pairer = s.RoutedPairer(wait_s=0.5)
    pairer._slots[("owner", 7)] = {"first": 0.0, "utc": "", "usdc": -5, "coins": {"M": 3}, "decimals": {},
                                   "sigs": {"SIG1"}, "refused": None, "done": None, "events": []}
    assert pairer.expire(10.0) == 1
    assert [(sig, outcome) for sig, outcome, _r, _t in skips.drain()] == [("SIG1", "pair_expired")]


def test_the_ring_is_bounded():
    for i in range(skips.RING_MAX + 10):
        skips.note("S%d" % i, "lane")
    rows = skips.drain()
    assert len(rows) == skips.RING_MAX and rows[0][0] == "S10"


def test_flush_writes_the_timing_file_and_never_the_ledger(tmp_path, monkeypatch):
    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda path, *a, **k: opened.append(str(path)) or real(path, *a, **k))
    skips.note("SIG1", "handoff", "pool_unknown")
    skips.note("SIG1", "lane", "pump_amm")                 # the latest decision for a signature wins
    conn = ft.connect(tmp_path / "timing.db")
    assert skips.flush(conn, skips.drain()) == 2
    assert conn.execute("SELECT outcome, reason FROM memecoin_slot_lane_skips").fetchall() == [("lane", "pump_amm")]
    assert all("sniper_tracker" not in p for p in opened)


def test_late_pair_expiry_cannot_erase_the_native_decision(tmp_path):
    conn = ft.connect(tmp_path / 'timing.db')
    skips.note('SIG1', 'rejected', 'pool_unknown_learning')
    skips.flush(conn, skips.drain())
    skips.note('SIG1', 'pair_expired')
    skips.note('SIG1', 'lane', 'pump_amm')
    skips.flush(conn, skips.drain())
    assert ft.skip_of(conn, 'solana', 'SIG1') == dict(outcome='rejected', reason='pool_unknown_learning')


def test_missing_receipt_reason_takes_precedence_over_routed_lane_note():
    f = ft.facts(dict(network_id='solana', signal_source='onchain_watch'),
        slot_attempt=dict(terminal_status='source_initiation_unknown', terminal_reason='missing_receipt_proof'),
        skip=dict(outcome='lane', reason='raydium_cpmm'), timing=dict(source_slot=100, landing_slot=104))
    assert ft.classify(f)[1].startswith('slot lane refused: missing_receipt_proof')


def test_the_drain_thread_survives_a_failing_write(tmp_path):
    calls = []

    def broken():
        calls.append(1)
        raise sqlite3.OperationalError("disk I/O error")
    stop = threading.Event()
    skips.note("SIG1", "lane")
    t = threading.Thread(target=skips.run_drain, args=(stop,), kwargs={"interval_s": 0.01, "connect": broken},
                         daemon=True)
    t.start()
    for _ in range(200):
        if calls:
            break
        stop.wait(0.01)
    skips.note("SIG2", "lane")
    stop.wait(0.05)
    stop.set()
    t.join(1)
    assert calls and not t.is_alive()


@pytest.mark.parametrize("pair, why", [
    (dict(usdc=-5, coins={"M": 3}, refused="other_quote"), "other_quote"),      # e.g. a wSOL hop
    (dict(usdc=-5, coins={"M": 3}, refused="lane"), "lane"),                    # the lane's own buy
    (dict(usdc=0, coins={"M": 3}), "usdc_not_spent"),                           # e.g. SOL-paid
    (dict(usdc=5, coins={"M": -3}), "sold"),                                    # a partial sell
    (dict(usdc=5, coins={}), "sold"),                   # a full sell that closed the coin account
    (dict(usdc=5, coins={"M": 3}), "usdc_not_spent"),   # USDC and a coin both rose: not a sale
    (dict(usdc=0, coins={"M": -3}), "sold"),            # a sell paid in native SOL (pump, PumpSwap)
    (dict(usdc=0, coins={"M": -3, "N": -1}), "sold"),
    (dict(usdc=-5, coins={"M": -3}), "coin_fell"),      # USDC out and the coin fell: not a sale
    (dict(usdc=-5, coins={"M": 3, "N": 2}), "coins=2"),
    (dict(usdc=-5, coins={"M": -3}), "coin_fell"),
    (dict(usdc=-5, coins={"M": 3}), "complete_unchecked"),
])
def test_an_expired_pair_names_why_it_never_paired(pair, why):
    """Routed-pair spec step 0 (2026-10-07): the note said only pair_expired, for SOL-paid routes,
    other-quote hops and frames the lane had claimed alike. The outcome stays pair_expired (its
    precedence under a real lane result is unchanged); the reason names the cause."""
    skips.drain()
    status = {}
    pairer = s.RoutedPairer(wait_s=0.5, stats=status)
    pairer._slots[("owner", 7)] = dict({"first": 0.0, "utc": "", "decimals": {}, "sigs": {"SIG1"},
                                        "refused": None, "done": None, "events": []}, **pair)
    assert pairer.expire(10.0) == 1
    assert [(sig, outcome, reason) for sig, outcome, reason, _t in skips.drain()] == [
        ("SIG1", "pair_expired", why)]
    assert status["expired_by"] == {why: 1} and status["expired"] == 1


@pytest.mark.parametrize('older_first', [True, False])
def test_terminal_note_only_finishes_current_submitted_owner(tmp_path, older_first):
    skips.note('OWNER', 'submitted', attempt_id='old')
    skips.note('OWNER', 'submitted', attempt_id='new')
    order = [('old', 'failed'), ('new', 'filled')]
    if not older_first: order.reverse()
    for aid, status in order:
        assert skips.finish_note('OWNER', aid, status) is (aid == 'new')
    assert skips.decision_for('OWNER') == ('filled', None)
    conn = ft.connect(tmp_path / 'owner.db')
    skips.flush(conn, skips.drain())
    assert ft.skip_of(conn, 'solana', 'OWNER')['outcome'] == 'filled'


def test_skip_owner_hints_and_ownerless_replacement():
    skips.note('OWNER2', 'submitted', attempt_id='old')
    skips.note('OWNER2', 'lane')
    assert skips.finish_note('OWNER2', 'old', 'failed')
    skips.note('OWNER2', 'submitted', attempt_id='old')
    skips.note('OWNER2', 'rejected')
    assert not skips.finish_note('OWNER2', 'old', 'failed')
    assert not skips.finish_note('absent', 'old', 'failed')


def test_evicted_skip_owner_cannot_reappear_after_completion(monkeypatch):
    monkeypatch.setattr(skips, 'LAST_MAX', 2)
    skips.note('evicted', 'submitted', attempt_id='old')
    skips.note('other1', 'submitted', attempt_id='one')
    skips.note('other2', 'submitted', attempt_id='two')
    assert skips.decision_for('evicted') is None
    assert not skips.finish_note('evicted', 'old', 'filled')
    assert not any(r[0] == 'evicted' and r[1] == 'filled' for r in skips.drain())


def test_lock_free_decision_peek_reads_immutable_tuple_while_writer_lock_is_held():
    skips.note('volume-lock-free', 'rejected', 'solana_volume_under_floor')
    completed, observed = threading.Event(), []
    def read():
        observed.extend([skips.peek_decision_for('volume-lock-free'), skips.peek_decision_for('absent')])
        completed.set()
    reader = threading.Thread(target=read, daemon=True)
    with skips._LAST_LOCK:
        reader.start()
        did_not_wait = completed.wait(.2)
    reader.join(1.)
    assert did_not_wait
    assert observed == [('rejected', 'solana_volume_under_floor'), None]


def test_replacing_same_signature_never_exposes_a_missing_decision(monkeypatch):
    observed = []
    class ObservedDecisions(OrderedDict):
        def pop(self, key, *args):
            result = super().pop(key, *args)
            observed.append(self.get(key))
            return result

        def __setitem__(self, key, value):
            observed.append(self.get(key))
            super().__setitem__(key, value)
            observed.append(self.get(key))

    decisions = ObservedDecisions()
    OrderedDict.__setitem__(decisions, 'volume-replacement', ('rejected', 'old', None))
    monkeypatch.setattr(skips, '_LAST', decisions)
    skips.note('volume-replacement', 'rejected', 'solana_volume_under_floor')
    assert observed and None not in observed
    assert skips.peek_decision_for('volume-replacement') == ('rejected', 'solana_volume_under_floor')


class _Passes:
    def __init__(self, n): self.n = n
    def wait(self, _interval):
        self.n -= 1
        return self.n < 0


def test_receipt_drain_without_skips_retries_failed_commit(tmp_path):
    from test_memecoin_feed_arrivals import _journal, _record
    journal = _journal(); _record(journal, 0)
    path = tmp_path / 'receipt.db'
    real = ft.connect(path)
    class FailedCommit:
        def __getattr__(self, name): return getattr(real, name)
        def commit(self): raise sqlite3.OperationalError('disk')
        def close(self): real.rollback()
    connections = iter([FailedCommit(), real])
    skips.run_drain(_Passes(2), connect=lambda: next(connections), journals=(journal,))
    rows = real.execute('SELECT sequence, recv_monotonic FROM memecoin_signal_receipts').fetchall()
    assert rows == [(1, 0.0)]


def test_drain_overwrite_is_explicit_gap_and_pass_bound_is_512(tmp_path):
    from test_memecoin_feed_arrivals import _journal, _record
    journal = _journal(600)
    for i in range(603): _record(journal, i)
    conn = ft.connect(tmp_path / 'gap.db')
    skips.run_drain(_Passes(1), connect=lambda: conn, journals=(journal,))
    assert conn.execute('SELECT COUNT(*) FROM memecoin_signal_receipts').fetchone()[0] == 512
    assert conn.execute('SELECT lost_count, mono_start, mono_end FROM memecoin_execution_coverage').fetchone() == (3, 0.0, 3.0)
    assert conn.execute('SELECT MAX(sequence) FROM memecoin_signal_receipts').fetchone()[0] == 515


def test_gate_disposition_keeps_identity_and_missing_proof_unknown(tmp_path):
    conn = ft.connect(tmp_path / 'disposition.db')
    skips.note('SOURCE', 'rejected', 'pool_unknown', evidence={
        'source_wallet': 'WALLET', 'mint': None, 'recv_monotonic': 0,
        'producer_run_id': 'source-A', 'boot_id': 'boot', 'clock_domain': 'mono'})
    skips.flush(conn, skips.drain())
    row = conn.execute('SELECT evidence_json FROM memecoin_slot_lane_skips').fetchone()[0]
    import json
    evidence = json.loads(row)
    assert evidence['eligibility'] == 'eligibility_unknown'
    assert evidence['source_wallet'] == 'WALLET' and evidence['recv_monotonic'] == 0


def test_background_disposition_preserves_original_routed_identity():
    from types import SimpleNamespace
    from test_memecoin_feed_arrivals import _journal
    journal = _journal()
    original = SimpleNamespace(receipt_evidence=journal.identity(0, 3), owner='wallet',
        signature='source', mint='mint', pool='pool')
    converted = SimpleNamespace(receipt_evidence=None, routed=original, owner='wallet',
        signature='source', mint='mint', pool='pool')
    evidence = lane._receipt_facts(converted, {'status': 'rejected', 'reason': 'not_approved'})
    assert evidence['producer_run_id'] == 'source-A' and evidence['recv_monotonic'] == 0
    assert evidence['policy_refused'] and evidence['independent_inputs_complete']
    unknown = lane._receipt_facts(converted, {'status': 'rejected', 'reason': 'source_initiation_unknown'})
    assert not unknown['policy_refused']


def test_drain_disposition_distinguishes_accepted_policy_and_unknown(tmp_path):
    conn = ft.connect(tmp_path / 'facts.db')
    import json
    for sig, outcome, evidence, expected in [
        ('accepted', 'submitted', {'source_wallet': 'wallet', 'mint': 'mint'}, 'eligible'),
        ('refused', 'rejected', {'source_wallet': 'wallet', 'mint': 'mint',
            'independent_inputs_complete': True, 'policy_refused': True}, 'independent_policy_refused'),
        ('unknown', 'rejected', {'source_wallet': 'wallet', 'mint': None,
            'independent_inputs_complete': False, 'policy_refused': True}, 'eligibility_unknown')]:
        skips.note(sig, outcome, evidence=evidence)
        skips.flush(conn, skips.drain())
        raw, = conn.execute('SELECT evidence_json FROM memecoin_slot_lane_skips WHERE signature=?', (sig,)).fetchone()
        assert json.loads(raw)['eligibility'] == expected


def test_failed_journal_does_not_hide_other_writer_with_no_skip_rows(tmp_path):
    from test_memecoin_feed_arrivals import _journal, _record
    class BrokenSnapshot:
        def snapshot(self): raise ValueError('malformed measurement')
    source = _journal(); _record(source, 0)
    conn = ft.connect(tmp_path / 'independent.db')
    skips.run_drain(_Passes(1), connect=lambda: conn, journals=(BrokenSnapshot(), source))
    assert conn.execute('SELECT COUNT(*) FROM memecoin_signal_receipts').fetchone()[0] == 1


def test_huge_optional_disposition_number_is_best_effort():
    from types import SimpleNamespace
    buy = SimpleNamespace(receipt_evidence={'producer_run_id': 'original'}, owner='wallet',
                          signature='source', mint='mint', pool=None)
    assert lane._receipt_facts(buy, {'their_usd': 10 ** 1000}) is None
