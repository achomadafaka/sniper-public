"""4663 Relay prewarm (P2) and pre-buy (P3): spec docs/superpowers/specs/2026-10-14-rh-relay-prebuy-design.md.

Task 1: a verified deposit's Relay request is accepted only on an exact match, polled at most 8 times
166 ms apart, stopping at once on a 429 or 5xx."""
import pytest

from sniper.services import memecoin_rh_relay_prebuy as rp

UID = "bb2ab76d-f022-53a1-820e-a5f46522576b"
WALLET = "0xbf6c6f756033faab8a5b2ee8da8835e0d9e0651b"
MINT = "0x403cb172fc842115392e6b4389793e1712b0d294"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ORDER = "0x1335dfaa6bf7bbc12225624a9f92809f60e4e137c3c0f918af4d0715212e64f7"
DEPOSIT = dict(order_id=ORDER, slot=456819840,
               signature="aiTu6q8dS7tyi3a9bsMaRMryiqa9a2L4479YhA3mmeiiNUhywk51nxszNXcb9acKCVjZ6ZPA2gX9Km3ZLGy5qgo",
               depositor="8sfbKCYFvKuzG6vE5dAyHrSVxEgRWgMrcjUEvfhYr4ng", amount_atomic=750_000_000,
               uids=(UID,), arrived=1791976875.579)
WATCHED = {4663: {WALLET: (UID, "koy")}}             # the roster per pre-buy chain


def _request(**over):
    """The shape GET /requests/v2?orderId= returned for koy's order (2026-10-14), unsettled."""
    request = {
        "id": "0x1791976874135ebfea6c0a2a7a8f94cb2ab3fadd58b1baa1a188c7905e1e8c5e", "status": "pending",
        "user": DEPOSIT["depositor"], "recipient": WALLET, "createdAt": "2026-10-14T11:21:15.877Z",
        "data": {"inTxs": [], "outTxs": [], "metadata": {
            "recipient": WALLET,
            "currencyIn": {"currency": {"chainId": 792703809, "address": USDC, "decimals": 6},
                           "amount": "750000000"},
            "currencyOut": {"currency": {"chainId": 4663, "address": MINT, "decimals": 18},
                            "amount": "21818689697704593307814854",
                            "minimumAmount": "14180425000263118281948284"}}}}
    for key, value in over.items():
        if key in ("status", "recipient"):
            request[key] = value
        else:
            request["data"][key] = value
    return request


def _meta(request):
    return request["data"]["metadata"]


def test_the_banksy_request_is_accepted_with_its_mint():
    got, why = rp.request_match({"requests": [_request()]}, DEPOSIT, watched=WATCHED)
    assert why is None and got["mint"] == MINT and got["wallet"] == WALLET and got["uid"] == UID
    assert got["request_id"].startswith("0x179197") and got["deposit"] == DEPOSIT


@pytest.mark.parametrize("change, reason", [
    (lambda r: r.update(recipient="0x" + "11" * 20), "recipient_mismatch"),
    (lambda r: _meta(r)["currencyOut"]["currency"].update(chainId=8453), "destination_not_enabled"),
    (lambda r: _meta(r)["currencyIn"].update(amount="749999999"), "amount_mismatch"),
    (lambda r: _meta(r)["currencyIn"]["currency"].update(address="So11111111111111111111111111111111111111112"),
     "currency_in_not_usdc"),
    (lambda r: r.update(status="success"), "indexed_after_fill"),
    (lambda r: r["data"].update(outTxs=[{"hash": "0x24f5"}]), "indexed_after_fill"),
    (lambda r: r.update(status="refund"), "request_not_unsettled"),
    (lambda r: _meta(r)["currencyOut"]["currency"].pop("address"), "malformed_request"),
])
def test_anything_but_an_exact_unsettled_match_is_refused(change, reason):
    request = _request()
    change(request)
    assert rp.request_match({"requests": [request]}, DEPOSIT, watched=WATCHED) == (None, reason)


def test_the_right_users_wallet_on_another_chain_is_refused():
    # watched is the 4663 roster: the uid's 8453 wallet is not in it, whatever the request says.
    other_chain = dict(WATCHED)
    request = _request(recipient="0x" + "22" * 20)
    _meta(request)["currencyOut"]["currency"]["chainId"] = 8453
    assert rp.request_match({"requests": [request]}, DEPOSIT, watched=other_chain)[0] is None


def test_another_users_4663_wallet_is_refused():
    assert rp.request_match({"requests": [_request()]}, DEPOSIT,
                            watched={4663: {WALLET: ("someone-else", "x")}}) == (None, "recipient_mismatch")


@pytest.mark.parametrize("payload, reason", [({"requests": []}, "not_indexed"),
                                             ({"requests": [_request(), _request()]}, "ambiguous_request"),
                                             ({}, "malformed_request")])
def test_zero_two_or_no_requests(payload, reason):
    assert rp.request_match(payload, DEPOSIT, watched=WATCHED) == (None, reason)


class _Clock:
    def __init__(self):
        self.t, self.slept = 100.0, []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _poller(answers, clock):
    calls = []

    def fetch(url, **kw):
        calls.append(url)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    return rp.RequestPoller(fetch=fetch, watched_fn=lambda: WATCHED, clock=clock.monotonic,
                            sleep=clock.sleep, max_polls=8, per_s=6), calls


def test_not_indexed_polls_until_found_at_least_166_ms_apart():
    clock = _Clock()
    poller, calls = _poller([{"requests": []}, {"requests": []}, {"requests": [_request()]}], clock)
    got, why = poller.poll(DEPOSIT)
    assert why is None and got["mint"] == MINT and len(calls) == 3
    assert all(s >= 1 / 6 - 1e-9 for s in clock.slept) and "orderId=" + ORDER in calls[0]
    assert poller.counters["polls"] == 3 and got["found_ms"] >= 2 * 1000 / 6 - 1e-6


def test_first_polls_of_a_deposit_burst_share_the_request_start_budget():
    clock = _Clock()
    poller, calls = _poller([{"requests": [_request()]}] * 4, clock)
    starts = []
    for _ in range(4):
        assert poller.poll(DEPOSIT)[0] is not None
        starts.append(clock.monotonic())
    assert len(calls) == 4
    assert all(b - a >= 1 / 6 - 1e-9 for a, b in zip(starts, starts[1:]))


def test_outcome_poll_shares_the_deposit_request_budget(tmp_path):
    clock = _Clock()
    pipe, _fetched, _learned = _pipeline(tmp_path)
    starts = []
    pipe.poller.clock, pipe.poller.sleep = clock.monotonic, clock.sleep
    pipe.poller.fetch = lambda url: starts.append(clock.monotonic()) or {"requests": [_request()]}
    assert pipe.poller.poll(DEPOSIT)[0] is not None
    pipe.due.append((0, ORDER))
    assert pipe.settle_due(now=1) == "none_30s"
    assert len(starts) == 2 and starts[1] - starts[0] >= 1 / 6 - 1e-9


def test_eight_unindexed_polls_give_up_and_count():
    clock = _Clock()
    poller, calls = _poller([{"requests": []}] * 9, clock)
    assert poller.poll(DEPOSIT) == (None, "not_indexed_after_8") and len(calls) == 8
    assert poller.counters["not_indexed_after_8"] == 1


@pytest.mark.parametrize("status, counter", [(429, "http_429"), (503, "http_5xx")])
def test_a_429_or_5xx_stops_at_once(status, counter):
    clock = _Clock()
    poller, calls = _poller([rp.RelayHttpError(status), {"requests": [_request()]}], clock)
    assert poller.poll(DEPOSIT) == (None, counter) and len(calls) == 1
    assert poller.counters[counter] == 1


def test_a_mismatch_stops_and_counts_once():
    clock = _Clock()
    poller, calls = _poller([{"requests": [_request(recipient="0x" + "33" * 20)]}], clock)
    assert poller.poll(DEPOSIT) == (None, "recipient_mismatch") and len(calls) == 1
    assert poller.counters["rejected_mismatch"] == 1


# --- Task 2: the pre-buy decision on the detector's tape -------------------------------------------

from fractions import Fraction

from sniper.services import memecoin_rh_fast_decide as fast
from test_memecoin_rh_fast_decide import B, HOOK, META, TOKEN, ZPOOL, ZUP_PRE, _row, _tape

FOUND = dict(mint=TOKEN, wallet=WALLET, uid=UID, request_id="0xr", deposit=DEPOSIT)


def _decide(tape, *, head=None, gap=False, clip_usd=20.0, impact=5.0):
    return rp.prebuy_decide(FOUND, tape=tape, eth_usd=2500.0, clip_usd=clip_usd, max_impact_pct=impact,
                            max_lag_blocks=8, chain_head=head if head is not None else tape.head,
                            gap_fn=lambda lo, hi: gap, now=1000.0)


def test_a_known_hook_pool_gives_the_spot_cap_and_its_min_out():
    tape = _tape([_row(ZPOOL, B - 1, "0xprev", ZUP_PRE["sqrt"])])
    got, why = _decide(tape)
    assert why is None and got["route_kind"] == "v4_native" and got["pool"] == ZPOOL
    spot = fast.coin_price(ZUP_PRE["sqrt"], True)
    cap = Fraction(got["cap_num"], got["cap_den"])
    assert cap == spot * Fraction(105, 100)
    clip_wei = int(Fraction(20) / Fraction(2500) * 10 ** 18)
    assert got["clip_wei"] == clip_wei and got["min_out"] == clip_wei * cap.denominator // cap.numerator
    assert 0 < got["impact_pct"] <= 5 and got["est_out"] >= got["min_out"]
    assert got["state_kind"] == "prebuy_tape" and got["state_age_blocks"] == 1


def test_an_impact_over_5_pct_is_no_buy():
    tape = _tape([_row(ZPOOL, B - 1, "0xprev", ZUP_PRE["sqrt"], liquidity=10 ** 15)])   # a thin pool
    assert _decide(tape) == (None, "impact_over_cap")


def test_a_tape_gap_over_the_pool_is_no_buy():
    tape = _tape([_row(ZPOOL, B - 40, "0xprev", ZUP_PRE["sqrt"])])
    seen = []
    got = rp.prebuy_decide(FOUND, tape=tape, eth_usd=2500.0, clip_usd=20.0, max_impact_pct=5.0,
                           max_lag_blocks=8, chain_head=B - 40,
                           gap_fn=lambda lo, hi: seen.append((lo, hi)) or True, now=1000.0)
    assert got == (None, "tape_gap") and seen == [(B - 40, B - 40)]


def test_a_tape_behind_the_chain_is_no_buy():
    tape = _tape([_row(ZPOOL, B - 1, "0xprev", ZUP_PRE["sqrt"])])
    assert _decide(tape, head=B - 1 + 9) == (None, "tape_behind")


def test_an_unknown_hook_is_no_buy(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_HOOK_OUTPUT_TAKE_BPS", {})
    tape = _tape([_row(ZPOOL, B - 1, "0xprev", ZUP_PRE["sqrt"])])
    assert _decide(tape) == (None, "unknown_hook")
    assert HOOK in META[ZPOOL]["hooks"]


def test_an_old_quiet_pool_on_a_current_tape_is_allowed_and_its_age_recorded():
    # Option 1 (Alpha): no pool-age bound for a pre-buy; the tape is current and gap-free.
    tape = _tape([_row(ZPOOL, B - 500, "0xold", ZUP_PRE["sqrt"])])
    tape.head = B                                   # other pools' rows keep the tape current
    got, why = _decide(tape, head=B)
    assert why is None and got["state_age_blocks"] == 501        # head + 1 - its last row


def test_a_missed_buy_reverts_on_min_out_and_never_pays_over_1_05_spot():
    # The tape's state is stale: a buy it missed moved the real price up 10%. The signed min_out
    # (clip at tape spot x 1.05) is more than the real pool gives, so the swap reverts.
    tape = _tape([_row(ZPOOL, B - 30, "0xold", ZUP_PRE["sqrt"])])
    got, _ = _decide(tape)
    real_sqrt = int(ZUP_PRE["sqrt"] / (1.10 ** 0.5))          # price (wei/coin) up 10%
    real_out = fast.source_pool_est_out(got["clip_wei"], dict(sqrt=real_sqrt, liquidity=ZUP_PRE["liquidity"]),
                                        rp._key_of(got), True)
    assert real_out < got["min_out"]                          # reverts: no fill above the cap
    cap = fast.coin_price(ZUP_PRE["sqrt"], True) * Fraction(105, 100)
    # min_out is the clip at the cap, rounded down as min_out_at rounds: the most we can pay is the cap
    assert got["min_out"] * cap <= got["clip_wei"] < (got["min_out"] + 1) * cap


def test_no_pool_is_no_buy():
    assert _decide(_tape([]), head=B) == (None, "tape_empty")
    tape = _tape([_row(ZPOOL, B - 1, "0xprev", ZUP_PRE["sqrt"])])
    got = rp.prebuy_decide(dict(FOUND, mint="0x" + "77" * 20), tape=tape, eth_usd=2500.0, clip_usd=20.0,
                           max_impact_pct=5.0, max_lag_blocks=8, chain_head=tape.head,
                           gap_fn=lambda lo, hi: False, now=1000.0)
    assert got == (None, "no_pool")


# --- Task 3: the pre-buy's initiation proof and its guard --------------------------------------------

from types import SimpleNamespace

from sniper.services.memecoin_source_initiation import source_initiation_refusal


def _prebuy_ev(**over):
    proof = rp.prebuy_initiation(FOUND_BANKSY)
    ev = SimpleNamespace(signal_source="relay_prebuy", mint=MINT, source_user_id=UID, source_wallet=WALLET,
                         fomo_event_id="rhprebuy:4663:%s:%s" % (ORDER, MINT), source_block_number=None,
                         source_initiation=proof)
    for key, value in over.items():
        if key in proof:
            proof[key] = value
        else:
            setattr(ev, key, value)
    return ev


FOUND_BANKSY = dict(mint=MINT, wallet=WALLET, uid=UID, request_id="0x" + "17" * 32, deposit=DEPOSIT,
                    out_chain=4663, in_amount=DEPOSIT["amount_atomic"])


@pytest.fixture
def prebuy_on(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREBUY", True)


def test_a_matching_prebuy_proof_is_accepted(prebuy_on):
    assert source_initiation_refusal(_prebuy_ev(), "4663") is None


def test_a_prebuy_without_a_proof_no_longer_passes_unguarded(prebuy_on):
    # Before this change an unknown signal source returned None: no proof needed at all.
    assert source_initiation_refusal(_prebuy_ev(source_initiation=None), "4663") == "missing_receipt_proof"


def test_with_the_knob_off_a_prebuy_proof_is_refused():
    assert source_initiation_refusal(_prebuy_ev(), "4663") == "wallet_initiation_unproved"


@pytest.mark.parametrize("over", [
    dict(beneficiary="0x" + "11" * 20), dict(mint="0x" + "22" * 20), dict(source_user_id="other"),
    dict(origin_identity_user_id="other"), dict(order_id="0x" + "99" * 32), dict(request_in_amount=1),
    dict(request_out_chain=8453), dict(origin_network_id="8453"), dict(proof_version="relay-deposit-processed-v1"),
    dict(fomo_event_id="rhfast:4663:%s:%s" % (ORDER, MINT)), dict(signal_source="rh_fast_lane"),
])
def test_any_mismatch_refuses(prebuy_on, over):
    assert source_initiation_refusal(_prebuy_ev(**over), "4663") is not None


def test_only_on_4663(prebuy_on):
    assert source_initiation_refusal(_prebuy_ev(), "8453") is not None


# --- Task 4: the runner side: the event, the lane, its worker, the dispatch ---------------------------

import itertools
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import test_memecoin_rh_fast_entry as fe  # noqa: E402
from sniper.services import memecoin_rh_fast_lane as lane  # noqa: E402

LANE_WALLET = "0x" + "ef" * 20                 # the harness's armed user "uid"
AMOUNT = 65_300_000                            # $65.30 USDC: the harness's source size
_ORDERS = itertools.count(1)


def _prebuy(**over):
    """A relay_prebuy_v1 datagram as the feed sends it: the harness's coin at its tape spot, a 5% cap."""
    from sniper.services.memecoin_evm_v4_events import v4_pool_id
    from sniper.util.clock import real_utc_now_iso
    now = time.time()
    order = "0x%064x" % (0xfeed0000 + next(_ORDERS))
    spot = Fraction(1 << 192, fe.SQRT ** 2)                 # wei per coin atomic (native is currency0)
    cap = spot * Fraction(105, 100)
    clip = 8 * 10 ** 15                                     # $20 at $2,500
    deposit = dict(DEPOSIT, order_id=order, amount_atomic=AMOUNT, uids=("uid",))
    proof = rp.prebuy_initiation(dict(mint=fe.MINT, wallet=LANE_WALLET, uid="uid", request_id="0x" + "17" * 32,
                                      out_chain=4663, in_amount=AMOUNT, deposit=deposit))
    p = dict(kind="relay_prebuy_v1", tx_hash=order, seq=1000, wallet=LANE_WALLET, mint=fe.MINT, handle="Degen",
             source_user_id="uid", permit_nonce="relay:" + order, arrival_utc=real_utc_now_iso(),
             pool=v4_pool_id(fe.KEY), key=fe.KEY, route_kind="v4_native", in_is_c0=True,
             cap_num=cap.numerator, cap_den=cap.denominator, clip_wei=clip,
             min_out=clip * cap.denominator // cap.numerator, state_kind="prebuy_tape", tape_lag_blocks=0,
             sent_epoch=now, snapshot=dict(native_wei=10 ** 18, weth_wei=10 ** 18, nonce=42,
                                           gas_price_wei=40_000_000, read_at=now - 1),
             eth_usd=2500.0, eth_usd_at=now, paid_usd=AMOUNT / 1e6,
             spot_wei_num=spot.numerator, spot_wei_den=spot.denominator, source_initiation=proof,
             prebuy=dict(order_id=order, deposit_sig=DEPOSIT["signature"], request_id="0x" + "17" * 32,
                         deposit_seen_at="2026-10-14T11:21:15.579Z", request_found_at="2026-10-14T11:21:15.877Z",
                         state_block=998, state_age_blocks=3, impact_pct=1.2))
    p.update(over)
    return p


@pytest.fixture
def armed(monkeypatch, tmp_path):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREBUY", True)
    return fe._setup(monkeypatch, tmp_path, "live")


def _run(built, payload):
    """The pre-buy as the worker runs it, on this thread."""
    return built["prebuy"].run_one(payload, time.time())


def _prebuy_rows(built):
    import sqlite3
    db = sqlite3.connect(built["db_path"])
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute("SELECT * FROM rh_relay_prebuys ORDER BY rowid")]
    finally:
        db.close()


def test_the_prebuy_event_prices_at_spot_from_the_deposit():
    p = _prebuy()
    ev = rp.PrebuyEvent(p, decimals=18, supply=10 ** 27)
    spot = (1 << 192) / fe.SQRT ** 2
    assert ev.signal_source == "relay_prebuy" and ev.delivery_lane == "rh_relay_prebuy"
    assert ev.fomo_event_id == "rhprebuy:4663:%s:%s" % (p["tx_hash"], fe.MINT)
    assert ev.their_size_usd == 65.3                           # the deposit's signed USDC
    assert ev.their_price == pytest.approx(spot * 2500.0)      # USD per whole coin at the tape spot
    assert ev.their_market_cap == pytest.approx(10 ** 27 * spot / 1e18 * 2500.0)
    assert ev.evm_chase_reference.post() == {"reason": "relay_prebuy_no_source_fill"}
    assert ev.source_initiation["proof_version"] == "relay-request-prebuy-v1"
    assert ev.source_evidence["signature"] == p["tx_hash"] and ev.source_evidence["source_fill"] is None


def test_a_prebuy_runs_every_funded_gate_and_reaches_the_lane_send(armed, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 30.0)       # the hook approves more than $20
    built, sender, net, ledger = armed
    rec = _run(built, _prebuy())
    assert rec["reason"].startswith("rejected:"), rec            # the harness sender refuses every send
    assert len(sender.raws) == 1 and rec["initiation"].startswith("relay_request:")
    # The lane's I/O rule: nothing leaves the host before the send (no early shared-route quote).
    assert net.calls == []
    trace = ledger.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0]
    assert "evm.early_quote_start" not in trace
    assert fe._attempts(ledger)[-1]["signal_source"] == "relay_prebuy"
    pending = ledger.execute("SELECT signal_source, origin_event_id, status, resolution_reason "
                             "FROM memecoin_pending_entries").fetchone()
    assert pending["signal_source"] == "relay_prebuy" and pending["status"] == "failed"
    assert pending["origin_event_id"].startswith("rhprebuy:4663:")
    assert pending["resolution_reason"].startswith("rh_prebuy_rejected")
    # The $20 clip at the 5% spot cap; never the fast lane's pilot clip.
    clip = 8 * 10 ** 15
    p = _prebuy()
    assert rec["clip_wei"] == clip and rec["min_out"] == clip * p["cap_den"] // p["cap_num"]
    [row] = _prebuy_rows(built)
    assert row["reason"] == rec["reason"] and row["uid"] == "uid" and row["state_age_blocks"] == 3
    assert row["request_id"] == "0x" + "17" * 32 and row["decide_head_block"] == 1000


def _pending_rows(ledger):
    return [dict(r) for r in ledger.execute(
        "SELECT signal_source, status, resolution_reason, tx_sig FROM memecoin_pending_entries ORDER BY id")]


def test_with_the_knob_off_there_is_no_prebuy_worker_and_the_datagram_is_dropped(monkeypatch, tmp_path):
    built, sender, net, ledger = fe._setup(monkeypatch, tmp_path, "live")
    assert built["prebuy"] is None and built["dispatch"].prebuy is None
    assert fe._go(built, _prebuy()) is None
    assert built["dispatch"].drops.counts["prebuy_off"] == 1
    assert sender.raws == [] and _pending_rows(ledger) == []


@pytest.mark.parametrize("change", [
    dict(source_initiation=None), dict(route_kind="v4_shared"), dict(spot_wei_num=0),
    dict(paid_usd=float("nan")), dict(prebuy={"order_id": "0x" + "99" * 32}), dict(seq=0),
])
def test_a_malformed_prebuy_datagram_is_dropped_by_the_listener(armed, change):
    built, sender, net, ledger = armed
    assert fe._go(built, _prebuy(**change)) is None
    assert built["dispatch"].drops.counts["malformed"] == 1 and _pending_rows(ledger) == []


def test_the_listener_only_queues_and_the_worker_decides_once(armed):
    built, sender, net, ledger = armed
    worker = built["prebuy"]
    worker.start()
    try:
        p = _prebuy()
        assert fe._go(built, p) == "prebuy_queued"
        assert worker.idle.wait(10)
        assert not worker.inflight(fe.MINT)                  # finished: the source fill is the lane's again
        assert fe._go(built, p) is None and built["dispatch"].drops.counts["duplicate"] == 1
        worker.busy = True                                   # a pre-buy in flight: the next is refused
        assert fe._go(built, _prebuy()) == "prebuy_busy"
        worker.busy = False
    finally:
        worker.close()
    assert [r["order_id"] for r in _prebuy_rows(built)] == [p["tx_hash"]] and len(sender.raws) == 1


@pytest.mark.parametrize("change,reason", [
    (dict(wallet="0x" + "11" * 20), "prebuy_proof_refused:receipt_identity_mismatch"),
    (dict(source_user_id="other"), "prebuy_proof_refused:receipt_identity_mismatch"),
    (dict(mint="0x" + "4" * 40), "prebuy_proof_refused:receipt_identity_mismatch"),
])
def test_a_proof_that_does_not_bind_this_buy_refuses_before_any_funded_row(armed, change, reason):
    built, sender, net, ledger = armed
    if "mint" in change:                                  # warm, so the proof is what refuses it
        built["cache"]._verdicts[change["mint"]] = (True, None, time.time(), 0.0)
    rec = _run(built, _prebuy(**change))
    assert rec["reason"] == reason and sender.raws == [] and _pending_rows(ledger) == []


def test_a_cap_looser_than_spot_plus_the_impact_cap_refuses_before_any_funded_row(armed):
    built, sender, net, ledger = armed
    spot = Fraction(1 << 192, fe.SQRT ** 2)
    loose = spot * Fraction(106, 100)
    rec = _run(built, _prebuy(cap_num=loose.numerator, cap_den=loose.denominator))
    assert rec["reason"] == "prebuy_cap_unbound" and sender.raws == [] and _pending_rows(ledger) == []


def test_a_fast_lane_tape_state_is_not_a_prebuy_state(armed):
    built, sender, net, ledger = armed
    rec = _run(built, _prebuy(state_kind="tape_post"))
    assert rec["reason"] == "estimated_state" and sender.raws == []


@pytest.mark.parametrize("cap", ["hour", "fills"])
def test_the_prebuy_pilot_caps_are_its_own(armed, cap):
    built, sender, net, ledger = armed
    pre = built["prebuy"].lane
    assert (pre.pilot_fills, pre.max_sends_per_hour, pre.clip_usd) == (20, 4, 20.0)
    if cap == "hour":
        pre.send_times.extend([time.time() - 60] * 4)
    else:
        pre.fills = 20
    rec = _run(built, _prebuy())
    assert rec["reason"] == ("pilot_rate_cap" if cap == "hour" else "pilot_complete") and sender.raws == []
    # The fast lane's own pilot is untouched by the pre-buy's.
    assert built["live"].fills == 0 and len(built["live"].send_times) == 0


def test_the_fast_lane_stop_file_stops_the_prebuy(armed):
    built, sender, net, ledger = armed
    built["live"].stopped = True
    rec = _run(built, _prebuy())
    assert rec["reason"] == "operator_stop" and sender.raws == [] and _pending_rows(ledger) == []


class _ScriptedLane:
    """A pre-buy lane whose handle answers `reasons` in turn (the worker's own logic under test)."""
    def __init__(self, reasons, fills=0):
        self.reasons, self.fills, self.pilot_fills, self.calls = list(reasons), fills, 20, 0
        self.stopped, self.conn = False, None

    def handle(self, payload, **_kw):
        self.calls += 1
        reason = self.reasons.pop(0)
        if reason.startswith("filled"):
            self.fills += 1
        return {"reason": reason, "tx_hash": "0x" + "ab" * 32, "send_reply": "accepted",
                "source_tx": payload["tx_hash"], "mint": payload["mint"]}


class _Cache:
    def note_candidates(self, entries, *, now):
        pass

    def reason(self, mint, *, now, evidence=None):
        return None

    def stats(self, mint, *, now):
        return None


def _worker(tmp_path, reasons, fills=0, alerts=None):
    import sqlite3
    return rp.RelayPrebuyWorker(
        _ScriptedLane(reasons, fills=fills), live=None, connect=lambda: sqlite3.connect(":memory:"),
        db_path=str(tmp_path / "lane.db"), registry={}, cache=_Cache(), max_reverts=3,
        alert_fn=(alerts if alerts is not None else []).append)


def test_three_consecutive_reverts_stop_the_prebuy_with_one_alert_and_survive_a_restart(tmp_path):
    alerts = []
    worker = _worker(tmp_path, ["reverted"] * 3, alerts=alerts)
    for _ in range(3):
        worker.run_one(_prebuy(), time.time())
    assert worker.stop_reason == "revert_stop" and len(alerts) == 1 and "3 consecutive reverts" in alerts[0]
    assert worker.run_one(_prebuy(), time.time())["reason"] == "prebuy_revert_stop"
    assert worker.lane.calls == 3 and len(alerts) == 1             # stopped: the lane is never asked
    restarted = _worker(tmp_path, ["filled"])
    assert restarted.stop_reason == "revert_stop"
    assert restarted.run_one(_prebuy(), time.time())["reason"] == "prebuy_revert_stop"
    # The operator's reset row ends the run.
    import sqlite3
    db = sqlite3.connect(str(tmp_path / "lane.db"))
    db.execute("INSERT INTO rh_relay_prebuys (order_id, reason) VALUES ('reset-1', 'operator_reset')")
    db.commit()
    db.close()
    assert _worker(tmp_path, ["filled"]).stop_reason is None


def test_a_fill_between_reverts_resets_the_run(tmp_path):
    worker = _worker(tmp_path, ["reverted", "reverted", "filled", "reverted", "reverted"])
    for _ in range(5):
        worker.run_one(_prebuy(), time.time())
    assert worker.stop_reason is None and worker.reverts == 2
    assert _worker(tmp_path, []).reverts == 2


def test_the_twentieth_fill_alerts_once(tmp_path):
    alerts = []
    worker = _worker(tmp_path, ["filled", "pilot_complete"], fills=19, alerts=alerts)
    worker.run_one(_prebuy(), time.time())
    worker.run_one(_prebuy(), time.time())
    assert len(alerts) == 1 and "pilot complete: 20 fills" in alerts[0]


def test_an_unreadable_records_file_is_a_stop_and_no_file_is_a_fresh_start(tmp_path):
    assert rp.consecutive_reverts(str(tmp_path / "absent.db"), 3) == 0
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite file" * 100)
    assert rp.consecutive_reverts(str(bad), 3) == 3


# --- dedup (spec §2.4): the source fill after a pre-buy, on the lane and on the slow path ------------

def _slow_path(ledger, monkeypatch):
    """The ordinary funded leg for the same coin (a J7 copy of the source's buy) up to its executor."""
    from sniper.services import memecoin_real_leg as RL
    from test_memecoin_entry_retry_lane import _ev
    from dataclasses import replace
    monkeypatch.setattr(RL, "real_entries_allowed", lambda kill_path=None: True)
    executed = []
    ev = replace(_ev(fid="j7-source", mint=fe.MINT), network_id="4663")
    res = RL.real_entry(
        ledger, ev, day="2026-10-14", usdc_balance=300.0, sol_balance=0.0,
        now_iso="2026-10-14T11:21:17+00:00", trade_size=10.0, token_safety_fn=lambda *a, **k: (True, {}),
        execute_fn=lambda *a, **k: {}, evm_allowlist={"4663"}, resolve_fn=lambda _m: "4663",
        evm_entry_fn=lambda *a, **k: executed.append(ev.fomo_event_id) or {"status": "filled"})
    return res, executed


def test_after_a_prebuy_reserves_the_source_fill_is_refused_on_the_lane_and_the_slow_path(armed, monkeypatch):
    built, sender, net, ledger = armed
    pre = built["prebuy"].lane
    pre.lock_receipt_cap_s = pre.receipt_timeout_s = 0.05
    sender.send = lambda raw: sender.raws.append(raw) or ("ambiguous", "timeout")
    rec = _run(built, _prebuy())
    assert rec["reason"] == "submitted_unknown", rec                # we may own the coin: kept bound
    [pending] = _pending_rows(ledger)
    assert pending["signal_source"] == "relay_prebuy" and pending["tx_sig"] == rec["tx_hash"]
    sends = len(sender.raws)
    lane_rec = fe._go(built, fe._fresh_after_send())                # the source's own fill, on the lane
    assert lane_rec["reason"].startswith(("hook:rejected:one position per coin",
                                          "hook:rejected:unresolved signed transaction on 4663")), lane_rec
    assert len(sender.raws) == sends
    res, executed = _slow_path(ledger, monkeypatch)
    assert res["status"] == "rejected" and executed == [], res
    assert res["reason"].startswith(("one position per coin", "unresolved signed transaction on 4663")), res


def test_a_prebuy_aborted_before_its_send_blocks_neither_path(armed, monkeypatch):
    from sniper.execution.exec_lock import wallet_exec_lock
    built, sender, net, ledger = armed
    pre = built["prebuy"].lane
    with wallet_exec_lock(pre.lock_key, lock_dir=pre.lock_dir, blocking=False) as held:
        assert held
        rec = _run(built, _prebuy())
    assert rec["reason"] == "lock_busy" and sender.raws == []
    assert _pending_rows(ledger)[0]["status"] == "failed"
    lane_rec = fe._go(built, fe._payload())                         # the source's own fill, on the lane
    assert lane_rec["reason"].startswith("rejected:"), lane_rec      # reached the harness send
    assert len(sender.raws) == 1
    res, executed = _slow_path(ledger, monkeypatch)
    assert executed == ["j7-source"], res


def test_a_prebuy_reservation_alone_refuses_the_source_fill_on_the_lane(armed):
    """The window between the pre-buy's reservation and its signature: one position per coin."""
    from sniper.persistence import db_memecoin as db
    built, sender, net, ledger = armed
    db.reserve_pending(ledger, mint=fe.MINT, source_user_id="uid", strategy="alpha_b", notional=20.0,
                       reserved_at="2026-10-14T11:21:16+00:00", network_id="4663", signal_source="relay_prebuy",
                       origin_event_id="rhprebuy:4663:%s:%s" % ("0x" + "12" * 32, fe.MINT))
    rec = fe._go(built, fe._payload())
    assert rec["reason"].startswith("hook:rejected:one position per coin"), rec
    assert sender.raws == []


# --- Task 5: the feed side: deposit -> poll -> warm / decide -> datagram -----------------------------

SEEN_AT = "2026-10-14T11:21:15.579000+00:00"


def _token_request(**over):
    request = _request(**over)
    _meta(request)["currencyOut"]["currency"]["address"] = TOKEN
    return {"requests": [request]}


class _Sent:
    def __init__(self, ok=True):
        self.ok, self.payloads = ok, []

    def __call__(self, address, payload):
        self.payloads.append(payload)
        return self.ok


def _pipeline(tmp_path, *, answers=None, prewarm=False, prebuy=True, head=None, snapshot=True, send=None,
              watched=None, tape=None, registry_ok=lambda depositor, uid: True):
    tape = tape or _tape([_row(ZPOOL, B - 1, "0xprev", ZUP_PRE["sqrt"])])
    fetched, learned = [], []
    answers = list(answers or [_token_request()])

    def fetch(url):
        fetched.append(url)
        return answers.pop(0)
    poller = rp.RequestPoller(fetch=fetch, watched_fn=lambda: watched or WATCHED, clock=lambda: 0.0,
                              sleep=lambda s: None)
    live = dict(eth_usd=2500.0, eth_usd_at=1000.0, max_lag_blocks=8,
                snapshot=dict(native_wei=10 ** 18, weth_wei=0, nonce=7, gas_price_wei=40_000_000, read_at=999.0)
                if snapshot else None)
    pipe = rp.RelayPipeline(poller=poller, tape=tape, live=live, learn=learned.append, send=send or _Sent(),
                            head_fn=lambda: tape.head if head is None else head, gap_fn=lambda lo, hi: False,
                            watched_fn=lambda: watched or WATCHED, db_path=str(tmp_path / "feed.db"),
                            address="\0test", prewarm=prewarm, prebuy=prebuy, clip_usd=20.0, max_impact_pct=5.0,
                            registry_ok=registry_ok,
                            now_fn=lambda: 1000.0, now_iso=lambda: "2026-10-14T11:21:15.900000+00:00",
                            clock=lambda: 1791976875.9)
    return pipe, fetched, learned


def _requests_rows(tmp_path):
    import sqlite3
    db = sqlite3.connect(str(tmp_path / "feed.db"))
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute("SELECT * FROM rh_relay_requests")]
    finally:
        db.close()


def test_a_found_request_becomes_one_prebuy_datagram_the_lane_accepts(tmp_path, prebuy_on):
    pipe, fetched, learned = _pipeline(tmp_path)
    row = pipe.handle(DEPOSIT, SEEN_AT)
    [payload] = pipe.send.payloads
    assert row["prebuy_reason"] == "sent" and learned == [TOKEN] and len(fetched) == 1
    # The runner's own checks on what the feed sent: the listener's shape, the cap and the proof.
    assert lane._prebuy_malformed(payload) is None
    assert payload["kind"] == "relay_prebuy_v1" and payload["tx_hash"] == ORDER and payload["seq"] == B - 1
    assert payload["paid_usd"] == 750.0 and payload["wallet"] == WALLET and payload["source_user_id"] == UID
    assert payload["arrival_utc"] == SEEN_AT and payload["state_kind"] == "prebuy_tape"
    assert rp.prebuy_cap_bound(payload, 5.0) and not rp.prebuy_cap_bound(payload, 4.0)
    ev = rp.PrebuyEvent(payload, decimals=18, supply=10 ** 27)
    assert source_initiation_refusal(ev, "4663") is None
    import json
    json.dumps(payload)                                          # one datagram: JSON as sent
    [stored] = _requests_rows(tmp_path)
    assert stored["poll_reason"] == "found" and stored["mint"] == TOKEN and stored["prebuy_reason"] == "sent"
    assert stored["route_kind"] == "v4_native" and stored["chain_head"] == B - 1


def test_prewarm_alone_learns_and_warms_the_lane_and_never_sends_a_prebuy(tmp_path):
    pipe, fetched, learned = _pipeline(tmp_path, prewarm=True, prebuy=False)
    row = pipe.handle(DEPOSIT, SEEN_AT)
    [warm] = pipe.send.payloads
    assert warm["kind"] == "candidates" and lane.check_sender(warm, os.getpid(), os.getuid(),
                                                                detector_pid=os.getpid()) is None
    assert warm["entries"] == [dict(kind="v4_native", pool=ZPOOL, key=warm["entries"][0]["key"], mint=TOKEN,
                                    priority=-1)]
    assert row["warmed"] == 1 and "prebuy_reason" not in row and learned == [TOKEN]
    assert pipe.counters["warmed"] == 1 and TOKEN in pipe.warmed


def test_a_deposit_of_a_user_not_armed_on_4663_never_calls_relay(tmp_path):
    pipe, fetched, learned = _pipeline(tmp_path, watched={4663: {"0x" + "12" * 20: ("someone-else", "x")}})
    assert pipe.handle(DEPOSIT, SEEN_AT) is None
    assert fetched == [] and pipe.send.payloads == [] and pipe.counters["not_armed_prebuy_chain"] == 1


@pytest.mark.parametrize("answers,reason", [
    ([_token_request(recipient="0x" + "12" * 20)], "recipient_mismatch"),
    ([_token_request(status="success")], "indexed_after_fill"),
])
def test_a_refused_request_sends_nothing_and_is_recorded(tmp_path, answers, reason):
    pipe, fetched, learned = _pipeline(tmp_path, answers=answers, prewarm=True)
    row = pipe.handle(DEPOSIT, SEEN_AT)
    assert row["poll_reason"] == reason and pipe.send.payloads == [] and learned == []
    assert _requests_rows(tmp_path)[0]["poll_reason"] == reason


@pytest.mark.parametrize("kw,reason", [(dict(head=B - 1 + 9), "tape_behind"), (dict(snapshot=False), "no_snapshot")])
def test_a_refused_prebuy_decision_sends_nothing_and_counts(tmp_path, kw, reason):
    pipe, fetched, learned = _pipeline(tmp_path, **kw)
    row = pipe.handle(DEPOSIT, SEEN_AT)
    assert row["prebuy_reason"] == reason and pipe.send.payloads == []
    assert pipe.counters["prebuy_refused:" + reason] == 1


def test_a_failed_datagram_send_is_recorded_as_such(tmp_path):
    pipe, fetched, learned = _pipeline(tmp_path, send=_Sent(ok=False))
    assert pipe.handle(DEPOSIT, SEEN_AT)["prebuy_reason"] == "send_failed"
    assert pipe.counters["prebuy_send_failed"] == 1


def test_the_deposit_socket_offers_each_new_deposit_once_and_survives_a_failing_offer():
    from sniper.services.memecoin_rh_deposit_proof import DepositProofs
    offered = []
    proofs = DepositProofs(clock=lambda: DEPOSIT["arrived"], on_put=offered.append)
    proofs.put(dict(DEPOSIT))
    proofs.put(dict(DEPOSIT))                                     # the same deposit delivered again
    assert [p["order_id"] for p in offered] == [ORDER]

    def boom(proof):
        raise RuntimeError("queue gone")
    failing = DepositProofs(clock=lambda: DEPOSIT["arrived"], on_put=boom)
    failing.put(dict(DEPOSIT))
    assert len(failing) == 1 and failing.counters["on_put_error"] == 1


@pytest.mark.parametrize("on", [True, False])
def test_start_decide_feeds_the_poller_from_the_deposit_socket_only_with_a_knob_on(tmp_path, monkeypatch, on):
    import sqlite3
    import threading
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_rh_deposit_proof as dp
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_decide import _tape_db
    _tape_db(tmp_path / "t.db", []).commit()
    ledger = sqlite3.connect(str(tmp_path / "ledger.db"))
    m.ensure_memecoin_tables(ledger)
    ledger.commit()
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREBUY", on)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREWARM", False)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)       # wiring only, no loops
    captured = {}
    monkeypatch.setattr(dp, "start_deposit_proofs",
                        lambda **kw: captured.update(kw) or (None, None, None, ["socket"]))
    fast.start_decide(db_path=str(tmp_path / "d.db"), tape_path=str(tmp_path / "t.db"), log=lambda *a: None,
                      ledger_path=str(tmp_path / "ledger.db"), watched_fn=lambda: WATCHED, head_fn=lambda: B)
    if on:
        assert callable(captured["on_proof"]) and captured["on_proof"].poller.max_polls == 8
    else:
        assert captured["on_proof"] is None


# --- Task 6: the source outcome, and the heartbeat counters on both sides ----------------------------

def _found_then(tmp_path, *outcome_answers, clock=None):
    """A pipeline that found Banksy's request, then answers `outcome_answers` to the outcome call."""
    pipe, fetched, learned = _pipeline(tmp_path, answers=[_token_request(), *outcome_answers])
    if clock is not None:
        pipe.clock = clock
    pipe.handle(DEPOSIT, SEEN_AT)
    return pipe, fetched


def test_a_fill_the_feed_saw_settles_as_landed_without_asking_relay(tmp_path):
    pipe, fetched = _found_then(tmp_path)
    pipe.note_fills([dict(order_id=ORDER.upper().replace("0X", "0x"), side="buy", tx_hash="0x" + "fe" * 32,
                          seq=B + 1, mint=TOKEN), dict(order_id=None, tx_hash="0xother")])
    assert pipe.settle_due(1791976875.9 + 29) is None                    # not due before 30 s
    assert pipe.settle_due(1791976875.9 + 30) == "landed" and len(fetched) == 1
    [row] = _requests_rows(tmp_path)
    assert (row["source_outcome"], row["source_fill_tx"], row["source_fill_block"], row["outcome_detail"]) == (
        "landed", "0x" + "fe" * 32, B + 1, "feed")
    assert pipe.settle_due(1791976875.9 + 60) is None                    # settled once


@pytest.mark.parametrize("answer,outcome", [
    ({"requests": [dict(_request(status="refund"))]}, "refunded"),
    ({"requests": [dict(_request(status="pending"))]}, "none_30s"),
    ({"requests": [dict(_request(status="failure"))]}, "failed"),
    ({"requests": [_request(status="success", outTxs=[{"hash": "0x" + "ab" * 32}])]}, "landed"),
    (rp.RelayHttpError(429), "unknown"),
])
def test_without_a_feed_fill_one_relay_call_names_the_outcome(tmp_path, answer, outcome):
    pipe, fetched, learned = _pipeline(tmp_path, answers=[_token_request()])
    pipe.handle(DEPOSIT, SEEN_AT)

    def fetch(url):
        fetched.append(url)
        if isinstance(answer, Exception):
            raise answer
        return answer
    pipe.poller.fetch = fetch
    assert pipe.settle_due(1791976875.9 + 30) == outcome and len(fetched) == 2
    assert fetched[1].endswith("orderId=" + ORDER)
    assert pipe.counters["source_outcome:" + outcome] == 1
    [row] = _requests_rows(tmp_path)
    assert row["source_outcome"] == outcome
    if outcome == "landed":
        assert row["source_fill_tx"] == "0x" + "ab" * 32 and row["outcome_detail"] == "relay:success"


def test_the_feed_heartbeat_carries_the_relay_counters(tmp_path):
    pipe, fetched, learned = _pipeline(tmp_path, prewarm=True)
    pipe.handle(DEPOSIT, SEEN_AT)
    beat = pipe.beat({TOKEN: 1791976875.9 + 0.4})                  # the source's signal after the warm
    assert beat["deposits_seen"] == 1 and beat["polls"] == 1 and beat["found"] == 1
    assert beat["warmed"] == 1 and beat["warmed_then_signal"] == 1 and beat["prebuy_sent"] == 1
    assert beat["mint_found_ms_p50"] == beat["mint_found_ms_p90"] == 0.0
    import sqlite3
    db_path = str(tmp_path / "d.db")
    fast._beat(db_path, __import__("collections").Counter(), 0.5, _tape([]), lambda: "2026-10-14T00:00:00Z", 5,
               relay=beat)
    import json
    got = json.loads(sqlite3.connect(db_path).execute("SELECT relay_counters FROM rh_fast_heartbeat").fetchone()[0])
    assert got["warmed_then_signal"] == 1


def test_the_lane_heartbeat_carries_the_prebuy_counters_only_with_the_worker(armed, monkeypatch, tmp_path):
    import json
    import sqlite3
    built, sender, net, ledger = armed
    _run(built, _prebuy())
    built["dispatch"].counts["prebuys"] = 1

    def beat_row():
        lane.write_heartbeat(built["db_path"], built["dispatch"], built["cache"], built["paced"])
        db = sqlite3.connect(built["db_path"])
        try:
            return db.execute("SELECT prebuy_counters FROM rh_fast_lane_heartbeat ORDER BY rowid DESC").fetchone()[0]
        finally:
            db.close()
    got = json.loads(beat_row())
    assert got["prebuy_sent"] == 1 and got["prebuys"] == 1 and got["sends_last_hour"] == 1
    assert got["reverts"] == 0 and got["stop_reason"] is None and got["fills"] == 0
    built["dispatch"].prebuy = None
    assert beat_row() is None                                       # no worker: unknown, never zeros



def _raises(depositor, uid):
    raise OSError("ledger locked")


@pytest.mark.parametrize("registry_ok,reason", [
    (lambda depositor, uid: False, "registry_revoked"), (None, "registry_unavailable"), (_raises, "registry_error"),
])
def test_a_depositor_no_longer_verified_now_is_no_prebuy(tmp_path, registry_ok, reason):
    """Main's rule (2026-10-14, DepositProofs.registry_ok): the depositor must STILL be the uid's verified,
    conflict-free Solana identity when the deposit is used, not only when the roster was read."""
    seen = []

    def checked(depositor, uid):
        seen.append((depositor, uid))
        return registry_ok(depositor, uid)
    pipe, fetched, learned = _pipeline(tmp_path, registry_ok=None if registry_ok is None else checked)
    row = pipe.handle(DEPOSIT, SEEN_AT)
    assert row["prebuy_reason"] == reason and pipe.send.payloads == []
    assert seen in ([], [(DEPOSIT["depositor"], UID)])


# --- Alpha's decisions on Gamma's MEDIUMs (2026-10-14): warm only; defer and re-judge the source fill ----

def _accepting(sender):
    sender.send = lambda raw: sender.raws.append(raw) or ("accepted", None)


def _rejecting(sender):
    sender.send = lambda raw: sender.raws.append(raw) or ("rejected", "insufficient funds for gas * price + value")


def _source_fill():
    """The source's own fill on the lane, its snapshot read after our pre-buy's send (nonce past ours)."""
    p = fe._fresh_after_send()
    p["snapshot"] = dict(p["snapshot"], nonce=43)
    return p


def _reverts(pre, monkeypatch):
    """Our pre-buy's receipt: a canonical status-0 revert (it acquired nothing)."""
    from sniper.execution import evm_swap_executor as ex
    monkeypatch.setattr(ex, "_receipt_is_canonical", lambda *a, **k: True)
    pre._receipt = lambda tx, timeout_s, **k: {"transactionHash": tx, "status": "0x0", "blockNumber": "0x3f2",
                                               "transactionIndex": "0x1", "logs": []}
    pre.rpc_read = lambda method, params: {"hash": params[0]} if method == "eth_getTransactionByHash" else {
        "timestamp": "0x1", "number": params[0]}
    pre._revert_kind = lambda tx_hash, rec=None: "min_out"


def _lands(pre):
    """Our pre-buy's receipt books a position (as _settle's booking does)."""
    from sniper.persistence import db_memecoin as m
    from sniper.util.clock import real_utc_now_iso
    pre._receipt = lambda tx, timeout_s, **k: {"transactionHash": tx, "status": "0x1"}

    def settle(payload, rec, conn, pending_id, tx_hash, decimals, receipt, trace):
        attempt = conn.execute("SELECT attempt_id FROM memecoin_pending_entries WHERE id=?", (pending_id,)).fetchone()[0]
        now = real_utc_now_iso()
        position, _fill = m.finalize_pending_entry(conn, pending_id=pending_id, position_kwargs=dict(
            source_user_id="uid", source_handle="Degen", mint=fe.MINT, ticker="T", entry_price=2e-5,
            entry_notional=20.0, entry_fill_source="chain", entry_lag_s=0.0, their_size_usd=65.3, opened_at=now,
            execution_mode="real", network_id="4663", attempt_id=attempt), fill_kwargs=dict(
            kind="entry", price=2e-5, qty=1e6, notional=20.0, fill_source="chain", fraction=1.0, created_at=now,
            tx_sig=tx_hash, attempt_id=attempt, network_id="4663"))
        pre.fills += 1
        rec["_outcome"] = "filled"
        return {"status": "filled", "reason": "filled", "pending_id": pending_id, "tx_hash": tx_hash,
                "position_id": position}
    pre._settle = settle


def test_a_cold_mint_gets_no_prebuy_and_no_inline_read(armed):
    built, sender, net, ledger = armed
    pre = built["prebuy"].lane
    assert pre.inline_safety_fn is None and pre.inline_facts_fn is None       # no inline on this path
    built["cache"]._verdicts.pop(fe.MINT)
    rec = _run(built, _prebuy())
    assert rec["reason"] == "prebuy_skipped:cold_safety" and sender.raws == [] and _pending_rows(ledger) == []
    assert built["prebuy"].counts["prebuy_skipped:cold_safety"] == 1
    built["cache"]._verdicts[fe.MINT] = (True, None, time.time() - 3600, 0.0)   # stale is cold too
    assert _run(built, _prebuy())["reason"] == "prebuy_skipped:cold_safety"


def test_while_a_prebuy_is_in_flight_the_lane_leaves_the_source_fill_to_the_onchain_lane(armed):
    built, sender, net, ledger = armed
    built["prebuy"]._inflight = fe.MINT                    # offered, not finished
    p = _source_fill()
    rec = fe._go(built, p)
    assert rec["reason"] == "relay_prebuy_in_flight" and sender.raws == []
    assert fe._attempts(ledger) == [] and not lane.LANE_HOLDS.holds(p["tx_hash"], fe.MINT)
    built["prebuy"]._inflight = None
    assert fe._go(built, _source_fill())["reason"].startswith("rejected:")   # judged as today


def test_a_reverted_prebuy_then_the_source_fill_is_copied_by_the_lane(armed, monkeypatch):
    import json
    built, sender, net, ledger = armed
    pre = built["prebuy"].lane
    _accepting(sender)
    _reverts(pre, monkeypatch)
    rec = _run(built, _prebuy())
    assert rec["reason"] == "reverted", rec
    _rejecting(sender)
    src = fe._go(built, _source_fill())
    assert src["reason"].startswith("rejected:"), src                        # reached its send: copied
    beat = built["prebuy"].beat()
    assert beat["source_fill_rejudged_after_prebuy:reverted"] == 1
    assert "source_fill_refused_by_prebuy:reverted" not in beat
    lane.write_heartbeat(built["db_path"], built["dispatch"], built["cache"], built["paced"])
    [row] = _prebuy_rows(built)
    assert (row["source_fill_tx"], row["source_fill_lane"], row["source_fill_disposition"]) == (
        src["source_tx"], "rh_fast_lane", "rejudged_after_prebuy")
    assert json.loads(row["rec_json"])["reason"] == "reverted"


def test_a_landed_prebuy_then_the_source_fill_is_refused(armed):
    built, sender, net, ledger = armed
    pre = built["prebuy"].lane
    _accepting(sender)
    _lands(pre)
    assert _run(built, _prebuy())["reason"] == "filled"
    sends = len(sender.raws)
    src = fe._go(built, _source_fill())
    assert src["reason"].startswith("hook:rejected:one position per coin"), src
    assert len(sender.raws) == sends
    assert built["prebuy"].beat()["source_fill_refused_by_prebuy:filled"] == 1


def test_a_submitted_unknown_prebuy_then_the_source_fill_is_refused(armed):
    built, sender, net, ledger = armed
    pre = built["prebuy"].lane
    pre.lock_receipt_cap_s = pre.receipt_timeout_s = 0.05
    sender.send = lambda raw: sender.raws.append(raw) or ("ambiguous", "timeout")
    assert _run(built, _prebuy())["reason"] == "submitted_unknown"
    _rejecting(sender)
    src = fe._go(built, _source_fill())
    assert src["reason"].startswith(("hook:rejected:one position per coin",
                                     "hook:rejected:unresolved signed transaction on 4663")), src
    assert built["prebuy"].beat()["source_fill_refused_by_prebuy:submitted_unknown"] == 1


# --- the on-chain lane waits on a pre-buy in flight, by its attempt row's state ---------------------------

ONCHAIN_TX = "0x" + "5a" * 32


@pytest.fixture
def onchain_ledger():
    import sqlite3
    from scripts.research.onchain_wallet_watch import SCHEMA
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    m.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


def _onchain_now():
    from datetime import datetime, timezone
    return datetime(2026, 10, 14, 11, 21, 17, tzinfo=timezone.utc)


def _prebuy_in_flight(conn, *, mint=fe.MINT):
    """Our pre-buy mid-decision: its trace row (terminal NULL) and its reservation."""
    import json
    from datetime import timedelta
    from sniper.persistence import db_memecoin as m
    started = (_onchain_now() - timedelta(seconds=0.6)).isoformat()
    origin = "rhprebuy:4663:%s:%s" % (ORDER, mint)
    pid = m.reserve_pending(conn, mint=mint, source_user_id="uid", strategy="alpha_b", notional=20.0,
                            reserved_at=started, network_id="4663", origin_event_id=origin,
                            attempt_id="prebuy", signal_source="relay_prebuy")
    conn.execute("INSERT INTO memecoin_entry_attempts(attempt_id, origin_event_id, network_id, mint, source_user_id, "
                 "signal_source, delivery_lane, decision_started_at, trace_json) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("prebuy", origin, "4663", mint, "uid", "relay_prebuy", "rh_relay_prebuy", started,
                  json.dumps({"stages": {}, "submission_events": []})))
    return pid


def _source_detection(conn):
    import json
    from datetime import timedelta
    arrival = (_onchain_now() - timedelta(seconds=0.1)).isoformat()
    conn.execute("INSERT INTO onchain_wallet_detections(signature,source_user_id,handle,address,network_id,"
                 "mint,source_slot,source_size_usd,source_price,detected_at,created_at,source_valuation_json) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("4663:%s:%s" % (ONCHAIN_TX, fe.MINT), "uid", "Degen", LANE_WALLET, "4663", fe.MINT, 1000, 65.3,
                  2e-5, arrival, arrival, json.dumps({})))


def _onchain_sweep(conn, calls, *, cursor=0, deferred, now=None):
    from sniper.services import memecoin_onchain_lane as onchain
    return onchain.sweep_onchain_detections(
        conn, lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "ordinary verdict"},
        cursor=cursor, now_fn=lambda: (now or _onchain_now()).isoformat(), max_age_s=45, network_id="4663",
        deferred=deferred, approved_ids_fn=lambda _conn, net: {"uid"} if net == "4663" else set())


@pytest.fixture
def no_lane_hold(monkeypatch):
    monkeypatch.setattr(lane, "LANE_HOLDS", lane.LaneHolds(bound_s=1.0, clock=lambda: 0.0, wake_fn=lambda: None))


@pytest.mark.parametrize("ending", ["aborted", "reverted"])
def test_the_onchain_lane_waits_on_a_prebuy_in_flight_then_copies_the_source_fill(onchain_ledger, no_lane_hold, prebuy_on,
                                                                                  ending, monkeypatch):
    from sniper.persistence import db_memecoin as m
    pid = _prebuy_in_flight(onchain_ledger)
    _source_detection(onchain_ledger)
    calls, deferred = [], {}
    cursor, results = _onchain_sweep(onchain_ledger, calls, deferred=deferred)
    assert calls == [] and results[0]["result"] == {"status": "deferred", "reason": "relay_prebuy_in_flight"}
    cursor, results = _onchain_sweep(onchain_ledger, calls, cursor=cursor, deferred=deferred)
    assert calls == [] and results == [] and len(deferred) == 1                # still in flight: waiting
    assert m.release_pending(onchain_ledger, pid, "rh_prebuy_" + ("lock_busy" if ending == "aborted" else "reverted"))
    onchain_ledger.execute("UPDATE memecoin_entry_attempts SET terminal_status=?, terminal_reason=?, completed_at=? "
                           "WHERE attempt_id='prebuy'", ("aborted" if ending == "aborted" else "failed", ending,
                                                         _onchain_now().isoformat()))
    noted = []

    class Worker:
        def note_source_fill(self, *args):
            noted.append(args)
    monkeypatch.setattr(rp, "ACTIVE", {"4663": Worker()})
    _cursor, results = _onchain_sweep(onchain_ledger, calls, cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and results[0]["result"]["reason"] == "ordinary verdict" and not deferred
    # The verdict is noted for the pre-buy's source-fill counters, with the source's own transaction.
    assert noted == [(fe.MINT, "uid", ONCHAIN_TX, "onchain_watch", "ordinary verdict")]


def test_a_prebuy_on_another_coin_never_holds_the_source_fill(onchain_ledger, no_lane_hold, prebuy_on):
    _prebuy_in_flight(onchain_ledger, mint="0x" + "4" * 40)
    _source_detection(onchain_ledger)
    calls = []
    _cursor, results = _onchain_sweep(onchain_ledger, calls, deferred={})
    assert len(calls) == 1 and results[0]["result"]["reason"] == "ordinary verdict"


def test_a_wait_on_a_prebuy_that_outlives_the_detection_expires_by_its_own_name(onchain_ledger, no_lane_hold, prebuy_on):
    from datetime import timedelta
    _prebuy_in_flight(onchain_ledger)
    _source_detection(onchain_ledger)
    calls, deferred = [], {}
    cursor, _results = _onchain_sweep(onchain_ledger, calls, deferred=deferred)
    _cursor, results = _onchain_sweep(onchain_ledger, calls, cursor=cursor, deferred=deferred,
                                      now=_onchain_now() + timedelta(seconds=46))
    assert calls == [] and not deferred
    assert results[0]["result"] == {"status": "skipped", "reason": "relay_prebuy_wait_expired"}


# --- Gamma's LOWs on cd0865d18 ---------------------------------------------------------------------------

def test_a_request_recipient_that_is_not_the_beneficiary_refuses(prebuy_on):
    """LOW 1: the request's recipient was only covered through the beneficiary."""
    assert source_initiation_refusal(_prebuy_ev(request_recipient="0x" + "11" * 20), "4663") is not None


@pytest.mark.parametrize("asset", ["0x" + "00" * 20, "weth", "stable"])
def test_a_request_buying_a_quote_asset_is_never_polled_further(tmp_path, asset):
    """LOW 2: native, WETH or a stable as currencyOut costs no learn, warm or pre-buy."""
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.settings import memecoin as cfg
    out = {"weth": CHAIN_QUOTE["4663"]["token"], "stable": (list(cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES.get("4663", ()))
                                                            or ["0x" + "5" * 40])[0]}.get(asset, asset)
    answer = _token_request()
    _meta(answer["requests"][0])["currencyOut"]["currency"]["address"] = out
    pipe, fetched, learned = _pipeline(tmp_path, answers=[answer], prewarm=True)
    pipe.quote_assets = {4663: frozenset({"0x" + "00" * 20, CHAIN_QUOTE["4663"]["token"].lower(), out.lower()})}
    row = pipe.handle(DEPOSIT, SEEN_AT)
    assert row["poll_reason"] == "quote_asset_out" and learned == [] and pipe.send.payloads == []



def test_with_the_knob_off_the_onchain_lane_never_waits_on_a_prebuy_row(onchain_ledger, no_lane_hold):
    _prebuy_in_flight(onchain_ledger)
    _source_detection(onchain_ledger)
    calls = []
    _cursor, results = _onchain_sweep(onchain_ledger, calls, deferred={})
    assert len(calls) == 1 and results[0]["result"]["reason"] == "ordinary verdict"


# --- J7 during another entry's reservation: wait for its verdict, then judge this event ---

J7_NOW = "2026-10-14T11:21:16+00:00"


@pytest.fixture
def retry_lane(monkeypatch):
    from sniper.services import memecoin_real_leg as RL
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_INTERVAL_S", 6.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_MAX_ATTEMPTS", 12)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 1.0)
    monkeypatch.setattr(RL, "real_entries_allowed", lambda kill_path=None: True)


def _j7_ledger():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _j7_hook(executed, *, quote_fn=None):
    """Run the real leg and EVM risk gate before the inert quote/executor seam."""
    from sniper.services import memecoin_real_leg as RL
    from sniper.services.memecoin_evm_risk import build_evm_risk_context, evm_entry_allowed
    from sniper.services.memecoin_risk import risk_refusal_result

    def evm_entry(conn, ev, **_kw):
        risk = build_evm_risk_context(conn, usdc_balance=300.0, native_balance=1.0,
                                     day="2026-10-14", network_id="4663")
        decision = evm_entry_allowed(risk, mint=ev.mint, trade_size=risk.trade_size_usd)
        if not decision.allowed:
            return risk_refusal_result(decision)
        if quote_fn is not None:
            from sniper.execution.evm_swap_executor import _quote_over_source_refusal
            reason = _quote_over_source_refusal("4663", quote_fn(ev), ev.their_price)
            if reason:
                return {"status": "skipped_chase", "reason": reason}
        executed.append(ev.fomo_event_id)
        return {"status": "filled"}

    def hook(conn, ev):
        return RL.real_entry(
            conn, ev, day="2026-10-14", usdc_balance=300.0, sol_balance=0.0, now_iso=J7_NOW, trade_size=10.0,
            token_safety_fn=lambda *a, **k: (True, {}), execute_fn=lambda *a, **k: {}, evm_allowlist={"4663"},
            resolve_fn=lambda _m: "4663", evm_entry_fn=evm_entry)
    return hook


@pytest.mark.parametrize("holder", ["relay_prebuy", "rh_fast_lane", "onchain_watch"])
def test_j7_inside_an_entry_reservation_is_re_asked_and_copies_after_the_abort(retry_lane, holder):
    from dataclasses import replace
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_entry_retry_lane import _ev
    c, executed = _j7_ledger(), []
    pid = m.reserve_pending(c, mint=fe.MINT, source_user_id="uid", strategy="alpha_b", notional=20.0,
                            reserved_at=J7_NOW, network_id="4663", signal_source=holder,
                            origin_event_id="rhprebuy:4663:%s:%s" % (ORDER, fe.MINT))     # reserved, unsigned
    ev = replace(_ev(fid="j7-source", mint=fe.MINT), network_id="4663")
    first = _j7_hook(executed)(c, ev)
    assert first["reason"] == "one position per coin: another entry attempt was in progress at decision time"
    assert first["duplicate_blocker"]["reservation_id"] == pid
    assert executed == [] and R._maybe_enqueue_retry(c, ev, first, J7_NOW)
    due = lambda at: m.due_entry_retries(c, at, window_s=60, max_attempts=12)  # noqa: E731
    assert not due("2026-10-14T11:21:16.900000+00:00") and due("2026-10-14T11:21:17+00:00")   # 1.0 s
    R.run_entry_retry_lane(c, _j7_hook(executed), "2026-10-14T11:21:17+00:00")
    assert executed == []                       # still reserved: refused again, re-asked again
    assert due("2026-10-14T11:21:18+00:00")
    assert m.release_pending(c, pid, "rh_prebuy_lock_busy")                          # the pre-buy aborts unsigned
    R.run_entry_retry_lane(c, _j7_hook(executed), "2026-10-14T11:21:18+00:00")
    assert executed == ["j7-source"]            # the re-ask copies


@pytest.mark.parametrize("signed", [False, True])
def test_a_temporary_reservation_still_blocks_every_retry_before_quote_work(retry_lane, signed):
    from dataclasses import replace
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_entry_retry_lane import _ev
    c, executed = _j7_ledger(), []
    pid = m.reserve_pending(c, mint=fe.MINT, source_user_id="uid", strategy="alpha_b", notional=20.0,
                            reserved_at=J7_NOW, network_id="4663", signal_source="onchain_watch")
    if signed:
        m.set_pending_broadcast(c, pid, "0x" + "ab" * 32, 17, J7_NOW)
    ev = replace(_ev(fid="j7-source", mint=fe.MINT), network_id="4663")
    hook = _j7_hook(executed, quote_fn=lambda _ev: pytest.fail("a reserved coin cannot reach the quote"))
    assert R._maybe_enqueue_retry(c, ev, hook(c, ev), J7_NOW)
    R.run_entry_retry_lane(c, hook, "2026-10-14T11:21:17+00:00")
    assert executed == [] and m.pending_row(c, pid)["status"] == "reserved"
    assert m.due_entry_retries(c, "2026-10-14T11:21:18+00:00", window_s=60, max_attempts=12)


def test_an_owner_fill_ends_the_temporary_reservation_retry_without_another_quote(retry_lane):
    from dataclasses import replace
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_entry_retry_lane import _ev
    c, executed = _j7_ledger(), []
    pid = m.reserve_pending(c, mint=fe.MINT, source_user_id="uid", strategy="alpha_b", notional=20.0,
                            reserved_at=J7_NOW, network_id="4663", signal_source="onchain_watch")
    ev = replace(_ev(fid="j7-source", mint=fe.MINT), network_id="4663")
    hook = _j7_hook(executed, quote_fn=lambda _ev: pytest.fail("an open position cannot reach the quote"))
    assert R._maybe_enqueue_retry(c, ev, hook(c, ev), J7_NOW)
    m.open_position(c, source_user_id="uid", source_handle="owner", mint=fe.MINT, ticker="SHI",
                    entry_price=ev.their_price, entry_notional=20.0, entry_fill_source="chain",
                    entry_lag_s=1.0, their_size_usd=1000.0, opened_at=J7_NOW, strategy="alpha_b",
                    execution_mode="real", network_id="4663")
    assert m.confirm_pending(c, pid)
    result = hook(c, ev)
    assert result["duplicate_blocker"]["kind"] == "position"
    assert not R._is_transient_entry_miss(result)
    R.run_entry_retry_lane(c, hook, "2026-10-14T11:21:17+00:00")
    assert executed == [] and not m.due_entry_retries(
        c, "2026-10-14T11:21:18+00:00", window_s=60, max_attempts=12)


def test_temporary_reservation_release_still_requires_a_fresh_source_price_cap(retry_lane, monkeypatch):
    from dataclasses import replace
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as R
    from sniper.settings import memecoin as cfg
    from test_memecoin_entry_retry_lane import _ev
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_RETRY_ENABLED", False)
    monkeypatch.setenv("MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663", "30")
    c, executed, quoted = _j7_ledger(), [], []
    pid = m.reserve_pending(c, mint=fe.MINT, source_user_id="uid", strategy="alpha_b", notional=20.0,
                            reserved_at=J7_NOW, network_id="4663", signal_source="onchain_watch")
    ev = replace(_ev(fid="j7-source", mint=fe.MINT), network_id="4663", their_price=0.0000440156848777,
                 created_at=J7_NOW, received_at_normalized=J7_NOW,
                 clock_domain="observed_real_utc", receipt_clock={"monotonic_ns": 123, "sequence": 4})

    def quote(e):
        quoted.append(e)
        return 0.0000576147544473

    hook = _j7_hook(executed, quote_fn=quote)
    assert R._maybe_enqueue_retry(c, ev, hook(c, ev), J7_NOW)
    assert quoted == [] and m.release_pending(c, pid, "quote_over_source")
    R.run_entry_retry_lane(c, hook, "2026-10-14T11:21:17+00:00")
    assert executed == [] and len(quoted) == 1
    assert quoted[0].their_price == ev.their_price and quoted[0].receipt_clock == ev.receipt_clock
    assert quoted[0].received_at_normalized == ev.received_at_normalized
    assert quoted[0].created_at == ev.created_at and quoted[0].fomo_event_id == ev.fomo_event_id
    assert not m.due_entry_retries(c, "2026-10-14T11:21:18+00:00", window_s=60, max_attempts=12)
