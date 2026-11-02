"""The fill-timing resolver (spec rev 2, 2.4): one row per SUBMITTED attempt in the timing file; what
is stored first, reads only behind their knobs; never the ledger for writing."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scripts.research import fill_timing_resolver as r
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_fill_timing as ft

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
SRC, OURS = "5uRiphjyAVbVVhFEeBWTFfMBsWfTidMJRtZBUwBR2DsGdp11QLAYpkY2rhEpGSooHTk4", "3n73buoDJitHLmyRpdTS2LHycX75TmUHnLL8fd4e8u5U3pb47jLzmgENUsMnKxDxgkwdhjDY"


def _iso(minutes_ago=0.0):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


@pytest.fixture
def dbs(tmp_path):
    ledger = sqlite3.connect(tmp_path / "ledger.db")
    ledger.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(ledger)
    timing = ft.connect(tmp_path / "timing.db")
    return ledger, timing


def _attempt(ledger, aid, *, status="filled", origin="onchain:" + SRC, source="onchain_watch", lane=None,
             network="solana", tx=OURS, trace=None, reason=None, minutes_ago=1.0):
    ledger.execute(
        "INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, delivery_lane, "
        "network_id, mint, decision_started_at, observed_at, transaction_submitted_at, "
        "chain_confirmation_completed_at, completed_at, terminal_status, terminal_reason, tx_sig, trace_json) "
        "VALUES (?,?,?,?,?,'MINT',?,?,?,?,?,?,?,?,?)",
        (aid, origin, source, lane, network, _iso(minutes_ago), _iso(minutes_ago), _iso(minutes_ago - 0.005),
         _iso(minutes_ago - 0.01), _iso(minutes_ago - 0.012), status, reason or status, tx, trace))
    ledger.commit()


def _landing_trace(slot):
    return json.dumps({"submission_events": [{"stage": "chain_confirmation", "landing_slot": slot}]})


def _detection(ledger, sig=SRC, slot=100):
    m.record_onchain_detection(ledger, signature=sig, source_user_id="u", handle="h", address="A", mint="MINT",
                               detected_at=_iso(1), created_at=_iso(1), block_time=1790932881, source_slot=slot)


def _row(timing, aid):
    timing.row_factory = sqlite3.Row
    got = timing.execute("SELECT * FROM memecoin_fill_timing WHERE attempt_id=?", (aid,)).fetchone()
    return dict(got) if got else None


def _pass(ledger, timing, **kw):
    kw.setdefault("now", NOW)
    return r.resolve_pass(ledger, timing, **kw)


def test_every_submitted_attempt_gets_a_timing_row(dbs):
    """The check Alpha asked for: it fails if a fill can be written without its timing row."""
    ledger, timing = dbs
    _attempt(ledger, "filled", trace=_landing_trace(105))
    _attempt(ledger, "failed", status="failed", reason="slippage")
    _attempt(ledger, "refused", status="refused", tx=None)
    _attempt(ledger, "presubmit", status="failed", tx=None)
    _pass(ledger, timing)
    rows = {x[0] for x in timing.execute("SELECT attempt_id FROM memecoin_fill_timing")}
    assert rows == {"filled", "failed"}


def test_a_slot_lane_fill_is_complete_with_no_read(dbs):
    ledger, timing = dbs
    trace = json.dumps({"source_evidence": {"source_slot": 100},
                        "submission_events": [{"stage": "chain_confirmation", "landing_slot": 101}]})
    _attempt(ledger, "a", origin="slotlane:" + SRC, source="slot_lane", lane="slot_lane_processed", trace=trace)
    calls = []
    _pass(ledger, timing, solana_read=lambda sigs: calls.append(sigs) or {})
    row = _row(timing, "a")
    assert (row["state"], row["gap"], row["cause"], row["reads"]) == ("complete", 1, "on_time", 0) and calls == []


def test_an_onchain_fill_with_captures_is_complete_without_reads(dbs):
    ledger, timing = dbs
    _detection(ledger, slot=100)
    _attempt(ledger, "a", trace=_landing_trace(106))
    _pass(ledger, timing)
    row = _row(timing, "a")
    assert (row["state"], row["gap"], row["source_slot_basis"], row["cause"]) == (
        "complete", 6, "watcher", "slot_lane_skipped")
    assert "slot lane never saw it" in row["cause_detail"]


def test_the_slot_lane_skip_row_names_the_cause(dbs):
    ledger, timing = dbs
    _detection(ledger, slot=100)
    _attempt(ledger, "a", trace=_landing_trace(106))
    timing.execute("INSERT INTO memecoin_slot_lane_skips VALUES (?, 'handoff', 'pool_unknown', ?)", (SRC, _iso(1)))
    timing.commit()
    _pass(ledger, timing)
    assert "slot lane: handoff pool_unknown" in _row(timing, "a")["cause_detail"]


def test_a_needed_read_with_the_knob_off_is_unread_and_reopens_when_it_turns_on(dbs):
    ledger, timing = dbs
    _attempt(ledger, "a")                                     # no detection slot, no landing slot
    _pass(ledger, timing, solana_read=None)
    assert _row(timing, "a")["state"] == "unread"
    calls = []

    def read(sigs):
        calls.append(sorted(sigs))
        return {SRC: 100, OURS: 104}
    _pass(ledger, timing, solana_read=read)
    row = _row(timing, "a")
    assert (row["state"], row["gap"], row["source_slot_basis"], row["landing_slot_basis"]) == (
        "complete", 4, "rpc", "rpc")
    assert calls == [sorted([SRC, OURS])]                     # ONE batched call holding both


def test_solana_reads_are_batched_across_attempts(dbs):
    ledger, timing = dbs
    _attempt(ledger, "a", origin="onchain:SRCA", tx="OURSA")
    _attempt(ledger, "b", origin="onchain:SRCB", tx="OURSB")
    calls = []
    _pass(ledger, timing, solana_read=lambda sigs: calls.append(sorted(sigs)) or {
        "SRCA": 1, "OURSA": 2, "SRCB": 10, "OURSB": 12})
    assert calls == [["OURSA", "OURSB", "SRCA", "SRCB"]]


def test_our_signature_still_null_after_ten_minutes_is_not_landed(dbs):
    ledger, timing = dbs
    _detection(ledger, slot=100)
    _attempt(ledger, "a", status="failed", reason="expired", minutes_ago=11)
    _pass(ledger, timing, solana_read=lambda sigs: {SRC: 100, OURS: None})
    assert _row(timing, "a")["state"] == "not_landed"


def test_an_unread_row_past_the_window_is_unknown(dbs):
    ledger, timing = dbs
    _attempt(ledger, "a")
    _pass(ledger, timing)
    _pass(ledger, timing, now=NOW + timedelta(hours=25))
    assert _row(timing, "a")["state"] == "unknown"


def test_a_j7_source_resolved_by_read_sends_one_follow_up(dbs):
    ledger, timing = dbs
    _attempt(ledger, "a", origin="swap:raybot:%s:addr:mint" % SRC, source="follow_activity", lane="j7_wallets",
             trace=_landing_trace(107))
    sent = []
    for _ in range(2):
        _pass(ledger, timing, solana_read=lambda sigs: {SRC: 100}, alert=sent.append)
    assert len(sent) == 1 and "S+7" in sent[0] and _row(timing, "a")["followup_sent"] == 1


def test_main_opens_the_ledger_read_only(tmp_path, monkeypatch, dbs):
    ledger, timing = dbs
    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda path, *a, **k: opened.append((str(path), k.get("uri"))) or real(path, *a, **k))
    led = tmp_path / "ledger.db"
    r.main(["--ledger", str(led), "--timing", str(tmp_path / "timing.db"), "--max-seconds", "1"])
    ledger_opens = [o for o in opened if "ledger.db" in o[0]]
    assert ledger_opens and all(o[0].startswith("file:") and "mode=ro" in o[0] and o[1] for o in ledger_opens)


def test_a_manual_entry_has_no_source_and_completes_on_its_landing(dbs):
    ledger, timing = dbs
    _attempt(ledger, "a", origin=None, source="manual", trace=_landing_trace(500))
    _pass(ledger, timing)
    row = _row(timing, "a")
    assert (row["state"], row["gap"], row["cause"]) == ("no_source", None, "manual")


def test_the_solana_reader_maps_statuses_and_raises_on_no_result():
    calls = []
    read = r.solana_reader("https://helius.example/?api-key=k", rpc=lambda method, params: calls.append(
        (method, params)) or {"value": [{"slot": 100}, None]})
    assert read(["A", "B"]) == {"A": 100, "B": None}
    assert calls == [("getSignatureStatuses", [["A", "B"], {"searchTransactionHistory": True}])]
    with pytest.raises(RuntimeError):
        r.solana_reader("u", rpc=lambda *a: None)(["A"])


def test_a_relative_db_path_resolves_under_the_state_root_not_the_cwd(tmp_path, monkeypatch, capsys):
    """Live 2026-10-02 12:06Z: .env carries DB_PATH=sniper_tracker.db, the cron runs in $HOME, and a
    stray empty ~/sniper_tracker.db answered 'no such table: memecoin_entry_attempts'."""
    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)
    # Keep this path test inside the resolver's 24-hour window after the fixture's calendar date.
    monkeypatch.setattr(r, "datetime", FixtureClock)
    root, elsewhere = tmp_path / "root", tmp_path / "home"
    (root / "data").mkdir(parents=True)
    elsewhere.mkdir()
    sqlite3.connect(elsewhere / "sniper_tracker.db").close()            # the stray empty file
    led = sqlite3.connect(root / "sniper_tracker.db")
    led.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(led)
    _attempt(led, "a", trace=_landing_trace(106))
    led.close()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(root))
    monkeypatch.setenv("DB_PATH", "sniper_tracker.db")
    monkeypatch.chdir(elsewhere)
    assert r.main(["--timing", str(tmp_path / "t.db"), "--summary"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["new"] == 1


# --- R8 (Codex review 2026-10-06): 'complete' means the timing was measured ----------------------
TX56, MINT56 = "0x" + "a9" * 32, "0x7580d478f78d5a59c94cde67a4b4c3f3afa97777"


def _fast_trace(source_block=None):
    return json.dumps({
        "source_evidence": {"network_id": "56", "source_block_number": source_block},
        "substage_events": [{"name": "rh_fast_lane_send", "completed_at": _iso(0.98)},
                            {"name": "rh_fast_lane_receipt", "completed_at": _iso(0.97),
                             "block": 126067043, "block_time": 1791292837}]})


def test_a_bsc_fast_lane_fill_is_timed_from_its_trace_without_reads(dbs):
    ledger, timing = dbs
    _attempt(ledger, "a", origin="bscfast:56:%s:%s" % (TX56, MINT56), source="bsc_fast_lane",
             lane="bsc_fast_lane", network="56", tx="0x" + "45" * 32, trace=_fast_trace(126067036))
    _pass(ledger, timing)
    row = _row(timing, "a")
    assert (row["state"], row["gap"], row["source_sig"], row["source_slot_basis"]) == (
        "complete", 7, TX56, "fast_lane")
    assert row["cause"] != "fast_lane_skipped" and row["reads"] == 0


def test_a_fast_lane_fill_without_its_source_block_is_not_complete(dbs):
    """56 has no read host: the row waits as unread (then unknown), never 'complete' with NULLs."""
    ledger, timing = dbs
    _attempt(ledger, "a", origin="bscfast:56:%s:%s" % (TX56, MINT56), source="bsc_fast_lane",
             lane="bsc_fast_lane", network="56", tx="0x" + "45" * 32, trace=_fast_trace(None))
    _pass(ledger, timing)
    row = _row(timing, "a")
    assert (row["state"], row["gap"], row["source_sig"]) == ("unread", None, TX56)
    assert row["cause"] != "fast_lane_skipped"


def test_an_origin_naming_no_readable_source_is_unsupported_not_complete(dbs):
    ledger, timing = dbs
    _attempt(ledger, "a", origin="swap:raybot:" + SRC.lower(), source="j7", trace=_landing_trace(500))
    _pass(ledger, timing)
    assert _row(timing, "a")["state"] == "unsupported"


def test_a_row_stored_complete_without_a_gap_is_reopened(dbs):
    """Rows written 'complete' with no measured gap before R8 are revisited once."""
    ledger, timing = dbs
    _attempt(ledger, "a", origin="bscfast:56:%s:%s" % (TX56, MINT56), source="bsc_fast_lane",
             lane="bsc_fast_lane", network="56", tx="0x" + "45" * 32, trace=_fast_trace(126067036))
    timing.execute("INSERT INTO memecoin_fill_timing (attempt_id, state, first_seen_at, updated_at, cause) "
                   "VALUES ('a', 'complete', ?, ?, 'fast_lane_skipped')", (_iso(5), _iso(5)))
    timing.execute("INSERT INTO memecoin_fill_timing_cursor VALUES ('attempts_completed_at', ?)", (_iso(0),))
    timing.commit()
    _pass(ledger, timing)
    row = _row(timing, "a")
    assert (row["state"], row["gap"]) == ("complete", 7) and row["cause"] != "fast_lane_skipped"


def _j7_bsc_attempt(ledger, aid="a", *, trace=None):
    origin = "swap:raybot:%s:%s:%s" % (TX56, "0x" + "ef" * 20, MINT56)
    _attempt(ledger, aid, origin=origin, source="follow_activity", lane="j7_wallets",
             network="56", tx="0x" + "45" * 32, trace=trace or _landing_trace(126956875))


def test_receipt_observation_completes_bsc_without_any_additional_rpc(dbs):
    ledger, timing = dbs
    trace = json.dumps({"substage_events": [
        {"name": "evm_source_receipt_observation", "observation": {
            "network_id": "56", "source_tx": TX56, "source_block_number": 126956870}},
        {"name": "evm.confirmation", "block": 126956875}]})
    _j7_bsc_attempt(ledger, trace=trace)
    calls = []
    _pass(ledger, timing, evm_read=lambda *args: calls.append(args), evm_chains=("56",))
    row = _row(timing, "a")
    assert (row["state"], row["source_slot_basis"], row["gap"], row["reads"]) == (
        "complete", "receipt_observation", 5, 0)
    assert calls == []


def test_bsc_dedicated_read_host_is_admitted_only_on_the_right_chain(monkeypatch):
    from scripts.research import verify_axiom_addresses as verify
    from sniper.services import memecoin_evm_post_trade as pt
    monkeypatch.setattr(verify, "code_read_url", lambda *_: (None, "unconfigured fixture"))
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56", "https://bsc-reader.example/rpc")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://custody.example/rpc")
    monkeypatch.setattr(pt, "default_read_fn", lambda *_: lambda *args: "0x38")
    assert r.evm_hosts() == {"56": "https://bsc-reader.example/rpc"}
    monkeypatch.setattr(pt, "default_read_fn", lambda *_: lambda *args: "0x1")
    assert r.evm_hosts() == {}


@pytest.mark.parametrize("url", ["", "http://bsc-reader.example/rpc", "https://custody.example/rpc", "https://["])
def test_bsc_dedicated_read_host_never_uses_missing_insecure_or_custody_url(monkeypatch, url):
    from scripts.research import verify_axiom_addresses as verify
    from sniper.services import memecoin_evm_post_trade as pt
    monkeypatch.setattr(verify, "code_read_url", lambda *_: (None, "unconfigured fixture"))
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56", url)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "https://custody.example/rpc")
    calls = []
    monkeypatch.setattr(pt, "default_read_fn", lambda *_: lambda *args: calls.append(args) or "0x38")
    assert r.evm_hosts() == {} and calls == []


def test_bounded_pending_work_does_not_read_the_full_existing_backlog(dbs):
    ledger, timing = dbs
    for i in range(6):
        aid = "a%d" % i
        _j7_bsc_attempt(ledger, aid)
        timing.execute("INSERT INTO memecoin_fill_timing (attempt_id,state,first_seen_at,updated_at) "
                       "VALUES (?,'unread',?,?)", (aid, _iso(1), _iso(1)))
    timing.commit()
    _pass(ledger, timing, limit=2)
    processed = timing.execute("SELECT count(*) FROM memecoin_fill_timing WHERE network_id='56'").fetchone()[0]
    assert processed == 2


def test_resolver_deadline_preserves_a_source_fact_and_stops_before_the_next_rpc(dbs):
    ledger, timing = dbs
    _j7_bsc_attempt(ledger)
    clock = [0.0]
    calls = []

    def read(chain, method, params):
        calls.append((chain, method, params))
        clock[0] = 2.0
        return {"transactionHash": TX56, "blockNumber": "0x7913546", "status": "0x1", "logs": []}

    _pass(ledger, timing, evm_read=read, evm_chains=("56",), max_seconds=1,
          clock=lambda: clock[0])
    row = _row(timing, "a")
    assert row["state"] == "complete" and row["gap"] == 5 and row["reads"] == 1
    assert calls == [("56", "eth_getTransactionReceipt", [TX56])]


def test_resolver_deadline_never_starts_an_expired_read(dbs):
    ledger, timing = dbs
    _j7_bsc_attempt(ledger)
    calls = []
    _pass(ledger, timing, evm_read=lambda *args: calls.append(args), evm_chains=("56",),
          max_seconds=0, clock=lambda: 0)
    row = _row(timing, "a")
    assert row["state"] == "pending" and row["source_slot"] is None and row["reads"] == 0
    assert calls == []


def test_resolver_deadline_deferral_does_not_starve_the_next_pending_attempt(dbs):
    ledger, timing = dbs
    second_tx = "0x" + "b9" * 32
    _j7_bsc_attempt(ledger, "a")
    _attempt(ledger, "b", network="56", source="follow_activity", lane="j7_wallets",
             origin="swap:raybot:%s:%s:%s" % (second_tx, "0x" + "ef" * 20, MINT56),
             tx="0x" + "46" * 32, trace=_landing_trace(126956875))
    for aid in ("a", "b"):
        timing.execute("INSERT INTO memecoin_fill_timing (attempt_id,state,first_seen_at,updated_at) "
                       "VALUES (?,'pending',?,?)", (aid, _iso(1), _iso(1)))
    timing.commit()
    clock, calls = [0.0], []

    def read(chain, method, params):
        calls.append(params[0])
        clock[0] = 2.0
        raise TimeoutError("this request consumed the pass budget")

    _pass(ledger, timing, evm_read=read, evm_chains=("56",), limit=2, max_seconds=1,
          clock=lambda: clock[0])
    assert (_row(timing, "a")["reads"], _row(timing, "b")["reads"]) == (1, 0)
    clock[0] = 0.0
    _pass(ledger, timing, now=NOW + timedelta(minutes=1), evm_read=read, evm_chains=("56",),
          limit=2, max_seconds=1, clock=lambda: clock[0])
    assert calls == [TX56, second_tx]
    assert (_row(timing, "a")["reads"], _row(timing, "b")["reads"]) == (1, 1)


def test_resolver_bsc_rpc_failure_remains_pending_and_retries(dbs):
    ledger, timing = dbs
    _j7_bsc_attempt(ledger)

    def failed(*args):
        raise TimeoutError("read timed out")

    _pass(ledger, timing, evm_read=failed, evm_chains=("56",))
    row = _row(timing, "a")
    assert row["state"] == "pending" and row["source_slot"] is None and row["reads"] == 1

    def read(chain, method, params):
        if method == "eth_getTransactionReceipt":
            return {"transactionHash": TX56, "blockNumber": "0x7913546", "status": "0x1", "logs": []}
        return {"timestamp": "0x6aaad0bd"}

    _pass(ledger, timing, evm_read=read, evm_chains=("56",))
    row = _row(timing, "a")
    assert row["state"] == "complete" and row["gap"] == 5 and row["reads"] >= 2


@pytest.mark.parametrize("changes", [
    {"transactionHash": "0x" + "ab" * 32}, {"transactionHash": None},
    {"status": "0x0"}, {"status": None}, {"blockNumber": True},
    {"blockNumber": 1.5}, {"blockNumber": "0xno"}, {"logs": None},
    {"logs": [{"removed": True}]},
])
def test_resolver_bsc_source_receipt_needs_successful_exact_identity(dbs, changes):
    ledger, timing = dbs
    _j7_bsc_attempt(ledger)
    receipt = {"transactionHash": TX56, "blockNumber": "0x7913546", "status": "0x1", "logs": []}
    receipt.update(changes)
    _pass(ledger, timing, evm_chains=("56",), evm_read=lambda chain, method, params:
          receipt if method == "eth_getTransactionReceipt" else {"timestamp": "0x6aaad0bd"})
    row = _row(timing, "a")
    assert row["state"] == "pending" and row["source_slot"] is None and row["gap"] is None


def test_resolver_bsc_reverted_own_transaction_still_has_a_landing_block(dbs):
    ledger, timing = dbs
    _j7_bsc_attempt(ledger, trace="{}")

    def read(chain, method, params):
        if method == "eth_getTransactionReceipt":
            source = params[0] == TX56
            return {"transactionHash": params[0], "blockNumber": "0x7913546" if source else "0x791354b",
                    "status": "0x1" if source else "0x0", "logs": []}
        return {"timestamp": "0x6aaad0bd"}

    _pass(ledger, timing, evm_chains=("56",), evm_read=read)
    row = _row(timing, "a")
    assert row["state"] == "complete" and row["gap"] == 5 and row["landing_slot"] == 126956875


@pytest.mark.parametrize("receipt,expected_state", [
    ({"transactionHash": "0x" + "ab" * 32, "blockNumber": "0x791354b", "status": "0x1", "logs": []},
     "pending"),
    (None, "not_landed"),
])
def test_resolver_invalid_own_receipt_is_pending_but_an_old_rpc_null_is_not_landed(dbs, receipt, expected_state):
    ledger, timing = dbs
    trace = json.dumps({"substage_events": [{"name": "evm_source_receipt_observation", "observation": {
        "network_id": "56", "source_tx": TX56, "source_block_number": 126956870}}]})
    _attempt(ledger, "a", network="56", source="follow_activity", lane="j7_wallets", minutes_ago=11,
             origin="swap:raybot:%s:%s:%s" % (TX56, "0x" + "ef" * 20, MINT56),
             tx="0x" + "45" * 32, trace=trace)
    _pass(ledger, timing, evm_chains=("56",), evm_read=lambda *_: receipt)
    row = _row(timing, "a")
    assert row["state"] == expected_state and row["landing_slot"] is None and row["gap"] is None


def test_main_passes_its_max_seconds_budget_to_the_resolver(tmp_path, monkeypatch, dbs):
    got = []
    monkeypatch.setattr(r, "resolve_pass", lambda *args, **kwargs: got.append(kwargs) or {})
    monkeypatch.setattr(r.ft, "open_lanes_readonly", lambda: {})
    r.main(["--ledger", str(tmp_path / "ledger.db"), "--timing", str(tmp_path / "timing.db"),
            "--max-seconds", "0.25"])
    assert 0 <= got[0].get("max_seconds", -1) <= 0.25


def _execution_source(timing, *, mint='MINT', sequence=1, mono=10.0):
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    from sniper.services.memecoin_slot_skips import flush_journal
    journal = ReceiptJournal(producer_run_id='SOURCE', boot_id='B', clock_domain='C')
    for _ in range(sequence):
        journal.record_source(network_id='solana', source_sig=SRC, source_wallet='TRADER',
            mint=mint, decode_state='decoded', recv_monotonic=mono, connection_generation=1,
            config_generation='CONFIG', roster_generation='ROSTER')
    flush_journal(timing, journal.snapshot(), 0)


def _execution_confirmed(ledger, timing, *, receipt_slot=100, corrupt_hash=False):
    import base64
    from solders.keypair import Keypair
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from sniper.execution.submission_transport import sha256_hex
    kp = Keypair()
    tx = VersionedTransaction(MessageV0.try_compile(kp.pubkey(), [], [], Hash.default()), [kp])
    signed = base64.b64encode(bytes(tx)).decode('ascii')
    sig = str(tx.signatures[0])
    proof = dict(state='verified', network_id='solana', custody_pubkey=str(kp.pubkey()),
        mint='MINT', tx_sig=sig, receipt_slot=receipt_slot, payment_atomic='20000000',
        acquired_atomic='9007199254740993', payment_asset='usdc')
    trace = dict(source_evidence=dict(signature=SRC, producer_run_id='SOURCE', recv_monotonic=10.0, boot_id='B', clock_domain='C', source_initiation=dict(payer='TRADER', mint='MINT')),
        submission_events=[dict(stage='chain_confirmation', landing_slot=100)],
        substage_events=[dict(name='receipt_execution_evidence', evidence=proof)])
    m.start_entry_attempt(ledger, attempt_id='a', decision_started_at=_iso(),
        origin_event_id='slotlane:' + SRC, network_id='solana', mint='MINT', source_user_id='u')
    pid = m.reserve_pending(ledger, mint='MINT', source_user_id='u', strategy='alpha_b',
        notional=20.0, reserved_at=_iso(), network_id='solana', attempt_id='a')
    m.bind_entry_submission(ledger, pid, tx_identity=sig, signed_payload_b64=signed,
        signed_payload_sha256='wrong' if corrupt_hash else sha256_hex(signed), blockhash=str(Hash.default()),
        last_valid_height=100, nonce=None, backend='rpc', bound_at=_iso())
    position, fill = m.finalize_pending_entry(ledger, pending_id=pid, position_kwargs=dict(
        source_user_id='u', source_handle='trader', mint='MINT', ticker='M', entry_price=1.,
        entry_notional=20., entry_fill_source='chain', entry_lag_s=0., their_size_usd=20.,
        opened_at=_iso(), execution_mode='real', network_id='solana', attempt_id='a'),
        fill_kwargs=dict(kind='entry', price=1., qty=20., notional=20., fill_source='chain',
            fraction=1., created_at=_iso(), tx_sig=sig, attempt_id='a', network_id='solana'))
    m.finish_entry_attempt(ledger, 'a', 'filled', 'filled', _iso(), json.dumps(trace),
        pending_id=pid, position_id=position, fill_id=fill, tx_sig=sig)
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    from sniper.services.memecoin_slot_skips import flush_journal
    journal = ReceiptJournal(producer_run_id='OBSERVER', boot_id='B', clock_domain='C')
    for mono in (10.190, 10.5):
        journal.record_processed(network_id='solana', our_sig=sig, slot=100, error_state='success',
            recv_monotonic=mono, connection_generation=1)
    flush_journal(timing, journal.snapshot(), 0)
    return sig


def test_execution_orphan_late_exact_confirmed_join_without_rpc(dbs):
    ledger, timing = dbs
    _execution_source(timing)
    first = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert first['unknown'] == 1 and first['verified_sub200'] == 0
    _execution_confirmed(ledger, timing)
    statements = []
    ledger.set_trace_callback(statements.append)
    result = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert result['verified_sub200'] == 1 and result['eligible'] == 1
    assert result['p50_ms'] == pytest.approx(190) and result['p90_ms'] == pytest.approx(190)
    assert result['results'][0]['canonical_execution_time'] == 'unverified'
    assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in statements)
    assert timing.execute('SELECT count(*) FROM memecoin_processed_receipts').fetchone()[0] == 2


@pytest.mark.parametrize('change', ['hash', 'slot', 'exact_case', 'fill_position', 'missing_atoms'])
def test_execution_resolver_never_infers_identity_from_real_fill_floats(dbs, change):
    ledger, timing = dbs
    _execution_source(timing)
    _execution_confirmed(ledger, timing, receipt_slot=101 if change == 'slot' else 100,
        corrupt_hash=change == 'hash')
    if change == 'exact_case':
        ledger.execute('UPDATE memecoin_fills SET tx_sig=lower(tx_sig)')
    if change == 'fill_position':
        ledger.execute('UPDATE memecoin_fills SET position_id=999')
    if change == 'missing_atoms':
        ledger.execute("UPDATE memecoin_entry_attempts SET trace_json='{}'")
    ledger.commit()
    result = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert result['verified_sub200'] == 0 and result['unknown'] == 1


def test_execution_duplicate_sources_do_not_shrink_denominator_or_first_receipt(dbs):
    ledger, timing = dbs
    _execution_source(timing, sequence=3)
    _execution_confirmed(ledger, timing)
    result = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert result['received_events'] == 1 and result['eligible'] == 1
    assert result['verified_sub200'] == 1 and result['p90_ms'] == pytest.approx(190)
    assert result['source_runs'][0]['config_generation'] == 'CONFIG'


def test_execution_resolver_bounded_pages_retry_old_unattempted_unknown(dbs):
    ledger, timing = dbs
    _execution_source(timing, mint=None, sequence=5)
    one = r.resolve_execution_pass(ledger, timing, max_rows=2, now=NOW)
    assert one['scanned'] <= 2 and one['unknown_eligibility'] >= 1
    _execution_confirmed(ledger, timing)
    two = r.resolve_execution_pass(ledger, timing, max_rows=2, now=NOW)
    assert two['scanned'] <= 2 and two['verified_sub200'] == 0 and two['unknown_eligibility'] == 2


def test_execution_resolver_zero_budget_does_no_join(dbs):
    ledger, timing = dbs
    _execution_source(timing)
    statements = []
    ledger.set_trace_callback(statements.append)
    result = r.resolve_execution_pass(ledger, timing, max_seconds=0)
    assert result['scanned'] == 0 and statements == []


def test_unresolved_native_decode_never_chooses_only_attempt_mint(dbs):
    ledger, timing = dbs
    _execution_source(timing, mint=None, sequence=3)
    _execution_confirmed(ledger, timing)
    result = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert result['verified_sub200'] == 0 and result['unknown_eligibility'] == 3
    assert result['received_events'] == 3


@pytest.mark.parametrize('trace', [{'source_evidence': [1]}, {'source_evidence': {'source_initiation': [1]}},
    {'source_evidence': {'source_wallet': 'TRADER'}, 'submission_events': ['bad']}])
def test_execution_malformed_nested_trace_retains_unknown(dbs, trace):
    ledger, timing = dbs
    _execution_source(timing)
    _attempt(ledger, 'a', origin='slotlane:' + SRC, trace=json.dumps(trace))
    result = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert result['unknown'] == 1 and result['verified_sub200'] == 0


def test_execution_late_page_keeps_original_source_provenance(dbs):
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    from sniper.services.memecoin_slot_skips import flush_journal
    ledger, timing = dbs
    _execution_source(timing)
    late = ReceiptJournal(producer_run_id='LATE', boot_id='B', clock_domain='C')
    late.record_source(network_id='solana', source_sig=SRC, source_wallet='TRADER', mint='MINT',
        decode_state='duplicate_buy', recv_monotonic=10.050, connection_generation=1,
        config_generation='LATE_CONFIG')
    flush_journal(timing, late.snapshot(), 0)
    timing.execute('INSERT INTO memecoin_fill_timing_cursor VALUES (?,?)',
        ('execution_source_receipts_v1', json.dumps(dict(cursor=1, retry=[]))))
    timing.commit()
    _execution_confirmed(ledger, timing)
    result = r.resolve_execution_pass(ledger, timing, max_rows=1)
    assert result['p90_ms'] == pytest.approx(190)
    assert result['results'][0]['config_generation'] == 'CONFIG'
    assert result['results'][0]['producer_run_id'] == 'SOURCE'


@pytest.mark.parametrize('value', [[], None, False, 1, 'diagnostic'])
def test_execution_nonobject_skip_json_retains_unknown(dbs, value):
    ledger, timing = dbs
    _execution_source(timing)
    timing.execute('ALTER TABLE memecoin_slot_lane_skips ADD COLUMN evidence_json TEXT')
    timing.execute('INSERT INTO memecoin_slot_lane_skips '
        '(signature,outcome,reason,noted_at,evidence_json) VALUES (?,?,?,?,?)',
        (SRC, 'handoff', 'pool_unknown', _iso(), json.dumps(value)))
    timing.commit()
    result = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert result['unknown'] == 1 and result['unknown_eligibility'] == 1
    assert result['results'][0]['state'] == 'unknown'
    assert result['results'][0]['eligibility'] == 'unknown'


def test_a_rabbit_pre_execution_stamp_is_never_a_source_receipt(dbs):
    """MEMECOIN_SLOT_LANE_RABBIT_SIGNAL stamps owner-signed RabbitStream frames before execution in
    the same journal. One earlier than the processed receipt must not become the first receipt."""
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    from sniper.services.memecoin_slot_skips import flush_journal
    ledger, timing = dbs
    rabbit = ReceiptJournal(producer_run_id='RABBIT', boot_id='B', clock_domain='C')
    rabbit.record_source(network_id='solana', source_sig=SRC, source_wallet='TRADER', mint='MINT',
        decode_state='rabbit_pre_execution', recv_monotonic=9.9, connection_generation=1,
        config_generation='CONFIG', roster_generation='ROSTER', feed='rabbit:rabbitstream.fra.shyft.to')
    flush_journal(timing, rabbit.snapshot(), 0)
    alone = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert alone['scanned'] == 0 and alone['received_events'] == 0
    _execution_source(timing)
    _execution_confirmed(ledger, timing)
    result = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert result['verified_sub200'] == 1 and result['p50_ms'] == pytest.approx(190)


def test_a_late_fill_behind_hundreds_of_orphans_is_still_revisited(dbs):
    """Gamma MEDIUM on 18c83d208: decoded buys with no attempt ('unattempted', ~470-600/day) must
    not fill the 512-row retry list and push a real late join off its tail."""
    from sniper.services.memecoin_feed_arrivals import ReceiptJournal
    from sniper.services.memecoin_slot_skips import flush_journal
    ledger, timing = dbs
    journal = ReceiptJournal(producer_run_id='ORPHANS', boot_id='B', clock_domain='C')
    for i in range(600):
        journal.record_source(network_id='solana', source_sig=f'orphan{i}', source_wallet='NOBODY',
            mint='OM', decode_state='decoded', recv_monotonic=1.0, connection_generation=1,
            config_generation='CONFIG', roster_generation='ROSTER')
    flush_journal(timing, journal.snapshot(), 0)
    _execution_source(timing)                     # the real buy, behind 600 orphans
    for _ in range(3):
        r.resolve_execution_pass(ledger, timing, max_rows=512, now=NOW)
    _execution_confirmed(ledger, timing)          # its fill lands late
    verified = [r.resolve_execution_pass(ledger, timing, max_rows=512, now=NOW)['verified_sub200']
                for _ in range(6)]
    assert sum(verified) == 1, verified


def test_execution_receipt_with_candidates_is_never_dropped_until_its_join_completes(dbs):
    """Owed pin (Gamma, 2026-10-21): only an 'unattempted' receipt (no candidate attempt) leaves the
    retry list after one retry. A receipt that has a candidate whose join is still incomplete is
    retained on every pass, and its late join completes on pass >= 3."""
    from sniper.execution.submission_transport import sha256_hex
    ledger, timing = dbs
    _execution_source(timing)
    _execution_confirmed(ledger, timing, corrupt_hash=True)   # a candidate whose join cannot complete yet
    rowid = timing.execute('SELECT MAX(rowid) FROM memecoin_signal_receipts').fetchone()[0]
    for _ in range(2):
        out = r.resolve_execution_pass(ledger, timing, now=NOW)
        assert out['unknown'] == 1 and out['verified_sub200'] == 0
        state = json.loads(timing.execute("SELECT value FROM memecoin_fill_timing_cursor WHERE name="
                                          "'execution_source_receipts_v1'").fetchone()[0])
        assert rowid in state['retry']
    payload = ledger.execute('SELECT signed_payload_b64 FROM memecoin_pending_entries').fetchone()[0]
    ledger.execute('UPDATE memecoin_pending_entries SET signed_payload_sha256=?', (sha256_hex(payload),))
    ledger.commit()
    third = r.resolve_execution_pass(ledger, timing, now=NOW)
    assert third['verified_sub200'] == 1 and third['p50_ms'] == pytest.approx(190)
