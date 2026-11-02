"""A manual buy enters what the operator names; a manual buy is still not allowed to lose the money.

The operator's decision, 2026-08-27: SELECTION gates are bypassed for manual entry, SAFETY gates are
not. A selection gate judges whether the coin is a GOOD BUY, which is a judgement the operator is
making by clicking. A safety gate protects the capital, which no click makes unnecessary.

The value of this file is the LAST test. Enumerating the exemptions by hand rots the moment someone
adds a gate. So it walks the hook's own syntax tree, finds every refusal, and forces each one to be
declared here on purpose — a new gate fails this file until somebody decides which kind it is.
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

from sniper.services import memecoin_real_runner as RR

REFUSAL_STATUSES = {"rejected", "skipped_chase"}

# Every refusal inside real_entry_hook is a SELECTION gate: it judges whether the coin is worth
# buying. All are bypassed for a manual entry. The source-buy floor reached that state first, on
# 2026-08-23, using an inline `signal_source != "manual"` rather than the `_manual_entry` flag —
# both idioms count, which is why _is_exempt accepts either.
SELECTION = {
    "coin already ran",            # chase vs the copied trader's fill
    "coin ran %.1fx above",        # first-seen chase ratio
    # m5 / h1 volume floors. Matched by "revival guard" until 2026-09-25, when that label was
    # dropped from the refusal text.
    "m5 trades %s under floor",    # m5 trade-count floor
    "volume $%.0f %s $%.0f",       # m5 / h1 dollar and turnover floors, m5 ceiling
    "solana_volume_under_floor",  # fresh complete in-memory organic m5 floor
    "no-movement class",           # coin-age ceiling
    "coin band is not reservable",
    "_ss_reason",                  # sell-share dump filter
    "source buy",                  # legacy literal source-buy refusal
    "source_reason",               # computed source-buy refusal (manual-exempt before assignment)
    # WINNER RE-ENTRY (2026-08-27). Both replace what the blanket cooldown used to do for a coin
    # whose last real close won. Selection: they judge whether THIS re-entry is worth taking, and
    # the operator making a manual buy has already made that judgement.
    "winner re-entry above our exit",
    "winner re-entry daily cap",
    "route capacity is below the trade floor",
    # GLOBAL FEED (2026-09-06). Selection in the purest form: it judges the SIGNAL'S ORIGIN, not
    # the coin's risk. In the 14 days to 2026-09-06 the global feed produced 16 real fills for
    # -$79.9 while follow_activity produced 394 for +$942.8, so the origin alone is the judgement.
    # An operator clicking Buy has made that judgement themselves, and `is_manual_entry` is checked
    # first, so a manual buy never reaches this gate whatever the switch says.
    "global_feed entries disabled",
    # MARKET-DATA PROVIDER (2026-09-16, Arc build). Judges whether the coin can be assessed at
    # all, the same category as the revival guard and coin-age ceiling above: an operator's
    # manual click already IS the coin-quality judgement the automated gates exist to make.
    "_no_market_data",
    # DEAD-COIN GATE (2026-10-07, SWCH). Old AND quiet is a coin-quality judgement, like the volume
    # floors above; an operator's manual click already made it (`_dc_enforced` checks _manual_entry).
    "dead coin: %.0fd old",
    # ADD-ON REFUSAL (2026-10-17, memecoin_addon_guard). Selection: it judges the source's buy
    # pattern (an add-on after a missed first buy); the hook gates it on `not _manual_entry`.
    "_addon[0]",
    "_window_reason",              # first source transaction and original observed slot window
}

# The SAFETY gates are not in this hook. Token safety, the impact cap, the round-trip cap, the
# bankroll reservation and the kill switch all refuse further down, inside real_entry and the
# executor, so the tree walk below cannot see them. That is exactly why the last test in this file
# exists: it pins that no code outside this one hook ever learned the word "manual".
SAFETY_OWNING_MODULE = "sniper/services/memecoin_real_runner.py"

# WHERE "manual" MAY AND MAY NOT APPEAR (widened 2026-08-27, after the re-entry policy fix).
#
# The layering is the invariant, not the word. A CALLER owns policy and may know who asked; the
# risk modules receive finished policy sets and must never learn it, because the moment a risk
# module branches on the requester, "does this trade pass" and "who is asking" stop being separable
# and every future gate inherits the ambiguity.
#
# The legs are callers, so they legitimately name it — but only to bind a policy PARAMETER. A leg
# naming "manual" next to anything else is a bypass being added at the wrong altitude.
POLICY_OWNING_MODULES = (
    "sniper/services/memecoin_real_leg.py",
    "sniper/services/memecoin_evm_leg.py",
)
POLICY_BLIND_MODULES = (
    "sniper/services/memecoin_risk.py",
    "sniper/services/memecoin_evm_risk.py",
)
# The only parameters a leg may switch on the requester. Adding one is a deliberate act and
# belongs here in writing.
# `_apply_cooldown` joined the list 2026-08-28: the leg now resolves the manual signal and the
# wallet's per-gate exemptions into ONE named switch instead of repeating the expression at each
# hop, because a per-wallet exemption names a single re-entry family and the two must move
# independently. The guard fired on it, which is the deliberate act it exists to force.
# `_apply_quiet_hours` joined 2026-09-11: the operator's UTC quiet window stands down AUTOMATIC
# entries only. A manual buy is the operator's own hand, so the leg resolves the manual signal into
# this one named switch before consulting the window. It is a schedule, not a safety gate.
ALLOWED_POLICY_PARAMS = ("include_reentry_policy", "include_losing_reentry",
                         "size_override", "_apply_cooldown", "enforce_concurrency",
                         "funded_policy", "_apply_quiet_hours")


def _is_exempt(condition):
    return "_manual_entry" in condition or "signal_source" in condition


def _hook_gates():
    """(reason_fragment, enclosing_condition_source) for every refusal inside real_entry_hook."""
    tree = ast.parse(inspect.getsource(RR.real_entry_hook))
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        if "status" not in keys:
            continue
        status = node.values[keys.index("status")]
        if not (isinstance(status, ast.Constant) and status.value in REFUSAL_STATUSES):
            continue
        # EVERY enclosing condition, not the nearest: a guard on an outer `if` bypasses the gate
        # just as effectively, and reading only the innermost would call that a missing exemption.
        conds, cur = [], parent.get(node)
        while cur is not None:
            if isinstance(cur, ast.If):
                conds.append(ast.unparse(cur.test))
            cur = parent.get(cur)
        out.append((ast.unparse(node), " AND ".join(conds)))
    return out


def test_is_manual_entry_reads_the_signal_source():
    assert RR.is_manual_entry(SimpleNamespace(signal_source="manual")) is True
    assert RR.is_manual_entry(SimpleNamespace(signal_source="follow_activity")) is False
    assert RR.is_manual_entry(SimpleNamespace()) is False


def test_every_selection_gate_is_bypassed_for_a_manual_entry():
    gates = _hook_gates()
    assert len(gates) >= 6, "the tree walk found almost nothing — it stopped seeing the gates"
    for payload, condition in gates:
        if any(frag in payload for frag in SELECTION):
            assert _is_exempt(condition), (
                f"selection gate still refuses a manual buy: {payload!r} under {condition!r}")


def test_every_refusal_is_declared_as_selection():
    """The guard that survives the next gate. Add one, and you must classify it here."""
    undeclared = [p for p, _ in _hook_gates() if not any(f in p for f in SELECTION)]
    assert not undeclared, (
        "a refusal in real_entry_hook is not declared in SELECTION. Decide what it does on a "
        "manual buy: bypass it and list it there, or leave it enforced and say why here.\n  "
        + "\n  ".join(undeclared))


def test_no_safety_gate_anywhere_learned_the_word_manual():
    """The real protection. Selection lives in one hook; safety must stay blind to who asked.

    If a manual exemption ever appears in the token-safety, impact, round-trip, reservation or
    kill-switch path, "enter anything I put in" has quietly become "spend it on anything", which is
    not what the operator chose on 2026-08-27.
    """
    import pathlib
    tree = ast.parse(_manual_code(SAFETY_OWNING_MODULE))
    # `_hook_raw` owns source identity before it calls `real_entry_hook`. A manual intent has no
    # copied source identity, so that selection check must not block it. It does not own a risk
    # gate. Keep this function named here so another manual-aware wrapper stays visible.
    allowed = {"real_entry_hook", "is_manual_entry", "make_real_hook", "_hook_raw",
               "_first_buy_window_applies"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in allowed:
            continue
        body = ast.unparse(node)
        if "_manual_entry" in body or "is_manual_entry" in body:
            offenders.append(node.name)
    assert not offenders, (
        "manual-entry awareness leaked outside the selection hook, into: "
        + ", ".join(sorted(offenders)))


def _manual_code(rel):
    """Comment-free, docstring-free source. A module docstring naming the manual signal source
    would otherwise register as a reference in a module required to hold none."""
    import pathlib
    from _source_guard import code_of
    return code_of(pathlib.Path(rel))


def _manual_lines(rel, *, context=2):
    """(lineno, statement) for every non-comment line naming the manual signal source.

    The unit is the STATEMENT, not the line: `size_override = (trade_size if ... == "manual" else
    None)` wraps, so the line holding "manual" does not hold the parameter it binds. Matching
    line-by-line called that a bypass. The preceding `context` lines are joined in so a wrapped
    binding is judged by what it actually assigns.
    """
    # code_of also emits ONE STATEMENT PER LINE, which removes the wrapped-assignment trap this
    # guard hit on its first run: the line holding "manual" did not hold the parameter it binds.
    lines = _manual_code(rel).splitlines()
    out = []
    for i, line in enumerate(lines, 1):
        if line.strip().startswith("#"):
            continue
        # The desk origin (2026-10-21) routes the same decision through memecoin_origin's two
        # helpers; a call to either is the requester being named, exactly like the literal.
        if ('"manual"' in line or "'manual'" in line or "enforces_capacity(" in line
                or "bypasses_selection(" in line):
            start = max(0, i - 1 - context)
            stmt = " ".join(l.strip() for l in lines[start:i]
                            if not l.strip().startswith("#"))
            out.append((i, stmt))
    return out


def test_the_risk_modules_never_learn_who_asked():
    """They receive policy SETS. A risk module that branches on the requester stops being able to
    answer "does this trade pass" independently of "who is asking", and every gate added after it
    inherits that. Callers own policy; this is the layer that must not."""
    offenders = []
    for rel in POLICY_BLIND_MODULES:
        offenders += [f"{rel}:{n}  {t}" for n, t in _manual_lines(rel)]
    assert not offenders, (
        "a risk module named the manual signal source; policy belongs to the caller:\n  "
        + "\n  ".join(offenders))


def test_a_leg_may_switch_policy_on_manual_and_nothing_else():
    """The legs ARE the callers, so naming it is correct — but only to bind a policy parameter.

    Live 2026-08-27: the re-entry fix had to touch two hops in memecoin_real_leg, and a third hop
    added later without the same expression would silently re-apply the policy to manual buys. That
    is the failure this catches. It also catches the opposite one: a leg quietly widening a SAFETY
    gate for manual, which is the thing the whole split exists to prevent.
    """
    offenders = []
    for rel in POLICY_OWNING_MODULES:
        for n, text in _manual_lines(rel):
            if not any(param in text for param in ALLOWED_POLICY_PARAMS):
                offenders.append(f"{rel}:{n}  {text}")
    assert not offenders, (
        "a leg switched on the manual signal source for something other than "
        + "/".join(ALLOWED_POLICY_PARAMS)
        + ". If that is deliberate, add the parameter to ALLOWED_POLICY_PARAMS and say why:\n  "
        + "\n  ".join(offenders))


def test_the_guard_is_watching_something():
    """A guard over an empty set passes forever. Both legs really do carry the expression."""
    found = sum(len(_manual_lines(rel)) for rel in POLICY_OWNING_MODULES)
    assert found >= 3, (
        f"only {found} manual references across the legs — the policy expression was removed, "
        "or these paths moved and this guard is now watching nothing")


# ------------------------------------------------------------------ per-wallet gate exemptions

EXEMPTION_SYMBOLS = ("wallet_gate_exemptions", "entry_gate_exemptions", "gate_exempt_families_for",
                     "_wallet_exempt", "_exempt", "EXEMPTIBLE_GATE_FAMILIES")


def test_the_risk_modules_never_read_exemption_data():
    """Same layering rule as the manual signal, extended 2026-08-28. A risk module that knows a
    wallet is excused stops being able to answer "does this trade pass" independently of "who is
    asking", and every gate added after it inherits that. Callers resolve exemptions into finished
    policy sets; the risk layer receives sets and nothing else."""
    offenders = []
    for rel in POLICY_BLIND_MODULES:
        code = _manual_code(rel)
        for sym in EXEMPTION_SYMBOLS:
            if sym in code:
                offenders.append(f"{rel} reads {sym}")
    assert not offenders, ("exemption data reached a risk module: " + "; ".join(offenders))


def test_exemptions_resolve_only_in_policy_owning_modules():
    """And they must resolve SOMEWHERE, or the guard above passes over a feature that was removed.
    The selection hook and the leg are the two policy owners that may look a wallet up."""
    import pathlib
    from _source_guard import code_of
    resolvers = [rel for rel in (SAFETY_OWNING_MODULE,) + POLICY_OWNING_MODULES
                 if "gate_exemptions" in code_of(pathlib.Path(rel))]
    assert len(resolvers) >= 2, (
        "expected the selection hook and the leg to resolve exemptions; found " + str(resolvers))


def test_only_selection_families_are_exemptible():
    """The exemptible set must never grow a safety gate. execution_premium and route_over_mid are
    cost gates — the same reasoning that keeps them out of the manual bypass."""
    from sniper.persistence.db_memecoin import EXEMPTIBLE_GATE_FAMILIES
    for safety in ("execution_premium", "route_over_mid"):
        assert safety not in EXEMPTIBLE_GATE_FAMILIES


def test_every_exemptible_family_has_a_taxonomy_name():
    """One vocabulary: an exemption name IS a refusal-capture family. A family here that
    classify_gate cannot produce would be an exemption that never matches a real refusal."""
    from sniper.persistence.db_memecoin import EXEMPTIBLE_GATE_FAMILIES
    from sniper.services.memecoin_refusal_capture import GATES
    known = {g for g, _ in GATES}
    missing = sorted(EXEMPTIBLE_GATE_FAMILIES - known)
    assert not missing, "exemptible families with no capture-taxonomy name: " + ", ".join(missing)


def test_the_min_trade_floor_is_a_selection_gate_and_a_manual_buy_bypasses_it():
    """Live 2026-09-03: two manual buys on 43uJZGxf… refused at 13% of equity = $4.18 against a
    $5.00 floor. By this file's own doctrine the floor is SELECTION — it judges whether the clip
    is worth taking, not whether the capital is safe, and a $4.18 buy risks $4.18. Its sibling
    "route capacity is below the trade floor" was already exempt; this one escaped only because it
    lives in build_memecoin_risk_context rather than in real_entry_hook, where the tree walk below
    can see it."""
    from sniper.services.memecoin_risk import MemecoinRiskCtx, memecoin_entry_allowed

    def _ctx(floor):
        return MemecoinRiskCtx(
            usdc_balance=32.15, sol_balance=1.0, effective_concurrent=0,
            effective_exposure_usd=0.0, effective_pending_notional_usd=0.0,
            held_coin_ids=frozenset(), daily_tripped=False, real_enabled=True,
            kill_tripped=False, trade_size_usd=4.18, max_concurrent=3,
            max_total_exposure_usd=700.0, min_trade_floor_usd=floor,
            gas_entry_sol=0.0, gas_exit_sol=0.0, gas_retry_factor=1.0)

    blocked = memecoin_entry_allowed(_ctx(5.0), mint="43uJZGxf", trade_size=4.18)
    assert not blocked.allowed
    # The refusal must carry BOTH numbers. "-EV at this clip" alone reads as a verdict on the coin,
    # so the operator cannot tell a small clip from a nearly-empty wallet.
    assert "4.18" in blocked.reason and "5.00" in blocked.reason, blocked.reason

    # enforce_min_floor=False zeroes the floor, which is how the manual path bypasses it.
    assert memecoin_entry_allowed(_ctx(0.0), mint="43uJZGxf", trade_size=4.18).allowed


def test_manual_signal_source_turns_the_floor_off_end_to_end():
    """The flag must be DERIVED from signal_source, not passed by hand at one call site."""
    import inspect
    from sniper.services import memecoin_real_leg as RL
    src = inspect.getsource(RL)
    assert '_enforce_min_floor = _enforce_concurrency' in src, (
        "the min-floor exemption must track the same manual condition as concurrency")
    # Both hops must receive it: preflight AND the reservation. The 2026-08-27 manual re-buy fix
    # missed exactly this second hop, so a fixed reserve_entry alone never ran.
    assert src.count("enforce_min_floor=_enforce_min_floor") == 2, (
        "preflight and reserve_entry must BOTH get the flag")
