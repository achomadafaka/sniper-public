"""Exit price tape, part B on EVM: whole-bag quotes of the exited quantity after every funded exit.

Spec docs/superpowers/specs/2026-09-30-memecoin-exit-price-tape-design.md §4a. The route-and-sell
test runs the REAL ExactLane on the REAL recorded frames of the refusal-capture lane
(tests/fixtures/memecoin/evm_exact_capture_frames.json): a calldata drift answers an unrecorded
call and fails here. The scheduling tests use a small fake lane, because they are about time.
"""
import ast
import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_exit_followup as fu
from sniper.services import memecoin_exit_tape as tape_mod
from sniper.settings import memecoin as cfg

ROOT = Path(__file__).resolve().parents[1]
FIX = json.load(open(ROOT / "tests" / "fixtures" / "memecoin" / "evm_exact_capture_frames.json"))
ANCHOR = "2026-10-01T10:00:00+00:00"
ANCHOR_EPOCH = fu._epoch(ANCHOR)


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _tape(tmp_path):
    return fu.connect(tmp_path / "tape.db")


def _position(c, network_id="4663", mint="0x" + "ab" * 20, mode="real", notional=20.0):
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint=mint, ticker="T", entry_price=1.0,
        entry_notional=notional, entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
        opened_at="2026-10-01T09:00:00+00:00", strategy=cfg.MEMECOIN_REAL_POLICY,
        execution_mode=mode, network_id=network_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=20.0, notional=notional,
                  fill_source="chain", fraction=1.0, created_at="2026-10-01T09:00:00+00:00",
                  tx_sig="E%d" % pid)
    return pid


def _exit(c, pid, kind="bracket_exit", tx="0xexit", fraction=1.0, qty=20.0):
    m.insert_fill(c, position_id=pid, kind=kind, price=1.5, qty=qty, notional=30.0,
                  fill_source="chain", fraction=fraction, created_at=ANCHOR, tx_sig=tx)
    if fraction >= 1.0:
        c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=?, closed_recorded_at=?,"
                  " exit_reason='real_tp' WHERE id=?", (ANCHOR, ANCHOR, pid))
        c.commit()


def test_the_schedule_is_every_2s_to_120_then_every_10s_to_900():
    assert len(fu.SLOTS) == 138
    assert fu.SLOTS[:3] == (2, 4, 6) and fu.SLOTS[59] == 120 and fu.SLOTS[60] == 130
    assert fu.SLOTS[-1] == 900
    assert fu.window_end(0) == 4 and fu.window_end(59) == 130 and fu.window_end(137) == 910


def test_only_funded_full_exits_are_read_each_with_its_attempt():
    c = _ledger()
    real = _position(c)
    _exit(c, real, tx="0xreal")
    m.record_exit_execution(c, position_id=real, network_id="4663", exit_reason="real_tp",
                            attempted_at="2026-10-01T09:59:59+00:00", status="closed", sig="0xreal")
    paper = _position(c, mode="paper", mint="0x" + "01" * 20)
    _exit(c, paper, tx="0xpaper")
    partial = _position(c, mint="0x" + "02" * 20)
    _exit(c, partial, kind="derisk", tx="0xderisk", fraction=0.5, qty=10.0)
    written_off = _position(c, mint="0x" + "03" * 20)
    _exit(c, written_off, kind="manual_close", tx=None)

    rows = fu.new_exits(c, 0)

    assert [(r["position_id"], r["tx_sig"]) for r in rows] == [(real, "0xreal")]
    r = rows[0]
    assert (r["attempt_status"], r["attempted_at"], r["attempt_reason"]) == (
        "closed", "2026-10-01T09:59:59+00:00", "real_tp")
    assert fu.new_exits(c, r["fill_id"]) == []


def test_the_first_start_takes_the_newest_fill_and_never_backfills(tmp_path):
    c = _ledger()
    pid = _position(c)
    _exit(c, pid)
    tape = _tape(tmp_path)
    cursor = fu.init_cursor(tape, c)
    assert fu.new_exits(c, cursor) == []
    assert fu.init_cursor(tape, c) == cursor          # persisted, not re-taken


def test_each_exit_is_copied_and_opened_by_chain(tmp_path):
    c = _ledger()
    tape = _tape(tmp_path)
    evm = _position(c, network_id="4663")
    sol = _position(c, network_id="solana", mint="SoLMint")
    arc = _position(c, network_id="5042")
    for pid, tx in ((evm, "0xa"), (sol, "solsig"), (arc, "0xc")):
        _exit(c, pid, tx=tx)
    m.record_exit_execution(c, position_id=evm, network_id="4663", exit_reason="real_tp",
                            attempted_at="2026-10-01T09:59:59+00:00", status="closed", sig="0xa")
    pool = "0x" + "9f" * 32
    m.record_route_series(c, [{"position_id": evm, "mint": "m", "network_id": "4663",
                               "seam": "exit_decision", "observed_at": "2026-10-01T09:59:59.5+00:00",
                               "expected_usdc": 30.0, "quote_evidence_json": json.dumps(
                                   {"route": {"pool_id": pool.upper().replace("0X", "0x"),
                                              "protocol": "uniswap_v4"}})}])
    # The A-lite confirmation for the same attempt holds the exact balance.
    t = tape_mod.Tape(str(tmp_path / "tape.db"))
    t.append({"position_id": evm, "reason": "real_tp", "decision_at": "2026-10-01T09:59:59+00:00",
              "status": "ok", "amount_atomic": "123456789012345678901234", "host_at": ANCHOR,
              "host_mono": 1.0})
    t.drain_once()

    out = {}
    for row in fu.new_exits(c, 0):
        out[row["network_id"]] = fu.record_exit(tape, c, row, lanes={"4663": object()},
                                                refused=fu.BLOCKED, now_iso=ANCHOR)

    assert out["solana"] is None and out["5042"] is None
    f = out["4663"]
    assert f["status"] == "pending" and f["exit_pool"] == pool
    assert (f["qty_atomic"], f["qty_basis"]) == ("123456789012345678901234", "confirmation")
    status = dict(tape.execute("SELECT network_id, status FROM exit_followups").fetchall())
    assert status == {"4663": "pending", "solana": "no_lane", "5042": fu.BLOCKED["5042"]}
    copied = tape.execute("SELECT COUNT(*) FROM exit_fills").fetchone()[0]
    assert copied == 3


# --- the real lane on recorded frames --------------------------------------------------------------

class Replay:
    def __init__(self, chain):
        fx = FIX["chains"][chain]
        self.fx = fx
        self.frames = {}
        for f in fx["frames"]:
            p = f["params"]
            key = (f["method"], p[0]["to"].lower(), p[0]["data"].lower()) if f["method"] == "eth_call" \
                else (f["method"],)
            self.frames[key] = (f["status"], f["reply"])
        self.posts, self.unrecorded = [], []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        if method == "GET":
            return 200, {"price": str(self.fx["native_usd"])}, {}
        req = json.loads(body)
        p = req["params"]
        key = (req["method"], p[0]["to"].lower(), p[0]["data"].lower()) \
            if req["method"] == "eth_call" else (req["method"],)
        self.posts.append((url, key))
        if key not in self.frames:
            self.unrecorded.append(key)
            raise AssertionError("unrecorded call %s" % (key[:2],))
        status, reply = self.frames[key]
        return status, reply, {}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, float(s))


@pytest.mark.parametrize("chain", sorted(FIX["chains"]))
def test_the_real_lane_finds_the_route_and_sells_the_exited_bag(tmp_path, chain, monkeypatch):
    # These unchanged September 28 RPC frames predate the fee-100 connector default.
    monkeypatch.setitem(cfg.MEMECOIN_EVM_V3_CONNECTORS, "56",
                        (("0x55d398326f99059ff775485246999027b3197955", 10000),))
    fx = FIX["chains"][chain]
    http, clock = Replay(chain), Clock()
    lane = fu.build_lane(chain, "https://" + fx["host"], 4.0, http=http, clock=clock,
                         sleep=clock.sleep, keys_fn=lambda c, mint: [tuple(k) for k in fx["v4_keys"]])
    tape = _tape(tmp_path)
    best_pool = fx["best"]["route"].split(":", 1)[1] if fx["best"]["kind"] == "v4" else None
    f = {"fill_id": 1, "position_id": 7, "network_id": chain, "mint": fx["mint"].lower(),
         "status": "pending", "anchor_at": ANCHOR, "entry_notional": 20.0, "qty": 1.0,
         "qty_atomic": fx["best"]["out"], "qty_basis": "confirmation",
         "exit_pool": best_pool, "entry_pool": None}
    fu.save_followup(tape, f, ANCHOR)
    w = fu.ChainWorker(chain, lane, str(tmp_path / "tape.db"),
                       wall=lambda: ("2026-10-01T10:00:02.500000+00:00", ANCHOR_EPOCH + 2.5))
    w.submit(f)

    w.run_once(tape)

    assert http.unrecorded == []
    assert {url for url, _k in http.posts} == {"https://" + fx["host"]}
    if best_pool:                       # a known exit pool asks the V4 quoter and nothing else
        from sniper.execution.evm_v4_quote import V4
        quoters = {k[1] for _u, k in http.posts if k[0] == "eth_call"}
        assert quoters == {V4[chain]["quoter"].lower()}, quoters
    fol = dict(tape.execute("SELECT * FROM exit_followups").fetchone())
    assert fol["status"] == "active"
    assert fol["route_basis"] == ("exit_pool" if best_pool else "scan_best")
    assert fol["route_label"] == fx["best"]["route"]
    q = dict(tape.execute("SELECT * FROM post_exit_quotes").fetchone())
    assert (q["slot_s"], q["status"], q["age_s"]) == (2, "ok", pytest.approx(2.5))
    out = int(q["quote_output_atomic"])
    assert q["value_usd"] == pytest.approx(out / 1e18 * fx["native_usd"])
    # The bag the $20 clip bought, sold straight back on the same pool: less, never nothing.
    assert 10.0 < q["value_usd"] < 20.0, q["value_usd"]


# --- scheduling on a fake lane -------------------------------------------------------------------

class Host:
    def __init__(self):
        self.counts, self.yield_s, self.url = Counter(), fu.HOLD_MIN_S, "https://research.example"


class FakeLane:
    clip_usd = 20.0

    def __init__(self, chain="4663", answers=None):
        self.chain = chain
        self.hosts = {chain: Host()}
        self.answers = list(answers or [])
        self.calls = 0
        self.rpc = {}

    def retry_pending(self):
        pass

    def _ready(self, chain):
        return True

    def stats(self):
        return {}

    def native_usd(self, chain):
        return 1000.0

    def _quote(self, chain, route, amount, side, state):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else 10 ** 18
        if answer == "limit":
            self.hosts[chain].counts["held"] += 1
            state["failed"] = "status=429 code=None"
            return None
        return answer

    def _rpc_http(self, chain, state):
        def http(method, url, headers=None, body=None, timeout=None):
            req = json.loads(body)
            return 200, {"result": self.rpc.get(req["method"])}, {}
        return http


def _active(fill_id=1, next_index=0):
    return {"fill_id": fill_id, "position_id": 7, "network_id": "4663", "mint": "0xm",
            "status": "active", "anchor_at": ANCHOR, "entry_notional": 20.0, "qty": 1.0,
            "qty_atomic": str(10 ** 18), "qty_basis": "confirmation",
            "route": {"kind": "v2", "router": "0xr", "path": ["0xa", "0xb"]},
            "route_label": "v2:0xr:0xa>0xb", "route_basis": "scan_best", "next_index": next_index}


def _worker(tmp_path, lane, age):
    return fu.ChainWorker("4663", lane, str(tmp_path / "tape.db"),
                          wall=lambda: ("t+%s" % age, ANCHOR_EPOCH + age))


def _quotes(tape):
    return [tuple(r) for r in tape.execute(
        "SELECT slot_s, status, quote_output_atomic FROM post_exit_quotes ORDER BY slot_s")]


def test_a_slot_past_its_window_is_skipped_never_interpolated(tmp_path):
    tape, lane = _tape(tmp_path), FakeLane(answers=[777])
    w = _worker(tmp_path, lane, 7.0)
    w.submit(_active())
    w.run_once(tape)
    assert _quotes(tape) == [(2, "skipped", None), (4, "skipped", None), (6, "ok", "777")]
    assert lane.calls == 1


def test_a_limit_doubles_the_hold_and_a_good_quote_resets_it(tmp_path):
    tape, lane = _tape(tmp_path), FakeLane(answers=["limit", "limit", 5])
    host = lane.hosts["4663"]
    f = _active()
    ages = iter([2.1, 4.1, 6.1])
    w = fu.ChainWorker("4663", lane, str(tmp_path / "tape.db"),
                       wall=lambda: (ANCHOR, ANCHOR_EPOCH + current[0]))
    current = [0.0]
    w.submit(f)
    seen = []
    for age in ages:
        current[0] = age
        w.run_once(tape)
        seen.append(host.yield_s)
    assert seen == [4.0, 8.0, fu.HOLD_MIN_S]
    assert [s for _slot, s, _o in _quotes(tape)] == ["held", "held", "ok"]
    stats = w.stats()                       # the heartbeat's per-chain counters
    assert (stats["limits"], stats["held_rows"], stats["hold_s"]) == (2, 2, fu.HOLD_MIN_S)


def test_a_limit_during_the_route_scan_also_doubles_the_hold(tmp_path):
    tape, lane = _tape(tmp_path), FakeLane()

    def scan(chain, mint, amount_in, state, kinds=None):
        lane.hosts[chain].counts["held"] += 1
        state["failed"] = "status=429 code=None"
        return []
    lane._scan = scan
    w = _worker(tmp_path, lane, 1.0)
    w.submit(dict(_active(), route=None, status="pending"))
    w.run_once(tape)
    assert lane.hosts["4663"].yield_s == 4.0 and w.stats()["limits"] == 1
    assert 1 in w.active                    # still pending: retried next pass, never final


def test_the_hold_never_exceeds_300s():
    host = Host()
    host.yield_s = 256.0
    lane = FakeLane(answers=["limit"])
    lane.hosts["4663"] = host
    w = fu.ChainWorker("4663", lane, ":memory:", wall=lambda: (ANCHOR, ANCHOR_EPOCH + 2.1))
    conn = fu.connect(":memory:")
    w.submit(_active())
    w.run_once(conn)
    assert host.yield_s == fu.HOLD_MAX_S


def test_the_horizon_ends_with_the_chain_time_and_done(tmp_path):
    tape, lane = _tape(tmp_path), FakeLane()
    tape.execute("INSERT INTO exit_fills(fill_id, position_id, tx_sig, copied_at) VALUES(1, 7, '0xt', ?)",
                 (ANCHOR,))
    tape.commit()
    lane.rpc = {"eth_getTransactionReceipt": {"blockNumber": "0x10"},
                "eth_getBlockByNumber": {"timestamp": hex(int(ANCHOR_EPOCH) - 1)}}
    w = _worker(tmp_path, lane, 911.0)
    w.submit(_active(next_index=137))
    w.run_once(tape)
    assert _quotes(tape) == [(900, "skipped", None)]
    fill = dict(tape.execute("SELECT chain_block, chain_time, chain_time_status FROM exit_fills").fetchone())
    assert fill == {"chain_block": 16, "chain_time": "2026-10-01T09:59:59+00:00", "chain_time_status": "ok"}
    assert tape.execute("SELECT status FROM exit_followups").fetchone()[0] == "done"
    assert w.active == {}


def test_a_restart_resumes_after_the_last_written_slot(tmp_path):
    tape = _tape(tmp_path)
    f = _active()
    fu.save_followup(tape, f, ANCHOR)
    tape.execute("INSERT INTO exit_fills(fill_id, position_id, qty, copied_at) VALUES(1, 7, 1.0, ?)",
                 (ANCHOR,))
    for slot in (2, 4):
        tape.execute("INSERT INTO post_exit_quotes(fill_id, slot_s, status) VALUES(1, ?, 'ok')", (slot,))
    tape.commit()
    (got,) = fu.resumable(tape, {"4663"})
    assert got["next_index"] == 2 and got["route"] == f["route"]
    assert fu.resumable(tape, {"56"}) == []


def test_the_hosts_refuse_a_funded_domain_and_a_wrong_chain():
    def http(method, url, headers=None, body=None, timeout=None):
        if "tenderly" in url:
            return 200, {"result": hex(1)}, {}          # answers chain 1, not 8453
        return 200, {"result": hex(4663 if "quiknode" in url else 56)}, {}
    env = {"MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663": "https://x.robinhood-mainnet.quiknode.pro/k/",
           "MEMECOIN_RESEARCH_DRPC_RPC_URL_56": "https://lb.drpc.org/ogrpc?network=bsc&dkey=K",
           "MEMECOIN_SOME_FUNDED_RPC": "https://lb.drpc.org/ogrpc?network=base&dkey=K"}
    usable, pending, refused = fu.resolve_hosts(env, http=http)
    assert set(usable) == {"4663"} and pending == {}
    assert "used by another role" in refused["56"]
    assert "answered chain 1" in refused["8453"]
    assert refused["5042"] == fu.BLOCKED["5042"]


def test_no_decision_path_imports_the_follower():
    """It reads exit_confirmations; only its own script may import it."""
    allowed = {"scripts/memecoin_exit_tape_follow.py"}
    hits = []
    for base in ("sniper", "scripts"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            if rel in allowed or rel == "sniper/services/memecoin_exit_followup.py":
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                         [node.module or ""] + [a.name for a in node.names]
                         if isinstance(node, ast.ImportFrom) else [])
                if any("memecoin_exit_followup" in n for n in names):
                    hits.append(rel)
    assert hits == [], hits
    for rel in allowed:
        assert (ROOT / rel).exists(), "stale allowed importer %s" % rel


def test_a_horizon_already_gone_buys_no_route_scan(tmp_path):
    tape, lane = _tape(tmp_path), FakeLane()
    tape.execute("INSERT INTO exit_fills(fill_id, position_id, tx_sig, copied_at) VALUES(1, 7, '0xt', ?)",
                 (ANCHOR,))
    tape.commit()
    f = dict(_active(), route=None, status="pending")
    lane._scan = lambda *a, **k: pytest.fail("scanned a route for a horizon that is gone")
    w = _worker(tmp_path, lane, 3600.0)
    w.submit(f)
    w.run_once(tape)
    statuses = Counter(s for _slot, s, _o in _quotes(tape))
    assert statuses == {"skipped": 138} and lane.calls == 0
    assert tape.execute("SELECT status FROM exit_followups").fetchone()[0] == "missed"


KEYED_56 = "https://lb.drpc.org/ogrpc?network=bsc&dkey=SECRET"


def _answers_56(method, url, headers=None, body=None, timeout=None):
    return 200, {"result": hex(56)}, {}


def test_the_follower_reads_56_on_the_keyed_drpc_account_beside_the_hop_seed():
    """56 reads the operator's keyed dRPC account or nothing. The BSC tape's hop seed reads the same
    account since 2026-10-02 (both through its one pacer), and that never refuses the follower."""
    from sniper.services.memecoin_bsc_v2_live import HOP_SEED_URL_VAR
    usable, pending, refused = fu.resolve_hosts({}, http=_answers_56)
    assert "56" not in usable and "56" not in pending and refused["56"] == "no research host set"
    usable, _pending, _refused = fu.resolve_hosts({HOP_SEED_URL_VAR: KEYED_56}, http=_answers_56)
    assert usable["56"] == (KEYED_56, 1.5)


def test_every_read_on_the_keyed_drpc_account_takes_its_shared_pacer(monkeypatch):
    """The identity check in resolve_hosts and every read of the lane (here its pending identity
    retry) go through drpc_paced_http; a read on another host does not."""
    import sniper.services.memecoin_research_pacer as rp
    took, asked = [], []
    monkeypatch.setattr(rp.SharedPacer, "acquire", lambda self: took.append(self.path))

    def http(method, url, headers=None, body=None, timeout=None):
        asked.append(url)
        return _answers_56(method, url)
    fu.resolve_hosts({"MEMECOIN_RESEARCH_DRPC_RPC_URL_56": KEYED_56}, http=http)
    assert asked.count(KEYED_56) == 1 and len(asked) > 1 and len(took) == 1
    clock = Clock()
    lane = fu.build_lane("56", KEYED_56, 1.5, pending=True, http=http, clock=clock, sleep=clock.sleep)
    clock.t += 60.0
    lane.retry_pending()
    assert asked.count(KEYED_56) == 2 and len(took) == 2 and lane._ready("56")
    other = fu.build_lane("8453", "https://base.gateway.tenderly.co", 2.0, pending=True,
                          http=lambda *a, **k: (200, {"result": hex(8453)}, {}), clock=clock,
                          sleep=clock.sleep)
    clock.t += 60.0
    other.retry_pending()
    assert len(took) == 2 and other._ready("8453")


def test_quote_rows_say_which_budget_read_them_and_at_what_pace(tmp_path):
    """Alpha: the 56 tape is coarser on the shared dRPC account (1/1.5 s whatever the lane's own
    rate), so every row records the read budget and the minimum spacing. An old tape file gains
    both columns on connect."""
    path = tmp_path / "tape.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE post_exit_quotes (id INTEGER PRIMARY KEY AUTOINCREMENT, fill_id "
                "INTEGER NOT NULL, position_id INTEGER, network_id TEXT, slot_s INTEGER NOT NULL, "
                "status TEXT NOT NULL, observed_at TEXT, age_s REAL, quote_output_atomic TEXT, "
                "quote_asset_usd REAL, value_usd REAL, http_status TEXT, latency_ms REAL, "
                "UNIQUE(fill_id, slot_s))")
    old.commit()
    old.close()
    tape = fu.connect(path)
    fu.connect(path).close()                     # a second connect finds the columns there
    keyed = FakeLane("56")
    keyed.hosts["56"].url, keyed.hosts["56"].rate = KEYED_56, 4.0
    fu.ChainWorker("56", keyed, str(path))._write_quote(tape, {"fill_id": 1, "position_id": 7}, 2, "ok")
    plain = FakeLane("8453")
    plain.hosts["8453"].url, plain.hosts["8453"].rate = "https://base.gateway.tenderly.co", 2.0
    fu.ChainWorker("8453", plain, str(path))._write_quote(tape, {"fill_id": 2, "position_id": 8}, 2, "ok")
    sol = fu.SolanaWorker(fu.SolanaReader("https://mainnet.helius-rpc.com/?api-key=SECRET", 4.0),
                          str(path))
    sol._write_quote(tape, {"fill_id": 3, "position_id": 9}, 2, "ok")
    rows = tape.execute("SELECT network_id, read_budget, read_pace_s FROM post_exit_quotes "
                        "ORDER BY fill_id").fetchall()
    assert [tuple(r) for r in rows] == [("56", "drpc.org (keyed)", pytest.approx(1 / 1.5)),
                                        ("8453", "tenderly.co", pytest.approx(0.5)),
                                        ("solana", "helius-rpc.com", pytest.approx(0.25))]
    assert "SECRET" not in str([tuple(r) for r in rows])
