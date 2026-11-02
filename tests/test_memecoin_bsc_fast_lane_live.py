"""BSC 56 fast lane, step 3: live (MEMECOIN_BSC_FAST_LANE=live; Alpha 2026-10-02: build it now,
RUN it once the operator's own 56 key is set).

Before the reservation everything is in memory: the pilot, the BNB price, the wallet snapshot and
float, the safety verdict, the coin's chain facts (decimals, totalSupply) and market stats, the
minimum. The safety cache, the facts and the snapshot read ONLY the lane's own host
(MEMECOIN_BSC_FAST_LANE_RPC_URL), refused at start when absent or shared with another role. Then
the runner's own funded hook, and the 4663 lane's executor with the 56 pieces: the native-BNB
PancakeSwap V2 buy, the shared nonce floor, the write-ahead, one parallel send of the identical
bytes, the receipt booking from the coin's pair, the block-pinned delta fallback, the recovery."""
import inspect
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import rlp
from eth_account import Account
from eth_utils import keccak

sys.path.insert(0, str(Path(__file__).parent))

from sniper.persistence import db_memecoin as db  # noqa: E402
from sniper.services import memecoin_bsc_fast_lane as bsc  # noqa: E402
from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC  # noqa: E402
from test_memecoin_bsc_fast_lane import QUOTE_X, ROW, WBNB, _decide, _synthetic  # noqa: E402
from test_memecoin_bsc_fast_lane_shadow import _ds_pair  # noqa: E402

LANE_URL = "https://bsc-lane.example-provider.test/key-in-path"
# The synthetic pair's post price is 1e-6 BNB a coin: 33.3M coins at $600 BNB is a ~$20k cap.
SUPPLY = 33_333_333 * 10 ** 18


def _paid_row(row, receipt):
    """A coherent wallet-paid receipt supplies proof for these funded-path fixtures."""
    from sniper.services.memecoin_evm_onchain_watch import direct_buy_from_receipt
    router = "0x" + "0d" * 20  # _synthetic's forwarding router pays the wallet.
    tx = dict(hash=row["tx_hash"], blockHash="0x" + "81" * 32,
              blockNumber=row["log"]["blockNumber"], chainId="0x38", to=router,
              value=hex(10 ** 18), **{"from": row["wallet"]})
    rc = dict(receipt, transactionHash=tx["hash"], blockHash=tx["blockHash"],
              blockNumber=tx["blockNumber"], to=router, **{"from": row["wallet"]})
    buy = direct_buy_from_receipt(rc, tx, row["wallet"], weth=WBNB, quote_usd=600.0,
                                  decimals_fn=lambda _: 18, chain="56")
    assert buy and buy["source_initiation"]
    return dict(row, buy=dict(row["buy"], source_initiation=dict(
        buy["source_initiation"], source_user_id=row["source_user_id"])))


def _payload(**over):
    receipt, mint, post = _synthetic()
    out = _decide(receipt, mint, post)
    assert out["reason"] == "would_send", out
    row = dict(ROW, tx_hash="0x%064x" % int(time.time() * 1e6), buy=dict(ROW["buy"], mint=mint),
               source_user_id="uid", now=__import__("sniper.util.clock", fromlist=["x"]).real_utc_now_iso())
    now = time.time()
    row = _paid_row(row, receipt)
    p = bsc.lane_payload(row, out, decimals=18, bnb_usd=600.0, bnb_usd_at=now, sent_epoch=now)
    p.update(over)
    return p


# ---- the lane's own host: refused at start unless set and unshared ----

@pytest.mark.parametrize("url, why", [
    ("", "no MEMECOIN_BSC_FAST_LANE_RPC_URL"),
    ("http://bsc-lane.example-provider.test/k", "not an https URL"),
    ("https://bsc-rpc.publicnode.com", "publicnode.com"),            # a funded submission member
    ("https://lb.drpc.org/ogrpc?network=bsc&dkey=K2", "drpc.org (keyed)"),  # the tape's seed account
    ("https://other.shared-provider.test/k2", "shared-provider.test"),  # another role's variable
])
def test_the_lane_host_must_be_set_https_and_used_by_no_other_role(url, why):
    env = {"MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56": "https://x.shared-provider.test/k1",
           "MEMECOIN_RESEARCH_DRPC_RPC_URL_56": "https://lb.drpc.org/ogrpc?network=bsc&dkey=K1"}
    assert why in bsc.lane_host_refusal(url, env)
    assert bsc.lane_host_refusal(LANE_URL, env) is None


def test_live_without_its_host_refuses_at_start_with_one_line(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMECOIN_BSC_FAST_LANE_RPC_URL", raising=False)
    lines = []
    assert bsc.build_bsc_lane({"56": {"address": "0x" + "6a" * 20}}, log=lines.append, mode="live",
                              root=tmp_path) is None
    assert len(lines) == 1 and "live REFUSED at start" in lines[0], lines
    assert "no MEMECOIN_BSC_FAST_LANE_RPC_URL" in lines[0]


# ---- end to end on the production 56 context ----

class Net:
    def __init__(self):
        self.sent, self.calls = False, []


@pytest.fixture
def live56(monkeypatch, tmp_path):
    from sniper.execution import evm_wallet
    from sniper.execution import solana_quote as sq
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg
    from test_memecoin_evm_context_keys import _production_context
    monkeypatch.setattr(cfg, "memecoin_evm_approval_pipeline", lambda chain=None: True)
    ctx = _production_context(monkeypatch, tmp_path, "56")

    def today_executor(*a, **k):
        raise AssertionError("the hook ran today's 56 executor for a lane signal")
    ctx["execute_fn"] = today_executor
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56"})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_QUIET_HOURS_UTC", "", raising=False)
    monkeypatch.setenv("MEMECOIN_KILL_SWITCH_FILE", str(tmp_path / "kill"))
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE_RPC_URL", LANE_URL)
    monkeypatch.setattr(runner, "_gate_balance_cache", {})
    monkeypatch.setattr(sq, "_ds_cooldown_until", [0.0])
    net = Net()

    def guard(name):
        def blocked(*a, **k):
            net.calls.append((name, net.sent))
            raise OSError("network blocked in this test: %s" % name)
        return blocked
    monkeypatch.setattr(socket.socket, "connect", guard("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", guard("socket.create_connection"))
    monkeypatch.setattr(urllib.request, "urlopen", guard("urlopen"))
    monkeypatch.setattr(evm_wallet, "_http", guard("evm_wallet._http"))

    class Sender:
        def __init__(self):
            self.raws = []

        def send(self, raw, max_block=None):
            net.sent = True
            self.raws.append(raw)
            return ("rejected", "invalid sender")
    sender = Sender()
    path = str(tmp_path / "ledger.db")
    ledger = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    db.add_wallet(ledger, "uid", "w")
    db.set_wallet_real_approved(ledger, "uid", True, chains=["56"], force=True)

    def ro():
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, check_same_thread=False,
                            isolation_level=None)
        c.row_factory = sqlite3.Row
        return c
    stats_fdv = {"fdv": 20_000.0}

    def dex_base(method, url, headers=None, body=None):
        mint = url.rstrip("/").rsplit("/", 1)[-1]
        return 200, {"pairs": [_ds_pair(mint, fdv=stats_fdv["fdv"])]}, None
    lines = []
    lane = bsc.build_bsc_lane({"56": ctx}, log=lines.append, mode="live", root=tmp_path,
                              ro_connect=ro, dex_base=dex_base, connect=lambda: ledger,
                              sender=sender)
    assert lane is not None and lane["live"] is not None, lines
    Path(lane["dispatch"].beat).parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    Path(lane["dispatch"].beat).write_text(json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}))
    live = lane["live"]
    mint = _payload()["mint"]

    def prime(supply=SUPPLY, fdv=20_000.0):
        stats_fdv["fdv"] = fdv
        live.cache._verdicts[mint] = (True, None, time.time(), 0.0)
        live.cache._facts.pop(mint, None)
        if supply:
            live.cache._facts[mint] = (18, supply)
        live.cache._stats[mint] = (lane["dex"].stats(mint, network_id="56"), time.time())
        live.snapshot._last = dict(native_wei=10 ** 18, weth_wei=10 ** 18, nonce=42,
                                   gas_price_wei=10 ** 8, read_at=time.time() - 1)
    prime()
    return lane, ledger, net, sender, prime


def _signal(lane):
    """The watcher's own signal function, delivering straight to the runner's dispatch."""
    receipt, mint, post = _synthetic()
    got = []
    signal = bsc.make_watcher_signal(
        "live", "\0unused",
        send_fn=lambda a, p: got.append(lane["dispatch"].on_datagram(p, os.getpid(), os.getuid(),
                                                                     time.time())) or True)
    from sniper.util.clock import real_utc_now_iso
    row = dict(ROW, tx_hash="0x%064x" % int(time.time() * 1e6), buy=dict(ROW["buy"], mint=mint),
               source_user_id="uid", now=real_utc_now_iso())
    row = _paid_row(row, receipt)
    out = signal(row, receipt, post, decimals=18, bnb_usd=600.0, bnb_usd_age_s=1.0)
    assert out["reason"] == "would_send" and out["sent"], out
    return got[0]


def _attempts(ledger):
    return [dict(r) for r in ledger.execute(
        "SELECT signal_source, terminal_status, terminal_reason FROM memecoin_entry_attempts "
        "ORDER BY rowid")]


def test_nothing_leaves_the_host_before_the_send_and_the_bytes_are_the_native_bnb_v2_buy(live56):
    lane, ledger, net, sender, _prime = live56
    rec = _signal(lane)
    assert sender.raws, (rec, _attempts(ledger))
    assert [c for c in net.calls if not c[1]] == [], net.calls   # no network before the send
    (pending,) = ledger.execute("SELECT * FROM memecoin_pending_entries").fetchall()
    assert pending["signal_source"] == "bsc_fast_lane" and pending["network_id"] == "56"
    assert pending["status"] == "failed"                         # a stateless refusal frees it
    (attempt,) = _attempts(ledger)
    assert attempt["signal_source"] == "bsc_fast_lane" and attempt["terminal_status"] == "aborted"
    assert rec["reason"].startswith("rejected:")
    fields = rlp.decode(bytes.fromhex(sender.raws[0][2:]))       # legacy: nonce..data, v, r, s
    v = int.from_bytes(fields[6], "big")
    assert (v - 35) // 2 == 56                                   # EIP-155 chain id 56
    assert "0x" + fields[3].hex() == bsc.PCS_V2_ROUTER
    assert fields[5][:4].hex() == bsc.SWAP_ETH_FOR_TOKENS_FOT
    assert int.from_bytes(fields[0], "big") == 42                # the snapshot's pending nonce
    assert int.from_bytes(fields[4], "big") == rec["clip_wei"] > 0
    lane_db = sqlite3.connect(lane["db_path"])
    assert lane_db.execute("SELECT tx_hash, mode FROM bsc_lane_events").fetchall() == [
        (rec["source_tx"], "live")]                              # keyed on the SOURCE buy
    assert lane_db.execute("SELECT source_tx, our_tx, send_reply FROM bsc_lane_live").fetchall() == [
        (rec["source_tx"], "0x" + keccak(hexstr=sender.raws[0]).hex(), "rejected")]


def test_small_native_bankroll_reaches_shared_funded_sizing(live56, monkeypatch):
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    # Native can afford a ~$39 approved clip plus gas, but not the $50 pilot cap.
    live.snapshot._last.update(native_wei=int(.05457 * 10 ** 18),
                               weth_wei=int(.04788 * 10 ** 18))
    reached = []
    from types import SimpleNamespace
    def factory(contexts, stats):
        def attempt(conn, ev):
            reached.append((ev, contexts))
            return {"status": "not_approved"}
        return SimpleNamespace(attempt=attempt)
    live.hook_factory = factory
    rec = live.handle(_payload(bnb_usd=780.0), recv_epoch=time.time())
    assert reached, rec
    assert not sender.raws
    # Quote bookkeeping stays conservative at the pilot cap; final sizing owns the smaller clip.
    from sniper.services.memecoin_rh_fast_lane import clip_wei_for
    assert rec["clip_wei"] == clip_wei_for(50.0, 780.0)


def test_unknown_source_refuses_before_lane_inline_or_background_provider_work(live56, monkeypatch):
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    touched = []
    monkeypatch.setattr(live.cache, "note_candidates", lambda *a, **k: touched.append("background"))
    live.cache._verdicts.clear()
    live.inline_safety_fn = lambda *a, **k: touched.append("inline") or (True, None)
    rec = live.handle(_payload(source_initiation=None), recv_epoch=time.time())
    assert rec["reason"] == "source_initiation_unknown:missing_receipt_proof", rec
    assert touched == [] and not sender.raws
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_disabled_minimum_floor_still_reaches_funded_sizing(live56, monkeypatch):
    from sniper.settings import memecoin as cfg
    from types import SimpleNamespace
    lane, ledger, net, sender, _prime = live56
    reached = []
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 0.0)
    lane["live"].hook_factory = lambda *a: SimpleNamespace(
        attempt=lambda *a: reached.append(a[1]) or {"status": "not_approved"})
    rec = lane["live"].handle(_payload(), recv_epoch=time.time())
    assert reached and not sender.raws, rec


def test_live_mcap_is_chain_supply_never_dexscreener_supply(live56):
    # Alpha: live supply is the coin's chain totalSupply, read once per mint off the fill path on
    # the lane's own host. DexScreener's fdv here would put the cap at ~$10^13 (out of every band);
    # the lane still reaches its send because the hook sees chain supply x the exact price.
    lane, ledger, net, sender, prime = live56
    prime(fdv=10 ** 15)
    rec = _signal(lane)
    assert sender.raws and rec["reason"].startswith("rejected:"), rec
    p = _payload()
    assert bsc.live_market_cap(p, decimals=18, supply=SUPPLY) == pytest.approx(20_000.0, rel=1e-6)
    src = inspect.getsource(bsc.BscLive)
    assert "shadow_market_cap" not in src and "fdv" not in src


def test_a_coin_without_its_chain_supply_falls_back_before_any_funded_row(live56):
    lane, ledger, net, sender, prime = live56
    prime(supply=None)
    rec = _signal(lane)
    assert rec["reason"] == "no_supply" and not sender.raws
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


@pytest.mark.parametrize("change, reason", [
    (lambda live, mint: (live.cache._verdicts.pop(mint), setattr(live, "inline_safety_fn", None)),
     "no_safety_verdict"),
    (lambda live, mint: live.cache._stats.pop(mint), "no_market_stats"),
    (lambda live, mint: setattr(live.snapshot, "_last", None), "no_snapshot"),
    (lambda live, mint: live.snapshot._last.update(native_wei=10 ** 15), "float_short"),
    (lambda live, mint: setattr(live, "fills", 20), "pilot_complete"),
    (lambda live, mint: live.send_times.extend([time.time()] * 6), "pilot_rate_cap"),
])
def test_in_memory_fallbacks_never_reach_the_hook(live56, change, reason):
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    change(live, _payload()["mint"])
    assert _signal(lane)["reason"] == reason and not sender.raws
    assert ledger.execute("SELECT count(*) FROM memecoin_entry_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("over, facts, reason", [
    ({"cap_num": 10 ** 60, "cap_den": 1, "r_coin": 1}, None, "build_refused"),  # no bound at all
    ({"bnb_usd_at": 1.0}, None, "bnb_usd_stale"),
    ({"bnb_usd": 0.0}, None, "no_bnb_usd"),
    ({}, (9, SUPPLY), "decimals_disagree"),                          # the watcher's vs the chain's
])
def test_payload_fallbacks_never_reach_the_hook(live56, over, facts, reason):
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    p = _payload(**over)
    if facts:
        live.cache._facts[p["mint"]] = facts
    assert live.handle(p, recv_epoch=time.time())["reason"] == reason and not sender.raws
    assert ledger.execute("SELECT count(*) FROM memecoin_entry_attempts").fetchone()[0] == 0


# ---- Alpha 2026-10-02: a cache miss is checked inline on the lane's own host ----

def _miss(live, mint, *, stale=False):
    if stale:
        live.cache._verdicts[mint] = (True, None, time.time() - 3600, 0.0)
    else:
        live.cache._verdicts.pop(mint)
    live.cache._facts.pop(mint, None)
    live.cache._stats.pop(mint, None)


def test_cold_over_ceiling_coin_abandons_safety_without_reserving(live56, monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    lane, ledger, _net, sender, _prime = live56
    live = lane["live"]
    mint = _payload()["mint"]
    _miss(live, mint)
    checked = []
    live.inline_facts_fn = lambda _mint: (18, SUPPLY * 100)
    live.stats_fn = lambda m: lane["dex"].stats(m, network_id="56")
    live.inline_safety_fn = lambda m, usd=None: checked.append(m) or (True, None)

    rec = _signal(lane)

    assert rec["reason"].startswith("hook:skipped_mcap:"), rec
    live._safety_future.result(timeout=1)
    assert checked == [mint] and sender.raws == [] and rec["parallel_safety"] == "abandoned"
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    assert ledger.execute("SELECT count(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_a_cold_coin_the_hook_refuses_before_safety_keeps_its_input_legs(live56, monkeypatch):
    """The organic 56 row at 2026-10-14 11:56:11Z (inline path, re-entry cooldown) recorded no legs: they
    were stamped only in the safety step, which a hook refusal never reaches."""
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    lane, ledger, _net, sender, _prime = live56
    live = lane["live"]
    mint = _payload()["mint"]
    _miss(live, mint)
    live.inline_facts_fn = lambda _mint: (18, SUPPLY * 100)
    live.stats_fn = lambda m: lane["dex"].stats(m, network_id="56")
    live.inline_safety_fn = lambda m, usd=None: (True, None)

    rec = _signal(lane)

    assert rec["reason"].startswith("hook:skipped_mcap:"), rec
    live._safety_future.result(timeout=1)
    evidence = rec["execution_evidence"]
    assert 0 <= evidence["inline_facts_ms"] <= evidence["inline_inputs_path_ms"]
    assert 0 <= evidence["inline_stats_ms"] <= evidence["inline_inputs_path_ms"]


def test_cold_inputs_overlap_and_safety_owns_a_reservation(live56):
    import threading

    lane, ledger, _net, sender, _prime = live56
    live = lane["live"]
    mint = _payload()["mint"]
    _miss(live, mint)
    market_started = threading.Event()
    checks = []

    def facts(_mint):
        assert market_started.wait(0.5), "market and facts reads were serialized"
        return 18, SUPPLY

    def stats(m):
        market_started.set()
        return lane["dex"].stats(m, network_id="56")

    def verdict(m, label):
        pending = ledger.execute("SELECT status,tx_sig FROM memecoin_pending_entries").fetchone()
        assert pending is not None and pending["status"] == "reserved" and pending["tx_sig"] is None
        checks.append(m)
        return answer(m, label)

    answer = live.cache.cached_answer
    live.cache.cached_answer = verdict
    live.inline_facts_fn, live.stats_fn = facts, stats
    live.inline_safety_fn = lambda m, usd=None: (True, None)
    rec = _signal(lane)

    assert sender.raws and rec["reason"].startswith("rejected:"), rec
    assert checks == [mint]
    # Cut 4: each leg of the cold inputs is timed beside their path (the post-deploy read).
    evidence = rec["execution_evidence"]
    assert 0 <= evidence["inline_facts_ms"] <= evidence["inline_inputs_path_ms"]
    assert 0 <= evidence["inline_stats_ms"] <= evidence["inline_inputs_path_ms"]


@pytest.mark.parametrize("stale", [False, True], ids=["absent", "stale"])
def test_a_cache_miss_is_checked_inline_then_sent_and_cached(live56, stale):
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    mint = _payload()["mint"]
    _miss(live, mint, stale=stale)
    asked = []
    live.inline_safety_fn = lambda m, usd=None: asked.append(("safety", m, usd)) or (True, None)
    live.inline_facts_fn = lambda m: asked.append(("facts", m)) or (18, SUPPLY)
    live.stats_fn = lambda m: asked.append(("stats", m)) or lane["dex"].stats(m, network_id="56")
    rec = _signal(lane)
    assert sender.raws and rec["reason"].startswith("rejected:"), rec
    assert rec["safety_path"] == "inline" and rec["safety_ms"] >= 0
    assert sorted(a[0] for a in asked) == ["facts", "safety", "stats"]
    assert ("safety", mint, 50.0) in asked
    assert live.cache.reason(mint, now=time.time()) is None and live.cache.facts(mint) == (18, SUPPLY)
    (path, ms), = sqlite3.connect(lane["db_path"]).execute(
        "SELECT safety_path, safety_ms FROM bsc_lane_live").fetchall()
    assert path == "inline" and ms is not None


@pytest.mark.parametrize("safety, facts, reason", [
    (lambda m, usd=None: (False, "honeypot"), None, "inline_unsafe:honeypot"),
    (lambda m, usd=None: 1 / 0, None, "inline_safety_error:ZeroDivisionError"),
    (lambda m, usd=None: (True, None), lambda m: 1 / 0, "inline_facts_error:ZeroDivisionError"),
])
def test_an_inline_refusal_or_failure_falls_back_and_is_never_cached(live56, safety, facts, reason):
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    mint = _payload()["mint"]
    _miss(live, mint)
    live.inline_safety_fn = safety
    live.inline_facts_fn = facts or (lambda _mint: (18, SUPPLY))
    live.stats_fn = lambda m: lane["dex"].stats(m, network_id="56")
    assert _signal(lane)["reason"] == reason and not sender.raws
    assert live.cache.reason(mint, now=time.time()) == "no_safety_verdict"
    pending = ledger.execute("SELECT status,tx_sig FROM memecoin_pending_entries").fetchall()
    if facts is None:
        assert len(pending) == 1 and pending[0]["status"] == "failed" and pending[0]["tx_sig"] is None
    else:
        assert pending == []


def test_the_inline_check_runs_inside_the_signals_deadline(live56):
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    mint = _payload()["mint"]
    _miss(live, mint)
    seen = []
    live.inline_facts_fn = lambda _mint: (18, SUPPLY)
    live.stats_fn = lambda m: lane["dex"].stats(m, network_id="56")
    live.inline_safety_fn = lambda m, usd=None: seen.append(bsc._INLINE.deadline - time.monotonic()) or (
        False, "x")
    _signal(lane)
    assert 0 < seen[0] <= live.inline_deadline_s and bsc._INLINE.deadline is None


def test_the_production_inline_check_reads_nothing_past_its_deadline(live56):
    lane, ledger, net, sender, _prime = live56
    asked = []

    def base(method, url, headers=None, body=None, timeout=10):
        asked.append(url)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": "0x"}, {}
    ctx = lane["live"].ctx
    live = bsc.build_live({"56": ctx}, ctx, log=lambda *_: None, lock_key="k",
                          db_path=lane["db_path"], url=LANE_URL, connect=lambda: ledger,
                          sender=sender, http_base=base)
    rec = {}
    from sniper.settings import memecoin as cfg
    cfg_deadline = cfg.MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S
    try:
        cfg.MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S = 0.0
        live = bsc.build_live({"56": ctx}, ctx, log=lambda *_: None, lock_key="k",
                              db_path=lane["db_path"], url=LANE_URL, connect=lambda: ledger,
                              sender=sender, http_base=base)
    finally:
        cfg.MEMECOIN_BSC_FAST_INLINE_SAFETY_DEADLINE_S = cfg_deadline
    live.inline_deadline_s = 0.0
    assert live._inline(_payload()["mint"], rec) is not None and asked == [], (rec, asked)


def test_a_cache_hit_is_recorded_as_one(live56):
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    live.inline_safety_fn = lambda m, usd=None: pytest.fail("a cache hit ran the inline check")
    assert _signal(lane)["safety_path"] == "cache"


def test_the_inline_seam_keeps_the_deadline_the_host_and_the_pause():
    from sniper.services import memecoin_rh_fast_lane as rh
    calls = []

    def base(method, url, headers=None, body=None, timeout=10):
        calls.append(timeout)
        return (429 if len(calls) == 2 else 200), {"result": "0x1"}, {}
    paced = rh.PacedHttp(base, min_gap_s=0.0)
    http = bsc.lane_inline_http(LANE_URL, paced, base=base)
    with pytest.raises(RuntimeError):
        http("POST", "https://rpc-56", body="{}")
    bsc._INLINE.deadline = time.monotonic() + 0.5
    try:
        http("POST", LANE_URL, body="{}")
        assert 0 < calls[0] <= 0.5                            # never longer than the deadline left
        bsc._INLINE.deadline = time.monotonic() - 0.01
        from sniper.execution.evm_token_safety import EvmSafetyDeadlineExceeded
        with pytest.raises(EvmSafetyDeadlineExceeded):
            http("POST", LANE_URL, body="{}")
    finally:
        bsc._INLINE.deadline = None
    http("POST", LANE_URL, body="{}")                         # answers 429: the host's budget pauses
    assert paced.paused()
    with pytest.raises(rh.ResearchPaused):
        http("POST", LANE_URL, body="{}")
    assert len(calls) == 2                                    # nothing sent while paused
    with pytest.raises(rh.ResearchPaused):
        paced("POST", LANE_URL, body="{}")                   # the background reads pause too


def test_a_signal_queues_its_coin_for_a_verdict_and_records_its_quote(live56):
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    mint = _payload()["mint"]
    live.cache._verdicts.pop(mint)
    live.cache._priority.clear()
    live.inline_safety_fn = lambda m, usd=None: (False, "x")
    _signal(lane)
    assert live.cache.next_due(time.time()) is None           # the warm worker yields 1 s after an entry
    live.entry_ended_at -= bsc.WARM_ENTRY_COOLDOWN_S
    assert live.cache.next_due(time.time()) == mint and live.quotes[mint] == WBNB
    # ...and its decimals, which size the safety's sell probe before any host read (cut 3b)
    assert ("signal", _payload()["decimals"]) in live.cache.decimals_hint(mint)


# ---- the lane's own host: every pre-signal read goes there and nowhere else ----

def test_safety_facts_and_snapshot_read_only_the_lane_host(live56):
    lane, ledger, net, sender, _prime = live56
    from sniper.services import memecoin_rh_fast_lane as rh
    from types import SimpleNamespace
    asked = []

    def base(method, url, headers=None, body=None, timeout=10):
        asked.append(url)
        req = json.loads(body)
        answers = {"eth_getBalance": "0x" + "1" * 10, "eth_getTransactionCount": "0x2a",
                   "eth_gasPrice": "0x5f5e100", "eth_chainId": "0x38"}

        def one(r):
            result = "0x" + "%064x" % 18 if r["method"] == "eth_call" else answers.get(r["method"], "0x")
            return {"jsonrpc": "2.0", "id": r.get("id", 1), "result": result}
        return 200, ([one(r) for r in req] if isinstance(req, list) else one(req)), {}   # a batch too
    ctx = lane["live"].ctx
    live = bsc.build_live({"56": ctx}, ctx, log=lambda *_: None, lock_key="k",
                          db_path=lane["db_path"], url=LANE_URL, connect=lambda: ledger,
                          sender=sender, http_base=base,
                          dex=SimpleNamespace(stats=lambda *a, **kw: None))
    live.paced.min_gap_s = 0.0
    snap = live.snapshot.poll_once()
    assert snap and snap["nonce"] == 42 and snap["gas_price_wei"] == 10 ** 8
    mint = _payload()["mint"]
    live.quotes[mint] = WBNB
    live.cache.run_one(mint, now=time.time())               # the verdict itself does not matter
    live.cache._facts.clear()
    live.cache.facts_fn(mint)
    live.inline_safety_fn(mint, usd=50.0)                    # the inline check and facts too
    live.inline_facts_fn(mint)
    assert asked and set(asked) == {LANE_URL}, set(asked)
    assert net.calls == []                                   # never today's http, never a socket
    http = bsc.lane_only_http(rh.PacedHttp(base, min_gap_s=0.0), LANE_URL)
    with pytest.raises(RuntimeError):
        http("POST", "https://rpc-56", body="{}")


# ---- the send: identical bytes to every member, one answer ----

class Member:
    def __init__(self, reply, delay=0.0):
        self.reply, self.delay, self.raws = reply, delay, []

    def send(self, raw):
        time.sleep(self.delay)
        self.raws.append(raw)
        return self.reply

    def warm(self):
        return True


def test_every_member_gets_the_identical_bytes_and_one_acceptance_returns_at_once():
    slow = Member(("ambiguous", "x"), delay=0.5)
    members = [Member(("accepted", "0x" + "1" * 64)), slow]
    t0 = time.perf_counter()
    assert bsc.BscSender(members).send("0xraw")[0] == "accepted"
    assert time.perf_counter() - t0 < 0.4                    # the slow member does not hold it
    time.sleep(0.6)
    assert [m.raws for m in members] == [["0xraw"], ["0xraw"]]


@pytest.mark.parametrize("replies, kind", [
    ([("rejected", "invalid sender"), ("rejected", "intrinsic gas too low")], "rejected"),
    ([("rejected", "insufficient funds for gas * price + value"), ("rejected", "invalid sender")],
     "ambiguous"),                                           # balance is one node's view of state
    ([("rejected", "invalid sender"), ("nonce", "nonce too low")], "ambiguous"),
    ([("rejected", "invalid sender"), ("ambiguous", "transport: timeout")], "ambiguous"),
])
def test_a_refusal_counts_only_when_stateless_and_unanimous(replies, kind):
    assert bsc.BscSender([Member(r) for r in replies]).send("0xraw")[0] == kind


def test_a_member_past_the_timeout_is_ambiguous():
    assert bsc.BscSender([Member(("rejected", "invalid sender"), delay=0.5)],
                         timeout_s=0.1).send("0xraw")[0] == "ambiguous"


def test_blockrazor_gets_the_bytes_with_the_operator_token_and_is_never_proof():
    posted = []
    send = bsc.blockrazor_bundle("tok", post=lambda url, body, headers: posted.append(
        (url, json.loads(body), headers)) or {"result": "0xbundle"})
    sender = bsc.BscSender([Member(("rejected", "invalid sender"))], bundle=send)
    raw = "0x" + bytes(Account.sign_transaction(
        {"type": 2, "chainId": 56, "nonce": 1, "maxFeePerGas": 10, "maxPriorityFeePerGas": 0,
         "gas": 21000, "to": "0x" + "22" * 20, "value": 0, "data": b""},
        b"\x03" * 32).raw_transaction).hex()
    assert sender.send(raw)[0] == "ambiguous"                # the bundle reply is not a refusal
    (url, body, headers), = posted
    assert url == bsc.BLOCKRAZOR_URL and headers == {"Authorization": "tok"}
    assert body["method"] == "eth_sendBundle" and body["params"] == [{"txs": [raw]}]


def test_the_default_members_are_puissant_and_the_chain_owners_endpoints():
    members = bsc.default_members()
    assert [m.host for m in members] == ["puissant-builder.48.club", "bsc-dataseed.bnbchain.org",
                                         "bsc-rpc.publicnode.com"]
    assert all("User-Agent" in m.headers for m in members[1:])


# ---- the executor's 56 pieces: booking from the coin's pair, the delta fallback, the gas ----

ACCT = Account.from_key(b"\x06" * 32)
BLOCK = "0x" + "b" * 64


def _transfer(token, sender, recipient, amount, index, tx_hash):
    return {"address": token, "topics": [TRANSFER_TOPIC, "0x" + "0" * 24 + sender[2:],
                                         "0x" + "0" * 24 + recipient[2:]],
            "data": hex(amount), "transactionHash": tx_hash, "blockHash": BLOCK,
            "logIndex": hex(index)}


class Chain:
    """Receipt, transaction and block answers for whatever raw the lane sent."""
    def __init__(self, payload, status="0x1", got=5 * 10 ** 18, source=None):
        self.raw, self.status, self.got = None, status, got
        self.source = source or payload["pool"]
        self.mint = payload["mint"]

    def read(self, method, params):
        if self.raw is None:
            return None
        h = "0x" + keccak(hexstr=self.raw).hex()
        if params[0] not in (h, "0x3f2"):
            return None
        owner = ACCT.address.lower()
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": h, "blockHash": BLOCK, "blockNumber": "0x3f2",
                    "status": self.status, "from": owner,
                    "logs": [_transfer(self.mint, self.source, owner, self.got, 1, h)]}
        if method == "eth_getTransactionByHash":
            fields = rlp.decode(bytes.fromhex(self.raw[2:]))
            return {"hash": h, "from": owner, "blockHash": BLOCK, "blockNumber": "0x3f2",
                    "to": bsc.PCS_V2_ROUTER, "value": hex(int.from_bytes(fields[4], "big")),
                    "input": "0x" + fields[5].hex()}
        if method == "eth_getBlockByNumber":
            return {"hash": BLOCK, "number": "0x3f2", "timestamp": hex(1790900000)}
        return None


def _ledger():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(c)
    return c


class _Cache:
    def facts(self, mint):
        return (18, SUPPLY)


def _live(conn, chain, tmp_path, **kw):
    from sniper.execution.evm_real_seams import sign_tx

    class Accepting:
        def send(self, raw, max_block=None):
            chain.raw = raw
            return ("accepted", "0x" + keccak(hexstr=raw).hex())
    args = dict(
        snapshot=None, cache=_Cache(), quotes={}, gas_floor_wei=50_000_000,
        ctx={"address": ACCT.address, "sign_tx_fn": lambda tx: sign_tx(tx, ACCT),
             "lane_claim_nonce_fn": lambda n: n, "lane_release_nonce_fn": lambda n: True,
             "gasprice_buffer": 1.5},
        conn=conn, sender=Accepting(), fanout_fn=lambda raw: None, rpc_read=chain.read,
        lock_key="k", lock_dir=str(tmp_path), clip_usd=50.0, low_wei=0,
        generation_fn=lambda: 900.0, host_clock=time.time, max_snapshot_age_s=30.0,
        eth_usd_max_age_s=120.0, generation_margin_s=1.0, pilot_fills=20, max_sends_per_hour=6,
        receipt_timeout_s=0.5, receipt_poll_s=0.01, lock_receipt_cap_s=0.2, sleep=lambda s: None,
        alert_fn=lambda text: None)
    args.update(kw)
    return bsc.BscLive(**args)


def _run(live, conn, payload):
    pending_id = db.reserve_pending(conn, mint=payload["mint"], source_user_id="uid",
                                    strategy="alpha_b", notional=50.0,
                                    reserved_at="2026-10-02T00:00:00+00:00", network_id="56",
                                    signal_source="bsc_fast_lane")
    rec = {"_perf0": time.perf_counter()}
    snap = dict(native_wei=10 ** 18, weth_wei=0, nonce=7, gas_price_wei=10 ** 8, read_at=time.time())
    result = live.execute_reserved(dict(payload, snapshot=snap), rec, conn, pending_id, 50.0,
                                   decimals=18)
    return result, rec, pending_id


@pytest.mark.parametrize("approved,native,stale,expected", [
    (39.0, .05457, False, "filled"), (50.0, .05457, False, "float_short"),
    (39.0, 0.0, False, "float_short"), (39.0, .05457, True, "snapshot_stale"),
])
def test_final_locked_check_uses_exact_approved_size_and_never_signs_unaffordable_clip(
        tmp_path, monkeypatch, approved, native, stale, expected):
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_rh_fast_lane import clip_wei_for
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 0.0)
    conn, p = _ledger(), _payload(bnb_usd=780.0)
    chain = Chain(p)
    live = _live(conn, chain, tmp_path)
    if expected != "filled":
        live.ctx["sign_tx_fn"] = lambda *a: pytest.fail("unaffordable/stale clip signed")
        live.ctx["lane_claim_nonce_fn"] = lambda *a: pytest.fail("unaffordable/stale clip claimed nonce")
    pending_id = db.reserve_pending(conn, mint=p["mint"], source_user_id="uid", strategy="alpha_b",
                                    notional=approved, reserved_at="2026-10-02T00:00:00+00:00",
                                    network_id="56", signal_source="bsc_fast_lane")
    snapshot = dict(native_wei=int(native * 1e18), weth_wei=0, nonce=7,
                    gas_price_wei=10 ** 8, read_at=time.time() - (120 if stale else 1))
    rec = {"_perf0": time.perf_counter()}
    result = live.execute_reserved(dict(p, snapshot=snapshot), rec, conn, pending_id, approved, decimals=18)
    assert rec["clip_wei"] == clip_wei_for(approved, 780.0)
    if expected == "filled":
        assert result["status"] == "filled" and chain.raw
        pos = conn.execute("SELECT entry_notional FROM memecoin_positions").fetchone()
        assert pos["entry_notional"] == pytest.approx(approved)
    else:
        assert result["status"] == "aborted" and result["reason"] == expected
        assert chain.raw is None
        assert db.pending_row(conn, pending_id)["status"] == "failed"


def test_a_fill_is_booked_from_the_coin_pairs_transfer_at_the_bnb_price(tmp_path):
    conn, p = _ledger(), _payload()
    chain = Chain(p)
    result, rec, pending_id = _run(_live(conn, chain, tmp_path), conn, p)
    assert rec["_outcome"] == "filled" and result["status"] == "filled", (rec, result)
    pos = conn.execute("SELECT * FROM memecoin_positions").fetchone()
    assert pos["network_id"] == "56" and db.open_qty(conn, pos["id"]) == 5.0
    assert pos["entry_notional"] == pytest.approx(rec["clip_wei"] / 1e18 * 600.0)
    assert db.pending_row(conn, pending_id)["status"] == "confirmed"


def test_a_coin_from_anywhere_but_its_pair_is_booked_from_its_own_receipt_net(tmp_path):
    # Codex EVM-03: the block delta (4) is the wallet's; this swap's own receipt says 5.
    conn, p = _ledger(), _payload()
    chain = Chain(p, source="0x" + "9" * 40)
    alerts = []
    live = _live(conn, chain, tmp_path, alert_fn=alerts.append,
                 balance_at_fn=lambda mint, block: {0x3f1: 0, 0x3f2: 4 * 10 ** 18}[block])
    result, rec, _pid = _run(live, conn, p)
    assert rec["_outcome"] == "filled_from_receipt_net" and alerts
    pos = conn.execute("SELECT * FROM memecoin_positions").fetchone()
    assert db.open_qty(conn, pos["id"]) == 5.0


def test_a_confirmed_buy_nothing_can_book_is_held_loudly(tmp_path):
    conn, p = _ledger(), _payload()
    alerts = []
    live = _live(conn, Chain(p, got=0), tmp_path, alert_fn=alerts.append,
                 balance_at_fn=lambda mint, block: {0x3f1: 0, 0x3f2: 4 * 10 ** 18}[block])
    result, rec, pending_id = _run(live, conn, p)
    assert rec["_outcome"] == "booking_held" and "reconcile" in alerts[-1]
    assert result["status"] == "unknown" and db.pending_row(conn, pending_id)["status"] == "reserved"


def test_a_zero_minimum_at_the_approved_clip_aborts_before_the_lock(tmp_path):
    conn = _ledger()
    p = _payload(cap_num=10 ** 60, cap_den=1, r_coin=1)
    chain = Chain(p)
    result, rec, pending_id = _run(_live(conn, chain, tmp_path), conn, p)
    assert rec["_outcome"] == "build_refused" and chain.raw is None
    assert db.pending_row(conn, pending_id)["status"] == "failed"


def test_the_gas_bid_never_goes_under_the_validators_floor(tmp_path):
    live = _live(_ledger(), Chain(_payload()), tmp_path)
    assert live._gas_price({"gas_price_wei": 1}) == 50_000_000
    assert live._gas_price({"gas_price_wei": 10 ** 8}) == 150_000_000


def test_live_seeds_its_pilot_from_its_own_rows_only(live56):
    lane, ledger, net, sender, _prime = live56
    _bind_rows(ledger)
    ctx = lane["live"].ctx
    live = bsc.build_live({"56": ctx}, ctx, log=lambda *_: None, lock_key="k",
                          db_path=lane["db_path"], url=LANE_URL, connect=lambda: ledger,
                          sender=sender)
    assert len(live.send_times) == 1


def _bind_rows(conn):
    for source in ("bsc_fast_lane", "rh_fast_lane"):
        pid = db.reserve_pending(conn, mint="0x" + "77" * 20, source_user_id="uid",
                                 strategy="alpha_b", notional=50.0,
                                 reserved_at="2026-10-02T00:00:00Z", network_id="56",
                                 signal_source=source)
        db.bind_entry_submission(conn, pid, tx_identity="0x%064x" % len(source),
                                 signed_payload_b64="0xr", signed_payload_sha256="a" * 64,
                                 blockhash=None, last_valid_height=None, nonce=1,
                                 backend="b", bound_at=__import__(
                                     "sniper.util.clock", fromlist=["x"]).real_utc_now_iso(),
                                 evm_quote_asset_usd=600.0)


def test_the_pilot_counts_only_this_lanes_rows():
    from sniper.services.memecoin_rh_fast_lane import _seed_pilot
    conn = _ledger()
    _bind_rows(conn)
    assert _seed_pilot(conn, signal_source="bsc_fast_lane")[1].__len__() == 1


def test_restart_hour_cap_keeps_reverted_sends_and_dedupes_provider_reoffers():
    from datetime import datetime, timezone
    from sniper.execution.submission_transport import AttemptState, SubmissionAttempt
    from sniper.services.memecoin_rh_fast_lane import _seed_pilot
    conn = _ledger()
    now = time.time()
    iso = lambda at: datetime.fromtimestamp(at, timezone.utc).isoformat()
    pid = db.reserve_pending(conn, mint="0x" + "78" * 20, source_user_id="uid",
                             strategy="alpha_b", notional=20, reserved_at=iso(now - 100),
                             network_id="56", signal_source="relay_prebuy")
    def bind(identity, at):
        assert db.bind_entry_submission(conn, pid, tx_identity=identity, signed_payload_b64="0xr",
            signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None, nonce=1,
            backend="b", bound_at=iso(at), evm_quote_asset_usd=600)
    def audit(identity, at, chain="56", action="entry", owner_kind="pending"):
        db.record_submission_attempts(conn, chain_id=chain, action=action, owner_kind=owner_kind,
            owner_id=str(pid), tx_identity=identity, signed_payload_sha256="a" * 64,
            attempts=[SubmissionAttempt("a", AttemptState.EXPLICIT_REJECTED, None, 1, "error"),
                      SubmissionAttempt("b", AttemptState.AMBIGUOUS, None, None, None)], created_at=iso(at))
    # Two providers and a later reoffer are one signed send, even after its canonical revert.
    bind("0xold", now - 100)
    audit("0xold", now - 100)
    audit("0xold", now - 50)
    assert db.reset_entry_submission_after_failed_receipt(conn, pid, "0xold")
    assert _seed_pilot(conn, "relay_prebuy", "56")[1] == [pytest.approx(now - 100, abs=1e-5, rel=0)]
    # New bytes count separately; a still-bound identity must not duplicate its audit.
    bind("0xnew", now - 20)
    audit("0xnew", now - 20)
    audit("0xexit", now - 10, action="exit")
    audit("0xforeign", now - 10, chain="4663")
    audit("0xwrongowner", now - 10, owner_kind="exit")
    assert _seed_pilot(conn, "relay_prebuy", "56")[1] == [
        pytest.approx(now - 100, abs=1e-5, rel=0), pytest.approx(now - 20, abs=1e-5, rel=0)]
    assert _seed_pilot(conn, "bsc_fast_lane", "56")[1] == []


def test_old_identity_reoffered_this_hour_does_not_refresh_restart_cap_window():
    from datetime import datetime, timezone
    from sniper.execution.submission_transport import AttemptState, SubmissionAttempt
    from sniper.services.memecoin_rh_fast_lane import _seed_pilot
    conn = _ledger()
    now = time.time()
    iso = lambda at: datetime.fromtimestamp(at, timezone.utc).isoformat()
    pid = db.reserve_pending(conn, mint="0x" + "79" * 20, source_user_id="uid",
                             strategy="alpha_b", notional=20, reserved_at=iso(now - 4000),
                             network_id="56", signal_source="relay_prebuy")
    db.bind_entry_submission(conn, pid, tx_identity="0xold", signed_payload_b64="0xr",
        signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None, nonce=1,
        backend="b", bound_at=iso(now - 10), evm_quote_asset_usd=600)
    for at in (now - 4000, now - 10):
        identity = "0xOLD" if at == now - 4000 else "0xold"
        db.record_submission_attempts(conn, chain_id="56", action="entry", owner_kind="pending",
            owner_id=str(pid), tx_identity=identity, signed_payload_sha256="a" * 64,
            attempts=[SubmissionAttempt("a", AttemptState.ACCEPTED, identity, 1, "ok")], created_at=iso(at))
    assert _seed_pilot(conn, "relay_prebuy", "56")[1] == []


@pytest.mark.parametrize("bound", ["broken", "2026-10-14T13:00:00"])
@pytest.mark.parametrize("durable", [False, True])
def test_restart_cap_unknown_send_time_fails_closed(bound, durable):
    from sniper.execution.submission_transport import AttemptState, SubmissionAttempt
    from sniper.services.memecoin_rh_fast_lane import _seed_pilot
    conn = _ledger()
    pid = db.reserve_pending(conn, mint="0x" + "80" * 20, source_user_id="uid",
                             strategy="alpha_b", notional=20, reserved_at="2026-10-14T13:00:00Z",
                             network_id="56", signal_source="relay_prebuy")
    db.bind_entry_submission(conn, pid, tx_identity="0xunknown", signed_payload_b64="0xr",
        signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None, nonce=1,
        backend="b", bound_at=bound, evm_quote_asset_usd=600)
    if durable:
        db.record_submission_attempts(conn, chain_id="56", action="entry", owner_kind="pending",
            owner_id=str(pid), tx_identity="0xunknown", signed_payload_sha256="a" * 64,
            attempts=[SubmissionAttempt("a", AttemptState.NOT_SENT, None, None, None)], created_at=bound)
    with pytest.raises(ValueError, match="send timestamp"):
        _seed_pilot(conn, "relay_prebuy", "56")


# ---- a lane buy confirmed across a restart is booked by the 56 recovery ----

def test_the_56_recovery_books_a_bound_lane_row_from_its_receipt(monkeypatch, tmp_path):
    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg
    from test_memecoin_evm_context_keys import _production_context
    monkeypatch.setattr(cfg, "memecoin_evm_approval_pipeline", lambda chain=None: True)
    ctx = _production_context(monkeypatch, tmp_path, "56")
    owner = ctx["address"].lower()
    p = _payload()
    tx_hash = "0x" + "e" * 64
    conn = _ledger()
    pending_id = db.reserve_pending(conn, mint=p["mint"], source_user_id="src", strategy="alpha_b",
                                    notional=50.0, reserved_at="2026-10-02T00:00:00Z",
                                    network_id="56", signal_source="bsc_fast_lane")
    db.bind_entry_submission(conn, pending_id, tx_identity=tx_hash, signed_payload_b64="0xraw",
                             signed_payload_sha256="a" * 64, blockhash=None, last_valid_height=None,
                             nonce=7, backend="bsc_fast_lane", bound_at="2026-10-02T00:00:01Z",
                             evm_quote_asset_usd=600.0)
    data = bsc.v2_native_buy_tx(p, 8 * 10 ** 16, 1, 1790000060, owner)["data"]
    receipt = {"transactionHash": tx_hash, "blockHash": BLOCK, "blockNumber": "0x10",
               "status": "0x1", "from": owner,
               "logs": [_transfer(p["mint"], p["pool"], owner, 5 * 10 ** 18, 1, tx_hash)]}
    tx = {"hash": tx_hash, "from": owner, "blockHash": BLOCK, "blockNumber": "0x10",
          "to": bsc.PCS_V2_ROUTER, "value": hex(8 * 10 ** 16), "input": data}
    block = {"hash": BLOCK, "number": "0x10", "timestamp": hex(1790900000)}
    answers = {"eth_getTransactionReceipt": receipt, "eth_getTransactionByHash": tx,
               "eth_getBlockByNumber": block, "eth_call": "0x" + "%064x" % 18}
    monkeypatch.setattr(seams, "_rpc", lambda url, method, params, http=None: {"result": answers[method]})
    monkeypatch.setattr(seams, "erc20_decimals", lambda mint, url, http=None: 18)
    got = ctx["recover_confirmed_entry_fn"](conn, db.pending_row(conn, pending_id),
                                            "2026-10-02T00:01:00Z")
    assert got and got["status"] == "filled"
    pos = conn.execute("SELECT * FROM memecoin_positions").fetchone()
    assert pos["network_id"] == "56" and db.open_qty(conn, pos["id"]) == 5.0
    assert bsc.coin_pool_of(tx) == p["pool"]
    hop = dict(p, quote=QUOTE_X)                                 # WBNB -> quote -> coin
    assert bsc.coin_pool_of(dict(tx, input=bsc.v2_native_buy_tx(hop, 1, 1, 1, owner)["data"])) == (
        bsc.our_pair(p["mint"], QUOTE_X))
    assert bsc.coin_pool_of(dict(tx, input="0xdeadbeef")) is None


# ---- Gamma on 616818099: the live mcap through a meme quote's hop, in every unit ----

@pytest.mark.parametrize("qdec", [18, 6])
def test_live_mcap_converts_a_meme_quote_through_its_wbnb_hop(qdec):
    # coin pair: 1e6 quote vs 1e8 coins (0.01 quote a coin); hop: 50 WBNB vs 1e5 quote (0.0005 BNB a
    # quote); BNB $600: a quote is $0.30, a coin $0.003, 1e9 coins $3M, whatever the quote's decimals.
    p = dict(r_quote=10 ** 6 * 10 ** qdec, r_coin=10 ** 8 * 10 ** 18, quote=QUOTE_X, bnb_usd=600.0,
             hop={"r_wbnb": 50 * 10 ** 18, "r_quote": 10 ** 5 * 10 ** qdec})
    assert bsc.live_market_cap(p, decimals=18, supply=10 ** 9 * 10 ** 18) == pytest.approx(3e6)
    assert bsc.live_market_cap(dict(p, hop=None), decimals=18, supply=10 ** 27) is None


# ---- Gamma on 616818099: the lane keeps 56's own BNB low-water mark, never 4663's ETH one ----

def test_the_lane_float_check_uses_56s_own_low_water_mark(live56):
    from sniper.services.memecoin_evm_gas_topup import gas_pass_bounds
    lane, ledger, net, sender, _prime = live56
    assert lane["live"].low_wei == gas_pass_bounds("56")["low_water_wei"]


@pytest.mark.parametrize("mode, refill, target, low", [
    ("live", 0, 30 + 100, 10), ("live", 50, 30 + 100, 10 + 100),
    ("live", 110, 30 + 100, 10), ("live", 120, 30 + 100, 10),
    ("live", 200, 30 + 100, 10),
    ("runner_shadow", 50, 30, 10), ("off", 50, 30, 10)])
def test_lane_float_bounds_raise_the_target_only_while_live(mode, refill, target, low):
    bounds = {"low_water_wei": 10, "target_wei": 30, "reclaim_floor_gas": 7}
    got = bsc.lane_float_bounds(bounds, mode=mode, float_wei=100, refill_below_wei=refill)
    assert (got["target_wei"], got["low_water_wei"], got["reclaim_floor_gas"]) == (target, low, 7)
    assert bsc.lane_float_bounds(bounds, mode="live", float_wei=0, refill_below_wei=50) == bounds
    assert bsc.lane_float_bounds(bounds, mode="live", float_wei=5, refill_below_wei=0) == dict(
        bounds, target_wei=35)


def test_the_inline_host_is_never_called_for_a_buy_the_absolute_band_refuses(live56, monkeypatch):
    """Alpha/Gamma 2026-10-02: on a cache miss the 56 lane runs the hook's network-free absolute
    source-buy band before its inline check, as the 4663 lane does."""
    from sniper.settings import memecoin as cfg
    lane, ledger, net, sender, _prime = live56
    live = lane["live"]
    paid = float(_payload()["paid_usd"])
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", paid * 10, raising=False)
    _miss(live, _payload()["mint"])
    live.inline_safety_fn = lambda m, usd=None: pytest.fail("inline read for a band refusal")
    rec = _signal(lane)
    assert rec["reason"].startswith("source_band:source buy $") and "under absolute floor" in rec["reason"]
    assert not sender.raws and _attempts(ledger) == []


# ---- operator 2026-10-02: the BSC lane and the 4663 inline safety host share one Chainstack account ----

BSC_CHAINSTACK = "https://bsc-mainnet.core.chainstack.com/k-bsc"
INLINE_CHAINSTACK = "https://robinhood-mainnet.core.chainstack.com/k-inline"


def test_the_4663_inline_safety_host_may_share_the_bsc_lanes_chainstack_account():
    env = {"MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL": INLINE_CHAINSTACK}
    assert bsc.lane_host_refusal(BSC_CHAINSTACK, env) is None


def test_any_other_key_on_chainstack_still_refuses_the_bsc_lane():
    for key in ("MEMECOIN_RH_FAST_SAFETY_RPC_URL", "MEMECOIN_EVM_RPC_URL_56", "SOME_OTHER_CHAINSTACK"):
        env = {"MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL": INLINE_CHAINSTACK,
               key: "https://other.core.chainstack.com/k2"}
        assert "chainstack.com" in bsc.lane_host_refusal(BSC_CHAINSTACK, env), key


def test_the_inline_host_equal_to_the_bsc_lanes_url_is_still_refused_on_the_rh_side():
    from sniper.services.memecoin_rh_fast_lane import safety_host_refusal
    env = {"MEMECOIN_BSC_FAST_LANE_RPC_URL": BSC_CHAINSTACK, "MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL": BSC_CHAINSTACK}
    why = safety_host_refusal(BSC_CHAINSTACK, env, own_name="MEMECOIN_RH_FAST_INLINE_SAFETY_RPC_URL")
    assert why and "MEMECOIN_BSC_FAST_LANE_RPC_URL" in why


@pytest.mark.parametrize('invalidated', ['age', 'generation'])
def test_inline_safety_invalidated_wallet_snapshot_never_signs(live56, invalidated):
    lane, ledger, net, sender, _prime = live56
    live = lane['live']
    mint = _payload()['mint']
    _miss(live, mint)
    clock, generation = [time.time()], [0.0]
    live.host_clock = lambda: clock[0]
    live.generation_fn = lambda: generation[0]
    live.snapshot._last['read_at'] = clock[0] - 29.9

    def safety(_mint, usd=None):
        if invalidated == 'age':
            clock[0] += 0.5
        else:
            generation[0] = clock[0] + 0.1
        return True, None

    live.inline_safety_fn = safety
    live.inline_facts_fn = lambda _mint: (18, SUPPLY)
    live.stats_fn = lambda mint: lane['dex'].stats(mint, network_id='56')
    record = _signal(lane)
    assert record['reason'] == ('snapshot_stale' if invalidated == 'age' else 'snapshot_predates_execution')
    assert not sender.raws
    pending = ledger.execute('SELECT * FROM memecoin_pending_entries').fetchone()
    assert pending['status'] == 'failed' and pending['tx_sig'] is None


def test_live_56_classifies_a_mined_revert_with_the_funded_replay_on_the_custody_host(live56, monkeypatch):
    from sniper.execution import evm_real_seams as seams
    lane, _ledger, _net, _sender, _prime = live56
    asked = []
    monkeypatch.setattr(seams, "evm_revert_reason",
                        lambda h, url, http=None: asked.append((h, url)) or ("min_out", "x"))
    assert lane["live"].revert_reason_fn("0xabc") == ("min_out", "x")
    assert asked == [("0xabc", lane["live"].ctx["rpc_url"])] and asked[0][1]

# ---- EVM-01 (Codex 2026-10-06): the independent copy-price cap on the native 56 lane ----

def test_the_copy_cap_refuses_a_buy_whose_post_state_output_is_below_its_floor(live56, monkeypatch):
    # The synthetic source paid $0.000247 a coin (wallet net) and left the pair at $0.0006: at the
    # receipt's own post state our buy already prices > 35% over the source, so nothing is signed.
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    lane, ledger, net, sender, _prime = live56
    rec = _signal(lane)
    assert sender.raws == [] and rec["reason"] == "copy_signed_floor", (rec, _attempts(ledger))
    (pending,) = ledger.execute("SELECT * FROM memecoin_pending_entries").fetchall()
    assert pending["source_price"] == pytest.approx(ROW["buy"]["source_price"])
    assert pending["status"] == "failed"


def test_the_copy_cap_floor_is_the_signed_minimum_when_it_binds(tmp_path, monkeypatch):
    from fractions import Fraction
    from sniper.execution.entry_attribution import copy_cap_min_out
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_MAX_COPY_PREMIUM_PCT", 30.0)
    conn = _ledger()
    base = _payload()
    loose = Fraction(int(base["r_quote"]), int(base["r_coin"])) * 2   # a cap that never binds
    p = _payload(cap_num=loose.numerator, cap_den=loose.denominator)
    clip = 50 * 10 ** 18 // 600
    expected = bsc.expected_out_for(clip, p)
    fill_usd = Fraction(clip, 10 ** 18) * 600 / Fraction(expected, 10 ** 18)
    src = float(fill_usd / Fraction(135, 100) * Fraction(101, 100))  # floor ~1% under expected
    floor = copy_cap_min_out(clip, 600.0, src, 18, 35.0)
    assert bsc.min_out_for(clip, p, expected_out=expected) < floor <= expected
    chain = Chain(p, got=floor)
    live = _live(conn, chain, tmp_path)
    pending_id = db.reserve_pending(conn, mint=p["mint"], source_user_id="uid", strategy="alpha_b",
                                    notional=50.0, reserved_at="2026-10-02T00:00:00+00:00",
                                    network_id="56", signal_source="bsc_fast_lane", source_price=src)
    rec = {"_perf0": time.perf_counter()}
    snap = dict(native_wei=10 ** 18, weth_wei=0, nonce=7, gas_price_wei=10 ** 8, read_at=time.time())
    result = live.execute_reserved(dict(p, snapshot=snap), rec, conn, pending_id, 50.0, decimals=18)
    assert rec["clip_wei"] == clip and rec["min_out"] == floor and result["status"] == "filled"
    fields = rlp.decode(bytes.fromhex(chain.raw[2:]))
    assert int.from_bytes(fields[5][4:36], "big") == floor          # amountOutMin
    evidence = rec['execution_evidence']
    assert evidence['signed_floors']['signed_minimum'] == str(floor)
    assert evidence['signed_floors']['binding'] == ['copy']
    assert evidence['quote_price'] is None
    assert evidence['public_transaction']['chain'] == '56'
    assert evidence['public_transaction']['calldata'] == '0x' + fields[5].hex()
    assert evidence['public_receipt']['transactionHash'] == result['tx_hash']


def test_bsc_execution_evidence_roundtrips(tmp_path):
    path = str(tmp_path / "bsc.db")
    evidence = {"signed_floors": {"cap": "123", "slippage": "234", "copy": "0"}, "quote_price": None}
    bsc.write_live(path, {"execution_evidence": evidence})
    conn = sqlite3.connect(path)
    try:
        assert json.loads(conn.execute("SELECT execution_evidence_json FROM bsc_lane_live").fetchone()[0]) == evidence
    finally:
        conn.close()


@pytest.mark.parametrize('reason, tx, pending, expected', [
    ('float_short', None, 123, 1),
    ('float_short', '0xsigned', 123, 0),
    ('float_short', None, None, 0),
    ('filled', '0xfilled', 123, 0),
    ('unknown', None, 123, 0),
])
def test_unsigned_float_short_wakes_fallback_after_persisting_only_its_state_root(
        tmp_path, monkeypatch, reason, tx, pending, expected):
    import sqlite3
    from sniper.services import memecoin_onchain_wake as wake
    data = tmp_path / 'data'
    data.mkdir()
    path = data / 'memecoin_bsc_fast_lane.db'
    heard = []

    def send(address):
        assert address == wake.wake_address(str(tmp_path))
        with sqlite3.connect(path) as conn:
            assert conn.execute('select reason from bsc_lane_live').fetchone()[0] == reason
        heard.append(address)
        return False  # A lost hint leaves the ordinary poll in place.

    monkeypatch.setattr(wake, 'send_wake', send)
    bsc.write_live(path, {'reason': reason, 'tx_hash': tx, 'pending_id': pending})
    assert len(heard) == expected


# --- cut 4b: the inline seam keeps its connections alive ------------------------------------------

class _Pooled:
    """Stands in for fast_http.PooledHttpClient: records its sizing and every request."""
    made = []

    def __init__(self, **kw):
        self.kw, self.requests, self.fail = kw, [], None
        _Pooled.made.append(self)

    def request(self, method, url, *, headers=None, body=None, timeout=None):
        self.requests.append((url, json.loads(body), timeout))
        if self.fail is not None:
            raise self.fail
        req = json.loads(body)
        rows = req if isinstance(req, list) else [req]
        out = [{"jsonrpc": "2.0", "id": r["id"], "result": "0x" + "%064x" % 18} for r in rows]
        return 200, (out if isinstance(req, list) else out[0]), None


def _pooled_live(live56, monkeypatch):
    from sniper.execution import fast_http
    from types import SimpleNamespace
    lane, ledger, _net, sender, _prime = live56
    _Pooled.made = []
    monkeypatch.setattr(fast_http, "PooledHttpClient", _Pooled)
    ctx = lane["live"].ctx
    live = bsc.build_live({"56": ctx}, ctx, log=lambda *_: None, lock_key="k",
                          db_path=lane["db_path"], url=LANE_URL, connect=lambda: ledger,
                          sender=sender, dex=SimpleNamespace(stats=lambda *a, **kw: None))
    return live, _Pooled.made


def test_the_inline_seam_reads_through_its_own_kept_alive_pool_sized_for_the_check(live56, monkeypatch):
    from sniper.execution.evm_token_safety import _SLOT_TAIL_WORKERS
    live, made = _pooled_live(live56, monkeypatch)
    assert len(made) == 1                                   # one pool, this lane's inline seam only
    pool = made[0]
    assert pool.kw["max_connections"] >= 3 + 2 * _SLOT_TAIL_WORKERS + 1   # nothing queues in it
    assert pool.kw["max_keepalive"] == pool.kw["max_connections"]
    mint = _payload()["mint"]
    assert live.inline_facts_fn(mint) == (18, 18)
    # the lane host only: lane_inline_http refuses any other target (the custody RPC included)
    assert [url for url, _req, _t in pool.requests] == [LANE_URL]


def test_a_stale_kept_alive_connection_fails_closed_once_and_caches_nothing(live56, monkeypatch):
    from sniper.execution.fast_http import FastHttpError
    live, made = _pooled_live(live56, monkeypatch)
    made[0].fail = FastHttpError("POST request failed: RemoteProtocolError")
    mint = _payload()["mint"]
    before = live.paced.counts["transport_errors"]
    with pytest.raises(FastHttpError):
        live.inline_facts_fn(mint)
    assert len(made[0].requests) == 1                       # sent once: no retry on the fill path
    assert live.paced.counts["transport_errors"] == before + 1
    assert live.cache.facts(mint) is None


def test_snapshot_warning_names_its_lane(caplog):
    """The Base lane reuses Snapshot: its failures must not log as the BSC lane's (Beta 2026-10-21)."""
    import logging

    def dead(method, params):
        raise RuntimeError("429")
    for lane, kwargs in (("bsc", {}), ("base", {"lane": "base"})):
        caplog.clear()
        snap = bsc.Snapshot(dead, "0x" + "11" * 20, **kwargs)
        with caplog.at_level(logging.WARNING, logger=bsc.logger.name):
            assert snap.poll_once() is None
        assert [r.getMessage() for r in caplog.records] == [
            "%s fast lane: snapshot read failed: RuntimeError" % lane]
        assert snap.counts["errors"] == 1 and snap.current() is None
    src = Path(bsc.__file__).with_name("memecoin_base_fast_lane.py").read_text()
    assert 'Snapshot(host, address, weth=WETH, lane="base")' in src
