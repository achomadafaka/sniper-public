"""Solana slot->time backfill: bounded, newest-first, fail-closed, and READ-ONLY by construction."""
import sqlite3
import json

from sniper.persistence import db_ticks as t
from sniper.services import memecoin_solana_blocktime as sb


def test_rpc_transport_identifies_as_browser_for_public_read_nodes(monkeypatch):
    seen = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return json.dumps({"result": 123}).encode()

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: seen.append(request) or Response())
    assert sb._rpc_call("https://read-only", "getSlot", []) == 123
    assert "Mozilla/5.0" in seen[0].get_header("User-agent")


def _tc(slots):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); t.ensure_block_times(c)
    t.insert_ticks(c, [{"chain_id": "solana", "pool": "MINT", "kind": "sol_tx", "block_no": s,
                        "tx_sig": f"sig{s}", "log_index": 0, "maker": None,
                        "amount0": None, "amount1": None,
                        "observed_at": "2026-07-24T00:00:00+00:00"} for s in slots])
    return c


def test_backfills_newest_slots_first_and_respects_the_cap():
    tc = _tc([1, 2, 3, 4, 5])
    seen = []

    def get_bt(slot):
        seen.append(slot)
        return 1_700_000_000 + slot

    n = sb.backfill_solana_block_times(tc, "2026-07-24T00:00:00+00:00", cap=2, get_block_time=get_bt)
    assert n == 2
    # SELECTION is newest-first (that is what the cap depends on); backfill_block_times sorts the
    # batch ascending internally, so assert the chosen SET, not the request order.
    assert set(seen) == {4, 5}
    assert set(t.block_ts_map(tc, "solana", [1, 2, 3, 4, 5])) == {4, 5}


def test_unanswerable_slot_is_left_absent_never_guessed():
    tc = _tc([9])
    n = sb.backfill_solana_block_times(tc, "2026-07-24T00:00:00+00:00", cap=8,
                                       get_block_time=lambda s: None)
    assert n == 0
    assert t.block_ts_map(tc, "solana", [9]) == {}


def test_no_read_endpoint_configured_writes_nothing(monkeypatch):
    monkeypatch.setattr("sniper.settings.memecoin.MEMECOIN_SOLANA_READ_RPC_URL", "", raising=False)
    monkeypatch.setattr("sniper.settings.memecoin.solana_read_rpc", lambda: "", raising=False)
    tc = _tc([1])
    assert sb.backfill_solana_block_times(tc, "2026-07-24T00:00:00+00:00", cap=4) == 0


def test_module_never_touches_the_custody_rpc():
    """The funded keypair exists on this host, so MEMECOIN_SOLANA_RPC_URL is what ARMS the real
    Solana leg. An observation module must never reference it."""
    import inspect
    src = inspect.getsource(sb)
    body = src.split('"""', 2)[-1]          # drop the module docstring, which WARNS about the var
    assert "MEMECOIN_SOLANA_RPC_URL" not in body    # never referenced in executable code
    assert "solana_read_rpc" in body
