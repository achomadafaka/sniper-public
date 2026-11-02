"""Only the runner may freeze a funded contract.

The web server captures its settings at import and deploys never restart it, so a contract built
there carries whatever config the process started with. Position 44706 opened at 15:01 under
`follow-30-50-v1` / 780s while same-minute feed entries froze v2 / 14400 / 60 — the timecap lift
had activated at 14:33. `build_funded_contract` already had ONE call site: a single construction
POINT is not a single construction PROCESS.
"""
from __future__ import annotations

import ast
import math
import pathlib
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_intents as mi
from _source_guard import code_of

T0 = "2026-08-26T15:00:00+00:00"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    return c


def _at(sec):
    return "2026-08-26T15:%02d:%02d+00:00" % (sec // 60, sec % 60)


# ------------------------------------------------------------------ the property

def test_no_route_module_can_build_a_funded_contract():
    """THE PROPERTY. Everything else is mechanism. Same guard shape as the versioned-policy-id
    check: state the invariant where a future edit trips over it."""
    for path in pathlib.Path("sniper/routes").glob("*.py"):
        body = path.read_text()
        for forbidden in ("build_funded_contract", "prepare_funded_contract"):
            assert forbidden not in body, f"{path.name} must not build funded contracts"


def test_the_web_server_binds_the_enqueue_function_not_the_executor():
    body = pathlib.Path("sniper/web/server.py").read_text()
    assert "make_enqueue_buy_fn" in body
    assert "make_real_buy_fn" not in body, \
        "the web importing the executor is how the two-writer divergence comes back"


def test_the_runner_is_the_process_that_drains_intents():
    body = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    assert "memecoin_manual_intents import drain" in body
    assert "make_real_buy_fn(contexts)" in body


def test_the_drain_runs_after_the_exit_sweep():
    """An operator's entry may never delay a sell."""
    body = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    assert body.index("run_slow_exit_sweep") < body.index("manual intent drain failed")


def test_the_drain_is_not_gated_by_the_fifteen_second_bracket_sweep():
    """A manual request must reach the runner on each main cycle, not each bracket cycle."""
    tree = ast.parse(code_of(pathlib.Path("scripts/memecoin_runner_loop.py")))
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_mi_drain"]
    assert len(calls) == 1
    node = calls[0]
    while node in parent:
        node = parent[node]
        if isinstance(node, ast.If):
            assert "last_bracket" not in ast.unparse(node.test), (
                "manual intent drain still waits for the 15-second bracket sweep")


def test_an_intent_never_carries_a_contract_decision():
    """If the web could name a POLICY the divergence returns wearing a different hat.

    Narrowed 2026-08-27. Operator exit rails are now allowed (see the next two tests); a contract
    field still is not. The line is who produced the value: the operator typing 60 cannot be stale,
    the web reading cfg.MEMECOIN_TP_PCT can.
    """
    conn = _conn()
    mi.enqueue(conn, mint="MINT", now_iso=T0, size_usd=40.0)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_manual_intents)")}
    for decision in ("policy_id", "max_hold_s", "stop_pct", "clip_usd", "coin_band",
                     "voldeath_exit_s", "policy_version", "funded_contract_json"):
        assert decision not in columns, f"an intent must not carry {decision}"


def test_percent_intent_keeps_input_and_resolved_dollars_separate():
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, size_pct_equity=0.13)
    row = mi.read_status(conn, iid)
    assert row["size_pct_equity"] == 0.13
    assert row["size_usd"] is None
    assert row["resolved_size_usd"] is None


def test_an_intent_rejects_two_size_authorities():
    conn = _conn()
    with pytest.raises(ValueError, match="one size"):
        mi.enqueue(conn, mint="MINT", now_iso=T0, size_usd=20.0, size_pct_equity=0.13)


def test_an_intent_rejects_a_nonfinite_dollar_size():
    conn = _conn()
    with pytest.raises(ValueError, match="size_usd"):
        mi.enqueue(conn, mint="MINT", now_iso=T0, size_usd=math.inf)


@pytest.mark.parametrize("value", [True, math.nan, math.inf])
def test_an_intent_rejects_an_invalid_percent(value):
    conn = _conn()
    with pytest.raises(ValueError, match="size_pct_equity"):
        mi.enqueue(conn, mint="MINT", now_iso=T0, size_pct_equity=value)


def test_a_blank_rail_is_null_so_the_runner_decides_it():
    """The web must send absence, not a default. A defaulted rail IS the 44706 divergence."""
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, size_usd=40.0)
    row = mi.read_status(conn, iid)
    for field in mi.RAIL_FIELDS:
        assert row[field] is None, f"{field} must be NULL when the operator did not type one"
    assert row["rails_mode"] is None


def test_the_web_never_sources_a_rail_default_from_its_own_config():
    """The guard that keeps the refinement honest: rails may cross, cfg-derived values may not."""
    body = pathlib.Path("sniper/routes/api_memecoin.py").read_text().splitlines()
    inside = False
    for line in body:
        stripped = line.strip()
        if stripped.startswith("def real_buy("):
            inside = True
            continue
        if inside and stripped.startswith("@bp."):
            break
        if not inside or stripped.startswith("#"):
            continue
        for field in mi.RAIL_FIELDS:
            assert not (field in stripped and ("cfg." in stripped or "web_settings" in stripped)), \
                f"/real/buy must not default {field} from config: {stripped!r}"


def test_custom_mode_switches_off_every_rail_the_operator_did_not_name():
    """The operator's chosen semantics: what you set is what runs, and nothing else."""
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, size_usd=40.0,
                     rails={"tp_pct": 60.0}, rails_mode=mi.CUSTOM_RAILS)
    seen = {}
    mi.apply_rails(conn, mi.read_status(conn, iid), 4242, now_iso=T0,
                   set_override_fn=lambda c, pid, src, **kw: seen.update(kw) or 1)
    assert seen["tp_pct"] == 60.0
    for off in ("sl_pct", "trail_pct", "derisk_pct"):
        assert seen[off] == 0.0, f"custom mode must switch {off} OFF, not leave it defaulted"
    for absent in ("tp_target_mcap", "sl_target_mcap"):
        assert seen[absent] is None, "0 is not a market cap"


def test_default_mode_writes_no_override_at_all():
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, size_usd=40.0)
    called = []
    out = mi.apply_rails(conn, mi.read_status(conn, iid), 4242, now_iso=T0,
                         set_override_fn=lambda *a, **k: called.append(1) or 1)
    assert out is None and not called, "the runner's own policy must not be overridden"


def test_an_unknown_rail_field_is_refused_loudly():
    """A silently dropped rail is an operator who believes a stop is armed."""
    conn = _conn()
    with pytest.raises(ValueError, match="unknown rail"):
        mi.enqueue(conn, mint="MINT", now_iso=T0, rails={"stop_pct": 35.0})


# ------------------------------------------------------------------ the queue

def test_enqueue_records_the_request_and_executes_nothing():
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, network_id="solana", size_usd=40.0)
    row = mi.read_status(conn, iid)
    assert row["state"] == mi.QUEUED and row["position_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


def test_two_runner_passes_cannot_claim_the_same_intent():
    conn = _conn()
    mi.enqueue(conn, mint="MINT", now_iso=T0)
    first = mi.claim_next(conn, now_iso=T0)
    second = mi.claim_next(conn, now_iso=T0)
    assert first is not None and second is None


def test_claims_are_taken_oldest_first():
    conn = _conn()
    mi.enqueue(conn, mint="OLD", now_iso=_at(0))
    mi.enqueue(conn, mint="NEW", now_iso=_at(30))
    assert mi.claim_next(conn, now_iso=_at(31))["mint"] == "OLD"


def test_a_resolved_intent_is_immutable():
    """A retry may re-run an intent. It may not revise an observation."""
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0)
    assert mi.resolve(conn, iid, state=mi.FILLED, now_iso=T0, position_id=1) is True
    assert mi.resolve(conn, iid, state=mi.REFUSED, now_iso=T0, reason="nope") is False
    assert mi.read_status(conn, iid)["position_id"] == 1


def test_a_non_terminal_resolution_is_refused():
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0)
    with pytest.raises(ValueError):
        mi.resolve(conn, iid, state=mi.QUEUED, now_iso=T0)


# ------------------------------------------------------------------ nothing disappears

def test_an_unclaimed_intent_expires_visibly_rather_than_vanishing():
    """The failure mode this design INTRODUCES, so it gets a named state and a reason."""
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, ttl_s=120.0)
    assert mi.expire_stale(conn, now_iso=_at(60))["expired"] == 0
    assert mi.expire_stale(conn, now_iso=_at(200))["expired"] == 1
    row = mi.read_status(conn, iid)
    assert row["state"] == mi.EXPIRED and "expired" in row["reason"]


def test_a_claim_held_by_a_dead_runner_returns_to_the_queue():
    """The operator's request outlives one process."""
    conn = _conn()
    mi.enqueue(conn, mint="MINT", now_iso=T0, ttl_s=86400.0)
    mi.claim_next(conn, now_iso=T0)
    assert mi.claim_next(conn, now_iso=_at(10)) is None
    assert mi.expire_stale(conn, now_iso=_at(200))["released"] == 1
    assert mi.claim_next(conn, now_iso=_at(201))["mint"] == "MINT"


def test_expiry_never_touches_an_already_terminal_intent():
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, ttl_s=1.0)
    mi.resolve(conn, iid, state=mi.FILLED, now_iso=T0, position_id=7)
    mi.expire_stale(conn, now_iso=_at(600))
    assert mi.read_status(conn, iid)["state"] == mi.FILLED


# ------------------------------------------------------------------ the web side

def test_the_enqueue_fn_reports_queued_not_filled():
    """The routes report ok = status == 'filled'. A queued intent must not claim a fill."""
    conn = _conn()
    fn = mi.make_enqueue_buy_fn(lambda: T0)
    out = fn(conn, "MINT", "uid", ticker="TKR", network_id="solana", size_usd=40.0)
    assert out["status"] == "queued" and out["intent_id"]
    row = mi.read_status(conn, out["intent_id"])
    assert (row["mint"], row["network_id"], row["size_usd"]) == ("MINT", "solana", 40.0)


def test_the_enqueue_fn_never_raises_into_a_route():
    class Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("db gone")

    out = mi.make_enqueue_buy_fn(lambda: T0)(Broken(), "MINT")
    assert out["status"] == "error" and "queue" in out["reason"]


# ------------------------------------------------------------------ the runner side

def test_the_drain_executes_with_the_runners_own_buy_fn():
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, size_usd=40.0, network_id="solana")
    seen = {}

    def buy_fn(c, mint, uid, *, ticker=None, network_id=None, size_usd=None):
        seen.update(mint=mint, network_id=network_id, size_usd=size_usd)
        return {"status": "filled", "position_id": 44707, "attempt_id": "a1"}

    assert mi.drain(conn, buy_fn, now_fn=lambda: T0) == [(iid, mi.FILLED)]
    assert seen == {"mint": "MINT", "network_id": "solana", "size_usd": 40.0}
    row = mi.read_status(conn, iid)
    assert row["state"] == mi.FILLED and row["position_id"] == 44707


def test_the_drain_persists_the_runners_resolved_dollars():
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0, size_pct_equity=0.26,
                     origin_event_id="origin-1")
    seen = {}

    def buy_fn(c, mint, uid, *, ticker=None, network_id=None, size_pct_equity=None,
               origin_event_id=None):
        seen["size_pct_equity"] = size_pct_equity
        seen["origin_event_id"] = origin_event_id
        return {"status": "filled", "position_id": 44708,
                "resolved_size_usd": 52.0}

    mi.drain(conn, buy_fn, now_fn=lambda: T0)
    assert seen == {"size_pct_equity": 0.26, "origin_event_id": "origin-1"}
    assert mi.read_status(conn, iid)["resolved_size_usd"] == 52.0


def test_a_refusal_reason_reaches_the_row_the_ui_reads():
    """The operator learned 'insufficient bankroll' from a Telegram alert, not from the button."""
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0)
    mi.drain(conn, lambda *a, **k: {"status": "refused", "reason": "m5 volume under floor"},
             now_fn=lambda: T0)
    row = mi.read_status(conn, iid)
    assert row["state"] == mi.REFUSED and row["reason"] == "m5 volume under floor"


def test_an_executor_exception_preserves_unresolved_execution():
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=T0)

    def boom(*a, **k):
        raise RuntimeError("rpc exploded")

    assert mi.drain(conn, boom, now_fn=lambda: T0) == [(iid, mi.UNRESOLVED)]
    assert "rpc exploded" in mi.read_status(conn, iid)["reason"]


def test_the_drain_is_bounded_so_a_backlog_cannot_starve_the_exit_loop():
    conn = _conn()
    for i in range(6):
        mi.enqueue(conn, mint="M%d" % i, now_iso=T0)
    assert len(mi.drain(conn, lambda *a, **k: {"status": "filled"},
                        now_fn=lambda: T0, max_per_pass=2)) == 2
    assert mi.pending_count(conn) == 4


def test_the_drain_expires_stale_intents_before_claiming():
    conn = _conn()
    iid = mi.enqueue(conn, mint="OLD", now_iso=T0, ttl_s=10.0)
    calls = []
    mi.drain(conn, lambda *a, **k: calls.append(1) or {"status": "filled"},
             now_fn=lambda: _at(300))
    assert calls == [], "an expired intent must never be filled late"
    assert mi.read_status(conn, iid)["state"] == mi.EXPIRED


# ----------------------------------------------------------------- TTL survives a dead claimer

def test_a_stale_claim_past_its_ttl_expires_instead_of_returning_to_the_queue():
    """Regression: the release swept every stale claim back to `queued` without reading
    `expires_at`, and `drain` calls the sweep immediately before `claim_next`, so an operator
    request could execute long after its TTL. Measured before the fix: a 120s intent released and
    reclaimed at 200s."""
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=_at(0), size_usd=40.0, ttl_s=120.0)
    assert mi.claim_next(conn, now_iso=_at(1))["intent_id"] == iid
    out = mi.expire_stale(conn, now_iso=_at(200))      # stale (>90s) AND past the 120s TTL
    assert out["released"] == 0, "a dead intent must not go back to the queue"
    assert out["expired"] == 1
    assert mi.read_status(conn, iid)["state"] == mi.EXPIRED
    assert mi.claim_next(conn, now_iso=_at(201)) is None


def test_a_stale_claim_still_inside_its_ttl_is_released_for_another_runner():
    conn = _conn()
    iid = mi.enqueue(conn, mint="MINT", now_iso=_at(0), size_usd=40.0, ttl_s=300.0)
    mi.claim_next(conn, now_iso=_at(1))
    out = mi.expire_stale(conn, now_iso=_at(120))      # 119s stale, still inside a 300s TTL
    assert out["released"] == 1 and out["expired"] == 0
    assert mi.read_status(conn, iid)["state"] == mi.QUEUED


def test_claim_next_refuses_an_expired_row_even_without_a_sweep():
    """The TTL is a local invariant of the claim, not a property of whatever swept last."""
    conn = _conn()
    mi.enqueue(conn, mint="MINT", now_iso=_at(0), size_usd=40.0, ttl_s=120.0)
    assert mi.claim_next(conn, now_iso=_at(300)) is None
