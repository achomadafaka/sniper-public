"""Paper exit decisions on on-chain ticks, onchain_ticks_v1 (spec
docs/superpowers/specs/2026-10-08-memecoin-paper-exit-onchain-ticks-design.md): the engine freezes at
entry, the 15 s sweep never decides a v1 position (the 14-day backstop aside), the same rail
function decides on the pool's own state, a missing or stale tick holds the price rails while the
time rails fire, and the 56 poller stays inside the dRPC account's rules."""
import json
import math
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_exits as EX
from sniper.services import memecoin_paper_ticks as PT

NATIVE = "0x" + "00" * 20
MINT = "0x" + "ab" * 20
POOL = "0x" + "c4" * 32
T0 = 1_791_000_000.0
OPENED = "2026-10-08T07:00:00+00:00"
ETH_USD = 1000.0
BLOCK_HASH = "0x" + "aa" * 32


def _sqrt_for(usd):
    """The V4 sqrt price (native currency0, coin currency1, 18/18 decimals) at `usd` per coin."""
    return int(2 ** 96 * math.sqrt(ETH_USD / usd))


def _tape(tmp_path, *, usd=1.0, kind="v3_swap", received=T0, gap=False, decimals=18,
          lag=(1.0, 1.0), lag_observed=None, lag_table=True):
    """`lag` = the writer's (v4_lag_s, v3_lag_s) in volume_feed_lag, observed at `lag_observed`
    (default: with the feed); `lag_table=False` is a store from before that table."""
    path = str(tmp_path / "tape.db")
    c = sqlite3.connect(path)
    if lag_table:
        c.execute("CREATE TABLE volume_feed_lag (chain_id TEXT PRIMARY KEY, v4_lag_s REAL, v3_lag_s REAL, "
                  "samples INTEGER, observed REAL, pons_lag_s REAL)")
        c.execute("INSERT INTO volume_feed_lag VALUES ('4663', ?, ?, 10, ?, NULL)",
                  (lag[0], lag[1], received if lag_observed is None else lag_observed))
    c.executescript("""
        CREATE TABLE volume_feed (chain_id TEXT, since REAL, through REAL, received REAL, logs_seen INTEGER);
        CREATE TABLE block_times (chain_id TEXT, block_no INTEGER, block_ts TEXT, block_hash TEXT,
            PRIMARY KEY (chain_id, block_no));
        CREATE TABLE tick_gaps (id INTEGER PRIMARY KEY, chain_id TEXT, from_block INTEGER,
            to_block INTEGER, reason TEXT, detected_at TEXT, resolved_at TEXT);
        CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT, quote_side INTEGER,
            resolved_at TEXT, token0_decimals INTEGER, token1_decimals INTEGER, resolve_state TEXT,
            fee INTEGER, tick_spacing INTEGER, hooks TEXT);
        CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY, chain_id TEXT, pool TEXT, kind TEXT,
            block_no INTEGER, tx_sig TEXT, log_index INTEGER, maker TEXT, amount0 TEXT, amount1 TEXT,
            observed_at TEXT, tick INTEGER, tick_lower INTEGER, tick_upper INTEGER, liquidity TEXT,
            sqrt_price_x96 TEXT, block_hash TEXT);""")
    c.execute("INSERT INTO volume_feed VALUES ('4663', 0, 0, ?, 1)", (received,))
    observed = datetime.fromtimestamp(T0, timezone.utc).isoformat()
    c.execute("INSERT INTO block_times VALUES ('4663', 10, ?, ?)", (observed, BLOCK_HASH))
    if gap:
        c.execute("INSERT INTO tick_gaps (chain_id, from_block, to_block) VALUES ('4663', 1, 2)")
    c.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, token0_decimals, "
              "token1_decimals) VALUES ('4663', ?, ?, ?, 0, 18, ?)", (POOL, NATIVE, MINT, decimals))
    c.execute("INSERT INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig, log_index, observed_at, "
              "sqrt_price_x96, block_hash) VALUES ('4663', ?, ?, 10, '0xsrc', 0, ?, ?, ?)",
              (POOL, kind, observed, str(_sqrt_for(usd)) if kind == "v3_swap" else None, BLOCK_HASH))
    c.commit()
    c.close()
    return path


def _set_price(path, usd, received=T0):
    c = sqlite3.connect(path)
    c.execute("UPDATE pool_ticks SET sqrt_price_x96=?, observed_at=?",
              (str(_sqrt_for(usd)), datetime.fromtimestamp(received, timezone.utc).isoformat()))
    c.execute("UPDATE volume_feed SET received=?", (received,))
    c.execute("UPDATE volume_feed_lag SET observed=?", (received,))
    c.commit()
    c.close()


def _ledger():
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c, m


def _open(c, m, *, engine=PT.V1, network="4663", pool=POOL, arm="funded_clone", policy=None,
          route=None):
    rails = {"tp_pct": 25.0, "stop_pct": 35.0, "catastrophe_pct": 80.0, "derisk_pct": 0.0,
             "trail_pct": 0.0, "trail_arm_pct": 0.0, "max_hold_s": 780.0,
             "unquotable_exit_s": 300.0, "stop_debounce_sweeps": 1}
    rails.update(policy or {})
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint=MINT, ticker="T", entry_price=1.0,
        entry_notional=100.0, entry_fill_source="paper_quote", entry_lag_s=0.0, their_size_usd=0.0,
        opened_at=OPENED, strategy=arm, execution_mode="paper", network_id=network)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source="paper_quote", fraction=1.0, created_at=OPENED)
    params = {"paper_max_hold_s": 1209600.0, "paper_unquotable_s": 300.0, "mirror_on_sell": False}
    if engine == PT.V1:
        params["exit_engine"] = PT.V1
    c.execute("UPDATE memecoin_positions SET assigned_policy_json=?, experiment_status='eligible', "
              "paper_tick_json=? WHERE id=?",
              (json.dumps({"exit_policy": rails, "params": params}),
               json.dumps({"binding": "entry_route", "pool": pool, **({"route": route} if route else {})})
               if pool else None, pid))
    c.commit()
    return pid


def _engine(c, tape_path, now, *, iso="2026-10-08T07:00:30+00:00", bsc=None):
    clock = {"t": now}
    eng = PT.PaperTickEngine(connect=lambda: c, now_fn=lambda: clock["iso"], tape_path=tape_path,
                             evm=PT.EvmTapeTicks(lambda chain, token: ETH_USD), bsc=bsc,
                             clock=lambda: clock["t"], max_age_s=5.0, bsc_period_s=5.0)
    clock["iso"] = iso
    return eng, clock


def _row(c, pid):
    return dict(c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())


# ------------------------------------------------------------------------ freeze at entry

def test_the_engine_freezes_into_study_arms_only_and_v0_bytes_are_unchanged(monkeypatch):
    from sniper.services import memecoin_policy as PO
    from sniper.settings import memecoin as S
    monkeypatch.setattr(S, "MEMECOIN_FOLLOW3050_ALL_WALLETS", True)        # production's resolver
    monkeypatch.setattr(S, "MEMECOIN_PAPER_EXIT_ENGINE", PT.V0)
    c, _m = _ledger()
    v0 = PO.resolve_paper_arm_policy("funded_clone", entry_mcap=100_000.0, network_id="solana",
                                     conn=c, source_user_id="u1")
    assert "exit_engine" not in json.loads(v0[2])["params"]
    monkeypatch.setattr(S, "MEMECOIN_PAPER_EXIT_ENGINE", PT.V1)
    v1 = PO.resolve_paper_arm_policy("funded_clone", entry_mcap=100_000.0, network_id="solana",
                                     conn=c, source_user_id="u1")
    assert json.loads(v1[2])["params"]["exit_engine"] == PT.V1 and v1[0] != v0[0]
    control = PO.resolve_paper_arm_policy("control")
    assert "exit_engine" not in json.loads(control[2])["params"]       # not a v1 arm


def test_entry_binding_takes_the_quotes_own_pool():
    assert PT.entry_binding("solana", {"amm_keys": ["A", "B"], "route_plan": [1, 2]}) == {
        "binding": "entry_route", "pool": "B"}
    v4 = {"protocol": "uniswap_v4", "pool_id": POOL.upper().replace("0X", "0x"), "pool_key": {"x": 1}}
    assert PT.entry_binding("4663", v4)["pool"] == POOL
    v2 = {"protocol": "uniswap_v2", "tokens": [MINT, NATIVE], "market_pool": "0xAA", "router": "0xr"}
    got = PT.entry_binding("56", v2)
    assert got["pool"] == "0xaa" and got["route"] == v2
    assert PT.entry_binding("56", {"protocol": "uniswap_v2"}) is None
    assert PT.entry_binding("solana", None) is None


def test_a_paper_entry_persists_its_quotes_pool(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_copy_trader as CT

    def fill(mint, network_id, size_usd, *, quote_provenance=None):
        quote_provenance.update(observed_at="2026-10-08T07:00:00+00:00", provider="evm_route",
                                route_binding={"protocol": "uniswap_v4", "pool_id": POOL,
                                               "pool_key": {"fee": 0}}, fingerprint="f",
                                _token_decimals=9)
        return 1.0, "route_quote"
    monkeypatch.setattr(CT, "executable_entry_fill", fill)
    from sniper.settings import memecoin as S
    monkeypatch.setattr(S, "MEMECOIN_FOLLOW3050_ALL_WALLETS", True)
    c, m = _ledger()
    ev = SimpleNamespace(side="buy", mint=MINT, network_id="4663", source_user_id="u1",
                         source_handle="h", ticker="T", lag_s=0.0, their_size_usd=10.0,
                         their_price=1.0, fomo_event_id="swap:raybot:0xsrc:0xw:" + MINT,
                         signal_source="onchain")
    out = CT.copy_event_to_arms(c, ev, 100.0, OPENED, arms=["funded_clone"],
                                stats_fn=lambda *a, **k: {"price": 1.0, "liquidity": 1e6})
    assert out == {"funded_clone": "opened"}, out
    row = c.execute("SELECT executed_pool, executed_pool_source, paper_tick_json FROM "
                    "memecoin_positions").fetchone()
    assert (row[0], row[1]) == (POOL, "paper_quote_route")
    assert json.loads(row[2]) == {"binding": "entry_route", "pool": POOL, "coin_decimals": 9,
                                  "route": {"protocol": "uniswap_v4", "pool_id": POOL,
                                            "pool_key": {"fee": 0}}}


# ------------------------------------------------------------------------ the v0 sweep leaves v1 alone

def test_the_sweep_never_decides_a_v1_position_time_rails_included():
    c, m = _ledger()
    v1 = _open(c, m)
    v0 = _open(c, m, engine=PT.V0, arm="profile_matched_v1")
    late = "2026-10-08T08:00:00+00:00"                     # past the 780 s timecap; price at TP
    EX.sweep_exits(c, {("4663", MINT): {"price": 1.30, "liquidity": 1e6}}, late)
    assert _row(c, v1)["is_open"] == 1
    assert _row(c, v0)["is_open"] == 0


def test_the_14_day_backstop_still_closes_a_v1_position():
    c, m = _ledger()
    pid = _open(c, m)
    EX.sweep_exits(c, {("4663", MINT): {"price": 1.05, "liquidity": 1e6}},
                   "2026-10-23T07:00:00+00:00")
    assert _row(c, pid)["is_open"] == 0 and _row(c, pid)["exit_reason"] == "max_hold_cap"


# ------------------------------------------------------------------------ the v1 loop

def test_a_live_tick_at_take_profit_closes_on_the_rail_and_records_its_mark(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    tape = _tape(tmp_path, usd=1.30, received=T0)
    eng, clock = _engine(c, tape, T0 + 1.0)
    eng.step()
    row = _row(c, pid)
    assert row["is_open"] == 0 and row["exit_reason"] == "real_tp", row["exit_reason"]
    decision = json.loads(row["exit_decision_json"])
    assert decision["source"] == "tape_v4_spot" and decision["tick_kind"] == "v4_spot"
    assert decision["engine"] == PT.V1 and decision["age_ms"] == pytest.approx(1000.0)
    assert decision["price"] == pytest.approx(1.30, rel=1e-6)
    assert json.loads(row["paper_tick_json"])["live_s"] >= 0


def test_the_v1_loop_never_decides_a_v0_position(tmp_path):
    c, m = _ledger()
    pid = _open(c, m, engine=PT.V0)
    eng, _clock = _engine(c, _tape(tmp_path, usd=1.30), T0 + 1.0)
    eng.step()
    assert _row(c, pid)["is_open"] == 1


@pytest.mark.parametrize("why, tape_kw, now", [
    ("feed_stale", {"usd": 0.50}, T0 + 6.0),             # the writer went quiet: no tick
    ("tick_gap", {"usd": 0.50, "gap": True}, T0 + 1.0),  # an open gap: no tick
    ("v2_no_reserves", {"usd": 0.50, "kind": "v2_swap"}, T0 + 1.0),
])
def test_no_tick_holds_the_price_rails_and_counts_the_blind_seconds(tmp_path, why, tape_kw, now):
    c, m = _ledger()
    pid = _open(c, m)
    eng, clock = _engine(c, _tape(tmp_path, **tape_kw), now)
    eng.step()
    clock["t"] += 3.0
    eng.step()                                            # -50% would stop; it must hold
    assert _row(c, pid)["is_open"] == 1
    assert eng._state[pid]["why"] == why and eng._state[pid]["blind_s"] == pytest.approx(3.0)
    assert eng.counters["hold:%s" % why] == 2


def test_the_timecap_fires_without_a_tick(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    eng, _clock = _engine(c, _tape(tmp_path, usd=1.0, received=T0 - 60.0), T0,
                          iso="2026-10-08T07:14:00+00:00")
    eng.step()
    row = _row(c, pid)
    assert row["is_open"] == 0 and row["exit_reason"].startswith("b_timecap")
    assert json.loads(row["exit_decision_json"])["source"] is None


def test_a_missing_tick_never_fires_the_unquotable_rail(tmp_path):
    c, m = _ledger()
    pid = _open(c, m, policy={"max_hold_s": 0.0})         # no timecap: only unquotable could fire
    eng, _clock = _engine(c, _tape(tmp_path, received=T0 - 60.0), T0,
                          iso="2026-10-08T07:30:00+00:00")    # 30 min > unquotable_exit_s 300
    eng.step()
    assert _row(c, pid)["is_open"] == 1


def test_the_same_rail_gives_the_same_verdict_on_the_same_price(tmp_path):
    """Only the input changes: a v0 sweep and the v1 loop close a twin at the same rail."""
    c, m = _ledger()
    v0 = _open(c, m, engine=PT.V0, arm="profile_matched_v1")
    v1 = _open(c, m)
    EX.sweep_exits(c, {("4663", MINT): {"price": 0.60, "liquidity": 1e6}},
                   "2026-10-08T07:00:30+00:00")
    eng, _clock = _engine(c, _tape(tmp_path, usd=0.60), T0 + 1.0)
    eng.step()
    assert _row(c, v0)["exit_reason"] == _row(c, v1)["exit_reason"] == "b_stop"


def test_a_new_high_is_tracked_in_memory_and_written_before_the_close(tmp_path):
    c, m = _ledger()
    pid = _open(c, m, policy={"trail_pct": 10.0, "trail_arm_pct": 5.0, "tp_pct": 100.0})
    tape = _tape(tmp_path, usd=1.20)
    eng, clock = _engine(c, tape, T0 + 1.0)
    eng.step()
    assert _row(c, pid)["is_open"] == 1 and eng._state[pid]["peak"] == pytest.approx(1.20, rel=1e-6)
    _set_price(tape, 1.05, received=T0 + 2.0)               # 12.5% off the 1.20 peak: the trail
    clock["t"] = T0 + 2.5
    eng.step()
    row = _row(c, pid)
    assert row["exit_reason"] == "real_trail" and row["peak_price"] == pytest.approx(1.20, rel=1e-6)


def test_an_unbound_evm_position_resolves_the_source_swaps_pool(tmp_path):
    c, m = _ledger()
    pid = _open(c, m, pool=None)
    c.execute("UPDATE memecoin_positions SET origin_event_id=? WHERE id=?",
              ("swap:raybot:0xsrc:0x" + "11" * 20 + ":" + MINT, pid))
    c.commit()
    eng, _clock = _engine(c, _tape(tmp_path, usd=1.30), T0 + 1.0)
    eng.step()
    row = _row(c, pid)
    assert row["exit_reason"] == "real_tp"
    assert json.loads(row["paper_tick_json"])["binding"] == "source_swap"


def test_spot_usd_orientation_and_decimals():
    sq = _sqrt_for(2.0)
    assert PT.spot_usd(sq, mint_is_c1=True, coin_decimals=18, quote_decimals=18,
                       quote_usd=ETH_USD) == pytest.approx(2.0, rel=1e-9)
    # A 6-decimal coin: one human coin is 10**6 atomic, so the same atomic ratio is 10**12 less.
    assert PT.spot_usd(sq, mint_is_c1=True, coin_decimals=6, quote_decimals=18,
                       quote_usd=ETH_USD) == pytest.approx(2.0e-12, rel=1e-9)
    assert PT.spot_usd(sq, mint_is_c1=True, coin_decimals=18, quote_decimals=18,
                       quote_usd=None) is None


# ------------------------------------------------------------------------ 56: the dRPC poller

class _Http:
    def __init__(self, reply=None, status=200):
        self.calls, self.reply, self.status = 0, reply or {"jsonrpc": "2.0", "id": 1, "result": "0x"}, status

    def __call__(self, method, url, headers=None, body=None, timeout=10):
        self.calls += 1
        return self.status, self.reply, {}


@pytest.fixture(autouse=True)
def _unpaced(monkeypatch):
    """The account pacer's sleeps are its own tests' business: here every send goes straight
    through (and is counted), so a limit answer cannot park a test for the 60 s pause."""
    from sniper.services import memecoin_research_pacer as RP
    sent = []
    monkeypatch.setattr(RP, "_drpc_send", lambda url, send, limited: sent.append(url) or send())
    return sent


def _poller(http, *, paused=0.0, quote=None, clock=None, alerts=None):
    return PT.BscBagQuotes(
        "https://lb.drpc.org/ogrpc?network=bsc&dkey=k", usd_fn=lambda chain, token: 600.0,
        period_s=5.0, http=http, paused_fn=lambda url: paused, decimals_fn=lambda mint: 18,
        quote_fn=quote or (lambda mint, qty, route: {"amount_out": qty // 600}),
        alert_fn=(alerts.append if alerts is not None else None),
        clock=clock or (lambda: T0))


def test_no_open_56_position_makes_zero_drpc_calls(_unpaced):
    http = _Http()
    p = _poller(http)
    for _ in range(10):
        p.step()
    assert http.calls == 0 and not _unpaced and p.counters["reads"] == 0


def test_every_56_call_goes_through_the_account_pacer(_unpaced):
    http = _Http()
    p = _poller(http, quote=lambda mint, qty, route: p.http("POST", p.url, body="{}") and None)
    p.wanted = [(1, MINT, 100.0, {})]
    p.step()
    assert http.calls == 1 and _unpaced == [p.url]


def test_a_56_read_is_once_per_period_and_prices_the_bag(tmp_path):
    t = {"now": T0}
    p = _poller(_Http(), clock=lambda: t["now"])
    p.wanted = [(1, MINT, 100.0, {"protocol": "uniswap_v2"})]
    p.step()
    p.step()
    assert p.counters["reads"] == 1
    tick = p.tick(1)
    # 100 coins -> 10**20 // 600 wei of WBNB at $600: $100 / 100 coins = $1.00 per coin
    assert tick.tick_kind == "bag_quote" and tick.price == pytest.approx(1.0, rel=1e-6)
    t["now"] += 5.0
    p.step()
    assert p.counters["reads"] == 2


def test_a_paused_account_is_skipped_without_a_call():
    http = _Http()
    p = _poller(http, paused=30.0)
    p.wanted = [(1, MINT, 100.0, {"protocol": "uniswap_v2"})]
    p.step()
    assert p.counters["pause_skips"] == 1 and p.counters["reads"] == 0 and http.calls == 0


def test_a_quota_answer_stops_the_poller_for_the_process_and_pages_once():
    alerts = []
    http = _Http({"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "usage limit"}})
    p = _poller(http, alerts=alerts,
                quote=lambda mint, qty, route: p.http("POST", p.url, body="{}") and None)
    p.wanted = [(1, MINT, 100.0, {}), (2, MINT, 100.0, {})]
    p.step()
    p.step()
    assert p.stopped == "quota" and len(alerts) == 1 and http.calls == 1


def test_a_third_limit_inside_an_hour_stops_the_poller():
    t = {"now": T0}
    http = _Http({"jsonrpc": "2.0", "id": 1, "error": {"code": 15, "message": "free plan rate limit"}})
    p = _poller(http, clock=lambda: t["now"],
                quote=lambda mint, qty, route: p.http("POST", p.url, body="{}") and None)
    p.wanted = [(1, MINT, 100.0, {})]
    for _ in range(3):
        assert p.stopped is None
        p.step()
        t["now"] += 10.0
    assert p.stopped == "limits" and p.counters["limits"] == 3


def test_a_56_v1_position_is_decided_on_its_bag_quote(tmp_path):
    c, m = _ledger()
    route = {"protocol": "uniswap_v2", "tokens": [MINT, "0xbb"], "market_pool": "0xaa"}
    pid = _open(c, m, network="56", pool="0xaa", route=route)
    p = _poller(_Http(), quote=lambda mint, qty, route: {"amount_out": qty * 13 // 6000})
    eng, _clock = _engine(c, _tape(tmp_path), T0 + 1.0, bsc=p)
    eng.step()                                       # publishes the wanted list
    assert p.wanted == [(pid, MINT, 100.0, route)]
    p.step()                                         # $1.30 a coin
    eng.step()
    row = _row(c, pid)
    assert row["exit_reason"] == "real_tp"
    assert json.loads(row["exit_decision_json"])["tick_kind"] == "bag_quote"


def test_a_56_bag_quote_older_than_period_plus_max_age_holds(tmp_path):
    c, m = _ledger()
    route = {"protocol": "uniswap_v2", "tokens": [MINT, "0xbb"], "market_pool": "0xaa"}
    pid = _open(c, m, network="56", pool="0xaa", route=route)
    t = {"now": T0}
    p = _poller(_Http(), clock=lambda: t["now"],
                quote=lambda mint, qty, route: {"amount_out": qty * 13 // 6000})
    eng, clock = _engine(c, _tape(tmp_path), T0 + 11.0, bsc=p)
    p.wanted = [(pid, MINT, 100.0, route)]
    p.step()                                   # a $1.30 quote taken at T0, read 11 s later
    eng.step()
    assert _row(c, pid)["is_open"] == 1 and eng._state[pid]["blind_s"] == 0.0
    assert eng.counters["hold:no_quote_yet"] == 1


def test_a_v1_close_keeps_the_implausible_quote_veto(tmp_path, monkeypatch):
    monkeypatch.setattr(EX, "_exit_quote_fn", lambda *a, **k: (13.0, "route_quote"))
    c, m = _ledger()
    pid = _open(c, m)
    eng, _clock = _engine(c, _tape(tmp_path, usd=1.30), T0 + 1.0)
    eng.step()
    assert _row(c, pid)["exit_reason"] == "real_tp_noquote"


# ------------------------------------------------------------------------ the funded decision mark

def _funded():
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "source", "source")
    conn.execute("UPDATE memecoin_wallets SET real_approved=1")
    conn.commit()
    pid = m.open_position(
        conn, source_user_id="source", source_handle="source", mint="MINT", ticker="T",
        entry_price=1.0, entry_notional=100.0, entry_fill_source="chain", entry_lag_s=0.0,
        their_size_usd=1000.0, opened_at="2026-07-16T00:00:00+00:00",
        strategy="alpha_b", execution_mode="real", network_id="solana")
    return conn, pid


def _funded_stop(conn, calls, *, seen=None):
    from types import SimpleNamespace
    from sniper.services import memecoin_real_runner as runner
    context = {
        "preview_exit_fn": lambda _pos: SimpleNamespace(expected_usdc=40.0),
        "exit_fn": lambda c, pos, reason, _now: (
            seen.append(c.execute("SELECT exit_decision_json FROM memecoin_positions WHERE id=?",
                                  (pos["id"],)).fetchone()[0]) if seen is not None else None,
            calls.append(reason))[1] or {"status": "unknown"},
    }
    runner.real_exit_sweep(
        conn, {"solana": context},
        price_fn=lambda _mints: {"MINT": {"price": 0.4, "source": "dexscreener",
                                          "observed_at": "2026-07-16T00:59:50+00:00"}},
        now_fn=lambda: "2026-07-16T01:00:00+00:00")


@pytest.fixture(autouse=True)
def _empty_decision_queue():
    PT._DECISIONS.clear()
    PT.DECISION_COUNTS.clear()
    yield
    PT._DECISIONS.clear()


def _stop_knobs(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)


def test_a_funded_exit_queues_the_mark_its_rail_read_and_the_engine_writes_it(monkeypatch):
    _stop_knobs(monkeypatch)
    conn, pid = _funded()
    calls, seen = [], []
    _funded_stop(conn, calls, seen=seen)
    assert calls == ["b_stop"] and seen == [None]
    # THE FUNDED THREAD WROTE NOTHING (Gamma LOW-1, Alpha): the mark waits in the queue.
    assert conn.execute("SELECT exit_decision_json FROM memecoin_positions WHERE id=?",
                        (pid,)).fetchone()[0] is None and len(PT._DECISIONS) == 1
    assert PT.drain_exit_decisions(conn) == 1
    got = json.loads(conn.execute("SELECT exit_decision_json FROM memecoin_positions WHERE id=?",
                                  (pid,)).fetchone()[0])
    assert got["engine"] == "funded" and got["reason"] == "b_stop"
    assert (got["basis"], got["source"], got["tick_kind"]) == ("display", "dexscreener", "display")
    assert got["age_ms"] == pytest.approx(10_000.0) and got["price"] == pytest.approx(0.4)


@pytest.mark.parametrize("reason", ["b_stop", "derisk"])
def test_the_funded_mark_is_queued_only_after_the_executor_returns(monkeypatch, reason):
    """Gamma LOW-2: the close AND the derisk branch."""
    from sniper.services import memecoin_real_runner as runner
    _stop_knobs(monkeypatch)
    order = []
    monkeypatch.setattr(PT, "queue_exit_decision", lambda pid, value: order.append("queued"))
    if reason == "derisk":
        monkeypatch.setattr(runner, "alpha_b_real_exit_reason", lambda *a, **k: "derisk")
        monkeypatch.setattr(runner, "executable_exit_allowed", lambda *a, **k: (True, {}))
        monkeypatch.setattr(runner, "_run_derisk",
                            lambda *a, **k: order.append("executor") or {"status": "unknown"})
    conn, _pid = _funded()
    calls = []
    _funded_stop(conn, calls, seen=None)
    if reason == "b_stop":
        order[:0] = ["executor"] * len(calls)        # exit_fn ran (calls) before any queue entry
        assert calls == ["b_stop"]
    assert order == ["executor", "queued"], order


def test_a_full_queue_drops_and_counts_never_blocks():
    for i in range(PT._DECISIONS_MAX):
        assert PT.queue_exit_decision(i, "{}") is True
    assert PT.queue_exit_decision(999, "{}") is False
    assert PT.DECISION_COUNTS["dropped"] == 1 and len(PT._DECISIONS) == PT._DECISIONS_MAX


def test_a_failing_decision_record_never_changes_the_funded_exit(monkeypatch):
    _stop_knobs(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("queue broken")
    monkeypatch.setattr(PT, "queue_exit_decision", boom)
    conn, _pid = _funded()
    calls = []
    _funded_stop(conn, calls)
    assert calls == ["b_stop"]


def test_the_engine_thread_writes_the_queued_funded_marks(tmp_path):
    conn, pid = _funded()
    PT.queue_exit_decision(pid, '{"engine": "funded"}')
    eng, _clock = _engine(conn, _tape(tmp_path), T0)
    eng.step()
    assert conn.execute("SELECT exit_decision_json FROM memecoin_positions WHERE id=?",
                        (pid,)).fetchone()[0] == '{"engine": "funded"}'
    assert eng.heartbeat()["funded_decisions"]["written"] == 1



# ------------------------------------------------------------------------ decimals the tape lacks

def _bare_tape(tmp_path, *, token_table=None, usd=1.30):
    """A tape whose pool_meta carries NO decimals (88% of 4663 rows, 99% of 8453 on 2026-10-08)."""
    path = _tape(tmp_path, usd=usd)
    c = sqlite3.connect(path)
    c.execute("UPDATE pool_meta SET token0_decimals=NULL, token1_decimals=NULL")
    c.execute("CREATE TABLE volume_token_decimals (chain_id TEXT, token TEXT, decimals INTEGER, observed REAL)")
    for token, dec in (token_table or {}).items():
        c.execute("INSERT INTO volume_token_decimals VALUES ('4663', ?, ?, 0)", (token, dec))
    c.commit()
    c.close()
    return path


@pytest.mark.parametrize("where", ["entry_quote", "tape_table"])
def test_missing_pool_meta_decimals_come_from_the_entry_quote_or_the_tape(tmp_path, where):
    c, m = _ledger()
    pid = _open(c, m)
    if where == "entry_quote":
        c.execute("UPDATE memecoin_positions SET paper_tick_json=? WHERE id=?",
                  (json.dumps({"binding": "entry_route", "pool": POOL, "coin_decimals": 18}), pid))
        c.commit()
        tape = _bare_tape(tmp_path)
    else:
        tape = _bare_tape(tmp_path, token_table={MINT: 18})
    eng, _clock = _engine(c, tape, T0 + 1.0)
    eng.step()
    row = _row(c, pid)
    assert row["exit_reason"] == "real_tp"                  # native quote: 18 by definition
    assert json.loads(row["exit_decision_json"])["price"] == pytest.approx(1.30, rel=1e-6)


def test_unknown_decimals_hold_and_never_guess(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    eng, _clock = _engine(c, _bare_tape(tmp_path), T0 + 1.0)
    eng.step()
    assert _row(c, pid)["is_open"] == 1 and eng._state[pid]["why"] == "decimals_unknown"


def test_the_decimals_order_is_meta_then_entry_then_definition_then_table(tmp_path):
    import sqlite3 as _sq
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    tape = _sq.connect(_bare_tape(tmp_path, token_table={MINT: 6}))
    D = PT.EvmTapeTicks.decimals
    assert D(tape, "4663", MINT, 9, known=12) == 9            # the pool's own meta first
    assert D(tape, "4663", MINT, None, known=12) == 12        # then the entry quote's read
    assert D(tape, "4663", NATIVE, None) == 18
    assert D(tape, "4663", CHAIN_QUOTE["4663"]["token"], None) == CHAIN_QUOTE["4663"]["decimals"]
    assert D(tape, "4663", MINT, None) == 6                   # then the tape's token table
    assert D(tape, "4663", "0x" + "99" * 20, None) is None
    assert D(_sq.connect(":memory:"), "4663", "0x" + "99" * 20, None) is None   # no table: unknown


def test_the_real_entry_quote_path_binds_its_decimals_and_leaves_the_snapshot_alone(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_copy_trader as CT
    from sniper.services import memecoin_quotes
    from sniper.settings import memecoin as S
    monkeypatch.setattr(S, "MEMECOIN_FOLLOW3050_ALL_WALLETS", True)
    monkeypatch.setattr(memecoin_quotes, "quote_usd_price", lambda chain: ETH_USD)
    monkeypatch.setattr(memecoin_quotes, "quote_entry", lambda *a, **k: SimpleNamespace(
        unit_price="0.001", observed_at=OPENED, quote_provider="evm_route",
        route_binding={"protocol": "uniswap_v4", "pool_id": POOL, "pool_key": {"fee": 0}},
        quote_fingerprint="f", token_decimals=9))
    c, m = _ledger()
    ev = SimpleNamespace(side="buy", mint=MINT, network_id="4663", source_user_id="u1",
                         source_handle="h", ticker="T", lag_s=0.0, their_size_usd=10.0,
                         their_price=1.0, fomo_event_id="swap:raybot:0xsrc:0xw:" + MINT,
                         signal_source="onchain")
    out = CT.copy_event_to_arms(c, ev, 100.0, OPENED, arms=["funded_clone"],
                                stats_fn=lambda *a, **k: {"price": 1.0, "liquidity": 1e6})
    assert out == {"funded_clone": "opened"}, out
    assert json.loads(c.execute("SELECT paper_tick_json FROM memecoin_positions").fetchone()[0])[
        "coin_decimals"] == 9
    snap = json.loads(c.execute("SELECT features_json FROM memecoin_entry_snapshots").fetchone()[0])
    assert "_token_decimals" not in snap["paper_quote"] and "token_decimals" not in snap["paper_quote"]
