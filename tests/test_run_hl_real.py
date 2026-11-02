"""Runner-loop tests for scripts/run_hl_real.py — real intel_store on a tmp DB, fake
exchange, no network. Covers the spec's hard behaviors: write-ahead intents, entries-only
kill switch, dry-run never submits, epoch gate, crash recovery, reconcile blocking."""
import importlib.util
import sys

from sniper.hyperlive import intel_store as IS
from sniper.hyperlive import real_store as RS


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_hl_real", "scripts/run_hl_real.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_hl_real"] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load_runner()


import pytest


@pytest.fixture(autouse=True)
def _reset_runner_globals():
    """The runner keeps per-process state (_cycle_n reconcile parity, _fault_block,
    _notified_faults). Without a reset, test outcomes depend on how many cycle() calls
    ran earlier in the SAME pytest process — order/parity-dependent flakes (found by
    Codex 2026-07-17: breaker test failed only in their file ordering)."""
    R._cycle_n = 0
    R._fault_block = False
    R._reconcile_error = None
    R._notified_faults.clear()
    yield


class Cfg:
    HL_REAL_ENABLED = True
    HL_REAL_DRY_RUN = False
    HL_REAL_PER_POS_USD = 15.0
    HL_REAL_MAX_CONCURRENT = 5
    HL_REAL_MAX_TOTAL_NOTIONAL = 75.0
    HL_REAL_MAX_CHASE_BPS = 100.0
    HL_REAL_DAY_LOSS_BREAKER_USD = 20.0
    HL_REAL_TARGETS = ["0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8"]


class FakeEx:
    """Adapter-shaped fake. Marks + sz decimals canned; records submissions."""

    def __init__(self):
        self.opens, self.closes, self.lev = [], [], []
        self.marks = {"ZEC": 100.0}
        self.exchange_positions = {}
        self.status_by_cloid = {}
        self.fills = {}

    def market_open(self, coin, is_buy, sz, cloid_hex):
        self.opens.append((coin, is_buy, sz, cloid_hex))
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": str(sz), "avgPx": str(self.marks[coin])}}]}}}

    def market_close(self, coin, cloid_hex):
        self.closes.append((coin, cloid_hex))
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.15", "avgPx": "102.0"}}]}}}

    def set_isolated_1x(self, coin):
        self.lev.append(coin)

    def order_status(self, cloid_hex):
        return self.status_by_cloid.get(cloid_hex)

    def positions(self):
        return self.exchange_positions

    def sz_decimals(self, coin):
        return 2

    def fetch_marks(self):
        return dict(self.marks)

    def fills_for_cloid(self, cloid_hex):
        return self.fills.get(cloid_hex)


TARGET = "0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8"


def _setup(tmp_path):
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    dep = RS.get_or_create_deployment(conn, now_ms=1_000_000)
    return conn, FakeEx(), dep


def _paper_open(conn, *, copy_id_holder=None, detect_ts_ms=2_000_000, tier="proven",
                coin="ZEC", side="long", entry_mark=100.0, target=TARGET):
    cur = conn.execute(
        "INSERT INTO hl_copy_book(target, coin, side, entry_mark, detect_ts_ms, status, "
        "copy_tier, is_holdout, is_cold_start) VALUES (?,?,?,?,?, 'open', ?, 1, 0)",
        (target, coin, side, entry_mark, detect_ts_ms, tier))
    conn.commit()
    return cur.lastrowid


def _paper_close(conn, copy_id, *, pnl_bps=200.0):
    conn.execute("UPDATE hl_copy_book SET status='closed', exit_ts_ms=3_000_000, "
                 "exit_mark=102.0, pnl_bps=? WHERE id=?", (pnl_bps, copy_id))
    conn.commit()


def test_eligible_open_submits_and_records(tmp_path):
    conn, ex, dep = _setup(tmp_path)
    cid = _paper_open(conn)
    out = R.cycle(conn, ex, Cfg, now_ms=2_500_000)
    assert out["opened"] == 1
    assert ex.lev == ["ZEC"]
    assert len(ex.opens) == 1 and ex.opens[0][0] == "ZEC" and ex.opens[0][1] is True
    it = RS.intent_for(conn, cid, "open")
    assert it["state"] == "filled"
    assert RS.leg_open_positions(conn)[0]["copy_id"] == cid


def test_paper_close_mirrors_and_finalizes_drift(tmp_path):
    conn, ex, dep = _setup(tmp_path)
    cid = _paper_open(conn)
    R.cycle(conn, ex, Cfg, now_ms=2_500_000)
    _paper_close(conn, cid, pnl_bps=200.0)
    out = R.cycle(conn, ex, Cfg, now_ms=3_500_000)
    assert out["closed"] == 1 and len(ex.closes) == 1
    row = conn.execute("SELECT * FROM hl_real_fills f JOIN hl_real_intents i "
                       "ON i.cloid=f.cloid WHERE i.leg='close'").fetchone()
    assert row["state"] == "filled"
    # open 0.15@100 → close 0.15@102 → +200bps gross real vs paper 200 → drift ≈ 0
    assert abs(row["real_net_bps"] - 200.0) < 1.0
    assert abs(row["drift_bps"]) < 1.0
    assert RS.leg_open_positions(conn) == []


def test_kill_switch_blocks_entries_not_exits(tmp_path):
    conn, ex, dep = _setup(tmp_path)
    cid = _paper_open(conn)
    R.cycle(conn, ex, Cfg, now_ms=2_500_000)          # open while enabled
    _paper_close(conn, cid)
    cid2 = _paper_open(conn, detect_ts_ms=2_600_000, coin="ZEC")

    class Off(Cfg):
        HL_REAL_ENABLED = False

    out = R.cycle(conn, ex, Off, now_ms=3_500_000)
    assert out["closed"] == 1                          # exit STILL mirrored
    assert len(ex.opens) == 1                          # no new entry
    assert RS.intent_for(conn, cid2, "open")["reason"] == "disabled"


def test_dry_run_records_but_never_submits(tmp_path):
    conn, ex, dep = _setup(tmp_path)
    cid = _paper_open(conn)

    class Dry(Cfg):
        HL_REAL_DRY_RUN = True

    out = R.cycle(conn, ex, Dry, now_ms=2_500_000)
    assert out["opened"] == 0 and ex.opens == [] and ex.lev == []
    assert RS.intent_for(conn, cid, "open")["state"] == "dry_run"


def test_pre_epoch_rows_silently_ignored(tmp_path):
    # The existing 127-row paper book must never be adopted NOR flood the intent ledger
    # with pre_epoch noise: pre-epoch rows are filtered in SQL, zero intents recorded.
    conn, ex, dep = _setup(tmp_path)
    _paper_open(conn, detect_ts_ms=500_000)            # before epoch 1_000_000
    R.cycle(conn, ex, Cfg, now_ms=2_500_000)
    R.cycle(conn, ex, Cfg, now_ms=2_600_000)
    assert ex.opens == []
    assert conn.execute("SELECT COUNT(*) FROM hl_real_intents").fetchone()[0] == 0


def test_crash_recovery_settles_submitted_without_resend(tmp_path):
    conn, ex, dep = _setup(tmp_path)
    cid = _paper_open(conn)
    from sniper.hyperlive.real_exec import derive_cloid
    cloid = derive_cloid(dep["deployment_id"], cid, "open")
    RS.record_intent(conn, cloid=cloid, deployment_id=dep["deployment_id"], copy_id=cid,
                     leg="open", coin="ZEC", side="long", requested_usd=15.0,
                     requested_sz=0.15, state="submitted", reason=None, now_ms=2_400_000)
    ex.status_by_cloid[cloid] = {"order": {"oid": 9}, "status": "filled"}
    ex.fills[cloid] = {"px": 100.5, "sz": 0.15, "fee": 0.007}
    out = R.cycle(conn, ex, Cfg, now_ms=2_500_000)
    assert ex.opens == []                              # settled by query, never re-sent
    it = RS.intent_for(conn, cid, "open")
    assert it["state"] == "filled"
    f = conn.execute("SELECT * FROM hl_real_fills WHERE cloid=?", (cloid,)).fetchone()
    assert f["fill_px"] == 100.5 and f["fee_usd"] == 0.007


def test_reconcile_fault_blocks_opens_and_counts(tmp_path):
    conn, ex, dep = _setup(tmp_path)
    cid = _paper_open(conn)
    R.cycle(conn, ex, Cfg, now_ms=2_500_000)
    ex.exchange_positions = {}                         # position vanished on exchange
    cid2 = _paper_open(conn, detect_ts_ms=2_600_000, coin="ZEC")
    out = R.cycle(conn, ex, Cfg, now_ms=3_000_000, force_reconcile=True)
    assert out["faults"] == 1
    assert len(ex.opens) == 1                          # second open blocked by fault


def test_reconcile_transport_failure_blocks_opens_and_is_reported(tmp_path):
    conn, ex, _dep = _setup(tmp_path)
    cid = _paper_open(conn)

    def unavailable():
        raise TimeoutError("provider timed out")

    ex.positions = unavailable
    out = R.cycle(conn, ex, Cfg, now_ms=2_500_000)

    assert ex.opens == []
    assert RS.intent_for(conn, cid, "open")["reason"] == "reconcile_fault"
    assert out["reconcile_error"] == "TimeoutError"
    assert R._fault_block is True


def test_breaker_latches_on_cumulative_day_loss(tmp_path):
    # One $15 position can lose at most ~$15 — the −$20 breaker is CUMULATIVE across
    # the UTC day, so two wiped positions must latch it.
    conn, ex, dep = _setup(tmp_path)
    ex.marks["SOL"] = 50.0
    c1 = _paper_open(conn, coin="ZEC")
    c2 = _paper_open(conn, coin="SOL", entry_mark=50.0)
    R.cycle(conn, ex, Cfg, now_ms=2_500_000)
    _paper_close(conn, c1)
    _paper_close(conn, c2)
    ex.market_close = lambda coin, cloid_hex: {"status": "ok", "response": {"data": {
        "statuses": [{"filled": {"totalSz": "9", "avgPx": "1.0"}}]}}}   # near-total loss
    R.cycle(conn, ex, Cfg, now_ms=3_500_000)
    # ZEC: (1−100)×0.15 = −14.85; SOL: (1−50)×0.3 = −14.7 → −29.55 ≤ −20 → latch
    assert RS.breaker_latched(conn) is not None
    c3 = _paper_open(conn, detect_ts_ms=3_600_000)
    R.cycle(conn, ex, Cfg, now_ms=3_700_000)
    assert RS.intent_for(conn, c3, "open")["reason"] == "breaker_latched"
