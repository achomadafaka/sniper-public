import pytest
from sniper.persistence.db_wallet_execution import open_store, freeze_manifest, record_origin, ingest_episode, advance_cursor, cursor
from test_wallet_execution_evidence import episode


def test_store_is_immutable_and_keeps_atomic_text(tmp_path):
    db = open_store(tmp_path/'research.db')
    e = episode(); ingest_episode(db, e)
    row = db.execute('SELECT acquired_atomic,typeof(acquired_atomic) AS type FROM we_episodes').fetchone()
    assert row['acquired_atomic'] == '1000000' and row['type'] == 'text'
    ingest_episode(db, e)
    e['frames'][120]['buy']['fee_usd'] = 9.
    with pytest.raises(ValueError, match='immutable'): ingest_episode(db, e)
    assert db.execute('SELECT count(*) FROM we_episodes').fetchone()[0] == 1


def test_origin_is_admission_independent_and_deduped(tmp_path):
    db = open_store(tmp_path/'r.db')
    source = {'upstream_event_id':'a', 'source_user_id':'u', 'network_id':'56', 'mint':'coin',
              'normalized_side':'buy', 'observed_at':'2026-10-04T12:00:00+00:00', 'raw_json':'{}'}
    record_origin(db, source); record_origin(db, source)
    assert db.execute('SELECT count(*) FROM we_origins').fetchone()[0] == 1
    source['mint'] = 'different'
    with pytest.raises(ValueError, match='immutable'): record_origin(db, source)


def test_cursors_are_append_only_and_never_rewind(tmp_path):
    db = open_store(tmp_path/'r.db')
    advance_cursor(db, 'feed', 12); advance_cursor(db, 'feed', 20)
    assert cursor(db, 'feed') == 20
    with pytest.raises(ValueError): advance_cursor(db, 'feed', 10)
    assert db.execute('SELECT count(*) FROM we_cursors').fetchone()[0] == 2


def test_frozen_manifest_cannot_silently_change_roster(tmp_path):
    db = open_store(tmp_path/'r.db')
    a = freeze_manifest(db, {'members':[{'wallet':'u','chain':'56'}], 'sealed_at':0})
    b = freeze_manifest(db, {'members':[{'wallet':'u','chain':'8453'}], 'sealed_at':1})
    assert a != b and db.execute('SELECT count(*) FROM we_manifests').fetchone()[0] == 2
