"""Item 2 — network router in the real leg. Route each curated buy by venue: base58 mint -> Solana
leg; allowlisted EVM chain (Base 8453, BSC 56) -> EVM leg (disarmed until it exists); excluded EVM
(ETH L1 by default, Monad, unknown chain) -> dropped with a counter, never an alert. An 0x (EVM)
mint must NEVER reach the Solana executor."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_arming
from sniper.services.memecoin_real_leg import route_real_entry, real_entry
from sniper.settings import memecoin as cfg

AL = {"8453", "56"}


class _Ev:
    def __init__(self, mint, network_id=""):
        self.mint = mint
        self.network_id = network_id
        self.source_user_id = "u1"


# --- pure router ---

def test_base58_mint_routes_solana():
    assert route_real_entry(_Ev("So1anaMint", "solana"), AL) == "solana"
    assert route_real_entry(_Ev("So1anaMint", ""), AL) == "solana"      # feed omitted networkId


def test_allowlisted_evm_routes_evm():
    assert route_real_entry(_Ev("0xabc", "8453"), AL) == "evm:8453"
    assert route_real_entry(_Ev("0xabc", "56"), AL) == "evm:56"


def test_excluded_and_unknown_evm_drop():
    assert route_real_entry(_Ev("0xabc", "1"), AL) == "drop"            # ETH L1 excluded by default
    assert route_real_entry(_Ev("0xabc", "143"), AL) == "drop"         # Monad off
    assert route_real_entry(_Ev("0xabc", ""), AL) == "drop"            # unknown EVM chain -> fail closed


# --- real_entry integration ---

def _arm(monkeypatch, armed=True):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", armed)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", armed)


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _leg(c, ev, kill, *, evm_entry_fn=None):
    calls = {"exec": 0}

    def ex(conn, pid, mint, size, now_iso, impact_cap_pct=None):
        calls["exec"] += 1
        return {"status": "filled", "position_id": pid}
    r = real_entry(c, ev, day="2026-07-09", usdc_balance=300.0, sol_balance=1.0,
                   now_iso="2026-07-09T00:00:00Z", trade_size=10.0,
                   token_safety_fn=lambda mint, **kw: (True, "ok"), execute_fn=ex, kill_path=kill,
                   evm_allowlist=AL, evm_entry_fn=evm_entry_fn)
    return r, calls


def test_excluded_evm_drops_no_reserve_and_counts(monkeypatch, tmp_path):
    _arm(monkeypatch)
    c = _c()
    r, calls = _leg(c, _Ev("0xabc", "1"), str(tmp_path / "k"))
    assert r["status"] == "dropped_network" and calls["exec"] == 0
    assert m.pending_totals(c) == (0, 0.0)                              # never reserved
    assert m.network_drop_counts(c).get(("1", "excluded"), 0) == 1


def test_allowlisted_evm_unarmed_never_hits_solana_executor(monkeypatch, tmp_path):
    _arm(monkeypatch)
    c = _c()
    r, calls = _leg(c, _Ev("0xabc", "8453"), str(tmp_path / "k"))       # no evm_entry_fn wired
    assert r["status"] == "evm_unarmed" and calls["exec"] == 0
    assert m.pending_totals(c) == (0, 0.0)
    assert m.network_drop_counts(c).get(("8453", "evm_unarmed"), 0) == 1


def test_allowlisted_evm_dispatches_when_leg_exists(monkeypatch, tmp_path):
    _arm(monkeypatch)
    c = _c()
    seen = {}

    def evm_fn(conn, ev, *, route, **kw):
        seen["route"] = route
        return {"status": "evm_filled"}
    r, calls = _leg(c, _Ev("0xdef", "56"), str(tmp_path / "k"), evm_entry_fn=evm_fn)
    assert r["status"] == "evm_filled" and seen["route"] == "evm:56"
    assert calls["exec"] == 0                                           # solana executor untouched


def test_solana_still_flows_to_executor(monkeypatch, tmp_path):
    _arm(monkeypatch)
    c = _c()
    r, calls = _leg(c, _Ev("So1anaMint", "solana"), str(tmp_path / "k"))
    assert r["status"] == "filled" and calls["exec"] == 1
