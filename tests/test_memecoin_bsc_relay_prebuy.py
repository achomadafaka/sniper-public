"""BSC 56 Relay pre-buy: spec docs/superpowers/specs/2026-10-14-bsc-relay-prebuy-design.md,
plan docs/superpowers/plans/2026-10-14-bsc-relay-prebuy.md."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sniper.services import memecoin_rh_relay_prebuy as rp  # noqa: E402
from sniper.services.memecoin_source_initiation import source_initiation_refusal  # noqa: E402

UID = "bb2ab76d-f022-53a1-820e-a5f46522576b"
WALLET56 = "0x" + "a5" * 20
MINT56 = "0x71b58f07e2962de4691f3da80d54cfc8cfcc7777"
ORDER = "0x02effa2435" + "0" * 54
DEPOSIT = dict(order_id=ORDER, slot=456820000,
               signature="aiTu6q8dS7tyi3a9bsMaRMryiqa9a2L4479YhA3mmeiiNUhywk51nxszNXcb9acKCVjZ6ZPA2gX9Km3ZLGy5qgo",
               depositor="8sfbKCYFvKuzG6vE5dAyHrSVxEgRWgMrcjUEvfhYr4ng", amount_atomic=4_000_000_000,
               uids=(UID,), arrived=1791983187.947)


def _found(chain=56, **over):
    found = dict(mint=MINT56, wallet=WALLET56, uid=UID, request_id="0x" + "17" * 32, deposit=DEPOSIT,
                 out_chain=chain, in_amount=DEPOSIT["amount_atomic"])
    found.update(over)
    return found


def _ev(chain="56", **over):
    proof = rp.prebuy_initiation(_found(int(chain)))
    ev = SimpleNamespace(signal_source="relay_prebuy", mint=MINT56, source_user_id=UID, source_wallet=WALLET56,
                         fomo_event_id="rhprebuy:%s:%s:%s" % (chain, ORDER, MINT56), source_block_number=None,
                         source_initiation=proof)
    for key, value in over.items():
        if key in proof:
            proof[key] = value
        else:
            setattr(ev, key, value)
    return ev


@pytest.fixture
def bsc_on(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_RELAY_PREBUY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREBUY", False)


# --- Task 1: the knob, and the initiation proof on 56 -----------------------------------------------------

def test_the_bsc_knob_defaults_off():
    from sniper.settings import memecoin as cfg
    assert 'os.getenv(\n    "MEMECOIN_BSC_RELAY_PREBUY", "0")' in Path(cfg.__file__).read_text()


def test_a_56_proof_binds_56(bsc_on):
    proof = rp.prebuy_initiation(_found(56))
    assert proof["network_id"] == "56" and proof["request_out_chain"] == 56
    assert source_initiation_refusal(_ev("56"), "56") is None


def test_the_56_proof_needs_the_56_knob(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_RELAY_PREBUY", False)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREBUY", True)              # 4663's knob alone
    assert source_initiation_refusal(_ev("56"), "56") == "wallet_initiation_unproved"


def test_a_4663_proof_on_56_and_a_56_proof_on_4663_refuse(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_RELAY_PREBUY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREBUY", True)
    assert source_initiation_refusal(_ev("4663"), "4663") is None              # control
    assert source_initiation_refusal(_ev("4663"), "56") is not None            # a 4663 proof judged on 56
    assert source_initiation_refusal(_ev("56"), "4663") is not None            # and the reverse
    assert source_initiation_refusal(_ev("56", network_id="4663"), "56") is not None
    assert source_initiation_refusal(_ev("56", request_out_chain=4663), "56") is not None


@pytest.mark.parametrize("over", [
    dict(beneficiary="0x" + "11" * 20), dict(mint="0x" + "22" * 20), dict(source_user_id="other"),
    dict(origin_identity_user_id="other"), dict(order_id="0x" + "99" * 32), dict(request_in_amount=1),
    dict(origin_network_id="8453"), dict(proof_version="relay-deposit-processed-v1"),
    dict(request_recipient="0x" + "11" * 20),
    dict(fomo_event_id="rhprebuy:4663:%s:%s" % (ORDER, MINT56)), dict(fomo_event_id="bscfast:56:%s:%s" % (ORDER, MINT56)),
    dict(signal_source="bsc_fast_lane"),
])
def test_any_mismatch_refuses_on_56(bsc_on, over):
    assert source_initiation_refusal(_ev("56", **over), "56") is not None


def test_other_chains_stay_refused(bsc_on):
    assert source_initiation_refusal(_ev("56"), "8453") is not None


# --- Task 2: the feed side routes a 56 match to the BSC lane, unpriced ------------------------------------

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SEEN_AT = "2026-10-14T13:06:27.947000+00:00"


def _request56(chain=56, recipient=WALLET56, **over):
    """Relay's v2 shape for bigwarzeth's order (2026-10-14), unsettled, bound for 56."""
    request = {"id": "0x" + "17" * 32, "status": "pending", "recipient": recipient,
               "createdAt": "2026-10-14T13:06:28.300Z",
               "data": {"inTxs": [], "outTxs": [], "metadata": {
                   "recipient": recipient,
                   "currencyIn": {"currency": {"chainId": 792703809, "address": USDC, "decimals": 6},
                                  "amount": str(DEPOSIT["amount_atomic"])},
                   "currencyOut": {"currency": {"chainId": chain, "address": MINT56, "decimals": 18},
                                   "amount": "1"}}}}
    request.update(over)
    return {"requests": [request]}


class _Sent:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, address, payload):
        self.calls.append((address, payload))
        return self.ok


def _pipeline56(tmp_path, *, answers=None, watched=None, registry_ok=lambda depositor, uid: True, bsc=True):
    answers = list(answers or [_request56()])
    fetched, learned = [], []

    def fetch(url):
        fetched.append(url)
        return answers.pop(0)
    watched = watched if watched is not None else {4663: {}, 56: {WALLET56: (UID, "bigwarzeth")}}
    poller = rp.RequestPoller(fetch=fetch, watched_fn=lambda: watched, clock=lambda: 0.0, sleep=lambda s: None)
    sent4663, sent56 = _Sent(), _Sent()
    pipe = rp.RelayPipeline(poller=poller, tape=None, live={}, learn=learned.append, send=sent4663,
                            head_fn=lambda: None, gap_fn=lambda lo, hi: False, watched_fn=lambda: watched,
                            db_path=str(tmp_path / "feed.db"), address="\0rh", prewarm=True, prebuy=True,
                            clip_usd=20.0, max_impact_pct=5.0, now_fn=lambda: 1000.0,
                            now_iso=lambda: "2026-10-14T13:06:28.400000+00:00", registry_ok=registry_ok,
                            clock=lambda: 1791983188.4, bsc_address="\0bsc" if bsc else None,
                            bsc_send=sent56 if bsc else None)
    return pipe, fetched, learned, sent4663, sent56


def test_a_56_request_reaches_the_bsc_lane_as_one_unpriced_datagram(tmp_path, bsc_on):
    pipe, fetched, learned, sent4663, sent56 = _pipeline56(tmp_path)
    row = pipe.handle(DEPOSIT, SEEN_AT)
    assert row["prebuy_reason"] == "sent_56" and len(fetched) == 1
    assert learned == [] and sent4663.calls == []                # nothing on 4663's tape or lane
    [(address, payload)] = sent56.calls
    assert address == "\0bsc" and payload["kind"] == "relay_prebuy_v1" and payload["chain"] == "56"
    assert payload["tx_hash"] == ORDER and payload["wallet"] == WALLET56 and payload["mint"] == MINT56
    assert payload["paid_usd"] == 4000.0 and payload["arrival_utc"] == SEEN_AT
    assert not {"pool", "key", "cap_num", "spot_wei_num"} & set(payload)          # unpriced
    ev = SimpleNamespace(signal_source="relay_prebuy", mint=MINT56, source_user_id=UID, source_wallet=WALLET56,
                         fomo_event_id="rhprebuy:56:%s:%s" % (ORDER, MINT56), source_block_number=None,
                         source_initiation=payload["source_initiation"])
    assert source_initiation_refusal(ev, "56") is None                             # the runner's own check
    import json
    json.dumps(payload)


def test_a_56_request_without_the_56_roster_is_not_enabled(tmp_path):
    # Armed on 4663 (so polled), the request bound for 56 while 56's pre-buy is off: no hand-off.
    pipe, fetched, learned, sent4663, sent56 = _pipeline56(tmp_path, watched={4663: {"0x" + "ef" * 20: (UID, "x")}})
    row = pipe.handle(DEPOSIT, SEEN_AT)
    assert row["poll_reason"] == "destination_not_enabled" and sent56.calls == [] and sent4663.calls == []


@pytest.mark.parametrize("watched,chain", [
    ({4663: {WALLET56: (UID, "x")}, 56: {}}, 56),          # the 56 recipient is only in the 4663 roster
    ({4663: {}, 56: {WALLET56: (UID, "x")}}, 4663),        # a 4663 request whose recipient is only on 56
])
def test_a_wallet_from_the_other_chain_s_roster_is_refused(tmp_path, bsc_on, watched, chain):
    pipe, fetched, learned, sent4663, sent56 = _pipeline56(tmp_path, answers=[_request56(chain=chain)],
                                                           watched=watched)
    row = pipe.handle(DEPOSIT, SEEN_AT)
    assert row["poll_reason"] == "recipient_mismatch" and sent56.calls == [] and sent4663.calls == []


@pytest.mark.parametrize("registry_ok,reason", [(lambda d, u: False, "registry_revoked"), (None, "registry_unavailable")])
def test_a_56_depositor_no_longer_verified_is_not_handed_off(tmp_path, bsc_on, registry_ok, reason):
    pipe, fetched, learned, sent4663, sent56 = _pipeline56(tmp_path, registry_ok=registry_ok)
    assert pipe.handle(DEPOSIT, SEEN_AT)["prebuy_reason"] == reason and sent56.calls == []


def test_a_56_request_buying_wbnb_is_a_quote_asset(tmp_path, bsc_on):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    answer = _request56()
    answer["requests"][0]["data"]["metadata"]["currencyOut"]["currency"]["address"] = bsc.WBNB
    pipe, fetched, learned, sent4663, sent56 = _pipeline56(tmp_path, answers=[answer])
    pipe.quote_assets = {56: frozenset({bsc.WBNB})}
    assert pipe.handle(DEPOSIT, SEEN_AT)["poll_reason"] == "quote_asset_out" and sent56.calls == []


@pytest.mark.parametrize("rh,bsc,chains", [(True, False, [4663]), (False, True, [56]), (True, True, [56, 4663]),
                                           (False, False, [])])
def test_the_poller_s_rosters_are_the_chains_whose_prebuy_is_on(monkeypatch, rh, bsc, chains):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREBUY", rh)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREWARM", False)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_RELAY_PREBUY", bsc)
    rosters = rp.relay_rosters(lambda chain: {"0x" + chain[:2].rjust(40, "0"): ("uid-" + chain, "h")})
    assert sorted(rosters) == chains
    assert all(next(iter(roster.values()))[0] == "uid-%s" % chain for chain, roster in rosters.items())


def test_start_decide_starts_the_poller_for_56_alone(tmp_path, monkeypatch):
    import sqlite3
    import threading
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_rh_deposit_proof as dp
    from sniper.services import memecoin_rh_fast_decide as fast
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_decide import _tape_db
    _tape_db(tmp_path / "t.db", []).commit()
    ledger = sqlite3.connect(str(tmp_path / "ledger.db"))
    m.ensure_memecoin_tables(ledger)
    ledger.commit()
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREBUY", False)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREWARM", False)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_RELAY_PREBUY", True)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    captured = {}
    monkeypatch.setattr(dp, "start_deposit_proofs", lambda **kw: captured.update(kw) or (None, None, None, ["s"]))
    fast.start_decide(db_path=str(tmp_path / "d.db"), tape_path=str(tmp_path / "t.db"), log=lambda *a: None,
                      ledger_path=str(tmp_path / "ledger.db"), watched_fn=lambda: {56: {}}, head_fn=lambda: 1)
    assert callable(captured["on_proof"])


# --- Task 3: one batch on the lane host; the cheapest V2 route ----------------------------------------

from fractions import Fraction  # noqa: E402

USDT = "0x55d398326f99059ff775485246999027b3197955"
CLIP = 20 * 10 ** 18 // 600                     # $20 at $600 BNB


def _word(r0, r1):
    return "0x" + format(r0, "064x") + format(r1, "064x") + format(1791983187, "064x")


class _Host:
    """The lane host's batch: answers by pair, or a scripted failure."""
    def __init__(self, reserves, *, block=127599760, fail=None, decimals="0x" + format(18, "064x")):
        self.reserves, self.block, self.fail, self.calls = reserves, block, fail, []
        self.decimals = decimals

    def batch(self, calls):
        self.calls.append(calls)
        if self.fail == "unsupported":
            return None
        if self.fail == "raise":
            raise RuntimeError("lane host batch: replies do not match the calls")
        out = []
        for method, params in calls:
            if method == "eth_blockNumber":
                out.append(hex(self.block))
            elif params[0]["data"] == "0x313ce567":
                out.append(self.decimals)
            else:
                state = self.reserves.get(params[0]["to"])
                out.append(_word(*state) if state else "0x")
        if self.fail == "malformed":
            out[2] = "0x1234"
        return out


def _pairs():
    from sniper.services import memecoin_bsc_fast_lane as bsc
    return bsc.our_pair(MINT56, bsc.WBNB), bsc.our_pair(MINT56, USDT), bsc.our_pair(bsc.WBNB, USDT)


def _ordered(mint_reserve, quote_reserve, quote):
    """(reserve0, reserve1) by token order on the pair of MINT56 and `quote`."""
    return (quote_reserve, mint_reserve) if int(MINT56, 16) > int(quote, 16) else (mint_reserve, quote_reserve)


def test_one_batch_reads_the_block_and_every_candidate_pair():
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    direct, coin_usdt, hop = _pairs()
    host = _Host({direct: _ordered(10 ** 27, 50 * 10 ** 18, "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c")})
    state, why = bp.bsc_spot_batch(host, MINT56, (USDT,))
    assert why is None and state["block"] == 127599760 and state["decimals"] == 18 and len(host.calls) == 1
    assert {params[0]["to"] for method, params in host.calls[0][2:]} == {direct, coin_usdt, hop}
    assert host.calls[0][1] == ("eth_call", [{"to": MINT56, "data": "0x313ce567"}, "latest"])
    assert set(state["reserves"]) == {direct}            # a pair with no code ("0x") is absent


@pytest.mark.parametrize("word", ["0x", "0x" + format(77, "064x")])
def test_a_coin_without_readable_decimals_is_no_buy(word):
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    assert bp.bsc_spot_batch(_Host({}, decimals=word), MINT56, (USDT,)) == (None, "no_decimals")


@pytest.mark.parametrize("fail,reason", [("unsupported", "batch_unsupported"), ("raise", "spot_read_failed"),
                                         ("malformed", "spot_read_failed")])
def test_a_host_that_cannot_answer_the_batch_means_no_buy(fail, reason):
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    assert bp.bsc_spot_batch(_Host({}, fail=fail), MINT56, (USDT,)) == (None, reason)


def test_a_wbnb_pair_gives_the_spot_cap_and_the_lane_s_own_min_out():
    from sniper.services import memecoin_bsc_fast_lane as bsc
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    direct, _coin_usdt, _hop = _pairs()
    reserves = {direct: _ordered(10 ** 27, 50 * 10 ** 18, bsc.WBNB)}
    got, why = bp.bsc_prebuy_route(MINT56, 127599760, reserves, clip_wei=CLIP, max_impact_pct=5.0, hop_quotes=(USDT,))
    assert why is None and got["pool"] == direct and got["quote"] == bsc.WBNB and got["hop"] is None
    spot = Fraction(50 * 10 ** 18, 10 ** 27)
    assert Fraction(got["cap_num"], got["cap_den"]) == spot * Fraction(105, 100)
    assert 0 < got["impact_pct"] <= 5 and got["seq"] == 127599760 and got["state_kind"] == "prebuy_lane_host"
    payload = dict(got, mint=MINT56)
    # The lane's min_out is the stricter of the cap rule and the slippage rule: with a loose slippage the
    # cap binds (never over spot x 1.05); with the configured one the spot state still fills.
    assert bsc.min_out_for(CLIP, payload, slippage_pct=50) == int(Fraction(CLIP) / Fraction(got["cap_num"],
                                                                                          got["cap_den"]))
    assert bsc.expected_out_for(CLIP, payload) >= bsc.min_out_for(CLIP, payload)


def test_a_usdt_hop_route_is_priced_through_the_hop():
    from sniper.services import memecoin_bsc_fast_lane as bsc
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    direct, coin_usdt, hop = _pairs()
    wbnb1 = int(bsc.WBNB, 16) > int(USDT, 16)
    hop_state = (600_000 * 10 ** 18, 1000 * 10 ** 18) if wbnb1 else (1000 * 10 ** 18, 600_000 * 10 ** 18)
    reserves = {coin_usdt: _ordered(10 ** 27, 30_000 * 10 ** 18, USDT), hop: hop_state}
    got, why = bp.bsc_prebuy_route(MINT56, 1, reserves, clip_wei=CLIP, max_impact_pct=5.0, hop_quotes=(USDT,))
    assert why is None and got["pool"] == coin_usdt and got["quote"] == USDT
    assert got["hop"] == {"pair": hop, "r_wbnb": 1000 * 10 ** 18, "r_quote": 600_000 * 10 ** 18,
                          "source": "lane_host", "height": 1}
    assert bsc.buy_path(dict(got, mint=MINT56)) == [bsc.WBNB, USDT, MINT56]
    assert bsc.expected_out_for(CLIP, dict(got, mint=MINT56)) >= bsc.min_out_for(CLIP, dict(got, mint=MINT56),
                                                                                slippage_pct=0)


def test_with_two_routes_the_cheaper_one_wins_and_both_over_the_cap_is_no_buy():
    from sniper.services import memecoin_bsc_fast_lane as bsc
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    direct, coin_usdt, hop = _pairs()
    wbnb1 = int(bsc.WBNB, 16) > int(USDT, 16)
    hop_state = (600_000 * 10 ** 18, 1000 * 10 ** 18) if wbnb1 else (1000 * 10 ** 18, 600_000 * 10 ** 18)
    thin_direct = _ordered(10 ** 25, 5 * 10 ** 17, bsc.WBNB)                   # 0.5 BNB of depth
    deep_usdt = _ordered(10 ** 27, 30_000 * 10 ** 18, USDT)
    got, why = bp.bsc_prebuy_route(MINT56, 1, {direct: thin_direct, coin_usdt: deep_usdt, hop: hop_state},
                                   clip_wei=CLIP, max_impact_pct=50.0, hop_quotes=(USDT,))
    assert why is None and got["pool"] == coin_usdt                         # the deeper route
    assert bp.bsc_prebuy_route(MINT56, 1, {direct: thin_direct}, clip_wei=CLIP, max_impact_pct=5.0,
                               hop_quotes=(USDT,)) == (None, "impact_over_cap")


@pytest.mark.parametrize("reserves", [{}, "zero"])
def test_no_v2_pair_is_no_v2_route(reserves):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    if reserves == "zero":
        direct, _coin_usdt, _hop = _pairs()
        reserves = {direct: (0, 0)}
    assert bp.bsc_prebuy_route(MINT56, 1, reserves, clip_wei=CLIP, max_impact_pct=5.0,
                               hop_quotes=(USDT,)) == (None, "no_v2_route")
    assert bsc  # noqa


def test_bnb_usd_comes_from_the_tape_quote_inside_its_ttl(tmp_path):
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    from sniper.services import memecoin_bsc_v2_live as tape
    path = tmp_path / "v2.db"
    c = tape.connect(path)
    tape.record_quote(c, 612.5, 1000.0)
    c.close()
    assert bp.bnb_usd_now(1030.0, path=path) == (612.5, 1000.0)
    assert bp.bnb_usd_now(1000.0 + tape.QUOTE_TTL_S + 1, path=path) == (None, None)
    assert bp.bnb_usd_now(1000.0, path=tmp_path / "absent.db") == (None, None)


# --- Task 4: the runner's pre-buy lane and worker, on the live 56 harness --------------------------------

import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

from test_memecoin_bsc_fast_lane_live import live56  # noqa: E402,F401

H_MINT = "0x" + "77" * 20                     # the harness coin: a WBNB pair at 1e-6 BNB a coin, ~$20k cap
H_WALLET = "0x" + "6f" * 20
H_ORDER = "0x" + "4e" * 32


def _h_found(order=H_ORDER):
    deposit = dict(DEPOSIT, order_id=order, amount_atomic=500_000_000, uids=("uid",))
    return dict(mint=H_MINT, wallet=H_WALLET, uid="uid", handle="w", request_id="0x" + "17" * 32, deposit=deposit,
                out_chain=56, in_amount=500_000_000, status="pending")


def _h_payload(order=H_ORDER):
    from sniper.util.clock import real_utc_now_iso
    return rp.bsc_prebuy_payload(_h_found(order), arrival_utc=real_utc_now_iso(), sent_epoch=time.time())


def _h_reserves():
    from sniper.services import memecoin_bsc_fast_lane as bsc
    pair = bsc.our_pair(H_MINT, bsc.WBNB)
    coin1 = int(H_MINT, 16) > int(bsc.WBNB, 16)
    return {pair: (10 ** 20, 10 ** 26) if coin1 else (10 ** 26, 10 ** 20)}


@pytest.fixture
def armed56(bsc_on, live56, tmp_path, monkeypatch):
    """The live 56 lane with its pre-buy worker: the tape's BNB price, the 4663 feed's heartbeat (this
    pid), and the lane host's batch answering the harness coin's WBNB pair."""
    from sniper.services import memecoin_bsc_v2_live as tape
    from sniper.util.clock import real_utc_now
    lane, ledger, net, sender, prime = live56
    worker = lane["prebuy"]
    assert worker is not None and lane["dispatch"].prebuy is worker
    c = tape.connect()
    tape.record_quote(c, 600.0, real_utc_now())
    c.close()
    beat = Path(lane["dispatch"].rh_beat)
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(json.dumps({"ts": time.time(), "pid": os.getpid()}))
    worker.lane_host = _Host(_h_reserves())
    return lane, ledger, net, sender, prime, worker


def _go56(lane, payload, pid=None):
    return lane["dispatch"].on_datagram(payload, os.getpid() if pid is None else pid, os.getuid(), time.time())


def _pending56(ledger):
    return [dict(r) for r in ledger.execute(
        "SELECT signal_source, network_id, status, resolution_reason, origin_event_id FROM memecoin_pending_entries")]


def test_bsc_start_and_off_path_watch_apply_the_operator_stop_file(armed56, monkeypatch, tmp_path):
    import threading
    from sniper.services import memecoin_bsc_fast_lane as bsc
    from sniper.services import memecoin_rh_fast_lane as rh
    lane, ledger, net, sender, prime, worker = armed56
    stop = tmp_path / "operator.stop"
    stop.touch()
    monkeypatch.setattr(rh, "stop_file_path", lambda: str(stop))
    monkeypatch.setattr(rh, "lane_dex_base", lambda base: None)
    monkeypatch.setattr(bsc, "build_bsc_lane", lambda *a, **k: lane)
    threads = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: threads.append(self))
    monkeypatch.setattr(worker, "start", lambda: None)
    assert bsc.start_bsc_lane({}, log=lambda line: None) is not None
    assert lane["live"].stopped
    assert worker.run_one(_h_payload(), time.time())["reason"] == "operator_stop"
    assert sender.raws == [] and worker.lane_host.calls == []
    [watch] = [t for t in threads if t.name == "bsc-fast-stop"]
    class EndPass(Exception):
        pass
    monkeypatch.setattr(bsc.time, "sleep", lambda s: (_ for _ in ()).throw(EndPass()))
    stop.unlink()
    with pytest.raises(EndPass):
        watch._target()
    assert not lane["live"].stopped
    stop.touch()
    with pytest.raises(EndPass):
        watch._target()
    assert lane["live"].stopped


def test_bsc_prebuy_rechecks_operator_stop_after_spot_reads(armed56):
    lane, ledger, net, sender, prime, worker = armed56
    batch = worker.lane_host.batch
    def stopped_during_read(*args, **kwargs):
        result = batch(*args, **kwargs)
        lane["live"].stopped = True
        return result
    worker.lane_host.batch = stopped_during_read
    rec = worker.run_one(_h_payload(), time.time())
    assert rec["reason"] == "operator_stop" and sender.raws == []


def test_bsc_prebuy_rechecks_current_stop_owner_immediately_before_hook(armed56):
    lane, ledger, net, sender, prime, worker = armed56
    facts = worker.lane.cache.facts
    def stopped_during_facts(mint):
        result = facts(mint)
        lane["live"].stopped = True
        return result
    worker.lane.cache.facts = stopped_during_facts
    rec = worker.run_one(_h_payload(), time.time())
    assert rec["reason"] == "operator_stop" and sender.raws == []
    assert _pending56(ledger) == []


def test_a_warm_coin_is_priced_on_the_lane_host_and_reaches_the_builder_send(armed56):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    lane, ledger, net, sender, prime, worker = armed56
    rec = worker.run_one(_h_payload(), time.time())
    assert rec["reason"].startswith("rejected:"), rec                        # the harness sender refuses
    assert len(sender.raws) == 1 and rec["safety_path"] == "cache" and worker.counts["prebuy_cached"] == 1
    [pending] = _pending56(ledger)
    assert pending["signal_source"] == "relay_prebuy" and pending["network_id"] == "56"
    assert pending["origin_event_id"] == "rhprebuy:56:%s:%s" % (H_ORDER, H_MINT)
    assert pending["resolution_reason"].startswith("bsc_prebuy_rejected")
    assert [c for c in net.calls if not c[1]] == []                            # no network before the send
    payload = dict(_h_payload(), **bsc_route_fields_for_test())
    assert rec["min_out"] == bsc.min_out_for(rec["clip_wei"], payload)
    assert rec["execution_evidence"]["relay_prebuy"]["route_pool"] == bsc.our_pair(H_MINT, bsc.WBNB)


def bsc_route_fields_for_test():
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    got, _ = bp.bsc_prebuy_route(H_MINT, 127599760, _h_reserves(), clip_wei=20 * 10 ** 18 // 600,
                                 max_impact_pct=5.0, hop_quotes=())
    return got


def test_the_listener_authenticates_the_feed_and_queues_once(armed56):
    lane, ledger, net, sender, prime, worker = armed56
    worker.start()
    try:
        p = _h_payload()
        assert _go56(lane, p, pid=os.getpid() + 1) is None
        assert lane["dispatch"].drops["not_the_rh_feed"] == 1                  # never the 56 watcher's pid
        assert _go56(lane, dict(p, mint="0x12")) is None and lane["dispatch"].drops["malformed"] == 1
        assert _go56(lane, p) == "prebuy_queued"
        assert worker.idle.wait(10)
        assert _go56(lane, p) is None and lane["dispatch"].drops["duplicate"] == 1
    finally:
        worker.close()
    assert len(sender.raws) == 1


def test_a_cold_coin_is_checked_inline_and_reaches_the_send(armed56):
    lane, ledger, net, sender, prime, worker = armed56
    live = lane["live"]
    live.cache._verdicts.pop(H_MINT)
    asked = []
    worker.lane.inline_safety_fn = lambda m, usd=None: asked.append(m) or (True, None)
    rec = worker.run_one(_h_payload(), time.time())
    assert asked == [H_MINT] and rec["safety_path"] == "inline" and rec["reason"].startswith("rejected:"), rec
    assert worker.counts["prebuy_inline"] == 1 and len(sender.raws) == 1


def test_a_cold_coin_the_inline_check_refuses_is_no_prebuy(armed56):
    lane, ledger, net, sender, prime, worker = armed56
    lane["live"].cache._verdicts.pop(H_MINT)
    worker.lane.inline_safety_fn = lambda m, usd=None: (False, "honeypot")
    rec = worker.run_one(_h_payload(), time.time())
    assert rec["reason"] == "inline_unsafe:honeypot" and sender.raws == []
    assert all(p["status"] == "failed" for p in _pending56(ledger))


def test_a_source_fill_seen_first_means_no_prebuy(armed56):
    lane, ledger, net, sender, prime, worker = armed56
    lane["dispatch"].source_seen[("uid", H_MINT)] = time.time() - 1
    rec = worker.run_one(_h_payload(), time.time())
    assert rec["reason"] == "source_fill_seen" and sender.raws == [] and _pending56(ledger) == []
    assert worker.lane_host.calls == []                    # yielded before any read on the lane host


def test_a_source_fill_during_the_inline_check_yields_before_any_send(armed56):
    lane, ledger, net, sender, prime, worker = armed56
    lane["live"].cache._verdicts.pop(H_MINT)

    def inline(mint, usd=None):
        lane["dispatch"].source_seen[("uid", H_MINT)] = time.time()          # the source's fill arrives
        return True, None
    worker.lane.inline_safety_fn = inline
    rec = worker.run_one(_h_payload(), time.time())
    assert rec["reason"] == "source_fill_seen" and sender.raws == [], rec
    assert all(p["status"] == "failed" for p in _pending56(ledger))           # released, unsigned


def test_while_a_56_prebuy_is_in_flight_the_lane_leaves_the_source_fill(armed56):
    from test_memecoin_bsc_fast_lane_live import _signal
    lane, ledger, net, sender, prime, worker = armed56
    worker._inflight = "0x" + "77" * 20
    rec = _signal(lane)
    assert rec["reason"] == "relay_prebuy_in_flight" and sender.raws == []
    assert ledger.execute("SELECT count(*) FROM memecoin_entry_attempts").fetchone()[0] == 0


def test_with_the_knob_off_there_is_no_56_worker(live56):
    lane, ledger, net, sender, prime = live56
    assert lane["prebuy"] is None and lane["dispatch"].prebuy is None
    assert _go56(lane, _h_payload()) is None and lane["dispatch"].drops["prebuy_off"] == 1


def test_the_56_streak_and_caps_are_its_own(armed56):
    lane, ledger, net, sender, prime, worker = armed56
    assert (worker.lane.clip_usd, worker.lane.pilot_fills, worker.lane.max_sends_per_hour) == (20.0, 20, 4)
    assert worker.db_path == lane["db_path"] and rp.ACTIVE["56"] is worker
    import sqlite3
    db = sqlite3.connect(lane["db_path"])
    db.execute(rp._PREBUYS)
    db.executemany("INSERT INTO rh_relay_prebuys (order_id, reason) VALUES (?, 'reverted')",
                   [("o%d" % i,) for i in range(3)])
    db.commit()
    db.close()
    assert rp.consecutive_reverts(lane["db_path"], 3) == 3                       # 56's own file
    assert rp.consecutive_reverts(str(Path(lane["db_path"]).with_name("memecoin_rh_fast_lane.db")), 3) == 0


def test_a_56_proof_that_does_not_bind_this_buy_refuses_before_any_funded_row(armed56):
    lane, ledger, net, sender, prime, worker = armed56
    payload = _h_payload()
    payload["source_initiation"] = dict(payload["source_initiation"], beneficiary="0x" + "11" * 20)
    rec = worker.run_one(payload, time.time())
    assert rec["reason"] == "prebuy_proof_refused:receipt_identity_mismatch" and sender.raws == []
    assert _pending56(ledger) == []


def test_no_fresh_bnb_price_is_no_prebuy(armed56, monkeypatch):
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    lane, ledger, net, sender, prime, worker = armed56
    monkeypatch.setattr(bp, "bnb_usd_now", lambda now, path=None: (None, None))
    assert worker.run_one(_h_payload(), time.time())["reason"] == "no_bnb_usd" and worker.lane_host.calls == []


# --- Task 5: defer and re-judge the source fill on 56 -----------------------------------------------------

def _sender56(sender, reply):
    sender.send = lambda raw, max_block=None: sender.raws.append(raw) or reply


def _fresh_snapshot(lane, nonce=43):
    """The wallet snapshot read after our pre-buy's send (the generation rule), its nonce past ours."""
    lane["live"].snapshot._last = dict(native_wei=10 ** 18, weth_wei=10 ** 18, nonce=nonce,
                                       gas_price_wei=10 ** 8, read_at=time.time() + 2)


def _reverts56(pre, monkeypatch):
    from sniper.execution import evm_swap_executor as ex
    monkeypatch.setattr(ex, "_receipt_is_canonical", lambda *a, **k: True)
    pre._receipt = lambda tx, timeout_s, **k: {"transactionHash": tx, "status": "0x0", "blockNumber": "0x79b0f1c",
                                               "transactionIndex": "0x1", "logs": []}
    pre.rpc_read = lambda method, params: {"hash": params[0]} if method == "eth_getTransactionByHash" else {
        "timestamp": "0x1", "number": params[0]}
    pre._revert_kind = lambda tx_hash, rec=None: "min_out"


def _lands56(pre):
    from sniper.persistence import db_memecoin as m
    from sniper.util.clock import real_utc_now_iso
    pre._receipt = lambda tx, timeout_s, **k: {"transactionHash": tx, "status": "0x1"}

    def settle(payload, rec, conn, pending_id, tx_hash, decimals, receipt, trace):
        attempt = conn.execute("SELECT attempt_id FROM memecoin_pending_entries WHERE id=?", (pending_id,)).fetchone()[0]
        now = real_utc_now_iso()
        position, _fill = m.finalize_pending_entry(conn, pending_id=pending_id, position_kwargs=dict(
            source_user_id="uid", source_handle="w", mint=H_MINT, ticker="T", entry_price=6e-4, entry_notional=20.0,
            entry_fill_source="chain", entry_lag_s=0.0, their_size_usd=500.0, opened_at=now, execution_mode="real",
            network_id="56", attempt_id=attempt), fill_kwargs=dict(
            kind="entry", price=6e-4, qty=33333.0, notional=20.0, fill_source="chain", fraction=1.0, created_at=now,
            tx_sig=tx_hash, attempt_id=attempt, network_id="56"))
        pre.fills += 1
        rec["_outcome"] = "filled"
        return {"status": "filled", "reason": "filled", "pending_id": pending_id, "tx_hash": tx_hash,
                "position_id": position}
    pre._settle = settle


def test_a_reverted_56_prebuy_then_the_source_fill_is_copied_by_the_bsc_lane(armed56, monkeypatch):
    from test_memecoin_bsc_fast_lane_live import _signal
    lane, ledger, net, sender, prime, worker = armed56
    _sender56(sender, ("accepted", None))
    _reverts56(worker.lane, monkeypatch)
    assert worker.run_one(_h_payload(), time.time())["reason"] == "reverted"
    _sender56(sender, ("rejected", "invalid sender"))
    _fresh_snapshot(lane)
    lane["dispatch"].source_seen.clear()
    src = _signal(lane)
    assert src["reason"].startswith("rejected:"), src                       # reached its send: copied
    assert worker.beat()["source_fill_rejudged_after_prebuy:reverted"] == 1


def test_a_landed_56_prebuy_then_the_source_fill_is_refused(armed56):
    from test_memecoin_bsc_fast_lane_live import _signal
    lane, ledger, net, sender, prime, worker = armed56
    _sender56(sender, ("accepted", None))
    _lands56(worker.lane)
    assert worker.run_one(_h_payload(), time.time())["reason"] == "filled"
    _sender56(sender, ("rejected", "invalid sender"))
    _fresh_snapshot(lane)
    sends = len(sender.raws)
    src = _signal(lane)
    assert src["reason"].startswith("hook:rejected:one position per coin"), src
    assert len(sender.raws) == sends and worker.beat()["source_fill_refused_by_prebuy:filled"] == 1


def test_a_submitted_unknown_56_prebuy_then_the_source_fill_is_refused(armed56):
    from test_memecoin_bsc_fast_lane_live import _signal
    lane, ledger, net, sender, prime, worker = armed56
    worker.lane.lock_receipt_cap_s = worker.lane.receipt_timeout_s = 0.05
    worker.lane._receipt = lambda tx, timeout_s, **k: None
    _sender56(sender, ("ambiguous", "timeout"))
    assert worker.run_one(_h_payload(), time.time())["reason"] == "submitted_unknown"
    _sender56(sender, ("rejected", "invalid sender"))
    _fresh_snapshot(lane)
    src = _signal(lane)
    assert src["reason"].startswith(("hook:rejected:one position per coin",
                                     "hook:rejected:unresolved signed transaction on 56")), src
    assert worker.beat()["source_fill_refused_by_prebuy:submitted_unknown"] == 1


def test_the_56_beat_carries_the_prebuy_counters(armed56):
    from sniper.services import memecoin_bsc_fast_lane as bsc
    import sqlite3
    lane, ledger, net, sender, prime, worker = armed56
    worker.run_one(_h_payload(), time.time())
    bsc.write_beat(lane)
    (lane_json,) = sqlite3.connect(lane["db_path"]).execute(
        "SELECT lane_json FROM bsc_lane_beat ORDER BY rowid DESC").fetchone()
    got = json.loads(lane_json)["prebuy"]
    assert got["prebuy_sent"] == 1 and got["prebuy_cached"] == 1 and got["reverts"] == 0
    assert got["inline_dead"] == 0 and "inline_429" in got           # the pre-buy's own pacer


# The on-chain lane on 56: it waits on a pre-buy's row in flight, then judges as today.

@pytest.fixture
def ledger56():
    import sqlite3
    from scripts.research.onchain_wallet_watch import SCHEMA
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    m.ensure_memecoin_tables(conn)
    yield conn
    conn.close()


def _now56():
    from datetime import datetime, timezone
    return datetime(2026, 10, 14, 13, 6, 30, tzinfo=timezone.utc)


def _prebuy_in_flight56(conn, mint=H_MINT):
    from datetime import timedelta
    from sniper.persistence import db_memecoin as m
    started = (_now56() - timedelta(seconds=1.5)).isoformat()
    origin = "rhprebuy:56:%s:%s" % (H_ORDER, mint)
    pid = m.reserve_pending(conn, mint=mint, source_user_id="uid", strategy="alpha_b", notional=20.0,
                            reserved_at=started, network_id="56", origin_event_id=origin, attempt_id="prebuy56",
                            signal_source="relay_prebuy")
    conn.execute("INSERT INTO memecoin_entry_attempts(attempt_id, origin_event_id, network_id, mint, source_user_id, "
                 "signal_source, delivery_lane, decision_started_at, trace_json) VALUES (?,?,?,?,?,?,?,?,?)",
                 ("prebuy56", origin, "56", mint, "uid", "relay_prebuy", "bsc_relay_prebuy", started,
                  json.dumps({"stages": {}, "submission_events": []})))
    return pid


def _detection56(conn):
    from datetime import timedelta
    arrival = (_now56() - timedelta(seconds=0.1)).isoformat()
    conn.execute("INSERT INTO onchain_wallet_detections(signature,source_user_id,handle,address,network_id,"
                 "mint,source_slot,source_size_usd,source_price,detected_at,created_at,source_valuation_json) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("56:%s:%s" % ("0x" + "62" * 32, H_MINT), "uid", "w", H_WALLET, "56", H_MINT, 127599765, 500.0,
                  6e-4, arrival, arrival, json.dumps({})))


def _sweep56(conn, calls, *, cursor=0, deferred):
    from sniper.services import memecoin_onchain_lane as onchain
    return onchain.sweep_onchain_detections(
        conn, lambda c, ev: calls.append(ev) or {"status": "rejected", "reason": "ordinary verdict"},
        cursor=cursor, now_fn=lambda: _now56().isoformat(), max_age_s=45, network_id="56", deferred=deferred,
        approved_ids_fn=lambda _conn, net: {"uid"} if net == "56" else set())


def test_an_aborted_56_prebuy_in_flight_then_the_source_fill_is_copied_by_the_onchain_lane(ledger56, bsc_on):
    from sniper.persistence import db_memecoin as m
    pid = _prebuy_in_flight56(ledger56)
    _detection56(ledger56)
    calls, deferred = [], {}
    cursor, results = _sweep56(ledger56, calls, deferred=deferred)
    assert calls == [] and results[0]["result"] == {"status": "deferred", "reason": "relay_prebuy_in_flight"}
    assert m.release_pending(ledger56, pid, "bsc_prebuy_lock_busy")
    ledger56.execute("UPDATE memecoin_entry_attempts SET terminal_status='aborted', terminal_reason='lock_busy', "
                     "completed_at=? WHERE attempt_id='prebuy56'", (_now56().isoformat(),))
    _cursor, results = _sweep56(ledger56, calls, cursor=cursor, deferred=deferred)
    assert len(calls) == 1 and results[0]["result"]["reason"] == "ordinary verdict" and not deferred


def test_the_4663_knob_alone_never_holds_a_56_detection(ledger56, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_RELAY_PREBUY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_BSC_RELAY_PREBUY", False)
    _prebuy_in_flight56(ledger56)
    _detection56(ledger56)
    calls = []
    _cursor, results = _sweep56(ledger56, calls, deferred={})
    assert len(calls) == 1 and results[0]["result"]["reason"] == "ordinary verdict"


# J7 inside a 56 pre-buy's pre-sign window: the chain-agnostic transient, on 56.

def test_j7_in_a_56_prebuy_s_pre_sign_window_is_re_asked_and_copies(monkeypatch):
    import sqlite3
    from dataclasses import replace
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_leg as RL
    from sniper.services import memecoin_real_runner as R
    from sniper.services.memecoin_risk import duplicate_refusal, risk_refusal_result
    from sniper.settings import memecoin as cfg
    from test_memecoin_entry_retry_lane import _ev
    for name, value in (("MEMECOIN_ENTRY_RETRY_WINDOW_S", 60.0), ("MEMECOIN_ENTRY_RETRY_INTERVAL_S", 6.0),
                        ("MEMECOIN_ENTRY_RETRY_MAX_ATTEMPTS", 12), ("MEMECOIN_ENTRY_RETRY_UNRESOLVED_S", 1.0)):
        monkeypatch.setattr(cfg, name, value)
    monkeypatch.setattr(RL, "real_entries_allowed", lambda kill_path=None: True)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    now = "2026-10-14T13:06:29+00:00"
    pid = m.reserve_pending(c, mint=H_MINT, source_user_id="uid", strategy="alpha_b", notional=20.0, reserved_at=now,
                            network_id="56", signal_source="relay_prebuy",
                            origin_event_id="rhprebuy:56:%s:%s" % (H_ORDER, H_MINT))
    executed = []

    def evm_entry(conn, ev, **_kw):
        blockers = {}
        held = m.real_open_coin_ids(conn, blockers=blockers) | m.pending_open_coin_ids(conn, blockers=blockers)
        if m.coin_identity("56", ev.mint) in held:
            return risk_refusal_result(duplicate_refusal(blockers.get(m.coin_identity("56", ev.mint))))
        executed.append(ev.fomo_event_id)
        return {"status": "filled"}

    def hook(conn, ev):
        return RL.real_entry(conn, ev, day="2026-10-14", usdc_balance=300.0, sol_balance=0.0, now_iso=now,
                             trade_size=10.0, token_safety_fn=lambda *a, **k: (True, {}), execute_fn=lambda *a, **k: {},
                             evm_allowlist={"56"}, resolve_fn=lambda _m: "56", evm_entry_fn=evm_entry)
    ev = replace(_ev(fid="j7-56", mint=H_MINT), network_id="56")
    first = hook(c, ev)
    assert first["duplicate_blocker"]["signal_source"] == "relay_prebuy"
    assert R._maybe_enqueue_retry(c, ev, first, now) is True
    assert m.release_pending(c, pid, "bsc_prebuy_lock_busy")
    R.run_entry_retry_lane(c, hook, "2026-10-14T13:06:30+00:00")
    assert executed == ["j7-56"]


# --- Task 6: the 56 record row ----------------------------------------------------------------------------

def test_the_56_record_row_names_the_route_block_and_impact(armed56):
    import sqlite3
    from sniper.services import memecoin_bsc_fast_lane as bsc
    lane, ledger, net, sender, prime, worker = armed56
    rec = worker.run_one(_h_payload(), time.time())
    db = sqlite3.connect(lane["db_path"])
    db.row_factory = sqlite3.Row
    [row] = [dict(r) for r in db.execute("SELECT * FROM rh_relay_prebuys")]
    assert row["order_id"] == H_ORDER and row["uid"] == "uid" and row["reason"] == rec["reason"]
    assert row["route_pool"] == bsc.our_pair(H_MINT, bsc.WBNB) and row["route_kind"] == "v2_native"
    assert row["decide_head_block"] == 127599760 and 0 < row["impact_pct"] <= 5
    assert row["request_id"] == "0x" + "17" * 32 and row["our_tx"] == rec["tx_hash"]


def test_a_refusal_before_pricing_still_writes_its_row(armed56):
    import sqlite3
    lane, ledger, net, sender, prime, worker = armed56
    lane["dispatch"].source_seen[("uid", H_MINT)] = time.time()
    worker.run_one(_h_payload(), time.time())
    [(reason, pool)] = sqlite3.connect(lane["db_path"]).execute(
        "SELECT reason, route_pool FROM rh_relay_prebuys").fetchall()
    assert reason == "source_fill_seen" and pool is None


# --- Gamma's LOWs on de45153d2 ----------------------------------------------------------------------------

def test_a_hop_route_s_paid_amount_uses_the_hop_s_fee_inclusive_spot():
    """LOW 1: `paid` must match min_out_for's conversion (clip x 9975/10000 x r_quote/r_wbnb)."""
    from sniper.services import memecoin_bsc_fast_lane as bsc
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    _direct, coin_usdt, hop = _pairs()
    wbnb1 = int(bsc.WBNB, 16) > int(USDT, 16)
    hop_state = (600_000 * 10 ** 18, 1000 * 10 ** 18) if wbnb1 else (1000 * 10 ** 18, 600_000 * 10 ** 18)
    reserves = {coin_usdt: _ordered(10 ** 27, 30_000 * 10 ** 18, USDT), hop: hop_state}
    got, why = bp.bsc_prebuy_route(MINT56, 1, reserves, clip_wei=CLIP, max_impact_pct=5.0, hop_quotes=(USDT,))
    assert why is None
    assert got["src_in"] == int(Fraction(CLIP) * 9975 / 10000 * Fraction(600_000 * 10 ** 18, 1000 * 10 ** 18))
    spot = Fraction(30_000 * 10 ** 18, 10 ** 27)
    expected_impact = (Fraction(CLIP) * 9975 / 10000 * 600 / got["est_out"] / spot - 1) * 100
    assert got["impact_pct"] == pytest.approx(float(expected_impact))


def test_a_source_fill_seen_after_pricing_yields_before_the_lane(armed56, monkeypatch):
    """LOW 2: the post-pricing check, apart from the one inside the hook."""
    from sniper.services import memecoin_bsc_relay_prebuy as bp
    lane, ledger, net, sender, prime, worker = armed56
    real = bp.bsc_prebuy_route

    def route_then_the_source_arrives(*args, **kwargs):
        lane["dispatch"].source_seen[("uid", H_MINT)] = time.time()
        return real(*args, **kwargs)
    monkeypatch.setattr(bp, "bsc_prebuy_route", route_then_the_source_arrives)
    rec = worker.run_one(_h_payload(), time.time())
    assert rec["reason"] == "source_fill_seen" and sender.raws == [] and _pending56(ledger) == []
    assert len(worker.lane_host.calls) == 1                # priced, then yielded before the lane


# --- Gamma's LOW 4 (Alpha go, 2026-10-14): the pre-buy's own pacer on the same host ----------------------

def test_prebuy_429s_and_dead_marking_never_pause_or_kill_the_live_lane_s_inline_path(armed56):
    from test_memecoin_bsc_fast_lane_live import _miss, _signal
    lane, ledger, net, sender, prime, worker = armed56
    live, pre = lane["live"], worker.lane
    assert pre.inline_host is not live.inline_host                       # its own pacer
    # Through the pre-buy's REAL seam (the harness blocks the network): transport failures dead-mark it.
    from sniper.services.memecoin_bsc_fast_lane import LaneHost
    seam_host = pre.prebuy_lane_host
    assert isinstance(seam_host, LaneHost)
    for _ in range(3):
        with pytest.raises(Exception):
            seam_host("eth_blockNumber", [])
    pre.inline_host.pause()                                              # and a 429 pause
    assert pre.inline_host.dead() and pre.inline_host.paused()
    assert not live.inline_host.dead() and not live.inline_host.paused()
    # The live lane's own cold coin still runs its inline check.
    _miss(live, H_MINT)
    asked = []
    live.inline_safety_fn = lambda m, usd=None: asked.append(m) or (True, None)
    live.inline_facts_fn = lambda m: (18, 33_333_333 * 10 ** 18)
    rec = _signal(lane)
    assert rec["safety_path"] == "inline" and asked == [H_MINT], rec
    assert rec["reason"] != "inline_host_dead"
    # And the reverse: the live lane's pause never pauses the pre-buy's own pacer.
    fresh = type(pre.inline_host)(min_gap_s=0.0, pause_s=120.0)
    pre.inline_host = fresh
    live.inline_host.pause()
    assert live.inline_host.paused() and not fresh.paused()


def test_the_prebuy_respects_the_live_lane_s_429_pause(armed56):
    """Gamma's note on bdde13eb9: a 429 is the shared key's, so while the live lane waits it out the pre-buy
    reads nothing on that key; it never pauses the live lane itself."""
    lane, ledger, net, sender, prime, worker = armed56
    lane["live"].inline_host.pause()
    rec = worker.run_one(_h_payload(), time.time())
    assert rec["reason"] == "live_host_paused" and worker.lane_host.calls == [] and sender.raws == []
    assert worker.counts["prebuy_refused:live_host_paused"] == 1
    lane["live"].inline_host._paused_until = 0.0                    # the pause ends: the pre-buy reads again
    assert worker.run_one(_h_payload("0x" + "4f" * 32), time.time())["reason"].startswith("rejected:")
    assert len(worker.lane_host.calls) == 1
