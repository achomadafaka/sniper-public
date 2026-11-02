"""Solana WS hot tape — vault-delta rows via the live-verified sign rule. Pure, offline."""
import json

from sniper.services.memecoin_solana_ws import VaultTape, parse_frame, subscribe_frame

COIN_V, QUOTE_V, MINT = "CoinVault", "QuoteVault", "Mint111"


def _notif(sub, slot, amount, decimals=6):
    return {"params": {"subscription": sub, "result": {
        "context": {"slot": slot},
        "value": {"data": {"parsed": {"info": {"tokenAmount": {
            "amount": str(amount), "decimals": decimals,
        }}}}}}}}


def test_subscribe_frame_is_accountsubscribe_jsonparsed():
    f = json.loads(subscribe_frame("V1", 7))
    assert f["method"] == "accountSubscribe" and f["params"][0] == "V1"
    assert f["params"][1]["encoding"] == "jsonParsed"


def test_parse_balance_and_ack():
    assert parse_frame({"id": 1, "result": 99}, {}) == ("ack", 1, 99)
    assert parse_frame(_notif(99, 500, 1234), {99: COIN_V}) == (
        "balance", COIN_V, 500, 1234, 6)


def test_parse_balance_retains_token_decimals():
    assert parse_frame(_notif(99, 500, 1234, decimals=9), {99: COIN_V}) == (
        "balance", COIN_V, 500, 1234, 9)


def test_parse_fails_closed_on_unreadable_frames():
    assert parse_frame({"params": {"subscription": 99, "result": {}}}, {99: COIN_V}) is None
    assert parse_frame(_notif(99, 500, "abc"), {99: COIN_V}) is None
    assert parse_frame(_notif(77, 500, 1), {99: COIN_V}) is None      # unknown subscription
    assert parse_frame("nonsense", {}) is None


def _tape():
    return VaultTape(COIN_V, QUOTE_V, mint=MINT)


def test_first_sighting_sets_a_baseline_and_emits_no_delta():
    t = _tape()
    assert t.on_balance(COIN_V, 1, 1000) is None       # nothing to diff against yet


def test_opposite_sign_deltas_are_a_swap():
    t = _tape()
    t.on_balance(COIN_V, 1, 1000); t.on_balance(QUOTE_V, 1, 500)
    t.on_balance(COIN_V, 2, 1200)                       # coin +200 (into pool)
    row = t.on_balance(QUOTE_V, 2, 400)                 # quote -100 (out)
    assert row["kind"] == "sol_swap" and row["amount0"] == "200" and row["amount1"] == "-100"
    assert row["block_no"] == 2 and row["pool"] == MINT


def test_swap_row_retains_both_decimal_values():
    tape = _tape()
    tape.on_balance(COIN_V, 1, 1000, decimals=6)
    tape.on_balance(QUOTE_V, 1, 500, decimals=9)
    tape.on_balance(COIN_V, 2, 1200, decimals=6)
    row = tape.on_balance(QUOTE_V, 2, 400, decimals=9)
    assert row["decimals0"] == 6
    assert row["decimals1"] == 9


def test_missing_decimals_preserve_the_existing_hot_tape_row():
    tape = _tape()
    tape.on_balance(COIN_V, 1, 1000)
    tape.on_balance(QUOTE_V, 1, 500)
    tape.on_balance(COIN_V, 2, 1200)
    row = tape.on_balance(QUOTE_V, 2, 400)
    assert row["kind"] == "sol_swap"
    assert row["decimals0"] is None
    assert row["decimals1"] is None


def test_same_sign_deltas_are_an_LP_event_not_flow():
    """LIVE-VERIFIED rule: same-sign = liquidity add/remove. Counting it as flow would fabricate
    directional pressure that never happened."""
    t = _tape()
    t.on_balance(COIN_V, 1, 1000); t.on_balance(QUOTE_V, 1, 500)
    t.on_balance(COIN_V, 2, 800)                        # both DOWN -> LP removed
    row = t.on_balance(QUOTE_V, 2, 400)
    assert row["kind"] == "sol_burn"
    t2 = _tape()
    t2.on_balance(COIN_V, 1, 1000); t2.on_balance(QUOTE_V, 1, 500)
    t2.on_balance(COIN_V, 2, 1200)                      # both UP -> LP added
    assert t2.on_balance(QUOTE_V, 2, 600)["kind"] == "sol_mint"


def test_one_sided_slot_emits_reserves_only_never_flow():
    t = _tape()
    t.on_balance(COIN_V, 1, 1000); t.on_balance(QUOTE_V, 1, 500)
    row = t.on_balance(COIN_V, 2, 1200)                 # only the coin side moved this slot
    assert row["kind"] == "sol_sync"                    # reserves captured, no direction invented


def test_foreign_vault_is_ignored():
    t = _tape()
    assert t.on_balance("SomeOtherVault", 1, 5) is None


def test_module_is_read_only_and_never_touches_custody():
    import inspect
    from sniper.services import memecoin_solana_ws as m
    body = inspect.getsource(m).split('"""', 2)[-1]
    assert "MEMECOIN_SOLANA_RPC_URL" not in body
    for forbidden in ("sendTransaction", "signTransaction", "sign(", "keypair"):
        assert forbidden not in body
