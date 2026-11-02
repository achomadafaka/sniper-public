"""Blocker 2 — the 6 operational alerts had zero call sites. Wire them into the real leg and verify
they actually fire: entry/failed on the entry hook, exit/stuck/failed on the exit paths, and the
kill-switch/daily-loss/gas-low transition alerts (once on the False->True crossing, not every cycle)."""
import logging
import time
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_alerts as alerts
from sniper.services import memecoin_arming
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


@pytest.fixture
def spy(monkeypatch):
    fired = {}
    for name in ("alert_entry", "alert_exit", "alert_stuck_bag", "alert_failed_tx",
                 "alert_daily_loss", "alert_gas_low", "alert_kill_switch",
                 "alert_entry_result"):
        monkeypatch.setattr(alerts, name,
                            (lambda n: (lambda *a, **k: fired.setdefault(n, []).append(a)))(name))
    return fired


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


class _Ev:
    def __init__(self, mint="M", created_at="2026-07-09T00:00:00Z"):
        self.mint = mint; self.source_user_id = "u1"; self.created_at = created_at
        self.network_id = "solana"; self.side = "buy"


def _open_real(c, mint):
    return m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="T",
                           entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                           entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                           strategy="alpha_b", execution_mode="real", network_id="solana")


def test_entry_hook_fires_alert_entry(monkeypatch, tmp_path, spy):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _c()

    seen = {}

    def ex(conn, pid, mint, size, now, impact_cap_pct=None,
           entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        seen["entry_window"] = (entry_min_slot, entry_max_slot, entry_head_fn)
        return {"status": "filled", "position_id": pid, "fill_price": 2.0, "sig": "SIG"}
    rc = {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
          "token_safety_fn": lambda mint, **kw: (True, "ok"), "execute_fn": ex, "kill_path": str(tmp_path / "k")}
    ev = _Ev()
    ev.source_block_number = 100
    R.real_entry_hook(c, ev, {"solana": rc}, now_fn=lambda: "2026-07-09T00:00:00Z", trade_size=10.0, max_age_s=120)
    assert "alert_entry" in spy and spy["alert_entry"][0][:3] == ("M", 2.0, 10.0)
    # PR #486 (2026-10-20): automatic Solana entries carry no source-slot window; both bounds are None.
    assert seen["entry_window"][:2] == (None, None)
    assert callable(seen["entry_window"][2])


def test_entry_hook_passes_copy_provenance_to_filled_alert(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    sent = []
    monkeypatch.setattr(alerts, "alert_entry", lambda *args, **kwargs: sent.append(kwargs))
    c = _c()
    ev = _Ev()
    ev.ticker = "WORLD"
    ev.source_handle = ""
    ev.their_size_usd = 999.187419
    ev.their_price = 0.000235191
    ev.lag_s = 40.534614
    ev.fomo_event_id = "act-1"
    ev.source_block_number = 100
    seen = {}

    def ex(conn, pid, mint, size, now, impact_cap_pct=None,
           entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        seen["entry_window"] = (entry_min_slot, entry_max_slot, entry_head_fn)
        return {"status": "filled", "position_id": pid, "fill_price": 0.000205138, "sig": "SIG"}

    rc = {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
          "token_safety_fn": lambda mint, **kw: (True, "ok"), "execute_fn": ex,
          "kill_path": str(tmp_path / "k")}
    R.real_entry_hook(c, ev, {"solana": rc}, now_fn=lambda: "2026-07-09T00:00:00Z",
                      trade_size=70.11, max_age_s=120)

    assert sent == [{
        "handle": "", "ticker": "WORLD", "source_user_id": "u1",
        "source_size_usd": 999.187419, "source_price": 0.000235191,
        "entry_lag_s": 40.534614, "policy": cfg.MEMECOIN_REAL_POLICY,
        "network_id": "solana", "source_id": "act-1",
    }]
    # PR #486 (2026-10-20): automatic Solana entries carry no source-slot window; both bounds are None.
    assert seen["entry_window"][:2] == (None, None)
    assert callable(seen["entry_window"][2])


@pytest.mark.parametrize("status", [
    "skipped_mcap", "aborted", "dropped_network", "evm_unarmed", "route_failed", "unsafe", "rejected",
    "stale", "disarmed",
])
def test_entry_result_fires_one_named_refusal_alert(status, spy):
    R._alert_entry_result("M", 10.0, {"status": status, "reason": "because"},
                          handle="alice", ticker="COIN")
    assert len(spy.get("alert_entry_result", [])) == 1


def test_not_approved_does_not_send_real_result_alert(spy):
    R._alert_entry_result("M", 10.0, {"status": "not_approved"})
    assert "alert_entry_result" not in spy


def test_config_gap_alert_names_every_missing_chain():
    sent = []
    alerts.alert_config_gap(["56", "8453"], send_fn=lambda text: sent.append(text) or (True, 0.0))
    assert sent == [
        "🔴 memecoin CONFIG-GAP | funded trading enabled but allowlisted EVM chain(s) "
        "have no built context: 56,8453"
    ]


def test_exit_sweep_fires_alert_exit_and_stuck(spy, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)   # subject: alerts, not debounce
    c = _c()
    _open_real(c, "MDrop")
    _open_real(c, "MStuck")
    from sniper.execution.solana_quote import norm_mint
    drop = 1.0 * (1 - (cfg.MEMECOIN_B_STOP_PCT + 1) / 100.0)
    pmap = {norm_mint("MDrop"): {"price": drop}, norm_mint("MStuck"): {"price": drop}}

    def exit_fn(conn, pos, reason, now):
        return {"status": "closed", "realized": 5.0, "sig": "X"} if pos["mint"] == "MDrop" \
            else {"status": "stuck"}
    rc = {"exit_fn": exit_fn, "preview_exit_fn": lambda pos: {"expected_usdc": 1.0}}
    R.real_exit_sweep(c, {"solana": rc}, price_fn=lambda mints: pmap, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert spy["alert_exit"][0][:1] == ("MDrop",) and spy["alert_exit"][0][2] == "b_stop"
    assert spy["alert_stuck_bag"][0][0] == "MStuck"


def test_kill_switch_alert_fires_once_on_transition(tmp_path, spy):
    kill = str(tmp_path / "k")
    assert R.check_kill_switch_alert(kill, False) is False          # not tripped
    memecoin_arming.trip_kill_switch(kill)
    assert R.check_kill_switch_alert(kill, False) is True           # False->True -> alert
    assert R.check_kill_switch_alert(kill, True) is True            # already tripped -> no re-alert
    assert len(spy.get("alert_kill_switch", [])) == 1


def test_daily_loss_alert_fires_on_fresh_latch(spy):
    c = _c()
    pid = _open_real(c, "MLoss")
    m.close_position(c, pid, "2026-07-09T00:05:00Z", realized_pnl=-(cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD + 1))
    m.check_and_latch_daily_loss(c, "2026-07-09", cfg.MEMECOIN_DAILY_LOSS_LIMIT_USD)
    assert R.check_daily_loss_alert(c, "2026-07-09", False) is True
    assert len(spy.get("alert_daily_loss", [])) == 1
    R.check_daily_loss_alert(c, "2026-07-09", True)                 # no re-alert
    assert len(spy.get("alert_daily_loss", [])) == 1


def test_gas_alert_fires_when_below_reserve(spy):
    rc = {"sol_fn": lambda: 0.0}                                    # no SOL -> below any reserve
    assert R.check_gas_alert(rc, 0, False) is True
    assert len(spy.get("alert_gas_low", [])) == 1
    rc_ok = {"sol_fn": lambda: 100.0}
    assert R.check_gas_alert(rc_ok, 0, False) is False             # ample SOL -> no alert


def test_gas_alert_does_not_report_a_failed_sol_read(spy):
    from sniper.execution.solana_wallet import NativeSolBalanceRead

    rc = {"sol_fn": lambda: NativeSolBalanceRead(
        ok=False, balance=None, failure="rpc_error")}

    assert R.check_gas_alert(rc, 0, False) is False
    assert "alert_gas_low" not in spy


def test_lifi_disable_alerts_emit_new_reasons_once_and_one_recovery():
    from sniper.services.memecoin_alerts import alert_lifi_disable_transitions

    sent = []
    current = alert_lifi_disable_transitions(
        {"quota_unknown"}, {"quota_unknown", "recovery_incomplete"},
        send_fn=lambda message: sent.append(message),
    )
    repeated = alert_lifi_disable_transitions(
        current, {"quota_unknown", "recovery_incomplete"},
        send_fn=lambda message: sent.append(message),
    )
    cleared = alert_lifi_disable_transitions(
        repeated, {"quota_unknown"}, send_fn=lambda message: sent.append(message),
    )

    assert current == repeated == frozenset({"quota_unknown", "recovery_incomplete"})
    assert cleared == frozenset({"quota_unknown"})
    assert len(sent) == 2
    assert "recovery_incomplete" in sent[0]
    assert "RECOVERED" in sent[1]


def test_lifi_startup_waits_for_a_stable_baseline_before_it_alerts():
    from sniper.services.memecoin_alerts import alert_lifi_startup_transition

    sent = []
    pending = alert_lifi_startup_transition(
        None, {"unauthenticated", "recovery_incomplete"}, baseline_ready=False,
        send_fn=lambda message: sent.append(message),
    )
    healthy = alert_lifi_startup_transition(
        pending, set(), baseline_ready=True,
        send_fn=lambda message: sent.append(message),
    )
    failed = alert_lifi_startup_transition(
        None, {"unauthenticated"}, baseline_ready=True,
        send_fn=lambda message: sent.append(message),
    )

    assert pending is None
    assert healthy == frozenset()
    assert failed == frozenset({"unauthenticated"})
    assert len(sent) == 1
    assert "LIFI-DISABLED" in sent[0]
    assert "unauthenticated" in sent[0]


def test_consensus_skip_alert_says_consensus_not_ceiling(monkeypatch):
    """2026-07-17 (RUSSELL 21:01): a solo $1.8M refusal rendered as 'market cap $1.8M > $2.0M
    ceiling' — false. The consensus gate reuses skipped_mcap; the alert must say the real reason.

    Un-silences the kind explicitly: CONSENSUS-SKIP is in the 2026-08-06 default silent set, and
    this test is about REASON ROUTING, not about whether the kind reaches the phone."""
    from sniper.services import memecoin_alerts as A
    monkeypatch.setattr(A.cfg, "MEMECOIN_ALERT_SILENT_REFUSALS", frozenset())
    sent = []
    A.alert_entry_result(
        "0xmint", {"status": "skipped_mcap", "mcap": 1_800_000.0,
                   "reason": "no consensus above $1000K mcap (0 prior buyers/60m, need >=1)"},
        ceiling=2_000_000.0, handle="smol_intern", ticker="RUSSELL",
        send_fn=lambda msg: sent.append(msg))
    assert len(sent) == 1
    assert "CONSENSUS-SKIP" in sent[0]
    assert "consensus" in sent[0]
    assert "ceiling" not in sent[0]


# --- telegram is the funded operator channel (2026-08-06) ----------------------
# 84 of the ~173 entry alerts in 24h were refusals on wallets with no funded authority on that
# chain — half the traffic, none of it actionable.

def test_refusals_from_unarmed_wallets_never_reach_telegram():
    import sniper.services.memecoin_real_runner as RR
    sent = []
    RR._alert_entry_result("M", 10.0, {"status": "rejected", "reason": "coin band"},
                           handle="bob", ticker="T", armed=False)
    assert sent == []


def test_a_fill_alerts_even_if_the_wallet_reads_as_unarmed(monkeypatch):
    """Money moved. Silencing that because a wallet-table lookup disagreed is the worst failure
    this gate could have — an earlier draft gated the whole function and swallowed fills."""
    import sniper.services.memecoin_real_runner as RR
    fired = []
    monkeypatch.setattr(RR, "_safe_alert", lambda fn, *a: fired.append(a))
    RR._alert_entry_result("M", 10.0, {"status": "filled", "fill_price": 2.0, "sig": "S"},
                           handle="bob", ticker="T", armed=False)
    assert fired, "a filled entry must alert regardless of the armed lookup"


def test_a_failed_tx_alerts_even_if_the_wallet_reads_as_unarmed(monkeypatch):
    import sniper.services.memecoin_real_runner as RR
    fired = []
    monkeypatch.setattr(RR, "_safe_alert", lambda fn, *a: fired.append(a))
    RR._alert_entry_result("M", 10.0, {"status": "failed", "sig": "S"},
                           handle="bob", ticker="T", armed=False)
    assert fired, "a failed transaction must alert regardless of the armed lookup"


def test_armed_lookup_fails_open(monkeypatch):
    """A broken lookup must alert, never silence."""
    import sniper.services.memecoin_real_runner as RR

    class _Boom:
        def execute(self, *a, **k): raise RuntimeError("db down")

    ev = type("E", (), {"source_user_id": "u", "network_id": "solana"})()
    identity = type("I", (), {"network_id": "solana"})()
    assert RR._wallet_is_armed(_Boom(), ev, identity) is True
    assert RR._wallet_is_armed(None, ev, identity) is True

def test_policy_declines_are_silent_but_faults_still_alert(monkeypatch):
    """2026-08-06 operator: routine policy declines off the phone, faults stay loud.

    The split is the whole point of the change — silencing a token-safety or route refusal would
    hide 'this coin has no sell route' behind 'policy said no', which is the failure mode this
    test exists to prevent."""
    from sniper.services import memecoin_alerts as A
    silent = {"status": "rejected", "reason": "coin band is not reservable: 'assumed_nano'"}
    loud = [
        ({"status": "unsafe", "reason": "sell simulation unverifiable"}, "TOKEN-SAFETY-REFUSAL"),
        ({"status": "route_failed", "reason": "no sell route"}, "ENTRY-ROUTE-REFUSAL"),
        ({"status": "aborted", "reason": "price impact 40%"}, "SLIPPAGE-REFUSAL"),
        ({"status": "dropped_network", "network": "9999"}, "NETWORK-DROP"),
        ({"status": "disarmed", "reason": "kill switch"}, "ENTRY-DISARMED"),
    ]
    sent = []
    assert A.alert_entry_result("0xm", silent, send_fn=lambda m: sent.append(m)) == (False, 0.0)
    assert sent == [], "a routine policy decline must not reach the phone"
    for result, kind in loud:
        sent.clear()
        A.alert_entry_result("0xm", result, send_fn=lambda m: sent.append(m))
        assert len(sent) == 1 and kind in sent[0], f"{kind} is a FAULT and must always alert"
    # config-only: emptying the set restores the old fully-verbose behaviour, no redeploy
    monkeypatch.setattr(A.cfg, "MEMECOIN_ALERT_SILENT_REFUSALS", frozenset())
    sent.clear()
    A.alert_entry_result("0xm", silent, send_fn=lambda m: sent.append(m))
    assert len(sent) == 1 and "RISK-REFUSAL" in sent[0]


# --- the kill switch was silent in the LOG as well as the tape (2026-09-03) -------------------
# `check_kill_switch_alert` paged Telegram ONCE on the crossing and wrote nothing to the log.
# `grep -i 'kill.switch'` over both runner log rotations returned zero lines, including for the
# 2026-09-02T20:33:26Z trip that stood the funded leg down for 11.6 hours. Alpha's 98005275 fixed
# the refusal tape (a tripped switch surfaced as a bare `disarmed`); the log stayed silent.
#
# Same shape as the AUTH-DEAD defect: one page on the crossing, then a surface indistinguishable
# from healthy. Log-only by instruction — the switch already sends its one Telegram page and the
# operator gets one page per real event.
#
# Elapsed time comes from the kill FILE, never from process-local state: `prev_tripped` resets on
# every restart, so a process-local clock would report "down 2m" on a leg that has been dark 11.6h.

def test_kill_switch_trip_and_clear_are_both_logged(tmp_path, caplog):
    kill = str(tmp_path / "k")
    R.check_kill_switch_alert(kill, False)
    memecoin_arming.trip_kill_switch(kill)
    with caplog.at_level(logging.WARNING):
        R.check_kill_switch_alert(kill, False)
    assert any("TRIPPED" in r.message for r in caplog.records), [r.message for r in caplog.records]
    caplog.clear()
    memecoin_arming.clear_kill_switch(kill, expected_token=memecoin_arming.kill_switch_status(kill)["kill_token"])
    with caplog.at_level(logging.WARNING):
        R.check_kill_switch_alert(kill, True)
    assert any("CLEARED" in r.message for r in caplog.records), [r.message for r in caplog.records]


def test_kill_switch_logs_at_warning_because_this_process_discards_info(tmp_path, caplog):
    """The runner configures no logging level, so the root logger sits at WARNING and every
    logger.info on the funded path is thrown away. A kill-switch line at INFO would not exist."""
    kill = str(tmp_path / "k")
    memecoin_arming.trip_kill_switch(kill)
    with caplog.at_level(logging.WARNING):
        R.check_kill_switch_alert(kill, False)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_still_tripped_switch_relogs_so_a_long_dark_period_is_visible(tmp_path, caplog):
    """A one-shot beacon cannot report continued death — the whole point of the 11.6h outage."""
    kill = str(tmp_path / "k")
    memecoin_arming.trip_kill_switch(kill)
    state = {}
    clock = [1000.0]
    with caplog.at_level(logging.WARNING):
        R.check_kill_switch_alert(kill, False, now_fn=lambda: clock[0], state=state)
        caplog.clear()
        R.check_kill_switch_alert(kill, True, now_fn=lambda: clock[0], state=state)
        assert not caplog.records, "must not re-log every cycle"
        clock[0] += R._KILL_RELOG_S + 1
        R.check_kill_switch_alert(kill, True, now_fn=lambda: clock[0], state=state)
    assert any("STILL TRIPPED" in r.message for r in caplog.records), [r.message for r in caplog.records]


def test_elapsed_comes_from_the_file_not_from_process_local_state(tmp_path, caplog):
    """prev_tripped resets on restart. A process-local clock would report a fresh outage on a leg
    that has been dark for hours — the exact misread this fix exists to prevent."""
    import os as _os
    kill = str(tmp_path / "k")
    memecoin_arming.trip_kill_switch(kill)
    old = time.time() - 7 * 3600                       # tripped 7 hours ago, before this process
    _os.utime(kill, (old, old))
    state = {}
    with caplog.at_level(logging.WARNING):
        R.check_kill_switch_alert(kill, False, now_fn=lambda: 1000.0, state=state)
    msg = " ".join(r.message for r in caplog.records)
    assert "420m" in msg or "7h" in msg, (
        "a restart must report the TRUE age of the trip, not its own uptime: %r" % msg)


def test_the_relog_never_sends_a_second_telegram_page(tmp_path, spy):
    """Instruction from Alpha: log-only. The switch already sends its one page."""
    kill = str(tmp_path / "k")
    memecoin_arming.trip_kill_switch(kill)
    state = {}
    clock = [1000.0]
    R.check_kill_switch_alert(kill, False, now_fn=lambda: clock[0], state=state)
    for _ in range(5):
        clock[0] += R._KILL_RELOG_S + 1
        R.check_kill_switch_alert(kill, True, now_fn=lambda: clock[0], state=state)
    assert len(spy.get("alert_kill_switch", [])) == 1, "re-logging must never re-page"


# --- a FAILED-TX page must say WHY (2026-09-08) --------------------------------------------------
#
# The operator forwarded `🔴 memecoin FAILED-TX | B5vZ3spp… sig=XuMkuwvn — reservation released`
# and asked what it was. Answering took a mainnet getTransaction, because the page carried a mint
# and eight characters of signature and nothing else. The runner already HAD the answer: the
# executor puts the chain's own words in `res["reason"]` and had recorded
# "exceeds desired slippage limit" — a designed, atomic, no-funds-moved refusal.
#
# That distinction is the whole value of the page. "Raydium refused the price" and "the swap ran
# out of funds" are the same red alert today, and only one of them is worth waking up for.

def test_a_failed_tx_page_carries_the_chain_reason(monkeypatch):
    import sniper.services.memecoin_real_runner as RR
    fired = []
    monkeypatch.setattr(RR, "_safe_alert", lambda fn, *a: fired.append(a))
    RR._alert_entry_result("M", 10.0, {"status": "failed", "sig": "S",
                                       "reason": "chain_error:exceeds desired slippage limit"})
    # the raw reason reaches the alert; `alert_failed_tx` owns the `chain_error:` strip
    assert fired and fired[0][-1] == "chain_error:exceeds desired slippage limit", fired


def test_a_failed_tx_page_without_a_reason_still_fires(monkeypatch):
    """Older seams return a bare 'failed'. Losing the page would be far worse than losing the why."""
    import sniper.services.memecoin_real_runner as RR
    fired = []
    monkeypatch.setattr(RR, "_safe_alert", lambda fn, *a: fired.append(a))
    RR._alert_entry_result("M", 10.0, {"status": "failed", "sig": "S"})
    assert fired and fired[0][-1] is None


def test_a_failed_exit_page_carries_the_reason_too(monkeypatch):
    """An exit that reverts is money still at risk — it needs the why more than an entry does."""
    import sniper.services.memecoin_real_runner as RR
    fired = []
    monkeypatch.setattr(RR, "_safe_alert", lambda fn, *a: fired.append(a))
    RR._alert_exit_result("M", 1, "tp", {"status": "failed", "sig": "S",
                                         "reason": "chain_error:LiquidityInsufficient"})
    assert fired and fired[0][-1] == "chain_error:LiquidityInsufficient", fired


def test_the_alert_text_shows_the_reason():
    from sniper.services import memecoin_alerts as A
    sent = []
    A.alert_failed_tx("MINT", "SIGNATURE1234", "exceeds desired slippage limit",
                      send_fn=lambda text, **k: sent.append(text) or True)
    assert sent and "exceeds desired slippage limit" in sent[0]
    sent.clear()
    A.alert_failed_tx("MINT", "SIGNATURE1234", None,
                      send_fn=lambda text, **k: sent.append(text) or True)
    assert sent and "transaction failed" in sent[0] and "None" not in sent[0]
    assert "reservation released" not in sent[0]



def test_a_deploy_trip_inside_the_grace_does_not_page(tmp_path, spy):
    """2026-09-11: each deploy paged "KILL-SWITCH tripped" from the fresh runner."""
    kill = str(tmp_path / "k")
    memecoin_arming.trip_kill_switch(kill, source="runtime-deploy")
    state = {}
    assert R.check_kill_switch_alert(kill, False, state=state) is True
    assert R.check_kill_switch_alert(kill, True, state=state) is True
    memecoin_arming.clear_kill_switch(kill, source="runtime-deploy",
        expected_token=memecoin_arming.kill_switch_status(kill)["kill_token"])
    assert R.check_kill_switch_alert(kill, True, state=state) is False
    assert spy.get("alert_kill_switch", []) == []


def test_a_deploy_trip_that_outlives_the_grace_pages_once(tmp_path, spy, monkeypatch):
    import os, time
    kill = str(tmp_path / "k")
    memecoin_arming.trip_kill_switch(kill, source="runtime-deploy")
    state = {}
    R.check_kill_switch_alert(kill, False, state=state)
    assert spy.get("alert_kill_switch", []) == []
    old = time.time() - R._KILL_DEPLOY_GRACE_S - 5
    os.utime(kill, (old, old))
    R.check_kill_switch_alert(kill, True, state=state)
    R.check_kill_switch_alert(kill, True, state=state)
    assert len(spy.get("alert_kill_switch", [])) == 1


def test_an_operator_trip_pages_at_once(tmp_path, spy):
    kill = str(tmp_path / "k")
    memecoin_arming.trip_kill_switch(kill, source="web:127.0.0.1")
    R.check_kill_switch_alert(kill, False, state={})
    assert len(spy.get("alert_kill_switch", [])) == 1


def test_daily_loss_alert_follows_effective_limit_without_clearing_latch(spy, monkeypatch):
    c = _c()
    day = "2026-07-09"
    c.execute("INSERT INTO memecoin_daily_loss_trips(day, tripped_at) VALUES (?, ?)", (day, day))
    c.commit()
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)
    assert R.check_daily_loss_alert(c, day, False) is False
    assert spy.get("alert_daily_loss", []) == []
    assert m.is_daily_loss_tripped(c, day)
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 100.0)
    assert R.check_daily_loss_alert(c, day, False) is True
    assert len(spy["alert_daily_loss"]) == 1


def test_a_source_clock_ahead_of_ours_is_not_printed_as_a_negative_duration():
    """POOL ref f500c417 read "+-9.6s processed" (2026-09-18).

    The J7 relay's clock has run ahead of ours since 2026-09-17T21:00Z, so its stamp lands after
    our receipt and the subtraction goes negative. Elapsed time cannot be negative: printing one
    as a duration asks the operator to believe we acted before the event happened.
    """
    from sniper.services.memecoin_telegram import render_entry_notification as _render
    view = {
        "status": "refused", "ticker": "POOL", "network_id": "solana",
        "source_handle": "songz", "terminal_reason": "post_peak_confirmed",
        "source_timestamp_trust": "claimed_untrusted",
        "source_created_at": "2026-09-18T21:22:28.268000+00:00",
        "observed_at": "2026-09-18T21:22:18.628249+00:00",     # 9.6s BEFORE the source stamp
    }
    text = "\n".join(_render(view)) if isinstance(_render(view), list) else str(_render(view))
    assert "+-" not in text, "a negative interval reached the card as a duration"
    assert "-9.6s" not in text
    assert "source clock ahead" in text, "the card must name the clock that disagreed"
    assert "received unknown (source clock ahead by 9.6s)" in text
    assert "processed unknown" in text


def test_two_local_stamps_out_of_order_are_named_not_blamed_on_the_source():
    """Inherited review finding O3. Receipt and processed are OUR stamps. When they disagree the
    card must say which two stamps disagree and keep the order unknown, never print 'processed
    after receipt' as if that were observed, and never blame the source clock for it."""
    from sniper.services.memecoin_telegram import render_entry_notification as _render
    base = {"status": "refused", "ticker": "X", "network_id": "solana", "source_handle": "w",
            "terminal_reason": "x"}
    both = dict(base, source_created_at="2026-09-18T12:00:20+00:00",
                socket_received_at="2026-09-18T12:00:10+00:00",
                observed_at="2026-09-18T12:00:09+00:00")
    out = _render(both)
    text = "\n".join(out) if isinstance(out, list) else str(out)
    assert "processed after receipt" not in text
    assert "receipt" in text and "processed" in text and "disagree" in text
    local_only = dict(base, socket_received_at="2026-09-18T12:00:10+00:00",
                      observed_at="2026-09-18T12:00:09+00:00")
    out = _render(local_only)
    text = "\n".join(out) if isinstance(out, list) else str(out)
    assert "source clock ahead" not in text
    assert "disagree" in text and "-1.0s" not in text and "+-" not in text


def test_an_interval_that_is_zero_at_the_cards_resolution_is_not_a_disagreement():
    """The card prints tenths of a second. A fast lane stamps its decision start a few
    milliseconds before the detection row (177 of 1,654 attempts in the 24 h to 2026-09-30T14Z),
    and the card then read 'stamps disagree by 0.0s, order unknown'. Below the card's own
    resolution there is nothing to disagree about; from 0.05 s up the disagreement still prints."""
    from sniper.services.memecoin_telegram import render_entry_notification as _render

    def text(**stamps):
        out = _render(dict({"status": "refused", "ticker": "X", "network_id": "solana",
                            "source_handle": "w", "terminal_reason": "x"}, **stamps))
        return "\n".join(out) if isinstance(out, list) else str(out)

    local = text(observed_at="2026-09-30T12:00:10.030+00:00",
                 decision_started_at="2026-09-30T12:00:10.000+00:00")
    assert "+0.0s processed" in local and "disagree" not in local
    assert "processed unknown" not in local
    source = text(source_created_at="2026-09-30T12:00:10.020+00:00",
                  observed_at="2026-09-30T12:00:10.000+00:00")
    assert "+0.0s received" in source and "source clock ahead" not in source
    assert "processed unknown" in source
    result = text(observed_at="2026-09-30T12:00:10.010+00:00",
                  status_updated_at="2026-09-30T12:00:10.000+00:00")
    assert "0.0s processed to result" in result and "clocks disagree" not in result
    # One tenth or more is still a disagreement, on each of the three legs.
    local = text(observed_at="2026-09-30T12:00:10.060+00:00",
                 decision_started_at="2026-09-30T12:00:10.000+00:00")
    assert "receipt/processed stamps disagree by 0.1s, order unknown" in local
    source = text(source_created_at="2026-09-30T12:00:10.060+00:00",
                  observed_at="2026-09-30T12:00:10.000+00:00")
    assert "source clock ahead by 0.1s" in source
    result = text(observed_at="2026-09-30T12:00:10.060+00:00",
                  status_updated_at="2026-09-30T12:00:10.000+00:00")
    assert "result time unknown (clocks disagree)" in result


def test_a_sane_clock_still_reports_its_intervals():
    from sniper.services.memecoin_telegram import render_entry_notification as _render
    view = {
        "status": "refused", "ticker": "OK", "network_id": "solana", "source_handle": "w",
        "terminal_reason": "x", "source_created_at": "2026-09-18T21:22:00+00:00",
        "observed_at": "2026-09-18T21:22:02+00:00",
    }
    out = _render(view)
    text = "\n".join(out) if isinstance(out, list) else str(out)
    assert "+2.0s received" in text
    assert "processed unknown" in text and "+2.0s processed" not in text
    assert "source clock ahead" not in text
