"""The residual solver must never report a quantity it did not quote."""
from sniper.services import memecoin_residual_tail as R

TOTAL = 1_000_000_000


def convex(total, *, unit=1.0, impact=0.5):
    """A bag whose proceeds grow sublinearly with size — real price impact, not a discount.

    out(f) = unit * total * f * (1 - impact * f). At impact=0.5 selling everything yields half the
    naive linear figure, which is the regime that makes a linear f estimate wrong.
    """
    calls = []

    def quote(atomic):
        calls.append(atomic)
        f = atomic / total
        return unit * total * f * (1.0 - impact * f)
    return quote, calls


def test_it_returns_a_fraction_that_was_actually_quoted():
    quote, calls = convex(TOTAL, unit=1e-7)
    r = R.resolve_residual(open_qty_atomic=TOTAL, target_usdc=40.0, quote_out_fn=quote)
    assert r["status"] == R.RESOLVED
    assert r["sold_qty_atomic"] in calls, "the reported quantity must be one we priced"
    assert r["sold_usdc"] >= 40.0, "the reported sale must actually cover principal"
    assert r["residual_qty_atomic"] == TOTAL - r["sold_qty_atomic"]


def test_pricing_off_a_unit_price_would_undersell_and_this_does_not():
    """The exact hazard, and it is not hypothetical: the post-TP collector already stores a
    `unit_price` (quote_usdc / qty), so the natural shortcut is f = target / (unit * total). That
    unit price is the MARGINAL rate at the size quoted; impact means a fraction priced at it does
    NOT cover the target, and reporting that fraction would invent a residual that never existed.
    """
    quote, _ = convex(TOTAL, unit=1e-7, impact=0.5)
    target = 40.0
    # the marginal rate a small quote reports, which is what a unit_price row would carry
    small = int(TOTAL * 0.001)
    unit_price = quote(small) / small
    naive_f = target / (unit_price * TOTAL)               # 0.4 — priced as if impact were free
    assert quote(int(TOTAL * naive_f)) < target, "fixture must exhibit the convexity being tested"

    r = R.resolve_residual(open_qty_atomic=TOTAL, target_usdc=target, quote_out_fn=quote)
    assert r["status"] == R.RESOLVED
    assert r["fraction"] > naive_f, "the exact answer must sell MORE than the unit price implies"
    assert r["sold_usdc"] >= target, "and it must actually cover principal"


def test_a_bag_that_cannot_cover_principal_is_a_finding_not_a_failure():
    quote, _ = convex(TOTAL, unit=1e-9)
    r = R.resolve_residual(open_qty_atomic=TOTAL, target_usdc=1_000_000.0, quote_out_fn=quote)
    assert r["status"] == R.UNREACHABLE
    assert r["fraction"] is None and r["residual_qty_atomic"] is None


def test_no_route_is_typed_and_never_a_zero():
    r = R.resolve_residual(open_qty_atomic=TOTAL, target_usdc=10.0, quote_out_fn=lambda a: None)
    assert r["status"] == R.NO_ROUTE
    assert r["sold_usdc"] is None and r["residual_qty_atomic"] is None


def test_the_probe_budget_is_respected():
    quote, calls = convex(TOTAL, unit=1e-7)
    r = R.resolve_residual(open_qty_atomic=TOTAL, target_usdc=40.0, quote_out_fn=quote,
                           max_probes=3)
    assert len(calls) <= 3 and r["probes"] <= 3


def test_a_raising_provider_does_not_raise_out():
    def boom(_atomic):
        raise RuntimeError("provider down")
    r = R.resolve_residual(open_qty_atomic=TOTAL, target_usdc=10.0, quote_out_fn=boom)
    assert r["status"] == R.NO_ROUTE, "an observer must never raise into the runner"


def test_unusable_inputs_are_refused_rather_than_guessed():
    for qty, target in ((0, 10.0), (TOTAL, 0.0), (None, 10.0), (TOTAL, None)):
        r = R.resolve_residual(open_qty_atomic=qty, target_usdc=target,
                               quote_out_fn=lambda a: 1.0)
        assert r["status"] == R.UNDETERMINED and r["fraction"] is None


def test_it_never_reports_a_residual_on_a_non_resolved_status():
    """Every failure path must null the quantities — a partial answer reads as a measurement."""
    for r in (R.resolve_residual(open_qty_atomic=TOTAL, target_usdc=1e9,
                                 quote_out_fn=convex(TOTAL, unit=1e-9)[0]),
              R.resolve_residual(open_qty_atomic=TOTAL, target_usdc=10.0,
                                 quote_out_fn=lambda a: None)):
        assert r["status"] != R.RESOLVED
        assert r["fraction"] is None
        assert r["residual_qty_atomic"] is None
        assert r["sold_qty_atomic"] is None
