"""A PASS-feed sheet request carries NO 'feed' key (tools/sheet.py adds it only for near_pass). Every consumer must read absent as 'pass'."""
from types import SimpleNamespace

from sniper.office import eval_report as ER, judge_rounds as R, store as S
from sniper.office.runtime import candidate_item

E = 'office-paper-eval-1'


def pass_receipt():
    request = {'network_id': 'solana', 'mint': 'M', 'pool': 'P', 'strategy_epoch': 'c'}      # as built by SH.sheet for feed pass
    return {'cache_key': 'k', 'sheet_hash': 's', 'lenses_hash': 'l', 'request': request, 'sheet': {}, 'lenses': {},
            'clock_sample': {'corrected_utc': 10.}}


def test_candidate_item_reads_a_request_without_feed_as_pass():
    assert candidate_item(pass_receipt(), 'h' * 64)['feed'] == 'pass'
    near = pass_receipt(); near['request']['feed'] = 'near_pass'
    assert candidate_item(near, 'h' * 64)['feed'] == 'near_pass'


def test_a_real_shaped_pass_sheet_is_assigned_to_a_round(tmp_path):
    conn = S.open_store(tmp_path / 'o.db')
    item = candidate_item(pass_receipt(), 'h' * 64)
    got, _ = R.advance(conn, epoch_id=E, now=item['known_at'] + 1, judges=['j1', 'j2'],
                       tickets=lambda now: [{'ticket_id': 't', 'feed': 'pass'}], sheet=lambda tid: item)
    assert got is not None and got['item']['feed'] == 'pass'


def test_report_feed_of_a_sheet_without_a_feed_key_is_pass_and_a_missing_sheet_is_unknown():
    known = lambda request, state='known': {'kind': 'sheet', 'state': state, 'request': request}
    assert ER._feed(known({'mint': 'M'})) == 'pass'
    assert ER._feed(known({'feed': 'near_pass'})) == 'near_pass'
    assert ER._feed(known({'feed': 'weird'})) == 'unknown'
    assert ER._feed(known({'mint': 'M'}, state='unknown')) == 'unknown'      # no known-sheet evidence: stays unknown
    assert ER._feed({'request': {'mint': 'M'}}) == 'unknown'
    assert ER._feed(None) == 'unknown'
