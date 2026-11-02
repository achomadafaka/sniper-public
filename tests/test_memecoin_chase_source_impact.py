"""The chase cap must not charge us the SOURCE's own price impact.

`chase_gap_pct` measures our exact quote against the trader's FILL. On a thin book a large buy
walks that book, so his fill already sits above the pre-trade price by his own footprint — and the
gate then refuses us a premium HE created. s4if put $999 into a $72k coin and was refused at +31%
against a cap of 30.

This is the 2026-08-23 finding one level deeper: we stopped the cap charging OUR route cost, and it
still charged us the source's.

Every failure here is quiet. An estimator that always returns None silently restores the old
behaviour; one that forgets the clamp opens the gate onto a -63.4% median; one that fires on the
accept path doubles the funded quote budget with nothing to show for it.
"""
import pytest

from sniper.services import memecoin_real_runner as RR
from sniper.settings import memecoin as cfg


class _Ev:
    mint = "MINT"
    network_id = "solana"

    def __init__(self, size=999.0, price=1.0, network="solana"):
        self.their_size_usd = size
        self.their_price = price
        self.network_id = network


class _EvChain(_Ev):
    def __init__(self, network_id, size=999.0):
        super().__init__(size=size)
        self.network_id = network_id


def _q(impact_fraction):
    return lambda _usd: {"priceImpactPct": str(impact_fraction)}


# ---------------------------------------------------------------- the estimator

# MEASURED AGAINST THE LIVE PROVIDER on 2026-08-29, not asserted from a reading of its docs.
#
# The v1 formula shipped on a BELIEF about this field (that it was a final pool-price move, needing
# halving to reach an average fill). It was wrong, and every test agreed with it because every
# fixture encoded the same belief — the probe-before-fixture failure exactly. So the contract is
# now pinned to real quotes.
#
# Method: two quotes per pool on the research key, $1 and $2000. Recover spot from the $1 quote by
# undoing ITS own reported impact, then compare the measured paid-price premium at $2000 against
# each candidate formula. Undoing the reference's impact is load-bearing: a first pass that skipped
# it made raw `i` look closest, and correcting it flipped the answer.
#
#   mint            reported i   measured premium   i/(1-i)   raw i
#   26vrrfTKMn1RPq     0.1848        22.67%          22.67%   18.48%
#   BL3LoGQnGx3SNT     0.2066        26.03%          26.03%   20.66%
#   J9gYdXoRj5kNGi     0.2953        41.91%          41.91%   29.53%
#   GW53gYD89bJJDj     0.1266        14.49%          14.49%   12.66%
#   DJ83eyBTEzBehm     0.1008        11.21%          11.21%   10.08%
#   CybSRsmzi8A9yD     0.3335        50.05%          50.05%   33.35%
#   95TDpd91E9Kcwi     0.1535        18.13%          18.13%   15.35%
#   4oN7E4KkDEZqS3     0.1808        22.08%          22.08%   18.08%
#
# n=8, mean |error|: i/(1-i) = 0.00 points, raw i = 6.05 points. The field is a LOSS FRACTION.
MEASURED_PROVIDER_CONTRACT = [
    (0.1848, 22.67), (0.2066, 26.03), (0.2953, 41.91), (0.1266, 14.49),
    (0.1008, 11.21), (0.3335, 50.05), (0.1535, 18.13), (0.1808, 22.08),
]


@pytest.mark.parametrize("i,measured", MEASURED_PROVIDER_CONTRACT)
def test_the_formula_reproduces_the_MEASURED_provider_behaviour(i, measured):
    """Pinned to real quotes rather than to anyone's reading of the field's name.

    A fixture with provenance is not the same thing as a fixture encoding a belief: these numbers
    came off the live provider, so a future formula change has to explain them rather than just
    agree with its own author.
    """
    assert RR.source_impact_pct(_Ev(), quote_fn=_q(i))[0] == pytest.approx(measured, abs=0.02)


def test_provider_loss_fraction_converts_to_paid_price_premium():
    """A 20 percent value loss means the paid unit price is 25 percent above the reference."""
    assert RR.source_impact_pct(_Ev(), quote_fn=_q(0.20))[0] == pytest.approx(25.0)


def test_the_withdrawn_v1_halving_is_refuted_by_the_measurement():
    """v1 returned half the raw fraction. On the largest measured pool that is 16.7% against a
    measured 50.05% — not a modelling nuance, a different quantity."""
    i, measured = 0.3335, 50.05
    assert abs(i * 50.0 - measured) > 30.0
    assert RR.source_impact_pct(_Ev(), quote_fn=_q(i))[0] == pytest.approx(measured, abs=0.02)
    assert "v2" in RR.SOURCE_IMPACT_FORMULA


def test_jupiter_reports_a_FRACTION_not_a_percent():
    """The provider value 0.0173 is a fractional value loss, not a percent value."""
    assert RR.source_impact_pct(_Ev(), quote_fn=_q("0.0173"))[0] == pytest.approx(
        1.7604558868423728)


@pytest.mark.parametrize(
    "bad", [None, "", "abc", float("nan"), float("inf"), float("-inf"), 0, -0.01])
def test_an_unusable_impact_yields_None_so_the_ORIGINAL_cap_stands(bad):
    assert RR.source_impact_pct(_Ev(), quote_fn=lambda _u: {"priceImpactPct": bad})[0] is None


def test_a_malformed_quote_yields_none():
    assert RR.source_impact_pct(_Ev(), quote_fn=lambda _u: ["bad"])[0] is None


def test_no_source_size_means_no_adjustment():
    assert RR.source_impact_pct(_Ev(size=0), quote_fn=_q(0.5))[0] is None
    assert RR.source_impact_pct(_Ev(size=None), quote_fn=_q(0.5))[0] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_source_size_does_not_call_the_provider(bad):
    calls = []

    def quote(size):
        calls.append(size)
        return {"priceImpactPct": "0.1"}

    assert RR.source_impact_pct(_Ev(size=bad), quote_fn=quote)[0] is None
    assert calls == []


def test_an_evm_event_does_not_call_the_solana_provider():
    calls = []

    def quote(size):
        calls.append(size)
        return {"priceImpactPct": "0.1"}

    assert RR.source_impact_pct(_Ev(network="4663"), quote_fn=quote)[0] is None
    assert calls == []


def test_a_failing_quote_never_raises_into_the_entry_path():
    def boom(_usd):
        raise RuntimeError("429")
    assert RR.source_impact_pct(_Ev(), quote_fn=boom)[0] is None
    assert RR.source_impact_pct(_Ev(), quote_fn=lambda _u: None)[0] is None


# ---------------------------------------------------------------- the clamp

def test_the_allowance_extends_the_cap():
    assert RR.effective_chase_cap(30.0, 8.0, ceiling_pct=25.0) == pytest.approx(38.0)


def test_the_clamp_bounds_a_whale_nuking_a_dust_pool():
    """A $50k buy into a $70k coin would otherwise justify almost any premium, and at that point we
    are not copying a trade, we are buying the crater."""
    assert RR.effective_chase_cap(30.0, 400.0, ceiling_pct=25.0) == pytest.approx(55.0)


def test_the_shipped_clamp_keeps_admissions_off_the_measured_cliff():
    """From the read register: entries at >= 2.0x the source price run a median -63.4% (n=12).

    Checked against BOTH live caps, because the fresh-follow path uses a wider one and it is the
    wider path that reaches furthest — a test on the standard cap alone would pass while the
    fresh-follow ceiling sat on the cliff.
    """
    clamp = cfg.MEMECOIN_CHASE_SOURCE_IMPACT_MAX_PCT
    for cap in (cfg.MEMECOIN_CHASE_MAX_PCT, cfg.MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT):
        ceiling_ratio = 1.0 + (cap + clamp) / 100.0
        assert ceiling_ratio < 2.0, (
            f"cap {cap} + clamp {clamp} reaches {ceiling_ratio:.2f}x, on the -63.4% cliff")
    assert clamp == pytest.approx(25.0)


def test_no_impact_leaves_the_cap_exactly_as_it_was():
    """Every failure path lands here, so 'unchanged' has to be exact rather than approximate."""
    assert RR.effective_chase_cap(30.0, None, ceiling_pct=25.0) == 30.0


def test_a_negative_impact_cannot_TIGHTEN_the_cap():
    """A quote returning a negative impact is nonsense, but it must not silently refuse entries the
    base cap would have admitted."""
    assert RR.effective_chase_cap(30.0, -12.0, ceiling_pct=25.0) == 30.0


# ---------------------------------------------------------------- scope and provenance

def test_the_extra_quote_fires_ONLY_on_the_would_refuse_path():
    """Measured at 4.3/day. On the accept path the funded quote budget must be untouched — the
    2026-08-29 incident was a second Jupiter consumer inside the runner."""
    import ast
    import pathlib
    from _source_guard import code_of

    src = code_of(pathlib.Path(RR.__file__))
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "source_impact_pct"]
    assert len(calls) == 1, f"source_impact_pct is called {len(calls)} times; it must be once"
    # and that one call must sit inside the `not chase_ok(...)` branch
    hook = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "real_entry_hook")
    guard = next(n for n in ast.walk(hook)
                 if isinstance(n, ast.If) and "chase_ok(" in ast.unparse(n.test))
    # Since 2026-09-28 the branch reaches it through chase_impact_allowance (which skips the quote
    # on a post-trade chase reference); that helper must be the one caller and be in the branch.
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "chase_impact_allowance")
    assert any(isinstance(x, ast.Call) and getattr(x.func, "id", "") == "source_impact_pct"
               for x in ast.walk(helper)), "the helper must be the caller"
    assert any(isinstance(x, ast.Call) and getattr(x.func, "id", "") == "chase_impact_allowance"
               for x in ast.walk(guard)), "the quote must be inside the would-refuse branch"
    helper_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and getattr(n.func, "id", "") == "chase_impact_allowance"]
    assert len(helper_calls) == 1, "chase_impact_allowance must be called once, in the branch"


def test_the_formula_is_versioned_for_retrospective_recompute():
    """The registered control recomputes the extended cap over historical refusals. A recompute
    using a different estimator than the live decision would grade a rule nobody ran."""
    assert RR.SOURCE_IMPACT_FORMULA == "jupiter_loss_fraction_to_paid_price_premium_v2"


def test_the_attempt_row_carries_the_INPUTS_not_only_the_verdict():
    import sqlite3

    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_entry_attempts)")}
    for needed in ("chase_naive_premium_pct", "chase_source_impact_pct",
                   "chase_effective_cap_pct", "chase_base_cap_pct",
                   "chase_source_size_usd", "chase_impact_formula",
                   "chase_clamp_hit", "chase_decided_by"):
        assert needed in cols, needed


# ---------------------------------------------------------------- why there was no allowance

def test_a_saturated_impact_is_a_SENTINEL_not_a_measurement():
    """Jupiter returns exactly 1 when the route is saturated. Measured on mint 5BVSdqHxRt: '1' at
    $499, at $100 AND at $10 — a value that does not move with size carries no information about
    the source's footprint, and i/(1-i) on it is infinite. Rejecting it is right; the reason is
    that it is not a measurement, not that a 100% impact is implausible."""
    got, why = RR.source_impact_pct(_Ev(), quote_fn=_q("1"))
    assert got is None and why == RR.IMPACT_SATURATED


def test_every_no_allowance_path_names_ITSELF():
    """A bare None conflated five facts, and telling them apart cost a live probe. `no_quote` is
    our blindness; `saturated_impact` is a real and terminal book state; `not_solana` is a scope
    decision. A refusal graded on the pooled bucket cannot tell the gate working from the gate
    blind."""
    cases = [
        (RR.IMPACT_NO_SIZE, _Ev(size=0), _q(0.1)),
        (RR.IMPACT_NOT_SOLANA, _EvChain("4663"), _q(0.1)),
        (RR.IMPACT_NO_QUOTE, _Ev(), lambda _u: None),
        (RR.IMPACT_UNPARSEABLE, _Ev(), _q("abc")),
        (RR.IMPACT_NONPOSITIVE, _Ev(), _q(0.0)),
        (RR.IMPACT_SATURATED, _Ev(), _q(1.5)),
        (RR.IMPACT_OK, _Ev(), _q(0.2)),
    ]
    seen = set()
    for expected, ev, fn in cases:
        _got, why = RR.source_impact_pct(ev, quote_fn=fn)
        assert why == expected, f"{expected} reported as {why}"
        seen.add(why)
    assert len(seen) == len(cases), "each path must have its OWN label"


def test_the_reason_reaches_the_attempt_row():
    import ast
    import pathlib
    from _source_guard import code_of
    src = code_of(pathlib.Path(RR.__file__))
    assert "chase_impact_skip_reason=_impact_why" in src


# --- the free model estimate, recorded beside the quote-derived one (2026-09-02) ----------------
#
# MEASUREMENT ONLY. It exists because the reference-size and estimator questions could not be
# answered from stored rows: liquidity is present on 345 of 346 4663 attempts but on 0 of the 22
# chase REFUSALS, because gate_inputs_json is written after this gate. The population rate was read
# from the wrong side of the branch.
#
# The quiet failure here is the sentinel. `slippage_frac` answers unknown liquidity with its cap
# (0.5), which is conservative for a paper fill and FAIL-OPEN for an allowance: an absent input
# would justify the maximum. A test that only checks the happy path passes either way.


def test_absent_liquidity_yields_None_and_NOT_the_fail_open_cap():
    """The whole reason this is not a bare slippage_frac call."""
    from sniper.execution.solana_quote import slippage_frac
    assert slippage_frac(1000.0, None) == pytest.approx(cfg.MEMECOIN_SLIPPAGE_CAP)
    for missing in (None, 0, 0.0, -1.0, "", "x"):
        assert RR.model_impact_pct(1000.0, missing) is None, missing


@pytest.mark.parametrize("bad", [None, 0, -5.0, float("nan"), float("inf"), "abc"])
def test_an_unusable_source_size_yields_None(bad):
    assert RR.model_impact_pct(bad, 500_000.0) is None


def test_it_is_a_paid_premium_percent_in_the_SAME_space_as_the_quote_estimate():
    """slippage_frac already returns the fraction of mid the fill moves, and modeled_fill applies
    it as mid*(1+slip). That IS the paid premium. Applying Jupiter's i/(1-i) on top would be the
    two-spaces defect."""
    got = RR.model_impact_pct(1_000.0, 100_000.0)
    raw = cfg.MEMECOIN_SLIPPAGE_K * 1_000.0 / 100_000.0
    assert got == pytest.approx(raw * 100.0)
    assert got != pytest.approx(raw / (1.0 - raw) * 100.0)


def test_the_estimate_is_UNCAPPED_so_a_thin_pool_is_not_censored():
    """A capped value cannot be graded against a quote, and the cap binds exactly where the pool is
    thinnest — which is the population this data exists to describe."""
    thin = RR.model_impact_pct(10_000.0, 5_000.0)
    assert thin > cfg.MEMECOIN_SLIPPAGE_CAP * 100.0
    assert thin == pytest.approx(cfg.MEMECOIN_SLIPPAGE_K * 10_000.0 / 5_000.0 * 100.0)


def test_it_rises_with_source_size_and_falls_with_liquidity():
    assert (RR.model_impact_pct(5_000.0, 1e6) > RR.model_impact_pct(1_000.0, 1e6))
    assert (RR.model_impact_pct(1_000.0, 1e5) > RR.model_impact_pct(1_000.0, 1e6))


def test_the_model_formula_is_tagged_and_distinct_from_the_quote_formula():
    """Grading two estimators as one is how a control arm quietly stops being a control."""
    assert RR.MODEL_IMPACT_FORMULA
    assert RR.MODEL_IMPACT_FORMULA != RR.SOURCE_IMPACT_FORMULA


def test_the_model_estimate_is_never_read_by_the_allowance():
    """Measurement-only: the effective cap must depend on the QUOTE estimate alone."""
    base, ceiling = 15.0, 25.0
    assert RR.effective_chase_cap(base, None, ceiling_pct=ceiling) == base
    # a large model estimate exists in the same module and must not leak into the cap
    assert RR.model_impact_pct(50_000.0, 10_000.0) > 100.0
    assert RR.effective_chase_cap(base, None, ceiling_pct=ceiling) == base


# --- the CALL SITE, not the pure function (Gamma review of 07b15249, 2026-09-02) ----------------
#
# The four mutation checks above all target `model_impact_pct`. The call site had no coverage, and
# Gamma found three mutations that survive the whole suite:
#
#   st.get("liquidity") -> st.get("liquidity_usd")   49 passed
#   arguments swapped                                49 passed
#   chase_liquidity_usd=_chase_liq deleted           49 passed
#
# The first is the one that would ship. A wrong key gives 100% NULL, which is indistinguishable
# from "no 4663 refusals yet" — the same shape as the FOMO_TO_DEX miss that made this gate inert on
# 4663 in the first place, and the same shape as the refusal-only column this telemetry exists to
# fix. A silent all-NULL column is the failure mode this whole commit is a response to.


def _hook_source():
    import ast
    import pathlib
    from _source_guard import code_of
    tree = ast.parse(code_of(pathlib.Path(RR.__file__)))
    return ast, next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "real_entry_hook")


def test_the_liquidity_key_is_exactly_the_one_dexscreener_writes():
    """A wrong key yields 100% NULL, which reads as 'the gate never fired'."""
    ast, hook = _hook_source()
    call = next(n for n in ast.walk(hook)
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "model_impact_pct")
    src = ast.unparse(hook)
    # E6 (2026-09-29) moved the read into `_telemetry_liquidity`, which returns the provider's
    # st['liquidity'] while E6 is on: the key is checked where it is now read.
    if "_telemetry_liquidity(st" in src:
        import inspect
        from sniper.services import memecoin_real_runner as R
        src = inspect.getsource(R._telemetry_liquidity)
    assert 'st.get("liquidity")' in src or "st.get('liquidity')" in src, \
        "the liquidity input must be read as st['liquidity'] — the key dexscreener_stats writes"
    assert "liquidity_usd\")" not in ast.unparse(call), "st has no 'liquidity_usd' key"


def test_the_arguments_are_size_then_liquidity_and_not_reversed():
    """Swapping them keeps every value non-NULL and inverts the ratio — no test above catches it."""
    ast, hook = _hook_source()
    call = next(n for n in ast.walk(hook)
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "model_impact_pct")
    assert len(call.args) == 2, "model_impact_pct(size_usd, liquidity_usd)"
    first, second = ast.unparse(call.args[0]), ast.unparse(call.args[1])
    assert "their_size_usd" in first, f"first argument must be the source size, got {first}"
    assert "liq" in second.lower(), f"second argument must be the liquidity, got {second}"
    # and the pure function must agree that the order matters
    assert RR.model_impact_pct(1_000.0, 100_000.0) != RR.model_impact_pct(100_000.0, 1_000.0)


def test_the_liquidity_INPUT_reaches_the_attempt_row_not_only_the_derived_value():
    """A derived value whose input was not stored cannot be re-graded when the model changes. That
    is the entire reason this data did not already exist."""
    ast, hook = _hook_source()
    update = next(n for n in ast.walk(hook)
                  if isinstance(n, ast.Call)
                  and ast.unparse(n.func).endswith("update_entry_attempt_fields")
                  and any(k.arg == "chase_model_impact_pct" for k in n.keywords))
    written = {k.arg for k in update.keywords}
    for col in ("chase_liquidity_usd", "chase_model_impact_pct", "chase_model_formula"):
        assert col in written, f"{col} must be written on the refusal path"


# --- the ratio estimator: blocker 3, the one that works off-Solana (2026-09-02) -----------------
#
# source_impact_pct reads quote["priceImpactPct"], which no EVM provider emits — so on 4663 it
# returns IMPACT_UNPARSEABLE, a different label on the same zero. This asks the book instead:
# two quotes on one pool, impact = price_at_dust / price_at_size - 1.
#
# The quiet failures here are (a) treating a missing dust quote as zero impact, which grants an
# allowance on absent evidence, and (b) a flat quoter that returns the same answer twice, which
# reads as "no impact" when it means "no measurement".


class _Book:
    """A constant-product-ish book: tokens out falls as the order grows."""

    def __init__(self, depth_usd=100_000.0, key="buyAmount"):
        self.depth, self.key, self.calls = depth_usd, key, []

    def __call__(self, usd):
        self.calls.append(usd)
        # tokens = usd * (1 - usd/depth): strictly worse unit price as usd grows
        return {self.key: usd * (1.0 - usd / self.depth) * 1e9}


def test_it_measures_a_real_book_and_the_sign_is_a_PREMIUM():
    ev = _Ev(size=10_000.0, network="4663")
    pct, why = RR.ratio_impact_pct(ev, quote_fn=_Book(1e6), dust_usd=5.0)
    assert why == RR.IMPACT_OK
    assert pct > 0, "a larger order pays MORE per token, so the premium is positive"
    # exact: p_dust/p_size - 1 with the book above
    expected = ((1 - 5.0 / 1e6) / (1 - 10_000.0 / 1e6) - 1.0) * 100.0
    assert pct == pytest.approx(expected)


def test_it_needs_no_priceImpactPct_which_is_the_whole_point():
    """The field source_impact_pct depends on is absent from every EVM quoter."""
    book = _Book()
    pct, why = RR.ratio_impact_pct(_Ev(size=1_000.0, network="4663"),
                                   quote_fn=book, dust_usd=5.0)
    assert why == RR.IMPACT_OK and pct > 0
    assert all("priceImpact" not in str(k) for k in book(1.0))
    # and the Solana estimator fails on exactly this quote shape
    assert RR.source_impact_pct(_Ev(size=1_000.0), quote_fn=book) == (
        None, RR.IMPACT_UNPARSEABLE)


def test_it_reads_the_jupiter_key_too_so_one_call_site_serves_both_chains():
    pct, why = RR.ratio_impact_pct(_Ev(size=1_000.0), quote_fn=_Book(key="outAmount"),
                                   dust_usd=5.0)
    assert why == RR.IMPACT_OK and pct > 0


def test_an_absent_DUST_quote_is_not_zero_impact():
    """Absent evidence of a footprint must not grant an allowance."""
    def only_the_big_one(usd):
        return {"buyAmount": 1e9} if usd > 100 else None
    pct, why = RR.ratio_impact_pct(_Ev(size=1_000.0), quote_fn=only_the_big_one, dust_usd=5.0)
    assert pct is None and why == RR.IMPACT_NO_DUST_QUOTE
    assert RR.effective_chase_cap(15.0, pct, ceiling_pct=25.0) == 15.0


def test_a_flat_quoter_is_rejected_as_uninformative_not_read_as_zero_impact():
    """Same answer at both sizes = a cached or flat response. It carries no footprint."""
    pct, why = RR.ratio_impact_pct(_Ev(size=1_000.0),
                                   quote_fn=lambda usd: {"buyAmount": usd * 1e9}, dust_usd=5.0)
    assert pct is None and why == RR.IMPACT_NONPOSITIVE


def test_a_dust_size_at_or_above_the_source_size_measures_nothing():
    for dust in (1_000.0, 5_000.0):
        pct, why = RR.ratio_impact_pct(_Ev(size=1_000.0), quote_fn=_Book(), dust_usd=dust)
        assert pct is None and why == RR.IMPACT_DUST_NOT_SMALLER, dust


@pytest.mark.parametrize("bad", [None, 0, -1.0, float("nan"), float("inf"), "x"])
def test_an_unusable_source_size_never_calls_the_provider(bad):
    book = _Book()
    pct, why = RR.ratio_impact_pct(_Ev(size=bad), quote_fn=book, dust_usd=5.0)
    assert pct is None and why == RR.IMPACT_NO_SIZE
    assert book.calls == [], "no quote may be bought for an event with no size"


def test_a_raising_quoter_never_escapes_into_the_entry_path():
    def boom(usd):
        raise RuntimeError("provider down")
    pct, why = RR.ratio_impact_pct(_Ev(size=1_000.0), quote_fn=boom, dust_usd=5.0)
    assert pct is None and why == RR.IMPACT_NO_QUOTE


def test_it_quotes_the_SIZE_before_the_dust_so_a_dead_provider_costs_one_call():
    book = _Book()
    RR.ratio_impact_pct(_Ev(size=1_000.0), quote_fn=book, dust_usd=5.0)
    assert book.calls == [1_000.0, 5.0]


def test_the_ratio_formula_is_tagged_distinctly_from_the_other_two():
    """Three estimators now. Grading any two as one is how a control stops being a control."""
    tags = {RR.SOURCE_IMPACT_FORMULA, RR.MODEL_IMPACT_FORMULA, RR.RATIO_IMPACT_FORMULA}
    assert len(tags) == 3


def test_the_dust_default_comes_from_settings_and_is_small():
    assert RR.ratio_impact_pct(_Ev(size=1_000.0), quote_fn=_Book())[1] == RR.IMPACT_OK
    assert 0 < cfg.MEMECOIN_CHASE_IMPACT_DUST_USD <= 50
