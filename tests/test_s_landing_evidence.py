"""Landing evidence keeps unreadable pages, units and signed outcomes unknown."""
import sqlite3
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/research"))

def test_census_null_page_is_not_successful_zero_coverage(monkeypatch):
    import s_landing_census as census
    def rpc(method, params):
        if method == 'getTransaction':
            return {'slot': 100}
        if method == 'getTransactionsForAddress':
            return None
        raise AssertionError(method)
    monkeypatch.setattr(census.fet, 'rpc', rpc)
    monkeypatch.setattr(census.ccf, 'fee_facts', lambda tx: {'priority_lamports': 0, 'cu_price': 0, 'cu_limit': 1, 'sol_out': []})
    row = census.one(('aid', 'solana:source', 'mint', 'ours', 'failed', 'cap', '2026-10-09'), [0], 10)
    assert row.get('error') or row.get('censored'), row


def _replay_fixture(*, wrong_mint_first=False, quote='So11111111111111111111111111111111111111112'):
    import sol_signal_window_tape as tw
    c = sqlite3.connect(':memory:')
    c.executescript(tw.DDL)
    if wrong_mint_first:
        c.execute('INSERT INTO signals(signal_key,source_sig,mint,first_decided_at,attempts_json,s_slot,landing_sig,landing_slot,status) VALUES (?,?,?,?,?,?,?,?,?)',
                  ('source:Other', 'source', 'Other', '2026-10-09', '[]', 100, 'wrong-landing', 105, 'read'))
    c.execute('INSERT INTO signals(signal_key,source_sig,mint,first_decided_at,attempts_json,s_slot,landing_sig,landing_slot,status) VALUES (?,?,?,?,?,?,?,?,?)',
              ('source:CoinA', 'source', 'CoinA', '2026-10-09', '[]', 100, 'right-landing', 102, 'read'))
    c.execute('INSERT INTO coverage VALUES (?,?,?,?,?,?)', ('CoinA', 100, 1000, 0, 'source:CoinA', 'dense'))
    for sig, slot, index, bt in [('source', 100, 1, 1000), ('right-landing', 102, 1, 1001), ('exit', 260, 1, 1060)]:
        c.execute('INSERT INTO pool_swaps(signature,mint,venue,coin_vault,quote_mint,quote_decimals,coin_decimals,coin_reserve_after,quote_reserve_after,price,cp,fee_bps,slot,tx_index,block_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (sig, 'CoinA', 'pump', 'vault', quote, 9, 6, '1000000000000', '1000000000000', 0.001, 1, 125, slot, index, bt))
    ledger = sqlite3.connect(':memory:')
    ledger.execute('CREATE TABLE memecoin_positions(id INTEGER,realized_pnl REAL)')
    ledger.execute('INSERT INTO memecoin_positions VALUES (1,0)')
    send = ('uid', 'h', 'aid', 'solana:source', 'CoinA', 'filled', 1, '2026-10-09', '{}')
    return c, ledger, send


def test_replay_binds_signal_to_its_own_mint():
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture(wrong_mint_first=True)
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=110, cap_pct=30, tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger)
    assert row['late_uncapped']['landing_offset_slots'] == 2, row['late_uncapped']
    assert row['today_replayed'] is not None


def test_replay_censors_unsupported_quote_units():
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture(quote='UnsupportedQuote')
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=110, cap_pct=30, tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger)
    assert row.get('censored') == 'quote_usd_unknown', row


def test_control_single_mint_supported_quote_replays():
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture()
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=110, cap_pct=30, tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger)
    assert row['late_uncapped']['landing_offset_slots'] == 2
    assert row['today_replayed'] is not None


@pytest.mark.parametrize("skew,age", [(-1, None), (0, 5.0)])
def test_delayed_feed_head_cannot_identify_bank_or_fee_movable_subset(skew, age):
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture()
    trace = json.dumps({"substage_events": [{"name": "slot_receipt",
                       "head_at_recv_minus_s": skew, "head_at_recv_age_ms": age}]})
    send = (*send[:8], trace)
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=110, cap_pct=30,
                            tiers=(0.0015, 0.01), today_priority_sol=0.0015, ledger=ledger)
    assert row.get("feed_head_proxy") is True
    assert row.get("bank_feasible") is None
    report = replay.wallet_report([row], (0.0015, 0.01), 110, 0.0015)
    assert report.get("option_a_go_no_go_early") is None
    assert report["bank_feasibility"] == "censored_missing_bank_phase_witness"
    assert report["feed_head_proxy_share"] == {"early": 1, "late": 0, "unknown": 0}


def test_control_census_valid_page_includes_source(monkeypatch):
    import s_landing_census as census
    source = {'slot':100, 'transactionIndex':1, 'transaction':{'signatures':['source']}}
    monkeypatch.setattr(census.fet, 'rpc', lambda method,params: source if method=='getTransaction' else {'data':[source]})
    monkeypatch.setattr(census.ccf, 'fee_facts', lambda tx: {'priority_lamports':0,'cu_price':0,'cu_limit':1,'sol_out':[]})
    row = census.one(('aid','solana:source','mint','ours','failed','cap','2026-10-09'),[0],10)
    assert row['source_index'] == 1 and row['buyers'] == []


def test_replay_keeps_unresolved_signed_send_unknown():
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture()
    send = tuple(list(send[:5]) + ['submitted_unknown', None] + list(send[7:]))
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=110, cap_pct=30, tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger)
    assert row.get('censored') or (row['today_usd'] is None and row['today_replayed'] is None), row


def test_control_confirmed_failed_send_keeps_known_priority_cost():
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture()
    send = (*send[:5], 'failed', None, *send[7:], '2026-10-09T12:00:00+00:00')
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=110, cap_pct=30, tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger)
    assert row['today_usd'] == -0.165 and row['today_replayed']['pnl_30s'] == -0.17


@pytest.mark.parametrize('page', [None, {}, {'data': None}, {'data': {}}, {'data': [None]},
                                  {'data': [{'slot': 100}]},
                                  {'error': 'unavailable', 'data': [{'slot': 100, 'transactionIndex': 0,
                                     'transaction': {'signatures': ['source']}}]}])
def test_census_unreadable_page_is_censored(monkeypatch, page):
    import s_landing_census as census
    monkeypatch.setattr(census.fet, 'rpc', lambda method, params: {'slot': 100}
                        if method == 'getTransaction' else page)
    row = census.one(('aid', 'solana:source', 'mint', 'ours', 'failed', 'cap', 'at'), [0], 10)
    assert row.get('censored') == 'page_unreadable'
    assert 'buyers' not in row
    report = census.summary([row], 110)
    assert report['census_known'] == 0
    assert report['censored']['page_unreadable'] == 1


def test_census_missing_source_order_is_unknown(monkeypatch):
    import s_landing_census as census
    monkeypatch.setattr(census.fet, 'rpc', lambda method, params: {'slot': 100}
                        if method == 'getTransaction' else {'data': []})
    row = census.one(('aid', 'solana:source', 'mint', 'ours', 'failed', 'cap', 'at'), [0], 10)
    assert row.get('censored') == 'source_order_unknown'
    assert 'buyers' not in row


@pytest.mark.parametrize('status', ['submitted_unknown', 'unknown', 'transaction_unresolved',
                                   'retry_pending', 'needs_review', 'rejected'])
def test_replay_unknown_baseline_cannot_enter_paired_comparison(status):
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture()
    send = (*send[:5], status, None, *send[7:])
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=110, cap_pct=30,
                            tiers=(0.0015, 0.01), today_priority_sol=0.0015, ledger=ledger)
    assert row['today_usd'] is None and row['today_replayed'] is None
    report = replay.wallet_report([row], (0.0015, 0.01), 110, 0.0015)
    assert report['today_known'] == 0 and report['today_replayed_n'] == 0
    assert report['today_unknown'] == 1
    assert report['option_a_go_no_go'] == {}
    assert row['late_uncapped'] is None


def test_replay_missing_matching_mint_is_censored():
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture(wrong_mint_first=True)
    c.execute("DELETE FROM signals WHERE mint='CoinA'")
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=110, cap_pct=30,
                            tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger)
    assert row.get('censored') == 'no_tape_row'


@pytest.mark.parametrize('sol_usd', [0.0, float('nan'), float('inf')])
def test_replay_unpriced_native_quote_is_censored(sol_usd):
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture()
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=sol_usd, cap_pct=30,
                            tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger)
    assert row.get('censored') == 'quote_usd_unknown'


def test_census_page_timeout_is_censored(monkeypatch):
    import s_landing_census as census
    def rpc(method, params):
        if method == 'getTransaction':
            return {'slot': 100}
        raise TimeoutError()
    monkeypatch.setattr(census.fet, 'rpc', rpc)
    row = census.one(('aid', 'solana:source', 'mint', 'ours', 'failed', 'cap', 'at'), [0], 10)
    assert row.get('censored') == 'page_unreadable'


def test_census_cursor_cannot_repeat_or_hide_a_short_page(monkeypatch):
    import s_landing_census as census
    source = {'slot': 100, 'transactionIndex': 0, 'transaction': {'signatures': ['source']}}
    pages = iter([{'data': [source], 'paginationToken': 'next'},
                  {'data': [source], 'paginationToken': 'next'}])
    calls = []
    def rpc(method, params):
        calls.append(method)
        return source if method == 'getTransaction' else next(pages)
    monkeypatch.setattr(census.fet, 'rpc', rpc)
    row = census.one(('aid', 'solana:source', 'mint', 'ours', 'failed', 'cap', 'at'), [0], 10)
    assert row.get('censored') == 'page_unreadable'
    assert calls.count('getTransactionsForAddress') == 2


def test_failed_without_chain_confirmation_does_not_imply_a_paid_fee():
    import s_landing_replay as replay
    c, ledger, send = _replay_fixture()
    send = (*send[:5], 'failed', None, *send[7:])
    row = replay.replay_send(c, send, clip_usd=110, sol_usd=110, cap_pct=30,
                            tiers=(0.0015,), today_priority_sol=0.0015, ledger=ledger)
    assert row['today_usd'] is None and row['today_replayed'] is None


def test_send_reader_carries_recorded_chain_confirmation():
    import s_landing_replay as replay
    c = sqlite3.connect(':memory:')
    c.executescript('''
        CREATE TABLE memecoin_wallets(source_user_id TEXT, handle TEXT);
        CREATE TABLE memecoin_entry_attempts(source_user_id TEXT, source_handle TEXT, attempt_id TEXT,
            origin_event_id TEXT, mint TEXT, terminal_status TEXT, position_id INTEGER,
            decision_started_at TEXT, trace_json TEXT, network_id TEXT, delivery_lane TEXT, tx_sig TEXT,
            chain_confirmation_completed_at TEXT);
    ''')
    c.execute('INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
              ('uid', 'h', 'aid', 'solana:source', 'CoinA', 'failed', None, '2026-10-09', '{}',
               'solana', 'slot_lane', 'ours', '2026-10-09T12:00:00+00:00'))
    assert replay.sends(c, '2026-10-09')[0][-1] == '2026-10-09T12:00:00+00:00'


def _census_full_tx(sig, index):
    return {'slot': 100, 'transactionIndex': index, 'transaction': {'signatures': [sig],
            'message': {'accountKeys': ['buyer'], 'header': {'numRequiredSignatures': 1},
                        'instructions': []}},
            'meta': {'err': None, 'fee': 5000, 'preTokenBalances': [], 'postTokenBalances': [],
                     'preBalances': [10000], 'postBalances': [5000], 'logMessages': []}}


def _census_balance(mint):
    return {'owner': 'buyer', 'mint': mint, 'accountIndex': 0,
            'uiTokenAmount': {'amount': '1000000', 'decimals': 6}}


@pytest.mark.parametrize('missing', ['preTokenBalances', 'postTokenBalances', 'meta', 'message',
                                     'err', 'owner', 'fee', 'preBalances', 'postBalances'])
def test_census_partial_transaction_body_is_unknown(monkeypatch, missing):
    import s_landing_census as census
    source = _census_full_tx('source', 0)
    buyer = _census_full_tx('buyer-sig', 1)
    buyer['meta']['preTokenBalances'] = [_census_balance(census.ccf.dfx.USDC)]
    buyer['meta']['postTokenBalances'] = [_census_balance('CoinA')]
    if missing == 'meta':
        del buyer['meta']
    elif missing == 'message':
        del buyer['transaction']['message']
    elif missing == 'owner':
        del buyer['meta']['postTokenBalances'][0]['owner']
    else:
        del buyer['meta'][missing]
    monkeypatch.setattr(census.fet, 'rpc', lambda method, params: source if method == 'getTransaction'
                        else {'data': [source, buyer]})
    monkeypatch.setattr(census.jev, 'bundle_of', lambda sig: None)
    row = census.one(('aid', 'solana:source', 'CoinA', 'ours', 'failed', 'cap', 'at'), [0], 10)
    assert row.get('censored') == 'page_unreadable'
    assert census.summary([row], 110)['census_known'] == 0


def test_census_actual_decoder_distinguishes_buy_from_complete_empty_arrays(monkeypatch):
    import s_landing_census as census
    source = _census_full_tx('source', 0)
    buyer = _census_full_tx('buyer-sig', 1)
    buyer['meta']['preTokenBalances'] = [_census_balance(census.ccf.dfx.USDC)]
    buyer['meta']['postTokenBalances'] = [_census_balance('CoinA')]
    page = [source, buyer]
    monkeypatch.setattr(census.fet, 'rpc', lambda method, params: source if method == 'getTransaction'
                        else {'data': page})
    monkeypatch.setattr(census.jev, 'bundle_of', lambda sig: None)
    send = ('aid', 'solana:source', 'CoinA', 'ours', 'failed', 'cap', 'at')
    report = census.summary([census.one(send, [0], 10)], 110)
    assert report['census_known'] == 1 and report['in_s']['n'] == 1
    page[1] = copy.deepcopy(buyer)
    page[1]['meta']['preTokenBalances'] = page[1]['meta']['postTokenBalances'] = []
    report = census.summary([census.one(send, [0], 10)], 110)
    assert report['census_known'] == 1 and report['in_s']['n'] == 0


@pytest.mark.parametrize('required', [0, -1, 2])
def test_census_malformed_signer_count_is_unknown(monkeypatch, required):
    import s_landing_census as census
    source = _census_full_tx('source', 0)
    buyer = _census_full_tx('buyer-sig', 1)
    buyer['meta']['preTokenBalances'] = [_census_balance(census.ccf.dfx.USDC)]
    buyer['meta']['postTokenBalances'] = [_census_balance('CoinA')]
    buyer['transaction']['message']['header']['numRequiredSignatures'] = required
    monkeypatch.setattr(census.fet, 'rpc', lambda method, params: source if method == 'getTransaction'
                        else {'data': [source, buyer]})
    monkeypatch.setattr(census.jev, 'bundle_of', lambda sig: None)
    row = census.one(('aid', 'solana:source', 'CoinA', 'ours', 'failed', 'cap', 'at'), [0], 10)
    assert row.get('censored') == 'page_unreadable'

