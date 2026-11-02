"""Exit price tape stage 3b: Solana post-exit whole-bag values from PumpSwap pool state (B1).

Spec docs/superpowers/specs/2026-09-30-memecoin-exit-price-tape-design.md §4 (B1) and §4b (the 3b
rule): only a live-validated venue (PumpSwap) with a SOL or USDC quote is valued; every other exit is
an `unpriced` follow-up, counted, never dropped. The exited quantity and the fee bps come from our own
exit's SellEvent; each slot reads the pool and its two vaults in ONE getMultipleAccounts.
"""
import base64
import struct

from solders.pubkey import Pubkey

from sniper.services import memecoin_exit_followup as fu

ANCHOR = "2026-10-01T10:00:00+00:00"
ANCHOR_EPOCH = fu._epoch(ANCHOR)
OUR = str(Pubkey.from_bytes(bytes([1] * 32)))
OTHER_USER = str(Pubkey.from_bytes(bytes([2] * 32)))
POOL = str(Pubkey.from_bytes(bytes([3] * 32)))
MINT = str(Pubkey.from_bytes(bytes([4] * 32)))
BASE_VAULT = str(Pubkey.from_bytes(bytes([5] * 32)))
QUOTE_VAULT = str(Pubkey.from_bytes(bytes([6] * 32)))
OTHER_MINT = str(Pubkey.from_bytes(bytes([7] * 32)))


def _sell_event(user=OUR, pool=POOL, base=5_000_000, lp=20, proto=5, creator=5):
    fields = dict.fromkeys(fu.SELL_EVENT_U64, 0) | {"base": base, "lp_bps": lp, "proto_bps": proto}
    raw = (fu.AMM_SELL_DISC + bytes(8) + b"".join(struct.pack("<Q", fields[k]) for k in fu.SELL_EVENT_U64)
           + bytes(Pubkey.from_string(pool)) + bytes(Pubkey.from_string(user)))
    raw += bytes(344 - len(raw)) + struct.pack("<Q", creator)
    return "Program data: " + base64.b64encode(raw + bytes(360 - len(raw) - 0)).decode()


def _tx(events=(), programs=(fu.PUMP_AMM,), block_time=None):
    logs = []
    for e in events:
        logs += ["Program %s invoke [1]" % fu.PUMP_AMM, e, "Program %s success" % fu.PUMP_AMM]
    return {"blockTime": block_time or int(ANCHOR_EPOCH) - 1, "slot": 1000,
            "transaction": {"message": {"accountKeys": [OUR] + list(programs)}},
            "meta": {"logMessages": logs, "loadedAddresses": {"writable": [], "readonly": []}}}


def _pool_account(quote_mint=fu.WSOL, virtual_quote=7_000_000_000):
    raw = bytearray(fu.POOL_ACCOUNT_DISCRIMINATOR + bytes(253))
    for off, key in ((43, MINT), (75, quote_mint), (139, BASE_VAULT), (171, QUOTE_VAULT)):
        raw[off:off + 32] = bytes(Pubkey.from_string(key))
    raw[fu.POOL_VIRTUAL_QUOTE_OFFSET:fu.POOL_VIRTUAL_QUOTE_OFFSET + 16] = (
        virtual_quote.to_bytes(16, "little", signed=True))
    return {"data": [base64.b64encode(bytes(raw)).decode(), "base64"], "owner": fu.PUMP_AMM}


def _token_account(amount):
    raw = bytearray(165)
    struct.pack_into("<Q", raw, 64, amount)
    return {"data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


class Rpc:
    """A scripted Solana host: `answers` per method, consumed in order; 'limit' is an HTTP 429."""

    def __init__(self, **answers):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls = []

    def __call__(self, url, body):
        method = body["method"]
        self.calls.append(method)
        if method == "getTransaction":
            assert body["params"][1]["maxSupportedTransactionVersion"] == 1     # v1 exits read too
        got = self.answers[method].pop(0)
        if got == "rpc_error":
            return 200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32015, "message": "version"}}
        if got == "limit":
            return 429, {}
        if got == "unavailable":
            return 503, {}
        return 200, {"jsonrpc": "2.0", "id": 1, "result": got}


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _worker(tmp_path, rpc, age, sol_usd=150.0):
    clock = Clock()
    reader = fu.SolanaReader("https://research.example/?api-key=r", 2.0, http=rpc, clock=clock.monotonic,
                             sleep=clock.sleep)
    current = [age]
    w = fu.SolanaWorker(reader, str(tmp_path / "tape.db"), sol_usd_fn=lambda: sol_usd,
                        wall=lambda: ("t+%s" % current[0], ANCHOR_EPOCH + current[0]))
    return w, current, reader


def _follow(tx_sig="solsig"):
    return {"fill_id": 1, "position_id": 7, "network_id": "solana", "mint": MINT, "status": "pending",
            "anchor_at": ANCHOR, "entry_notional": 20.0, "qty": 5.0, "qty_atomic": None, "qty_basis": None,
            "exit_pool": None, "entry_pool": None, "tx_sig": tx_sig}


def _rows(tape):
    return [tuple(r) for r in tape.execute(
        "SELECT slot_s, status, quote_output_atomic, quote_asset_usd, value_usd FROM post_exit_quotes "
        "ORDER BY slot_s")]


def _followup(tape):
    return dict(tape.execute("SELECT * FROM exit_followups").fetchone())


def test_a_pumpswap_exit_is_valued_from_the_pool_and_its_vaults_each_slot(tmp_path):
    tape = fu.connect(tmp_path / "tape.db")
    tape.execute("INSERT INTO exit_fills(fill_id, position_id, copied_at) VALUES (1, 7, 't')")
    tape.commit()
    rpc = Rpc(getTransaction=[_tx([_sell_event()])],
              getMultipleAccounts=[{"value": [_pool_account()]},
                                   {"value": [_pool_account(), _token_account(1_000_000_000),
                                              _token_account(3_000_000_000)]}])
    w, now, _r = _worker(tmp_path, rpc, 2.0)
    w.submit(_follow())
    w.run_once(tape)
    f = _followup(tape)
    assert (f["status"], f["route_label"], f["route_basis"], f["provider"]) == (
        "active", "pumpswap", "exit_tx", "b1_pool_state")
    assert (f["qty_atomic"], f["qty_basis"], f["exit_pool"]) == ("5000000", "exit_event", POOL)
    # quote = vault 3e9 + virtual 7e9; base = vault 1e9; sell 5e6 base; fee 20+5+5 = 30 bps
    gross = 10_000_000_000 * 5_000_000 // (1_000_000_000 + 5_000_000)
    net = gross - gross * 30 // 10_000
    assert _rows(tape) == [(2, "ok", str(net), 150.0, net / 1e9 * 150.0)]
    assert rpc.calls == ["getTransaction", "getMultipleAccounts", "getMultipleAccounts"]
    # the block time came with the exit tx: no read after the horizon
    assert tuple(tape.execute("SELECT chain_block, chain_time, chain_time_status FROM exit_fills").fetchone()) == (
        1000, fu.datetime.fromtimestamp(int(ANCHOR_EPOCH) - 1, tz=fu.timezone.utc).isoformat(), "ok")


def test_a_usdc_quoted_pool_is_valued_at_one_dollar(tmp_path):
    tape = fu.connect(tmp_path / "tape.db")
    pool = _pool_account(quote_mint=fu.USDC, virtual_quote=0)
    rpc = Rpc(getTransaction=[_tx([_sell_event()])],
              getMultipleAccounts=[{"value": [pool]},
                                   {"value": [pool, _token_account(1_000_000), _token_account(2_000_000)]}])
    w, _now, _r = _worker(tmp_path, rpc, 2.0, sol_usd=None)
    w.submit(_follow())
    w.run_once(tape)
    (row,) = _rows(tape)
    assert row[1] == "ok" and row[3] == 1.0 and row[4] == int(row[2]) / 1e6


def test_nonpositive_effective_pumpswap_reserves_are_unpriced(tmp_path):
    for virtual in (-4_000_000_000, -3_000_000_000):
        path = tmp_path / str(virtual)
        path.mkdir()
        tape = fu.connect(path / "tape.db")
        pool = _pool_account(virtual_quote=virtual)
        rpc = Rpc(getTransaction=[_tx([_sell_event()])],
                  getMultipleAccounts=[{"value": [pool]}, {"value": [
                      pool, _token_account(1_000_000_000), _token_account(3_000_000_000)]}])
        w, _now, _r = _worker(path, rpc, 2.0)
        w.submit(_follow())
        w.run_once(tape)
        assert _rows(tape) == [(2, "no_route", None, None, None)]
        assert w.stats()["quotes"] == 0


def test_a_negative_virtual_quote_with_positive_effective_reserves_is_valued(tmp_path):
    tape = fu.connect(tmp_path / "tape.db")
    pool = _pool_account(virtual_quote=-1_000_000_000)
    rpc = Rpc(getTransaction=[_tx([_sell_event()])],
              getMultipleAccounts=[{"value": [pool]}, {"value": [
                  pool, _token_account(1_000_000_000), _token_account(3_000_000_000)]}])
    w, _now, _r = _worker(tmp_path, rpc, 2.0)
    w.submit(_follow())
    w.run_once(tape)
    gross = 2_000_000_000 * 5_000_000 // 1_005_000_000
    net = gross - gross * 30 // 10_000
    assert _rows(tape) == [(2, "ok", str(net), 150.0, net / 1e9 * 150.0)]


def test_every_other_exit_is_unpriced_and_counted_with_its_venue(tmp_path):
    cases = {
        "pump_curve": Rpc(getTransaction=[_tx(programs=(fu.PUMP_CURVE,))]),
        "raydium_cp": Rpc(getTransaction=[_tx(programs=(fu.RAYDIUM_CP,))]),
        "other_venue": Rpc(getTransaction=[_tx(programs=("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",))]),
        "multi_leg": Rpc(getTransaction=[_tx([_sell_event(), _sell_event(base=1)])]),
        "pumpswap_quote_other": Rpc(getTransaction=[_tx([_sell_event()])],
                                    getMultipleAccounts=[{"value": [_pool_account(quote_mint=OTHER_MINT)]}]),
        "pumpswap_mint_mismatch": Rpc(getTransaction=[_tx([_sell_event()])],
                                      getMultipleAccounts=[{"value": [_pool_account()]}]),
    }
    for label, rpc in cases.items():
        tape = fu.connect(tmp_path / ("%s.db" % label))
        w = fu.SolanaWorker(fu.SolanaReader("https://r/", 2.0, http=rpc, clock=Clock().monotonic,
                                            sleep=lambda s: None),
                            str(tmp_path / ("%s.db" % label)), sol_usd_fn=lambda: 150.0,
                            wall=lambda: ("t", ANCHOR_EPOCH + 2.0))
        f = _follow()
        if label == "pumpswap_mint_mismatch":
            f["mint"] = OTHER_MINT
        w.submit(f)
        w.run_once(tape)
        got = _followup(tape)
        assert (got["status"], got["route_label"]) == ("unpriced", label), label
        assert _rows(tape) == [] and w.active == {}, label         # final: no slot is read


def test_another_users_sell_event_in_the_same_tx_is_ignored(tmp_path):
    tape = fu.connect(tmp_path / "tape.db")
    rpc = Rpc(getTransaction=[_tx([_sell_event(user=OTHER_USER)], programs=(fu.PUMP_AMM,))])
    w, _now, _r = _worker(tmp_path, rpc, 2.0)
    w.submit(_follow())
    w.run_once(tape)
    assert (_followup(tape)["status"], _followup(tape)["route_label"]) == ("unpriced", "other_venue")


def test_a_limit_holds_the_slot_doubles_the_hold_and_a_good_read_resets_it(tmp_path):
    tape = fu.connect(tmp_path / "tape.db")
    good = {"value": [_pool_account(), _token_account(1_000_000_000), _token_account(3_000_000_000)]}
    rpc = Rpc(getTransaction=[_tx([_sell_event()])],
              getMultipleAccounts=[{"value": [_pool_account()]}, "limit", good])
    w, now, reader = _worker(tmp_path, rpc, 2.0)
    w.submit(_follow())
    w.run_once(tape)                                       # resolve, then slot 2: held
    assert reader.yield_s == 2 * fu.HOLD_MIN_S and _rows(tape)[0][1] == "held"
    now[0] = 4.0
    reader.clock = lambda: 1e9                             # the hold has passed
    w.run_once(tape)
    assert [r[1] for r in _rows(tape)] == ["held", "ok"] and reader.yield_s == fu.HOLD_MIN_S
    assert w.stats()["limits"] == 1 and w.stats()["held_rows"] == 1


def test_an_exit_tx_not_yet_visible_is_retried_next_pass(tmp_path):
    tape = fu.connect(tmp_path / "tape.db")
    rpc = Rpc(getTransaction=[None, _tx([_sell_event()])],
              getMultipleAccounts=[{"value": [_pool_account()]},
                                   {"value": [_pool_account(), _token_account(10 ** 9), _token_account(10 ** 9)]}])
    w, now, _r = _worker(tmp_path, rpc, 2.0)
    w.submit(_follow())
    w.run_once(tape)
    assert _rows(tape) == [] and 1 in w.active
    now[0] = 3.0
    w.run_once(tape)
    assert [r[1] for r in _rows(tape)] == ["ok"]


def test_the_solana_lane_is_off_by_default_and_refuses_a_funded_key():
    funded = "https://mainnet.helius-rpc.com/?api-key=funded-key"
    research = "https://mainnet.helius-rpc.com/?api-key=research-key"
    base = {"MEMECOIN_RESEARCH_HELIUS_RPC_URL": research, "MEMECOIN_SOLANA_RPC_URL": funded}
    assert fu.resolve_solana_host(base) == (None, "off: MEMECOIN_EXIT_TAPE_SOLANA_ENABLED is not 1")
    on = dict(base, MEMECOIN_EXIT_TAPE_SOLANA_ENABLED="1")
    assert fu.resolve_solana_host(on) == (research, None)
    same = dict(on, MEMECOIN_SOLANA_RPC_URL=research)
    url, why = fu.resolve_solana_host(same)
    assert url is None and "funded" in why
    assert fu.resolve_solana_host({"MEMECOIN_EXIT_TAPE_SOLANA_ENABLED": "1"}) == (
        None, "no MEMECOIN_RESEARCH_HELIUS_RPC_URL")


def test_a_solana_exit_opens_pending_when_the_lane_runs(tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u1", source_handle="h", mint=MINT, ticker="T", entry_price=1.0,
                          entry_notional=20.0, entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-10-01T09:00:00+00:00", strategy=cfg.MEMECOIN_REAL_POLICY,
                          execution_mode="real", network_id="solana")
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=1.5, qty=5.0, notional=30.0,
                  fill_source="chain", fraction=1.0, created_at=ANCHOR, tx_sig="solsig")
    tape = fu.connect(tmp_path / "tape.db")
    (row,) = fu.new_exits(c, 0)
    follow = fu.record_exit(tape, c, row, lanes={"solana": object()}, refused={}, now_iso=ANCHOR)
    assert follow["status"] == "pending" and follow["tx_sig"] == "solsig" and follow["mint"] == MINT


def test_a_provider_5xx_is_a_hold_too_never_a_price(tmp_path):
    tape = fu.connect(tmp_path / "tape.db")
    rpc = Rpc(getTransaction=[_tx([_sell_event()])], getMultipleAccounts=[{"value": [_pool_account()]}, "unavailable"])
    w, _now, reader = _worker(tmp_path, rpc, 2.0)
    w.submit(_follow())
    w.run_once(tape)
    assert [r[1] for r in _rows(tape)] == ["held"] and reader.counts["held"] == 1


def test_an_unread_sol_price_is_price_unknown_with_no_value_never_zero(tmp_path):
    """Gamma L2: absent must not read as zero."""
    tape = fu.connect(tmp_path / "tape.db")
    rpc = Rpc(getTransaction=[_tx([_sell_event()])],
              getMultipleAccounts=[{"value": [_pool_account()]},
                                   {"value": [_pool_account(), _token_account(10 ** 9), _token_account(10 ** 9)]}])
    w, _now, _r = _worker(tmp_path, rpc, 2.0, sol_usd=None)
    w.submit(_follow())
    w.run_once(tape)
    (row,) = _rows(tape)
    assert (row[1], row[3], row[4]) == ("price_unknown", None, None) and int(row[2]) > 0


def test_an_exit_tx_the_node_cannot_return_is_named_after_three_errors(tmp_path):
    """Gamma L1: never retried every second to the horizon and left 'missed' with no reason."""
    tape = fu.connect(tmp_path / "tape.db")
    rpc = Rpc(getTransaction=["rpc_error"] * 3)
    w, now, _r = _worker(tmp_path, rpc, 2.0)
    w.submit(_follow())
    for age in (2.0, 3.0, 4.0):
        now[0] = age
        w.run_once(tape)
    assert (_followup(tape)["status"], _followup(tape)["route_label"]) == ("unpriced", "tx_unreadable")
    assert w.active == {} and rpc.calls == ["getTransaction"] * 3
