"""Relay option A: the shared single-use store of signed Solana deposits (spec 2026-10-09 relay deposit
proof on the on-chain lane, sections 3.1, 3.2 and 4).

The deposit proof is option C's frame proof (the recorded DopamineFeenFr1 deposit of the option C tests);
the store adds what crosses processes: one claim per fill tx, the registry re-checked at take time, and
the shared-depositor rule keyed on source_user_id (version b: shared at 3 users, a page at 2)."""
import threading

import pytest

import test_memecoin_rh_deposit_proof as C
from sniper.services import memecoin_relay_deposit_store as st

UID = C.UID
FILL = "0x" + "ab" * 32
OTHER_FILL = "0x" + "cd" * 32
USD = 600.0 * 0.995                     # inside 0.98..1.00 of the 600 USDC deposit


def _store(tmp_path, **kw):
    return st.DepositStore(str(tmp_path / "deposits.db"), clock=lambda: C.ARRIVED + 1.0, **kw)


def _take(store, *, fill=FILL, uid=UID, usd=USD, now=None, registry=lambda dep, uid: True, order=None):
    return store.take(order_id=order or C.ORDER_ID, fill_tx=fill, source_user_id=uid, fill_usd=usd,
                      now=C.ARRIVED + 1.0 if now is None else now, registry_ok=registry)


def test_a_stored_deposit_proves_its_fill_once(tmp_path):
    store = _store(tmp_path)
    store.put(C._proof())
    proof, status = _take(store)
    assert status == "matched"
    assert (proof["order_id"], proof["origin_wallet"], proof["origin_identity_user_id"]) == (
        C.ORDER_ID, C.DEPOSITOR, UID)
    assert proof["origin_tx"] == C.ORIGIN["transaction"]["signatures"][0]
    assert proof["origin_amount_atomic"] == "600000000" and proof["origin_slot"] == C.ORIGIN["slot"]
    # The same fill read again (another lane, a restart) keeps its proof; a second fill never does.
    assert _take(store)[1] == "matched"
    assert _take(store, fill=OTHER_FILL) == (None, "order_reused")


def test_a_second_process_sees_the_claim(tmp_path):
    a, b = _store(tmp_path), _store(tmp_path)
    a.put(C._proof())
    assert _take(a)[1] == "matched"
    assert _take(b, fill=OTHER_FILL) == (None, "order_reused")


def test_two_claims_race_to_one_winner(tmp_path):
    store = _store(tmp_path)
    store.put(C._proof())
    results, barrier = [], threading.Barrier(8)

    def claim(i):
        barrier.wait()
        results.append(_take(st.DepositStore(store.path, clock=store.clock), fill="0x%064x" % (i + 1))[1])
    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["matched"] + ["order_reused"] * 7


@pytest.mark.parametrize("change, status", [
    (dict(now=C.ARRIVED + st.MAX_FILL_LAG_S + 1), "stale"),
    (dict(now=C.ARRIVED - 1), "stale"),
    (dict(uid="someone-else"), "uid_mismatch"),
    (dict(usd=600.0 * 1.01), "amount_band"),
    (dict(usd=600.0 * 0.97), "amount_band"),
    (dict(usd=None), "amount_band"),
    (dict(order="0x" + "00" * 32), "no_deposit"),
])
def test_a_fill_that_does_not_bind_gets_no_proof_and_claims_nothing(tmp_path, change, status):
    store = _store(tmp_path)
    store.put(C._proof())
    assert _take(store, **change) == (None, status)
    assert _take(store)[1] == "matched"          # the refused look left the deposit unclaimed


def test_the_registry_is_read_at_take_time_not_trusted_from_the_frame(tmp_path):
    store = _store(tmp_path)
    store.put(C._proof())
    seen = []

    def revoked(depositor, uid):
        seen.append((depositor, uid))
        return False
    assert _take(store, registry=revoked) == (None, "registry_revoked")
    assert seen == [(C.DEPOSITOR, UID)]


def test_two_deposits_on_one_order_id_prove_nothing(tmp_path):
    store = _store(tmp_path)
    store.put(C._proof())
    store.put(C._proof())                         # the same deposit delivered again: still one
    assert _take(store)[1] == "matched"
    other = dict(C._proof(), signature="5" * 88)       # a second SIGNED deposit on the same order id
    (tmp_path / "two").mkdir()
    two = _store(tmp_path / "two")
    two.put(C._proof())
    two.put(other)
    assert _take(two) == (None, "duplicate_order")


def test_old_rows_are_pruned(tmp_path):
    store = _store(tmp_path)
    store.put(C._proof())
    store.prune(C.ARRIVED + st.STORE_TTL_S + 1)
    assert _take(store) == (None, "no_deposit")


# --- shared depositor, version (b): shared at 3 source users, a page at 2 ----------------------------

def test_a_depositor_funding_three_users_is_shared_even_when_registry_verified(tmp_path):
    """THE CHECK LEFT BEHIND (spec section 7): fails if the rule is removed or keyed on the registry."""
    store = _store(tmp_path)
    store.note_funder(C.DEPOSITOR, "user-b", C.ARRIVED - 50)
    store.note_funder(C.DEPOSITOR, "user-c", C.ARRIVED - 40)
    store.put(C._proof())
    assert _take(store) == (None, "shared_depositor")
    assert store.funder_count(C.DEPOSITOR) == 3


def test_at_two_users_the_registry_owner_keeps_its_proof_and_the_pair_pages(tmp_path):
    pages = []
    store = _store(tmp_path, page_fn=pages.append)
    store.note_funder(C.DEPOSITOR, "user-b", C.ARRIVED - 50)
    store.put(C._proof())
    assert _take(store)[1] == "matched"
    assert len(pages) == 1 and C.DEPOSITOR in pages[0] and "user-b" in pages[0] and UID in pages[0]
    assert _take(store)[1] == "matched" and len(pages) == 1          # one page per pair, not per take


def test_one_user_under_two_handles_is_one_funder(tmp_path):
    """d47559fc is NotARandomUser in the feed and randomuser123 in memecoin_wallets: the key is the uid."""
    store = _store(tmp_path)
    for _ in range(5):
        store.note_funder("2L17Sw85Lh2Ca7ejEzWqyJsZ3yS4DxQhYr52X5H5xRns", "d47559fc-02c3-5165-b94f-1316fdd19560",
                          C.ARRIVED)
    assert store.funder_count("2L17Sw85Lh2Ca7ejEzWqyJsZ3yS4DxQhYr52X5H5xRns") == 1


def test_a_matched_fill_records_its_funder_pair(tmp_path):
    store = _store(tmp_path)
    store.put(C._proof())
    assert store.funder_count(C.DEPOSITOR) == 0
    assert _take(store)[1] == "matched"
    assert store.funder_count(C.DEPOSITOR) == 1


def test_a_locked_store_is_no_proof_never_a_raise(tmp_path):
    import sqlite3
    store = _store(tmp_path)
    store.put(C._proof())
    hold = sqlite3.connect(store.path, timeout=0, isolation_level=None)
    hold.execute("BEGIN EXCLUSIVE")
    try:
        assert _take(st.DepositStore(store.path, clock=store.clock, busy_ms=10)) == (None, "store_error")
    finally:
        hold.execute("ROLLBACK")
        hold.close()
