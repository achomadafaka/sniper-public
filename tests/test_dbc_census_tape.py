from sniper.research import dbc_census_tape as T

W = T.WSOL
OTHER = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


def mk(window=6 * 3600):
    c = T.connect(':memory:')
    T.start_run(c, 'r', 1000.0, window, 'owners=DBC program only')
    return c


def test_quote_token_pool_never_sol_and_unknown_stays_null():
    assert T.quote_class(OTHER) == 'other' and T.quote_class(W) == 'SOL' and T.quote_class(None) is None
    c = mk()
    T.insert_swap(c, 'r', 's1', 0, 1001.0, config='c', pool='p', quote_mint=OTHER)
    T.insert_swap(c, 'r', 's2', 0, 1002.0, config='c', pool='p2')
    row = c.execute('SELECT quote_decimals,err,slot_lag_s FROM swaps WHERE sig="s2"').fetchone()
    assert row == (None, None, None)
    classes = {r['quote_class'] for r in T.summary(c, 'r', 900, now=10**9) if r['swaps']}
    assert classes == {'other', None}


def test_stopped_run_reports_later_buckets_null_not_zero():
    c = mk()
    T.insert_swap(c, 'r', 's1', 0, 1100.0, config='c', quote_mint=W, pool='p', mint='m')
    T.stop_run(c, 'r', '429', 1000.0 + 3 * 3600)
    rows = T.summary(c, 'r', 3600, now=10**9)
    assert [r['swaps'] for r in rows][:1] == [1]
    assert [r['swaps'] for r in rows[1:3]] == [0, 0]       # ran, saw nothing
    assert all(r['swaps'] is None for r in rows[3:])       # hours 4-6: unknown


def test_dup_ignored_counted_failed_kept_and_drops_flag_bucket():
    c = mk()
    assert T.insert_swap(c, 'r', 's1', 0, 1001.0, config='c', quote_mint=W, err='InstructionError')
    assert not T.insert_swap(c, 'r', 's1', 0, 1002.0, config='c')
    assert T.insert_swap(c, 'r', 's1', 1, 1003.0, config='c', quote_mint=W, err='')
    T.record_drop(c, 'r', 1005.0, 7)
    r = [x for x in T.summary(c, 'r', 900, now=10**9) if x['swaps']][0]
    assert (r['swaps'], r['failed'], r['dropped']) == (2, 1, 7)
    assert c.execute('SELECT dup_ignored FROM run').fetchone()[0] == 1


def test_default_key_is_not_sol_and_partial_bucket_and_running_future_null():
    assert T.quote_class(T.SYS_DEFAULT) == 'other_or_unset'
    c = mk()
    T.insert_swap(c, 'r', 's1', 0, 1100.0, config='c', quote_mint=W, pool='p', mint='m', direction='buy',
                  amount_in='2000000000', amount_out='5', quote_decimals=9)
    T.insert_swap(c, 'r', 's2', 0, 1200.0, config='c', quote_mint=W, pool='p', mint='m', direction='sell',
                  amount_out='1000000000', amount_in='5')   # decimals unknown -> notional NULL
    rows = T.summary(c, 'r', 3600, now=1000.0 + 1800)       # running, half of hour 1 elapsed
    assert rows[0]['partial'] == 1 and rows[0]['swaps'] == 2 and rows[0]['err_unknown'] == 2
    assert (rows[0]['buys'], rows[0]['sells'], rows[0]['notional_quote_net']) == (1, 1, None)
    assert all(r['swaps'] is None for r in rows[1:])
    T.stop_run(c, 'r', '429', 1000.0 + 1800)
    assert T.summary(c, 'r', 3600, now=10**9)[0]['partial'] == 1


def test_distinct_pools_not_summed_across_quote_mints():
    c = mk()
    for i, qm in enumerate((W, OTHER)):
        T.insert_swap(c, 'r', f's{i}', 0, 1100.0, config='c', quote_mint=qm, pool=f'p{i}', mint='m')
    T.insert_swap(c, 'r', 's9', 0, 1101.0, config='c', quote_mint=W, pool='p0', mint='m')
    r = {x['quote_class']: x for x in T.summary(c, 'r', 3600, now=10**9) if x['swaps']}
    assert (r['SOL']['pools'], r['SOL']['mints'], r['other']['pools']) == (1, 1, 1)


def test_failed_swap_adds_no_notional():
    c = mk()
    T.insert_swap(c, 'r', 'a', 0, 1100.0, config='c', quote_mint=W, direction='buy', amount_in='1000000000',
                  quote_decimals=9, err='')
    T.insert_swap(c, 'r', 'b', 0, 1101.0, config='c', quote_mint=W, direction='buy', amount_in='5000000000',
                  quote_decimals=9, err='InstructionError')
    r = [x for x in T.summary(c, 'r', 3600, now=10**9) if x['swaps']][0]
    assert (r['swaps'], r['failed'], r['notional_quote_net']) == (2, 1, 1.0)
