"""EVM runner wiring — multi-venue routing. The load-bearing correctness property: every real
position is handled by ITS OWN venue (a Solana position never reaches the EVM executor and an 0x mint
never reaches the Solana executor — the exact bug the review flagged). Entry, exit sweep, mirror,
reconcile, and reservation-recovery all route by network_id."""
import sqlite3

import pytest

from sniper.execution.solana_quote import norm_mint
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


class _Ev:
    def __init__(self, mint, network_id, side="buy", uid="u1"):
        self.mint = mint; self.network_id = network_id; self.side = side
        self.source_user_id = uid; self.created_at = "2026-07-09T00:00:00Z"


def _open_real(c, mint, network, uid="u1"):
    return m.open_position(c, source_user_id=uid, source_handle="(real)", mint=mint, ticker="",
                           entry_price=1.0, entry_notional=10.0, entry_fill_source="chain",
                           entry_lag_s=-1.0, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                           strategy="alpha_b", execution_mode="real", network_id=network)
    # (fills added by callers that need open_qty)


def _sol_ctx(rec):
    return {"execute_fn": lambda conn, pid, mint, size, now, impact_cap_pct=None, **_kw: (
                                                              rec.append(("sol_exec", mint)),
                                                              {"status": "filled", "position_id": pid,
                                                               "fill_price": 2.0, "sig": "S"})[1],
            "exit_fn": lambda conn, pos, reason, now: (rec.append(("sol_exit", pos["mint"])),
                                                       {"status": "closed", "realized": 1.0, "sig": "S"})[1],
            "preview_exit_fn": lambda pos: {"expected_usdc": 1.0},
            "token_safety_fn": lambda mint, **kw: (True, "ok"),
            "usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
            "token_fn": lambda mint: rec.append(("sol_bal", mint)) or 0.0,
            "confirm_fn": lambda sig: "sol_confirm", "kill_path": None}


def _evm_ctx(rec):
    return {"execute_fn": lambda conn, pid, mint, size, now: (rec.append(("evm_exec", mint)),
                                                              {"status": "filled", "position_id": pid,
                                                               "fill_price": 2.0, "tx_hash": "0xh"})[1],
            "exit_fn": lambda conn, pos, reason, now: (rec.append(("evm_exit", pos["mint"])),
                                                       {"status": "closed", "realized": 1.0, "tx_hash": "0xh"})[1],
            "preview_exit_fn": lambda pos: {"expected_usdc": 1.0},
            "token_safety_fn": lambda mint, **kw: (True, "ok"),
            "usdc_fn": lambda: 300.0, "native_fn": lambda: 1.0,
            "token_fn": lambda mint: rec.append(("evm_bal", mint)) or 0.0,
            "confirm_fn": lambda sig: "evm_confirm", "kill_path": None}


# --- context_for routing ---

def test_context_for_routes_by_network():
    contexts = {"solana": {"x": 1}, "8453": {"y": 2}}
    assert R.context_for(contexts, "solana") == {"x": 1}
    assert R.context_for(contexts, "8453") == {"y": 2}
    assert R.context_for(contexts, None) == {"x": 1}      # None -> solana default
    assert R.context_for(contexts, "56") is None          # unconfigured chain -> no context


def test_missing_evm_context_chains_reports_exact_allowlist_gap():
    contexts = {"solana": {"tag": "sol"}, "4663": {"tag": "rh"}}
    assert R.missing_evm_context_chains(
        contexts, allowlist={"8453", "4663", "56"},
    ) == ["56", "8453"]


# --- entry dispatch: base58 -> Solana, 0x -> EVM, never crossed ---

def _arm(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453"})


def test_entry_base58_hits_solana_only(monkeypatch):
    _arm(monkeypatch)
    c = _c(); rec = []
    contexts = {"solana": _sol_ctx(rec), "8453": _evm_ctx(rec)}
    R.real_entry_hook(c, _Ev("So1Mint", "solana"), contexts,
                      now_fn=lambda: "2026-07-09T00:00:00Z", trade_size=10.0, max_age_s=120)
    assert ("sol_exec", "So1Mint") in rec
    assert not any(t == "evm_exec" for t, _ in rec)       # EVM executor untouched by a base58 buy


def test_entry_0x_hits_evm_only(monkeypatch):
    _arm(monkeypatch)
    c = _c(); rec = []
    contexts = {"solana": _sol_ctx(rec), "8453": _evm_ctx(rec)}
    R.real_entry_hook(c, _Ev("0xtok", "8453"), contexts,
                      now_fn=lambda: "2026-07-09T00:00:00Z", trade_size=10.0, max_age_s=120)
    assert ("evm_exec", "0xtok") in rec
    assert not any(t == "sol_exec" for t, _ in rec)       # Solana executor never sees an 0x mint


def test_evm_entry_does_not_read_solana_risk_balances(monkeypatch):
    """An explicit EVM identity must route before evaluating unrelated Solana balance RPC seams."""
    _arm(monkeypatch)
    c = _c(); rec = []
    sol = _sol_ctx(rec)
    sol["usdc_fn"] = lambda: pytest.fail("EVM entry read Solana USDC")
    sol["sol_fn"] = lambda: pytest.fail("EVM entry read Solana native balance")
    contexts = {"solana": sol, "8453": _evm_ctx(rec)}

    result = R.real_entry_hook(
        c, _Ev("0xtok", "8453"), contexts,
        now_fn=lambda: "2026-07-09T00:00:00Z", trade_size=10.0, max_age_s=120,
    )

    assert result["status"] == "filled"
    assert ("evm_exec", "0xtok") in rec


def test_entry_0x_unconfigured_chain_drops(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "56"})  # 56 allowlisted but no context
    c = _c(); rec = []
    contexts = {"solana": _sol_ctx(rec), "8453": _evm_ctx(rec)}         # no '56' context
    r = R.real_entry_hook(c, _Ev("0xbsc", "56"), contexts,
                          now_fn=lambda: "2026-07-09T00:00:00Z", trade_size=10.0, max_age_s=120)
    assert r["status"] == "evm_unarmed"
    assert not any(t in ("sol_exec", "evm_exec") for t, _ in rec)      # neither executor touched


# --- exit sweep: each venue exits only its own positions ---

def test_exit_sweep_routes_each_position_to_its_venue(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)   # subject: venue routing
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SECONDS", 0)
    c = _c(); rec = []
    _open_real(c, "So1Drop", "solana")
    _open_real(c, "0xDrop", "8453")
    contexts = {"solana": _sol_ctx(rec), "8453": _evm_ctx(rec)}
    drop = 1.0 * (1 - (cfg.MEMECOIN_B_STOP_PCT + 1) / 100.0)
    pmap = {norm_mint("So1Drop"): {"price": drop}, norm_mint("0xDrop"): {"price": drop}}
    R.real_exit_sweep(c, contexts, price_fn=lambda mints: pmap, now_fn=lambda: "2026-07-09T00:05:00Z")
    assert ("sol_exit", "So1Drop") in rec and ("evm_exit", "0xDrop") in rec
    assert ("sol_exit", "0xDrop") not in rec              # Solana exit NEVER handed the 0x mint
    assert ("evm_exit", "So1Drop") not in rec             # EVM exit NEVER handed the base58 mint


def test_exit_sweep_skips_position_with_no_context():
    c = _c(); rec = []
    _open_real(c, "0xOrphanChain", "999")                 # a chain we have no context for
    contexts = {"solana": _sol_ctx(rec)}                  # only solana
    drop = 1.0 * (1 - (cfg.MEMECOIN_B_STOP_PCT + 1) / 100.0)
    R.real_exit_sweep(c, contexts, price_fn=lambda mints: {norm_mint("0xOrphanChain"): {"price": drop}},
                      now_fn=lambda: "2026-07-09T00:05:00Z")
    assert rec == []                                      # skipped + logged; never mis-routed to Solana


# --- mirror-on-sell routes by venue ---

def test_mirror_routes_to_position_venue(monkeypatch):
    """Routing, under the fractional policy that still exits on a feed sell: a sell on an EVM position
    goes to the EVM exit, never the Solana one. Since #469 (0074fa52d, 2026-10-18) the default 1.0 is
    full-only, where a feed sell with unproven size holds (the receipt-backed mirror exits), pinned
    below so the routing check cannot silently pass by holding."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 0.5)
    c = _c(); rec = []
    _open_real(c, "0xMirror", "8453")
    contexts = {"solana": _sol_ctx(rec), "8453": _evm_ctx(rec)}
    R.real_mirror_on_sell(c, _Ev("0xMirror", "8453", side="sell"), contexts,
                          now_fn=lambda: "2026-07-09T00:05:00Z")
    assert ("evm_exit", "0xMirror") in rec and not any(t == "sol_exit" for t, _ in rec)


def test_full_only_feed_mirror_holds_and_touches_no_executor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIRROR_SIZE_MIN_FRACTION", 1.0)   # the #469 default
    c = _c(); rec = []
    _open_real(c, "0xMirror", "8453")
    out = R.real_mirror_on_sell(c, _Ev("0xMirror", "8453", side="sell"),
                                {"solana": _sol_ctx(rec), "8453": _evm_ctx(rec)},
                                now_fn=lambda: "2026-07-09T00:05:00Z")
    assert (out["status"], out["reason"]) == ("mirror_held", "size_unknown") and rec == []


# --- reconcile routes balance reads by venue ---

def test_reconcile_reads_each_position_on_its_own_chain():
    c = _c(); rec = []
    sol_pid = _open_real(c, "So1Rec", "solana")
    evm_pid = _open_real(c, "0xRec", "8453")
    for pid in (sol_pid, evm_pid):
        m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=500.0, notional=10.0,
                      fill_source="chain", fraction=1.0, created_at="t")
    contexts = {"solana": _sol_ctx(rec), "8453": _evm_ctx(rec)}
    R.real_reconcile(c, contexts, alert_fn=lambda mint, pid: None)
    # each position's balance was read on ITS OWN venue's reader, not the other
    assert ("sol_bal", "So1Rec") in rec and ("evm_bal", "0xRec") in rec
    assert ("sol_bal", "0xRec") not in rec and ("evm_bal", "So1Rec") not in rec


# --- recovery confirm router ---

def test_confirm_for_routes_by_network():
    rec = []
    contexts = {"solana": _sol_ctx(rec), "8453": _evm_ctx(rec)}
    confirm_for = R.make_confirm_for(contexts)
    assert confirm_for("solana", "sig") == "sol_confirm"
    assert confirm_for("8453", "sig") == "evm_confirm"
    assert confirm_for("999", "sig") == "unavailable"     # no context -> fail-safe (leave reserved)


def test_confirm_for_uses_single_shot_not_the_fill_wait_poll():
    # recovery runs inline in the loop, so it MUST use the single-shot check, not the executor's
    # ~40s/120s fill-wait poll (which would stall the copy path + exit sweep every cycle).
    ctx = {"confirm_once_fn": lambda sig: "single_shot", "confirm_fn": lambda sig: "long_poll"}
    confirm_for = R.make_confirm_for({"solana": ctx})
    assert confirm_for("solana", "sig") == "single_shot"


def test_confirmed_nonce_for_routes_only_to_an_evm_context():
    nonce_for = R.make_confirmed_nonce_for({"4663": {"confirmed_nonce_fn": lambda: 9}})
    assert nonce_for("4663") == 9
    assert nonce_for("solana") is None


# --- defensive mint<->network cross-check (self-defending no-cross property) ---

def test_context_for_position_rejects_venue_mint_mismatch():
    contexts = {"solana": {"tag": "sol"}, "8453": {"tag": "evm"}}
    # correct pairings route
    assert R.context_for_position(contexts, {"mint": "So1", "network_id": "solana"})["tag"] == "sol"
    assert R.context_for_position(contexts, {"mint": "0xabc", "network_id": "8453"})["tag"] == "evm"
    # corruption: an 0x mint tagged 'solana' must NOT route to the Solana executor
    assert R.context_for_position(contexts, {"mint": "0xabc", "network_id": "solana", "id": 1}) is None
    # corruption: a base58 mint tagged an EVM chain must NOT route to the EVM executor
    assert R.context_for_position(contexts, {"mint": "So1", "network_id": "8453", "id": 2}) is None


# --- builders ---

def test_build_all_contexts_empty_when_disarmed(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", False)
    assert R.build_all_contexts() == {}


def test_build_real_evm_contexts_builds_only_explicit_direct_chain(monkeypatch, tmp_path):
    import json

    from sniper.execution.evm_wallet import generate_keypair_file
    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"8453", "4663"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_8453", "https://base-rpc")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_8453", "disabled")
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    def http(_method, _url, **kwargs):
        if json.loads(kwargs["body"])["method"] == "eth_chainId":
            return 200, {"result": "0x1237"}, None
        return 200, {}, None

    ctxs = R.build_real_evm_contexts(http=http)
    assert set(ctxs.keys()) == {"4663"}
    assert callable(ctxs["4663"]["execute_fn"]) and callable(ctxs["4663"]["confirm_fn"])


def test_an_allowlisted_chain_with_no_execution_path_logs_loudly(monkeypatch, caplog):
    """Allowlisting a chain with no working execution venue used to build zero context and say
    nothing — the next entry attempt just never fired, with no signal pointing at why."""
    import logging

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"5042"})
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_5042", "disabled")
    with caplog.at_level(logging.WARNING):
        ctxs = R.build_real_evm_contexts()
    assert ctxs == {}
    assert any("no execution path for chain 5042" in rec.message for rec in caplog.records)


def test_a_chain_with_a_working_context_logs_nothing(monkeypatch, tmp_path, caplog):
    import json
    import logging

    from sniper.execution.evm_wallet import generate_keypair_file
    kpf = str(tmp_path / "evm.json"); generate_keypair_file(kpf)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", kpf)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://robinhood-rpc")
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")

    def http(_method, _url, **kwargs):
        if json.loads(kwargs["body"])["method"] == "eth_chainId":
            return 200, {"result": "0x1237"}, None
        return 200, {}, None

    with caplog.at_level(logging.WARNING):
        ctxs = R.build_real_evm_contexts(http=http)
    assert set(ctxs.keys()) == {"4663"}
    assert not any("no execution path" in r.message for r in caplog.records)


def test_build_real_evm_contexts_passes_the_runner_connection(monkeypatch):
    from sniper.services import memecoin_evm_leg

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    seen = {}

    def build(chain, **kwargs):
        seen[chain] = kwargs
        return {"chain": chain}

    monkeypatch.setattr(memecoin_evm_leg, "build_real_evm_context", build)
    # A real (empty) connection: the Ink recovery check reads it before any chain is built, and an
    # opaque sentinel is a read error that must fail loudly (2026-09-29).
    import sqlite3
    conn = sqlite3.connect(":memory:")

    assert R.build_real_evm_contexts(conn=conn) == {"4663": {"chain": "4663"}}
    assert seen["4663"]["conn"] is conn
