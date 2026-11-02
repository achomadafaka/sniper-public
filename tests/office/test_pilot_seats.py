"""Office pilot money-side seats (Alpha assignment 2026-10-28, Codex T15 call shapes): the always-on paper watcher,
deterministic Risk (veto or full clip, never a size cut) and the deterministic Trader (one adjudicated bundle -> one
idempotent ledger.submit), plus the discovery-only pilot bootstrap."""
import dataclasses
import importlib.util
import json
from pathlib import Path

import pytest

from sniper.office import ledger as L
from sniper.office import risk as R
from sniper.office import trader as T
from sniper.office.supervisor import Context

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0
FX = 150.0
MINT = "Mint1111111111111111111111111111111111111pump"


def policy(**over):
    return L.OfficeBookPolicy(**{**json.loads((ROOT / "config/office/book_policy_pilot.json").read_text())["policy"],
                                 "chains": ("solana",), **over})


def safe(**over):
    return {"authority_mint": "closed", "authority_freeze": "closed", "sell_probe": "ok", "sells_exist_60s": True,
            "cluster_hit": None, "lp_locked_or_burned": None, "top10_pct": 20.0, "creator_pct": 1.0, **over}


def bundle(**over):
    return {"decision": "ENTER", "decision_id": "d1", "network_id": "solana", "mint": MINT, "pool": "P",
            "bundle_hash": "b" * 64, **over}


def ctx(**over):
    return Context(**{"actor_id": "trader-1", "role": "trader", "book_id": "PILOT", "generation": 1,
                      "lease_generation": 1, "decision_id": "d1", "turn_id": "t1", "policy_hash": "p", "prompt_hash": "h",
                      "cut": {}, "deadline_at": NOW + 60, **over})


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / "ledger.db")
    T.bootstrap_pilot(conn, epoch_id="PILOT-E1", book_id="PILOT", clip_usd=policy().clip_usd, rule_version="none",
                      capital_usd=1000.0, actor="operator", now=NOW - 10)
    yield conn
    conn.close()


# ---- the pilot policy file

def test_the_pilot_policy_is_valid_frozen_and_uses_cohort_d_clip_and_bounds():
    doc = json.loads((ROOT / "config/office/book_policy_pilot.json").read_text())
    assert doc["label"] == "pilot" and doc["evaluation"] is False
    p = policy()
    assert L.policy_refusal(p) is None and p.clip_usd == 25.0
    assert p.rail_bounds == {"sl_pct": [10.0, 40.0], "tp_pct": [15.0, 150.0], "trail_pct": [8.0, 30.0]}


# ---- pilot bootstrap

def test_bootstrap_is_discovery_only_and_idempotent(book):
    T.bootstrap_pilot(book, epoch_id="PILOT-E1", book_id="PILOT", clip_usd=25.0, rule_version="none",
                      capital_usd=1000.0, actor="operator", now=NOW)               # replay: no second effect
    assert [tuple(r) for r in book.execute("SELECT kind FROM books")] == [("discovery",)]
    assert L.capital(book, "PILOT") == 1000.0
    kw = dict(now=NOW, mark_max_age_s=30.0, sol_usd=FX, sol_at=NOW)
    assert L.equity(book, epoch_id="PILOT-E1", **kw).cash == 0.0                     # evaluated scope stays empty
    with pytest.raises(ValueError, match="pilot_bootstrap_conflict"):
        T.bootstrap_pilot(book, epoch_id="PILOT-E1", book_id="PILOT", clip_usd=30.0, rule_version="none",
                          capital_usd=1000.0, actor="operator", now=NOW)


# ---- Risk

def test_risk_allows_only_the_full_frozen_clip():
    r = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe())
    assert (r.decision, r.reason, r.clip_usd) == ("allow", None, 25.0) and r.risk_version == R.RISK_VERSION
    assert {f.name for f in dataclasses.fields(r)} == {"decision", "reason", "clip_usd", "risk_version", "bundle_hash",
                                                       "decision_id", "policy_hash"}
    assert (r.bundle_hash, r.decision_id) == ("b" * 64, "d1")


@pytest.mark.parametrize("b,s,why", [
    (dict(decision="WAIT"), {}, "not_enter"),
    (dict(network_id="8453"), {}, "chain_not_supported"),
    ({}, dict(authority_mint="open"), "authority_mint_not_closed"),
    ({}, dict(authority_mint=None), "authority_mint_not_closed"),
    ({}, dict(authority_freeze="unknown"), "authority_freeze_not_closed"),
    ({}, dict(sell_probe=None), "sell_probe_not_ok"),
    ({}, dict(sells_exist_60s=False), "no_sells_60s"),
    ({}, dict(sells_exist_60s=None), "sells_60s_unknown"),
])
def test_risk_vetoes_are_typed_and_carry_no_size(b, s, why):
    r = R.risk_check(policy(), bundle=bundle(**b), safety_blocks=safe(**s))
    assert (r.decision, r.reason, r.clip_usd) == ("veto", why, None)


def test_risk_vetoes_an_invalid_or_disabled_policy_and_a_missing_block():
    assert R.risk_check(policy(enabled=False), bundle=bundle(), safety_blocks=safe()).reason == "office_book_disabled"
    assert R.risk_check(policy(sl_pct=90.0), bundle=bundle(), safety_blocks=safe()).reason.startswith("policy:")
    assert R.risk_check(policy(), bundle=bundle(), safety_blocks=None).reason == "safety_blocks_missing"


# ---- Trader

def test_an_allowed_enter_is_one_idempotent_submit_keyed_by_the_action_key(book):
    risk = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe())
    kw = dict(context=ctx(), action_key="act-1", bundle=bundle(), risk_result=risk, now=NOW, sol_usd=FX, sol_at=NOW)
    first = T.execute_decision(book, policy(), **kw)
    assert first["outcome"] == "submitted" and first["order_id"] == "act-1"
    assert T.execute_decision(book, policy(), **kw) == first                          # retry: same receipt
    assert book.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert tuple(book.execute("SELECT book_id, decision_bundle_hash FROM orders").fetchone()) == ("PILOT", "b" * 64)
    assert T.lookup_decision(book, action_key="act-1", decision_id="d1", book_id="PILOT") == first


def test_a_veto_has_its_own_immutable_receipt_and_no_order(book):
    risk = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe(sell_probe=None))
    r = T.execute_decision(book, policy(), context=ctx(), action_key="act-2", bundle=bundle(), risk_result=risk,
                           now=NOW, sol_usd=FX, sol_at=NOW)
    assert (r["outcome"], r["reason"], r["order_id"]) == ("vetoed", "sell_probe_not_ok", None)
    assert book.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert T.lookup_decision(book, action_key="act-2", decision_id="d1", book_id="PILOT") == r
    assert T.lookup_decision(book, action_key="missing", decision_id="d1", book_id="PILOT") is None   # not a veto


def test_a_ledger_refusal_is_a_typed_receipt(book):
    risk = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe())
    r = T.execute_decision(book, policy(), context=ctx(), action_key="act-3", bundle=bundle(), risk_result=risk,
                           now=NOW, sol_usd=None, sol_at=None)                         # unknown FX: ledger refuses
    assert (r["outcome"], r["reason"]) == ("refused", "fx_unknown")


@pytest.mark.parametrize("change,why", [
    (dict(context=dict(decision_id="other")), "context_decision_mismatch"),
    (dict(context=dict(book_id="OTHER")), "book_unknown"),
    (dict(context=dict(role="head")), "role_not_trader"),
    (dict(context=dict(role="Trader")), "role_not_trader"),                   # the agents.role spelling is not a capability
])
def test_the_trader_refuses_untrusted_identity(book, change, why):
    risk = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe())
    with pytest.raises(ValueError, match=why):
        T.execute_decision(book, policy(), context=ctx(**change["context"]), action_key="act-4", bundle=bundle(),
                           risk_result=risk, now=NOW, sol_usd=FX, sol_at=NOW)


def test_a_reused_action_key_with_another_identity_is_a_conflict(book):
    risk = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe())
    T.execute_decision(book, policy(), context=ctx(), action_key="act-5", bundle=bundle(), risk_result=risk,
                       now=NOW, sol_usd=FX, sol_at=NOW)
    b2 = bundle(decision_id="d2", bundle_hash="c" * 64)
    with pytest.raises(ValueError, match="action_key_conflict"):
        T.execute_decision(book, policy(), context=ctx(decision_id="d2"), action_key="act-5", bundle=b2,
                           risk_result=R.risk_check(policy(), bundle=b2, safety_blocks=safe()),
                           now=NOW, sol_usd=FX, sol_at=NOW)
    with pytest.raises(ValueError, match="action_key_conflict"):
        T.lookup_decision(book, action_key="act-5", decision_id="d2", book_id="PILOT")


def test_a_non_enter_bundle_submits_nothing(book):
    risk = R.risk_check(policy(), bundle=bundle(decision="SKIP"), safety_blocks=safe())
    r = T.execute_decision(book, policy(), context=ctx(), action_key="act-6", bundle=bundle(decision="SKIP"),
                           risk_result=risk, now=NOW, sol_usd=FX, sol_at=NOW)
    assert r["outcome"] == "vetoed" and r["reason"] == "not_enter"
    assert book.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


# ---- the paper watcher (Delta's draft test, adopted)

def _watcher():
    spec = importlib.util.spec_from_file_location("office_paper_watcher", ROOT / "scripts/office_paper_watcher.py")
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    return P


def test_paper_watcher_keeps_utc_fx_and_native_clock_sample_separate(tmp_path):
    P = _watcher()
    clock = tmp_path / "clock.json"
    clock.write_text(json.dumps({"offset_s": 100., "at": 1000.}))
    desk = tmp_path / "desk"
    desk.mkdir()
    (desk / "universe.heartbeat").write_text(json.dumps({"ts": 1000., "connected": True, "sol_usd": 150.,
                                                         "sol_age_s": 10.}))
    calls = []
    result = P.tick(None, desk_dir=desk, clock_file=clock, now_host=1000.,
                    manage=lambda conn, policy, **kw: calls.append(kw))
    assert result["state"] == "managed"
    assert calls[0]["now"] == 900. and calls[0]["sol_at"] == 890.
    assert calls[0]["clock_sample"]["offset_s"] == 100. and calls[0]["clock_sample"]["sampled_at"] == 1000.
    clock.unlink()
    calls.clear()
    result = P.tick(None, desk_dir=desk, clock_file=clock, now_host=1000., manage=lambda *a, **kw: calls.append(kw))
    assert result["state"] == "clock_unknown" and calls == []


def test_a_manage_error_is_a_typed_heartbeat_and_is_logged(tmp_path, caplog):
    P = _watcher()
    clock = tmp_path / "clock.json"
    clock.write_text(json.dumps({"offset_s": 0., "at": 1000.}))

    def boom(*a, **k):
        raise RuntimeError("ledger broke")
    result = P.tick(None, desk_dir=tmp_path, clock_file=clock, now_host=1000., manage=boom)
    assert result["state"] == "paper_error" and result["error"] == "RuntimeError"
    assert "ledger broke" in caplog.text


def test_the_watcher_writes_its_heartbeat_atomically(tmp_path):
    P = _watcher()
    path = tmp_path / "beat.json"
    P.write_atomic(path, {"state": "managed"})
    assert json.loads(path.read_text()) == {"state": "managed"} and not list(tmp_path.glob("*.tmp"))


def test_the_pilot_book_bootstraps_from_the_frozen_config(tmp_path, monkeypatch):
    """Alpha ruling: 1000 paper USD, epoch office-pilot-discovery-1, book pilot-floor, discovery only."""
    conn = L.open_ledger(tmp_path / "ledger.db")
    path = ROOT / "config/office/book_policy_pilot.json"
    pol, b = T.bootstrap_from_config(conn, path, now=NOW)
    T.bootstrap_from_config(conn, path, now=NOW + 1)                                 # idempotent
    assert (b["epoch_id"], b["book_id"], b["capital_usd"]) == ("office-pilot-discovery-1", "pilot-floor", 1000.0)
    assert [tuple(r) for r in conn.execute("SELECT book_id, epoch_id, kind FROM books")] == [
        ("pilot-floor", "office-pilot-discovery-1", "discovery")]
    assert L.capital(conn, "pilot-floor") == 1000.0 and pol.clip_usd == 25.0



def test_a_real_supervisor_context_is_accepted(tmp_path, monkeypatch):
    """Codex: the executor must accept the context the real Supervisor.context() factory builds (role 'trader')."""
    import sys
    sys.path.insert(0, str(ROOT / "tests" / "office"))
    from test_supervisor import Clock, make
    from sniper.office import store as ST
    from sniper.office import supervisor as SV
    oconn = ST.open_store(tmp_path / "office.db")
    clock = Clock()
    oconn.execute("INSERT INTO agents VALUES(?,?)", ("agent", json.dumps(dict(
        agent_id="agent", role="Trader", state="active", generation=1, policy_hash="p", prompt_hash="q", model="m",
        provider="codex", book_id="PILOT"))))
    oconn.commit()
    lease = SV.Lease(oconn, clock=clock, ttl_s=100)
    gen = lease.acquire("supervisor-a")
    sup = make((oconn, clock, lease, gen), book_id=lambda record: record["book_id"])
    context = sup.context("agent", "d1", gen, sup.freeze_cut(), deadline_at=1002.0)
    assert context.role == "trader" and context.decision_id == "d1"
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / "ledger.db")
    T.bootstrap_pilot(conn, epoch_id="E", book_id="PILOT", clip_usd=25.0, rule_version="none", capital_usd=1000.0,
                      actor="operator", now=NOW - 10)
    risk = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe())
    r = T.execute_decision(conn, policy(), context=context, action_key="act-real", bundle=bundle(), risk_result=risk,
                           now=NOW, sol_usd=FX, sol_at=NOW)
    assert r["outcome"] == "submitted" and r["book_id"] == "PILOT"



# ---- Zeta HOLD c572af78a

def test_execute_decision_never_commits_or_breaks_a_caller_transaction(book):
    """The receipt DDL is in the ledger schema; nothing inside execute_decision commits the caller's transaction."""
    risk = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe())
    book.execute("BEGIN IMMEDIATE")
    book.execute("INSERT INTO marks(order_id, at, lamports, source) VALUES('marker', 1, NULL, 'test')")
    T.execute_decision(book, policy(), context=ctx(), action_key="act-tx", bundle=bundle(), risk_result=risk,
                       now=NOW, sol_usd=FX, sol_at=NOW)
    assert book.in_transaction
    book.execute("ROLLBACK")
    assert book.execute("SELECT COUNT(*) FROM marks WHERE order_id='marker'").fetchone()[0] == 0
    assert book.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert book.execute("SELECT COUNT(*) FROM decision_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("other", [dict(decision="SKIP"), dict(decision_id="d2"), dict(mint=MINT + "2", bundle_hash="c" * 64),
                                   dict(bundle_hash="c" * 64)])
def test_an_allow_is_bound_to_its_exact_bundle_and_policy(book, other):
    allow = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe())
    b2 = bundle(**other)
    with pytest.raises(ValueError, match="risk_not_bound|context_decision_mismatch"):
        T.execute_decision(book, policy(), context=ctx(decision_id=b2["decision_id"]), action_key="act-b",
                           bundle=b2, risk_result=allow, now=NOW, sol_usd=FX, sol_at=NOW)
    assert book.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_an_allow_under_another_policy_is_not_bound(book):
    allow = R.risk_check(policy(), bundle=bundle(), safety_blocks=safe())
    with pytest.raises(ValueError, match="risk_not_bound"):
        T.execute_decision(book, policy(tp_pct=50.0), context=ctx(), action_key="act-p", bundle=bundle(),
                           risk_result=allow, now=NOW, sol_usd=FX, sol_at=NOW)


# ---- the watcher sandbox (Delta: systemd --user read-only properties are not enforced here; bwrap is)

UNIT = ROOT / "deploy/office/office-paper-watcher.service"


def _exec_start():
    import shlex
    line = next(l for l in UNIT.read_text().splitlines() if l.startswith("ExecStart="))
    return shlex.split(line[len("ExecStart="):])


def test_the_watcher_unit_runs_under_bwrap_with_only_the_ledger_and_watcher_dirs_writable():
    argv = _exec_start()
    assert argv[0] == "/usr/bin/bwrap"
    for flag in ("--unshare-user", "--unshare-pid", "--die-with-parent", "--new-session"):
        assert flag in argv
    assert argv[argv.index("--ro-bind") + 1: argv.index("--ro-bind") + 3] == ["/", "/"]
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    rw = [argv[i + 1] for i, a in enumerate(argv) if a == "--bind"]
    assert rw == ["%h/Desktop/Sniper/data/office/ledger", "%h/Desktop/Sniper/data/office/watcher"]
    py = argv[argv.index("/usr/bin/env"):]
    assert py[1] == "-i" and not any("KEY" in a or "TOKEN" in a for a in py)
    ledger = py[py.index("--ledger") + 1]
    beat = py[py.index("--heartbeat") + 1]
    assert ledger.startswith(rw[0] + "/") and beat.startswith(rw[1] + "/")
    cfg = json.loads((ROOT / "config/office/book_policy_pilot.json").read_text())["bootstrap"]
    assert ledger.endswith("/" + cfg["ledger_path"])


def test_bwrap_with_the_unit_flags_denies_writes_outside_the_bound_dirs(tmp_path):
    """A live write-denial check of the same flag set: the bound dir is writable, everything else is not."""
    import shutil
    import subprocess
    if not shutil.which("bwrap"):
        pytest.skip("bwrap not installed")
    rw, other = tmp_path / "ledger", tmp_path / "elsewhere"
    rw.mkdir()
    other.mkdir()
    probe = ("import sys\nfor p in sys.argv[1:]:\n    try:\n        open(p, 'w').write('x'); print('ok', p)\n"
             "    except OSError as e:\n        print('denied', p)\n"
             "st = dict(l.split(':', 1) for l in open('/proc/self/status').read().splitlines() if ':' in l)\n"
             "print('nnp', st['NoNewPrivs'].strip(), 'capeff', int(st['CapEff'].strip(), 16))\n")
    out = subprocess.run(["bwrap", "--unshare-user", "--unshare-pid", "--die-with-parent", "--new-session",
                          "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                          "--bind", str(rw), str(rw), "--cap-drop", "ALL", "/usr/bin/env", "-i", "python3", "-c", probe,
                          str(rw / "ledger.db-wal"), str(other / "canary"), str(ROOT / "canary_should_not_exist")],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0 and "No permissions to creating new namespace" in out.stderr:
        pytest.skip("user namespaces unavailable in this environment")
    lines = out.stdout.split("\n")
    assert lines[0] == "ok %s" % (rw / "ledger.db-wal")
    assert lines[1].startswith("denied") and lines[2].startswith("denied")
    assert lines[3] == "nnp 1 capeff 0"                                         # the payload is locked down
    assert not (other / "canary").exists() and not (ROOT / "canary_should_not_exist").exists()



def test_the_watcher_refuses_to_run_outside_its_sandbox():
    P = _watcher()
    assert P.sandbox_refusal("NoNewPrivs:\t1\nCapEff:\t0000000000000000\n") is None
    assert P.sandbox_refusal("NoNewPrivs:\t0\nCapEff:\t0000000000000000\n") == "no_new_privs_not_set"
    assert P.sandbox_refusal("NoNewPrivs:\t1\nCapEff:\t0000000000000400\n") == "effective_capabilities_present"
    assert P.sandbox_refusal("NoNewPrivs:\t1\n") == "capabilities_unreadable"
    directives = [l for l in UNIT.read_text().splitlines() if not l.lstrip().startswith("#")]
    assert "NoNewPrivileges=no" in directives and "NoNewPrivileges=yes" not in directives
    assert any("--cap-drop ALL" in l for l in directives)


# ---- Alpha 2026-10-28: marks throttled to 15 s, fills and due exits never; watcher quota 50 %

def test_marks_are_throttled_but_due_exits_and_fills_are_not(tmp_path, monkeypatch):
    sys_path = ROOT / "tests" / "office"
    import sys
    sys.path.insert(0, str(sys_path))
    from test_ledger import Quotes, policy as lpolicy
    q = Quotes()
    monkeypatch.setattr(L, "_ps_buy", q.buy)
    monkeypatch.setattr(L, "_ps_sell", q.sell)
    monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    conn = L.open_ledger(tmp_path / "ledger.db")
    T.bootstrap_pilot(conn, epoch_id="E", book_id="B", clip_usd=30.0, rule_version="none", capital_usd=1000.0,
                      actor="operator", now=NOW - 10)
    L.submit(conn, lpolicy(), book_id="B", order_id="o1", network_id="solana", mint=MINT, pool="P",
             decision_bundle_hash="b" * 64, now=NOW, sol_usd=FX, sol_at=NOW)
    clock = {"offset_s": 0.0, "sampled_at": NOW - 5}
    kw = dict(desk_dir=Path("/x"), clock_sample=clock, sol_usd=FX, sol_at=NOW, mark_min_interval_s=15.0)
    L.manage(conn, lpolicy(), now=NOW + 2, **kw)                         # fill is never throttled
    assert conn.execute("SELECT state FROM orders").fetchone()[0] == "open"
    for t in (NOW + 3, NOW + 4, NOW + 10, NOW + 17.9):
        L.manage(conn, lpolicy(), now=t, **kw)
    assert [r[0] for r in conn.execute("SELECT at FROM marks ORDER BY at")] == [NOW + 3]   # none again before +15 s
    L.manage(conn, lpolicy(), now=NOW + 18.1, **kw)
    assert [r[0] for r in conn.execute("SELECT at FROM marks ORDER BY at")] == [NOW + 3, NOW + 18.1]
    L.request_exit(conn, order_id="o1", reason="x", requested_by="Trader", now=NOW + 19)
    L.manage(conn, lpolicy(), now=NOW + 20, **kw)                       # due exit inside the mark window: runs
    assert conn.execute("SELECT state FROM orders").fetchone()[0] == "closed"


def test_the_watcher_unit_has_half_a_core_and_15_s_marks():
    directives = [l for l in UNIT.read_text().splitlines() if not l.lstrip().startswith("#")]
    assert "CPUQuota=50%" in directives
    assert "--mark-every-s" in _exec_start() and _exec_start()[_exec_start().index("--mark-every-s") + 1] == "15"


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), True, 0, -1.0, "15"])
def test_the_mark_interval_must_be_none_or_finite_positive(tmp_path, bad):
    """Zeta 757959127: an infinite interval silently stopped every future mark and rail."""
    conn = L.open_ledger(tmp_path / "ledger.db")
    with pytest.raises(ValueError, match="mark_min_interval_invalid"):
        L.manage(conn, policy(), desk_dir=Path("/x"), now=NOW, clock_sample=None, mark_min_interval_s=bad)
    L.manage(conn, policy(), desk_dir=Path("/x"), now=NOW, clock_sample=None, mark_min_interval_s=None)


@pytest.mark.parametrize("bad", ["inf", "nan", "0", "-5", "301"])
def test_the_watcher_cli_refuses_a_bad_mark_interval_before_the_loop(tmp_path, bad):
    P = _watcher()
    with pytest.raises(SystemExit):
        P.main(["--ledger", str(tmp_path / "l.db"), "--desk-dir", str(tmp_path), "--clock-file", str(tmp_path / "c"),
                "--heartbeat", str(tmp_path / "b.json"), "--no-sandbox-check", "--mark-every-s", bad])
    assert not (tmp_path / "b.json").exists()
