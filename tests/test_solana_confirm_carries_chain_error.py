"""A failed funded tx must keep the chain's own words.

2026-08-24, sunnydog06: a real entry reverted and everything we persisted was the word "failed".
The chain had said AnchorError ConstraintMintTokenProgram out of Meteora's BuyExactIn, and
`getSignatureStatuses` had handed us the err object — `rpc_confirm` returned a bare string and threw
it away. Explaining ONE revert then cost six manual getTransaction calls.
"""
import json

from sniper.execution import solana_real_seams as s


class _Http:
    """Minimal stand-in: statuses first, then the getTransaction the failure path makes."""

    def __init__(self, err, logs=None):
        self.err = err
        self.logs = logs or []
        self.calls = []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        payload = json.loads(body)
        self.calls.append(payload["method"])
        if payload["method"] == "getSignatureStatuses":
            return 200, {"result": {"value": [{"err": self.err,
                                               "confirmationStatus": "confirmed"}]}}, None
        return 200, {"result": {"meta": {"logMessages": self.logs}}}, None


INSTR_ERR = {"InstructionError": [0, {"Custom": 2022}]}
ANCHOR_LOGS = [
    "Program log: Instruction: BuyExactIn",
    "Program log: AnchorError occurred. Error Code: ConstraintMintTokenProgram. Error Number: 2022.",
    "Program LanMV9s failed: custom program error: 0x7e6",
]


def _confirm(http):
    return s.rpc_confirm("sig1", "http://rpc", http=http, max_attempts=1, poll_s=0)


def test_it_is_still_the_string_failed():
    """Every existing caller compares against these literals — that must not change."""
    out = _confirm(_Http(INSTR_ERR, ANCHOR_LOGS))
    assert out == "failed" and out in ("confirmed", "failed") and isinstance(out, str)


def test_it_names_the_anchor_error():
    out = _confirm(_Http(INSTR_ERR, ANCHOR_LOGS))
    assert out.program_error == "ConstraintMintTokenProgram"
    assert out.reason_code == "chain_error:ConstraintMintTokenProgram"


def test_it_keeps_the_raw_err_even_with_no_usable_logs():
    out = _confirm(_Http(INSTR_ERR, []))
    assert out.program_error is None
    assert out.err == INSTR_ERR
    assert out.reason_code.startswith("chain_error:")
    assert out.reason_code != "chain_error:"


def test_an_instruction_error_is_a_deterministic_rejection():
    """A program ran and refused this route — re-quoting the same venue will refuse again."""
    assert _confirm(_Http(INSTR_ERR, ANCHOR_LOGS)).deterministic is True


def test_a_non_instruction_error_is_NOT_deterministic():
    """Safe direction: the retry lane stops on `deterministic`, and stopping wrongly loses a good
    entry, so anything we cannot prove is a program verdict keeps retrying."""
    assert _confirm(_Http("BlockhashNotFound")).deterministic is False


def test_a_clean_confirm_never_pays_for_getTransaction():
    http = _Http(None)
    out = s.rpc_confirm("sig1", "http://rpc", http=http, max_attempts=1, poll_s=0)
    assert out == "confirmed"
    assert "getTransaction" not in http.calls




# --- non-Anchor programs say it in plain words, and we threw those away too -----------------------
#
# B5vZ3spp (JUST), 2026-09-08T14:34:22Z, funded Solana entry. Raydium's Router is NOT an Anchor
# program: it printed `Route error: exceeds desired slippage limit` and no `Error Code:` line, so
# `_anchor_error_name` returned None and all we persisted was
# `chain_error:{"InstructionError":[0,{"Custom":38}]}`. Explaining that revert cost a getTransaction
# against mainnet — the exact cost the 2026-08-24 fix existed to remove, still being paid because
# the extractor only understood one program's dialect. Four of these landed in three days.
#
# Custom 38 is 0x26, which means nothing without the log line. The log line means everything.
RAYDIUM_SLIPPAGE_ERR = {"InstructionError": [0, {"Custom": 38}]}
RAYDIUM_LOGS = [
    "Program routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS invoke [1]",
    "Program log: Instruction: TransferChecked",
    "Program log: 2249852 -> 253666740339995",
    "Program log: Route error: exceeds desired slippage limit",
    "Program routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS failed: custom program error: 0x26",
]


def test_it_names_a_non_anchor_program_error():
    """THE REGRESSION. The chain said why in English; we persisted a hex code."""
    out = _confirm(_Http(RAYDIUM_SLIPPAGE_ERR, RAYDIUM_LOGS))
    assert out == "failed"
    assert out.program_error == "exceeds desired slippage limit"
    assert out.reason_code == "chain_error:exceeds desired slippage limit"


def test_an_anchor_error_still_wins_over_a_plain_log_line():
    """Anchor's name is the more precise of the two; adding a fallback must not demote it."""
    logs = ["Program log: some error happened"] + ANCHOR_LOGS
    assert _confirm(_Http(INSTR_ERR, logs)).program_error == "ConstraintMintTokenProgram"


def test_it_takes_the_error_line_closest_to_the_failure():
    """A token's own chatter earlier in the tx is not the reason the route died."""
    logs = ["Program log: retrying after error", "Program log: Route error: pool is frozen"]
    assert _confirm(_Http(RAYDIUM_SLIPPAGE_ERR, logs)).program_error == "pool is frozen"


def test_it_falls_back_to_the_programs_own_failure_line():
    """No `Error Code:`, no `error` log — the last resort is what the runtime printed."""
    logs = ["Program log: Instruction: Swap",
            "Program Aabb failed: custom program error: 0x26"]
    out = _confirm(_Http(RAYDIUM_SLIPPAGE_ERR, logs))
    assert out.program_error == "custom program error: 0x26"


def test_ordinary_logs_still_yield_nothing():
    """Guard against a fallback loose enough to name every transaction."""
    assert s._program_error_name(["Program log: hello", "Program X success"]) is None
    assert s._program_error_name([]) is None


# --- asking at the wrong commitment threw the words away anyway (2026-09-08) ---------------------
#
# ELzdKrjF (JUST), 15:37:39Z, the first funded revert after the extractor shipped. The page and the
# persisted reason both read `chain_error:{"InstructionError":[0,{"Custom":38}]}` — the raw code.
# Replayed against that signature's own logs minutes later, `_program_error_name` returns
# "exceeds desired slippage limit". The extractor was never the problem.
#
# `rpc_confirm` returns terminal as soon as `confirmationStatus` is `confirmed`. `getTransaction`
# defaults to FINALIZED, which is ~32 slots (~13s) later. So at the instant we ask, the transaction
# is confirmed but not finalized, the RPC answers `result: null`, `meta` falls back to `{}`, and
# `program_error` stays None — with no error raised and nothing logged.
#
# This is no longer cosmetic. `_is_transient_entry_miss` now matches the word "slippage" in the
# reason to decide whether to re-quote, so a lost name is a lost entry, not just a vaguer page.

class _LateHttp:
    """getTransaction answers `result: null` until the tx has been asked for `after` times."""

    def __init__(self, err, logs, after=1):
        self.err, self.logs, self.after = err, logs, after
        self.gets = 0
        self.params = []

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        payload = json.loads(body)
        if payload["method"] == "getSignatureStatuses":
            return 200, {"result": {"value": [{"err": self.err,
                                               "confirmationStatus": "confirmed"}]}}, None
        self.gets += 1
        self.params.append(payload["params"][1])
        if self.gets <= self.after:
            return 200, {"result": None}, None
        return 200, {"result": {"meta": {"logMessages": self.logs}}}, None


def _confirm_late(http):
    return s.rpc_confirm("sig1", "http://rpc", http=http, max_attempts=1, poll_s=0,
                         sleep=lambda _s: None)


def test_it_asks_at_the_commitment_it_already_accepted():
    """THE REGRESSION. Terminal at `confirmed`, so the log read must ask at `confirmed` too."""
    http = _LateHttp(RAYDIUM_SLIPPAGE_ERR, RAYDIUM_LOGS, after=0)
    _confirm_late(http)
    assert http.params, "getTransaction was never called"
    assert http.params[0].get("commitment") == "confirmed", http.params[0]


def test_it_retries_a_not_yet_indexed_transaction():
    """A null result is 'ask again in a moment', not 'this transaction has no logs'."""
    http = _LateHttp(RAYDIUM_SLIPPAGE_ERR, RAYDIUM_LOGS, after=1)
    out = _confirm_late(http)
    assert out.program_error == "exceeds desired slippage limit"
    assert http.gets == 2, http.gets


def test_the_retry_is_bounded_and_still_returns_a_verdict():
    """It must never spin: the reservation release waits behind this."""
    http = _LateHttp(RAYDIUM_SLIPPAGE_ERR, RAYDIUM_LOGS, after=99)
    out = _confirm_late(http)
    assert out == "failed"
    assert out.program_error is None
    assert out.reason_code.startswith("chain_error:")
    assert http.gets <= 3, "unbounded getTransaction retry on the funded failure path"


def test_a_clean_confirm_still_never_pays_for_getTransaction():
    http = _LateHttp(None, RAYDIUM_LOGS, after=0)
    assert _confirm_late(http) == "confirmed"
    assert http.gets == 0
