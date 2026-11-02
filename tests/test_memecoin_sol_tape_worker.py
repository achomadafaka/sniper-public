import math
import sqlite3

import scripts.memecoin_sol_tape_worker as worker
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)


def test_v6_scope_includes_every_open_main_position_for_target_mint():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE memecoin_positions(id INTEGER,mint TEXT,network_id TEXT,opened_at TEXT,"
        "is_open INTEGER,execution_mode TEXT)"
    )
    conn.executemany(
        "INSERT INTO memecoin_positions VALUES(?,?,?,?,?,?)",
        ((1, "MintA", "solana", "2026-07-26T11:00:00+00:00", 1, "real"),
         (2, "MintA", "solana", "2026-07-26T11:30:00+00:00", 1, "paper"),
         (3, "MintB", "solana", "2026-07-26T10:00:00+00:00", 1, "real"),
         (4, "MintA", "solana", "2026-07-26T09:00:00+00:00", 0, "real")),
    )
    v6 = [{"id": -1, "mint": "MintA", "network_id": "solana",
           "opened_at": "2026-07-26T12:00:00+00:00"}]

    scoped, complete = worker._v6_global_scope_targets(conn, v6)

    assert complete is True
    assert [row["id"] for row in scoped] == [-1]
    assert scoped[0]["opened_at"] == "2026-07-26T11:00:00+00:00"


def test_worker_is_default_off_and_opens_nothing(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_ENABLED", "0")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    opened = []
    monkeypatch.setattr(worker, "_connect_ledger", lambda: opened.append("ledger"))
    monkeypatch.setattr(worker, "_connect_sol_tape", lambda: opened.append("tape"))
    assert worker.main(iterations=1) == 0
    assert opened == []


def test_worker_uses_one_shared_call_for_every_adapter(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_ENABLED", "1")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(worker, "_connect_sol_tape", lambda: "TC")
    shared = object()
    monkeypatch.setattr(worker, "_build_rpc_call", lambda: shared)
    monkeypatch.setattr(worker, "get_signatures_fn", lambda *, call: ("SIGS", call))
    monkeypatch.setattr(worker, "get_transaction_fn", lambda *, call: ("TX", call))
    seen = []
    monkeypatch.setattr(worker, "solana_tape_pass", lambda m, t, gs, gt, now, **kw:
                        seen.append((m, t, gs, gt, kw["call_fn"])) or 0)
    monkeypatch.setattr(worker, "real_utc_now", lambda: 1000.0)
    assert worker.main(iterations=1) == 1
    assert seen == [("MC", "TC", ("SIGS", shared), ("TX", shared), shared)]


def test_worker_gives_v6_targets_a_dedicated_budget_before_normal_capture(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_ENABLED", "1")
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_MAX_TX_PER_TICK", "40")
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_V6_MAX_TX_PER_TICK", "12")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(worker, "_connect_sol_tape", lambda: "TC")
    shared = object()
    monkeypatch.setattr(worker, "_build_rpc_call", lambda: shared)
    monkeypatch.setattr(worker, "get_signatures_fn", lambda *, call: "SIGS")
    monkeypatch.setattr(worker, "get_transaction_fn", lambda *, call: "TX")
    targets = [{"id": "v6-1", "mint": "Mint", "network_id": "solana",
                "opened_at": NOW}]
    monkeypatch.setattr(worker, "v6_capture_targets", lambda network_id: targets)
    monkeypatch.setattr(worker, "_v6_targets_incomplete", lambda _tc, _targets: False)
    seen = []
    monkeypatch.setattr(
        worker, "solana_tape_pass",
        lambda m, t, gs, gt, now, **kw: seen.append(kw) or 0,
    )
    monkeypatch.setattr(worker, "real_utc_now", lambda: 1000.0)

    assert worker.main(iterations=1) == 1
    assert len(seen) == 2
    assert seen[0]["tx_budget"] == 12
    assert seen[0]["positions_override"] == targets
    assert "extra_positions" not in seen[0]
    assert seen[1]["tx_budget"] == 40
    assert "positions_override" not in seen[1]
    assert "extra_positions" not in seen[1]


def test_worker_defers_normal_pass_while_v6_pagination_gap_remains(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_ENABLED", "1")
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_V6_MAX_TX_PER_TICK", "12")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(worker, "_connect_sol_tape", lambda: "TC")
    monkeypatch.setattr(worker, "_build_rpc_call", lambda: object())
    monkeypatch.setattr(worker, "get_signatures_fn", lambda **_kw: "SIGS")
    monkeypatch.setattr(worker, "get_transaction_fn", lambda **_kw: "TX")
    targets = [{"id": "v6-1", "mint": "Mint", "network_id": "solana", "opened_at": NOW}]
    monkeypatch.setattr(worker, "v6_capture_targets", lambda _network: targets)
    monkeypatch.setattr(worker, "_v6_targets_incomplete", lambda _tc, _targets: True)
    seen = []
    monkeypatch.setattr(
        worker, "solana_tape_pass",
        lambda _m, _t, _gs, _gt, _now, **kwargs: seen.append(kwargs) or 0,
    )
    monkeypatch.setattr(worker, "real_utc_now", lambda: 1000.0)

    assert worker.main(iterations=1) == 1
    assert len(seen) == 1
    assert seen[0]["positions_override"] == targets


def test_worker_defers_long_normal_pass_while_v6_backfill_consumes_its_budget(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_ENABLED", "1")
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_MAX_TX_PER_TICK", "40")
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_V6_MAX_TX_PER_TICK", "12")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(worker, "_connect_sol_tape", lambda: "TC")
    monkeypatch.setattr(worker, "_build_rpc_call", lambda: object())
    monkeypatch.setattr(worker, "get_signatures_fn", lambda **_kw: "SIGS")
    monkeypatch.setattr(worker, "get_transaction_fn", lambda **_kw: "TX")
    monkeypatch.setattr(worker, "v6_capture_targets", lambda _network: [{
        "id": "v6-1", "mint": "Mint", "network_id": "solana", "opened_at": NOW,
    }])
    seen = []

    def pass_once(_m, _t, _gs, _gt, _now, **kwargs):
        seen.append(kwargs)
        return kwargs["tx_budget"]

    monkeypatch.setattr(worker, "solana_tape_pass", pass_once)
    monkeypatch.setattr(worker, "real_utc_now", lambda: 1000.0)

    assert worker.main(iterations=1) == 1
    assert len(seen) == 1
    assert seen[0]["positions_override"][0]["id"] == "v6-1"


def test_v6_enabled_time_slices_normal_capture_for_new_target_preemption(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_ENABLED", "1")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(worker, "_connect_sol_tape", lambda: "TC")
    monkeypatch.setattr(worker, "_build_rpc_call", lambda: object())
    monkeypatch.setattr(worker, "get_signatures_fn", lambda **_kw: "SIGS")
    monkeypatch.setattr(worker, "get_transaction_fn", lambda **_kw: "TX")
    monkeypatch.setattr(worker, "v6_capture_targets", lambda _network: [])
    monkeypatch.setattr(worker, "_v6_capture_enabled", lambda: True)
    monkeypatch.setattr(worker, "_v6_period_s", lambda: 5.0)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    seen = []
    monkeypatch.setattr(
        worker, "solana_tape_pass",
        lambda _m, _t, _gs, _gt, _now, **kwargs: seen.append(kwargs) or 0,
    )
    monkeypatch.setattr(worker, "real_utc_now", lambda: 1000.0)

    assert worker.main(iterations=1) == 1
    assert seen[0]["deadline_monotonic"] == 105.0


def test_positive_period_rejects_infinity(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_PERIOD_S", "inf")
    assert worker._period_s() == 300.0
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_PERIOD_S", "2.5")
    assert math.isclose(worker._period_s(), 2.5)


def test_v6_recovery_period_defaults_fast_and_is_configurable(monkeypatch):
    monkeypatch.delenv("MEMECOIN_SOL_TAPE_V6_PERIOD_S", raising=False)
    assert worker._v6_period_s() == 5.0
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_V6_PERIOD_S", "1.5")
    assert worker._v6_period_s() == 1.5


def test_v6_incomplete_backfill_period_defaults_subsecond(monkeypatch):
    monkeypatch.delenv("MEMECOIN_SOL_TAPE_V6_BACKFILL_PERIOD_S", raising=False)
    assert worker._v6_backfill_period_s() == 0.5
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_V6_BACKFILL_PERIOD_S", "0.2")
    assert worker._v6_backfill_period_s() == 0.2


def test_v6_recovery_workers_allow_measured_read_endpoint_parallelism(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_V6_FETCH_WORKERS", "32")
    assert worker._v6_fetch_workers() == 32
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_V6_FETCH_WORKERS", "100")
    assert worker._v6_fetch_workers() == 32


def test_tape_can_use_a_dedicated_read_only_rpc_endpoint(monkeypatch):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_RPC_URL", "https://public-read.example")
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return "CALL"

    monkeypatch.setattr(worker, "build_read_rpc_call", build)
    assert worker._build_rpc_call() == "CALL"
    assert seen["url"] == "https://public-read.example"


# ---- V6 capture targets (default-OFF, read-only, byte-identical when disabled/absent) ---------

NOW = "2026-07-26T12:00:00+00:00"


def _valid_v6_entry(entry_id, network, mint, token_qty=1_000_000):
    from sniper.services.memecoin_v6_engine import fingerprint, policy_document, policy_hash
    doc = policy_document()
    qp = {"in_atomic": 25_000_000, "out_atomic": token_qty}
    return {
        "entry_id": entry_id, "origin_event_id": f"evt-{entry_id}", "network_id": network,
        "mint": mint, "ticker": "PEP", "source_wallet_id": "w", "source_wallet_handle": "@w",
        "signal_source": "fomo", "notional_usd_micros": 25_000_000, "entry_price": "0.000042",
        "quote_in_asset": "USDC", "quote_in_atomic": 25_000_000, "quote_in_decimals": 6,
        "quote_out_asset": mint, "quote_out_atomic": token_qty, "quote_out_decimals": 6,
        "token_qty_atomic": token_qty, "route_target": "raydium", "pool_or_vault": "poolX",
        "quote_id": "q", "quote_provider": "jupiter", "quote_age_s": 1.0,
        "entry_mcap": None, "entry_liquidity": None,
        "signal_ts": NOW, "quote_ts": NOW, "fill_ts": NOW,
        "policy_hash": policy_hash(), "policy_document": doc,
        "quote_payload": qp, "quote_fingerprint": fingerprint(qp),
        "route_binding": {"venue": "jupiter"}, "costs": {"slippage_bps": 300},
        "net_pnl_computable": True,
    }


def test_v6_capture_targets_off_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "sniper.settings.memecoin_v6_paper.MEMECOIN_V6_PAPER_ENABLED", False, raising=False)
    assert worker.v6_capture_targets("solana") == []


def test_v6_capture_targets_missing_db_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "sniper.settings.memecoin_v6_paper.MEMECOIN_V6_PAPER_ENABLED", True, raising=False)
    monkeypatch.setattr("sniper.settings.memecoin_v6_paper.MEMECOIN_V6_PAPER_DB",
                        str(tmp_path / "absent.sqlite"), raising=False)
    assert worker.v6_capture_targets("solana") == []


def test_v6_capture_targets_reads_open_solana_positions(monkeypatch, tmp_path):
    from sniper.persistence import db_memecoin_v6_paper as v6db
    path = str(tmp_path / "v6.sqlite")
    conn = v6db.connect(path)
    v6db.record_entry_fill(conn, _valid_v6_entry("s1", "solana", "MintSol"))
    conn.close()
    monkeypatch.setattr(
        "sniper.settings.memecoin_v6_paper.MEMECOIN_V6_PAPER_ENABLED", True, raising=False)
    monkeypatch.setattr("sniper.settings.memecoin_v6_paper.MEMECOIN_V6_PAPER_DB", path, raising=False)
    targets = worker.v6_capture_targets("solana")
    assert [t["mint"] for t in targets] == ["MintSol"]
    assert targets[0]["opened_at"] == NOW


# ---------------------------------------------------------------------------------------------
# 2026-09-22, BOBCOIN 63654: bought 20:18:26 on an exotic-quote pool, stopped out 20:19:30 for
# -$70.17, a 63-second life. The pull tape wrote 20:15:02..20:15:05 then nothing until 21:00:02,
# so the whole position fell between two 300 s passes and produced ZERO rows -- while the fastlane
# held 29 continuous marks on the same coin. The cadence, not the lane, was the gap.

def _book(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memecoin_positions(id INTEGER,mint TEXT,network_id TEXT,"
                 "opened_at TEXT,is_open INTEGER,execution_mode TEXT,tracking_paused_at TEXT)")
    conn.executemany("INSERT INTO memecoin_positions(id,mint,network_id,opened_at,is_open,execution_mode) "
                     "VALUES(?,?,?,?,?,?)", rows)
    return conn


FAST_NOW = 1790000000.0                 # a fixed epoch for the window tests only
#         (this file already defines NOW for the v6 fixtures -- do not shadow it)


def _at(offset_s):
    """An ISO opened_at `offset_s` seconds BEFORE NOW, in the column's real format."""
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00", _t.gmtime(FAST_NOW - offset_s))


def test_an_open_funded_solana_position_inside_the_window_is_seen():
    conn = _book([(1, "M", "solana", _at(10), 1, "real")])
    assert worker._funded_solana_open(conn, now=FAST_NOW, window_s=300) is True


def test_paused_funded_solana_position_does_not_trigger_fast_tape():
    conn = _book([(1, "M", "solana", _at(10), 1, "real")])
    conn.execute("UPDATE memecoin_positions SET tracking_paused_at=? WHERE id=1", (_at(5),))
    assert worker._funded_solana_open(conn, now=FAST_NOW, window_s=300) is False
    conn.execute("UPDATE memecoin_positions SET tracking_paused_at=NULL WHERE id=1")
    assert worker._funded_solana_open(conn, now=FAST_NOW, window_s=300) is True


def test_a_flat_book_is_not():
    conn = _book([(1, "M", "solana", _at(10), 0, "real")])
    assert worker._funded_solana_open(conn, now=FAST_NOW, window_s=300) is False


def test_a_position_PAST_the_window_falls_back_to_the_long_period():
    """THE BOUND. A long hold is exactly the position the 300 s cadence already covers well, and
    keeping it on the fast floor is what makes the quota ceiling 15x instead of 5x."""
    conn = _book([(1, "M", "solana", _at(600), 1, "real")])
    assert worker._funded_solana_open(conn, now=FAST_NOW, window_s=300) is False


def test_the_window_edge_is_inclusive_on_the_inside():
    conn = _book([(1, "M", "solana", _at(299), 1, "real")])
    assert worker._funded_solana_open(conn, now=FAST_NOW, window_s=300) is True
    conn2 = _book([(1, "M", "solana", _at(301), 1, "real")])
    assert worker._funded_solana_open(conn2, now=FAST_NOW, window_s=300) is False


def test_one_fresh_position_among_stale_ones_still_shortens_the_period():
    conn = _book([(1, "A", "solana", _at(9000), 1, "real"),
                  (2, "B", "solana", _at(30), 1, "real")])
    assert worker._funded_solana_open(conn, now=FAST_NOW, window_s=300) is True


def test_the_fast_window_default_is_300s_and_configurable(monkeypatch):
    assert worker._open_fast_window_s() == 300.0
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_OPEN_FAST_WINDOW_S", "120")
    assert worker._open_fast_window_s() == 120.0
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_OPEN_FAST_WINDOW_S", "0")
    assert worker._open_fast_window_s() == 300.0
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_OPEN_FAST_WINDOW_S", "nan")
    assert worker._open_fast_window_s() == 300.0


def test_paper_and_other_chains_do_not_shorten_the_period():
    """The floor exists for FUNDED Solana exposure. A paper position or an EVM position must not
    spend Solana signature quota."""
    conn = _book([(1, "M", "solana", _at(10), 1, "paper"), (2, "N", "4663", _at(10), 1, "real")])
    assert worker._funded_solana_open(conn, now=FAST_NOW, window_s=300) is False


def test_a_broken_book_read_holds_the_configured_period():
    """An unreadable book is not an open position. Fail to the SLOW period, never to a fast loop
    on a database we could not read."""
    class Boom:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("locked")
    assert worker._funded_solana_open(Boom()) is False


def test_the_open_period_is_sub_30s_by_default_and_configurable(monkeypatch):
    assert worker._open_period_s() == 20.0
    assert worker._open_period_s() < 30.0, "the BOBCOIN case needs sub-30s"
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_OPEN_PERIOD_S", "12.5")
    assert math.isclose(worker._open_period_s(), 12.5)
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_OPEN_PERIOD_S", "0")
    assert worker._open_period_s() == 20.0, "a non-positive period falls back, never to zero"
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_OPEN_PERIOD_S", "inf")
    assert worker._open_period_s() == 20.0


def test_an_open_position_shortens_the_300s_period(monkeypatch):
    """THE PROPERTY THIS BLOCK EXISTS FOR."""
    slept = []
    patch_local(monkeypatch, "sleep", slept.append)
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_ENABLED", "1")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(worker, "_connect_sol_tape", lambda: "TC")
    monkeypatch.setattr(worker, "_build_rpc_call", lambda: object())
    monkeypatch.setattr(worker, "get_transaction_fn", lambda **k: (lambda *x, **y: {}))
    monkeypatch.setattr(worker, "real_utc_now", lambda: 1000.0)
    monkeypatch.setattr(worker, "_v6_capture_enabled", lambda: False)
    monkeypatch.setattr(worker, "v6_capture_targets", lambda _c: [])
    monkeypatch.setattr(worker, "solana_tape_pass", lambda *a, **k: 0)
    monkeypatch.setattr(worker, "_period_s", lambda: 300.0)
    monkeypatch.setattr(worker, "_open_period_s", lambda: 20.0)
    monkeypatch.setattr(worker, "_funded_solana_open", lambda _c: True)
    monkeypatch.setattr(worker, "get_signatures_fn", lambda *a, **k: (lambda *x, **y: []))
    worker.main(iterations=2)
    assert slept and slept[-1] <= 20.0, slept


def test_a_flat_book_keeps_the_long_period(monkeypatch):
    """Nothing is open, so there is nothing to poll and no quota to spend."""
    slept = []
    patch_local(monkeypatch, "sleep", slept.append)
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_ENABLED", "1")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(worker, "_connect_sol_tape", lambda: "TC")
    monkeypatch.setattr(worker, "_build_rpc_call", lambda: object())
    monkeypatch.setattr(worker, "get_transaction_fn", lambda **k: (lambda *x, **y: {}))
    monkeypatch.setattr(worker, "real_utc_now", lambda: 1000.0)
    monkeypatch.setattr(worker, "_v6_capture_enabled", lambda: False)
    monkeypatch.setattr(worker, "v6_capture_targets", lambda _c: [])
    monkeypatch.setattr(worker, "solana_tape_pass", lambda *a, **k: 0)
    monkeypatch.setattr(worker, "_period_s", lambda: 300.0)
    monkeypatch.setattr(worker, "_funded_solana_open", lambda _c: False)
    monkeypatch.setattr(worker, "get_signatures_fn", lambda *a, **k: (lambda *x, **y: []))
    worker.main(iterations=2)
    # The sleep is SLICED now (<=20 s each), so the period is the SUM, not the last call. A flat
    # book still waits the whole 300 s; it is merely interruptible.
    assert slept and sum(slept) > 200.0, slept
    assert max(slept) <= 20.0, "no single sleep may exceed the fast floor"


def test_the_floor_never_slows_a_faster_v6_lane(monkeypatch):
    """`min`, not assignment: a V6 backfill at 0.5 s outranks this 20 s floor."""
    slept = []
    patch_local(monkeypatch, "sleep", slept.append)
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_ENABLED", "1")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_connect_ledger", lambda: "MC")
    monkeypatch.setattr(worker, "_connect_sol_tape", lambda: "TC")
    monkeypatch.setattr(worker, "_build_rpc_call", lambda: object())
    monkeypatch.setattr(worker, "get_transaction_fn", lambda **k: (lambda *x, **y: {}))
    monkeypatch.setattr(worker, "real_utc_now", lambda: 1000.0)
    monkeypatch.setattr(worker, "_v6_capture_enabled", lambda: True)
    monkeypatch.setattr(worker, "v6_capture_targets", lambda _c: [])
    monkeypatch.setattr(worker, "solana_tape_pass", lambda *a, **k: 0)
    monkeypatch.setattr(worker, "_v6_period_s", lambda: 5.0)
    monkeypatch.setattr(worker, "_open_period_s", lambda: 20.0)
    monkeypatch.setattr(worker, "_funded_solana_open", lambda _c: True)
    monkeypatch.setattr(worker, "get_signatures_fn", lambda *a, **k: (lambda *x, **y: []))
    worker.main(iterations=2)
    assert slept and slept[-1] <= 5.0, slept


# ---------------------------------------------------------------------------------------------
# 2026-09-22, Quagsire 63795: the floor was DEPLOYED and the problem repeated. The period was
# decided once at the end of a pass, from the book at that instant, then slept in one block.
# Quagsire opened 22:27:12 and closed 22:29:13 -- a 121 s hold that began and ended inside a 300 s
# sleep chosen while the book was flat. The tape wrote nothing; `_funded_solana_open` was never
# called while it was open.

def _slices(monkeypatch, remaining, *, opens_after=None, floor=20.0):
    """Run the sliced sleep with a no-op sleep, returning (returned_early, slices)."""
    slices = []
    state = {"open": False}

    def sleep(seconds):
        slices.append(seconds)
        if opens_after is not None and sum(slices) >= opens_after:
            state["open"] = True
    monkeypatch.setattr(worker, "_funded_solana_open", lambda *_a, **_k: state["open"])
    early = worker._sleep_watching_for_a_position(remaining, object(), floor, sleep=sleep)
    return early, slices


def test_a_position_opening_MID_SLEEP_is_noticed_within_one_slice(monkeypatch):
    """THE PROPERTY THIS BLOCK EXISTS FOR. Worst-case latency to notice a new position drops from
    the whole period to one slice."""
    early, slices = _slices(monkeypatch, 300.0, opens_after=45.0)
    assert early is True, "it must return early rather than sleep out the period"
    assert sum(slices) <= 80.0, "noticed within a slice of the position opening, not at 300 s"
    assert max(slices) <= 20.0, "no single sleep may exceed the fast floor"


def test_a_flat_book_still_sleeps_the_whole_period(monkeypatch):
    """Slicing must not turn an idle worker into a busy one: same total, just interruptible."""
    early, slices = _slices(monkeypatch, 300.0)
    assert early is False
    assert abs(sum(slices) - 300.0) < 0.01
    assert max(slices) <= 20.0


def test_the_slice_never_exceeds_the_fast_floor(monkeypatch):
    """A slice longer than the window it protects would reintroduce the blind spot in miniature."""
    _early, slices = _slices(monkeypatch, 120.0, floor=900.0)
    assert max(slices) <= 20.0, "capped at 20 s even when the floor is configured longer"


def test_a_zero_or_negative_remainder_sleeps_nothing(monkeypatch):
    for remaining in (0.0, -5.0):
        early, slices = _slices(monkeypatch, remaining)
        assert early is False and slices == []
