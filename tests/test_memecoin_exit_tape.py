"""Exit price tape, part A-lite (spec 2026-09-30 §3a, Alpha 2026-10-01).

One in-memory append at the confirmation site of `real_exit_sweep`; a daemon writer drains it to
its own file. The join that makes it worth having, confirmation -> attempt on
`decision_at == attempted_at`, is proven here on the PRODUCTION sweep with the PRODUCTION EVM
executor (`build_real_evm_executor`), not on a hand-built exit_fn: a hand-built one would stamp
whatever the test told it to.
"""
import ast
import json
import sqlite3
from pathlib import Path

import pytest
from eth_account import Account

from sniper.analysis.memecoin_quote_failures import QuoteFailure
from sniper.execution.evm_real_seams import build_real_evm_executor
from sniper.execution.exit_preview import ExitPreview, PreviewResult
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_exit_tape as tape_mod
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

ROOT = Path(__file__).resolve().parents[1]
DECISION_AT = "2026-10-01T09:00:00+00:00"
MINT = "0x" + "22" * 20
SPENDER = "0x" + "55" * 20
BAG_ATOMIC = 10 * 10 ** 18


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, network_id="8453", mint=MINT):
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint=mint, ticker="T", entry_price=1.0,
        entry_notional=10.0, entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
        opened_at="2026-10-01T08:00:00+00:00", strategy=cfg.MEMECOIN_REAL_POLICY,
        execution_mode="real", network_id=network_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="chain", fraction=1.0, created_at="2026-10-01T08:00:00+00:00",
                  tx_sig="E")
    return pid


def _http(balance_ok=True):
    def http(method, url, headers=None, body=None, timeout=10):
        if method != "POST":
            return 200, {}, None
        meth = json.loads(body).get("method")
        if meth == "eth_getTransactionCount":
            return 200, {"result": "0x1"}, None
        if meth == "eth_gasPrice":
            return 200, {"result": "0x3b9aca00"}, None
        if meth == "eth_getBalance":
            return 200, {"result": hex(10 ** 18)}, None
        if meth == "eth_call":
            if not balance_ok:
                raise TimeoutError("balance read timed out")
            data = str((json.loads(body)["params"][0] or {}).get("data") or "")
            if data.startswith("0x313ce567"):                     # decimals()
                return 200, {"result": "0x" + hex(18)[2:].rjust(64, "0")}, None
            if data.startswith("0x70a08231"):                     # balanceOf
                return 200, {"result": "0x" + hex(BAG_ATOMIC)[2:].rjust(64, "0")}, None
            return 200, {"result": "0x" + "f" * 64}, None         # allowance and the rest
        if meth == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1"}}, None
        return 200, {"result": None}, None
    return http


def _quote(sell_token, buy_token, amount, *, slippage_bps=None):
    return {"sellAmount": str(amount), "buyAmount": "16000000",
            "otherAmountThreshold": "15000000",
            "transaction": {"to": SPENDER, "data": "0x", "value": "0",
                            "gas": "300000", "gasPrice": "1000000000"},
            "issues": {"allowance": {"spender": SPENDER}}}


def _executor(balance_ok=True):
    usdc = iter([0.0, 16.0, 16.0, 16.0])
    return build_real_evm_executor(
        acct=Account.create(), chain_id="8453", rpc_url="rpc", http=_http(balance_ok),
        venue_quote_fn=_quote, usdc_fn=lambda: next(usdc), token_fn=lambda mint: 10.0,
        backend_label="auto_free")


@pytest.fixture
def tape(tmp_path, monkeypatch):
    t = tape_mod.Tape(str(tmp_path / "tape.db"))
    monkeypatch.setattr(tape_mod, "_TAPE", t)
    return t


def _rows(t):
    t.drain_once()
    c = sqlite3.connect(t.path)
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute("SELECT * FROM exit_confirmations ORDER BY id")]


def _priced(price, mint=MINT):
    from sniper.execution.solana_quote import norm_mint
    return {norm_mint(mint): {"price": price, "liquidity": 1e9}}


def _tp(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)


def test_the_confirmation_joins_its_attempt_and_fill_exactly_on_the_production_path(tape, monkeypatch):
    _tp(monkeypatch)
    c = _ledger()
    pid = _open(c)
    ex = _executor()
    ctx = {"exit_fn": ex["exit_fn"], "preview_exit_fn": ex["preview_exit_fn"]}

    R.real_exit_sweep(c, {"8453": ctx}, price_fn=lambda mints: {}, now_fn=lambda: DECISION_AT)

    rows = _rows(tape)
    assert len(rows) == 1
    row = rows[0]
    attempt = c.execute("SELECT * FROM memecoin_exit_execution_attempts WHERE position_id=?",
                        (pid,)).fetchone()
    assert attempt is not None and attempt["status"] == "closed"
    # THE JOIN: exact equality, no time window.
    assert (row["position_id"], row["decision_at"], row["reason"]) == (
        attempt["position_id"], attempt["attempted_at"], attempt["exit_reason"])
    fill = c.execute("SELECT * FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
                     (pid,)).fetchone()
    assert attempt["sig"] and fill["tx_sig"] == attempt["sig"]
    assert row["status"] == "ok" and row["allowed"] == 1
    assert row["expected_usdc"] == pytest.approx(16.0)
    assert row["amount_atomic"] == str(BAG_ATOMIC)              # TEXT: past 2**63
    assert row["lane"] == "periodic_sweep" and row["network_id"] == "8453"
    assert row["host_at"] and row["host_mono"] is not None


def test_a_failed_preview_is_a_status_row_with_no_price(tape, monkeypatch):
    _tp(monkeypatch)
    c = _ledger()
    pid = _open(c)
    ex = _executor(balance_ok=False)
    ctx = {"exit_fn": ex["exit_fn"], "preview_exit_fn": ex["preview_exit_fn"]}

    # A feed mid at +60% nominates the take-profit; the exact preview then fails.
    R.real_exit_sweep(c, {"8453": ctx}, price_fn=lambda mints: _priced(1.6),
                      now_fn=lambda: DECISION_AT)

    rows = _rows(tape)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == QuoteFailure.PROVIDER_TIMEOUT.value
    assert row["expected_usdc"] is None and row["amount_atomic"] is None
    assert row["allowed"] == 0 and row["refusal_code"]
    # A priced chain with no exact policy buys its preview AT the confirmation.
    assert row["preview_src"] == "confirm"
    assert c.execute("SELECT COUNT(*) FROM memecoin_exit_execution_attempts").fetchone()[0] == 0


def test_a_raising_append_never_changes_the_exit(tape, monkeypatch):
    _tp(monkeypatch)
    c = _ledger()
    _open(c, network_id="4663")
    calls = []

    def boom(row):
        raise RuntimeError("tape broken")
    monkeypatch.setattr(tape, "append", boom)
    ctx = {"exit_fn": lambda conn, pos, reason, now: calls.append(reason) or {"status": "closed"},
           "preview_exit_fn": lambda pos: {"expected_usdc": 16.0}}

    R.real_exit_sweep(c, {"4663": ctx}, price_fn=lambda mints: {}, now_fn=lambda: DECISION_AT)

    assert calls == ["real_tp"]


def test_no_tape_records_nothing_and_the_exit_runs(monkeypatch):
    _tp(monkeypatch)
    monkeypatch.setattr(tape_mod, "_TAPE", None)
    c = _ledger()
    _open(c, network_id="4663")
    calls = []
    ctx = {"exit_fn": lambda conn, pos, reason, now: calls.append(reason) or {"status": "closed"},
           "preview_exit_fn": lambda pos: {"expected_usdc": 16.0}}
    R.real_exit_sweep(c, {"4663": ctx}, price_fn=lambda mints: {}, now_fn=lambda: DECISION_AT)
    assert calls == ["real_tp"]


def test_the_preview_evidence_lands_in_the_row(tape, monkeypatch):
    """A Solana-shaped PreviewResult: provider, venue, slot and the provider's own stamp."""
    _tp(monkeypatch)
    c = _ledger()
    _open(c, network_id="solana", mint="SoLTapeMint")
    ok = PreviewResult.succeeded(
        ExitPreview(16.0, 10_000_000, "jupiter", "2026-10-01T08:59:59.900000+00:00",
                    quote_evidence={"venue": "jupiter", "expected_output_atomic": "16000000",
                                    "route_target": None, "context_slot": 123}),
        provider_id="solana:exact_quote", provider_group="solana:exact_quote")
    ctx = {"exit_fn": lambda conn, pos, reason, now: {"status": "closed"},
           "preview_exit_fn": lambda pos: ok}
    R.real_exit_sweep(c, {"solana": ctx}, price_fn=lambda mints: _priced(1.6, "SoLTapeMint"),
                      now_fn=lambda: DECISION_AT)
    rows = _rows(tape)
    assert [r["reason"] for r in rows] == ["real_tp"]
    row = rows[0]
    assert row["provider_id"] == "solana:exact_quote" and row["venue"] == "jupiter"
    assert row["context_slot"] == 123 and row["expected_output_atomic"] == "16000000"
    assert row["quoted_at"] == "2026-10-01T08:59:59.900000+00:00"
    assert row["preview_src"] == "confirm" and row["status"] == "ok"


def test_a_full_queue_drops_the_oldest_and_counts_it(tmp_path):
    t = tape_mod.Tape(str(tmp_path / "t.db"), maxlen=3)
    for i in range(5):
        t.append({"position_id": i})
    assert t.snapshot()["dropped"] == 2
    assert [r["position_id"] for r in t._q] == [2, 3, 4]


def test_the_tape_refuses_the_ledger(tmp_path):
    from sniper.persistence.db_core import get_db_path
    with pytest.raises(ValueError):
        tape_mod.Tape(get_db_path())
    with pytest.raises(ValueError):
        tape_mod.Tape(str(tmp_path / "sniper_tracker.db"))


def test_a_failed_batch_write_is_dropped_and_counted(tmp_path):
    t = tape_mod.Tape(str(tmp_path / "t.db"))
    t.append({"position_id": "not-an-int", "bogus_column": 1})
    assert t.drain_once() == 0
    snap = t.snapshot()
    assert snap["errors"] == 1 and snap["queued"] == 0


def test_the_tape_module_imports_no_persistence_layer():
    """The writer opens its own file and nothing else; no ledger handle can come from here."""
    tree = ast.parse((ROOT / "sniper/services/memecoin_exit_tape.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert not {n for n in names if n.startswith("sniper.persistence") or n == "db"}, names


def test_the_heartbeat_carries_the_tape_counters(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / "hb.json"
    write_heartbeat({}, True, path=str(path),
                    exit_tape={"appended": 3, "dropped": 1, "alive": False})
    assert json.loads(path.read_text())["exit_tape"] == {"appended": 3, "dropped": 1, "alive": False}


def test_start_writes_to_the_state_root_and_the_writer_drains(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(tape_mod, "_TAPE", None)
    t = tape_mod.start()
    try:
        assert t.path == str((tmp_path / "data" / "memecoin_exit_price_tape.db").resolve())
        assert tape_mod.snapshot()["alive"] is True
    finally:
        t._stop.set()


# The tape is measurement only. A module under sniper/ that names its file or its table could start
# reading it into a decision; only the tape itself may (Gamma LOW on da71cb753).
TAPE_READERS = {
    "sniper/services/memecoin_exit_tape.py": "the tape itself",
    "sniper/services/memecoin_exit_followup.py": "part B, its own measurement process; it reads "
                                                 "the exact exited quantity from the confirmations",
}


def test_no_other_sniper_module_names_the_tape_file_or_table():
    hits = []
    for path in sorted((ROOT / "sniper").rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        if rel in TAPE_READERS:
            continue
        text = path.read_text()
        if "memecoin_exit_price_tape" in text or "exit_confirmations" in text:
            hits.append(rel)
    assert hits == [], "a decision path must never read the exit tape: %s" % hits
    for rel in TAPE_READERS:
        assert (ROOT / rel).exists(), "stale TAPE_READERS entry %s" % rel


@pytest.fixture(autouse=True)
def _generic_usd_quote_fixture(monkeypatch):
    """Generic exit plumbing uses a USD-shaped stand-in; ETH canonical accounting has fork coverage."""
    from sniper.execution import evm_wallet as wallet
    monkeypatch.setitem(wallet.CHAIN_QUOTE, "8453",
                        {"token": wallet.CHAIN_USDC["8453"], "decimals": 6, "kind": "usd"})
