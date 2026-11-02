"""Head/Judge operating view on a synthetic paper ledger."""
from sniper.office import eval_transition as X, ledger as L, operating_view as V

DAY = 86400. * 20000


def _ledger(tmp_path):
    conn = L.open_ledger(tmp_path / 'ledger.db')
    X.provision_ledger(conn, now=DAY)
    return conn


def _order(conn, oid, *, state, filled_at=None, closed_at=None, usd=None):
    conn.execute("INSERT INTO orders(order_id,book_id,epoch_id,book_kind,network_id,mint,pool,decision_bundle_hash,policy_json,"
                 "policy_hash,fill_model_version,state,requested_at,filled_at,spend_lamports,tokens_atomic,spend_usd,closed_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (oid, X.BOOK, X.NEW, 'evaluated', 'solana', 'M' + oid, 'P' + oid, 'b' * 64, '{}', 'h', 'office-ps-paper-v1', state,
                  DAY + 1, filled_at, '100000000', '1000', 25., closed_at))
    if usd is not None:
        conn.execute("INSERT INTO cashflows(order_id,kind,at,lamports,cost_lamports,cost_basis,cost_evidence,fx_usd,fx_at,usd) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (oid, 'exit', closed_at, '0', '0', 'x', 'x', 200., closed_at, usd))
    conn.commit()


def v(conn, now):
    return V.view(conn, book_id=X.BOOK, now=now, sol_usd=200., sol_at=now, daily_loss_usd=100., max_concurrent=3)


def test_empty_book_is_known_at_initial_capital(tmp_path):
    out = v(_ledger(tmp_path), DAY + 10)
    assert out['positions'] == [] and out['equity']['state'] == 'known' and out['equity']['usd'] == 1000.
    assert out['daily_realized_usd'] == 0 and out['daily_loss_halted'] is False and len(out['equity_snapshot_id']) == 64


def test_unpriced_position_is_never_zero(tmp_path):
    conn = _ledger(tmp_path)
    _order(conn, 'o1', state='open', filled_at=DAY + 2)
    out = v(conn, DAY + 10)
    assert out['positions'][0]['priced'] is False and out['positions'][0]['mark_age_s'] is None
    assert out['equity']['state'] != 'known' and out['equity']['usd'] is None and out['equity']['unpriced_count'] == 1


def test_daily_realized_is_the_halt_quantity(tmp_path):
    conn = _ledger(tmp_path)
    _order(conn, 'o1', state='closed', filled_at=DAY + 2, closed_at=DAY + 5, usd=-120.)
    out = v(conn, DAY + 10)
    assert out['daily_realized_usd'] == L.daily_realized_usd(conn, book_id=X.BOOK, now=DAY + 10) == -120.
    assert out['daily_loss_halted'] is True
    assert v(conn, DAY + 86400 + 10)['daily_realized_usd'] == 0          # next UTC day


def test_view_hides_evaluation_statistics_and_ids_change_with_content(tmp_path):
    conn = _ledger(tmp_path)
    a = v(conn, DAY + 10)
    assert not {'net', 'win_rate', 'grades', 'closed', 'intervals', 'look'} & set(a)
    _order(conn, 'o1', state='open', filled_at=DAY + 2)
    assert v(conn, DAY + 10)['equity_snapshot_id'] != a['equity_snapshot_id']


def test_positions_carry_the_entry_call_thesis_read_only(tmp_path):
    conn = _ledger(tmp_path)
    _order(conn, 'o1', state='open', filled_at=DAY + 2)
    call = {'public_reason': 'fresh buyers', 'disproof': 'Disproof: top10 > 40%'}
    out = V.view(conn, book_id=X.BOOK, now=DAY + 10, sol_usd=200., sol_at=DAY + 10, daily_loss_usd=100., max_concurrent=3,
                 entry_call=lambda oid: call if oid == 'o1' else None)
    assert out['positions'][0]['entry_call'] == call
    assert v(conn, DAY + 10)['positions'][0]['entry_call'] is None          # unknown, never invented


def test_stale_mark_is_unpriced_in_the_row_and_the_equity(tmp_path):
    conn = _ledger(tmp_path)
    _order(conn, 'o1', state='open', filled_at=DAY + 2)
    conn.execute("INSERT INTO marks(order_id,at,lamports,source) VALUES('o1',?, '90000000','fixture')", (DAY + 3,)); conn.commit()
    out = v(conn, DAY + 3 + V.MARK_MAX_AGE_S + 1)
    assert out['positions'][0]['priced'] is False and out['equity']['state'] != 'known'
