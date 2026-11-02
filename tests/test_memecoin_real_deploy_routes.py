"""Real-deploy routes (2026-07-10 spec): per-wallet real toggle, manual real buy (full gate
chain, never a modeled fill), and the accounting endpoint (cash/deployed/unrealized/realized)."""
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from _web_csrf import csrf_client


def _client(ctx_extra=None):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    ctx = {"conn": c}
    if ctx_extra:
        ctx.update(ctx_extra)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint(ctx))
    return csrf_client(app), c


def _post_buy(client, payload):
    token = client.get("/api/memecoin/real/status").get_json()["csrf_token"]
    return client.post("/api/memecoin/real/buy", json=payload,
                       headers={"X-CSRF-Token": token})


def _open_real(c, mint="So1Mint", uid="u1", entry_notional=50.0, entry_price=0.02,
               network_id="solana"):
    pid = m.open_position(c, source_user_id=uid, source_handle="h", mint=mint, ticker="T",
                          entry_price=entry_price, entry_notional=entry_notional,
                          entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-09T00:00:00Z", strategy="alpha_b",
                          execution_mode="real", network_id=network_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=entry_price,
                  qty=entry_notional / entry_price, notional=entry_notional,
                  fill_source="chain", fraction=1.0, created_at="2026-07-09T00:00:00Z", tx_sig="E")
    # A HEALTHY position: freshly marked at entry. Sizing values an open position at its last FRESH
    # executable mark since 2026-09-06, so an unmarked fixture is a DEAD bag worth zero — which is
    # not what these route tests are about.
    from sniper.util.clock import real_utc_now_iso
    c.execute("UPDATE memecoin_positions SET last_executable_price=?, last_executable_at=? "
              "WHERE id=?", (entry_price, real_utc_now_iso(), pid))
    c.commit()
    return pid


# --- per-wallet real toggle ---

def test_real_toggle_roundtrip():
    cl, c = _client({
        "follow_user_fn": lambda a: "me",
        "follow_following_ids_fn": lambda a: set(),
        "follow_target_fn": lambda a, you, target: True,
    })
    m.add_wallet(c, "u1", "alice", "")
    r = cl.post("/api/memecoin/wallets/u1/real", json={"approved": True, "chains": ["solana"], "force": True})
    assert r.status_code == 200 and r.get_json()["ok"]
    ws = {w["source_user_id"]: w for w in cl.get("/api/memecoin/wallets").get_json()["wallets"]}
    assert ws["u1"]["real_approved"] == 1
    cl.post("/api/memecoin/wallets/u1/real", json={"approved": False})
    assert m.real_approved_user_ids(c) == set()


# --- per-position mirror-exit toggle ---

def test_mirror_toggle_roundtrip():
    cl, c = _client()
    pid = _open_real(c)
    # default: mirror active -> positions board reports mirror_disabled False
    p0 = {p["id"]: p for p in cl.get("/api/memecoin/real/positions").get_json()["positions"]}[pid]
    assert p0["mirror_disabled"] is False
    # disable
    r = cl.post(f"/api/memecoin/real/positions/{pid}/mirror", json={"disabled": True})
    assert r.status_code == 200 and r.get_json()["mirror_disabled"] is True
    assert m.get_position(c, pid)["mirror_disabled"] == 1
    p1 = {p["id"]: p for p in cl.get("/api/memecoin/real/positions").get_json()["positions"]}[pid]
    assert p1["mirror_disabled"] is True
    # re-enable
    r = cl.post(f"/api/memecoin/real/positions/{pid}/mirror", json={"disabled": False})
    assert r.status_code == 200 and r.get_json()["mirror_disabled"] is False
    assert m.get_position(c, pid)["mirror_disabled"] == 0


def test_mirror_toggle_404_for_paper_position():
    cl, c = _client()
    pid = m.open_position(c, source_user_id="u2", source_handle="h", mint="Pap", ticker="T",
                          entry_price=0.02, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z",
                          strategy="alpha_b")   # PAPER
    assert cl.post(f"/api/memecoin/real/positions/{pid}/mirror", json={"disabled": True}).status_code == 404


# --- manual real buy ---

def test_real_buy_without_binding_fails_loud():
    cl, _ = _client()   # no real_buy_fn bound (disarmed / no creds)
    r = _post_buy(cl, {"mint": "So1Mint", "source_user_id": "u1"})
    assert r.status_code == 503


def test_real_buy_requires_a_mint_but_not_a_trader():
    """source_user_id became OPTIONAL on 2026-08-23 (operator): naming a trader only buys the
    mirror-close on their later sell, and manual entry never used it for authority. The mint is
    still required -- there is nothing to buy without it. Asserted rather than dropped so a
    silent return to mint+uid still fails here."""
    cl, _ = _client({"real_buy_fn": lambda *a, **k: {"status": "filled"}})
    assert _post_buy(cl, {"mint": "So1Mint"}).status_code == 200
    assert _post_buy(cl, {"source_user_id": "u1"}).status_code == 400
    assert _post_buy(cl, {}).status_code == 400


def test_real_buy_rejects_a_nonsense_size():
    cl, _ = _client({"real_buy_fn": lambda *a, **k: {"status": "filled"}})
    for bad in ("abc", 0, -5):
        r = _post_buy(cl, {"mint": "So1Mint", "size_usd": bad})
        assert r.status_code == 400, bad


def test_real_buy_passes_through_gate_chain():
    seen = {}

    def buy_fn(conn, mint, source_user_id, ticker=None, network_id=None):
        seen.update(mint=mint, uid=source_user_id, ticker=ticker, network=network_id)
        return {"status": "filled", "sig": "SIG", "fill_price": 0.02}

    cl, _ = _client({"real_buy_fn": buy_fn})
    r = _post_buy(cl, {"mint": "So1Mint", "source_user_id": "u1", "ticker": "PEPE"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert seen["mint"] == "So1Mint" and seen["uid"] == "u1"
    # a gate rejection surfaces honestly (ok=False + reason), not a 500
    cl2, _ = _client({"real_buy_fn": lambda *a, **k: {"status": "rejected", "reason": "kill switch"}})
    r2 = _post_buy(cl2, {"mint": "So1Mint", "source_user_id": "u1"})
    assert r2.status_code == 200 and r2.get_json()["ok"] is False


# --- B1: a 0x mint must route to the RIGHT chain, never blind-default to Base ---

def _buy_capture():
    seen = {}

    def buy_fn(conn, mint, source_user_id, ticker=None, network_id=None):
        seen["network"] = network_id
        return {"status": "filled", "sig": "S"}
    return buy_fn, seen


def test_real_buy_evm_uses_explicit_network_id():
    buy_fn, seen = _buy_capture()
    cl, _ = _client({"real_buy_fn": buy_fn})
    _post_buy(cl, {"mint": "0xToken", "source_user_id": "u1", "network_id": "4663"})
    assert seen["network"] == "4663"                     # explicit chain honored, NOT defaulted to 8453


def test_real_buy_evm_resolves_chain_from_signal_not_base():
    """No network_id in the request: resolve the chain from the feed event that produced the mint —
    a Robinhood-4663 token must NOT blind-default to Base (8453) and route to the wrong wallet."""
    buy_fn, seen = _buy_capture()
    cl, c = _client({"real_buy_fn": buy_fn})
    c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,mint,network_id,created_at,side) "
              "VALUES('e1','u1','0xToken','4663','2026-07-15T00:00:00Z','buy')")
    c.commit()
    r = _post_buy(cl, {"mint": "0xToken", "source_user_id": "u1"})
    assert r.status_code == 200
    assert seen["network"] == "4663"                     # resolved from the signal, not 8453


def test_real_buy_evm_unknown_chain_is_refused_not_guessed():
    """0x mint with no network_id and no signal on record: refuse (400) rather than guess a chain
    for a real-money swap."""
    buy_fn, seen = _buy_capture()
    cl, _ = _client({"real_buy_fn": buy_fn})
    r = _post_buy(cl, {"mint": "0xUnknown", "source_user_id": "u1"})
    assert r.status_code == 400 and "network_id" in r.get_json()["error"]
    assert "network" not in seen                          # buy_fn never called with a guessed chain


def test_real_buy_evm_ambiguous_chain_is_refused_not_routed_to_latest():
    """The same 0x address may exist on multiple chains. Without an explicit chain the API cannot
    know which signal the operator means; 'latest wins' can spend from the wrong wallet."""
    buy_fn, seen = _buy_capture()
    cl, c = _client({"real_buy_fn": buy_fn})
    c.executemany(
        "INSERT INTO memecoin_events(fomo_event_id,source_user_id,mint,network_id,created_at,side) "
        "VALUES(?,?,?,?,?,'buy')",
        [
            ("e1", "u1", "0xSame", "8453", "2026-07-15T00:00:00Z"),
            ("e2", "u1", "0xSame", "4663", "2026-07-15T00:01:00Z"),
        ],
    )
    c.commit()

    r = _post_buy(cl, {"mint": "0xSame", "source_user_id": "u1"})

    assert r.status_code == 400 and "network_id" in r.get_json()["error"]
    assert "network" not in seen


# --- manual close on a real position runs the demote check ---

def test_manual_real_close_triggers_demote(monkeypatch):
    # 2026-09-05: a manual close is an INTENT; the runner's drain sells and runs the demote check.
    from sniper.services import memecoin_manual_exit_intents as xi
    from sniper.services.memecoin_real_runner import check_wallet_demotion
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DEMOTE_LOSS_USD", 100.0)

    def exit_fn(conn, pos, reason, now):
        m.claim_close(conn, pos["id"])
        m.finalize_close(conn, pos["id"], now, -150.0, exit_reason=reason)
        return {"status": "closed", "realized": -150.0, "sig": "X"}

    cl, c = _client({"real_exit_intent_fn": xi.make_enqueue_exit_fn(lambda: "2026-07-09T00:00:01Z")})
    m.add_wallet(c, "u1", "alice", "")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    pid = _open_real(c, uid="u1")
    approved = lambda: c.execute("SELECT real_approved FROM memecoin_wallets WHERE source_user_id='u1'").fetchone()[0]
    r = cl.post(f"/api/memecoin/positions/{pid}/close")
    assert r.status_code == 202 and approved() == 1 and m.get_position(c, pid)["is_open"] == 1   # nothing sold yet
    done = xi.drain(c, exit_fn, lambda *a: {"status": "no_partial"}, now_fn=lambda: "2026-07-09T00:00:05Z",
                    position_fn=m.get_position, demotion_fn=check_wallet_demotion)
    assert done and done[0][1] == "done"
    assert approved() == 0                                # -150 <= -100 -> auto-demoted by the drain


# --- accounting endpoint ---

def test_arc_accounting_uses_funded_size_and_counts_usdc_once(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"5042"})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {"5042": "fraction"})
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION_BY_CHAIN", {"5042": 0.5})
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {"5042": 3})
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN", {"5042": False})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_RESERVE_MIN_NATIVE_BY_CHAIN", {"5042": 10.0})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {"5042": 150.0})
    balance = {"cash_usd": 110.0, "native": 110.0, "native_usd": 110.0}
    cl, _ = _client({"acct_stats_fn": lambda mints: {},
                     "acct_balances_fn": lambda: {"5042": balance}})
    result = cl.get("/api/memecoin/real/accounting").get_json()
    arc = result["by_network"]["5042"]
    assert arc["equity"] == result["totals"]["equity"] == 110.0
    assert arc["clip_next"] == 50.0
    assert arc["sizing_equity"] == 100.0
    assert arc["max_concurrent"] == 3
    assert arc["gas_required"] == 10.0
    assert arc["gas_usd"] == result["totals"]["gas_usd"] == 0.0
    balance.update(cash_usd=1010.0, native=1010.0, native_usd=1010.0)
    assert cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["5042"]["clip_next"] == 150.0

def test_real_accounting_endpoint(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fraction")
    monkeypatch.setattr(cfg, "MEMECOIN_BANKROLL_FRACTION", 0.13)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)

    def stats_fn(mints):
        return {"So1Mint": {"price": 0.03}}              # EVM mint unquotable

    def balances_fn():
        return {"solana": {"cash_usd": 340.0, "native": 0.05},
                "8453": {"cash_usd": 247.42, "native": 0.005}}

    cl, c = _client({"acct_stats_fn": stats_fn, "acct_balances_fn": balances_fn})
    _open_real(c, mint="So1Mint", entry_notional=50.0, entry_price=0.02)     # +25 unrealized
    _open_real(c, mint="0xBase", entry_notional=36.97, entry_price=0.1, network_id="8453")
    pid = _open_real(c, mint="So1Done", entry_notional=40.0)
    m.claim_close(c, pid)
    m.finalize_close(c, pid, "2026-07-09T01:00:00Z", 12.5, exit_reason="b_mirror")

    j = cl.get("/api/memecoin/real/accounting").get_json()
    sol = j["by_network"]["solana"]
    assert sol["cash"] == 340.0
    assert sol["unrealized"] == 25.0
    assert sol["equity"] == 340.0 + 50.0 + 25.0
    assert sol["clip_next"] == 50.70                     # 13% of (340 free + 50 deployed)
    assert sol["sizing_equity"] == 390.0
    assert sol["manual_presets"] == {"0.13": 50.7, "0.26": 101.4}
    base = j["by_network"]["8453"]
    assert base["cash"] == 247.42
    assert base["unquotable_n"] == 1
    assert base["manual_presets"] == {"0.13": 36.97, "0.26": 73.94}
    assert j["realized_total"] == 12.5
    assert j["slots"]["used"] == 2
    assert j["totals"]["equity"] == sol["equity"] + base["equity"]


def test_real_accounting_uses_each_chain_slot_divisor(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {"4663": 2})
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 5.0)

    cl, _ = _client({
        "acct_stats_fn": lambda mints: {},
        "acct_balances_fn": lambda: {
            "solana": {"cash_usd": 100.0, "native": 0.1},
            "4663": {"cash_usd": 100.0, "native": 0.01},
        },
    })

    by_network = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]
    assert by_network["solana"]["clip_next"] == 100.0
    assert by_network["4663"]["clip_next"] == 50.0


def test_real_accounting_marks_unquotable_evm_via_venue_quote(monkeypatch):
    """M1: a DexScreener-unquotable 4663 bag is marked at its EXECUTABLE sell value (the venue
    quote), NOT cost basis — so equity reflects what it could actually be sold for. Without the
    mark, a −20% bag would still show at cost and overstate equity."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")

    def stats_fn(mints):
        return {}                                        # nothing quotable via DexScreener

    def balances_fn():
        return {"4663": {"cash_usd": 100.0, "native": 0.01}}

    def mark_fn(positions):                              # venue sell-quote: bag now worth $0.08/token
        from sniper.execution.solana_quote import norm_mint
        return {(p["network_id"], norm_mint(p["mint"])): 0.08
                for p in positions if p["mint"] == "0xBag"}

    cl, c = _client({"acct_stats_fn": stats_fn, "acct_balances_fn": balances_fn,
                     "real_mark_fn": mark_fn})
    _open_real(c, mint="0xBag", entry_notional=30.0, entry_price=0.1, network_id="4663")  # qty 300
    base = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["4663"]
    assert base["unquotable_n"] == 0                     # marked, no longer counted unquotable
    assert abs(base["unrealized"] - (-6.0)) < 1e-9       # 300 * (0.08 − 0.10) = −6 (a real −20%)
    assert base["equity"] == 100.0 + 30.0 - 6.0          # cash + cost + unrealized == cash + qty*mark


def test_real_accounting_falls_back_to_stored_marks_when_venue_quote_dead():
    """The 0x/venue preview quote died 2026-07-16 (key retired), so mark_fn can't price 4663 bags.
    A blind-chain bag that neither DexScreener nor the venue quote can price must still be VALUED
    from the newest stored price mark (memecoin_position_marks — now fed by GeckoTerminal), exactly
    as /real/positions does, instead of being counted unquotable at cost basis."""
    def stats_fn(mints):
        return {}                                        # DexScreener: nothing
    def dead_mark_fn(positions):
        return {}                                        # venue quote dead -> no marks
    def balances_fn():
        return {"4663": {"cash_usd": 100.0, "native": 0.01}}
    cl, c = _client({"acct_stats_fn": stats_fn, "acct_balances_fn": balances_fn,
                     "real_mark_fn": dead_mark_fn})
    _open_real(c, mint="0xbag", entry_notional=30.0, entry_price=0.1, network_id="4663")  # qty 300
    # a fresh stored mark, as bracket_pass now writes via GeckoTerminal: $0.08/token
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality) "
              "VALUES('4663','0xbag','2026-07-16T00:00:00+00:00',0.08,'geckoterminal','live')")
    c.commit()
    base = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["4663"]
    assert base["unquotable_n"] == 0                      # valued from the stored mark, not unquotable
    assert abs(base["unrealized"] - (-6.0)) < 1e-9        # 300 * (0.08 − 0.10) = −6


def test_real_accounting_route_keeps_same_address_marks_chain_scoped():
    def stats_fn(keys):
        return {
            ("8453", "0xabc"): {"price": 0.2, "liquidity": 1e12},
            ("4663", "0xabc"): {"price": 0.3, "liquidity": 1e12},
        }

    cl, c = _client({
        "acct_stats_fn": stats_fn,
        "acct_balances_fn": lambda: {
            "8453": {"cash_usd": 0.0, "native": 1.0},
            "4663": {"cash_usd": 0.0, "native": 1.0},
        },
    })
    _open_real(c, mint="0xabc", entry_notional=50.0, entry_price=0.1, network_id="8453")
    _open_real(c, mint="0xabc", entry_notional=50.0, entry_price=0.1, network_id="4663")

    by_network = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]

    assert by_network["8453"]["unrealized"] == 50.0
    assert by_network["4663"]["unrealized"] == 100.0


def test_real_accounting_survives_balance_failure():
    def bad_balances():
        raise RuntimeError("rpc down")

    cl, c = _client({"acct_stats_fn": lambda mints: {}, "acct_balances_fn": bad_balances})
    _open_real(c, mint="So1Mint")
    j = cl.get("/api/memecoin/real/accounting").get_json()
    assert j["by_network"]["solana"]["cash"] is None     # honest: unknown, never fabricated
    assert j["by_network"]["solana"]["equity"] is None
    assert j["by_network"]["solana"]["deployed"] == 50.0  # ledger facts still served


def test_live_balance_reader_prices_weth_quoted_chains(monkeypatch, tmp_path):
    """4663 holds its bankroll in WETH (zero USDC pools). The cockpit reader must price the chain's
    QUOTE token — reading USDC there reported cash=$0/equity=$0 on a funded wallet (2026-07-14)."""
    from sniper.execution import evm_wallet as W
    from sniper.settings import memecoin as cfg

    keyfile = tmp_path / "evm_keypair.json"
    keyfile.write_text("{}")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(keyfile))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")          # solana leg out of scope here
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: "http://rpc")
    monkeypatch.setattr(W, "load_keypair", lambda path: object())
    monkeypatch.setattr(W, "address_str", lambda acct: "0x" + "11" * 20)
    monkeypatch.setattr(W, "native_balance", lambda owner, rpc, http=None: 0.0138)
    monkeypatch.setattr(W, "native_balance_or_none",
                        lambda owner, rpc, http=None, **_kwargs: 0.0138)
    monkeypatch.setattr(W, "eth_usd_price", lambda http=None: 1877.17)
    monkeypatch.setattr(W, "erc20_balance_checked",
                        lambda owner, token, rpc, http=None, **_metadata:
                        0.021908 if token == W.CHAIN_QUOTE["4663"]["token"] else 0.0)

    cl, _c = _client({"acct_stats_fn": lambda mints: {}})
    v = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["4663"]
    assert abs(v["cash"] - 0.021908 * 1877.17) < 1e-6                 # WETH x ETH/USD, NOT 0.0
    assert v["gas_usd"] == 0.0138 * 1877.17
    assert v["equity"] == 67.03                                       # WETH + native ETH
    assert v["gas"] == 0.0138                                         # native ETH stays a gas number


def test_display_retries_a_failed_weth_read_but_preserves_zero_and_unknown(monkeypatch, tmp_path):
    from sniper.execution import evm_wallet as W
    from sniper.settings import memecoin as cfg

    keyfile = tmp_path / "evm_keypair.json"
    keyfile.write_text("{}")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"1"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(keyfile))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: "http://rpc")
    monkeypatch.setattr(W, "load_keypair", lambda path: object())
    monkeypatch.setattr(W, "address_str", lambda acct: "0x" + "11" * 20)
    monkeypatch.setattr(W, "native_balance_or_none", lambda *a, **kw: 0.003)
    monkeypatch.setattr(W, "native_usd_price", lambda chain: 2500.0)
    calls, answers = [], []

    def balance(owner, token, rpc, **kw):
        calls.append(token)
        value = answers.pop(0)
        if value is None:
            raise RuntimeError("temporary RPC failure")
        return value

    monkeypatch.setattr(W, "erc20_balance_checked", balance)
    cl, c = _client({"acct_stats_fn": lambda mints: {}})
    try:
        for reads, want, count in (([None, 0.04], 100.0, 2), ([0.0], 0.0, 1),
                                   ([None, None], None, 2)):
            calls.clear()
            answers[:] = reads
            row = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["1"]
            assert row["cash"] == want
            assert calls == [W.CHAIN_QUOTE["1"]["token"]] * count
    finally:
        c.close()


def test_display_does_not_re_ask_a_rate_limited_weth_read(monkeypatch, tmp_path):
    """Gamma LOW on 24d2b8a6f: the page re-polls every 5 s while cash is null, so an immediate second
    read after a 429 doubles each tab's reads into the funded gate's quota (Infura on chain 1). A 429
    is read once; any other failure keeps its second chance."""
    from sniper.execution import evm_wallet as W
    from sniper.settings import memecoin as cfg

    keyfile = tmp_path / "evm_keypair.json"
    keyfile.write_text("{}")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"1"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(keyfile))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: "http://rpc")
    monkeypatch.setattr(W, "load_keypair", lambda path: object())
    monkeypatch.setattr(W, "address_str", lambda acct: "0x" + "11" * 20)
    monkeypatch.setattr(W, "native_balance_or_none", lambda *a, **kw: 0.003)
    monkeypatch.setattr(W, "native_usd_price", lambda chain: 2500.0)
    calls, failure = [], {}

    def balance(owner, token, rpc, **kw):
        calls.append(token)
        raise W._http_failure(failure["status"])

    monkeypatch.setattr(W, "erc20_balance_checked", balance)
    cl, c = _client({"acct_stats_fn": lambda mints: {}})
    try:
        for status, count in ((429, 1), (503, 2)):
            calls.clear()
            failure["status"] = status
            row = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["1"]
            assert row["cash"] is None, "unknown, never 0.0"
            assert len(calls) == count, status
    finally:
        c.close()


def test_live_balance_reader_reports_unpriceable_chain_as_unknown(monkeypatch, tmp_path):
    """ETH/USD lookup down -> the WETH bankroll is unknown, not zero. Show it as missing."""
    from sniper.execution import evm_wallet as W
    from sniper.settings import memecoin as cfg

    keyfile = tmp_path / "evm_keypair.json"
    keyfile.write_text("{}")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(keyfile))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "")
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: "http://rpc")
    monkeypatch.setattr(W, "load_keypair", lambda path: object())
    monkeypatch.setattr(W, "address_str", lambda acct: "0x" + "11" * 20)
    monkeypatch.setattr(W, "native_balance", lambda owner, rpc, http=None: 0.0138)
    monkeypatch.setattr(W, "eth_usd_price", lambda http=None: None)   # price feed down
    monkeypatch.setattr(W, "erc20_balance", lambda owner, token, rpc, http=None: 0.021908)

    cl, _c = _client({"acct_stats_fn": lambda mints: {}})
    j = cl.get("/api/memecoin/real/accounting").get_json()
    assert j["by_network"]["4663"]["cash"] is None
    assert j["by_network"]["4663"]["equity"] is None
    assert "4663" in j["totals"]["balances_missing"]


def test_real_accounting_reports_the_sol_the_bot_cannot_spend(monkeypatch):
    """1.8558 SOL ($223.07) sat beside $262.47 USDC. Sizing reads USDC + positions only, so the SOL
    above the swap tool's gas keep is reported, never folded into sizing_equity or the presets."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)
    cl, _c = _client({"acct_stats_fn": lambda mints: {},
                      "acct_balances_fn": lambda: {"solana": {
                          "cash_usd": 262.47, "native": 1.8558, "native_usd": 223.07}}})
    sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
    assert sol["sizing_equity"] == 262.47                       # SOL is not in it
    assert sol["native_keep"] == 0.14
    assert sol["native_swappable_usd"] == round((1.8558 - 0.14) * 223.07 / 1.8558, 2)
    assert sol["equity"] == round(262.47 + 223.07, 2)             # but it is equity


def test_the_slot_lane_sol_buffer_is_not_reported_as_idle(monkeypatch):
    """With the native SOL execution buffer on (cap $175), SOL up to keep + cap is the lane's
    working float: only the excess over the cap may be offered as a swap to USDC, and none when
    the SOL above the keep is all buffer."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SOL_EXEC_BUFFER_CAP_USD", 175.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOL_EXEC_BUFFER_HEADROOM_USD", 5.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_SLOT_LANE_ENABLED", "1")
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")
    cl, _c = _client({"acct_stats_fn": lambda mints: {},
                      "acct_balances_fn": lambda: {"solana": {
                          "cash_usd": 262.47, "native": 1.8558, "native_usd": 223.07}}})
    sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
    above_keep = (1.8558 - 0.14) * 223.07 / 1.8558
    assert sol["native_swappable_usd"] == round(above_keep - 175.0 - 5.0, 2)
    cl, _c = _client({"acct_stats_fn": lambda mints: {},
                      "acct_balances_fn": lambda: {"solana": {
                          "cash_usd": 262.47, "native": 0.568, "native_usd": 68.30}}})
    sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
    assert sol["native_swappable_usd"] == 0.0            # $51 above the keep is all buffer


def test_the_ui_gas_keep_is_the_swap_tools_keep():
    import pathlib
    import re
    from sniper.routes import api_memecoin
    src = (pathlib.Path(__file__).resolve().parents[1] / "scripts"
           / "memecoin_swap_sol_usdc.py").read_text()
    keep = float(re.search(r'"--keep", type=float, default=([0-9.]+)', src).group(1))
    assert api_memecoin._SOL_GAS_KEEP == keep
