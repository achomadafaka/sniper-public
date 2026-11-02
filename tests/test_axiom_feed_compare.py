"""S4: Axiom wallet events beside J7 relays, on ONE local clock. Table-driven, no network."""
import sqlite3
import json

import pytest

from sniper.persistence.db_axiom import ensure_axiom_tables
from sniper.services import axiom_feed_compare as fc
from sniper.services.axiom_address_verify import PROOF_VERSION

PROOF = json.dumps({"proof_version": PROOF_VERSION})

MS = 1_000_000
ADDR = "0xTRADER"
NOW = "2026-09-16T12:00:00+00:00"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_axiom_tables(c)
    c.executescript("""
        CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY AUTOINCREMENT, fomo_event_id TEXT,
            source_user_id TEXT, source_handle TEXT, mint TEXT, side TEXT, their_size_usd REAL,
            network_id TEXT, created_at TEXT, received_at TEXT, signal_source TEXT);
        CREATE TABLE memecoin_wallet_addresses (source_user_id TEXT PRIMARY KEY, handle TEXT,
            network_id TEXT NOT NULL, address TEXT NOT NULL, resolved_at TEXT NOT NULL,
            method TEXT, coins_held INTEGER, coins_checked INTEGER, verify_status TEXT,
            verify_detail TEXT);
    """)
    c.execute("INSERT INTO axiom_verified_addresses(source_user_id, handle, x_handle, network_id, "
              "address, verify_status, verified_at, detail) VALUES('u1','alice','Alice','solana',?, "
              "'axiom_confirmed',?,?)", (ADDR, NOW, PROOF))
    c.commit()
    return c


def _axiom(c, *, mint="MINT1", side="buy", amount=100.0, mono=1000 * MS,
           at="2026-09-16T12:00:00+00:00", address=ADDR, chain="solana", tx_sig=None, kind="live", transport="ws", domain="host:boot"):
    c.execute("INSERT INTO axiom_wallet_events(received_mono_ns, received_at, address, chain, "
              "mint, side, amount_usd, tx_sig) VALUES (?,?,?,?,?,?,?,?)",
              (mono, at, address, chain, mint, side, amount, tx_sig))
    c.execute('UPDATE axiom_wallet_events SET observation_kind=?, transport=?, clock_domain=? WHERE id=last_insert_rowid()',
              (kind, transport, domain))
    c.commit()


def _j7(c, *, event_id="e1", mint="MINT1", side="buy", amount=100.0, mono=None,
        at="2026-09-16T12:00:00+00:00", chain="solana", uid="u1", receipt=True, sig=None, domain="host:boot", created_at=None):
    c.execute("INSERT INTO memecoin_events(fomo_event_id, source_user_id, source_handle, mint, "
              "side, their_size_usd, network_id, created_at, received_at, signal_source) "
              "VALUES (?,?,?,?,?,?,?,?,?, 'follow_activity')",
              (event_id, uid, "alice", mint, side, amount, chain, created_at or at, at))
    if receipt:
        c.execute("INSERT INTO j7_receipts(fomo_event_id, received_mono_ns, received_at) "
                  "VALUES (?,?,?)", (event_id, mono if mono is not None else 900 * MS, at))
        identity = {'source_user_id': uid, 'chain': chain, 'mint': mint, 'side': side, 'amount_usd': amount}
        if sig:
            identity['tx_sig'] = sig
        c.execute('UPDATE j7_receipts SET clock_domain=?, identity_json=? WHERE fomo_event_id=?',
                  (domain, json.dumps(identity), event_id))
    c.commit()


def test_a_pair_matches_weakly_and_reports_the_arrival_difference():
    c = _conn()
    _j7(c, mono=900 * MS)
    _axiom(c, mono=1000 * MS)
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 1 and out["weak"] == 1 and out["exact"] == 0
    assert out["median_diff_ms"] == 100.0 and out["p95_diff_ms"] == 100.0
    assert out["only_axiom"] == 0 and out["only_j7"] == 0


def test_exact_matching_reports_itself_unavailable_without_a_signature_column():
    """memecoin_events stores no transaction signature, so 'no exact match' is unknown, not zero."""
    c = _conn()
    _j7(c)
    _axiom(c, tx_sig="SIG1")
    report = fc.compare_feeds(c)
    assert report["exact_matching"] == "receipt identity; legacy event signature unavailable"
    assert report["chains"]["solana"]["exact"] == 0


def test_a_signature_column_is_used_when_it_exists():
    c = _conn()
    c.execute("ALTER TABLE memecoin_events ADD COLUMN tx_sig TEXT")
    _j7(c, amount=999.0, mono=900 * MS)      # weak match would FAIL on these fields
    c.execute("UPDATE memecoin_events SET tx_sig='SIG1'")
    c.commit()
    _axiom(c, tx_sig="SIG1", mono=1000 * MS)
    report = fc.compare_feeds(c)
    assert report["exact_matching"] == "receipt identity; legacy event signature tx_sig"
    assert report["chains"]["solana"]["exact"] == 1 and report["chains"]["solana"]["weak"] == 0


def test_amounts_further_than_five_percent_apart_do_not_match():
    c = _conn()
    _j7(c, amount=100.0)
    _axiom(c, amount=106.0)
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 0 and out["only_axiom"] == 1 and out["only_j7"] == 1


def test_a_relay_more_than_the_window_apart_does_not_match():
    c = _conn()
    _j7(c, at="2026-09-16T12:00:00+00:00")
    _axiom(c, at="2026-09-16T12:05:00+00:00", mono=300_900 * MS)
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 0 and out["only_axiom"] == 1 and out["only_j7"] == 1


def test_only_in_one_feed_is_counted_per_feed():
    c = _conn()
    _j7(c, event_id="e1", mint="MINT1", mono=900 * MS)
    _axiom(c, mint="MINT1", mono=1000 * MS)
    _axiom(c, mint="MINT2", mono=1100 * MS)
    _j7(c, event_id="e2", mint="MINT3", mono=1200 * MS)
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 1 and out["only_axiom"] == 1 and out["only_j7"] == 1


def test_two_amount_matches_are_ambiguous_without_signatures():
    c = _conn()
    _j7(c, mono=900 * MS)
    _axiom(c, mono=1000 * MS)
    _axiom(c, mono=1010 * MS)
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 0 and out["duplicates"] == 0 and out["ambiguous_axiom"] == 2


def test_a_relay_without_a_receipt_stamp_cannot_time_the_pair():
    """A downstream event cannot substitute for a missing receipt."""
    c = _conn()
    _j7(c, receipt=False)
    _axiom(c)
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 0 and out["unobserved_j7"] == 1 and out["median_diff_ms"] is None


def test_stamps_from_different_boots_are_not_compared():
    """monotonic_ns resets at reboot. Two stamps whose wall clocks are hours apart cannot be
    subtracted, so the pair counts as matched but untimed."""
    c = _conn()
    _j7(c, at="2026-09-16T12:00:00+00:00", mono=900_000 * MS)   # 15 minutes of uptime
    _axiom(c, at="2026-09-16T12:00:01+00:00", mono=5 * MS, domain="host:boot-2")      # 5 ms of uptime: another boot
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 1 and out["timed"] == 0 and out["clock_incomparable"] == 1


def test_chains_are_reported_separately():
    c = _conn()
    c.execute("INSERT INTO axiom_verified_addresses(source_user_id, handle, x_handle, network_id, "
              "address, verify_status, verified_at, detail) VALUES('u2','bob','Bob','4663','0xBOB', "
              "'axiom_confirmed',?,?)", (NOW, PROOF))
    c.commit()
    _j7(c, mono=900 * MS)
    _axiom(c, mono=1000 * MS)
    _j7(c, event_id="e9", chain="4663", uid="u2", mint="0xcc", mono=800 * MS)
    _axiom(c, address="0xBOB", chain="4663", mint="0xcc", mono=1200 * MS)
    chains = fc.compare_feeds(c)["chains"]
    assert set(chains) == {"solana", "4663"}
    assert chains["solana"]["median_diff_ms"] == 100.0
    assert chains["4663"]["median_diff_ms"] == 400.0


def test_an_axiom_row_whose_address_is_not_verified_is_reported_unattributed():
    c = _conn()
    _axiom(c, address="0xSTRANGER")
    report = fc.compare_feeds(c)
    assert report["unattributed_axiom_rows"] == 1
    # it belongs to no tracked trader, so it is evidence about neither feed's coverage
    assert "solana" not in report["chains"]


def test_a_relay_of_another_trader_is_not_the_match():
    """Two tracked traders buy the same coin seconds apart. The Axiom row belongs to ONE of them."""
    c = _conn()
    c.execute("INSERT INTO axiom_verified_addresses(source_user_id, handle, x_handle, network_id, "
              "address, verify_status, verified_at, detail) VALUES('u2','bob','Bob','solana','0xBOB', "
              "'axiom_confirmed',?,?)", (NOW, PROOF))
    c.commit()
    _j7(c, event_id="bob-1", uid="u2", mint="MINT1", mono=800 * MS)
    _j7(c, event_id="alice-1", uid="u1", mint="MINT1", mono=900 * MS)
    _axiom(c, address=ADDR, mint="MINT1", mono=1000 * MS)          # alice's address
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 1 and out["only_j7"] == 1
    assert out["median_diff_ms"] == 100.0                          # paired with alice, not bob


# ----------------------------------------------- Axiom v4 rows carry no amount (Beta, 2026-09-16)

def test_a_pair_matches_without_an_amount_and_says_so():
    """tracked-wallet-transactions-v4 sets amount_usd None by design. Requiring an amount would
    match NOTHING; matching without one is weaker evidence and is labelled, never hidden."""
    c = _conn()
    _j7(c, amount=100.0, mono=900 * MS)
    _axiom(c, amount=None, mono=1000 * MS)
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 1 and out["weak_no_amount"] == 1 and out["weak"] == 0
    assert out["median_diff_ms"] == 100.0


def test_an_amountless_pair_still_needs_the_trader_the_coin_the_side_and_the_window():
    c = _conn()
    _j7(c, mint="MINT1", side="buy", mono=900 * MS)
    _axiom(c, mint="MINT1", side="sell", amount=None, mono=1000 * MS)     # wrong side
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 0 and out["only_axiom"] == 1 and out["only_j7"] == 1


def test_an_amount_on_both_sides_still_has_to_agree():
    c = _conn()
    _j7(c, amount=100.0)
    _axiom(c, amount=200.0)
    assert fc.compare_feeds(c)["chains"]["solana"]["matched"] == 0


def test_explicit_live_rows_are_not_reclassified_by_batch_size():
    """Explicit provenance controls inclusion, regardless of the batch size."""
    c = _conn()
    batch_at = "2026-09-16T12:00:00+00:00"
    for i in range(6):
        _j7(c, event_id=f"e{i}", mint=f"MINT{i}", mono=(900 + i) * MS,
            at="2026-09-16T11:59:5%d+00:00" % i)
        _axiom(c, mint=f"MINT{i}", mono=1000 * MS + i * 20_000, at=batch_at)
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["matched"] == 6
    assert out["timed"] == 6
    assert out["latency_by_match"]["weak"]["timed"] == 6


def test_a_small_group_sharing_a_stamp_is_not_a_batch():
    c = _conn()
    _j7(c, event_id="e1", mint="MINT1", mono=900 * MS)
    _axiom(c, mint="MINT1", mono=1000 * MS)
    out = fc.compare_feeds(c)["chains"]["solana"]
    assert out["timed"] == 1


@pytest.mark.parametrize('change', [{'mint': 'OTHER'}, {'side': 'sell'}, {'uid': 'other'}])
def test_same_signature_requires_full_identity(change):
    c = _conn()
    _j7(c, sig='SAME', **change)
    _axiom(c, tx_sig='SAME')
    assert fc.compare_feeds(c)['chains']['solana']['matched'] == 0


def test_distinct_signatures_never_match_by_amount_or_count_as_duplicates():
    c = _conn()
    _j7(c, sig='S1')
    _axiom(c, tx_sig='S1')
    _axiom(c, tx_sig='S2')
    out = fc.compare_feeds(c)['chains']['solana']
    assert (out['exact'], out['weak'], out['duplicates'], out['only_axiom']) == (1, 0, 0, 1)


def test_a_signature_pair_is_used_once_and_latency_groups_stay_separate():
    c = _conn()
    _j7(c, sig='S1')
    _axiom(c, tx_sig='S1')
    _axiom(c, mint='OTHER', amount=None)
    _j7(c, event_id='e2', mint='OTHER')
    out = fc.compare_feeds(c)['chains']['solana']
    assert out['exact'] == 1 and out['weak_no_amount'] == 1
    assert out['latency_by_match']['exact']['timed'] == 1
    assert out['latency_by_match']['weak_no_amount']['timed'] == 1
    assert out['median_diff_ms'] is None


@pytest.mark.parametrize('kind,transport', [('history', 'rest'), ('unknown', 'ws'), ('live', 'rest')])
def test_history_and_unknown_provenance_are_excluded(kind, transport):
    c = _conn()
    _j7(c)
    _axiom(c, kind=kind, transport=transport)
    out = fc.compare_feeds(c)
    assert out['excluded_axiom_rows'] == 1
    assert out['chains']['solana']['matched'] == 0
    assert out['chains']['solana']['only_axiom'] == 0


@pytest.mark.parametrize('domain', [None, 'other-host:same-boot'])
def test_missing_or_distinct_clock_domain_cannot_time(domain):
    c = _conn()
    _j7(c)
    _axiom(c, domain=domain)
    out = fc.compare_feeds(c)['chains']['solana']
    assert out['matched'] == 1 and out['timed'] == 0


def test_receipt_window_is_shared_even_when_provider_timestamp_is_old():
    c = _conn()
    _j7(c, created_at='2020-01-01T00:00:00+00:00')
    _axiom(c)
    out = fc.compare_feeds(c, since=NOW, until='2026-09-16T12:01:00+00:00')
    assert out['chains']['solana']['matched'] == 1


def test_owner_lookup_preserves_chain_and_solana_case():
    c = _conn()
    c.execute("INSERT INTO axiom_verified_addresses(source_user_id, network_id, address, verify_status, verified_at, detail) VALUES('u2','4663',?,'axiom_confirmed',?,?)", (ADDR, NOW, PROOF))
    _j7(c)
    _axiom(c)
    _axiom(c, address=ADDR.lower(), mint='OTHER')
    out = fc.compare_feeds(c)
    assert out['chains']['solana']['matched'] == 1
    assert out['unattributed_axiom_rows'] == 1


def test_unmapped_j7_trader_is_outside_the_observable_cohort():
    c = _conn()
    _j7(c, uid='unknown')
    out = fc.compare_feeds(c)
    assert out['unattributed_j7_rows'] == 1
    assert 'solana' not in out['chains']


def test_raw_receipt_identity_survives_absent_downstream_event():
    c = _conn()
    _j7(c, sig='S1')
    c.execute('DELETE FROM memecoin_events')
    _axiom(c, tx_sig='S1')
    assert fc.compare_feeds(c)['chains']['solana']['exact'] == 1


@pytest.mark.parametrize('kwargs', [dict(window_s=-1), dict(window_s=float('nan')),
                                   dict(since=NOW, until=NOW)])
def test_invalid_intervals_are_rejected(kwargs):
    with pytest.raises(ValueError):
        fc.compare_feeds(_conn(), **kwargs)


def test_cli_report_opens_a_readonly_database(tmp_path, capsys):
    from scripts.research.compare_axiom_j7 import main
    c = _conn()
    _j7(c, sig='same')
    _axiom(c, tx_sig='same')
    path = tmp_path / 'fixture.db'
    with sqlite3.connect(path) as target:
        c.backup(target)
    before = path.read_bytes()
    main(['--db', str(path), '--since', NOW, '--until', '2026-09-16T12:01:00+00:00'])
    report = json.loads(capsys.readouterr().out)
    assert report['chains']['solana']['exact'] == 1
    assert report['coverage_verdict'].startswith('unproven')
    assert path.read_bytes() == before


def test_exact_matching_is_one_to_one_and_deduplicates_full_identity_only():
    c = _conn()
    _j7(c, event_id='first', sig='S1')
    _j7(c, event_id='replay', sig='S1')
    _axiom(c, tx_sig='S1')
    _axiom(c, tx_sig='S1', side='sell')
    out = fc.compare_feeds(c)['chains']['solana']
    assert out['exact'] == 1 and out['j7_duplicates'] == 1
    assert out['only_axiom'] == 1 and out['only_j7'] == 0


def test_evm_identity_is_case_insensitive_but_solana_mints_are_not():
    c = _conn()
    c.execute("INSERT INTO axiom_verified_addresses(source_user_id, network_id, address, verify_status, verified_at, detail) VALUES('u2','4663','0xAbC','axiom_confirmed',?,?)", (NOW, PROOF))
    _j7(c, uid='u2', chain='4663', mint='0xDEf', sig='0xABCD')
    _axiom(c, address='0xabc', chain='4663', mint='0xdef', tx_sig='0xabcd')
    _j7(c, event_id='sol', mint='MiNt')
    _axiom(c, mint='mint')
    out = fc.compare_feeds(c)['chains']
    assert out['4663']['exact'] == 1
    assert out['solana']['matched'] == 0


def test_exact_match_still_requires_the_receipt_window():
    c = _conn()
    _j7(c, sig='S1')
    _axiom(c, tx_sig='S1', at='2026-09-16T13:00:00+00:00', mono=3_600_900 * MS)
    assert fc.compare_feeds(c)['chains']['solana']['matched'] == 0


def test_same_clock_domain_uses_monotonic_proximity_despite_wall_clock_change():
    c = _conn()
    _j7(c, sig='S1', mono=1000 * MS)
    _axiom(c, tx_sig='S1', mono=1001 * MS, at='2026-09-16T13:00:00+00:00')
    out = fc.compare_feeds(c)['chains']['solana']
    assert out['exact'] == 1 and out['latency_by_match']['exact']['median_diff_ms'] == 1.0
