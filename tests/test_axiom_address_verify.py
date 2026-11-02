"""S1 address verification: a directory address earns 'axiom_confirmed' only on repeated buys."""
import sqlite3

import pytest

from sniper.persistence.db_axiom import ensure_axiom_tables
from sniper.services import axiom_address_verify as av

NOW = "2026-09-16T12:00:00+00:00"


def _relay_key_fixture(chain='56'):
    import json
    from sniper.persistence import db_memecoin as m
    c = _conn(wallet_rows=[('u1', 'alice', 1, '1,56,8453,4663,5042')])
    m.ensure_address_registry_tables(c)
    origin = 'CzU8MaRcwvwUoNkwJFLbvtFWJugcEXAhDDQqNFE4ybb7'
    address = '0x'+'12'*20
    m.record_address_evidence(c, source_user_id='u1', network_id='solana', address=origin,
        status='verified', evidence_kind='fomo_buy_receipt_match', now_iso=NOW, actor='test')
    detail = dict(proof_version='solana-relay-key-v1', source_user_id='u1',
        origin_wallet=origin, origin_status_at=NOW, beneficiary=address,
        chain_code='0xef0100'+'34'*20, chain=chain, contradictions=0,
        orders=[dict(chain='56', mint='0x'+f'{i+1:040x}', tx='0x'+f'{i+1:064x}',
                     origin='5' * 87 + str(i+6), order_id='0x'+f'{i+4:064x}',
                     acquired_atomic='10', usdc_deposit_atomic='20') for i in range(3)])
    c.execute("INSERT INTO axiom_verified_addresses(source_user_id,handle,network_id,address,"
              "verify_status,verified_at,detail) VALUES('u1','alice',?,?,'axiom_confirmed',?,?)",
              (chain,address,NOW,json.dumps(detail)))
    c.commit()
    return c, address, detail


@pytest.mark.parametrize('chain',['1','56','8453','4663','5042'])
def test_signed_solana_relay_key_identity_reaches_the_armed_evm_watcher(chain):
    c, address, _ = _relay_key_fixture(chain)
    assert av.evm_watched_owners(c,chain) == {address:('u1','alice')}
    assert av.evm_watched_owners(c,'1' if chain != '1' else '56') == {}


@pytest.mark.parametrize('mutation', ['wrong_uid','wrong_beneficiary','wrong_chain','missing_code',
    'contract_code','too_few_coins','duplicate_order','missing_payment','bad_transaction',
    'unverified_origin','refuted_origin','conflicting_origin','foreign_origin_owner',
    'contradiction','stale_origin','foreign_evm_owner','foreign_legacy_owner'])
def test_signed_solana_relay_key_identity_refuses_unbound_or_ambiguous_evidence(mutation):
    import json
    c, address, detail = _relay_key_fixture()
    if mutation == 'wrong_uid':detail['source_user_id']='u2'
    elif mutation == 'wrong_beneficiary':detail['beneficiary']='0x'+'99'*20
    elif mutation == 'wrong_chain':detail['chain']='1'
    elif mutation == 'missing_code':detail['chain_code']=None
    elif mutation == 'contract_code':detail['chain_code']='0x60006000'
    elif mutation == 'too_few_coins':detail['orders'][1]['mint']=detail['orders'][0]['mint']
    elif mutation == 'duplicate_order':detail['orders'][1]['order_id']=detail['orders'][0]['order_id']
    elif mutation == 'missing_payment':detail['orders'][0]['usdc_deposit_atomic']='0'
    elif mutation == 'bad_transaction':detail['orders'][0]['tx']='invalid'
    elif mutation in ('unverified_origin','refuted_origin'):
        c.execute('UPDATE memecoin_address_registry SET status=?',
                  ('candidate' if mutation=='unverified_origin' else 'refuted',))
    elif mutation == 'conflicting_origin':c.execute('UPDATE memecoin_address_registry SET conflict_at=?',(NOW,))
    elif mutation == 'foreign_origin_owner':
        c.execute("INSERT INTO memecoin_address_registry(source_user_id,network_id,address,status,"
                  "evidence_kind,first_seen_at,status_at) SELECT 'u2',network_id,address,status,"
                  "evidence_kind,first_seen_at,status_at FROM memecoin_address_registry")
    elif mutation == 'contradiction': detail['contradictions']=1
    elif mutation == 'stale_origin': c.execute('UPDATE memecoin_address_registry SET status_at=?',('2026-10-09',))
    elif mutation == 'foreign_evm_owner':
        c.execute("INSERT INTO axiom_verified_addresses(source_user_id,handle,network_id,address,"
                  "verify_status,verified_at) VALUES('u2','bob','8453',?,'unverified',?)",(address,NOW))
    elif mutation == 'foreign_legacy_owner':
        c.execute("INSERT INTO memecoin_wallet_addresses(source_user_id,handle,network_id,address,"
                  "verify_status,resolved_at) VALUES('u2','bob','8453',?,'fomoscan_confirmed',?)",(address,NOW))
    c.execute("UPDATE axiom_verified_addresses SET detail=? WHERE source_user_id='u1'",(json.dumps(detail),))
    assert av.evm_watched_owners(c,'56') == {}


def test_signed_solana_relay_key_identity_respects_disarm_and_pause():
    c, address, _ = _relay_key_fixture()
    assert av.evm_watched_owners(c,'56') == {address:('u1','alice')}
    c.execute("UPDATE memecoin_wallets SET real_chains='1'")
    assert av.evm_watched_owners(c,'56') == {}
    c.execute("UPDATE memecoin_wallets SET real_chains='56',paused=1")
    assert av.evm_watched_owners(c,'56') == {}


def test_enrolled_identity_cannot_replace_a_trade_initiation_receipt():
    from types import SimpleNamespace
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    _, address, detail = _relay_key_fixture()
    event = SimpleNamespace(signal_source='onchain_watch', fomo_event_id='onchain:56:trade',
                            source_wallet=address, source_initiation=detail)
    assert source_initiation_refusal(event,'56') == 'wallet_initiation_unproved'


def _conn(directory_rows=(), wallet_rows=(), events=(), addresses=(), entries=()):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    c.executescript("""
        CREATE TABLE memecoin_wallets (id INTEGER PRIMARY KEY AUTOINCREMENT, source_user_id TEXT,
            handle TEXT, real_approved INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0,
            paused INTEGER DEFAULT 0, real_chains TEXT);
        CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY AUTOINCREMENT, fomo_event_id TEXT,
            source_user_id TEXT, mint TEXT, side TEXT, network_id TEXT, created_at TEXT,
            signal_source TEXT);
        CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            origin_event_id TEXT, execution_mode TEXT, entry_lag_s REAL, network_id TEXT);
        CREATE TABLE memecoin_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER,
            kind TEXT, tx_sig TEXT);
        CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, handle TEXT,
            network_id TEXT NOT NULL, address TEXT NOT NULL, resolved_at TEXT NOT NULL,
            method TEXT, coins_held INTEGER, coins_checked INTEGER, verify_status TEXT,
            verify_detail TEXT);
    """)
    for r in directory_rows:
        c.execute("INSERT INTO axiom_trader_directory(x_handle,display,address,chain,seen_at) "
                  "VALUES(?,?,?,?,?)", r)
    c.commit()
    for r in wallet_rows:
        c.execute("INSERT INTO memecoin_wallets(source_user_id,handle,real_approved,real_chains) "
                  "VALUES(?,?,?,?)", r)
    for i, r in enumerate(events):
        c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,mint,side,network_id,"
                  "created_at,signal_source) VALUES(?,?,?,?,?,?,?)", (f"e{i}",) + r)
    for event_id, lag, tx_sig in entries:
        c.execute("INSERT INTO memecoin_positions(origin_event_id,execution_mode,entry_lag_s) "
                  "VALUES(?,'real',?)", (event_id, lag))
        c.execute("INSERT INTO memecoin_fills(position_id,kind,tx_sig) VALUES(?, 'entry', ?)",
                  (c.execute("SELECT MAX(id) FROM memecoin_positions").fetchone()[0], tx_sig))
    for r in addresses:
        c.execute("INSERT INTO memecoin_wallet_addresses(source_user_id,handle,network_id,address,"
                  "resolved_at,method,verify_status) VALUES(?,?,?,?,?,?,?)", r)
    c.commit()
    return c


def _same_mint_buys(n, uid="u1", chain="solana", mint="ONEMINT"):
    """n buy events of the SAME coin — a rebuy campaign, not independent identity evidence."""
    return tuple((uid, mint, "buy", chain, f"2026-09-1{(i % 5) + 1}T10:00:00+00:00",
                  "follow_activity") for i in range(n))


def _buys(n, uid="u1", chain="solana", start=1):
    return tuple((uid, f"MINT{i}", "buy", chain, f"2026-09-1{(i % 5) + 1}T10:00:00+00:00",
                  "follow_activity") for i in range(start, start + n))


def _fixture(events, wallet=("u1", "alice", 1, "solana"), entries=()):
    return _conn(directory_rows=[("Alice", "Alice", "ADDR1", "solana", NOW)],
                 wallet_rows=[wallet], events=events, entries=entries)


def _reader(verdicts):
    """Answer each read from a list, in order; the last answer repeats."""
    calls = []

    def read(chain, address, mint, *, window_s=120, at_iso=None, anchor=None):
        calls.append((chain, address, mint, window_s, at_iso, anchor))
        return verdicts[min(len(calls) - 1, len(verdicts) - 1)]
    read.calls = calls
    return read


def test_three_matches_confirm_the_address():
    conn = _fixture(_buys(3))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert [r["verify_status"] for r in out] == ["axiom_confirmed"]
    row = conn.execute("SELECT * FROM memecoin_wallet_addresses WHERE source_user_id='u1'").fetchone()
    assert row["address"] == "ADDR1" and row["method"] == "axiom_directory"
    assert row["verify_status"] == "axiom_confirmed" and row["network_id"] == "solana"
    assert row["coins_held"] == 3 and row["coins_checked"] == 3


def test_stored_claim_without_directory_row_requires_new_purchase_proof():
    conn = _conn(wallet_rows=[('u1', 'alice', 1, '4663')], events=_buys(3, chain='4663'))
    conn.execute("INSERT INTO axiom_verified_addresses(source_user_id,handle,network_id,address,"
                 "verify_status,verified_at,detail) VALUES('u1','alice','4663','0xabc',"
                 "'axiom_confirmed',?,'{}')", (NOW,))
    conn.commit()
    assert av.confirmed_addresses(conn) == {}
    read = _reader([av.NO_DATA])
    result = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW, chain='4663')
    assert len(read.calls) == 3
    assert result[0]['verify_status'] == av.UNVERIFIED
    assert av.confirmed_addresses(conn) == {}


def test_chain_filter_runs_before_candidate_limit():
    conn = _conn(directory_rows=[('alice','Alice','0xabc','evm',NOW)],
                 wallet_rows=[('u1','alice',1,'4663,56')], events=_buys(3, chain='56'))
    read = _reader([av.BOUGHT])
    result = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW,
                                 chain='56', max_candidates=1)
    assert len(result) == 1 and result[0]['chain'] == '56'
    assert len(read.calls) == 3


def test_one_contradiction_refutes_even_with_three_matches():
    conn = _fixture(_buys(4), entries=[(f"e{i}", 3.0, f"OURTX{i}") for i in range(4)])
    read = _reader([av.BOUGHT, av.BOUGHT, av.BOUGHT, av.NO_BUY])
    out = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert out[0]["verify_status"] == "REFUTED"
    assert conn.execute("SELECT verify_status FROM memecoin_wallet_addresses").fetchone()[0] == "REFUTED"


def test_two_matches_stay_unverified():
    conn = _fixture(_buys(2))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert out[0]["verify_status"] == "unverified"


def test_no_data_counts_neither_way():
    conn = _fixture(_buys(8))
    read = _reader([av.NO_DATA, av.BOUGHT, av.NO_DATA, av.BOUGHT, av.NO_DATA, av.BOUGHT,
                    av.NO_DATA, av.NO_DATA])
    out = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert out[0]["verify_status"] == "axiom_confirmed"
    assert out[0]["matches"] == 3 and out[0]["contradictions"] == 0 and out[0]["no_data"] == 5


def test_a_fomoscan_row_is_never_overwritten():
    conn = _conn(directory_rows=[("Alice", "Alice", "ADDR1", "solana", NOW)],
                 wallet_rows=[("u1", "alice", 1, "solana")], events=_buys(3),
                 addresses=[("u1", "alice", "solana", "OLDADDR", "2026-09-01T00:00:00+00:00",
                             "fomoscan", "fomoscan_confirmed")])
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    row = conn.execute("SELECT * FROM memecoin_wallet_addresses WHERE source_user_id='u1'").fetchone()
    assert row["address"] == "OLDADDR" and row["method"] == "fomoscan"
    assert out[0]["written"] is False and out[0]["skip_reason"] == "existing_method:fomoscan"


def test_only_armed_traders_are_candidates_and_the_handle_match_ignores_case():
    conn = _conn(directory_rows=[("ALICE", "Alice", "ADDR1", "solana", NOW),
                                 ("bob", "Bob", "ADDR2", "solana", NOW)],
                 wallet_rows=[("u1", "alice", 1, "solana"), ("u2", "bob", 0, "solana")],
                 events=_buys(3) + _buys(3, uid="u2"))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert [r["source_user_id"] for r in out] == ["u1"]


def test_a_sell_event_is_never_checked():
    sells = tuple(("u1", f"S{i}", "sell", "solana", "2026-09-14T10:00:00+00:00", "follow_activity")
                  for i in range(5))
    conn = _fixture(_buys(3) + sells)
    read = _reader([av.BOUGHT])
    out = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert out[0]["checked"] == 3 and len(read.calls) == 3
    assert all(not call[2].startswith("S") for call in read.calls)


def test_events_of_another_chain_are_not_checked():
    other = tuple(("u1", f"B{i}", "buy", "56", "2026-09-14T10:00:00+00:00", "follow_activity")
                  for i in range(4))
    conn = _fixture(_buys(3) + other)
    read = _reader([av.BOUGHT])
    av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert {call[0] for call in read.calls} == {"solana"}
    assert len(read.calls) == 3


def test_max_events_bounds_the_reads():
    conn = _fixture(_buys(12))
    read = _reader([av.BOUGHT])
    av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW, max_events=5)
    assert len(read.calls) == 5


def test_an_unknown_reader_answer_counts_as_no_data():
    """A reader that returns None or a new string must never confirm an address."""
    conn = _fixture(_buys(4))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([None, "error", av.BOUGHT, av.BOUGHT]),
                               now_iso=lambda: NOW)
    assert out[0]["no_data"] == 2 and out[0]["matches"] == 2
    assert out[0]["verify_status"] == "unverified"


def test_an_event_with_a_funded_entry_of_ours_is_read_anchored_on_our_own_fill():
    """Our own entry transaction dates the window exactly; its block cannot drift."""
    conn = _fixture(_buys(1), entries=[("e0", 12.5, "OURTX1")])
    read = _reader([av.BOUGHT])
    av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert read.calls[0][5] == {"tx_sig": "OURTX1", "entry_lag_s": 12.5}


def test_an_event_we_never_copied_is_read_unanchored():
    conn = _fixture(_buys(1))
    read = _reader([av.BOUGHT])
    av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert read.calls[0][5] is None


def test_an_unanchored_miss_can_never_refute():
    """Without our own fill in the window there is no control, so 'absent' is only 'unknown'."""
    conn = _fixture(_buys(4))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.NO_BUY]), now_iso=lambda: NOW)
    assert out[0]["contradictions"] == 0 and out[0]["no_data"] == 4
    assert out[0]["unanchored"] == 4 and out[0]["verify_status"] == "unverified"


def test_an_unanchored_match_still_counts():
    conn = _fixture(_buys(3))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert out[0]["matches"] == 3 and out[0]["verify_status"] == "axiom_confirmed"


def test_an_anchored_miss_refutes():
    conn = _fixture(_buys(2), entries=[("e0", 4.0, "OURTX1"), ("e1", 4.0, "OURTX2")])
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT, av.NO_BUY]),
                               now_iso=lambda: NOW)
    assert out[0]["contradictions"] == 1 and out[0]["verify_status"] == "REFUTED"


def _lane_row(conn, uid, handle, chain, address, status):
    conn.execute("INSERT INTO axiom_verified_addresses(source_user_id, handle, x_handle, "
                 "network_id, address, verify_status, matches, contradictions, no_data, checked, "
                 "verified_at, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (uid, handle, handle, chain, address, status, 3, 0, 0, 3, NOW, '{"proof_version":"wallet-paid-v2"}'))
    conn.commit()


# ------------------------------------------------- identity evidence, not coincident activity
# Codex review 2026-09-16: three reads of one campaign, a transfer-in, or a shared router must
# never confirm an identity, and an unread candidate must never refute one.

def test_three_reads_of_one_coin_do_not_confirm():
    conn = _fixture(_same_mint_buys(5))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert out[0]["matches"] == 5 and out[0]["distinct_mints"] == 1
    assert out[0]["verify_status"] == "unverified"


def test_confirmation_needs_three_distinct_mints():
    conn = _fixture(_buys(3))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert out[0]["distinct_mints"] == 3 and out[0]["verify_status"] == "axiom_confirmed"


def test_a_transfer_in_is_not_a_buy():
    """A positive token delta with no quote spend is someone sending the coin, not buying it."""
    conn = _fixture(_buys(4))
    read = _reader([av.TRANSFER, av.TRANSFER, av.TRANSFER, av.BOUGHT])
    out = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert out[0]["transfers"] == 3 and out[0]["matches"] == 1
    assert out[0]["contradictions"] == 0            # a transfer is not a contradiction either
    assert out[0]["verify_status"] == "unverified"


def test_an_address_claimed_by_two_traders_never_confirms():
    conn = _conn(directory_rows=[("Alice", "Alice", "SHARED", "solana", NOW),
                                 ("Bob", "Bob", "SHARED", "solana", NOW)],
                 wallet_rows=[("u1", "alice", 1, "solana"), ("u2", "bob", 1, "solana")],
                 events=_buys(3) + _buys(3, uid="u2", start=11))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert {r["verify_status"] for r in out} == {"shared_executor"}
    assert av.confirmed_addresses(conn) == {}


def test_a_shared_executor_already_in_the_table_blocks_a_later_trader():
    conn = _conn(directory_rows=[("Bob", "Bob", "SHARED", "solana", NOW)],
                 wallet_rows=[("u2", "bob", 1, "solana")], events=_buys(3, uid="u2"))
    conn.execute("INSERT INTO axiom_verified_addresses(source_user_id, handle, x_handle, "
                 "network_id, address, verify_status, verified_at) VALUES "
                 "('u1','alice','Alice','solana','SHARED','axiom_confirmed',?)", (NOW,))
    conn.commit()
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert out[0]["verify_status"] == "shared_executor"
    # and the earlier confirmation is retired, because the same address cannot be two traders
    assert av.confirmed_addresses(conn) == {}


def test_the_same_trader_on_two_chains_is_not_a_shared_executor():
    conn = _conn(directory_rows=[("Alice", "Alice", "SAME", "solana", NOW),
                                 ("Alice", "Alice", "SAME", "bsc", NOW)],
                 wallet_rows=[("u1", "alice", 1, "solana,56")],
                 events=_buys(3) + tuple(("u1", f"B{i}", "buy", "56",
                                          "2026-09-14T10:00:00+00:00", "follow_activity")
                                         for i in range(3)))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert {r["verify_status"] for r in out} == {"axiom_confirmed"}


def test_confirmed_addresses_serves_only_confirmed_rows_of_the_lane_table():
    conn = _conn()
    _lane_row(conn, "u1", "alice", "solana", "ADDR1", "axiom_confirmed")
    _lane_row(conn, "u2", "bob", "56", "ADDR2", "REFUTED")
    _lane_row(conn, "u3", "carol", "4663", "ADDR3", "unverified")
    assert av.confirmed_addresses(conn) == {("solana", "ADDR1"): "alice"}


def test_the_watcher_book_unions_fomoscan_and_axiom_confirmations():
    """The watcher gets both proven sources and nothing else."""
    conn = _conn(addresses=[
        ("u4", "dave", "solana", "LEGACY_OK", NOW, "fomoscan", "fomoscan_confirmed"),
        ("u5", "erin", "solana", "LEGACY_BAD", NOW, "holder_intersection", "REFUTED"),
        ("u6", "frank", "4663", "LEGACY_OLD", NOW, "axiom_directory", "unverified")])
    _lane_row(conn, "u1", "alice", "solana", "ADDR1", "axiom_confirmed")
    _lane_row(conn, "u1", "alice", "56", "ADDR2", "axiom_confirmed")
    _lane_row(conn, "u2", "bob", "4663", "ADDR3", "REFUTED")
    assert av.watcher_address_book(conn) == {
        ("solana", "ADDR1"): "alice", ("56", "ADDR2"): "alice", ("solana", "LEGACY_OK"): "dave"}


def test_a_trader_keeps_one_verified_row_per_chain():
    """The lane table is keyed per chain, so a two-chain trader no longer loses one of them."""
    conn = _conn(directory_rows=[("Alice", "Alice", "SOLADDR", "solana", NOW),
                                 ("Alice", "Alice", "BSCADDR", "bsc", NOW)],
                 wallet_rows=[("u1", "alice", 1, "solana,56")],
                 events=_buys(3) + tuple(("u1", f"B{i}", "buy", "56",
                                          "2026-09-14T10:00:00+00:00", "follow_activity")
                                         for i in range(3)))
    av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT network_id, address, verify_status FROM axiom_verified_addresses")}
    assert rows == {"solana": ("SOLADDR", "axiom_confirmed"),
                    "56": ("BSCADDR", "axiom_confirmed")}
    # the legacy table still carries one row, for readers that only know it
    legacy = conn.execute("SELECT network_id, address, method FROM memecoin_wallet_addresses "
                          "WHERE source_user_id='u1'").fetchall()
    assert len(legacy) == 1 and legacy[0][2] == "axiom_directory"


def test_the_lane_row_is_written_even_when_the_legacy_row_belongs_to_fomoscan():
    conn = _conn(directory_rows=[("Alice", "Alice", "ADDR1", "solana", NOW)],
                 wallet_rows=[("u1", "alice", 1, "solana")], events=_buys(3),
                 addresses=[("u1", "alice", "solana", "OLDADDR", NOW, "fomoscan",
                             "fomoscan_confirmed")])
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert out[0]["written"] is False and out[0]["lane_written"] is True
    assert conn.execute("SELECT address FROM axiom_verified_addresses").fetchone()[0] == "ADDR1"
    assert conn.execute("SELECT address FROM memecoin_wallet_addresses").fetchone()[0] == "OLDADDR"


def test_a_re_run_updates_the_lane_row_in_place():
    conn = _fixture(_buys(4), entries=[(f"e{i}", 2.0, f"TX{i}") for i in range(4)])
    av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    av.verify_candidates(conn, rpc_read_fn=_reader([av.NO_BUY]), now_iso=lambda: NOW)
    rows = [tuple(r) for r in conn.execute(
        "SELECT verify_status, contradictions FROM axiom_verified_addresses")]
    assert rows == [("REFUTED", 4)]


def test_a_second_address_on_the_same_chain_cannot_displace_a_confirmed_one():
    """The lane key holds one address per chain, so the stronger verdict of a run wins."""
    conn = _conn(directory_rows=[("Alice", "Alice", "GOOD", "solana", NOW),
                                 ("Alice", "Alice", "OTHER", "solana", NOW)],
                 wallet_rows=[("u1", "alice", 1, "solana")], events=_buys(3))
    verdicts = {"GOOD": av.BOUGHT, "OTHER": av.NO_DATA}

    def read(chain, address, mint, *, window_s=120, at_iso=None, anchor=None):
        return verdicts[address]

    out = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    row = tuple(conn.execute("SELECT address, verify_status FROM axiom_verified_addresses").fetchone())
    assert row == ("GOOD", "axiom_confirmed")
    weaker = [r for r in out if r["address"] == "OTHER"][0]
    assert weaker["lane_written"] is False and weaker["skip_reason"] == "weaker_than:axiom_confirmed"


def test_a_trader_with_no_events_on_that_chain_is_unverified_and_reads_nothing():
    conn = _fixture(())
    read = _reader([av.BOUGHT])
    out = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert out[0]["verify_status"] == "unverified" and out[0]["checked"] == 0
    assert read.calls == []


def test_the_window_passed_to_the_reader_is_the_spec_window():
    conn = _fixture(_buys(1))
    read = _reader([av.BOUGHT])
    av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert read.calls[0][3] == 120 and read.calls[0][4] == "2026-09-12T10:00:00+00:00"


# ------------------------------------------------------------------ public-RPC readers

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
HEAD = 2_000_000
BEND = HEAD - 200_000          # newer blocks run at 0.05 s, older ones at 0.10 s
BASE = 1_700_000_000


def _ts_of(block):
    if block <= BEND:
        return BASE + int(block * 0.10)
    return BASE + int(BEND * 0.10) + int((block - BEND) * 0.05)


def _paid_receipt(block, wallet):
    def transfer(token, sender, receiver, amount):
        return {"address": token, "topics": [TRANSFER, "0x" + "0" * 24 + sender[2:],
                                              "0x" + "0" * 24 + receiver[2:]], "data": hex(amount)}
    router = "0x" + "ab" * 20
    return {"status": "0x1", "blockNumber": hex(block), "from": wallet, "logs": [
        transfer("0x" + "cc" * 20, router, wallet, 7),
        transfer("0x" + "77" * 20, wallet, router, 10 ** 18)]}


def _evm_chain(true_block, receivers, *, logs_empty=False, fail=None):
    """A chain whose block rate CHANGES, so one linear estimate off the head cannot find a block."""
    seen = {"windows": [], "calls": 0}

    def call(url, method, params):
        seen["calls"] += 1
        if method == fail:
            return None, "boom"
        if method == "eth_blockNumber":
            return hex(HEAD), None
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(_ts_of(int(params[0], 16)))}, None
        if method == "eth_getTransactionReceipt":
            who = params[0].replace("0xtx-", "0x")
            return _paid_receipt(true_block, who), None
        if method == "eth_getLogs":
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            seen["windows"].append((lo, hi))
            if logs_empty or not (lo <= true_block <= hi):
                return [], None
            return [{"topics": [TRANSFER, "0x" + "0" * 24 + "ab" * 10, "0x" + "0" * 24 + r[2:]],
                     "data": hex(5), "transactionHash": "0xtx-" + r[2:]} for r in receivers], None
        raise AssertionError(method)
    call.seen = seen
    return call


def _read(call):
    return av.public_rpc_reader(call_fn=call, sleep_fn=lambda _s: None,
                                quote_token_fn=lambda _: "0x" + "77" * 20)


def test_evm_finds_the_buy_through_a_rate_change():
    true_block = HEAD - 900_000
    addr = "0x" + "11" * 20
    call = _evm_chain(true_block, [addr])
    verdict = _read(call)("4663", addr.upper(), "0x" + "cc" * 20,
                          at_iso=_iso(_ts_of(true_block)))
    assert verdict == av.BOUGHT
    lo, hi = call.seen["windows"][-1]
    assert lo <= true_block <= hi


def test_evm_reports_no_buy_when_the_token_moved_without_the_address():
    true_block = HEAD - 900_000
    call = _evm_chain(true_block, ["0x" + "22" * 20])
    assert _read(call)("4663", "0x" + "11" * 20, "0x" + "cc" * 20,
                       at_iso=_iso(_ts_of(true_block))) == av.NO_BUY


def test_evm_empty_window_is_no_data_not_a_contradiction():
    true_block = HEAD - 900_000
    call = _evm_chain(true_block, ["0x" + "11" * 20], logs_empty=True)
    assert _read(call)("4663", "0x" + "11" * 20, "0x" + "cc" * 20,
                       at_iso=_iso(_ts_of(true_block))) == av.NO_DATA


def test_evm_rpc_failure_is_no_data():
    call = _evm_chain(HEAD - 900_000, ["0x" + "11" * 20], fail="eth_getLogs")
    assert _read(call)("4663", "0x" + "11" * 20, "0x" + "cc" * 20,
                       at_iso=_iso(_ts_of(HEAD - 900_000))) == av.NO_DATA


def test_a_chain_without_a_public_reader_is_no_data():
    call = _evm_chain(HEAD, ["0x" + "11" * 20])
    assert _read(call)("5042", "0x" + "11" * 20, "0x" + "cc" * 20, at_iso=_iso(BASE)) == av.NO_DATA


def test_base_reads_through_its_own_public_endpoint():
    seen = []

    def call(url, method, params):
        seen.append(url)
        if method == "eth_blockNumber":
            return hex(HEAD), None
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(_ts_of(int(params[0], 16)))}, None
        if method == "eth_getLogs":
            return [], None
        raise AssertionError(method)

    assert _read(call)("8453", "0x" + "11" * 20, "0x" + "cc" * 20,
                       at_iso=_iso(_ts_of(HEAD - 10_000))) == av.NO_DATA
    assert set(seen) == {"https://mainnet.base.org"}


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _solana_chain(sig_times, balances, *, oldest_extra=None):
    def call(url, method, params):
        if method == "getSignaturesForAddress":
            rows = [{"signature": f"sig{i}", "blockTime": t} for i, t in enumerate(sig_times)]
            if oldest_extra is not None:
                rows.append({"signature": "sigold", "blockTime": oldest_extra})
            return rows, None
        if method == "getTransaction":
            return {"meta": balances.get(params[0], {})}, None
        raise AssertionError(method)
    return call


def _bal(owner, mint, pre, post):
    """A BUY: the coin arrives and SOL leaves the same wallet in the same transaction."""
    return {"preTokenBalances": [{"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(pre)}}],
            "postTokenBalances": [{"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(post)}}],
            "preBalances": [10 ** 9], "postBalances": [10 ** 9 - 10 ** 8], "fee": 5000,
            "accountKeys": [owner]}


def test_solana_payment_and_token_deltas_alone_remain_unknown():
    at = BASE
    call = _solana_chain([at + 5], {"sig0": _bal("WALLET", "MINT", 0, 500)}, oldest_extra=at - 5000)
    assert _read(call)("solana", "WALLET", "MINT", at_iso=_iso(at)) == av.NO_DATA


def test_solana_covered_window_without_a_delta_is_a_contradiction():
    at = BASE
    call = _solana_chain([at + 5], {"sig0": _bal("WALLET", "OTHERMINT", 0, 500)},
                         oldest_extra=at - 5000)
    assert _read(call)("solana", "WALLET", "MINT", at_iso=_iso(at)) == av.NO_BUY


def test_solana_history_that_never_reaches_the_window_is_no_data():
    at = BASE
    call = _solana_chain([at + 100_000, at + 90_000], {})      # all newer than the window
    assert _read(call)("solana", "WALLET", "MINT", at_iso=_iso(at)) == av.NO_DATA


# ------------------------------------------------------- anchored reads (our own fill dates them)

OUR = "0x" + "99" * 20


def _anchored_chain(anchor_block, receivers, *, receipt=True):
    seen = {"windows": []}

    def call(url, method, params):
        if method == "eth_getTransactionReceipt":
            if not receipt:
                return None, "not found"
            if params[0] != "0xdead":
                return _paid_receipt(anchor_block, params[0].replace("0xtx-", "0x")), None
            return {"blockNumber": hex(anchor_block), "from": OUR, "logs": []}, None
        if method == "eth_blockNumber":
            return hex(HEAD), None
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(_ts_of(int(params[0], 16)))}, None
        if method == "eth_getLogs":
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            seen["windows"].append((lo, hi))
            return [{"topics": [TRANSFER, "0x" + "0" * 24 + "ab" * 10, "0x" + "0" * 24 + r[2:]],
                     "data": hex(7), "transactionHash": "0xdead" if r == OUR else "0xtx-" + r[2:]} for r in receivers], None
        raise AssertionError(method)
    call.seen = seen
    return call


def test_anchored_evm_read_finds_the_trader_beside_our_own_buy():
    trader = "0x" + "11" * 20
    call = _anchored_chain(HEAD - 500_000, [OUR, trader])
    verdict = _read(call)("4663", trader, "0x" + "cc" * 20, at_iso=_iso(BASE),
                          anchor={"tx_sig": "0xdead", "entry_lag_s": 20.0})
    assert verdict == av.BOUGHT
    lo, hi = call.seen["windows"][-1]
    assert lo < HEAD - 500_000 < hi


def test_anchored_evm_read_refutes_when_only_our_own_buy_is_there():
    call = _anchored_chain(HEAD - 500_000, [OUR])
    assert _read(call)("4663", "0x" + "11" * 20, "0x" + "cc" * 20, at_iso=_iso(BASE),
                       anchor={"tx_sig": "0xdead", "entry_lag_s": 20.0}) == av.NO_BUY


def test_anchored_evm_read_without_our_own_buy_is_no_data():
    """The control failed: the window does not contain our fill, so it proves nothing."""
    call = _anchored_chain(HEAD - 500_000, ["0x" + "22" * 20])
    assert _read(call)("4663", "0x" + "11" * 20, "0x" + "cc" * 20, at_iso=_iso(BASE),
                       anchor={"tx_sig": "0xdead", "entry_lag_s": 20.0}) == av.NO_DATA


def test_anchored_evm_read_without_a_receipt_is_no_data():
    call = _anchored_chain(HEAD - 500_000, [OUR, "0x" + "11" * 20], receipt=False)
    assert _read(call)("4663", "0x" + "11" * 20, "0x" + "cc" * 20, at_iso=_iso(BASE),
                       anchor={"tx_sig": "0xdead", "entry_lag_s": 20.0}) == av.NO_DATA


def test_anchored_solana_read_dates_the_window_by_our_transaction():
    """The event clock is a foreign clock; our own transaction is the one that dates the window."""
    our_time = BASE + 10_000
    balances = {"OURTX": {"blockTime": our_time}, "sig0": _bal("WALLET", "MINT", 0, 900)}

    def call(url, method, params):
        if method == "getTransaction":
            if params[0] == "OURTX":
                return {"blockTime": our_time, "meta": {}}, None
            return {"meta": balances.get(params[0], {})}, None
        if method == "getSignaturesForAddress":
            return [{"signature": "sig0", "blockTime": our_time - 30},
                    {"signature": "old", "blockTime": our_time - 9_000}], None
        raise AssertionError(method)

    verdict = _read(call)("solana", "WALLET", "MINT", at_iso=_iso(BASE - 50_000),
                          anchor={"tx_sig": "OURTX", "entry_lag_s": 15.0})
    assert verdict == av.NO_DATA


def _lagged_chain(anchor_block, trader_block, trader):
    """Our own transfer sits at the anchor; the trader's sits earlier, by more than the margin."""
    def call(url, method, params):
        if method == "eth_getTransactionReceipt":
            if params[0] != "0xdead":
                return _paid_receipt(anchor_block, params[0].replace("0xtx-", "0x")), None
            return {"blockNumber": hex(anchor_block), "from": OUR, "logs": []}, None
        if method == "eth_blockNumber":
            return hex(HEAD), None
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(_ts_of(int(params[0], 16)))}, None
        if method == "eth_getLogs":
            lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            out = []
            for block, who in ((anchor_block, OUR), (trader_block, trader)):
                if lo <= block <= hi:
                    out.append({"topics": [TRANSFER, "0x" + "0" * 24 + "ab" * 10,
                                           "0x" + "0" * 24 + who[2:]], "data": hex(7),
                                "transactionHash": "0xdead" if who == OUR else "0xtx-" + who[2:]})
            return out, None
        raise AssertionError(method)
    return call


def test_the_anchored_window_reaches_back_by_our_copy_lag():
    """A slow copy still sees the trader's buy: the window opens entry_lag before our fill."""
    trader = "0x" + "11" * 20
    anchor_block = HEAD - 50_000
    call = _lagged_chain(anchor_block, anchor_block - 10_000, trader)
    assert _read(call)("4663", trader, "0x" + "cc" * 20, at_iso=_iso(BASE),
                       anchor={"tx_sig": "0xdead", "entry_lag_s": 600.0}) == av.BOUGHT
    # the same trade, read as if we had copied instantly, falls outside the window
    assert _read(call)("4663", trader, "0x" + "cc" * 20, at_iso=_iso(BASE),
                       anchor={"tx_sig": "0xdead", "entry_lag_s": 0.0}) == av.NO_BUY


def test_events_we_funded_are_checked_first_and_unfunded_ones_fill_the_rest():
    """An anchored read can confirm AND refute, so spend the budget on those first."""
    conn = _fixture(_buys(8), entries=[("e2", 5.0, "OURTX2"), ("e5", 5.0, "OURTX5")])
    read = _reader([av.BOUGHT])
    av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW, max_events=4)
    anchors = [call[5] for call in read.calls]
    assert len(anchors) == 4
    assert [a["tx_sig"] for a in anchors[:2] if a] == ["OURTX2", "OURTX5"]  # newest first
    assert anchors[2] is None and anchors[3] is None


def test_the_budget_still_holds_when_every_event_was_funded():
    conn = _fixture(_buys(6), entries=[(f"e{i}", 5.0, f"TX{i}") for i in range(6)])
    read = _reader([av.BOUGHT])
    av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW, max_events=3)
    assert len(read.calls) == 3 and all(call[5] for call in read.calls)


# ------------------------------------------- readers: a delta is not a buy, a cap is not a "no"

def _evm_swap_chain(anchor_block, *, token_to, quote_from=None, receipt_from=None,
                    receipt_fail=False, extra_candidates=0, other_token_leg=False):
    """Token transfers in the window, plus a per-tx receipt that says who paid."""
    QUOTE = "0x" + "77" * 20

    def topic(addr):
        return "0x" + "0" * 24 + addr[2:]

    def call(url, method, params):
        if method == "eth_getTransactionReceipt":
            tx = params[0]
            if receipt_fail:
                return None, "receipt unavailable"
            if tx == "0xanchor":
                return {"blockNumber": hex(anchor_block), "from": OUR, "logs": []}, None
            logs = [{"address": "0x" + "cc" * 20,
                     "topics": [TRANSFER, topic("0x" + "ab" * 20), topic(token_to)],
                     "data": hex(7)}]
            if other_token_leg:
                logs.append({"address": "0x" + "55" * 20,
                             "topics": [TRANSFER, topic("0x" + "f7" * 20),
                                        topic("0x" + "b9" * 20)], "data": hex(598918556)})
            if quote_from:
                logs.append({"address": QUOTE, "topics": [TRANSFER, topic(quote_from),
                                                          topic("0x" + "ab" * 20)],
                             "data": hex(10 ** 18)})
            return {"status": "0x1", "blockNumber": hex(anchor_block), "from": receipt_from or "0x" + "ee" * 20,
                    "logs": logs}, None
        if method == "eth_blockNumber":
            return hex(HEAD), None
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(_ts_of(int(params[0], 16)))}, None
        if method == "eth_getLogs":
            out = [{"topics": [TRANSFER, topic("0x" + "ab" * 20), topic(OUR)], "data": hex(5),
                    "transactionHash": "0xanchor"}]
            for i in range(1 + extra_candidates):
                out.append({"topics": [TRANSFER, topic("0x" + "ab" * 20), topic(token_to)],
                            "data": hex(7), "transactionHash": "0xbuy%d" % i})
            return out, None
        raise AssertionError(method)
    call.quote = QUOTE
    return call


def _strict_read(call, quote_token="0x" + "77" * 20):
    return av.public_rpc_reader(call_fn=call, sleep_fn=lambda _s: None,
                                quote_token_fn=lambda _chain: quote_token)


ANCHOR = {"tx_sig": "0xanchor", "entry_lag_s": 20.0}


def test_evm_counts_a_buy_when_the_address_paid_the_quote_asset():
    trader = "0x" + "11" * 20
    call = _evm_swap_chain(HEAD - 500_000, token_to=trader, quote_from=trader)
    assert _strict_read(call)("4663", trader, "0x" + "cc" * 20, at_iso=_iso(BASE),
                              anchor=ANCHOR) == av.BOUGHT


def test_evm_signature_without_payment_is_only_a_transfer():
    trader = "0x" + "11" * 20
    call = _evm_swap_chain(HEAD - 500_000, token_to=trader, receipt_from=trader)
    assert _strict_read(call)("4663", trader, "0x" + "cc" * 20, at_iso=_iso(BASE),
                              anchor=ANCHOR) == av.TRANSFER


def test_evm_unrelated_second_token_does_not_prove_relayer_payment():
    trader = "0x" + "11" * 20
    call = _evm_swap_chain(HEAD - 500_000, token_to=trader, other_token_leg=True)
    assert _strict_read(call)("4663", trader, "0x" + "cc" * 20, at_iso=_iso(BASE),
                              anchor=ANCHOR) == av.NO_DATA


def test_evm_calls_a_bare_credit_a_transfer_not_a_buy():
    """Someone sent the coin in: no quote left the address and it signed nothing."""
    trader = "0x" + "11" * 20
    call = _evm_swap_chain(HEAD - 500_000, token_to=trader)
    assert _strict_read(call)("4663", trader, "0x" + "cc" * 20, at_iso=_iso(BASE),
                              anchor=ANCHOR) == av.TRANSFER


def test_evm_unreadable_receipt_is_no_data():
    trader = "0x" + "11" * 20
    call = _evm_swap_chain(HEAD - 500_000, token_to=trader, quote_from=trader, receipt_fail=True)
    assert _strict_read(call)("4663", trader, "0x" + "cc" * 20, at_iso=_iso(BASE),
                              anchor=ANCHOR) == av.NO_DATA


def test_evm_leaves_candidates_unchecked_as_no_data():
    """More candidate transactions than the read budget: an unchecked one cannot be ruled out."""
    trader = "0x" + "11" * 20
    call = _evm_swap_chain(HEAD - 500_000, token_to=trader, extra_candidates=6)
    assert _strict_read(call)("4663", trader, "0x" + "cc" * 20, at_iso=_iso(BASE),
                              anchor=ANCHOR) == av.NO_DATA


def _sol_chain(sig_times, metas, *, oldest=None, missing_time=False, fail_tx=()):
    def call(url, method, params):
        if method == "getSignaturesForAddress":
            rows = [{"signature": f"sig{i}", "blockTime": t} for i, t in enumerate(sig_times)]
            if missing_time:
                rows.append({"signature": "sigunknown", "blockTime": None})
            if oldest is not None:
                rows.append({"signature": "sigold", "blockTime": oldest})
            return rows, None
        if method == "getTransaction":
            if params[0] in fail_tx:
                return None, "tx read failed"
            if params[0] == "OURTX":
                return {"blockTime": BASE, "meta": {}}, None
            return {"meta": metas.get(params[0], {})}, None
        raise AssertionError(method)
    return call


def _sol_meta(*, token_delta, sol_spent=0, quote_delta=0, owner="WALLET", mint="MINT"):
    pre = [{"owner": owner, "mint": mint, "uiTokenAmount": {"amount": "0"}}]
    post = [{"owner": owner, "mint": mint, "uiTokenAmount": {"amount": str(token_delta)}}]
    if quote_delta:
        pre.append({"owner": owner, "mint": av.SOL_MINT,
                    "uiTokenAmount": {"amount": str(abs(quote_delta))}})
        post.append({"owner": owner, "mint": av.SOL_MINT, "uiTokenAmount": {"amount": "0"}})
    return {"preTokenBalances": pre, "postTokenBalances": post,
            "preBalances": [10 ** 9], "postBalances": [10 ** 9 - sol_spent], "fee": 5000,
            "accountKeys": [owner]}


def test_solana_sol_debit_does_not_prove_a_purchase():
    call = _sol_chain([BASE + 5], {"sig0": _sol_meta(token_delta=900, sol_spent=10 ** 8)},
                      oldest=BASE - 5000)
    assert _strict_read(call)("solana", "WALLET", "MINT", at_iso=_iso(BASE)) == av.NO_DATA


def test_solana_calls_a_delta_without_spend_a_transfer():
    call = _sol_chain([BASE + 5], {"sig0": _sol_meta(token_delta=900)}, oldest=BASE - 5000)
    assert _strict_read(call)("solana", "WALLET", "MINT", at_iso=_iso(BASE)) == av.TRANSFER


def test_solana_failed_candidate_reads_are_no_data():
    call = _sol_chain([BASE + 5], {"sig0": _sol_meta(token_delta=900, sol_spent=10 ** 8)},
                      oldest=BASE - 5000, fail_tx=("sig0",))
    assert _strict_read(call)("solana", "WALLET", "MINT", at_iso=_iso(BASE)) == av.NO_DATA


def test_solana_more_candidates_than_the_read_budget_is_no_data():
    times = [BASE + i for i in range(12)]
    call = _sol_chain(times, {f"sig{i}": _sol_meta(token_delta=0) for i in range(12)},
                      oldest=BASE - 5000)
    assert _strict_read(call)("solana", "WALLET", "MINT", at_iso=_iso(BASE)) == av.NO_DATA


def test_solana_a_signature_without_a_timestamp_leaves_coverage_unknown():
    call = _sol_chain([BASE + 5], {"sig0": _sol_meta(token_delta=0)}, oldest=BASE - 5000,
                      missing_time=True)
    assert _strict_read(call)("solana", "WALLET", "MINT", at_iso=_iso(BASE)) == av.NO_DATA


# ------------------------------------- Axiom keeps ONE generic EVM address (Alpha, 2026-09-16)

def test_a_generic_evm_row_is_tested_on_each_evm_chain_separately():
    """Axiom stores chain='evm' for one address across every EVM chain. The address may be the
    trader on one chain and unproven on another, so each chain gets its own verdict."""
    evm_buys = tuple(("u1", f"0xM{i}", "buy", chain, "2026-09-14T10:00:00+00:00", "follow_activity")
                     for chain in ("4663", "56") for i in range(3))
    # the 56 events are ones we funded, so a miss there is a real contradiction; an unanchored
    # miss could only ever be no_data
    conn = _conn(directory_rows=[("Alice", "Alice", "0xEVM", "evm", NOW)],
                 wallet_rows=[("u1", "alice", 1, "4663,56")], events=evm_buys,
                 entries=[(f"e{i}", 3.0, f"TX{i}") for i in (3, 4, 5)])
    verdicts = {"4663": av.BOUGHT, "56": av.NO_BUY}

    def read(chain, address, mint, *, window_s=120, at_iso=None, anchor=None):
        return verdicts[chain]

    out = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    by_chain = {r["chain"]: r["verify_status"] for r in out}
    # chain 1 joined the EVM chains 2026-09-30 (eth-mainnet spec): no events there, so unverified
    assert by_chain == {"4663": "axiom_confirmed", "56": "REFUTED", "8453": "unverified",
                        "1": "unverified"}
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT network_id, verify_status FROM axiom_verified_addresses")}
    assert rows == {"4663": "axiom_confirmed", "56": "REFUTED"}      # 8453 anchored nothing


def test_a_generic_evm_row_reads_nothing_on_a_chain_with_no_events():
    conn = _conn(directory_rows=[("Alice", "Alice", "0xEVM", "evm", NOW)],
                 wallet_rows=[("u1", "alice", 1, "4663")],
                 events=tuple(("u1", f"0xM{i}", "buy", "4663", "2026-09-14T10:00:00+00:00",
                               "follow_activity") for i in range(3)))
    read = _reader([av.BOUGHT])
    out = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert {call[0] for call in read.calls} == {"4663"}
    empty = [r for r in out if r["chain"] == "56"][0]
    assert empty["checked"] == 0 and empty["lane_written"] is False
    assert empty["skip_reason"] == "no_events_on_chain"


def test_a_named_chain_is_not_fanned_out():
    conn = _fixture(_buys(3))
    out = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert [r["chain"] for r in out] == ["solana"]


def test_evm_self_transfer_with_quote_payment_does_not_prove_acquisition():
    trader = "0x" + "11" * 20
    call = _evm_swap_chain(HEAD - 500_000, token_to=trader, quote_from=trader)
    def self_transfer(url, method, params):
        result, error = call(url, method, params)
        if method == "eth_getTransactionReceipt" and params[0] != "0xanchor":
            result["logs"][0]["topics"][1] = result["logs"][0]["topics"][2]
        return result, error
    assert _strict_read(self_transfer)("4663", trader, "0x" + "cc" * 20,
                                      anchor=ANCHOR) != av.BOUGHT


def test_historical_window_bounds_use_timestamps_at_both_edges():
    true_block = HEAD - 900_000
    call = _evm_chain(true_block, ["0x" + "22" * 20])
    at = _ts_of(true_block)
    _read(call)("4663", "0x" + "11" * 20, "0x" + "cc" * 20, at_iso=_iso(at))
    lo, hi = call.seen["windows"][-1]
    assert _ts_of(lo) >= at - av.WINDOW_S
    assert _ts_of(lo - 1) < at - av.WINDOW_S
    assert _ts_of(hi) <= at + av.WINDOW_S
    assert _ts_of(hi + 1) > at + av.WINDOW_S


def test_block_search_budget_exhaustion_is_unknown():
    assert av._block_at_time(lambda b: b * 10, 15555, 1, 1000000, 10,
                             max_reads=1) is None


def test_old_confirmation_without_current_payment_proof_is_not_watched():
    conn = _conn()
    _lane_row(conn, "u1", "alice", "4663", "0xaaa", "axiom_confirmed")
    conn.execute("UPDATE axiom_verified_addresses SET detail='{}'")
    assert av.confirmed_addresses(conn) == {}
    assert av.watcher_address_owners(conn) == {}


def test_readonly_verification_returns_proof_without_database_writes():
    conn = _fixture(_buys(3))
    conn.execute("PRAGMA query_only=ON")
    result = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW,
                                   write_results=False, max_candidates=1)
    assert result[0]["verify_status"] == av.CONFIRMED
    assert result[0]["lane_written"] is False
    assert conn.execute("SELECT COUNT(*) FROM axiom_verified_addresses").fetchone()[0] == 0


def test_max_candidates_bounds_chain_expansion():
    conn = _conn(directory_rows=[("alice", "Alice", "0xaaa", "evm", NOW)],
                 wallet_rows=[("u1", "alice", 1, "4663")],
                 events=_buys(3, chain="4663") + _buys(3, chain="56"))
    read = _reader([av.BOUGHT])
    result = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW,
                                   max_candidates=1, write_results=False)
    assert len(result) == 1 and {c[0] for c in read.calls} == {"4663"}


def test_explicit_research_link_uses_events_without_funded_authority():
    conn = _conn(directory_rows=[("otherhandle", "Other", "ADDR1", "solana", NOW)],
                 wallet_rows=[("u1", "alice", 0, "")], events=_buys(3))
    conn.execute("INSERT INTO axiom_research_accounts(chain,address,handle,active,requested_source_user_id,updated_at) VALUES "
                 "('solana','ADDR1','otherhandle',1,'u1',?)", (NOW,))
    conn.commit()
    result = av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW)
    assert len(result) == 1 and result[0]["verify_status"] == av.CONFIRMED
    assert conn.execute("SELECT real_approved FROM memecoin_wallets").fetchone()[0] == 0


def test_an_external_account_without_a_source_link_has_no_fabricated_identity():
    conn = _conn(directory_rows=[("external", "External", "ADDR1", "solana", NOW)])
    conn.execute("INSERT INTO axiom_research_accounts(chain,address,handle,active,requested_source_user_id,updated_at) VALUES "
                 "('solana','ADDR1','external',1,NULL,?)", (NOW,))
    assert av.verify_candidates(conn, rpc_read_fn=_reader([av.BOUGHT]), now_iso=lambda: NOW) == []


def test_verifier_does_not_use_its_own_onchain_detections_as_identity_evidence():
    events = tuple(("u1", f"M{i}", "buy", "solana", NOW, "onchain_watch") for i in range(3))
    conn = _fixture(events)
    read = _reader([av.BOUGHT])
    result = av.verify_candidates(conn, rpc_read_fn=read, now_iso=lambda: NOW)
    assert read.calls == [] and result[0]["verify_status"] == av.UNVERIFIED


def test_anchored_negative_requires_the_exact_control_transaction():
    trader = "0x" + "11" * 20
    call = _anchored_chain(HEAD - 500_000, [OUR])
    def different_control(url, method, params):
        result, error = call(url, method, params)
        if method == "eth_getLogs":
            for log in result:
                log["transactionHash"] = "0xdifferent"
        return result, error
    assert _strict_read(different_control)("4663", trader, "0x" + "cc" * 20,
                                           anchor={"tx_sig": "0xdead"}) == av.NO_DATA


def test_candidate_collision_preserves_the_fomo_owner():
    conn = _conn(directory_rows=[('alice','Alice','0xABC','4663',NOW)],
                 wallet_rows=[('u1','alice',1,'4663')], events=_buys(3,chain='4663'),
                 addresses=[('u2','bob','4663','0xabc',NOW,'fomoscan',av.LEGACY_CONFIRMED)])
    read = _reader([av.BOUGHT])
    result = av.verify_candidates(conn,rpc_read_fn=read,now_iso=lambda:NOW)
    assert result[0]['verify_status'] == av.SHARED and read.calls == []
    assert av.watcher_address_owners(conn) == {('4663','0xabc'): ('u2','bob')}
    assert conn.execute('SELECT source_user_id,verify_status FROM memecoin_wallet_addresses '
                         "WHERE source_user_id='u2'").fetchone()[:] == ('u2',av.LEGACY_CONFIRMED)


def test_conflicting_axiom_proof_cannot_replace_the_fomo_owner_on_read():
    conn = _conn(addresses=[('u2','bob','4663','0xabc',NOW,'fomoscan',av.LEGACY_CONFIRMED)])
    _lane_row(conn,'u1','alice','4663','0xABC',av.CONFIRMED)
    assert av.confirmed_addresses(conn) == {}
    assert av.watcher_address_owners(conn) == {('4663','0xabc'): ('u2','bob')}


def test_explicit_manual_research_address_does_not_need_a_directory_row():
    conn = _conn(wallet_rows=[('u1','alice',0,'')], events=_buys(3))
    conn.execute("INSERT INTO axiom_research_accounts(chain,address,handle,active,requested_source_user_id,updated_at) VALUES ('solana','MANUAL','external',1,'u1',?)",(NOW,))
    result = av.verify_candidates(conn,rpc_read_fn=_reader([av.BOUGHT]),now_iso=lambda:NOW)
    assert len(result) == 1 and result[0]['address'] == 'MANUAL'
    assert result[0]['verify_status'] == av.CONFIRMED


def test_solana_gift_plus_account_rent_does_not_prove_a_buy():
    call = _sol_chain([BASE + 5], {'sig0':_sol_meta(token_delta=900,sol_spent=2039280)},
                      oldest=BASE-5000)
    read = _strict_read(call)
    assert read('solana','WALLET','MINT',at_iso=_iso(BASE)) == av.NO_DATA
    assert read.last_reason == 'solana_payment_relation_unsupported'
