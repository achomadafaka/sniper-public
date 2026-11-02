"""Held pump-curve positions get their market mark from the bonding-curve account, not DexScreener.

2026-09-27 (operator: "get these DexScreener prices out"; Alpha step 1). A curve position's only
market price was the DexScreener mid, which lags 25-55 s: 65922 showed -2.0% while the exact exit
quote read -35.4%. build_price_book skipped curves because its decoder reads SPL token vaults.
Now the curve PDA is subscribed on the same swap-mark socket (base64), seeded once after each ack,
decoded fail-closed, and stored as a QuoteMark(source="fastlane_sol_curve", quote_mint=WSOL) that
reaches the rail wake through cache._notify (the contract agreed with Lambda). Display and marks
only: exact route quotes stay authoritative, and curve pushes never become vol_death evidence.
"""
import asyncio
import base64
import hashlib
import json
import sqlite3
import struct
import threading

import pytest

from sniper.services import memecoin_solana_swap_marks as S
from sniper.services import memecoin_solana_ws as W
from sniper.services.memecoin_solana_amounts import WSOL

DISC = hashlib.sha256(b"account:BondingCurve").digest()[:8]
SUPPLY = 1_000_000_000 * 10**6
CURVE = "5fN9cTNL6EnhMjr5LYmNiLJb9SWecExHqKJv3gBRKz9M"
MINT = "3BbszudUCPL5MTKX97tVveF9hhkLuKarEWEUh1ympump"
VAULT = "2SF3pfpbXH5z6L5X7TEfQqbvQJR7WBwbrrHxowegXNLJ"


def _blob(vt=800_000_000 * 10**6, vs=40 * 10**9, rt=520_000_000 * 10**6, rs=10 * 10**9,
          supply=SUPPLY, complete=0, disc=DISC, tail=b"\x00" * 32):
    return disc + struct.pack("<5Q", vt, vs, rt, rs, supply) + bytes([complete]) + tail


# --- decode ------------------------------------------------------------------------------------

def test_a_real_shaped_curve_decodes_to_its_reserves():
    state = W.decode_bonding_curve(_blob())
    assert state == {"virtual_token": 800_000_000 * 10**6, "virtual_sol": 40 * 10**9,
                     "real_token": 520_000_000 * 10**6, "real_sol": 10 * 10**9, "complete": False}


@pytest.mark.parametrize("blob", [
    _blob(disc=b"\x00" * 8),                 # not a BondingCurve account
    _blob(supply=SUPPLY // 10),              # not pump's fixed supply: the decimals are unproven
    _blob(complete=7),                       # not a bool byte: misaligned layout
    _blob(vt=1, rt=2),                       # virtual below real: misaligned layout
    _blob(vs=0),                             # an empty curve prices nothing
    _blob()[:40],                            # truncated
    "not bytes",
])
def test_anything_unproven_decodes_to_nothing(blob):
    assert W.decode_bonding_curve(blob) is None


def test_a_curve_frame_parses_as_raw_bytes():
    frame = {"jsonrpc": "2.0", "method": "accountNotification", "params": {
        "subscription": 9, "result": {"context": {"slot": 451000001}, "value": {
            "data": [base64.b64encode(_blob()).decode(), "base64"], "owner": "x"}}}}
    assert W.parse_frame(frame, {9: CURVE}) == ("raw", CURVE, 451000001, _blob())


def test_the_curve_is_subscribed_base64_and_the_vaults_stay_jsonparsed():
    curve = json.loads(W.curve_subscribe_frame(CURVE, 3))
    assert curve["method"] == "accountSubscribe"
    assert curve["params"] == [CURVE, {"encoding": "base64", "commitment": "confirmed"}]
    assert json.loads(W.subscribe_frame(VAULT, 4))["params"][1]["encoding"] == "jsonParsed"


def test_an_older_seed_never_overwrites_a_newer_push():
    tape = W.CurveTape(CURVE, mint=CURVE)
    assert tape.on_raw(100, _blob(vs=41 * 10**9))["virtual_sol"] == 41 * 10**9
    assert tape.on_raw(99, _blob(vs=40 * 10**9)) is None
    assert tape.on_raw(100, _blob(vs=42 * 10**9))["virtual_sol"] == 42 * 10**9


# --- the stream: base64 subscribe, seed after ack -----------------------------------------------

class _Socket:
    def __init__(self):
        self.sent, self.frames = [], []

    async def send(self, text):
        req = json.loads(text)
        self.sent.append(req)
        self.frames.append({"jsonrpc": "2.0", "id": req["id"], "result": 70 + req["id"]})

    def __aiter__(self):
        return self

    async def __anext__(self):
        for _ in range(50):
            if self.frames:
                return json.dumps(self.frames.pop(0))
            await asyncio.sleep(0.001)
        raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_the_stream_subscribes_the_curve_and_seeds_it_after_the_ack():
    sock = _Socket()
    rows, seeded = [], []

    def seed(account):
        seeded.append(account)
        return 451000005, _blob(vs=43 * 10**9)

    vaults = {CURVE: W.CurveTape(CURVE, mint=CURVE)}
    asyncio.run(W.run_vault_stream(vaults, rows.append, ws_url="wss://x.invalid/",
                                   connect=lambda url: sock, seed_fn=seed))
    assert sock.sent[0]["params"][1]["encoding"] == "base64"
    assert seeded == [CURVE]
    assert len(rows) == 1 and rows[0]["kind"] == "sol_curve_state"
    assert rows[0]["pool"] == CURVE and rows[0]["block_no"] == 451000005


def test_a_failed_seed_is_no_row_not_a_fault():
    sock = _Socket()
    rows = []

    def seed(account):
        raise TimeoutError("read timed out")

    asyncio.run(W.run_vault_stream({CURVE: W.CurveTape(CURVE, mint=CURVE)}, rows.append,
                                   ws_url="wss://x.invalid/", connect=lambda url: sock,
                                   seed_fn=seed))
    assert rows == []


@pytest.mark.parametrize("complete", [False, True])
def test_delayed_same_slot_seed_cannot_replace_a_curve_push(complete):
    pushed = threading.Event()
    seeded = threading.Event()
    rows = []
    cache = S.SolanaSwapMarkCache()
    payload = (_blob(complete=1, vt=0, vs=0, rt=0, rs=0) if complete
               else _blob(vs=42 * 10**9))

    class Socket(_Socket):
        async def send(self, text):
            await super().send(text)
            self.frames.append({"method": "accountNotification", "params": {
                "subscription": 71, "result": {"context": {"slot": 100}, "value": {
                    "data": [base64.b64encode(payload).decode(), "base64"]}}}})

    def seed(account):
        assert pushed.wait(1.0)
        seeded.set()
        return 100, _blob()

    def on_row(row):
        rows.append(row)
        cache.record(row, _binding(), now_ts=1000.0, observed_at="push")
        pushed.set()

    sock = Socket()
    asyncio.run(W.run_vault_stream({CURVE: W.CurveTape(CURVE, mint=CURVE)}, on_row,
                                   ws_url="wss://x.invalid/", connect=lambda url: sock,
                                   seed_fn=seed))
    assert seeded.is_set()
    assert len(rows) == 1
    prices = cache.fresh_price_map({WSOL: 150.0}, now_ts=1001.0, max_age_s=5.0)
    if complete:
        assert prices == {}
    else:
        assert prices[("solana", MINT)]["price"] == pytest.approx(7.875e-6)


# --- the cache: the agreed QuoteMark, the wake, the price map ----------------------------------

def _binding():
    return S.PoolBinding(mint=MINT, pool=CURVE, coin_vault=VAULT, quote_vault=CURVE,
                         quote_mint=WSOL)


def _row(**kw):
    return {"chain_id": "solana", "pool": CURVE, "kind": "sol_curve_state", "block_no": 451000009,
            **W.decode_bonding_curve(_blob(**kw))}


def test_a_curve_row_becomes_the_agreed_quote_mark_and_wakes_the_rail_lane():
    woke = []
    cache = S.SolanaSwapMarkCache(on_mark=woke.append)
    assert cache.record(_row(), _binding(), now_ts=1000.0, observed_at="2026-09-27T04:30:00Z")
    mark = woke[0]
    assert (mark.mint, mark.pool, mark.quote_mint, mark.source) == (
        MINT, CURVE, WSOL, "fastlane_sol_curve")
    assert mark.quote_per_token == pytest.approx((40 * 10**9 / 1e9) / (800_000_000 * 10**6 / 1e6))
    assert mark.slot == 451000009 and mark.observed_ts == 1000.0


def test_curve_only_mark_exposes_its_quote_binding():
    cache = S.SolanaSwapMarkCache()
    cache.record(_row(), _binding(), now_ts=1000.0, observed_at="2026-09-27T04:30:00Z")
    assert cache.quote_mints() == {WSOL}
    assert cache.bindings_by_mint()[MINT] == WSOL


def test_a_quiet_curve_stays_priced_until_the_epoch_invalidates_it():
    cache = S.SolanaSwapMarkCache()
    cache.record(_row(), _binding(), now_ts=1000.0, observed_at="2026-09-27T04:30:00Z")
    book = cache.fresh_price_map({WSOL: 150.0}, now_ts=1025.0, max_age_s=5.0)
    assert book[("solana", MINT)]["source"] == "fastlane_sol_curve"
    assert book[("solana", MINT)]["price"] == pytest.approx((40 / 800_000_000) * 150.0)
    assert book[("solana", MINT)]["observed_at"] == "2026-09-27T04:30:00Z"
    cache.invalidate_prices()
    assert ("solana", MINT) not in cache.fresh_price_map({WSOL: 150.0}, now_ts=1026.0, max_age_s=5.0)


def test_a_completed_curve_stops_pricing_and_drops_its_mark():
    woke = []
    cache = S.SolanaSwapMarkCache(on_mark=woke.append)
    cache.record(_row(), _binding(), now_ts=1000.0, observed_at="t0")
    assert cache.record(_row(complete=1), _binding(), now_ts=1001.0, observed_at="t1") is False
    assert len(woke) == 1
    assert ("solana", MINT) not in cache.fresh_price_map({WSOL: 150.0}, now_ts=1001.0, max_age_s=5.0)


def test_a_curve_push_is_never_vol_death_reserve_evidence():
    cache = S.SolanaSwapMarkCache()
    cache.record(_row(), _binding(), now_ts=1000.0, observed_at="t0")
    assert cache._reserve_times == {} and cache._reserve_changed == {}


# --- the price book: curves are subscribed now, AMMs unchanged --------------------------------

def test_the_price_book_subscribes_a_held_curve_at_its_pda():
    tape = sqlite3.connect(":memory:")
    tape.executescript(
        "CREATE TABLE sol_position_pools (position_id INTEGER, mint TEXT, pool TEXT, venue_kind TEXT);"
        "CREATE TABLE sol_pool_vaults (pool TEXT, mint TEXT, coin_vault TEXT, quote_vault TEXT, "
        "quote_mint TEXT);")
    tape.execute("INSERT INTO sol_position_pools VALUES (65860, ?, ?, 'pump_curve')", (MINT, VAULT))
    tape.execute("INSERT INTO sol_pool_vaults VALUES (?, ?, ?, ?, 'native-sol')",
                 (VAULT, MINT, VAULT, CURVE))
    vaults, bindings = S.build_price_book([{"id": 65860, "mint": MINT, "network_id": "solana"}], tape)
    assert isinstance(vaults[CURVE], W.CurveTape) and set(vaults) == {CURVE}
    assert bindings[CURVE] == _binding()


# --- the seed read and the epoch wiring -----------------------------------------------------

def test_the_seed_reads_one_account_at_confirmed_and_fails_closed():
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        return {"context": {"slot": 451000011},
                "value": {"data": [base64.b64encode(_blob()).decode(), "base64"]}}

    seed = S.curve_seed_fn("https://x.invalid/", rpc_call=rpc)
    assert seed(CURVE) == (451000011, _blob())
    assert calls == [("getAccountInfo", [CURVE, {"encoding": "base64", "commitment": "confirmed"}])]
    for bad in (None, {}, {"context": {"slot": 1}, "value": None},
                {"context": {"slot": 1}, "value": {"data": ["!!", "base64"]}},
                {"context": {}, "value": {"data": ["", "base64"]}}):
        assert S.curve_seed_fn("u", rpc_call=lambda m, p, _b=bad: _b)(CURVE) is None


def test_the_epoch_hands_the_seed_to_the_stream():
    got = {}

    async def stream(vaults, on_row, **kwargs):
        got.update(kwargs)

    cache = S.SolanaSwapMarkCache()
    seed = object()
    asyncio.run(S.run_price_stream_epoch({}, {}, cache, ws_url="wss://x.invalid/", duration_s=1.0,
                                         stream_fn=stream, seed_fn=seed))
    assert got["seed_fn"] is seed


def test_the_runner_worker_seeds_curves_from_the_read_rpc():
    import ast
    src = open("scripts/memecoin_runner_loop.py").read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", None)) == "run_price_stream_epoch"]
    assert calls and all(any(k.arg == "seed_fn" for k in c.keywords) for c in calls)


def test_a_graduated_curve_with_zeroed_reserves_still_says_complete_and_drops_the_mark():
    """Alpha's live read of 65924 (2026-09-27): after graduation the curve account holds
    (vt, vs, rt, rs, supply) = (0, 0, 0, 0, 1e15) with complete=1. Refusing it as 'empty' kept the
    pre-graduation price alive until the epoch clear, right when the coin moves hardest."""
    graduated = _blob(vt=0, vs=0, rt=0, rs=0, supply=SUPPLY, complete=1)
    state = W.decode_bonding_curve(graduated)
    assert state is not None and state["complete"] is True
    woke = []
    cache = S.SolanaSwapMarkCache(on_mark=woke.append)
    cache.record(_row(), _binding(), now_ts=1000.0, observed_at="t0")
    tape = W.CurveTape(CURVE, mint=CURVE)
    row = tape.on_raw(451000020, graduated)
    assert row is not None and row["complete"] is True
    assert cache.record(row, _binding(), now_ts=1001.0, observed_at="t1") is False
    assert ("solana", MINT) not in cache.fresh_price_map({WSOL: 150.0}, now_ts=1001.0, max_age_s=5.0)
    assert len(woke) == 1, "a completed curve wakes nothing"


def test_complete_is_still_proven_only_by_the_discriminator_and_the_supply():
    assert W.decode_bonding_curve(_blob(vt=0, vs=0, rt=0, rs=0, complete=1, disc=b"\x00" * 8)) is None
    assert W.decode_bonding_curve(_blob(vt=0, vs=0, rt=0, rs=0, complete=1, supply=5)) is None


# --- the epoch line the runner prints (Alpha, 2026-09-27: logger.info never reached runner.log) ---

def test_the_epoch_summary_counts_vaults_curves_and_pools():
    vaults = {CURVE: W.CurveTape(CURVE, mint=CURVE), "CoinV": object(), "QuoteV": object()}
    bindings = {CURVE: _binding(), "PoolA": object()}
    assert S.epoch_summary(vaults, bindings) == (
        "sol price stream epoch: 3 vaults (1 curves), 2 pools")


def test_the_runner_prints_the_epoch_summary_once_per_epoch():
    import ast
    tree = ast.parse(open("scripts/memecoin_runner_loop.py").read())
    worker = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_solana_swap_mark_worker")
    printed = [n for n in ast.walk(worker) if isinstance(n, ast.Call)
               and getattr(n.func, "id", None) == "_p"
               and any(isinstance(a, ast.Call) and getattr(a.func, "id", None) == "epoch_summary"
                       for a in ast.walk(n))]
    assert len(printed) == 1
