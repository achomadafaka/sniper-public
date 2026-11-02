"""The entry chain must be RESOLVED from the raw feed tag before routing.

THE BUG (2026-07-14, measured): `make_real_hook` gated on `ev.network_id` straight from the feed.
The fomo feed omits the chain on ~27% of curated EVM buys, tagging them `evm-unknown`, which the
gate refused as `not_approved` — including buys from wallets on the coin's real chain. (Arming was
per-chain then; it is GLOBAL as of 2026-07-15, and the unroutable-chain refusal now lives in the
venue-context guard — but resolution is still required so a routable buy reaches the right venue.)

`route_real_entry` already carries a `resolve_fn` for exactly this case, and its docstring says so:
"When the feed omits the chain entirely ('' / legacy 'evm-unknown' — ~27% of curated EVM buys),
resolve_fn(mint) recovers it from DexScreener so allowlisted-chain tokens still trade."
It never ran — the allowlist gate returned first, so the recovery path was dead code.

Cost, measured on the paper tape (real_shadow arm = the live real rails, 7d, mint-grain):
12 PAPER WINNERS refused this way, $640.67 of paper PnL (~$192 at the $30 real clip) — the SECOND
biggest blocker in the funnel, behind only the allowlist itself. Binkieee +$293, zinceth +$128,
bruhcolli +$92, Magneto +$36.

Invariant preserved: a chain the feed NAMED is authoritative and is never second-guessed — a
resolver mistake must not smuggle a held-out-chain token past the allowlist.
"""
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_real_runner import resolve_entry_chain


def _ev(network_id, mint="0xabc"):
    return SimpleNamespace(side="buy", source_user_id="u1", source_handle="alice",
                           mint=mint, ticker="T", network_id=network_id,
                           their_market_cap=1_000_000, their_size_usd=5_000)


def test_unknown_chain_is_resolved_before_the_allowlist_is_checked():
    """The whole bug: an armed wallet's buy tagged `evm-unknown` must resolve to its real chain."""
    assert resolve_entry_chain(_ev("evm-unknown"), resolve_fn=lambda mint: "4663") == "4663"
    assert resolve_entry_chain(_ev(""), resolve_fn=lambda mint: "4663") == "4663"
    assert resolve_entry_chain(_ev(None), resolve_fn=lambda mint: "8453") == "8453"


def test_a_named_chain_is_authoritative_and_never_second_guessed():
    """A resolver mistake must NOT be able to smuggle a held-out-chain token past the allowlist."""
    def resolver(mint):
        raise AssertionError("must not resolve a chain the feed already named")
    assert resolve_entry_chain(_ev("4663"), resolve_fn=resolver) == "4663"
    assert resolve_entry_chain(_ev("solana", mint="SoLmint"), resolve_fn=resolver) == "solana"


def test_solana_is_never_sent_to_an_evm_resolver():
    """A base58 mint is Solana by construction — the chain tag is irrelevant and costs a lookup."""
    def resolver(mint):
        raise AssertionError("must not resolve an EVM chain for a Solana mint")
    assert resolve_entry_chain(_ev("", mint="CizcDa22HJjXybKLriibqP167GcJjqiDHDPtuiMmpump"),
                               resolve_fn=resolver) == "solana"


def test_unresolvable_chain_stays_unknown_and_is_still_refused():
    """Fail-closed: if we cannot learn the chain we must NOT guess one. The buy is still refused —
    but by a decision we can see, not by a tag the feed happened to omit."""
    assert resolve_entry_chain(_ev("evm-unknown"), resolve_fn=lambda mint: None) == "evm-unknown"
    assert resolve_entry_chain(_ev("evm-unknown"), resolve_fn=lambda mint: "") == "evm-unknown"


def test_resolver_failure_does_not_break_the_entry_path():
    def boom(mint):
        raise RuntimeError("dexscreener down")
    assert resolve_entry_chain(_ev("evm-unknown"), resolve_fn=boom) == "evm-unknown"


def test_arming_is_global_so_the_chain_tag_never_gates_an_armed_wallet(tmp_path):
    """Arming is GLOBAL (2026-07-15): an armed wallet is on the allowlist regardless of the feed's
    chain tag (evm-unknown included) — `network_id` no longer filters. The unroutable-chain refusal
    moved to make_real_hook's venue-context guard, not per-wallet arming. Resolution still runs so
    the buy routes to the right venue."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.execute("INSERT INTO memecoin_wallets(source_user_id, handle, real_approved, paused, excluded)"
                 " VALUES('u1','bruhcolli',1,0,0)")
    conn.commit()

    m.set_wallet_real_chains(conn, "u1", ["4663"])
    assert m.real_approved_user_ids(conn, network_id="4663") == {"u1"}
    # an unresolved chain is not an authority: it must not inherit 4663's arming
    assert m.real_approved_user_ids(conn, network_id="evm-unknown") == set()
    assert m.real_approved_user_ids(conn, network_id="solana") == set()
    assert m.real_approved_user_ids(conn, network_id=None) == set()
    # routing still recovers the real chain so the buy reaches the right venue
    assert resolve_entry_chain(_ev("evm-unknown"), resolve_fn=lambda mint: "4663") == "4663"
