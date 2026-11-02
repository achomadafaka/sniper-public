"""A full close whose transaction provably died must release, not strand the position forever.

2026-08-14, position 37331 (RIDEABULL, +48%). The rails signed a take-profit at 12:05:14Z and
broadcast it. The transaction never landed: its blockhash expired at height 417274036 while the
chain went on to 417274232, `getSignatureStatuses` with full history search returned None, and the
token balance still equalled the intent's `balance_snapshot` to the decimal.

`recover_unverified_closes` held it anyway — it treats EVERY signed close as permanently ambiguous
and logs "never auto-released". So the rails stopped retrying and the operator's close button
returned `already_closing` on every press. The position could not be exited by anyone. It took a
hand-written CAS release against the database to free it.

The de-risk path already solved exactly this with `sig_provably_dead` (chain past
`last_valid_height` AND the full bag still held). The full-close path simply never got it. These
tests bind the full-close path to the same proof, and to nothing weaker.
"""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R

LAST_VALID = 417274036
SNAPSHOT = 4148530.751416
SIG = "5kqg3CNSLufp16nNrHEUmUyDzGEn7Ch9cXKmPADyoiv92GFZ5URudbRSeH7o2TjH4uKJSRyUkgEE1DEEeNJoqZ8z"
CLAIM = "57c5890782ee43488844c6dee1850795"
NOW = "2026-08-14T12:10:00+00:00"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "dropped_close.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    return c


def _signed_close(conn, *, sig=SIG, claim=CLAIM, last_valid=LAST_VALID, snapshot=SNAPSHOT,
                  quote_asset_usd=None, mint="MintDrop"):
    """A real position sitting exactly where 37331 sat: signed, broadcast, still open."""
    from sniper.settings import memecoin as cfg
    pid = m.open_position(
        conn, source_user_id="u1", source_handle="h", mint=mint, ticker="RIDEABULL",
        entry_price=0.00001, entry_notional=41.46, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at="2026-08-14T12:03:40+00:00",
        strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="solana")
    intent = {"action": "close", "claim_id": claim, "position_id": pid, "fraction": 1.0,
              "last_valid_height": last_valid, "balance_snapshot": snapshot, "tx_identity": sig}
    if quote_asset_usd is not None:
        intent["quote_asset_usd"] = quote_asset_usd
    conn.execute(
        "UPDATE memecoin_positions SET close_claim_id=?, close_sig=?, close_exit_reason='real_tp', "
        "close_submitted_at=?, exit_intent_json=? WHERE id=?",
        (claim, sig, "2026-08-14T12:05:14+00:00", json.dumps(intent), pid))
    conn.commit()
    return pid


def _ctx(*, height, balance):
    return {"solana": {"confirm_once_fn": lambda _sig: "not_found",
                       "block_height_fn": lambda: height,
                       "token_checked_fn": lambda _mint: balance}}


# ── the release, and its exact preconditions ────────────────────────────────

def test_dropped_close_releases_when_expired_and_bag_intact(conn):
    """The 37331 case: chain past last_valid_height AND full bag held -> release the claim."""
    pid = _signed_close(conn)
    R.recover_unverified_closes(conn, _ctx(height=LAST_VALID + 196, balance=SNAPSHOT), NOW)

    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] is None, "claim must be released so the exit can be retried"
    assert pos["close_sig"] is None
    assert pos["is_open"] == 1, "releasing a dropped sell must never book a close"
    assert pos["closed_at"] is None


def test_dropped_close_marks_the_intent_dropped(conn):
    pid = _signed_close(conn)
    R.recover_unverified_closes(conn, _ctx(height=LAST_VALID + 196, balance=SNAPSHOT), NOW)
    status = conn.execute(
        "SELECT status FROM memecoin_exit_intents WHERE claim_id=?", (CLAIM,)).fetchone()
    if status is not None:                     # intent row is optional in this fixture
        assert status[0] == "dropped"
    assert pid


def test_inside_the_blockhash_window_is_held(conn):
    """Still inside its window the sell may yet land. Releasing here risks a double-sell."""
    pid = _signed_close(conn)
    R.recover_unverified_closes(conn, _ctx(height=LAST_VALID - 1, balance=SNAPSHOT), NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM, "must stay held while the transaction can still land"
    assert pos["close_sig"] == SIG


def test_a_reduced_balance_is_held(conn):
    """A smaller bag means the sell LANDED. Never release, never re-arm — that is the double-sell."""
    pid = _signed_close(conn)
    R.recover_unverified_closes(conn, _ctx(height=LAST_VALID + 196, balance=SNAPSHOT / 2), NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM
    assert pos["close_sig"] == SIG


def test_confirmed_on_chain_is_never_released(conn):
    """A confirmed read is terminal bookkeeping, not a drop."""
    pid = _signed_close(conn)
    ctx = {"solana": {"confirm_once_fn": lambda _sig: "confirmed",
                      "block_height_fn": lambda: LAST_VALID + 196,
                      "token_checked_fn": lambda _mint: SNAPSHOT}}
    R.recover_unverified_closes(conn, ctx, NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_sig"] == SIG, "a confirmed sell must keep its signed identity"


def test_confirmed_close_books_its_receipt_proceeds(conn):
    """A confirmed signed close settles from the exact receipt when balance reads lag."""
    pid = _signed_close(conn, snapshot=10.0)
    ctx = {"solana": {
        "confirm_once_fn": lambda _sig: "confirmed",
        "receipt_proceeds_fn": lambda _sig: 15.0,
    }}

    R.recover_unverified_closes(conn, ctx, NOW)

    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    fill = conn.execute(
        "SELECT qty,notional FROM memecoin_fills WHERE position_id=? AND kind='bracket_exit'",
        (pid,),
    ).fetchone()
    assert pos["is_open"] == 0
    assert pos["realized_pnl"] == pytest.approx(-26.46)
    assert tuple(fill) == pytest.approx((10.0, 15.0))


def test_confirmed_close_uses_the_stamped_quote_asset_price(conn):
    """Recovery must value an EVM native receipt at the price bound before broadcast."""
    seen = {}
    pid = _signed_close(conn, snapshot=10.0, quote_asset_usd=2345.67)

    def proceeds(_sig, *, quote_asset_usd=None, mint="MintDrop"):
        seen["quote_asset_usd"] = quote_asset_usd
        return 15.0

    R.recover_unverified_closes(conn, {"solana": {
        "confirm_once_fn": lambda _sig: "confirmed",
        "receipt_proceeds_fn": proceeds,
    }}, NOW)

    assert seen["quote_asset_usd"] == 2345.67
    assert m.get_position(conn, pid)["realized_pnl"] == pytest.approx(-26.46)


def test_confirmed_close_without_receipt_closes_as_unpriced(conn):
    """A confirmed close never remains an open bag when USD proceeds are unavailable."""
    pid = _signed_close(conn, snapshot=10.0)
    ctx = {"solana": {"confirm_once_fn": lambda _sig: "confirmed"}}

    R.recover_unverified_closes(conn, ctx, NOW)

    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    fill = conn.execute(
        "SELECT qty,notional,fill_source FROM memecoin_fills "
        "WHERE position_id=? AND kind='bracket_exit'", (pid,),
    ).fetchone()
    assert pos["is_open"] == 0
    assert pos["realized_pnl"] is None
    assert tuple(fill) == (10.0, None, "chain_confirmed_unpriced")


def test_unpriced_confirmed_close_can_be_reconciled_once(conn):
    """A confirmed but unpriced full exit can receive its later receipt accounting once."""
    pid = _signed_close(conn, snapshot=10.0)
    R.recover_unverified_closes(conn, {"solana": {
        "confirm_once_fn": lambda _sig: "confirmed",
    }}, NOW)

    assert m.reconcile_unpriced_full_exit(
        conn, position_id=pid, sig=SIG, price=1.5, qty=10.0, notional=15.0,
        fill_source="chain_confirmed_native_reconciled",
    ) is True
    pos = m.get_position(conn, pid)
    fill = conn.execute(
        "SELECT price,notional,fill_source FROM memecoin_fills WHERE position_id=? "
        "AND kind='bracket_exit'", (pid,),
    ).fetchone()
    assert pos["realized_pnl"] == pytest.approx(-26.46)
    assert tuple(fill) == (1.5, 15.0, "chain_confirmed_native_reconciled")
    assert not m.reconcile_unpriced_full_exit(
        conn, position_id=pid, sig=SIG, price=1.5, qty=10.0, notional=15.0,
        fill_source="chain_confirmed_native_reconciled",
    )


def test_closed_unpriced_manual_close_reconciles_when_the_receipt_becomes_available(conn):
    """A later receipt pass must price a manual close that recovery closed without proceeds."""
    pid = _signed_close(conn, snapshot=10.0)
    conn.execute(
        "UPDATE memecoin_positions SET close_exit_reason='manual' WHERE id=?", (pid,))
    conn.commit()
    R.recover_unverified_closes(conn, {"solana": {
        "confirm_once_fn": lambda _sig: "confirmed",
    }}, NOW)

    R.recover_unverified_closes(conn, {"solana": {
        "confirm_once_fn": lambda _sig: "confirmed",
        "receipt_proceeds_fn": lambda _sig: 15.0,
    }}, NOW)

    pos = m.get_position(conn, pid)
    fill = conn.execute(
        "SELECT kind,price,qty,notional,fill_source FROM memecoin_fills WHERE position_id=? ",
        (pid,),
    ).fetchone()
    assert pos["realized_pnl"] == pytest.approx(-26.46)
    assert tuple(fill) == ("manual_close", 1.5, 10.0, 15.0,
                           "chain_confirmed_receipt_reconciled")


def test_missing_height_probe_holds(conn):
    """Fail closed. A chain with no height probe (EVM today) keeps the old hold-forever behaviour."""
    pid = _signed_close(conn)
    ctx = {"solana": {"confirm_once_fn": lambda _sig: "not_found",
                      "token_checked_fn": lambda _mint: SNAPSHOT}}
    R.recover_unverified_closes(conn, ctx, NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM


def test_probe_exception_holds(conn):
    """An unreadable balance is not evidence of anything."""
    def boom(_mint):
        raise RuntimeError("rpc down")

    pid = _signed_close(conn)
    ctx = {"solana": {"confirm_once_fn": lambda _sig: "not_found",
                      "block_height_fn": lambda: LAST_VALID + 196,
                      "token_checked_fn": boom}}
    R.recover_unverified_closes(conn, ctx, NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM


# ── the release primitive is CAS-guarded ────────────────────────────────────

def test_release_is_cas_guarded_on_claim_and_sig(conn):
    """A stale recovery pass must not release a claim that has since been replaced."""
    pid = _signed_close(conn)
    assert m.release_dropped_close_claim(conn, pid, "other-claim", SIG) is False
    assert m.release_dropped_close_claim(conn, pid, CLAIM, "other-sig") is False
    assert m.release_dropped_close_claim(conn, pid, CLAIM, SIG) is True
    assert m.release_dropped_close_claim(conn, pid, CLAIM, SIG) is False, "not idempotent-releasable"


def test_release_refuses_a_closed_position(conn):
    """Terminal bookkeeping wins. Never reopen a settled close."""
    pid = _signed_close(conn)
    conn.execute("UPDATE memecoin_positions SET is_open=0, closed_at=? WHERE id=?", (NOW, pid))
    conn.commit()
    assert m.release_dropped_close_claim(conn, pid, CLAIM, SIG) is False


# ── the operator hears about it ─────────────────────────────────────────────

def test_release_alerts_the_operator(conn, monkeypatch):
    """A position that froze for 20 minutes with no page is how this cost an afternoon."""
    seen = []
    monkeypatch.setattr(R.alerts, "alert_close_rearmed",
                        lambda mint, reason, **kw: seen.append((mint, reason)))
    _signed_close(conn)
    R.recover_unverified_closes(conn, _ctx(height=LAST_VALID + 196, balance=SNAPSHOT), NOW)
    assert seen, "releasing a dropped close must page the operator"
    assert "MintDrop" == seen[0][0]


# ── a REVERTED close: the receipt is proof, and it was treated as an unknown ─────────────────
#
# RIZZLER 48554, 2026-09-02. A b_stop fired on a quote reading -38.6%; the sell REVERTED on chain
# (0x2f6f86c6…, receipt status 0x0, 739k gas). The position then sat un-exitable while the next two
# readings said +23% and +33%, and five further exit attempts all returned `stuck`.
#
# `eth_confirm` returns "failed" for exactly one thing: a mined receipt with status 0x0. A reverted
# transaction changes no state and moves no tokens, so the bag is intact BY CONSTRUCTION — strictly
# stronger evidence than the not_found path above, which must infer "never landed" from an expiry
# height plus a balance read. Only the weaker case released.


def _reverted_ctx(**over):
    ctx = {"solana": {"confirm_once_fn": lambda _sig: "failed"}}
    ctx["solana"].update(over)
    return ctx


def test_a_reverted_close_releases_the_claim_and_re_arms_the_exit(conn):
    pid = _signed_close(conn)
    R.recover_unverified_closes(conn, _reverted_ctx(), NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] is None, "a reverted sell must not pin the position"
    assert pos["close_sig"] is None
    assert pos["is_open"] == 1, "a revert moved no tokens — never book a close"
    assert pos["closed_at"] is None


def test_it_needs_NO_balance_read_because_a_revert_moves_nothing(conn):
    """The revert is usually caused by the same provider trouble that breaks balance reads. If the
    release depended on one, it would fail exactly when it is needed."""
    def boom(_mint):
        raise RuntimeError("EVM RPC provider HTTP failure")
    pid = _signed_close(conn)
    R.recover_unverified_closes(
        conn, _reverted_ctx(token_checked_fn=boom, block_height_fn=boom), NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] is None, "a dead RPC must not block a receipt-proven release"
    assert pid


def test_a_reverted_close_marks_the_intent_failed_not_submitted_unknown(conn):
    _signed_close(conn)
    R.recover_unverified_closes(conn, _reverted_ctx(), NOW)
    row = conn.execute(
        "SELECT status FROM memecoin_exit_intents WHERE claim_id=?", (CLAIM,)).fetchone()
    if row is not None:
        assert row[0] == "failed", "the receipt said reverted; the intent must not say unknown"


@pytest.mark.parametrize("state", ["unknown", "unavailable", "not_found"])
def test_an_ambiguous_chain_state_is_still_held(conn, state):
    """The guard on the guard. ONLY a mined status-0x0 receipt releases. Ambiguity never does: a
    broadcast that may have landed must not be re-armed into a double-sell. `not_found` here has no
    height/balance seams, so it cannot reach the provably-dead branch either."""
    pid = _signed_close(conn)
    R.recover_unverified_closes(conn, {"solana": {"confirm_once_fn": lambda _s: state}}, NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM, f"{state} is not proof — the claim must stay held"
    assert pos["close_sig"] == SIG


def test_a_CONFIRMED_close_is_never_released_by_this_path(conn):
    """status 0x1 means the tokens DID move. Releasing that re-arms a sell on an empty bag."""
    pid = _signed_close(conn)
    R.recover_unverified_closes(conn, {"solana": {"confirm_once_fn": lambda _s: "confirmed"}}, NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_sig"] is not None or pos["closed_at"] is not None, \
        "a confirmed close is settled or held — never released as if it had reverted"


# ── the EVM dropped close: the Solana proof cannot run there at all ──────────────────────────
#
# `sig_provably_dead` needs `last_valid_height`, and an EVM intent records it as null — a blockhash
# expiry is a Solana concept. The Solana branch also needs `block_height_fn`, which the EVM context
# never provides, so its guard was permanently False on 4663 and the auto-release could not fire.
#
# A human has now freed a funded bag by hand TWICE: 37331 (RIDEABULL, +48%, 2026-08-14) and 48554
# (RIZZLER, 2026-09-02) whose claim was pinned on a sell dropped from the mempool entirely.
#
# An EVM tx stays valid until its NONCE is consumed, so the mined nonce is the equivalent fact.

EVM_NONCE = 1253


def _evm_close(conn, *, nonce=EVM_NONCE, snapshot=SNAPSHOT):
    pid = _signed_close(conn)
    intent = json.loads(conn.execute(
        "SELECT exit_intent_json FROM memecoin_positions WHERE id=?", (pid,)).fetchone()[0])
    intent.update({"last_valid_height": None, "nonce": nonce, "balance_snapshot": snapshot})
    conn.execute("UPDATE memecoin_positions SET exit_intent_json=? WHERE id=?",
                 (json.dumps(intent), pid))
    conn.commit()
    return pid


def _evm_ctx(*, mined_nonce, balance):
    return {"solana": {"confirm_once_fn": lambda _s: "not_found",
                       "confirmed_nonce_fn": lambda: mined_nonce,
                       "token_checked_fn": lambda _m: balance}}


def test_a_dropped_evm_close_releases_when_the_nonce_moved_on_and_the_bag_is_whole(conn):
    pid = _evm_close(conn)
    R.recover_unverified_closes(conn, _evm_ctx(mined_nonce=EVM_NONCE + 1, balance=SNAPSHOT), NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] is None, "the nonce is spent — this tx can never mine"
    assert pos["is_open"] == 1 and pos["closed_at"] is None


@pytest.mark.parametrize("mined", [EVM_NONCE - 1, EVM_NONCE])
def test_an_UNSPENT_nonce_is_held_because_the_sell_may_still_mine(conn, mined):
    """The whole point of the block. An EVM tx has no expiry — it waits for its nonce, so a nonce
    that is not yet spent means the sell can still land and a release would double-sell."""
    pid = _evm_close(conn)
    R.recover_unverified_closes(conn, _evm_ctx(mined_nonce=mined, balance=SNAPSHOT), NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM, f"mined nonce {mined} does not spend {EVM_NONCE}"


def test_a_REDUCED_bag_is_never_released_even_with_a_spent_nonce(conn):
    """A landed partial reduced the bag. Releasing here re-arms a sell that double-sells."""
    pid = _evm_close(conn)
    R.recover_unverified_closes(
        conn, _evm_ctx(mined_nonce=EVM_NONCE + 5, balance=SNAPSHOT * 0.5), NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM, "a partial sale must keep the claim held"


def test_an_unreadable_balance_holds_rather_than_releases(conn):
    def boom(_m):
        raise RuntimeError("EVM RPC provider HTTP failure")
    pid = _evm_close(conn)
    R.recover_unverified_closes(
        conn, {"solana": {"confirm_once_fn": lambda _s: "not_found",
                          "confirmed_nonce_fn": lambda: EVM_NONCE + 1,
                          "token_checked_fn": boom}}, NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM, "fail closed: an unreadable bag proves nothing"


def test_floating_point_noise_does_not_block_a_whole_bag(conn):
    pid = _evm_close(conn)
    R.recover_unverified_closes(
        conn, _evm_ctx(mined_nonce=EVM_NONCE + 1, balance=SNAPSHOT * 0.9999999), NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] is None, "noise is not a partial sale"


def test_a_missing_nonce_on_the_intent_holds(conn):
    """Fail closed on an intent that cannot support the proof."""
    pid = _signed_close(conn)          # intent has last_valid_height but no nonce
    conn.execute("UPDATE memecoin_positions SET exit_intent_json=? WHERE id=?",
                 (json.dumps({"balance_snapshot": SNAPSHOT}), pid))
    conn.commit()
    R.recover_unverified_closes(conn, _evm_ctx(mined_nonce=99999, balance=SNAPSHOT), NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM


def test_a_nonce_read_that_returns_None_holds_and_does_not_raise(conn):
    """`eth_nonce` returns None on any RPC failure — it does not raise. Without the isinstance
    guard `None <= 1253` is a TypeError that escapes into the recovery sweep, so this is both a
    fail-closed test and a crash test. Found by mutation: removing the guard broke nothing."""
    pid = _evm_close(conn)
    R.recover_unverified_closes(
        conn, {"solana": {"confirm_once_fn": lambda _s: "not_found",
                          "confirmed_nonce_fn": lambda: None,
                          "token_checked_fn": lambda _m: SNAPSHOT}}, NOW)
    pos = dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())
    assert pos["close_claim_id"] == CLAIM, "an unreadable nonce proves nothing — hold"


@pytest.mark.parametrize('raw', [None, '', '{bad', '[]', 'null'])
def test_missing_or_malformed_intent_cannot_abort_recovery_or_release_a_signature(conn, raw):
    first = _signed_close(conn, sig='FIRST', claim='FIRST-CLAIM', snapshot=10.0)
    conn.execute('UPDATE memecoin_positions SET exit_intent_json=? WHERE id=?', (raw, first))
    second = _signed_close(conn, sig='SECOND', claim='SECOND-CLAIM', snapshot=10.0, mint='MintSecond')
    ctx = {'solana': {'confirm_once_fn': lambda sig: 'confirmed', 'receipt_proceeds_fn': lambda sig: 15.0}}
    R.recover_unverified_closes(conn, ctx, NOW)
    assert m.get_position(conn, first)['close_sig'] == 'FIRST'
    assert m.get_position(conn, first)['is_open'] == 1
    assert m.get_position(conn, second)['is_open'] == 0


def test_recovery_does_not_reuse_previous_positions_quote_asset_price(conn):
    _signed_close(conn, sig='FIRST', claim='FIRST-CLAIM', snapshot=10.0, quote_asset_usd=3000)
    second = _signed_close(conn, sig='SECOND', claim='SECOND-CLAIM', snapshot=10.0, mint='MintSecond')
    conn.execute('UPDATE memecoin_positions SET exit_intent_json=NULL WHERE id=?', (second,))
    calls=[]
    R.recover_unverified_closes(conn, {'solana': {'confirm_once_fn': lambda sig:'confirmed',
        'receipt_proceeds_fn': lambda sig, **kw:calls.append((sig,kw)) or 15.0}}, NOW)
    assert ('SECOND', {'quote_asset_usd':3000}) not in calls
    assert m.get_position(conn, second)['is_open'] == 1


def test_recovered_close_does_not_attribute_slippage_to_a_prior_mark(conn):
    pid = _signed_close(conn, snapshot=10.0)
    conn.execute('UPDATE memecoin_positions SET last_executable_price=2.0 WHERE id=?', (pid,))
    conn.commit()
    ctx = {'solana': {'confirm_once_fn': lambda sig: 'confirmed',
                      'receipt_proceeds_fn': lambda sig: 15.0}}
    R.recover_unverified_closes(conn, ctx, NOW)
    fill = conn.execute('SELECT price, quote_price, execution_slippage_pct FROM memecoin_fills '
                        'WHERE position_id=? AND tx_sig=?', (pid, SIG)).fetchone()
    assert fill['price'] == pytest.approx(1.5)
    assert fill['quote_price'] is None
    assert fill['execution_slippage_pct'] is None
