"""Wallet graph funded_by / shared_funder (spec section 3; Alpha GO 2026-09-29), Solana only.

The funder of a wallet is the fee payer of the first transaction that takes the wallet from 0 to a
positive SOL balance, read from one Helius getTransactionsForAddress page of the wallet's oldest
transactions. Cached forever per wallet. A failed read is 'unread' (censored), never "self-funded".
shared_funder links two of our wallets with the same funder; a funder of more than HUB_FUNDED of
our wallets is flagged as a hub (likely an exchange) and makes no edges.
"""
from scripts.research import wallet_link_graph as g


def _tx(sig, keys, pre, post, block_time=1000):
    return {"blockTime": block_time, "transaction": {"signatures": [sig], "message": {"accountKeys": keys}},
            "meta": {"err": None, "preBalances": pre, "postBalances": post}}


def test_the_funder_is_the_payer_of_the_first_transaction_that_funds_the_wallet():
    txs = [_tx("s1", ["CEX", "W"], [10**12, 0], [10**12 - 5 * 10**9, 5 * 10**9], block_time=111),
           _tx("s2", ["W", "X"], [5 * 10**9, 0], [4 * 10**9, 10**9])]
    assert g.first_funder(txs, "W") == {"status": "resolved", "funder": "CEX", "payer": "CEX", "payer_differs": False,
                                        "funding_sig": "s1", "funded_at": 111}


def test_a_history_that_starts_funded_is_not_resolved():
    txs = [_tx("s1", ["W", "X"], [5, 0], [4, 1])]                       # W already had SOL
    assert g.first_funder(txs, "W")["status"] == "no_funding_tx"
    assert g.first_funder([], "W")["status"] == "no_history"


def test_shared_funder_pairs_our_wallets_and_skips_hubs():
    funders = {"A": "F1", "B": "F1", "C": "F2", "D": "HUB", "E": "HUB", "G": "HUB", "H": "HUB"}
    edges = g.shared_funder_edges(funders, owners={"A": "uid-a"})
    pairs = {(e["src"], e["dst"]) for e in edges}
    assert pairs == {("B", "uid-a")} or pairs == {("uid-a", "B")}
    (e,) = edges
    assert e["edge_type"] == "shared_funder" and e["evidence"]["funder"] == "F1"
    assert e["evidence"]["funder_degree"] == 2 and e["coverage"]["hub_funders"] == {"HUB": 4}


def test_funder_reads_stop_at_the_budget_and_while_the_book_is_open():
    wallets = ["W%d" % i for i in range(5)]
    reads, calls, why = g.read_funders(wallets, fetch=lambda w: ([], 1), budget=3, flat_fn=lambda: True)
    assert (len(reads), calls, why) == (3, 3, "budget")
    assert all(r["status"] == "no_history" for r in reads)
    flat = iter([True, False])
    reads, _calls, why = g.read_funders(wallets, fetch=lambda w: ([], 1), budget=9, flat_fn=lambda: next(flat))
    assert (len(reads), why) == (1, "solana book open")

    def boom(w):
        raise g.ReadStop("HTTP 429 max usage")
    reads, _calls, why = g.read_funders(wallets, fetch=boom, budget=9, flat_fn=lambda: True)
    assert [r["status"] for r in reads] == ["unread"] and why.startswith("stopped")


def test_funders_persist_and_only_unread_ones_are_retried(tmp_path):
    graph = tmp_path / "graph.db"
    g.save_funders(graph, [{"wallet": "A", "status": "resolved", "funder": "F1", "funding_sig": "s", "funded_at": 1},
                           {"wallet": "B", "status": "unread"}], read_at="t")
    assert g.pending_funders(graph, ["A", "B", "C"]) == ["B", "C"]
    assert g.load_funders(graph) == {"A": "F1"}


def test_the_shared_funder_statement_names_the_funder_and_the_caveat():
    e = g.shared_funder_edges({"A": "F1", "B": "F1", "C": "F9"}, owners={"A": "uid-a"})[0]
    st = g.statement_for(e, {})
    assert "F1" in st and "the same first funder" in st and "may be an exchange" in st
    assert "funds 2 of the 3 wallets we resolved" in st


def test_only_the_first_zero_to_positive_credit_by_another_payer_counts():
    self_paid = [_tx("s1", ["W", "X"], [0, 10], [5, 5])]                  # never "self-funded"
    assert g.first_funder(self_paid, "W")["status"] == "no_funding_tx"
    credited_again = [_tx("s1", ["CEX", "W"], [100, 5], [90, 15])]        # history starts funded
    assert g.first_funder(credited_again, "W")["status"] == "no_funding_tx"
    drained_then_refunded = [_tx("s1", ["W", "X"], [5, 0], [0, 5]), _tx("s2", ["Y", "W"], [10, 0], [5, 5])]
    assert g.first_funder(drained_then_refunded, "W")["status"] == "no_funding_tx"   # Y is not the funder



def test_two_companions_sharing_a_funder_make_no_edge():
    """Gamma MED 2026-09-29: an exchange hot wallet funding two random slot companions stayed under
    the hub cap and became facts about random addresses. An edge needs a roster wallet on one side."""
    funders = {"CompA": "Binance1", "CompB": "Binance1", "Roster": "F2", "CompC": "F2"}
    edges = g.shared_funder_edges(funders, owners={"Roster": "uid-r"})
    assert [(e["src"], e["dst"]) for e in edges] == [("CompC", "uid-r")]


def test_the_funder_is_the_account_that_sent_the_sol_not_the_relayer():
    """Gamma LOW: a relayed transfer's fee payer did not send the SOL. The funder is the account with
    the largest lamport decrease; the payer is kept beside it and a mismatch is flagged."""
    txs = [_tx("s1", ["RELAYER", "SENDER", "W"], [10**9, 10**12, 0], [10**9 - 5000, 10**12 - 3 * 10**9, 3 * 10**9])]
    got = g.first_funder(txs, "W")
    assert (got["funder"], got["payer"], got["payer_differs"]) == ("SENDER", "RELAYER", True)


def test_a_missing_block_time_is_unknown_not_1970():
    e = g.shared_funder_edges({"A": "F1", "B": "F1"}, owners={"A": "uid-a"}, funded_at={})[0]
    assert (e["first_seen"], e["last_seen"]) == ("unknown", "unknown")
