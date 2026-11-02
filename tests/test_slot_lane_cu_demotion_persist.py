"""A demoted slot-lane CU class survives a restart (spec 2026-10-09 slot-lane CU right-sizing, section 9;
Alpha's scope: persistent demotion, the class key on default builds, the third exhaustion form).

A class-limited send that runs out of compute units demotes its class to the lane default. Until
2026-10-09 that demotion lived only in pump_curve_buy.CU_DEMOTED, so every watchdog restart or deploy
re-armed a class that had already run out, and the next send of that class could fail the same way.
Now the lane writes one ledger row per demotion on a daemon thread (never the send thread), and
start_slot_lane loads the rows into CU_DEMOTED before the lane thread starts. A row binds to the
class's own table entry (n, max, sorted token programs), so only a reviewed table change clears it.
A ledger with no table yet is a known zero; any other load failure demotes every class and pages."""
import sqlite3
import time

import pytest

from sniper.execution import pump_curve_buy as curve
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_slot_lane as lane
from sniper.settings import memecoin as cfg

CLS = ("pump_curve", "leg", "BisonFi")
EXHAUSTED = {"status": "failed", "reason": "chain_error:Computational budget exceeded", "sig": "SigCU",
             "chain_err": {"InstructionError": [3, "ComputationalBudgetExceeded"]},
             "program_error": "Computational budget exceeded"}


@pytest.fixture(autouse=True)
def _demoted_snapshot():
    """CU_DEMOTED is process-global: snapshot and restore it around every test (xdist reuses workers)."""
    saved = set(curve.CU_DEMOTED)
    curve.CU_DEMOTED.clear()
    yield
    curve.CU_DEMOTED.clear()
    curve.CU_DEMOTED.update(saved)


@pytest.fixture
def ledger(tmp_path):
    path = str(tmp_path / "ledger.db")
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.commit()
    c.close()
    return path


def _factory(path):
    def connect():
        c = sqlite3.connect(path, check_same_thread=False, timeout=5)
        c.row_factory = sqlite3.Row
        return c
    return connect


def _lane(conn, sink):
    from test_memecoin_slot_lane import _snap

    def build_fn(buy, clip, snap):
        return "MESSAGE", {"blockhash": "Hash1", "cu_limit": 207_000, "cu_class": CLS}
    return lane.SlotLane(conn=conn, executor={"slot_execute_fn": lambda *a, **k: dict(EXHAUSTED)},
                         build_fn=build_fn, snapshot_fn=lambda: _snap(time.monotonic()),
                         armed_ids_fn=lambda: {"w1"}, kill_tripped_fn=lambda: False,
                         now_iso_fn=lambda: "2026-10-09T12:00:00+00:00", lock_key="sol-test",
                         mode="live", cu_demotion_sink=sink)


def _live(monkeypatch, tmp_path):
    from sniper.execution import exec_lock
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(lane.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)


def test_the_stored_exhaustion_text_is_matched():
    """4oFrhiTQ (2026-10-07) persisted `chain_error:Computational budget exceeded`: the runtime's own
    `Program <id> failed: <msg>` wording, carried as program_error."""
    assert curve.cu_exhausted({"chain_err": None, "program_error": "Computational budget exceeded"})
    assert not curve.cu_exhausted({"chain_err": None, "program_error": "BuySlippageBelowMinTokensOut"})


def test_a_restart_never_forgets_a_demotion(monkeypatch, tmp_path, ledger):
    """THE CHECK (Alpha): demote through the lane path, wait for the writer, forget the process state,
    load as a new process would, and the class builds at the lane default."""
    import test_memecoin_slot_lane as t
    _live(monkeypatch, tmp_path)
    pages, threads = [], []
    writer = lane.cu_demotion_writer(_factory(ledger), alert_fn=lambda kind, text: pages.append((kind, text)))

    def sink(cls, row):
        threads.append(writer(cls, row))
        return threads[-1]
    conn = sqlite3.connect(ledger)
    conn.row_factory = sqlite3.Row
    try:
        assert _lane(conn, sink).handle(t._buy())["status"] == "failed"
    finally:
        conn.close()
    assert CLS in curve.CU_DEMOTED and len(threads) == 1
    threads[0].join(5)
    assert not threads[0].is_alive()
    assert [kind for kind, _ in pages] == ["slot_cu_demoted"] and "SigCU" in pages[0][1]

    curve.CU_DEMOTED.clear()                                  # a new process
    assert lane.load_cu_demotions(_factory(ledger), alert_fn=lambda *a: pages.append(a)) == {CLS}
    assert CLS in curve.CU_DEMOTED
    limit, cls, _key = curve.build_cu_limit(
        type("B", (), {"_cu_explicit": False})(), "pump_curve", "leg",
        leg={"quote": {"routePlan": [{"swapInfo": {"label": "BisonFi"}}]}},
        token_program=sorted(cfg.SLOT_LANE_CU_CLASSES[CLS][2])[0])
    assert (limit, cls) == (cfg.MEMECOIN_SLOT_LANE_CU_LIMIT, None)
    assert len(pages) == 1                                    # a clean load pages nothing


def test_the_write_runs_off_the_send_thread(ledger):
    import threading
    seen = []
    real = m.record_cu_demotion

    def spy(conn, **kw):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(conn, **kw)
    writer = lane.cu_demotion_writer(_factory(ledger), alert_fn=lambda *a: None)
    try:
        m.record_cu_demotion = spy
        writer(CLS, {"sig": "S", "signed_limit": 207_000, "reason": "r", "demoted_at": "t"}).join(5)
    finally:
        m.record_cu_demotion = real
    assert seen == [False]


def test_a_row_for_an_older_table_entry_does_not_apply(ledger):
    c = sqlite3.connect(ledger)
    m.record_cu_demotion(c, class_key="pump_curve/leg/BisonFi", entry_key="9:1:old", sig="S",
                         signed_limit=1, reason="r", demoted_at="t")
    c.commit()
    c.close()
    assert lane.load_cu_demotions(_factory(ledger), alert_fn=lambda *a: None) == set()
    assert not curve.CU_DEMOTED


def test_a_ledger_without_the_table_is_a_known_zero(tmp_path):
    path = str(tmp_path / "bare.db")
    sqlite3.connect(path).close()
    pages = []
    assert lane.load_cu_demotions(_factory(path), alert_fn=lambda *a: pages.append(a)) == set()
    assert not curve.CU_DEMOTED and pages == []


@pytest.mark.parametrize("exc", [sqlite3.DatabaseError("database disk image is malformed"),
                                 sqlite3.OperationalError("database is locked"), OSError("gone")])
def test_any_other_load_failure_demotes_every_class_and_pages_once(exc):
    pages = []

    def broken():
        raise exc
    got = lane.load_cu_demotions(broken, alert_fn=lambda kind, text: pages.append(kind))
    assert got == set(cfg.MEMECOIN_SLOT_LANE_CU_LIMIT_BY_CLASS) == curve.CU_DEMOTED
    assert pages == ["slot_cu_demotion_load_failed"]


def test_a_failed_write_pages_and_keeps_the_process_demotion(tmp_path):
    pages = []

    def broken():
        raise sqlite3.OperationalError("disk I/O error")
    curve.demote_cu_class(CLS)
    lane.cu_demotion_writer(broken, alert_fn=lambda kind, text: pages.append(kind))(
        CLS, {"sig": "S", "signed_limit": 207_000, "reason": "r", "demoted_at": "t"}).join(5)
    assert CLS in curve.CU_DEMOTED
    assert pages == ["slot_cu_demoted", "slot_cu_demotion_write_failed"]


def test_the_class_key_rides_the_slot_build_substage_and_never_reaches_the_executor():
    class _Trace:
        def __init__(self):
            self.events = []

        def mark_substage(self, name, **kw):
            self.events.append((name, kw))
    trace = _Trace()
    validity = lane.record_slot_build(trace, {"blockhash": "H", "cu_limit": 400_000, "cu_class": None,
                                              "cu_key": ("pump_amm", "native", None, "T22")}, duration_ms=1.0)
    assert validity == {"blockhash": "H"}
    assert trace.events == [("slot_build", {"duration_ms": 1.0, "cu_limit": 400_000, "cu_class": None,
                                            "cu_venue": "pump_amm", "cu_funding": "native",
                                            "cu_leg_dex": None, "cu_token_program": "T22",
                                            "cu_native_variant": None})]


def test_the_builders_report_the_key_they_looked_up(monkeypatch):
    import test_pump_curve_buy as tc
    import test_pumpswap_buy as ts
    from test_slot_lane_cu_classes import _with_route
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    buy = tc._buy(token_program="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    _msg, validity = _with_route(tc._builder(tc._Clock()), "SomeNewDex").build(buy, 5.0, tc.SNAP)
    assert validity["cu_key"] == ("pump_curve", "leg", "SomeNewDex", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    assert validity["cu_class"] is None and validity["cu_limit"] == cfg.MEMECOIN_SLOT_LANE_CU_LIMIT
    builder = _with_route(ts._builder(ts._Clock()), "SomeNewDex")
    _msg, validity = builder.build(ts._buy(), 5.0, ts.SNAP)
    assert validity["cu_key"][:3] == ("pump_amm", "leg", "SomeNewDex")


from test_memecoin_slot_lane import conn  # noqa: E402,F401  (fixture)
