"""Desk PAPER mode (operator 10-26, design B): no manual intent row, exact native fills on our tape at +1.5 s,
the runner's own rails, paper-only book gates, and no path that can load a key or send a transaction."""
import json
import sqlite3
from datetime import datetime

import pytest

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk as service
from sniper.services import memecoin_desk_paper as paper
from test_memecoin_desk_contract import candidate, evidence, policy, verdict
from test_memecoin_desk_pipeline import AT, POLICY, RESPONSE, api, databases, decide  # noqa: F401

@pytest.fixture(autouse=True)
def committed_tape(monkeypatch):
    """These fixtures write no universe heartbeat: the tape counts as committed through `now` (the frontier rule
    itself is pinned by tests/test_desk_paper_codex_frontier_witnesses.py, which does not use this)."""
    monkeypatch.setattr(paper, "_cut", lambda desk_dir, now, venue: now)


SCHEMA = ("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, "
          "quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, "
          "block_time INTEGER, received_at REAL, real_token INTEGER, fee_bps INTEGER, creator_fee_bps INTEGER, "
          "tx_index INTEGER, real_quote INTEGER, quote_mint TEXT, q_amount INTEGER, q_virtual INTEGER, q_real INTEGER, "
          "cashback_fee_bps INTEGER, cashback INTEGER, PRIMARY KEY(signature, ordinal)) WITHOUT ROWID")
VQ, VT = 40_000_000_000, 800_000_000_000_000       # 40 SOL virtual, 800M tokens virtual (6 decimals)


def tape(desk_dir, mint, rows):
    """rows = [(received_at, virtual_quote, virtual_token)]; one day file, no gaps."""
    for sub in ("swaps", "gaps"):
        (desk_dir / sub).mkdir(parents=True, exist_ok=True)
    day = paper._iso(rows[0][0])[:10]
    c = sqlite3.connect(desk_dir / "swaps" / ("%s.db" % day))
    c.execute(SCHEMA)
    for i, (t, vq, vt) in enumerate(rows):
        c.execute("INSERT INTO swaps(slot, signature, ordinal, mint, is_buy, virtual_quote, virtual_token, received_at, "
                  "real_token, fee_bps, creator_fee_bps, real_quote, quote_mint, cashback_fee_bps) VALUES "
                  "(?,?,0,?,1,?,?,?,?,95,5,?,?,0)", (i, "s%d" % i, mint, vq, vt, t, vt * 3 // 4,
                                                    vq * 3 // 4, "So11111111111111111111111111111111111111112"))
    c.commit()
    g = sqlite3.connect(desk_dir / "gaps" / ("%s.db" % day))
    g.execute("CREATE TABLE IF NOT EXISTS gaps(at REAL, cause TEXT, last_slot INTEGER)")
    g.commit()
    return g


@pytest.fixture
def paper_mode(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_EXECUTION", "paper")


def entered(databases):
    cohort = api().provision_cohort(*databases, producer="U", policy=POLICY, prompt_version="desk-judge-v18", created_at=AT)
    t = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]
    api().evaluate_candidate(databases[0], t, evidence=evidence(), policy=policy(), now_iso=AT)
    assert decide(databases, t)["action"] == "ENTER"
    return t


def test_paper_mode_writes_no_manual_intent_and_routes_to_the_paper_table(databases, paper_mode):
    t = entered(databases)
    out = service.dispatch_outbox(*databases, now_iso=RESPONSE)
    assert databases[1].execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 0
    assert out[0]["state"] == "refused" and out[0]["reason"] == "paper_routed"
    row = databases[0].execute("SELECT state, ticket_id FROM desk_paper_positions").fetchone()
    assert tuple(row) == ("pending_fill", t)


def test_real_mode_is_unchanged(databases):
    entered(databases)
    service.dispatch_outbox(*databases, now_iso=RESPONSE)
    assert databases[1].execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 1


def test_unknown_execution_mode_refuses():
    with pytest.raises(ValueError):
        paper.execution_mode({"MEMECOIN_DESK_EXECUTION": "papr"})


def _routed(databases):
    t = entered(databases)
    service.dispatch_outbox(*databases, now_iso=RESPONSE)
    return t, paper._ts(databases[0].execute("SELECT requested_at FROM desk_paper_positions").fetchone()[0])


def test_fill_is_the_exact_native_buy_at_the_decision_plus_land_delay(databases, paper_mode, tmp_path):
    from sniper.execution.pump_curve_buy import expected_tokens_out
    t, t0 = _routed(databases)
    mint = candidate()["mint"]
    tape(tmp_path / "desk", mint, [(t0 - 5, VQ, VT), (t0 + 1.0, VQ + 1_000_000_000, VT - 19_000_000_000_000)])
    assert paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 0.5, sol_usd=180.0, sol_at=t0) == []
    [r] = paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    row = dict(databases[0].execute("SELECT * FROM desk_paper_positions").fetchone())
    state, _ = paper.curve_state(tmp_path / "desk", mint, t0 + paper.ENTRY_DELAY_S)
    assert row["qty_atomic"] == str(expected_tokens_out(int(25 / 180.0 * 1e9), state))     # the +1.5 s state, not t0
    q1 = json.loads(row["quote1_json"])
    assert q1["tokens"] != row["qty_atomic"] and q1["at"] == t0


def test_a_gap_at_the_landing_moment_fails_the_entry_by_name(databases, paper_mode, tmp_path):
    t, t0 = _routed(databases)
    g = tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ, VT)])
    g.execute("INSERT INTO gaps VALUES (?, 'stream_reset', 1)", (t0 + 1.0,))
    g.commit()
    [r] = paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0)
    assert r["state"] == "failed" and r["refusal"] == "entry_failed:tape_gap"


def test_a_stale_fx_never_fills(databases, paper_mode, tmp_path):
    t, t0 = _routed(databases)
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ, VT)])
    [r] = paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 - 500)
    assert r["refusal"] == "entry_failed:sol_usd_stale"


def test_stop_exit_is_valued_by_the_exact_sell_at_the_exit_plus_land_delay(databases, paper_mode, tmp_path):
    t, t0 = _routed(databases)
    mint = candidate()["mint"]
    tape(tmp_path / "desk", mint, [(t0 - 5, VQ, VT), (t0 + 30, VQ // 2, VT * 2), (t0 + 31, VQ // 3, VT * 3)])
    paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    [r] = paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 30.5, sol_usd=180.0, sol_at=t0 + 30)
    assert r["state"] == "exit_requested" and r["reason"] in ("b_stop", "catastrophe")
    [c] = paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 32.1, sol_usd=180.0, sol_at=t0 + 30)
    row = dict(databases[0].execute("SELECT * FROM desk_paper_positions").fetchone())
    from sniper.execution.pump_native_exit import curve_sell_quote
    st, _ = paper.curve_state(tmp_path / "desk", mint, t0 + 30.5 + paper.EXIT_DELAY_S)
    q, _ = curve_sell_quote(st, int(row["qty_atomic"]), 50.0, protocol_bps=95, creator_bps=5)
    assert row["state"] == "closed" and row["exit_usd"] == pytest.approx((q["expected_out"] / 1e9 - 0.000105) * 180.0)
    assert row["pnl_usd"] < 0


def test_paper_book_gates_count_paper_positions_only(databases, paper_mode, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 1)
    _routed(databases)
    request = {"intent_id": "x", "requested_at": RESPONSE, "payload": {
        "mint": "Other1111111111111111111111111111111111111", "network_id": "solana", "size_usd": 25.0,
        "rails": {"tp_pct": 60, "sl_pct": 20, "trail_pct": None}, "desk_ticket_id": "desk:U:other",
        "desk_metadata": json.loads(databases[0].execute("SELECT desk_metadata_json FROM desk_paper_positions").fetchone()[0])}}
    assert paper.book_refusal(databases[0], request=request, now_iso=RESPONSE) == "desk_max_concurrent"


def test_no_path_can_load_a_key_or_send(databases, paper_mode, tmp_path, monkeypatch):
    """The no-spend proof: the engine module never names a signer, and a full routed cycle runs with every key loader and
    sender booby-trapped."""
    import inspect
    src = inspect.getsource(paper)
    for word in ("load_keypair", "send_transaction", "sendTransaction", "sign(", "Keypair", "manual_intents.enqueue"):
        assert word not in src, word
    from sniper.execution import solana_wallet
    def trap(*a, **k):
        raise AssertionError("a paper path touched a key or a sender")
    monkeypatch.setattr(solana_wallet, "load_keypair", trap)
    from sniper.services import memecoin_manual_intents as intents
    monkeypatch.setattr(intents, "enqueue", trap)
    t, t0 = _routed(databases)
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ, VT)])
    paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 3.0, sol_usd=180.0, sol_at=t0 + 2)


def test_paper_cards_are_headed_paper_and_sent_once(databases, paper_mode, tmp_path):
    t, t0 = _routed(databases)
    mint = candidate()["mint"]
    tape(tmp_path / "desk", mint, [(t0 - 5, VQ, VT), (t0 + 30, VQ // 2, VT * 2)])
    paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    sent = []
    send = lambda text, **kw: (sent.append((kw["kind"], text)), (True, 1.0))[1]           # noqa: E731
    paper.send_cards(databases[0], send_fn=send, now_iso=RESPONSE)
    paper.send_cards(databases[0], send_fn=send, now_iso=RESPONSE)
    assert [k for k, _ in sent] == ["desk_paper_entry"] and sent[0][1].startswith("📝 PAPER")
    paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 30.5, sol_usd=180.0, sol_at=t0 + 30)
    paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 32.1, sol_usd=180.0, sol_at=t0 + 30)
    paper.send_cards(databases[0], send_fn=send, now_iso=RESPONSE)
    assert [k for k, _ in sent] == ["desk_paper_entry", "desk_paper_result"]
    assert "PAPER — desk EXIT LOSS" in sent[1][1] and "paper PnL -" in sent[1][1]


def test_a_failed_send_is_retried(databases, paper_mode, tmp_path):
    t, t0 = _routed(databases)
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ, VT)])
    paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    assert paper.send_cards(databases[0], send_fn=lambda text, **kw: (False, 0.0), now_iso=RESPONSE) == []
    assert paper.send_cards(databases[0], send_fn=lambda text, **kw: (True, 0.0), now_iso=RESPONSE) == [(t, "entry")]


def test_paper_api_shows_only_paper_rows(databases, paper_mode, tmp_path):
    from sniper.routes.api_memecoin_desk import paper_positions
    t, t0 = _routed(databases)
    path = databases[0].execute("PRAGMA database_list").fetchone()[2]
    body = paper_positions(path)
    assert body["available"] and body["paper"] and [r["ticket_id"] for r in body["positions"]] == [t]


class _Cache:
    def __init__(self, fact):
        self.fact, self.calls = fact, []

    def lookup(self, *, mint, pool, now):
        self.calls.append((mint, pool))
        return self.fact


@pytest.mark.parametrize("mode", ["real", "paper"])
def test_a_dump_group_kill_refuses_at_the_final_courier_step(databases, monkeypatch, mode):
    monkeypatch.setenv("MEMECOIN_DESK_EXECUTION", mode)
    entered(databases)
    cache = _Cache({"status": "kill", "reason": "dump_group_buyers"})
    [r] = service.dispatch_outbox(*databases, now_iso=RESPONSE, dump_cache=cache)
    assert r["state"] == "refused" and r["reason"] == "dump_group_buyers" and cache.calls
    assert databases[1].execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 0


def test_an_unknown_dump_group_fact_is_logged_not_refused(databases):
    entered(databases)
    [r] = service.dispatch_outbox(*databases, now_iso=RESPONSE, dump_cache=_Cache({"status": "unknown", "reason": "x"}))
    assert r["state"] == "sent" and r["dump_group"]["status"] == "unknown"


def test_a_coin_under_the_desk_age_floor_is_refused_like_real(databases, paper_mode, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_DESK_MIN_COIN_AGE_S", "100000")
    t, t0 = _routed(databases)
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ, VT)])
    young = {"snapshot": {"fields": {"age_s": {"value": 20.0, "known_at": t0, "completeness": "complete"}}}}
    [r] = paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1,
                             candidate_fn=lambda _t: young)
    assert r["state"] == "refused" and r["refusal"].startswith("coin_too_young:")


def test_a_fill_over_the_verdict_cap_is_refused_with_the_executors_reason(databases, paper_mode, tmp_path):
    t, t0 = _routed(databases)
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ * 8, VT)])             # price far above the sheet's
    [r] = paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    assert r["state"] == "refused"
    assert r["refusal"].startswith("desk verdict price: exact quote is +") and "over the verdict price (cap" in r["refusal"]


def test_unknown_execution_cost_is_labelled_never_zero(databases, paper_mode, tmp_path, monkeypatch):
    monkeypatch.setitem(paper.EXEC_COST_SOL, "entry", None)
    monkeypatch.setitem(paper.EXEC_COST_SOL, "exit", None)
    t, t0 = _routed(databases)
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ, VT), (t0 + 30, VQ // 2, VT * 2)])
    paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 30.5, sol_usd=180.0, sol_at=t0 + 30)
    paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 32.1, sol_usd=180.0, sol_at=t0 + 30)
    row = dict(databases[0].execute("SELECT * FROM desk_paper_positions").fetchone())
    assert json.loads(row["fill_json"])["exec_cost"] == "unknown"
    assert json.loads(row["exit_json"])["pnl_basis"] == "before network/priority costs"
    assert "(before network/priority costs)" in paper.end_card(row)


def test_measured_costs_are_charged_on_both_legs(databases, paper_mode, tmp_path):
    t, t0 = _routed(databases)
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ, VT), (t0 + 30, VQ // 2, VT * 2)])
    paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    row = dict(databases[0].execute("SELECT * FROM desk_paper_positions").fetchone())
    assert row["entry_cost_usd"] == pytest.approx(25.0 + 0.001899 * 180.0)
    paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 30.5, sol_usd=180.0, sol_at=t0 + 30)
    paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 32.1, sol_usd=180.0, sol_at=t0 + 30)
    row = dict(databases[0].execute("SELECT * FROM desk_paper_positions").fetchone())
    assert json.loads(row["exit_json"])["pnl_basis"] == paper.PNL_BASIS and paper.PNL_BASIS in paper.end_card(row)


def test_an_absent_dump_gate_is_a_typed_unknown_on_the_ticket(databases):
    t = entered(databases)
    service.dispatch_outbox(*databases, now_iso=RESPONSE)
    gate = [s for s in store.get_ticket(databases[0], t)["stages"] if s["stage"] == "dump_gate"][0]
    assert gate["output"] == {"status": "unknown", "reason": "dump_group_unknown", "detail": "cache_absent"}


def test_the_engine_process_opens_no_socket_key_or_ledger(tmp_path):
    """Codex (6): import the paper engine and the runner's pure rail function, then run a full engine pass, in a
    fresh process whose sockets, key loader and ledger opens are trapped."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = r'''
import builtins, socket, sqlite3, sys
sys.path.insert(0, %(root)r)
opened = []
def no_socket(*a, **k):
    raise AssertionError("socket opened")
socket.socket.connect = no_socket
socket.create_connection = no_socket
real_open, real_connect = builtins.open, sqlite3.connect
def watch_open(path, *a, **k):
    opened.append(str(path))
    return real_open(path, *a, **k)
builtins.open = watch_open
def watch_connect(path, *a, **k):
    if "sniper_tracker" in str(path) or "ledger" in str(path):
        raise AssertionError("ledger opened: %%s" %% path)
    return real_connect(path, *a, **k)
sqlite3.connect = watch_connect
from sniper.execution import solana_wallet
def no_key(*a, **k):
    raise AssertionError("key loaded")
solana_wallet.load_keypair = no_key
import importlib.util
spec = importlib.util.spec_from_file_location("eng", %(engine)r)
eng = importlib.util.module_from_spec(spec); spec.loader.exec_module(eng)
from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason
conn = sqlite3.connect(%(db)r); conn.row_factory = sqlite3.Row
eng.run_once(conn, %(desk)r)
bad = [p for p in opened if any(w in p.lower() for w in ("keypair", "secret", "wallet.json", ".key"))]
assert not bad, bad
print("OK")
''' % {"root": str(root), "engine": str(root / "scripts" / "memecoin_desk_paper_engine.py"),
       "db": str(tmp_path / "desk.db"), "desk": str(tmp_path / "desk")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         env={**__import__("os").environ, "SNIPER_STATE_ROOT": str(tmp_path)})
    assert out.returncode == 0 and out.stdout.strip().endswith("OK"), out.stderr[-2000:]


def test_a_paper_skipped_chase_opens_the_chase_order_like_real(databases, monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_DESK_EXECUTION", "paper")
    pol = {**POLICY, "chase_limit": {"version": "chase-limit-v1", "order_max_s": 7200, "book_max": 50,
                                     "graduation_guard_pct": 97.0}}
    cohort = api().provision_cohort(*databases, producer="U", policy=pol, prompt_version="desk-judge-v18", created_at=AT)
    t = api().ingest_candidate(databases[0], candidate(), cohort_id=cohort, now_iso=AT)["ticket_id"]   # fixture: arm B
    api().evaluate_candidate(databases[0], t, evidence=evidence(), policy=policy(), now_iso=AT)
    decide(databases, t, verdict(k=t), asof={"candidate": candidate(), "meta": {"rank": {"price_now": 3e-8}}})
    service.dispatch_outbox(*databases, now_iso=RESPONSE)
    t0 = paper._ts(databases[0].execute("SELECT requested_at FROM desk_paper_positions").fetchone()[0])
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ * 8, VT)])               # above the cap
    out = paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    assert any(r.get("step") == "paper_chase" and r.get("state") == "limit_watching" for r in out), out
    child = store.get_ticket(databases[0], t + ":limit")
    trig = [s for s in child["stages"] if s["stage"] == "vet"][0]["inputs"]["trigger"]
    assert trig["order"] == "standing-v2" and trig["order_mode"] == "chase_limit"


def _open_paper(databases, tmp_path):
    t, t0 = _routed(databases)
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ, VT)])
    paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 3.0, sol_usd=180.0, sol_at=t0 + 2)
    return t, t0


def _decisions(desk_dir, rows):
    c = sqlite3.connect(desk_dir / paper.RISK_OUT_DB)
    c.execute("CREATE TABLE risk_decisions(id INTEGER PRIMARY KEY, position_id INTEGER, action TEXT, rails_json TEXT, "
              "decided_at REAL, expires_at REAL, live INTEGER)")
    c.executemany("INSERT INTO risk_decisions(position_id, action, rails_json, decided_at, expires_at, live) VALUES (?,?,?,?,?,1)",
                  rows)
    c.commit()


def test_risk_positions_feed_the_watcher_shape(databases, paper_mode, tmp_path):
    from sniper.services import memecoin_desk_risk as R
    t, t0 = _open_paper(databases, tmp_path)
    path = databases[0].execute("PRAGMA database_list").fetchone()[2]
    [(pos, policy, rails, mark)] = paper.risk_positions(path)
    f = R.facts(pos, policy, mark=mark, bars=[], boundary="cycle", now=t0 + 4)
    assert f["k"] == "pos:%d" % pos["id"] and f["pnl_pct_exact"] is not None and f["rails"]["sl_pct"] == 20


def test_a_paper_risk_exit_closes_like_a_real_risk_exit(databases, paper_mode, tmp_path):
    t, t0 = _open_paper(databases, tmp_path)
    pid = databases[0].execute("SELECT id FROM desk_paper_positions").fetchone()[0]
    _decisions(tmp_path / "desk", [(pid, "EXIT", None, t0 + 3.5, t0 + 60),
                                   (pid, "TIGHTEN", json.dumps({"sl_pct": 30}), t0 + 3.5, t0 + 60)])   # looser: ignored
    exits = paper.risk_orders(databases[0], desk_dir=tmp_path / "desk", now=t0 + 4)
    assert exits == {t: "desk_risk_exit"}
    # Codex P1: a crash right here (no tick) loses nothing: the request is already on the position
    row = dict(databases[0].execute("SELECT * FROM desk_paper_positions").fetchone())
    assert row["exit_reason"] == "desk_risk_exit" and row["exit_requested_at"] == paper._iso(t0 + 4)
    assert paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 5.0, sol_usd=180.0, sol_at=t0 + 3) == []   # < 1.5 s
    [r] = paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 5.6, sol_usd=180.0, sol_at=t0 + 3)
    assert r["state"] == "closed" and r["reason"] == "desk_risk_exit"
    ex = json.loads(databases[0].execute("SELECT exit_json FROM desk_paper_positions").fetchone()[0])
    assert ex["delay_kind"] == "risk" and ex["exit_delay_s"] == pytest.approx(1.5)      # the request + 1.5 s
    acks = dict(databases[0].execute("SELECT action, reason FROM desk_paper_risk_applies").fetchall())
    assert acks == {"EXIT": None, "TIGHTEN": "exit_already_requested"}     # nothing tightens a position already exiting
    assert paper.risk_orders(databases[0], desk_dir=tmp_path / "desk", now=t0 + 5) == {}      # once


def test_a_tighten_applies_stricter_rails_and_an_expired_order_is_refused(databases, paper_mode, tmp_path):
    t, t0 = _open_paper(databases, tmp_path)
    pid = databases[0].execute("SELECT id FROM desk_paper_positions").fetchone()[0]
    _decisions(tmp_path / "desk", [(pid, "TIGHTEN", json.dumps({"sl_pct": 10}), t0 + 3.5, t0 + 60),
                                   (pid, "EXIT", None, t0 - 100, t0 - 40)])
    assert paper.risk_orders(databases[0], desk_dir=tmp_path / "desk", now=t0 + 4) == {}
    assert json.loads(databases[0].execute("SELECT rails_json FROM desk_paper_positions").fetchone()[0])["sl_pct"] == 10
    assert dict(databases[0].execute("SELECT action, reason FROM desk_paper_risk_applies").fetchall())["EXIT"] == "decision_expired"



def test_quantity_is_exact_decimal_text_beyond_int64(databases, paper_mode):
    """Codex P1: qty_atomic is canonical decimal TEXT end to end; no int64 overflow, no float."""
    big = 2 ** 64 + 12345
    paper.ensure(databases[0])
    databases[0].execute("INSERT INTO desk_paper_positions(ticket_id, intent_id, mint, network_id, cohort_id, version, state, "
                         "requested_at, size_usd, rails_json, desk_metadata_json, qty_atomic) VALUES "
                         "('t','i','m','solana','c','v','open',?,25,'{}','{}',?)", (RESPONSE, str(big)))
    row = databases[0].execute("SELECT * FROM desk_paper_positions WHERE ticket_id='t'").fetchone()
    assert paper.qty_of(row) == big
    for bad in ("1e20", "007", 5, "-1"):
        with pytest.raises(ValueError):
            paper.qty_of({"qty_atomic": bad})


def test_fx_is_causal_never_a_later_quote_for_an_earlier_event():
    h = paper.FxHistory()
    h.add(180.0, 100.0)
    h.add(200.0, 110.0)
    assert h.at(105.0) == 180.0 and h.at(110.0) == 200.0 and h.at(99.0) is None
    assert h.at(110.0 + paper.MIN_SOL_USD_AGE_S + 1) is None                  # too old


def test_an_unsellable_exit_retries_at_a_later_tick_and_logs_its_delay(databases, paper_mode, tmp_path):
    t, t0 = _routed(databases)
    mint = candidate()["mint"]
    g = tape(tmp_path / "desk", mint, [(t0 - 5, VQ, VT), (t0 + 30, VQ // 2, VT * 2)])
    paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=t0 + 2.0, sol_usd=180.0, sol_at=t0 + 1)
    paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 30.5, sol_usd=180.0, sol_at=t0 + 30)
    g.execute("INSERT INTO gaps VALUES (?, 'stream_reset', 1)", (t0 + 31.2,))       # the exit moment (30.5 + 1.0) is unknown
    g.commit()
    [r] = paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 31.6, sol_usd=180.0, sol_at=t0 + 31)
    assert r["state"] == "exit_retry" and r["reason"] == "tape_gap"
    tape_rows = sqlite3.connect(tmp_path / "desk" / "swaps" / ("%s.db" % paper._iso(t0)[:10]))
    tape_rows.execute("INSERT INTO swaps(slot, signature, ordinal, mint, is_buy, virtual_quote, virtual_token, received_at, "
                      "real_token, fee_bps, creator_fee_bps, real_quote, quote_mint, cashback_fee_bps) VALUES "
                      "(99,'late',0,?,0,?,?,?,?,95,5,?,'So11111111111111111111111111111111111111112',0)",
                      (mint, VQ // 2, VT * 2, t0 + 33.0, VT * 3 // 2, VQ * 3 // 8))
    tape_rows.commit()
    [c] = paper.tick(databases[0], desk_dir=tmp_path / "desk", now=t0 + 34.0, sol_usd=180.0, sol_at=t0 + 33.5)
    row = dict(databases[0].execute("SELECT * FROM desk_paper_positions").fetchone())
    ex = json.loads(row["exit_json"])
    assert c["state"] == "closed" and ex["at"] == t0 + 34.0 and ex["attempts"][0]["reason"] == "tape_gap"
    assert ex["exit_delay_s"] == pytest.approx(3.5)


def test_an_entry_the_tape_never_commits_is_censored_not_filled(databases, paper_mode, tmp_path, monkeypatch):
    _, t0 = _routed(databases)
    tape(tmp_path / "desk", candidate()["mint"], [(t0 - 5, VQ, VT)]).close()
    monkeypatch.setattr(paper, "_cut", lambda desk_dir, now, venue: None)        # no committed cut at all
    fill_at = t0 + paper.ENTRY_DELAY_S
    assert paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=fill_at + 1, sol_usd=180.0, sol_at=t0) == []
    [r] = paper.fill_pending(databases[0], desk_dir=tmp_path / "desk", now=fill_at + paper.FRONTIER_WAIT_S + 1,
                             sol_usd=180.0, sol_at=t0)
    assert r["state"] == "failed" and r["refusal"] == "entry_failed:capture_unproven"
