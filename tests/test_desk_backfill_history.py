"""Helius G1 creator history + G2 coin age (spec 2026-10-27-helius-history-backfill-design.md; Alpha order 2026-10-28). Offline: fake transport, no provider."""
import hashlib
import json
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_desk_backfill_history as H
from sniper.services import memecoin_desk_backfill_ledger as L

NOW = datetime(2026, 10, 28, 3, tzinfo=timezone.utc).timestamp()
DAY = "2026-10-28"
DISC = hashlib.sha256(b"global:create").digest()[:8]
COSTS = {H.METHOD_FULL: 10}


def cycle(tmp_path, **k):
    c = dict(attested_at="2026-10-27T12:00:00Z", attested_by="op", epoch_id="e1", epoch_start="2026-10-27T00:00:00Z", epoch_end="2026-11-28T00:00:00Z", plan_credits=10_000_000,
             used_at_activation=0, month_cap=5_000_000, funded_reserve=2_000_000)
    c.update(k)
    p = tmp_path / "cycle.json"
    p.write_text(json.dumps(c))
    return L.load_cycle(p, now=NOW)


def b58(raw):
    import base58
    return base58.b58encode(raw).decode()


CR, OTHER, PAYER = b58(bytes([1]) * 32), b58(bytes([2]) * 32), b58(bytes([3]) * 32)


def ix_data(creator, disc=DISC):
    import base58
    s = lambda t: len(t).to_bytes(4, "little") + t
    return b58(disc + s(b"name") + s(b"SYM") + s(b"https://u") + base58.b58decode(creator) + b"\x00")


def create_tx(sig, mint, creator, slot, bt, *, err=None, disc=DISC, accounts=None, payer=None):
    accs = accounts or [mint, "auth", "curve", "acurve", "glob", "mpl", "meta", payer or creator]
    return {"slot": slot, "blockTime": bt, "transactionIndex": 3,
            "meta": {"err": err, "innerInstructions": []},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": payer or creator, "signer": True}, {"pubkey": mint, "signer": True}],
                                                              "instructions": [{"programId": H.PUMP, "accounts": accs, "data": ix_data(creator, disc)}]}}}


def other_tx(sig, creator, slot, bt):
    return {"slot": slot, "blockTime": bt, "transactionIndex": 1, "meta": {"err": None, "innerInstructions": []},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": creator, "signer": True}], "instructions": [
                {"programId": "11111111111111111111111111111111", "accounts": [creator], "data": b58(b"\x02\x00\x00\x00")}]}}}


class Fake:
    """script: list of (status, body) | Exception, consumed first; else pages[address] list of result dicts served in order."""

    def __init__(self, pages=None, script=None):
        self.pages, self.script, self.calls, self.pos = pages or {}, list(script or []), [], {}

    def transport(self, method, params):
        self.calls.append((method, params["address"], dict(params["config"])))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        a = params["address"]
        i = self.pos.get(a, 0)
        self.pos[a] = i + 1
        pages = self.pages.get(a, [])
        return 200, json.dumps({"jsonrpc": "2.0", "result": pages[i] if i < len(pages) else {"data": [], "paginationToken": None}}).encode()


def coin_page(mint, slot, bt, sig="sig1", **k):
    return {"data": [create_tx(sig, mint, CR, slot, bt, **k)], "paginationToken": None}


def build(tmp_path, fake, *, cap=None, costs=COSTS, page_cap=None):
    c = cycle(tmp_path)
    if cap is not None:
        c["month_cap"] = cap
    ledger = L.Ledger(tmp_path / "credits.db", c, clock=lambda: NOW, costs=costs)
    con = H.connect(tmp_path / "history.db")
    return H.Fetcher(con, ledger, fake.transport, clock=lambda: NOW, creator_page_cap=page_cap or 50), con, ledger


# ---- G2 coin age
def test_coin_creation_binds_the_create_instruction_in_the_first_tx_and_stores_the_witness(tmp_path):
    fake = Fake({"M1": [coin_page("M1", 555, 1_793_100_000)]})
    f, con, ledger = build(tmp_path, fake)
    assert f.fetch_coin("M1") == "known"
    row = con.execute("select state, signature, slot, block_time, witness, creator, fetched_at from coin_creation where mint='M1'").fetchone()
    assert row == ("known", "sig1", 555, 1_793_100_000, "create_instruction_in_first_tx", CR, NOW)
    m, addr, cfg = fake.calls[0]
    assert (m, addr) == (H.METHOD, "M1") and cfg["sortOrder"] == "asc" and cfg["limit"] == 1 and cfg["transactionDetails"] == "full" and cfg["filters"]["status"] == "succeeded"
    assert ledger.total() == 10


def test_an_empty_or_unusable_first_tx_is_unknown_never_zero(tmp_path):
    other = {"data": [other_tx("o1", CR, 5, 1_793_100_000)], "paginationToken": None}
    wrong_mint = {"data": [create_tx("w1", "M_OTHER", CR, 5, 1_793_100_000)], "paginationToken": None}
    bad = {"data": [{"slot": 5, "blockTime": 5, "meta": None, "transaction": None}], "paginationToken": None}
    for page, reason in (({"data": [], "paginationToken": None}, "no_history"), (coin_page("M1", 5, None), "block_time_unusable"), (coin_page("M1", 5, 0), "block_time_unusable"),
                         (coin_page("M1", None, 1_793_100_000), "slot_unusable"), (other, "first_tx_not_creation"), (wrong_mint, "first_tx_not_creation"),
                         (bad, "unreadable_tx"), (coin_page("M1", 5, 1_793_100_000, err={"InstructionError": [0, "x"]}), "first_tx_not_creation")):
        fake = Fake({"M1": [page]})
        f, con, ledger = build(tmp_path, fake)
        con.execute("delete from coin_creation")
        ledger.con.execute("delete from ledger")
        f.fetch_coin("M1")
        assert con.execute("select state, reason, slot, block_time from coin_creation where mint='M1'").fetchone() == ("unknown", reason, None, None)


def test_a_done_coin_is_never_fetched_or_charged_again(tmp_path):
    fake = Fake({"M1": [coin_page("M1", 555, 1_793_100_000)]})
    f, con, ledger = build(tmp_path, fake)
    f.fetch_coin("M1")
    f.fetch_coin("M1")
    assert len(fake.calls) == 1 and ledger.total() == 10


def test_an_unknown_coin_is_retried_next_run_but_a_typed_unknown_after_no_history_is_not_a_zero(tmp_path):
    fake = Fake(script=[TimeoutError("x")])
    f, con, ledger = build(tmp_path, fake)
    assert f.fetch_coin("M1") == "transport_error"
    assert ledger.counts() == {"ambiguous": 1} and ledger.total() == 10                         # a lost response is charged in full
    assert con.execute("select state, reason from coin_creation where mint='M1'").fetchone() == ("unknown", "transport_error")
    fake.pages["M1"] = [coin_page("M1", 9, 1_793_100_000)]
    assert f.fetch_coin("M1") == "known"                                                          # unknown is retried, known is final


def test_cap_boundary_refuses_the_crossing_reserve_and_records_a_typed_gap(tmp_path):
    fake = Fake({"M1": [coin_page("M1", 1, 1_793_100_000)], "M2": [coin_page("M2", 2, 1_793_100_000)]})
    f, con, ledger = build(tmp_path, fake, cap=10)
    assert f.fetch_coin("M1") == "known"                                                          # equals the cap: allowed
    assert f.fetch_coin("M2") == "credits_exhausted"                                              # would cross: refused, nothing sent
    assert len(fake.calls) == 1 and f.stopped == "credits_exhausted"
    assert con.execute("select state, reason from coin_creation where mint='M2'").fetchone() == ("unknown", "credits_exhausted")


def test_quota_answer_stops_everything(tmp_path):
    body = json.dumps({"jsonrpc": "2.0", "error": {"code": -32429, "message": "max usage reached"}}).encode()
    fake = Fake(script=[(200, body)])
    f, con, ledger = build(tmp_path, fake)
    assert f.fetch_coin("M1") == "max_usage" and ledger.stopped()
    assert f.fetch_coin("M2") == "max_usage" and len(fake.calls) == 1


# ---- decoders
def test_decoder_counts_only_successful_creates_whose_creator_argument_is_this_wallet():
    page = {"data": [create_tx("a", "MINT_A", CR, 10, 1000), create_tx("b", "MINT_B", CR, 20, 2000, err={"InstructionError": [0, "x"]}),
                     create_tx("c", "MINT_C", OTHER, 30, 3000), create_tx("d", "MINT_D", CR, 40, 4000, disc=b"\x01" * 8),
                     create_tx("e", "MINT_E", CR, 50, 5000, payer=PAYER), other_tx("f", CR, 60, 6000)], "paginationToken": "60:1"}
    launches, token, bad = H.decode_creator_page(page, CR)
    assert [(l["mint"], l["signature"], l["slot"], l["block_time"]) for l in launches] == [("MINT_A", "a", 10, 1000), ("MINT_E", "e", 50, 5000)]    # E: another payer funded it, the creator ARGUMENT is ours
    assert token == "60:1" and bad == 0


def test_decoder_counts_a_malformed_tx_as_unreadable_not_as_no_launch():
    launches, token, bad = H.decode_creator_page({"data": [{"slot": 1, "blockTime": 1, "meta": None, "transaction": None}], "paginationToken": None}, CR)
    assert launches == [] and bad == 1


# ---- G1 creator scan
def full_page(txs, token=None):
    return {"data": txs, "paginationToken": token}


def test_a_complete_scan_gives_prior_launches_strictly_before_the_coin(tmp_path):
    pages = {CR: [full_page([create_tx("a", "M_A", CR, 10, 1000), create_tx("b", "M_B", CR, 20, 2000)], "20:1"), full_page([create_tx("c", "M_C", CR, 30, 3000)])]}
    fake = Fake(pages)
    f, con, ledger = build(tmp_path, fake)
    assert f.fetch_creator(CR) == "complete"
    assert [c[2]["transactionDetails"] for c in fake.calls] == ["full", "full"] and ledger.total() == 20
    got = H.prior_launches(con, CR, coin_slot=30, coin_block_time=3000)
    assert got == {"lb": 2, "complete": True, "pages": 2, "source": "create_ix_arg"}              # M_C itself and later ones excluded
    assert H.prior_launches(con, CR, coin_slot=10, coin_block_time=1000)["lb"] == 0              # 'none seen' with complete=True


def test_a_scan_over_the_page_cap_is_capped_with_a_lower_bound_and_complete_only_before_the_slot_it_reached(tmp_path):
    pages = {CR: [full_page([create_tx("a%d" % i, "M%d" % i, CR, 10 + i, 1000 + i)], "t%d" % i) for i in range(5)]}
    fake = Fake(pages)
    f, con, ledger = build(tmp_path, fake, page_cap=3)
    assert f.fetch_creator(CR) == "pagination_incomplete" and len(fake.calls) == 3
    assert con.execute("select state, reason, pages, launches, covered_slot from creator_scans where creator=?", (CR,)).fetchone() == ("capped", "pagination_incomplete", 3, 3, 12)
    beyond = H.prior_launches(con, CR, coin_slot=999, coin_block_time=500)
    assert beyond["lb"] == 3 and beyond["complete"] is False                                     # at least 3, 'none seen' would mean nothing here
    assert H.prior_launches(con, CR, coin_slot=12, coin_block_time=500) == {"lb": 2, "complete": True, "pages": 3, "source": "create_ix_arg"}   # the ascending scan already passed this coin


def test_a_mid_scan_cap_stop_or_timeout_leaves_unknown_with_no_partial_count(tmp_path):
    pages = {CR: [full_page([create_tx("a", "M_A", CR, 10, 1000)], "t1")]}
    fake = Fake(pages, script=None)
    f, con, ledger = build(tmp_path, fake, cap=10)
    assert f.fetch_creator(CR) == "credits_exhausted"
    assert H.prior_launches(con, CR, coin_slot=99, coin_block_time=9999) is None


def test_an_unreadable_tx_makes_the_scan_unknown(tmp_path):
    bad = {"slot": 1, "blockTime": 1, "meta": None, "transaction": None}
    f, con, ledger = build(tmp_path, Fake({CR: [full_page([create_tx("a", "M_A", CR, 10, 1000), bad])]}))
    assert f.fetch_creator(CR) == "unreadable_tx"
    assert H.prior_launches(con, CR, coin_slot=99, coin_block_time=9999) is None


def test_a_scan_fetched_before_the_coin_existed_cannot_count_its_priors(tmp_path):
    f, con, ledger = build(tmp_path, Fake({CR: [full_page([create_tx("a", "M_A", CR, 10, 1000)])]}))
    f.fetch_creator(CR)                                                                           # fetched_at = NOW
    assert H.prior_launches(con, CR, coin_slot=50, coin_block_time=int(NOW) - 5) is None           # coin newer than the scan minus the finality margin
    assert H.prior_launches(con, CR, coin_slot=50, coin_block_time=int(NOW) - 3600)["lb"] == 1


def test_live_use_requires_the_scan_to_predate_the_decision(tmp_path):
    f, con, ledger = build(tmp_path, Fake({CR: [full_page([create_tx("a", "M_A", CR, 10, 1000)])]}))
    f.fetch_creator(CR)
    assert H.prior_launches(con, CR, coin_slot=50, coin_block_time=2000, decision_at=NOW + 1)["lb"] == 1
    assert H.prior_launches(con, CR, coin_slot=50, coin_block_time=2000, decision_at=NOW - 1) is None   # fetched after the decision = read-side leakage


def test_rerun_of_a_complete_creator_spends_nothing_and_a_rescan_does_not_double_launches(tmp_path):
    f, con, ledger = build(tmp_path, Fake({CR: [full_page([create_tx("a", "M_A", CR, 10, 1000)])]}))
    f.fetch_creator(CR)
    f.fetch_creator(CR)
    assert ledger.total() == 10 and con.execute("select count(*) from creator_launches").fetchone()[0] == 1
    con.execute("update creator_scans set state='unknown', reason='x'")
    f2 = H.Fetcher(con, ledger, Fake({CR: [full_page([create_tx("a", "M_A", CR, 10, 1000)])]}).transport, clock=lambda: NOW)
    f2.fetch_creator(CR)
    assert con.execute("select count(*) from creator_launches").fetchone()[0] == 1               # insert-or-ignore on (creator, mint)


# ---- frozen targets
def test_targets_are_frozen_with_the_date_and_never_reset(tmp_path):
    f, con, ledger = build(tmp_path, Fake())
    H.freeze_targets(con, DAY, [("coin", "M1", None), ("creator", CR, "M1")], now=NOW)
    con.execute("update g12_targets set state='done'")
    H.freeze_targets(con, DAY, [("coin", "M1", None), ("coin", "M2", None)], now=NOW + 5)          # a re-write adds M2, never resets M1 or moves frozen_at
    rows = con.execute("select kind, key, state, frozen_at from g12_targets order by key").fetchall()
    assert rows == [("creator", CR, "done", NOW), ("coin", "M1", "done", NOW), ("coin", "M2", "pending", NOW + 5)]
    assert con.execute("select state from g12_targets where key='M1'").fetchone()[0] == "done"


def test_the_creator_is_the_instruction_argument_not_the_fee_payer(tmp_path):
    f, con, ledger = build(tmp_path, Fake({"M1": [{"data": [create_tx("s", "M1", OTHER, 5, 1_793_100_000, payer=PAYER)], "paginationToken": None}]}))
    assert f.fetch_coin("M1") == "known"
    assert con.execute("select creator, payer from coin_creation where mint='M1'").fetchone() == (OTHER, PAYER)


def test_a_creator_over_the_page_cap_is_not_rescanned_at_the_same_cap_but_is_at_a_larger_one(tmp_path):
    pages = {CR: [full_page([create_tx("a%d" % i, "M%d" % i, CR, 10 + i, 1000 + i)], "t%d" % i) for i in range(2)] + [full_page([])]}
    fake = Fake(pages)
    f, con, ledger = build(tmp_path, fake, page_cap=1)
    assert f.fetch_creator(CR) == "pagination_incomplete" and ledger.total() == 10
    assert f.fetch_creator(CR) == "pagination_incomplete" and ledger.total() == 10                  # no second charge
    f.page_cap = 5
    assert f.fetch_creator(CR) == "complete"


def test_completeness_via_covered_slot_is_only_claimed_for_an_ascending_scan(tmp_path):
    f, con, ledger = build(tmp_path, Fake({CR: [full_page([create_tx("a%d" % i, "M%d" % i, CR, 10 + i, 1000 + i)], "t%d" % i) for i in range(5)]}), page_cap=3)
    f.fetch_creator(CR)
    assert H.prior_launches(con, CR, coin_slot=12, coin_block_time=500)["complete"] is True
    con.execute("update creator_scan_log set sort_order='desc'")
    assert H.prior_launches(con, CR, coin_slot=12, coin_block_time=500)["complete"] is False


def test_a_later_rescan_neither_hides_an_earlier_scan_from_a_live_decision_nor_leaks_its_finds(tmp_path):
    clock = [NOW]
    c = cycle(tmp_path)
    ledger = L.Ledger(tmp_path / "credits.db", c, clock=lambda: NOW, costs=COSTS)
    con = H.connect(tmp_path / "history.db")
    f = H.Fetcher(con, ledger, Fake({CR: [full_page([create_tx("a", "M_A", CR, 10, 1000)])]}).transport, clock=lambda: clock[0])
    f.fetch_creator(CR)                                                                               # scan 1 at NOW sees M_A
    con.execute("update creator_scans set state='unknown', reason='x'")                                 # force a rescan
    clock[0] = NOW + 100
    f2 = H.Fetcher(con, ledger, Fake({CR: [full_page([create_tx("a", "M_A", CR, 10, 1000), create_tx("b", "M_B", CR, 20, 2000)])]}).transport, clock=lambda: clock[0])
    f2.fetch_creator(CR)                                                                              # scan 2 at NOW+100 also sees M_B
    live = H.prior_launches(con, CR, coin_slot=99, coin_block_time=500, decision_at=NOW + 50)
    assert live["lb"] == 1                                                                              # scan 1 only: M_B was not known at the decision
    assert H.prior_launches(con, CR, coin_slot=99, coin_block_time=500)["lb"] == 1                      # offline join also uses the earliest valid scan
    assert H.prior_launches_why(con, CR, coin_slot=99, coin_block_time=500, decision_at=NOW - 1) == "rescan_after_decision"


def test_none_has_a_named_reason(tmp_path):
    f, con, ledger = build(tmp_path, Fake())
    assert H.prior_launches_why(con, CR, coin_slot=1, coin_block_time=1) == "no_scan"
    f.fetch_creator(CR)                                                                                 # empty history page: complete scan of 0 txs
    assert H.prior_launches_why(con, CR, coin_slot=1, coin_block_time=int(NOW) - 5) == "scan_before_coin"
    assert H.prior_launches_why(con, CR, coin_slot=1, coin_block_time=1, decision_at=NOW - 1) == "rescan_after_decision"
    con.execute("update creator_scan_log set state='unknown'")
    assert H.prior_launches_why(con, CR, coin_slot=1, coin_block_time=1) == "unknown_scan"


def test_a_capped_scan_that_met_an_unreadable_tx_is_unknown_not_a_lower_bound(tmp_path):                  # Delta P1 0646abf72
    page = full_page([{"slot": 10, "blockTime": 1000, "meta": None, "transaction": None}, create_tx("good", "X", CR, 100, 1000)], "next")
    f, con, ledger = build(tmp_path, Fake({CR: [page]}), page_cap=1)
    assert f.fetch_creator(CR) == "unreadable_tx"
    assert con.execute("select state, reason from creator_scans where creator=?", (CR,)).fetchone() == ("unknown", "unreadable_tx")
    assert H.prior_launches(con, CR, coin_slot=50, coin_block_time=1000) is None and con.execute("select count(*) from creator_launches").fetchone()[0] == 0


def test_the_nightly_fetcher_still_retries_a_plain_429_and_the_inline_one_does_not(tmp_path):                # Delta P2
    c = cycle(tmp_path)
    ledger = L.Ledger(tmp_path / "credits.db", c, clock=lambda: NOW, costs=COSTS)
    con = H.connect(tmp_path / "history.db")
    night = Fake(script=[(429, b"slow down")] * 6)
    assert H.Fetcher(con, ledger, night.transport, clock=lambda: NOW).fetch_coin("M1") == "rate_limited" and len(night.calls) == 4
    inline = Fake(script=[(429, b"slow down")] * 6)
    assert H.Fetcher(con, ledger, inline.transport, clock=lambda: NOW, rate_retry=False).fetch_coin("M2") == "rate_limited" and len(inline.calls) == 1


def test_an_inline_request_is_told_the_remaining_budget_and_never_starts_when_less_than_its_timeout_is_left(tmp_path):
    fake = Fake({"M1": [coin_page("M1", 5, 1_793_100_000)]})
    t = [NOW]
    f, con, ledger = build(tmp_path, fake)
    f.clock, f.call_timeout, f.deadline = (lambda: t[0]), 2.0, NOW + 3.0
    seen = []
    orig = fake.transport
    f.transport = lambda m, p: (seen.append(p["timeout"]), orig(m, p))[1]
    t[0] = NOW + 1.5                                                                                          # 1.5 s left < 2.0 s call timeout: not started, not reserved
    assert f.fetch_coin("M1") == "deadline" and seen == [] and ledger.total() == 0
    t[0] = NOW + 0.5                                                                                          # 2.5 s left: started with timeout min(2.0, 2.5)
    f.fetch_coin("M1")
    assert seen == [2.0]


def test_the_budget_is_re_read_after_the_reserve_so_its_wait_is_not_lost(tmp_path):                       # Delta: deadline after the DB reserve, not only before it
    fake = Fake({"M1": [coin_page("M1", 5, 1_793_100_000)]})
    t = [NOW]
    f, con, ledger = build(tmp_path, fake)
    f.clock, f.call_timeout, f.deadline = (lambda: t[0]), 2.0, NOW + 4.0
    real_reserve = ledger.reserve
    ledger.reserve = lambda m, page=None: (t.__setitem__(0, t[0] + 1.0), real_reserve(m, page=page))[1]    # the reserve itself takes 1 s
    seen = []
    orig = fake.transport
    f.transport = lambda m, p: (seen.append(p["timeout"]), orig(m, p))[1]
    f.fetch_coin("M1")
    assert seen == [2.0] and t[0] == NOW + 1.0
    t[0], seen[:] = NOW, []
    f.deadline = NOW + 2.5                                                                                    # admitted (2.5 >= 2.0) but the reserve eats 1 s: 1.5 s left
    f.fetch_coin("M2")
    assert seen == [1.5]                                                                                      # told the REAL remaining time


def test_a_reserved_request_with_no_time_left_is_not_sent_and_settles_at_zero_credits(tmp_path):
    fake = Fake()
    t = [NOW]
    f, con, ledger = build(tmp_path, fake)
    f.clock, f.call_timeout, f.deadline = (lambda: t[0]), 2.0, NOW + 2.5
    real_reserve = ledger.reserve
    ledger.reserve = lambda m, page=None: (t.__setitem__(0, t[0] + 2.6), real_reserve(m, page=page))[1]    # the reserve consumes the whole budget
    assert f.fetch_coin("M1") == "deadline" and fake.calls == []
    assert ledger.total() == 0 and ledger.counts() == {"committed": 1}                                        # one committed attempt at 0 credits, not a phantom reservation
