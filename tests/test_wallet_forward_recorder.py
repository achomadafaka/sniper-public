"""Read-only forward recorder of one wallet's Solana buys (operator order 2026-10-02 via Alpha, for Gamma's
pre-registered notdecu size-tier test, gamma episode ee10f5f6). Real transactions from the repo's
fixtures; the research key, the clock and the capture start are fakes."""
import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"


def _mod():
    spec = importlib.util.spec_from_file_location("wallet_forward_recorder",
                                                  ROOT / "scripts" / "research" / "wallet_forward_recorder.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = _mod()
VENUES = json.loads((FX / "solana" / "funded_entry_venues.json").read_text())
for _v in VENUES.values():                       # the stored receipts omit blockTime; a real getTransaction
    if isinstance(_v, dict) and isinstance(_v.get("tx"), dict):   # carries it, and a unit needs it (W5)
        _v["tx"].setdefault("blockTime", 1_790_000_000)
OURS = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
# Our wallet bought DhvuLL... on PumpSwap, FUNDED IN USDC: since c9c57f2f8 (Codex PR #453, "bind USDC-funded
# Pump source valuation to receipt payment") its size is the receipt's own USDC payment, $41.78, not the
# event's SOL leg at this file's fake SOL $150 ($79.74), and a route the valuation cannot bind is unknown.
PS = VENUES["pumpswap_entry"]
PS_USD = 41.78
PS_ACCOUNT = "DPsbdySm8RCeCvK1e7TxJQS5dwqZGMDW29mUQ3zZWQeS"
PS_SLOT = 440267074
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def frame(account, mint, amount, slot, owner=OURS, program="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"):
    return {"jsonrpc": "2.0", "method": "programNotification", "params": {"result": {
        "context": {"slot": slot}, "value": {"pubkey": account, "account": {"owner": program, "data": {
            "parsed": {"info": {"mint": mint, "owner": owner, "tokenAmount": {"amount": str(amount),
                                                                             "decimals": 6}}}}}}}}}


class Key:
    """The research key: signatures per token account, transactions by signature."""

    def __init__(self, sigs=None, txs=None):
        self.sigs, self.txs, self.calls = dict(sigs or {}), dict(txs or {}), []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "getSignaturesForAddress":
            return self.sigs.get(params[0], [])
        if method == "getTransaction":
            return self.txs.get(params[0])
        raise AssertionError(method)


class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _recorder(tmp_path, key, *, min_usd=300.0, cap=100, clock=None, started=None, t0=None, owner=OURS):
    clock = clock or Clock()
    db = sqlite3.connect(tmp_path / "fwd.db")
    started = started if started is not None else []
    rec = R.Recorder(db, owner, rpc=key, sol_usd_fn=lambda: (150.0, clock()), min_usd=min_usd,
                     cap=R.CallCap(cap, clock=clock), now_fn=clock,
                     start_fn=lambda **kw: started.append(kw), t0=t0 if t0 is not None else 1.0)
    return rec, db, started


def _ps_key():
    sig = PS["signature"]
    return Key({PS_ACCOUNT: [{"signature": sig, "slot": PS_SLOT, "err": None}]}, {sig: PS["tx"]})


def test_a_rise_on_a_coin_account_is_resolved_to_a_valued_buy(tmp_path):
    rec, db, started = _recorder(tmp_path, _ps_key())
    assert rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 475643406075, PS_SLOT))
    rec.resolve()
    (row,) = db.execute("SELECT signature, mint, token_delta, size_usd, basis, unit FROM buys").fetchall()
    assert row[:3] == (PS["signature"], PS["mint"], "475643406075")
    assert abs(row[3] - PS_USD) < 1e-6 and row[4] == "route_owner_quote_out" and row[5] == 0
    assert started == []                                   # $41.78 < $300: recorded, not a unit


def test_the_first_buy_at_or_over_the_floor_on_a_coin_starts_one_capture(tmp_path):
    rec, db, started = _recorder(tmp_path, _ps_key(), min_usd=40.0)          # $41.78 >= $40
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 475643406075, PS_SLOT))
    rec.resolve()
    assert [(s["mint"], s["origin_event_id"]) for s in started] == [
        (PS["mint"], "forward:%s:%s" % (OURS, PS["signature"]))]
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 2 * 475643406075, PS_SLOT + 10))  # a second buy, same coin
    rec.key.sigs[PS_ACCOUNT] = [{"signature": "second", "slot": PS_SLOT + 10, "err": None}]
    rec.key.txs["second"] = json.loads(json.dumps(PS["tx"]).replace(PS["signature"], "second"))
    rec.resolve()
    assert len(started) == 1 and db.execute("SELECT SUM(unit), COUNT(*) FROM buys").fetchone() == (1, 2)


def test_a_buy_before_t0_is_never_a_unit(tmp_path):
    rec, db, started = _recorder(tmp_path, _ps_key(), min_usd=50.0, t0=2_000_000_000.0)
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 475643406075, PS_SLOT))
    rec.resolve()
    assert started == [] and db.execute("SELECT unit FROM buys").fetchall() == [(0,)]


def test_falls_quotes_and_other_owners_are_not_candidates(tmp_path):
    rec, _db, _ = _recorder(tmp_path, Key())
    assert rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 100, 1))         # first sight: resolve it
    assert not rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 40, 2))      # a fall (a sell)
    assert not rec.on_frame(frame("UsdcAcct", USDC, 10 ** 9, 3))       # a quote, never the coin
    assert not rec.on_frame(frame("Other", PS["mint"], 10 ** 9, 4, owner="SomeoneElse1111111111111111111111111111111"))
    assert not rec.on_frame({"jsonrpc": "2.0", "id": 1, "result": 7})  # a subscription ack


def test_a_rise_that_is_not_his_buy_is_recorded_as_not_a_buy(tmp_path):
    # e.g. a pump fee handout or a transfer in: the coin rises with no quote paid by him
    key = _ps_key()
    rec = R.Recorder(sqlite3.connect(tmp_path / "f.db"), "7UBjMaYsE3tKcanfgGrtso6aHAMhVNSVHkkX5j3t7f5y",
                     rpc=key, sol_usd_fn=lambda: (150.0, 1.0), min_usd=1.0, cap=R.CallCap(100, clock=Clock()),
                     now_fn=Clock(), start_fn=lambda **kw: None, t0=1.0)
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 1, PS_SLOT, owner="7UBjMaYsE3tKcanfgGrtso6aHAMhVNSVHkkX5j3t7f5y"))
    rec.resolve()
    assert rec.db.execute("SELECT why FROM dropped").fetchall() == [("not_his_buy",)]


def test_a_signature_not_yet_visible_is_retried_then_dropped(tmp_path):
    rec, db, _ = _recorder(tmp_path, Key())
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 1, PS_SLOT))
    for _ in range(R.TRIES - 1):
        rec.resolve()
        assert rec.pending and db.execute("SELECT COUNT(*) FROM dropped").fetchone() == (0,)
    rec.resolve()
    assert not rec.pending and db.execute("SELECT why FROM dropped").fetchall() == [("unresolved",)]


def test_the_hourly_call_cap_defers_and_logs_once_an_hour(tmp_path):
    clock = Clock()
    key = _ps_key()
    rec, db, _ = _recorder(tmp_path, key, cap=2, clock=clock)
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 1, PS_SLOT))
    rec.on_frame(frame("Acct2", "Mint2", 1, PS_SLOT - 1))           # older: resolved after the newer one
    rec.resolve()
    rec.resolve()
    assert len(key.calls) == 2 and len(rec.pending) == 1               # the second waits; nothing dropped
    assert db.execute("SELECT COUNT(*) FROM cap_hits").fetchone() == (1,)
    clock.t += 3600
    rec.resolve()
    assert len(key.calls) == 3


def test_the_t0_is_fixed_at_first_start_and_kept(tmp_path):
    db = sqlite3.connect(tmp_path / "t.db")
    kw = dict(rpc=Key(), sol_usd_fn=lambda: None, cap=R.CallCap(1, clock=Clock()), start_fn=lambda **k: None)
    first = R.Recorder(db, OURS, now_fn=Clock(100.0), **kw)
    again = R.Recorder(db, OURS, now_fn=Clock(999.0), **kw)
    assert first.t0 == again.t0 == 100.0


def test_the_production_start_makes_a_forward_capture_with_its_own_gate():
    captures = {}
    start = R.capture_starter(captures, gate="forward_notdecu", horizon_s=330.0, cadence_s=25.0,
                              max_concurrent=4, refuse_fn=lambda *a, **k: None, mono=lambda: 5.0,
                              wall=lambda: 1_790_000_000.0)
    start(mint="M1", origin_event_id="forward:o:s1", refused_at="2026-10-02T09:00:00+00:00")
    (c,) = captures.values()
    assert (c.mint, c.gate, c.origin_event_id, c.horizon_s, c.cadence_s) == (
        "M1", "forward_notdecu", "forward:o:s1", 330.0, 25.0)


def test_the_recorder_never_imports_the_fill_path():
    src = (ROOT / "scripts" / "research" / "wallet_forward_recorder.py").read_text()
    for name in ("memecoin_real_runner", "onchain_wallet_watch", "memecoin_onchain_wake", "make_real_buy_fn",
                 "record_onchain_detection"):
        assert name not in src, name


def test_a_buy_the_valuation_cannot_read_falls_back_to_trade_of_and_says_so(tmp_path):
    # Our pump-curve entry: its TradeEvent is the short pre-upgrade form (brain 9241badf), so the
    # runner's valuation is unknown; the signer path prices it by the USDC we paid ($40.79).
    v = VENUES["pump_curve_entry"]
    acct, slot = "121hrh3bsaUx8noFEf87KaNDZRPHYmH4tAh35W1KqumA", 440266031
    key = Key({acct: [{"signature": v["signature"], "slot": slot, "err": None}]}, {v["signature"]: v["tx"]})
    rec, db, started = _recorder(tmp_path, key, min_usd=40.0)
    rec.on_frame(frame(acct, v["mint"], 3719655987608, slot))
    rec.resolve()
    (size, basis, unit), = db.execute("SELECT size_usd, basis, unit FROM buys").fetchall()
    assert abs(size - 40.79) < 1e-9 and basis == "trade_of_USDC" and unit == 1 and len(started) == 1


def test_only_the_signature_in_the_notified_slot_is_read(tmp_path):
    # The token account's history also holds his older trades on the coin; reading them would spend
    # the cap and could record an old buy as this one.
    key = _ps_key()
    key.sigs[PS_ACCOUNT] = [{"signature": PS["signature"], "slot": PS_SLOT, "err": None},
                            {"signature": "older", "slot": PS_SLOT - 500, "err": None}]
    rec, db, _ = _recorder(tmp_path, key)
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 475643406075, PS_SLOT))
    rec.resolve()
    assert key.calls == ["getSignaturesForAddress", "getTransaction"]


def test_the_socket_pings_while_idle_and_a_gap_is_the_time_it_was_down():
    # Smoke 2026-10-02: Helius closed the idle socket after 5 minutes (ConnectionClosedOK); the sync
    # client sends no keepalive, and the gap row held the CONNECTED interval instead of the outage.
    import queue
    import threading
    stop, frames, status = threading.Event(), queue.Queue(), {}
    t = iter(range(100, 200))
    clock = lambda: float(next(t))                                       # noqa: E731

    class WS:
        def __init__(self, script):
            self.script, self.pings, self.sent = list(script), 0, []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, m):
            self.sent.append(m)

        def ping(self):
            self.pings += 1

        def recv(self, timeout=None):
            step = self.script.pop(0)
            if step == "idle":
                raise TimeoutError()
            if step == "close":
                raise ConnectionResetError("closed")
            stop.set()
            raise TimeoutError()
    sockets = [WS(["idle", "idle", "close"]), WS(["stop"])]
    first_ws = sockets[0]
    R._socket("wss://x", OURS, frames, status, stop, connect=lambda *a, **k: sockets.pop(0), clock=clock,
              wait=lambda s: None)
    assert first_ws.pings == 2 and len(first_ws.sent) == 2
    first = [f for f in list(frames.queue) if "_gap" in f]
    assert len(first) == 1
    down, up, why = first[0]["_gap"]
    assert why == "ConnectionResetError" and down < up and up - down <= 2      # the outage, not the session
    assert sockets == [] and status["connected"] is True


def test_a_coin_sent_to_him_by_another_signer_is_not_his_buy(tmp_path):
    # First live smoke 2026-10-02: all three "buys" were other wallets sending coins INTO his token
    # accounts (he signed none of them, his SOL never moved). His balance rising is not his buy.
    v = json.loads((FX / "memecoin" / "forward_recorder_transfer_in.json").read_text())
    key = Key({v["account"]: [{"signature": v["signature"], "slot": v["slot"], "err": None}]}, {v["signature"]: v["tx"]})
    rec = R.Recorder(sqlite3.connect(tmp_path / "f.db"), v["owner"], rpc=key, sol_usd_fn=lambda: (150.0, 1.0),
                     min_usd=0.0, cap=R.CallCap(100, clock=Clock()), now_fn=Clock(), start_fn=lambda **kw: None, t0=1.0)
    rec.on_frame(frame(v["account"], v["mint"], 3519000000, v["slot"], owner=v["owner"]))
    rec.resolve()
    assert rec.db.execute("SELECT COUNT(*) FROM buys").fetchone() == (0,)
    assert rec.db.execute("SELECT why FROM dropped").fetchall() == [("not_his_signature",)]


def test_a_new_empty_token_account_is_not_queued(tmp_path):
    # The same smoke: two rises were the creation of his account at 0 (no trade yet).
    rec, _db, _ = _recorder(tmp_path, Key())
    assert not rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 0, 1))
    assert rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 5, 2))              # its first real rise


def test_process_downtime_since_the_last_heartbeat_is_a_gap(tmp_path):
    # Gamma LOW 1: a crash, restart or overnight hibernate left no row, so a buy then was silently
    # absent instead of censored.
    db = sqlite3.connect(tmp_path / "g.db")
    db.executescript(R.DDL)
    hb = tmp_path / "g.db.heartbeat"
    R.record_downtime(db, hb, now=2_000_000_000.0)                     # first start: nothing to record
    hb.write_text(json.dumps({"ts": "2026-10-02T09:00:00+00:00"}))
    R.record_downtime(db, hb, now=1_790_931_600.0 + 600)              # 10 minutes after that ts
    assert db.execute("SELECT from_at, why FROM gaps").fetchall() == [("2026-10-02T09:00:00+00:00", "process_down")]


def test_the_buy_signature_is_found_past_the_newest_page(tmp_path):
    # Gamma LOW 2: more than one page of newer transactions on his account (dust sends, or a backlog
    # behind the cap) pushed the buy off the newest page and it dropped as unresolved.
    sig = PS["signature"]
    newer = [{"signature": "n%d" % i, "slot": PS_SLOT + 1 + i, "err": None} for i in range(R.SIG_PAGE)]

    class Paged(Key):
        def __call__(self, method, params):
            if method == "getSignaturesForAddress":
                self.calls.append(method)
                return newer if "before" not in params[1] else [{"signature": sig, "slot": PS_SLOT, "err": None}]
            return super().__call__(method, params)
    key = Paged(txs={sig: PS["tx"]})
    rec, db, _ = _recorder(tmp_path, key)
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 475643406075, PS_SLOT))
    rec.resolve()
    assert db.execute("SELECT signature FROM buys").fetchall() == [(sig,)]


def test_a_repeated_pass_does_not_duplicate_dropped_rows(tmp_path):
    # Gamma LOW 3: a same-slot signature without its transaction yet made the next pass re-drop the
    # ones it had already read.
    v = json.loads((FX / "memecoin" / "forward_recorder_transfer_in.json").read_text())
    key = Key({v["account"]: [{"signature": v["signature"], "slot": v["slot"], "err": None},
                              {"signature": "later", "slot": v["slot"], "err": None}]}, {v["signature"]: v["tx"]})
    rec = R.Recorder(sqlite3.connect(tmp_path / "d.db"), v["owner"], rpc=key, sol_usd_fn=lambda: (150.0, 1.0),
                     min_usd=0.0, cap=R.CallCap(100, clock=Clock()), now_fn=Clock(), start_fn=lambda **kw: None, t0=1.0)
    rec.on_frame(frame(v["account"], v["mint"], 3519000000, v["slot"], owner=v["owner"]))
    rec.resolve()
    rec.resolve()
    assert rec.db.execute("SELECT COUNT(*) FROM dropped WHERE why='not_his_signature'").fetchone() == (1,)


# --- Codex review 2026-10-06 (W1, W2, W4, W5) -------------------------------------------------------

def _store(tmp_path):
    from sniper.persistence import db_memecoin_captures as store
    return store, store.connect(str(tmp_path / "fwd.db"))


def test_a_stale_unit_gets_a_durable_refusal_and_a_restart_offers_only_units_without_one(tmp_path):
    # W1: 193 of 208 units resolved after their window and left no row at all. A refused start is now a
    # terminal capture row; a unit registered before a crash is offered again at restart.
    store, conn = _store(tmp_path)
    captures = {}
    start = R.capture_starter(captures, gate="forward_notdecu", horizon_s=330.0, cadence_s=25.0, max_concurrent=4,
                              refuse_fn=lambda cid, outcome, **kw: store.terminal_row(conn, cid, outcome, **kw),
                              mono=lambda: 5.0, wall=lambda: 1_790_000_000.0)
    rec = R.Recorder(conn, OURS, rpc=Key(), sol_usd_fn=lambda: None, start_fn=start, cap=R.CallCap(1, clock=Clock()),
                     now_fn=Clock(), t0=1.0)
    conn.execute("INSERT INTO buys (signature, mint, block_time, unit) VALUES ('s1', 'M1', 1789990000.0, 1)")
    conn.commit()                                     # registered, then the process died before the start
    assert rec.recover(lambda m, o: R.has_capture(conn, m, o)) == 1
    assert captures == {}
    assert [tuple(r) for r in conn.execute(
        "SELECT capture_id, outcome, gate, expected_obs FROM memecoin_refusal_captures")] == [
        ("solana:M1:forward:%s:s1" % OURS, "not_captured_stale", "forward_notdecu", 13)]
    assert rec.recover(lambda m, o: R.has_capture(conn, m, o)) == 0     # once only


def test_a_fresh_unit_recovered_at_restart_starts_its_capture(tmp_path):
    started = []
    rec, db, _ = _recorder(tmp_path, Key(), started=started)
    db.execute("INSERT INTO buys (signature, mint, block_time, unit) VALUES ('s1', 'M1', 1790000000.0, 1)")
    assert rec.recover(lambda m, o: False) == 1 and started[0]["origin_event_id"] == "forward:%s:s1" % OURS


def test_a_rise_that_can_still_be_measured_is_resolved_before_an_old_backlog(tmp_path):
    key = _ps_key()
    rec, _db, _ = _recorder(tmp_path, key, cap=2)
    rec.on_frame(frame("OldAcct", "OldMint", 1, PS_SLOT - 9000))
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 1, PS_SLOT))
    rec.resolve()
    assert key.calls == ["getSignaturesForAddress", "getTransaction"]    # the newest rise got the budget
    assert [c["account"] for c in rec.pending] == ["OldAcct"]           # the old one still waits, not dropped


def test_finished_captures_get_a_terminal_coverage_outcome(tmp_path):
    # W2: the recorder never ran the reaper, so all 206 capture rows kept outcome NULL.
    store, conn = _store(tmp_path)
    base = dict(network_id="solana", gate="forward_notdecu", refused_at="2026-10-02T09:00:00+00:00",
                horizon_s=330.0, expected_obs=13, cadence_s=25.0)
    for i in range(14):
        store.record_observation(conn, dict(base, capture_id="solana:A:o", mint="A", age_s=25.0 * i,
                                            observed_at="2026-10-02T09:%02d:00+00:00" % i))
    store.record_observation(conn, dict(base, capture_id="solana:B:o", mint="B", age_s=0.0,
                                        observed_at="2026-10-02T09:00:01+00:00"))
    assert R.reap_captures(conn, now_iso="2026-10-02T10:00:00+00:00", horizon_s=330.0, cadence_s=25.0) == 2
    assert {r[0]: r[1] for r in conn.execute("SELECT capture_id, outcome FROM memecoin_refusal_captures "
                                             "WHERE observed_at IS NULL")} == {"solana:A:o": "complete",
                                                               "solana:B:o": "truncated_worker"}
    assert R.reap_captures(conn, now_iso="2026-10-02T11:00:00+00:00", horizon_s=330.0, cadence_s=25.0) == 0


def _with_gift(tx, owner, mint="OtherMint111111111111111111111111111111111111"):
    tx = json.loads(json.dumps(tx))
    used = {b["accountIndex"] for side in ("preTokenBalances", "postTokenBalances") for b in tx["meta"][side]}
    keys = tx["transaction"]["message"]["accountKeys"] + [
        k for side in ("writable", "readonly") for k in (tx["meta"].get("loadedAddresses") or {}).get(side, [])]
    idx = max(i for i in range(len(keys)) if i not in used)          # a real account, no balance of its own
    tx["meta"]["postTokenBalances"].append({"accountIndex": idx, "mint": mint, "owner": owner,
                                            "uiTokenAmount": {"amount": "1", "decimals": 6}})
    return tx, mint


# A SOL-FUNDED PumpSwap buy_exact_quote_in_v2 (tests/fixtures/memecoin/pumpswap_v2_receipts_20261008.json,
# "v2_second"): its event names the coin and prices it from the event's net SOL. The USDC-funded fixture
# above cannot carry W4 any more: with a second acquired mint its USDC route does not bind, so the
# valuation names no coin at all and both rows are honestly unknown (see the test after this one).
SOLF = json.loads((FX / "memecoin" / "pumpswap_v2_receipts_20261008.json").read_text())["v2_second"]
SOLF_OWNER = SOLF["owner"]
SOLF_MINT = "7sgVRpjXhLzUpkah9gAHeb6NLZLTAUAetFSYZUcCpump"
SOLF_ACCOUNT = "H2vtGkSN7VR5tTgNEjASM5TjoioVhas4ePRYVHC8CwFa"


def test_a_gift_in_his_signed_swap_does_not_borrow_the_swap_size_in_either_order(tmp_path):
    # W4: a real SOL-funded PumpSwap buy plus one atom of another coin into the same wallet. Only the
    # bought coin is a valued buy; both rows are kept whichever notification is resolved first.
    slot, sig = SOLF["tx"]["slot"], SOLF["tx"]["transaction"]["signatures"][0]
    for order in ((SOLF_ACCOUNT, "GiftAcct"), ("GiftAcct", SOLF_ACCOUNT)):
        tx, gift = _with_gift(SOLF["tx"], SOLF_OWNER)
        key = Key({SOLF_ACCOUNT: [{"signature": sig, "slot": slot, "err": None}],
                   "GiftAcct": [{"signature": sig, "slot": slot, "err": None}]}, {sig: tx})
        sub = tmp_path / ("o%d" % len(order[0]))
        sub.mkdir()
        rec, db, started = _recorder(sub, key, min_usd=1.0, owner=SOLF_OWNER,
                                     clock=Clock(SOLF["tx"]["blockTime"] + 5.0))
        mints = {SOLF_ACCOUNT: SOLF_MINT, "GiftAcct": gift}
        for acct in order:                                # same slot: resolved in arrival order
            rec.on_frame(frame(acct, mints[acct], 1, slot, owner=SOLF_OWNER))
        rec.resolve()
        rows = dict((m, (b, u)) for m, b, u in db.execute("SELECT mint, basis, unit FROM buys"))
        assert rows == {SOLF_MINT: ("pump_event_net_sol", 1), gift: ("valued_other_mint", 0)}, order
        assert [s["mint"] for s in started] == [SOLF_MINT]


def test_a_gift_in_a_usdc_funded_swap_leaves_both_coins_unvalued(tmp_path):
    # Since c9c57f2f8 a USDC-funded buy is valued only when its route payment binds to one acquired coin.
    # A second acquired mint breaks that binding: neither coin borrows a size, neither starts a capture.
    tx, gift = _with_gift(PS["tx"], OURS)
    sig = PS["signature"]
    key = Key({PS_ACCOUNT: [{"signature": sig, "slot": PS_SLOT, "err": None}],
               "GiftAcct": [{"signature": sig, "slot": PS_SLOT, "err": None}]}, {sig: tx})
    rec, db, started = _recorder(tmp_path, key, min_usd=1.0)
    for acct, mint in ((PS_ACCOUNT, PS["mint"]), ("GiftAcct", gift)):
        rec.on_frame(frame(acct, mint, 1, PS_SLOT))
    rec.resolve()
    rows = dict((m, (b, s, u)) for m, b, s, u in db.execute("SELECT mint, basis, size_usd, unit FROM buys"))
    assert rows == {PS["mint"]: ("ambiguous_multi_mint", None, 0), gift: ("ambiguous_multi_mint", None, 0)}
    assert started == []


def test_a_buy_without_a_chain_clock_is_kept_but_never_a_unit(tmp_path):
    # W5: blockTime missing used to become our resolution time, a fresh eligible unit.
    tx = json.loads(json.dumps(PS["tx"]))
    tx["blockTime"] = None
    sig = PS["signature"]
    rec, db, started = _recorder(tmp_path, Key({PS_ACCOUNT: [{"signature": sig, "slot": PS_SLOT, "err": None}]},
                                               {sig: tx}), min_usd=50.0)
    rec.on_frame(frame(PS_ACCOUNT, PS["mint"], 1, PS_SLOT))
    rec.resolve()
    assert db.execute("SELECT block_time, unit FROM buys").fetchall() == [(None, 0)] and started == []
    assert db.execute("SELECT why FROM dropped").fetchall() == [("no_source_clock",)]


def test_an_old_signature_keyed_table_is_migrated_once_with_its_rows(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""CREATE TABLE buys(signature TEXT PRIMARY KEY, mint TEXT NOT NULL, account TEXT, slot INTEGER,
      block_time REAL, notified_slot INTEGER, resolved_at TEXT, token_delta TEXT, size_usd REAL, basis TEXT,
      unit INTEGER NOT NULL DEFAULT 0); CREATE INDEX buys_mint ON buys(mint, unit);
      INSERT INTO buys (signature, mint, unit) VALUES ('s1', 'M1', 1);""")
    db.commit()
    kw = dict(rpc=Key(), sol_usd_fn=lambda: None, cap=R.CallCap(1, clock=Clock()), start_fn=lambda **k: None)
    R.Recorder(db, OURS, now_fn=Clock(), **kw)
    R.Recorder(db, OURS, now_fn=Clock(), **kw)
    assert [c[1] for c in db.execute("PRAGMA table_info(buys)") if c[5]] == ["signature", "mint"]
    assert db.execute("SELECT signature, mint, unit FROM buys").fetchall() == [("s1", "M1", 1)]
    assert db.execute("SELECT name FROM sqlite_master WHERE name IN ('buys_mint', 'buys_v1')").fetchall() == [
        ("buys_mint",)]
