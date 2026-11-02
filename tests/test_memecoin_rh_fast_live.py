"""4663 fast lane M2 step 3: the live lane (spec 2026-10-01-memecoin-4663-fast-lane-design.md
§2.5-§2.7; Alpha's invariants 2026-10-01). A lane fill is booked from its own canonical receipt
(paid = the transaction's native value, got = the coin the PoolManager sent us), never from a
whole-wallet balance delta, through the same one-transaction booking as the Ink recovery."""
import sqlite3

import pytest

from sniper.execution import evm_swap_executor as ex
from sniper.persistence import db_memecoin as db
from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC

HASH = "0x" + "a" * 64
BLOCK = "0x" + "b" * 64
OWNER = "0x" + "1" * 40
MINT = "0x" + "3" * 40
PM = "0x8366a39cc670b4001a1121b8f6a443a643e40951"                 # 4663 V4 PoolManager
UR = "0x8876789976decbfcbbbe364623c63652db8c0904"
PAID = 2 * 10 ** 16                                                 # 0.02 ETH: $50 at 2500


def _transfer(token, sender, recipient, amount, index):
    return {"address": token, "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + sender[2:],
                                         "0x" + "0" * 24 + recipient[2:]],
            "data": hex(amount), "transactionHash": HASH, "blockHash": BLOCK,
            "logIndex": hex(index)}


def _bound(conn, notional=50.0):
    pending_id = db.reserve_pending(conn, mint=MINT, source_user_id="src", strategy="alpha_b",
                                    notional=notional, reserved_at="2026-10-02T00:00:00Z",
                                    network_id="4663")
    assert db.bind_entry_submission(
        conn, pending_id, tx_identity=HASH, signed_payload_b64="0xraw", signed_payload_sha256="a" * 64,
        blockhash=None, last_valid_height=None, nonce=7, backend="rh_fast_sequencer",
        bound_at="2026-10-02T00:00:01Z", evm_quote_asset_usd=2500.0)
    return pending_id


def _chain(got=5 * 10 ** 18, value=PAID, status="0x1", sender=PM, extra_logs=()):
    receipt = {"transactionHash": HASH, "blockHash": BLOCK, "blockNumber": "0x10", "status": status,
               "from": OWNER, "logs": [_transfer(MINT, sender, OWNER, got, 1), *extra_logs]}
    tx = {"hash": HASH, "from": OWNER, "blockHash": BLOCK, "blockNumber": "0x10", "to": UR,
          "value": hex(value)}
    block = {"hash": BLOCK, "number": "0x10", "timestamp": hex(1790900000)}
    return receipt, tx, block


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(c)
    return c


def test_a_lane_buy_is_booked_once_from_its_receipt(conn):
    pending_id = _bound(conn)
    receipt, tx, block = _chain()
    got = ex.recover_confirmed_lane_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                          owner=OWNER, decimals_fn=lambda _m: 18)
    assert got["status"] == "filled"
    pos = conn.execute("SELECT * FROM memecoin_positions").fetchone()
    assert pos["entry_notional"] == pytest.approx(50.0) and db.open_qty(conn, got["position_id"]) == 5.0
    assert pos["network_id"] == "4663" and db.pending_row(conn, pending_id)["status"] == "confirmed"
    assert ex.recover_confirmed_lane_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                           owner=OWNER, decimals_fn=lambda _m: 18) is None
    assert conn.execute("SELECT count(*) FROM memecoin_positions").fetchone()[0] == 1


@pytest.mark.parametrize("chain_kw, why", [
    (dict(status="0x0"), "a reverted buy got nothing"),
    (dict(sender="0x" + "9" * 40), "the coin did not come from the PoolManager"),
    (dict(value=0), "nothing was paid"),
    (dict(value=PAID * 3), "paid more than the reservation allows"),
    (dict(got=0), "no coin arrived"),
    (dict(extra_logs=[_transfer("0x" + "4" * 40, PM, OWNER, 1, 2)]), "a second coin arrived"),
])
def test_anything_but_one_clean_paid_buy_stays_reserved(conn, chain_kw, why):
    pending_id = _bound(conn)
    receipt, tx, block = _chain(**chain_kw)
    assert ex.recover_confirmed_lane_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                           owner=OWNER, decimals_fn=lambda _m: 18) is None, why
    assert db.pending_row(conn, pending_id)["status"] == "reserved"


def test_a_receipt_for_another_transaction_or_sender_is_refused(conn):
    pending_id = _bound(conn)
    receipt, tx, block = _chain()
    tx["from"] = "0x" + "8" * 40
    assert ex.recover_confirmed_lane_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                           owner=OWNER, decimals_fn=lambda _m: 18) is None


# ---- the lane's nonce: the snapshot's pending count, claimed through the shared floor ----

def _ctx(monkeypatch, tmp_path, pipelined=True):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from sniper.settings import memecoin as cfg
    from test_memecoin_evm_context_keys import _production_context
    monkeypatch.setattr(cfg, "memecoin_evm_approval_pipeline", lambda chain=None: pipelined)
    return _production_context(monkeypatch, tmp_path, "4663")


def test_the_lane_claims_the_snapshot_nonce_through_the_shared_floor(monkeypatch, tmp_path):
    ctx = _ctx(monkeypatch, tmp_path)
    claim, release = ctx["lane_claim_nonce_fn"], ctx["lane_release_nonce_fn"]
    assert claim(42) == 42                       # no fresh floor: the snapshot's count
    assert claim(42) is None                     # our 42 is in flight (floor 43 > count 42)
    assert claim(44) == 44                       # another process sent 42-43: the newer count wins
    assert release(44) is True and claim(44) == 44
    assert claim(None) is None


def test_without_the_pipeline_floor_the_lane_never_signs(monkeypatch, tmp_path):
    ctx = _ctx(monkeypatch, tmp_path, pipelined=False)
    assert ctx["lane_claim_nonce_fn"](42) is None


def test_the_context_carries_the_gas_buffer_today_s_signer_uses(monkeypatch, tmp_path):
    assert _ctx(monkeypatch, tmp_path)["gasprice_buffer"] > 1.0


# ---- the live lane: in-memory checks, reserve, write-ahead, sequencer-first send, receipt booking ----

import time  # noqa: E402
from fractions import Fraction  # noqa: E402

from eth_account import Account  # noqa: E402
from eth_utils import keccak  # noqa: E402

from sniper.services import memecoin_rh_fast_lane as lane  # noqa: E402

ACCT = Account.from_key(b"\x05" * 32)
KEY = dict(currency0="0x" + "00" * 20, currency1=MINT, fee=0, tick_spacing=200,
           hooks="0xe5e702641ea86f4ae6cc3cdaed2b886f976be044")
SQRT = 874745060896749537239184449426278
SNAP = dict(native_wei=10 ** 17, weth_wei=5 * 10 ** 16, nonce=42, gas_price_wei=40_000_000,
            read_at=1000.0)
PAYLOAD = dict(tx_hash="0x" + "c" * 64, seq=1000, wallet="0x" + "ef" * 20, mint=MINT, handle="Degen",
               source_user_id="uid", permit_nonce=9, arrival_utc="2026-10-02T00:00:00+00:00",
               pool="0x" + "a1" * 32, key=KEY, cap_num=7, cap_den=3 * 10 ** 6, clip_wei=10 ** 16,
               min_out=1, state_kind="tape_post", tape_lag_blocks=0, sent_epoch=1000.0,
               snapshot=SNAP, eth_usd=2500.0, eth_usd_at=1000.0, post_sqrt=SQRT,
               src_in_wei=25854436920000000, src_out=3182688391454788035426447)
from rh_source_fill_fixture import proof_for, bind_native_prices
PAYLOAD["paid_usd"] = 65.3
bind_native_prices(PAYLOAD)


class Sender:
    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def send(self, raw):
        self.sent.append(raw)
        return self.replies.pop(0)


def _ledger():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(c)
    return c


class Chain:
    """Receipt, transaction and block answers for whatever raw the lane sent."""
    def __init__(self, status="0x1", got=5 * 10 ** 18, appears_after=0):
        self.raw, self.status, self.got, self.appears_after, self.asks = None, status, got, appears_after, 0

    def read(self, method, params):
        if self.raw is None:
            return None
        h = "0x" + keccak(hexstr=self.raw).hex()
        if params[0] not in (h, "0x3f2"):
            return None
        if method == "eth_getTransactionReceipt":
            self.asks += 1
            if self.asks <= self.appears_after:
                return None
            return {"transactionHash": h, "blockHash": BLOCK, "blockNumber": "0x3f2", "status": self.status,
                    "transactionIndex": "0x5",
                    "from": ACCT.address.lower(), "logs": [dict(_transfer(MINT, PM, ACCT.address.lower(),
                                                                         self.got, 1), transactionHash=h)]}
        if method == "eth_getTransactionByHash":
            return {"hash": h, "from": ACCT.address.lower(), "blockHash": BLOCK, "blockNumber": "0x3f2",
                    "to": UR, "value": hex(lane.signed_value(self.raw))}
        if method == "eth_getBlockByNumber":
            return {"hash": BLOCK, "number": "0x3f2", "timestamp": hex(1790900000)}
        return None


def _live(conn, sender, chain, **kw):
    from sniper.execution.evm_real_seams import sign_tx
    claimed = []

    def claim(n):
        claimed.append(n)
        return n
    released = []

    def send_and_remember(raw):
        chain.raw = raw
        return sender_orig(raw)
    sender_orig = sender.send
    sender.send = send_and_remember
    args = dict(
        ctx={"address": ACCT.address, "sign_tx_fn": lambda tx: sign_tx(tx, ACCT),
             "lane_claim_nonce_fn": claim, "lane_release_nonce_fn": released.append,
             "gasprice_buffer": 1.5},
        conn=conn, sender=sender, fanout_fn=lambda raw: chain.__setattr__("raw", raw) or "0xhash",
        rpc_read=chain.read, facts_fn=lambda mint: (18, 10 ** 27), lock_key="k",
        lock_dir=None, clip_usd=50.0, low_wei=2 * 10 ** 15, generation_fn=lambda: 900.0,
        host_clock=lambda: 1005.0, max_snapshot_age_s=30.0, eth_usd_max_age_s=120.0,
        generation_margin_s=1.0, pilot_fills=20, max_sends_per_hour=6, require_exact_state=True,
        receipt_timeout_s=1.0, receipt_poll_s=0.01, lock_receipt_cap_s=0.2, sleep=lambda s: None)
    args.update(kw)
    live = lane.LiveLane(**args)
    live._claimed, live._released = claimed, released
    return live


def _reserved(conn, notional=50.0):
    return db.reserve_pending(conn, mint=MINT, source_user_id="uid", strategy="alpha_b",
                              notional=notional, reserved_at="2026-10-02T00:00:00+00:00",
                              network_id="4663", signal_source="rh_fast_lane",
                              origin_event_id="rhfast:4663:%s:%s" % ("0x" + "c" * 64, MINT))


def _run(live, conn, trade_size=50.0, payload=None):
    """The leg's call into the lane executor for a reservation the hook made."""
    pending_id = _reserved(conn)
    rec = {"_perf0": time.perf_counter()}
    result = live.execute_reserved(dict(payload or PAYLOAD), rec, conn, pending_id, trade_size,
                                   decimals=18)
    return result, rec


def test_a_clean_signal_is_bound_sent_and_booked_from_its_receipt(tmp_path):
    conn = _ledger()
    chain = Chain()
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path))
    result, rec = _run(live, conn)
    assert result["status"] == "filled" and rec["_outcome"] == "filled", result
    assert live._claimed == [42] and len(live.sender.sent) == 1
    pending = conn.execute("SELECT * FROM memecoin_pending_entries").fetchone()
    assert pending["status"] == "confirmed" and pending["signal_source"] == "rh_fast_lane"
    assert pending["tx_sig"] == rec["tx_hash"] and pending["evm_quote_asset_usd"] == 2500.0
    pos = conn.execute("SELECT * FROM memecoin_positions").fetchone()
    assert pos["entry_notional"] == pytest.approx(50.0, rel=1e-6)
    assert rec["blocks_behind"] == 0x3f2 - 1000 and live.fills == 1
    # Gamma's pilot stop rule: our block and the exact fill from our own receipt
    assert rec["our_block"] == 0x3f2 and rec["our_tx_index"] == 5
    assert (rec["paid_wei"], rec["got_atomic"]) == (rec["clip_wei"], 5 * 10 ** 18)


def test_an_unbound_receipt_booking_keeps_the_exact_fill_too(tmp_path):
    conn = _ledger()
    chain = Chain()
    chain_read = chain.read

    def read(method, params):
        r = chain_read(method, params)
        if method == "eth_getTransactionReceipt" and r:
            r["logs"].append(dict(_transfer("0x" + "4" * 40, "0x" + "9" * 40, ACCT.address.lower(), 7, 2),
                                  transactionHash=r["transactionHash"]))
        return r
    balances = {0x3f1: 10 ** 18, 0x3f2: 6 * 10 ** 18}
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), rpc_read=read,
                 alert_fn=lambda t: None, balance_at_fn=lambda mint, block: balances.get(block))
    _result, rec = _run(live, conn)
    assert rec["_outcome"] == "filled_from_receipt_net"
    assert (rec["paid_wei"], rec["got_atomic"]) == (rec["clip_wei"], 5 * 10 ** 18)


def test_the_clip_is_the_approved_size_capped_at_the_pilot_clip(tmp_path):
    conn, chain = _ledger(), Chain()
    small = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path))
    _result, rec = _run(small, conn, trade_size=20.0)
    assert rec["clip_wei"] == 20 * 10 ** 18 // 2500                  # less approved: less spent
    conn, chain = _ledger(), Chain()
    big = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path))
    _result, rec = _run(big, conn, trade_size=400.0)
    assert rec["clip_wei"] == 50 * 10 ** 18 // 2500                  # never above the pilot clip


def _accepting_sender():
    s = Sender()
    s.send = lambda raw: s.sent.append(raw) or ("accepted", "0x" + keccak(hexstr=raw).hex())
    return s


def _pending(conn):
    return conn.execute("SELECT * FROM memecoin_pending_entries").fetchone()


def test_a_proven_refusal_releases_the_reservation_and_the_nonce(tmp_path):
    conn, chain = _ledger(), Chain()
    s = Sender(("rejected", "insufficient funds for gas * price + value"))
    live = _live(conn, s, chain, lock_dir=str(tmp_path))
    result, rec = _run(live, conn)
    assert rec["_outcome"].startswith("rejected:") and live._released == [42]
    assert result["status"] == "aborted"
    assert _pending(conn)["status"] == "failed" and live.fills == 0
    assert conn.execute("SELECT count(*) FROM memecoin_positions").fetchone()[0] == 0


def test_an_ambiguous_reply_offers_the_same_bytes_and_settles_by_receipt(tmp_path):
    conn, chain = _ledger(), Chain(appears_after=3)
    s = Sender(("ambiguous", "transport: TimeoutError"))
    offered = []
    live = _live(conn, s, chain, lock_dir=str(tmp_path),
                 fanout_fn=lambda raw: offered.append(raw) or None)
    result, rec = _run(live, conn)
    assert offered == s.sent and len(s.sent) == 1 and result["status"] == "filled"
    assert live._released == []                              # an ambiguous send keeps its nonce


def test_no_receipt_in_time_is_submitted_unknown_and_stays_bound(tmp_path):
    conn, chain = _ledger(), Chain(appears_after=10 ** 6)
    alerts = []
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), receipt_timeout_s=0.05,
                 lock_receipt_cap_s=0.01, alert_fn=alerts.append, sleep=time.sleep)
    result, rec = _run(live, conn)
    assert rec["_outcome"] == "submitted_unknown" and result["status"] == "unknown"
    p = _pending(conn)
    assert p["status"] == "reserved" and p["tx_sig"] == rec["tx_hash"] and live._released == []
    assert alerts and "lock released" in alerts[0]


def test_a_reverted_buy_frees_the_reservation(tmp_path):
    conn, chain = _ledger(), Chain(status="0x0")
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path))
    result, rec = _run(live, conn)
    assert rec["_outcome"] == "reverted" and result["status"] == "failed"
    assert _pending(conn)["status"] == "failed"


@pytest.mark.parametrize("payload, kw, reason", [
    (dict(state_kind="pre"), {}, "estimated_state"),
    ({}, dict(fills=20), "pilot_complete"),
    ({}, dict(send_times=[1000.0] * 6), "pilot_rate_cap"),
    ({}, dict(facts_fn=lambda mint: None), "no_mcap"),
    (dict(eth_usd=None), {}, "no_eth_usd"),
    (dict(snapshot=dict(SNAP, read_at=900.5)), {}, "snapshot_predates_execution"),
])
def test_in_memory_fallbacks_never_reach_the_hook(tmp_path, payload, kw, reason):
    conn, chain = _ledger(), Chain()
    s = _accepting_sender()

    def hook_factory(contexts, stats):
        raise AssertionError("an in-memory fallback reached the funded hook")
    live = _live(conn, s, chain, lock_dir=str(tmp_path), hook_factory=hook_factory, **kw)
    rec = live.handle(dict(PAYLOAD, **payload), recv_epoch=1000.0, safety_reason=None,
                      stats=({"price": 1.0}, 1000.0))
    assert rec["reason"] == reason and s.sent == [] and live._claimed == [] and _pending(conn) is None


def test_a_safety_fallback_never_reaches_the_hook(tmp_path):
    conn, chain = _ledger(), Chain()

    def hook_factory(contexts, stats):
        raise AssertionError("a safety fallback reached the funded hook")
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), hook_factory=hook_factory)
    rec = live.handle(dict(PAYLOAD), recv_epoch=1000.0, safety_reason="safety_stale",
                      stats=({"price": 1.0}, 1000.0))
    assert rec["reason"] == "safety_stale"


def test_a_busy_lock_releases_the_reservation_and_sends_nothing(tmp_path):
    from sniper.execution.exec_lock import wallet_exec_lock
    conn, chain = _ledger(), Chain()
    s = _accepting_sender()
    live = _live(conn, s, chain, lock_dir=str(tmp_path))
    with wallet_exec_lock("k", lock_dir=str(tmp_path)):
        result, rec = _run(live, conn)
    assert rec["_outcome"] == "lock_busy" and rec["lock_busy"] == 1 and s.sent == []
    assert result["status"] == "aborted" and _pending(conn)["status"] == "failed"
    assert live._claimed == []                               # no nonce is claimed outside the lock


def test_a_nonce_in_flight_releases_the_reservation_before_any_signature(tmp_path):
    conn, chain = _ledger(), Chain()
    s = _accepting_sender()
    live = _live(conn, s, chain, lock_dir=str(tmp_path))
    live.ctx["lane_claim_nonce_fn"] = lambda n: None
    result, rec = _run(live, conn)
    assert rec["_outcome"] == "nonce_in_flight" and s.sent == []
    assert _pending(conn)["status"] == "failed"


def test_a_raise_before_the_send_gives_back_the_nonce_and_the_reservation(tmp_path):
    conn, chain = _ledger(), Chain()
    s = _accepting_sender()
    live = _live(conn, s, chain, lock_dir=str(tmp_path))

    def broken(tx):
        raise RuntimeError("signer down")
    live.ctx["sign_tx_fn"] = broken
    with pytest.raises(RuntimeError):
        _run(live, conn)
    assert s.sent == [] and live._released == [42] and _pending(conn)["status"] == "failed"


# ---- Gamma MEDIUM on 3a: a status-1 buy the receipt rule refuses is never left unbooked silently ----

def test_a_receipt_the_rule_refuses_books_its_own_net_never_the_block_delta(tmp_path):
    # Codex EVM-03: our buy got 4 coins through a router (not from the PoolManager, so the strict
    # rule refuses) and another transaction in the same block sent us 100 more. The block delta
    # (104) is the wallet's, not this swap's; only this transaction's own receipt is.
    conn = _ledger()
    chain = Chain(got=4 * 10 ** 18)
    chain_read = chain.read

    def read(method, params):
        r = chain_read(method, params)
        if method == "eth_getTransactionReceipt" and r:
            r["logs"][0]["topics"][1] = "0x" + "0" * 24 + "9" * 40       # a router, not the PM
        return r
    alerts = []
    balances = {0x3f1: 10 ** 18, 0x3f2: 105 * 10 ** 18}
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), rpc_read=read,
                 alert_fn=alerts.append, balance_at_fn=lambda mint, block: balances.get(block))
    result, rec = _run(live, conn)
    assert rec["_outcome"] == "filled_from_receipt_net" and alerts
    assert result["status"] == "filled" and rec["got_atomic"] == 4 * 10 ** 18
    pos = conn.execute("SELECT * FROM memecoin_positions").fetchone()
    assert db.open_qty(conn, pos["id"]) == 4.0 and _pending(conn)["status"] == "confirmed"
    assert pos["entry_notional"] == pytest.approx(50.0, rel=1e-6)


def test_a_receipt_nothing_can_prove_is_held_even_with_a_block_delta(tmp_path):
    conn = _ledger()
    chain = Chain()
    chain_read = chain.read

    def read(method, params):
        r = chain_read(method, params)
        if method == "eth_getTransactionReceipt" and r:    # we also paid another token: cost unknown
            r["logs"].append(dict(_transfer("0x" + "4" * 40, ACCT.address.lower(), "0x" + "9" * 40, 7, 2),
                                  transactionHash=r["transactionHash"]))
        return r
    alerts = []
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), rpc_read=read,
                 alert_fn=alerts.append,
                 balance_at_fn=lambda mint, block: {0x3f1: 0, 0x3f2: 5 * 10 ** 18}[block])
    result, rec = _run(live, conn)
    assert rec["_outcome"] == "booking_held" and "reconcile" in alerts[-1]
    assert result["status"] == "unknown" and _pending(conn)["status"] == "reserved"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


def test_without_a_balance_delta_the_buy_is_held_loudly_for_reconcile(tmp_path):
    conn, chain = _ledger(), Chain(got=0)
    alerts = []
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), alert_fn=alerts.append,
                 balance_at_fn=lambda mint, block: None)
    result, rec = _run(live, conn)
    assert rec["_outcome"] == "booking_held" and alerts and "reconcile" in alerts[-1]
    assert result["status"] == "unknown"
    assert _pending(conn)["status"] == "reserved"


def test_a_receipt_booking_is_checked_against_the_block_pinned_delta(tmp_path):
    conn, chain = _ledger(), Chain()
    alerts = []
    checks = []
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), alert_fn=alerts.append,
                 balance_at_fn=lambda mint, block: {0x3f1: 0, 0x3f2: 4 * 10 ** 18}[block],
                 check_sink=checks.append)
    result, rec = _run(live, conn)
    live.join_checks()
    assert rec["_outcome"] == "filled"
    (check,) = checks
    assert check["receipt_got"] == 5 * 10 ** 18 and check["delta_got"] == 4 * 10 ** 18
    assert check["agree"] is False and alerts and "disagree" in alerts[0]


# ---- Gamma LOWs on 3a: the every-log check and the network guards ----

def test_a_log_from_another_transaction_or_block_refuses_the_booking(conn):
    pending_id = _bound(conn)
    receipt, tx, block = _chain()
    receipt["logs"][0]["transactionHash"] = "0x" + "e" * 64
    assert ex.recover_confirmed_lane_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                           owner=OWNER, decimals_fn=lambda _m: 18) is None
    receipt, tx, block = _chain()
    receipt["logs"][0]["blockHash"] = "0x" + "f" * 64
    assert ex.recover_confirmed_lane_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                           owner=OWNER, decimals_fn=lambda _m: 18) is None


def test_the_lane_and_ink_bookings_refuse_each_others_networks(conn):
    pending_id = _bound(conn)
    receipt, tx, block = _chain()
    conn.execute("UPDATE memecoin_pending_entries SET network_id='57073' WHERE id=?", (pending_id,))
    assert ex.recover_confirmed_lane_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                           owner=OWNER, decimals_fn=lambda _m: 18) is None
    assert ex.recover_confirmed_lane_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                           owner=OWNER, decimals_fn=lambda _m: 18, bound=False) is None
    # An Ink-shaped receipt (WETH paid to the pool, the coin back) on a 4663 reservation.
    weth, pool = "0x" + "4" * 40, "0x" + "2" * 40
    conn.execute("UPDATE memecoin_pending_entries SET network_id='4663' WHERE id=?", (pending_id,))
    receipt["logs"] = [_transfer(weth, OWNER, pool, 2 * 10 ** 16, 0), _transfer(MINT, pool, OWNER, 10 ** 18, 1)]
    tx.update(to=pool, value="0x0")
    assert ex.recover_confirmed_ink_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                          owner=OWNER, weth=weth, decimals_fn=lambda _m: 18) is None


# ---- a lane buy confirmed across a restart is booked by the 4663 recovery, never left a bag ----

def test_the_4663_recovery_books_a_bound_lane_row_from_its_receipt(monkeypatch, tmp_path):
    from sniper.execution import evm_real_seams as seams
    ctx = _ctx(monkeypatch, tmp_path)
    owner = ctx["address"].lower()
    conn = _ledger()
    pending_id = db.reserve_pending(conn, mint=MINT, source_user_id="src", strategy="alpha_b",
                                    notional=50.0, reserved_at="2026-10-02T00:00:00Z", network_id="4663",
                                    signal_source="rh_fast_lane")
    db.bind_entry_submission(conn, pending_id, tx_identity=HASH, signed_payload_b64="0xraw",
                             signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None,
                             nonce=7, backend="rh_fast_sequencer", bound_at="2026-10-02T00:00:01Z",
                             evm_quote_asset_usd=2500.0)
    receipt = {"transactionHash": HASH, "blockHash": BLOCK, "blockNumber": "0x10", "status": "0x1",
               "from": owner, "logs": [_transfer(MINT, PM, owner, 5 * 10 ** 18, 1)]}
    tx = {"hash": HASH, "from": owner, "blockHash": BLOCK, "blockNumber": "0x10", "to": UR,
          "value": hex(PAID)}
    block = {"hash": BLOCK, "number": "0x10", "timestamp": hex(1790900000)}
    answers = {"eth_getTransactionReceipt": receipt, "eth_getTransactionByHash": tx,
               "eth_getBlockByNumber": block, "eth_call": "0x" + "%064x" % 18}
    monkeypatch.setattr(seams, "_rpc", lambda url, method, params, http=None: {"result": answers[method]})
    monkeypatch.setattr(seams, "erc20_decimals", lambda mint, url, http=None: 18)
    got = ctx["recover_confirmed_entry_fn"](conn, db.pending_row(conn, pending_id), "2026-10-02T00:01:00Z")
    assert got and got["status"] == "filled"
    assert db.pending_row(conn, pending_id)["status"] == "confirmed"


def test_the_4663_recovery_books_an_unbound_receipt_from_its_net_and_reads_no_balance(monkeypatch, tmp_path):
    # Codex EVM-03 on the restart path: the receipt's own net, never a balanceOf block delta.
    from sniper.execution import evm_real_seams as seams
    ctx = _ctx(monkeypatch, tmp_path)
    owner = ctx["address"].lower()
    conn = _ledger()
    pending_id = db.reserve_pending(conn, mint=MINT, source_user_id="src", strategy="alpha_b",
                                    notional=50.0, reserved_at="2026-10-02T00:00:00Z", network_id="4663",
                                    signal_source="rh_fast_lane")
    db.bind_entry_submission(conn, pending_id, tx_identity=HASH, signed_payload_b64="0xraw",
                             signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None,
                             nonce=7, backend="rh_fast_sequencer", bound_at="2026-10-02T00:00:01Z",
                             evm_quote_asset_usd=2500.0)
    receipt = {"transactionHash": HASH, "blockHash": BLOCK, "blockNumber": "0x10", "status": "0x1",
               "from": owner, "logs": [_transfer(MINT, "0x" + "9" * 40, owner, 4 * 10 ** 18, 1)]}
    tx = {"hash": HASH, "from": owner, "blockHash": BLOCK, "blockNumber": "0x10", "to": UR,
          "value": hex(PAID)}
    block = {"hash": BLOCK, "number": "0x10", "timestamp": hex(1790900000)}
    calls = []

    def rpc(url, method, params, http=None):
        calls.append(method)
        return {"result": {"eth_getTransactionReceipt": receipt, "eth_getTransactionByHash": tx,
                           "eth_getBlockByNumber": block}.get(method, "0x" + "%064x" % (104 * 10 ** 18))}
    monkeypatch.setattr(seams, "_rpc", rpc)
    monkeypatch.setattr(seams, "erc20_decimals", lambda mint, url, http=None: 18)
    got = ctx["recover_confirmed_entry_fn"](conn, db.pending_row(conn, pending_id), "2026-10-02T00:01:00Z")
    assert got and got["status"] == "filled" and "eth_call" not in calls
    pos = conn.execute("SELECT * FROM memecoin_positions").fetchone()
    assert db.open_qty(conn, pos["id"]) == 4.0


def test_the_4663_recovery_leaves_an_unconfirmed_lane_row_alone(monkeypatch, tmp_path):
    from sniper.execution import evm_real_seams as seams
    ctx = _ctx(monkeypatch, tmp_path)
    conn = _ledger()
    pending_id = db.reserve_pending(conn, mint=MINT, source_user_id="src", strategy="alpha_b",
                                    notional=50.0, reserved_at="2026-10-02T00:00:00Z", network_id="4663",
                                    signal_source="rh_fast_lane")
    db.bind_entry_submission(conn, pending_id, tx_identity=HASH, signed_payload_b64="0xraw",
                             signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None,
                             nonce=7, backend="rh_fast_sequencer", bound_at="2026-10-02T00:00:01Z",
                             evm_quote_asset_usd=2500.0)
    monkeypatch.setattr(seams, "_rpc", lambda url, method, params, http=None: {"result": None})
    assert ctx["recover_confirmed_entry_fn"](conn, db.pending_row(conn, pending_id),
                                             "2026-10-02T00:01:00Z") is None
    assert db.pending_row(conn, pending_id)["status"] == "reserved"


# ---- Alpha on Gamma's MEDIUM: the lock is held through the receipt, at most the cap ----

def _held_while(live, conn, chain, tmp_path, release_after):
    """Run the lane in a thread whose receipt appears only when `release_after` is set; returns
    (worker, out, asked)."""
    import threading
    release, asked = threading.Event(), threading.Event()
    real_read = chain.read

    def slow_read(method, params):
        if method == "eth_getTransactionReceipt":
            asked.set()
            if not release.is_set():
                return None
        return real_read(method, params)
    live.rpc_read = slow_read
    out = []
    worker = threading.Thread(target=lambda: out.append(_run(live, conn)), daemon=True)
    worker.start()
    return worker, out, asked, release


def test_an_exit_waits_for_a_lane_receipt_inside_the_cap(tmp_path):
    from sniper.execution.exec_lock import wallet_exec_lock
    conn, chain = _ledger(), Chain()
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), receipt_timeout_s=10.0,
                 lock_receipt_cap_s=5.0, sleep=time.sleep)
    worker, out, asked, release = _held_while(live, conn, chain, tmp_path, None)
    assert asked.wait(5.0)                                   # the lane is waiting for its receipt
    with wallet_exec_lock("k", lock_dir=str(tmp_path), blocking=False) as held:
        assert not held, "a 4663 executor took the lock while a lane buy had no receipt"
    release.set()
    worker.join(5.0)
    assert out and out[0][0]["status"] == "filled"
    with wallet_exec_lock("k", lock_dir=str(tmp_path), blocking=False) as held:
        assert held                                          # released once the receipt landed


def test_past_the_cap_the_lock_is_released_with_an_alert_and_the_buy_still_booked(tmp_path):
    from sniper.execution.exec_lock import wallet_exec_lock
    conn, chain = _ledger(), Chain()
    alerts = []
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), receipt_timeout_s=10.0,
                 lock_receipt_cap_s=0.2, sleep=time.sleep, alert_fn=alerts.append)
    worker, out, asked, release = _held_while(live, conn, chain, tmp_path, None)
    assert asked.wait(5.0)
    deadline = time.monotonic() + 5.0
    while not alerts and time.monotonic() < deadline:
        time.sleep(0.01)
    assert alerts and "lock released" in alerts[0]
    with wallet_exec_lock("k", lock_dir=str(tmp_path), blocking=False) as held:
        assert held, "past the cap a 4663 exit still waited on the lane's receipt"
    release.set()
    worker.join(5.0)
    assert out and out[0][0]["status"] == "filled" and _pending(conn)["status"] == "confirmed"



# ---- Alpha 2026-10-02: the operator's stop file stops the live lane before anything else ----

def test_a_stopped_lane_refuses_every_signal_before_any_check_or_hook(tmp_path):
    conn, chain = _ledger(), Chain()

    def no_hook(*a, **k):
        raise AssertionError("a stopped lane reached the funded hook")
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), hook_factory=no_hook)
    live.stopped = True
    rec = live.handle(dict(PAYLOAD), recv_epoch=1000.0, safety_reason=None, stats=None)
    assert rec["reason"] == "operator_stop" and live.sender.sent == []


@pytest.mark.parametrize('invalidated', ['age', 'generation'])
def test_snapshot_invalidated_before_wallet_lock_never_claims_nonce_or_signs(tmp_path, monkeypatch, invalidated):
    from contextlib import contextmanager
    from sniper.execution import exec_lock
    conn, chain = _ledger(), Chain()
    clock, generation = [1005.0], [900.0]
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path),
                 host_clock=lambda: clock[0], generation_fn=lambda: generation[0])
    original_lock = exec_lock.wallet_exec_lock

    @contextmanager
    def lock_after_other_work(*args, **kwargs):
        with original_lock(*args, **kwargs) as held:
            if invalidated == 'age':
                clock[0] = 1031.0
            else:
                generation[0] = 1001.0
            yield held

    monkeypatch.setattr(exec_lock, 'wallet_exec_lock', lock_after_other_work)
    result, record = _run(live, conn)
    assert result['status'] == 'aborted'
    assert record['_outcome'] == ('snapshot_stale' if invalidated == 'age' else 'snapshot_predates_execution')
    assert not live._claimed and not live.sender.sent
    pending = conn.execute('SELECT * FROM memecoin_pending_entries').fetchone()
    assert pending['status'] == 'failed' and pending['tx_sig'] is None
    assert exec_lock.exec_generation(live.lock_key, lock_dir=str(tmp_path)) == 0.0


# ---- Codex EVM-02 (2026-10-06): the receipt books the owner's NET coin, not its gross inflow ----

FEE_TO = "0x" + "7" * 40


@pytest.mark.parametrize("logs, booked", [
    ([(MINT, PM, OWNER, 100), (MINT, OWNER, FEE_TO, 30)], 70),          # credit then fee debit
    ([(MINT, PM, OWNER, 100), (MINT, OWNER, "0x" + "0" * 40, 30)], 70),  # a burn from us
    ([(MINT, PM, OWNER, 60), (MINT, PM, OWNER, 40)], 100),              # allowed fragments
    ([(MINT, PM, OWNER, 100), (MINT, OWNER, OWNER, 100)], 100),         # a self transfer
    ([(MINT, PM, OWNER, 100), (MINT, OWNER, FEE_TO, 100)], None),       # nothing netted
    ([(MINT, PM, OWNER, 100), ("0x" + "4" * 40, OWNER, FEE_TO, 5)], None),  # another cost
    ([(MINT, PM, OWNER, 100), (MINT, "0x" + "9" * 40, OWNER, 5)], None),    # an unbound source
])
def test_the_lane_receipt_books_the_owners_net_coin(conn, logs, booked):
    pending_id = _bound(conn)
    receipt, tx, block = _chain()
    receipt["logs"] = [_transfer(t, s, r, a, i) for i, (t, s, r, a) in enumerate(logs)]
    got = ex.recover_confirmed_lane_entry(conn, db.pending_row(conn, pending_id), receipt, tx, block,
                                          owner=OWNER, decimals_fn=lambda _m: 0)
    if booked is None:
        assert got is None and db.pending_row(conn, pending_id)["status"] == "reserved"
        return
    pos = conn.execute("SELECT * FROM memecoin_positions").fetchone()
    assert got["status"] == "filled" and db.open_qty(conn, pos["id"]) == booked
    assert pos["entry_notional"] == pytest.approx(50.0)               # the whole value, on the net


# ---- Codex EVM-04: only a canonical, bound status-0 receipt releases a signed lane buy ----

def _shaped(chain, shape):
    def read(method, params):
        r = chain.read(method, params)
        if method == "eth_getTransactionReceipt" and r:
            shape(r)
        return r
    return read


def _kept_bound(conn, rec, result):
    pending = _pending(conn)
    assert result["status"] == "unknown" and rec["_outcome"] in ("submitted_unknown", "receipt_unknown")
    assert pending["status"] == "reserved" and pending["tx_sig"] == rec["tx_hash"]  # binding kept


@pytest.mark.parametrize("shape", [
    lambda r: r.pop("status"),                                         # a partial provider reply
    lambda r: r.update(status="0x2"),                                  # an unrecognized status
    lambda r: r.update(status=None),
    lambda r: r.update(status="0x0", transactionHash="0x" + "e" * 64),  # another tx's failure
])
def test_a_receipt_without_our_recognized_status_keeps_the_signed_buy_bound(tmp_path, shape):
    conn, chain = _ledger(), Chain(status="0x0")
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path),
                 rpc_read=_shaped(chain, shape), receipt_timeout_s=0.05, lock_receipt_cap_s=0.02,
                 alert_fn=lambda t: None)
    result, rec = _run(live, conn)
    _kept_bound(conn, rec, result)


def test_a_status_zero_receipt_not_in_its_canonical_block_keeps_the_buy_bound(tmp_path):
    conn, chain = _ledger(), Chain(status="0x0")
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), alert_fn=lambda t: None,
                 rpc_read=_shaped(chain, lambda r: r.update(blockHash="0x" + "d" * 64)))
    result, rec = _run(live, conn)
    _kept_bound(conn, rec, result)


def test_settle_never_releases_on_a_receipt_without_a_status(tmp_path):
    # Codex's repro at the seam itself: blockNumber and index, no status.
    conn, chain = _ledger(), Chain()
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), alert_fn=lambda t: None)
    pending_id = _bound(conn)
    rec = {"send_ms": 0.0}
    out = live._settle({"seq": 1, "mint": MINT}, rec, conn, pending_id, HASH, 18,
                       {"blockNumber": "0x1", "transactionIndex": "0x0", "transactionHash": HASH}, None)
    assert out["status"] == "unknown" and rec["_outcome"] == "receipt_unknown"
    assert db.pending_row(conn, pending_id)["status"] == "reserved"
    assert db.pending_row(conn, pending_id)["tx_sig"] == HASH


@pytest.mark.parametrize("revert, reason", [(None, "reverted_unknown"),
                                            (("min_out", "Too little received"), "reverted_min_out"),
                                            (("other", "x"), "reverted_unknown")])
def test_a_canonical_status_zero_releases_with_its_proven_revert_class(tmp_path, revert, reason):
    conn, chain = _ledger(), Chain(status="0x0")
    kw = {} if revert is None else {"revert_reason_fn": lambda h: revert}
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), **kw)
    result, rec = _run(live, conn)
    assert rec["_outcome"] == "reverted" and result["status"] == "failed"
    assert result["reason"] == reason and _pending(conn)["status"] == "failed"


class _ReceiptTrace:
    def __init__(self):
        self.marks = []

    def mark_substage(self, name, **metadata):
        self.marks.append((name, metadata))


def _failed_settlement(chain_id):
    """The shared settlement seam, with a real signed-binding row on either native lane."""
    from sniper.services.memecoin_bsc_fast_lane import BscLive
    conn = _ledger()
    pending_id = db.reserve_pending(conn, mint=MINT, source_user_id="src", strategy="alpha_b",
                                    notional=50.0, reserved_at="2026-10-02T00:00:00Z",
                                    network_id=str(chain_id))
    assert db.bind_entry_submission(
        conn, pending_id, tx_identity=HASH, signed_payload_b64="0xraw", signed_payload_sha256="a" * 64,
        blockhash=None, last_valid_height=None, nonce=7,
        backend="rh_fast_sequencer" if chain_id == 4663 else "bsc_fast_lane",
        bound_at="2026-10-02T00:00:01Z", evm_quote_asset_usd=2500.0)
    receipt, tx, block = _chain(status="0x0")
    live = object.__new__(lane.LiveLane if chain_id == 4663 else BscLive)
    live.ctx, live.alert_fn = {"address": OWNER}, lambda text: None
    live.rpc_read = lambda method, params: (tx if method == "eth_getTransactionByHash" else block)
    live._revert_kind = lambda tx_hash, rec=None: "unknown"
    return conn, pending_id, live, receipt, tx, block


@pytest.mark.parametrize("chain_id", [4663, 56], ids=["rh", "bsc"])
def test_a_failed_canonical_receipt_marks_its_exact_landing_before_replay(chain_id):
    conn, pending_id, live, receipt, _tx, block = _failed_settlement(chain_id)
    trace, rec = _ReceiptTrace(), {"send_ms": 20.0}

    def replay(tx_hash, rec=None):
        assert trace.marks == [("rh_fast_lane_receipt", {
            "block": 16, "block_time": int(block["timestamp"], 0), "attempt_no": 1, "tx_sig": HASH})]
        assert db.pending_row(conn, pending_id)["status"] == "failed"
        return "min_out"
    live._revert_kind = replay
    result = live._settle({"seq": 12, "mint": MINT}, rec, conn, pending_id, HASH, 18, receipt, trace)
    assert result["status"] == "failed" and result["reason"] == "reverted_min_out"
    assert rec["our_block"] == 16 and rec["blocks_behind"] == 4
    assert db.pending_row(conn, pending_id)["resolution_reason"] == live.RELEASE_PREFIX + "reverted"


@pytest.mark.parametrize("chain_id", [4663, 56], ids=["rh", "bsc"])
@pytest.mark.parametrize("timestamp", [None, "partial_timestamp", "0x0", "-1"],
                         ids=["absent", "malformed", "zero", "negative"])
def test_a_failed_canonical_receipt_keeps_its_block_when_time_is_unknown(chain_id, timestamp):
    conn, pending_id, live, receipt, _tx, block = _failed_settlement(chain_id)
    if timestamp is None:
        block.pop("timestamp")
    else:
        block["timestamp"] = timestamp
    trace = _ReceiptTrace()
    result = live._settle({"seq": 12, "mint": MINT}, {"send_ms": 20.0}, conn,
                          pending_id, HASH, 18, receipt, trace)
    assert result["status"] == "failed" and db.pending_row(conn, pending_id)["status"] == "failed"
    assert trace.marks == [("rh_fast_lane_receipt", {"block": 16, "block_time": None,
                                                 "attempt_no": 1, "tx_sig": HASH})]


@pytest.mark.parametrize("chain_id", [4663, 56], ids=["rh", "bsc"])
def test_a_failed_receipt_reset_conflict_never_releases_the_bound_entry(chain_id, monkeypatch):
    conn, pending_id, live, receipt, _tx, _block = _failed_settlement(chain_id)
    monkeypatch.setattr(db, "reset_entry_submission_after_failed_receipt", lambda *args, **kwargs: False)
    releases = []
    monkeypatch.setattr(db, "release_pending", lambda *args: releases.append(args))
    live._revert_kind = lambda tx_hash, rec=None: pytest.fail("classified a failed receipt after reset conflict")
    result = live._settle({"seq": 12, "mint": MINT}, {"send_ms": 20.0}, conn,
                          pending_id, HASH, 18, receipt, _ReceiptTrace())
    assert result["status"] == "unknown" and releases == []
    pending = db.pending_row(conn, pending_id)
    assert pending["status"] == "reserved" and pending["tx_sig"] == HASH
    assert pending["signed_payload_sha256"] == "a" * 64


@pytest.mark.parametrize("chain_id", [4663, 56], ids=["rh", "bsc"])
def test_a_failed_canonical_receipt_releases_with_the_identity_reset(chain_id, monkeypatch):
    conn, pending_id, live, receipt, _tx, _block = _failed_settlement(chain_id)
    monkeypatch.setattr(db, "release_pending",
                        lambda *args: pytest.fail("released after a separate identity-reset commit"))
    result = live._settle({"seq": 12, "mint": MINT}, {"send_ms": 20.0}, conn,
                          pending_id, HASH, 18, receipt, _ReceiptTrace())
    assert result["status"] == "failed" and result["reason"] == "reverted_unknown"
    pending = db.pending_row(conn, pending_id)
    assert pending["status"] == "failed"
    assert pending["resolution_reason"] == live.RELEASE_PREFIX + "reverted"
    assert pending["tx_sig"] is None and pending["signed_payload_sha256"] is None


@pytest.mark.parametrize("chain_id", [4663, 56], ids=["rh", "bsc"])
@pytest.mark.parametrize("shape", [
    lambda receipt: receipt.pop("blockNumber"),
    lambda receipt: receipt.update(blockNumber="not_a_block"),
    lambda receipt: receipt.pop("blockHash"),
    lambda receipt: receipt.pop("logs"),
    lambda receipt: receipt.update(logs=None),
    lambda receipt: receipt.update(transactionHash="0x" + "e" * 64),
    lambda receipt: receipt.update(blockHash="0x" + "d" * 64),
], ids=["missing_block", "bad_block", "missing_block_hash", "missing_logs", "partial_logs",
        "foreign_hash", "unproven_block"])
def test_an_unproven_failed_receipt_never_releases_or_marks_landing(chain_id, shape, monkeypatch):
    conn, pending_id, live, receipt, _tx, _block = _failed_settlement(chain_id)
    shape(receipt)
    monkeypatch.setattr(db, "release_pending", lambda *args: pytest.fail("released unproven receipt"))
    monkeypatch.setattr(db, "reset_entry_submission_after_failed_receipt",
                        lambda *args, **kwargs: pytest.fail("reset unproven receipt"))
    trace, rec = _ReceiptTrace(), {"send_ms": 20.0}
    result = live._settle({"seq": 12, "mint": MINT}, rec, conn, pending_id, HASH, 18, receipt, trace)
    assert result["status"] == "unknown" and trace.marks == []
    pending = db.pending_row(conn, pending_id)
    assert pending["status"] == "reserved" and pending["tx_sig"] == HASH
    assert pending["signed_payload_sha256"] == "a" * 64


def test_the_receipt_wait_reads_past_a_partial_reply_to_the_typed_receipt(tmp_path):
    conn, chain = _ledger(), Chain()
    seen = []

    def shape(r):
        seen.append(1)
        if len(seen) == 1:
            r.pop("status")
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), rpc_read=_shaped(chain, shape))
    result, rec = _run(live, conn)
    assert result["status"] == "filled" and len(seen) >= 2

# ---- EVM-01 (Codex 2026-10-06): the independent copy-price cap on the native lanes ----

def test_the_copy_cap_floor_is_the_smallest_output_inside_the_cap():
    from sniper.execution.entry_attribution import copy_cap_min_out
    clip, usd, src, dec = 10 ** 16, 600.0, 0.0006, 9                 # a 9-decimal coin

    def worst(out):
        return Fraction(clip, 10 ** 18) * Fraction(str(usd)) / Fraction(out, 10 ** dec)
    floor = copy_cap_min_out(clip, usd, src, dec, 35.0)
    limit = Fraction(str(src)) * Fraction(135, 100)
    assert worst(floor) <= limit < worst(floor - 1)                  # rounded up, exactly
    assert copy_cap_min_out(clip, usd, src, dec, 0.0) == 0          # cap off: no floor
    for bad in (None, 0.0, -1.0, float("nan"), float("inf"), "x"):
        assert copy_cap_min_out(clip, usd, bad, dec, 35.0) is None   # unknown source: refuse
        assert copy_cap_min_out(clip, bad, src, dec, 35.0) is None   # unknown quote price
    assert copy_cap_min_out(clip, usd, src, None, 35.0) is None


def _source_price():
    """The lane event's wallet-net source average (USD per whole coin), as LaneEvent prices it."""
    return float(Fraction(str(PAYLOAD["paid_usd"])) / Fraction(int(PAYLOAD["src_out"]), 10 ** 18))


def _run_priced(live, conn, source_price, trade_size=50.0):
    pending_id = db.reserve_pending(conn, mint=MINT, source_user_id="uid", strategy="alpha_b",
                                    notional=trade_size, reserved_at="2026-10-02T00:00:00+00:00",
                                    network_id="4663", signal_source="rh_fast_lane",
                                    source_price=source_price,
                                    origin_event_id="rhfast:4663:%s:%s" % ("0x" + "c" * 64, MINT))
    rec = {"_perf0": time.perf_counter()}
    result = live.execute_reserved(dict(PAYLOAD), rec, conn, pending_id, trade_size, decimals=18)
    return result, rec, pending_id


def test_the_copy_cap_raises_the_signed_minimum_above_the_transported_chase_cap(tmp_path, monkeypatch):
    from sniper.execution.entry_attribution import copy_cap_min_out
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT", 30.0)
    conn, chain = _ledger(), Chain()
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path))
    result, rec, _pid = _run_priced(live, conn, _source_price())
    floor = copy_cap_min_out(rec["clip_wei"], 2500.0, _source_price(), 18, 30.0)
    assert floor > rec["clip_wei"] * PAYLOAD["cap_den"] // PAYLOAD["cap_num"]  # chase alone is looser
    assert result["status"] == "filled" and rec["min_out"] == floor
    assert format(floor, "064x") in chain.raw                       # the signed bytes carry it
    assert rec["copy_floor"] == floor                               # a revert names its bound


@pytest.mark.parametrize("module, writer, table", [
    ("sniper.services.memecoin_rh_fast_lane", "write_live_record", "rh_fast_lane_live"),
    ("sniper.services.memecoin_bsc_fast_lane", "write_live", "bsc_lane_live")])
def test_the_live_row_keeps_the_copy_floor_on_a_db_written_before_it(tmp_path, module, writer, table):
    import importlib
    mod = importlib.import_module(module)
    path = str(tmp_path / "lane.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE %s (id INTEGER PRIMARY KEY, source_tx TEXT, our_tx TEXT, mint TEXT, "
                "reason TEXT, pending_id INTEGER, position_id INTEGER, blocks_behind INTEGER, "
                "send_ms REAL, decide_to_send_ms REAL, send_reply TEXT, held INTEGER, lock_ms REAL, "
                "our_block INTEGER, our_tx_index INTEGER, paid_wei TEXT, got_atomic TEXT, "
                "safety_path TEXT, safety_ms REAL, busy_ms REAL, written_at TEXT)" % table)
    old.commit()
    old.close()
    getattr(mod, writer)(path, {"source_tx": "0xs", "reason": "reverted", "copy_floor": 10 ** 30})
    (got,) = sqlite3.connect(path).execute("SELECT copy_floor FROM %s" % table).fetchone()
    assert got == str(10 ** 30)                                     # exact past 2^63


def test_an_unknown_source_price_under_the_copy_cap_never_signs(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    conn, chain = _ledger(), Chain()
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path))
    live.ctx["sign_tx_fn"] = lambda *a: pytest.fail("signed without a source price")
    result, rec, pending_id = _run_priced(live, conn, None)
    assert rec["_outcome"] == "copy_signed_floor" and result["status"] == "aborted"
    assert live._claimed == [] and chain.raw is None
    assert db.pending_row(conn, pending_id)["status"] == "failed"


def test_with_the_copy_cap_off_the_signed_minimum_is_today_s(tmp_path):
    conn, chain = _ledger(), Chain()
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path))
    _result, rec, _pid = _run_priced(live, conn, None)
    assert rec["min_out"] == rec["clip_wei"] * PAYLOAD["cap_den"] // PAYLOAD["cap_num"]


def test_a_slow_revert_replay_never_holds_the_release_or_the_lane(tmp_path, monkeypatch):
    # Gamma LOW on 13a6adca4: the replay is 3+ custody calls; the release comes first and the lane
    # waits for the class at most REVERT_CLASS_WAIT_S.
    import threading
    monkeypatch.setattr(lane, "REVERT_CLASS_WAIT_S", 0.05)
    conn, chain = _ledger(), Chain(status="0x0")
    hung, seen = threading.Event(), []

    def replay(h):
        seen.append(_pending(conn)["status"])                    # released before it ran
        hung.wait(5)
        return ("min_out", "late")
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), revert_reason_fn=replay)
    t0 = time.monotonic()
    result, rec = _run(live, conn)
    hung.set()
    assert time.monotonic() - t0 < 2.0
    assert result["reason"] == "reverted_unknown" and seen == ["failed"]
    assert rec["execution_evidence"]["replay_basis"] == "unknown"


# ---- Codex EVM-06: the lock cap bounds the receipt READ itself, not just the poll loop ----

def test_a_hung_custody_read_cannot_hold_the_wallet_lock_past_its_cap(tmp_path):
    import threading
    from sniper.execution.exec_lock import wallet_exec_lock
    conn, chain = _ledger(), Chain()
    alerts, hung, release = [], threading.Event(), threading.Event()
    real_read = chain.read

    def hanging_read(method, params):                # a synchronous read the HTTP layer holds 10 s
        if method == "eth_getTransactionReceipt" and not release.is_set():
            hung.set()
            release.wait(10)
        return real_read(method, params)
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), receipt_timeout_s=10.0,
                 lock_receipt_cap_s=0.3, sleep=time.sleep, alert_fn=alerts.append,
                 rpc_read=hanging_read)
    out = []
    worker = threading.Thread(target=lambda: out.append(_run(live, conn)), daemon=True)
    worker.start()
    assert hung.wait(5.0)
    t0 = time.monotonic()
    while not alerts and time.monotonic() - t0 < 5.0:
        time.sleep(0.01)
    assert alerts and "lock released" in alerts[0]
    assert time.monotonic() - t0 < 1.0, "the lock outlived its cap behind a hung read"
    with wallet_exec_lock("k", lock_dir=str(tmp_path), blocking=False) as held:
        assert held, "an exit could not take the lock at the cap"
    release.set()
    worker.join(5.0)
    assert out and out[0][0]["status"] == "filled" and _pending(conn)["status"] == "confirmed"


def test_a_timed_out_read_is_joined_never_multiplied(tmp_path):
    import threading
    conn, chain = _ledger(), Chain()
    started, release = [], threading.Event()

    def hanging_read(method, params):
        started.append(method)
        release.wait(5)
        return None
    live = _live(conn, _accepting_sender(), chain, lock_dir=str(tmp_path), rpc_read=hanging_read,
                 receipt_poll_s=0.01, sleep=time.sleep)
    for _ in range(3):                    # the cap's wait, then the outer waits, while it hangs
        assert live._receipt("0x" + "ab" * 32, 0.1) is None
    release.set()
    live._read_thread.join(5)
    assert live._read_thread.daemon                                  # a clean stop never waits on it
    assert started == ["eth_getTransactionReceipt"]                 # one read out, polls joined it


def test_a_supply_that_expires_between_the_precheck_and_the_event_falls_back(tmp_path):
    # Codex EVM-07: facts() can turn None at the TTL between the precheck and the LaneEvent read.
    conn, chain = _ledger(), Chain()
    s = _accepting_sender()
    answers = iter([(18, 10 ** 27)])

    def hook_factory(contexts, stats):
        raise AssertionError("an expired supply reached the funded hook")
    live = _live(conn, s, chain, lock_dir=str(tmp_path), hook_factory=hook_factory,
                 facts_fn=lambda mint: next(answers, None))
    rec = live.handle(dict(PAYLOAD), recv_epoch=1000.0, safety_reason=None,
                      stats=({"price": 1.0}, 1000.0))
    assert rec["reason"] == "no_mcap" and s.sent == [] and _pending(conn) is None
