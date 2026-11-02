"""Immutable as-of equity: strict provenance, one result, no mutable-ledger fallback."""
import copy
import hashlib
import json
import shutil
import sqlite3

import pytest

from sniper.office import equity_snapshot as E, ledger as L, store as S


def fixture(root, *, binding='curve_derived', later=False):
    root.mkdir(parents=True, exist_ok=True)
    path = root/'ledger.db'
    conn = L.open_ledger(path)
    L.open_epoch(conn, epoch_id='e', clip_usd=25., rule_version='fixture', actor='fixture', note='fixture', now=90.)
    L.open_book(conn, book_id='b', epoch_id='e', kind='evaluated', actor='fixture', note='fixture', now=90.)
    L.fund(conn, book_id='b', usd=1000., kind='initial', actor='fixture', note='fixture', now=91.)
    conn.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,policy_hash,fill_model_version,state,requested_at,filled_at,spend_lamports,tokens_atomic,spend_usd) VALUES('o','b','e','evaluated','solana','m','P','fixture','{}','fixture','fixture','open',94,95,'100','100000',25)")
    conn.execute("INSERT INTO cashflows(order_id,kind,at,lamports,cost_lamports,cost_basis,cost_evidence,fx_usd,fx_at,usd) VALUES('o','entry',?,'100','0','fixture','fixture',150,95,-25)", (101. if later else 95.,))
    quote = dict(lamports='1000000000', pool='P', binding_kind=binding,
                 clock=dict(offset_s=7., host_at=106.), proof=dict(committed_through=106.))
    conn.execute("INSERT INTO marks(order_id,at,lamports,source,binding_kind,quote_proof) VALUES('o',99,'1000000000','fixture',?,?)", (binding, json.dumps(quote)))
    conn.close()
    raw = path.read_bytes()
    path.chmod(0o444)
    doc = dict(schema='office-equity-snapshot-v1', epoch_id='e', as_of_cut=100., taken_at=101.,
               clock_sample_id='fixture-clock', clock_offset_s=7., clock_sampled_at_host=107.,
               ledger=dict(path='ledger.db', sha256=hashlib.sha256(raw).hexdigest(), user_version=0,
                   books=['b'], order_count=1, mark_count=1, cashflow_count=1,
                   max_order_time=95., max_cashflow_at=101. if later else 95., max_mark_at=99.),
               fx=dict(sol_usd=150., sol_usd_at=99., source='fixture', source_ref='fixture:fx', max_age_s=120.),
               receipt=dict(id='fixture-receipt', written_by='fixture', written_by_identity='trusted-snapshotter'))
    return doc


def write(root, doc):
    doc = copy.deepcopy(doc)
    doc['receipt'].pop('metadata_sha256', None)
    doc['receipt']['metadata_sha256'] = hashlib.sha256(S.canonical_json(doc)).hexdigest()
    path = root/'snapshot.json'
    if path.exists():
        path.chmod(0o644)
    path.write_bytes(S.canonical_json(doc))
    path.chmod(0o444)
    return dict(path='snapshot.json', sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def read(root, ref, auth=lambda receipt, digest: True):
    return E.read_equity_snapshot(root, ref, epoch_id='e', look_at=100., authenticate_snapshot=auth, runtime_dir=root/'runtime')


def test_strict_equity_and_binding_counts_come_from_one_call(tmp_path, monkeypatch):
    ref = write(tmp_path, fixture(tmp_path))
    calls = []
    original = L.equity
    def equity(conn, **kw):
        assert kw['require_proven'] is True and kw['epoch_id'] == 'e' and kw['kind'] == 'evaluated'
        conn.execute('PRAGMA query_only=OFF')
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE marks SET lamports='999'")
        conn.execute('PRAGMA query_only=ON')
        value = original(conn, **kw)
        calls.append(value)
        return value
    monkeypatch.setattr(L, 'equity', equity)
    result = read(tmp_path, ref)
    assert len(calls) == 1
    assert result['summary']['cash'] == calls[0].cash
    assert result['summary']['known_liq_lower'] == calls[0].known_liq_lower
    assert result['summary']['binding_kind_counts'] == {'curve_derived':1}
    assert result['proofs']['sidecar_sha256'] == ref['sha256']


def test_later_cashflow_is_refused_not_trimmed(tmp_path):
    ref = write(tmp_path, fixture(tmp_path, later=True))
    with pytest.raises(E.SnapshotRefused, match='after_cut'):
        read(tmp_path, ref)


@pytest.mark.parametrize('fx', [None, dict(sol_usd=150., sol_usd_at=-21., source='fixture', source_ref='fx', max_age_s=120.)])
def test_missing_or_stale_fx_refuses(tmp_path, fx):
    doc = fixture(tmp_path)
    doc['fx'] = fx
    with pytest.raises(E.SnapshotRefused, match='fx'):
        read(tmp_path, write(tmp_path, doc))


def test_changed_database_byte_or_wrong_sidecar_db_hash_refuses(tmp_path):
    doc = fixture(tmp_path)
    doc['ledger']['sha256'] = 'a'*64
    with pytest.raises(E.SnapshotRefused, match='hash'):
        read(tmp_path, write(tmp_path, doc))


def test_legacy_null_binding_inventory_is_unknown_never_partial_verdict(tmp_path):
    ref = write(tmp_path, fixture(tmp_path, binding=None))
    with pytest.raises(E.SnapshotRefused) as caught:
        read(tmp_path, ref)
    assert caught.value.summary['state'] == 'unknown'
    assert caught.value.summary['known_liq_lower'] is None
    assert caught.value.summary['unpriced_qty'] == '100000'
    assert caught.value.summary['binding_kind_counts'] == {}


@pytest.mark.parametrize('answer', [False, None, 'true'])
def test_snapshotter_auth_is_literal_true_only(tmp_path, answer):
    ref = write(tmp_path, fixture(tmp_path))
    with pytest.raises(E.SnapshotRefused, match='auth'):
        read(tmp_path, ref, lambda receipt,digest: answer)


def test_live_ledger_handle_is_not_an_accepted_snapshot(tmp_path):
    conn = L.open_ledger(tmp_path/'live.db')
    with pytest.raises(E.SnapshotRefused):
        read(tmp_path, conn)
    conn.close()


def test_uncheckpointed_wal_copy_is_not_silently_an_empty_book(tmp_path):
    source = L.open_ledger(tmp_path/'live.db')
    L.open_epoch(source, epoch_id='e', clip_usd=25., rule_version='fixture', actor='fixture', note='fixture', now=90.)
    L.open_book(source, book_id='b', epoch_id='e', kind='evaluated', actor='fixture', note='fixture', now=90.)
    root = tmp_path/'copy'
    doc = fixture(root)
    (root/'ledger.db').chmod(0o644)
    shutil.copyfile(tmp_path/'live.db', root/'ledger.db')
    (root/'ledger.db').chmod(0o444)
    doc['ledger']['sha256'] = hashlib.sha256((root/'ledger.db').read_bytes()).hexdigest()
    with pytest.raises(E.SnapshotRefused):
        read(root, write(root, doc))
    source.close()


def test_failed_equity_removes_private_copy(tmp_path):
    ref = write(tmp_path, fixture(tmp_path, binding=None))
    with pytest.raises(E.SnapshotRefused):
        read(tmp_path, ref)
    assert not list((tmp_path/'runtime').iterdir())


def test_copy_is_rehashed_and_cleaned_on_hash_failure(tmp_path, monkeypatch):
    from pathlib import Path
    ref = write(tmp_path, fixture(tmp_path))
    original = Path.read_bytes
    monkeypatch.setattr(Path, 'read_bytes', lambda p: b'changed-copy' if p.parent.name.startswith('equity-') else original(p))
    with pytest.raises(E.SnapshotRefused, match='private_copy_hash'):
        read(tmp_path, ref)
    assert not list((tmp_path/'runtime').iterdir())


def test_oversize_source_is_refused_before_private_copy(tmp_path, monkeypatch):
    doc = fixture(tmp_path)
    path = tmp_path/'ledger.db'
    path.chmod(0o644)
    with path.open('r+b') as stream:
        stream.truncate(E.MAX_LEDGER_BYTES+1)
    path.chmod(0o444)
    monkeypatch.setattr(E.tempfile, 'TemporaryDirectory', lambda **kw: pytest.fail('oversize copy attempted'))
    with pytest.raises(E.SnapshotRefused, match='bounded'):
        read(tmp_path, write(tmp_path, doc))


def test_concurrent_readers_use_different_private_files(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    ref = write(tmp_path, fixture(tmp_path))
    paths, lock = [], threading.Lock()
    barrier = threading.Barrier(2)
    original = L.equity
    def equity(conn, **kw):
        with lock:
            paths.append(conn.execute('PRAGMA database_list').fetchone()[2])
        barrier.wait(timeout=1.)
        return original(conn, **kw)
    monkeypatch.setattr(L, 'equity', equity)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: read(tmp_path, ref), range(2)))
    assert len(set(paths)) == 2 and values[0] == values[1]
    assert not list((tmp_path/'runtime').iterdir())


def test_auth_exception_refuses_without_copy(tmp_path):
    ref = write(tmp_path, fixture(tmp_path))
    def unknown(*args):
        raise RuntimeError('snapshotter unavailable')
    with pytest.raises(E.SnapshotRefused, match='auth'):
        read(tmp_path, ref, unknown)
    assert not (tmp_path/'runtime').exists()


def test_evaluated_release_requires_known_snapshot_and_seals_same_summary(tmp_path):
    from sniper.office import grader as G
    ref = write(tmp_path, fixture(tmp_path))
    verified = read(tmp_path, ref)
    office = S.open_store(tmp_path/'office.db')
    office.execute('INSERT INTO epochs VALUES(?,?)', ('e', S.canonical_json(dict(kind='evaluated', registered_look_at=100.)).decode()))
    args = dict(epoch_id='e', kind='epoch_economics', released_at=101., source_heads={'grades':'a'*64}, authorize_release=lambda *a:True)
    with pytest.raises(ValueError, match='strict_equity_required'):
        G.release_aggregate(office, **args, aggregates=dict(state='known',net_pnl_usd=10.))
    identity = G.release_aggregate(office, **args, aggregates=dict(state='known',strict_equity=verified))
    stored = json.loads(office.execute('SELECT payload_json FROM sealed_views WHERE row_hash=?', (identity,)).fetchone()[0])
    assert stored['aggregates']['strict_equity'] == verified
    assert stored['aggregates']['strict_equity']['summary']['binding_kind_counts'] == {'curve_derived':1}
    office.close()


def test_two_bag_partial_is_an_unknown_summary_not_partial_liquidation(tmp_path):
    doc = fixture(tmp_path)
    path = tmp_path/'ledger.db'
    path.chmod(0o644)
    conn = L.open_ledger(path)
    columns = [row[1] for row in conn.execute('PRAGMA table_info(orders)')]
    row = dict(conn.execute('SELECT * FROM orders').fetchone())
    row.update(order_id='second', mint='other')
    conn.execute('INSERT INTO orders('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')', tuple(row[key] for key in columns))
    conn.execute("INSERT INTO marks(order_id,at,lamports,source) VALUES('second',99,'1000000000','legacy')")
    conn.close()
    path.chmod(0o444)
    doc['ledger'].update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), order_count=2, mark_count=2)
    with pytest.raises(E.SnapshotRefused) as caught:
        read(tmp_path, write(tmp_path, doc))
    assert caught.value.summary['state'] == 'unknown'
    assert caught.value.summary['known_liq_lower'] is None
    assert caught.value.summary['binding_kind_counts'] == {'curve_derived':1}
    assert caught.value.summary['unpriced_count'] == 1


@pytest.mark.parametrize('field,value', [('order_count', True), ('mark_count', None), ('books', [])])
def test_sidecar_counts_are_typed_and_match_the_whole_copy(tmp_path, field, value):
    doc = fixture(tmp_path)
    doc['ledger'][field] = value
    with pytest.raises(E.SnapshotRefused, match='counts'):
        read(tmp_path, write(tmp_path, doc))


def test_release_freezes_same_call_values_before_authorization_callback(tmp_path):
    from sniper.office import grader as G
    ref = write(tmp_path, fixture(tmp_path))
    strict = read(tmp_path, ref)
    office = S.open_store(tmp_path/'office.db')
    office.execute('INSERT INTO epochs VALUES(?,?)', ('e', S.canonical_json(dict(kind='evaluated',registered_look_at=100.)).decode()))
    def authorize(*args):
        strict['summary']['cash'] = 999.
        strict['summary']['binding_kind_counts']['curve_derived'] = 999
        return True
    identity = G.release_aggregate(office, epoch_id='e',kind='epoch_economics',released_at=101.,
        aggregates={'state':'known','strict_equity':strict}, source_heads={'grades':'a'*64}, authorize_release=authorize)
    stored = json.loads(office.execute('SELECT payload_json FROM sealed_views WHERE row_hash=?',(identity,)).fetchone()[0])
    assert stored['aggregates']['strict_equity']['summary']['cash'] == 975.
    assert stored['aggregates']['strict_equity']['summary']['binding_kind_counts'] == {'curve_derived':1}
    office.close()


def test_cashflow_metadata_max_is_cashflow_at_not_fx_at(tmp_path):
    doc = fixture(tmp_path)
    path = tmp_path/'ledger.db'
    path.chmod(0o644)
    conn = L.open_ledger(path)
    conn.execute('UPDATE cashflows SET fx_at=99')
    conn.close()
    path.chmod(0o444)
    doc['ledger']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = read(tmp_path, write(tmp_path, doc))
    assert result['summary']['cash'] == 975.


def test_snapshot_budget_expiry_refuses_before_copy(tmp_path, monkeypatch):
    ref = write(tmp_path, fixture(tmp_path))
    monkeypatch.setattr(E, 'READ_BUDGET_S', 0.)
    with pytest.raises(E.SnapshotRefused, match='budget'):
        read(tmp_path, ref)
    assert not (tmp_path/'runtime').exists()
