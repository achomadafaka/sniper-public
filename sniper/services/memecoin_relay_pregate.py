"""Relay option A section 5 (spec 2026-10-09): the label-only pre-gate beside the Relay wait.

An on-chain Relay buy that waits on Relay's API was refused 8 s late as `wallet_initiation_unproved`
when the coin was refused anyway: ABYSS (4663, about $4.3M FDV) over the mcap ceiling. While such a row
waits (never when a deposit proved it), the watcher asks this pre-gate whether the hook's own mcap
ceiling or source-buy floor would refuse it. A label names the refusal; it authorises nothing and refuses
nothing that the unproved row would not already refuse.

ONE DECISION, THE HOOK'S OWN PIECES (Alpha 2026-10-09, option ii; no hook refactor): the mcap ceiling
is `mcap_entry_decision` with `_wallet_mcap_ceiling`/`_wallet_mcap_floor` and `_bigcap_lift`; the floor
is `source_buy_family_reason` with `_band_source_buy_ratio`, `_wallet_source_buy_ratio` and
`min_source_buy_usd_for`, in the hook's order (ceiling first). It is SILENT unless certain:
- the wallet is not armed on the chain, or the family is exempt for it;
- the mcap is unknown or came from a source this pre-gate does not read (it reads only the feed
  history `resolve_mint_mcap` and the volume observer's cached supply: no RPC, no provider);
- the ceiling decision is not `over_ceiling` (an under-floor or unresolvable mcap is the hook's call);
- auto-templates could admit the coin;
- the source size is unknown.
Gates the hook runs between the mcap stage and the floor are not modelled; the agreement check
(scripts/research/relay_pregate_agreement.py) replays recorded attempts to prove the labels match.

THE RUNNER'S CONFIG, NOT THIS PROCESS'S (Gamma M2, 2026-10-09). The runner judges on the knobs in
memecoin_entry_knob_overrides (pushed onto its settings module by its own cycle only) and on the .env it
last started with. The watcher's settings module holds neither, so `decision` takes every value it
judges with in `knobs` (an absent name falls back to the settings module) and `for_row` passes
memecoin_entry_knobs.effective() read-only (never apply_overrides: nothing here writes the settings
module under the worker threads). A watcher whose env files are newer than its own start is silent
(`config_stale`): its per-wallet and lift settings may not be the runner's.
"""
from __future__ import annotations

import math

ALLOWED_BASES = ("history", "evm_source", "evm_source_cached")
CEILING, FLOOR = "over_ceiling", "source_buy_floor"


def _number(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def env_stale(paths, started_at):
    """True when any existing env file changed after `started_at` (this process's start)."""
    import os
    for path in paths:
        try:
            if os.path.getmtime(path) > started_at:
                return True
        except OSError:
            continue
    return False


def runner_started_at(heartbeat_path):
    """The funded runner's process start (epoch) from its heartbeat's started_at, or None (no file,
    unreadable, no field). The caller treats None as silent: the runner's env age is unknown."""
    import json
    from datetime import datetime
    try:
        with open(heartbeat_path) as f:
            value = json.load(f).get("started_at")
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() if value else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def stale_reason(env_paths, started_at, runner_heartbeat=None):
    """Why the env this process and the runner judge with may differ, or None. Both directions
    (Gamma M2, L4): env files newer than THIS process (it judges on an old env), or newer than the
    RUNNER's start (the runner does: a watcher that restarted after an edit the runner has not seen)."""
    if env_stale(env_paths, started_at):
        return "config_stale"
    if runner_heartbeat is not None:
        runner = runner_started_at(runner_heartbeat)
        if runner is None:
            return "runner_start_unknown"
        if env_stale(env_paths, runner):
            return "config_stale_runner"
    return None


def decision(*, chain, source_user_id, mcap, mcap_basis, their_usd, prior_net_usd, armed,
             exempt=frozenset(), lift_fn=None, info=None, knobs=None):
    """(label class, reason text) when the hook would refuse with that class, else (None, why silent).
    `lift_fn(mcap, ceiling)` is the big-cap lift's lifted ceiling or None; None means no lift.
    `info`, when given, receives the bars it judged against (ceiling, ratio, abs_min). `knobs` holds
    the values to judge with by settings name (the runner's effective ones); absent names read the
    settings module."""
    info = {} if info is None else info
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    knobs = knobs or {}

    def k(name):
        return knobs[name] if name in knobs else getattr(cfg, name, None)
    chain = str(chain)
    if not armed:
        return None, "not_armed"
    mcap = _number(mcap)
    if mcap is None or mcap <= 0 or mcap_basis not in ALLOWED_BASES:
        return None, "mcap_uncertain"
    ceiling = rr._wallet_mcap_ceiling(k("MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET") or {}, chain, source_user_id,
                                      k("MEMECOIN_REAL_MAX_MCAP_USD"))
    mcap_floor = rr._wallet_mcap_floor(k("MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET") or {}, chain, source_user_id,
                                       k("MEMECOIN_REAL_MIN_MCAP_USD"))
    allow, eff, reason = rr.mcap_entry_decision(mcap, None, ceiling, mcap_floor)
    info["ceiling"] = ceiling
    if (not allow and k("MEMECOIN_AUTO_TEMPLATES") == "on" and eff is not None
            and eff >= k("MEMECOIN_SCALP_MCAP_USD")):
        return None, "auto_templates"
    if not allow and reason == "over_ceiling" and lift_fn is not None:
        lifted = lift_fn(eff, ceiling)
        if lifted:
            ceiling = lifted
            allow, eff, reason = rr.mcap_entry_decision(eff, None, ceiling, mcap_floor)
    if not allow:
        if reason != "over_ceiling":
            return None, "mcap_" + str(reason)
        return CEILING, "mcap $%s over ceiling $%s (over_ceiling)" % (
            format(int(eff), ","), format(int(ceiling or 0), ","))
    if FLOOR in exempt:
        return None, "floor_exempt"
    their_usd = _number(their_usd)
    if their_usd is None or their_usd < 0:
        return None, "size_unknown"
    ratio = max(0.0, float(k("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO") or 0.0))
    unknown_floor = max(0.0, float(k("MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD") or 0.0))
    band_ratios = k("MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND") or {}
    if eff is not None:
        ratio = rr._band_source_buy_ratio(band_ratios, chain, eff, ratio)
    ratio = rr._wallet_source_buy_ratio(chain, source_user_id, ratio,
                                        by_wallet=k("MEMECOIN_SOLANA_MIN_SOURCE_BUY_MCAP_RATIO_BY_WALLET") or {})
    # min_source_buy_usd_for with the global floor taken from `knobs`: a per-wallet floor is env.
    abs_min = cfg.source_buy_floor_override(chain, source_user_id)
    if abs_min is None:
        abs_min = _number(k("MEMECOIN_REAL_MIN_SOURCE_BUY_USD")) or 0.0
    abs_min = max(0.0, abs_min)
    info.update(ratio=ratio, abs_min=abs_min)
    abs_max = max(0.0, float(k("MEMECOIN_REAL_MAX_SOURCE_BUY_USD") or 0.0))
    reason, _info = rr.source_buy_family_reason(
        their_usd=their_usd, source_mcap=eff, prior_net_usd=_number(prior_net_usd), ratio=ratio,
        unknown_mcap_floor=unknown_floor, abs_min=abs_min, abs_max=abs_max,
        family_active=bool(ratio > 0 or unknown_floor > 0 or band_ratios),
        floor_tolerance_pct=k("MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT") or 0.0)
    if reason:
        return FLOOR, reason
    return None, "passes"


def hook_class(terminal_reason):
    """The class of the hook's own refusal: over_ceiling, source_buy_floor, or None."""
    text = str(terminal_reason or "")
    if "(over_ceiling)" in text:
        return CEILING
    if text.startswith("source buy "):
        return FLOOR
    return None


def for_row(conn, row, *, chain, now_iso, env_paths=(), started_at=None, runner_heartbeat=None):
    """The live pre-gate for a watcher Relay row, on local reads only: (class, text, inputs). It judges
    with the runner's effective knobs (overrides included) and is silent on a stale env, either way."""
    from types import SimpleNamespace
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_entry_knobs as entry_knobs
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    chain = str(chain)
    if started_at is not None:
        stale = stale_reason(env_paths, started_at, runner_heartbeat)
        if stale:
            return None, stale, dict(silent=stale)
    knobs = {name: value for name, (value, _source) in entry_knobs.effective(conn, cfg).items()
             if value is not None}
    buy = row.get("buy") or {}
    proof = buy.get("source_initiation") or {}
    uid, mint = row.get("source_user_id"), proof.get("mint") or buy.get("mint")
    price, their_usd = buy.get("source_price"), buy.get("source_size_usd")
    origin = "onchain:%s:%s:%s" % (chain, row.get("tx_hash") or proof.get("tx_hash"), mint)
    armed = uid in m.real_approved_user_ids(conn, network_id=chain)
    exempt = frozenset(m.entry_gate_exemptions(conn, uid, chain) or ()) if armed else frozenset()
    own_evm = chain in rr._evm_chain_knob(getattr(cfg, "MEMECOIN_EVM_OWN_MCAP_CHAINS", ""),
                                          "MEMECOIN_EVM_OWN_MCAP_CHAINS")
    mcap, basis = (m.resolve_mint_mcap(conn, mint, network_id=chain,
                                       since_iso=cfg.real_history_since_iso(now_iso), price=price,
                                       priced=True, supply_band=own_evm), "history")
    if not mcap:
        mcap, basis = rr._cached_evm_source_mcap(chain, mint, price), "evm_source_cached"
    if not mcap:
        basis = None
    window = float(getattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_CUMULATIVE_WINDOW_S", 0) or 0)
    prior = (m.source_prior_net_buy_usd(conn, source_user_id=uid, network_id=chain, mint=mint,
                                        before_iso=now_iso, window_s=window, exclude_event_id=origin,
                                        judged_usd=_number(their_usd))
             if window > 0 and armed else None)
    ev = SimpleNamespace(source_user_id=uid, their_size_usd=_number(their_usd), mint=mint,
                         fomo_event_id=origin, source_initiation=proof, network_id=chain,
                         their_price=_number(price))
    cls, text = decision(chain=chain, source_user_id=uid, mcap=mcap, mcap_basis=basis,
                         their_usd=their_usd, prior_net_usd=prior, armed=armed, exempt=exempt,
                         lift_fn=lambda mc, ceiling: rr._bigcap_lift(ev, chain, mc, ceiling), knobs=knobs)
    overrides = sorted(name for name, (_v, source) in entry_knobs.effective(conn, cfg).items()
                       if source == "override")
    return cls, text, dict(mcap=mcap, mcap_basis=basis, their_usd=_number(their_usd), prior_net_usd=prior,
                           armed=armed, silent=None if cls else text, overrides=overrides)
