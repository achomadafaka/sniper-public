"""The overnight page says what fired and drops only noise (2026-09-28, operator: "Fix these messages").

The 04:45Z page was headed "ACTIONABLE refusals" while it fired on a failed transaction, the 24h net
and tracebacks. Five lines were noise:
- the one-path guard, counted as a SYSTEM refusal;
- a failed-transaction row reading "None" for the ticker, which paged on a min-out revert, i.e. the
  slippage limit doing its job;
- the 24h net, re-paging every 15 minutes while it stayed under -$150;
- an OBSERVER header naming pages the code never sends;
- 60 traceback rows, each the first stack frame rather than the exception. They were one handled
  provider error (tickstream getLogs HTTP failure, logged with its stack), and they paged every run.
Every real fault still pages. A stuck reservation pages through the entry breaker; any other failed
transaction pages; the 24h net pages when it crosses and each further -$50; a traceback KIND pages
the first time in 24 h.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "overnight_watch_messages", ROOT / "scripts" / "memecoin_overnight_watch.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)

TRADED = {"4663", "8453", "solana", "56"}
TB = (
    'tickstream 8453: getLogs (51892452..51892457) failed (tick skipped)\n'
    'Traceback (most recent call last):\n'
    '  File "/x/.runtime/memecoin/releases/abc/sniper/services/memecoin_tick_stream.py", line 310, '
    'in evm_tick_pass\n'
    '    logs = rpc_call("eth_getLogs", [params])\n'
    '  File "/x/.runtime/memecoin/releases/abc/sniper/execution/evm_wallet.py", line 542, in _rpc_impl\n'
    '    raise RuntimeError("EVM RPC provider HTTP failure")\n'
    'RuntimeError: EVM RPC provider HTTP failure\n'
    '2026-09-28T05:44:22.572253+00:00 [warn] tickstream 8453: retry backoff 12s\n')


# --- the one-path guard is not a fault -------------------------------------------------------------

@pytest.mark.parametrize("reason", ["unresolved signed transaction on solana",
                                    "unresolved signed transaction on 4663"])
def test_the_one_path_guard_is_not_a_system_refusal(reason):
    """A buy refused because our own transaction for it is in flight. A reservation that STAYS
    stuck pages through the entry breaker, on duration."""
    assert W.classify(reason, "solana", TRADED) == "POLICY"


def test_a_real_fault_still_classifies_system():
    assert W.classify("no quote (provider_rpc_transport: timeout)", "solana", TRADED) == "SYSTEM"


# --- failed transactions --------------------------------------------------------------------------

def _attempts(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_entry_attempts(decision_started_at TEXT, ticker TEXT, mint TEXT, "
              "source_handle TEXT, network_id TEXT, tx_sig TEXT, terminal_status TEXT, "
              "terminal_reason TEXT)")
    c.executemany("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?,?)", rows)
    c.execute("CREATE TABLE memecoin_positions(closed_at TEXT, ticker TEXT, network_id TEXT, "
              "close_sig TEXT, exit_reason TEXT, execution_mode TEXT)")
    return c


MIN_OUT = ("2026-09-28T04:38:30", None, "ARPwPPWbajXYZpump", "ResellCalendar", "solana",
           "4pUSKdeGXhyHFzgfTyNvA66VKwD", "failed", "chain_error:BuySlippageBelowMinBaseAmountOut")
REAL = ("2026-09-28T04:40:00", "FOO", "0xabc", "someone", "4663", "0xdead", "failed",
        "chain_error:execution reverted: TRANSFER_FROM_FAILED")


def test_a_failed_row_names_the_coin_and_source_never_none():
    rows, warn = W.failed_transactions(_attempts([MIN_OUT]), "2026-09-28T04:00:00")
    assert warn is None and len(rows) == 1
    line = W.failed_line(rows[0])
    assert "None" not in line and "ARPwPPWb" in line and "ResellCalendar" in line


def test_a_min_out_revert_is_listed_but_does_not_page():
    rows, _ = W.failed_transactions(_attempts([MIN_OUT, REAL]), "2026-09-28T04:00:00")
    assert [W.is_min_out(r) for r in rows] == [True, False]
    assert W.failed_pages(rows) == [
        "1 failed transaction(s) with gas spent (not a min-out revert) · "
        "4663 entry FOO: chain_error:execution reverted: TRANSFER_FROM_FAILED"]
    assert W.failed_pages(rows[:1]) == []


@pytest.mark.parametrize("reason", [
    "chain_error:BuySlippageBelowMinBaseAmountOut", "reverted_min_out: V4TooLittleReceived",
    "chain_error:TooLittleReceived", "execution reverted: UniswapV2Router: INSUFFICIENT_OUTPUT_AMOUNT",
    "chain_error:SellSlippageBelowMinQuoteAmountOut", "exceeds desired slippage limit"])
def test_every_min_out_shape_is_recognised(reason):
    assert W.is_min_out(("entry", "t", "x", "solana", "sig", reason, "h"))


@pytest.mark.parametrize("reason", ["chain_error:InsufficientFunds", "execution reverted",
                                    "chain_error:AccountNotInitialized", ""])
def test_any_other_failure_still_pages(reason):
    assert not W.is_min_out(("entry", "t", "x", "solana", "sig", reason, "h"))


# --- the 24h net ----------------------------------------------------------------------------------

def test_the_24h_net_pages_on_the_crossing_and_each_further_50_dollars(tmp_path):
    state = tmp_path / "s.json"
    assert W.pnl24_pages(-120.0, str(state)) == []
    assert W.pnl24_pages(-173.92, str(state)) == ["24h net $-173.92 below -$150"]
    assert W.pnl24_pages(-178.0, str(state)) == [], "the same fact does not page every 15 minutes"
    assert W.pnl24_pages(-199.0, str(state)) == []
    assert W.pnl24_pages(-224.5, str(state)) == ["24h net $-224.50 below -$150 (down $50.58 since "
                                                 "the last page)"]
    assert W.pnl24_pages(-140.0, str(state)) == [], "back above the floor clears the latch"
    assert W.pnl24_pages(-151.0, str(state)) == ["24h net $-151.00 below -$150"]


def test_the_pnl_latch_keeps_the_other_keys_in_the_shared_state(tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"loss_streaks": {"solana": {"start": "x", "len": 3}}, "/a.log": 9}))
    W.pnl24_pages(-160.0, str(state))
    saved = json.loads(state.read_text())
    assert saved["loss_streaks"] and saved["/a.log"] == 9 and saved["pnl24"]


# --- tracebacks -----------------------------------------------------------------------------------

def test_a_traceback_reports_its_exception_and_frames_not_the_first_line(tmp_path):
    log = tmp_path / "memecoin_runner.log"
    log.write_text(TB * 3)
    found, errs = W.new_tracebacks([str(log)], str(tmp_path / "s.json"))
    assert not errs and len(found) == 3
    name, summary, kind = found[0]
    assert summary == ("RuntimeError: EVM RPC provider HTTP failure -- memecoin_tick_stream.py:310 "
                       "evm_tick_pass -> evm_wallet.py:542 _rpc_impl")
    assert ".runtime" not in summary and len({k for _n, _s, k in found}) == 1


def test_identical_tracebacks_group_into_one_counted_row(tmp_path):
    log = tmp_path / "memecoin_runner.log"
    log.write_text(TB * 60)
    found, _ = W.new_tracebacks([str(log)], str(tmp_path / "s.json"))
    assert W.traceback_rows(found) == [
        "     60x memecoin_runner.log: RuntimeError: EVM RPC provider HTTP failure -- "
        "memecoin_tick_stream.py:310 evm_tick_pass -> evm_wallet.py:542 _rpc_impl"]


def test_a_traceback_kind_pages_once_per_24h(tmp_path):
    state = tmp_path / "s.json"
    log = tmp_path / "memecoin_runner.log"
    log.write_text(TB)
    found, _ = W.new_tracebacks([str(log)], str(state))
    assert W.traceback_pages(found, str(state), now_s=1000.0) == ["1 new traceback kind(s): "
                                                                   "RuntimeError: EVM RPC provider HTTP failure"]
    assert W.traceback_pages(found, str(state), now_s=2000.0) == [], "a known kind does not re-page"
    assert W.traceback_pages(found, str(state), now_s=1000.0 + 86400 + 1) != [], "once a day"
    other = [("memecoin_runner.log", "ValueError: boom -- a.py:1 f", "ValueError|a.py:1|a.py:1")]
    assert W.traceback_pages(other, str(state), now_s=2000.0) == [
        "1 new traceback kind(s): ValueError: boom"], "a NEW kind pages at once"


def test_a_traceback_cut_mid_block_is_still_reported(tmp_path):
    log = tmp_path / "memecoin_runner.log"
    log.write_text('Traceback (most recent call last):\n  File "/a/b/c.py", line 9, in g\n')
    found, _ = W.new_tracebacks([str(log)], str(tmp_path / "s.json"))
    assert len(found) == 1 and "c.py:9 g" in found[0][1]


# --- headers --------------------------------------------------------------------------------------

def test_the_observer_header_names_only_the_page_it_sends():
    src = (ROOT / "scripts" / "memecoin_overnight_watch.py").read_text()
    assert "resets >6 per 15m" not in src and "no pricer pass in 5m" not in src
    assert "OBSERVER (pages on feed dark >120s)" in src


def test_the_page_title_names_what_fired():
    assert W.page_title(["1 failed transaction(s) with gas spent (not a min-out revert)",
                         "2 SYSTEM refusal(s)"], since_minutes=20) == (
        "🟠 Memecoin watch · last 20m\n"
        "• Failed transactions: 1; gas spent\n"
        "• System refusals: 2\n"
        "Details: logs/memecoin_overnight_watch.log")
    assert "refusals" not in W.page_title(["24h net $-173.92 below -$150"], since_minutes=20)
    assert W.page_title([], since_minutes=20) == ""


def test_the_reported_401_is_a_short_error_alert():
    message = W.page_title([
        "1 new traceback kind(s): urllib.error.HTTPError: HTTP Error 401: Unauthorized"
    ], since_minutes=20)
    assert message == ("🟠 Memecoin watch · last 20m\n"
                       "• New error: HTTP 401: Unauthorized\n"
                       "Details: logs/memecoin_overnight_watch.log")


def test_a_fault_burst_is_bounded_and_points_to_the_full_report():
    message = W.page_title([f"fault {i}\n" + "x" * 1000 for i in range(50)], since_minutes=20)
    assert len(message.splitlines()) == 6 and len(message) < 800
    assert "fault 0" in message and "fault 2" in message
    assert "…" in message and "+47 more alerts" in message
    assert message.endswith("Details: logs/memecoin_overnight_watch.log")


@pytest.mark.parametrize("page,fault", [(True, "health"), (True, "refusal"),
                                       (True, None), (False, "health")])
def test_telegram_receives_only_faults_while_stdout_keeps_diagnostics(
        tmp_path, monkeypatch, capsys, page, fault):
    db = tmp_path / "watch.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE memecoin_positions(network_id TEXT, entry_notional REAL,
            execution_mode TEXT, opened_at TEXT, is_open INTEGER, closed_at TEXT,
            realized_pnl REAL, attempt_id TEXT);
        CREATE TABLE memecoin_entry_attempts(network_id TEXT, terminal_reason TEXT,
            observed_at TEXT);
        CREATE TABLE memecoin_pending_entries(network_id TEXT, mint TEXT, status TEXT,
            tx_sig TEXT, cleanup_tx_sig TEXT, signed_payload_sha256 TEXT, broadcast_at TEXT,
            attempt_id TEXT, reserved_at TEXT);
    """)
    reason = "quote balance unavailable (http_429)"
    if fault == "refusal":
        c.execute("INSERT INTO memecoin_entry_attempts VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%S','now'))",
                  ("8453", reason))
        c.commit()
    c.close()
    monkeypatch.setattr(W, "ROOT", tmp_path)
    monkeypatch.setattr(W, "STATE_PATH", tmp_path / "watch.state")
    monkeypatch.setattr(W, "_traded_chains", lambda: TRADED)
    monkeypatch.setattr(W, "runner_health", lambda *a, **k:
                        ["HTTP 401: Unauthorized"] if fault == "health" else [])
    monkeypatch.setattr(W, "failed_transactions", lambda *a: ([], None))
    monkeypatch.setattr(W, "observer_health", lambda *a, **k: ({}, None, None, None))
    monkeypatch.setattr(W, "unresolved_pools", lambda *a: ({}, None))
    monkeypatch.setattr(W, "new_tracebacks", lambda *a: ([], []))
    monkeypatch.setattr("sniper.util.envfiles.load_env_files", lambda *a: None)
    sent = []
    monkeypatch.setattr("sniper.services.memecoin_telegram.send_telegram", sent.append)
    monkeypatch.setattr("sys.argv", ["watch", "--db", str(db), "--since-minutes", "20"]
                        + (["--page"] if page else []))

    assert W.main() == 0
    output = capsys.readouterr().out
    assert "POLICY refusals: 0" in output and "OBSERVER" in output and "OUTCOMES" in output
    if page and fault:
        assert len(sent[0].splitlines()) == 3
        trigger = ("HTTP 401: Unauthorized" if fault == "health" else
                   f"1 SYSTEM refusal(s) · 8453: {reason}")
        assert sent == [W.page_title([trigger], since_minutes=20)]
        if fault == "refusal":
            assert f"System refusals: 1 · 8453: {reason}" in sent[0]
    else:
        assert sent == []


def test_the_page_names_each_exception_once(tmp_path):
    found = [("r.log", "TimeoutError: The read operation timed out -- a.py:1 f", "TimeoutError|a.py:1|x"),
             ("r.log", "TimeoutError: The read operation timed out -- b.py:2 g", "TimeoutError|b.py:2|x"),
             ("r.log", "OSError: x -- c.py:3 h", "OSError|c.py:3|x"),
             ("r.log", "KeyError: 'k' -- d.py:4 i", "KeyError|d.py:4|x"),
             ("r.log", "ValueError: v -- e.py:5 j", "ValueError|e.py:5|x")]
    assert W.traceback_pages(found, str(tmp_path / "s.json"), now_s=1.0) == [
        "5 new traceback kind(s): TimeoutError: The read operation timed out; OSError: x; "
        "KeyError: 'k' (+1 more)"]
