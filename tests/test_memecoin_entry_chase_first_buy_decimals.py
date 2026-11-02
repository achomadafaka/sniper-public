"""The chase gate must survive a FIRST buy, where the wallet cannot supply the mint's decimals.

`token_balance_snapshot` reads OUR token accounts. On a first buy we own none, so it returns
`decimals=None`. `bind_quote_metadata` then calls `quote_unit_price(buy_decimals=None)`, which does
`int(None)`, catches the TypeError, and yields `_sniper_quote_price=None`. The chase gate reads
that value:

    if chase_cap and chase_cap > 0 and pending_source_price and quote_px:

so `quote_px is None` skipped the whole check. It fired only for a RE-entry, where dust from an
earlier fill made the decimals readable.

Live 2026-08-19, funded Solana, since 08-14: 131 entry attempts reached that code region, 4
evaluated the gate, and `terminal_status='skipped_chase'` never appeared once. Fills landed at
+76.5% (pos 40210), +519.3% (pos 37571) and +2895.6% (pos 37514) above the source price while
MEMECOIN_REAL_CHASE_MAX_PCT was 30. All 4 that evaluated it were re-entries of a held mint.

The seam now reads the mint with getTokenSupply when the wallet has no account for it. Note the
old fixtures hid this: they returned a zero balance that still carried decimals=6, which no live
first buy ever does.
"""
import sqlite3

import pytest

from sniper.execution import solana_real_seams as S
from sniper.execution import solana_swap_executor as ex
from sniper.execution.solana_wallet import TokenBalanceSnapshot
from sniper.persistence import db_memecoin as m


MINT = "7Rk2bQ1Xovkmeeaw4kJ9WaTxdtyScjvStwv7hjgWpump"


class _Keypair:
    def pubkey(self):
        return "OurWallet11111111111111111111111111111111111"


def _http_for(*, owner_accounts, supply_decimals):
    """Minimal Solana JSON-RPC double. `owner_accounts=[]` is a wallet that never held the mint."""
    seen = []

    def http(_method, _url, body=None, **_kw):
        import json
        method = json.loads(body)["method"]
        seen.append(method)
        if method == "getTokenAccountsByOwner":
            return (200, {"result": {"value": owner_accounts}}, None)
        if method == "getTokenSupply":
            if supply_decimals is None:
                return (500, {}, None)
            return (200, {"result": {"value": {"decimals": supply_decimals}}}, None)
        return (200, {"result": {}}, None)

    http.seen = seen
    return http


def _snapshot(**kw):
    return S.entry_token_snapshot(
        _Keypair().pubkey(), MINT, "http://rpc.invalid", http=_http_for(**kw))


def _held_account(decimals, amount="500000000"):
    return [{"pubkey": "account", "account": {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
             "data": {"parsed": {"type": "account", "info": {"owner": _Keypair().pubkey(),
             "mint": MINT, "tokenAmount": {
        "amount": amount, "decimals": decimals}}}}}}]


# --- the seam ----------------------------------------------------------------------------------

def test_a_first_buy_still_resolves_the_mint_decimals():
    """The wallet owns no account for the mint. The decimals come from the mint itself."""
    snapshot = _snapshot(owner_accounts=[], supply_decimals=6)
    assert snapshot.atomic == 0
    assert snapshot.decimals == 6


def test_a_held_mint_keeps_the_wallets_own_decimals():
    """Re-entry: the wallet's account answers, and its decimals win over the mint read. Since
    2026-09-27 the mint read runs at the same time as the wallet read, so a holder pays one
    discarded getTokenSupply that is off the critical path (it used to be skipped to save a serial
    round trip on the sign path, which no longer exists)."""
    http = _http_for(owner_accounts=_held_account(9), supply_decimals=6)
    snapshot = S.entry_token_snapshot(
        _Keypair().pubkey(), MINT, "http://rpc.invalid", http=http)
    assert snapshot.decimals == 9 and snapshot.atomic == 500_000_000


def test_the_wallet_and_mint_reads_run_at_the_same_time():
    """A first buy used to pay getTokenAccountsByOwner THEN getTokenSupply (token read p50 179 ms).
    Each read here waits for the other at a barrier; a serial snapshot breaks it and reads None."""
    import threading
    barrier = threading.Barrier(2, timeout=2.0)
    inner = _http_for(owner_accounts=[], supply_decimals=6)

    def http(method, url, body=None, **kw):
        barrier.wait()
        return inner(method, url, body=body, **kw)

    snapshot = S.entry_token_snapshot(_Keypair().pubkey(), MINT, "http://rpc.invalid", http=http)
    assert (snapshot.atomic, snapshot.decimals) == (0, 6)


def test_an_unreadable_mint_keeps_the_old_behaviour():
    """A failed supply read is still `None`; it must not raise on the funded entry path."""
    assert _snapshot(owner_accounts=[], supply_decimals=None).decimals is None


def test_the_balance_is_never_invented():
    """Only the decimals come from the mint. The atomic balance stays the wallet's own."""
    assert _snapshot(owner_accounts=[], supply_decimals=6).atomic == 0


def test_the_executor_seam_uses_it():
    """Pin the wiring, not a mirrored predicate: the built entry path must see decimals=6."""
    import inspect
    src = inspect.getsource(S.build_real_solana_executor)
    assert "entry_token_snapshot(" in src
    built = S.build_real_solana_executor(
        keypair=_Keypair(), rpc_url="http://rpc.invalid",
        http=_http_for(owner_accounts=[], supply_decimals=6),
        token_fn=lambda mint: TokenBalanceSnapshot(123, 4))
    assert built["pubkey"] == _Keypair().pubkey()      # injected token_fn still wins downstream


# --- the gate it feeds -------------------------------------------------------------------------

class _Deps:
    """A $0.02 route quote. `decimals` is what the PRE-buy snapshot reports.

    The POST-buy read always knows the decimals, because by then we own the token account. That
    asymmetry is the whole live failure: the gate runs before the buy, the accounting runs after.
    """

    def __init__(self, decimals):
        self.decimals, self.calls = decimals, []

    def quote_fn(self, mint, usd):
        return {"inAmount": "10000000", "outAmount": "500000000",
                "otherAmountThreshold": "470000000", "priceImpactPct": 0.01}

    def reverse_quote_fn(self, mint, amount):
        return {"inAmount": str(amount), "outAmount": "9800000",
                "otherAmountThreshold": "9800000"}

    def usdc_fn(self):
        return 90.0 if "sent" in self.calls else 100.0

    def token_fn(self, mint):
        if "sent" in self.calls:
            return TokenBalanceSnapshot(500_000_000, 6)
        return TokenBalanceSnapshot(0, self.decimals)

    def sign_fn(self, quote):
        self.calls.append("sign")
        return (b"signedtx", "SIG123")

    def send_fn(self, signed):
        self.calls.extend(("send", "sent"))
        return True

    def confirm_fn(self, sig):
        return "confirmed"


def _run(decimals, source_price):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint=MINT, source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    c.execute("UPDATE memecoin_pending_entries SET source_price=? WHERE id=?", (source_price, pid))
    c.commit()
    d = _Deps(decimals)
    result = ex.execute_entry(
        c, pending_id=pid, mint=MINT, usd_amount=10.0, strategy="alpha_b", source_user_id="u1",
        now_iso="2026-08-19T00:00:00Z", quote_fn=d.quote_fn,
        reverse_quote_fn=d.reverse_quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_fn=d.token_fn,
        impact_cap_pct=12.0, chase_max_pct=10.0)
    return result, d


def test_known_decimals_refuse_the_chase():
    """Control: quote $0.02 against a $0.01 source is +100%, far past the 10% cap."""
    result, d = _run(decimals=6, source_price=0.01)
    assert result["status"] == "skipped_chase"
    assert "sign" not in d.calls


def test_unknown_decimals_are_exactly_what_broke_the_gate():
    """Pin the failure mode. Without the seam fix this same +100% chase signs and fills."""
    result, d = _run(decimals=None, source_price=0.01)
    assert result["status"] == "filled" and "sign" in d.calls
    assert ex.quote_fields(  # the unpriceable quote is the reason, and it is worth naming
        ex.bind_quote_metadata({"inAmount": "10000000", "outAmount": "500000000"},
                               network_id="solana", mint=MINT, quote_observed_at="t",
                               sell_decimals=6, buy_decimals=None))["quote_price"] is None


@pytest.mark.parametrize("source_price,expected", [
    (0.0100, "skipped_chase"),    # quote 0.02 -> +100%
    (0.0180, "skipped_chase"),    # +11.1%, just past a 10% cap
    (0.0185, "filled"),           # +8.1%
    (0.0250, "filled"),           # -20%, buying UNDER the trader is never refused
])
def test_the_cap_is_applied_at_the_quote(source_price, expected):
    """The boundary itself, once the decimals resolve."""
    assert _run(decimals=6, source_price=source_price)[0]["status"] == expected


# --- the gate must never be silently absent again -----------------------------------------------

class _Clock:
    """Monotonic source the real trace requires; it refuses a clock that moves backwards."""

    def __init__(self):
        self.value = 100.0

    def perf_counter(self):
        self.value += 0.01
        return self.value


def _run_traced(decimals, source_price, chase_max_pct=10.0):
    """Drive the REAL EntryAttemptTrace, not a double — the substage contract is what we assert."""
    from types import SimpleNamespace
    from sniper.services.memecoin_entry_trace import EntryAttemptTrace

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.reserve_pending(c, mint=MINT, source_user_id="u1", strategy="alpha_b",
                            notional=10.0, reserved_at="t")
    if source_price is not None:
        c.execute("UPDATE memecoin_pending_entries SET source_price=? WHERE id=?",
                  (source_price, pid))
        c.commit()
    ev = SimpleNamespace(
        fomo_event_id="event-chase", source_user_id="u1", source_handle="alice",
        signal_source="follow_activity", network_id="solana", mint=MINT, ticker="M",
        side="buy", their_price=source_price, their_market_cap=1_000_000.0,
        created_at="2026-08-19T00:00:00+00:00",
        received_at_normalized="2026-08-19T00:00:00.900000+00:00",
        clock_domain="observed_real_utc")
    trace = EntryAttemptTrace.start(c, ev, "2026-08-19T00:00:01+00:00", _Clock())
    d = _Deps(decimals)
    ex.execute_entry(
        c, pending_id=pid, mint=MINT, usd_amount=10.0, strategy="alpha_b", source_user_id="u1",
        now_iso="2026-08-19T00:00:00Z", quote_fn=d.quote_fn,
        reverse_quote_fn=d.reverse_quote_fn, sign_fn=d.sign_fn, send_fn=d.send_fn,
        confirm_fn=d.confirm_fn, usdc_fn=d.usdc_fn, token_fn=d.token_fn,
        impact_cap_pct=12.0, chase_max_pct=chase_max_pct, trace=trace)
    trace.finish("filled", "filled")
    row = c.execute("SELECT trace_json FROM memecoin_entry_attempts "
                    "WHERE attempt_id=?", (trace.attempt_id,)).fetchone()
    import json
    return {e["name"]: e for e in json.loads(row[0])["substage_events"]}


def test_an_evaluated_gate_says_so():
    assert "chase_quote_confirmation" in _run_traced(decimals=6, source_price=0.01)


@pytest.mark.parametrize("decimals,source_price,cap,reason", [
    (None, 0.01, 10.0, "unpriceable_quote"),   # the live defect: getTokenSupply also failed
    (6, None, 10.0, "no_source_price"),        # a manual operator entry
    (6, 0.01, 0.0, "cap_off"),                 # the guard is deliberately disabled
])
def test_a_gate_that_stands_down_names_its_reason(decimals, source_price, cap, reason):
    """An absent substage was indistinguishable from one that ran and passed. That ambiguity is
    exactly how the gate stayed dead for weeks without a single alert."""
    subs = _run_traced(decimals=decimals, source_price=source_price, chase_max_pct=cap)
    assert "chase_quote_confirmation" not in subs
    assert subs["chase_quote_skipped"]["reason"] == reason


def test_the_skip_carries_the_decimals_that_caused_it():
    """The field that lets an operator count the residual fail-open in the ledger."""
    assert _run_traced(decimals=None, source_price=0.01)["chase_quote_skipped"]["buy_decimals"] is None
