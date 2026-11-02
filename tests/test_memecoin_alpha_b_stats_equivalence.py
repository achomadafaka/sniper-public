"""`alpha_b_copy_stats` and `alpha_b_copy_stats_all` must agree BY CONSTRUCTION.

947bfa55 made the singular and the batched form share one predicate and one chain-scope builder so
the per-chain Armed board could stop calling the singular 527 times. The evidence for that change
was empirical — zero row differences on the live ledger — which is the right check and not a proof.

It needs to be a proof, because this number is not a display. `alpha_b_copy_stats` feeds
`real_approval_preview` (db_memecoin.py:3282), which `set_wallet_chain_armed` (:2998) reads to grant
or refuse FUNDED AUTHORITY ON A CHAIN. Too low and a qualified wallet cannot be armed; too high and
one is armed on evidence it does not have. A silent divergence is also least visible exactly here: a
wallet that quietly stops qualifying looks identical to a wallet that has not earned it yet.

So these tests fuzz positions and fills into a temp DB and assert both forms against an INDEPENDENT
PYTHON ORACLE built from the fixture rows — not against each other.

That distinction is the whole test. 947bfa55 made the two forms share one predicate builder and one
chain-scope builder, so "singular == batch" is now nearly a tautology: a bug in the shared code
makes both wrong in the same direction and the equality still holds. Mutation-checked on
2026-09-08 — narrowing the chain scope from every network id to one spelling left a
singular-vs-batch suite fully green. The oracle is what catches that; the sibling comparison is
kept only to catch a future change that un-shares them.
"""
import random
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m

CHAINS = ("solana", "robin", "base", "bsc")
NETWORKS = ("solana", "4663", "robin", "8453", "56")
ZERO = {"n_closes": 0, "pnl": 0.0}


def _canonical_network(network_id, mint):
    """The network-id canonicalisation, RESTATED — deliberately not `normalized_coin_network`.

    `COIN_NETWORK_SQL` normalises the stored column and the `network_id=` branch normalises the
    parameter, so an oracle that compares raw strings is right only for the spellings that happen
    to be in the table today. Every stored value is canonical right now ('solana' 17,016, '4663'
    12,858, '8453' 2), so this is latent — but `1399811149` is a real Solana chain-id spelling that
    `normalized_coin_network` exists to absorb, and a NULL network_id falls back on the mint. If a
    feed adapter ever writes either, the SQL would be RIGHT and a raw-comparison oracle would go
    red against it. Someone would then "fix" the SQL to match the test and break the arming gate.

    Restated rather than imported on purpose: calling the production helper would rebuild the
    tautology this suite was rewritten to escape (2026-09-08). Adding a fifth branch to the real
    normaliser must fail here, loudly, so a human confirms the arming gate should count it.
    """
    network = str(network_id or "").strip().lower()
    if network == "1399811149":
        return "solana"
    if network:
        return network
    return "evm-unknown" if str(mint or "").strip().lower().startswith("0x") else "solana"


def _oracle(rows, uid, *, chain=None, network_id=None):
    """What the alpha_b copy metric MUST say, computed in Python from the fixture rows.

    Independent of the SQL under test on purpose. It restates the rule in prose-to-code: a closed
    non-real alpha_b position, measurable, not fomo-priced, on the requested chain.
    """
    from sniper.services.memecoin_chains import network_ids_for, resolve_chain
    if chain is not None:
        key = resolve_chain(chain)
        if key is None:
            raise ValueError("unknown chain")
        wanted = set(network_ids_for(key))
    elif network_id is not None:
        # the real query normalises the PARAMETER too, not only the column
        wanted = {_canonical_network(network_id, "")}
    else:
        wanted = None
    total, n = 0.0, 0
    for r in rows:
        if r["uid"] != uid or r["strategy"] != "alpha_b" or r["mode"] == "real":
            continue
        if not r["measurable"] or r["fomo"]:
            continue
        if wanted is not None and _canonical_network(r["network"], r["mint"]) not in wanted:
            continue
        n += 1
        total += r["pnl"]
    return {"n_closes": n, "pnl": round(total, 2)}


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _position(c, pid, uid, network, *, strategy, mode, pnl, measurable, fomo=False, mint=None):
    """One closed position, with the fills that decide whether it is MEASURABLE.

    Measurable needs a positive entry-fill quantity and at least one executable exit leg with no
    non-executable leg. The unmeasurable variant here omits the exit leg, which is the orphan-close
    malformation the predicate fails closed on.
    """
    c.execute(
        "INSERT INTO memecoin_positions(id,source_user_id,network_id,mint,strategy,execution_mode,"
        "is_open,realized_pnl,entry_notional,entry_fill_source,exit_reason) "
        "VALUES(?,?,?,?,?,?,0,?,10.0,?,'real_tp')",
        (pid, uid, network, mint or f"mint{pid}", strategy, mode, pnl,
         "fomo" if fomo else "dexscreener"))
    c.execute("INSERT INTO memecoin_fills(position_id,kind,qty,notional,fill_source) "
              "VALUES(?,'entry',100.0,10.0,'dexscreener')", (pid,))
    if measurable:
        c.execute("INSERT INTO memecoin_fills(position_id,kind,qty,notional,fill_source) "
                  "VALUES(?,'bracket_exit',100.0,12.0,'dexscreener')", (pid,))


def _fuzz(c, seed, n_wallets=12, n_positions=140):
    """Returns (uids, rows) — `rows` is what the oracle reads, so it never consults the DB."""
    rnd = random.Random(seed)
    uids = [f"w{i}" for i in range(n_wallets)]
    for uid in uids:
        c.execute("INSERT INTO memecoin_wallets(source_user_id,handle) VALUES(?,?)", (uid, uid))
    rows = []
    for pid in range(1, n_positions + 1):
        r = {
            "uid": rnd.choice(uids), "network": rnd.choice(NETWORKS), "mint": f"mint{pid}",
            # every axis the shared predicate filters on gets exercised, both ways
            "strategy": rnd.choice(["alpha_b", "alpha_b", "alpha_b", "scalp_c2", "real_shadow"]),
            "mode": rnd.choice(["paper", "paper", "paper", "real"]),
            "pnl": round(rnd.uniform(-40, 60), 2),
            "measurable": rnd.random() > 0.25,
            "fomo": rnd.random() < 0.15,
        }
        _position(c, pid, r["uid"], r["network"], strategy=r["strategy"], mode=r["mode"],
                  pnl=r["pnl"], measurable=r["measurable"], fomo=r["fomo"], mint=r["mint"])
        rows.append(r)
    c.commit()
    return uids, rows


@pytest.mark.parametrize("seed", range(12))
def test_both_forms_match_an_independent_oracle_for_every_wallet_and_chain(conn, seed):
    """THE load-bearing test. Both forms are checked against Python, not against each other."""
    uids, rows = _fuzz(conn, seed)
    for chain in CHAINS:
        batch = m.alpha_b_copy_stats_all(conn, chain=chain)
        for uid in uids:
            want = _oracle(rows, uid, chain=chain)
            assert m.alpha_b_copy_stats(conn, uid, chain=chain) == want, (
                f"singular seed={seed} chain={chain} uid={uid}")
            assert (batch.get(uid) or ZERO) == want, f"batch seed={seed} chain={chain} uid={uid}"
    batch = m.alpha_b_copy_stats_all(conn)
    for uid in uids:
        want = _oracle(rows, uid)
        assert m.alpha_b_copy_stats(conn, uid) == want
        assert (batch.get(uid) or ZERO) == want


@pytest.mark.parametrize("seed", range(6))
def test_an_exact_network_id_matches_the_oracle_and_does_not_expand(conn, seed):
    """`network_id=` matches ONE spelling; `chain=` expands to every spelling. Pin the difference.

    Without this, narrowing the chain scope to a single network id passes unnoticed: both forms
    share the builder, so they narrow together and stay equal to each other.
    """
    uids, rows = _fuzz(conn, seed)
    for network in NETWORKS:
        batch = m.alpha_b_copy_stats_all(conn, network)
        for uid in uids:
            want = _oracle(rows, uid, network_id=network)
            assert m.alpha_b_copy_stats(conn, uid, network) == want
            assert (batch.get(uid) or ZERO) == want


def test_a_chain_scope_covers_every_network_id_that_chain_is_stored_under(conn):
    """robin is stored as both '4663' and 'robin'. A scope that matches one is a silent undercount,
    and it undercounts the number that grants funded authority."""
    from sniper.services.memecoin_chains import network_ids_for
    nets = set(network_ids_for("robin"))
    assert len(nets) > 1, "this test needs a chain with more than one stored spelling"
    conn.execute("INSERT INTO memecoin_wallets(source_user_id,handle) VALUES('w','w')")
    for i, net in enumerate(sorted(nets), start=1):
        _position(conn, i, "w", net, strategy="alpha_b", mode="paper", pnl=5.0, measurable=True,
                  mint=f"m{i}")
    conn.commit()
    assert m.alpha_b_copy_stats(conn, "w", chain="robin")["n_closes"] == len(nets)
    assert m.alpha_b_copy_stats_all(conn, chain="robin")["w"]["n_closes"] == len(nets)


def test_a_chain_with_no_rows_agrees_too(conn):
    """The empty-dict case. base and bsc were EMPTY on the live ledger on 2026-09-08."""
    uids, _rows = _fuzz(conn, 99)
    conn.execute("DELETE FROM memecoin_positions WHERE %s IN ('8453','56','base','bsc')"
                 % m.COIN_NETWORK_SQL)
    conn.commit()
    for chain in ("base", "bsc"):
        assert m.alpha_b_copy_stats_all(conn, chain=chain) == {}
        for uid in uids:
            assert m.alpha_b_copy_stats(conn, uid, chain=chain) == ZERO


def test_an_empty_database_agrees(conn):
    assert m.alpha_b_copy_stats_all(conn) == {}
    assert m.alpha_b_copy_stats(conn, "nobody") == ZERO


def test_an_unknown_chain_raises_the_same_way_in_both_forms(conn):
    _fuzz(conn, 1)[0]
    with pytest.raises(ValueError, match="never pooled"):
        m.alpha_b_copy_stats(conn, "w0", chain="nope")
    with pytest.raises(ValueError, match="never pooled"):
        m.alpha_b_copy_stats_all(conn, chain="nope")


# --- dense ------------------------------------------------------------------------------------

@pytest.mark.parametrize("chain", CHAINS)
def test_dense_fills_every_wallet_with_the_singular_answer(conn, chain):
    """`dense=True` must add the explicit zero the singular gives, never a different number."""
    uids, rows = _fuzz(conn, 7)
    dense = m.alpha_b_copy_stats_all(conn, chain=chain, dense=True)
    assert set(uids) <= set(dense), "every curated wallet must be present"
    for uid in uids:
        assert dense[uid] == _oracle(rows, uid, chain=chain)


def test_dense_is_off_by_default_so_existing_callers_are_untouched(conn):
    uids, _rows = _fuzz(conn, 3)
    sparse = m.alpha_b_copy_stats_all(conn, chain="base")
    assert set(sparse) < set(uids) or sparse == {}, "the default shape must stay sparse"


def test_dense_does_not_invent_wallets_that_are_not_curated(conn):
    _fuzz(conn, 5)
    conn.execute("INSERT INTO memecoin_positions(id,source_user_id,network_id,mint,strategy,"
                 "execution_mode,is_open,realized_pnl,entry_notional) "
                 "VALUES(9999,'ghost','solana','m','alpha_b','paper',0,1.0,10.0)")
    conn.commit()
    dense = m.alpha_b_copy_stats_all(conn, chain="solana", dense=True)
    # a wallet with rows but no roster entry still appears from the GROUP BY; dense adds roster
    # wallets only, so it cannot manufacture a wallet that exists in neither.
    assert "not_a_wallet_anywhere" not in dense


def test_the_armed_board_asks_for_the_dense_shape():
    """The board gates funded authority; it must not be able to read absent as anything else."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "sniper" / "routes" / "api_memecoin.py").read_text()
    assert "alpha_b_copy_stats_all(conn, chain=chain, dense=True)" in src


# --- network-id canonicalisation ---------------------------------------------------------------
#
# `COIN_NETWORK_SQL` has FOUR branches, and every stored value today exercises only one of them.
# These pin the other three against real fixture rows so the suite certifies the rule rather than
# the current contents of the table. Raised by Alpha for `1399811149`; the NULL-network branches
# are the same defect and are more reachable, because a NULL network_id is a plausible write and
# the fallback then decides the chain from the MINT.

@pytest.mark.parametrize("stored", ["1399811149", "SOLANA", "  solana  "])
def test_a_noncanonical_solana_spelling_still_counts_as_solana(conn, stored):
    """The SQL maps these to solana. An oracle comparing raw strings would call the SQL wrong."""
    conn.execute("INSERT INTO memecoin_wallets(source_user_id,handle) VALUES('w','w')")
    _position(conn, 1, "w", stored, strategy="alpha_b", mode="paper", pnl=9.0, measurable=True,
              mint="SoLmint")
    conn.commit()
    want = {"n_closes": 1, "pnl": 9.0}
    assert m.alpha_b_copy_stats(conn, "w", chain="solana") == want
    assert m.alpha_b_copy_stats_all(conn, chain="solana")["w"] == want
    assert m.alpha_b_copy_stats(conn, "w", chain="robin") == ZERO


def test_a_null_network_with_an_evm_mint_belongs_to_no_funded_chain(conn):
    """Empty network + `0x` mint canonicalises to `evm-unknown`, which is not a funded chain.

    It must therefore count on NONE of them — not silently land on solana, which is what the
    else-branch would do if the mint were ignored.
    """
    conn.execute("INSERT INTO memecoin_wallets(source_user_id,handle) VALUES('w','w')")
    _position(conn, 1, "w", None, strategy="alpha_b", mode="paper", pnl=9.0, measurable=True,
              mint="0xdeadbeef")
    conn.commit()
    for chain in CHAINS:
        assert m.alpha_b_copy_stats(conn, "w", chain=chain) == ZERO, chain
    assert m.alpha_b_copy_stats(conn, "w") == {"n_closes": 1, "pnl": 9.0}, "unscoped still counts it"


def test_a_null_network_with_a_non_evm_mint_falls_back_to_solana(conn):
    conn.execute("INSERT INTO memecoin_wallets(source_user_id,handle) VALUES('w','w')")
    _position(conn, 1, "w", None, strategy="alpha_b", mode="paper", pnl=4.0, measurable=True,
              mint="SoLmint")
    conn.commit()
    assert m.alpha_b_copy_stats(conn, "w", chain="solana") == {"n_closes": 1, "pnl": 4.0}
    assert m.alpha_b_copy_stats(conn, "w", chain="robin") == ZERO


def test_the_oracle_agrees_on_every_canonicalisation_branch(conn):
    """The oracle must reach the same answer as the SQL on all four branches, not just today's."""
    conn.execute("INSERT INTO memecoin_wallets(source_user_id,handle) VALUES('w','w')")
    rows = []
    for i, (net, mint) in enumerate([("1399811149", "SoL"), ("SOLANA", "SoL"), ("4663", "0xab"),
                                     (None, "0xab"), (None, "SoL"), ("  ROBIN ", "0xcd")], start=1):
        _position(conn, i, "w", net, strategy="alpha_b", mode="paper", pnl=1.0, measurable=True,
                  mint=mint)
        rows.append({"uid": "w", "network": net, "mint": mint, "strategy": "alpha_b",
                     "mode": "paper", "pnl": 1.0, "measurable": True, "fomo": False})
    conn.commit()
    for chain in CHAINS:
        assert m.alpha_b_copy_stats(conn, "w", chain=chain) == _oracle(rows, "w", chain=chain), chain


@pytest.mark.parametrize("param", ["1399811149", "SOLANA", "  solana  "])
def test_a_noncanonical_network_id_PARAMETER_is_normalised_too(conn, param):
    """Both sides normalise, and the parameter side needs its own row to be reachable.

    Caught by mutation-testing on 2026-09-08: with only canonical parameters in the fixtures,
    deleting `normalized_coin_network(network_id, "")` from the scope builder left the suite
    fully green — the very branch this addition exists to protect.
    """
    conn.execute("INSERT INTO memecoin_wallets(source_user_id,handle) VALUES('w','w')")
    _position(conn, 1, "w", "solana", strategy="alpha_b", mode="paper", pnl=3.0, measurable=True,
              mint="SoLmint")
    conn.commit()
    want = {"n_closes": 1, "pnl": 3.0}
    rows = [{"uid": "w", "network": "solana", "mint": "SoLmint", "strategy": "alpha_b",
             "mode": "paper", "pnl": 3.0, "measurable": True, "fomo": False}]
    assert _oracle(rows, "w", network_id=param) == want, "the oracle must normalise the parameter"
    assert m.alpha_b_copy_stats(conn, "w", param) == want
    assert m.alpha_b_copy_stats_all(conn, param)["w"] == want
