"""BSC receipt split recovery and local pending-nonce reuse keep their original guards."""
import os
import time
from fractions import Fraction

import pytest
from eth_abi import decode

from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_evm_post_trade as pt
from test_memecoin_bsc_fast_lane import ROW, _synthetic
from test_memecoin_bsc_fast_lane_live import _paid_row, live56  # noqa: F401


def _topic(address):
    return "0x" + "0" * 24 + address[2:]


def _words(*values):
    return "0x" + "".join(format(value % 2**256, "064x") for value in values)


def _multi_venue_receipt(*, tax=False):
    receipt, mint, _ = _synthetic(tax=tax)
    pool, router = "0x" + "aa" * 20, "0x" + "0d" * 20
    amount = 10**24
    receipt["logs"] += [
        {"address": bsc.WBNB, "topics": [pt.TRANSFER, _topic(router), _topic(pool)],
         "data": _words(10**18)},
        {"address": mint, "topics": [pt.TRANSFER, _topic(pool), _topic(ROW["wallet"])],
         "data": _words(amount)},
        {"address": pool, "topics": [pt.V3_SWAP, _topic(router), _topic(router)],
         "data": _words(-amount, 10**18, 2**96, 10**25, 0)},
    ]
    return receipt, mint


def _decide(receipt, mint, *, owner=ROW["wallet"]):
    post = pt.source_post_trade("56", receipt, mint, owner=owner)
    return post, bsc.decide_from_receipt(receipt, mint=mint, evm_post=post, owner=owner,
                                        chase_pct=30, premium=1.5, seq=16)


def test_decoder_multiple_venue_result_uses_the_exact_single_pcs_v2_leg():
    receipt, mint = _multi_venue_receipt()
    post, out = _decide(receipt, mint)
    assert post == {"reason": "split_route"}
    assert out["reason"] == "would_send", out
    assert out["split"] is True and out["pool"] == bsc.our_pair(mint)
    assert out["hop"] is None and out["tax"] == 0
    assert out["cap"] == min(Fraction(out["r_quote"], out["r_coin"]) * Fraction(13, 10),
                              Fraction(out["src_in"], out["src_out"]) * Fraction(3, 2))


def test_multiple_venue_receipt_reaches_native_signer_with_the_same_floor(live56):
    import rlp
    from sniper.util.clock import real_utc_now_iso

    lane, ledger, net, sender, _prime = live56
    receipt, mint = _multi_venue_receipt()
    row = dict(ROW, now=real_utc_now_iso(), buy=dict(ROW["buy"], mint=mint))
    row = _paid_row(row, receipt)
    post = pt.source_post_trade("56", receipt, mint, owner=row["wallet"])
    records, payloads = [], []

    def send(_address, payload):
        payloads.append(payload)
        records.append(lane["dispatch"].on_datagram(payload, os.getpid(), os.getuid(), time.time()))
        return True

    signal = bsc.make_watcher_signal("live", "\0unused", send_fn=send)
    out = signal(row, receipt, post, decimals=18, bnb_usd=600.0, bnb_usd_age_s=1.0)
    assert out["reason"] == "would_send" and out["split"] and out["sent"], out
    assert len(sender.raws) == 1, records
    fields = rlp.decode(bytes.fromhex(sender.raws[0][2:]))
    minimum, path, recipient, _deadline = decode(
        ["uint256", "address[]", "address", "uint256"], fields[5][4:])
    clip = int.from_bytes(fields[4], "big")
    assert minimum == bsc.min_out_for(clip, payloads[0])
    assert list(path) == [bsc.WBNB, mint] and recipient == lane["live"].ctx["address"].lower()
    assert [call for call in net.calls if not call[1]] == []
    attempt = ledger.execute("SELECT signal_source, terminal_status FROM memecoin_entry_attempts").fetchone()
    assert tuple(attempt) == ("bsc_fast_lane", "aborted")  # Fixture transport rejects, no real send.


@pytest.mark.parametrize("case, expected", [
    ("second_v2", "split:no_single_v2_leg"),
    ("owner_forwarded", "split:owner_short"),
    ("unknown_tax", "tax_unknown"),
    ("other_factory", "not_our_pair"),
    ("no_sync", "pair_state_unreadable"),
    ("missing_owner", "post:split_route"),
    ("malformed", "post:decode_error"),
])
def test_split_recovery_keeps_ambiguous_unbound_tax_and_malformed_receipts_on_fallback(case, expected):
    receipt, mint = _multi_venue_receipt(tax=case == "unknown_tax")
    pair, owner = bsc.our_pair(mint), ROW["wallet"]
    if case == "second_v2":
        other = "0x" + "ab" * 20
        receipt["logs"] += [
            {"address": mint, "topics": [pt.TRANSFER, _topic(other), _topic(owner)],
             "data": _words(10**18)},
            {"address": other, "topics": [pt.V2_SWAP, _topic(owner), _topic(owner)],
             "data": _words(0, 10**18, 10**18, 0)},
        ]
    elif case == "owner_forwarded":
        receipt["logs"].append({"address": mint, "topics": [pt.TRANSFER, _topic(owner),
                                                               _topic("0x" + "cd" * 20)],
                                "data": _words(3 * 10**25)})
    elif case == "unknown_tax":
        receipt["logs"][1]["topics"][2] = _topic("0x" + "12" * 20)
    elif case == "other_factory":
        other = "0x" + "ef" * 20
        for log in receipt["logs"]:
            log["address"] = other if log["address"] == pair else log["address"]
            log["topics"] = [_topic(other) if topic == _topic(pair) else topic for topic in log["topics"]]
    elif case == "no_sync":
        receipt["logs"] = [log for log in receipt["logs"] if log["topics"][0] != pt.V2_SYNC]
    elif case == "malformed":
        receipt["logs"][1]["data"] = "0xnothex"
    post = pt.source_post_trade("56", receipt, mint, owner=owner)
    assert post == {"reason": "decode_error" if case == "malformed" else "split_route"}
    out = bsc.decide_from_receipt(receipt, mint=mint, evm_post=post,
                                   owner=None if case == "missing_owner" else owner,
                                   chase_pct=30, premium=1.5, seq=16)
    assert out["reason"] == expected, out


def test_split_native_payload_without_whole_source_proof_never_reserves_or_signs(live56):
    lane, ledger, _net, sender, _prime = live56
    receipt, mint = _multi_venue_receipt()
    _post, out = _decide(receipt, mint)
    assert out["reason"] == "would_send", out
    payload = bsc.lane_payload(dict(ROW, buy=dict(ROW["buy"], mint=mint)), out,
                               decimals=18, bnb_usd=600.0, bnb_usd_at=time.time(), sent_epoch=time.time())
    result = lane["live"].handle(payload, recv_epoch=time.time())
    assert result["reason"] == "source_initiation_unknown:missing_receipt_proof"
    assert not sender.raws
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def _snapshot_live():
    live = bsc.BscLive.__new__(bsc.BscLive)
    address = "0x" + "56" * 20
    live.ctx = {"address": address}
    live.snapshot = bsc.Snapshot(lambda *args: pytest.fail("callback made a provider request"),
                                 address, clock=lambda: 1000.0)
    live.snapshot._last = dict(native_wei=10**18, weth_wei=0, nonce=0,
                               gas_price_wei=10**8, read_at=999.0)
    live.host_clock = lambda: 1000.0
    live.max_snapshot_age_s, live.generation_margin_s = 30.0, 1.0
    live.generation_fn = lambda: pytest.fail("callback read execution files")
    return live


def test_signing_snapshot_is_a_copied_local_record_and_nonce_zero_is_valid():
    live = _snapshot_live()
    snap = live.entry_signing_snapshot()
    assert snap == {"chain": "56", "address": live.ctx["address"], "nonce": 0,
                    "read_at": 999.0, "max_age_s": 30.0, "generation_margin_s": 1.0}
    snap["nonce"] = 999
    assert live.snapshot.current()["nonce"] == 0


@pytest.mark.parametrize("field, value", [
    ("nonce", None), ("nonce", True), ("nonce", -1), ("nonce", "0"),
    ("native_wei", None), ("native_wei", -1), ("weth_wei", False),
    ("gas_price_wei", 0), ("gas_price_wei", "100"),
    ("read_at", None), ("read_at", True), ("read_at", "999"),
    ("read_at", float("nan")), ("read_at", float("inf")),
    ("read_at", 1000.01), ("read_at", 969.99),
])
def test_incomplete_or_stale_signing_snapshot_is_unknown(field, value):
    live = _snapshot_live()
    live.snapshot._last[field] = value
    assert live.entry_signing_snapshot() is None


def test_missing_snapshot_or_wrong_custody_is_unknown():
    live = _snapshot_live()
    live.snapshot._last = None
    assert live.entry_signing_snapshot() is None
    live = _snapshot_live()
    live.snapshot.address = "0x" + "99" * 20
    assert live.entry_signing_snapshot() is None


def test_live_startup_installs_callback_on_original_funded_context(live56):
    lane, _ledger, _net, _sender, _prime = live56
    live = lane["live"]
    assert live.ctx["entry_signing_snapshot_fn"].__self__ is live
    assert live.ctx["entry_signing_snapshot_fn"]()["nonce"] == 42
