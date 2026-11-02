"""TX SHAPE tape: routing record of hash-sampled swap transactions (terminal attention), evidence rule, never on the fill path."""
import json
import sqlite3
import struct

import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_txshape as T
from sniper.services import memecoin_desk_universe as U
from test_memecoin_desk_universe_worker import W, frame_meta, trade_event, vq_for, worker

PAYER, ROUTER, FEE, TIP, OTHER = "Payer1111111111111111111111111111111111111", "RouterProgram1111111111111111111111111111111", \
    "FeeAccount11111111111111111111111111111111", sorted(T.JITO_TIP_ACCOUNTS)[0], "Other11111111111111111111111111111111111"


def transfer(lamports):
    return struct.pack("<IQ", 2, lamports)


def budget_price(v):
    return bytes([3]) + struct.pack("<Q", v)


def budget_limit(v):
    return bytes([2]) + struct.pack("<I", v)


KEYS = [PAYER, T.SYSTEM, T.COMPUTE_BUDGET, ROUTER, FEE, TIP, U.PUMP_PROGRAM, OTHER]


def sample_shape(**kw):
    top = [(2, b"", budget_limit(200_000)), (2, b"", budget_price(5_000)),
           (1, bytes([0, 4]), transfer(1_000_000)),                  # a fee to FEE
           (1, bytes([0, 5]), transfer(2_000_000)),                  # a tip to a Jito tip account
           (3, bytes([0]), b"route"), (3, bytes([0]), b"route"), (6, bytes([0]), b"")]
    inner = [(4, [(7, bytes([0]), b"x"), (1, bytes([0, 4]), transfer(3_000))])]
    return T.shape(KEYS, top, inner, venue="curve", **kw)


def test_shape_reads_router_inner_programs_transfers_and_compute_budget_and_skips_core():
    row = sample_shape()
    assert row["payer"] == PAYER and row["venue"] == "curve"
    assert row["top"] == [ROUTER]                                   # core programs gone, consecutive duplicate collapsed (pump program is core here)
    assert row["inner"] == [OTHER]
    assert row["xfers"] == [[FEE, 1_000_000], [TIP, 2_000_000], [FEE, 3_000]]
    assert row["cu_price"] == 5_000 and row["cu_limit"] == 200_000 and row["n_top"] == 7 and row["n_inner"] == 2


def test_shape_is_none_on_a_bad_index_and_ignores_funding_moves_and_caps_transfers():
    assert T.shape(KEYS, [(99, b"", b"")], [], venue="ps") is None
    assert T.shape(KEYS, [], [(0, [(99, b"", b"")])], venue="ps") is None
    big = T.shape(KEYS, [(1, bytes([0, 4]), transfer(6 * 10**9)), (1, bytes([0, 4]), transfer(0))], [], venue="ps")
    assert big["xfers"] == []                                       # above 5 SOL is funding, zero is nothing
    many = T.shape(KEYS, [(1, bytes([0, 4]), transfer(1_000 + i)) for i in range(20)], [], venue="ps")
    assert len(many["xfers"]) == T.MAX_TRANSFERS
    assert T.shape(KEYS, [(1, bytes([0, 4]), b"\x02\x00\x00")], [], venue="ps")["xfers"] == []          # not a 12-byte transfer


def test_hash_sampling_is_deterministic_by_signature_and_pct_bounds_hold():
    sigs = ["sig%06d" % i for i in range(4000)]
    share = sum(T.hash_sampled(s, 5) for s in sigs) / len(sigs)
    assert 0.035 < share < 0.065 and not any(T.hash_sampled(s, 0) for s in sigs[:200]) and all(T.hash_sampled(s, 100) for s in sigs[:200])
    assert [T.hash_sampled(s, 5) for s in sigs[:300]] == [T.hash_sampled(s, 5) for s in sigs[:300]]


def test_writer_stores_rows_idempotently_swallows_errors_and_deletes_old_days(tmp_path, monkeypatch):
    w = T.Writer(tmp_path, 100)
    now = 1_800_000_000.0
    row = sample_shape()
    assert w.add("sigA", 10, 1, now, row, now) and w.add("sigA", 10, 1, now, row, now)             # a duplicate is ignored by the key
    w.commit(now)
    c = sqlite3.connect(tmp_path / "txshape" / ("%s.db" % T.utc_day(now)))
    got = c.execute("SELECT signature, venue, payer, top_json, cu_price, version FROM txshape").fetchall()
    assert got == [("sigA", "curve", PAYER, json.dumps([ROUTER]), 5000, "txshape-v1")]
    assert not w.add("sigB", 1, 1, now, None, now) and w.stats["skipped_decode"] == 1
    old = tmp_path / "txshape" / ("%s.db" % T.utc_day(now - 10 * 86400))
    old.write_bytes(b"x")
    w.close()
    w.add("sigC", 1, 1, now + 1, row, now + 1)                                                    # reopen: day roll runs the retention
    assert not old.exists()
    monkeypatch.setattr(T.sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    w.close()
    assert w.add("sigD", 1, 1, now, row, now) is False and w.stats["errors"] >= 1                 # never raises


def test_candidates_apply_the_evidence_rule_and_exclude_tips_core_programs_and_self_transfers():
    rows = []
    for i in range(60):                                              # ROUTER and FEE: 60 txs, 25 payers, 4 hours
        rows.append((1_800_000_000 + (i % 4) * 3600, "payer%d" % (i % 25), [ROUTER], [], [[FEE, 1000], [TIP, 5000]]))
    rows += [(1_800_000_000, "p%d" % i, [ROUTER + "X"], [], [[OTHER, 10]]) for i in range(10)]        # too few txs
    rows += [(1_800_000_000 + i * 3600, "solo", ["OneWallet"], [], [["solo", 1]]) for i in range(60)]  # one payer, own account
    out = {(r["kind"], r["id"]): r for r in T.candidates(rows)}
    assert out[("router_program", ROUTER)]["qualifies"] and out[("fee_recipient", FEE)]["qualifies"]
    assert out[("fee_recipient", FEE)]["median_lamports"] == 1000 and out[("router_program", ROUTER)]["payers"] == 25
    assert not out[("router_program", ROUTER + "X")]["qualifies"] and not out[("router_program", "OneWallet")]["qualifies"]   # 1 payer
    assert all(k[1] != TIP for k in out) and ("fee_recipient", "solo") not in out                       # a tip and a self-transfer are never candidates
    assert T.candidates(rows, min_txs=10, min_payers=5, min_hours=1)[0]["qualifies"]
    assert all(r["id"] not in T.CORE_PROGRAMS for r in T.candidates([(1.0, "p", [T.SYSTEM, ROUTER], [U.PUMP_PROGRAM], [])]))


# ------------------------------------------------------------------------------------------------------ the worker wiring

def frame_with_route(slot, sig_byte, payload, *, fee=1_000_000):
    u = pb.SubscribeUpdate()
    t = u.transaction
    t.slot = slot
    t.transaction.index = 1
    t.transaction.signature = bytes([sig_byte]) * 64
    keys = [b"\x09" * 32, U.b58decode(U.PUMP_PROGRAM), U.b58decode(T.SYSTEM), b"\x07" * 32, b"\x08" * 32]
    t.transaction.transaction.message.account_keys.extend(keys)
    ix = t.transaction.transaction.message.instructions.add()
    ix.program_id_index = 3
    ix.accounts = bytes([0])
    ix.data = b"route"
    ix = t.transaction.transaction.message.instructions.add()
    ix.program_id_index = 2
    ix.accounts = bytes([0, 4])
    ix.data = struct.pack("<IQ", 2, fee)
    g = t.transaction.meta.inner_instructions.add()
    g.index = 0
    ev = g.instructions.add()
    ev.program_id_index = 1
    ev.data = U.CPI_EVENT_PREFIX + payload
    return u.SerializeToString()


def rows_of(tmp_path):
    out = []
    for f in (tmp_path / "txshape").glob("*.db"):
        c = sqlite3.connect(f)
        out += c.execute("SELECT signature, venue, payer, top_json, xfers_json FROM txshape").fetchall()
    return out


def test_the_worker_records_a_sampled_swap_after_its_band_emission_and_the_knob_zero_writes_nothing(tmp_path, monkeypatch):
    w, _ = worker(tmp_path, txshape_pct="100")
    order = []
    real_add, real_emit = w.txshape.add, w.emit
    monkeypatch.setattr(w.txshape, "add", lambda *a, **k: (order.append("txshape"), real_add(*a, **k))[1])
    monkeypatch.setattr(w, "emit", lambda *a, **k: (order.append("emit"), real_emit(*a, **k))[1])
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_with_route(10, 1, trade_event(*vq_for(20_000))))
    w.process(frame_with_route(11, 2, trade_event(*vq_for(40_000))))                       # crosses the band: one emission
    w.files.commit()
    w.txshape.commit(1e18)
    rows = rows_of(tmp_path)
    assert len(rows) == 2 and rows[0][1] == "curve" and json.loads(rows[0][3]) == [U.b58encode(b"\x07" * 32)]
    assert json.loads(rows[0][4]) == [[U.b58encode(b"\x08" * 32), 1_000_000]]
    assert order.index("emit") < len(order) - 1 and order[-1] == "txshape" and "emit" in order[:-1]  # the band emission came BEFORE the tx record
    off, _ = worker(tmp_path / "off", txshape_pct="0")
    off.process(frame_meta(10, 1_700_000_000))
    off.process(frame_with_route(11, 3, trade_event(*vq_for(40_000))))
    off.files.commit()
    assert off.txshape is None and not (tmp_path / "off" / "txshape").exists()
    off.heartbeat(True)
    assert "txshape" not in json.loads((tmp_path / "off" / "universe.heartbeat").read_text())


def test_a_txshape_failure_never_breaks_the_tape_or_the_band_emission_and_reaches_the_heartbeat(tmp_path, monkeypatch):
    w, _ = worker(tmp_path, txshape_pct="100")
    monkeypatch.setattr(T, "shape", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    w.process(frame_meta(10, 1_700_000_000))
    w.process(frame_with_route(10, 1, trade_event(*vq_for(20_000))))
    w.process(frame_with_route(11, 2, trade_event(*vq_for(40_000))))
    w.files.commit()
    assert w.stats["emissions"] == 1 and w.stats["txshape_errors"] == 2
    c = sqlite3.connect(next((tmp_path / "swaps").glob("*.db")))
    assert c.execute("SELECT count(*) FROM swaps").fetchone()[0] == 2                      # the curve tape is intact
    w.heartbeat(True)
    beat = json.loads((tmp_path / "universe.heartbeat").read_text())["txshape"]
    assert beat["errors"] == 2 and beat["pct"] == 100 and beat["us_p50"] is not None


def test_unsampled_transactions_are_skipped_by_the_hash_gate(tmp_path):
    w, _ = worker(tmp_path, txshape_pct="5")
    sigs = 0
    for i in range(1, 120):
        w.process(frame_with_route(10 + i, i, trade_event(*vq_for(20_000))))
        sigs += 1
    w.txshape.commit(1e18)
    n = len(rows_of(tmp_path))
    assert 0 < n < sigs * 0.2 and w.txshape.stats["sampled"] == n
