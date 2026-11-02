"""The pinned-close re-offer on the PRODUCTION contexts, behind its per-chain switch.

`recover_unverified_closes` re-offers a not_found sell's own stored bytes (64ca8da61, 2026-09-02).
It read two keys the production contexts never set, so it never ran on any chain, and its wiring
tests were source scans (tests/test_memecoin_exit_rebroadcast.py). These tests build the real
Solana context and the real EVM context of every funded chain, put one signed-and-unseen sell in
a ledger, and count what reaches the chain's send method.

The switch, MEMECOIN_PINNED_CLOSE_REOFFER_CHAINS, is empty by default: nothing is re-offered.
"""
import base64
import json
import sqlite3

import pytest
from eth_account import Account
from eth_utils import keccak

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_exit_rebroadcast as REB
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg
from test_memecoin_evm_context_keys import FUNDED_EVM_CHAINS

CHAINS = ("solana",) + FUNDED_EVM_CHAINS
SUBMITTED, NOW = "2026-09-30T12:00:00+00:00", "2026-09-30T12:00:30+00:00"      # 30 s old
EVM_MINT, SOL_MINT = "0x" + "5e" * 20, "So1Mint" + "1" * 37
SOL_SIG = "5" * 87
SWITCH = "MEMECOIN_PINNED_CLOSE_REOFFER_CHAINS"


class Chain:
    """One fake node per chain: answers the context's reads and records every send."""

    def __init__(self, chain):
        self.chain, self.sends, self.send_error = chain, [], None
        self.nonce_latest, self.nonce_pending, self.receipt = 7, 8, None
        self.send_timeouts, self.send_delay_s = [], 0.0
        self.receipt_reads, self.receipt_down_from = 0, None    # the Nth receipt read fails
        self.receipt_timeouts, self.receipt_delay_s = [], 0.0
        self.receipt_remaining = []             # the copy's budget left at each receipt read
        self.send_log, self.send_status = [], None      # (url, monotonic at arrival); an HTTP status

    def __call__(self, _method, url, headers=None, body=None, timeout=None):
        req = json.loads(body)
        if isinstance(req, list):
            return 200, [self._one(r, timeout) for r in req], None
        if req["method"] in ("eth_sendRawTransaction", "sendTransaction"):
            import time
            self.send_log.append((url, time.monotonic()))
        answer = self._one(req, timeout)
        if req["method"] == "eth_sendRawTransaction" and self.send_status:
            return self.send_status, {}, None
        return 200, answer, None

    def _one(self, req, timeout=None):
        name, params = req["method"], req.get("params") or []
        if name in ("eth_getTransactionReceipt", "getSignatureStatuses"):
            self.receipt_reads += 1
            self.receipt_timeouts.append(timeout)
            self.receipt_remaining.append(REB._copy_remaining_s())
            if self.receipt_delay_s and self.receipt_reads % 2 == 0:    # the copy's read is the 2nd
                import time
                time.sleep(self.receipt_delay_s)
            if self.receipt_down_from is not None and self.receipt_reads >= self.receipt_down_from:
                raise OSError("receipt read: node down")
        if name in ("eth_sendRawTransaction", "sendTransaction"):
            self.sends.append(params[0])
            self.send_timeouts.append(timeout)
            if self.send_delay_s:
                import time
                time.sleep(self.send_delay_s)
            if self.send_error:
                return {"error": {"code": -32000, "message": self.send_error}}
            return {"result": SOL_SIG if name == "sendTransaction" else
                    "0x" + keccak(bytes.fromhex(params[0][2:])).hex()}
        answers = {
            "eth_chainId": hex(int(self.chain)) if self.chain.isdigit() else None,
            "eth_getTransactionReceipt": self.receipt,
            "eth_getTransactionCount": hex(self.nonce_pending if params[-1:] == ["pending"]
                                           else self.nonce_latest),
            "eth_getBalance": hex(10 ** 18), "eth_gasPrice": hex(10 ** 9), "eth_blockNumber": "0x64",
            "getHealth": "ok", "getSignatureStatuses": {"context": {"slot": 1}, "value": [None]},
        }
        if name == "getGenesisHash":
            from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS
            return {"result": SOLANA_MAINNET_GENESIS}
        if name == "eth_call":                                  # decimals, else the coin balance
            data = params[0].get("data", "")
            return {"result": "0x" + format(18 if data.startswith("0x313ce567") else 5 * 10 ** 18, "064x")}
        return {"result": answers.get(name)}


def _context(monkeypatch, tmp_path, chain, node):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    if chain == "solana":
        from sniper.execution.solana_wallet import generate_keypair_file
        keypair = str(tmp_path / "sol.json")
        generate_keypair_file(keypair)
        monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", keypair)
        monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "https://rpc-solana")
        monkeypatch.delenv("MEMECOIN_SOLANA_SUBMIT_MODE", raising=False)
        return R.build_real_context(http=node, own_sidecar=False)
    from sniper.execution.evm_wallet import generate_keypair_file
    from sniper.services import memecoin_evm_leg as leg
    keypair = str(tmp_path / "evm.json")
    generate_keypair_file(keypair)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", keypair)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_" + chain, "direct")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_" + chain, "https://rpc-" + chain)
    monkeypatch.delenv("MEMECOIN_EVM_SUBMIT_MODE", raising=False)
    return leg.build_real_evm_context(chain, http=node)


def _signed_sell(chain):
    """(the string the executor first sent, its identity, nonce): what the intent stores."""
    if chain == "solana":
        return base64.b64encode(b"a signed solana sell " * 8).decode(), SOL_SIG, None
    signed = Account.create().sign_transaction({
        "to": "0x" + "66" * 20, "value": 0, "data": "0x3593564c", "gas": 700_000,
        "gasPrice": 10 ** 9, "nonce": 7, "chainId": int(chain)})
    raw = "0x" + signed.raw_transaction.hex().removeprefix("0x")
    return raw, "0x" + signed.hash.hex().removeprefix("0x"), 7


@pytest.fixture
def pinned(monkeypatch, tmp_path, request):
    """One real sell, signed and submitted 30 s ago, that the chain has never seen."""
    chain = request.param
    REB.reset_state()
    monkeypatch.setattr(R, "_safe_alert", lambda *a, **k: None)
    monkeypatch.setattr(REB, "_spawn", lambda job: job())      # the detached copy runs inline here
    wall = [None]                                              # the REAL clock at the dispatch
    monkeypatch.setattr(REB, "_wall", lambda: wall[0])
    node = Chain(chain)
    ctx = _context(monkeypatch, tmp_path, chain, node)
    assert ctx is not None
    raw, identity, nonce = _signed_sell(chain)
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    intent = {"tx_identity": identity, "nonce": nonce, "balance_snapshot": 5.0,
              "signed_payload_b64": base64.b64encode(raw.encode()).decode()}
    conn.execute(
        "INSERT INTO memecoin_positions (id, mint, network_id, source_user_id, execution_mode, "
        "is_open, entry_price, entry_notional, opened_at, close_claim_id, close_sig, "
        "close_submitted_at, exit_intent_json) VALUES (1, ?, ?, 'u', 'real', 1, 1.0, 20.0, ?, "
        "'claim-1', ?, ?, ?)",
        (SOL_MINT if chain == "solana" else EVM_MINT, chain, SUBMITTED, identity, SUBMITTED,
         json.dumps(intent)))
    conn.commit()

    class P:
        pass
    p = P()
    p.chain, p.node, p.ctx, p.conn, p.raw, p.identity = chain, node, ctx, conn, raw, identity

    def sweep(now=NOW, reads_s=0.0):
        """One sweep stamped `now`; its own reads before the dispatch took `reads_s` of real time."""
        wall[0] = REB._epoch(now) + reads_s
        return R.recover_unverified_closes(conn, {chain: p.ctx}, now)
    p.sweep = sweep
    p.sweeps = lambda n: [p.sweep(now=_aged(20 + 15 * i)) for i in range(n)]  # ages 20, 35, 50 ...
    p.evm = chain != "solana"
    p.position = lambda: dict(conn.execute("SELECT * FROM memecoin_positions WHERE id=1").fetchone())
    yield p
    conn.close()


def _aged(seconds):
    """The sweep time at which the sell is `seconds` old."""
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(SUBMITTED) + timedelta(seconds=seconds)).isoformat()


every_chain = pytest.mark.parametrize("pinned", CHAINS, indirect=True)


@every_chain
def test_with_the_switch_off_nothing_is_ever_sent(pinned, monkeypatch):
    """The default, and the merge's whole live effect: the path is today's."""
    monkeypatch.delenv(SWITCH, raising=False)
    before = pinned.position()
    pinned.sweeps(4)
    assert pinned.node.sends == [] and pinned.position() == before
    assert REB.snapshot()["eligible"] == 0


@every_chain
def test_a_chain_named_in_the_switch_re_offers_the_sells_own_bytes_and_nothing_else(pinned, monkeypatch):
    monkeypatch.setenv(SWITCH, ",".join(CHAINS))
    before = pinned.position()
    pinned.sweep()
    assert pinned.node.sends == [pinned.raw]                   # byte-equal to the first send
    if pinned.chain != "solana":                               # same hash, so same nonce: one sell
        assert "0x" + keccak(bytes.fromhex(pinned.node.sends[0][2:])).hex() == pinned.identity
    assert pinned.position() == before                         # the claim is untouched: still held


@every_chain
def test_another_chains_switch_does_not_open_this_one(pinned, monkeypatch):
    monkeypatch.setenv(SWITCH, ",".join(c for c in CHAINS if c != pinned.chain))
    pinned.sweep()
    assert pinned.node.sends == []


@every_chain
def test_a_submission_is_re_offered_twice_at_most(pinned, monkeypatch):
    monkeypatch.setenv(SWITCH, pinned.chain)
    if pinned.chain != "solana":
        pinned.node.nonce_pending = 9          # the node holds something of ours: no 60 s release
    pinned.sweeps(6)                                                # ages 20 to 95 s
    assert REB.MAX_REOFFERS == 2 and pinned.node.sends == [pinned.raw] * 2   # at 20 s and 35 s
    assert REB.snapshot()["exhausted"] == 4
    assert pinned.node.send_timeouts == [REB.SEND_TIMEOUT_S] * 2     # its own short timeout
    assert pinned.node.receipt_timeouts.count(REB.RECEIPT_TIMEOUT_S) == 2     # each copy's own read


@every_chain
def test_two_sweeps_inside_fifteen_seconds_send_one_copy(pinned, monkeypatch):
    monkeypatch.setenv(SWITCH, pinned.chain)
    for age in (20, 25, 34):
        pinned.sweep(now=_aged(age))
    assert pinned.node.sends == [pinned.raw]
    pinned.sweep(now=_aged(35))
    assert pinned.node.sends == [pinned.raw] * 2


@every_chain
def test_a_rejection_consumes_an_attempt(pinned, monkeypatch, caplog):
    """The first copy landed late, or the node refuses the bytes: the answer repeats, so the
    bound must count it. A re-offer can never become a second sell either way."""
    monkeypatch.setenv(SWITCH, pinned.chain)
    pinned.node.send_error = "nonce too low"
    if pinned.chain != "solana":
        pinned.node.nonce_pending = 9
    with caplog.at_level("WARNING"):
        pinned.sweeps(6)
    assert pinned.node.sends == [pinned.raw] * REB.MAX_REOFFERS
    assert REB.snapshot()["send_failed"] == REB.MAX_REOFFERS and REB.snapshot()["resent"] == 0
    assert pinned.position()["close_sig"] == pinned.identity
    # One line per refused copy: the transaction, the attempt, the node's own words.
    lines = [r.getMessage() for r in caplog.records if "re-offer refused" in r.getMessage()]
    assert len(lines) == 2 and all("tx=%s" % pinned.identity in line for line in lines)
    assert [line.split("attempt=")[1][:3] for line in lines] == ["1/2", "2/2"]
    assert all("nonce too low" in line for line in lines), lines


@every_chain
def test_a_sell_younger_than_twenty_seconds_is_not_re_offered(pinned, monkeypatch):
    monkeypatch.setenv(SWITCH, pinned.chain)
    pinned.sweep(now="2026-09-30T12:00:19+00:00")
    assert pinned.node.sends == []


@every_chain
def test_a_provably_dead_sell_is_released_and_never_re_offered(pinned, monkeypatch):
    """Release comes first. The re-armed exit signs a NEW sell; offering the dead one again
    beside it is what the order exists to prevent."""
    monkeypatch.setenv(SWITCH, pinned.chain)
    if pinned.chain == "solana":
        intent = json.loads(pinned.position()["exit_intent_json"])
        pinned.conn.execute("UPDATE memecoin_positions SET exit_intent_json=? WHERE id=1",
                            (json.dumps(dict(intent, last_valid_height=100)),))
        pinned.conn.commit()
        # The two READ seams of the blockhash proof; the send and confirm seams stay production's.
        pinned.ctx = dict(pinned.ctx, block_height_fn=lambda: 101, token_checked_fn=lambda mint: 5.0)
    else:
        pinned.node.nonce_latest = pinned.node.nonce_pending = 8     # nonce 7 spent by another tx
    pinned.sweep()
    after = pinned.position()
    assert after["close_sig"] is None and after["close_claim_id"] is None, after
    assert pinned.node.sends == []
    pinned.sweep(now=_aged(55))
    assert pinned.node.sends == []


@pytest.mark.parametrize("pinned", FUNDED_EVM_CHAINS, indirect=True)
def test_the_two_copies_go_at_twenty_and_thirty_five_seconds_and_none_near_the_release(
        pinned, monkeypatch):
    """latest == pending == the sell's nonce: the node holds nothing of ours. The copies go at 20
    and 35 s of age; at 60 s the claim is released and nothing is offered after it."""
    monkeypatch.setenv(SWITCH, pinned.chain)
    pinned.node.nonce_latest = pinned.node.nonce_pending = 7
    sent = []
    for age in (19, 20, 34, 35, 49, 50, 59):
        pinned.sweep(now=_aged(age))
        sent.append(len(pinned.node.sends))
    assert sent == [0, 1, 1, 2, 2, 2, 2]
    assert pinned.position()["close_sig"] == pinned.identity
    pinned.sweep(now=_aged(60))                                   # 60 s old: released
    assert pinned.position()["close_sig"] is None
    pinned.sweep(now=_aged(75))
    assert len(pinned.node.sends) == 2


@every_chain
def test_a_failed_receipt_read_sends_nothing(pinned, monkeypatch):
    """Unknown is not absent (Gamma LOW 2 on 0bc521735). The sweep's own read says not_found; the
    re-offer reads again before it sends, and when THAT read fails the copy is not sent."""
    monkeypatch.setenv(SWITCH, pinned.chain)
    pinned.node.receipt_down_from = 2                           # the sweep's read, then the copy's
    pinned.sweep()
    assert pinned.node.receipt_reads >= 2 and pinned.node.sends == []
    pinned.node.receipt_down_from = 1                           # and when the sweep's own read fails
    pinned.sweep(now=_aged(36))
    assert pinned.node.sends == []


@pytest.mark.parametrize("pinned", ["solana"], indirect=True)
def test_the_switch_is_tested_on_the_normalized_network(pinned, monkeypatch):
    """A Solana position whose network_id is NULL is still a Solana position (Gamma LOW 1)."""
    monkeypatch.setenv(SWITCH, "solana")
    pinned.conn.execute("UPDATE memecoin_positions SET network_id=NULL WHERE id=1")
    pinned.conn.commit()
    pinned.sweep()
    assert pinned.node.sends == [pinned.raw]


@every_chain
def test_a_slow_node_never_holds_the_exit_sweep(pinned, monkeypatch):
    """Rule 5.2, measured with REAL threads: the send sleeps 1.5 s in the node fake and the sweep
    returns without it. 15 sweeps each way; the timings are printed for the review."""
    import threading
    import time
    threads = []

    def spawn(job):
        thread = threading.Thread(target=job, daemon=True)
        threads.append(thread)
        thread.start()
    monkeypatch.setattr(REB, "_spawn", spawn)
    pinned.node.send_delay_s = 1.5
    if pinned.chain != "solana":
        pinned.node.nonce_pending = 9

    def timed(switch):
        REB.reset_state()
        monkeypatch.setenv(SWITCH, switch)
        out = []
        for _ in range(15):
            started = time.perf_counter()
            pinned.sweep()
            out.append((time.perf_counter() - started) * 1000.0)
        return sorted(out)
    off = timed("")
    on = timed(pinned.chain)
    started_copies = len(threads)
    for thread in threads:
        thread.join(10)
    print("\nsweep ms chain=%s  switch off p50=%.2f p90=%.2f  |  re-offer pending p50=%.2f p90=%.2f"
          "  (send sleeps 1500 ms)" % (pinned.chain, off[7], off[13], on[7], on[13]))
    # ONE copy in 15 sweeps at one instant: every later sweep started nothing.
    assert started_copies == 1 and pinned.node.sends == [pinned.raw]
    assert REB.snapshot()["in_flight"] + REB.snapshot()["not_due"] == 14
    assert REB.snapshot()["in_flight_now"] == 0
    assert on[-1] < 500.0, "a sweep waited for the send: %s" % on   # the slowest of 15, not 1500 ms


@pytest.mark.parametrize("pinned", FUNDED_EVM_CHAINS, indirect=True)
def test_a_sell_whose_receipt_exists_is_not_re_offered(pinned, monkeypatch):
    monkeypatch.setenv(SWITCH, pinned.chain)
    pinned.node.receipt = {"status": "0x0", "blockNumber": "0x63", "transactionHash": pinned.identity}
    pinned.sweep()
    assert pinned.node.sends == []


def test_the_switch_is_empty_by_default_and_malformed_is_empty(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    assert cfg.memecoin_pinned_close_reoffer_chains() == frozenset()
    monkeypatch.setenv(SWITCH, " solana , 4663 ")
    assert cfg.memecoin_pinned_close_reoffer_chains() == frozenset({"solana", "4663"})
    for bad in ("all", "4663;56", "1,,x y"):
        monkeypatch.setenv(SWITCH, bad)
        assert cfg.memecoin_pinned_close_reoffer_chains() == frozenset(), bad


def test_a_stored_payload_that_is_not_the_executors_text_is_never_sent():
    """Nothing is re-encoded. Binary bytes where the executor's string should be raise before the
    send seam, and `rebroadcast_pinned_exit` counts that as send_raised."""
    sent = []
    send = REB.stored_payload_sender(sent.append)
    with pytest.raises(UnicodeDecodeError):
        send(b"\xff\xfe\x00binary")
    assert sent == [] and REB.stored_payload_sender(None) is None
    send(b"0xabcdef")
    assert sent == ["0xabcdef"]
    REB.reset_state()
    intent = {"tx_identity": "0xid", "signed_payload_b64": base64.b64encode(b"\xff\xfe").decode()}
    assert REB.rebroadcast_pinned_exit(intent, SUBMITTED, now_iso=NOW, receipt_fn=lambda h: None,
                                       send_fn=send) == "send_raised"
    assert sent == ["0xabcdef"]


def test_the_heartbeat_carries_the_re_offer_counters(tmp_path):
    from sniper.services import memecoin_runner as run
    REB.reset_state()
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), pinned_reoffer=REB.snapshot())
    beat = json.loads(path.read_text())["pinned_reoffer"]
    assert beat["resent"] == 0 and beat["in_flight_now"] == 0 and "send_failed" in beat
    from pathlib import Path
    source = (Path(run.__file__).resolve().parents[2] / "scripts/memecoin_runner_loop.py").read_text()
    assert "pinned_reoffer=_pinned_reoffer_status()" in source


# --- a copy never arrives after its claim was released (Gamma, three rounds) ----------------------
#
# EVM releases a never-admitted sell at 60 s with its nonce still FREE, and the re-armed exit signs
# a NEW sell on that nonce. Old bytes sent after that compete with it.


def _real_threads(monkeypatch):
    import threading
    threads = []

    def spawn(job):
        thread = threading.Thread(target=job, daemon=True)
        threads.append(thread)
        thread.start()
    monkeypatch.setattr(REB, "_spawn", spawn)
    return threads


@pytest.mark.parametrize("pinned", FUNDED_EVM_CHAINS, indirect=True)
def test_no_copy_starts_with_less_than_a_read_and_a_send_before_its_last_send_time(
        pinned, monkeypatch):
    """The last send time is 43 s after the submission (the 60 s release less 17 s). A copy needs
    a receipt read and a send, 2 s each: past 39 s nothing is started. A restart finds the sell old."""
    monkeypatch.setenv(SWITCH, pinned.chain)
    pinned.node.nonce_latest = pinned.node.nonce_pending = 7
    assert cfg.MEMECOIN_EVM_DROPPED_CLOSE_MIN_AGE_S - REB.RELEASE_CLEARANCE_S == 43.0
    for age in (39.5, 43, 55, 59):
        pinned.sweep(now=_aged(age))
    assert pinned.node.sends == [] and REB.snapshot()["too_late"] == 4
    pinned.sweep(now=_aged(60))                                   # 60 s: released
    assert pinned.position()["close_sig"] is None and pinned.node.sends == []


@pytest.mark.parametrize("pinned", FUNDED_EVM_CHAINS, indirect=True)
def test_a_copy_first_seen_at_thirty_nine_seconds_still_goes(pinned, monkeypatch):
    monkeypatch.setenv(SWITCH, pinned.chain)
    pinned.node.nonce_latest = pinned.node.nonce_pending = 7
    pinned.sweep(now=_aged(39))
    assert pinned.node.sends == [pinned.raw]


@pytest.mark.parametrize("pinned", FUNDED_EVM_CHAINS, indirect=True)
def test_the_copys_deadline_is_anchored_to_the_real_clock_not_the_sweeps_stamp(pinned, monkeypatch):
    """Gamma on 27e566474. `now_iso` is stamped when the sweep starts; its own reads, for this
    and earlier positions, run before the dispatch. A sweep stamped 36 s whose reads took 5 s is
    really at 41 s: a copy from there could not finish by 43 s."""
    monkeypatch.setenv(SWITCH, pinned.chain)
    pinned.node.nonce_latest = pinned.node.nonce_pending = 7
    pinned.sweep(now=_aged(36), reads_s=5.0)
    assert pinned.node.sends == [] and REB.snapshot()["too_late"] == 1
    # And a copy that does start gets what is REALLY left, never 8 s from the sweep's stamp.
    REB.reset_state()
    monkeypatch.setattr(REB, "_monotonic", lambda: 1000.0)
    pinned.sweep(now=_aged(30), reads_s=6.0)                      # really 36 s: 7 s to the last send
    assert pinned.node.sends == [pinned.raw]
    assert pinned.node.receipt_remaining[-1] == 7.0
    assert pinned.node.receipt_remaining[-2] == float("inf")      # the sweep's own read: no deadline
    REB.reset_state()
    pinned.sweep(now=_aged(21))                                   # early: the copy's own 8 s
    assert pinned.node.receipt_remaining[-1] == REB.COPY_DEADLINE_S == 8.0


@pytest.mark.parametrize("pinned", ["4663"], indirect=True)
def test_a_pooled_copy_starts_no_request_that_could_outlast_its_deadline(pinned, monkeypatch):
    """Gamma on 27e566474: a per-request timeout does not bound a copy, because the RPC layer
    rotates across pool members, each with its own timeout. REAL thread, three members, each
    answering the send with HTTP 429 after 1.2 s. The copy has 4 s: two members are asked, the
    third is not, and it is not quarantined for a request it never got."""
    import time
    from sniper.execution import evm_rpc_pool as pool
    members = ("https://rpc-4663", "https://pool-2", "https://pool-3")
    pool.reset_for_tests()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"4663": members})
    threads = _real_threads(monkeypatch)
    monkeypatch.setenv(SWITCH, "4663")
    pinned.node.nonce_latest = pinned.node.nonce_pending = 7
    pinned.node.send_status, pinned.node.send_delay_s = 429, 1.2
    try:
        started = time.monotonic()
        pinned.sweep(now=_aged(39))                               # 4 s to the last send time
        assert len(threads) == 1
        pinned.sweep(now=_aged(41))                               # a sweep while the copy runs
        threads[0].join(10)
        asked = [url for url, _at in pinned.node.send_log]
        assert len(asked) == 2 and len(set(asked)) == 2, asked
        # No send started with less than its own 2 s timeout left of the 4 s.
        assert max(at - started for _url, at in pinned.node.send_log) <= 4.0 - REB.SEND_TIMEOUT_S
        unasked = (set(members) - set(asked)).pop()
        assert pool.available(unasked) and not any(pool.available(url) for url in asked)
        snap = REB.snapshot()
        assert snap["send_failed"] == 1 and snap["resent"] == 0 and snap["in_flight_now"] == 0
        pool.reset_for_tests()
        pinned.node.send_status = None
        pinned.sweep(now=_aged(60))                               # the release
        assert pinned.position()["close_sig"] is None
        pinned.sweep(now=_aged(75))
        assert len(pinned.node.send_log) == 2, "old bytes were sent after the release"
    finally:
        pool.reset_for_tests()


@pytest.mark.parametrize("pinned", FUNDED_EVM_CHAINS, indirect=True)
def test_a_claim_is_not_released_while_a_copy_of_its_sell_is_in_flight(pinned, monkeypatch):
    """The belt. REAL thread: the copy's receipt read hangs 2.3 s. A sweep finds the sell
    releasable meanwhile (60 s, nonce free): it must wait for the copy, then release."""
    threads = _real_threads(monkeypatch)
    monkeypatch.setenv(SWITCH, pinned.chain)
    pinned.node.nonce_latest = pinned.node.nonce_pending = 7
    pinned.node.receipt_delay_s = 2.3                           # the copy's read only
    pinned.sweep(now=_aged(39))                                   # 4 s of budget: dispatched
    assert len(threads) == 1
    pinned.node.receipt_delay_s = 0.0
    pinned.sweep(now=_aged(60))                                   # releasable, but a copy is out
    assert pinned.position()["close_sig"] == pinned.identity
    assert REB.snapshot()["release_deferred"] == 1
    threads[0].join(10)
    # The read left 1.7 s, less than one send's timeout: the copy is dropped and uses an attempt.
    assert pinned.node.sends == []
    snap = REB.snapshot()
    assert snap["late"] == 1 and snap["resent"] == 0 and snap["tracked"] == 1
    pinned.sweep(now=_aged(61))
    assert pinned.position()["close_sig"] is None and pinned.node.sends == []
    assert REB.snapshot()["release_deferred"] == 1


@pytest.mark.parametrize("pinned", ["solana"], indirect=True)
def test_solana_has_no_deadline_and_no_deferred_release(pinned, monkeypatch):
    """Past its blockhash a Solana copy cannot land, so nothing is released with the sell alive,
    and a release never waits for a copy. REAL thread: the copy's send is still out when the
    blockhash proof fires."""
    monkeypatch.setenv(SWITCH, "solana")
    pinned.sweep(now=_aged(300))                                  # 300 s old
    assert pinned.node.sends == [pinned.raw]
    assert pinned.node.receipt_remaining[-1] == float("inf") and REB.snapshot()["too_late"] == 0
    threads = _real_threads(monkeypatch)
    pinned.node.send_delay_s = 1.0
    pinned.sweep(now=_aged(320))                                  # the second copy, in flight
    assert len(threads) == 1 and REB.snapshot()["in_flight_now"] == 1
    intent = json.loads(pinned.position()["exit_intent_json"])
    pinned.conn.execute("UPDATE memecoin_positions SET exit_intent_json=? WHERE id=1",
                        (json.dumps(dict(intent, last_valid_height=100)),))
    pinned.conn.commit()
    pinned.ctx = dict(pinned.ctx, block_height_fn=lambda: 101, token_checked_fn=lambda mint: 5.0)
    pinned.sweep(now=_aged(321))
    assert pinned.position()["close_sig"] is None and REB.snapshot()["release_deferred"] == 0
    threads[0].join(10)


def test_two_pinned_sells_in_one_sweep_each_read_their_own_chain(monkeypatch, tmp_path):
    """The copy's receipt read is bound per position (Gamma LOW 1). With a late-bound closure and
    real threads, both copies would ask the LAST position's chain."""
    REB.reset_state()
    monkeypatch.setattr(R, "_safe_alert", lambda *a, **k: None)
    jobs = []
    monkeypatch.setattr(REB, "_spawn", jobs.append)             # the jobs run after the sweep
    monkeypatch.setattr(REB, "_wall", lambda: REB._epoch(NOW))
    monkeypatch.setenv(SWITCH, "4663,56")
    nodes = {chain: Chain(chain) for chain in ("4663", "56")}
    contexts = {chain: _context(monkeypatch, tmp_path, chain, nodes[chain]) for chain in nodes}
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    raws = {}
    for position_id, chain in enumerate(nodes, 1):
        raw, identity, nonce = _signed_sell(chain)
        raws[chain] = raw
        intent = {"tx_identity": identity, "nonce": nonce, "balance_snapshot": 5.0,
                  "signed_payload_b64": base64.b64encode(raw.encode()).decode()}
        conn.execute(
            "INSERT INTO memecoin_positions (id, mint, network_id, source_user_id, execution_mode, "
            "is_open, entry_price, entry_notional, opened_at, close_claim_id, close_sig, "
            "close_submitted_at, exit_intent_json) VALUES (?, ?, ?, 'u', 'real', 1, 1.0, 20.0, ?, "
            "?, ?, ?, ?)", (position_id, "0x%040x" % position_id, chain, SUBMITTED,
                            "claim-%d" % position_id, identity, SUBMITTED, json.dumps(intent)))
    conn.commit()
    R.recover_unverified_closes(conn, contexts, NOW)
    assert len(jobs) == 2 and all(node.sends == [] for node in nodes.values())
    for job in jobs:
        job()
    for chain, node in nodes.items():
        assert node.sends == [raws[chain]], chain
        assert node.receipt_timeouts.count(REB.RECEIPT_TIMEOUT_S) == 1, chain   # its own copy's read
    conn.close()


def test_the_solana_send_error_is_taken_once():
    from sniper.execution import solana_real_seams as S

    def refusing(_method, _url, **kw):
        return 200, {"error": {"code": -32002, "message": "already processed"}}, None
    assert S.rpc_send("AAAA", "https://rpc", http=refusing) is None
    assert "already processed" in S.take_send_error()
    assert S.take_send_error() is None                          # consumed (Gamma LOW 3)
    assert S.rpc_send("AAAA", "https://rpc", http=lambda *a, **k: (200, {"result": "sig"}, None)) == "sig"
    assert S.take_send_error() is None                          # a success leaves no words
