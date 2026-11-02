"""Launchpad-gated Token-2022 mints are refused at token safety, before the quote.

2026-09-27: three funded Solana origins (ZrueWB1Y, 74cmFdL5, 2iCpCQRK) passed token safety as 'ok'
and died at the quote with Jupiter TOKEN_NOT_TRADABLE + Raydium ROUTE_NOT_FOUND, and the retry lane
re-quoted them because 'no quote' is transient. No route of ours can trade a coin while its DBC
transfer hook is set: Jupiter answers TOKEN_NOT_TRADABLE both ways, and no builder of ours builds
DBC Swap2WithTransferHook (Gamma 2026-10-08). The hooks differ: some need a platform co-sign, others
pass one-signer trades, so the refusal does not claim a co-sign.

The key is a NON-NULL hook program under the DBC pool authority. Census over every Solana attempt
mint 09-13..09-27 (1,873): 16 carry a transferHook extension, 0 of them funded; 11 have a null
program (a no-op hook: xStocks and others) and must not be refused; all 5 non-null programs sit
under the DBC pool authority. Frames are real (tests/fixtures/memecoin/launchpad_hook_mints.json).
"""
import copy
import json
from pathlib import Path

from solders.pubkey import Pubkey

from sniper.execution import solana_token_safety as ts

FRAMES = json.loads((Path(__file__).parent / "fixtures" / "memecoin"
                     / "launchpad_hook_mints.json").read_text())
# The true label (2026-10-08, Gamma; operator (a') via Alpha): Jupiter answers TOKEN_NOT_TRADABLE for
# these mints both ways and no builder of ours builds Swap2WithTransferHook. Not "requires the platform
# co-sign", which was wrong for some hooks. The "launchpad-gated: " prefix stays for the triage scripts.
LABEL = ("launchpad-gated: transfer hook eyesEswW not tradable on our routes (Meteora DBC)")
HOOKS = {"9eQyvzp3": "9eQyvzp3hfghBhA4VUECLZzyZswmFGcne1rfs9QN3CnL",
         "Bf7ecqFi": "Bf7ecqFieSoacTNbangY4trvnU7bVig6na1RMr6G84dk",
         "DMohCzuY": "DMohCzuYMQUsmYAiitgtYtSpsBtyEw9UWZha7EGqTv8M"}


def _verdict(value):
    return ts.token_safety_verdict(value, None, max_transfer_fee_bps=300,
                                   allow_live_fee_authority=True)


def _hook(value):
    info = value["data"]["parsed"]["info"]
    return next(e for e in info["extensions"] if e["extension"] == "transferHook")


def test_dbc_pool_authority_is_the_derived_pda():
    program = Pubkey.from_string("dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN")
    pda, _bump = Pubkey.find_program_address([b"pool_authority"], program)
    assert str(pda) == ts.DBC_POOL_AUTHORITY


def test_real_dbc_hooked_mint_is_refused_with_the_true_label():
    value = FRAMES["dbc_hook_ZrueWB1Y"]["value"]
    assert _hook(value)["state"]["authority"] == ts.DBC_POOL_AUTHORITY   # the frame is what we key on
    assert _verdict(value) == (False, LABEL)


def test_refusal_is_terminal_not_retried():
    from sniper.services.memecoin_real_runner import _RETRY_TRANSIENT, _is_transient_entry_miss
    res = {"status": "unsafe", "reason": LABEL}
    assert ("unsafe", LABEL) not in _RETRY_TRANSIENT
    assert not _is_transient_entry_miss(res)


def test_null_program_hook_is_never_launchpad_gated():
    value = FRAMES["null_program_hook_Xs78JED6"]["value"]
    assert _hook(value)["state"]["programId"] is None
    # It refuses today on its live freeze authority; that reason must stand unchanged.
    assert _verdict(value) == (False, "live freeze authority (account can be frozen)")


def test_dbc_frame_with_null_program_passes():
    value = copy.deepcopy(FRAMES["dbc_hook_ZrueWB1Y"]["value"])
    _hook(value)["state"]["programId"] = None
    assert _verdict(value) == (True, "ok")


def test_non_null_hook_under_another_authority_goes_to_the_quote():
    value = copy.deepcopy(FRAMES["dbc_hook_ZrueWB1Y"]["value"])
    _hook(value)["state"]["authority"] = "5aMNNLQJwAEeoemTEMkv5NVjqKwvvefRYCQ5Z67HFvEq"
    assert _verdict(value) == (True, "ok")


def test_classic_owner_never_reads_a_hook():
    value = copy.deepcopy(FRAMES["dbc_hook_ZrueWB1Y"]["value"])
    value["owner"] = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    assert _verdict(value) == (True, "ok")


def test_funded_token_2022_fee_coin_is_unchanged():
    value = FRAMES["funded_t22_65986"]["value"]
    assert _verdict(value) == (
        True, "ok transfer_fee_bps=100 fee_limit_bps=300 fee_authority_immutable=false")


# --- every DBC hook refuses: no route of ours can trade one (Gamma 2026-10-08) ---------------------

def _with_hook(program):
    value = copy.deepcopy(FRAMES["dbc_hook_ZrueWB1Y"]["value"])
    _hook(value)["state"]["programId"] = program
    return value


def test_every_dbc_hook_refuses_with_the_true_label_whatever_the_environment(monkeypatch):
    # The deleted knob, set as it would have been: it must change nothing.
    monkeypatch.setenv("MEMECOIN_SOLANA_OPEN_TRANSFER_HOOKS", ",".join(HOOKS.values()))
    for short, program in HOOKS.items():
        assert _verdict(_with_hook(program)) == (
            False, "launchpad-gated: transfer hook %s not tradable on our routes (Meteora DBC)" % short)


def test_the_allow_knob_is_gone():
    import inspect
    from sniper.settings import memecoin as cfg
    assert not hasattr(cfg, "solana_open_transfer_hooks")
    assert "open_transfer_hooks" not in inspect.signature(ts.token_safety_verdict).parameters
    assert "open_transfer_hooks" not in inspect.signature(ts.token_safety_ok).parameters
    assert "MEMECOIN_SOLANA_OPEN_TRANSFER_HOOKS" not in inspect.getsource(ts)


def test_triage_still_files_the_label_as_coin_policy():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "research" / "refusal_infra_triage.py"
    spec = importlib.util.spec_from_file_location("refusal_infra_triage", path)
    triage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(triage)
    assert triage.classify(LABEL)[:2] == ("POLICY", "coin_policy")
