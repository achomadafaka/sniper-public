"""Cohort identity and cumulative cash-net guards on real temporary ledgers."""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_intents as intents
from sniper.services import memecoin_origin as origin
from sniper.settings import memecoin as cfg

NOW = "2026-10-21T12:00:00+00:00"
POLICY = {"clip_usd": 25, "max_open": 5, "daily_halt_usd": 200, "gates": "fixture-v1"}
RAILS = {"tp_pct": 60.0, "sl_pct": 25.0, "trail_pct": 15.0}


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHAINS", frozenset({"solana"}))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_DAILY_LOSS_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", {
        "tp_pct": (15.0, 150.0), "sl_pct": (10.0, 40.0), "trail_pct": (8.0, 30.0)})


def ledger(path=":memory:"):
    conn = sqlite3.connect(str(path), timeout=2)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def register(conn, producer="H", **kw):
    return m.register_desk_cohort(conn, producer=producer, policy=kw.pop("policy_override", POLICY),
                                  prompt_version="desk-judge-v6", created_at=NOW, **kw)


def metadata(cohort, producer="H", opportunity="desk:H:example"):
    return {"cohort_id": cohort, "producer": producer, "opportunity_id": opportunity,
            "snapshot_hash": "a" * 64}


def contract(cohort, producer="H"):
    # This unit exercises the tolerant source block; pipeline tests freeze the entire policy.
    return json.dumps({"source": {"desk": {**metadata(cohort, producer),
                                          "ticket_id": "ticket", "rails": RAILS}}})


def position(conn, cohort, mint="coin", producer="H", source="desk", ceiling=None):
    """`ceiling` (lamports): the entry was signed under the option B clamp: its confirmed, signed pending row
    (same attempt_id) carries that recorded fee footprint, so its unpriced entry fee is a bounded leg."""
    attempt = None
    if ceiling is not None:
        attempt = "attempt-" + mint
        pid = m.reserve_pending(conn, mint=mint, source_user_id=None, strategy="control", notional=25,
                                reserved_at=NOW, network_id="solana", signal_source="desk", attempt_id=attempt,
                                funded_contract_json=contract(cohort, producer))
        conn.execute("UPDATE memecoin_pending_entries SET tx_sig=?, status='confirmed', entry_fee_ceiling_lamports=? "
                     "WHERE id=?", ("sig-" + mint, ceiling, pid))
        conn.commit()
    return m.open_position(
        conn, source_user_id=None, source_handle="fixture", mint=mint, ticker=None,
        entry_price=1.0, entry_notional=25.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, opened_at=NOW, execution_mode="real", network_id="solana",
        signal_source=source, funded_contract_json=contract(cohort, producer), attempt_id=attempt)


def exact_entry_cost(conn, cohort, pid, usd=0):
    return m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id=f"entry:{pid}",
                                    usd=usd, kind="execution", occurred_at=NOW,
                                    provenance="fixture:exact", position_id=pid)


def reserve(conn, cohort, *, mint="next", producer="H", commit=True):
    return m.reserve_pending(conn, mint=mint, source_user_id=None, strategy="control",
                             notional=25, reserved_at=NOW, network_id="solana",
                             signal_source="desk", funded_contract_json=contract(cohort, producer),
                             commit=commit)


def test_cohort_registration_is_frozen_and_cannot_reset_by_alias():
    conn = ledger()
    cohort = register(conn)
    assert register(conn) == cohort
    with pytest.raises(ValueError, match="different|conflict|policy"):
        register(conn, cohort_id=cohort, policy_override={"gates": "changed"})
    with pytest.raises(ValueError, match="alias|registered|policy"):
        register(conn, cohort_id="a-new-name-for-the-same-policy")
    assert conn.execute("SELECT COUNT(*) FROM memecoin_desk_cohorts").fetchone()[0] == 1


def test_cohort_identity_survives_booking_and_unknown_cost_is_not_zero():
    conn = ledger()
    cohort = register(conn)
    pid = position(conn, cohort, ceiling=6_520_000)          # signed under the clamp: a bounded unknown leg
    row = m.get_position(conn, pid)
    assert (row["desk_cohort_id"], row["desk_producer"], row["desk_snapshot_hash"]) == (
        cohort, "H", "a" * 64)
    state = m.desk_cohort_accounting(conn, cohort)
    assert state["unknown_costs"] == 1
    assert state["net_usd"] is None
    # cost gate (operator 2026-10-22): one bounded unknown leg far from the kill admits; never valued at 0
    gate = m.desk_cohort_gate(conn, cohort_id=cohort, producer="H")
    assert gate["refusal"] is None and gate["bounded_net_usd"] < 0.0
    exact_entry_cost(conn, cohort, pid, 0)
    assert m.desk_cohort_accounting(conn, cohort)["net_usd"] == 0.0
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") is None


def test_expenses_include_abstentions_are_idempotent_and_unknown_resolution_trips():
    conn = ledger()
    cohort = register(conn)
    pid = position(conn, cohort)
    exact_entry_cost(conn, cohort, pid)
    m.close_position(conn, pid, NOW, -195.0, "stop")
    assert not m.desk_cohort_killed(conn, cohort)
    m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="skipped-model-call", usd=None,
                              kind="model", occurred_at=NOW, provenance="fixture:pending")
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") == "cohort_accounting_unknown"
    for _ in range(2):
        m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="skipped-model-call", usd=10.0,
                                  kind="model", occurred_at=NOW, provenance="fixture:bill")
    assert m.desk_cohort_killed(conn, cohort)
    assert m.desk_cohort_accounting(conn, cohort)["known_cost_usd"] == 10.0
    with pytest.raises(ValueError, match="different|conflict|immutable"):
        m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="skipped-model-call", usd=0,
                                  kind="model", occurred_at=NOW, provenance="fixture:bill")


def test_kill_survives_midnight_later_winner_and_restart(tmp_path):
    path = tmp_path / "ledger.db"
    conn = ledger(path)
    cohort = register(conn)
    loser = position(conn, cohort, "loss")
    exact_entry_cost(conn, cohort, loser)
    m.close_position(conn, loser, "2026-10-21T23:59:59+00:00", -110, "stop")
    second = position(conn, cohort, "second")
    exact_entry_cost(conn, cohort, second)
    m.close_position(conn, second, "2026-10-22T00:00:01+00:00", -100, "stop")
    assert m.desk_cohort_killed(conn, cohort)
    winner = position(conn, cohort, "already-in-flight")
    exact_entry_cost(conn, cohort, winner)
    m.close_position(conn, winner, "2026-10-22T00:00:02+00:00", 500, "tp")
    conn.close()
    again = ledger(path)
    assert m.desk_cohort_refusal(again, cohort_id=cohort, producer="H") == "desk_cohort_killed"
    assert origin.desk_book_refusal(again, now_iso="2026-10-23T12:00:00+00:00",
                                   desk_metadata=metadata(cohort)) == "desk_cohort_killed"


def test_h_and_u_cohorts_do_not_share_losses_or_identity():
    conn = ledger()
    h, u = register(conn), register(conn, "U")
    pid = position(conn, h)
    exact_entry_cost(conn, h, pid)
    m.close_position(conn, pid, NOW, -200, "stop")
    assert m.desk_cohort_refusal(conn, cohort_id=h, producer="H") == "desk_cohort_killed"
    assert m.desk_cohort_refusal(conn, cohort_id=u, producer="U") is None
    assert m.desk_cohort_refusal(conn, cohort_id=u, producer="H") == "desk_cohort_mismatch"


def test_atomic_reservation_refuses_a_cohort_killed_after_prework(tmp_path):
    path = tmp_path / "race.db"
    a, b = ledger(path), ledger(path)
    cohort = register(a)
    assert m.desk_cohort_refusal(a, cohort_id=cohort, producer="H") is None
    m.record_desk_cohort_cost(b, cohort_id=cohort, event_id="bill", usd=200,
                              kind="model", occurred_at=NOW, provenance="fixture:bill")
    assert reserve(a, cohort) is None
    assert m.lost_reservation_reason(a, network_id="solana", mint="next", signal_source="desk",
                                     at_iso=NOW, funded_contract_json=contract(cohort)).startswith(
        "desk cohort killed")
    assert m.reserve_pending(a, mint="copy", source_user_id="w", strategy="control", notional=25,
                             reserved_at=NOW, network_id="solana", signal_source="slot_lane")


def test_missing_cohort_never_authorizes_new_unsigned_desk_reservation():
    conn = ledger()
    assert m.reserve_pending(conn, mint="new", source_user_id=None, strategy="control", notional=25,
                             reserved_at=NOW, network_id="solana", signal_source="desk") is None


def test_ticket_replay_cannot_change_its_frozen_cohort():
    conn = ledger()
    h, u = register(conn), register(conn, "U")
    args = dict(mint="mint", network_id="solana", now_iso=NOW, size_usd=25,
                rails=RAILS, rails_mode="custom", signal_source="desk", desk_ticket_id="one")
    first = intents.enqueue(conn, **args, desk_metadata=metadata(h))
    assert intents.enqueue(conn, **args, desk_metadata=metadata(h)) == first
    with pytest.raises(ValueError, match="different"):
        intents.enqueue(conn, **args, desk_metadata=metadata(u, "U"))
    assert conn.execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 1


def test_unknown_closed_pnl_blocks_admission_but_priced_recovery_still_books():
    conn = ledger()
    cohort = register(conn)
    pid = position(conn, cohort)
    exact_entry_cost(conn, cohort, pid)
    m.close_position(conn, pid, NOW, None, "confirmed_unpriced")
    assert m.desk_cohort_refusal(conn, cohort_id=cohort, producer="H") == "cohort_accounting_unknown"
    # Finalization/reconciliation remains legal even after an accounting hold or kill.
    m.finalize_close(conn, pid, NOW, -201, "priced_recovery")
    assert m.desk_cohort_killed(conn, cohort)


def test_a_later_refund_never_clears_the_kill():
    conn = ledger()
    cohort = register(conn)
    m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="bill", usd=201,
                              kind="model", occurred_at=NOW, provenance="fixture:bill")
    m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="refund", usd=-500,
                              kind="refund", occurred_at=NOW, provenance="fixture:refund")
    assert m.desk_cohort_accounting(conn, cohort)["net_usd"] == 299.0
    assert m.desk_cohort_killed(conn, cohort)


def test_cohort_latch_and_cost_can_join_a_callers_transaction():
    conn = ledger()
    cohort = register(conn)
    conn.execute("BEGIN IMMEDIATE")
    m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="bill", usd=200,
                              kind="model", occurred_at=NOW, provenance="fixture:bill", commit=False)
    assert conn.in_transaction and m.desk_cohort_killed(conn, cohort)
    conn.rollback()
    assert not m.desk_cohort_killed(conn, cohort)
    assert m.desk_cohort_accounting(conn, cohort)["known_cost_usd"] == 0


@pytest.mark.parametrize("usd", [True, float("nan"), float("inf"), "10"])
def test_invalid_cost_cannot_be_converted_or_silently_counted_as_zero(usd):
    conn = ledger()
    cohort = register(conn)
    with pytest.raises(ValueError):
        m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="bad", usd=usd,
                                  kind="model", occurred_at=NOW, provenance="fixture:bad")


@pytest.mark.parametrize('writer', ['full_fill', 'receipt', 'unpriced_partial', 'empty_claim',
                                    'writeoff', 'orphan', 'ordinary'])
def test_every_settled_loss_writer_latches_the_same_cohort(writer):
    c = ledger()
    cohort = register(c)
    pid = position(c, cohort)
    exact_entry_cost(c, cohort, pid)
    c.execute('UPDATE memecoin_positions SET entry_notional=400 WHERE id=?', (pid,))
    if writer == 'full_fill':
        c.execute("UPDATE memecoin_positions SET close_claim_id='claim',close_sig='sig' WHERE id=?", (pid,))
        c.commit()
        assert m.book_full_exit_and_finalize(c, position_id=pid, claim_id='claim', sig='sig',
            closed_at=NOW, realized_pnl=-350, exit_reason='stop', fill_kind='bracket_exit',
            price=.05, qty=1000, notional=50)
    elif writer == 'receipt':
        c.execute("UPDATE memecoin_positions SET is_open=0,closed_at=?,close_sig='sig',realized_pnl=NULL WHERE id=?", (NOW, pid))
        c.execute("INSERT INTO memecoin_fills(position_id,kind,qty,fill_source,created_at,tx_sig) "
                  "VALUES(?,'bracket_exit',1000,'chain_confirmed_unpriced',?,'sig')", (pid, NOW))
        c.commit()
        assert m.reconcile_unpriced_full_exit(c, position_id=pid, sig='sig', price=.05, qty=1000,
                                              notional=50, fill_source='chain')
    elif writer == 'unpriced_partial':
        c.execute('UPDATE memecoin_positions SET is_open=0,closed_at=?,realized_pnl=NULL WHERE id=?', (NOW, pid))
        f = c.execute("INSERT INTO memecoin_fills(position_id,kind,qty,fill_source,created_at,tx_sig) "
                      "VALUES(?,'derisk',500,'chain_confirmed_unpriced',?,'partial')", (pid, NOW)).lastrowid
        c.execute("INSERT INTO memecoin_fills(position_id,kind,qty,notional,fill_source,created_at,tx_sig) "
                  "VALUES(?,'bracket_exit',500,40,'chain',?,'sig')", (pid, NOW))
        c.commit()
        assert m.reconcile_unpriced_derisk(c, fill_id=f, position_id=pid, sig='partial', notional=10, fill_source='chain')
    elif writer == 'empty_claim':
        c.execute("UPDATE memecoin_positions SET close_claim_id='claim' WHERE id=?", (pid,)); c.commit()
        assert m.finalize_claimed_close_without_fill(c, position_id=pid, claim_id='claim',
            closed_at=NOW, realized_pnl=-400, exit_reason='empty')
    elif writer == 'writeoff':
        c.commit()
        assert m.write_off_real_position(c, pid, actor='fixture', reason='fixture:lost',
                                         balance_text='0', at=NOW)['status'] == 'written_off'
    elif writer == 'orphan':
        c.execute('UPDATE memecoin_positions SET is_open=0 WHERE id=?', (pid,)); c.commit()
        m.finalize_close(c, pid, NOW, -350, 'recovered')
    else:
        c.commit()
        m.close_position(c, pid, NOW, -350, 'stop')
    assert m.desk_cohort_killed(c, cohort)
    # Later correct/winning reconciliation cannot clear a once-observed breach.
    m.finalize_close(c, pid, NOW, 500, 'later_recovery')
    assert m.desk_cohort_killed(c, cohort)


def test_paid_partial_and_full_exit_create_unknown_costs_without_provider_calls():
    c = ledger()
    cohort = register(c)
    pid = position(c, cohort)
    exact_entry_cost(c, cohort, pid)
    m.book_derisk_fill(c, position_id=pid, at=NOW, price=2, qty=5, notional=10,
                       fraction=.2, tx_sig='partial')
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    m.record_desk_cohort_cost(c, cohort_id=cohort, event_id=f'partial:{pid}:partial', usd=1,
        kind='execution', occurred_at=NOW, provenance='fixture:total', position_id=pid)
    c.execute("UPDATE memecoin_positions SET close_claim_id='claim',close_sig='sig' WHERE id=?", (pid,)); c.commit()
    assert m.book_full_exit_and_finalize(c, position_id=pid, claim_id='claim', sig='sig',
        closed_at=NOW, realized_pnl=5, exit_reason='stop', fill_kind='bracket_exit',
        price=1, qty=20, notional=20)
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    gate = m.desk_cohort_gate(c, cohort_id=cohort, producer='H')
    # an unpriced EXIT has no enforced fee ceiling at signing: unbounded, it holds the cohort (Alpha 2026-10-22)
    assert gate['unknown_by_kind'] == {'exit_unbounded': 1} and gate['refusal'] == 'cohort_accounting_unknown'


def test_signed_legacy_desk_booking_without_new_metadata_still_reconciles():
    c = ledger()
    pid = m.open_position(c, source_user_id=None, source_handle='legacy', mint='legacy', ticker=None,
        entry_price=1, entry_notional=25, entry_fill_source='chain', entry_lag_s=0,
        their_size_usd=0, opened_at=NOW, signal_source='desk', execution_mode='real',
        network_id='solana', funded_contract_json=json.dumps({'source': {'desk': {'ticket_id':'old'}}}))
    m.close_position(c, pid, NOW, -25, 'legacy_recovery')
    assert m.get_position(c, pid)['desk_cohort_id'] is None
    assert m.get_position(c, pid)['realized_pnl'] == -25


def test_reverting_a_quote_residue_credit_latches_the_cumulative_loss():
    c = ledger()
    cohort = register(c)
    pid = position(c, cohort)
    exact_entry_cost(c, cohort, pid)
    m.close_position(c, pid, NOW, -190, 'stop')
    c.execute("INSERT INTO memecoin_quote_residue_credits(residue_id,position_id,intent_id,quote_mint,"
              "residue_atomic,consumed_atomic,foreign_atomic,usd,credited_at) VALUES(1,?,'i','q','20','20','0',20,?)", (pid, NOW))
    c.commit()
    assert m.revert_quote_residue_credit(c, 1, now_iso=NOW, reason='wrong_receipt')
    assert m.desk_cohort_killed(c, cohort)


def test_confirmed_unpriced_partial_also_records_unknown_execution_cost():
    c = ledger()
    cohort = register(c)
    pid = position(c, cohort)
    exact_entry_cost(c, cohort, pid)
    c.execute("UPDATE memecoin_positions SET derisk_claim_id='claim',derisk_sig='partial',derisked_at=? WHERE id=?", (NOW, pid))
    c.commit()
    assert m.settle_confirmed_derisk_unpriced(c, position_id=pid, claim_id='claim', sig='partial',
                                              qty=5, fraction=.2, at=NOW)
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1


def test_backfilled_paid_partial_keeps_cost_unknown_after_proceeds_are_priced():
    c = ledger()
    cohort = register(c)
    pid = position(c, cohort)
    exact_entry_cost(c, cohort, pid)
    fid = m.backfill_unbooked_derisk(c, position_id=pid, sig='backfill', qty=5, fraction=.2, at=NOW)
    assert fid
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    assert m.reconcile_unpriced_derisk(c, fill_id=fid, position_id=pid, sig='backfill', notional=10, fill_source='chain')
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1


def test_signed_pending_expense_is_unknown_even_when_no_position_was_booked(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_COST_GATE_LEGACY", True)   # pins the old any-unknown reservation rule
    c = ledger()
    cohort = register(c)
    pending = reserve(c, cohort)
    assert pending
    m.set_pending_sig(c, pending, 'paid-failure', NOW)
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    assert reserve(c, cohort, mint='other') is None
    assert m.reset_entry_submission_after_failed_receipt(c, pending, 'paid-failure', release_reason='mined failure')
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    m.record_desk_cohort_cost(c, cohort_id=cohort, event_id=f'pending:{pending}:paid-failure', usd=.1,
        kind='execution', occurred_at=c.execute(
            'SELECT occurred_at FROM memecoin_desk_cohort_costs WHERE event_id=?',
            (f'pending:{pending}:paid-failure',)).fetchone()[0], provenance='fixture:receipt')
    assert m.desk_cohort_accounting(c, cohort)['known_cost_usd'] == .1
    assert m.desk_cohort_refusal(c, cohort_id=cohort, producer='H') is None


def test_each_failed_retry_signature_keeps_its_own_unknown_charge():
    c = ledger()
    cohort = register(c)
    pending = reserve(c, cohort)
    for sig in ['one', 'two']:
        m.set_pending_sig(c, pending, sig, NOW)
        assert m.reset_entry_submission_after_failed_receipt(c, pending, sig)
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 2
    for sig in ['one', 'two']:
        m.record_desk_cohort_cost(c, cohort_id=cohort, event_id=f'pending:{pending}:{sig}', usd=100,
            kind='execution', occurred_at=c.execute(
                'SELECT occurred_at FROM memecoin_desk_cohort_costs WHERE event_id=?',
                (f'pending:{pending}:{sig}',)).fetchone()[0], provenance='fixture:receipt')
    assert m.desk_cohort_killed(c, cohort)


@pytest.mark.parametrize('kind', ['exit', 'partial'])
def test_unknown_and_failed_exit_signatures_keep_their_charge_after_reset(kind, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_COST_GATE_LEGACY", True)   # pins the old any-unknown reservation rule
    c = ledger()
    cohort = register(c)
    pid = position(c, cohort)
    exact_entry_cost(c, cohort, pid)
    if kind == 'exit':
        c.execute("UPDATE memecoin_positions SET close_claim_id='claim',close_sig='paid' WHERE id=?", (pid,))
    else:
        c.execute("UPDATE memecoin_positions SET derisk_claim_id='claim',derisk_sig='paid',derisked_at=? WHERE id=?", (NOW, pid))
    c.commit()
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    assert reserve(c, cohort, mint='other') is None
    if kind == 'exit':
        assert m.reset_position_close_submission(c, pid, 'claim', 'paid', NOW)
    else:
        assert m.reset_position_derisk_submission(c, pid, 'claim', 'paid', NOW)
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    m.record_desk_cohort_cost(c, cohort_id=cohort, event_id=f'{kind}:{pid}:paid', usd=200,
        kind='execution', occurred_at=NOW, provenance='fixture:receipt', position_id=pid)
    assert m.desk_cohort_killed(c, cohort)


def test_confirmed_pending_uses_its_booked_entry_expense_once():
    c = ledger()
    cohort = register(c)
    pending = reserve(c, cohort)
    m.set_pending_sig(c, pending, 'landed', NOW)
    pid = position(c, cohort)
    c.execute("UPDATE memecoin_pending_entries SET status='confirmed' WHERE id=?", (pending,)); c.commit()
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    exact_entry_cost(c, cohort, pid, 1)
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 0
    assert m.desk_cohort_accounting(c, cohort)['known_cost_usd'] == 1


@pytest.mark.parametrize('kind', ['exit', 'partial'])
def test_paid_failure_recovery_preserves_cost_when_it_clears_the_signature(tmp_path, kind, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_COST_GATE_LEGACY", True)   # pins the old any-unknown reservation rule
    c = ledger(tmp_path / 'recovery.db')
    cohort = register(c)
    pid = position(c, cohort)
    exact_entry_cost(c, cohort, pid)
    column = 'close' if kind == 'exit' else 'derisk'
    c.execute(f"UPDATE memecoin_positions SET {column}_claim_id='claim',{column}_sig='paid',"
              "derisked_at=? WHERE id=?", (NOW, pid))
    c.commit()
    clear = (lambda: m.release_dropped_close_claim(c, pid, 'claim', 'paid')) if kind == 'exit' else (
        lambda: m.clear_position_derisk(c, pid, claim_id='claim', sig='paid', exact_claim=True))
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    assert clear()
    assert not clear()
    assert m.get_position(c, pid)[f'{column}_sig'] is None
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1
    assert reserve(c, cohort, mint='other') is None
    event = f'{kind}:{pid}:paid'
    at = c.execute('SELECT occurred_at FROM memecoin_desk_cohort_costs WHERE event_id=?', (event,)).fetchone()[0]
    for _ in range(2):
        m.record_desk_cohort_cost(c, cohort_id=cohort, event_id=event, usd=200, kind='execution',
                                  occurred_at=at, provenance='fixture:receipt', position_id=pid)
    assert m.desk_cohort_killed(c, cohort)
    assert m.desk_cohort_accounting(c, cohort)['known_cost_usd'] == 200


def test_signed_new_desk_partial_cannot_be_cleared_without_its_signature():
    c = ledger()
    cohort = register(c)
    pid = position(c, cohort)
    exact_entry_cost(c, cohort, pid)
    c.execute("UPDATE memecoin_positions SET derisk_claim_id='claim',derisk_sig='paid',"
              "derisked_at=? WHERE id=?", (NOW, pid)); c.commit()
    assert not m.clear_position_derisk(c, pid, claim_id='claim')
    assert m.get_position(c, pid)['derisk_sig'] == 'paid'
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 1


@pytest.mark.parametrize('source', ['slot_lane', 'legacy_desk', 'unsigned_desk'])
def test_partial_clear_preserves_unsigned_copy_and_legacy_semantics(source):
    c = ledger()
    cohort = register(c)
    pid = position(c, cohort, source='slot_lane' if source == 'slot_lane' else 'desk')
    if source == 'legacy_desk':
        c.execute('UPDATE memecoin_positions SET desk_cohort_id=NULL WHERE id=?', (pid,))
    c.execute("UPDATE memecoin_positions SET derisk_claim_id='claim',derisk_sig=?,"
              "derisked_at=? WHERE id=?", (None if source == 'unsigned_desk' else 'paid', NOW, pid))
    c.commit()
    assert m.clear_position_derisk(c, pid, claim_id='claim')
    assert m.get_position(c, pid)['derisk_sig'] is None
    assert c.execute("SELECT COUNT(*) FROM memecoin_desk_cohort_costs WHERE event_id LIKE 'partial:%'").fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['exit', 'partial'])
def test_recovery_cost_failure_rolls_back_signature_clear(kind):
    c = ledger()
    cohort = register(c)
    pid = position(c, cohort)
    column = 'close' if kind == 'exit' else 'derisk'
    c.execute(f"UPDATE memecoin_positions SET {column}_claim_id='claim',{column}_sig='paid',"
              "derisked_at=? WHERE id=?", (NOW, pid))
    c.execute("CREATE TEMP TRIGGER refuse_cost BEFORE INSERT ON memecoin_desk_cohort_costs "
              "BEGIN SELECT RAISE(ABORT, 'fixture cost failure'); END")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='fixture cost failure'):
        if kind == 'exit':
            m.release_dropped_close_claim(c, pid, 'claim', 'paid')
        else:
            m.clear_position_derisk(c, pid, claim_id='claim', sig='paid')
    assert not c.in_transaction
    assert m.get_position(c, pid)[f'{column}_sig'] == 'paid'


def test_pending_fee_cannot_be_priced_before_successful_booking(tmp_path):
    c = ledger(tmp_path / 'billing.db')
    cohort = register(c)
    pending = reserve(c, cohort)
    m.set_pending_sig(c, pending, 'landed', NOW)
    event = f'pending:{pending}:landed'
    kwargs = dict(cohort_id=cohort, event_id=event, usd=1, kind='execution',
                  occurred_at=NOW, provenance='fixture:receipt')
    with pytest.raises(ValueError, match='pending|entry'):
        m.record_desk_cohort_cost(c, **kwargs)
    assert c.execute('SELECT COUNT(*) FROM memecoin_desk_cohort_costs WHERE event_id=?', (event,)).fetchone()[0] == 0
    pid = position(c, cohort)
    c.execute("UPDATE memecoin_pending_entries SET status='confirmed' WHERE id=?", (pending,)); c.commit()
    with pytest.raises(ValueError, match='pending|entry'):
        m.record_desk_cohort_cost(c, **kwargs)
    exact_entry_cost(c, cohort, pid, 1)
    assert m.desk_cohort_accounting(c, cohort)['net_usd'] == -1
    assert m.desk_cohort_refusal(c, cohort_id=cohort, producer='H') is None


def test_failed_pending_signature_can_be_priced_and_replayed_without_a_reset_marker():
    c = ledger()
    cohort = register(c)
    pending = reserve(c, cohort)
    m.set_pending_sig(c, pending, 'failed', NOW)
    m.release_pending(c, pending, 'mined failure')
    for _ in range(2):
        m.record_desk_cohort_cost(c, cohort_id=cohort, event_id=f'pending:{pending}:failed',
            usd=1, kind='execution', occurred_at=NOW, provenance='fixture:receipt')
    assert m.desk_cohort_accounting(c, cohort)['unknown_costs'] == 0
    assert m.desk_cohort_accounting(c, cohort)['known_cost_usd'] == 1


def test_rejected_pending_bill_leaves_callers_transaction_unchanged():
    c = ledger()
    cohort = register(c)
    pending = reserve(c, cohort)
    m.set_pending_sig(c, pending, 'inflight', NOW)
    c.execute('BEGIN IMMEDIATE')
    m.record_desk_cohort_cost(c, cohort_id=cohort, event_id='unrelated', usd=2, kind='model',
                              occurred_at=NOW, provenance='fixture:unrelated', commit=False)
    event = f'pending:{pending}:inflight'
    with pytest.raises(ValueError, match='pending|entry'):
        m.record_desk_cohort_cost(c, cohort_id=cohort, event_id=event, usd=1, kind='execution',
                                  occurred_at=NOW, provenance='fixture:receipt', commit=False)
    assert c.in_transaction
    c.commit()
    assert [tuple(row) for row in c.execute('SELECT event_id,usd FROM memecoin_desk_cohort_costs')] == [
        ('unrelated', 2.0)]


@pytest.mark.parametrize("stored_limit,cost_usd,refusal,allowed", [
    (100.0, 100.0, "desk_cohort_killed", False),
    (300.0, 250.0, None, True),
])
def test_preflight_and_atomic_reservation_use_the_same_persisted_cohort_limit(
        stored_limit, cost_usd, refusal, allowed):
    c = ledger()
    cohort = register(c)
    assert c.execute("SELECT kill_limit_usd FROM memecoin_desk_cohorts WHERE cohort_id=?",
                     (cohort,)).fetchone()[0] == 200.0
    # Synthetic historical state exercises reader parity; registration has no variable loss limit.
    c.execute("UPDATE memecoin_desk_cohorts SET kill_limit_usd=? WHERE cohort_id=?", (stored_limit, cohort))
    c.execute("INSERT INTO memecoin_desk_cohort_costs "
              "(cohort_id,event_id,kind,usd,occurred_at,provenance) VALUES (?,'known-bill','model',?,?,?)",
              (cohort, cost_usd, NOW, "fixture:priced"))
    c.commit()
    assert not m.desk_cohort_killed(c, cohort)
    bound = m.desk_execution_cost_bound()["usd"]
    lim = m.desk_execution_cost_bound()["priority_lamports"]
    assert bool(c.execute("SELECT " + m._DESK_COHORT_SQL,
                          (1, cohort, "H", lim, lim, lim, bound, lim, lim, lim)).fetchone()[0]) is allowed
    assert m.desk_cohort_refusal(c, cohort_id=cohort, producer="H") == refusal
    assert (reserve(c, cohort) is not None) is allowed
    c.close()


def test_signed_desk_cost_gap_uses_its_partial_cohort_index():
    c = ledger()
    cohort, other = register(c), register(c, "U")
    ids = {}
    for mint, identity, source, signature in [
        ("signed-desk", cohort, "desk", "desk-sig"),
        ("unsigned-desk", cohort, "desk", None),
        ("signed-copy", cohort, "slot_lane", "copy-sig"),
        ("other-cohort", other, "desk", "other-sig"),
    ]:
        ids[mint] = c.execute(
            "INSERT INTO memecoin_pending_entries "
            "(mint,network_id,desk_cohort_id,signal_source,tx_sig,status) VALUES (?,'solana',?,?,?,'failed')",
            (mint, identity, source, signature)).lastrowid
    c.commit()
    predicate = m._DESK_PENDING_COST_GAP_SQL.format(cohort="?", priced="AND fee.usd IS NOT NULL")
    query = "SELECT p.id FROM memecoin_pending_entries p WHERE " + predicate
    assert [row[0] for row in c.execute(query, (cohort,))] == [ids["signed-desk"]]
    details = [row[3] for row in c.execute("EXPLAIN QUERY PLAN " + query, (cohort,))]
    assert any(detail == f"SEARCH p USING INDEX {index} (desk_cohort_id=?)"
               for detail in details
               for index in ("idx_mc_pending_desk_cohort", "idx_mc_pending_pilot_capacity")), details
    assert not any("SCAN " in detail for detail in details), details
    forced = "SELECT p.id FROM memecoin_pending_entries p INDEXED BY idx_mc_pending_desk_cohort WHERE " + predicate
    assert [row[0] for row in c.execute(forced, (cohort,))] == [ids["signed-desk"]]
    # A forced partial index cannot answer a query that also needs unsigned or copy rows.
    for wider_predicate in ("p.signal_source='desk'", "p.tx_sig IS NOT NULL"):
        with pytest.raises(sqlite3.OperationalError, match="no query solution"):
            c.execute("SELECT p.id FROM memecoin_pending_entries p INDEXED BY idx_mc_pending_desk_cohort "
                      "WHERE p.desk_cohort_id=? AND " + wider_predicate, (cohort,)).fetchall()
    c.close()
