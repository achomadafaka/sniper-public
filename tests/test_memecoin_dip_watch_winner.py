"""Dip watch rebuild (operator order 86d053ce, spec 91d9d3dff, Alpha's ruling 2026-09-30).

A refused EVM buy is watched as if we held it: executable quotes on the route that priced the
refusal, never a display feed and never the research host. After tick 0 (the refusal's own quote)
the watch re-quotes the winning rung only: the pinned pool for a V4 single hop, the pinned path for
a V4 multihop, Kyber's routes call for a Kyber route, `only_rung` for the rest. Two winner misses
lead to one full-ladder tick without the two V4 rungs (their discovery reads DexScreener and
GeckoTerminal pool lists). An aggregator winner and a ladder tick are also priced on the V4 pools
whose key we already hold, and the better output counts (Gamma M1/M2 on b2e2d2fb6). The premium is a
ratio of token outputs at the refusal's own input, so no tick reads a quote-token USD price.

No network. Every quote function here is a fake.
"""
import inspect
import json
import sqlite3
from dataclasses import asdict

import pytest

from sniper.execution import evm_swap_executor as X
from sniper.feeds.fomo_feed import CopyEvent
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_dip_retry as D
from sniper.services import memecoin_real_runner as R

DEGEN = "fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb"
SIGNAL = "2026-09-30T12:00:00.000Z"
T0 = 1790769600.0
MINT = "0x0401324674a0a3df33f1fc23bfcb04c13fe39fbc"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
SELL, BUY0 = 10 ** 16, 1000
POOL = "0x" + "3954914e" * 8
NO_V4 = ("v4", "v4_multihop")
LEGS = [{"currency": "0x" + "aa" * 20,
         "key": {"currency0": "0x" + "00" * 20, "currency1": "0x" + "aa" * 20, "fee": 3000,
                 "tick_spacing": 60, "hooks": "0x" + "00" * 20}},
        {"currency": MINT,
         "key": {"currency0": MINT, "currency1": "0x" + "aa" * 20, "fee": 10000,
                 "tick_spacing": 200, "hooks": "0x" + "00" * 20}}]


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_S", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_WALLETS", ("degencapitalllc",))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana", "4663"))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_MAX_HITS", 3)
    return monkeypatch


def _conn(tmp_path=None):
    c = sqlite3.connect(str(tmp_path / "ledger.db") if tmp_path else ":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _ev(fid="x1", mint=MINT):
    return CopyEvent(
        source_user_id=DEGEN, source_handle="DegenCapitalLLC", mint=mint, ticker="", side="buy",
        their_price=1e-5, their_market_cap=50000.0, their_size_usd=500.0, is_first_buy=True,
        fomo_event_id=fid, created_at=SIGNAL, received_at=SIGNAL, lag_s=0.5,
        signal_source="follow_activity", network_id="4663")


def _refusal(conn, ev, *, rung="pons_v2", legs_path=None, route=True, pool_id=POOL):
    """A 4663 source-basis premium refusal at 1.60x against a 1.50x cap (refused premium 60%)."""
    aid = "A-" + ev.fomo_event_id
    pid = m.reserve_pending(conn, mint=ev.mint, source_user_id=ev.source_user_id,
                            strategy="alpha_b", notional=29.62,
                            reserved_at="2026-09-30T12:00:01+00:00", attempt_id=aid,
                            network_id="4663")
    conn.execute("INSERT INTO memecoin_entry_attempts (attempt_id, mint, network_id, pending_id, "
                 "decision_started_at) VALUES (?,?,?,?,?)",
                 (aid, ev.mint, "4663", pid, "2026-09-30T12:00:01+00:00"))
    m.release_pending(conn, pid, "execution_premium")
    res = {"status": "skipped_chase", "attempt_id": aid,
           "reason": "route quote is 1.60x the source price (cap 1.50x)",
           "quote_price": 6.4e-5, "source_drift_pct": 60.0}
    if route:
        res["dip_route"] = {"rung": rung, "sell_atomic": str(SELL), "buy_atomic": str(BUY0),
                            "pool_id": pool_id if rung == "v4" else None, "legs_path": legs_path}
    return res


def _seed(tmp_path, n=1, **kw):
    c = _conn(tmp_path)
    for i in range(n):
        ev = _ev(fid="x%d" % (i + 1), mint=MINT[:-1] + str(i))
        assert D.maybe_enqueue(c, ev, _refusal(c, ev, **kw), "2026-09-30T12:00:01+00:00",
                               event_json=json.dumps(asdict(ev)))
    c.commit()
    return c


class _Ctx(dict):
    """A chain context holding only what the watch may use. Anything else is a KeyError."""

    def __init__(self, answers, **extra):
        super().__init__(quote_token=WETH, rpc_url="https://rpc", http=None, **extra)
        self.answers, self.calls = list(answers), []
        self["venue_quote_with_miss_fn"] = self._quote

    def _quote(self, sell_token, buy_token, amount_atomic, **kw):
        self.calls.append((sell_token, buy_token, amount_atomic, kw))
        got = self.answers.pop(0)
        if isinstance(got, Exception):
            raise got
        if isinstance(got, str):
            return None, got
        if got is None:
            return None, "no_route"
        rung = kw.get("only_rung") or "v3"
        return {"buyAmount": str(got), "sellAmount": str(amount_atomic), "_sniper_winner": rung}, None


class _Pinned:
    """`evm_v4_quote.pinned_buy_out`, faked: the V4 pools whose key we already hold."""

    def __init__(self, monkeypatch, answers=()):
        from sniper.execution import evm_v4_quote as v4
        self.answers, self.calls = list(answers), []
        monkeypatch.setattr(v4, "pinned_buy_out", self)

    def __call__(self, chain, mint, amount, rpc_url, *, pool_ids=None, http=None):
        self.calls.append(pool_ids)
        out = self.answers.pop(0) if self.answers else None
        if isinstance(out, Exception):
            raise out
        return (out, (pool_ids or [POOL])[0]) if out else (None, None)


@pytest.fixture
def pinned(monkeypatch):
    return _Pinned(monkeypatch)


def _watch(tmp_path, ctx, *, now=T0 + 5, wake=None):
    clock = now if callable(now) else (lambda: now)
    return D.EvmDipWatch(str(tmp_path / "ledger.db"), "4663",
                         contexts_fn=lambda: {"4663": ctx} if ctx is not None else {},
                         wake_fn=wake, clock=clock)


# --- the refusal carries its route as plain data ---

def _quote(**over):
    q = {"sellAmount": str(SELL), "buyAmount": str(BUY0), "_sniper_winner": "v4_multihop",
         "_requote": lambda **k: None,
         "route": {"protocol": "uniswap_v4_multihop", "legs_path": LEGS, "pool_id": POOL}}
    q.update(over)
    return q


def test_the_dip_route_is_plain_json_and_names_the_winner_and_its_path():
    route = X._dip_route(_quote())
    assert route == {"rung": "v4_multihop", "sell_atomic": str(SELL), "buy_atomic": str(BUY0),
                     "pool_id": POOL, "legs_path": LEGS}
    assert json.loads(json.dumps(route)) == route           # no callable, nothing unserializable


@pytest.mark.parametrize("over", [{"buyAmount": None}, {"sellAmount": "x"},
                                  {"route": {"legs_path": [{"key": b"\x00"}]}}])
def test_a_quote_without_usable_amounts_or_with_an_unserializable_path_has_no_dip_route(over):
    assert X._dip_route(_quote(**over)) is None
    assert X._dip_route(None) is None


def test_both_premium_refusals_carry_the_dip_route():
    src = inspect.getsource(X.execute_entry_evm)
    assert src.count('"dip_route": _dip_route(quote)') == 2
    assert src.count("premium_refusal = _premium_refusal(premium)") == 2


def test_enqueue_stores_the_route_with_the_row(tmp_path):
    c = _seed(tmp_path, rung="v4_multihop", legs_path=LEGS)
    [row] = m.dip_retry_rows(c)
    dip = json.loads(row["dip_json"])
    assert dip["dip_route"] == {"rung": "v4_multihop", "sell_atomic": str(SELL),
                                "buy_atomic": str(BUY0), "pool_id": None, "legs_path": LEGS}
    assert dip["refused_premium_pct"] == 60.0 and "route_kinds" not in dip


# --- winner ticks ---

def test_an_on_chain_winner_is_re_quoted_on_its_rung_only_and_the_premium_is_a_token_ratio(
        tmp_path, pinned):
    _seed(tmp_path)
    ctx = _Ctx([1050, 1100])                              # 1.6*1000/1050 = +52.4%, /1100 = +45.5%
    woke = []
    w = _watch(tmp_path, ctx, wake=lambda: woke.append(1))
    w.tick()
    assert w.take_due() == set() and woke == []
    w.tick()
    assert w.take_due() == {"x1"} and woke == [1]
    assert ctx.calls == [(WETH, MINT[:-1] + "0", SELL, {"only_rung": "pons_v2"})] * 2
    assert w.premium["x1"] == pytest.approx((1.6 * 1000 / 1100 - 1) * 100)
    assert pinned.calls == []                             # an on-chain winner needs no second look


def test_a_v4_winner_is_quoted_on_its_own_pinned_pool_never_through_the_rung(tmp_path, monkeypatch):
    """Gamma M1: `only_rung="v4"` rediscovers pools through the display providers' pool lists."""
    _seed(tmp_path, rung="v4")
    pinned = _Pinned(monkeypatch, [1050, 1100])
    ctx = _Ctx([])
    w = _watch(tmp_path, ctx)
    w.tick()
    w.tick()
    assert w.take_due() == {"x1"} and ctx.calls == []
    assert pinned.calls == [[POOL], [POOL]]
    assert w.stats_for("x1")["series"][-1]["rung"] == "v4"


def test_a_v4_winner_whose_pool_cannot_be_quoted_falls_to_the_ladder_without_the_v4_rungs(
        tmp_path, monkeypatch):
    _seed(tmp_path, rung="v4")
    pinned = _Pinned(monkeypatch, [None, None, None])
    ctx = _Ctx([1100])
    w = _watch(tmp_path, ctx)
    for _ in range(3):
        w.tick()
    assert [call[3] for call in ctx.calls] == [{"exclude_rungs": NO_V4}]
    assert pinned.calls == [[POOL], [POOL], None]          # the ladder tick also asks the held pools
    assert w.take_due() == {"x1"}


def test_a_multihop_winner_is_quoted_on_its_pinned_path_with_no_discovery(tmp_path, monkeypatch, pinned):
    _seed(tmp_path, rung="v4_multihop", legs_path=LEGS)
    seen = []
    from sniper.execution import evm_v4_multihop as mh
    monkeypatch.setattr(mh, "_quote_path", lambda chain, cash, path, amount, rpc_url, **kw: (
        seen.append((chain, cash, [(c, k["fee"]) for c, k in path], amount, rpc_url)) or 1100))
    for name in ("_pools_with_depth", "_provider_pools", "multihop_quote"):
        monkeypatch.setattr(mh, name, lambda *a, **k: pytest.fail("%s: discovery in the watch" % name))
    ctx = _Ctx([])
    w = _watch(tmp_path, ctx)
    w.tick()
    assert w.take_due() == {"x1"} and ctx.calls == []
    assert seen == [("4663", mh._multihop_cash("4663"),
                     [(LEGS[0]["currency"], 3000), (MINT, 10000)], SELL, "https://rpc")]


def _kyber(monkeypatch, outs):
    from sniper.execution import evm_kyber_quote as ky
    seen, outs = [], list(outs)

    def routes(sell, buy, amount, **kw):
        seen.append((sell, buy, amount, kw.get("chain")))
        out = outs.pop(0)
        if isinstance(out, Exception):
            raise out
        return {"amountOut": str(out)} if out else None
    monkeypatch.setattr(ky, "_routes", routes)
    monkeypatch.setattr(ky, "_http_json", lambda *a, **k: pytest.fail("kyber build in the watch"))
    return seen


def test_a_kyber_winner_is_priced_on_the_routes_call_and_on_the_pools_we_hold(tmp_path, monkeypatch):
    """Gamma M2 (HOOKERS): Kyber read 1.82x while the pool was at 1.46x, and Kyber never misses,
    so the ladder tick never ran. The held V4 pools are quoted beside it and the better output
    counts. The first pool that answers is pinned."""
    _seed(tmp_path, rung="kyber")
    seen = _kyber(monkeypatch, [1010, 1010, 1010])        # stale: +58.4%, over the 50% cap
    pinned = _Pinned(monkeypatch, [None, 1100, 1100])     # key not warm yet, then the pool: +45.5%
    ctx = _Ctx([])
    t = {"now": T0 + 5}
    w = _watch(tmp_path, ctx, now=lambda: t["now"])
    w.tick()
    assert w.take_due() == set()
    t["now"] += D.HELD_RESCAN_S                           # an empty scan waits before it repeats
    w.tick()
    assert w.take_due() == {"x1"} and ctx.calls == []
    w.tick()
    assert seen == [(WETH, MINT[:-1] + "0", SELL, "4663")] * 3
    assert pinned.calls == [None, None, [POOL]]            # stored pools until one answers
    series = w.stats_for("x1")["series"]
    assert [tick["rung"] for tick in series[1:]] == ["kyber", "v4_held", "v4_held"]
    assert series[2]["buy_atomic"] == "1100"
    assert w.stats_for("x1")["unknown"] == 0              # the aggregator answered every tick


def test_a_relay_winner_is_priced_on_its_rung_and_on_the_pools_we_hold(tmp_path, monkeypatch):
    _seed(tmp_path, rung="relay")
    pinned = _Pinned(monkeypatch, [1100])
    ctx = _Ctx([1010])
    w = _watch(tmp_path, ctx)
    w.tick()
    assert [call[3] for call in ctx.calls] == [{"only_rung": "relay"}]
    assert pinned.calls == [None] and w.take_due() == {"x1"}


def test_a_failing_held_pool_quote_never_costs_the_aggregator_tick(tmp_path, monkeypatch):
    _seed(tmp_path, rung="kyber")
    _kyber(monkeypatch, [1100])
    _Pinned(monkeypatch, [RuntimeError("store busy")])
    w = _watch(tmp_path, _Ctx([]))
    w.tick()
    assert w.take_due() == {"x1"}


def test_two_winner_misses_lead_to_one_ladder_tick_without_the_v4_rungs_which_adopts_its_winner(
        tmp_path, pinned):
    _seed(tmp_path)
    ctx = _Ctx([None, "provider_rpc_transport", 1040, 1045, 1100])
    w = _watch(tmp_path, ctx)
    for _ in range(5):
        w.tick()
    kws = [call[3] for call in ctx.calls]
    assert kws == [{"only_rung": "pons_v2"}, {"only_rung": "pons_v2"},
                   {"exclude_rungs": NO_V4},                       # the one ladder tick
                   {"only_rung": "v3"}, {"only_rung": "v3"}]       # its winner, adopted
    assert w.take_due() == {"x1"}
    s = w.stats_for("x1")
    assert (s["quotes"], s["unknown"]) == (5, 2)
    assert pinned.calls == [None]                          # held pools: on the ladder tick only


def test_the_watch_keeps_the_lowest_premium_it_saw(tmp_path, pinned):
    _seed(tmp_path)
    w = _watch(tmp_path, _Ctx([1040, 1050, None, 1045]))
    for _ in range(4):
        w.tick()
    s = w.stats_for("x1")
    assert (s["quotes"], s["unknown"]) == (4, 1)
    assert s["min_premium_pct"] == pytest.approx((1.6 * 1000 / 1050 - 1) * 100)


def test_a_refusal_that_did_not_name_its_rung_is_watched_on_the_ladder(tmp_path, pinned):
    _seed(tmp_path, rung=None)
    ctx = _Ctx([None, None])
    w = _watch(tmp_path, ctx)
    w.tick()
    w.tick()
    assert [call[3] for call in ctx.calls] == [{"exclude_rungs": NO_V4}] * 2


def test_a_multihop_refusal_without_a_path_never_runs_the_rung(tmp_path, pinned):
    _seed(tmp_path, rung="v4_multihop", legs_path=None)
    ctx = _Ctx([None, 1100])
    w = _watch(tmp_path, ctx)
    w.tick()
    w.tick()
    assert [call[3] for call in ctx.calls] == [{"exclude_rungs": NO_V4}] * 2
    assert w.take_due() == {"x1"}


def test_a_row_without_the_refusal_quote_is_unknown_and_costs_no_quote(tmp_path, pinned):
    _seed(tmp_path, route=False)
    ctx = _Ctx([])
    w = _watch(tmp_path, ctx)
    w.tick()
    assert ctx.calls == [] and pinned.calls == [] and w.take_due() == set()
    assert w.stats_for("x1")["unknown"] == 1


@pytest.mark.parametrize("ctx", [None, _Ctx([], recovery_only=True)])
def test_no_entry_context_prices_nothing(tmp_path, ctx, pinned):
    _seed(tmp_path)
    w = _watch(tmp_path, ctx)
    w.tick()
    assert w.take_due() == set() and (ctx is None or ctx.calls == []) and pinned.calls == []
    assert w.stats_for("x1")["series"][-1]["miss"] == "no_entry_context"


def test_a_rate_limit_pauses_the_chain_for_60_s(tmp_path, _cfg, pinned):
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_S", 300.0)          # the row outlives the pause
    _seed(tmp_path)
    t = {"now": T0 + 5}
    ctx = _Ctx(["provider_rate_limit", 1100])
    w = _watch(tmp_path, ctx, now=lambda: t["now"])
    w.tick()
    t["now"] += D.PAUSE_AFTER_COOLING_S - 1
    w.tick()
    assert len(ctx.calls) == 1
    t["now"] += 2
    w.tick()
    assert len(ctx.calls) == 2 and w.take_due() == {"x1"}


def test_a_kyber_rate_limit_pauses_the_chain_too(tmp_path, monkeypatch, _cfg):
    import urllib.error
    _seed(tmp_path, rung="kyber")
    seen = _kyber(monkeypatch, [urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None), 1100])
    _Pinned(monkeypatch)
    w = _watch(tmp_path, _Ctx([]))
    w.tick()
    w.tick()
    assert len(seen) == 1 and w.take_due() == set()
    assert w.stats_for("x1")["series"][-1]["miss"] == "provider_rate_limit"


def test_a_raising_quote_is_a_typed_miss_never_a_price(tmp_path, pinned):
    _seed(tmp_path)
    ctx = _Ctx([RuntimeError("boom")])
    w = _watch(tmp_path, ctx)
    w.tick()
    assert w.take_due() == set()
    assert w.stats_for("x1")["series"][-1]["miss"] == "RuntimeError"


def test_a_zero_output_is_unknown(tmp_path, pinned):
    _seed(tmp_path)
    w = _watch(tmp_path, _Ctx([0]))
    w.tick()
    assert w.take_due() == set() and w.stats_for("x1")["unknown"] == 1


def test_at_most_max_watched_rows_are_quoted(tmp_path, _cfg, pinned):
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_MAX_WATCHED", 2)
    _seed(tmp_path, n=3)
    ctx = _Ctx([1050] * 6)
    w = _watch(tmp_path, ctx)
    for _ in range(6):
        w.tick()
    assert len({call[1] for call in ctx.calls}) == 2


def test_the_series_starts_at_the_refusal_and_keeps_every_tick_up_to_the_cap(
        tmp_path, monkeypatch, pinned):
    _seed(tmp_path)
    monkeypatch.setattr(D, "SERIES_MAX", 3)
    w = _watch(tmp_path, _Ctx([1050, None, 1060, 1070]))
    for _ in range(4):
        w.tick()
    series = w.stats_for("x1")["series"]
    assert len(series) == 3
    assert series[0] == {"t": 1.0, "premium_pct": 60.0, "rung": "pons_v2", "buy_atomic": str(BUY0),
                         "miss": None}                     # tick 0: the refusal's own quote
    assert series[1]["buy_atomic"] == "1050" and series[1]["t"] == 5.0
    assert series[1]["premium_pct"] == pytest.approx((1.6 * 1000 / 1050 - 1) * 100)
    assert (series[2]["premium_pct"], series[2]["buy_atomic"], series[2]["miss"]) == (
        None, None, "no_route")


# --- no display feed is reachable from a watch tick (operator order 86d053ce, Gamma M1) ---

V4_KEY = {"currency0": "0x" + "00" * 20, "currency1": MINT, "fee": 3000, "tick_spacing": 60,
          "hooks": "0x" + "00" * 20}


@pytest.fixture
def no_display_feed(monkeypatch):
    """Every pool-list read of the display providers fails the test."""
    from sniper.execution import evm_v4_multihop as mh
    from sniper.execution import evm_v4_quote as v4
    for mod, name in ((v4, "_pools_for_mint"), (v4, "_quote_pools_for_mint"),
                      (v4, "_gt_pools_ranked"), (v4, "_ds_pools_ranked"),
                      (mh, "_pools_with_depth")):
        monkeypatch.setattr(mod, name, lambda *a, _n=name, **k: pytest.fail(
            "%s: a display provider's pool list was read on the watch thread" % _n))
    return v4


def test_the_pinned_v4_quote_uses_only_keys_we_hold(monkeypatch, no_display_feed):
    v4 = no_display_feed
    quoted = []
    monkeypatch.setattr(v4, "_pool_key_cached", lambda chain, pid: V4_KEY if pid == POOL else None)
    monkeypatch.setattr(v4, "_quote_single", lambda chain, key, zf1, amount, rpc_url, **kw: (
        quoted.append((zf1, amount, kw.get("raise_blind"))) or 1100))
    assert v4.pinned_buy_out("4663", MINT, SELL, "https://rpc", pool_ids=[POOL]) == (1100, POOL)
    assert quoted == [(True, SELL, True)]                 # native is currency0: a buy is 0 -> 1
    assert v4.pinned_buy_out("4663", MINT, SELL, "https://rpc", pool_ids=["0x" + "77" * 32]) == (
        None, None)                                        # a key we do not hold is never read
    assert v4.pinned_buy_out("4663", MINT, 0, "https://rpc", pool_ids=[POOL]) == (None, None)


def test_without_a_pinned_pool_the_held_pools_of_the_mint_are_quoted_and_the_best_wins(
        monkeypatch, no_display_feed):
    v4 = no_display_feed
    other = "0x" + "aa" * 20
    keys = {"0x" + "01" * 32: V4_KEY,
            "0x" + "02" * 32: dict(V4_KEY, fee=10000),
            "0x" + "03" * 32: dict(V4_KEY, fee=910600),                        # a fee trap
            "0x" + "04" * 32: dict(V4_KEY, currency0=other)}                   # a foreign pair
    outs = {3000: 1100, 10000: 1150}
    monkeypatch.setattr(v4, "_stored_pools_for_mint", lambda chain, mint: sorted(keys))
    monkeypatch.setattr(v4, "_pool_key_cached", lambda chain, pid: keys.get(pid))
    monkeypatch.setattr(v4, "_quote_single",
                        lambda chain, key, zf1, amount, rpc_url, **kw: outs[key["fee"]])
    assert v4.pinned_buy_out("4663", MINT, SELL, "https://rpc") == (1150, "0x" + "02" * 32)


def test_the_watch_ladder_never_reaches_the_v4_rungs(monkeypatch, no_display_feed):
    """The real ladder with its real V4 rungs: the watch's filter drops both before they are asked.
    The control asks the same ladder without the filter and the pool list IS read."""
    from sniper.execution import evm_real_seams as S
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {"4663"})
    rungs = []
    ladder = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain="4663", quote_token=WETH, taker="0x" + "11" * 20,
        v2_quote_fn=lambda *a, **k: None, acct=object(), rpc_url="https://rpc",
        on_rung=rungs.append)
    assert ladder(WETH, MINT, SELL, exclude_rungs=D.WATCH_EXCLUDED_RUNGS) is None
    assert not {"v4", "v4_multihop"} & {r["rung"] for r in rungs}
    assert D.WATCH_EXCLUDED_RUNGS == NO_V4
    with pytest.raises(BaseException, match="pool list was read"):      # the control
        ladder(WETH, MINT, SELL, only_rung="v4")
        raise AssertionError("the control never reached the rung's discovery")


def test_the_watch_thread_never_reads_a_usd_price_or_the_research_lane():
    src = inspect.getsource(D)
    for banned in ("warm_quote_price_fn", "quote_usd_price", "ExactLane", "dexscreener",
                   "buy_price_usd", "entry_quote_probe_fn"):
        assert banned not in src.replace("DexScreener", ""), banned


def test_the_evm_watch_has_its_own_poll_floor(_cfg):
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S == 1.0          # Gamma M3: not faster until measured
    assert cfg.MEMECOIN_REAL_DIP_RETRY_MAX_WATCHED == 2
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S", 0.4)
    assert D.EvmDipWatch("x", "4663", contexts_fn=dict)._poll_s() == 0.4
    assert D.DipWatch("x")._poll_s() == cfg.MEMECOIN_REAL_DIP_RETRY_POLL_S


# --- one watch thread per chain, hits on their own woken thread ---

def test_ensure_watch_starts_one_evm_thread_per_chain_with_the_contexts(tmp_path, monkeypatch, _cfg):
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana", "4663", "8453"))
    started = []
    monkeypatch.setattr(D, "_WATCH", None)
    monkeypatch.setattr(D, "_EVM_WATCHES", {})
    monkeypatch.setattr(D.DipWatch, "start", lambda self: started.append(self.name))
    contexts = {"4663": {"x": 1}}
    wake = lambda: None                                                        # noqa: E731
    D.ensure_watch(str(tmp_path / "ledger.db"), contexts=contexts, wake=wake)
    D.ensure_watch(str(tmp_path / "ledger.db"), contexts=contexts, wake=wake)
    assert started == ["memecoin-dip-watch", "memecoin-dip-watch-4663", "memecoin-dip-watch-8453"]
    w = D._EVM_WATCHES["4663"]
    assert w.daemon and w.contexts_fn() is contexts and w.wake_fn is wake and D._WATCH.wake_fn is wake


def test_without_contexts_no_evm_thread_starts(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(D, "_WATCH", None)
    monkeypatch.setattr(D, "_EVM_WATCHES", {})
    monkeypatch.setattr(D.DipWatch, "start", lambda self: started.append(self.name))
    D.ensure_watch(str(tmp_path / "ledger.db"))
    assert started == ["memecoin-dip-watch"]


NOW = "2026-09-30T12:00:05+00:00"


def _pass_counter(monkeypatch):
    ran = []
    monkeypatch.setattr(D, "run_hits", lambda conn, attempt, now, **k: ran.append(1) or {})
    monkeypatch.setattr(D, "_LANE_SEEN", None)
    return ran


def test_the_main_loop_hosts_the_hits_only_while_the_lane_is_not_passing(monkeypatch):
    ran = _pass_counter(monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(D, "_monotonic", lambda: clock["t"])
    R._run_dip_hits(None, None, NOW)                          # the lane never started: main runs
    assert ran == [1]
    R._run_dip_hits(None, None, NOW, host="dip_lane")         # the lane passes
    R._run_dip_hits(None, None, NOW)                          # ... so main stays out
    assert ran == [1, 1]
    clock["t"] += D.LANE_STALE_S                              # the lane thread died
    R._run_dip_hits(None, None, NOW)
    assert ran == [1, 1, 1]


@pytest.mark.parametrize("host", ["main", "dip_lane"])
def test_two_passes_never_overlap(monkeypatch, host):
    ran = _pass_counter(monkeypatch)
    assert D.HITS_LOCK.acquire(blocking=False)                # another pass is inside a decision
    try:
        R._run_dip_hits(None, None, NOW, host=host)
        assert ran == []
    finally:
        D.HITS_LOCK.release()
    R._run_dip_hits(None, None, NOW, host=host)
    assert ran == [1] and not D.HITS_LOCK.locked()


def test_a_failed_pass_releases_the_lock(monkeypatch):
    monkeypatch.setattr(D, "_LANE_SEEN", None)
    monkeypatch.setattr(D, "run_hits", lambda *a, **k: 1 / 0)
    R._run_dip_hits(None, None, NOW)
    assert not D.HITS_LOCK.locked()


# --- chain 56 reads a DexScreener pool list OUTSIDE the V4 rungs (Gamma MEDIUM on 4a4a4773c) ---
# v2 rung -> evm_v2_exit_quote.pair_quote_tokens, the v3 path after a miss, mixed_v3_v2 and
# probe_v3_v2: all gated by MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS / MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS
# (both default 56). So the dip retry does not run on a chain named in either knob.

def _real_ladder(monkeypatch, chain, display_reads):
    """The real entry ladder of `chain` with its real on-chain rungs, no network: RPC answers an
    empty result, aggregators are off, and every display-provider or raw HTTP read is recorded."""
    from sniper.execution import evm_real_seams as S
    from sniper.execution import solana_quote
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", False, raising=False)
    monkeypatch.setattr(solana_quote, "dexscreener_cooldown_active", lambda **k: False)
    monkeypatch.setattr(solana_quote, "_http", lambda method, url, *a, **k: (
        display_reads.append(url) or (200, {"pairs": []}, None)))
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (
        display_reads.append("urlopen") or (_ for _ in ()).throw(OSError("no network in a test"))))
    rpc = lambda method, url, headers=None, body=None, timeout=None: (200, {"result": "0x"}, None)  # noqa: E731
    quote_token = CHAIN_QUOTE[chain]["token"]
    ladder = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=chain, quote_token=quote_token, taker="0x" + "11" * 20,
        http=rpc, acct=object(), rpc_url="https://rpc.invalid")
    return ladder, quote_token


@pytest.mark.parametrize("chain", ["4663", "8453"])
def test_the_watch_ladder_reads_no_display_feed_on_a_chain_the_watch_may_run(monkeypatch, chain):
    reads = []
    ladder, quote_token = _real_ladder(monkeypatch, chain, reads)
    ladder(quote_token, MINT, SELL, exclude_rungs=D.WATCH_EXCLUDED_RUNGS)
    for rung in ("v2", "v3", "pons_v2", "launchpad_curve", "mixed_v3_v2"):
        ladder(quote_token, MINT, SELL, only_rung=rung)
    assert reads == []


def test_control_the_bsc_ladder_does_read_a_dexscreener_pool_list(monkeypatch):
    """Why 56 is excluded: the same call on BSC reads the token's pair list from DexScreener."""
    reads = []
    ladder, quote_token = _real_ladder(monkeypatch, "56", reads)
    ladder(quote_token, MINT, SELL, exclude_rungs=D.WATCH_EXCLUDED_RUNGS)
    assert any("dexscreener" in str(url) for url in reads), reads


def test_a_display_feed_chain_is_a_dip_chain_with_a_feed_free_watch(tmp_path, _cfg):
    """Superseded 2026-10-08 (operator: dip "for top traders on all chains"; design 92f34c9b7). A
    display-feed chain used to be dropped here. It now queues, and its watch quotes only the refused
    route's pin (tests/test_memecoin_dip_bsc_pinned.py proves no pair list is read)."""
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana", "4663", "56", "8453"))
    _cfg.setattr(cfg, "MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS", frozenset({"56"}))
    _cfg.setattr(cfg, "MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS", frozenset({"8453"}))
    assert D.chains() == {"solana", "4663", "56", "8453"} and D.display_feed_chains() == {"56", "8453"}
    c = _conn(tmp_path)
    ev = CopyEvent(**{**asdict(_ev()), "network_id": "56"})
    res = _refusal(c, ev)
    assert D.maybe_enqueue(c, ev, res, "2026-09-30T12:00:01+00:00",
                           event_json=json.dumps(asdict(ev))) is True


def test_ensure_watch_starts_a_thread_for_a_display_feed_chain_too(tmp_path, monkeypatch, _cfg):
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana", "4663", "56"))
    _cfg.setattr(cfg, "MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS", frozenset({"56"}))
    started = []
    monkeypatch.setattr(D, "_WATCH", None)
    monkeypatch.setattr(D, "_EVM_WATCHES", {})
    monkeypatch.setattr(D.DipWatch, "start", lambda self: started.append(self.name))
    D.ensure_watch(str(tmp_path / "ledger.db"), contexts={})
    assert sorted(started) == ["memecoin-dip-watch", "memecoin-dip-watch-4663", "memecoin-dip-watch-56"]


# --- a rate limit on a pinned quoter call is typed and pauses at once (Gamma LOW 2) ---

def _http_failure(status):
    from sniper.execution.evm_wallet import _http_failure as make
    return make(status)


def test_the_rpc_layer_says_which_http_status_failed():
    """The read layer raised one generic error for a 429 and a 5xx, so nothing above it could tell
    a rate limit from an outage. The status now rides on the exception; the message is unchanged."""
    from sniper.execution import evm_wallet as W
    for status in (429, 503):
        with pytest.raises(RuntimeError, match="EVM RPC provider HTTP failure") as err:
            W._rpc("https://solo.invalid", "eth_call", [{}, "latest"],
                   http=lambda *a, **k: (status, None, None))
        assert err.value.http_status == status


@pytest.mark.parametrize("error, kind", [
    (_http_failure(429), "rpc_rate_limit"),
    (_http_failure(503), "rpc_transport"),
    (RuntimeError("EVM RPC provider HTTP failure"), "rpc_transport"),      # no status: not a limit
    (OSError("HTTP Error 429: Too Many Requests"), "rpc_rate_limit"),
    (TimeoutError("read timed out"), "timeout")])
def test_a_pinned_quoter_read_that_fails_is_typed_never_a_silent_none(
        monkeypatch, no_display_feed, error, kind):
    v4 = no_display_feed
    from sniper.execution import evm_read_rpc
    from sniper.execution import evm_v4_multihop as mh
    monkeypatch.setattr(v4, "_pool_key_cached", lambda chain, pid: V4_KEY)

    def failing(url, method, params, http=None, **kw):
        raise error
    monkeypatch.setattr(v4, "_rpc", failing)
    monkeypatch.setattr(evm_read_rpc, "_rpc", failing)
    monkeypatch.setattr(evm_read_rpc, "_freshest_read_member", lambda url: url)
    with pytest.raises(v4.PinnedQuoteBlind) as err:
        v4.pinned_buy_out("4663", MINT, SELL, "https://rpc", pool_ids=[POOL])
    assert err.value.failure_type == kind
    with pytest.raises(v4.PinnedQuoteBlind) as err:
        mh.pinned_path_out("4663", LEGS, SELL, "https://rpc")
    assert err.value.failure_type == kind
    # the funded callers of the same quoter keep their silent None
    assert v4._quote_single("4663", V4_KEY, True, SELL, "https://rpc") is None
    assert mh._quote_path("4663", MINT, [(leg["currency"], leg["key"]) for leg in LEGS], SELL,
                          "https://rpc") is None


def test_a_pool_that_answers_no_quote_is_not_blind(monkeypatch, no_display_feed):
    v4 = no_display_feed
    from sniper.execution import evm_read_rpc
    monkeypatch.setattr(v4, "_pool_key_cached", lambda chain, pid: V4_KEY)
    monkeypatch.setattr(v4, "_rpc", lambda *a, **k: {"error": {"code": 3, "message": "revert"}})
    monkeypatch.setattr(evm_read_rpc, "_freshest_read_member", lambda url: url)
    assert v4.pinned_buy_out("4663", MINT, SELL, "https://rpc", pool_ids=[POOL]) == (None, None)


@pytest.mark.parametrize("kind, paused", [("rpc_rate_limit", True), ("rpc_transport", False),
                                          ("timeout", False)])
def test_a_blind_held_pool_quote_never_costs_the_aggregator_tick_and_only_a_rate_limit_pauses(
        tmp_path, monkeypatch, _cfg, kind, paused):
    from sniper.execution import evm_v4_quote as v4
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_S", 300.0)
    _seed(tmp_path, rung="kyber")
    seen = _kyber(monkeypatch, [1100, 1100])
    monkeypatch.setattr(v4, "pinned_buy_out",
                        lambda *a, **k: (_ for _ in ()).throw(v4.PinnedQuoteBlind(kind)))
    t = {"now": T0 + 5}
    w = _watch(tmp_path, _Ctx([]), now=lambda: t["now"])
    w.tick()
    assert w.take_due() == {"x1"}                           # the aggregator's price still counted
    t["now"] += 5
    w.tick()
    assert len(seen) == (1 if paused else 2)


# Gamma LOW 2 on c618a17ac, Alpha's ruling: ONE failed pinned read of any type paused the chain
# for 60 s, longer than the 30 s dip window, so every live row expired unwatched. Only a rate limit
# pauses; any other failed read is a typed miss and the next tick asks again.
@pytest.mark.parametrize("rung, kw", [("v4", {}), ("v4_multihop", {"legs_path": LEGS})])
@pytest.mark.parametrize("kind, paused", [("rpc_rate_limit", True), ("rpc_transport", False),
                                          ("timeout", False)])
def test_a_blind_pinned_winner_quote_is_a_typed_miss_and_only_a_rate_limit_pauses(
        tmp_path, monkeypatch, _cfg, rung, kw, kind, paused):
    from sniper.execution import evm_v4_multihop as mh
    from sniper.execution import evm_v4_quote as v4
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_S", 300.0)
    _seed(tmp_path, rung=rung, **kw)
    calls = []

    def blind(*a, **k):
        calls.append(1)
        raise v4.PinnedQuoteBlind(kind)
    monkeypatch.setattr(v4, "pinned_buy_out", blind)
    monkeypatch.setattr(mh, "pinned_path_out", blind)
    t = {"now": T0 + 5}
    w = _watch(tmp_path, _Ctx([]), now=lambda: t["now"])
    w.tick()
    assert w.stats_for("x1")["series"][-1]["miss"] == "provider_" + kind
    t["now"] += 5
    w.tick()
    assert len(calls) == (1 if paused else 2)


# --- Gamma LOW 1 and 3 on 4a4a4773c: which held pools, and how often ---

def test_with_more_held_pools_than_the_cap_the_deepest_on_our_own_tape_are_quoted(
        monkeypatch, no_display_feed):
    v4 = no_display_feed
    ids = ["0x%064x" % i for i in range(1, 13)]                   # 12 stored pools, sorted by id
    deep = ids[10]                                                # beyond the first 8 by id
    monkeypatch.setattr(v4, "_stored_pools_for_mint", lambda chain, mint: list(ids))
    monkeypatch.setattr(v4, "_own_depths", lambda chain, pids: {
        pid: (5000.0 if pid == deep else 10.0 if pid == ids[0] else None) for pid in pids})
    monkeypatch.setattr(v4, "_pool_key_cached", lambda chain, pid: dict(V4_KEY, tick_spacing=int(pid, 16)))
    asked = []
    monkeypatch.setattr(v4, "_quote_single", lambda chain, key, zf1, amount, rpc_url, **kw: (
        asked.append(key["tick_spacing"]) or (2000 if key["tick_spacing"] == 11 else 1000)))
    assert v4.pinned_buy_out("4663", MINT, SELL, "https://rpc") == (2000, deep)
    assert len(asked) == 8 and asked[:2] == [11, 1]              # deepest first, then known, then unknown


def test_eight_or_fewer_held_pools_need_no_depth_read(monkeypatch, no_display_feed):
    v4 = no_display_feed
    monkeypatch.setattr(v4, "_stored_pools_for_mint", lambda chain, mint: [POOL])
    monkeypatch.setattr(v4, "_own_depths", lambda *a: pytest.fail("a depth read nobody needs"))
    monkeypatch.setattr(v4, "_pool_key_cached", lambda chain, pid: V4_KEY)
    monkeypatch.setattr(v4, "_quote_single", lambda *a, **k: 1100)
    assert v4.pinned_buy_out("4663", MINT, SELL, "https://rpc") == (1100, POOL)


def test_an_empty_held_pool_scan_is_not_repeated_every_tick(tmp_path, monkeypatch, _cfg):
    """Until a held pool answers, each aggregator tick cost up to 8 eth_calls and a SQLite open.
    An empty scan now waits HELD_RESCAN_S before it runs again; a pinned pool is asked every tick."""
    from sniper.settings import memecoin as cfg
    _cfg.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_S", 300.0)
    _seed(tmp_path, rung="kyber")
    _kyber(monkeypatch, [1010] * 6)
    pinned = _Pinned(monkeypatch, [None, None, 1020, 1020])
    t = {"now": T0 + 5}
    w = _watch(tmp_path, _Ctx([]), now=lambda: t["now"])
    w.tick()                                                 # scan 1: nothing held
    t["now"] += 1
    w.tick()                                                 # inside the wait: no scan
    assert pinned.calls == [None]
    t["now"] += D.HELD_RESCAN_S
    w.tick()                                                 # scan 2: still nothing
    t["now"] += D.HELD_RESCAN_S
    w.tick()                                                 # scan 3: a pool answers and is pinned
    t["now"] += 1
    w.tick()                                                 # pinned: asked on the very next tick
    assert pinned.calls == [None, None, None, [POOL]]


# --- a lane that stops passing is visible: heartbeat field and one page (Gamma LOW, Alpha) ---

def _lane_clock(monkeypatch, *, expected=True):
    clock = {"t": 5000.0}
    monkeypatch.setattr(D, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(D, "_LANE_SEEN", None)
    monkeypatch.setattr(D, "_LANE_EXPECTED", False)
    monkeypatch.setattr(D, "_LAST_STALE_PAGE", None)
    pages = []
    from sniper.services import memecoin_alerts
    monkeypatch.setattr(memecoin_alerts, "send_alert",
                        lambda kind, text, **k: pages.append((kind, text)))
    if expected:
        D.expect_lane()
    return clock, pages


def _queued(n=1):
    c = _conn()
    for i in range(n):
        ev = _ev(fid="q%d" % i, mint=MINT[:-1] + str(i))
        D.maybe_enqueue(c, ev, _refusal(c, ev), "2026-09-30T12:00:01+00:00",
                        event_json=json.dumps(asdict(ev)))
    return c


def test_the_heartbeat_carries_the_lanes_last_pass_age(monkeypatch, tmp_path):
    clock, _pages = _lane_clock(monkeypatch, expected=False)
    assert D.lane_status() == {"expected": False, "last_pass_age_s": None, "alive": False}
    D.expect_lane()
    D.lane_seen()
    clock["t"] += 3.26
    assert D.lane_status() == {"expected": True, "last_pass_age_s": 3.3, "alive": True}
    clock["t"] += D.LANE_STALE_S
    assert D.lane_status()["alive"] is False
    from sniper.services import memecoin_runner
    path = tmp_path / "hb.json"
    memecoin_runner.write_heartbeat({}, True, path=str(path), dip_lane=D.lane_status())
    assert json.loads(path.read_text())["dip_lane"] == {
        "expected": True, "last_pass_age_s": 18.3, "alive": False}


def test_a_lane_that_stopped_passing_with_rows_queued_pages_once(monkeypatch):
    clock, pages = _lane_clock(monkeypatch)
    c = _queued(2)
    clock["t"] += D.LANE_PAGE_S - 1
    D.page_stale_lane(c)
    assert pages == []                                     # a long decision is not a hang yet
    clock["t"] += 1
    D.page_stale_lane(c)
    D.page_stale_lane(c)
    assert [kind for kind, _t in pages] == ["DIP-LANE-STALE"]
    assert "2 dip row" in pages[0][1] and "120 s" in pages[0][1]
    clock["t"] += D.STALE_PAGE_EVERY_S
    D.page_stale_lane(c)
    assert len(pages) == 2                                 # still stale: again after the cooldown


@pytest.mark.parametrize("case", ["no_rows", "lane_not_expected", "lane_passing"])
def test_no_page_without_a_stale_expected_lane_and_queued_rows(monkeypatch, case):
    clock, pages = _lane_clock(monkeypatch, expected=case != "lane_not_expected")
    c = _queued(0 if case == "no_rows" else 1)
    clock["t"] += D.LANE_PAGE_S + 5
    if case == "lane_passing":
        D.lane_seen()
    D.page_stale_lane(c)
    assert pages == []


def test_the_page_never_raises(monkeypatch):
    clock, _pages = _lane_clock(monkeypatch)
    clock["t"] += D.LANE_PAGE_S + 5
    D.page_stale_lane(None)                                # a broken connection


def test_the_main_loop_pages_while_it_covers_for_a_stale_lane(monkeypatch):
    ran = _pass_counter(monkeypatch)
    paged = []
    monkeypatch.setattr(D, "page_stale_lane", lambda conn: paged.append(conn))
    R._run_dip_hits("conn", None, NOW)                          # the lane is not passing
    assert ran == [1] and paged == ["conn"]
    R._run_dip_hits("conn", None, NOW, host="dip_lane")
    R._run_dip_hits("conn", None, NOW)                          # alive: main neither runs nor pages
    assert ran == [1, 1] and paged == ["conn"]
    assert D.HITS_LOCK.acquire(blocking=False)                  # a lane hung INSIDE a decision
    try:
        monkeypatch.setattr(D, "lane_alive", lambda: False)
        R._run_dip_hits("conn", None, NOW)
        assert ran == [1, 1] and paged == ["conn", "conn"]      # no pass possible, still paged
    finally:
        D.HITS_LOCK.release()


def test_the_runner_hosts_the_hits_on_their_own_woken_thread_never_on_a_fill_lane():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts/memecoin_runner_loop.py").read_text()
    worker = src[src.index("def _dip_hit_worker"):src.index("_PAPER_BRACKET_THREAD = ")]
    assert worker.index("_dip_wake.wait(1.0)") < worker.index('host="dip_lane"') < worker.index(
        "finish_db_cycle(dconn)")
    loop = worker[worker.index("while True:"):worker.index("if real_hook is not None:")]
    assert loop.index("try:") < loop.index("_dip_wake.wait(1.0)") and "except Exception:" in loop
    assert "target=_dip_hit_worker" in worker
    assert "contexts=contexts, wake=_dip_wake.set" in src
    assert worker.index("_expect_dip_lane()") < worker.index("target=_dip_hit_worker")
    assert "dip_lane=_dip_lane_status()" in src             # the heartbeat field
    lane = src[src.index("def _onchain_lane_worker"):src.index("if MEMECOIN_ONCHAIN_LANE_ENABLED and real_hook")]
    assert "_run_dip_hits" not in lane            # a hit never holds the on-chain lane's detections


# --- the terminal row keeps the series ---

def test_the_terminal_log_row_carries_the_tick_series(tmp_path):
    c = _seed(tmp_path)
    w = _watch(tmp_path, _Ctx([1050]))
    w.tick()
    D.run_hits(c, lambda conn, ev: pytest.fail("no decision on an expired row"),
               "2026-09-30T12:00:31+00:00", watch=w,
               event_from_json=lambda j: CopyEvent(**json.loads(j)))
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    series = json.loads(log["watch_series_json"])
    assert log["status"] == "expired" and [tick["buy_atomic"] for tick in series] == ["1000", "1050"]
    assert (log["watch_quotes"], log["watch_unknown"]) == (1, 0)


def test_a_row_the_watch_never_saw_has_a_null_series():
    c = _conn()
    ev = _ev()
    D.maybe_enqueue(c, ev, _refusal(c, ev), "2026-09-30T12:00:01+00:00",
                    event_json=json.dumps(asdict(ev)))

    class Blind:
        premium, stats = {}, {}

        def take_due(self):
            return set()
    D.run_hits(c, None, "2026-09-30T12:00:31+00:00", watch=Blind(),
               event_from_json=lambda j: CopyEvent(**json.loads(j)))
    [log] = [dict(r) for r in c.execute("SELECT * FROM memecoin_dip_retry_log")]
    assert log["watch_series_json"] is None and log["watch_quotes"] is None


# --- the chain context gives the watch what it needs ---

def test_the_context_exports_its_quote_token_and_passes_ladder_filters_through(monkeypatch):
    from eth_account import Account
    from sniper.execution import evm_real_seams as S
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: set())        # the primary rung only
    asked = []

    def primary(sell_token, buy_token, amount, *, slippage_bps=None):
        asked.append((sell_token, buy_token, amount))
        buy = "1100" if buy_token == MINT else str(int(SELL * 0.98))       # the rung's round trip
        return {"sellAmount": str(amount), "buyAmount": buy,
                "transaction": {"to": "0x" + "55" * 20, "data": "0x"}, "issues": {"allowance": None}}
    ctx = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="rpc", http=lambda *a, **k: (200, {}, None),
        venue_quote_fn=primary, usdc_fn=lambda: 100.0, token_fn=lambda mint: 0.0,
        backend_label="auto_free")
    assert ctx["quote_token"] == CHAIN_QUOTE["4663"]["token"]
    quote, miss = ctx["venue_quote_with_miss_fn"](
        ctx["quote_token"], MINT, SELL, exclude_rungs=("v4_multihop",))
    assert (quote["buyAmount"], miss) == ("1100", None)
    assert asked[0] == (ctx["quote_token"], MINT, SELL)
    n = len(asked)
    none, miss = ctx["venue_quote_with_miss_fn"](ctx["quote_token"], MINT, SELL, only_rung="kyber")
    assert none is None and len(asked) == n               # `only_rung` reached the ladder
