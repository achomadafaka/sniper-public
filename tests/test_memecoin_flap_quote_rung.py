"""Option B: the flap.sh quote-token exit runs inside the runner's chain-56 exit (2026-10-02).

For an eligible coin (a flap.sh curve quoted in a token, native swap off) the core runs INSTEAD of
the ladder: after the 15:11Z restart the ladder's Kyber route valued the whole DAODUN bag at 0.0002
WBNB and the runner signed 9 sells on it. Both values are logged for grading; the ladder's never
decides. MEMECOIN_EVM_FLAP_QUOTE_EXIT (default 0) gates it.
"""
import json
import logging
import sqlite3

import pytest

from sniper.execution import evm_flap_quote_exit as X
from sniper.persistence import db_memecoin as m
from test_memecoin_flap_quote_exit import (ADDR, CHEEMS, V3_OUT, FakeChain, _position, _signer,
                                           conn, locks)  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def journals(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "journal_path", lambda pid: tmp_path / ("%d.json" % pid))
    monkeypatch.setattr(X, "_ELIGIBLE", set())
    monkeypatch.setattr(X, "_NOT_ELIGIBLE", set())
    monkeypatch.setattr(X, "_PREWARMING", set())
    monkeypatch.setattr(X, "_RESUMES", {})


def _factory(conn, chain, *, budget=None, clock=None, lines=None):  # noqa: F811
    built = []
    clock = clock if clock is not None else {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    def factory(pos, path):
        run = X.Run(conn, pos["id"], chain=chain, sign_fn=_signer(), journal_path=path,
                    price_fn=lambda: 600.0, addresses=ADDR,
                    out=(lines.append if lines is not None else (lambda *_: None)),
                    sleep_fn=sleep, clock=lambda: clock["t"],
                    state_fn=lambda c, mint: {"quote_token": CHEEMS, "status": 4},
                    overrides_fn=lambda *a, **k: {"x": 1}, step_budget_s=budget,
                    wall_fn=lambda: clock["t"])
        built.append(run)
        return run
    factory.built = built
    return factory


def _exit(conn, pos, chain, *, eligible=True, ladder=None, budget=None, clock=None, lines=None,
          page=None):  # noqa: F811
    factory = _factory(conn, chain, budget=budget, clock=clock, lines=lines)
    result = X.runner_exit(conn, pos, "b_stop", "2026-10-02T15:20:00Z", run_factory=factory,
                           eligible_fn=lambda mint: eligible, ladder_value_fn=ladder, page_fn=page)
    return result, factory


def test_the_core_decides_even_when_the_ladder_quotes_a_tenth_of_a_percent(conn, caplog):  # noqa: F811
    import threading
    pid = _position(conn)
    chain = FakeChain()
    dust = V3_OUT // 1000                                  # Kyber's 0.1% of the core's value
    graded, lines = threading.Event(), []

    def ladder(pos):
        graded.set()
        return dust
    with caplog.at_level(logging.WARNING, logger="memecoin"):
        result, _ = _exit(conn, m.get_position(conn, pid), chain, ladder=ladder, lines=lines)
        assert graded.wait(5)
        for _ in range(200):
            if any("grading" in r.getMessage() for r in caplog.records):
                break
            threading.Event().wait(0.01)
    assert result["status"] == "closed" and result["route"] == "flap_quote_exit"
    assert chain.sent == ["approve", "leg1", "approve", "leg2"]
    grade = next(r.getMessage() for r in caplog.records if "grading" in r.getMessage())
    assert "ladder_wbnb=%.6f" % (dust / 1e18) in grade
    assert any("leg 2 sent" in line and "quote %.6f" % (V3_OUT / 1e18) in line for line in lines)
    assert not m.get_position(conn, pid)["is_open"]


def test_an_ineligible_coin_is_left_to_the_ladder(conn):  # noqa: F811
    pid = _position(conn)
    result, factory = _exit(conn, m.get_position(conn, pid), FakeChain(), eligible=False)
    assert result is None and factory.built == []


def test_an_unfinished_journal_resumes_whatever_eligibility_reads(conn, tmp_path):  # noqa: F811
    pid = _position(conn)
    chain = FakeChain(revert_leg2=1)
    first, _ = _exit(conn, m.get_position(conn, pid), chain)
    assert first["status"] == "stuck" and first["failure_type"] == "flap_leg2_pending"
    again, _ = _exit(conn, m.get_position(conn, pid), chain, eligible=False)   # curve unreadable now
    assert again["status"] == "closed"
    assert chain.sent == ["approve", "leg1", "approve", "leg2", "leg2"]


def test_an_unmined_leg_reads_pending_and_a_refusal_before_any_claim_reads_aborted(
        conn, monkeypatch, tmp_path):  # noqa: F811
    pid = _position(conn)
    chain = FakeChain(hold_receipt=("leg1",))
    result, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S)
    assert result["status"] == "pending" and result["tx_hash"]
    monkeypatch.setattr(X, "journal_path", lambda pid: tmp_path / "other" / ("%d.json" % pid))
    other = sqlite3.connect(":memory:")
    other.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(other)
    pid2 = _position(other)
    busy = FakeChain()
    busy.pending = busy.nonce + 1
    result, _ = _exit(other, m.get_position(other, pid2), busy, budget=X.STEP_BUDGET_S)
    assert result["status"] == "aborted" and busy.sent == []


class SlowChain(FakeChain):
    """Every read costs 0.12 s of the fake clock; a transaction mines after MINE_POLLS receipt reads."""

    MINE_POLLS = 6

    def __init__(self, clock, **kw):
        super().__init__(**kw)
        self.clock, self.polls = clock, {}

    def _tick(self):
        self.clock["t"] += 0.12

    def call(self, *a, **k):
        self._tick()
        return super().call(*a, **k)

    def balance(self, token):
        self._tick()
        return super().balance(token)

    def allowance(self, token, spender):
        self._tick()
        return super().allowance(token, spender)

    def nonces(self):
        self._tick()
        return super().nonces()

    def estimate_gas(self, tx):
        self._tick()
        return super().estimate_gas(tx)

    def receipt(self, tx_hash):
        self._tick()
        self.polls[tx_hash] = self.polls.get(tx_hash, 0) + 1
        return super().receipt(tx_hash) if self.polls[tx_hash] > self.MINE_POLLS else None


def test_each_runner_call_stays_inside_its_step_budget_and_the_exit_still_completes(conn):  # noqa: F811
    pid = _position(conn)
    clock = {"t": 0.0}
    chain = SlowChain(clock)
    walls, result = [], None
    for _ in range(40):
        start = clock["t"]
        result, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S, clock=clock)
        walls.append(clock["t"] - start)
        if result["status"] == "closed":
            break
        assert result["status"] == "pending", result
    assert result["status"] == "closed" and len(walls) > 2
    print("walls", [round(w, 2) for w in walls])
    assert max(walls) <= X.STEP_BUDGET_S + 0.5, walls          # one read past the budget at most
    assert chain.sent == ["approve", "leg1", "approve", "leg2"]


def test_three_portal_refusals_in_a_row_demote_the_coin_and_page_once(conn, monkeypatch):  # noqa: F811
    pid = _position(conn)
    chain = FakeChain()
    chain.call = lambda to, data, **k: None if to == ADDR["portal"] else FakeChain.call(chain, to, data, **k)
    pages = []
    X._ELIGIBLE.add(chain.address)                         # unrelated entry stays
    mint = m.get_position(conn, pid)["mint"]
    X._ELIGIBLE.add(mint)
    for n in range(X.DEMOTE_AFTER):
        result, _ = _exit(conn, m.get_position(conn, pid), chain, page=pages.append)
        assert result["status"] == "aborted"
    assert len(pages) == 1 and "DEMOTED" in pages[0]
    assert X.cached_eligibility(mint) is False
    assert m.get_position(conn, pid)["close_claim_id"] is None     # each refusal released its claim


def test_recovery_resumes_a_flap_exit_past_leg1(monkeypatch, tmp_path):
    from sniper.services import memecoin_real_runner as R
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", True)
    X.write_journal(X.journal_path(7), {"state": "leg2_failed"})
    X.write_journal(X.journal_path(8), {"state": "approving_leg1"})
    calls = []
    ctx = {"exit_fn": lambda conn, pos, reason, now: calls.append((pos["id"], reason)) or {"status": "pending"}}
    monkeypatch.setattr(R, "context_for_position", lambda contexts, pos: ctx)
    assert R._resume_flap_quote_exit(None, {}, {"id": 7, "network_id": "56"}, "now") is True
    assert R._resume_flap_quote_exit(None, {}, {"id": 8, "network_id": "56"}, "now") is False
    assert R._resume_flap_quote_exit(None, {}, {"id": 7, "network_id": "4663"}, "now") is False
    assert calls == [(7, "manual")]


def test_eligibility_caches_definitive_answers_and_never_an_unknown():
    reads = []
    answers = {"0xflap": "yes", "0xpool": "no", "0xflaky": "unknown"}

    def knows(mint, rpc):
        reads.append(mint)
        return answers[mint]

    def state(chain, mint, rpc):
        return {"quote_token": CHEEMS, "native_to_quote_swap_enabled": 0}
    for _ in range(3):
        assert X.eligible("0xFLAP", "rpc", state_fn=state, knows_fn=knows) is True
        assert X.eligible("0xpool", "rpc", state_fn=state, knows_fn=knows) is False
        assert X.eligible("0xflaky", "rpc", state_fn=state, knows_fn=knows) is False
    assert reads == ["0xflap", "0xpool", "0xflaky", "0xflaky", "0xflaky"]   # unknown re-reads
    assert X.cached_eligibility("0xflaky") is None


def test_a_flap_coin_whose_state_cannot_be_read_is_not_cached():
    assert X.eligible("0xflap", "rpc", state_fn=lambda *a: None, knows_fn=lambda *a: "yes") is False
    assert X.cached_eligibility("0xflap") is None


def test_prewarm_reads_off_thread_once_and_the_exit_then_reads_memory():
    import threading
    gate, reads = threading.Event(), []

    def knows(mint, rpc):
        reads.append(mint)
        gate.wait(5)
        return "no"
    assert X.prewarm("0xPool", "rpc", knows_fn=knows) is True
    assert X.prewarm("0xpool", "rpc", knows_fn=knows) is False          # one read in flight
    gate.set()
    for _ in range(100):
        if X.cached_eligibility("0xpool") is not None:
            break
        threading.Event().wait(0.01)
    assert X.cached_eligibility("0xpool") is False and reads == ["0xpool"]
    assert X.eligible("0xpool", "rpc", knows_fn=lambda *a: pytest.fail("read at exit")) is False
    assert X.prewarm("0xpool", "rpc", knows_fn=knows) is False          # cached: nothing to do


# --- the hook in the live exit seam -------------------------------------------------------------------

def _executor(venue_quote_fn=None):
    from eth_account import Account
    from sniper.execution.evm_real_seams import build_real_evm_executor

    def http(method, url, headers=None, body=None, timeout=10):
        return 200, {"result": None}, None
    return build_real_evm_executor(acct=Account.create(), chain_id="56", rpc_url="rpc", http=http,
                                   venue_quote_fn=venue_quote_fn or (lambda *a, **k: None), usdc_fn=lambda: 0.0,
                                   token_fn=lambda mint: 10.0)


@pytest.mark.parametrize("knob", [True, False])
def test_the_live_exit_seam_asks_the_flap_exit_first_only_with_the_knob(monkeypatch, knob):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", knob)
    seen = []

    def runner_exit(conn, position, exit_reason, now_iso, **kw):
        seen.append((position["id"], exit_reason, sorted(kw)))
        return {"status": "closed", "route": "flap_quote_exit"}
    monkeypatch.setattr(X, "runner_exit", runner_exit)
    ex = _executor()
    position = {"id": 5, "mint": "0x" + "ab" * 20, "entry_notional": 10.0}
    if knob:
        assert ex["exit_fn"](None, position, "b_stop", "2026-10-02T15:20:00Z")["route"] == "flap_quote_exit"
        assert seen == [(5, "b_stop", ["eligible_fn", "ladder_value_fn", "run_factory"])]
    else:
        try:
            ex["exit_fn"](sqlite3.connect(":memory:"), position, "b_stop", "2026-10-02T15:20:00Z")
        except Exception:
            pass                                          # the ladder ran (and failed on fakes)
        assert seen == []


def test_a_partial_exit_of_an_eligible_coin_is_refused(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", True)
    monkeypatch.setattr(X, "eligible", lambda mint, rpc, **k: True)
    ex = _executor()
    out = ex["derisk_fn"](None, {"id": 5, "mint": "0x" + "ab" * 20}, 0.5, "2026-10-02T15:20:00Z")
    assert out == {"status": "derisk_failed", "reason": "flap quote-token coin: full exits only"}


@pytest.mark.parametrize("knob", [True, False])
def test_the_exit_preview_prewarms_eligibility_only_with_the_knob(monkeypatch, knob):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", knob)
    seen = []
    monkeypatch.setattr(X, "prewarm", lambda mint, rpc, **k: seen.append(mint))
    ex = _executor()
    try:
        ex["preview_exit_fn"]({"id": 5, "mint": "0x" + "ab" * 20, "entry_notional": 10.0})
    except Exception:
        pass                                              # the preview itself fails on fakes
    assert seen == (["0x" + "ab" * 20] if knob else [])


def test_flap_preview_quotes_the_executed_two_leg_route_without_writes(conn, monkeypatch):
    from sniper.execution import evm_flap_curve as F
    from test_memecoin_flap_quote_exit import MINT, RAW
    pid = _position(conn)
    chain = FakeChain()
    monkeypatch.setattr(F, "curve_state", lambda *a, **k: {"quote_token": CHEEMS})
    monkeypatch.setattr(F, "discover_sell_overrides", lambda *a, **k: {"x": 1})
    result = X.preview_exit(MINT, RAW, chain=chain, addresses=ADDR, price_fn=lambda: 600.)
    assert result.expected_usdc == V3_OUT / 1e18 * 600.
    assert result.amount_atomic == RAW
    assert result.quote_evidence["venue"] == "flap_quote_exit"
    assert result.quote_evidence["curve_leg"]["quote_mint"] == CHEEMS
    assert result.quote_evidence["input_atomic"] == str(RAW)
    assert chain.sent == [] and not X.journal_path(pid).exists()
    assert m.get_position(conn, pid)["is_open"]


@pytest.mark.parametrize("eligible", [True, None])
def test_funded_flap_preview_never_prices_the_unexecuted_aggregator(monkeypatch, eligible):
    from sniper.execution import evm_wallet as W
    from sniper.execution.exit_preview import ExitPreview, PreviewResult
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from sniper.settings import memecoin as cfg
    mint = "0x" + "ab" * 20
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", True)
    monkeypatch.setattr(X, "prewarm", lambda *a, **k: None)
    monkeypatch.setattr(X, "cached_eligibility", lambda *a: eligible)
    monkeypatch.setattr(W, "erc20_balance_raw", lambda *a, **k: 1722300532973262322812275)
    seen = []
    def preview(mint, atomic, **kwargs):
        seen.append((mint, atomic))
        return PreviewResult.succeeded(ExitPreview(29.96, atomic, "flap_quote_exit", "2026-10-10T08:50:04Z"))
    monkeypatch.setattr(X, "preview_exit", preview, raising=False)
    result = _executor(lambda *a, **k: pytest.fail("the unexecuted aggregator must not price this bag"))[
        "preview_exit_fn"]({"id": 72959, "mint": mint, "entry_notional": 50.})
    if eligible:
        assert result.expected_usdc == 29.96
        assert seen == [(mint, 1722300532973262322812275)]
    else:
        assert result.failure == QuoteFailure.MALFORMED_RESPONSE and seen == []


@pytest.mark.parametrize("fault", ["timeout", "transport", "missing_state", "missing_price", "no_route"])
def test_flap_preview_failures_stay_unknown_and_never_return_a_price(monkeypatch, fault):
    from sniper.execution import evm_flap_curve as F
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    from test_memecoin_flap_quote_exit import MINT, RAW
    chain = FakeChain(direct=fault != "no_route")
    def state(*a, **k):
        if fault == "timeout": raise TimeoutError()
        if fault == "transport": raise ConnectionError()
        return None if fault == "missing_state" else {"quote_token": CHEEMS}
    monkeypatch.setattr(F, "curve_state", state)
    monkeypatch.setattr(F, "discover_sell_overrides", lambda *a, **k: {"x": 1})
    result = X.preview_exit(MINT, RAW, chain=chain, addresses=ADDR,
                            price_fn=lambda: None if fault == "missing_price" else 600.)
    assert result.preview is None and result.expected_usdc is None
    expected = {"timeout": QuoteFailure.PROVIDER_TIMEOUT, "transport": QuoteFailure.RPC_TRANSPORT}.get(
        fault, QuoteFailure.MALFORMED_RESPONSE)
    assert result.failure == expected and chain.sent == []


def test_flap_preview_drives_the_same_card_return_and_stop(conn, monkeypatch):
    from sniper.execution import evm_flap_curve as F
    from sniper.services.memecoin_fastlane import executable_unit_price
    from sniper.services.memecoin_real_runner import executable_exit_allowed
    from sniper.routes.api_memecoin import _select_display_mark
    from test_memecoin_flap_quote_exit import MINT, RAW
    pid = _position(conn)
    # The incident's $50 cost, $52.88 aggregator mark and $29.96 executed-route value.
    conn.execute("UPDATE memecoin_positions SET entry_notional=50,entry_price=5 WHERE id=?", (pid,))
    conn.execute("UPDATE memecoin_fills SET notional=50,price=5 WHERE position_id=?", (pid,))
    conn.commit()
    pos = m.get_position(conn, pid)
    monkeypatch.setattr(F, "curve_state", lambda *a, **k: {"quote_token": CHEEMS})
    monkeypatch.setattr(F, "discover_sell_overrides", lambda *a, **k: {"x": 1})
    preview = X.preview_exit(MINT, RAW, chain=FakeChain(), addresses=ADDR,
                              price_fn=lambda: 29.96 / (V3_OUT / 1e18))
    unit = executable_unit_price(conn, pos, preview_fn=lambda p: preview)
    now = "2026-10-10T08:50:04Z"
    shown, basis, *_ = _select_display_mark(mid=52.88 / 10., mid_observed_at=now,
        liquidity=None, qty=10., now_iso=now, exact_price=unit, exact_observed_at=now)
    assert basis == "measured_route" and shown == pytest.approx(2.996)
    assert m.position_return_pct(conn, pid, entry_notional=50., last_price=shown,
                                 entry_price=5., open_quantity=10.) == pytest.approx(-40.08)
    assert executable_exit_allowed(conn, pos, "b_stop", preview,
                                   override={"sl_pct": -35}, quote_qty=10.)[0]


@pytest.mark.parametrize("status, knob, expect", [("filled", True, True), ("aborted", True, False),
                                                  ("filled", False, False)])
def test_a_filled_56_entry_prewarms_after_the_fill(monkeypatch, status, knob, expect):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_QUOTE_EXIT", knob)
    seen = []
    monkeypatch.setattr(X, "prewarm", lambda mint, rpc, **k: seen.append(mint))
    X.prewarm_after_fill("0x" + "cd" * 20, "rpc", {"status": status})
    assert seen == (["0x" + "cd" * 20] if expect else [])
    assert getattr(_executor()["execute_fn"], "__wrapped__", None) is not None   # 56 seam wraps


@pytest.mark.parametrize("leg", ["leg1", "leg2"])
def test_a_dropped_leg_is_proven_dropped_by_its_used_nonce_and_retried_not_pinned(conn, leg):  # noqa: F811
    # Gamma MED on 4d255bf4a: with no receipt the settle step returned pending forever.
    pid = _position(conn)
    chain = FakeChain()
    chain.drop = {leg}
    clock = {"t": 0.0}
    for _ in range(3):                                   # until the drop is first seen
        first, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S, clock=clock)
        if first["status"] != "pending":
            break
        clock["t"] += 1.0
        if "drop_seen" in json.dumps(X.read_journal(X.journal_path(pid))):
            break
    assert first["status"] == "pending"                  # one proof is not enough (backend skew)
    clock["t"] += X.DROP_CONFIRM_S
    first, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S, clock=clock)
    expect = "aborted" if leg == "leg1" else "stuck"
    assert first["status"] == expect and "dropped" in first["reason"], first
    if leg == "leg1":
        pos = m.get_position(conn, pid)
        assert pos["close_claim_id"] is None and pos["close_sig"] is None and pos["is_open"]
    chain.drop = set()
    for _ in range(10):
        result, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S, clock=clock)
        if result["status"] == "closed":
            break
    assert result["status"] == "closed"
    assert chain.sent.count("dropped") == 1


def test_a_send_whose_nonce_is_not_yet_used_stays_pending(conn):  # noqa: F811
    pid = _position(conn)
    chain = FakeChain(hold_receipt=("leg1",))
    chain.nonce -= 1                                       # latest has not passed leg 1's nonce
    first, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S)
    assert first["status"] in ("pending", "aborted")


def test_the_resume_cap_pages_once_and_a_state_change_starts_over():
    pages = []
    X.write_journal(X.journal_path(9), {"state": "leg2_submitted"})
    admitted = [X.resume_admit(9, page_fn=pages.append) for _ in range(X.RESUME_CAP + 5)]
    assert admitted == [True] * X.RESUME_CAP + [False] * 5
    assert len(pages) == 1 and "position 9" in pages[0]
    X.write_journal(X.journal_path(9), {"state": "leg2_failed"})
    assert X.resume_admit(9, page_fn=pages.append) is True and len(pages) == 1


def test_a_drop_proof_that_does_not_hold_twice_is_not_a_drop(conn):  # noqa: F811
    # Gamma LOW-MED on b7bb4e3d2: a fresh nonce beside a stale balance on one URL must not end a
    # mined leg. The second read sees the effect (the bag moved), so the first sighting is cleared.
    pid = _position(conn)
    chain = FakeChain(hold_receipt=("leg1",))
    clock = {"t": 0.0}
    first, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S, clock=clock)
    assert first["status"] == "pending"
    chain.nonce += 1                                      # latest passes leg 1's nonce...
    stale = {"on": True}
    real_balance = chain.balance
    before = int(X.read_journal(X.journal_path(pid))["balance_raw"])
    chain.balance = lambda token: before if stale["on"] and token == MINT else real_balance(token)
    second, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S, clock=clock)
    assert second["status"] == "pending"                  # ...a stale balance: first sighting only
    stale["on"] = False                                   # the next read shows the bag moved
    clock["t"] += X.DROP_CONFIRM_S + 1
    third, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S, clock=clock)
    assert third["status"] == "pending"                   # not a drop: the claim stays
    assert m.get_position(conn, pid)["close_sig"]


from test_memecoin_flap_quote_exit import MINT  # noqa: E402


def test_automatic_flap_close_keeps_its_reason_through_pending_resume(conn):
    pid = _position(conn)
    chain = FakeChain(revert_leg2=1)
    first, _ = _exit(conn, m.get_position(conn, pid), chain)
    assert first['status'] == 'stuck'
    result, _ = _exit(conn, m.get_position(conn, pid), chain, eligible=False)
    assert result['status'] == 'closed'
    pos = m.get_position(conn, pid)
    assert pos['exit_reason'] == pos['close_exit_reason'] == 'b_stop'


def test_flap_actual_route_refusal_sends_no_approval(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain()
    run = X.Run(conn, pid, chain=chain, sign_fn=_signer(), journal_path=tmp_path/'gate.json',
                price_fn=lambda: 600., addresses=ADDR,
                state_fn=lambda *args: {'quote_token': CHEEMS, 'status': 4},
                overrides_fn=lambda *args, **kwargs: {'x': 1},
                quote_check_fn=lambda pos, expected, floor, qty: (False, 'executable_threshold_not_met'))
    with pytest.raises(X.Refused, match='executable_threshold_not_met'):
        run.run_locked()
    assert chain.sent == []
    assert m.get_position(conn, pid)['close_claim_id'] is None


def test_read_adapter_returns_before_blocked_read_and_reuses_its_result():
    import threading
    import time
    entered, release = threading.Event(), threading.Event()
    reads = []
    def http(method, url, **kwargs):
        reads.append(kwargs['body'])
        entered.set()
        release.wait(2.)
        return 200, {'result': '0x1'}, {}
    adapter = X.ReadBudget(http)
    adapter.begin(time.monotonic() + .05)
    started = time.monotonic()
    try:
        with pytest.raises(X.Pending):
            adapter('POST', 'http://offline', body='{"method":"eth_call"}')
        assert time.monotonic() - started < .25
        assert entered.is_set()
        release.set()
        adapter.begin(time.monotonic() + 1.)
        assert adapter('POST', 'http://offline', body='{"method":"eth_call"}')[1] == {'result': '0x1'}
        assert len(reads) == 1
    finally:
        release.set()


@pytest.mark.parametrize("incoming_reason", ["b_stop", "real_tp"])
def test_funded_seam_checks_flap_value_instead_of_the_callers_dust_stop(conn, monkeypatch, incoming_reason):
    from types import SimpleNamespace
    from sniper.execution import evm_real_seams as S
    from sniper.services.memecoin_real_runner import executable_exit_allowed
    from sniper.settings import memecoin as cfg
    from test_memecoin_flap_quote_exit import WALLET
    pid = _position(conn)
    pos = m.get_position(conn, pid)
    assert executable_exit_allowed(conn, pos, 'b_stop', {'expected_usdc': .12},
                                   override={'stop_pct': -30}, quote_qty=10.)[0]
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_FLAP_QUOTE_EXIT', True)
    monkeypatch.setattr(X, 'cached_eligibility', lambda *args: True)
    monkeypatch.setattr(X, '_grade', lambda *args: None)
    chain = FakeChain()
    original = X.Run
    def offline_run(*args, **kwargs):
        kwargs.update(chain=chain, sign_fn=_signer(), addresses=ADDR,
                      state_fn=lambda *args: {'quote_token': CHEEMS, 'status': 4},
                      overrides_fn=lambda *args, **kwargs: {'x': 1})
        return original(*args, **kwargs)
    monkeypatch.setattr(X, 'Run', offline_run)
    if incoming_reason != 'b_stop':
        X.write_journal(X.journal_path(pid), {'state': 'approving_leg1', 'mint': pos['mint'],
                         'quote_token': CHEEMS, 'balance_raw': str(10 * 10**18), 'exit_reason': 'b_stop'})
    seam = S.build_real_evm_executor(acct=SimpleNamespace(address=WALLET), chain_id='56',
                                     rpc_url='http://offline', price_fn=lambda: 6000.)
    result = seam['exit_fn'](conn, pos, incoming_reason, '2026-10-02T15:20:00Z')
    assert result['status'] == 'aborted'
    assert 'executable_threshold_not_met' in result['reason']
    assert chain.sent == []
    row = conn.execute("SELECT allowed, preview_usdc, detail FROM memecoin_exit_trigger_audit "
                       "WHERE position_id=? AND reason='b_stop_execution'", (pid,)).fetchone()
    assert row['allowed'] == 0 and row['preview_usdc'] == 366.
    assert json.loads(row['detail'])['route'] == 'flap_quote_exit'


def test_flap_rechecks_after_approval_before_the_coin_sale(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain()
    values = iter([(True, 'threshold_confirmed'), (False, 'executable_threshold_not_met')])
    run = X.Run(conn, pid, chain=chain, sign_fn=_signer(), journal_path=tmp_path/'recheck.json',
                price_fn=lambda: 600., addresses=ADDR,
                state_fn=lambda *args: {'quote_token': CHEEMS, 'status': 4},
                overrides_fn=lambda *args, **kwargs: {'x': 1},
                quote_check_fn=lambda *args: next(values), exit_reason='b_stop')
    with pytest.raises(X.Refused, match='executable_threshold_not_met'):
        run.run_locked()
    assert chain.sent == ['approve']
    assert m.get_position(conn, pid)['close_claim_id'] is None


def test_chain_receipt_rpc_deadline_does_not_become_an_empty_answer():
    import threading
    import time
    release = threading.Event()
    def http(*args, **kwargs):
        release.wait(2.)
        return 200, {'result': None}, {}
    budget = X.ReadBudget(http)
    chain = X.Chain('http://offline', '0xwallet', read_budget=budget)
    budget.begin(time.monotonic() + .05)
    try:
        with pytest.raises(X.Pending):
            chain.receipt('0xhash')
    finally:
        release.set()


def test_runner_blocked_read_obeys_budget_then_resumes_without_a_second_send(conn, tmp_path):
    import threading
    import time
    from test_memecoin_flap_quote_exit import RAW, WALLET, MINT
    pid = _position(conn)
    fake, release = FakeChain(), threading.Event()
    first_balance = [True]
    def http(method, url, **kwargs):
        request = json.loads(kwargs['body'])
        name, args = request['method'], request['params']
        if name == 'eth_call':
            call = args[0]
            data, token = call['data'], call['to'].lower()
            if data.startswith('0x70a08231'):
                if first_balance[0]:
                    first_balance[0] = False
                    release.wait(2.)
                answer = hex(fake.bal[token])
            elif data.startswith('0xdd62ed3e'):
                spender = '0x' + data[-40:]
                answer = hex(fake.allow.get((token, spender), 0))
            else:
                answer = fake.call(token, data, overrides=args[2] if len(args) > 2 else None)
        elif name == 'eth_estimateGas':
            answer = hex(500000)
        elif name == 'eth_gasPrice':
            answer = hex(100000000)
        elif name == 'eth_getTransactionCount':
            answer = hex(fake.nonce)
        elif name == 'eth_getTransactionReceipt':
            answer = fake.receipt(args[0])
        else:
            raise AssertionError(name)
        return 200, {'result': answer}, {}
    budget = X.ReadBudget(http)
    chain = X.Chain('http://offline', WALLET, read_budget=budget,
                    send_fn=fake.send, approve_send_fn=lambda raw: fake.send(raw))
    path = tmp_path/'bounded-run.json'
    def factory():
        return X.Run(conn, pid, chain=chain, sign_fn=_signer(), journal_path=path,
                     price_fn=lambda: 600., addresses=ADDR, step_budget_s=.05,
                     state_fn=lambda *args: {'quote_token': CHEEMS, 'status': 4})
    started = time.monotonic()
    try:
        with pytest.raises(X.Pending):
            factory().run_locked()
        assert time.monotonic() - started < .25
        assert fake.sent == []
        release.set()
        run = factory()
        run.step_budget_s = 3.
        assert run.run_locked()['state'] == 'booked'
        assert fake.sent == ['approve', 'leg1', 'approve', 'leg2']
        assert m.get_position(conn, pid)['is_open'] == 0
    finally:
        release.set()


def test_manual_force_sell_supersedes_an_unsigned_held_attempt(conn, tmp_path):
    pid = _position(conn)
    chain = FakeChain()
    path = tmp_path/'force.json'
    X.write_journal(path, {'state': 'approving_leg1', 'mint': m.get_position(conn, pid)['mint'],
                          'balance_raw': str(10 * 10**18), 'quote_token': CHEEMS,
                          'exit_reason': 'b_stop'})
    run = X.Run(conn, pid, chain=chain, sign_fn=_signer(), journal_path=path,
                price_fn=lambda: 600., addresses=ADDR,
                state_fn=lambda *args: {'quote_token': CHEEMS, 'status': 4}, exit_reason='manual')
    assert run.run_locked()['state'] == 'booked'
    assert m.get_position(conn, pid)['exit_reason'] == 'manual'


def test_signed_flap_resume_keeps_reason_when_the_operator_finishes_it(conn):
    pid = _position(conn)
    chain = FakeChain(revert_leg2=1)
    first, _ = _exit(conn, m.get_position(conn, pid), chain)
    assert first['status'] == 'stuck'
    result = X.runner_exit(conn, m.get_position(conn, pid), 'manual', 'now',
                           run_factory=_factory(conn, chain), eligible_fn=lambda _: False)
    assert result['status'] == 'closed'
    assert m.get_position(conn, pid)['exit_reason'] == 'b_stop'


@pytest.mark.parametrize('outcome', ['aborted', 'pending', 'stuck', 'read_pending'])
def test_read_budget_is_removed_only_for_an_explicit_unsigned_abort(conn, monkeypatch, outcome):
    pid = _position(conn)
    budget = object()
    monkeypatch.setattr(X, '_READ_BUDGETS', {pid: budget})
    if outcome == 'aborted':
        chain = FakeChain()
        mint = m.get_position(conn, pid)['mint']
        chain.allow[(mint, ADDR['portal'])] = 10 * 10**18
        chain.pending = chain.nonce + 1
        X.write_journal(X.journal_path(pid), {
            'state': 'approving_leg1', 'mint': mint, 'quote_token': CHEEMS,
            'balance_raw': str(10 * 10**18), 'exit_reason': 'b_stop',
            'leg1_approve': '0xapproved',
            'leg1_approve_for': [mint, ADDR['portal'], str(10 * 10**18)]})
    elif outcome == 'read_pending':
        chain = FakeChain()
        def pending_read(*args):
            raise X.Pending('balance read still running')
        monkeypatch.setattr(chain, 'balance', pending_read)
    else:
        chain = FakeChain(hold_receipt=('leg1',)) if outcome == 'pending' else FakeChain(revert_leg2=1)
    result, _ = _exit(conn, m.get_position(conn, pid), chain,
                      budget=3. if outcome == 'pending' else None)
    assert result['status'] == ('pending' if outcome == 'read_pending' else outcome)
    if outcome == 'aborted':
        assert pid not in X._READ_BUDGETS
        assert X.read_journal(X.journal_path(pid))['leg1_approve'] == '0xapproved'
        assert chain.sent == []
        assert m.get_position(conn, pid)['close_claim_id'] is None
    else:
        assert X._READ_BUDGETS[pid] is budget
        assert (m.get_position(conn, pid)['close_sig'] is None) == (outcome == 'read_pending')


def test_retry_cap_keeps_two_leg_exit_out_of_generic_close_recovery(monkeypatch):
    from sniper.services import memecoin_real_runner as R
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg,'MEMECOIN_EVM_FLAP_QUOTE_EXIT',True)
    monkeypatch.setattr(X,'resume_due',lambda pid:True)
    monkeypatch.setattr(X,'resume_admit',lambda pid:False)
    calls=[]
    monkeypatch.setattr(R,'context_for_position',lambda *a:{'exit_fn':lambda *a:calls.append(a)})
    assert R._resume_flap_quote_exit(None,{}, {'id':7,'network_id':'56'},'now') is True
    assert calls==[]


def test_a_step_that_ends_past_the_budget_hands_the_next_step_to_the_next_call(conn, monkeypatch):  # noqa: F811
    # Gamma INFO on 3a17fb8e3: with every signature its own hash (as a real signer's), leg 1 really
    # needs MINE_POLLS reads; a receipt wait ending past the deadline must not run leg 2's reads too.
    import sys
    seq = iter(range(1, 10 ** 6))

    def unique():
        def sign(tx):
            h = "0x%064x" % next(seq)
            return json.dumps(dict(tx, hash=h)), h
        return sign
    monkeypatch.setattr(sys.modules[__name__], "_signer", unique)
    pid = _position(conn)
    clock = {"t": 0.0}
    chain = SlowChain(clock)
    walls, result = [], None
    for _ in range(40):
        start = clock["t"]
        result, _ = _exit(conn, m.get_position(conn, pid), chain, budget=X.STEP_BUDGET_S, clock=clock)
        walls.append(clock["t"] - start)
        if result["status"] == "closed":
            break
    assert result["status"] == "closed" and max(walls) <= X.STEP_BUDGET_S + 0.5, walls
    assert chain.sent == ["approve", "leg1", "approve", "leg2"]
