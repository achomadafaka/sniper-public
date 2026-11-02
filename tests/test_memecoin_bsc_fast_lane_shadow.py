"""BSC 56 fast lane, step 2: the runner's lane in runner_shadow (Alpha's approval (b): no 56 RPC key
yet, so safety and the wallet snapshot are absent; the signal is still judged; live stays blocked).

The lane thread authenticates the 56 watcher's datagram, builds the unsigned native-BNB PancakeSwap
V2 buy at the pilot clip with the whole-path minimum and tries the wallet lock without waiting. Off
the lane thread, one counted DexScreener read feeds the funded hook, whose every gate judges the
signal on a read-only ledger connection, rolled back, stopped before the leg. Alpha: in
runner_shadow the runner makes ZERO chain calls for this lane; a test proves it."""
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.request
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sniper.persistence import db_memecoin as db  # noqa: E402
from sniper.services import memecoin_bsc_fast_lane as bsc  # noqa: E402
from test_memecoin_bsc_fast_lane import QUOTE_X, ROW, WBNB, _synthetic, _decide  # noqa: E402

TAKER = "0x" + "6a" * 20


def _payload(quote=WBNB, **over):
    receipt, mint, post = _synthetic(quote=quote, hop_sync=None if quote == WBNB else (
        5 * 10 ** 21, 9 * 10 ** 20))
    out = _decide(receipt, mint, post)
    assert out["reason"] == "would_send", out
    row = dict(ROW, buy=dict(ROW["buy"], mint=mint))
    from test_memecoin_bsc_fast_lane_live import _paid_row
    row = _paid_row(row, receipt)
    now = time.time()
    p = bsc.lane_payload(row, out, decimals=18, bnb_usd=600.0, bnb_usd_at=now, sent_epoch=now)
    p.update(over)
    return p


# ---- the datagram's sender, once per buy ----

@pytest.mark.parametrize("pid, uid, watcher, payload, why", [
    (10, os.getuid() + 1, 10, None, "foreign_uid"),
    (10, os.getuid(), None, None, "no_watcher"),
    (11, os.getuid(), 10, None, "not_the_watcher"),
    (10, os.getuid(), 10, {"chain": "56"}, "malformed"),
    (10, os.getuid(), 10, "chain4663", "malformed"),
])
def test_only_the_56_watcher_of_this_user_is_heard(pid, uid, watcher, payload, why):
    if payload is None:
        payload = _payload()
    elif payload == "chain4663":
        payload = dict(_payload(), chain="4663")
    assert bsc.check_sender(payload, pid, uid, watcher_pid=watcher) == why
    assert bsc.check_sender(_payload(), 10, os.getuid(), watcher_pid=10) is None


def test_a_buy_is_judged_once():
    seen, p = bsc.Seen(), _payload()
    assert seen.first(p) is True and seen.first(dict(p)) is False
    assert seen.first(dict(p, mint="0x" + "99" * 20)) is True


# ---- the unsigned buy: native BNB, the whole-path minimum, no approval ----

@pytest.mark.parametrize("quote", [WBNB, QUOTE_X])
def test_the_buy_is_a_native_bnb_pancake_v2_swap_with_the_whole_path_minimum(quote):
    from eth_abi import decode
    p = _payload(quote=quote)
    clip = 10 ** 17
    min_out = bsc.min_out_for(clip, p)
    tx = bsc.v2_native_buy_tx(p, clip, min_out, 1790000060, TAKER)
    assert tx["to"] == bsc.PCS_V2_ROUTER and tx["value"] == clip
    assert tx["data"][:10] == "0x" + bsc.SWAP_ETH_FOR_TOKENS_FOT
    got_min, path, to, deadline = decode(["uint256", "address[]", "address", "uint256"],
                                         bytes.fromhex(tx["data"][10:]))
    assert (got_min, [a.lower() for a in path], to.lower(), deadline) == (
        min_out, bsc.buy_path(p), TAKER, 1790000060)
    assert bsc.buy_path(p) == ([WBNB, p["mint"]] if quote == WBNB else [WBNB, QUOTE_X, p["mint"]])


# ---- the lane thread's own work ----

def _shadow(tmp_path, **kw):
    args = dict(lock_key="evm-56-0xabc", lock_dir=str(tmp_path), taker=TAKER, now_fn=time.time,
                clip_usd=50.0, bnb_usd_max_age_s=120.0)
    args.update(kw)
    return bsc.BscShadow(**args)


def test_the_lane_builds_measures_and_never_waits_for_the_wallet_lock(tmp_path):
    import threading
    from sniper.execution.exec_lock import wallet_exec_lock
    shadow = _shadow(tmp_path)
    rec = shadow.handle(_payload(), recv_epoch=time.time())
    assert rec["reason"] == "built" and rec["lock_busy"] == 0 and rec["tx_bytes"] > 0
    assert rec["clip_wei"] == int(Fraction(50, 600) * 10 ** 18)
    got = []
    with wallet_exec_lock("evm-56-0xabc", lock_dir=str(tmp_path)):
        t = threading.Thread(target=lambda: got.append(shadow.handle(_payload(), recv_epoch=time.time())))
        t.start()
        t.join(0.5)
        assert not t.is_alive(), "the lane waited for the wallet lock"
    assert got[0]["reason"] == "lock_busy"


@pytest.mark.parametrize("over, reason", [({"bnb_usd": 0.0}, "no_bnb_usd"),
                                          ({"bnb_usd_at": 1.0}, "bnb_usd_stale")])
def test_a_missing_or_stale_bnb_price_is_a_named_fallback(tmp_path, over, reason):
    assert _shadow(tmp_path).handle(_payload(**over), recv_epoch=time.time())["reason"] == reason


# ---- end to end on the production 56 context: zero chain calls, the hook's every gate ----

class Net:
    def __init__(self):
        self.calls = []


def _ds_pair(mint, *, fdv=20_000.0, price="0.000247"):
    return {"chainId": "bsc", "dexId": "pancakeswap", "pairAddress": "0x" + "ab" * 20,
            "baseToken": {"address": mint, "symbol": "TKN"},
            "quoteToken": {"address": WBNB, "symbol": "WBNB"}, "priceUsd": price,
            "marketCap": fdv, "fdv": fdv, "liquidity": {"usd": 50000},
            "volume": {"m5": 4000, "h1": 20000, "h24": 40000},
            "txns": {"m5": {"buys": 30, "sells": 10}, "h1": {"buys": 90, "sells": 40}},
            "pairCreatedAt": int((time.time() - 3 * 3600) * 1000)}


@pytest.fixture
def lane56(monkeypatch, tmp_path):
    from sniper.execution import evm_wallet
    from sniper.execution import solana_quote as sq
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg
    from test_memecoin_evm_context_keys import _production_context
    ctx = _production_context(monkeypatch, tmp_path, "56")

    def today_executor(*a, **k):
        raise AssertionError("the hook ran today's 56 executor for a lane signal")
    ctx["execute_fn"] = today_executor
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56"})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_QUIET_HOURS_UTC", "", raising=False)
    monkeypatch.setenv("MEMECOIN_KILL_SWITCH_FILE", str(tmp_path / "kill"))
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(runner, "_gate_balance_cache", {})
    monkeypatch.setattr(sq, "_ds_cooldown_until", [0.0])
    net = Net()

    def guard(name):
        def blocked(*a, **k):
            net.calls.append(name)
            raise OSError("network blocked in this test: %s" % name)
        return blocked
    monkeypatch.setattr(socket.socket, "connect", guard("socket.connect"))
    monkeypatch.setattr(socket, "create_connection", guard("socket.create_connection"))
    monkeypatch.setattr(urllib.request, "urlopen", guard("urlopen"))
    monkeypatch.setattr(evm_wallet, "_http", guard("evm_wallet._http"))
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
    dex_asked = []
    stats = {"fdv": 20_000.0}

    def dex_base(method, url, headers=None, body=None):
        dex_asked.append(url)
        mint = url.rstrip("/").rsplit("/", 1)[-1]
        return 200, {"pairs": [_ds_pair(mint, fdv=stats["fdv"])]}, None
    lane = bsc.build_bsc_lane({"56": ctx}, log=lambda *_: None, mode="runner_shadow",
                              root=tmp_path, ro_connect=ro, dex_base=dex_base)
    Path(lane["dispatch"].beat).parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    Path(lane["dispatch"].beat).write_text(json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}))
    return lane, ledger, net, dex_asked, stats


def _rows(ledger):
    return {t: ledger.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
            for t in ("memecoin_entry_attempts", "memecoin_pending_entries", "memecoin_positions")}


def _signal_through_the_watcher(lane, payload_quote=WBNB):
    """The watcher's own signal function, delivering straight to the runner's dispatch."""
    receipt, mint, post = _synthetic(quote=payload_quote)
    got = []
    signal = bsc.make_watcher_signal(
        "runner_shadow", "\0unused",
        send_fn=lambda a, p: got.append(lane["dispatch"].on_datagram(p, os.getpid(), os.getuid(),
                                                                     time.time())) or True)
    from sniper.util.clock import real_utc_now_iso
    row = dict(ROW, tx_hash="0x%064x" % int(time.time() * 1e6), buy=dict(ROW["buy"], mint=mint),
               source_user_id="uid", now=real_utc_now_iso())
    from test_memecoin_bsc_fast_lane_live import _paid_row
    row = _paid_row(row, receipt)
    out = signal(row, receipt, post, decimals=18, bnb_usd=600.0, bnb_usd_age_s=1.0)
    assert out["reason"] == "would_send" and out["sent"], out
    lane["shadow"].join_checks()
    return got[0]


def _verdicts(lane):
    return sqlite3.connect(lane["db_path"]).execute(
        "SELECT verdict, stats FROM bsc_lane_verdicts ORDER BY id").fetchall()


def test_the_shadow_mcap_is_dexscreener_supply_at_the_source_price_and_says_so(lane56):
    # Gamma on 5424e373e: the value, and its basis on the row (funded 56 uses chain totalSupply).
    lane, ledger, net, dex_asked, stats = lane56
    _signal_through_the_watcher(lane)
    (mcap, basis), = sqlite3.connect(lane["db_path"]).execute(
        "SELECT mcap, mcap_basis FROM bsc_lane_verdicts").fetchall()
    assert mcap == pytest.approx(20_000.0 / 0.000247 * ROW["buy"]["source_price"])
    assert basis == bsc.SHADOW_MCAP_BASIS
    assert bsc.shadow_market_cap({"fdv": 1000.0, "price": 0.5}, 2.0) == pytest.approx(4000.0)
    assert bsc.shadow_market_cap({"fdv": 1000.0, "price": 0}, 2.0) is None


def test_a_zero_minimum_output_is_refused_never_built(tmp_path):
    # Gamma on 5424e373e: min_out 0 is no slippage bound at all.
    rec = _shadow(tmp_path).handle(_payload(cap_num=10 ** 60, cap_den=1, r_coin=1), recv_epoch=time.time())
    assert rec["min_out"] == 0 and rec["reason"] == "build_refused"


def test_runner_shadow_judges_every_gate_with_zero_chain_calls_and_no_funded_row(lane56):
    lane, ledger, net, dex_asked, stats = lane56
    before = _rows(ledger)
    rec = _signal_through_the_watcher(lane)
    assert rec["reason"] == "built"
    db.set_wallet_real_approved(ledger, "uid", False, chains=["56"], force=True)
    _signal_through_the_watcher(lane)
    assert _verdicts(lane)[0][0] == "pass", _verdicts(lane)
    assert [v[0].split(":")[0] if v[0].startswith("hook:") else v[0] for v in _verdicts(lane)] == [
        "pass", "hook"]
    assert _verdicts(lane)[1][0].startswith("hook:not_approved:")
    assert net.calls == [] and len(dex_asked) == 2              # only the counted DexScreener reads
    assert _rows(ledger) == before and before["memecoin_entry_attempts"] == 0
    events = sqlite3.connect(lane["db_path"]).execute(
        "SELECT reason, mode FROM bsc_lane_events").fetchall()
    assert events == [("built", "runner_shadow")] * 2


@pytest.mark.parametrize("absolute_floor,ratio,ceiling", [
    (0.0, .005, 0.0), (200.0, .005, 0.0), (200.0, .1, 0.0), (0.0, .005, 50.0),
    (0.0, .001, 13.37),
    (13.37, .001, 13.37),
])
def test_startup_warm_reaches_full_shadow_hook_with_zero_provider_or_funded_work(
        lane56, monkeypatch, absolute_floor, ratio, ceiling):
    lane, ledger, net, dex_asked, stats = lane56
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(runner.cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", absolute_floor)
    monkeypatch.setattr(runner.cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {"56:nano": ratio})
    monkeypatch.setattr(runner.cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", ceiling)
    from sniper.services import memecoin_evm_onchain_watch as decoder
    observed = []
    original = decoder.direct_buy_from_receipt
    def capture(*args, **kwargs):
        buy = original(*args, **kwargs)
        observed.append(buy)
        return buy
    monkeypatch.setattr(decoder, "direct_buy_from_receipt", capture)
    def forbidden(*a, **k):
        pytest.fail("read-only startup warm reached funded/provider work")
    monkeypatch.setattr(runner, "real_entry", forbidden)
    for context in (lane["shadow"].ctx, lane["shadow"].contexts["56"]):
        for key in ("execute_fn", "sign_tx_fn", "usdc_fn", "native_fn", "token_safety_fn", "http"):
            monkeypatch.setitem(context, key, forbidden)
    before = _rows(ledger)
    lines = []
    elapsed, verdict = bsc.warm_bsc_hook(lane["shadow"], log=lines.append)
    assert verdict == "pass", lines
    assert elapsed >= 0 and len(lines) == 1
    assert net.calls == [] and dex_asked == []
    assert _rows(ledger) == before
    buy, = observed
    assert buy["source_size_usd"] >= max(absolute_floor, 6000.0 * ratio)
    assert not ceiling or buy["source_size_usd"] <= ceiling
    assert buy["source_price"] == pytest.approx(6e-6)
    assert buy["source_size_usd"] / buy["token_delta"] == buy["source_price"]


def test_startup_warm_preserves_contradictory_source_buy_policy_decline(lane56, monkeypatch):
    from sniper.settings import memecoin as cfg
    lane, ledger, net, dex_asked, stats = lane56
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_SOURCE_BUY_USD", 100.0)
    before = _rows(ledger)
    lines = []
    elapsed, verdict = bsc.warm_bsc_hook(lane["shadow"], log=lines.append)
    assert verdict.startswith("hook:rejected:"), lines
    assert cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_USD == 200.0
    assert cfg.MEMECOIN_REAL_MAX_SOURCE_BUY_USD == 100.0
    assert elapsed >= 0 and net.calls == [] and dex_asked == [] and _rows(ledger) == before


@pytest.mark.parametrize("invalid", [None, float("nan")])
def test_startup_warm_unknown_floor_never_claims_a_passing_probe(lane56, monkeypatch, invalid):
    from sniper.settings import memecoin as cfg
    lane, ledger, net, dex_asked, stats = lane56
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", invalid)
    before = _rows(ledger)
    lines = []
    assert bsc.warm_bsc_hook(lane["shadow"], log=lines.append) == (None, None)
    assert "hook warm failed" in lines[0]
    assert net.calls == [] and dex_asked == [] and _rows(ledger) == before


def test_startup_warm_without_armed_roster_keeps_existing_decline(lane56):
    lane, ledger, net, dex_asked, stats = lane56
    db.set_wallet_real_approved(ledger, "uid", False, chains=["56"], force=True)
    before = _rows(ledger)
    lines = []
    elapsed, verdict = bsc.warm_bsc_hook(lane["shadow"], log=lines.append)
    assert verdict.startswith("hook:"), lines
    assert "source_initiation_unknown" not in verdict
    assert verdict.startswith(("hook:not_approved:", "hook:unknown_source:"))
    assert elapsed >= 0 and net.calls == [] and dex_asked == []
    assert _rows(ledger) == before


def test_stats_without_a_supply_basis_is_no_market_cap_never_a_chain_read(lane56):
    lane, ledger, net, dex_asked, stats = lane56
    stats["fdv"] = 0.0
    _signal_through_the_watcher(lane)
    assert _verdicts(lane) == [("no_market_cap", 1)] and net.calls == []


def test_a_foreign_sender_is_dropped_before_any_work(lane56):
    lane, ledger, net, dex_asked, stats = lane56
    p = _payload()
    assert lane["dispatch"].on_datagram(p, os.getpid() + 1, os.getuid(), time.time()) is None
    assert lane["dispatch"].drops["not_the_watcher"] == 1 and dex_asked == []


# ---- wiring ----

def test_the_lane_is_off_unless_the_knob_and_a_56_context(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "")
    assert bsc.start_bsc_lane({"56": {"address": "0xabc"}}, log=lambda *_: None, root=tmp_path) is None
    monkeypatch.setenv("MEMECOIN_BSC_FAST_LANE", "runner_shadow")
    assert bsc.start_bsc_lane({}, log=lambda *_: None, root=tmp_path) is None


def test_the_runner_starts_the_56_lane():
    src = (Path(bsc.__file__).resolve().parents[2] / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "start_bsc_lane(contexts" in src
