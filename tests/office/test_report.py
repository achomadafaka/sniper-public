import json
from sniper.office import store
from sniper.office.report import read_state, daily


def test_report_numbers_trace_to_released_rows(tmp_path):
    conn = store.open_store(tmp_path / 'office.db')
    store.append(conn, 'sealed_views', dict(epoch_id='e', kind='epoch_economics', released_at=100., aggregates=dict(state='known', opportunity_count=12, entered_count=3, net_pnl_usd=7.5, cost_usd=25., office_net_usd=-17.5), source_heads={'grades':'a'*64}))
    conn.execute('INSERT INTO messages VALUES(?,?)', ('head', json.dumps(dict(kind='head_report', text='Keep collecting.', author_role='Head', known_at=150., refs=[1]))))
    conn.commit()
    state = read_state(tmp_path, now=200., stale_after_s=30.)
    text = daily(state)
    assert '12 eligible' in text and '3 entered' in text
    assert '$7.50' in text and '$25.00' in text and '-$17.50' in text
    assert 'Keep collecting.' in text and 'unverified' in text
    assert text == daily(state)


def test_daily_delivery_is_explicit_and_getters_do_not_send(tmp_path):
    from sniper.office.report import send_daily
    state=read_state(tmp_path,now=200.)
    calls=[]
    assert send_daily(state,send_alert=lambda **kw:calls.append(kw)) is None
    assert calls==[dict(kind='office-daily',text=daily(state))]
    assert 'Agents in loaded window: unknown' in daily(state)


def test_released_credits_remain_separate_and_never_add_to_equity(tmp_path):
    conn=store.open_store(tmp_path/'office.db')
    a=dict(agent_id='fired-agent',metric='entered_coins',value=None,sample_n=5,censored_n=1,note='Discovery, not evidence.',epoch_id='e',epoch_kind='discovery',look_id=None,additive=False)
    store.append(conn,'sealed_views',dict(epoch_id='e',kind='descriptive_credit',released_at=100.,aggregates=a,source_heads={'grades':'a'*64}))
    state=read_state(tmp_path,now=200.)
    assert state['credits']['items'][0]['value'] is None
    assert state['credits']['items'][0]['agent_id']=='fired-agent'
    assert state['equity']['state']=='unknown'
    assert 'credit_total' not in state and 'credit_total' not in state['credits']


# --- per-pod open bags (Gamma Finding 2): a pod stuck in unsold bags must not read as neutral ---

def _bag_state(items, *, state='known', limited=False, now=10_000.):
    base = dict(agents=dict(state='unknown', items=[]), office=dict(state='known'), ledger=dict(state='known'),
                equity=dict(state='known'), sealed_views=dict(state='known', items=[]), clock=dict(now=now),
                positions=dict(state=state, items=items, limited=limited))
    return base


def _pos(book, state, filled_at=None, mark_state='priced', after=False):
    return dict(book_id=book, state=state, filled_at=filled_at, mark_state=mark_state, observed_after_cut=after)


def test_open_bags_are_counted_per_book_with_age_and_unpriced():
    from sniper.office.report import open_bags
    s = _bag_state([_pos('e:pod1', 'open', 10_000. - 7200, 'stale'), _pos('e:pod1', 'open', 10_000. - 60),
                    _pos('e:pod2', 'closed', 1.), _pos('e:pod2', 'pending'), _pos('e:pod3', 'open', 20_000., after=True)])
    bags = open_bags(s)
    assert bags['state'] == 'known' and bags['limited'] is False
    assert bags['items'] == [dict(book_id='e:pod1', open_n=2, oldest_age_s=7200., unpriced_n=1)]
    assert 'Open bags: e:pod1 2 (oldest 2.0h, 1 not priced).' in daily(s)


def test_open_bags_unknown_stays_unknown():
    from sniper.office.report import open_bags
    assert open_bags(_bag_state([], state='read_error'))['state'] == 'unknown'
    assert 'Open bags: unknown.' in daily(_bag_state([], state='read_error'))
    assert open_bags(_bag_state([_pos('e:pod1', 'open', 9_000.)], now=None))['state'] == 'unknown'
    s = _bag_state([_pos('e:pod1', 'open', None)])                          # no fill time: age unknown
    assert open_bags(s)['items'][0]['oldest_age_s'] is None
    assert 'oldest unknown' in daily(s)
    assert 'Open bags: none in loaded window.' in daily(_bag_state([]))
    assert 'at least' in daily(_bag_state([_pos('e:pod1', 'open', 9_000.)], limited=True))


def test_read_state_carries_open_bags_for_the_view(tmp_path):
    state = read_state(tmp_path, now=200.)                                    # no ledger on disk
    assert state['open_bags'] == dict(state='unknown', items=[], limited=False)


def test_open_bags_through_the_real_reader_with_no_clock_is_unknown(tmp_path, monkeypatch):
    # Zeta HOLD: with now=None the reader marks every order observed_after_cut; that must not read as 'none'
    from sniper.office import ledger as L
    from sniper.office.report import open_bags
    from .test_ledger import NOW, Quotes, opened, policy, setup
    q = Quotes()
    monkeypatch.setattr(L, "_ps_buy", q.buy)
    monkeypatch.setattr(L, "_ps_sell", q.sell)
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / 'ledger.db')
    setup(conn, "A", 1000.0)
    opened(conn, q, policy(), "o1")
    conn.close()
    blind = read_state(tmp_path, now=None)
    assert blind['positions']['state'] == 'known' and blind['positions']['items']
    assert blind['open_bags']['state'] == 'unknown'
    assert 'Open bags: unknown.' in daily(blind)
    seen = read_state(tmp_path, now=NOW + 3600)
    assert [(b['book_id'], b['open_n']) for b in seen['open_bags']['items']] == [('A', 1)]


def test_a_malformed_position_row_makes_open_bags_unknown_not_a_crash():
    from sniper.office.report import open_bags
    s = _bag_state([dict(state='open', filled_at=1., mark_state='priced', observed_after_cut=False)])
    assert open_bags(s)['state'] == 'unknown'
    assert 'Open bags: unknown.' in daily(s)
