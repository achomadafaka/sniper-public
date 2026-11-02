"""Desk intake step 3: IntakeBook turns a fresh intake sighting into a PumpSwap candidate on the coin's next proven BUY
print (same state build as the band book), durably, with a sliding cooldown and one outcome per sighting."""
import json
from datetime import datetime, timezone
import sqlite3

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_intake as I
from sniper.services import memecoin_desk_intake_book as B
from test_memecoin_desk_pumpswap_candidates import MINT, NOW, SIG, event, facts

SOL = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
OFFSET = 7 * 3600.0                     # a +7 h host clock: the sighting age must use the row's own offset


def sight(tmp_path, at_host, *, mint=MINT, filt="active", offset=OFFSET):
    out = I.open_day(tmp_path / "intake", "pumpfun", at_host)
    I.record_poll(out, "pumpfun", filt, at_host, [{"network_id": "solana", "chain_raw": SOL, "mint": mint,
                                                   "venue": "pumpswap", "raw": {"complete": True}}], offset_s=offset)
    out.close()


def book(tmp_path, **kw):
    return B.IntakeBook(intake_dir=tmp_path / "intake", db_path=tmp_path / "emitter.db", **kw)


def printed(b, now, quote="40000000000"):
    m, meta = facts()
    return b.on_print(event=event(quote), mapping=m, metadata=meta, slot=10, signature=SIG, ordinal=0, tx_index=None,
                      now=now, received_at=now, sol_usd=100, sol_usd_at=now - 1)


def test_a_fresh_sighting_becomes_one_valid_intake_candidate_on_the_next_buy(tmp_path):
    sight(tmp_path, NOW - 30)
    b = book(tmp_path)
    b.reload(NOW)
    [(oid, body)] = printed(b, NOW)
    c = K.validate_candidate(json.loads(body))
    assert c["ticket_id"] == oid and oid.startswith("desk:U:intake:") and c["venue"] == "pumpswap"
    assert c["intake"]["feed"] == "pumpfun" and c["intake"]["filter"] == "active" and c["intake"]["clock_offset_s"] == OFFSET
    assert c["snapshot"]["fields"]["pool_state"]["value"]["supply_atomic"] == "1000000000000000"
    assert b.take_pending() == [(oid, body)]                      # stored BEFORE the caller appends
    b.committed(oid)
    assert b.take_pending() == []
    assert printed(b, NOW + 1) == []                               # one candidate per sighting


def test_stale_future_or_unknown_clock_sightings_never_emit(tmp_path):
    sight(tmp_path, NOW - 300, mint="A" * 43)                     # 300 s old (corrected)
    sight(tmp_path, NOW + 60, mint="B" * 43)                      # future
    sight(tmp_path, NOW - 10, mint="C" * 43, offset=None)         # offset unknown: observed_at NULL
    b = book(tmp_path)
    b.reload(NOW)
    assert b.listed == {}


def test_the_cooldown_slides_across_midnight_and_restarts(tmp_path):
    day_end = 1_800_057_599.0                                     # 23:59:59 UTC of the fixture day
    sight(tmp_path, day_end - 5)
    b = book(tmp_path)
    b.reload(day_end)
    [(oid, _)] = printed(b, day_end)
    b.committed(oid)
    sight(tmp_path, day_end + 2, filt="active")                   # 00:00:01, a NEW sighting of the same coin
    b2 = book(tmp_path)                                            # restart
    b2.reload(day_end + 2)
    assert printed(b2, day_end + 3) == []                          # a later print, within the sliding hour: cooldown
    reasons = dict(sqlite3.connect(tmp_path / "emitter.db").execute("SELECT reason, COUNT(*) FROM outcomes GROUP BY 1"))
    assert reasons == {"emitted": 1, "cooldown": 1}


def test_a_restart_reappends_the_stored_bytes_not_a_rebuilt_candidate(tmp_path):
    sight(tmp_path, NOW - 30)
    b = book(tmp_path)
    b.reload(NOW)
    [(oid, body)] = printed(b, NOW)                                # crash before the append: still pending
    b2 = book(tmp_path)
    assert b2.take_pending() == [(oid, body)]                      # identical bytes, whatever the tape now says


def test_a_listed_coin_that_never_prints_gets_no_print(tmp_path):
    sight(tmp_path, NOW - 30)
    b = book(tmp_path)
    b.reload(NOW)
    b.reload(NOW + 200)                                            # aged out with no BUY print
    assert sqlite3.connect(tmp_path / "emitter.db").execute("SELECT reason FROM outcomes").fetchall() == [("no_print",)]


def test_an_unproven_print_waits_for_the_next_one(tmp_path):
    sight(tmp_path, NOW - 30)
    b = book(tmp_path)
    b.reload(NOW)
    m, meta = facts()
    assert b.on_print(event={**event(), "kind": "sell"}, mapping=m, metadata=meta, slot=10, signature=SIG, ordinal=0,
                      tx_index=None, now=NOW, received_at=NOW, sol_usd=100, sol_usd_at=NOW - 1) == []
    assert len(printed(b, NOW + 1)) == 1                           # the sighting survived the unproven print


def _ps_worker(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CAPTURE", "1")
    spec = importlib.util.spec_from_file_location(
        "desk_universe_worker_intake", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_universe_worker.py")
    W = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(W)
    w = W.Worker(W.knobs({}), base=tmp_path, sol_price_fn=lambda: None, alert_fn=lambda *a: None)
    return w


def test_an_intake_failure_never_reaches_the_tape(tmp_path, monkeypatch):
    """Alpha's condition 1: any IntakeBook exception is caught and counted inside the worker's hook."""
    w = _ps_worker(tmp_path, monkeypatch)
    assert w.intake_book is not None                               # on by default with PS capture

    class Boom:
        listed = {MINT: {}}
        def on_print(self, **kw):
            raise RuntimeError("boom")
    w.intake_book = Boom()
    m, _ = facts()
    w.pool_metadata = lambda *a: None
    w.intake_hook(event(), m, 10, SIG, 0, None, NOW, NOW)          # must not raise
    assert w.stats["intake_errors"] == 1
    w.intake_apply(({}, 0), NOW)                                    # Boom has no apply_listed: counted, not raised
    assert w.stats["intake_errors"] == 2


def test_the_kill_knob_turns_the_book_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_INTAKE_EMIT", "0")
    assert _ps_worker(tmp_path, monkeypatch).intake_book is None


def test_a_pending_claim_is_appended_once_at_worker_start(tmp_path, monkeypatch):
    sight(tmp_path, NOW - 30)
    b = B.IntakeBook(intake_dir=tmp_path / "intake", db_path=tmp_path / "intake_emitter.db")
    b.reload(NOW)
    [(oid, body)] = printed(b, NOW)                                # crash before the append
    w = _ps_worker(tmp_path, monkeypatch)                          # restart: re-append the stored bytes
    files = list((tmp_path / "candidate_emissions_intake").iterdir())
    assert [l for f in files for l in f.read_text().splitlines()] == [body]
    assert w.intake_book.take_pending() == []


def _ps_print(pool, quote="40000000000"):
    e = dict(event(quote))
    e.update(pool=pool, actor="A" * 43, coin_creator="C" * 43, base_atomic="1000", quote_atomic="1000",
             pre_base_reserve_atomic="100000001000", pre_quote_reserve_atomic="39999999000", buyback_fee_bps=0,
             event_time=int(NOW), payload={}, payload_b64="")
    return e


def test_live_shaped_a_listed_coin_outside_m_with_no_binding_and_no_metadata_emits_on_its_next_buy(tmp_path, monkeypatch):
    """Alpha F1+F2+F3 with today's live shape: the pool is not in M, there is no create_pool / ps_grads row, and
    data/desk/pumpswap/metadata.db does not exist. The print is stored while listed, bound by the canonical PDA, and
    the candidate carries no supply (partial pool_state, mcap absent) for the assembler's mint read to fill."""
    from sniper.services import memecoin_desk_safety as S
    w = _ps_worker(tmp_path, monkeypatch)
    w.sol_usd, w.sol_at = 100, NOW - 1
    pool = S.pumpswap_pool_address(MINT)
    assert pool not in w.matter_set(NOW) and not (tmp_path / "pumpswap" / "metadata.db").exists()
    sight(tmp_path, NOW - 30)
    w.intake_apply(w.intake_book.read_listed(NOW), NOW)
    assert w.intake_book.pools == {pool: MINT}
    w.on_pumpswap(_ps_print(pool), 10, SIG, 0, None, None, NOW, NOW)
    assert w.stats["intake_errors"] == 0 and w.stats["intake_emissions"] == 1
    w.commit_files()
    stored = sqlite3.connect(tmp_path / "pumpswap" / "tape.db").execute(
        "SELECT map_status FROM swaps_ps WHERE pool=?", (pool,)).fetchall()
    assert stored == [("unmapped",)]                               # stored while listed; the row keeps its own status
    [line] = [l for f in (tmp_path / "candidate_emissions_intake").iterdir() for l in f.read_text().splitlines()]
    c = K.validate_candidate(json.loads(line))
    f = c["snapshot"]["fields"]
    assert f["pool_state"]["completeness"] == "partial" and f["pool_state"]["value"]["supply_atomic"] is None
    assert f["mcap_usd"]["value"] is None and f["liq_usd"]["value"] == 4000
    assert f["pool_state"]["value"]["map_source"] == "intake_canonical_pda"
    assert f["pool_state"]["value"]["map_known_at"] == NOW                     # the print's own receipt


def test_an_unlisted_pool_outside_m_is_still_held_and_never_bound(tmp_path, monkeypatch):
    from sniper.services import memecoin_desk_safety as S
    w = _ps_worker(tmp_path, monkeypatch)
    w.sol_usd, w.sol_at = 100, NOW - 1
    pool = S.pumpswap_pool_address(MINT)
    w.on_pumpswap(_ps_print(pool), 10, SIG, 0, None, None, NOW, NOW)
    assert w.stats["intake_emissions"] == 0 and w.ps_hold_rows == 1     # held, as today


def test_the_partial_intake_candidate_passes_intake_v1_with_the_mint_reads_supply(tmp_path, monkeypatch):
    """F3 end of the line: the assembler's mint read supplies supply/decimals; a missing read still refuses."""
    from sniper.services import memecoin_desk_safety as S
    from test_memecoin_desk_contract import evidence, policy, AT
    w = _ps_worker(tmp_path, monkeypatch)
    w.sol_usd, w.sol_at = 100, NOW - 1
    pool = S.pumpswap_pool_address(MINT)
    sight(tmp_path, NOW - 30)
    w.intake_apply(w.intake_book.read_listed(NOW), NOW)
    w.on_pumpswap(_ps_print(pool, quote="200000000000"), 10, SIG, 0, None, None, NOW, NOW)    # 200 SOL x $100
    [line] = [l for f in (tmp_path / "candidate_emissions_intake").iterdir() for l in f.read_text().splitlines()]
    c = json.loads(line)
    pol = {**policy(), "intake": {"version": "intake-v1", "mcap_safety": [5000, 50_000_000], "liq_min_quote_sol": 40}}
    e = evidence(c)
    e["checks"]["token_safety"].update(supply_atomic="1000000000000000", decimals=6)
    when = datetime.fromtimestamp(NOW + 5, timezone.utc).isoformat()
    e["known_at"] = when
    assert K.hard_filter(c, evidence=e, policy=pol, evaluated_at=when)["reason"] == "pass"
    del e["checks"]["token_safety"]["supply_atomic"]
    assert K.hard_filter(c, evidence=e, policy=pol, evaluated_at=when)["reason"] == "unknown:market_data:supply"


def test_a_print_received_before_the_sighting_never_triggers_it(tmp_path):
    """Zeta fe4017bd7 P1: sighting received at NOW-1, a queued print received at NOW-10 processed at NOW."""
    sight(tmp_path, NOW - 1)
    b = book(tmp_path)
    b.reload(NOW)
    m, meta = facts()
    assert b.on_print(event=event("40000000000"), mapping=m, metadata=meta, slot=10, signature=SIG, ordinal=0,
                      tx_index=None, now=NOW, received_at=NOW - 10, sol_usd=100, sol_usd_at=NOW - 11) == []
    assert len(printed(b, NOW + 1)) == 1                           # the sighting waits for a genuinely later print


def test_emitted_is_recorded_only_after_the_durable_append(tmp_path):
    """Zeta P2: an append failure leaves the claim pending AND the outcome 'claimed', never 'emitted'."""
    sight(tmp_path, NOW - 30)
    b = book(tmp_path)
    b.reload(NOW)
    [(oid, _)] = printed(b, NOW)
    db = sqlite3.connect(tmp_path / "emitter.db")
    assert db.execute("SELECT reason FROM outcomes").fetchall() == [("claimed",)]
    b.committed(oid)
    assert db.execute("SELECT reason FROM outcomes").fetchall() == [("emitted",)]


def test_a_partial_line_from_a_crash_is_terminated_before_the_reappend(tmp_path, monkeypatch):
    """A crash mid-append leaves bytes without a newline; the restart's re-append must be its own whole line."""
    sight(tmp_path, NOW - 30)
    b = B.IntakeBook(intake_dir=tmp_path / "intake", db_path=tmp_path / "intake_emitter.db")
    b.reload(NOW)
    [(oid, body)] = printed(b, NOW)                                # claimed, append crashed half-way
    out = tmp_path / "candidate_emissions_intake"
    out.mkdir()
    import time
    (out / ("%s.jsonl" % time.strftime("%Y-%m-%d", time.gmtime()))).write_text(body[:40])
    _ps_worker(tmp_path, monkeypatch)                              # restart: re-append
    lines = [l for f in out.iterdir() for l in f.read_text().splitlines()]
    assert lines[-1] == body and lines[0] == body[:40]


def test_am36_the_cut_is_the_end_of_the_trigger_receipt_batch_not_the_processing_time(tmp_path):
    """Cohort D Amendment 36: emission_at = nextafter(trigger received_at), so every reader's received_at < cut bound
    holds exactly the trigger's receipt batch; swaps received after the print but before processing are outside the
    cut (live 2026-10-29: 8 of 12 pool_tip_unproven). The processing time stays on the claim (processed_at)."""
    sight(tmp_path, NOW - 30)
    b = book(tmp_path)
    b.reload(NOW)
    m, meta = facts()
    received = NOW - 0.75                                   # the print was received 0.75 s before it was processed
    [(oid, body)] = b.on_print(event=event(), mapping=m, metadata=meta, slot=10, signature=SIG, ordinal=0,
                               tx_index=None, now=NOW, received_at=received, sol_usd=100, sol_usd_at=received - 1)
    c = K.validate_candidate(json.loads(body))
    cut = c["snapshot"]["emission_at"]
    assert received < cut <= received + 1e-6 and datetime.fromisoformat(c["emitted_at"]).timestamp() == cut
    assert c["snapshot"]["fields"]["pool_state"]["value"]["reserve_known_at"] == received
    processed_at, stored = b.db.execute("SELECT claimed_at_host, candidate_json FROM claims").fetchone()
    snap = json.loads(stored)["snapshot"]
    assert processed_at == NOW                               # processed_at = the claim row's existing time column
    assert snap["emission_at"] == B.batch_end(snap["fields"]["pool_state"]["value"]["reserve_known_at"])  # cut_basis
    assert B.CUT_BASIS == "batch_end"


def test_am36_fx_known_only_after_the_print_cannot_price_the_cut(tmp_path):
    sight(tmp_path, NOW - 30)
    b = book(tmp_path)
    b.reload(NOW)
    m, meta = facts()
    received = NOW - 0.75
    assert b.on_print(event=event(), mapping=m, metadata=meta, slot=10, signature=SIG, ordinal=0, tx_index=None,
                      now=NOW, received_at=received, sol_usd=100, sol_usd_at=NOW - 0.1) == []   # FX after the cut


def test_am36_the_intake_hook_prices_with_the_cached_fx_sample_at_or_before_the_cut(tmp_path, monkeypatch):
    """Replay 2026-10-29: 23/295 (7.8%) emissions in 2 h had FX fetched after the print and would refuse under the
    batch-end cut. The worker keeps the previous cached sample and prices the print with the newest one at or before
    the cut; no fetch."""
    w = _ps_worker(tmp_path, monkeypatch)
    w.sol_prev, (w.sol_usd, w.sol_at) = (99.0, NOW - 50), (101.0, NOW + 0.2)
    assert w.fx_at_or_before(NOW) == (99.0, NOW - 50)              # current sample fetched after the cut: previous one
    assert w.fx_at_or_before(NOW + 1) == (101.0, NOW + 0.2)
    w.sol_prev = (None, None)
    assert w.fx_at_or_before(NOW) == (None, None)                  # nothing at or before the cut: unknown, never later FX
    seen = {}

    class Book:
        listed = {MINT: {}}
        def on_print(self, **kw):
            seen.update(kw)
            return []
    w.intake_book = Book()
    w.sol_prev, (w.sol_usd, w.sol_at) = (99.0, NOW - 50), (101.0, NOW + 0.2)
    m, meta = facts()
    w.intake_hook(event(), m, 10, SIG, 0, None, NOW + 0.5, NOW)
    assert (seen["sol_usd"], seen["sol_usd_at"]) == (99.0, NOW - 50)



def test_am37_the_cut_is_the_first_microsecond_boundary_strictly_after_the_receipt():
    import math
    r = 1_800_000_000.1234561          # early in its microsecond bucket (one ULP here is ~0.24 us)
    cut = B.batch_end(r)
    assert cut == datetime.fromisoformat(datetime.fromtimestamp(cut, timezone.utc).isoformat()).timestamp()
    assert r < cut and math.nextafter(r, math.inf) < cut              # the same microsecond (r + 1 ULP) is inside
    assert not cut < cut                                               # a receipt AT the boundary is outside (< cut)
    on_boundary = datetime.fromisoformat("2027-01-15T08:00:00.500000+00:00").timestamp()
    assert B.batch_end(on_boundary) > on_boundary                     # a receipt exactly on a boundary: the NEXT one
    assert abs(B.batch_end(on_boundary) - on_boundary - 1e-6) < 1e-7


def test_am37_fx_after_cut_is_counted(tmp_path, monkeypatch):
    w = _ps_worker(tmp_path, monkeypatch)
    w.sol_prev, (w.sol_usd, w.sol_at) = (None, None), (101.0, NOW + 0.2)

    class Book:
        listed = {MINT: {"pumpfun:active:ref": {}}}              # a pending sighting
        deferred = []
        def fx_deferred(self, mint, now):
            self.deferred.append((mint, now))
        def on_print(self, **kw):
            return []
    w.intake_book = Book()
    m, meta = facts()
    w.intake_hook(event(), m, 10, SIG, 0, None, NOW + 0.5, NOW)
    assert w.stats["prints_fx_after_cut"] == 1              # a print diagnostic; on_print never called



def test_am39_fx_deferrals_are_one_insert_only_row_per_coin_with_its_first_ticket(tmp_path):
    sight(tmp_path, NOW - 30)
    b = book(tmp_path)
    b.reload(NOW)
    b.fx_deferred(MINT, NOW - 2)
    b.fx_deferred(MINT, NOW - 1)                                     # a second deferral only counts the print
    assert b.db.execute("SELECT network_id,mint,first_fx_deferred_at,deferral_prints,first_ticket_at FROM fx_deferrals"
                        ).fetchall() == [("solana", MINT, NOW - 2, 2, None)]
    [(oid, body)] = printed(b, NOW)                                  # the coin tickets: first_ticket_at set once
    assert b.db.execute("SELECT first_ticket_at FROM fx_deferrals").fetchone() == (NOW,)
    b.db.execute("UPDATE fx_deferrals SET first_ticket_at=first_ticket_at")
    assert b.db.execute("SELECT COUNT(*) FROM claims").fetchone() == (1,)   # claims untouched: 10 columns, no ALTER
    assert len(b.db.execute("PRAGMA table_info(claims)").fetchall()) == 10
