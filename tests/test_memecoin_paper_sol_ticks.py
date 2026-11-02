"""The Solana paper vault stream for onchain_ticks_v1 (spec 2026-10-08 paper exit ticks, part b): its
own key behind a guard, its own bindings (entry route, source swap, graduation), the funded tick
kinds, and nothing shared with the funded lane."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_paper_sol_ticks as SOL
from sniper.services import memecoin_paper_ticks as PT

PAPER = "https://mainnet.helius-rpc.com/?api-key=paper-key"
MINT = "Mint1111111111111111111111111111111111111111"
POOL = "Pool1111111111111111111111111111111111111111"
OTHER = "Pool2222222222222222222222222222222222222222"
SIG = "5" * 88
ORIGIN = "swap:raybot:%s:wallet111:%s" % (SIG, MINT.lower())
T0 = 1_791_000_000.0


# ------------------------------------------------------------------------ the key guard

@pytest.mark.parametrize("env, why", [
    ({}, "unset"),
    ({SOL.KNOB: "https://api.mainnet-beta.solana.com"}, "keyless"),
    ({SOL.KNOB: PAPER, "MEMECOIN_SOLANA_RPC_URL": PAPER}, "shared_with:MEMECOIN_SOLANA_RPC_URL"),
    ({SOL.KNOB: PAPER, "MEMECOIN_SOLANA_RPC_POOL_URLS": "https://a/?api-key=x," + PAPER},
     "shared_with:MEMECOIN_SOLANA_RPC_POOL_URLS"),
    ({SOL.KNOB: PAPER, "ONCHAIN_WATCH_HELIUS_RPC_URL": PAPER}, "shared_with:ONCHAIN_WATCH_HELIUS_RPC_URL"),
    ({SOL.KNOB: PAPER, "MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL": PAPER},
     "shared_with:MEMECOIN_RH_FAST_DEPOSIT_HELIUS_URL"),
    ({SOL.KNOB: PAPER, "MEMECOIN_CAPTURE_RPC_POOL_URLS": PAPER},
     "shared_with:MEMECOIN_CAPTURE_RPC_POOL_URLS"),
    # The research list it was taken from is the one allowed sharer (operator 2026-10-08).
    ({SOL.KNOB: PAPER, "MEMECOIN_RESEARCH_HELIUS_RPC_URLS": "https://b/?api-key=y," + PAPER}, None),
])
def test_the_key_guard(env, why):
    assert SOL.key_refusal(env.get(SOL.KNOB), env) == why


def test_option_c_key_is_refused_by_id(monkeypatch):
    from sniper.execution.solana_rpc_pool import key_id
    monkeypatch.setattr(SOL, "OPTION_C_KEY", key_id(PAPER))
    assert SOL.key_refusal(PAPER, {SOL.KNOB: PAPER}) == "option_c_key"


def test_a_refused_key_starts_nothing(monkeypatch):
    import threading
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name))
    assert SOL.start({SOL.KNOB: PAPER, "MEMECOIN_SOLANA_RPC_URL": PAPER}) is None
    assert started == []


# ------------------------------------------------------------------------ binding

class _Rpc:
    def __init__(self, sigs=()):
        self.calls, self.sigs = [], list(sigs)

    def __call__(self, method, params):
        self.calls.append((method, params[0]))
        if method == "getSignaturesForAddress":
            return [{"signature": s} for s in self.sigs]
        if method == "getTransaction":
            return {"sig": params[0], "transaction": {"message": {"accountKeys": [{"pubkey": "payer"}]}}}
        return None


def _resolver(monkeypatch, owners):
    """`_resolve_accounts` reading the pool each fake transaction resolves to (None: no pool)."""
    from sniper.services import memecoin_solana_tape as tape

    def resolve(tx, mint, *, exclude_owner=None, required_owner=None):
        owner = owners.get(tx.get("sig"))
        if owner is None or (required_owner is not None and owner != required_owner):
            return None
        quote = "native-sol" if owner.startswith("Curve") else "So11111111111111111111111111111111111111112"
        return (owner, "cv_" + owner, quote, ("Curve" + owner) if quote == "native-sol" else "qv_" + owner)
    monkeypatch.setattr(tape, "_resolve_accounts", resolve)


def _src(rpc, **kw):
    return SOL.PaperSolTicks(PAPER, rpc_call=rpc, **kw)


def test_the_source_swap_on_the_entry_pool_binds_entry_route_in_one_read(monkeypatch):
    _resolver(monkeypatch, {SIG: POOL})
    rpc = _Rpc()
    src = _src(rpc)
    src.wanted = [(1, MINT, POOL, ORIGIN)]
    src.resolve_pass()
    assert src.binding(1) == {"binding": "entry_route", "pool": POOL, "program": None}
    # One read binds; one more names the pool's program (the carry scope, Gamma (b)).
    assert rpc.calls == [("getTransaction", SIG), ("getAccountInfo", POOL)]


def test_a_different_source_pool_resolves_the_entry_pool_from_its_own_txs(monkeypatch):
    _resolver(monkeypatch, {SIG: OTHER, "sx": OTHER, "s1": POOL})
    rpc = _Rpc(sigs=["sx", "s1"])                 # a tx of the pool that resolves elsewhere: skipped
    src = _src(rpc)
    src.wanted = [(1, MINT, POOL, ORIGIN)]
    src.resolve_pass()
    assert src.binding(1)["binding"] == "entry_route" and src.binding(1)["pool"] == POOL
    assert ("getSignaturesForAddress", POOL) in rpc.calls


def test_the_source_pool_is_the_fallback(monkeypatch):
    _resolver(monkeypatch, {SIG: OTHER})
    src = _src(_Rpc(sigs=["s1", "s2"]))
    src.wanted = [(1, MINT, POOL, ORIGIN)]
    src.resolve_pass()
    assert src.binding(1)["binding"] == "source_swap" and src.binding(1)["pool"] == OTHER


def test_an_unresolvable_position_stops_spending_reads_and_reads_unbound(monkeypatch):
    _resolver(monkeypatch, {})
    rpc = _Rpc(sigs=["s1"])
    src = _src(rpc)
    src.wanted = [(1, MINT, POOL, "swap:3f2a-uuid")]
    for _ in range(SOL.MAX_ATTEMPTS + 3):
        src.resolve_pass()
    reads = len(rpc.calls)
    src.resolve_pass()
    assert len(rpc.calls) == reads and reads == SOL.MAX_ATTEMPTS * 2
    assert src.binding(1) == {"binding": "unbound"}
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "unbound")


def test_a_completed_curve_rebinds_to_its_pumpswap_pool(monkeypatch):
    from sniper.services import memecoin_slot_graduation as grad
    canonical = "PumpSwapPool11111111111111111111111111111"
    monkeypatch.setattr(grad, "canonical_pool", lambda mint, quote_mint=None: canonical)
    _resolver(monkeypatch, {SIG: "CurveA", "g1": canonical})
    src = _src(_Rpc(sigs=["g1"]))
    src.wanted = [(1, MINT, "CurveA", ORIGIN)]
    src.resolve_pass()
    assert src.binding(1)["binding"] == "entry_route"
    curve = src._bindings[1]["quote_vault"]
    _vaults, bindings = src.book()
    src.cache.record({"kind": "sol_curve_state", "pool": curve, "complete": True, "block_no": 9,
                      "virtual_token": 0, "virtual_sol": 0, "real_token": 0, "real_sol": 0},
                     bindings[curve], now_ts=0.0, observed_at="2026-10-08T07:00:00+00:00")
    src.resolve_pass()
    assert src.binding(1)["binding"] == "graduated" and src.binding(1)["pool"] == canonical
    assert src.counters["graduations"] == 1 and src.counters["rebinds"] == 1


def test_a_graduating_position_holds_until_the_rebind_lands(monkeypatch):
    _resolver(monkeypatch, {SIG: "CurveA"})
    src = _src(_Rpc())
    src.wanted = [(1, MINT, "CurveA", ORIGIN)]
    src.resolve_pass()
    src._bindings[1] = dict(src._bindings[1], binding="graduating", stale=True)
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "graduating")


def test_the_book_subscribes_curves_and_both_vaults():
    from sniper.services.memecoin_solana_ws import CurveTape, VaultTape
    src = _src(_Rpc())
    src._bindings = {
        1: SOL.PaperSolTicks._entry("entry_route", MINT, ("CurveA", "cv", "native-sol", "CurvePDA")),
        2: SOL.PaperSolTicks._entry("source_swap", "M2", ("P2", "cv2", "So1", "qv2"))}
    vaults, bindings = src.book()
    assert isinstance(vaults["CurvePDA"], CurveTape) and bindings["CurvePDA"].mint == MINT
    assert vaults["cv2"] is vaults["qv2"] and isinstance(vaults["cv2"], VaultTape)
    assert bindings["P2"].quote_mint == "So1"


# ------------------------------------------------------------------------ the tick

def _bound_src(monkeypatch, mark):
    _resolver(monkeypatch, {SIG: POOL})
    src = _src(_Rpc(), now_iso_fn=lambda: "2026-10-08T07:00:03+00:00",
               quote_usd_fn=lambda mints: {}, mono=lambda: 100.0)
    src.wanted = [(1, MINT, POOL, ORIGIN)]
    src.resolve_pass()
    monkeypatch.setattr(src.cache, "fresh_price_map",
                        lambda pool, quote_usd, *, now_ts, max_age_s: {("solana", MINT): mark} if mark else {})
    return src


def test_a_swap_mark_is_a_tick_aged_by_its_observation(monkeypatch):
    src = _bound_src(monkeypatch, {"price": 1.3, "source": "fastlane_sol_swap",
                                   "observed_at": "2026-10-08T07:00:01+00:00"})
    tick, why = src.tick({"id": 1, "mint": MINT}, {}, T0)
    assert why is None and tick.tick_kind == "sol_swap" and tick.source == "paper_sol_swap"
    assert tick.price == 1.3 and tick.at == pytest.approx(T0 - 2.0)


def test_a_curve_mark_is_current_while_subscribed(monkeypatch):
    src = _bound_src(monkeypatch, {"price": 0.9, "source": "fastlane_sol_curve",
                                   "observed_at": "2026-10-08T06:50:00+00:00"})
    tick, _why = src.tick({"id": 1, "mint": MINT}, {}, T0)
    assert tick.tick_kind == "sol_curve" and tick.at == T0


def test_no_mark_is_no_tick(monkeypatch):
    src = _bound_src(monkeypatch, None)
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")


def test_every_read_goes_through_the_paper_rpc(monkeypatch):
    _resolver(monkeypatch, {})
    rpc = _Rpc(sigs=["s1"])
    src = _src(rpc)
    src.wanted = [(1, MINT, POOL, ORIGIN)]
    src.resolve_pass()
    assert rpc.calls and src.counters["rpc:getTransaction"] == 2
    assert src.counters["rpc:getSignaturesForAddress"] == 1


def test_no_binding_means_no_socket():
    calls, sleeps = [], []

    class Stop(Exception):
        pass

    def sleep(s):
        sleeps.append(s)
        raise Stop
    src = _src(_Rpc(), stream_fn=lambda *a, **k: calls.append(1))
    with pytest.raises(Stop):
        src.stream_forever(sleep=sleep)
    assert calls == [] and sleeps == [2.0]


# ------------------------------------------------------------------------ the engine on Solana

def test_a_solana_v1_position_closes_on_the_paper_stream_and_records_its_binding(monkeypatch, tmp_path):
    from sniper.persistence import db_memecoin as m
    from test_memecoin_paper_ticks import _ledger, _open, _row, _tape
    c, _m = _ledger()
    pid = _open(c, m, network="solana", pool=POOL)
    c.execute("UPDATE memecoin_positions SET mint=?, origin_event_id=? WHERE id=?", (MINT, ORIGIN, pid))
    c.commit()
    _resolver(monkeypatch, {SIG: OTHER})                  # the source traded another pool
    src = _src(_Rpc(), now_iso_fn=lambda: "2026-10-08T07:00:30+00:00", quote_usd_fn=lambda m_: {},
               mono=lambda: 100.0)
    monkeypatch.setattr(src.cache, "fresh_price_map", lambda pool, q, *, now_ts, max_age_s: {
        ("solana", MINT): {"price": 1.30, "source": "fastlane_sol_swap",
                           "observed_at": "2026-10-08T07:00:29+00:00"}})
    clock = {"t": T0}
    eng = PT.PaperTickEngine(connect=lambda: c, now_fn=lambda: "2026-10-08T07:00:30+00:00",
                             tape_path=_tape(tmp_path), sol=src, clock=lambda: clock["t"],
                             max_age_s=5.0, bsc_period_s=5.0)
    eng.step()                                     # publishes wanted
    src.resolve_pass()                             # binds (source_swap: the entry pool never resolved)
    eng.step()
    row = _row(c, pid)
    assert row["exit_reason"] == "real_tp"
    assert json.loads(row["exit_decision_json"])["tick_kind"] == "sol_swap"
    tick_json = json.loads(row["paper_tick_json"])
    assert tick_json["binding"] == "source_swap" and tick_json["pool"] == OTHER
    assert tick_json["entry_pool"] == POOL


def test_a_set_key_with_no_v1_position_reads_nothing_and_opens_nothing():
    """Gamma LOW: the key set while the engine is v0 (no Solana v1 position): no bind read, no
    seed, no socket."""
    rpc, streams, sleeps = _Rpc(sigs=["s1"]), [], []

    class Stop(Exception):
        pass

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise Stop
    src = _src(rpc, stream_fn=lambda *a, **k: streams.append(1))
    src.wanted = []
    for _ in range(3):
        src.resolve_pass()
    with pytest.raises(Stop):
        src.stream_forever(sleep=sleep)
    assert rpc.calls == [] and streams == [] and not any(k.startswith("rpc") for k in src.counters)


def test_the_heartbeat_reports_the_stream_threads_cpu_and_frame_rate():
    import threading
    import time as _time
    src = _src(_Rpc())
    done, started, go, burned = (threading.Event() for _ in range(4))

    def busy():
        src._idents["stream"] = threading.get_ident()
        started.set()
        go.wait(2)          # burn BETWEEN the beats: under a loaded suite a burn before the first
        end = _time.thread_time() + 0.05    # beat left a zero CPU delta (deploy 8f788d344)
        while _time.thread_time() < end:
            pass
        burned.set()
        done.wait(2)
    t = threading.Thread(target=busy)
    t.start()
    started.wait(2)
    first = src.heartbeat()
    assert "cpu_pct_stream" not in first                 # no rate before a second beat
    go.set()
    src.counters["rows"] += 30
    burned.wait(2)
    _time.sleep(0.01)
    second = src.heartbeat()
    done.set()
    t.join()
    assert second["cpu_pct_stream"] is not None and second["cpu_pct_stream"] > 0
    assert second["rows_per_min"] > 0



# ------------------------------------------------------------------------ carried CPMM spot (Gamma (b))

PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"


class _OwnerRpc(_Rpc):
    def __init__(self, owner, **kw):
        super().__init__(**kw)
        self.owner = owner

    def __call__(self, method, params):
        if method == "getAccountInfo":
            self.calls.append((method, params[0]))
            return {"value": {"owner": self.owner}} if self.owner else {"value": None}
        return super().__call__(method, params)


def _carry_src(monkeypatch, owner=PUMP_AMM, *, quote_usd=150.0):
    _resolver(monkeypatch, {SIG: POOL})
    src = _src(_OwnerRpc(owner), now_iso_fn=lambda: "2026-10-08T07:00:30+00:00",
               quote_usd_fn=lambda mints: {WSOL: quote_usd}, mono=lambda: 100.0)
    src.wanted = [(1, MINT, POOL, ORIGIN)]
    src.resolve_pass()
    _vaults, bindings = src.book()
    binding = next(iter(bindings.values()))
    return src, binding


def _swap_price(coin, quote, swap_ratio):
    """quote per coin of the swap `_sync` records: `swap_ratio` x the vault spot quote/coin."""
    coin_out = coin // 1000
    return (int(coin_out * quote / coin * swap_ratio) / 10 ** 9) / (coin_out / 10 ** 6)


def _sync(src, binding, *, coin, quote, slot=5, now_ts=10.0, swap_ratio=1.0, synced=None):
    """A swap (aged out of the fresh map by now) whose price is `swap_ratio` x the reserve spot and
    whose post-trade balances are (coin, quote), then a one-sided reserve row holding `synced`
    (default: the same reserves): a quiet pool, unless `synced` moved them."""
    if swap_ratio is not None:
        coin_out = coin // 1000
        src.cache.record({"kind": "sol_swap", "pool": binding.pool, "amount0": -coin_out,
                          "amount1": int(coin_out * quote / coin * swap_ratio), "block_no": slot - 1,
                          "decimals0": 6, "decimals1": 9, "balance0": coin, "balance1": quote},
                         binding, now_ts=now_ts - 9.0, observed_at="2026-10-08T07:00:01+00:00")
    coin2, quote2 = synced or (coin, quote)
    src.cache.record({"kind": "sol_sync", "pool": binding.pool, "amount0": coin2, "amount1": quote2,
                      "block_no": slot, "decimals0": 6, "decimals1": 9}, binding,
                     now_ts=now_ts, observed_at="2026-10-08T07:00:10+00:00")


def test_a_quiet_pumpswap_pool_carries_its_last_swap_while_the_socket_lives(monkeypatch):
    src, binding = _carry_src(monkeypatch)
    assert src.binding(1)["program"] == "pumpswap"
    _sync(src, binding, coin=2_000_000 * 10 ** 6, quote=10 * 10 ** 9)   # 10 SOL / 2M coins
    src._socket_live = True
    tick, why = src.tick({"id": 1, "mint": MINT}, {}, T0)
    assert why is None and tick.tick_kind == "sol_state_carried" and tick.at == T0
    assert tick.price == pytest.approx(_swap_price(2_000_000 * 10 ** 6, 10 * 10 ** 9, 1.0) * 150.0)


def test_a_pumpswap_vault_spot_under_the_swap_price_carries_the_swap_price(monkeypatch):
    """Gamma 2026-10-08: PumpSwap prices trades off a virtual quote reserve its vault does not
    hold; funded measured vault/swap at a median 0.923. The carried tick is the swap, not the vault."""
    src, binding = _carry_src(monkeypatch)
    coin, quote = 2_000_000 * 10 ** 6, 10 * 10 ** 9
    _sync(src, binding, coin=coin, quote=quote, swap_ratio=1 / 0.92)      # vault spot = 0.92 x swap
    src._socket_live = True
    tick, why = src.tick({"id": 1, "mint": MINT}, {}, T0)
    assert why is None
    assert tick.price == pytest.approx(_swap_price(coin, quote, 1 / 0.92) * 150.0)
    assert tick.price != pytest.approx(10 / 2_000_000 * 150.0, rel=0.01)


def test_reserves_that_moved_since_the_last_swap_carry_nothing(monkeypatch):
    """A reserve change with no swap row to price it is an unseen trade or an LP change: refuse."""
    src, binding = _carry_src(monkeypatch)
    coin, quote = 2_000_000 * 10 ** 6, 10 * 10 ** 9
    _sync(src, binding, coin=coin, quote=quote, synced=(coin, quote + 10 ** 8))
    src._socket_live = True
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")
    assert src.counters["carry_reserves_moved"] == 1


def test_a_swap_without_post_balances_never_carries(monkeypatch):
    src, binding = _carry_src(monkeypatch)
    coin, quote = 2_000_000 * 10 ** 6, 10 * 10 ** 9
    src.cache.record({"kind": "sol_swap", "pool": binding.pool, "amount0": -coin // 1000,
                      "amount1": quote // 1000, "block_no": 4, "decimals0": 6, "decimals1": 9},
                     binding, now_ts=1.0, observed_at="2026-10-08T07:00:01+00:00")
    src.cache.record({"kind": "sol_sync", "pool": binding.pool, "amount0": coin, "amount1": quote,
                      "block_no": 5, "decimals0": 6, "decimals1": 9}, binding,
                     now_ts=10.0, observed_at="2026-10-08T07:00:10+00:00")
    src._socket_live = True
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")
    assert src.counters["carry_reserves_moved"] == 0       # unknown is not moved


def test_an_unreadable_program_is_counted_and_never_carries(monkeypatch):
    """Gamma LOW: a failed or 429'd owner read is permanent per binding; count it apart."""
    class _Failing(_OwnerRpc):
        def __call__(self, method, params):
            if method == "getAccountInfo":
                raise RuntimeError("HTTP 429")
            return super().__call__(method, params)
    _resolver(monkeypatch, {SIG: POOL})
    src = _src(_Failing(PUMP_AMM), now_iso_fn=lambda: "2026-10-08T07:00:30+00:00",
               quote_usd_fn=lambda mints: {WSOL: 150.0}, mono=lambda: 100.0)
    src.wanted = [(1, MINT, POOL, ORIGIN)]
    src.resolve_pass()
    assert src.binding(1)["program"] is None
    assert src.counters["program_unread"] == 1


@pytest.mark.parametrize("owner", [None, "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # DLMM
                                   "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"])     # CLMM
def test_an_unknown_or_concentrated_program_never_carries(monkeypatch, owner):
    src, binding = _carry_src(monkeypatch, owner)
    _sync(src, binding, coin=2_000_000 * 10 ** 6, quote=10 * 10 ** 9)
    src._socket_live = True
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")


def test_a_dead_socket_carries_nothing(monkeypatch):
    src, binding = _carry_src(monkeypatch)
    _sync(src, binding, coin=2_000_000 * 10 ** 6, quote=10 * 10 ** 9)
    src._socket_live = False
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")


def test_a_rollover_needs_a_fresh_reserve_observation_before_carrying(monkeypatch):
    src, binding = _carry_src(monkeypatch)
    _sync(src, binding, coin=2_000_000 * 10 ** 6, quote=10 * 10 ** 9, now_ts=10.0)
    src._socket_live = True
    src.cache.invalidate_prices(now_ts=20.0)                 # the epoch rolled over
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")
    src.cache.record({"kind": "sol_seed", "pool": binding.pool, "amount0": 2_000_000 * 10 ** 6,
                      "amount1": 10 * 10 ** 9, "block_no": 9, "decimals1": 9}, binding,
                     now_ts=21.0, observed_at="2026-10-08T07:00:21+00:00")
    tick, _why = src.tick({"id": 1, "mint": MINT}, {}, T0)
    assert tick.tick_kind == "sol_state_carried"
    assert tick.price == pytest.approx(_swap_price(2_000_000 * 10 ** 6, 10 * 10 ** 9, 1.0) * 150.0)


def test_a_seed_after_the_rollover_with_moved_reserves_carries_nothing(monkeypatch):
    src, binding = _carry_src(monkeypatch)
    _sync(src, binding, coin=2_000_000 * 10 ** 6, quote=10 * 10 ** 9, now_ts=10.0)
    src._socket_live = True
    src.cache.invalidate_prices(now_ts=20.0)
    src.cache.record({"kind": "sol_seed", "pool": binding.pool, "amount0": 2_000_000 * 10 ** 6,
                      "amount1": 105 * 10 ** 8, "block_no": 9, "decimals1": 9}, binding,
                     now_ts=21.0, observed_at="2026-10-08T07:00:21+00:00")
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")


def test_the_keepalive_socket_is_the_default_stream():
    import functools
    fn = SOL.PaperSolTicks._default_stream()
    assert isinstance(fn, functools.partial)
    connect = fn.keywords["connect"]
    assert connect.keywords == {"ping_interval": SOL.PING_S, "ping_timeout": SOL.PING_S}


def test_the_engine_counts_carried_seconds_apart(monkeypatch, tmp_path):
    from sniper.persistence import db_memecoin as m
    from test_memecoin_paper_ticks import _ledger, _open, _row, _tape
    c, _m = _ledger()
    pid = _open(c, m, network="solana", pool=POOL, policy={"tp_pct": 1000.0})
    c.execute("UPDATE memecoin_positions SET mint=?, origin_event_id=? WHERE id=?", (MINT, ORIGIN, pid))
    c.commit()
    src, binding = _carry_src(monkeypatch)
    src.wanted = [(pid, MINT, POOL, ORIGIN)]
    src._bindings = {pid: src._bindings[1]}
    _sync(src, binding, coin=150 * 10 ** 6, quote=10 ** 9)          # $1.00 a coin: no rail fires
    src._socket_live = True
    clock = {"t": T0}
    eng = PT.PaperTickEngine(connect=lambda: c, now_fn=lambda: "2026-10-08T07:00:30+00:00",
                             tape_path=_tape(tmp_path), sol=src, clock=lambda: clock["t"],
                             max_age_s=5.0, bsc_period_s=5.0)
    eng.step()
    clock["t"] += 4.0
    eng.step()
    assert eng.counters["live:carried"] == 2, eng.counters
    assert eng._state[pid]["carried_s"] == pytest.approx(4.0)
    eng._flush(c, _row(c, pid), {}, eng._state[pid], clock["t"], None)
    assert json.loads(_row(c, pid)["paper_tick_json"])["carried_s"] == pytest.approx(4.0)


def test_the_socket_reads_live_only_inside_a_running_epoch(monkeypatch):
    src, _binding = _carry_src(monkeypatch)
    during = []

    class Halt(BaseException):
        pass

    async def stream(vaults, on_row, **kw):
        during.append(src._socket_live)
        raise RuntimeError("socket closed")              # a dead socket ends the epoch

    def sleep(_s):
        raise Halt
    src._stream_fn = stream
    with pytest.raises(Halt):
        src.stream_forever(sleep=sleep)
    assert during == [True] and src._socket_live is False


@pytest.mark.parametrize("swap_ratio", [1.05, 0.92, 1.2, 0.8, None])
def test_the_carry_is_the_last_swap_price_whatever_the_vault_spot(monkeypatch, swap_ratio):
    """The vault spot never prices a carried tick; no last swap, no carry."""
    src, binding = _carry_src(monkeypatch)
    coin, quote = 2_000_000 * 10 ** 6, 10 * 10 ** 9
    _sync(src, binding, coin=coin, quote=quote, swap_ratio=swap_ratio)
    src._socket_live = True
    tick, why = src.tick({"id": 1, "mint": MINT}, {}, T0)
    if swap_ratio is None:
        assert (tick, why) == (None, "no_mark")
    else:
        assert tick.price == pytest.approx(_swap_price(coin, quote, swap_ratio) * 150.0)


def test_a_last_swap_in_another_quote_never_carries(monkeypatch):
    src, binding = _carry_src(monkeypatch)
    _sync(src, binding, coin=2_000_000 * 10 ** 6, quote=10 * 10 ** 9)
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    with src._lock:
        src._bindings[1] = dict(src._bindings[1], quote_mint=usdc)
    src._quote_usd_fn = lambda mints: {WSOL: 150.0, usdc: 1.0}     # priceable, so only the mint refuses
    src._socket_live = True
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")


# ------------------------------------------------------------------------ C04: the pool is the identity
# Engineering review 2026-10-08 C04 (P1, paper): the fresh mark map was keyed ('solana', mint), so two
# positions on the same mint bound to different pools both took the newest print of EITHER pool.

def _two_pools(mono=100.0, quote_usd=100.0):
    src = _src(_Rpc(), now_iso_fn=lambda: "2026-10-08T07:00:03+00:00",
               quote_usd_fn=lambda mints: {WSOL: quote_usd}, mono=lambda: mono)
    src._bindings = {1: SOL.PaperSolTicks._entry("entry_route", MINT, (POOL, "cvA", WSOL, "qvA")),
                     2: SOL.PaperSolTicks._entry("entry_route", MINT, (OTHER, "cvB", WSOL, "qvB"))}
    _vaults, bindings = src.book()
    return src, bindings


def _swap(src, binding, quote_per_token, *, now_ts=99.0, slot=5):
    coin = 1_000 * 10 ** 6
    src.cache.record({"kind": "sol_swap", "pool": binding.pool, "amount0": -coin,
                      "amount1": int(quote_per_token * 1_000 * 10 ** 9), "block_no": slot,
                      "decimals0": 6, "decimals1": 9}, binding,
                     now_ts=now_ts, observed_at="2026-10-08T07:00:02+00:00")


def _prices(src):
    return [getattr(src.tick({"id": pid, "mint": MINT}, {}, T0)[0], "price", None) for pid in (1, 2)]


def test_review_c04_witness_each_position_prices_on_its_own_pool():
    src, bindings = _two_pools()
    _swap(src, bindings[POOL], 1.5)                   # $150 a token on pool A
    _swap(src, bindings[OTHER], 3.0, now_ts=99.5)     # $300 on pool B, newer
    assert _prices(src) == [pytest.approx(150.0), pytest.approx(300.0)]


def test_reversed_arrival_order_prices_the_same():
    src, bindings = _two_pools()
    _swap(src, bindings[OTHER], 3.0)
    _swap(src, bindings[POOL], 1.5, now_ts=99.5)
    assert _prices(src) == [pytest.approx(150.0), pytest.approx(300.0)]


def test_a_pool_without_its_own_mark_is_unknown_never_the_other_pools():
    src, bindings = _two_pools()
    _swap(src, bindings[OTHER], 3.0)
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")
    assert src.tick({"id": 2, "mint": MINT}, {}, T0)[0].price == pytest.approx(300.0)


def test_another_pools_swap_never_corroborates_a_state_mark():
    src, bindings = _two_pools()
    _swap(src, bindings[POOL], 1.5)
    src.cache.record({"kind": "sol_sync", "pool": OTHER, "amount0": 1_000 * 10 ** 6, "amount1": 1_520 * 10 ** 9,
                      "block_no": 6, "decimals0": 6, "decimals1": 9}, bindings[OTHER],
                     now_ts=99.5, observed_at="2026-10-08T07:00:02+00:00")
    assert src.tick({"id": 2, "mint": MINT}, {}, T0) == (None, "no_mark")
    assert src.tick({"id": 1, "mint": MINT}, {}, T0)[0].price == pytest.approx(150.0)


def test_after_a_rebind_the_old_pools_marks_never_price_the_new_binding(monkeypatch):
    src, bindings = _two_pools()
    _swap(src, bindings[POOL], 1.5)
    with src._lock:                                     # position 1 graduates onto pool B
        src._bindings[1] = SOL.PaperSolTicks._entry("graduated", MINT, (OTHER, "cvB", WSOL, "qvB"))
    src._prices = (None, {})
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")
    _swap(src, bindings[POOL], 1.6, now_ts=99.6)        # a late print on the old pool
    src._prices = (None, {})
    assert src.tick({"id": 1, "mint": MINT}, {}, T0) == (None, "no_mark")
    _swap(src, bindings[OTHER], 3.0, now_ts=99.7)
    src._prices = (None, {})
    assert src.tick({"id": 1, "mint": MINT}, {}, T0)[0].price == pytest.approx(300.0)


def test_a_late_curve_completion_after_a_rebind_leaves_the_amm_price(monkeypatch):
    from sniper.services import memecoin_slot_graduation as grad
    canonical = "PumpSwapPool11111111111111111111111111111"
    monkeypatch.setattr(grad, "canonical_pool", lambda mint, quote_mint=None: canonical)
    _resolver(monkeypatch, {SIG: "CurveA", "g1": canonical})
    src = _src(_Rpc(sigs=["g1"]), now_iso_fn=lambda: "2026-10-08T07:00:03+00:00",
               quote_usd_fn=lambda mints: {WSOL: 100.0}, mono=lambda: 100.0)
    src.wanted = [(1, MINT, "CurveA", ORIGIN)]
    src.resolve_pass()
    curve = src._bindings[1]["quote_vault"]
    _vaults, bindings = src.book()
    done = {"kind": "sol_curve_state", "pool": curve, "complete": True, "block_no": 9,
            "virtual_token": 0, "virtual_sol": 0, "real_token": 0, "real_sol": 0}
    src.cache.record(done, bindings[curve], now_ts=0.0, observed_at="2026-10-08T07:00:00+00:00")
    src.resolve_pass()
    assert src.binding(1)["pool"] == canonical
    _vaults, bindings = src.book()
    _swap(src, bindings[canonical], 2.0)
    src.cache.record(dict(done, block_no=10), SOL_binding(curve), now_ts=99.8,
                     observed_at="2026-10-08T07:00:02+00:00")
    src.resolve_pass()
    assert src.binding(1)["pool"] == canonical
    assert src.tick({"id": 1, "mint": MINT}, {}, T0)[0].price == pytest.approx(200.0)


def SOL_binding(curve):
    from sniper.services.memecoin_solana_swap_marks import PoolBinding
    return PoolBinding(mint=MINT, pool=curve, coin_vault="cv", quote_vault=curve, quote_mint=WSOL)


def test_the_book_drops_the_marks_of_pools_nothing_is_bound_to():
    src, bindings = _two_pools()
    _swap(src, bindings[POOL], 1.5)
    _swap(src, bindings[OTHER], 3.0)
    with src._lock:
        src._bindings.pop(2)
    src.book()
    assert src.cache.pools() == {POOL}


def test_default_paper_rpc_paces_seed_and_receipt_reads_and_backs_off_429(monkeypatch):
    import urllib.error
    from sniper.services import memecoin_solana_blocktime as BT
    build = BT.build_read_rpc_call
    now = [0.0]
    calls = []

    def raw(url, method, params, *, timeout):
        assert url == PAPER
        calls.append((method, now[0]))
        if len(calls) == 1:
            raise urllib.error.HTTPError(url, 429, 'limited', {}, None)
        return {'ok': True}

    def paced(*args, **kwargs):
        kwargs.update(clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
        return build(*args, **kwargs)

    monkeypatch.setattr(BT, '_rpc_call', raw)
    monkeypatch.setattr(BT, 'build_read_rpc_call', paced)
    src = SOL.PaperSolTicks(PAPER)
    assert src._get_tx(SIG) == {'ok': True}
    assert src.rpc('getAccountInfo', [POOL]) == {'ok': True}
    assert calls == [('getTransaction', 0.0), ('getTransaction', 3.0), ('getAccountInfo', 4.2)]
