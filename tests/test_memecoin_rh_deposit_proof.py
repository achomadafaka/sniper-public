"""4663 fast lane option C: a Relay buy's initiation proved from the armed user's own signed Solana
deposit, seen at processed, matched to the 4663 fill by its order id (operator yes 2026-10-07).

The deposit frame is a recorded jsonParsed transaction (hooks fixture, 600 USDC from a verified
DopamineFeenFr1 address) in the shape Helius transactionSubscribe delivers it."""
import copy
import gzip
import json
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import base58
import pytest

from sniper.services import memecoin_rh_deposit_proof as dep
from sniper.services.memecoin_source_initiation import source_initiation_refusal

FRAME = json.load(gzip.open(Path(__file__).parent / "fixtures/memecoin/hooks_relay_initiation_20261004.json.gz", "rt"))
ORIGIN = FRAME["origin-sol"]["result"]
UID = "a3d6096e-59eb-5684-a9fe-0048e07a0eb4"
DEPOSITOR = "FXr1GVAQtLoQHP25HZztqEfw1PBcDbvP6uJgEo7cdwEf"
VERIFIED_AT = datetime.fromisoformat("2026-09-27T00:34:05.377359+00:00").timestamp()
ARRIVED = float(ORIGIN["blockTime"]) + 0.4
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WALLET = "0x" + "ef" * 20
MINT = "0x" + "3" * 40
PAYER = "0x" + "9a" * 20


def _ix():
    return next(i for i in ORIGIN["transaction"]["message"]["instructions"]
                if i.get("programId") == dep.SOLANA_DEPOSITORY)


ORDER_ID = "0x" + base58.b58decode(_ix()["data"])[16:].hex()


def _frame():
    """The transactionNotification `result` for the recorded deposit."""
    return copy.deepcopy({"signature": ORIGIN["transaction"]["signatures"][0], "slot": ORIGIN["slot"],
                          "transaction": {"transaction": ORIGIN["transaction"], "meta": ORIGIN["meta"]}})


OWNERS = {DEPOSITOR: {UID: VERIFIED_AT}}


def _proof():
    proof, why = dep.deposit_from_frame(_frame(), owners=OWNERS, arrived=ARRIVED)
    assert why is None, why
    return proof


def test_the_recorded_deposit_is_a_full_proof_at_arrival():
    proof = _proof()
    assert proof["order_id"] == ORDER_ID and proof["depositor"] == DEPOSITOR
    assert proof["amount_atomic"] == 600_000_000 and proof["uids"] == (UID,)
    assert proof["slot"] == ORIGIN["slot"] and proof["arrived"] == ARRIVED
    assert proof["signature"] == ORIGIN["transaction"]["signatures"][0]


def _mutate(fn):
    frame = _frame()
    fn(frame, frame["transaction"]["transaction"]["message"], frame["transaction"]["meta"],
       next(i for i in frame["transaction"]["transaction"]["message"]["instructions"]
            if i.get("programId") == dep.SOLANA_DEPOSITORY))
    return dep.deposit_from_frame(frame, owners=OWNERS, arrived=ARRIVED)


def _keys_unsigned(f, msg, meta, ix):
    for k in msg["accountKeys"]:
        if k["pubkey"] == DEPOSITOR:
            k["signer"] = False


def _other_sender(f, msg, meta, ix):
    ix["accounts"][1] = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"


def _other_mint(f, msg, meta, ix):
    ix["accounts"][4] = "So11111111111111111111111111111111111111112"


def _short_balance(f, msg, meta, ix):
    for row in meta["postTokenBalances"]:
        if row["owner"] == dep.SOLANA_VAULT:
            row["uiTokenAmount"]["amount"] = str(int(row["uiTokenAmount"]["amount"]) - 1)


def _two_deposits(f, msg, meta, ix):
    msg["instructions"].append(copy.deepcopy(ix))


def _wrong_disc(f, msg, meta, ix):
    raw = bytearray(base58.b58decode(ix["data"]))
    raw[0] ^= 1
    ix["data"] = base58.b58encode(bytes(raw)).decode()


def _failed(f, msg, meta, ix):
    meta["err"] = {"InstructionError": [2, "Custom"]}


@pytest.mark.parametrize("mutation, reason", [
    (_keys_unsigned, "depositor_did_not_sign"), (_other_sender, "sender_not_depositor"),
    (_other_mint, "not_usdc_vault"), (_short_balance, "payment_unproved"),
    (_two_deposits, "ambiguous_deposit"), (_wrong_disc, "not_a_token_deposit"),
    (_failed, "failed"),
])
def test_every_broken_deposit_is_no_proof_with_its_reason(mutation, reason):
    assert _mutate(mutation) == (None, reason)


@pytest.mark.parametrize("owners", [{}, {DEPOSITOR: {UID: ARRIVED + 5}}, {DEPOSITOR: {UID: None}}])
def test_an_unregistered_or_later_verified_depositor_is_no_proof(owners):
    assert dep.deposit_from_frame(_frame(), owners=owners, arrived=ARRIVED) == (
        None, "unregistered_depositor")


def _fill(**over):
    f = dict(order_id=ORDER_ID, source_user_id=UID, wallet=WALLET, mint=MINT, payment_token=USDG,
             paid=598_000_000, payer=PAYER, tx_hash="0x" + "ab" * 32, seq=81691737)
    f.update(over)
    return f


def test_a_matching_fill_takes_the_proof_once():
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: ARRIVED)
    proofs.put(_proof())
    lane, status, margin = proofs.take(_fill(), decimals={USDG: 6}, now=ARRIVED + 1.2)
    assert status == "matched" and margin == pytest.approx(1200.0)
    assert lane == dict(proof_version=dep.PROOF_VERSION, order_id=ORDER_ID,
                        origin_tx=ORIGIN["transaction"]["signatures"][0], origin_wallet=DEPOSITOR,
                        origin_slot=ORIGIN["slot"], origin_amount_atomic="600000000",
                        origin_identity_user_id=UID, payer=PAYER, origin_arrived=ARRIVED,
                        margin_ms=pytest.approx(1200.0))
    json.dumps(lane)                                    # rides the datagram
    assert proofs.take(_fill(), decimals={USDG: 6}, now=ARRIVED + 1.3)[:2] == (None, "no_deposit")


@pytest.mark.parametrize("over, at, status", [
    (dict(order_id="0x" + "11" * 32), 1.0, "no_deposit"),
    (dict(order_id=None), 1.0, "no_deposit"),
    (dict(source_user_id="someone-else"), 1.0, "uid_mismatch"),
    (dict(paid=587_000_000), 1.0, "amount_band"),          # 0.978 of the deposit
    (dict(paid=600_000_001), 1.0, "amount_band"),          # more than was deposited
    (dict(payment_token="0x" + "44" * 20), 1.0, "payment_token"),
    ({}, 121.0, "stale"),
    ({}, -0.5, "stale"),
])
def test_a_fill_that_does_not_bind_gets_no_proof(over, at, status):
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: ARRIVED)
    proofs.put(_proof())
    lane, got, _margin = proofs.take(_fill(**over), decimals={USDG: 6}, now=ARRIVED + at)
    assert (lane, got) == (None, status)


def test_unknown_payment_decimals_get_no_proof():
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: ARRIVED)
    proofs.put(_proof())
    assert proofs.take(_fill(), decimals={}, now=ARRIVED + 1)[:2] == (None, "payment_token")


def test_two_deposits_on_one_order_id_prove_nothing_and_a_redelivery_is_one():
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: ARRIVED)
    proofs.put(_proof())
    proofs.put(_proof())                                   # the same signature again
    assert proofs.take(_fill(), decimals={USDG: 6}, now=ARRIVED + 1)[1] == "matched"
    proofs.put(_proof())
    proofs.put(dict(_proof(), signature="5" * 88))
    assert proofs.take(_fill(), decimals={USDG: 6}, now=ARRIVED + 1)[:2] == (None, "duplicate_order")


def test_the_map_is_bounded_by_age_and_size():
    now = [ARRIVED]
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: now[0], ttl_s=600.0, max_proofs=3)
    for n in range(5):
        proofs.put(dict(_proof(), order_id="0x%064x" % n, signature=str(n)))
    assert len(proofs) == 3 and proofs.counters["evicted"] == 2
    now[0] += 601
    proofs.put(dict(_proof(), order_id="0x%064x" % 9, signature="9"))
    assert len(proofs) == 1


# ---- the funded hook's initiation gate ----

def _payload(**over):
    p = dict(tx_hash="0x" + "ab" * 32, seq=81691737, wallet=WALLET, mint=MINT, source_user_id=UID)
    p.update(over)
    lane, status, _m = _taken()
    p.setdefault("deposit_proof", lane)
    return p


def _taken():
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: ARRIVED)
    proofs.put(_proof())
    return proofs.take(_fill(), decimals={USDG: 6}, now=ARRIVED + 1)


def _event(payload):
    tx, mint = payload["tx_hash"], payload["mint"]
    return SimpleNamespace(signal_source="rh_fast_lane", fomo_event_id="rhfast:4663:%s:%s" % (tx, mint),
                           mint=mint, source_user_id=payload["source_user_id"],
                           source_wallet=payload["wallet"], source_block_number=payload["seq"],
                           source_initiation=dep.lane_initiation(payload))


def test_the_hook_accepts_a_deposit_proof_only_with_the_knob(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True, raising=False)
    assert source_initiation_refusal(_event(_payload()), "4663") is None
    assert dep.payload_refusal(_payload()) is None
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", False)
    assert source_initiation_refusal(_event(_payload()), "4663") == "wallet_initiation_unproved"
    assert dep.payload_refusal(_payload()) == "wallet_initiation_unproved"


@pytest.mark.parametrize("field, value", [
    ("order_id", None), ("order_id", "0x12"), ("origin_identity_user_id", "someone-else"),
    ("origin_tx", "short"), ("origin_wallet", None), ("origin_slot", 0),
    ("origin_amount_atomic", "0"), ("payer", None),
])
def test_the_hook_refuses_a_malformed_deposit_proof(monkeypatch, field, value):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True, raising=False)
    p = _payload()
    p["deposit_proof"] = dict(p["deposit_proof"], **{field: value})
    assert source_initiation_refusal(_event(p), "4663") == "receipt_identity_mismatch"


def test_the_deposit_proof_is_never_accepted_off_the_4663_lane(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True, raising=False)
    ev = _event(_payload())
    for chain in ("56", "8453", "solana"):          # the version itself is refused off 4663
        assert source_initiation_refusal(ev, chain) == "wallet_initiation_unproved", chain
    ev.fomo_event_id = "onchain:4663:%s:%s" % ("0x" + "ab" * 32, MINT)
    ev.signal_source = "onchain_watch"
    assert source_initiation_refusal(ev, "4663") == "wallet_initiation_unproved"


def test_the_payload_cap_must_not_exceed_the_post_price_plus_chase(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    post = Fraction(7, 3 * 10 ** 6)
    p = dict(post_wei_num=post.numerator, post_wei_den=post.denominator)
    cap = post * Fraction(13, 10)
    assert dep.cap_bound(dict(p, cap_num=cap.numerator, cap_den=cap.denominator))
    loose = cap * Fraction(1001, 1000)
    assert not dep.cap_bound(dict(p, cap_num=loose.numerator, cap_den=loose.denominator))
    assert not dep.cap_bound(dict(cap_num=cap.numerator, cap_den=cap.denominator))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 0.0)
    assert not dep.cap_bound(dict(p, cap_num=cap.numerator, cap_den=cap.denominator))


# ---- the post-hoc confirmation ----

class Rpc:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, params):
        self.calls.append((method, params))
        got = self.answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return got


def _confirmer(answers):
    rows, pages = [], []
    rpc = Rpc(answers)
    c = dep.DepositConfirmer(rpc, alert_fn=pages.append, sink=rows.append)
    lane, _s, _m = _taken()
    c.add(lane, _fill())
    return c, rpc, rows, pages


def _status(conf, err=None):
    return {"context": {"slot": 1}, "value": [{"slot": 1, "confirmations": 0, "err": err,
                                               "confirmationStatus": conf}]}


UNSEEN = {"context": {"slot": 1}, "value": [None]}


def test_a_deposit_that_confirms_is_recorded_without_a_page():
    c, rpc, rows, pages = _confirmer([UNSEEN, _status("processed"), _status("confirmed")])
    for t in (2, 4, 6, 8):
        c.tick(ARRIVED + t)
    assert [r["status"] for r in rows] == ["confirmed"] and pages == []
    assert rpc.calls[0] == ("getSignatureStatuses", [[ORIGIN["transaction"]["signatures"][0]]])
    assert len(rpc.calls) == 3                              # nothing left to ask


def test_a_failed_deposit_pages_and_marks_the_fill():
    c, rpc, rows, pages = _confirmer([_status("confirmed", err={"InstructionError": [2, "x"]})])
    c.tick(ARRIVED + 2)
    assert [r["status"] for r in rows] == ["initiation_unconfirmed:failed"] and len(pages) == 1
    assert "0x" + "ab" * 32 in pages[0] and "failed" in pages[0]


def test_a_dropped_deposit_pages_at_the_deadline_and_an_unanswered_one_says_so():
    c, rpc, rows, pages = _confirmer([UNSEEN] * 50)
    for t in range(2, 92, 2):
        c.tick(ARRIVED + t)
    assert [r["status"] for r in rows] == ["initiation_unconfirmed:dropped"] and len(pages) == 1
    c, rpc, rows, pages = _confirmer([OSError("down")] * 50)
    for t in range(2, 92, 2):
        c.tick(ARRIVED + t)
    assert [r["status"] for r in rows] == ["initiation_unconfirmed:unchecked"] and len(pages) == 1


def test_the_roster_is_the_4663_armed_users_verified_solana_addresses(tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as db
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    for uid, chains in (("armed", ["4663"]), ("elsewhere", ["56"])):
        db.add_wallet(conn, uid, uid)
        db.set_wallet_real_approved(conn, uid, True, chains=chains, force=True)
    rows = [("armed", DEPOSITOR, "verified", None), ("armed", "B" * 44, "candidate", None),
            ("armed", "C" * 44, "verified", "2026-10-01T00:00:00+00:00"),
            ("elsewhere", "D" * 44, "verified", None)]
    for uid, address, status, conflict in rows:
        conn.execute("INSERT INTO memecoin_address_registry (source_user_id, network_id, address, "
                     "status, evidence_kind, status_at, conflict_at, first_seen_at) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (uid, "solana", address, status, "test", "2026-09-27T00:34:05+00:00", conflict,
                      "2026-09-27T00:34:05+00:00"))
    assert dep.deposit_owners(conn) == {DEPOSITOR: {"armed": pytest.approx(
        datetime.fromisoformat("2026-09-27T00:34:05+00:00").timestamp())}}


def test_the_key_guard_refuses_the_watcher_key_and_funded_keys():
    env = {"ONCHAIN_WATCH_HELIUS_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=watch",
           "MEMECOIN_SOLANA_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=funded"}
    assert dep.key_refusal("https://mainnet.helius-rpc.com/?api-key=watch", env) == "watcher_key"
    assert dep.key_refusal("https://mainnet.helius-rpc.com/?api-key=funded", env) == "funded_key"
    assert dep.key_refusal("", env) == "unset"
    assert dep.key_refusal("https://mainnet.helius-rpc.com/?api-key=research", env) is None
    assert dep.ws_url("https://mainnet.helius-rpc.com/?api-key=research") == (
        "wss://mainnet.helius-rpc.com/?api-key=research")


def test_the_feed_record_carries_the_fill_order_id():
    from test_memecoin_rh_feed_shadow import QUOTES, WATCHED, _input
    from sniper.services.memecoin_rh_feed_shadow import decode_relay_fill
    data = _input()
    fill = decode_relay_fill(data, WATCHED, QUOTES)
    assert fill["order_id"] == "0x" + data[-32:].hex() and len(fill["order_id"]) == 66


# ---- the decide path: estimated only with a proof, option-2 cap ----

def _decide_bits():
    from test_memecoin_rh_fast_decide import B, CTX, SIG, ZPOOL, ZUP_IN, ZUP_PRE, _paid_for, _row, _tape
    tape = _tape([_row(ZPOOL, B - 3, "0xpre", ZUP_PRE["sqrt"])])
    return tape, CTX, dict(SIG, paid=_paid_for(ZUP_IN))


def test_an_estimated_source_cap_is_the_post_price_and_chase_alone():
    from sniper.services import memecoin_rh_fast_decide as fast
    tape, ctx, sig = _decide_bits()
    # A premium of 0.5 would be far tighter than the chase term; estimated, it is ignored.
    rec = fast.decide(sig, tape=tape, **dict(ctx, premium=0.5))
    assert rec["reason"] == "would_send" and rec["state_kind"] == "pre"
    assert rec["cap_price"] == Fraction(1 << 192, rec["est_post_sqrt"] ** 2) * Fraction(13, 10)


def _signal(order_id=ORDER_ID, **over):
    from test_memecoin_rh_fast_decide import USDG as usdg
    tape, ctx, sig = _decide_bits()
    rec = dict(sig, payment_token=usdg, paid=598_000_000, order_id=order_id, source_user_id=UID,
               wallet=WALLET, payer=PAYER, handle="h", arrival_utc="2026-10-07T21:00:00+00:00")
    rec.update(over)
    live = dict(ctx, now=None)
    live.pop("allow_estimated_source")                   # production's live dict never has it
    return tape, live, rec


def _handle(tmp_path, rec, tape, live, deposits, sent=None, confirmed=None):
    import time as _time
    from sniper.services import memecoin_rh_fast_decide as fast
    return fast.handle_signal(
        rec, _time.perf_counter(), tape=tape, live=live, db_path=str(tmp_path / "d.db"),
        now_fn=lambda: 1000.0, deposits=deposits,
        on_decided=None if sent is None else (lambda r, out, params: sent.append((r, out))),
        confirm=None if confirmed is None else (lambda proof, r: confirmed.append(proof)))


def _live_proofs():
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: ARRIVED)
    proofs.put(dict(_proof(), arrived=__import__("time").time() - 1.0))
    return proofs


def test_a_proved_signal_is_decided_without_its_receipt_and_queued_for_confirmation(tmp_path, monkeypatch):
    import sqlite3
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", False)
    tape, live, rec = _signal()
    sent, confirmed = [], []
    out = _handle(tmp_path, rec, tape, live, _live_proofs(), sent, confirmed)
    assert out["reason"] == "would_send" and out["state_kind"] == "pre"
    assert rec["deposit_status"] == "matched" and rec["deposit_proof"]["order_id"] == ORDER_ID
    assert confirmed == [rec["deposit_proof"]] and sent[0][0]["deposit_proof"] is rec["deposit_proof"]
    row = sqlite3.connect(str(tmp_path / "d.db")).execute(
        "SELECT order_id, deposit_status, deposit_margin_ms, deposit_origin_tx FROM rh_fast_decisions").fetchone()
    assert row[:2] == (ORDER_ID, "matched") and 900 < row[2] < 5000
    assert row[3] == ORIGIN["transaction"]["signatures"][0]


def test_without_a_proof_or_with_exact_state_required_the_signal_waits_for_its_receipt(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", False)
    tape, live, rec = _signal(order_id="0x" + "11" * 32)
    confirmed = []
    out = _handle(tmp_path, rec, tape, live, _live_proofs(), confirmed=confirmed)
    assert out["reason"] == "source_swap_pending" and rec["deposit_status"] == "no_deposit"
    assert confirmed == []
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", True)   # the knob reaches decide
    tape, live, rec = _signal()
    assert _handle(tmp_path, rec, tape, live, _live_proofs())["reason"] == "source_swap_pending"
    assert rec["deposit_status"] == "matched"


def test_with_the_knob_off_the_decision_is_todays(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", False)
    tape, live, rec = _signal()
    before = dict(rec)
    out = _handle(tmp_path, rec, tape, live, None)
    assert out["reason"] == "source_swap_pending" and rec == before


def test_a_reask_keeps_its_first_proof(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", False)
    tape, live, rec = _signal()
    proofs = _live_proofs()
    _handle(tmp_path, rec, tape, live, proofs)
    first = rec["deposit_proof"]
    out = _handle(tmp_path, rec, tape, live, proofs)      # the single-use proof is already taken
    assert rec["deposit_proof"] is first and out["reason"] == "would_send"


# ---- the runner's lane: the production entry point ----

@pytest.fixture
def entry(monkeypatch, tmp_path):
    from test_memecoin_rh_fast_entry import _setup
    return _setup(monkeypatch, tmp_path, "live")


def _lane_payload(**over):
    from test_memecoin_rh_fast_entry import _payload
    p = _payload()
    p.pop("source_fill")
    post = Fraction(1 << 192, p["post_sqrt"] ** 2)
    p.update(post_wei_num=post.numerator, post_wei_den=post.denominator,
             deposit_proof=dict(proof_version=dep.PROOF_VERSION, order_id=ORDER_ID,
                                origin_tx=ORIGIN["transaction"]["signatures"][0], origin_wallet=DEPOSITOR,
                                origin_slot=ORIGIN["slot"], origin_amount_atomic="65000000",
                                origin_identity_user_id="uid", payer=PAYER, origin_arrived=ARRIVED,
                                margin_ms=900.0))
    p.update(over)
    return p


def test_a_deposit_proved_payload_reaches_the_hook_and_the_send(entry, monkeypatch):
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_entry import _attempts, _go
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True)
    built, sender, net, ledger = entry
    rec = _go(built, _lane_payload())
    assert rec["initiation"] == "deposit:" + ORIGIN["transaction"]["signatures"][0]
    assert [a["signal_source"] for a in _attempts(ledger)] == ["rh_fast_lane"], rec
    assert "source_initiation" not in str(rec["reason"]) and sender.raws, rec


def test_with_the_knob_off_a_deposit_payload_is_todays_refusal(entry, monkeypatch):
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_entry import _attempts, _go
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", False)
    built, sender, net, ledger = entry
    rec = _go(built, _lane_payload())
    assert rec["reason"] == "source_fill_unproved" and _attempts(ledger) == [] and sender.raws == []


@pytest.mark.parametrize("change, reason", [
    (lambda p: p["deposit_proof"].update(origin_identity_user_id="other"),
     "deposit_proof_refused:receipt_identity_mismatch"),
    (lambda p: p["deposit_proof"].update(order_id=None), "deposit_proof_refused:receipt_identity_mismatch"),
    (lambda p: p.update(cap_num=p["cap_num"] * 10), "deposit_cap_unbound"),
    (lambda p: p.update(eth_usd=None), "no_eth_usd"),
])
def test_a_bad_deposit_payload_ends_at_the_lane_without_an_attempt(entry, monkeypatch, change, reason):
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_entry import _attempts, _go
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True)
    built, sender, net, ledger = entry
    p = _lane_payload()
    change(p)
    rec = _go(built, p)
    assert rec["reason"] == reason and _attempts(ledger) == [] and sender.raws == [] and net.calls == []


def test_a_shared_route_still_needs_its_receipt(entry, monkeypatch):
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_entry import _attempts, _go
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True)
    built, sender, net, ledger = entry
    p = _lane_payload(route_kind="v3_shared", key=None, post_ratio=1.0)
    rec = _go(built, p)
    assert rec["reason"] in ("source_fill_unproved", "route_off:v3_shared") and _attempts(ledger) == []


# ---- the socket: one processed transactionSubscribe; bytes and frames counted from the start ----

def test_the_socket_subscribes_at_processed_counts_bytes_and_puts_proofs(monkeypatch):
    import asyncio
    import collections
    import websockets

    class Stop(Exception):
        pass

    class WS:
        def __init__(self, frames):
            self.frames, self.sent = list(frames), []

        async def send(self, text):
            self.sent.append(json.loads(text))

        async def recv(self):
            if not self.frames:
                raise ConnectionError("closed by the provider")
            return self.frames.pop(0)

    class Conn:
        def __init__(self, ws):
            self.ws = ws

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *a):
            return False

    other = {"signature": "9" * 88, "slot": 1, "transaction": {"transaction": {
        "signatures": ["9" * 88], "message": {"accountKeys": [], "instructions": []}}, "meta": {"err": None}}}
    frames = [json.dumps({"jsonrpc": "2.0", "result": 7, "id": 1}),
              json.dumps({"jsonrpc": "2.0", "method": "transactionNotification",
                          "params": {"subscription": 7, "result": other}}),
              json.dumps({"jsonrpc": "2.0", "method": "transactionNotification",
                          "params": {"subscription": 7, "result": _frame()}})]
    ws, urls = WS(frames), []
    monkeypatch.setattr(websockets, "connect", lambda url, **k: (urls.append(url), Conn(ws))[1])

    async def stop(_s):
        raise Stop()
    monkeypatch.setattr(asyncio, "sleep", stop)
    monkeypatch.setattr(dep.time, "time", lambda: ARRIVED)
    roster = dep.Roster()
    roster.set(OWNERS)
    proofs, counters, lines = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: ARRIVED), collections.Counter(), []
    with pytest.raises(Stop):
        asyncio.run(dep._socket("https://mainnet.helius-rpc.com/?api-key=k", roster, proofs, counters,
                                lines.append))
    assert urls == ["wss://mainnet.helius-rpc.com/?api-key=k"]
    sub = ws.sent[0]
    assert sub["method"] == "transactionSubscribe" and sub["params"][0]["accountInclude"] == [DEPOSITOR]
    assert sub["params"][1]["commitment"] == "processed" and sub["params"][1]["encoding"] == "jsonParsed"
    assert counters["ws_frames"] == 3 and counters["ws_bytes"] == sum(len(f) for f in frames)
    assert counters["deposit_proof"] == 1 and counters["ws_errors"] == 1 and len(proofs) == 1
    assert any("socket down" in line for line in lines)


def test_a_refused_subscription_is_a_socket_failure(monkeypatch):
    import asyncio
    import collections
    import websockets

    class Stop(Exception):
        pass

    class WS:
        sent = []

        async def send(self, text):
            self.sent.append(text)

        async def recv(self):
            return json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
                "code": -32600, "message": "transactionSubscribe is not available on the free plan"}})

    class Conn:
        async def __aenter__(self):
            return WS()

        async def __aexit__(self, *a):
            return False
    monkeypatch.setattr(websockets, "connect", lambda url, **k: Conn())

    async def stop(_s):
        raise Stop()
    monkeypatch.setattr(asyncio, "sleep", stop)
    roster = dep.Roster()
    roster.set(OWNERS)
    counters, lines = collections.Counter(), []
    with pytest.raises(Stop):
        asyncio.run(dep._socket("https://x/?api-key=k", roster, dep.DepositProofs(registry_ok=lambda d, u: True), counters, lines.append))
    assert counters["ws_errors"] == 1 and "free plan" in lines[-1]


def test_start_is_off_without_the_knob_and_refuses_a_bad_key(monkeypatch, tmp_path):
    from sniper.settings import memecoin as cfg
    lines = []
    kw = dict(ledger_path=str(tmp_path / "l.db"), db_path=str(tmp_path / "d.db"), log=lines.append)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", False)
    assert dep.start_deposit_proofs(env={"MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL": "https://x/?api-key=r"},
                                    **kw) == (None, None, None, []) and lines == []
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True)
    env = {"MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL": "https://x/?api-key=watch",
           "ONCHAIN_WATCH_HELIUS_RPC_URL": "https://y/?api-key=watch"}
    assert dep.start_deposit_proofs(env=env, **kw) == (None, None, None, [])
    assert "watcher_key" in lines[-1] and "api-key" not in lines[-1]


def test_the_heartbeat_row_carries_the_deposit_counters(tmp_path):
    import collections
    import sqlite3
    from sniper.services import memecoin_rh_fast_decide as fast
    proofs = dep.DepositProofs(registry_ok=lambda d, u: True, clock=lambda: ARRIVED)
    proofs.put(_proof())
    confirmer = dep.DepositConfirmer(lambda m, p: None, alert_fn=print, sink=print)
    beat = dep.heartbeat_counters(collections.Counter(ws_bytes=175007, ws_frames=13), proofs, confirmer)
    tape, _ctx, _sig = _decide_bits()
    fast._beat(str(tmp_path / "d.db"), collections.Counter(), 0.5, tape, lambda: "2026-10-07T23:00:00+00:00",
               5, deposit=beat)
    got = json.loads(sqlite3.connect(str(tmp_path / "d.db")).execute(
        "SELECT deposit_counters FROM rh_fast_heartbeat").fetchone()[0])
    assert got["ws_bytes"] == 175007 and got["ws_frames"] == 13 and got["map_proofs"] == 1
    assert got["map_size"] == 1


def test_a_logged_failure_never_carries_the_key():
    assert dep._redact("bad uri wss://h/?api-key=SECRET&x=1") == "bad uri wss://h/?api-key=***&x=1"


# ---- Gamma LOW-1: a refused deposit row never makes the on-chain lane skip the same buy ----

def test_a_deposit_refused_row_then_the_onchain_row_for_the_same_buy_is_admitted(entry, monkeypatch):
    from datetime import datetime, timezone
    from sniper.services.memecoin_onchain_lane import already_attempted_from_another_lane
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_entry import _go
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET", {"4663:uid": 10000.0}, raising=False)
    built, sender, net, ledger = entry
    p = _lane_payload()
    rec = _go(built, p)
    assert rec["reason"].startswith("hook:skipped_mcap:") and sender.raws == [], rec
    row = ledger.execute("SELECT delivery_lane, terminal_status, origin_event_id FROM memecoin_entry_attempts"
                         ).fetchone()
    assert row[0] == dep.DEPOSIT_DELIVERY_LANE and row[1] is not None
    sig = "4663:%s:%s" % (p["tx_hash"], p["mint"])
    now = datetime.now(timezone.utc).isoformat()
    assert not already_attempted_from_another_lane(ledger, sig, now, source_user_id="uid")
    # A receipt row for the same buy keeps today's rule: the on-chain lane skips it.
    ledger.execute("UPDATE memecoin_entry_attempts SET delivery_lane='rh_fast_lane'")
    assert already_attempted_from_another_lane(ledger, sig, now, source_user_id="uid")


@pytest.mark.parametrize("terminal, tx_sig, trace, bound, taken", [
    (None, None, None, None, True),                              # in flight: our buy may land
    ("aborted", None, '{"substages":[{"name":"rh_fast_lane_send"}]}', None, True),   # reached the send
    ("unknown", "0x" + "cd" * 32, None, None, True),             # ambiguous send
    ("error", None, '{"stages":{}}', "2026-10-07T23:30:00+00:00", True),   # crash after the bind
    ("refused", None, '{"stages":{}}', None, False),             # the hook refused it
    ("aborted", None, '{"stages":{}}', None, False),             # the executor stopped before a send
])
def test_only_a_deposit_row_that_sent_or_is_in_flight_counts_as_taken(tmp_path, terminal, tx_sig, trace,
                                                                       bound, taken):
    import sqlite3
    from datetime import datetime, timezone
    from sniper.persistence import db_memecoin as db
    from sniper.services.memecoin_onchain_lane import already_attempted_from_another_lane
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    tx = "0x" + "ab" * 32
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, "
                 "delivery_lane, network_id, mint, source_user_id, decision_started_at, terminal_status, "
                 "terminal_reason, completed_at, tx_sig, trace_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("a1", "rhfast:4663:%s:%s" % (tx, MINT), "rh_fast_lane", dep.DEPOSIT_DELIVERY_LANE,
                  "4663", MINT, UID, now, terminal, terminal and "why", terminal and now, tx_sig, trace))
    conn.execute("INSERT INTO memecoin_pending_entries (source_user_id, network_id, mint, attempt_id, "
                 "status, submission_bound_at) VALUES (?,?,?,?,?,?)",
                 (UID, "4663", MINT, "a1", "reserved", bound))
    assert already_attempted_from_another_lane(conn, "4663:%s:%s" % (tx, MINT), now,
                                               source_user_id=UID) is taken


# ---- operator option 2 at the executor: a deposit-proved row signs the cap's minimum alone ----

def test_a_deposit_proved_row_signs_the_caps_minimum_without_the_copy_floor(entry, monkeypatch):
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_entry import _go, _payload
    from sniper.services.memecoin_rh_fast_lane import clip_wei_for
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_PROOF", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 1.0)   # a floor tighter than the cap
    built, sender, net, ledger = entry
    p = _lane_payload()
    rec = _go(built, p)
    assert sender.raws, rec                                   # reached the send
    cap_min = int(rec["clip_wei"]) * int(p["cap_den"]) // int(p["cap_num"])
    assert rec["min_out"] == cap_min and int(rec["copy_floor"]) == 0, rec
    # The same buy proved by its receipt keeps the copy floor, which binds here.
    receipt = _payload()
    receipt["snapshot"] = dict(receipt["snapshot"], read_at=__import__("time").time() + 2.0)
    got = _go(built, receipt)
    assert int(got["copy_floor"]) > 0 and got["min_out"] == int(got["copy_floor"]), got


# ---- 2026-10-08 (Gamma, pos 71906): a deposit-proved re-ask waits for the source's TAPE swap,
# never for the receipt the deposit already replaced ----

def _reask_tape(with_source_swap):
    from test_memecoin_rh_fast_decide import B, ZPOOL, ZUP_IN, ZUP_OUT, ZUP_POST, ZUP_PRE, _row, _tape
    rows = [_row(ZPOOL, B - 89, "0xpre", ZUP_PRE["sqrt"])]
    if with_source_swap:
        rows.append(_row(ZPOOL, B, "0xsrc", ZUP_POST, a0=ZUP_IN, a1=-ZUP_OUT))
    return _tape(rows)


@pytest.mark.parametrize("exact, proved, swap, ready", [
    (False, True, True, True),        # the source's swap is on the tape: re-decide now (tape_post)
    (False, True, False, False),      # not on the tape yet: keep waiting
    (False, False, True, False),      # no deposit proof: today's receipt wait
    (True, True, True, False),        # REQUIRE_EXACT_STATE on: the receipt is still required
])
def test_a_deposit_proved_reask_is_ready_on_the_tape_swap(monkeypatch, exact, proved, swap, ready):
    from sniper.services import memecoin_rh_fast_decide as fast
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_decide import SIG
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", exact)
    rec = dict(SIG, deposit_proof={"order_id": ORDER_ID} if proved else None)
    pending = fast.PendingSourceSwaps(wait_s=2.0)
    assert pending.offer(rec, enqueued=0.0, now=0.0)
    got = pending.ready(_reask_tape(swap), now=0.5)
    assert bool(got) is ready


def test_an_expired_deposit_reask_names_the_missing_tape_swap(monkeypatch):
    from sniper.services import memecoin_rh_fast_decide as fast
    from sniper.settings import memecoin as cfg
    from test_memecoin_rh_fast_decide import SIG
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", False)
    pending = fast.PendingSourceSwaps(wait_s=2.0)
    pending.offer(dict(SIG, deposit_proof={"order_id": ORDER_ID}), enqueued=0.0, now=0.0)
    assert pending.ready(_reask_tape(False), now=3.0) == []
    [(_rec, _enq, blocker)] = pending.pop_expired()
    assert blocker == "tape_swap_absent"


def test_a_ready_deposit_reask_decides_tape_post_without_a_receipt(tmp_path, monkeypatch):
    import time as _time
    from sniper.services import memecoin_rh_fast_decide as fast
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_LANE_REQUIRE_EXACT_STATE", False)
    tape_live, live, rec = _signal()
    tape = _reask_tape(True)
    rec.update(tx_hash="0xsrc", deposit_proof={"order_id": ORDER_ID, "origin_tx": "x"},
               deposit_status="matched")
    pending = fast.PendingSourceSwaps(wait_s=2.0)
    pending.offer(rec, enqueued=0.0, now=0.0)
    sent = []
    decided = fast.service_pending_sources(
        pending, tape=tape, live=live, db_path=str(tmp_path / "d.db"), now_fn=lambda: 1000.0,
        on_decided=lambda r, out, params: sent.append(out), now=0.5, deposits=dep.DepositProofs(registry_ok=lambda d, u: True))
    assert decided == 1 and sent and sent[0]["state_kind"] == "tape_post", sent
    assert sent[0]["reason"] == "would_send" and not sent[0].get("source_fill")
