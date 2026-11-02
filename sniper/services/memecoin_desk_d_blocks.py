"""Cohort D sheet blocks with explicit inputs (moved out of scripts/memecoin_desk_judge_worker.py for the Office sheet
tool, Task 5; the worker imports these names unchanged)."""
from __future__ import annotations

from pathlib import Path


def history_blocks(desk_dir, mint, pool, *, sheet_as_of, decision_at, pass_persist_at, offset, market_cut):
    """(creator_history, coin_age) for the D sheet. Lambda's readers compare CORRECTED stamps (fetched_at minus its
    stored offset), so our three host times are converted with ONE offset sampled for this sheet (Alpha/Gamma).
    Unknown offset = both blocks unknown:clock_unknown, never an implicit 0."""
    from sniper.services import memecoin_desk_history_facts as H
    if offset is None:
        times = dict(sheet_as_of=sheet_as_of, decision_at=decision_at, pass_persist_at=pass_persist_at)
        history = H.creator_history_fact("/nonexistent", mint, **times)
        age = H.coin_age_fact("/nonexistent", mint, **times)["coin"]
        for row in (history, history["creator_prior_launches_lb"], age):
            row["reason"] = "clock_unknown"
        history["history_reason"] = "clock_unknown"
        history["field_unknown"] = {k: "clock_unknown" for k in history["field_unknown"]}
        return history, age
    times = dict(sheet_as_of=sheet_as_of - offset, decision_at=decision_at - offset,
                 pass_persist_at=pass_persist_at - offset)
    # market_cut stays host (swaps_ps receipts); launches rows carry no stored offset, so ours is passed (Lambda Am13)
    history = H.creator_history_fact(desk_dir, mint, pool=pool, market_cut=market_cut, host_offset_s=offset, **times)
    history["as_of"] = sheet_as_of                      # the block binds to the sheet clock (ps_facts.as_of)
    return history, H.coin_age_fact(desk_dir, mint, **times)["coin"]


def build_d_blocks(candidate, facts, *, desk_dir, ours, pass_persist_at, decision_at, clock, ledger, unread=frozenset()):
    """Cohort D sheet blocks at the same cut as facts (Amendment 2: market_cut for tape blocks, sheet_as_of for external
    ones). Every input is explicit, no globals (Office Task 5, Zeta): desk_dir; ours = our fill actors; clock = ONE
    (offset_s, sampled_at) sample (None, None when unknown); ledger = desk_buyer_ledger rows, or the Exception its read
    raised. Local reads only; every reader returns a typed unknown instead of raising."""
    from sniper.services import memecoin_desk_buyer_facts as B
    from sniper.services import memecoin_desk_roster_history as R
    import sqlite3
    if pass_persist_at is None:
        raise ValueError("ps_pass_persist_unknown")
    sheet_as_of = facts["as_of"]
    pool_age = facts["ages"]["pool"]
    try:
        roster = R.open_ro(Path(desk_dir) / "roster_history.db")
    except sqlite3.Error:
        buyers = B._unknown("roster_unreadable")
    else:
        try:
            buyers = B.buyers_for(mint=candidate["mint"], pool=candidate["pool"], receipt_cut=facts["receipt_cut"],
                                  cut_slot=facts["cut_slot"], sol_usd=None,
                                  pool_created_at=pool_age["occurred_at"] if pool_age["state"] == "known" else None,
                                  desk_dir=desk_dir, ledger=ledger, roster_store=roster, ours=ours)
        finally:
            roster.close()
    offset, offset_at = clock
    history, age = history_blocks(desk_dir, candidate["mint"], candidate["pool"], sheet_as_of=sheet_as_of,
                                  decision_at=decision_at, pass_persist_at=pass_persist_at, offset=offset,
                                  market_cut=facts["receipt_cut"])
    out = {"buyers": buyers, "creator_history": history, "coin_age": age,
           "clock": {"offset_s": offset, "sampled_at": offset_at}}
    out.update(context_blocks(candidate, desk_dir=desk_dir, sheet_as_of=sheet_as_of, offset=offset, unread=unread))
    return out


def context_blocks(candidate, *, desk_dir, sheet_as_of, offset, unread=frozenset()):
    """Epsilon's G5 context readers (heat / list_claims) take corrected UTC. sheet_as_of is host time, so
    it is converted with the SAME offset sample as the history blocks; an unknown offset is each block's typed
    unknown:clock_unknown with no read at all, never the host time and never an implicit 0 (Zeta HOLD e76b9881d)."""
    from sniper.services import memecoin_desk_contract as K
    out = {}
    for name in K.D_CONTEXT_MODULES:
        mod = K._context_module(name)
        if name in unread and offset is not None:   # a caller with no read budget for this block: typed, never a read
            out[name] = mod._unknown("read_budget", sheet_as_of - offset)
            continue
        out[name] = (mod._unknown("clock_unknown", None) if offset is None
                     else getattr(mod, name + "_for")(candidate, desk_dir=desk_dir, as_of=sheet_as_of - offset))
    return out
