"""An unreadable receipt is an unknown outcome, not a missing one (2026-10-08 review EVM01).

The production one-shot receipt read answers 'confirmed' | 'failed' | 'not_found' | 'unknown'.
`_receipt_reader` used to fold 'unknown' (a timeout, a refusal, a failed canonical check) into
None, and `_unmined` then failed the sell 60 s after the nonce moved, claiming another
transaction spent it, and released the chain's fence. A moved nonce proves SOME transaction at
that nonce mined; ours is one candidate. So an unknown read keeps the row and its fence, and
only a clean 'not_found' enters the missing-receipt path.
"""
import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_holdings as eh
from test_memecoin_evm_holding_unmined import PAGES, T0, _Chain, _fresh, _pass, _payload, _row, _state  # noqa: F401
from test_memecoin_evm_holdings import _conn


@pytest.fixture
def prod(monkeypatch, tmp_path):
    """The PRODUCTION 4663 context with its real `_confirm_once` (eth_receipt_state). Only the
    transport and the nonce reads are fakes."""
    from sniper.execution import evm_real_seams as seams
    from test_memecoin_evm_context_keys import _production_context
    ctx = _production_context(monkeypatch, tmp_path, "4663")
    world = {"latest": 6, "pending": 6, "receipt": "timeout"}

    def rpc(_url, method, params, **_k):
        assert method == "eth_getTransactionReceipt", method
        answer = world["receipt"]
        if answer == "timeout":
            raise TimeoutError("read timed out")
        return {"result": answer}
    monkeypatch.setattr(seams, "_rpc", rpc)
    ctx.update(confirmed_nonce_fn=lambda: world["latest"], pending_nonce_fn=lambda: world["pending"])
    return ctx, world


def _passes(c, ctx, times):
    for t in times:
        eh._NONCE_VIEW.clear()
        eh._resolve_locked(c, ctx=ctx, now_iso=T0, changed=[False], clock=lambda t=t: t)


def test_an_unreadable_receipt_after_the_nonce_moved_keeps_the_sell_and_its_fence(prod):
    ctx, _world = prod
    c = _conn()
    iid = _row(c, nonce=5)
    _passes(c, ctx, (0.0, 61.0, 3600.0))
    state, reason = _state(c, iid)
    assert state == "submitted", (state, reason)
    assert "never mined" not in str(reason or "")
    assert m.unresolved_signed_pending_count(c, "4663") == 1


def test_an_unknown_then_reverted_receipt_settles_failed_on_the_revert(prod):
    ctx, world = prod
    c = _conn()
    iid = _row(c, nonce=5)
    _passes(c, ctx, (0.0, 61.0))
    world["receipt"] = {"status": "0x0", "blockNumber": "0x10"}
    _passes(c, ctx, (62.0,))
    assert _state(c, iid) == ("failed", "the swap reverted on chain")


def test_an_unknown_then_mined_receipt_settles_sold(prod):
    ctx, world = prod
    c = _conn()
    iid = _row(c, nonce=5)
    _passes(c, ctx, (0.0, 61.0))
    world["receipt"] = {"status": "0x1", "blockNumber": "0x10"}
    _passes(c, ctx, (62.0,))
    assert _state(c, iid)[0] == "sold"


def test_an_unknown_receipt_with_an_unmoved_nonce_never_says_the_tx_was_dropped(prod):
    """pending == confirmed with an unreadable receipt: the stuck page would tell the operator to
    cancel a transaction whose outcome we could not read. It must name the unreadable receipt."""
    ctx, world = prod
    world.update(latest=5, pending=5)
    c = _conn()
    iid = _row(c, nonce=5)
    _passes(c, ctx, (0.0, 700.0, 1400.0))
    assert _state(c, iid)[0] == "submitted"
    assert not any("cancel it" in p for p in PAGES), PAGES
    assert len(PAGES) == 1 and "unreadable" in PAGES[0] and "NOT failed" in PAGES[0], PAGES


def test_an_unknown_approval_is_never_turned_into_a_sell_request(prod):
    ctx, _world = prod
    c = _conn()
    iid = _row(c, nonce=5, phase="approval")
    _passes(c, ctx, (0.0, 61.0, 3600.0))
    assert _state(c, iid)[0] == "approval_submitted"
    assert m.unresolved_signed_pending_count(c, "4663") == 1


def test_a_clean_absence_after_the_nonce_moved_still_settles_without_naming_another_tx(prod):
    """The 5ae0aaf55 repair stays: a node that cleanly answers 'no receipt' for our hash while the
    nonce sits past ours for the confirm window settles the sell, so a dead sell never fences the
    chain forever. A moved nonce does not identify which transaction used it, so the reason says
    what was seen: no receipt for our hash, not 'spent by another tx'."""
    ctx, world = prod
    world["receipt"] = None
    c = _conn()
    iid = _row(c, nonce=5)
    _passes(c, ctx, (0.0, 61.0))
    state, reason = _state(c, iid)
    assert state == "failed" and "another tx" not in reason and "no receipt for our hash" in reason
    assert m.unresolved_signed_pending_count(c, "4663") == 0


# --- R-02: an unreadable nonce view pages once and never releases -----------------------------------

@pytest.mark.parametrize("broken", ["none", "raises"])
def test_an_unreadable_nonce_view_pages_once_and_never_releases(broken):
    """The _unmined docstring promised a page for an unreadable nonce; the RPC-view path returned
    silently, so a holding sell could fence the chain's funded entries with nobody told."""
    c = _conn()
    iid = _row(c, nonce=5)
    chain = _Chain(latest=None, pending=9)
    ctx = chain.ctx()
    if broken == "raises":
        def boom():
            raise TimeoutError("nonce read")
        ctx["confirmed_nonce_fn"] = boom
    for t in (0.0, 100.0, 700.0, 1400.0):
        import sniper.services.memecoin_evm_holdings as holdings
        holdings._NONCE_VIEW.clear()
        assert holdings._resolve_locked(c, ctx=ctx, now_iso=T0, changed=[False], clock=lambda t=t: t) == 0
    assert len(PAGES) == 1, PAGES
    assert "nonce" in PAGES[0] and "unreadable" in PAGES[0] and "NOT failed" in PAGES[0]
    assert _state(c, iid)[0] == "submitted" and m.unresolved_signed_pending_count(c, "4663") == 1
