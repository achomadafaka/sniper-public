"""A signed EVM holding sell that never mines (Gamma HIGH on PR #407).

Before: no receipt meant 'submitted' forever, and every funded entry on the chain stayed fenced
(admission, executor, the 4663 fast lane). Now: our nonce spent by another transaction, held for
the confirm window with still no receipt of our hash, fails it; our nonce unspent with nothing of
ours in the node for N minutes pages the operator once for a cancel; an unreadable nonce pages and
never fails. Nothing is ever rebroadcast."""
import base64

import pytest
from eth_account import Account
from eth_utils import to_checksum_address

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_holdings as eh
from sniper.settings import memecoin as cfg
from test_memecoin_evm_holdings import ADDR, TOKEN, _conn

T0 = "2026-10-08T09:00:00+00:00"
KEY = "0x" + "11" * 32


def _payload(nonce):
    """What sell_once persists: base64 of the signed `0x…` hex string."""
    signed = Account.sign_transaction({
        "chainId": 4663, "nonce": nonce, "to": to_checksum_address(TOKEN), "value": 0, "gas": 100_000,
        "maxFeePerGas": 2 * 10 ** 9, "maxPriorityFeePerGas": 10 ** 8, "data": "0x"}, KEY)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    return base64.b64encode(("0x" + raw.hex().removeprefix("0x")).encode("ascii")).decode("ascii")


PAGES = []


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from sniper.services import memecoin_alerts
    PAGES.clear()
    monkeypatch.setattr(memecoin_alerts, "send_alert",
                        lambda kind, text, **k: PAGES.append(text) if kind == "HOLDING-SELL-STUCK" else None)
    eh._STUCK.clear()
    eh._NONCE_VIEW.clear()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_HOLDING_STUCK_PAGE_MIN", 10.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_HOLDING_NONCE_SPENT_CONFIRM_S", 60.0)
    yield
    eh._STUCK.clear()
    eh._NONCE_VIEW.clear()


class _Chain:
    def __init__(self, *, latest, pending, receipt=None):
        self.latest, self.pending, self.receipt = latest, pending, receipt
        self.pages, self.sends, self.nonce_reads = PAGES, [], 0

    def ctx(self):
        def latest():
            self.nonce_reads += 1
            return self.latest
        return {"chain": "4663", "address": ADDR, "confirmed_nonce_fn": latest,
                "pending_nonce_fn": lambda: self.pending,
                "lifi_local": {"receipt_fn": lambda h: self.receipt,
                               "send_fn": lambda raw: self.sends.append(raw)}}


def _row(c, *, nonce=5, payload=None, phase="swap", mint=TOKEN):
    iid = m.enqueue_holding_sell(c, "4663", mint, now_iso=T0)
    m.resolve_holding_sell(c, iid, "approval_submitted" if phase == "approval" else "submitted",
                           now_iso=T0, sig="0xours", balance_raw=123, transaction_phase=phase,
                           signed_payload_b64=payload if payload is not None else _payload(nonce))
    return iid


def _pass(c, chain, t):
    return eh._resolve_locked(c, ctx=chain.ctx(), now_iso=T0, changed=[False], clock=lambda: t)


def _state(c, iid):
    return tuple(c.execute("SELECT state, reason FROM memecoin_holding_sell_intents WHERE intent_id=?",
                           (iid,)).fetchone())


@pytest.mark.parametrize("phase", ["swap", "approval"])
def test_a_nonce_spent_by_another_tx_fails_after_the_confirm_window_and_reopens_the_chain(phase):
    c = _conn()
    iid = _row(c, nonce=5, phase=phase)
    chain = _Chain(latest=9, pending=9)
    assert m.unresolved_holding_sell_count(c, "4663") == 1
    assert m.unresolved_signed_pending_count(c, "4663") == 1          # the funded entry fence
    assert _pass(c, chain, 0.0) == 0                                   # first sight: wait
    assert _pass(c, chain, 59.0) == 0
    assert _pass(c, chain, 61.0) == 1
    state, reason = _state(c, iid)
    assert state == "failed" and "nonce 5 advanced" in reason and "no receipt for our hash" in reason
    assert phase in reason
    # A RESOLVED ROW ADMITS ENTRIES ON THAT CHAIN AGAIN.
    assert m.unresolved_holding_sell_count(c, "4663") == 0
    assert m.unresolved_signed_pending_count(c, "4663") == 0
    assert chain.sends == [] and chain.pages == []                     # never rebroadcast


def test_a_late_receipt_of_our_own_hash_is_not_another_tx():
    c = _conn()
    iid = _row(c, nonce=5)
    chain = _Chain(latest=9, pending=9)
    _pass(c, chain, 0.0)
    calls = {"n": 0}

    def lagging(_h):                     # the index catches up exactly at the settle re-check
        calls["n"] += 1
        return {"status": "0x1"} if calls["n"] > 1 else None
    ctx = chain.ctx()
    ctx["lifi_local"]["receipt_fn"] = lagging
    assert eh._resolve_locked(c, ctx=ctx, now_iso=T0, changed=[False], clock=lambda: 61.0) == 0
    assert _state(c, iid)[0] == "submitted"
    chain.receipt = {"status": "0x1"}
    assert _pass(c, chain, 62.0) == 1 and _state(c, iid)[0] == "sold"


def test_our_own_receipt_settles_on_todays_path_whatever_the_nonce():
    c = _conn()
    iid = _row(c, nonce=5)
    chain = _Chain(latest=9, pending=9, receipt={"status": "0x1"})
    assert _pass(c, chain, 0.0) == 1
    assert _state(c, iid)[0] == "sold" and chain.nonce_reads == 0


def test_a_reorg_that_unspends_the_nonce_restarts_the_window():
    c = _conn()
    iid = _row(c, nonce=5)
    chain = _Chain(latest=9, pending=9)
    _pass(c, chain, 0.0)
    eh._NONCE_VIEW.clear()
    chain.latest = chain.pending = 5                      # the other tx reorged out
    _pass(c, chain, 30.0)
    eh._NONCE_VIEW.clear()
    chain.latest = chain.pending = 9
    assert _pass(c, chain, 61.0) == 0                     # a fresh window, not the first sight's
    eh._NONCE_VIEW.clear()
    assert _pass(c, chain, 122.0) == 1 and _state(c, iid)[0] == "failed"


def test_a_dropped_sell_pages_once_after_n_minutes_and_is_never_failed_or_rebroadcast():
    c = _conn()
    iid = _row(c, nonce=5)
    chain = _Chain(latest=5, pending=5)                   # unspent, nothing of ours in the node
    for t in (0.0, 300.0, 599.0):
        eh._NONCE_VIEW.clear()
        assert _pass(c, chain, t) == 0
    assert chain.pages == []
    for t in (600.0, 900.0):
        eh._NONCE_VIEW.clear()
        assert _pass(c, chain, t) == 0
    assert len(chain.pages) == 1
    page = chain.pages[0]
    assert "chain 4663" in page and "nonce 5" in page and "0xours" in page and "cancel" in page
    assert _state(c, iid)[0] == "submitted" and chain.sends == []


def test_a_sell_still_in_a_mempool_pages_only_after_the_longer_window():
    """Gamma LOW: an underpriced sell can sit in a mempool (pending > confirmed) for ever."""
    c = _conn()
    iid = _row(c, nonce=5)
    chain = _Chain(latest=5, pending=6)                   # something of ours is pending
    for t in (0.0, 700.0, 1799.0):
        eh._NONCE_VIEW.clear()
        _pass(c, chain, t)
    assert chain.pages == []                              # the 10-min dropped page never fires here
    for t in (1800.0, 2400.0):
        eh._NONCE_VIEW.clear()
        _pass(c, chain, t)
    assert len(chain.pages) == 1
    page = chain.pages[0]
    assert "still pending in a mempool" in page and "nonce 5" in page and "higher fee" in page
    assert _state(c, iid)[0] == "submitted" and chain.sends == []


@pytest.mark.parametrize("payload", ["bm90LWEtdHg=", ""])
def test_an_unreadable_nonce_pages_after_n_minutes_and_never_fails(payload):
    c = _conn()
    iid = _row(c, payload=payload)
    if payload == "":
        c.execute("UPDATE memecoin_holding_sell_intents SET signed_payload_b64=NULL WHERE intent_id=?",
                  (iid,))
        c.commit()
    chain = _Chain(latest=9, pending=9)
    for t in (0.0, 599.0, 600.0, 1200.0, 5000.0):
        assert _pass(c, chain, t) == 0
    assert len(chain.pages) == 1 and "no readable nonce" in chain.pages[0]
    assert _state(c, iid)[0] == "submitted" and chain.nonce_reads == 0


def test_an_unreadable_nonce_view_never_fails_and_pages_once_after_n_minutes():
    """R-02 (2026-10-08 review): this used to pin silence; the docstring promises one page."""
    c = _conn()
    iid = _row(c, nonce=5)
    chain = _Chain(latest=None, pending=9)
    for t in (0.0, 100.0, 1000.0, 2000.0):
        assert _pass(c, chain, t) == 0
    assert _state(c, iid)[0] == "submitted" and len(chain.pages) == 1


def test_the_nonce_is_read_at_most_once_per_check_period_per_chain():
    c = _conn()
    _row(c, nonce=5)
    _row(c, nonce=6, mint="0x" + "ee" * 20)                # two unmined rows on one chain
    chain = _Chain(latest=5, pending=5)
    for t in (0.0, 1.0, 5.0, 14.0):
        _pass(c, chain, t)
    assert chain.nonce_reads == 1
    _pass(c, chain, 15.0)
    assert chain.nonce_reads == 2


def test_the_payload_nonce_is_what_sell_once_signs():
    assert eh._payload_nonce(_payload(17)) == 17
    assert eh._payload_nonce(None) is None and eh._payload_nonce("@@") is None


def test_leaving_the_mempool_restarts_the_mempool_window():
    c = _conn()
    _row(c, nonce=5)
    chain = _Chain(latest=5, pending=6)
    _pass(c, chain, 0.0)
    eh._NONCE_VIEW.clear()
    chain.pending = 5                                     # dropped from the mempool
    _pass(c, chain, 1000.0)
    eh._NONCE_VIEW.clear()
    chain.pending = 6                                     # seen pending again
    _pass(c, chain, 1801.0)
    assert not any("mempool" in p for p in chain.pages)


def test_the_production_context_resolves_and_pages(monkeypatch, tmp_path):
    """The hand-made-context trap (brain 0e30ad57): build the PRODUCTION 4663 context, swap only the
    VALUES of its own nonce and receipt seams for fakes, and the page and the settle both run."""
    from test_memecoin_evm_context_keys import _production_context
    ctx = _production_context(monkeypatch, tmp_path, "4663")
    for key in ("confirm_once_fn", "confirmed_nonce_fn", "pending_nonce_fn"):
        assert key in ctx, key                           # the production context carries the seam
    seen = {"latest": 5, "pending": 5}
    # A clean "no receipt" (not_found): an unreadable one ('unknown') keeps the row and its fence
    # (review EVM01; tests/test_memecoin_evm_holding_receipt_unknown.py).
    ctx.update(confirm_once_fn=lambda h: "not_found", confirmed_nonce_fn=lambda: seen["latest"],
               pending_nonce_fn=lambda: seen["pending"])
    c = _conn()
    iid = _row(c, nonce=5)
    for t in (0.0, 600.0):
        eh._NONCE_VIEW.clear()
        eh._resolve_locked(c, ctx=ctx, now_iso=T0, changed=[False], clock=lambda: t)
    assert len(PAGES) == 1 and "nonce 5" in PAGES[0]
    seen.update(latest=9, pending=9)
    for t in (700.0, 761.0):
        eh._NONCE_VIEW.clear()
        eh._resolve_locked(c, ctx=ctx, now_iso=T0, changed=[False], clock=lambda: t)
    assert _state(c, iid)[0] == "failed" and m.unresolved_signed_pending_count(c, "4663") == 0
