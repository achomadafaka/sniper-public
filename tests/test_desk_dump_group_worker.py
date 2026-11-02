"""dump-group worker: replay -> warm -> publish, consumer read through the shared cache client, freeze/stale/rollback/exclusion behaviour on a simulated tape."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_dump_group_source as S
from sniper.services import memecoin_desk_dump_group_worker as W
from sniper.services.memecoin_desk_dump_group import BurstDetector
from sniper.services.memecoin_desk_dump_group_cache import SnapshotCache

T = 1_793_000_000.0                         # 2026-10-27T... fixed fake "now" base
DAY = datetime.fromtimestamp(T, timezone.utc).date().isoformat()
SOL = "1" * 32


class Sim:
    """One standard curve coin simulated with constant-product arithmetic, recording post-swap reserves like the tape does."""

    def __init__(self, mint):
        self.mint, self.q, self.b, self.real = mint, 30_000_000_000, 1_073_000_000_000_000, 0
        self.rows = []

    def buy(self, at, actor, net, slot=None):
        ta = self.b * net // (self.q + net)
        self.q, self.b, self.real = self.q + net, self.b - ta, self.real + net
        self._row(at, slot, actor, 1, net, ta)
        return ta

    def sell(self, at, actor, tokens, slot=None):
        gross = tokens * self.q // (self.b + tokens)
        self.q, self.b, self.real = self.q - gross, self.b + tokens, self.real - gross
        self._row(at, slot, actor, 0, gross, tokens)

    def _row(self, at, slot, actor, buy, qa, ta):
        n = len(self.rows)
        slot = 100 + int((at - T) * 10) if slot is None else slot            # slots grow with time, as on the real tape
        self.rows.append((slot, "%s_%d" % (self.mint, n), 0, self.mint, "pool_" + self.mint, buy, qa, ta, actor, self.q, self.b, int(at), at, self.real, 95, 30, None, SOL, self.real))


def write_tape(desk, sims, day=DAY, filler_to=60):
    (desk / "swaps").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(desk / "swaps" / ("%s.db" % day))
    con.execute("create table swaps(slot integer, signature text, ordinal integer, mint text, curve text, is_buy integer, quote_atomic integer, token_atomic integer, "
                "actor text, virtual_quote integer, virtual_token integer, block_time integer, received_at real, real_token integer, fee_bps integer, "
                "creator_fee_bps integer, tx_index integer, quote_mint text, real_quote integer, primary key(signature, ordinal))")
    con.execute("create index swaps_slot on swaps(slot)")
    rows = [r for s in sims for r in s.rows]
    f = Sim("FILL")
    for k in range(int(filler_to / 2)):
        f.buy(T + 2 * k, "fx", 1_000_000)
    rows += f.rows
    for r in rows:
        con.execute("insert into swaps values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()


def burst_and_buyers(flagged=("s1", "s2", "s3")):
    a = Sim("DUMPED")
    whale_tokens = a.buy(T + 4, "whale", 80_000_000_000)
    for i in range(20):
        a.sell(T + 10 + i * 0.01, "s%d" % i, whale_tokens // 20, slot=200 + i // 7)
    n = Sim("TARGET")
    n.buy(T + 14, "creator", 2_000_000_000)
    for i, who in enumerate(list(flagged) + ["x1", "x2", "x3", "x4", "x5"]):
        n.buy(T + 15 + i * 0.5, who, 1_000_000_000)
    return [a, n]


def make_worker(tmp, own=("OURS",), now=T + 30, detector=None, alive=True, ledger=None):
    desk = tmp / "desk"
    clock = {"t": now}
    pub = []
    from sniper.services.memecoin_desk_dump_group_cache import publish_cache

    def publish(*a, **k):
        pub.append(k)
        return publish_cache(*a, **k)
    w = W.Worker(str(desk), str(tmp / "dg" / "cache.json"), str(tmp / "dg" / "hb.json"), S.Exclusions(own, ledger=ledger, clock=lambda: clock["t"]),
                 clock=lambda: clock["t"], batch=500, detector=detector, publish=publish, alive=lambda d, n: alive)
    return w, clock, pub


def run_until_warm(w, clock, cycles=60, step=1.0):
    for _ in range(cycles):
        hb = w.cycle()
        if hb.get("published"):
            return hb
        clock["t"] += step
    return w._heartbeat(clock["t"])


def test_replay_publishes_and_the_shared_cache_client_reads_a_proven_kill(tmp_path):
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, pub = make_worker(tmp_path)
    hb = run_until_warm(w, clock)
    assert w.warm and hb["published"] >= 1 and hb["episodes"] == 1 and hb["dropped"] == {}
    cache = SnapshotCache(tmp_path / "dg" / "cache.json")
    cache.reload()
    fact = cache.lookup(mint="TARGET", pool="pool_TARGET", now=clock["t"])
    assert fact["status"] == "kill" and fact["matched_buyers"] == 3 and fact["buyers"] == 9 and fact["positive_proven"] is True
    cov = json.loads((tmp_path / "dg" / "cache.json").read_text())["coverage"]["curve"]
    assert cov["warm"] is True and cov["exclusions"]["valid"] is True and cov["n_holes"] >= 1 and cov["memory_s"] >= 24 * 3600 - 60      # the default 24 h replay; the missing previous day is a recorded hole
    assert cov["complete"] is False                                                                          # partial history never reads as complete


def test_our_own_wallet_in_the_group_removes_the_burst(tmp_path):
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, _ = make_worker(tmp_path, own=("OURS", "s7"))
    hb = run_until_warm(w, clock)
    assert hb["episodes"] == 0


def test_a_detector_exception_freezes_the_worker_and_publishes_nothing_more(tmp_path):
    write_tape(tmp_path / "desk", burst_and_buyers())

    class Boom(BurstDetector):
        def feed(self, row):
            if self.fed > 30:
                raise RuntimeError("boom")
            return super().feed(row)
    w, clock, pub = make_worker(tmp_path, detector=Boom())
    for _ in range(20):
        w.cycle()
        clock["t"] += 1
    assert w.frozen == "detector_error" and not pub and w.state.errors == 1 and "boom" in w.last_error
    assert not (tmp_path / "dg" / "cache.json").exists()


def test_a_stopped_capture_freezes_the_cutoff_at_the_last_row(tmp_path):
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, pub = make_worker(tmp_path, alive=False, now=T + 200)
    run_until_warm(w, clock, cycles=40)
    last_row = max(r[12] for r in sqlite3.connect(tmp_path / "desk" / "swaps" / ("%s.db" % DAY)).execute("select * from swaps").fetchall())
    assert w.cutoff == last_row and last_row < T + 200 - 100                          # the writer stopped: the cutoff does NOT slide to 'now - margin'
    clock["t"] += 100
    w.cycle()
    assert w.cutoff == last_row


def test_a_live_capture_lets_the_cutoff_follow_the_clock_on_a_quiet_tape(tmp_path):
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, _ = make_worker(tmp_path, alive=True, now=T + 200)
    run_until_warm(w, clock, cycles=40)
    assert w.cutoff == pytest.approx(clock["t"] - W.MARGIN_S, abs=1.01)


def test_a_clock_that_goes_backwards_publishes_nothing_and_says_so(tmp_path):
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, pub = make_worker(tmp_path)
    run_until_warm(w, clock)
    n = len(pub)
    clock["t"] -= 500
    w.cycle()
    assert len(pub) == n and w.clock_anomaly is True


def test_restart_is_a_full_replay_with_identical_results(tmp_path):
    write_tape(tmp_path / "desk", burst_and_buyers())
    a, ca, _ = make_worker(tmp_path)
    run_until_warm(a, ca)
    b, cb, _ = make_worker(tmp_path)
    run_until_warm(b, cb)
    ea, eb = a.state.episodes, b.state.episodes
    assert ea == eb and a.state.rows_fed == b.state.rows_fed and a.tail.cursor == b.tail.cursor


def test_nothing_is_published_until_the_replay_reaches_the_live_edge(tmp_path):
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, pub = make_worker(tmp_path)
    w.tail.batch = 5
    w.cycle()
    assert not w.warm and not pub                                                      # mid-replay: no publication


def test_a_row_behind_the_cursor_is_a_counted_gap_not_a_crash(tmp_path):
    st = S.GroupState(BurstDetector(), S.Exclusions(("OURS",)))
    row = lambda i, at: {"venue": "curve", "pool": "P", "mint": "M", "signature": "g%d" % i, "ordinal": 0, "slot": i, "known_at": at, "is_buy": True, "actor": "a%d" % i,  # noqa: E731
                         "quote_sol": 1.0, "price_sol": 1e-6, "price_before_sol": 1e-6, "pre_real_quote_sol": 10.0}
    st.feed(row(1, T))
    st.feed(row(2, T - 1))
    assert st.out_of_order == 1 and st.rows_fed == 1 and st.gaps and st.gaps[0][2] == "out_of_order"


# --- PumpSwap burst learned from the PS tape, curve buyers killed on it (the acceptance shape) -----------------------------
VQ_SRC = "pumpswap_grpc:buy_event_virtual_quote"


class PSSim:
    """One canonical wSOL PumpSwap pool: constant product over (real quote + virtual quote), a proven legacy buy first so the pool has a carried virtual quote."""

    def __init__(self, pool="APEPOOL", mint="APEMINT"):
        self.pool, self.mint, self.B, self.Q, self.VQ, self.n, self.rows = pool, mint, 1_000_000_000_000_000, 80_000_000_000, 30_000_000_000, 0, []

    def _row(self, at, slot, actor, buy, base, quote, pre, post, payload):
        self.n += 1
        self.rows.append((self.pool, self.mint, "So11111111111111111111111111111111111111112", slot, "%s_%d" % (self.pool, self.n), 0, 0, buy, actor, str(base), str(quote),
                          str(pre[0]), str(pre[1]), None if post is None else str(post[0]), None if post is None else str(post[1]), at, at, payload))

    def buy(self, at, actor, quote_in, slot):
        pre, k = (self.B, self.Q), (self.Q + self.VQ) * self.B
        nb = k // (self.Q + self.VQ + quote_in)
        base_out, self.B, self.Q = self.B - nb, nb, self.Q + quote_in
        payload = json.dumps({"virtual_quote_atomic": str(self.VQ), "virtual_quote_source": VQ_SRC, "ix_name": "buy", "virtual_quote_reserves": self.VQ})
        self._row(at, slot, actor, 1, base_out, quote_in, pre, (self.B, self.Q), payload)

    def sell(self, at, actor, base_in, slot):
        pre, k = (self.B, self.Q), (self.Q + self.VQ) * self.B
        nb = self.B + base_in
        quote_out = (self.Q + self.VQ) - k // nb
        self.B, self.Q = nb, self.Q - quote_out
        self._row(at, slot, actor, 0, base_in, quote_out, pre, None, None)


def write_ps(desk, sims, filler_to=60):
    (desk / "pumpswap").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(desk / "pumpswap" / "tape.db")
    con.execute("create table swaps_ps(pool text, mint text, quote_mint text, slot integer, signature text, ordinal integer, tx_index integer, is_buy integer, actor text, "
                "base_atomic text, quote_atomic text, pre_base_reserve_atomic text, pre_quote_reserve_atomic text, post_base_reserve_atomic text, "
                "post_quote_reserve_atomic text, received_at real, processed_at real, payload_json text)")
    con.execute("create index swaps_ps_received on swaps_ps(received_at)")
    con.execute("create table gaps_ps(at real, cause text, last_slot integer)")
    rows = [r for s in sims for r in s.rows]
    f = PSSim("PFILL", "PFILLMINT")
    for k in range(int(filler_to / 2)):
        f.buy(T + 2 * k, "pf", 1_000_000, 100 + k)
    rows += f.rows
    for r in rows:
        con.execute("insert into swaps_ps values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    con.commit()
    con.close()


def ps_burst(flagged=("p1", "p2", "p3")):
    a = PSSim()
    a.buy(T + 4, "psbuyer", 5_000_000_000, 90)                                          # a proven legacy buy: the pool now carries a virtual quote
    for i in range(20):
        a.sell(T + 10 + i * 0.01, "p%d" % i, a.B // 40, 300 + i // 7)
    return a


def test_a_pumpswap_burst_flags_its_sellers_and_the_curve_buyers_group_is_killed_on_it(tmp_path):
    desk = tmp_path / "desk"
    write_tape(desk, [burst_and_buyers(flagged=())[1]])                                 # curve: only the target coin, whose buyers include p1..p3 (names reused below)
    n = Sim("TARGET2")
    n.buy(T + 14, "creator", 2_000_000_000)
    for i, who in enumerate(["p1", "p2", "p3", "x1", "x2", "x3", "x4", "x5"]):
        n.buy(T + 15 + i * 0.5, who, 1_000_000_000)
    desk2 = tmp_path / "desk2"
    write_tape(desk2, [n])
    write_ps(desk2, [ps_burst()])
    w, clock, pub = make_worker_at(tmp_path, desk2)
    run_until_warm(w, clock)
    assert w.warm and len(w.state.episodes) == 1 and next(iter(w.state.episodes.values()))["venue"] == "ps"
    cache = SnapshotCache(tmp_path / "dg" / "cache.json")
    cache.reload()
    fact = cache.lookup(mint="TARGET2", pool="pool_TARGET2", now=clock["t"])
    assert fact["status"] == "kill" and fact["matched_buyers"] == 3 and fact["positive_proven"] is True


def test_without_a_prior_proven_virtual_quote_the_pumpswap_burst_is_not_counted(tmp_path):
    desk = tmp_path / "desk"
    a = PSSim()
    for i in range(20):
        a.sell(T + 10 + i * 0.01, "p%d" % i, a.B // 40, 300 + i // 7)                    # no proven buy first: every price is None
    write_tape(desk, [Sim("TARGET2")])
    write_ps(desk, [a])
    w, clock, _ = make_worker_at(tmp_path, desk)
    run_until_warm(w, clock)
    assert not w.state.episodes and w.warm


def test_a_pumpswap_continuity_break_retracts_a_burst_that_spans_it(tmp_path):
    desk = tmp_path / "desk"
    a = PSSim()
    a.buy(T + 4, "psbuyer", 5_000_000_000, 90)
    for i in range(20):
        if i == 10:
            a.B += 123_456_789                                                           # a swap we never saw changed the pool between two rows
        a.sell(T + 10 + i * 0.01, "p%d" % i, a.B // 40, 300 + i // 7)
    write_tape(desk, [Sim("TARGET2")])
    write_ps(desk, [a])
    w, clock, _ = make_worker_at(tmp_path, desk)
    run_until_warm(w, clock)
    assert not w.state.episodes and any(g[2] == "ps_lost_swap" for g in w.state.gaps)


def make_worker_at(tmp, desk, **kw):
    w, clock, pub = make_worker(tmp, **kw)
    w.desk = str(desk)
    w.tail.desk = str(desk)
    w.ps_tail = S.PSTail(str(desk), w.replay_from, batch=500, normalizer=S.PSNormalizer({}))
    w.streams = [w.streams[0], W.Stream("ps", w.ps_tail, w.replay_from)]
    return w, clock, pub


def test_the_default_restart_replays_24h_and_a_shorter_replay_publishes_the_shorter_flag_memory(tmp_path):
    write_tape(tmp_path / "desk", burst_and_buyers())
    assert make_worker(tmp_path)[0].replay_from == pytest.approx(T + 30 - 24 * 3600.0)
    w, clock, pub = make_worker(tmp_path)
    w.replay_from = clock["t"] - 6 * 3600.0                                              # --replay-hours 6
    w.tail.day = datetime.fromtimestamp(w.replay_from, timezone.utc).date().isoformat()
    w.ps_tail = None
    w.streams = [W.Stream("curve", w.tail, w.replay_from)]
    run_until_warm(w, clock)
    cov = json.loads((tmp_path / "dg" / "cache.json").read_text())["coverage"]["curve"]
    assert cov["history_start"] == w.replay_from and 5.9 * 3600 < cov["memory_s"] < 6.1 * 3600 and cov["complete"] is False       # memory < 24 h: positives block, nothing reads complete


def test_a_late_row_found_by_the_worker_recheck_makes_that_pools_target_window_incomplete(tmp_path):
    """Zeta witness, through Worker: the recheck result must reach GroupState as a late_row gap, and the published kill must not stay proven."""
    desk = tmp_path / "desk"
    write_tape(desk, burst_and_buyers())
    w, clock, pub = make_worker(tmp_path)
    hb = run_until_warm(w, clock)
    assert hb.get("published") and w.state.target_complete(T + 30, "pool_TARGET", "curve")
    con = sqlite3.connect(desk / "swaps" / ("%s.db" % DAY))
    con.execute("insert into swaps values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (w.tail.cursor[0] - 10, "late_1", 0, "TARGET", "pool_TARGET", 1, 1_000_000_000, 1, "latecomer", 1, 1, int(T + 25), T + 25, 1, 95, 30, None, SOL, 1))
    con.commit()
    con.close()
    for _ in range(40):
        clock["t"] += 1.0
        w.cycle()
        if w.tail.late_rows:
            break
    assert w.tail.late_rows == 1
    assert any(g[2] == "late_row" and g[3] == "pool_TARGET" and g[4] == "curve" for g in w.state.gaps)
    assert not w.state.target_complete(T + 30, "pool_TARGET", "curve")
    assert w.state.target_complete(T + 30, "pool_DUMPED", "curve")                               # other pools stay complete


def test_the_cli_reads_own_addresses_from_the_funded_checkout_env_not_its_release_root(tmp_path, monkeypatch):
    """Install bug 10-27: the pinned release worktree has no .env, so the worker started with 0 own addresses and exclusions invalid (no kill proven)."""
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("dg_cli", root / "scripts" / "memecoin_desk_dump_group_worker.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    (tmp_path / "sniper_tracker.db").write_text("")
    (tmp_path / ".env").write_text("MEMECOIN_OWN_ADDRESSES=OwnAddrAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n")
    monkeypatch.delenv("MEMECOIN_OWN_ADDRESSES", raising=False)
    monkeypatch.setattr(cli, "ROOT", tmp_path / "release_without_env")
    monkeypatch.setattr(cli, "LEDGER", str(tmp_path / "sniper_tracker.db"))
    assert cli._env_file("MEMECOIN_OWN_ADDRESSES").startswith("OwnAddr")


def test_a_failed_recheck_is_a_gap_not_nothing_late(tmp_path):
    """Zeta: recheck() returning None (db error) set an unused flag and the cache still published full proof."""
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, pub = make_worker(tmp_path)
    hb = run_until_warm(w, clock)
    assert hb.get("published") and w.state.target_complete(T + 30, "pool_TARGET", "curve")
    w.tail.recheck = lambda: None
    clock["t"] += 10.0
    w.cycle()
    assert w.recheck_failed and any(g[2] == "recheck_failed" and g[4] == "curve" for g in w.state.gaps)
    assert not w.state.target_complete(T + 30, "pool_TARGET", "curve")


def test_an_unreadable_producer_gap_table_is_a_gap_not_no_gaps(tmp_path, monkeypatch):
    """Zeta: gap_rows/ps_gap_rows turned a locked/broken gaps db into [] (verified empty) while fresh proven scores kept publishing."""
    write_tape(tmp_path / "desk", burst_and_buyers())
    (tmp_path / "desk" / "gaps").mkdir(parents=True, exist_ok=True)
    sqlite3.connect(tmp_path / "desk" / "gaps" / ("%s.db" % DAY)).execute("create table gaps(at real, cause text)").connection.close()
    assert W.gap_rows(str(tmp_path / "desk"), DAY) == []                                       # a readable empty table stays empty
    w, clock, pub = make_worker(tmp_path)
    hb = run_until_warm(w, clock)
    assert hb.get("published") and w.state.target_complete(T + 30, "pool_TARGET", "curve")
    real = S._ro

    def locked(path, *a, **k):
        if "gaps" in str(path):
            raise sqlite3.OperationalError("database is locked")
        return real(path, *a, **k)
    monkeypatch.setattr(S, "_ro", locked)
    assert W.gap_rows(str(tmp_path / "desk"), DAY) is None
    clock["t"] += 10.0
    w.cycle()
    clock["t"] += 10.0
    w.cycle()
    assert w.producer_read_errors >= 1 and any(g[2] == "producer_gap_read_failed" and g[4] == "curve" for g in w.state.gaps)
    assert not w.state.target_complete(w.cutoff, "pool_TARGET", "curve")


# --- curve tape retired (operator 10-27, knob MEMECOIN_DESK_CURVE_CAPTURE) --------------------------------------------------
def _hb(desk, **kw):
    Path(desk).mkdir(parents=True, exist_ok=True)
    kw.setdefault("ts", NOW_HB)
    (Path(desk) / "universe.heartbeat").write_text(json.dumps(kw))


NOW_HB = 5_000_000.0


def test_curve_capture_off_reads_the_heartbeat_or_the_env_and_unknown_is_on(tmp_path):
    env = tmp_path / ".env"
    off = lambda e=env, now=NOW_HB + 1.0: W.curve_capture_off(tmp_path, e, now)          # noqa: E731
    assert W.curve_capture_off(tmp_path, env, NOW_HB) is False                          # nothing readable: on
    _hb(tmp_path, curve_capture=True)
    assert off() is False
    _hb(tmp_path, curve_capture=False)
    assert off() is True
    _hb(tmp_path)                                                                       # key absent: on
    assert off() is False
    env.write_text("OTHER=1\nMEMECOIN_DESK_CURVE_CAPTURE=0\n")
    assert off() is True
    env.write_text("MEMECOIN_DESK_CURVE_CAPTURE=1\n")
    assert off() is False


def test_a_retired_curve_reports_retired_incomplete_and_pumpswap_keeps_publishing(tmp_path, monkeypatch):
    desk = tmp_path / "desk"
    write_tape(desk, [Sim("TARGET2")])
    write_ps(desk, [ps_burst()])
    w, clock, pub = make_worker_at(tmp_path, desk)
    run_until_warm(w, clock)
    monkeypatch.setattr(W, "curve_capture_off", lambda d, e=None, now=None: True)
    for _ in range(80):                                                                 # the tape ends; the retired curve must not hold the watermark
        clock["t"] += 1.0
        w.cycle()
    cov = json.loads((tmp_path / "dg" / "cache.json").read_text())["coverage"]
    assert cov["curve"]["retired"] is True and cov["curve"]["complete"] is False and cov["curve"]["reason"] == "curve_capture_retired"
    assert w.streams[0].retired_at is not None and w.streams[0].wm == float("inf")
    assert json.loads((tmp_path / "dg" / "cache.json").read_text())["published_at"] > clock["t"] - 5.0     # PS side still publishes on the clock
    assert w.frozen is None


def test_switching_curve_capture_back_on_records_the_off_span_as_a_hole(tmp_path, monkeypatch):
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, _ = make_worker(tmp_path)
    run_until_warm(w, clock)
    state = {"off": True}
    monkeypatch.setattr(W, "curve_capture_off", lambda d, e=None, now=None: state["off"])
    clock["t"] += 31.0
    w.cycle()
    assert w.streams[0].retired_at is not None
    state["off"] = False
    clock["t"] += 31.0
    w.cycle()
    assert w.streams[0].retired_at is None and any(g[2] == "curve_capture_retired" and g[4] == "curve" for g in w.state.gaps)


def test_cache_complete_ignores_a_retired_curve_only_after_the_flag_memory_has_aged_out(tmp_path):
    from sniper.services.memecoin_desk_dump_group_cache import RETIRED_AGE_OUT_S, publish_cache
    score = {"pool": "P", "mint": "M", "status": "pass"}
    cutoff = 2_000_000.0

    def run(curve, ps_complete=True):
        out = tmp_path / "c.json"
        publish_cache(out, {"P": dict(score)}, coverage={"curve": curve, "ps": {"complete": ps_complete}}, cutoff=cutoff, published_at=cutoff + 0.5)
        return json.loads(out.read_text())["scores"]["P"]
    young = {"complete": False, "retired": True, "retired_since": cutoff - 3600.0}
    old = {"complete": False, "retired": True, "retired_since": cutoff - RETIRED_AGE_OUT_S - 1.0}
    assert run(young)["status"] == "unknown"                                            # fail closed until 24 h after retirement
    assert run(old)["status"] == "pass"                                                 # PS history alone is the whole flag memory
    assert run(old, ps_complete=False)["status"] == "unknown"                           # PS itself must still be complete
    assert run({"complete": False, "retired": True})["status"] == "unknown"             # no timestamp: never
    assert run({"complete": False, "retired": True, "retired_since": True})["status"] == "unknown"
    assert run({"complete": False})["status"] == "unknown"                              # incomplete but NOT retired: unchanged


def test_reactivation_restores_a_finite_curve_watermark(tmp_path, monkeypatch):
    """Zeta: a retired+drained curve had wm=inf; switching back on kept max(inf, cursor) forever, so the curve could never hold the merged cutoff again."""
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, _ = make_worker(tmp_path)
    run_until_warm(w, clock)
    state = {"off": True}
    monkeypatch.setattr(W, "curve_capture_off", lambda d, e=None, now=None: state["off"])
    for _ in range(3):
        clock["t"] += 31.0
        w.cycle()
    assert w.streams[0].wm == float("inf")
    state["off"] = False
    clock["t"] += 31.0
    w.cycle()
    assert w.streams[0].retired_at is None and w.streams[0].wm != float("inf")


def test_a_retired_curve_publishes_no_curve_scores_even_for_recent_buys(tmp_path, monkeypatch):
    write_tape(tmp_path / "desk", burst_and_buyers())
    w, clock, _ = make_worker(tmp_path)
    run_until_warm(w, clock)
    before = json.loads((tmp_path / "dg" / "cache.json").read_text())["scores"]
    assert any(v.get("venue") == "curve" for v in before.values())
    monkeypatch.setattr(W, "curve_capture_off", lambda d, e=None, now=None: True)
    clock["t"] += 31.0
    w.cycle()
    clock["t"] += 1.0
    w.cycle()
    after = json.loads((tmp_path / "dg" / "cache.json").read_text())
    assert after["coverage"]["curve"]["retired"] is True and not [v for v in after["scores"].values() if v.get("venue") == "curve"]


def test_a_stale_or_future_heartbeat_false_does_not_retire_the_curve(tmp_path):
    """Zeta: a leftover curve_capture=false (ts=1) kept retirement alive after the capture was re-enabled."""
    env = tmp_path / ".env"
    env.write_text("MEMECOIN_DESK_CURVE_CAPTURE=1\n")
    _hb(tmp_path, curve_capture=False, ts=1.0)
    assert W.curve_capture_off(tmp_path, env, NOW_HB) is False                          # stale
    _hb(tmp_path, curve_capture=False, ts=NOW_HB + 600.0)
    assert W.curve_capture_off(tmp_path, env, NOW_HB) is False                          # future
    (tmp_path / "universe.heartbeat").write_text(json.dumps({"curve_capture": False}))
    assert W.curve_capture_off(tmp_path, env, NOW_HB) is False                          # no timestamp
    _hb(tmp_path, curve_capture=False, ts=NOW_HB - 5.0)
    assert W.curve_capture_off(tmp_path, env, NOW_HB) is True                           # fresh false wins over an env ON (the producer says it is not recording)
    env.write_text("MEMECOIN_DESK_CURVE_CAPTURE=0\n")
    _hb(tmp_path, curve_capture=True, ts=1.0)
    assert W.curve_capture_off(tmp_path, env, NOW_HB) is True                           # explicit env OFF stays authority


def test_producer_retired_since_is_used_only_when_fresh_sane_and_curve_off(tmp_path):
    _hb(tmp_path, curve_capture=False, curve_retired_since=NOW_HB - 7200.0)
    assert W.producer_retired_since(tmp_path, NOW_HB + 1.0) == NOW_HB - 7200.0
    _hb(tmp_path, curve_capture=True, curve_retired_since=NOW_HB - 7200.0)
    assert W.producer_retired_since(tmp_path, NOW_HB + 1.0) is None                     # capture on: the stamp is stale news
    _hb(tmp_path, curve_capture=False, curve_retired_since=NOW_HB + 99.0)
    assert W.producer_retired_since(tmp_path, NOW_HB + 1.0) is None                     # future
    _hb(tmp_path, curve_capture=False, curve_retired_since=True)
    assert W.producer_retired_since(tmp_path, NOW_HB + 1.0) is None                     # bool is not a time
    _hb(tmp_path, curve_capture=False, curve_retired_since=NOW_HB - 7200.0, ts=1.0)
    assert W.producer_retired_since(tmp_path, NOW_HB + 1.0) is None                     # stale heartbeat


def test_a_retired_curve_with_no_day_file_reads_retired_not_tape_missing(tmp_path, monkeypatch):
    desk = tmp_path / "desk"
    write_ps(desk, [ps_burst()])                                                        # no swaps/<day>.db at all: the retired producer stopped writing curve files
    w, clock, _ = make_worker_at(tmp_path, desk)
    monkeypatch.setattr(W, "curve_capture_off", lambda d, e=None, now=None: True)
    run_until_warm(w, clock)
    cov = json.loads((tmp_path / "dg" / "cache.json").read_text())["coverage"]
    assert cov["curve"].get("retired") is True and cov["curve"]["reason"] == "curve_capture_retired"
