from sniper.services import memecoin_solana_amounts as sa
from sniper.services import memecoin_solana_tape as st


MINT = "PumpMint11111111111111111111111111111111111"
CURVE = "Curve11111111111111111111111111111111111111"
VAULT = "CurveCoinVault1111111111111111111111111111111"
PAYER = "Payer11111111111111111111111111111111111111"


def _curve_tx(*, coin_pre=1000, coin_post=900, lamports_pre=10_000,
              lamports_post=10_500, program=sa.PUMP_PROGRAM_ID, second=False):
    keys = [PAYER, CURVE, VAULT]
    pre_tokens = [{"mint": MINT, "owner": CURVE, "accountIndex": 2,
                   "uiTokenAmount": {"amount": str(coin_pre)}}]
    post_tokens = [{"mint": MINT, "owner": CURVE, "accountIndex": 2,
                    "uiTokenAmount": {"amount": str(coin_post)}}]
    if second:
        keys += ["CurveTwo1111111111111111111111111111111111", "VaultTwo1111111111111111111111111111111111"]
        pre_tokens.append({"mint": MINT, "owner": keys[3], "accountIndex": 4,
                           "uiTokenAmount": {"amount": "500"}})
        post_tokens.append({"mint": MINT, "owner": keys[3], "accountIndex": 4,
                            "uiTokenAmount": {"amount": "400"}})
    pre_balances = [1_000_000, lamports_pre, 2_039_280] + ([20_000, 2_039_280] if second else [])
    post_balances = [999_995, lamports_post, 2_039_280] + ([20_500, 2_039_280] if second else [])
    return {
        "transaction": {"message": {
            "accountKeys": [{"pubkey": key} for key in keys],
            "instructions": [{"programId": program}],
        }},
        "meta": {
            "preTokenBalances": pre_tokens,
            "postTokenBalances": post_tokens,
            "preBalances": pre_balances,
            "postBalances": post_balances,
            "logMessages": [f"Program {program} invoke [1]"],
        },
    }


def test_pump_curve_buy_is_transaction_grounded_and_emits_swap_plus_sync():
    tx = _curve_tx()
    assert sa.resolve_pump_curve_accounts(tx, MINT) == (
        CURVE, VAULT, sa.NATIVE_SOL, CURVE)
    rows = sa.pump_curve_rows(tx, MINT, VAULT, CURVE, slot=10, signature="sig",
                              observed_at="now")
    assert [row["kind"] for row in rows] == ["sol_curve_sync", "sol_curve_swap"]
    assert rows[1]["amount0"] == "-100"
    assert rows[1]["amount1"] == "500"
    assert rows[1]["venue_kind"] == "pump_curve"


def test_pump_curve_sell_preserves_pool_side_signs():
    rows = sa.pump_curve_rows(
        _curve_tx(coin_pre=900, coin_post=1000, lamports_pre=10_500, lamports_post=10_000),
        MINT, VAULT, CURVE, slot=11, signature="sell")
    assert rows[1]["amount0"] == "100"
    assert rows[1]["amount1"] == "-500"


def test_unrelated_native_transfer_or_unsupported_program_has_no_authority():
    tx = _curve_tx(program="Other111111111111111111111111111111111111")
    assert sa.resolve_pump_curve_accounts(tx, MINT) is None
    tx = _curve_tx(lamports_post=10_000)
    assert sa.resolve_pump_curve_accounts(tx, MINT) is None


def test_ambiguous_curve_owners_fail_closed():
    assert sa.resolve_pump_curve_accounts(_curve_tx(second=True), MINT) is None


def test_graduated_amm_shape_remains_on_amm_decoder():
    # A normal WSOL-vault pool is authoritative even if the transaction also invokes Pump.
    tx = _curve_tx()
    tx["transaction"]["message"]["accountKeys"].append({"pubkey": "WsolVault111"})
    tx["meta"]["preBalances"].append(2_039_280)
    tx["meta"]["postBalances"].append(2_039_280)
    tx["meta"]["preTokenBalances"].append(
        {"mint": sa.WSOL, "owner": CURVE, "accountIndex": 3,
         "uiTokenAmount": {"amount": "500"}})
    tx["meta"]["postTokenBalances"].append(
        {"mint": sa.WSOL, "owner": CURVE, "accountIndex": 3,
         "uiTokenAmount": {"amount": "550"}})
    assert sa.resolve_pool_accounts(tx, MINT) == (CURVE, VAULT, sa.WSOL, "WsolVault111")


def test_resolved_curve_is_cached_and_decoded_by_tape_pass():
    import sqlite3

    from sniper.analysis.tick_features import ensure_feature_tables
    from sniper.persistence import db_ticks as dbt

    tc = sqlite3.connect(":memory:")
    tc.row_factory = sqlite3.Row
    dbt.ensure_tick_tables(tc)
    dbt.ensure_block_times(tc)
    ensure_feature_tables(tc)
    st.ensure_sol_cursor(tc)
    ledger = sqlite3.connect(":memory:")
    ledger.row_factory = sqlite3.Row
    ledger.execute("CREATE TABLE memecoin_fills (id INTEGER, position_id INTEGER, kind TEXT, "
                   "tx_sig TEXT)")
    ledger.execute("INSERT INTO memecoin_fills VALUES (1, 7, 'entry', 'entry')")
    tx = _curve_tx()
    tx["blockTime"] = 1_700_000_000
    vault = st.resolve_position_pool(
        tc, ledger, {"id": 7, "mint": MINT}, lambda _sig: tx)
    assert vault == VAULT
    cached = tc.execute("SELECT quote_mint FROM sol_pool_vaults WHERE pool=?",
                        (VAULT,)).fetchone()
    assert tuple(cached) == (sa.NATIVE_SOL,)
    assert tc.execute("SELECT pool FROM sol_position_pools WHERE position_id=7").fetchone()[0] == VAULT

    sigs = [{"signature": "trade", "slot": 10, "blockTime": 1_700_000_000}]
    st.tape_pass_for_vault(tc, MINT, VAULT, lambda _a, _p: sigs, lambda _sig: tx,
                           "2026-07-26T00:00:00+00:00", tx_budget=2,
                           floor_ts=1_699_999_000)
    assert {r[0] for r in tc.execute("SELECT kind FROM pool_ticks")} == {
        "sol_curve_swap", "sol_curve_sync"}


def test_curve_lamport_sync_never_claims_trusted_depth():
    from datetime import datetime, timezone

    from sniper.services.memecoin_exit_market_state import compute_state

    rows, blocks = [], {}
    for i in range(6):
        rows.append({"kind": "sol_curve_swap", "block_no": i, "tx_sig": f"s{i}",
                     "log_index": 0, "maker": "m", "amount0": str(-100 + i),
                     "amount1": str(50 + i)})
        rows.append({"kind": "sol_curve_sync", "block_no": i, "tx_sig": f"s{i}",
                     "log_index": 1, "maker": "m", "amount0": str(1000 - i),
                     "amount1": str(10_000 + i)})
        blocks[i] = datetime.fromtimestamp(1_700_000_000 + i, tz=timezone.utc).isoformat()
    state = compute_state(
        rows, blocks,
        {"chain_id": "solana", "mint": MINT, "pool": VAULT, "pool_kind": "sol",
         "position_id": 1, "entry_price": 1.0, "quote_side": 1},
        now_ts=1_700_000_005)
    assert state.provenance["depth"].quality != "trusted"
    assert state.candidate_capable() is False
