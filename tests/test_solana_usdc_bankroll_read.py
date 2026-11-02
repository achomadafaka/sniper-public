"""The funded USDC bankroll read, the ATA first (2026-10-07, Alpha's yes).

The owner scan (getTokenAccountsByOwner) was the prefetch loop's slow tail: 1.4-3.0 s at max. While
the last scan (<= 5 min) found exactly one USDC account, the wallet's associated account, the read
is that account alone (getAccountInfo jsonParsed, owner and mint checked). Any miss is the scan,
never None or 0: no recent scan, USDC in another account, an RPC error, a closed, frozen or foreign
account, or other decimals. A failed scan still raises (usdc_fn reports unknown, not zero).
"""
import json

import pytest

from sniper.execution import solana_wallet as W
from sniper.services import memecoin_slot_lane as L

OWNER = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
OTHER = "Other1111111111111111111111111111111111111"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _token_account(amount, *, owner=OWNER, mint=W.USDC_MINT, state="initialized", decimals=6,
                   program=W._TOKEN_PROGRAM):
    return {"owner": program, "data": {"parsed": {"type": "account", "info": {
        "owner": owner, "mint": mint, "state": state,
        "tokenAmount": {"amount": str(amount), "decimals": decimals}}}}}


class Chain:
    """http(method, url, body) for the two reads, with knobs for every failure."""

    def __init__(self, reader, accounts):
        self.ata, self.accounts, self.calls = reader.ata, dict(accounts), []
        self.ata_answer = None                  # None: the ATA's entry in `accounts`
        self.scan_error = False

    def __call__(self, method, url, body=None):
        req = json.loads(body)
        self.calls.append(req["method"])
        if req["method"] == "getTokenAccountsByOwner":
            if self.scan_error:
                return 429, {"error": {"code": 429}}, None
            value = [{"pubkey": k, "account": _token_account(v)} for k, v in self.accounts.items()]
            return 200, {"result": {"value": value}}, None
        assert req["method"] == "getAccountInfo" and req["params"][0] == self.ata
        if self.ata_answer == "error":
            return 429, {"error": {"code": 429}}, None
        if self.ata_answer is not None:
            return 200, {"result": {"value": self.ata_answer}}, None
        amount = self.accounts.get(self.ata)
        return 200, {"result": {"value": _token_account(amount) if amount is not None else None}}, None


def _reader(accounts=None):
    clock = Clock()
    reader = W.UsdcBankrollRead(OWNER, "http://custody.invalid", clock=clock)
    chain = Chain(reader, accounts if accounts is not None else {})
    reader.http = chain
    return reader, chain, clock


def test_the_ata_alone_is_read_once_a_scan_found_only_it():
    reader, chain, _clock = _reader()
    chain.accounts = {reader.ata: 147_000_000}
    assert reader.read() == 147.0
    assert reader.read() == 147.0 and reader.read() == 147.0
    assert chain.calls == ["getTokenAccountsByOwner", "getAccountInfo", "getAccountInfo"]


def test_usdc_in_another_account_keeps_the_scan_and_its_total():
    reader, chain, _clock = _reader()
    chain.accounts = {reader.ata: 100_000_000, OTHER: 47_000_000}
    assert reader.read() == 147.0 and reader.read() == 147.0
    assert chain.calls == ["getTokenAccountsByOwner"] * 2


@pytest.mark.parametrize("answer", [
    "error", "closed",
    _token_account(1, owner=OTHER), _token_account(1, mint="So11111111111111111111111111111111111111112"),
    _token_account(1, state="frozen"), _token_account(1, decimals=9),
    _token_account(1, program="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"),
    {"owner": W._TOKEN_PROGRAM, "data": ["AAAA", "base64"]},
], ids=["rpc_error", "closed", "foreign_owner", "other_mint", "frozen", "decimals", "token2022", "unparsed"])
def test_any_fast_path_miss_is_the_scan_never_none_or_zero(answer):
    reader, chain, _clock = _reader()
    chain.accounts = {reader.ata: 147_000_000}
    assert reader.read() == 147.0
    if answer == "closed":
        chain.accounts = {OTHER: 52_000_000}     # the ATA closed; USDC moved elsewhere
    else:
        chain.ata_answer = answer
    assert reader.read() == (52.0 if answer == "closed" else 147.0)
    assert chain.calls[-2:] == ["getAccountInfo", "getTokenAccountsByOwner"]


def test_the_scan_verdict_expires_after_five_minutes():
    reader, chain, clock = _reader()
    chain.accounts = {reader.ata: 147_000_000}
    reader.read()
    clock.now += W.UsdcBankrollRead.SCAN_MAX_AGE_S
    reader.read()
    assert chain.calls == ["getTokenAccountsByOwner", "getTokenAccountsByOwner"]


def test_a_failed_scan_still_raises_so_the_bankroll_is_unknown():
    reader, chain, _clock = _reader()
    chain.scan_error = True
    with pytest.raises(RuntimeError):
        reader.read()


def test_no_usdc_account_reads_a_true_zero_only_from_the_scan():
    reader, chain, _clock = _reader({})
    assert reader.read() == 0.0
    assert reader.read() == 0.0
    assert chain.calls == ["getTokenAccountsByOwner"] * 2


def test_the_prefetch_report_shows_each_paths_milliseconds():
    reader, chain, _clock = _reader()
    chain.accounts = {reader.ata: 147_000_000}
    reader.read()
    reader.read()
    stats = reader.take_stats()
    assert (len(stats["ata"]), len(stats["scan"])) == (1, 1)
    assert reader.take_stats() == {"ata": [], "scan": []}
    line = L._usdc_read_text({"usdc_read_ms": {"ata": [31.0, 40.0, 35.0], "scan": [1800.0]}})
    assert line == " usdc_read ata n=3 p50=35 p90=40 scan n=1 p50=1800 p90=1800"
    assert L._usdc_read_text({}) == ""


# --- SOL-4 (Codex two-day review 2026-10-07, inherited): a malformed scan is unknown, not zero ----

class _Scan:
    """http for one getTokenAccountsByOwner answer, given as the raw `result` object."""

    def __init__(self, result):
        self.result = result

    def __call__(self, method, url, body=None):
        return 200, {"result": self.result}, None


@pytest.mark.parametrize("result", [
    {"value": None}, {"value": False}, {"value": ""}, {"value": {}}, {"context": {"slot": 1}},
], ids=["null", "false", "empty_string", "object", "missing"])
def test_a_scan_without_an_account_list_raises_never_zero(result):
    with pytest.raises(RuntimeError):
        W.token_accounts_checked(OWNER, W.USDC_MINT, "http://custody.invalid", http=_Scan(result))
    with pytest.raises(RuntimeError):
        W.token_balance_checked(OWNER, W.USDC_MINT, "http://custody.invalid", http=_Scan(result))


@pytest.mark.parametrize("record", [{"pubkey": OTHER}, {"pubkey": OTHER, "account": None},
                                    {"pubkey": OTHER, "account": {"data": {"parsed": {"info": {}}}}}],
                         ids=["no_account", "null_account", "no_amount"])
def test_a_malformed_record_raises(record):
    with pytest.raises((KeyError, TypeError, ValueError)):
        W.token_accounts_checked(OWNER, W.USDC_MINT, "http://custody.invalid",
                                 http=_Scan({"value": [record]}))


def test_an_empty_list_or_a_zero_balance_account_is_a_known_zero_and_totals_stay_exact():
    url = "http://custody.invalid"
    assert W.token_balance_checked(OWNER, W.USDC_MINT, url, http=_Scan({"value": []})) == 0.0
    zero = {"value": [{"pubkey": OTHER, "account": _token_account(0)}]}
    assert W.token_balance_checked(OWNER, W.USDC_MINT, url, http=_Scan(zero)) == 0.0
    two = {"value": [{"pubkey": OTHER, "account": _token_account(100_000_001)},
                     {"pubkey": OWNER, "account": _token_account(47_000_002)}]}
    assert W.token_accounts_checked(OWNER, W.USDC_MINT, url, http=_Scan(two)) == [
        (OTHER, 100_000_001, 6), (OWNER, 47_000_002, 6)]
    mixed = {"value": [{"pubkey": OTHER, "account": _token_account(1)},
                       {"pubkey": OWNER, "account": _token_account(1, decimals=9)}]}
    with pytest.raises(ValueError):
        W.token_accounts_checked(OWNER, W.USDC_MINT, url, http=_Scan(mixed))


def test_a_failed_fast_path_and_a_null_scan_leave_the_bankroll_unknown():
    reader, chain, _clock = _reader()
    chain.ata_answer = "error"

    def http(method, url, body=None):
        if json.loads(body)["method"] == "getTokenAccountsByOwner":
            return 200, {"result": {"value": None}}, None
        return chain(method, url, body)
    reader.http = http
    with pytest.raises(RuntimeError):
        reader.read()
