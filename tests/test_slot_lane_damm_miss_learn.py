"""DAMM v2 first sight on the slot lane (Alpha 2026-10-07, operator report "slot lanes are failing").

A DAMM v2 routed frame names no pool (its swap event is a self-CPI), so a coin the cache never
learned was handed off and filled by the watcher at S+3/S+4 (INFERRA 9ufM9TJd 15:50:34Z, SWCH
6gnvghh8 15:51:55Z). With MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN the MissLearner finds the coin's
pool BY MINT inside its budget: getProgramAccounts memcmp on mint_a and on mint_b (in parallel),
then ONE getMultipleAccounts of the candidate pools, their quote vaults and the two mints, on its
own ONCHAIN_WATCH_HELIUS client. The deepest SOL pool at or above the depth floor wins (18 of 22
measured first-sight coins: the source's own pool; the 4 misses were MET/JUP-quoted source pools,
each SOL pool $0-406 deep). The build's signed source-price floor still bounds what a wrong pool
could cost: it reverts, it never overpays.
"""
import base64
import json
import threading
import time
import types
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.execution import meteora_damm_v2_buy as D
from sniper.services import memecoin_slot_lane as lane
from sniper.services import memecoin_slot_signal as s
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/damm_v2_sol_pools.json").read_text())
CLASSIC, T22 = FX["pools"]
COIN = CLASSIC["coin"]
WSOL, USDC = D.WSOL_MINT, D.USDC_MINT
MET = "METvsvVRapdj9cFLzq4Tr43xK4tAjQfwX76z3n6mWQL"
JUP = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
PAUSED = str(Pubkey.from_bytes(bytes([201]) * 32))
# LILMET D75KtdgF on chain 2026-10-08: 3wRxFYSp holds 55,964.924599 MET, C6GFJ9mX 0.007449119 SOL.
LILMET_MET, LILMET_SOL = 55_964_924_599, 7_449_119


def _key(n):
    return str(Pubkey.from_bytes(bytes([n]) * 32))


def _pool_raw(mint_a, mint_b, vault_a, vault_b):
    raw = bytearray(base64.b64decode(CLASSIC["pool_account"]["data"][0]))
    for off, key in ((D.OFF_MINT_A, mint_a), (D.OFF_MINT_B, mint_b), (D.OFF_VAULT_A, vault_a),
                     (D.OFF_VAULT_B, vault_b)):
        raw[off:off + 32] = bytes(Pubkey.from_string(key))
    return bytes(raw)


def _mint_value(mint):
    """The fixture's parsed mint account for the coin or wSOL (CLASSIC is a coin/SOL pool); any other
    quote mint reads as a plain 6-decimal SPL mint."""
    a, b = CLASSIC["mints"]
    state = D.parse_pool(base64.b64decode(CLASSIC["pool_account"]["data"][0]))
    by = {state["mint_a"]: a, state["mint_b"]: b}
    if mint in by:
        return by[mint]
    fake = json.loads(json.dumps(by[WSOL]))
    fake["data"]["parsed"]["info"]["decimals"] = 6
    if mint == PAUSED:                       # a quote no build can move (the worker refuses it too)
        fake["data"]["parsed"]["info"]["extensions"] = [
            {"extension": "pausableConfig", "state": {"paused": True}}]
    return fake


class ChainRpc:
    """getProgramAccounts by memcmp offset, getMultipleAccounts by address, from `pools`:
    [(pubkey, mint_a, mint_b, vault_a, vault_b, {vault: amount})]."""

    def __init__(self, pools, *, fail=None, short=False):
        self.pools, self.fail, self.short, self.calls = pools, fail, short, []
        self.accounts = {}
        for pk, ma, mb, va, vb, amounts in pools:
            raw = _pool_raw(ma, mb, va, vb)
            self.accounts[pk] = {"owner": D.PROGRAM, "lamports": 1,
                                 "data": [base64.b64encode(raw).decode(), "base64"]}
            for v, amount in amounts.items():
                self.accounts[v] = {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(amount)}}}}}

    def __call__(self, method, params):
        self.calls.append(method)
        if self.fail == method:
            raise TimeoutError("read timed out")
        if method == "getProgramAccounts":
            off = params[1]["filters"][1]["memcmp"]["offset"]
            want = params[1]["filters"][1]["memcmp"]["bytes"]
            out = []
            for pk, ma, mb, va, vb, _ in self.pools:
                if (ma if off == D.OFF_MINT_A else mb) == want:
                    raw = _pool_raw(ma, mb, va, vb)[D.OFF_MINT_A:D.OFF_MINT_A + 128]
                    out.append({"pubkey": pk, "account": {"data": [base64.b64encode(raw).decode(), "base64"]}})
            return out
        if method == "getAccountInfo":
            return {"value": self.accounts.get(params[0])}
        if method == "getMultipleAccounts":
            keys = params[0]
            values = [self.accounts.get(k) or (_mint_value(k) if k in (COIN, WSOL, USDC, MET, JUP, PAUSED)
                                                else None)
                      for k in keys]
            return {"value": values[:-1] if self.short else values}
        raise AssertionError(method)


def _sol_pool(pk, lamports, *, coin_side="a"):
    va, vb = _key(pk + 100), _key(pk + 150)
    if coin_side == "a":
        return (_key(pk), COIN, WSOL, va, vb, {vb: lamports})
    return (_key(pk), WSOL, COIN, va, vb, {va: lamports})


SOL_USD = 150.0


def _find(rpc, **kw):
    return D.find_pool_by_mint(rpc, COIN, sol_usd=kw.pop("sol_usd", SOL_USD),
                               min_depth_usd=kw.pop("min_depth_usd", 2500.0), **kw)


def test_the_deepest_sol_pool_wins_on_either_mint_side_and_carries_build_facts():
    pools = [_sol_pool(1, 5 * 10 ** 9), _sol_pool(2, 200 * 10 ** 9, coin_side="b"), _sol_pool(3, 40 * 10 ** 9)]
    facts, why = _find(ChainRpc(pools))
    assert why is None and facts["pool"] == _key(2) and facts["coin"] == COIN and facts["quote"] == WSOL
    assert facts["coin_refusal"] is None and facts["coin_facts"] is not None
    assert facts["depth_usd"] == pytest.approx(200 * SOL_USD)


def test_every_candidate_is_read_in_one_get_multiple_accounts_after_two_program_reads():
    rpc = ChainRpc([_sol_pool(1, 50 * 10 ** 9), _sol_pool(2, 60 * 10 ** 9, coin_side="b")])
    _find(rpc)
    assert sorted(rpc.calls) == ["getMultipleAccounts", "getProgramAccounts", "getProgramAccounts"]


def _exotic_pool(pk, amount, quote=MET):
    return (_key(pk), COIN, quote, _key(pk + 100), _key(pk + 150), {_key(pk + 150): amount})


LILMET = [_exotic_pool(9, LILMET_MET), _sol_pool(1, LILMET_SOL)]


@pytest.fixture
def exotic_listed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", ("meteora_damm_v2",), raising=False)


def test_unlisted_an_exotic_quoted_pool_is_never_a_candidate_however_deep(monkeypatch):
    """8iYPW781 / 5dvXTZ5q: the source swapped a MET pool; the SOL pool was $5 deep."""
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", (), raising=False)
    facts, why = _find(ChainRpc([_exotic_pool(9, 10 ** 15), _sol_pool(1, 3 * 10 ** 7)]))   # $4.50
    assert facts is None and why == "shallow"


def test_lilmet_first_sight_learns_its_met_pool_unpriced(exotic_listed):
    """Operator order 2026-10-08: 09:18:46Z refused "shallow" on the $1 SOL pool. The MET pool is
    adopted with its depth in quote units; the floor is judged once the leg prices MET."""
    before = time.monotonic()
    facts, why = _find(ChainRpc(LILMET))
    assert why is None and facts["pool"] == _key(9) and facts["quote"] == MET
    assert facts["depth_usd"] is None and facts["depth_quote_atomic"] == LILMET_MET
    assert facts["quote_facts"] == (6, 0, 0) and facts["mint_values"][MET] is not None
    assert before <= facts["quote_read_at"] <= time.monotonic()


def test_a_sol_pool_under_the_floor_still_refuses_with_exotic_listed(exotic_listed):
    assert _find(ChainRpc([_sol_pool(1, 10 ** 9)])) == (None, "shallow")          # $150 < $2,500
    assert _find(ChainRpc([_sol_pool(1, LILMET_SOL)])) == (None, "shallow")


def test_a_priced_pool_at_the_floor_beats_a_deeper_exotic_pool(exotic_listed):
    facts, why = _find(ChainRpc([_exotic_pool(9, 10 ** 15), _sol_pool(1, 20 * 10 ** 9)]))   # $3,000
    assert why is None and facts["quote"] == WSOL and facts["depth_usd"] == pytest.approx(3000.0)


def test_the_deepest_pool_of_one_exotic_quote_wins(exotic_listed):
    facts, why = _find(ChainRpc([_exotic_pool(9, 10 ** 9), _exotic_pool(8, 10 ** 12), _sol_pool(1, 10)]))
    assert why is None and facts["pool"] == _key(8) and facts["depth_quote_atomic"] == 10 ** 12


def test_two_exotic_quote_mints_with_no_priced_pool_refuse_ambiguous(exotic_listed):
    """MET and JUP cannot be compared without a price the learner does not fetch."""
    rpc = ChainRpc([_exotic_pool(9, 10 ** 15), _exotic_pool(8, 10 ** 15, quote=JUP), _sol_pool(1, LILMET_SOL)])
    assert _find(rpc) == (None, "exotic_ambiguous")


def test_a_quote_the_leg_could_not_move_is_never_a_candidate(exotic_listed):
    assert _find(ChainRpc([_exotic_pool(9, 10 ** 15, quote=PAUSED), _sol_pool(1, LILMET_SOL)])) == (
        None, "shallow")


def test_usdc_pools_are_candidates_only_when_non_sol_damm_is_listed(monkeypatch):
    usdc = (_key(5), COIN, USDC, _key(105), _key(155), {_key(155): 50_000 * 10 ** 6})
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", (), raising=False)
    assert _find(ChainRpc([usdc])) == (None, "no_sol_pool")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", ("meteora_damm_v2",), raising=False)
    facts, why = _find(ChainRpc([usdc]))
    assert why is None and facts["quote"] == USDC and facts["depth_usd"] == pytest.approx(50_000)


def test_no_pool_shallow_and_unknown_sol_price_each_refuse_with_their_reason():
    assert _find(ChainRpc([])) == (None, "no_pool")
    assert _find(ChainRpc([_sol_pool(1, 10 ** 9)])) == (None, "shallow")              # $150 < $2,500
    assert _find(ChainRpc([_sol_pool(1, 10 ** 12)]), sol_usd=None) == (None, "sol_usd_unknown")


@pytest.mark.parametrize("rpc", [ChainRpc([], fail="getProgramAccounts"),
                                 ChainRpc([_sol_pool(1, 10 ** 12)], fail="getMultipleAccounts"),
                                 ChainRpc([_sol_pool(1, 10 ** 12)], short=True)])
def test_a_failed_or_short_read_is_unknown_never_no_pool(rpc):
    assert _find(rpc) == (None, "unread")


def test_the_mint_learner_adopts_the_pool_into_the_cache_as_its_newest_entry():
    cache = D.DammV2PoolCache(rpc=None, store_cap=3)
    for i, coin in enumerate(("c1", "c2", "c3")):
        cache._remember(coin, {"learn_slot": 100 + i})
    learner = D.DammV2MintLearner(cache, ChainRpc([_sol_pool(1, 10 ** 12)]), sol_usd_fn=lambda: SOL_USD,
                                  min_depth_usd=2500.0)
    assert learner.learn(COIN) is True and cache.get(COIN)["pool"] == _key(1)
    cache._remember("c4", {"learn_slot": 104})            # over the cap: the OLDEST goes, never the adopted
    assert cache.get(COIN) is not None and cache.get("c1") is None
    assert learner.learn("UnknownCoin1111111111111111111111111111111") is False


def _routed(*, usdc=100_000_000):
    return s.RoutedBuy(source_user_id="w1", handle="yansanity", owner="Owner1", signature="sigDamm1",
                       slot=454268083, mint=COIN, token_delta_atomic=10_000_000 * 10 ** 6, token_decimals=6,
                       usdc_spent_atomic=usdc, recv_monotonic=time.monotonic(),
                       recv_utc="2026-10-07T15:50:34+00:00", paired_monotonic=time.monotonic(),
                       venue_events=(D.DammV2Seen(),))


class Learner:
    def __init__(self):
        self.offered = []

    def offer(self, buy):
        self.offered.append(buy)
        return True


def _route(monkeypatch, *, knob, learner, damm_mode="live"):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN", knob, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", damm_mode, raising=False)
    put, lines, handed = [], [], []
    cache = D.DammV2PoolCache(rpc=None)

    class Handoff:
        def write(self, routed):
            handed.append(routed.signature)
            return "written"
    decided = lane.route_routed_buy(_routed(), handoff=Handoff(), log=lines.append, put=put.append,
                                    amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True,
                                    learner=learner, damm_on=True, damm_pools=cache)
    return decided, put, handed, cache


def test_a_first_sight_damm_buy_goes_to_the_learner_not_the_hand_off(monkeypatch):
    learner = Learner()
    decided, put, handed, cache = _route(monkeypatch, knob=True, learner=learner)
    assert decided == ("learning", "pool_unknown") and handed == [] and put == []
    [miss] = learner.offered
    assert (miss.venue, miss.pool, miss.signature) == ("meteora_damm_v2", COIN, "sigDamm1")
    assert not lane.shadow_only(miss)                     # a hand-off from the learner writes the routed row
    assert miss.convert() == (None, "pool_unknown:pending")      # nothing adopted yet
    learner_obj = D.DammV2MintLearner(cache, ChainRpc([_sol_pool(1, 10 ** 12)]), sol_usd_fn=lambda: SOL_USD,
                                      min_depth_usd=2500.0)
    assert learner_obj.learn(COIN) is True
    buy, why = miss.convert()
    assert why is None and buy.venue == "meteora_damm_v2" and buy.pool == _key(1)


@pytest.mark.parametrize("knob,mode", [(False, "live"), (True, "shadow")])
def test_with_the_knob_off_or_damm_not_live_the_first_sight_is_handed_off_as_today(monkeypatch, knob, mode):
    learner = Learner()
    decided, put, handed, _ = _route(monkeypatch, knob=knob, learner=learner, damm_mode=mode)
    assert learner.offered == [] and decided[0] == "handoff"
    assert handed == (["sigDamm1"] if mode == "live" else handed)


def test_a_converted_buy_the_lane_could_only_shadow_is_handed_off(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "live", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", (), raising=False)
    cache = D.DammV2PoolCache(rpc=None)
    cache.adopt(COIN, {**_facts_for(USDC), "learn_slot": None})
    miss = lane.RoutedDammMiss(_routed(), cache)
    buy, why = miss.convert()
    assert buy is None and why == "damm_v2_shadow_quote"


def _facts_for(quote):
    state = D.parse_pool(base64.b64decode(CLASSIC["pool_account"]["data"][0]))
    facts = D.DammV2PoolCache._facts(CLASSIC["pool"], COIN, state, *CLASSIC["mints"], None)
    return {**facts, "quote": quote}


def test_the_mint_learner_refuses_a_url_sharing_the_funded_key(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_URL", "https://rpc.example/?api-key=FUNDED")
    with pytest.raises(ValueError):
        D.mint_learner_rpc("https://other.example/?api-key=FUNDED")
    assert callable(D.mint_learner_rpc("https://other.example/?api-key=WATCH"))


def test_the_lane_wires_the_first_sight_learner_only_when_on_live_and_safe(monkeypatch):
    lines, cache = [], D.DammV2PoolCache(rpc=None)
    env = {"ONCHAIN_WATCH_HELIUS_RPC_URL": "https://watch.example/?api-key=WATCH",
           "MEMECOIN_SOLANA_RPC_URL": "https://custody.example/?api-key=FUNDED"}
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "live", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN", False, raising=False)
    assert lane.damm_first_sight_learner(cache, sol_usd_fn=lambda: 1.0, log=lines.append, env=env) is None
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN", True, raising=False)
    assert lane.damm_first_sight_learner(None, sol_usd_fn=lambda: 1.0, log=lines.append, env=env) is None
    learner = lane.damm_first_sight_learner(cache, sol_usd_fn=lambda: 1.0, log=lines.append, env=env)
    assert isinstance(learner, D.DammV2MintLearner) and learner.pools is cache
    monkeypatch.setenv("MEMECOIN_SOLANA_RPC_URL", "https://custody.example/?api-key=FUNDED")
    shared = {**env, "ONCHAIN_WATCH_HELIUS_RPC_URL": "https://x.example/?api-key=FUNDED"}
    assert lane.damm_first_sight_learner(cache, sol_usd_fn=lambda: 1.0, log=lines.append, env=shared) is None
    assert any("first sight OFF" in l for l in lines)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "shadow", raising=False)
    assert lane.damm_first_sight_learner(cache, sol_usd_fn=lambda: 1.0, log=lines.append, env=env) is None


def test_the_miss_learner_runs_the_damm_venue_and_retries_the_buy_once(monkeypatch):
    """End to end on the production MissLearner: offer -> its DAMM thread learns by mint -> convert."""
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "live", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN", True, raising=False)
    cache = D.DammV2PoolCache(rpc=None)
    mint_learner = D.DammV2MintLearner(cache, ChainRpc([_sol_pool(1, 10 ** 12)]), sol_usd_fn=lambda: SOL_USD,
                                       min_depth_usd=2500.0)
    put, handed, lines = [], [], []
    ml = lane.MissLearner(pools={"pump_amm": object(), "meteora_damm_v2": mint_learner}, put=put.append,
                          handoff_fn=lambda b, r: handed.append(r), log=lines.append, budget_s=2.0)
    assert ml.offer(lane.RoutedDammMiss(_routed(), cache)) is True
    ml.learn_once(COIN)
    assert handed == [] and len(put) == 1 and put[0].pool == _key(1)
    assert any("-> retry" in l for l in lines)


# --- the exotic pool after first sight: floor, routing, leg reads (operator order 2026-10-08) -----

def _learned_lilmet(monkeypatch, cache=None, lines=None):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "live", raising=False)
    cache = cache if cache is not None else D.DammV2PoolCache(rpc=None)
    learner = D.DammV2MintLearner(cache, ChainRpc(LILMET), sol_usd_fn=lambda: SOL_USD, min_depth_usd=2500.0,
                                  log=None if lines is None else lines.append)
    assert learner.learn(COIN) is True
    return cache


def test_an_unpriced_exotic_pool_meets_the_floor_only_once_its_leg_prices_the_quote(exotic_listed,
                                                                                    monkeypatch):
    lines = []
    cache = _learned_lilmet(monkeypatch, lines=lines)
    assert any("quote=METvsvVR depth=unpriced quote_atomic=%d" % LILMET_MET in l for l in lines)
    buy, why = lane.RoutedDammMiss(_routed(), cache).convert()
    assert why is None and buy.quote_mint == MET and buy.pool == _key(9)
    assert D.market_facts(buy, cache, SOL_USD)[0] == "slot_lane_damm_v2_quote_not_supported"   # the cue
    assert D.market_facts(buy, cache, SOL_USD, quote=(6, 0.52))[0] is None          # $29,102 >= $2,500
    assert D.market_facts(buy, cache, SOL_USD, quote=(6, 0.04))[0] == "slot_lane_damm_v2_shallow"  # $2,239
    assert D.market_facts(buy, cache, SOL_USD, quote=(6, 0.0))[0] == "slot_lane_damm_v2_shallow"


def test_a_pool_learned_from_the_source_transaction_has_no_floor(exotic_listed, monkeypatch):
    cache = _learned_lilmet(monkeypatch)
    exact = {k: v for k, v in cache.get(COIN).items() if k not in ("depth_quote_atomic", "depth_usd")}
    cache._remember(COIN, exact)                          # the cache's own learner, the exact pool
    buy, _ = lane.RoutedDammMiss(_routed(), cache).convert()
    assert D.market_facts(buy, cache, SOL_USD, quote=(6, 0.0001))[0] is None


def test_a_learned_met_pool_retries_into_the_exotic_leg_like_a_known_pool(exotic_listed, monkeypatch):
    """End to end on the production MissLearner and SlotLane: learn by mint -> retry -> exotic hand-off."""
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2", "live", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DAMM_V2_MISS_LEARN", True, raising=False)
    monkeypatch.setattr(lane, "stale_input", lambda snap, now: None)
    cache = D.DammV2PoolCache(rpc=None)
    mint_learner = D.DammV2MintLearner(cache, ChainRpc(LILMET), sol_usd_fn=lambda: SOL_USD, min_depth_usd=2500.0)
    put, handed, lines = [], [], []
    ml = lane.MissLearner(pools={"pump_amm": object(), "meteora_damm_v2": mint_learner}, put=put.append,
                          handoff_fn=lambda b, r: handed.append(r), log=lines.append, budget_s=2.0)
    assert ml.offer(lane.RoutedDammMiss(_routed(), cache)) is True
    ml.learn_once(COIN)
    assert handed == [] and len(put) == 1 and put[0].quote_mint == MET
    offered, legs = [], lane.PendingLegs()
    sl = lane.SlotLane(conn=None, executor={}, build_fn=None,
                       snapshot_fn=lambda: types.SimpleNamespace(sol_usd=SOL_USD), armed_ids_fn=lambda: {"w1"},
                       kill_tripped_fn=lambda: False, now_iso_fn=None, lock_key="k", mode="live",
                       damm_pools=cache, exotic_offer=lambda b, **k: offered.append(b) or True,
                       pending_legs=legs)
    result = sl.handle(put[0])
    assert result["reason"] == lane.EXOTIC_PENDING_REASON and offered == put
    assert legs.holds("sigDamm1", now=time.monotonic())


USER = str(Pubkey.from_bytes(bytes([77]) * 32))
ALT = str(Pubkey.from_bytes(bytes([78]) * 32))


def _damm_worker(monkeypatch, custody, *, alts=(), out=10 ** 9):
    """The live on-demand worker on a DAMM builder whose pool cache learned LILMET. Its learner's
    paced pool_rpc fails the test if the leg reads through it."""
    import sniper.execution.pump_curve_buy as P
    from sniper.execution import raydium_launchlab_buy as LL
    paced = []
    b = D.SlotLaneDammV2Builder(user=USER, rpc=custody, leg_fn=None, start_learner=False,
                                pool_rpc=lambda method, params: paced.append(method))
    _learned_lilmet(monkeypatch, cache=b.pools)

    def funded(method, path, body=None, *, http=None):
        if path.startswith("/quote"):
            return 200, {"outAmount": str(out), "otherAmountThreshold": str(out * 985 // 1000),
                         "routePlan": [{"swapInfo": {"label": "Meteora", "ammKey": "QuotePool1"}}]}
        return 200, {"swapInstruction": {"programId": "11111111111111111111111111111111", "data": "AA==",
                                         "accounts": [{"pubkey": USER, "isSigner": True, "isWritable": True}]},
                     "setupInstructions": [], "cleanupInstruction": None,
                     "addressLookupTableAddresses": list(alts)}
    monkeypatch.setattr(P, "_funded_jup_call", funded)
    requeued, refused, done = [], [], threading.Event()
    w = LL.OnDemandLegWorker(
        builders={"meteora_damm_v2": b}, admission_fn=lambda buy, q: None,
        snapshot_fn=lambda: types.SimpleNamespace(sol_usd=SOL_USD), clip_fn=lambda: 175.0,
        offer_fn=lambda buy, r: done.set(), start=True, live_fn=lambda venue: True,
        requeue_fn=requeued.append, refuse_fn=lambda buy, reason: (refused.append(reason), done.set()),
        cap_open_fn=lambda venue: True)
    buy, _ = lane.RoutedDammMiss(_routed(), b.pools).convert()
    return w, buy, b, paced, requeued, refused, done


def test_the_leg_reads_on_the_custody_rpc_never_the_learners_paced_one(exotic_listed, monkeypatch):
    from sniper.execution import raydium_launchlab_buy as LL
    reads = []
    w, buy, b, paced, requeued, refused, done = _damm_worker(monkeypatch, lambda m, p: reads.append(m))
    assert LL._read_rpc(b) is b.rpc and LL._read_rpc(b) is not b.pools.rpc
    assert w.offer(buy) and done.wait(5.0)
    # First sight read the MET mint already: the leg makes no read at all (no ALTs on this route).
    assert refused == [] and len(requeued) == 1 and reads == [] and paced == []
    assert requeued[0].ondemand["quote_mint"] == MET


@pytest.mark.parametrize("stale", [False, True])
def test_a_custody_failure_during_the_leg_refuses_mechanically_and_never_hangs(exotic_listed, monkeypatch,
                                                                               stale):
    """Alpha 2026-10-08: the read fails (an ALT read on a fresh learn; the quote-mint re-read on a stale
    one); the worker refuses, the reason keeps the routed fallback, and the lane thread resolves it."""
    def custody(method, params):
        raise TimeoutError("custody read timed out")
    w, buy, b, paced, requeued, refused, done = _damm_worker(monkeypatch, custody, alts=(ALT,))
    if stale:
        facts = b.pools.get(COIN)
        b.pools._remember(COIN, {**facts, "quote_read_at": time.monotonic() - 10_000.0})
    assert w.offer(buy) and done.wait(5.0)
    assert requeued == [] and paced == [] and len(refused) == 1
    assert refused[0] == ("slot_lane_damm_v2_quote_unread" if stale else "slot_lane_exotic_worker_error")
    assert not lane.policy_refusal(refused[0])
    rows, legs = [], lane.PendingLegs()

    class Routed:
        def write(self, routed):
            rows.append(routed.signature)
            return "written"
    sl = lane.SlotLane(conn=None, executor={}, build_fn=None, snapshot_fn=lambda: None,
                       armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False, now_iso_fn=None,
                       lock_key="k", mode="live", routed_handoff=Routed(), pending_legs=legs)
    legs.add(buy, deadline=time.monotonic() + 30)
    assert sl.resolve_exotic_refusal(lane.ExoticRefusal(buy, refused[0])) is True and rows == ["sigDamm1"]


def test_the_dbc_builder_names_its_custody_read_rpc_for_the_worker():
    """It keeps no pool cache: before this every cold DBC exotic leg raised AttributeError."""
    from sniper.execution import raydium_launchlab_buy as LL
    from sniper.execution.meteora_dbc_buy import SlotLaneDbcBuilder

    def custody(method, params):
        return None
    assert LL._read_rpc(SlotLaneDbcBuilder(user=USER, rpc=custody, leg_fn=None)) is custody



def test_spam_exotic_pools_never_push_a_priced_pool_out_of_the_candidate_cap(exotic_listed):
    """Gamma MEDIUM on 3273f1db8: 31 MET/JUP pools ahead of a $3,000 SOL pool in the program-account
    order filled the 30-candidate cap; the SOL pool, the only one before, was cut and the coin refused
    exotic_ambiguous. Priced candidates go first."""
    spam = [_exotic_pool(10 + i, 10 ** 12, quote=(MET, JUP)[i % 2]) for i in range(31)]
    facts, why = _find(ChainRpc(spam + [_sol_pool(1, 20 * 10 ** 9)]))
    assert why is None and facts["pool"] == _key(1) and facts["depth_usd"] == pytest.approx(3000.0)


@pytest.mark.parametrize("quote", [(6, float("nan")), (6, None), ("x", 0.52), (6, float("inf")) ])
def test_an_unpriceable_leg_quote_refuses_the_unpriced_pool(exotic_listed, monkeypatch, quote):
    """Gamma LOW: NaN, None and garbage decimals must refuse, never pass the floor. inf is not a price."""
    cache = _learned_lilmet(monkeypatch)
    buy, _ = lane.RoutedDammMiss(_routed(), cache).convert()
    assert D.market_facts(buy, cache, SOL_USD, quote=quote)[0] == "slot_lane_damm_v2_shallow"
