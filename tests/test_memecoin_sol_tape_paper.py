"""Paper-position Solana tape widening (2026-07-26): open paper positions join the pull tape,
deduped by mint, real first; vault resolution for fill-less positions seeds from the mint's
largest token accounts and only trusts a candidate the pool decoder validates."""
import sqlite3

from sniper.services import memecoin_solana_amounts as sa
from sniper.services import memecoin_solana_tape as st

COIN = "CoinMint111111111111111111111111111111111111"
POOL_OWNER = "PoolAuth111111111111111111111111111111111111"
COIN_VAULT = "CoinVault11111111111111111111111111111111111"
WSOL_VAULT = "WsolVault11111111111111111111111111111111111"


def _swap_tx():
    entries_pre = [(COIN, POOL_OWNER, COIN_VAULT, 1000), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 500)]
    entries_post = [(COIN, POOL_OWNER, COIN_VAULT, 900), (sa.WSOL, POOL_OWNER, WSOL_VAULT, 560)]
    addrs, seen = ["Payer111111111111111111111111111111111111111"], set()
    for (_m, _o, a, _amt) in entries_pre + entries_post:
        if a not in seen:
            addrs.append(a); seen.add(a)
    def bals(entries):
        return [{"mint": m, "owner": o, "accountIndex": addrs.index(a),
                 "uiTokenAmount": {"amount": str(amt)}} for (m, o, a, amt) in entries]
    return {"transaction": {"message": {"accountKeys": [{"pubkey": a} for a in addrs]}},
            "meta": {"preTokenBalances": bals(entries_pre), "postTokenBalances": bals(entries_post)}}


def _dbs():
    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as t
    tc = sqlite3.connect(":memory:")
    tc.row_factory = sqlite3.Row
    t.ensure_tick_tables(tc)
    ensure_feature_tables(tc)
    st.ensure_sol_cursor(tc)
    m = sqlite3.connect(":memory:")
    m.row_factory = sqlite3.Row
    m.execute("CREATE TABLE memecoin_fills (id INTEGER PRIMARY KEY, position_id INTEGER, "
              "kind TEXT, tx_sig TEXT)")
    m.execute("CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, mint TEXT, "
              "network_id TEXT, execution_mode TEXT, is_open INTEGER, opened_at TEXT)")
    return tc, m


def test_paper_position_resolves_vault_via_largest_accounts():
    tc, m = _dbs()
    pos = {"id": 7, "mint": COIN}                      # paper: no memecoin_fills row
    calls = []

    def call_fn(method, params):
        calls.append(method)
        assert method == "getTokenLargestAccounts"
        return {"value": [{"address": COIN_VAULT}]}

    def get_sigs(addr, params):
        assert addr == COIN_VAULT
        return [{"signature": "sig1"}]

    def get_tx(sig):
        return _swap_tx() if sig == "sig1" else None

    vault = st.resolve_position_pool(tc, m, pos, get_tx, get_sigs_fn=get_sigs, call_fn=call_fn)
    assert vault == COIN_VAULT
    assert calls == ["getTokenLargestAccounts"]
    row = tc.execute("SELECT mint, quote_mint FROM sol_pool_vaults WHERE pool=?",
                     (COIN_VAULT,)).fetchone()
    assert row["mint"] == COIN and row["quote_mint"] == sa.WSOL


def test_paper_position_without_seams_stays_unresolved():
    tc, m = _dbs()
    assert st.resolve_position_pool(tc, m, {"id": 7, "mint": COIN},
                                    lambda sig: None) is None


def test_position_resolution_uses_entry_fill_not_an_earlier_exit_fill():
    tc, m = _dbs()
    m.execute("INSERT INTO memecoin_fills VALUES (1, 7, 'derisk', 'exit-sig')")
    m.execute("INSERT INTO memecoin_fills VALUES (2, 7, 'entry', 'entry-sig')")
    fetched = []

    def get_tx(sig):
        fetched.append(sig)
        return _swap_tx() if sig == "entry-sig" else None

    assert st.resolve_position_pool(tc, m, {"id": 7, "mint": COIN}, get_tx) == COIN_VAULT
    assert fetched == ["entry-sig"]


def test_unbound_position_re_resolves_instead_of_reusing_stale_mint_cache():
    tc, m = _dbs()
    tc.execute("INSERT INTO pool_meta(chain_id,pool,token0,token1,quote_side,resolved_at) "
               "VALUES('solana','STALE',?,'Q',1,'old')", (COIN,))
    # Name the columns. A positional INSERT here broke the moment `program` was added by
    # ALTER on 2026-09-02: 6 values against 7 columns, and the failure is in the test's
    # arrangement rather than in anything it means to assert.
    tc.execute("INSERT INTO sol_pool_vaults"
               "(pool,mint,coin_vault,quote_vault,quote_mint,resolved_at) "
               "VALUES('STALE',?,'STALE','STALE-Q','Q','old')",
               (COIN,))

    vault = st.resolve_position_pool(
        tc, m, {"id": 7, "mint": COIN},
        lambda sig: _swap_tx() if sig == "current" else None,
        get_sigs_fn=lambda _addr, _params: [{"signature": "current"}],
        call_fn=lambda _method, _params: {"value": [{"address": COIN_VAULT}]},
    )

    assert vault == COIN_VAULT
    assert tc.execute("SELECT pool FROM sol_position_pools WHERE position_id=7").fetchone()[0] == COIN_VAULT


def test_tape_pass_includes_paper_mints_deduped(monkeypatch):
    tc, m = _dbs()
    m.execute("INSERT INTO memecoin_positions VALUES (1, ?, 'solana', 'paper', 1, "
              "'2026-07-26T00:00:00+00:00')", (COIN,))
    m.execute("INSERT INTO memecoin_positions VALUES (2, ?, 'solana', 'paper', 1, "
              "'2026-07-26T00:01:00+00:00')", (COIN,))
    monkeypatch.setattr("sniper.services.memecoin_fastlane.open_real_positions", lambda _c: [])
    resolved = []
    monkeypatch.setattr(st, "resolve_position_pool",
                        lambda *a, **k: (resolved.append(a[2]["id"]) or (COIN_VAULT, 0)))
    taped = []
    monkeypatch.setattr(st, "tape_pass_for_vault",
                        lambda *a, **k: taped.append(a[2]) or 0)
    st.solana_tape_pass(m, tc, lambda a, p: [], lambda s: None, "2026-07-26T00:00:00+00:00",
                        tx_budget=10, include_paper=True, call_fn=lambda me, pa: {})
    assert resolved == [1, 2]                          # position identity is never guessed by mint
    assert taped == [COIN_VAULT]                       # same resolved venue is taped once
    resolved.clear()
    taped.clear()
    st.solana_tape_pass(m, tc, lambda a, p: [], lambda s: None, "2026-07-26T00:00:00+00:00",
                        tx_budget=10, include_paper=False, call_fn=lambda me, pa: {})
    assert resolved == [] and taped == []              # paper excluded when flag off


def test_same_mint_on_curve_and_amm_is_not_deduped_to_one_venue(monkeypatch):
    tc, m = _dbs()
    m.execute("INSERT INTO memecoin_positions VALUES (1, ?, 'solana', 'paper', 1, "
              "'2026-07-26T00:00:00+00:00')", (COIN,))
    m.execute("INSERT INTO memecoin_positions VALUES (2, ?, 'solana', 'paper', 1, "
              "'2026-07-26T00:01:00+00:00')", (COIN,))
    monkeypatch.setattr("sniper.services.memecoin_fastlane.open_real_positions", lambda _c: [])
    monkeypatch.setattr(
        st, "resolve_position_pool",
        lambda *a, **k: (("curve-vault" if a[2]["id"] == 1 else "amm-vault"), 0))
    taped = []
    monkeypatch.setattr(st, "tape_pass_for_vault",
                        lambda *a, **k: taped.append(a[2]) or 0)
    st.solana_tape_pass(m, tc, lambda *_a: [], lambda _s: None,
                        "2026-07-26T00:00:00+00:00", tx_budget=10,
                        include_paper=True, call_fn=lambda *_a: {})
    assert taped == ["curve-vault", "amm-vault"]


def test_position_without_a_valid_history_floor_is_not_walked_to_genesis(monkeypatch):
    tc, m = _dbs()
    m.execute("INSERT INTO memecoin_positions VALUES (1, ?, 'solana', 'paper', 1, NULL)", (COIN,))
    monkeypatch.setattr("sniper.services.memecoin_fastlane.open_real_positions", lambda _c: [])
    monkeypatch.setattr(st, "resolve_position_pool", lambda *a, **k: (COIN_VAULT, 0))
    taped = []
    monkeypatch.setattr(st, "tape_pass_for_vault",
                        lambda *a, **k: taped.append(a[2]) or 0)

    st.solana_tape_pass(m, tc, lambda *_a: [], lambda _s: None,
                        "2026-07-26T00:00:00+00:00", tx_budget=10,
                        include_paper=True, call_fn=lambda *_a: {})

    assert taped == []


def test_resolver_prefers_inventory_holding_pool_over_zero_balance_router():
    """Live 2026-07-26: USDC-quoted pool; a router owner carried coin+USDC ATAs both at ZERO post
    balance, tripping the two-candidate ambiguity guard on every tx of the coin."""
    USDC = sa.USDC
    ROUTER = "Router111111111111111111111111111111111111111"
    tx = {
        "transaction": {"message": {"accountKeys": [
            {"pubkey": a} for a in (
                "Payer111111111111111111111111111111111111111",
                "RCoin1111111111111111111111111111111111111111",
                "RUsdc1111111111111111111111111111111111111111",
                COIN_VAULT, "PoolUsdc111111111111111111111111111111111111")]}},
        "meta": {
            "preTokenBalances": [
                {"mint": COIN, "owner": ROUTER, "accountIndex": 1,
                 "uiTokenAmount": {"amount": "5"}},
                {"mint": USDC, "owner": ROUTER, "accountIndex": 2,
                 "uiTokenAmount": {"amount": "7"}},
                {"mint": COIN, "owner": POOL_OWNER, "accountIndex": 3,
                 "uiTokenAmount": {"amount": "1000"}},
                {"mint": USDC, "owner": POOL_OWNER, "accountIndex": 4,
                 "uiTokenAmount": {"amount": "500"}},
            ],
            "postTokenBalances": [
                {"mint": COIN, "owner": ROUTER, "accountIndex": 1,
                 "uiTokenAmount": {"amount": "0"}},
                {"mint": USDC, "owner": ROUTER, "accountIndex": 2,
                 "uiTokenAmount": {"amount": "0"}},
                {"mint": COIN, "owner": POOL_OWNER, "accountIndex": 3,
                 "uiTokenAmount": {"amount": "900"}},
                {"mint": USDC, "owner": POOL_OWNER, "accountIndex": 4,
                 "uiTokenAmount": {"amount": "560"}},
            ],
        },
    }
    got = sa.resolve_pool_accounts(tx, COIN)
    assert got == (POOL_OWNER, COIN_VAULT, USDC, "PoolUsdc111111111111111111111111111111111111")
    # all-zero double candidate stays fail-closed
    for tb in tx["meta"]["postTokenBalances"]:
        tb["uiTokenAmount"]["amount"] = "0"
    assert sa.resolve_pool_accounts(tx, COIN) is None


def test_resolver_rejects_single_zero_balance_router_candidate():
    tx = _swap_tx()
    for tb in tx["meta"]["postTokenBalances"]:
        tb["owner"] = "RouterOnly11111111111111111111111111111111111"
        tb["uiTokenAmount"]["amount"] = "0"
    assert sa.resolve_pool_accounts(tx, COIN) is None


def test_seed_search_skips_undecodable_tx_and_tries_next():
    """Codex review 2026-07-26: the seed search returned the FIRST fetched tx before decoding it —
    one irrelevant transfer from the largest account blocked resolution forever."""
    tc, m = _dbs()
    junk = {"transaction": {"message": {"accountKeys": []}}, "meta": {}}

    def call_fn(method, params):
        return {"value": [{"address": COIN_VAULT}]}

    def get_sigs(addr, params):
        return [{"signature": "junk"}, {"signature": "swap"}]

    def get_tx(sig):
        return junk if sig == "junk" else _swap_tx()

    vault = st.resolve_position_pool(tc, m, {"id": 8, "mint": COIN}, get_tx,
                                     get_sigs_fn=get_sigs, call_fn=call_fn)
    assert vault == COIN_VAULT


def test_seed_budget_is_distributed_across_candidate_accounts():
    accounts = ["First111", COIN_VAULT]

    def call_fn(_method, _params):
        return {"value": [{"address": a} for a in accounts]}

    def get_sigs(addr, _params):
        return ([{"signature": "junk1"}, {"signature": "junk2"}]
                if addr == accounts[0] else [{"signature": "swap"}])

    tx, spent = st._seed_tx_via_mint_activity(
        COIN, get_sigs, lambda sig: _swap_tx() if sig == "swap" else {"meta": {}}, call_fn,
        max_fetch=2)
    assert tx == _swap_tx()
    assert spent == 2
