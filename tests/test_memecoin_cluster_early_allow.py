"""Desk early allow (operator 2026-10-22 'fix the cluster budget'): stop tracing once no assignment of
the unresolved supply could reach a limit. Exact integer bound; distinct reasons; desk only."""
from sniper.services import memecoin_cluster_admission as P


def _book(n_tail=16):
    # 4 big distinct-funder owners (15 % each = 60 %) and a long tail of 2.5 % owners (40 %).
    return [("B%d" % i, 150) for i in range(4)] + [("T%d" % i, 25) for i in range(n_tail)]


def _decide(book, *, early_allow=True, thin=False):
    traced = []

    def trace(owner):
        traced.append(owner)
        return "F-" + owner, "resolved"
    d = P.decide_cluster(book, {}, trace_fn=trace, workers=1, early_allow=early_allow, thin_owner_pass=thin)
    return d, traced


def test_a_long_tail_is_allowed_on_the_bound_after_tracing_only_the_big_owners():
    d, traced = _decide(_book())
    # 4 big singletons: each open owner may join a different one, so the tail must shrink a lot first
    assert d.allowed and d.reason == "allowed_bound"
    assert len(traced) < len(_book())          # the tail owners that cannot change the verdict are skipped


def test_without_early_allow_every_owner_is_traced():
    d, traced = _decide(_book(), early_allow=False)
    assert d.allowed and d.reason == "allowed" and len(traced) == 20


def test_the_bound_is_exact_at_the_limit_never_early_at_equality():
    # attempted 1000; resolved 4 x 150 distinct; unresolved exactly 100 -> clustered_worst 250 = 25 %: no
    book = [("B%d" % i, 150) for i in range(4)] + [("T%d" % i, 25) for i in range(16)]
    d, traced = _decide(book)
    assert traced[:4] == ["B0", "B1", "B2", "B3"]
    # with 4 tail owners left (R = 100) the bound must not have fired
    assert len(traced) >= 4 + 12


def test_a_book_the_bound_cannot_clear_is_resolved_fully_or_blocked():
    # one owner with 45 %: ANY unresolved owner sharing its funder makes a >= 25 % cluster, so the bound
    # can never clear early; the full trace decides (a real limit of the gate, not of the bound)
    book = [("A", 450)] + [("T%d" % i, 25) for i in range(22)]
    d, traced = _decide(book)
    assert d.allowed and d.reason == "allowed" and len(traced) == 23


def test_a_blocking_book_still_blocks_first():
    book = [("A", 300), ("B", 300)] + [("T%d" % i, 25) for i in range(16)]
    def shared(owner):
        return ("WEB" if owner in ("A", "B") else "F-" + owner), "resolved"
    d = P.decide_cluster(book, {}, trace_fn=shared, workers=1, early_allow=True)
    assert not d.allowed and d.reason in ("cluster_share_high", "cluster_top_funder_high")


def test_thin_books_get_their_own_bound_reason():
    d, _ = _decide([("A", 10), ("B", 10), ("C", 1)], thin=True)
    assert d.allowed and d.reason in ("pass_thin_bound", "pass_thin_owners")



def test_a_lone_thin_owner_is_allowed_with_no_trace_at_all():
    d, traced = _decide([("A", 100)], thin=True)
    assert d.allowed and d.reason == "pass_thin_bound" and traced == []


def test_two_thin_owners_must_be_traced_and_a_shared_funder_blocks():
    d = P.decide_cluster([("A", 50), ("B", 50)], {}, trace_fn=lambda o: ("WEB", "resolved"), workers=1,
                         early_allow=True, thin_owner_pass=True)
    assert not d.allowed



def test_gamma_counterexample_open_owners_joining_different_singletons_never_early_allows():
    book = [("S%d" % i, 14) for i in range(7)] + [("U0", 1), ("U1", 1)]
    cached = {"S%d" % i: "F%d" % i for i in range(7)}
    d = P.decide_cluster(book, cached, trace_fn=lambda o: ("F0" if o == "U0" else "F1", "resolved"),
                         workers=1, early_allow=True)
    assert d.reason == "cluster_share_high" and not d.allowed


def test_property_early_allow_implies_every_assignment_passes_full_resolution():
    """Brute force (Gamma): small books, every funder assignment of the open owners. If early allow
    fires on the resolved part, NO assignment may block under full resolution."""
    import itertools
    values = (1, 3, 10, 25)
    for n in range(1, 7):
        for balances in itertools.product(values, repeat=n):
            for n_open in range(0, n + 1):
                for thin in (False, True):
                    book = [("O%d" % i, b) for i, b in enumerate(balances)]
                    resolved = {"O%d" % i: "F%d" % (i % 3) for i in range(n_open, n)}
                    open_owners = ["O%d" % i for i in range(n_open)]
                    funders = sorted(set(resolved.values())) + ["N%d" % i for i in range(n_open)]
                    early = P.decide_cluster(book, dict(resolved), trace_fn=lambda o: (_ for _ in ()).throw(
                        AssertionError("early decided before tracing")) if False else ("X", "unresolved"),
                        workers=1, early_allow=True, thin_owner_pass=thin, deadline_s=5)
                    if early.reason not in ("allowed_bound", "pass_thin_bound") or not open_owners:
                        continue
                    for assign in itertools.product(funders, repeat=len(open_owners)):
                        mapping = dict(zip(open_owners, assign))
                        full = P.decide_cluster(book, {**resolved, **mapping}, trace_fn=lambda o: ("X", "resolved"),
                                                workers=1, early_allow=False, thin_owner_pass=thin)
                        assert full.allowed, (balances, n_open, thin, mapping, full.reason)
