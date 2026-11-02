"""BSC 56 fast lane, step 1: the watcher-side decide and datagram (operator via Alpha 2026-10-02:
"BSC needs a fast lane"; Alpha's approvals (a) and (b)).

The 56 watcher already holds the source buy's receipt (C4(b) WS). The decide prices from that
receipt's own Sync on the coin/WBNB PancakeSwap V2 pair, the exact post-trade reserves at zero lag,
with the buy tax the funded path's own decode measured (source_post_trade, `evm_post`). A receipt
whose V2 leg is not our CREATE2 pair, or whose tax cannot be computed, falls back. The real
receipts are tests/fixtures/memecoin/evm_post_trade_receipts.json (fetched 2026-09-28)."""
import json
import os
import time
from fractions import Fraction
from pathlib import Path

import pytest
from eth_utils import keccak

from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_evm_post_trade as pt

HERE = Path(__file__).parent
CASES = json.load(open(HERE / "fixtures" / "memecoin" / "evm_post_trade_receipts.json"))["cases"]
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
V2 = "0x" + keccak(b"Swap(address,uint256,uint256,uint256,uint256,address)").hex()
SYNC = "0x" + keccak(b"Sync(uint112,uint112)").hex()
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"


def _owner(case):
    if case["trader"]:
        return case["trader"]
    last = [log for log in case["receipt"]["logs"] if log["topics"] and log["topics"][0] == TRANSFER
            and log["address"].lower() == case["mint"]][-1]
    return "0x" + last["topics"][2][-40:]


def _real(name):
    case = CASES[name]
    post = pt.source_post_trade("56", case["receipt"], case["mint"], owner=_owner(case))
    return case, post


def _decide(receipt, mint, post, chase_pct=30, premium=1.5, hop=None, seq=16):
    return bsc.decide_from_receipt(receipt, mint=mint, evm_post=post, chase_pct=chase_pct,
                                   premium=premium, seq=seq, hop_state_fn=hop or (lambda p, s: None))


def _words(log):
    data = str(log["data"])[2:]
    return [int(data[i:i + 64], 16) for i in range(0, len(data), 64)]


def test_a_buy_on_our_create2_pair_would_send_from_its_own_post_reserves_and_tax():
    case, post = _real("v2_56_fill")
    out = _decide(case["receipt"], case["mint"], post)
    assert out["reason"] == "would_send", out
    pool = out["pool"]
    assert pool == pt._v2_pair(*pt.V2_FACTORIES[("56", bsc.PCS_V2_ROUTER)], case["mint"], WBNB)
    assert out["quote"] == WBNB and out["hop"] is None
    syncs = [log for log in case["receipt"]["logs"] if log["address"].lower() == pool
             and log["topics"][0] == SYNC]
    r0, r1 = _words(syncs[-1])
    coin1 = int(case["mint"], 16) > int(WBNB, 16)
    assert (out["r_coin"], out["r_quote"]) == ((r1, r0) if coin1 else (r0, r1))
    (swap,) = [log for log in case["receipt"]["logs"] if log["address"].lower() == pool
               and log["topics"][0] == V2]
    a0in, a1in, a0out, a1out = _words(swap)
    assert (out["src_in"], out["src_out"]) == ((a0in, a1out) if coin1 else (a1in, a0out))
    paid = [_words(log)[0] for log in case["receipt"]["logs"]
            if log["address"].lower() == case["mint"] and len(log["topics"]) == 3
            and log["topics"][0] == TRANSFER and "0x" + log["topics"][1][-40:].lower() == pool]
    assert out["tax"] == Fraction(sum(paid) - max(paid), sum(paid))  # exact, not the float 0.01
    assert float(out["tax"]) == pytest.approx(post["buy_tax"], abs=1e-9)
    post_price = Fraction(out["r_quote"], out["r_coin"])
    avg = Fraction(out["src_in"], out["src_out"])
    assert out["cap"] == min(post_price * Fraction(13, 10), avg * Fraction(3, 2))


QUOTE_X = "0x9b8e987e6fec8cf1380c4dca7071e2c7853aeea1"   # the quote both other real receipts paid


@pytest.mark.parametrize("name", ["v2_56_taxed_refusal", "v2_56_fill_66521"])
def test_a_real_quote_pair_needs_the_wbnb_hop_state_or_falls_back(name):
    # Gamma/Alpha on b44fae0b2: the source traded the coin's PancakeSwap V2 pair against another
    # quote; the lane routes WBNB -> quote -> coin only when the WBNB/quote state is exact.
    case, post = _real(name)
    stale = _decide(case["receipt"], case["mint"], post)
    assert stale["reason"] == "hop_state_stale:unknown" and stale["quote"] == QUOTE_X
    asked = []
    hop_pair = pt._v2_pair(*pt.V2_FACTORIES[("56", bsc.PCS_V2_ROUTER)], WBNB, QUOTE_X)

    def tape(pair, seq):
        asked.append((pair, seq))
        return (7 * 10 ** 21, 3 * 10 ** 20, seq) if int(WBNB, 16) < int(QUOTE_X, 16) else (
            3 * 10 ** 20, 7 * 10 ** 21, seq)
    out = _decide(case["receipt"], case["mint"], post, hop=tape)
    assert out["reason"] == "would_send" and out["quote"] == QUOTE_X and asked == [(hop_pair, 16)]
    assert out["hop"] == {"pair": hop_pair, "r_wbnb": 7 * 10 ** 21, "r_quote": 3 * 10 ** 20,
                          "source": "tape", "height": 16}
    assert out["pool"] == pt._v2_pair(*pt.V2_FACTORIES[("56", bsc.PCS_V2_ROUTER)], case["mint"], QUOTE_X)


def test_a_v2_pool_of_another_factory_falls_back():
    # A self-consistent receipt on a V2 pool that is not the PCS CREATE2 pair of its two tokens.
    receipt, mint, post = _synthetic(pool="0x" + "ab" * 20)
    assert post["venue"] == "v2" and post["pool"] == "0x" + "ab" * 20 and "buy_tax" in post
    assert _decide(receipt, mint, post)["reason"] == "not_our_pair"


def _synthetic(*, tax=True, extra_swap=False, quote=WBNB, hop_sync=None, pool=None):
    """A buy on the coin's PancakeSwap V2 pair against `quote`, BINF-shaped: the pair pays 3% to the
    token contract. `hop_sync` adds the WBNB/quote pair's own Sync (reserve0, reserve1)."""
    mint = "0x" + "77" * 20
    factory = pt.V2_FACTORIES[("56", bsc.PCS_V2_ROUTER)]
    pair = pool or pt._v2_pair(*factory, mint, quote)
    router, owner = "0x" + "0d" * 20, "0x" + "6f" * 20
    word = lambda v: format(v, "064x")
    topic = lambda a: "0x" + "0" * 24 + a[2:]
    fee, main, into = 626_252 * 10 ** 18, 20_248_822 * 10 ** 18, 8 * 10 ** 18
    coin1 = int(mint, 16) > int(quote, 16)
    swap = (word(into) + word(0) + word(0) + word(fee + main)) if coin1 else (
        word(0) + word(into) + word(fee + main) + word(0))
    reserves = (word(10 ** 20) + word(10 ** 26)) if coin1 else (word(10 ** 26) + word(10 ** 20))
    logs = [{"address": quote, "topics": [TRANSFER, topic(router), topic(pair)], "data": "0x" + word(into)}]
    if hop_sync is not None:
        hop_pair = pt._v2_pair(*factory, WBNB, quote)
        logs.append({"address": hop_pair, "topics": [SYNC],
                     "data": "0x" + word(hop_sync[0]) + word(hop_sync[1])})
    if tax:
        logs.append({"address": mint, "topics": [TRANSFER, topic(pair), topic(mint)], "data": "0x" + word(fee)})
    logs += [
        {"address": mint, "topics": [TRANSFER, topic(pair), topic(router)],
         "data": "0x" + word(main + (0 if tax else fee))},
        {"address": pair, "topics": [SYNC], "data": "0x" + reserves},
        {"address": pair, "topics": [V2, topic(router), topic(router)], "data": "0x" + swap},
        {"address": mint, "topics": [TRANSFER, topic(router), topic(owner)],
         "data": "0x" + word(main + (0 if tax else fee))},
    ]
    if extra_swap:
        logs.append({"address": pair, "topics": [V2, topic(router), topic(router)],
                     "data": "0x" + (word(1) + word(0) + word(0) + word(1) if coin1
                                     else word(0) + word(1) + word(1) + word(0))})
    receipt = {"status": "0x1", "logs": logs}
    return receipt, mint, pt.source_post_trade("56", receipt, mint, owner=owner)


def test_the_hop_state_comes_from_the_receipt_when_the_source_touched_that_pair():
    receipt, mint, post = _synthetic(quote=QUOTE_X, hop_sync=(5 * 10 ** 21, 9 * 10 ** 20))

    def tape(pair, seq):
        raise AssertionError("the receipt had the hop's own Sync")
    out = _decide(receipt, mint, post, hop=tape)
    assert out["reason"] == "would_send" and out["hop"]["source"] == "receipt"
    wbnb1 = int(WBNB, 16) > int(QUOTE_X, 16)
    assert (out["hop"]["r_wbnb"], out["hop"]["r_quote"]) == (
        (9 * 10 ** 20, 5 * 10 ** 21) if wbnb1 else (5 * 10 ** 21, 9 * 10 ** 20))


def test_the_min_out_covers_the_whole_path():
    # Gamma: the cap and min-out cover WBNB -> quote, then the coin leg. Alpha 2026-10-02: the hop
    # at its fee-inclusive SPOT, so its impact counts against the cap.
    receipt, mint, post = _synthetic(quote=QUOTE_X, hop_sync=(5 * 10 ** 21, 9 * 10 ** 20))
    out = _decide(receipt, mint, post)
    p = bsc.lane_payload(ROW, out, decimals=18, bnb_usd=600.0, bnb_usd_at=1.0, sent_epoch=1.0)
    clip = 10 ** 17
    hop = out["hop"]
    x = Fraction(clip) * 9975 / 10000 * hop["r_quote"] / hop["r_wbnb"]
    assert bsc.min_out_for(clip, p) == int(x / out["cap"] * (1 - out["tax"]))
    direct = _synthetic()
    d_out = _decide(direct[0], direct[1], direct[2])
    d_p = bsc.lane_payload(ROW, d_out, decimals=18, bnb_usd=600.0, bnb_usd_at=1.0, sent_epoch=1.0)
    assert bsc.min_out_for(clip, d_p) == int(Fraction(clip) / d_out["cap"] * (1 - d_out["tax"]))


def test_a_taxed_buy_on_our_pair_carries_its_exact_tax():
    receipt, mint, post = _synthetic()
    out = _decide(receipt, mint, post)
    assert out["reason"] == "would_send"
    assert out["tax"] == Fraction(626_252, 626_252 + 20_248_822)


def test_a_tax_that_cannot_be_computed_falls_back():
    # Alpha (a): two Swaps on our pair in the source's transaction read an unknown tax.
    receipt, mint, post = _synthetic(extra_swap=True)
    assert "buy_tax" not in post
    assert _decide(receipt, mint, post)["reason"] == "tax_unknown"


@pytest.mark.parametrize("post, reason", [
    ({"reason": "no_venue_leg"}, "post:no_venue_leg"),
    (None, "post:none"),
    ({"ratio": 1.1, "pool": "0x" + "ab" * 20, "venue": "v3"}, "not_v2:v3"),
])
def test_a_missing_or_foreign_post_trade_decode_falls_back(post, reason):
    receipt, mint, _post = _synthetic()
    assert _decide(receipt, mint, post)["reason"] == reason


def test_a_reverted_source_or_no_price_cap_falls_back():
    receipt, mint, post = _synthetic()
    assert _decide(dict(receipt, status="0x0"), mint, post)["reason"] == "source_reverted"
    assert _decide(receipt, mint, post, chase_pct=0, premium=0)["reason"] == "no_price_cap"


# ---- the datagram: one per would-send decision, only while the runner's lane runs ----

ROW = dict(tx_hash="0x" + "c" * 64, wallet="0x" + "6f" * 20, source_user_id="uid", handle="w",
           buy=dict(mint="0x" + "77" * 20, token_delta=20248822.0, source_size_usd=5000.0,
                    source_price=0.000247), log={"blockNumber": "0x10", "blockTimestamp": "0x5"},
           now="2026-10-02T01:00:00+00:00")


def test_the_payload_carries_the_decision_and_survives_json():
    receipt, mint, post = _synthetic()
    out = _decide(receipt, mint, post)
    p = bsc.lane_payload(ROW, out, decimals=18, bnb_usd=600.0, bnb_usd_at=999.0, sent_epoch=1000.0)
    back = json.loads(json.dumps(p))
    assert Fraction(back["cap_num"], back["cap_den"]) == out["cap"]
    assert Fraction(back["tax_num"], back["tax_den"]) == out["tax"]
    assert (back["chain"], back["seq"], back["paid_usd"], back["state_kind"]) == (
        "56", 16, 5000.0, "receipt_post")
    assert all(k in back for k in bsc.REQUIRED)


@pytest.mark.parametrize("mode, sends", [("runner_shadow", True), ("live", True), ("off", False),
                                         ("", False)])
def test_the_watcher_sends_only_would_send_decisions_and_only_while_the_lane_runs(mode, sends):
    sent = []
    receipt, mint, post = _synthetic()
    signal = bsc.make_watcher_signal(mode, "\0addr", send_fn=lambda a, p: sent.append(p),
                                     clock=lambda: 1000.0)
    if not sends:
        assert signal is None
        return
    row = dict(ROW, buy=dict(ROW["buy"], mint=mint))
    assert signal(row, receipt, post, decimals=18, bnb_usd=600.0, bnb_usd_age_s=1.0)["reason"] == (
        "would_send")
    assert signal(row, receipt, {"reason": "no_venue_leg"}, decimals=18, bnb_usd=600.0,
                  bnb_usd_age_s=1.0)["reason"] == "post:no_venue_leg"
    assert len(sent) == 1 and sent[0]["mint"] == mint and sent[0]["bnb_usd_at"] == 999.0


def test_a_signal_without_a_price_or_decimals_is_a_named_fallback():
    sent = []
    receipt, mint, post = _synthetic()
    signal = bsc.make_watcher_signal("runner_shadow", "\0a", send_fn=lambda a, p: sent.append(p))
    row = dict(ROW, buy=dict(ROW["buy"], mint=mint))
    assert signal(row, receipt, post, decimals=None, bnb_usd=600.0, bnb_usd_age_s=1.0)["reason"] == (
        "decimals_unknown")
    assert signal(row, receipt, post, decimals=18, bnb_usd=None, bnb_usd_age_s=None)["reason"] == (
        "no_bnb_usd")
    assert sent == []


def test_a_signal_never_raises_into_the_watcher():
    def boom(a, p):
        raise OSError("socket gone")
    receipt, mint, post = _synthetic()
    signal = bsc.make_watcher_signal("runner_shadow", "\0a", send_fn=boom)
    row = dict(ROW, buy=dict(ROW["buy"], mint=mint))
    out = signal(row, receipt, post, decimals=18, bnb_usd=600.0, bnb_usd_age_s=1.0)
    assert out["reason"] == "would_send" and out["sent"] is False
    assert signal(None, None, None, decimals=18, bnb_usd=600.0, bnb_usd_age_s=1.0)["reason"] == "error"


def test_each_state_root_has_its_own_lane_address_apart_from_the_4663_lane(tmp_path):
    from sniper.services import memecoin_rh_fast_lane as rh
    assert bsc.lane_address(tmp_path) != bsc.lane_address(tmp_path / "x")
    assert bsc.lane_address(tmp_path) != rh.lane_address(tmp_path)


def test_the_watcher_pid_comes_from_its_own_fresh_iso_heartbeat(tmp_path):
    from datetime import datetime, timezone
    beat = tmp_path / "beat"
    now = time.time()
    iso = datetime.fromtimestamp(now - 5, timezone.utc).isoformat()
    beat.write_text(json.dumps({"ts": iso, "pid": os.getpid()}))
    assert bsc.watcher_pid(str(beat), now) == os.getpid()
    assert bsc.watcher_pid(str(beat), now + 3600) is None              # stale
    beat.write_text(json.dumps({"ts": iso}))
    assert bsc.watcher_pid(str(beat), now) is None                     # no pid
    assert bsc.watcher_pid(str(tmp_path / "missing"), now) is None


def test_the_mode_knob(monkeypatch):
    from sniper.settings import memecoin as cfg
    for raw, want in (("", "off"), ("runner_shadow", "runner_shadow"), (" LIVE ", "live"),
                      ("on", "off"), ("shadow", "off")):
        monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", raw)
        assert cfg.memecoin_bsc_fast_lane_mode() == want


# ---- in the 56 watcher: after today's commit and wake, never before; off by default ----

@pytest.fixture
def watch56(monkeypatch, tmp_path):
    import scripts.research.onchain_evm_wallet_watch as w
    from test_onchain_evm_watch_detection_latency import _Conn, WALLET
    events = []
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn(events))
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "send_wake", lambda address: events.append(("wake", w.now_iso())) or True)
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr(bsc, "send_signal", lambda address, p: events.append(("lane", p)) or True)
    yield w, events, WALLET
    w._configure("4663")


def _drive56(monkeypatch, w, wallet, chain="56"):
    from sniper.services import memecoin_onchain_push as P
    receipt, mint, post = _synthetic()
    heartbeat = w.HEARTBEAT
    w._configure(chain)
    monkeypatch.setattr(w, "HEARTBEAT", heartbeat)

    def detect(rpc_url, log, watched, *, now, quote_usd, outcome=None, **_kw):
        outcome.update(reason="detected", _receipt=receipt, evm_post=post, decimals=18)
        return dict(tx_hash="0x" + "11" * 32, wallet=wallet, source_user_id="u1", handle="h1",
                    buy=dict(ROW["buy"], mint=mint), log={"blockNumber": "0x10"}, now=now)
    monkeypatch.setattr(w, "push_detection", detect)

    def stream(ws_url, **kwargs):
        result = kwargs["on_transfer"]({"transactionHash": "0x" + "11" * 32, "address": mint,
                                        "blockNumber": "0x10"})
        if hasattr(result, "result"):
            result.result(timeout=2)
        return kwargs.get("state")
    monkeypatch.setattr(P, "run_push_stream", stream)
    price = w.NativePrice(lambda: 600.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    return mint


def test_the_56_watcher_signals_the_lane_after_its_commit_and_wake(watch56, monkeypatch, capsys):
    w, events, wallet = watch56
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "runner_shadow")
    mint = _drive56(monkeypatch, w, wallet)
    assert [e[0] for e in events] == ["insert", "commit", "wake", "lane"]
    payload = events[3][1]
    assert payload["mint"] == mint and payload["bnb_usd"] == 600.0 and payload["seq"] == 16
    assert "[bsc-lane]" in capsys.readouterr().out


@pytest.mark.parametrize("mode, chain", [("", "56"), ("runner_shadow", "4663")])
def test_off_or_on_another_chain_the_watcher_sends_nothing(watch56, monkeypatch, mode, chain):
    w, events, wallet = watch56
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", mode)
    _drive56(monkeypatch, w, wallet, chain=chain)
    assert [e[0] for e in events] == ["insert", "commit", "wake"]


def test_the_watcher_heartbeat_carries_its_pid(watch56):
    w, events, wallet = watch56
    w._write_beat({"ts": w.now_iso()})
    assert json.loads(w.HEARTBEAT.read_text())["pid"] == os.getpid()


def test_a_detection_keeps_its_receipt_and_coin_decimals_in_memory_for_the_lane():
    # The real path (a real Relay buy, the same frame tests/test_memecoin_evm_post_trade.py uses):
    # push_detection leaves the receipt and the coin's decimals in `outcome`, never in the row.
    import gzip
    import scripts.research.onchain_evm_wallet_watch as w
    from eth_abi import decode
    from sniper.services import memecoin_evm_onchain_watch as M
    relay = json.load(gzip.open(HERE / "fixtures" / "relay_purchases_2026-09-28.json.gz", "rt"))
    b = relay["buys"][11]

    def read(method, params):
        if method == "eth_chainId":
            return hex(8453)
        if method == "eth_getCode":
            return relay["code_8453"].get(params[0], "0xdeadbeef")
        if method == "eth_getTransactionByHash":
            return b["tx"]
        if method == "eth_getTransactionReceipt":
            return b["receipt"]
        raise AssertionError(method)
    w._configure("8453")
    try:
        log = next(lg for lg in b["receipt"]["logs"] if lg["topics"] and lg["topics"][0] == TRANSFER
                   and lg["address"].lower() == b["mint"] and lg["topics"][2][-40:] == b["wallet"][2:])
        payment = decode(M.PERMIT2_TYPES, bytes.fromhex(b["tx"]["input"][10:]))[1][0][0][0].lower()
        outcome = {}
        row = w.push_detection("http://unused", log, {b["wallet"]: ("uid", b["handle"])},
                               decimals_cache={b["mint"]: 18, payment: 18}, quote_usd=600.0,
                               now="2026-09-28T06:30:00+00:00", read_fn=read, outcome=outcome,
                               payment_price_fn=lambda token: 1.0 if token == payment else None)
    finally:
        w._configure("4663")
    assert row is not None and "_receipt" not in row
    assert outcome["_receipt"] is b["receipt"] and outcome["decimals"] == 18


# ---- the hop at lag 0 from the bsc_v2_live tape (Alpha's approval of Beta's (b), 2026-10-02) ----

HOP_QUOTES = ("0x205812cdbed920aff76c6580abd681a46d11efc7", "0x4902c5ebc598265ed2212b559b042de8a5eeec3f",
              "0x25d887ce7a35172c62febfd67a1856f20faebb00", "0x9b8e987e6fec8cf1380c4dca7071e2c7853aeea1")


def _tape(tmp_path, *, head=100, received=1000.0, sync_height=99, sync_hash=None, keep_from=None):
    import sqlite3
    from sniper.services import memecoin_bsc_v2_live as tape
    c = tape.connect(str(tmp_path / "tape.db"))
    c.row_factory = sqlite3.Row
    for h in range(keep_from or head - 3, head + 1):
        tape.record_head(c, h, "0x%064x" % h, 1790000000 + h, received, parent_hash="0x%064x" % (h - 1))
    pair = "0x" + "4b" * 20
    tape.record_sync(c, pair, sync_height, sync_hash or "0x%064x" % sync_height, 5 * 10 ** 21,
                     9 * 10 ** 20, received)
    return c, pair


def test_the_tape_gives_the_hop_state_at_lag_zero(tmp_path):
    c, pair = _tape(tmp_path)
    assert bsc.tape_hop_state(c, pair, 100, now=1001.0) == (5 * 10 ** 21, 9 * 10 ** 20, 99)


@pytest.mark.parametrize("seq, now, why", [(101, 1001.0, "head_behind"),    # the tape has not reached the source block
                                           (100, 1004.0, "head_old"),       # the tape's last head is over 3 s old
                                           (None, 1001.0, "no_seq")])       # no source block
def test_the_tape_refuses_a_hop_state_it_cannot_vouch_for(tmp_path, seq, now, why):
    c, pair = _tape(tmp_path)
    assert bsc.tape_hop_state(c, pair, seq, now=now) == why


def test_a_sync_off_the_canonical_heads_or_an_unknown_pair_is_no_state(tmp_path):
    c, pair = _tape(tmp_path, sync_hash="0x" + "ee" * 32)
    assert bsc.tape_hop_state(c, pair, 100, now=1001.0) == "off_canonical"
    assert bsc.tape_hop_state(c, "0x" + "5c" * 20, 100, now=1001.0) == "untracked"


# ---- F2 (Alpha 2026-10-07, dfeaef22): one stale reason hid five causes; each miss names its own ----

def test_a_tracked_pair_without_a_sync_is_no_sync_and_an_untracked_one_is_untracked(tmp_path):
    from sniper.services import memecoin_bsc_v2_live as tape
    c, _pair = _tape(tmp_path)
    configured = next(iter(bsc.hop_pairs()))
    discovered, stranger = "0x" + "6d" * 20, "0x" + "6e" * 20
    tape.remember_hop_targets(c, {discovered: "0x" + "77" * 20}, 1.0)
    assert bsc.tape_hop_state(c, configured, 100, now=1001.0) == "no_sync"
    assert bsc.tape_hop_state(c, discovered, 100, now=1001.0) == "no_sync"
    assert bsc.tape_hop_state(c, stranger, 100, now=1001.0) == "untracked"


def test_no_head_and_a_tape_read_error_are_named(tmp_path):
    from sniper.services import memecoin_bsc_v2_live as tape
    c, pair = _tape(tmp_path)
    tape.reset_coverage(c)
    assert bsc.tape_hop_state(c, pair, 100, now=1001.0) == "no_head"

    class Broken:
        def execute(self, *a):
            raise sqlite3.OperationalError("database is locked")
    import sqlite3
    assert bsc.tape_hop_state(Broken(), pair, 100, now=1001.0) == "tape_error"


def test_a_stale_hop_names_its_cause_and_its_quote_and_a_zero_reserve_is_its_own(tmp_path):
    case, post = _real("v2_56_fill_66521")
    out = _decide(case["receipt"], case["mint"], post, hop=lambda pair, seq: "head_old")
    assert out == {"reason": "hop_state_stale:head_old", "quote": QUOTE_X}
    out = _decide(case["receipt"], case["mint"], post, hop=lambda pair, seq: (0, 5, seq))
    assert out == {"reason": "hop_state_stale:zero_reserve", "quote": QUOTE_X}
    out = bsc.decide_from_receipt(case["receipt"], mint=case["mint"], evm_post=post, chase_pct=30,
                                  premium=1.5, seq=16, hop_state_fn=None)
    assert out["reason"] == "hop_state_stale:no_reader"


def test_a_quiet_pairs_sync_deeper_than_the_kept_heads_is_final_and_current(tmp_path):
    # Alpha: a quiet pair's last Sync may sit outside the heads window; inside the tape's unbroken
    # coverage it is still the pair's state (a coverage reset deletes every Sync).
    c, pair = _tape(tmp_path, head=100, keep_from=97, sync_height=50)
    assert bsc.tape_hop_state(c, pair, 100, now=1001.0) == (5 * 10 ** 21, 9 * 10 ** 20, 50)


def test_the_hop_pairs_are_the_create2_wbnb_pairs_of_the_configured_quotes(monkeypatch):
    from sniper.settings import memecoin as cfg
    assert tuple(cfg.MEMECOIN_BSC_FAST_LANE_HOP_QUOTES) == HOP_QUOTES
    pairs = bsc.hop_pairs()
    assert pairs == {pt._v2_pair(*pt.V2_FACTORIES[("56", bsc.PCS_V2_ROUTER)], WBNB, q): q
                     for q in HOP_QUOTES}


def test_the_watcher_prices_a_meme_quote_hop_from_the_tape_when_the_receipt_has_none(tmp_path, monkeypatch):
    from sniper.services import memecoin_bsc_v2_live as tape
    monkeypatch.setattr(tape, "db_path", lambda: tmp_path / "tape.db")
    c, _ = _tape(tmp_path, head=16, received=time.time())
    hop_pair = pt._v2_pair(*pt.V2_FACTORIES[("56", bsc.PCS_V2_ROUTER)], WBNB, QUOTE_X)
    tape.record_sync(c, hop_pair, 15, "0x%064x" % 15, 7 * 10 ** 21, 3 * 10 ** 20, time.time())
    receipt, mint, post = _synthetic(quote=QUOTE_X)
    sent = []
    signal = bsc.make_watcher_signal("runner_shadow", "\0a", send_fn=lambda a, p: sent.append(p))
    row = dict(ROW, buy=dict(ROW["buy"], mint=mint))
    out = signal(row, receipt, post, decimals=18, bnb_usd=600.0, bnb_usd_age_s=1.0)
    assert out["reason"] == "would_send" and out["hop"]["source"] == "tape", out
    assert sent and sent[0]["hop"]["pair"] == hop_pair


# ---- Gamma's hop_late measure: a tape-priced hop re-read after the decision ----

@pytest.mark.parametrize("seen, late", [(15, 0), (16, 1), (17, "masked"), (None, "unknown")])
def test_a_hop_is_late_only_when_its_pair_moved_into_the_used_height_to_the_source_block(seen, late):
    hop = {"pair": "0x" + "4b" * 20, "height": 15}
    assert bsc.hop_late_check(hop, 16, lambda pair: seen)["late"] == late


def test_the_watcher_rereads_a_tape_priced_hop_after_the_decision(tmp_path, monkeypatch):
    from sniper.services import memecoin_bsc_v2_live as tape
    monkeypatch.setattr(tape, "db_path", lambda: tmp_path / "tape.db")
    c, _ = _tape(tmp_path, head=16, received=time.time())
    hop_pair = pt._v2_pair(*pt.V2_FACTORIES[("56", bsc.PCS_V2_ROUTER)], WBNB, QUOTE_X)
    tape.record_sync(c, hop_pair, 15, "0x%064x" % 15, 7 * 10 ** 21, 3 * 10 ** 20, time.time())
    receipt, mint, post = _synthetic(quote=QUOTE_X)
    checks = []
    signal = bsc.make_watcher_signal("runner_shadow", "\0a", send_fn=lambda a, p: True,
                                     hop_check_fn=checks.append, delay_s=0.05)
    row = dict(ROW, buy=dict(ROW["buy"], mint=mint))
    out = signal(row, receipt, post, decimals=18, bnb_usd=600.0, bnb_usd_age_s=1.0)
    tape.record_sync(c, hop_pair, 16, "0x%064x" % 16, 8 * 10 ** 21, 3 * 10 ** 20, time.time())
    out["hop_check"].join(2.0)
    assert checks == [{"pair": hop_pair, "used": 15, "seen": 16, "seq": 16, "late": 1,
                       "tx_hash": ROW["tx_hash"]}]
    receipt, mint, post = _synthetic(quote=QUOTE_X, hop_sync=(5 * 10 ** 21, 9 * 10 ** 20))
    out = signal(dict(ROW, buy=dict(ROW["buy"], mint=mint)), receipt, post, decimals=18,
                 bnb_usd=600.0, bnb_usd_age_s=1.0)
    assert out["hop"]["source"] == "receipt" and "hop_check" not in out   # exact: nothing to check


def test_the_56_watcher_says_at_start_whether_the_lane_hook_is_armed(watch56, monkeypatch, capsys):
    w, events, wallet = watch56
    w._configure("56")
    for mode, word in (("runner_shadow", "ARMED"), ("", "off")):
        monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", mode)
        armed = w._bsc_lane_signal(False)
        line = capsys.readouterr().out
        assert ("[bsc-lane] hook %s: mode=%s" % (word, mode or "off")) in line, line
        assert (armed is not None) == (word == "ARMED")
        assert "socket=sniper-bsc-fast-lane-" in line and "\x00" not in repr(line)


@pytest.mark.parametrize("r_wbnb, fills", [(10 ** 23, True), (10 ** 18, False)], ids=["deep", "thin"])
def test_a_thin_hops_impact_counts_against_the_cap_so_it_reverts_never_overpays(r_wbnb, fills):
    # Gamma's money finding (Alpha's ruling): the cap leaves 3% over the coin leg's own execution
    # price. A deep hop fits; a 1-WBNB hop (25d887ce-like, ~7% impact at this clip) must revert.
    receipt, mint, post = _synthetic(quote=QUOTE_X, hop_sync=(5 * 10 ** 21, 9 * 10 ** 20))
    p = bsc.lane_payload(ROW, _decide(receipt, mint, post), decimals=18, bnb_usd=600.0,
                         bnb_usd_at=1.0, sent_epoch=1.0)
    p["hop"] = dict(p["hop"], r_wbnb=r_wbnb, r_quote=r_wbnb * 2000)    # one spot, two depths
    clip = 8 * 10 ** 16
    spot_quote = Fraction(clip) * 9975 / 10000 * 2000
    cap = spot_quote / bsc.v2_amount_out(int(spot_quote), p["r_quote"], p["r_coin"]) * Fraction(103, 100)
    p["cap_num"], p["cap_den"] = cap.numerator, cap.denominator
    keep = 1 - Fraction(p["tax_num"], p["tax_den"])
    got_quote = bsc.v2_amount_out(clip, p["hop"]["r_wbnb"], p["hop"]["r_quote"])   # the router's path
    got = int(bsc.v2_amount_out(got_quote, p["r_quote"], p["r_coin"]) * keep)
    min_out = bsc.min_out_for(clip, p)
    assert (got >= min_out) is fills, (got, min_out)
    if got >= min_out:                     # a fill never pays above the cap, the hop included
        assert spot_quote / (Fraction(got) / keep) <= cap


# ---- Gamma's sandwich finding (Alpha's ruling): the funded path's slippage, not the chase slack ----

@pytest.mark.parametrize("front_wei, fills", [(0, True), (5 * 10 ** 17, True), (25 * 10 ** 17, False)],
                         ids=["none", "1pct", "5pct"])
def test_a_front_run_past_the_entry_slippage_reverts_though_the_cap_had_room(front_wei, fills):
    receipt, mint, post = _synthetic()
    p = bsc.lane_payload(ROW, _decide(receipt, mint, post), decimals=18, bnb_usd=600.0,
                         bnb_usd_at=1.0, sent_epoch=1.0)
    clip = 8 * 10 ** 16
    keep = 1 - Fraction(p["tax_num"], p["tax_den"])
    r_q, r_c = p["r_quote"], p["r_coin"]
    cap = Fraction(r_q, r_c) * Fraction(13, 10)          # the 30% chase allowance, no move yet
    p["cap_num"], p["cap_den"] = cap.numerator, cap.denominator
    taken = bsc.v2_amount_out(front_wei, r_q, r_c) if front_wei else 0   # the sandwich's front leg
    got = int(bsc.v2_amount_out(clip, r_q + front_wei, r_c - taken) * keep)
    min_out = bsc.min_out_for(clip, p, slippage_pct=3)
    assert (got >= min_out) is fills, (got, min_out)
    assert int(Fraction(clip) / cap * keep) <= got     # the cap alone would have filled every case


def test_the_slippage_rule_reads_the_funded_paths_own_knob(monkeypatch):
    from sniper.settings import memecoin as cfg
    receipt, mint, post = _synthetic()
    p = bsc.lane_payload(ROW, _decide(receipt, mint, post), decimals=18, bnb_usd=600.0,
                         bnb_usd_at=1.0, sent_epoch=1.0)
    p["cap_num"], p["cap_den"] = 10 ** 60, 1                # the slippage rule decides alone
    for pct in (3.0, 7.0):
        monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_SLIPPAGE_PCT", pct)
        assert bsc.min_out_for(10 ** 17, p) == bsc.min_out_for(10 ** 17, p, slippage_pct=pct) > 0


def test_the_minimum_is_the_stricter_of_the_cap_and_the_slippage_rules():
    receipt, mint, post = _synthetic(quote=QUOTE_X, hop_sync=(5 * 10 ** 21, 9 * 10 ** 20))
    p = bsc.lane_payload(ROW, _decide(receipt, mint, post), decimals=18, bnb_usd=600.0,
                         bnb_usd_at=1.0, sent_epoch=1.0)
    clip, hop = 10 ** 17, p["hop"]
    keep = 1 - Fraction(p["tax_num"], p["tax_den"])
    exact = int(bsc.v2_amount_out(bsc.v2_amount_out(clip, hop["r_wbnb"], hop["r_quote"]),
                                  p["r_quote"], p["r_coin"]) * keep)
    assert bsc.expected_out_for(clip, p) == exact
    spot = Fraction(clip) * 9975 / 10000 * hop["r_quote"] / hop["r_wbnb"]
    cap_rule = int(spot / Fraction(p["cap_num"], p["cap_den"]) * keep)
    for pct in (0, 3, 50):
        assert bsc.min_out_for(clip, p, slippage_pct=pct) == max(
            cap_rule, int(Fraction(exact) * (100 - pct) / 100))


@pytest.mark.parametrize('clip_wei, fits', [(8 * 10**16, False), (10**15, True)])
def test_live_builder_skips_impossible_direct_hop_at_the_final_approved_clip(clip_wei, fits):
    receipt, mint, post = _synthetic(quote=QUOTE_X, hop_sync=(5 * 10**21, 9 * 10**20))
    payload = bsc.lane_payload(ROW, _decide(receipt, mint, post), decimals=18, bnb_usd=600.,
                               bnb_usd_at=1., sent_epoch=1.)
    payload['hop'] = dict(payload['hop'], r_wbnb=10**18, r_quote=2000 * 10**18)
    spot_quote = Fraction(8 * 10**16) * 9975 / 10000 * 2000
    cap = spot_quote / bsc.v2_amount_out(int(spot_quote), payload['r_quote'], payload['r_coin']) * Fraction(103, 100)
    payload['cap_num'], payload['cap_den'] = cap.numerator, cap.denominator
    live = object.__new__(bsc.BscLive)
    live.taker = ROW['wallet']
    tx, floor = live._buy_tx(payload, clip_wei, 60)
    if fits:
        assert tx and 0 < floor <= bsc.expected_out_for(clip_wei, payload)
    else:
        assert tx == {} and floor == 0
        from test_memecoin_bsc_fast_lane_live import _ledger
        from sniper.persistence import db_memecoin as db
        conn = _ledger()
        try:
            pending_id = db.reserve_pending(conn, mint=payload['mint'], source_user_id='uid',
                strategy='alpha_b', notional=48, reserved_at='2026-10-02T00:00:00+00:00',
                network_id='56', signal_source='bsc_fast_lane')
            live.clip_usd, live.host_clock = 50., lambda: 1.
            live._post_peak_nominated = lambda *args: False
            result = live.execute_reserved(payload, {}, conn, pending_id, 48., decimals=18)
            assert result['status'] == 'aborted'
            row = db.pending_row(conn, pending_id)
            assert row['status'] == 'failed' and row['resolution_reason'] == 'bsc_fast_build_refused'
            assert row['tx_sig'] is None and row['signed_payload_sha256'] is None
        finally:
            conn.close()
