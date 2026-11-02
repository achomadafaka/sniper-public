"""1-second price paths and the funded-exit replay (scripts/research/price_path_1s.py, Alpha task
85515c15). The venue transactions are REAL mainnet reads already in the repo's fixtures; the replay
runs the runner's own exit code on hand-built paths; the reader runs against a fake research key."""
import copy
import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "tests" / "fixtures"


def _mod():
    spec = importlib.util.spec_from_file_location("price_path_1s",
                                                  ROOT / "scripts" / "research" / "price_path_1s.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _mod()
VENUES = json.loads((FX / "solana" / "funded_entry_venues.json").read_text())
B1 = json.loads((FX / "solana" / "b1_exit_frames.json").read_text())
LEGS = json.loads((FX / "memecoin" / "slot_routed_leg_ratio_20260928.json").read_text())
SWAP2 = json.loads((FX / "memecoin" / "damm_v2_swap2_routed.json").read_text())
V_QUOTE = 17_584_505_596        # a PumpSwap Pool account's virtual quote (pumpswap_buy doc)


class _Read:
    def __init__(self, txs, pages=(), covered=()):
        self.txs, self.pages, self.covered, self.truncated = list(txs), list(pages), list(covered), False


def _swaps_of(tx, mint, v_quote=V_QUOTE):
    db = P.connect(":memory:")
    P.store(db, "row", mint, _Read([tx]))
    return P.load_swaps(db, mint, lambda _owner: v_quote), db


# --- one transaction per venue ------------------------------------------------------------------------

CASES = {
    "pump_curve": (VENUES["pump_curve_entry"]["tx"], VENUES["pump_curve_entry"]["mint"], "curve_event"),
    "pump_amm": (VENUES["pumpswap_entry"]["tx"], VENUES["pumpswap_entry"]["mint"], "pump_amm_vaults_v"),
    "raydium_cpmm": (B1["raydium_cp"]["tx"], B1["raydium_cp"]["mint"], "cpmm_event"),
    "raydium_launchlab": (LEGS["rows"][0]["tx"], LEGS["rows"][0]["mint"], "launchlab_event"),
}


@pytest.mark.parametrize("venue", sorted(CASES))
def test_each_exact_venue_prices_its_swap_inside_the_pools_own_spot_range(venue):
    """The spot after a swap comes from the venue's own reserves; the swap's paid price must lie
    between the spot before and after it, up to the fee. A wrong reserve (a missing virtual quote,
    accrued fees read as reserves) breaks this on real transactions."""
    tx, mint, basis = CASES[venue]
    swaps, _db = _swaps_of(tx, mint)
    rows = [s for s in swaps if s["venue"] == venue]
    assert len(rows) == 1 and rows[0]["basis"] == basis
    s = rows[0]
    pre = P._spot(basis, int(s["coin_res"]) - int(s["coin_delta"]),
                  int(s["quote_res"]) - int(s["quote_delta"]), s["coin_decimals"], s["quote_decimals"],
                  s["v_quote"])
    lo, hi = min(pre, s["spot"]), max(pre, s["spot"])
    assert lo * (1 - P.BRACKET_FEE) <= s["paid"] <= hi * (1 + P.BRACKET_FEE)
    assert s["fee_bps_event"] and 0 < s["fee_bps_event"] < 300, "the sell quote needs the venue fee"


def test_pumpswap_spot_adds_the_pool_accounts_virtual_quote():
    tx, mint, _ = CASES["pump_amm"]
    with_v, _ = _swaps_of(tx, mint, V_QUOTE)
    without, _ = _swaps_of(tx, mint, None)
    s = with_v[0]
    assert s["spot"] == pytest.approx(((int(s["quote_res"]) + V_QUOTE) / 1e9)
                                      / (int(s["coin_res"]) / 10 ** s["coin_decimals"]))
    assert without[0]["basis"] == "trade_avg" and without[0]["spot"] is None, \
        "an unread virtual quote is unknown, never priced as zero"


def _cl_swaps(bin_step=None):
    db = P.connect(":memory:")
    P.store(db, "row", SWAP2["coin"], _Read([SWAP2["tx"]]))
    swaps = P.load_swaps(db, SWAP2["coin"], lambda _o: None, lambda _lb: bin_step)
    return {s["venue"]: s for s in swaps}


def test_damm_v2_prices_from_its_evtswap2_post_swap_sqrt_price():
    """The routed buy pushed the DAMM v2 pool up: the post-swap spot sits above the paid price, by
    less than the swap's own fee and impact."""
    s = _cl_swaps()["meteora_damm_v2"]
    assert s["basis"] == "damm_v2_event" and s["extra"]["coin_is_a"] is True
    assert s["spot"] == pytest.approx((int(s["extra"]["sqrt_price_x64"]) / 2 ** 64) ** 2)   # both 9 dp
    assert s["paid"] < s["spot"] < s["paid"] * 1.10
    assert s["fee_bps_event"] == pytest.approx(1e4 * (325558846 + 81389711) / 19243860718)


def test_dlmm_prices_from_its_swap_events_end_bin_and_only_with_a_checked_bin_step():
    """Bin step 125 is the only one whose start..end bins bracket the swap's paid price; an unread
    or refused bin step leaves the swap unknown (trade_avg), never guessed."""
    s = _cl_swaps(125)["meteora_dlmm"]
    assert s["basis"] == "dlmm_event" and s["extra"]["coin_is_x"] is True
    lo, hi = (1.0125 ** s["extra"]["start_bin_id"], 1.0125 ** s["extra"]["end_bin_id"])
    assert lo < s["paid"] < hi and s["spot"] == pytest.approx(hi)
    assert _cl_swaps(None)["meteora_dlmm"]["basis"] == "trade_avg"
    raw = bytearray(96)
    raw[73:75] = raw[80:82] = (125).to_bytes(2, "little")
    assert P.dlmm_bin_step(bytes(raw)) == 125
    raw[80:82] = (99).to_bytes(2, "little")
    assert P.dlmm_bin_step(bytes(raw)) is None, "seed and bin_step disagree: wrong layout, refused"


def test_an_unpriced_concentrated_swap_stays_at_its_paid_price_flagged():
    s = _cl_swaps(None)["meteora_dlmm"]
    assert s["basis"] == "trade_avg" and s["spot"] is None and s["paid"] > 0
    path, _flags, _t = P.build_path([s], [], [], primary=s["pool"], t_start=s["block_time"],
                                    t_end=s["block_time"])
    assert path[0]["price_quote"] == s["paid"] and path[0]["basis"] == "trade_avg"


# --- the path -------------------------------------------------------------------------------------------

def _swap(t, price, pool="P", venue="pump_amm", basis="trade_avg", slot=None, sig=None, **kw):
    d = {"signature": sig or "s%d%s" % (t, pool), "venue": venue, "pool": pool, "coin_vault": pool,
         "pool_owner": "O" + pool, "slot": slot if slot is not None else t, "tx_index": 0, "block_time": t,
         "basis": basis, "spot": price if basis in P.EXACT else None, "paid": price,
         "fee_bps_event": None, "coin_decimals": 6, "quote_decimals": 9, "coin_res": None,
         "quote_res": None, "v_quote": 0, "quote_mint": P._tape().WSOL}
    d.update(kw)
    return d


def test_empty_seconds_carry_the_last_price_flagged_stale_and_nothing_before_the_first_swap():
    swaps = [_swap(100, 1.0), _swap(103, 1.2), _swap(103, 1.3, slot=104)]
    path, _, _t = P.build_path(swaps, [], [], primary="P", t_start=99, t_end=104)
    assert [(r["t_s"], r["price_quote"], r["stale"], r["n_swaps"]) for r in path] == [
        (99, None, 1, 0), (100, 1.0, 0, 1), (101, 1.0, 1, 0), (102, 1.0, 1, 0), (103, 1.3, 0, 2),
        (104, 1.3, 1, 0)]


def test_other_pools_are_counted_never_priced():
    swaps = [_swap(100, 1.0), _swap(100, 9.0, pool="Q")]
    path, _, _t = P.build_path(swaps, [], [], primary="P", t_start=100, t_end=100)
    assert path[0]["price_quote"] == 1.0 and path[0]["n_other_pool"] == 1


def test_a_reserve_basis_that_disagrees_with_its_paid_prices_falls_back_to_paid():
    """A wrong virtual quote: every paid price sits 10% above the computed spot range."""
    swaps = [_swap(100 + i, 1.0 + i * 0.001, basis="pump_amm_vaults_v",
                   coin_res="1000000", quote_res="1000", paid=(1.0 + i * 0.001) * 1.10) for i in range(8)]
    path, flags, _t = P.build_path(swaps, [], [], primary="P", t_start=100, t_end=107)
    assert "P" in flags["reserve_mismatch"]
    assert path[-1]["basis"] == "trade_avg" and path[-1]["price_quote"] == pytest.approx(1.007 * 1.10)


def test_a_migrated_curve_continues_on_its_pumpswap_pool():
    swaps = [_swap(100, 1.0, pool="curve", venue="pump_curve"), _swap(101, 1.1, pool="curve", venue="pump_curve"),
             _swap(105, 2.0, pool="AMM", venue="pump_amm")]
    path, flags, _t = P.build_path(swaps, [], [], primary="curve", t_start=100, t_end=106)
    assert flags["migrated"]["pool"] == "AMM" and path[-1]["price_quote"] == 2.0


def test_choose_pool_refuses_an_ambiguous_pick_and_takes_a_clear_leader():
    two = [_swap(100 + i, 1.0) for i in range(4)] + [_swap(100 + i, 1.0, pool="Q") for i in range(3)]
    assert P.choose_pool(two)[:2] == (None, "ambiguous")
    clear = [_swap(100 + i, 1.0) for i in range(6)] + [_swap(100, 1.0, pool="Q")]
    assert P.choose_pool(clear)[:2] == ("P", "most_swaps_first_minute")


def test_the_sell_quote_pays_impact_the_venue_fee_and_the_coins_transfer_fee():
    state = _swap(100, 1.0, basis="pump_amm_vaults_v", coin_res=str(10 ** 12), quote_res=str(10 ** 12),
                  fee_bps=100)
    spot = P._spot("pump_amm_vaults_v", 10 ** 12, 10 ** 12, 6, 9)
    plain = P.executable_unit(state, 1000.0)
    taxed = P.executable_unit(state, 1000.0, tf=(300, 0))
    assert plain < spot * 0.99 and taxed == pytest.approx(plain * 0.97, rel=1e-3)
    assert P.executable_unit(dict(state, fee_bps=None), 1000.0) is None, "an unseen fee is unknown"


# --- the replay ------------------------------------------------------------------------------------------

@pytest.fixture
def cfg(monkeypatch):
    from sniper.settings import memecoin as c
    # Synthetic replays must not collide with earlier tests' positive funded-position IDs.
    # Keep the real witness tape and out-of-order guard; replay's finally clears its own IDs.
    monkeypatch.setattr(P, "_REPLAY_IDS", iter(range(-1, -10**9, -1)))
    for name, value in (("MEMECOIN_REAL_TP_CONFIRM_SECONDS", 0.0), ("MEMECOIN_REAL_NO_LOSS_SELL_S", 0.0),
                        ("MEMECOIN_REAL_STOP_INSTANT_PCT", 0.0), ("MEMECOIN_REAL_STOP_CONFIRM_SECONDS", 0.0),
                        ("MEMECOIN_VOLDEATH_ARMED", False), ("MEMECOIN_REAL_MIRROR_SIZE_CHAINS", frozenset()),
                        ("MEMECOIN_VOLDEATH_STATE_WITNESS", True)):
        monkeypatch.setattr(c, name, value, raising=False)
    return c


def _policy(**kw):
    from sniper.services import memecoin_funded_policy as fp
    args = dict(policy_id="test-v1", coin_band="nano", tp=25.0, derisk=0.0, trail=0.0, stop=35.0,
                trail_arm=0.0, trail_requires_derisk=False, mirror_on_sell=False, catastrophe=80.0,
                debounce=1)
    args.update(kw)
    return fp._policy(**args)


T0 = 1_790_000_000


def _path(prices, swaps_at=None, basis="curve_event", undecoded=()):
    """{offset: price} -> seconds rows from T0; a missing offset carries the last price, stale."""
    rows, last = [], None
    for i in range(max(prices) + 1):
        last = prices.get(i, last)
        n = 1 if (i in prices if swaps_at is None else i in swaps_at) else 0
        rows.append({"t_s": T0 + i, "slot": i, "price_quote": last, "exec_quote": None, "n_swaps": n,
                     "stale": int(not n), "basis": basis, "n_undecoded": int(i in undecoded)})
    return rows


def test_tp_decides_on_the_first_second_at_target_and_lands_after_the_lag(cfg):
    r = P.replay(_path({0: 1.0, 5: 1.1, 9: 1.26, 10: 1.30, 30: 1.30}), _policy(), None, mint="M",
                 fill_t=T0, fill_price=1.0, qty=100.0, exit_lag_s=1.0)
    assert (r["reason"], r["decided_t"] - T0, r["landed_t"] - T0) == ("real_tp", 9, 10)
    assert r["return_pct"] == pytest.approx(30.0)


def test_the_exit_lands_on_the_state_one_lag_later_whatever_the_decision_moment(cfg):
    """Second 5 has two swaps: the TP fires at the second one (5.001) and lands on the state at
    6.001, which is second 6's state (no swap there), not the end of second 7 (Gamma LOW 1)."""
    path = _path({0: 1.0, 5: 1.0, 6: 1.40, 7: 2.0, 30: 2.0})
    ticks = [{"t": T0 + 5, "price": 1.0, "exec": None, "basis": "curve_event"},
             {"t": T0 + 5, "price": 1.30, "exec": None, "basis": "curve_event"}]
    r = P.replay(path, _policy(), None, mint="M", fill_t=T0, fill_price=1.0, qty=100.0, ticks=ticks)
    assert r["decided_t"] == pytest.approx(T0 + 5.001) and r["exit_price_landed"] == 1.40


def test_a_stop_waits_out_its_confirmation_window_and_a_recovery_restarts_it(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SECONDS", 10.0)
    prices = {0: 1.0, 5: 0.6, 10: 1.0, 20: 0.6, 60: 0.6}         # dip 5 s, recover, dip again
    r = P.replay(_path(prices), _policy(), None, mint="M", fill_t=T0, fill_price=1.0, qty=100.0)
    assert r["reason"] == "b_stop" and r["decided_t"] - T0 == 30


def test_a_nano_trail_arms_only_after_its_derisk_leg(cfg):
    pol = _policy(tp=0.0, derisk=20.0, derisk_mode="fraction", derisk_fraction=0.5, trail=15.0,
                  trail_requires_derisk=True, stop=60.0)
    prices = {0: 1.0, 5: 1.15, 8: 0.96, 12: 1.25, 20: 1.5, 25: 1.27, 40: 1.27}
    r = P.replay(_path(prices), pol, None, mint="M", fill_t=T0, fill_price=1.0, qty=100.0)
    assert [l["reason"] for l in r["legs"]] == ["derisk", "real_trail"]
    assert r["legs"][0]["t"] - T0 == 13 and r["decided_t"] - T0 == 25
    assert r["return_pct"] == pytest.approx((50 * 1.25 + 50 * 1.27) / 100 * 100 - 100)


def test_degen_v4_mirrors_the_first_source_sell_after_the_fill(cfg):
    from sniper.services.memecoin_funded_policy import exit_trial_v4_policy
    trades = [(T0 - 3, "buy", 500.0), (T0 + 7, "sell", 10.0)]
    r = P.replay(_path({0: 1.0, 30: 1.0}), exit_trial_v4_policy(coin_band="nano"), None, mint="M",
                 fill_t=T0, fill_price=1.0, qty=100.0, source_trades=trades)
    assert r["reason"] == "mirror_first_sell" and r["decided_t"] - T0 == 7


def test_vol_death_is_the_runners_own_decision_on_the_paths_swaps(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_ARMED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_EXIT_S", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_MOVE_VETO_PCT", 5.0)
    quiet = P.replay(_path({0: 1.0, 120: 1.0}, swaps_at={0}), _policy(), None, mint="M",
                     fill_t=T0, fill_price=1.0, qty=100.0)
    assert quiet["reason"] == "vol_death" and quiet["decided_t"] - T0 == 30
    traded = P.replay(_path({0: 1.0, 120: 1.0}, swaps_at={0, 20}), _policy(), None, mint="M",
                      fill_t=T0, fill_price=1.0, qty=100.0)
    assert traded["reason"] == "vol_death" and traded["decided_t"] - T0 == 50


def test_a_paid_price_never_decides_a_rail(cfg):
    """trade_avg seconds carry the last swap's PAID price: one buy above spot can fake a TP."""
    r = P.replay(_path({0: 1.0, 9: 1.30, 30: 1.30}, basis="trade_avg"), _policy(), None, mint="M",
                 fill_t=T0, fill_price=1.0, qty=100.0)
    assert (r["reason"], r["censor"], r["would_be"]) == (None, "trade_avg_decision", "real_tp")


def test_the_replay_decides_after_each_swap_never_on_a_later_swap_of_the_same_second(cfg):
    """A TP touched by one swap and reverted by the next inside second 5: the end-of-second state
    misses it, the per-swap replay sees it at that swap."""
    path = _path({0: 1.0, 5: 1.0, 30: 1.0})
    ticks = [{"t": T0, "price": 1.0, "exec": None, "basis": "curve_event"},
             {"t": T0 + 5, "price": 1.30, "exec": None, "basis": "curve_event"},
             {"t": T0 + 5, "price": 1.00, "exec": None, "basis": "curve_event"}]
    seen = P.replay(path, _policy(), None, mint="M", fill_t=T0, fill_price=1.0, qty=100.0, ticks=ticks)
    assert seen["reason"] == "real_tp" and seen["decided_t"] == T0 + 5
    blind = P.replay(path, _policy(), None, mint="M", fill_t=T0, fill_price=1.0, qty=100.0)
    assert blind["reason"] is None
    late = [{"t": T0 + 5, "price": 1.00, "exec": None, "basis": "curve_event"},
            {"t": T0 + 5, "price": 1.30, "exec": None, "basis": "curve_event"}]
    assert P.replay(path, _policy(), None, mint="M", fill_t=T0, fill_price=1.0, qty=100.0,
                    ticks=late)["decided_t"] == pytest.approx(T0 + 5.001)


def test_vol_death_never_runs_for_the_v4_trial_and_an_unseen_tx_censors_it(cfg, monkeypatch):
    from sniper.services.memecoin_funded_policy import exit_trial_v4_policy
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_ARMED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_EXIT_S", 30.0)
    quiet = _path({0: 1.0, 120: 1.0}, swaps_at={0})
    v4 = P.replay(quiet, exit_trial_v4_policy(coin_band="nano"), None, mint="M", fill_t=T0,
                  fill_price=1.0, qty=100.0)
    assert v4["reason"] != "vol_death"
    unseen = P.replay(_path({0: 1.0, 120: 1.0}, swaps_at={0}, undecoded={12}), _policy(), None,
                      mint="M", fill_t=T0, fill_price=1.0, qty=100.0)
    assert (unseen["censor"], unseen["would_be"]) == ("vol_death_unseen_txs", "vol_death")


def test_nothing_firing_is_censored_open_at_horizon_never_closed_at_the_last_bar(cfg):
    r = P.replay(_path({0: 1.0, 60: 1.05}), _policy(), None, mint="M", fill_t=T0, fill_price=1.0,
                 qty=100.0)
    assert r["reason"] is None and r["censor"] == "open_at_horizon" and "return_pct" not in r


# --- the reader --------------------------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_the_backfill_stops_the_moment_the_book_opens_and_sends_nothing_after():
    sent, flat = [], iter([True, True, False])
    clock = _Clock()
    call = P.LowPriorityRpc(lambda m, p: sent.append(m) or {}, flat_fn=lambda: next(flat),
                            pacer_state_fn=dict, clock=clock, sleep=clock.sleep)
    call("a", [])
    call("b", [])
    with pytest.raises(P.ReadStop):
        call("c", [])
    assert sent == ["a", "b"]


def test_the_backfill_keeps_its_own_gap_and_never_jumps_a_reservation_ahead():
    clock = _Clock()
    state = {"last": clock.t + 7.0, "paused_until": 0.0}
    call = P.LowPriorityRpc(lambda m, p: clock.t, flat_fn=lambda: True, pacer_state_fn=lambda: state,
                            clock=clock, sleep=clock.sleep)
    first = call("a", [])
    assert first >= 1007.0, "another reader's reserved slot goes first"
    state["last"] = 0.0
    second = call("b", [])
    assert second - first >= P.MIN_GAP_S


def test_a_429_waits_the_shared_pause_plus_sixty_seconds_then_retries():
    clock = _Clock()
    state = {"last": 0.0, "paused_until": 0.0}
    answers = iter([urllib.error.HTTPError("u", 429, "limit", {}, None), "ok"])

    def rpc(_m, _p):
        a = next(answers)
        if isinstance(a, Exception):
            state["paused_until"] = clock.t + 60.0           # what the shared pacer records
            raise a
        return a
    call = P.LowPriorityRpc(rpc, flat_fn=lambda: True, pacer_state_fn=lambda: state, clock=clock,
                            sleep=clock.sleep)
    assert call("m", []) == "ok"
    assert sum(clock.slept) >= 120.0


def _fake_tape_rpc(tx, mint):
    """getTransactionsForAddress: one copy of `tx` per slot, 100 a page, paginated by slot."""
    def rpc(method, params):
        assert method == "getTransactionsForAddress" and params[0] == mint
        opts = params[1]
        lo, hi = opts["filters"]["slot"]["gte"], opts["filters"]["slot"]["lte"]
        start = int(opts.get("paginationToken") or lo)
        out = []
        for s in range(start, min(hi, start + 99) + 1):
            t = copy.deepcopy(tx)
            t["slot"], t["blockTime"] = s, T0 + s
            t["transaction"]["signatures"] = ["sig%d" % s]
            out.append(t)
        return {"data": out, "paginationToken": str(start + 100) if start + 100 <= hi else None}
    return rpc


def test_the_page_cap_censors_the_path_and_names_where():
    tx, mint, _ = CASES["pump_curve"]
    db = P.connect(":memory:")
    covered, pages, censor = P.read_window(db, _fake_tape_rpc(tx, mint), {"row_id": "r", "mint": mint},
                                           lo=1000, hi=5000, page_cap=3)
    assert (censor, pages) == ("page_cap", 3) and covered < 1300


def test_a_book_that_opens_mid_read_keeps_every_batch_already_stored():
    tx, mint, _ = CASES["pump_curve"]
    db = P.connect(":memory:")
    calls = {"n": 0}
    inner = _fake_tape_rpc(tx, mint)
    clock = _Clock()

    def flat():
        calls["n"] += 1
        return calls["n"] <= P.BATCH_PAGES

    call = P.LowPriorityRpc(inner, flat_fn=flat, pacer_state_fn=dict, clock=clock, sleep=clock.sleep)
    with pytest.raises(P.ReadStop):
        P.read_window(db, call, {"row_id": "r", "mint": mint}, lo=1000, hi=9000, page_cap=600)
    assert db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == P.BATCH_PAGES
    assert db.execute("SELECT COUNT(DISTINCT slot) FROM pool_swaps").fetchone()[0] == 1500   # 10 chunks


# --- the input --------------------------------------------------------------------------------------------

def test_a_row_missing_what_the_replay_needs_is_refused_by_name():
    ok = {"row_id": "a", "cohort": "c", "mint": "M", "clip_usd": 50, "source_user_id": "u",
          "decision_at": "2026-10-01T00:00:00+00:00", "fill_slot": 1, "fill_price_quote": 1e-7}
    assert P.parse_row(ok)[1] is None
    assert P.parse_row(dict(ok, fill_slot=None))[1] == "missing fill_slot and fill_at"
    assert P.parse_row(dict(ok, fill_price_quote=None))[1] == "missing fill_price_quote and fill_price_usd"
    assert P.parse_row(dict(ok, band="huge"))[1] == "unknown band 'huge'"
    assert P.parse_row(dict(ok, decision_at="2026-10-01T00:00:00"))[1].startswith("bad value")


def test_one_row_runs_end_to_end_on_a_fake_key(tmp_path, cfg, monkeypatch):
    """The whole row: anchor, chunked reads, pool choice, the path files and one replay record."""
    from sniper.persistence import db_memecoin
    tx, mint, _ = CASES["pump_curve"]
    tape = _fake_tape_rpc(tx, mint)

    def rpc(method, params):
        if method == "getTransactionsForAddress":
            return ({"data": [], "paginationToken": None} if params[1].get("sortOrder") == "desc"
                    else tape(method, params))
        if method == "getBlockTime":
            return T0 + params[0]
        if method == "getAccountInfo":
            return {"value": None}
        raise AssertionError(method)
    monkeypatch.setattr(P, "policies_for", lambda _led, _row: [
        {"band": "nano", "policy": _policy(), "copy_arm": None, "exit_trial": None}])
    monkeypatch.setattr(db_memecoin, "own_tx_hashes", lambda *_a: set())
    clock = _Clock()
    call = P.LowPriorityRpc(rpc, flat_fn=lambda: True, pacer_state_fn=dict, clock=clock, sleep=clock.sleep)
    row = {"row_id": "r1", "cohort": "c", "mint": mint, "fill_slot": 2000, "fill_price_quote": 1.4e-7,
           "clip_usd": 50, "source_user_id": "u", "source_address": "X", "sol_usd": 120.0,
           "decision_at": "2026-10-01T12:00:00+00:00", "window_s": 60}
    records = P.run_row(P.connect(":memory:"), call, None, row, out_dir=tmp_path, until_exit=True)
    assert [(r["band"], r["censor"]) for r in records] == [("nano", "open_at_horizon")]
    assert all(r["replay_version"] == P.REPLAY_VERSION for r in records)
    header = json.loads((tmp_path / "r1.path.jsonl").read_text().splitlines()[0])
    assert header["primary"] == "curve" and header["censor"] is None and header["sol_usd"] == 120.0
    assert len((tmp_path / "r1.path.jsonl").read_text().splitlines()) == 1 + 60 + 61


# --- the fill anchor: slot_at (Gamma 2026-10-06: tries=8 exhausted every row, up to 393 s late) -------

def _chain(slot_s, *, head=450_000_000, n=200_000, skip=0.03, stall=None, seed=1):
    """A fake chain: slot -> integer blockTime, `slot_s` seconds per slot, some slots skipped,
    optionally a stall (no blocks) of `stall` = (after_slot, seconds)."""
    import random
    rnd, times, t = random.Random(seed), {}, 1_790_000_000.0
    for s in range(head - n, head + 1):
        t += slot_s() if callable(slot_s) else slot_s
        if stall and s == stall[0]:
            t += stall[1]
        if rnd.random() > skip:
            times[s] = int(t)
    calls = []

    def rpc(method, params):
        calls.append(method)
        if method == "getSlot":
            return head
        if params[0] not in times:
            raise RuntimeError("skipped")
        return times[params[0]]
    return rpc, times, calls


@pytest.mark.parametrize("slot_s", [0.4, 0.2, 0.55])
def test_slot_at_lands_in_the_right_second_whatever_the_real_slot_time(slot_s):
    rpc, times, calls = _chain(slot_s)
    head = max(times)
    for back in (300, 20_000, 55_000):
        target = next(s for s in range(head - back, head) if s in times)
        t = times[target] + 0.5
        calls.clear()
        slot, bt = P.slot_at(rpc, t)
        assert 0 <= t - bt < 1.0, (slot_s, back, t - bt)
        assert len(calls) <= 24, len(calls)


def test_slot_at_censors_a_time_with_no_block_within_two_seconds():
    rpc, times, _calls = _chain(0.4, stall=(449_990_000, 120.0))     # a 2-minute halt
    t = times[449_990_000] - 60.0                    # inside the halt (the slot after it ends it)
    with pytest.raises(P.RowCensor, match="fill_at_unresolved"):
        P.slot_at(rpc, t)


def _gap_chain(slot_s, gap, seed):
    """A chain with jittered slot times and a run of `gap` skipped slots just before the target."""
    import random
    rnd = random.Random(seed)
    rpc, times, calls = _chain(lambda: slot_s * (0.7 + 0.6 * rnd.random()), seed=seed)
    head = max(times)
    target = next(s for s in range(head - 30_000, head) if s in times)
    for s in range(target + 1, target + 1 + gap):
        times.pop(s, None)
    return rpc, times, calls, target


@pytest.mark.parametrize("gap", [12, 40])
def test_a_long_run_of_skipped_slots_censors_the_row_never_ends_the_run(gap):
    """Gamma on 7b1fb10a2: a probe landing in more than 8 skipped slots raised ReadStop, which ends
    the whole run. It is this row's unknown, so the row is censored; the head read keeps ReadStop."""
    for seed in range(40):
        rpc, times, _calls, target = _gap_chain(0.4, gap, seed)
        try:
            P.slot_at(rpc, times[target] + 0.5)
        except P.RowCensor:
            pass                                   # censored or resolved; never ReadStop


@pytest.mark.parametrize("slot_s", [0.2, 0.4, 0.55])
@pytest.mark.parametrize("gap", [0, 5])
def test_slot_at_returns_a_consistent_block_within_one_second_at_or_before_t(slot_s, gap):
    """Jittered slot times and a short skipped run after the target: the tuple is a real
    (slot, blockTime), at or before t and inside its second, within the read budget."""
    for seed in range(30):
        rpc, times, calls, target = _gap_chain(slot_s, gap, seed)
        t = times[target] + 0.5
        calls.clear()
        slot, bt = P.slot_at(rpc, t)
        assert times[slot] == bt and 0 <= t - bt < 1.0, (slot_s, gap, seed)
        assert len(calls) <= 24


def test_a_head_with_no_block_nearby_still_stops_the_run():
    rpc, times, _calls = _chain(0.4)
    head = max(times)
    for s in range(head - 10, head + 1):
        times.pop(s, None)
    with pytest.raises(P.ReadStop):
        P.slot_at(rpc, times[max(times)] - 100.0)


def test_a_row_with_an_attempt_takes_its_fill_slot_from_the_decision_trace():
    import sqlite3
    led = sqlite3.connect(":memory:")
    led.execute("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, trace_json TEXT)")
    led.execute("INSERT INTO memecoin_entry_attempts VALUES (?,?)", ("a1", json.dumps({
        "substage_events": [{"name": "attempt_row"},
                            {"name": "quote_fallback", "buy_context_slot": 452120872}]})))
    assert P.fill_slot_from_trace(led, "a1") == 452120872
    assert P.fill_slot_from_trace(led, "missing") is None
    led.execute("INSERT INTO memecoin_entry_attempts VALUES ('a2', '{}')")
    assert P.fill_slot_from_trace(led, "a2") is None


def test_an_unresolved_fill_anchor_censors_the_row_and_the_run_goes_on(tmp_path, monkeypatch):
    rows = [{"row_id": "r%d" % i, "cohort": "c", "mint": "M", "fill_at": "2026-10-01T12:00:00+00:00",
             "fill_price_quote": 1.0, "clip_usd": 50, "source_user_id": "u",
             "decision_at": "2026-10-01T12:00:00+00:00"} for i in (1, 2)]
    seen = []

    def run_row(_db, _call, _led, row, **_kw):
        seen.append(row["row_id"])
        raise P.RowCensor("fill_at_unresolved: nearest block 60 s away")
    monkeypatch.setattr(P, "run_row", run_row)
    out = P.run_rows(P.connect(":memory:"), None, None, rows, out_dir=tmp_path)
    assert seen == ["r1", "r2"]
    assert [r["kind"] for r in out["records"]] == ["censored", "censored"]
    assert out["summary"]["censored"] == 2 and out["summary"]["stopped"] is None

# --- R3 (Codex review 2026-10-06): an event binds to its own pool, never to an equal amount elsewhere

def _cpmm_two_pool_tx(reverse=False):
    """Two zero-fee CPMM swaps of 100 coins in one transaction: pool1 9900/10000 for 100 quote,
    pool2 9990/100000 for 10 quote. Each CPMM instruction lists its own pool state and vaults."""
    import base64
    import dataclasses
    import struct
    import test_slot_lane_cpmm as TC
    from solders.pubkey import Pubkey
    from sniper.execution import raydium_cpmm_buy as C
    pool1, pool2 = str(Pubkey.from_bytes(bytes([1]) * 32)), str(Pubkey.from_bytes(bytes([2]) * 32))
    e1 = dataclasses.replace(TC.USDC_EV, pool=pool1, input_vault_before=9900, output_vault_before=10000,
                             input_amount=100, output_amount=100, input_transfer_fee=0,
                             output_transfer_fee=0, trade_fee=0, creator_fee=0)
    e2 = dataclasses.replace(e1, pool=pool2, input_vault_before=9990, output_vault_before=100000,
                             input_amount=10)

    def line(e):
        raw = (C.SWAP_EVENT_DISCRIMINATOR + bytes(Pubkey.from_string(e.pool))
               + struct.pack("<6Q", e.input_vault_before, e.output_vault_before, e.input_amount,
                             e.output_amount, e.input_transfer_fee, e.output_transfer_fee)
               + bytes([e.base_input]) + bytes(Pubkey.from_string(e.input_mint))
               + bytes(Pubkey.from_string(e.output_mint)) + struct.pack("<2Q", e.trade_fee, e.creator_fee)
               + bytes([e.creator_fee_on_input]))
        return "Program data: " + base64.b64encode(raw).decode()

    keys = [C.CPMM_PROGRAM, pool1, "vault1", pool2, "vault2"]
    ixs = [{"programIdIndex": 0, "accounts": [1, 2], "data": ""},
           {"programIdIndex": 0, "accounts": [3, 4], "data": ""}]
    logs = []
    for e in (e1, e2):
        logs += ["Program %s invoke [1]" % C.CPMM_PROGRAM, line(e), "Program %s success" % C.CPMM_PROGRAM]
    if reverse:
        ixs, logs = ixs[::-1], logs[3:] + logs[:3]
    tx = {"transaction": {"message": {"accountKeys": keys, "instructions": ixs}},
          "meta": {"logMessages": logs, "innerInstructions": []}}
    swaps = [{"coin_vault": v, "pool_owner": C.CPMM_AUTHORITY, "venue": "raydium_cpmm", "coin_delta": -100}
             for v in ("vault1", "vault2")]
    return tx, e1.output_mint, swaps


@pytest.mark.parametrize("reverse", [False, True])
def test_each_cpmm_event_prices_only_its_own_pool(reverse):
    """The review's repro: both vaults took pool1's 9900/10000, a 10.09x error on pool2."""
    tx, mint, swaps = _cpmm_two_pool_tx(reverse)
    got = P.venue_states(tx, mint, swaps)
    assert got["vault1"][:3] == ("cpmm_event", 9900, 10000)
    assert got["vault2"][:3] == ("cpmm_event", 99900, 10000)


def test_an_event_whose_pool_no_instruction_binds_to_the_vault_is_not_used():
    tx, mint, swaps = _cpmm_two_pool_tx()
    tx["transaction"]["message"]["instructions"] = []
    assert P.venue_states(tx, mint, swaps) == {}


# --- R4: a liquidity event on the pool ends the executable state until the next swap ---------------

def test_a_liquidity_event_invalidates_the_executable_quote_until_the_next_swap():
    """The review's case: 1,000/1,000 reserves sell 100 coins for 0.909/coin; a 90% withdrawal
    leaves 100/100 (0.5/coin), and the next second still showed 0.909 with lp_event=1."""
    def cp(t, coin, quote, tx=1):
        return _swap(t, quote / coin, pool="P", venue="pump_amm", basis="pump_amm_vaults_v",
                     coin_res=str(coin), quote_res=str(quote), coin_decimals=0, quote_decimals=0,
                     fee_bps_event=0, tx_index=tx)
    swaps = [cp(100, 1000, 1000), cp(103, 100, 100, tx=2)]
    lp = [{"coin_vault": "P", "block_time": 101, "slot": 101, "tx_index": 0},
          {"coin_vault": "P", "block_time": 103, "slot": 103, "tx_index": 1}]     # before the 103 swap
    path, _, _t = P.build_path(swaps, lp, [], primary="P", t_start=100, t_end=104, qty=100)
    by = {r["t_s"]: r for r in path}
    assert by[100]["exec_quote"] == pytest.approx(1000 * 100 / 1100 / 100)
    assert by[101]["exec_quote"] is None and by[102]["exec_quote"] is None
    assert by[101]["exec_lp_stale"] == 1 and by[101]["lp_event"] == 1
    assert by[103]["exec_quote"] == pytest.approx(100 * 100 / 200 / 100) and by[103]["exec_lp_stale"] == 0
    assert by[104]["exec_quote"] == pytest.approx(0.5)


def test_another_pools_liquidity_event_leaves_the_primary_executable():
    swaps = [_swap(100, 1.0, basis="pump_amm_vaults_v", coin_res="1000", quote_res="1000",
                   coin_decimals=0, quote_decimals=0, fee_bps_event=0)]
    lp = [{"coin_vault": "Q", "block_time": 101, "slot": 101, "tx_index": 0}]
    path, _, _t = P.build_path(swaps, lp, [], primary="P", t_start=100, t_end=101, qty=100)
    assert path[-1]["exec_quote"] is not None and path[-1]["exec_lp_stale"] == 0


# --- R6: the book is checked after every wait, at the send ------------------------------------------

def test_a_book_that_opens_during_the_backfills_own_wait_sends_nothing():
    clock, sent = _Clock(), []
    flat = {"v": True}
    state = {"last": clock.t + 2.0, "paused_until": 0.0}

    def sleep(s):
        clock.sleep(s)
        flat["v"] = False                                 # the book opens while we wait
    call = P.LowPriorityRpc(lambda m, p: sent.append(m), flat_fn=lambda: flat["v"],
                            pacer_state_fn=lambda: state, clock=clock, sleep=sleep)
    with pytest.raises(P.ReadStop):
        call("a", [])
    assert sent == []


def test_a_book_that_opens_inside_the_shared_pacers_wait_sends_nothing(monkeypatch):
    import urllib.request
    from sniper.services.memecoin_slot_end_shadow import ResearchRpc
    flat = {"v": True}

    class Pacer:
        path = "unused"

        def acquire(self):
            flat["v"] = False                             # the book opens inside the pacer's wait

        def pause(self, s):
            pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("sent after the book opened"))
    rpc = ResearchRpc("https://research.invalid/", pacer=P.FlatAtSend(Pacer(), lambda: flat["v"]))
    with pytest.raises(P.ReadStop):
        rpc("getTransactionsForAddress", [])


# --- R9: numbers are finite, positive and not booleans; a slot is a non-negative integer -------------

@pytest.mark.parametrize("field,value", [
    ("clip_usd", float("inf")), ("clip_usd", float("nan")), ("clip_usd", True), ("clip_usd", 0),
    ("fill_price_quote", True), ("fill_price_quote", -1e-7), ("sol_usd", float("-inf")),
    ("fill_slot", -1), ("fill_slot", True), ("fill_slot", 1.5), ("fill_slot", "x")])
def test_a_nonfinite_boolean_or_negative_input_is_refused(field, value):
    ok = {"row_id": "a", "cohort": "c", "mint": "M", "clip_usd": 50, "source_user_id": "u",
          "decision_at": "2026-10-01T00:00:00+00:00", "fill_slot": 1, "fill_price_quote": 1e-7}
    assert P.parse_row(dict(ok, **{field: value}))[1] is not None
    assert P.parse_row(dict(ok, clip_usd="50", fill_slot="12"))[1] is None   # JSON string numbers


# --- N10 (Codex night review 2026-10-06): Unicode digits and integer overflow --------------------

@pytest.mark.parametrize("field,value", [
    ("fill_slot", "²"), ("fill_slot", "١٢"), ("fill_slot", "12 "), ("fill_slot", 10 ** 30),
    ("clip_usd", 10 ** 1000), ("fill_price_usd", 10 ** 400), ("sol_usd", "1e999")])
def test_a_unicode_digit_or_oversized_number_is_refused_never_raised(field, value):
    ok = {"row_id": "a", "cohort": "c", "mint": "M", "clip_usd": 50, "source_user_id": "u",
          "decision_at": "2026-10-01T00:00:00+00:00", "fill_slot": 1, "fill_price_quote": 1e-7}
    row, why = P.parse_row(dict(ok, **{field: value}))
    assert row is None and why
    assert P.parse_row(dict(ok, fill_slot="454092431"))[1] is None
    assert P.parse_row(dict(ok, fill_slot=454092431))[1] is None


# --- S1 (Codex two-day review 2026-10-07): an LP change censors the replay, never a mid price ------

def _lp_case(lp_at, *, swaps=(), sell_at=None, tx=1, t_end=12):
    """An exact pump_amm swap at T0, then `swaps` [(t, tx_index)], LP rows at `lp_at`
    [(t, tx_index)] on the primary pool, and a mirror replay on a source sell at `sell_at`."""
    def cp(t, txi):
        return _swap(t, .001, basis="pump_amm_vaults_v", coin_res=str(10 ** 12), quote_res=str(10 ** 12),
                     fee_bps_event=100, tx_index=txi)
    rows = [cp(T0, 0)] + [cp(t, i) for t, i in swaps]
    lp = [dict(coin_vault="P", block_time=t, slot=t, tx_index=i) for t, i in lp_at]
    path, _flags, ticks = P.build_path(rows, lp, [], primary="P", t_start=T0, t_end=T0 + t_end, qty=100.)
    return P.replay(path, _policy(mirror_on_sell=True), None, mint="M", fill_t=T0, fill_price=.001,
                    qty=100., ticks=ticks, source_trades=[(sell_at, "sell", 1.)] if sell_at else [])


def test_an_exit_after_an_lp_change_is_censored_never_priced_on_the_mid(cfg):
    """The audit's repro: swap at T0, LP change at T0+1, source sell at T0+2. Before: a numeric
    mirror exit at 0.001 with exec_from_mid."""
    r = _lp_case([(T0 + 1, 0)], sell_at=T0 + 2)
    assert r["censor"] == "lp_unknown_depth" and r["reason"] == "mirror"
    assert "return_pct" not in r and "exec_from_mid" not in r.get("flags", [])


def test_swap_then_lp_in_one_second_stays_invalid_and_lp_then_swap_restores(cfg):
    stale = _lp_case([(T0 + 1, 1)], swaps=[(T0 + 1, 0)], sell_at=T0 + 1)
    assert stale["censor"] == "lp_unknown_depth"
    fresh = _lp_case([(T0 + 1, 0)], swaps=[(T0 + 1, 1)], sell_at=T0 + 2)
    assert fresh.get("censor") is None and fresh["reason"] == "mirror" and fresh["return_pct"] is not None


def test_the_landing_lookup_never_scans_back_past_an_lp_change(cfg):
    r = _lp_case([(T0 + 1, 0)], sell_at=T0 + 4)
    assert r["censor"] == "lp_unknown_depth"


def test_a_later_swap_restores_scoring_without_erasing_the_censored_interval(cfg):
    early = _lp_case([(T0 + 1, 0)], swaps=[(T0 + 6, 0)], sell_at=T0 + 3)
    assert early["censor"] == "lp_unknown_depth"
    late = _lp_case([(T0 + 1, 0)], swaps=[(T0 + 6, 0)], sell_at=T0 + 7)
    assert late.get("censor") is None and late["return_pct"] is not None


def test_a_price_rail_holds_while_depth_is_unknown_and_lands_censored_after_an_lp_change(cfg):
    """A TP nominated at an invalid moment holds (no mid sizing or pricing); a TP decided on a swap
    that an LP change follows in the same second lands censored."""
    held = _path({0: 1.0, 2: 1.3, 10: 1.3})
    for r in held[1:]:
        r["exec_lp_stale"] = 1
    r = P.replay(held, _policy(), None, mint="M", fill_t=T0, fill_price=1.0, qty=100.0)
    assert r["censor"] == "open_at_horizon" and "lp_unknown_depth_hold" in r["flags"]
    path = _path({0: 1.0, 2: 1.3, 10: 1.3})
    for row in path[2:]:
        row["exec_lp_stale"] = 1
    ticks = [{"t": T0 + 2, "price": 1.3, "exec": None, "basis": "curve_event"}, {"t": T0 + 2, "lp": True}]
    r = P.replay(path, _policy(), None, mint="M", fill_t=T0, fill_price=1.0, qty=100.0, ticks=ticks)
    assert (r["reason"], r["censor"]) == ("real_tp", "lp_unknown_depth")


def test_the_replay_version_marks_the_lp_censor():
    assert P.REPLAY_VERSION == 2                # bump with any change to what a replay returns
