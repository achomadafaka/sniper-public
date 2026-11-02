import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


NOW = "2026-08-30T20:00:00+00:00"
RELEASE = "a" * 40
MANIFEST = "b" * 64
CUSTODY = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _pending(conn, mint=TOKEN):
    return m.reserve_pending(
        conn,
        mint=mint,
        source_user_id="wallet",
        strategy="control",
        notional=10,
        reserved_at=NOW,
        network_id="4663",
    )


def _start(conn, pending_id):
    return m.start_lifi_lifecycle(
        conn,
        pending_id,
        chain_id="4663",
        custody_wallet=CUSTODY,
        token=TOKEN,
        release_sha=RELEASE,
        manifest_hash=MANIFEST,
        created_at=NOW,
    )


def _open_position(conn, *, lifi_dependent):
    return m.open_position(
        conn,
        source_user_id="wallet",
        source_handle="wallet",
        mint=TOKEN,
        ticker="TKN",
        entry_price=1.0,
        entry_notional=10.0,
        entry_fill_source="chain",
        entry_lag_s=1.0,
        their_size_usd=100.0,
        opened_at=NOW,
        execution_mode="real",
        network_id="4663",
        lifi_dependent=lifi_dependent,
    )


def test_schema_uses_text_amounts_and_immutable_position_flag():
    conn = _conn()
    columns = {
        row["name"]: row["type"]
        for row in conn.execute("PRAGMA table_info(memecoin_lifi_authority_events)")
    }

    for name in (
        "amount_atomic", "input_atomic", "output_min_atomic", "fee_atomic",
        "allowance_atomic", "settlement_in_atomic", "settlement_out_atomic",
    ):
        assert columns[name] == "TEXT"
    position_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(memecoin_positions)")
    }
    assert "lifi_dependent" in position_columns

    position_id = _open_position(conn, lifi_dependent=True)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE memecoin_positions SET lifi_dependent=0 WHERE id=?", (position_id,)
        )


def test_schema_keeps_old_activations_when_it_removes_the_unique_canary_rule():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE memecoin_lifi_activations (
            release_sha TEXT NOT NULL,
            manifest_hash TEXT NOT NULL,
            chain_id TEXT NOT NULL,
            custody_wallet TEXT NOT NULL,
            canary_lifecycle_id TEXT NOT NULL UNIQUE,
            allowance_proof_sha256 TEXT NOT NULL,
            quota_limit TEXT NOT NULL,
            quota_remaining TEXT NOT NULL,
            quota_reset TEXT NOT NULL,
            activated_at TEXT NOT NULL,
            PRIMARY KEY(release_sha, manifest_hash, chain_id, custody_wallet)
        )
    """)
    values = (
        RELEASE, MANIFEST, "4663", CUSTODY, "canary", "c" * 64,
        "12000", "11990", "7200", NOW,
    )
    conn.execute(
        "INSERT INTO memecoin_lifi_activations VALUES (?,?,?,?,?,?,?,?,?,?)", values,
    )
    conn.commit()

    m.ensure_memecoin_tables(conn)
    conn.execute(
        "INSERT INTO memecoin_lifi_activations VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("d" * 40, *values[1:]),
    )

    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_lifi_activations WHERE canary_lifecycle_id='canary'"
    ).fetchone()[0] == 2


def test_start_and_compare_and_set_append_are_atomic():
    conn = _conn()
    pending_id = _pending(conn)
    lifecycle_id = _start(conn, pending_id)

    assert lifecycle_id
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="reserved",
        new_state="approval_signed",
        event_type="approval_signed",
        created_at=NOW,
        amount_atomic=123,
        tx_hash="0xabc",
    )
    assert not m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="reserved",
        new_state="wrong",
        event_type="wrong",
        created_at=NOW,
    )
    latest = m.latest_lifi_lifecycle(conn, lifecycle_id)
    assert latest["state"] == "approval_signed"
    assert latest["sequence"] == 1
    assert latest["amount_atomic"] == "123"
    assert conn.execute(
        "SELECT typeof(amount_atomic) FROM memecoin_lifi_authority_events "
        "WHERE lifecycle_id=? AND sequence=1",
        (lifecycle_id,),
    ).fetchone()[0] == "text"


def test_atomic_amounts_reject_noncanonical_text():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))

    with pytest.raises(ValueError):
        m.append_lifi_event(
            conn,
            lifecycle_id,
            expected_state="reserved",
            new_state="approval_signed",
            event_type="approval_signed",
            created_at=NOW,
            amount_atomic="001",
        )


def test_one_pending_reservation_can_own_one_lifecycle():
    conn = _conn()
    pending_id = _pending(conn)

    assert _start(conn, pending_id)
    assert _start(conn, pending_id) is None


def test_lifecycle_rows_reject_update_and_delete():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE memecoin_lifi_authority_events SET state='forged' "
            "WHERE lifecycle_id=? AND sequence=0",
            (lifecycle_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM memecoin_lifi_authority_events WHERE lifecycle_id=?",
            (lifecycle_id,),
        )


def test_database_rejects_a_transition_without_the_expected_prior_state():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))
    row = dict(conn.execute(
        "SELECT * FROM memecoin_lifi_authority_events WHERE lifecycle_id=? AND sequence=0",
        (lifecycle_id,),
    ).fetchone())
    row.update({
        "id": None,
        "sequence": 1,
        "expected_prior_state": "wrong",
        "state": "approval_signed",
        "event_type": "approval_signed",
    })
    columns = [name for name in row if name != "id"]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO memecoin_lifi_authority_events({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            [row[name] for name in columns],
        )


def test_each_revoke_attempt_gets_a_new_sequence():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="reserved",
        new_state="revoke_submitted",
        event_type="revoke_submitted",
        created_at=NOW,
        tx_hash="0xaaa",
    )
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="revoke_submitted",
        new_state="revoke_submitted",
        event_type="revoke_submitted",
        created_at=NOW,
        tx_hash="0xbbb",
    )

    rows = conn.execute(
        "SELECT sequence,tx_hash FROM memecoin_lifi_authority_events "
        "WHERE lifecycle_id=? AND event_type='revoke_submitted' ORDER BY sequence",
        (lifecycle_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [(1, "0xaaa"), (2, "0xbbb")]


def test_slot_count_uses_a_distinct_union_for_linked_positions():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))
    assert m.lifi_slots_used(conn, "4663", CUSTODY) == 1

    position_id = _open_position(conn, lifi_dependent=True)
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="reserved",
        new_state="position_open",
        event_type="position_linked",
        created_at=NOW,
        position_id=position_id,
    )
    assert m.lifi_slots_used(conn, "4663", CUSTODY) == 1

    with pytest.raises(sqlite3.IntegrityError):
        m.append_lifi_event(
            conn,
            lifecycle_id,
            expected_state="position_open",
            new_state="position_open",
            event_type="position_linked",
            created_at=NOW,
            position_id=position_id,
        )

    conn.execute("UPDATE memecoin_positions SET is_open=0 WHERE id=?", (position_id,))
    conn.commit()
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="position_open",
        new_state="position_closed",
        event_type="position_closed",
        created_at=NOW,
    )
    assert m.lifi_slots_used(conn, "4663", CUSTODY) == 0


def test_invalid_nonterminal_state_keeps_its_slot():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="reserved",
        new_state="invalid",
        event_type="invalid",
        created_at=NOW,
    )

    rows = m.unresolved_lifi_lifecycles(conn, "4663", CUSTODY)
    assert [row["lifecycle_id"] for row in rows] == [lifecycle_id]
    assert m.lifi_slots_used(conn, "4663", CUSTODY) == 1


def test_canary_terminal_event_keeps_release_and_allowance_proof():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="reserved",
        new_state="exit_settled",
        event_type="exit_settled",
        created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="exit_settled", new_state="allowance_zero",
        event_type="allowance_zero", created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="allowance_zero", new_state="position_closed",
        event_type="position_closed", created_at=NOW,
    )
    assert m.complete_lifi_canary(
        conn, lifecycle_id=lifecycle_id, allowances=[(TOKEN, CUSTODY, "0")],
        quota_limit=12000, quota_remaining=11990, quota_reset=7200,
        created_at=NOW,
    )

    latest = m.latest_lifi_lifecycle(conn, lifecycle_id)
    assert latest["release_sha"] == RELEASE
    assert latest["manifest_hash"] == MANIFEST
    assert json.loads(latest["detail_json"])["allowances"] == [
        {"token": TOKEN, "spender": CUSTODY, "amount_atomic": "0"},
    ]
    assert m.lifi_slots_used(conn, "4663", CUSTODY) == 0


def test_activation_requires_matching_terminal_canary_and_exact_zero_allowances():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))
    pairs = [(TOKEN, CUSTODY), ("0x3333333333333333333333333333333333333333", CUSTODY)]

    assert m.activate_lifi_release(
        conn, release_sha=RELEASE, manifest_hash=MANIFEST, chain_id="4663",
        custody_wallet=CUSTODY, canary_lifecycle_id=lifecycle_id,
        expected_allowances=pairs, quota_limit=12000, quota_remaining=11990,
        quota_reset=7200, activated_at=NOW,
    ) is None

    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="reserved", new_state="exit_settled",
        event_type="exit_settled", created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="exit_settled", new_state="allowance_zero",
        event_type="allowance_zero", created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="allowance_zero", new_state="position_closed",
        event_type="position_closed", created_at=NOW,
    )
    assert m.complete_lifi_canary(
        conn, lifecycle_id=lifecycle_id,
        allowances=[(token, spender, "0") for token, spender in pairs],
        quota_limit=12000, quota_remaining=11990, quota_reset=7200,
        created_at=NOW,
    )
    activation = m.activate_lifi_release(
        conn, release_sha=RELEASE, manifest_hash=MANIFEST, chain_id="4663",
        custody_wallet=CUSTODY, canary_lifecycle_id=lifecycle_id,
        expected_allowances=pairs, quota_limit=12000, quota_remaining=11990,
        quota_reset=7200, activated_at=NOW,
    )

    assert activation["canary_lifecycle_id"] == lifecycle_id
    assert activation["quota_remaining"] == "11990"
    assert m.active_lifi_release(
        conn, release_sha=RELEASE, manifest_hash=MANIFEST, chain_id="4663",
        custody_wallet=CUSTODY,
    )["canary_lifecycle_id"] == lifecycle_id
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE memecoin_lifi_activations SET quota_remaining='12000' "
            "WHERE canary_lifecycle_id=?", (lifecycle_id,),
        )


def test_activation_accepts_a_completed_canary_from_an_older_release():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))
    pairs = [(TOKEN, CUSTODY)]
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="reserved", new_state="exit_settled",
        event_type="exit_settled", created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="exit_settled", new_state="allowance_zero",
        event_type="allowance_zero", created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="allowance_zero", new_state="position_closed",
        event_type="position_closed", created_at=NOW,
    )
    assert m.complete_lifi_canary(
        conn, lifecycle_id=lifecycle_id, allowances=[(TOKEN, CUSTODY, "0")],
        quota_limit=12000, quota_remaining=11990, quota_reset=7200,
        created_at=NOW,
    )
    assert m.activate_lifi_release(
        conn, release_sha=RELEASE, manifest_hash=MANIFEST, chain_id="4663",
        custody_wallet=CUSTODY, canary_lifecycle_id=lifecycle_id,
        expected_allowances=pairs, quota_limit=12000, quota_remaining=11990,
        quota_reset=7200, activated_at=NOW,
    )

    activation = m.activate_lifi_release(
        conn, release_sha="d" * 40, manifest_hash=MANIFEST, chain_id="4663",
        custody_wallet=CUSTODY, canary_lifecycle_id=lifecycle_id,
        expected_allowances=pairs, quota_limit=12000, quota_remaining=11990,
        quota_reset=7200, activated_at=NOW,
    )

    assert activation["release_sha"] == "d" * 40
    assert activation["canary_lifecycle_id"] == lifecycle_id


def test_complete_canary_requires_terminal_close_and_zero_allowances():
    conn = _conn()
    pending_id = _pending(conn)
    lifecycle_id = _start(conn, pending_id)
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="reserved", new_state="position_closed",
        event_type="position_closed", created_at=NOW,
    )
    pairs = [(TOKEN, "0x3333333333333333333333333333333333333333")]

    assert not m.complete_lifi_canary(
        conn, lifecycle_id=lifecycle_id, allowances=[(*pairs[0], "0")],
        quota_limit=12000, quota_remaining=11990, quota_reset=7000, created_at=NOW,
    )

    pending_id = _pending(conn, mint="0x4444444444444444444444444444444444444444")
    lifecycle_id = _start(conn, pending_id)
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="reserved", new_state="exit_settled",
        event_type="exit_settled", created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="exit_settled", new_state="allowance_zero",
        event_type="allowance_zero", created_at=NOW,
    )
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="allowance_zero", new_state="position_closed",
        event_type="position_closed", created_at=NOW,
    )
    assert not m.complete_lifi_canary(
        conn, lifecycle_id=lifecycle_id, allowances=[(*pairs[0], "1")],
        quota_limit=12000, quota_remaining=11990, quota_reset=7000, created_at=NOW,
    )
    assert m.complete_lifi_canary(
        conn, lifecycle_id=lifecycle_id, allowances=[(*pairs[0], "0")],
        quota_limit=12000, quota_remaining=11990, quota_reset=7000, created_at=NOW,
    )
    latest = m.latest_lifi_lifecycle(conn, lifecycle_id)
    assert latest["state"] == "canary_complete"
    assert json.loads(latest["detail_json"])["allowances"][0]["amount_atomic"] == "0"


def test_activation_rejects_incomplete_allowance_proof_and_bad_quota():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))
    assert m.append_lifi_event(
        conn, lifecycle_id, expected_state="reserved", new_state="canary_complete",
        event_type="canary_complete", created_at=NOW,
        detail={
            "allowances": [{"token": TOKEN, "spender": CUSTODY, "amount_atomic": "1"}],
            "chain_id": "4663", "custody_wallet": CUSTODY,
        },
    )

    assert m.activate_lifi_release(
        conn, release_sha=RELEASE, manifest_hash=MANIFEST, chain_id="4663",
        custody_wallet=CUSTODY, canary_lifecycle_id=lifecycle_id,
        expected_allowances=[(TOKEN, CUSTODY)], quota_limit=12000,
        quota_remaining=12001, quota_reset=7200, activated_at=NOW,
    ) is None


def test_finalize_entry_commits_position_fill_pending_and_link_once():
    conn = _conn()
    pending_id = _pending(conn)
    lifecycle_id = _start(conn, pending_id)
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="reserved",
        new_state="entry_settled",
        event_type="entry_settled",
        created_at=NOW,
    )
    position_kwargs = {
        "source_user_id": "wallet",
        "source_handle": "wallet",
        "mint": TOKEN,
        "ticker": "TKN",
        "entry_price": 1.0,
        "entry_notional": 10.0,
        "entry_fill_source": "chain",
        "entry_lag_s": 1.0,
        "their_size_usd": 100.0,
        "opened_at": NOW,
        "execution_mode": "real",
        "network_id": "4663",
    }
    fill_kwargs = {
        "kind": "entry",
        "price": 1.0,
        "qty": 10.0,
        "notional": 10.0,
        "fill_source": "chain",
        "fraction": 1.0,
        "created_at": NOW,
        "tx_sig": "0xfill",
    }

    position_id = m.finalize_lifi_entry(
        conn,
        lifecycle_id=lifecycle_id,
        pending_id=pending_id,
        position_kwargs=position_kwargs,
        fill_kwargs=fill_kwargs,
        created_at=NOW,
    )

    position = conn.execute(
        "SELECT is_open,lifi_dependent FROM memecoin_positions WHERE id=?", (position_id,)
    ).fetchone()
    assert tuple(position) == (1, 1)
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_fills WHERE position_id=?", (position_id,)
    ).fetchone()[0] == 1
    assert m.pending_row(conn, pending_id)["status"] == "confirmed"
    latest = m.latest_lifi_lifecycle(conn, lifecycle_id)
    assert latest["state"] == "position_open"
    assert latest["position_id"] == position_id


def test_finalize_entry_rolls_back_each_write_on_failure():
    conn = _conn()
    pending_id = _pending(conn)
    lifecycle_id = _start(conn, pending_id)
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="reserved",
        new_state="entry_settled",
        event_type="entry_settled",
        created_at=NOW,
    )

    with pytest.raises(TypeError):
        m.finalize_lifi_entry(
            conn,
            lifecycle_id=lifecycle_id,
            pending_id=pending_id,
            position_kwargs={"mint": TOKEN},
            fill_kwargs={},
            created_at=NOW,
        )

    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0
    assert m.pending_row(conn, pending_id)["status"] == "reserved"
    assert m.latest_lifi_lifecycle(conn, lifecycle_id)["state"] == "entry_settled"


def test_finalize_close_commits_fill_position_and_terminal_event_once():
    conn = _conn()
    lifecycle_id = _start(conn, _pending(conn))
    position_id = _open_position(conn, lifi_dependent=True)
    assert m.append_lifi_event(
        conn,
        lifecycle_id,
        expected_state="reserved",
        new_state="position_open",
        event_type="position_linked",
        created_at=NOW,
        position_id=position_id,
    )
    conn.execute(
        "UPDATE memecoin_positions SET close_claim_id='claim',close_sig='0xsig' WHERE id=?",
        (position_id,),
    )
    conn.commit()

    assert m.finalize_lifi_close(
        conn,
        lifecycle_id=lifecycle_id,
        expected_state="position_open",
        exit_kwargs={
            "position_id": position_id,
            "claim_id": "claim",
            "sig": "0xsig",
            "closed_at": NOW,
            "realized_pnl": 1.0,
            "exit_reason": "real_manual",
            "fill_kind": "exit",
            "price": 1.1,
            "qty": 10.0,
            "notional": 11.0,
        },
        created_at=NOW,
    )
    assert conn.execute(
        "SELECT is_open FROM memecoin_positions WHERE id=?", (position_id,)
    ).fetchone()[0] == 0
    assert m.latest_lifi_lifecycle(conn, lifecycle_id)["state"] == "position_closed"
    assert m.lifi_slots_used(conn, "4663", CUSTODY) == 0
