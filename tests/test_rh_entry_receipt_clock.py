"""The first sequencer receipt clock precedes decode and survives duplicate/reask storage."""
import json
import sqlite3

from sniper.services import memecoin_rh_feed_shadow as S
from sniper.util.receipt_clock import ReceiptClock
import pytest


def test_frame_stamps_before_decode_and_preserves_first_receipt(monkeypatch, tmp_path):
    from test_memecoin_rh_feed_shadow import _shadow, _frame, RAW
    producer = ReceiptClock('rh:4663')
    first = producer.capture(monotonic_ns=100)
    second = producer.capture(monotonic_ns=200)
    stamps = iter([first, second])
    captured = []
    shadow, records = _shadow()
    shadow.receipt_producer = producer
    def capture():
        captured.append(True)
        return next(stamps)
    monkeypatch.setattr(producer, 'capture', capture)
    decode = S.tx_to_input
    def decode_after_stamp(raw):
        assert captured, 'decode ran before receipt capture'
        return decode(raw)
    monkeypatch.setattr(S, 'tx_to_input', decode_after_stamp)
    stats = dict(frames=0, cpu_ms=0., frame_ms_sum=0., frame_ms_max=0.)
    S.process_frame(shadow, stats, _frame([RAW]), now_utc=lambda: first['corrected_utc'])
    S.process_frame(shadow, stats, _frame([RAW], seq=2), now_utc=lambda: second['corrected_utc'])
    assert records[0]['receipt_clock'] == first
    assert records[1]['receipt_clock'] == first
    db = tmp_path / 'rh.db'
    S.write_records(records, db_path=str(db), tape_path=str(tmp_path / 'missing.db'))
    with sqlite3.connect(db) as conn:
        values = conn.execute('SELECT receipt_clock_json FROM rh_feed_signals ORDER BY id').fetchall()
    assert [json.loads(value) for (value,) in values] == [first, first]


@pytest.mark.parametrize('chain,expected', [('56', 'evm:56'), ('4663', 'evm:4663'), ('8453', None)])
def test_watcher_original_clock_producer_is_chain_scoped(chain, expected):
    from scripts.research import onchain_evm_wallet_watch as watch
    producer = watch.source_receipt_producer(chain)
    assert producer is None if expected is None else producer.producer_id.startswith(expected + ':')
