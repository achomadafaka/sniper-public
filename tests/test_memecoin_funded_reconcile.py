import json
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_funded_reconcile import validate_funded_contract_reconciliation


CUTOVER = "2026-07-18T12:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _contract(network_id="solana", mint="MINT"):
    contract = {
        "contract_version": 1,
        "identity": {"network_id": network_id, "mint": mint},
        "source": {"signal_source": "global_feed"},
        "classification": {"coin_band": "small"},
        "sizing": {"approved_clip_usd": 75.0},
        "policy": {"policy_id": "funded-small-v1"},
        "expectation": {"intent": "capture_trend"},
        "invalidations": ["post_peak_confirmed"],
    }
    payload, digest = m.canonical_funded_json(contract)
    return 1, digest, payload


def _attempt(conn, attempt_id, at, fields=None):
    m.start_entry_attempt(
        conn, attempt_id=attempt_id, decision_started_at=at,
        network_id="solana", mint="MINT",
    )
    if fields:
        m.set_entry_attempt_funded_contract(
            conn, attempt_id,
            funded_contract_version=fields[0],
            funded_contract_hash=fields[1],
            funded_contract_json=fields[2],
        )


def test_valid_post_cutover_pending_and_position_match_attempt():
    conn = _conn()
    fields = _contract()
    _attempt(conn, "attempt-1", "2026-07-18T12:00:01+00:00", fields)
    pending_id = m.reserve_pending(
        conn, mint="MINT", source_user_id="u", strategy="alpha_b", notional=75,
        reserved_at="2026-07-18T12:00:02+00:00", network_id="solana",
        attempt_id="attempt-1", funded_contract_version=fields[0],
        funded_contract_hash=fields[1], funded_contract_json=fields[2],
    )
    m.open_position(
        conn, source_user_id="u", source_handle="u", mint="MINT", ticker="M",
        entry_price=1, entry_notional=75, entry_fill_source="chain", entry_lag_s=1,
        their_size_usd=100, opened_at="2026-07-18T12:00:03+00:00",
        strategy="alpha_b", execution_mode="real", network_id="solana",
        attempt_id="attempt-1", funded_contract_version=fields[0],
        funded_contract_hash=fields[1], funded_contract_json=fields[2],
    )
    m.confirm_pending(conn, pending_id)

    assert validate_funded_contract_reconciliation(conn, cutover_at=CUTOVER) == []


def test_post_cutover_pending_without_contract_is_blocking():
    conn = _conn()
    _attempt(conn, "attempt-1", "2026-07-18T12:00:01+00:00")
    m.reserve_pending(
        conn, mint="MINT", source_user_id="u", strategy="alpha_b", notional=75,
        reserved_at="2026-07-18T12:00:02+00:00", network_id="solana",
        attempt_id="attempt-1",
    )

    errors = validate_funded_contract_reconciliation(conn, cutover_at=CUTOVER)

    assert any("post-cutover pending" in error and "missing funded contract" in error
               for error in errors)


def test_post_cutover_position_hash_mismatch_is_blocking():
    conn = _conn()
    fields = _contract()
    _attempt(conn, "attempt-1", "2026-07-18T12:00:01+00:00", fields)
    m.open_position(
        conn, source_user_id="u", source_handle="u", mint="MINT", ticker="M",
        entry_price=1, entry_notional=75, entry_fill_source="chain", entry_lag_s=1,
        their_size_usd=100, opened_at="2026-07-18T12:00:03+00:00",
        strategy="alpha_b", execution_mode="real", network_id="solana",
        attempt_id="attempt-1", funded_contract_version=fields[0],
        funded_contract_hash="0" * 64, funded_contract_json=fields[2],
    )

    errors = validate_funded_contract_reconciliation(conn, cutover_at=CUTOVER)

    assert any("post-cutover position" in error and "hash" in error for error in errors)


def test_pre_cutover_contractless_rows_remain_legacy_compatible():
    conn = _conn()
    _attempt(conn, "attempt-old", "2026-07-18T11:59:57+00:00")
    m.reserve_pending(
        conn, mint="OLD", source_user_id="u", strategy="alpha_b", notional=75,
        reserved_at="2026-07-18T11:59:58+00:00", network_id="solana",
        attempt_id="attempt-old",
    )
    m.open_position(
        conn, source_user_id="u", source_handle="u", mint="OLD", ticker="OLD",
        entry_price=1, entry_notional=75, entry_fill_source="chain", entry_lag_s=1,
        their_size_usd=100, opened_at="2026-07-18T11:59:59+00:00",
        strategy="alpha_b", execution_mode="real", network_id="solana",
        attempt_id="attempt-old",
    )

    assert validate_funded_contract_reconciliation(conn, cutover_at=CUTOVER) == []


def test_malformed_post_cutover_contract_is_blocking():
    conn = _conn()
    fields = (1, "0" * 64, json.dumps({"contract_version": 1}))
    _attempt(conn, "attempt-1", "2026-07-18T12:00:01+00:00")
    conn.execute(
        "UPDATE memecoin_entry_attempts SET funded_contract_version=?,"
        "funded_contract_hash=?,funded_contract_json=? WHERE attempt_id='attempt-1'", fields,
    )
    conn.execute(
        "INSERT INTO memecoin_pending_entries(mint,source_user_id,strategy,reserved_notional_usd,"
        "reserved_at,status,network_id,attempt_id,funded_contract_version,funded_contract_hash,"
        "funded_contract_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("MINT", "u", "alpha_b", 75, "2026-07-18T12:00:02+00:00", "reserved",
         "solana", "attempt-1", *fields),
    )
    conn.commit()

    errors = validate_funded_contract_reconciliation(conn, cutover_at=CUTOVER)

    assert any("post-cutover pending" in error for error in errors)


# ---- severity split (2026-07-21) -------------------------------------------------------------
# The 07-21 funded outage: a CLOSED manual position (and its consumed reservation) missing a
# contract bricked armed startup for 26 min, guarding no live risk. Terminal rows are audit
# debt -> loud warning, startup proceeds. Live rows (open position / unresolved pending) still
# block. The deploy validator stays strict (blocking + warnings both fail a deploy).

from sniper.services.memecoin_funded_reconcile import reconcile_severity


def _contractless_cohort(conn, *, is_open, pending_terminal):
    _attempt(conn, "attempt-t", "2026-07-18T12:00:01+00:00")
    pending_id = m.reserve_pending(
        conn, mint="MINT", source_user_id="u", strategy="alpha_b", notional=75,
        reserved_at="2026-07-18T12:00:02+00:00", network_id="solana",
        attempt_id="attempt-t",
    )
    pos_id = m.open_position(
        conn, source_user_id="u", source_handle="u", mint="MINT", ticker="M",
        entry_price=1, entry_notional=75, entry_fill_source="chain", entry_lag_s=1,
        their_size_usd=100, opened_at="2026-07-18T12:00:03+00:00",
        strategy="alpha_b", execution_mode="real", network_id="solana",
        attempt_id="attempt-t",
    )
    if pending_terminal:
        m.confirm_pending(conn, pending_id)
    if not is_open:
        conn.execute(
            "UPDATE memecoin_positions SET is_open=0, closed_at='2026-07-18T14:00:00+00:00', "
            "realized_pnl=-5.0, exit_reason='manual' WHERE id=?", (pos_id,))
        conn.commit()
    return pending_id, pos_id


def test_terminal_contractless_rows_warn_but_do_not_block():
    conn = _conn()
    _contractless_cohort(conn, is_open=False, pending_terminal=True)
    blocking, warnings = reconcile_severity(conn, cutover_at=CUTOVER)
    assert blocking == [], f"closed cohort must not block startup: {blocking}"
    assert len(warnings) == 2   # pending + position, both flagged loudly
    assert any("pending" in w for w in warnings)
    assert any("position" in w for w in warnings)


def test_open_position_without_contract_still_blocks():
    conn = _conn()
    _contractless_cohort(conn, is_open=True, pending_terminal=True)
    blocking, warnings = reconcile_severity(conn, cutover_at=CUTOVER)
    assert any("position" in b and "missing funded contract" in b for b in blocking)


def test_unresolved_pending_without_contract_still_blocks():
    conn = _conn()
    _contractless_cohort(conn, is_open=False, pending_terminal=False)
    blocking, _warnings = reconcile_severity(conn, cutover_at=CUTOVER)
    assert any("pending" in b for b in blocking), f"live reservation must block: {blocking}"


def test_deploy_validator_stays_strict_on_terminal_debt():
    conn = _conn()
    _contractless_cohort(conn, is_open=False, pending_terminal=True)
    errors = validate_funded_contract_reconciliation(conn, cutover_at=CUTOVER)
    assert errors, "deploy-facing validator must still fail on terminal audit debt"


def test_a_refused_attempt_is_not_audit_debt():
    """A refusal happens at the risk gate, BEFORE reserve_entry, so having no reservation is the
    correct outcome rather than debt.

    This began to matter when MEMECOIN_REAL_ONE_POSITION_PER_COIN was turned off (2026-08-13). With
    the policy on, a second signal for a held coin died before a contract was built. With it off
    both racers build one and the loser is refused as "lost reservation race", leaving a contract
    with no reservation — which failed the deploy-strict view and blocked EVERY deploy.
    """
    conn = _conn()
    _attempt(conn, "refused-1", "2026-08-13T00:00:00+00:00", fields=_contract())
    conn.execute(
        "UPDATE memecoin_entry_attempts SET terminal_status='refused',"
        " terminal_reason='one position per coin: lost reservation race',"
        " completed_at='2026-08-13T00:00:01+00:00' WHERE attempt_id='refused-1'")
    conn.commit()
    assert validate_funded_contract_reconciliation(conn, cutover_at=CUTOVER) == []


def test_a_non_refused_attempt_with_no_reservation_is_still_audit_debt():
    """The exemption must stay narrow: every other terminal status occurs at or after the
    reservation, so an unlinked one still means a contract that reserved and then vanished."""
    conn = _conn()
    _attempt(conn, "filled-1", "2026-08-13T00:00:00+00:00", fields=_contract())
    conn.execute(
        "UPDATE memecoin_entry_attempts SET terminal_status='filled', terminal_reason='ok',"
        " completed_at='2026-08-13T00:00:01+00:00' WHERE attempt_id='filled-1'")
    conn.commit()
    errs = validate_funded_contract_reconciliation(conn, cutover_at=CUTOVER)
    assert any("no reservation or position" in e for e in errs), errs
