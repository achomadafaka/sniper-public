"""Blocker 4 — orphaned reservations had no recovery: an unknown-confirm or a crash mid-executor left
a row 'reserved' forever, permanently blocking the mint and eating an exposure slot; a tx that landed
after 'unknown' left spent funds + held tokens invisible. recover_stale_reservations resolves stale
reserved rows against chain (chain->ledger direction, previously never checked)."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _reserve(c, mint, *, reserved_at, sig=None, nonce=None):
    pid = m.reserve_pending(c, mint=mint, source_user_id="u", strategy="alpha_b", notional=10.0,
                            reserved_at=reserved_at, network_id="solana")
    if sig is not None:
        m.set_pending_broadcast(c, pid, sig, nonce, reserved_at)
    return pid


NOW = "2026-07-09T01:00:00Z"          # cutoff at NOW-600s = 2026-07-09T00:50:00Z
OLD = "2026-07-09T00:00:00Z"          # well before the cutoff -> stale
FRESH = "2026-07-09T00:59:00Z"        # within the window -> not stale


def _confirm_for(_net, sig):
    return {"SIG_OK": "confirmed", "SIG_BAD": "failed", "SIG_PENDING": "pending"}.get(sig, "unavailable")


def test_no_sig_stale_row_released():
    c = _c()
    pid = _reserve(c, "M1", reserved_at=OLD)          # never got a tx_sig (crashed before signing)
    out = R.recover_stale_reservations(c, confirm_for=_confirm_for, now_iso=NOW)
    assert (pid, "released_never_broadcast") in out
    assert m.pending_totals(c) == (0, 0.0)            # slot freed


def test_early_unsigned_row_stays_reserved_with_receipt_recovery_enabled():
    c = _c()
    pid = _reserve(c, "M_EARLY_UNSIGNED", reserved_at="2026-07-09T00:55:00Z")

    out = R.recover_stale_reservations(
        c, confirm_for=_confirm_for, now_iso=NOW, expiry_proof_s=90,
        confirmed_entry_for=lambda _conn, _row, _now: None,
    )

    assert out == []
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_runner_stop_releases_only_unsigned_reservations():
    c = _c()
    unsigned = _reserve(c, "M_STOP_UNSIGNED", reserved_at=FRESH)
    signed = _reserve(c, "M_STOP_SIGNED", reserved_at=FRESH, sig="SIG_STOP", nonce=11)
    broadcast_only = _reserve(c, "M_STOP_BROADCAST", reserved_at=FRESH)
    nonce_only = _reserve(c, "M_STOP_NONCE", reserved_at=FRESH)
    payload_bound = _reserve(c, "M_STOP_PAYLOAD", reserved_at=FRESH)
    c.execute("UPDATE memecoin_pending_entries SET broadcast_at=? WHERE id=?", (FRESH, broadcast_only))
    c.execute("UPDATE memecoin_pending_entries SET nonce=? WHERE id=?", (12, nonce_only))
    c.execute("UPDATE memecoin_pending_entries SET signed_payload_sha256=? WHERE id=?",
              ("payload", payload_bound))
    c.commit()

    released = m.release_unsigned_reservations_after_runner_stop(c)

    assert released == [unsigned]
    assert m.pending_row(c, unsigned)["resolution_reason"] == "runner_stopped_before_signing"
    assert all(m.pending_row(c, pid)["status"] == "reserved"
               for pid in (signed, broadcast_only, nonce_only, payload_bound))


def test_runner_stop_closes_an_unsigned_lifi_lifecycle():
    c = _c()
    pending_id = m.reserve_pending(
        c, mint="0xtoken", source_user_id="u", strategy="alpha_b", notional=5.0,
        reserved_at=FRESH, network_id="4663",
    )
    lifecycle_id = m.start_lifi_lifecycle(
        c, pending_id, chain_id="4663", custody_wallet="0x" + "1" * 40,
        token="0x" + "2" * 40, release_sha="a" * 40,
        manifest_hash="b" * 64, created_at=FRESH,
    )

    assert m.release_unsigned_reservations_after_runner_stop(c) == [pending_id]
    assert m.latest_lifi_lifecycle(c, lifecycle_id)["state"] == "entry_failed"


def test_runner_stop_releases_only_unsigned_close_claims():
    c = _c()
    unsigned = m.open_position(
        c, source_user_id="u", source_handle="u", mint="M_CLOSE_UNSIGNED", ticker="",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=0.0,
        their_size_usd=0.0, opened_at=FRESH, execution_mode="real",
    )
    signed = m.open_position(
        c, source_user_id="u", source_handle="u", mint="M_CLOSE_SIGNED", ticker="",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=0.0,
        their_size_usd=0.0, opened_at=FRESH, execution_mode="real",
    )
    unsigned_claim = m.claim_position_close(c, unsigned, FRESH, "manual")
    signed_claim = m.claim_position_close(c, signed, FRESH, "manual")
    c.execute("UPDATE memecoin_positions SET close_sig='0xsigned' WHERE id=?", (signed,))
    c.commit()

    released = m.release_unsigned_close_claims_after_runner_stop(c)

    assert released == [unsigned]
    assert m.get_position(c, unsigned)["close_claim_id"] is None
    assert m.get_position(c, signed)["close_claim_id"] == signed_claim
    assert unsigned_claim


def test_confirmed_full_exit_clears_stuck_bag():
    c = _c()
    position_id = m.open_position(
        c, source_user_id="u", source_handle="u", mint="M_STUCK_CLOSE", ticker="",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="chain", entry_lag_s=0.0,
        their_size_usd=0.0, opened_at=FRESH, execution_mode="real",
    )
    m.mark_stuck_bag(c, position_id)
    claim_id = m.claim_position_close(c, position_id, FRESH, "manual")
    assert m.mark_position_close_submitted(c, position_id, claim_id, FRESH, "0xclosed")

    assert m.book_full_exit_and_finalize(
        c, position_id=position_id, claim_id=claim_id, sig="0xclosed", closed_at=NOW,
        realized_pnl=1.0, exit_reason="manual", fill_kind="bracket_exit", price=1.1,
        qty=10.0, notional=11.0,
    )

    assert m.get_position(c, position_id)["stuck_bag"] == 0


def test_runner_stop_cleanup_allows_an_empty_first_run_database():
    c = sqlite3.connect(":memory:")

    assert m.release_unsigned_reservations_after_runner_stop(c) == []


def test_failed_tx_released():
    c = _c()
    pid = _reserve(c, "M2", reserved_at=OLD, sig="SIG_BAD", nonce=1)
    R.recover_stale_reservations(c, confirm_for=_confirm_for, now_iso=NOW)
    row = m.pending_row(c, pid)
    assert row["status"] == "failed"
    assert row["resolution_reason"] == "tx_failed_recovery"
    assert m.pending_totals(c) == (0, 0.0)


def test_confirmed_native_cleanup_releases_before_the_failed_swap_check():
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok", source_user_id="u", strategy="alpha_b", notional=10.0,
        reserved_at=OLD, network_id="4663")
    m.set_pending_broadcast(c, pid, "0xswap", 7, OLD)
    m.bind_pending_cleanup(
        c, pid, tx_sig="0xcleanup", nonce=8, amount_atomic=10,
        payload_sha256="abc", signed_at=OLD)

    def confirm(_network, sig):
        return "confirmed" if sig == "0xcleanup" else "failed"

    out = R.recover_stale_reservations(c, confirm_for=confirm, now_iso=NOW)
    assert out == [(pid, "released_cleanup_confirmed")]
    assert m.pending_row(c, pid)["resolution_reason"] == "native_cleanup_recovered"


def test_unknown_native_cleanup_does_not_hide_a_failed_swap():
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok-failed", source_user_id="u", strategy="alpha_b", notional=10.0,
        reserved_at=OLD, network_id="4663")
    m.set_pending_broadcast(c, pid, "0xswap", 7, OLD)
    m.bind_pending_cleanup(
        c, pid, tx_sig="0xcleanup", nonce=8, amount_atomic=10,
        payload_sha256="abc", signed_at=OLD)

    def confirm(_network, sig):
        return "unavailable" if sig == "0xcleanup" else "failed"

    out = R.recover_stale_reservations(c, confirm_for=confirm, now_iso=NOW)

    assert out == [(pid, "released_failed")]
    row = m.pending_row(c, pid)
    assert row["status"] == "failed"
    assert row["cleanup_status"] == "unavailable"
    assert row["resolution_reason"] == "tx_failed_recovery"


def test_evm_confirmed_nonce_releases_unfound_primary_and_cleanup():
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok-superseded", source_user_id="u", strategy="alpha_b", notional=10.0,
        reserved_at=OLD, network_id="4663")
    m.set_pending_broadcast(c, pid, "0xswap", 7, OLD)
    m.bind_pending_cleanup(
        c, pid, tx_sig="0xcleanup", nonce=8, amount_atomic=10,
        payload_sha256="abc", signed_at=OLD)

    out = R.recover_stale_reservations(
        c, confirm_for=lambda _network, _sig: "not_found", now_iso=NOW,
        confirmed_nonce_for=lambda _network: 9,
    )

    assert out == [(pid, "released_evm_nonces_superseded")]
    row = m.pending_row(c, pid)
    assert row["status"] == "failed"
    assert row["resolution_reason"] == "evm_nonces_superseded"


def test_evm_confirmed_nonce_at_cleanup_nonce_keeps_the_reservation():
    c = _c()
    pid = m.reserve_pending(
        c, mint="0xtok-live", source_user_id="u", strategy="alpha_b", notional=10.0,
        reserved_at=OLD, network_id="4663")
    m.set_pending_broadcast(c, pid, "0xswap", 7, OLD)
    m.bind_pending_cleanup(
        c, pid, tx_sig="0xcleanup", nonce=8, amount_atomic=10,
        payload_sha256="abc", signed_at=OLD)

    out = R.recover_stale_reservations(
        c, confirm_for=lambda _network, _sig: "not_found", now_iso=NOW,
        confirmed_nonce_for=lambda _network: 8,
    )

    assert out == []
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_confirmed_evm_native_cleanup_recovers_before_orphaning():
    c = _c()
    pid = _reserve(c, "0xtok", reserved_at=OLD, sig="0xpre", nonce=7)
    c.execute("UPDATE memecoin_pending_entries SET network_id='4663', "
              "recovery_status='native_pre_transaction_ambiguous' WHERE id=?", (pid,))
    c.commit()

    def recover(_conn, row, _now):
        assert row["network_id"] == "4663"
        assert row["recovery_status"] == "native_pre_transaction_ambiguous"
        m.confirm_pending(c, row["id"])
        return {"status": "cleanup_confirmed"}

    out = R.recover_stale_reservations(
        c, confirm_for=lambda _network, _sig: "confirmed", now_iso=NOW,
        confirmed_entry_for=recover,
    )

    assert out == [(pid, "recovered_confirmed")]
    assert m.pending_row(c, pid)["status"] == "confirmed"


def test_confirmed_orphan_alerts_once_and_stays_blocking():
    c = _c()
    pid = _reserve(c, "M3", reserved_at=OLD, sig="SIG_OK", nonce=2)
    alerts = []
    out = R.recover_stale_reservations(c, confirm_for=_confirm_for, now_iso=NOW,
                                       alert_fn=lambda mint, p: alerts.append((mint, p)))
    assert (pid, "orphaned_confirmed") in out
    row = m.pending_row(c, pid)
    assert row["status"] == "reserved" and row["recovery_status"] == "orphaned"   # STILL blocks the coin
    assert m.pending_totals(c) == (1, 10.0)           # still counts exposure (we hold the coin)
    assert alerts == [("M3", pid)]
    # second pass must NOT re-alert
    R.recover_stale_reservations(c, confirm_for=_confirm_for, now_iso=NOW,
                                 alert_fn=lambda mint, p: alerts.append((mint, p)))
    assert alerts == [("M3", pid)]


def test_confirmed_solana_receipt_recovers_the_position_and_reservation_early():
    from sniper.execution.solana_swap_executor import recover_confirmed_entry
    from sniper.execution.solana_wallet import USDC_MINT

    c = _c()
    attempt_id = "attempt-recovered"
    m.start_entry_attempt(
        c, attempt_id=attempt_id, decision_started_at=OLD,
        network_id="solana", mint="M_RECOVERED",
    )
    pid = m.reserve_pending(
        c, mint="M_RECOVERED", source_user_id="u", strategy="alpha_b",
        notional=10.0, reserved_at="2026-07-09T00:55:00Z",
        network_id="solana", attempt_id=attempt_id, source_price=1.5,
    )
    m.set_pending_broadcast(c, pid, "SIG_RECOVERED", None, OLD)
    m.mark_pending_orphaned(c, pid)
    owner = "CustodyWallet"
    receipt = {
        "blockTime": 1788204959,
        "transaction": {
            "signatures": ["SIG_RECOVERED"],
            "message": {"accountKeys": [{"pubkey": owner, "signer": True}]},
        },
        "meta": {
            "err": None,
            "preTokenBalances": [
                {"accountIndex": 1, "mint": USDC_MINT, "owner": owner,
                 "uiTokenAmount": {"amount": "20000000", "decimals": 6}},
            ],
            "postTokenBalances": [
                {"accountIndex": 1, "mint": USDC_MINT, "owner": owner,
                 "uiTokenAmount": {"amount": "10000000", "decimals": 6}},
                {"accountIndex": 2, "mint": "M_RECOVERED", "owner": owner,
                 "uiTokenAmount": {"amount": "5000000", "decimals": 6}},
            ],
        },
    }

    out = R.recover_stale_reservations(
        c, confirm_for=lambda _network, _sig: "confirmed", now_iso=NOW,
        expiry_proof_s=90,
        confirmed_entry_for=lambda conn, row, now: recover_confirmed_entry(
            conn, row, receipt, owner=owner, now_iso=now),
    )

    assert out == [(pid, "recovered_confirmed")]
    row = m.pending_row(c, pid)
    assert row["status"] == "confirmed" and row["recovery_status"] is None
    position = c.execute(
        "SELECT * FROM memecoin_positions WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    fill = c.execute(
        "SELECT * FROM memecoin_fills WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    assert position["entry_notional"] == 10.0 and position["entry_price"] == 2.0
    assert fill["qty"] == 5.0 and fill["tx_sig"] == "SIG_RECOVERED"


def test_confirmed_solana_entry_uses_the_receipt_recovery_before_orphaning():
    c = _c()
    pid = _reserve(c, "M_RECEIPT", reserved_at=OLD, sig="SIG_OK", nonce=2)
    seen = []

    def recover(_conn, row, now_iso):
        seen.append((row["id"], now_iso))
        m.confirm_pending(c, row["id"])
        return {"status": "filled", "position_id": 99}

    out = R.recover_stale_reservations(
        c, confirm_for=_confirm_for, now_iso=NOW, confirmed_entry_for=recover,
    )

    assert out == [(pid, "recovered_confirmed")]
    assert seen == [(pid, NOW)]
    assert m.pending_row(c, pid)["status"] == "confirmed"


@pytest.mark.parametrize(("network_id", "mint", "sig"), [
    ("solana", "ContractSol", "SIG_OK"),
    ("4663", "0xcontract", "SIG_OK"),
])
def test_confirmed_orphan_preserves_funded_contract_for_manual_reconciliation(
    network_id, mint, sig
):
    c = _c()
    attempt_id = f"attempt-{network_id}"
    m.start_entry_attempt(
        c, attempt_id=attempt_id, decision_started_at=OLD,
        network_id=network_id, mint=mint,
    )
    payload, digest = m.canonical_funded_json({
        "contract_version": 1,
        "identity": {"network_id": network_id, "mint": mint},
    })
    m.set_entry_attempt_funded_contract(
        c, attempt_id, funded_contract_json=payload, funded_contract_hash=digest,
        funded_contract_version=1,
    )
    pid = m.reserve_pending(
        c, mint=mint, source_user_id="u", strategy="alpha_b", notional=10.0,
        reserved_at=OLD, network_id=network_id, attempt_id=attempt_id,
        funded_contract_version=1, funded_contract_hash=digest,
        funded_contract_json=payload,
    )
    m.set_pending_broadcast(c, pid, sig, 9, OLD)

    out = R.recover_stale_reservations(
        c, confirm_for=lambda _network, _sig: "confirmed", now_iso=NOW,
    )

    row = m.pending_row(c, pid)
    assert out == [(pid, "orphaned_confirmed")]
    assert row["status"] == "reserved" and row["recovery_status"] == "orphaned"
    assert (row["funded_contract_version"], row["funded_contract_hash"],
            row["funded_contract_json"]) == (1, digest, payload)


def test_pending_left_reserved_for_retry_and_resets_not_found_proof():
    # stale (>600s) but NOT yet expired (<1200s): the retry window. Ancient (>1200s) solana
    # unknowns now expire instead (blockhash death is definitive) — covered in
    # test_memecoin_real_deploy.py::test_recovery_expires_ancient_unknown_solana_reservation.
    c = _c()
    pid = _reserve(c, "M4", reserved_at="2026-07-09T00:45:00Z", sig="SIG_PENDING", nonce=3)
    c.execute(
        "UPDATE memecoin_pending_entries SET not_found_checks=2, "
        "first_not_found_at='2026-07-09T00:50:00Z', "
        "last_recovery_check_at='2026-07-09T00:51:00Z' WHERE id=?",
        (pid,),
    )
    c.commit()
    R.recover_stale_reservations(c, confirm_for=_confirm_for, now_iso=NOW)
    row = m.pending_row(c, pid)
    assert row["status"] == "reserved"   # not resolved -> retry next pass
    assert row["not_found_checks"] == 0
    assert row["first_not_found_at"] is None
    assert m.pending_totals(c) == (1, 10.0)


def test_explicit_not_found_requires_three_spaced_checks_after_expiry():
    c = _c()
    pid = _reserve(c, "M6", reserved_at=OLD, sig="SIG_MISSING", nonce=6)
    confirm = lambda _net, _sig: "not_found"

    first = R.recover_stale_reservations(c, confirm_for=confirm, now_iso=NOW)
    assert first == []
    assert m.pending_row(c, pid)["not_found_checks"] == 1

    too_fast = R.recover_stale_reservations(
        c, confirm_for=confirm, now_iso="2026-07-09T01:00:30Z",
    )
    assert too_fast == []
    assert m.pending_row(c, pid)["not_found_checks"] == 1

    second = R.recover_stale_reservations(
        c, confirm_for=confirm, now_iso="2026-07-09T01:01:00Z",
    )
    assert second == []
    assert m.pending_row(c, pid)["not_found_checks"] == 2

    third = R.recover_stale_reservations(
        c, confirm_for=confirm, now_iso="2026-07-09T01:02:00Z",
    )
    assert third == [(pid, "released_expired")]
    row = m.pending_row(c, pid)
    assert row["status"] == "failed"
    assert row["resolution_reason"] == "expired_unfound_proven"


def test_unavailable_does_not_increment_or_release_expiry_proof():
    c = _c()
    pid = _reserve(c, "M7", reserved_at=OLD, sig="SIG_RPC_DOWN", nonce=7)
    out = R.recover_stale_reservations(
        c, confirm_for=lambda _net, _sig: "unavailable", now_iso=NOW,
    )
    row = m.pending_row(c, pid)
    assert out == []
    assert row["status"] == "reserved"
    assert row["not_found_checks"] == 0


def test_evm_not_found_never_uses_solana_expiry_release():
    c = _c()
    pid = _reserve(c, "0xabc", reserved_at=OLD, sig="0xsig", nonce=8)
    c.execute("UPDATE memecoin_pending_entries SET network_id='8453' WHERE id=?", (pid,))
    c.commit()
    for minute in range(3):
        R.recover_stale_reservations(
            c,
            confirm_for=lambda _net, _sig: "not_found",
            now_iso=f"2026-07-09T01:0{minute}:00Z",
        )
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_broadcast_timestamp_without_signature_is_ambiguous_not_released():
    c = _c()
    pid = _reserve(c, "M8", reserved_at=OLD)
    c.execute(
        "UPDATE memecoin_pending_entries SET broadcast_at=? WHERE id=?",
        (OLD, pid),
    )
    c.commit()
    out = R.recover_stale_reservations(c, confirm_for=_confirm_for, now_iso=NOW)
    row = m.pending_row(c, pid)
    assert out == [(pid, "ambiguous_broadcast")]
    assert row["status"] == "reserved"
    assert row["recovery_status"] == "ambiguous_broadcast"


def test_fresh_reservation_untouched():
    c = _c()
    _reserve(c, "M5", reserved_at=FRESH, sig="SIG_BAD", nonce=4)   # would release IF it were stale
    out = R.recover_stale_reservations(c, confirm_for=_confirm_for, now_iso=NOW)
    assert out == []
    assert m.pending_totals(c) == (1, 10.0)                # in-flight, not touched


# --- the bound EVM sig is the native pre-transaction, not the swap (2026-09-03) -----------------

def _evm_reserve(c, mint, *, nonce, reserved_at=OLD, sig="SIG_OK"):
    pid = m.reserve_pending(c, mint=mint, source_user_id="u", strategy="alpha_b", notional=98.79,
                            reserved_at=reserved_at, network_id="4663")
    m.set_pending_broadcast(c, pid, sig, nonce, reserved_at)
    return pid


def _recover(c, **kw):
    return R.recover_stale_reservations(c, confirm_for=_confirm_for, now_iso=NOW, **kw)


def test_confirmed_evm_pretransaction_with_no_token_releases_the_slot():
    """Live 2026-09-03 reservation 1955: the WETH unwrap confirmed, the swap never ran, the gas
    top-up re-wrapped the native, and the wallet held zero of the coin. The old rule read the
    confirmed unwrap as "we hold the coin" and locked the entire 4663 bankroll for two hours."""
    c = _c()
    pid = _evm_reserve(c, "0xMINT", nonce=1342)
    alerts = []
    out = _recover(c, alert_fn=lambda mint, p: alerts.append((mint, p)),
                   confirmed_nonce_for=lambda _n: 1344,
                   token_balance_for=lambda _n, _m: 0)
    assert (pid, "released_pretransaction_no_token") in out
    row = m.pending_row(c, pid)
    assert row["status"] == "failed"
    assert row["resolution_reason"] == "evm_confirmed_pretransaction_no_token_proven"
    assert m.pending_totals(c) == (0, 0.0)           # the slot and the bankroll are free again
    assert alerts == []                              # nothing for a human to reconcile


def test_a_held_token_still_orphans_and_keeps_blocking():
    """The protection that matters: when the swap DID land, the slot must stay reserved."""
    c = _c()
    pid = _evm_reserve(c, "0xHELD", nonce=1342)
    out = _recover(c, confirmed_nonce_for=lambda _n: 1344,
                   token_balance_for=lambda _n, _m: 4_200_000)
    assert (pid, "orphaned_confirmed") in out
    row = m.pending_row(c, pid)
    assert row["status"] == "reserved" and row["recovery_status"] == "orphaned"
    assert m.pending_totals(c) == (1, 98.79)


def test_an_unreadable_balance_never_releases():
    """A failed read must not free a funded slot. Fail closed."""
    c = _c()
    pid = _evm_reserve(c, "0xUNREADABLE", nonce=1342)

    def boom(_n, _m):
        raise RuntimeError("rpc down")

    out = _recover(c, confirmed_nonce_for=lambda _n: 1344, token_balance_for=boom)
    assert (pid, "orphaned_confirmed") in out
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_a_live_entry_nonce_never_releases():
    """Until the account nonce passes the entry's own nonce, bytes signed for that entry can still
    land, so a zero balance right now proves nothing."""
    c = _c()
    pid = _evm_reserve(c, "0xLIVENONCE", nonce=1342)
    out = _recover(c, confirmed_nonce_for=lambda _n: 1342, token_balance_for=lambda _n, _m: 0)
    assert (pid, "orphaned_confirmed") in out
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_solana_confirmed_still_orphans_because_its_sig_is_the_swap():
    """On Solana the bound signature IS the swap, so 'confirmed' already means the coin is ours."""
    c = _c()
    pid = _reserve(c, "SOLMINT", reserved_at=OLD, sig="SIG_OK", nonce=2)
    out = _recover(c, confirmed_nonce_for=lambda _n: 99, token_balance_for=lambda _n, _m: 0)
    assert (pid, "orphaned_confirmed") in out
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_without_the_readers_behaviour_is_exactly_as_before():
    """No injected readers -> the old orphan hold, unchanged."""
    c = _c()
    pid = _evm_reserve(c, "0xNOREADER", nonce=1342)
    out = _recover(c)
    assert (pid, "orphaned_confirmed") in out
    assert m.pending_row(c, pid)["status"] == "reserved"


# --- a never-admitted EVM entry deadlocks the bankroll (2026-09-04, reservation 1967) ----------

def _never_admitted(c, mint="0xDEAD", nonce=1361, reserved_at=OLD):
    pid = m.reserve_pending(c, mint=mint, source_user_id="u", strategy="alpha_b", notional=55.42,
                            reserved_at=reserved_at, network_id="4663")
    m.set_pending_broadcast(c, pid, "SIG_GONE", nonce, reserved_at)
    c.execute("UPDATE memecoin_pending_entries SET signed_payload_b64='cmF3Ynl0ZXM=' WHERE id=?", (pid,))
    c.commit()
    return pid


def _gone(_net, sig):
    return "not_found" if sig == "SIG_GONE" else _confirm_for(_net, sig)


def test_a_never_admitted_entry_is_released_and_its_payload_destroyed():
    """Reservation 1967 held the whole funded 4663 bankroll for 3h20m: $55.42 reserved against a
    $55.30 balance. Releasing needs the nonce to PASS, a never-admitted tx never moves it, and the
    bot could not send anything because the notional was reserved. It only broke when the operator
    sold an unrelated position by hand."""
    c = _c()
    pid = _never_admitted(c)
    out = R.recover_stale_reservations(
        c, confirm_for=_gone, now_iso=NOW,
        confirmed_nonce_for=lambda _n: 1361,          # nonce NEVER moved
        pending_nonce_for=lambda _n: 1361,            # node holds nothing of ours
        token_balance_for=lambda _n, _m: 0)           # the buy never landed
    assert (pid, "released_evm_never_admitted") in out
    row = m.pending_row(c, pid)
    assert row["status"] == "failed"
    assert "evm_entry_never_admitted" in row["resolution_reason"]
    assert row["signed_payload_b64"] is None, "the rebroadcastable bytes must be destroyed"
    assert m.pending_totals(c) == (0, 0.0)            # the bankroll is free again


def test_a_landed_entry_is_never_released_by_this_proof():
    """The protection that matters: holding the coin means the buy LANDED. That is an orphan for a
    human, not a ghost to free."""
    c = _c()
    pid = _never_admitted(c)
    out = R.recover_stale_reservations(
        c, confirm_for=_gone, now_iso=NOW,
        confirmed_nonce_for=lambda _n: 1361, pending_nonce_for=lambda _n: 1361,
        token_balance_for=lambda _n, _m: 4_000_000)
    assert (pid, "released_evm_never_admitted") not in out
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_a_node_still_holding_our_transaction_blocks_the_release():
    """pending > latest means some node has it and it may yet mine."""
    c = _c()
    pid = _never_admitted(c)
    R.recover_stale_reservations(
        c, confirm_for=_gone, now_iso=NOW,
        confirmed_nonce_for=lambda _n: 1361, pending_nonce_for=lambda _n: 1362,
        token_balance_for=lambda _n, _m: 0)
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_a_young_reservation_is_never_called_dead():
    c = _c()
    pid = _never_admitted(c, reserved_at="2026-07-09T00:59:30Z")   # 30s before NOW
    R.recover_stale_reservations(
        c, confirm_for=_gone, now_iso=NOW,
        confirmed_nonce_for=lambda _n: 1361, pending_nonce_for=lambda _n: 1361,
        token_balance_for=lambda _n, _m: 0)
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_an_unreadable_probe_releases_nothing():
    c = _c()
    pid = _never_admitted(c)

    def boom(*_a):
        raise RuntimeError("rpc down")

    R.recover_stale_reservations(
        c, confirm_for=_gone, now_iso=NOW, confirmed_nonce_for=lambda _n: 1361,
        pending_nonce_for=boom, token_balance_for=lambda _n, _m: 0)
    assert m.pending_row(c, pid)["status"] == "reserved"


def test_solana_is_untouched_by_the_evm_proof():
    c = _c()
    pid = _reserve(c, "SOLM", reserved_at=OLD, sig="SIG_GONE", nonce=1361)
    R.recover_stale_reservations(
        c, confirm_for=_gone, now_iso=NOW, confirmed_nonce_for=lambda _n: 1361,
        pending_nonce_for=lambda _n: 1361, token_balance_for=lambda _n, _m: 0)
    assert m.pending_row(c, pid)["status"] == "reserved"
