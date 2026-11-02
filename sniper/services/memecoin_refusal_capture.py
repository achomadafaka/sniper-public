"""Forward exact-route capture for mints a live strategy gate refused.

Design: docs/superpowers/specs/2026-08-26-memecoin-refused-mint-forward-capture-design.md

WHY IT EXISTS. No forward exact-route series is persisted anywhere in this system, for any coin,
held or refused. `memecoin_position_marks` looks route-shaped and is not (`executable_exit_value`
equals `mid` on all 3,246,299 rows). So a gate's refusal counterfactual -- "what would that coin
have done" -- is not computable today at any confidence. This produces the missing instrument for
the refused side. The held side is owned elsewhere, by the funded-path quote persistence.

TWO DEVIATIONS FROM THE APPROVED DESIGN, both stated rather than silent:

1. ENQUEUE READS THE DURABLE REFUSAL RECORD, it is not an in-process hook on the funded entry
   path. The design said the refusal hook emits an event and each subscriber decides. Every
   refusal is ALREADY persisted to `memecoin_entry_attempts` with its mint, origin and exact
   reason, so a read-only poll gets the same event stream and adds ZERO code to the funded entry
   path -- no latency, and no bug of mine can reach an entry decision. That makes the design's
   independence rule structural instead of conventional. Cost: a capture starts up to one poll
   interval after the refusal, so its first observation lands at `age_s` of a few seconds rather
   than zero. That is visible in the data as the minimum `age_s` and is never backfilled.

2. IT YIELDS TO THE FUNDED RUNNER ON JUPITER, which the design did not require because the design
   assumed the RPC pool was the binding dependency. MEASURED 2026-08-26, and it is not:

     one observation  = 1 Jupiter GET + 0 RPC
     one MINT (ever)  = 1 RPC getTokenSupply for decimals, which are immutable and cached
     at the 162-observation horizon that is 162 Jupiter : 1 RPC

   Jupiter takes NO API KEY and rate-limits BY IP (`solana_real_seams.py`, live-verified
   2026-08-14 when lite-api answered 429 to every request from this host for over an hour, cost
   live entries, and left position 37108 rails-blind for 700 sweeps at -67%). The funded runner
   quotes from the same two hosts on the same IP, and its host cooldown map is PROCESS-LOCAL, so a
   separate capture process cannot see the funded runner's benching and would keep hammering a
   host funded has already given up on.

   The capture's own RPC key IS disjoint and asserted at startup -- but a disjoint key protects
   1/162 of the load. There is no key to make disjoint on the side that matters. So the only
   available protection is behavioural, and it is this: the capture is research, nothing waits on
   it, and it may lose observations at no cost. It therefore YIELDS. Any Jupiter 429 it sees shuts
   it for `MEMECOIN_CAPTURE_JUPITER_YIELD_S`, and it treats a shortfall as censoring to report,
   never as a reason to retry harder.

NO ADMISSION AUTHORITY. This never enters, sizes, re-asks, or grades. It writes rows.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sniper.persistence.db_memecoin_captures import DIP_SUFFIX   # the store routes `#dip` rows

logger = logging.getLogger(__name__)

_EXIT_SLIPPAGE_PCT = 5.0     # quote-only; never submitted, so this bounds the route search
PROVIDER = "jupiter"

# --- Gate identity -------------------------------------------------------------------------------
#
# EXACT identity, never a prefix. The bare strings `under_floor` and `over_ceiling` are the RETIRED
# mcap band and carry 349 refusals since 2026-08-19; the LIVE volume gates are different gates that
# merely share a word. A prefix match would pool a retired gate with a live one, which is the exact
# defect the step-2 gate audit had to correct.
#
# The turnover family names its gate inside parentheses and the suffix there is NOT stable -- it
# gained ", revival guard" when the revival watch shipped -- so match the gate TOKEN, never the
# whole parenthesis.
_MCAP_CAPTURE_MAX_MULT = 2.0


def _mcap_near_ceiling(reason):
    """True for a rendered mcap-ceiling refusal within `_MCAP_CAPTURE_MAX_MULT` of the ceiling.

    Fail-closed on anything it cannot parse: an unreadable reason is not silently treated as near
    the line, because that would spend quota on the $5M coins this bound exists to exclude.
    """
    text = str(reason or "")
    if not text.startswith("mcap $") or " over ceiling $" not in text:
        return False
    try:
        head, tail = text.split(" over ceiling $", 1)
        mcap = float(head[len("mcap $"):].replace(",", ""))
        ceiling = float(tail.split(" ")[0].replace(",", "").rstrip(")"))
    except (ValueError, IndexError):
        return False
    return ceiling > 0 and 0 < mcap <= _MCAP_CAPTURE_MAX_MULT * ceiling


def _token(gate):
    return lambda s: ("(" + gate) in s and (s.endswith(gate + ")") or (gate + ",") in s)


GATES = (
    ("m5_txns_under_floor", _token("m5_txns_under_floor")),
    ("m5_txns_unreadable", _token("m5_txns_unreadable")),
    ("m5_turnover_under_floor", _token("m5_turnover_under_floor")),
    ("m5_turnover_under_big_floor", _token("m5_turnover_under_big_floor")),
    ("m5_turnover_over_ceiling", _token("m5_turnover_over_ceiling")),
    # MICRO-BAND m5 DOLLAR FLOOR — added 2026-09-18 with the leg itself. It is armed on evidence
    # from 24 refused-equivalent closes over 10 days, which is thin, so the coins it refuses must
    # be observable from the start rather than argued from a later backtest.
    ("m5_volume_under_abs_floor", _token("m5_volume_under_abs_floor")),
    ("turnover_under_floor", _token("turnover_under_floor")),
    # H1 ABSOLUTE FLOOR — added 2026-09-30 (Gamma root cause, Alpha GO). 91 refusals in 14 days were
    # uncaptured because this module still believed the gate "publishes no gate token". Every live
    # reason now carries one: "h1 organic volume $N under floor $N (under_abs_floor)", with an
    # optional ", 2.00% of mcap $N", ", pro-rated to pair age Ns" or ", revival guard" suffix.
    # `_token` needs "(under_abs_floor", so the m5 twin "(m5_volume_under_abs_floor" never matches.
    ("under_abs_floor", _token("under_abs_floor")),
    # MCAP CEILING — added 2026-09-09. It is the largest refusal family on the funded lane (119 of
    # 183 armed-wallet refusals in one day, 38 coins) and it had NO capture at all, so "is $250k the
    # right ceiling" could only ever be argued from backtests. Same hole as `roundtrip_cap`.
    #
    # BOUNDED TO THE DECIDABLE REGION, and that is the whole design. Of 182 distinct mints refused
    # in 3 days, 40% were above 20x the ceiling ($5M+) and 34% above 4x. Observing a $15M coin
    # answers nothing — we would not trade it at any plausible ceiling — while blanket capture would
    # cost ~1,280 extra Jupiter quotes/day against ~3,065 funded quotes/day, a 42% rise on a key
    # already taking 429s. Only coins within `_MCAP_CAPTURE_MAX_MULT` of the ceiling are observed:
    # 7.7% of the population, ~5 mints/day, roughly 160 quotes/day (+5%).
    #
    # The ratio is parsed from the REASON ITSELF, which since 40f37bfe carries both numbers
    # ("mcap $343,423 over ceiling $250,000 (over_ceiling)"). No new input, no plumbing. It matches
    # only the RENDERED form, so the bare `over_ceiling` strings from the retired mcap band above
    # stay unmatched — the exact pooling this module's header warns about.
    ("mcap_over_ceiling", lambda s: _mcap_near_ceiling(s)),
    # MCAP FLOOR — added 2026-09-18 with the per-wallet floor. smol_intern is refused under $250k on
    # 4663 because its funded record splits by band (-$57 over 8 closes under, +$41 over 4 above),
    # and the operator wants to re-read that split from the wallet's OWN refused coins rather than
    # from a global backtest. Matches the RENDERED form only, exactly like the ceiling above, so the
    # retired bare `under_floor` strings stay unmatched.
    ("mcap_under_floor", lambda s: (str(s or "").startswith("mcap $")
                                    and " under floor $" in str(s or ""))),
    # ROUND-TRIP CAP — added 2026-09-08 after MOAN was refused ("round trip 12.21% over 10.0% cap
    # (sell back $153.63 of $175.00)") and there was nothing to grade the refusal with. This gate
    # has decided 22 funded entries across 19 mints since 2026-08-13 and NONE of them were ever
    # observed afterwards, so we have no evidence whether refusing on round-trip cost helps or
    # hurts. It is also the gate whose rule changed the same day (flat 10% -> 0.4 x TP), which is
    # exactly the wrong combination: a rule that can move and no feedback on where it should sit.
    #
    # A LAMBDA, not `_token`: that helper matches the parenthesised family tag other gates append,
    # and this reason ends with "(sell back $X of $Y)" instead. Adding the tag at the source would
    # have been tidier and would also have changed a string the retry matcher and Beta's refusal
    # analysis both already key on, so the matcher bends to the string rather than the reverse.
    #
    # Cost measured before adding, not assumed: 14 distinct mints in 7 days and 4 in the last 24h,
    # so 64-128 extra Jupiter quotes a day against ~3,065 funded quotes/day, a 2-4% rise with the
    # current loss rate at 1.4%. The EVM twin (`round_trip_over_budget`) has never fired on a
    # funded attempt, so it gets no family until it does — an unused matcher is untested code.
    ("roundtrip_cap", lambda s: s.startswith("round trip ")
     and " over " in s and " cap (sell back " in s),
    # PRICE-IMPACT CAP — added 2026-09-13 after kaRAT (ref dc82c96a): a GLDx-quoted Pump.fun curve
    # refused at 90.22% impact 40 s before it migrated, then ran +85% on its new pool. 9 Solana
    # impact refusals in a week and none observed afterwards, so whether "wait for the migration"
    # pays can only be argued, never measured. The capture quotes the refused clip, so the series
    # shows the route price through and after a migration.
    ("impact_cap", lambda s: s.startswith("price impact ") and "% over " in s and s.endswith(" cap")),
    ("chase_first_seen", lambda s: s.startswith("coin ran ") and "first-seen price" in s),
    ("chase_past_entry", lambda s: s.startswith("coin already ran ") and "past their entry" in s),
    # NOT `execution_premium` (controls review, 2026-09-08). This string is built by the
    # CHASE-on-quote gate in solana_swap_executor.py:550 / evm_swap_executor.py:569 from
    # `chase_cap`, and that gate returns `status: "skipped_chase"`. It measures the quote against
    # the COPIED TRADER'S price. The execution-premium gate measures it against the CHART price and
    # refuses with "route quote is Nx the chart mid", already filed below as `route_over_mid`.
    # Filing this one under `execution_premium` made every chase-quote refusal count as an
    # execution-premium refusal, and any report grouping by family wrong in both directions.
    ("chase_quote", lambda s: s.startswith("exact quote is ") and "above source price" in s),
    ("evm_chase_quote", lambda s: s.startswith("our quote is ") and "above source price" in s),
    ("route_over_mid", lambda s: s.startswith("route quote is ") and "the chart mid" in s),
    ("route_over_source", lambda s: s.startswith("route quote is ") and "the source price" in s),
    ("winner_reentry_price", lambda s: s.startswith("winner re-entry above our exit")),
    ("winner_reentry_daily_cap", lambda s: s.startswith("winner re-entry daily cap")),
    # The size ladder (2026-09-28): "exhausted" and "rank changed since classification".
    ("winner_reentry_ladder", lambda s: s.startswith("winner re-entry ladder")),
    ("losing_reentry", lambda s: s.startswith("losing re-entry:")),
    ("reentry_cooldown", lambda s: s.startswith("re-entry cooldown:")),
    # ADDED 2026-08-28 for per-wallet gate exemptions. An exemption names a gate FAMILY, and the
    # rule is one taxonomy across exemptions, refusal reasons and capture grading — so a gate that
    # can be exempted must be nameable here. These three were the only exemptible families with no
    # name. Side effect worth knowing: the capture lane can now grade them, and the family COUNT
    # moved for anything reading it.
    #
    # ANCHORED, and the anchor is load-bearing. Live rows carry BOTH
    #   "coin band is not reservable: 'micro'"                        <- the selection refusal
    #   "invalid_funded_contract: coin band is not reservable: 'micro'" <- a CONTRACT failure that
    # merely embeds the band text. A substring match would file contract failures under a selection
    # family and make exempted-entry outcomes unattributable, silently. startswith is the fix and
    # there is a test pinning that the prefixed variant does not match.
    ("coin_age_ceiling", lambda s: s.startswith("coin is ") and "no-movement class" in s),
    ("coin_band_unreservable", lambda s: s.startswith("coin band is not reservable")),
    # The ALLOW paths also begin "sell share ", so the refusal is identified by its own shape
    # ("... of m5 txns over N trades (cap M%)") rather than by the prefix alone.
    ("sell_share", lambda s: (s.startswith("sell share ") and " over " in s and "(cap" in s)),
    # Source-size refusals use either an mcap-relative threshold or the unknown-mcap fallback.
    ("source_buy_floor",
     lambda s: s.startswith("source buy ") and (
         " under mcap floor " in s or " under unknown-mcap floor " in s)),
    # ABSOLUTE SOURCE-BUY FLOOR — added 2026-09-30 (Gamma root cause, Alpha GO). The reason
    # "source buy $N under absolute floor $N" (memecoin_real_runner.source_buy_band_reason) had no
    # matcher, so 790 refusals in 14 days went uncaptured. A separate family from the mcap-relative
    # floor, so the two thresholds grade separately. Its twin "over absolute ceiling" has not
    # fired in 14 days and gets no family until it does (an unused matcher is untested code).
    ("source_buy_abs_floor", lambda s: (s.startswith("source buy $")
                                        and " under absolute floor $" in s)),
    # DRAWDOWN PAUSE (2026-09-17, operator). The pause is a BOOK rule, not a coin judgement, so by
    # the rule above it would never be captured. The operator asked what the pause costs, and the
    # only answer available on its first two windows was a DexScreener snapshot hours later: four
    # coins in the 16:07 window (SELFIE -41%, PC -94%, HARDER -61%, NODE gone) had no stored price
    # at all. A capture makes the next reading a real replay of the refused coins.
    ("drawdown_pause", lambda s: s.startswith("drawdown pause:")),
    # KNOBS WE WANT TO GRADE — added 2026-09-30 (Alpha GO, measurement-only under the existing
    # capacity cap). Each is a live refusal with no family until now (counts are 2026-09-16..29):
    #   coin_too_young  the young-coin floor: "coin_too_young: 60s old, floor 120s" and
    #                   "coin_too_young: mint 24s old (pair age unknown), floor 120s" (34)
    #   daily_loss_limit a BOOK rule, like drawdown_pause: "daily loss limit reached (latched)" (30)
    #   entry_stale     the signal max age: "entry older than 60s" (17)
    #   post_peak_confirmed  the exact-quote post-peak confirmation, status 'aborted' (8)
    #   sell_sim_min_out_repeated  the fresh funded sell simulation still hit router min-out,
    #                   status 'safety_failed' (11)
    #   m5_volume_unreadable  "... (m5_volume_unreadable)" (4)
    # Anchored on the reason's own start (or its token), so a contract failure that embeds the same
    # text ("invalid_funded_contract: ...") never files under a selection family.
    ("coin_too_young", lambda s: s.startswith("coin_too_young:")),
    ("daily_loss_limit", lambda s: s.startswith("daily loss limit reached")),
    ("entry_stale", lambda s: s.startswith("entry older than ") and s.endswith("s")),
    ("post_peak_confirmed", lambda s: s == "post_peak_confirmed"
     or s.startswith("post_peak_confirmed:") or s.startswith("post_peak_confirmed ")),
    ("sell_sim_min_out_repeated", lambda s: s.startswith("sell_sim_min_out_repeated:")),
    ("m5_volume_unreadable", _token("m5_volume_unreadable")),
    # SLIPPAGE FAILURES (2026-09-30, Alpha). Not refusals: the buy landed and reverted on its
    # minimum-out, status 'failed'. The operator weighs a per-wallet slippage exception for
    # bot-heavy wallets, and this is the coin's forward series for it. Exact strings only. A bare
    # "custom program error: 0x1771" is NOT here: 6001 means slippage only in Jupiter v6, which
    # `_program_error_name` already renames, and a bare code names no program.
    ("slippage_fail", lambda s: s in SLIPPAGE_FAIL_REASONS),
)

# Deliberately NOT captured, so a later reader does not read absence as an oversight:
#   bare `under_floor` / `over_ceiling`  -- retired mcap band, see above
#   `... under floor $N (revival guard)` with NO gate token -- an older h1 absolute-floor spelling.
#       Not seen in the 14 days to 2026-09-29; live rows carry `(under_abs_floor, ...)`, matched
#       above. A reason without the token stays unmatched rather than guessed.
#   safety verdicts (2026-09-30, Alpha): "exit route pays X at a Pump.fun hop ... (unsellable
#       trap ...)" and "live mint authority (infinite-mint rug risk)". A verdict, not a knob.
#   plumbing and roster: not_approved, unroutable_chain, disarmed, no quote, insufficient *,
#       one position per coin, source buy under floor, no planned-quantity sell route,
#       follow source event predates, invalid_funded_contract, failed, unknown, broadcast_rejected,
#       chain_error. None of these say anything about the coin.


# NAMING IS NOT CAPTURING (2026-08-28). `GATES` is the one vocabulary — exemptions, refusal
# reasons and capture grading all use these names, so a gate that can be exempted must be nameable
# here. But this lane exists to answer "what would that COIN have done", and a refusal that says
# nothing about the coin produces no counterfactual worth a quote.
#
# `source_buy_floor` is exactly that split: it judges the COPIED TRADER's buy size, not the coin,
# and it was a documented never-capture before it needed a name. Adding it to GATES so the operator
# can exempt it would silently have started spending capture quotes on it — so the lane filters on
# CAPTURED_FAMILIES instead of on GATES membership, and the two decisions stay separable.
# REVERSED 2026-09-17 (operator): the refusal slot SHOWS source_buy_floor refusals with an ENTER
# button, and without a capture that card had no price and no move (POKEMON). A shown card is priced.
# The capture spends the MEASUREMENT Jupiter key, never the funded one.
NON_CAPTURED_FAMILIES = frozenset()
CAPTURED_FAMILIES = frozenset(g for g, _ in GATES) - NON_CAPTURED_FAMILIES
# Every string counted 2026-09-16..30 (tests/test_memecoin_slippage_fail_capture.py has the counts).
SLIPPAGE_FAIL_REASONS = frozenset({
    "chain_error:BuySlippageBelowMinTokensOut",                 # pump curve 6042 (slot lane)
    "chain_error:BuySlippageBelowMinBaseAmountOut",             # PumpSwap 6040 (slot lane)
    "chain_error:slippage tolerance exceeded (Jupiter 0x1771)",
    "chain_error:exceeds desired slippage limit",               # Raydium router's own log line
    # Raydium CPMM 6005 on the slot lane (2026-10-07, Alpha GO): 6 landed, reverted BUYs 10-02..10-06,
    # every receipt `Program CPMMoo8L... failed: custom program error: 0x1775`, Error Code ExceededSlippage.
    "chain_error:ExceededSlippage",
})


def slippage_available_over_min(trace_json):
    """Tokens the spend would have bought when it landed over the signed minimum, read from the
    slot lane's `slot_pump_slippage` substage (the program's own Left/Right log pair). None when the
    error does not carry the pair, which is every non-pump failure."""
    import json
    try:
        events = json.loads(trace_json or "null").get("substage_events") or ()
        ev = next(e for e in events if isinstance(e, dict) and e.get("name") == "slot_pump_slippage")
        available, minimum = ev["available_tokens_atomic"], ev["min_tokens_atomic"]
    except (AttributeError, KeyError, StopIteration, TypeError, ValueError):
        return None
    if any(type(v) is not int for v in (available, minimum)) or minimum <= 0:
        return None
    return available / minimum


# LOW-PRIORITY FAMILIES (2026-09-30, Gamma LOW on 91b9a3c7a, Alpha GO). The families added on
# 2026-09-30 are many and mostly low-signal: replayed over 09-13..09-29 at 6 slots per lane, they
# would have displaced 75 Solana captures of the older families, including the roundtrip, chase and
# impact captures that feed pre-registered reads. So they take a slot only while at least
# LOW_PRIORITY_MIN_FREE slots are free, and are otherwise refused as `not_captured_capacity_reserve`
# (counted apart from `not_captured_capacity`, so the reserve's cost is measurable). The cap itself
# stays where the loop sets it.
#
# THE MCAP SOURCE-BUY FLOOR AND THE RE-ENTRY COOLDOWN JOIN THEM (2026-10-02, Gamma's INSTRUMENT GAP,
# Alpha GO). On 10-02 they and the absolute floor took every Solana slot (refused for capacity
# that day: source_buy_floor 36, source_buy_abs_floor 26, reentry_cooldown 23), so impact_cap got
# no capture after 10-01 18:11Z. Neither says anything about the coin's price: one judges the
# copied trader's buy size, the other our own timing.
LOW_PRIORITY_FAMILIES = frozenset({
    "source_buy_abs_floor", "under_abs_floor", "coin_too_young", "daily_loss_limit",
    "entry_stale", "post_peak_confirmed", "sell_sim_min_out_repeated", "m5_volume_unreadable",
    "slippage_fail", "source_buy_floor", "reentry_cooldown"})
LOW_PRIORITY_MIN_FREE = 2
SIZED_FAMILIES = frozenset({"roundtrip_cap", "impact_cap", "route_over_mid",
                            "route_over_source", "chase_quote", "evm_chase_quote",
                            "slippage_fail"})


def should_capture(gate):
    """Whether the forward-capture lane should spend a quote on this gate family."""
    return bool(gate) and gate in CAPTURED_FAMILIES


def classify_gate(reason):
    """Exact live-gate identity for a terminal reason, or None when it must not be captured."""
    text = str(reason or "").strip()
    if not text:
        return None
    for gate, match in GATES:
        try:
            if match(text):
                return gate
        except Exception:
            continue
    return None


# --- Key disjointness ----------------------------------------------------------------------------

CAPTURE_KEY_VAR = "MEMECOIN_CAPTURE_RPC_POOL_URLS"
# Every OTHER endpoint variable is a role the capture must not share, DERIVED from the environment
# rather than listed here.
#
# The first version of this guard hardcoded eight names, and five of them did not exist: the real
# research key is MEMECOIN_RESEARCH_HELIUS_RPC_URL, the TPU one is MEMECOIN_TPU_RPC_URL, and
# MEMECOIN_SOL_TAPE_RPC_URL / MEMECOIN_V6_SOLANA_FEED_RPC_URL / the opportunity recovery URL were
# absent altogether. It passed, while silently checking a partly imaginary list -- a guard that
# names its subjects by convention instead of verifying they exist, which is the same defect class
# this module refuses everywhere else. A derived set also covers a key added tomorrow, which a
# hardcoded list never does.
KEY_VAR_PATTERN = "RPC"


def _urls(env, name):
    return [u for u in str(env.get(name) or "").replace(",", " ").split() if u]


def other_endpoint_vars(env):
    """Every endpoint variable in the environment except the capture's own."""
    return sorted(n for n in env
                  if KEY_VAR_PATTERN in str(n).upper() and str(n) != CAPTURE_KEY_VAR)


def assert_pool_disjoint(env):
    """Capture endpoints must share no URL with any funded, custody, TPU or research role.

    Fails startup LOUDLY. Convention is not enough: on 2026-08-21 research load on a funded key
    hammered a 429 roughly 7,000 times over four hours against the funded runner's own quota, and
    an RPC key edit the same day cost a 20 minute funded outage. Both happened because one
    endpoint held two roles. Returns the capture URLs.
    """
    capture = _urls(env, CAPTURE_KEY_VAR)
    if not capture:
        raise RuntimeError("%s is not set; the capture has no endpoint of its own" % CAPTURE_KEY_VAR)
    for name in other_endpoint_vars(env):
        shared = set(capture) & set(_urls(env, name))
        if shared:
            # Never log the URL: it carries the key.
            raise RuntimeError(
                "%s shares %d endpoint(s) with %s; research load must not ride a funded quota"
                % (CAPTURE_KEY_VAR, len(shared), name))
    return capture


# --- Jupiter pressure ----------------------------------------------------------------------------

@dataclass
class JupiterYield:
    """Shut the capture after any Jupiter 429, for a fixed hold.

    A 429 is the provider answering about the whole IP, and the funded runner shares that IP. The
    capture has no deadline, so the correct response to pressure is to stop asking -- never to
    retry, and never to fail over to the other Jupiter host, which is the host funded falls back
    to. The lost observations are censoring, and censoring gets reported.
    """
    hold_s: float = 120.0
    clock: object = time.monotonic
    name: str = "Jupiter"          # the EVM lane holds its own instance for DexScreener
    _until: float = 0.0

    def observe(self, status):
        if status == 429:
            self._until = self.clock() + float(self.hold_s)
            logger.warning("capture: %s 429 -> yielding for %.0fs (funded shares this IP)",
                           self.name, self.hold_s)
            return True
        return False

    def blocked(self):
        return self.clock() < self._until


# --- Scheduler -----------------------------------------------------------------------------------

def _epoch(iso):
    """Wall-clock epoch seconds for an ISO stamp, or None."""
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


@dataclass
class Capture:
    capture_id: str
    mint: str
    origin_event_id: str
    gate: str
    refused_at: str
    refused_mono: float
    horizon_s: float
    cadence_s: float
    expected_obs: int
    network_id: str = "solana"
    refused_epoch: float = None
    decimals: int = None
    next_due: float = 0.0
    tokens_atomic: int = None      # the bag the entry quote would have bought; set on obs 0
    n_obs: int = 0
    done: bool = False
    quote_input_usd: float = None
    basis: str = None              # 'exact_route' on an EVM exact capture (its own lane)
    slippage_available_over_min: float = None   # slippage_fail only; None when not carried
    route: object = None           # the exact lane's fixed route, set by its first quote

    def due(self, now):
        return not self.done and now >= self.next_due

    def advance(self, now):
        """Step to the NEXT point on the capture's own 5-second grid, never `now + cadence`.

        Anchoring to `now` accumulates every pass's HTTP time and the loop's own poll granularity
        into the interval: measured 2026-08-26, a nominal 5s cadence ran at 6.2s. `expected_obs` is
        fixed at `horizon / cadence`, so a drifting interval fits fewer observations in the window
        and the reaper stamps `truncated_worker` on a capture whose worker did nothing wrong -- a
        false completeness failure in the one signal that exists to detect real ones.

        If a pass falls more than one cadence behind, the grid SNAPS forward rather than firing a
        catch-up burst: the missed observations are a gap, and a gap stays a gap.
        """
        self.next_due += self.cadence_s
        if self.next_due <= now:
            missed = int((now - self.next_due) // self.cadence_s) + 1
            self.next_due += missed * self.cadence_s

    def age(self, now_epoch):
        """Seconds since the REFUSAL, the simulation clock -- not since this capture started.

        The capture learns about a refusal from the durable record, so it starts up to one poll
        interval late. Measuring from its own start would hide that delay; measuring from
        `refused_at` records it honestly as a first `age_s` of a second or two.
        """
        if self.refused_epoch is None or now_epoch is None:
            return None
        return max(0.0, float(now_epoch) - float(self.refused_epoch))

    def expired(self, now):
        return now - self.refused_mono >= self.horizon_s


def capture_id(network_id, mint, origin_event_id):
    return "%s:%s:%s" % (network_id, mint, origin_event_id or "")


def _lane(network_id):
    return "solana" if str(network_id) == "solana" else "evm"


EXACT_SUFFIX = "#exact"
# DIP SHADOW (2026-09-30): its own id, lane and budget beside the refusal's base series, which
# pre-registered reads use and which must stay byte-identical. Every observation is a BUY of the
# reserved clip; the would-be dip fill is derived at read time, from the attempt's own cap and
# reference, as the first row back within it.
DIP_FAMILIES = frozenset({"chase_quote", "slippage_fail"})


def _capture_lane(c):
    if c.basis == "dip":
        return "solana_dip"
    return "evm_exact" if c.basis == "exact_route" else _lane(c.network_id)


def start(captures, *, mint, origin_event_id, gate, refused_at, network_id="solana",
          horizon_s, cadence_s, max_concurrent, now, on_refuse=None, now_epoch=None,
          quote_input_usd=None, exact=False, attempt_id=None, slippage_available_over_min=None,
          dip=False):
    """Begin one capture, or REFUSE it for capacity and say so.

    `attempt_id` rides on a polled refusal for the slot-end shadow (2026-09-29); a capture keys on
    its mint and origin and does not use it, so a polled row still feeds this call as it is.

    Refusing outright is deliberate. A queue would produce a series that starts late and silently
    misses the first minutes -- exactly the minutes a stop simulation needs -- and thinning an
    in-flight capture would make its fidelity unknowable after the fact. A refusal is explicit,
    per-mint and countable, so a downstream estimate can report "n captured, m refused".
    """
    # EXACT EVM (2026-09-28): a second series for the same refusal, on our own quoters. Its own id,
    # so the reaper and every reader grouping by capture_id never pool it with the display series.
    exact = bool(exact) and str(network_id) != "solana"
    if dip and (str(network_id) != "solana" or gate not in DIP_FAMILIES):
        return None
    key = (capture_id(network_id, mint, origin_event_id)
           + (EXACT_SUFFIX if exact else "") + (DIP_SUFFIX if dip else ""))
    if key in captures:
        return None
    if gate in SIZED_FAMILIES:
        import math
        try:
            quote_input_usd = float(quote_input_usd)
            valid_size = math.isfinite(quote_input_usd) and quote_input_usd > 0
        except (TypeError, ValueError, OverflowError):
            valid_size = False
        if not valid_size:
            if on_refuse is not None:
                on_refuse(key, "not_captured_size_unknown", mint, gate, refused_at)
            return None
    else:
        quote_input_usd = None
    # A refusal whose window has already closed has nothing left to capture: the hypothetical
    # position it models would have hit the timecap rail before the first quote. This is not a
    # theoretical case -- a worker restart polls a BACKLOG, and without this guard every stale
    # refusal in it would open a capture that starts past its own horizon and then be stamped
    # `complete` by the coverage test, because its first observation already reaches the end of
    # the window. That would put fabricated series into the one dataset built to answer a
    # counterfactual.
    # A refusal discovered PART-WAY through its window still starts, and that is deliberate: the
    # leading interval counts as a gap, so such a capture is stamped `gapped` and its `age_s`
    # records exactly what was never observed. It only arises after worker downtime shorter than
    # the horizon; a second freshness threshold would be a number from nowhere, and the existing
    # labelling already keeps it out of any `complete`-filtered consumer.
    started = _epoch(refused_at)
    reference = now_epoch if now_epoch is not None else time.time()
    if started is not None and (float(reference) - started) >= float(horizon_s):
        if on_refuse is not None:
            on_refuse(key, "not_captured_stale", mint, gate, refused_at)
        return None
    # PER LANE (2026-09-13). Solana and EVM spend different providers (Jupiter vs DexScreener), so
    # each lane has its own budget and an EVM capture never takes a Solana slot.
    lane = "solana_dip" if dip else "evm_exact" if exact else _lane(network_id)
    live = sum(1 for c in captures.values() if not c.done and _capture_lane(c) == lane)
    if live >= int(max_concurrent):
        if on_refuse is not None:
            on_refuse(key, "not_captured_capacity", mint, gate, refused_at)
        logger.info("capture at capacity (%d); refused mint=%s gate=%s", live, mint, gate)
        return None
    if (not dip and gate in LOW_PRIORITY_FAMILIES
            and int(max_concurrent) - live < LOW_PRIORITY_MIN_FREE):
        if on_refuse is not None:
            on_refuse(key, "not_captured_capacity_reserve", mint, gate, refused_at)
        logger.info("capture reserve (%d of %d live); refused low-priority mint=%s gate=%s",
                    live, int(max_concurrent), mint, gate)
        return None
    # Keep the full horizon. Only chart-premium samples need the faster uniform grid.
    if gate == "route_over_mid" and str(network_id) == "solana":
        cadence_s = min(float(cadence_s), 5.0)
    cap = Capture(
        capture_id=key, mint=str(mint), origin_event_id=str(origin_event_id or ""), gate=str(gate),
        refused_at=str(refused_at), refused_mono=float(now), refused_epoch=_epoch(refused_at),
        horizon_s=float(horizon_s),
        cadence_s=float(cadence_s), expected_obs=int(float(horizon_s) // float(cadence_s)),
        network_id=str(network_id), next_due=float(now), quote_input_usd=quote_input_usd,
        basis="dip" if dip else "exact_route" if exact else None,
        slippage_available_over_min=slippage_available_over_min)
    captures[key] = cap
    return cap


def start_dip(captures, refusal, *, now, now_epoch, on_refuse=None):
    """The dip shadow for one polled refusal, sized and scoped by the MEMECOIN_CAPTURE_DIP_* knobs.
    None when off, out of scope, or refused (and then counted through `on_refuse`)."""
    from sniper.settings import memecoin as cfg
    window = float(cfg.MEMECOIN_CAPTURE_DIP_WINDOW_S or 0)
    if window <= 0:
        return None
    return start(captures, mint=refusal["mint"], origin_event_id=refusal["origin_event_id"],
                 gate=refusal["gate"], refused_at=refusal["refused_at"],
                 network_id=refusal["network_id"], horizon_s=window,
                 cadence_s=float(cfg.MEMECOIN_CAPTURE_DIP_CADENCE_S),
                 max_concurrent=int(cfg.MEMECOIN_CAPTURE_DIP_MAX_CONCURRENT), now=now,
                 now_epoch=now_epoch, on_refuse=on_refuse,
                 quote_input_usd=refusal.get("quote_input_usd"),
                 slippage_available_over_min=refusal.get("slippage_available_over_min"), dip=True)


# --- Quoting -------------------------------------------------------------------------------------

class _Instrumented:
    """HTTP seam that records status, latency and host for the LAST call, for the cost measurement
    the design requires from day one (section 5). Never swallows: the caller still sees the error."""

    def __init__(self, http):
        self._http = http
        self.last = {}

    def __call__(self, method, url, headers=None, body=None, timeout=10):
        from urllib.parse import urlsplit
        started = time.monotonic()
        status = None
        try:
            status, payload, extra = self._http(method, url, headers=headers, body=body,
                                                timeout=timeout)
            return status, payload, extra
        except Exception as exc:
            # NOT `getattr(exc, "code")`: that reads urllib only, and this seam's whole job is to
            # not miss a 429. The day the transport becomes requests or httpx, a one-attribute
            # probe goes silent and the yield stops firing with nothing failing loudly. Reuse the
            # pool's extractor rather than growing a second one that drifts from it.
            from sniper.execution.solana_rpc_pool import _exception_status
            status = _exception_status(exc)
            raise
        finally:
            self.last = {"host": urlsplit(url).netloc, "status": status,
                         "ms": (time.monotonic() - started) * 1000.0}


def quote_once(cap, *, http, rpc_url, clip_usd, now_iso, now_mono, jup_yield=None,
               quote_fn=None, decimals_fn=None, now_epoch=None, exit_fn=None):
    """One immutable observation for one capture. Never raises.

    `quote_price` is USD per token from the exact route -- the same quantity funded rows carry in
    `positions.quote_price`, so the two are comparable without a unit conversion. It is NOT a
    chart mid; that is the whole point of this instrument.

    TWO SIDES, DELIBERATELY. The first observation is a BUY quote: it is the entry we would have
    paid, and it fixes the bag. Every later observation is a SELL quote of THAT bag, valued
    `expected_usdc / tokens` -- funded's own executable-exit convention.

    An all-buy series would be self-consistent and wrong. It measures what it would cost to buy the
    coin again, never what we would receive selling it, so a counterfactual computed on it never
    pays the exit spread and reads systematically better than the position would have. Consistency
    of price space is not enough: the SIDE has to match what the number is used for. (Raised by
    Beta, 2026-08-26, from the funded book's own entry-vs-mark divergence.)
    """
    from sniper.persistence.db_memecoin_captures import atomic_text

    clip_usd = cap.quote_input_usd if cap.quote_input_usd is not None else clip_usd
    if jup_yield is not None and jup_yield.blocked():
        return None
    if quote_fn is None:
        from sniper.execution.solana_real_seams import jupiter_entry_quote as quote_fn
    if exit_fn is None:
        from sniper.execution.solana_real_seams import jupiter_exit_quote as exit_fn
    if decimals_fn is None:
        from sniper.services.memecoin_quotes import token_decimals as decimals_fn
    seam = _Instrumented(http)
    row = {
        "capture_id": cap.capture_id, "origin_event_id": cap.origin_event_id,
        "network_id": cap.network_id, "mint": cap.mint, "gate": cap.gate,
        "refused_at": cap.refused_at, "observed_at": now_iso,
        "age_s": cap.age(now_epoch if now_epoch is not None else _epoch(now_iso)),
        # The cadence THIS series was taken at, recorded rather than inferred. Spacing alone cannot
        # separate "the cadence changed" from "we missed polls", and those are the two facts the
        # reaper's gap test exists to tell apart. The table already holds both 5s and 25s captures
        # after the 2026-08-26 Jupiter budget resize, so a global constant would misjudge one of them.
        "cadence_s": cap.cadence_s,
        "horizon_s": cap.horizon_s, "quote_input_usd": float(clip_usd),
        "provider": PROVIDER, "expected_obs": cap.expected_obs, "endpoint_index": 0,
        # Explicitly absent, never omitted: a missing key and a known-null price read the same at
        # the DB but not to a consumer holding the dict. Unknown must stay unknown, and visibly.
        "quote_price": None, "quote_id": None, "quote_route_target": None, "quality": None,
        "token_qty_atomic": atomic_text(cap.tokens_atomic), "token_decimals": cap.decimals,
        "quote_output_atomic": None, "quote_min_output_atomic": None,
        "quote_price_basis": "exact_route",
        "slippage_available_over_min": cap.slippage_available_over_min,
    }
    side = "buy" if cap.tokens_atomic is None else "sell"
    row["quote_side"] = side
    quote = None
    try:
        if side == "buy":
            quote = quote_fn(cap.mint, float(clip_usd), http=seam)
        else:
            quote = exit_fn(cap.mint, int(cap.tokens_atomic), _EXIT_SLIPPAGE_PCT, http=seam)
    except Exception as exc:
        row["quality"] = "timeout" if isinstance(exc, TimeoutError) else "provider_error"
    row["http_status"] = seam.last.get("status")
    row["latency_ms"] = seam.last.get("ms")
    if jup_yield is not None:
        jup_yield.observe(seam.last.get("status"))
    if quote is None:
        row["quality"] = row.get("quality") or "no_route"
        return row
    # Buy outputs use token atoms; sell outputs use USDC atoms. Missing floors stay unknown.
    row["quote_output_atomic"] = atomic_text(quote.get("outAmount"))
    row["quote_min_output_atomic"] = atomic_text(quote.get("otherAmountThreshold"))
    # Decimals are IMMUTABLE, so this costs one RPC per mint for the whole capture, never per
    # observation. That single call is the entire RPC dependency -- measured 2026-08-26.
    if cap.decimals is None:
        try:
            cap.decimals = decimals_fn(cap.mint, rpc_url, http=seam)
        except Exception:
            cap.decimals = None
    row["quote_id"] = quote.get("quote_id") or quote.get("contextSlot")
    row["quote_route_target"] = quote.get("routePlan") and "jupiter" or quote.get("route")
    try:
        if side == "buy":
            # The bag this entry would have bought. Fixed once; every later sell quote prices IT,
            # not a fresh notional, because that is the position we are pretending to hold.
            if cap.basis != "dip":       # a dip series never fixes a bag: every row is a buy
                cap.tokens_atomic = int(quote["outAmount"])
            out = int(quote["outAmount"]) / (10 ** int(cap.decimals))
            row["quote_price"] = float(clip_usd) / out if out > 0 else None
        else:
            # expected_usdc / tokens -- funded's executable-exit convention, so an exit here and an
            # exit on the funded book mean the same number.
            tokens = int(cap.tokens_atomic) / (10 ** int(cap.decimals))
            usdc = int(quote["outAmount"]) / 1e6
            row["quote_price"] = usdc / tokens if tokens > 0 else None
        row["quality"] = "ok" if row["quote_price"] else "no_route"
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        row["quality"] = "provider_error"
    row["token_qty_atomic"] = atomic_text(cap.tokens_atomic)
    row["token_decimals"] = cap.decimals
    return row


DISPLAY_PROVIDER = "dexscreener"


# DexScreener returns at most this many PAIRS per /latest/dex/tokens response, even for one token
# (measured 2026-09-17: WETH on Base and wSOL each returned exactly 30).
DS_PAIR_CAP = 30


def dexscreener_display_fn(http=None):
    """`keys -> (status, {(network_id, mint): priceUsd or None})`, one DexScreener read plus at most one.

    READ-ONLY: no wallet, no signature, no custody RPC. The pair is `select_dex_pair`'s pick, the
    same pair the funded gates read, and the URL is `ds_tokens_url`, so the cache bust follows the
    operator's switch.

    A key maps to None only when an UNCAPPED 200 response has no pair for it (no_route). A response
    of DS_PAIR_CAP pairs can drop a listed token: 8 Arc mints in one read returned 30 pairs and 3 of
    them priced only when asked alone. Those tokens are re-asked together in ONE more read, and a
    token still absent from a capped answer is left OUT of the map -- unknown, never no_route. A
    non-200 answer is never re-asked, so a 429 reaches the lane's yield on the same pass."""
    from sniper.execution.solana_quote import _http, ds_tokens_url, select_dex_pair

    def read(call, keys):
        status, payload, _extra = call("GET", ds_tokens_url(",".join(sorted({m for _n, m in keys}))))
        if status != 200:
            return status, {}
        pairs = (payload.get("pairs") or []) if isinstance(payload, dict) else []
        capped = len(pairs) >= DS_PAIR_CAP
        out = {}
        for network_id, mint in keys:
            pair = select_dex_pair(pairs, mint, network_id)
            if pair:
                out[(network_id, mint)] = float(pair["priceUsd"])
            elif not capped:
                out[(network_id, mint)] = None
        return status, out

    def fn(keys):
        keys = list(keys)[:30]
        call = http or (lambda m, u: _http(m, u, None, None, timeout=10))
        status, out = read(call, keys)
        unknown = [k for k in keys if k not in out]
        if status == 200 and unknown:
            try:
                again, more = read(call, unknown)
            except Exception:
                logger.warning("capture: DexScreener re-ask failed; %d token(s) stay unknown",
                               len(unknown), exc_info=True)
                return status, out
            status = again
            out.update(more)
        return status, out
    return fn


def display_observe(caps, *, now_iso, now_epoch, display_fn, display_yield=None):
    """One DISPLAY observation for each due EVM capture, from one batched read. Never raises.

    `quote_price` is DexScreener's priceUsd: a chart-space mark, never an exact route, and the row
    says so in `quote_price_basis`. No quote side and no clip: nothing was priced for a size. A
    yield is a gap for every capture in the batch, never a retry."""
    if not caps or display_fn is None or (display_yield is not None and display_yield.blocked()):
        return []
    started, status, prices, failed = time.monotonic(), None, {}, False
    try:
        status, prices = display_fn([(c.network_id, c.mint) for c in caps])
    except Exception as exc:
        from sniper.execution.solana_rpc_pool import _exception_status
        status, failed = _exception_status(exc), True
    ms = (time.monotonic() - started) * 1000.0
    if display_yield is not None:
        display_yield.observe(status)
    rows = []
    for c in caps:
        key = (c.network_id, c.mint)
        price = (prices or {}).get(key)
        rows.append((c, {
            "capture_id": c.capture_id, "origin_event_id": c.origin_event_id,
            "network_id": c.network_id, "mint": c.mint, "gate": c.gate,
            "refused_at": c.refused_at, "observed_at": now_iso,
            "age_s": c.age(now_epoch if now_epoch is not None else _epoch(now_iso)),
            "cadence_s": c.cadence_s, "horizon_s": c.horizon_s, "expected_obs": c.expected_obs,
            "provider": DISPLAY_PROVIDER, "endpoint_index": 0, "http_status": status,
            "latency_ms": ms, "quote_price": price if price and price > 0 else None,
            "quote_price_basis": "display", "quote_side": None, "quote_input_usd": None,
            # A key the read did not answer (a non-200, or a token a capped response dropped) is
            # unknown: provider_error. no_route needs an answer that named no pair.
            "quality": ("provider_error" if failed or key not in (prices or {})
                        else ("ok" if price and price > 0 else "no_route")),
        }))
    return rows


def pump(captures, *, now_mono, now_iso, http, rpc_url, clip_usd, record_fn,
         jup_yield=None, quote_fn=None, decimals_fn=None, now_epoch=None,
         display_fn=None, display_yield=None, exact_lane=None):
    """Serve every due capture once. Returns the number of observations written.

    Solana captures quote one by one on Jupiter. EVM captures share ONE DexScreener read per pass.
    Exact EVM captures go to `exact_lane` LAST, so its paced reads never delay a Solana stamp; each
    of its rows carries its own clock. Without a lane an exact capture writes nothing."""
    written = 0
    due_display, due_exact = [], []
    for cap in list(captures.values()):
        if cap.done:
            continue
        if cap.expired(now_mono):
            cap.done = True
            continue
        if not cap.due(now_mono):
            continue
        cap.advance(now_mono)
        if cap.basis == "exact_route":
            due_exact.append(cap)
            continue
        if _lane(cap.network_id) == "evm":
            due_display.append(cap)
            continue
        try:
            row = quote_once(cap, http=http, rpc_url=rpc_url, clip_usd=clip_usd, now_iso=now_iso,
                             now_mono=now_mono, jup_yield=jup_yield, quote_fn=quote_fn,
                             decimals_fn=decimals_fn, now_epoch=now_epoch)
        except Exception:
            logger.exception("capture: observation failed for %s", cap.capture_id)
            continue
        if row is None:                      # yielded to funded; a gap, never a retry
            continue
        try:
            record_fn(row)
            cap.n_obs += 1
            written += 1
        except Exception:
            logger.exception("capture: could not record observation for %s", cap.capture_id)
    for cap, row in display_observe(due_display, now_iso=now_iso, now_epoch=now_epoch,
                                    display_fn=display_fn, display_yield=display_yield):
        try:
            record_fn(row)
            cap.n_obs += 1
            written += 1
        except Exception:
            logger.exception("capture: could not record observation for %s", cap.capture_id)
    for cap in due_exact if exact_lane is not None else ():
        try:
            row = exact_lane.observe(cap)
        except Exception:
            logger.exception("capture: exact observation failed for %s", cap.capture_id)
            continue
        if row is None:                      # host held or unreadable: a gap, never a retry
            continue
        try:
            record_fn(row)
            cap.n_obs += 1
            written += 1
        except Exception:
            logger.exception("capture: could not record observation for %s", cap.capture_id)
    return written


# --- Completeness reaper -------------------------------------------------------------------------

def reap(counts, *, now_iso, horizon_s, cadence_s, stamp_fn, grace_s=60.0, max_gap_s=None):
    """Stamp a terminal outcome on every capture whose window has passed. Runs on its OWN clock.

    EVERY other outcome in this module is written by the capture loop itself, so none of them can
    report the loop not running. A capture that stalls, hangs or dies mid-series simply stops
    writing rows, and a consumer counting rows cannot separate "the coin stopped quoting" from "we
    stopped asking". That is precisely the failure that hid 87 mark-blind minutes on 2026-08-24: a
    streak counter that incremented inside the sweep, and a sweep that does not run cannot count
    itself as missing. So the trigger here is STALENESS -- a capture that should have finished and
    has not -- never a tally the worker had to be alive to increment.
    """
    import datetime as _dt
    stamped = 0
    cutoff = _dt.datetime.fromisoformat(str(now_iso)) - _dt.timedelta(
        seconds=float(horizon_s) + float(grace_s))
    # A gap that swallows a whole stop-confirm window makes a simulated stop unreproducible, which
    # is the thing this data exists to support. That is an OUTCOME anchor, not a percentile of the
    # gap distribution -- the same rule this design applies to every other threshold.
    try:
        from sniper.settings.memecoin import MEMECOIN_REAL_STOP_CONFIRM_SWEEPS as _sweeps
    except Exception:
        _sweeps = 5
    sweeps = max(2, int(_sweeps))
    gap_override = max_gap_s          # an explicit threshold still wins, for tests and one-offs
    for cid, (mint, refused_at, hz, expected, n_obs, terminal, reach_s, row_cadence,
              gap_s) in counts.items():
        if terminal:                          # already terminal; outcomes are written once
            continue
        try:
            if _dt.datetime.fromisoformat(str(refused_at)) > cutoff:
                continue                      # still inside its window
        except (TypeError, ValueError):
            continue
        # The cadence the series was ACTUALLY taken at, not the caller's current default. The
        # table outlives any one setting -- MEMECOIN_CAPTURE_CADENCE_S moved 5 -> 25 on 2026-08-26
        # to fit the Jupiter IP budget -- so captures at different cadences coexist, and judging a
        # 25s series against a 5s threshold would stamp every healthy row `gapped`. Same defect as
        # the rails censor set to one nominal interval, one layer over.
        eff_cadence = float(row_cadence or cadence_s)
        max_gap_s = float(gap_override) if gap_override is not None else eff_cadence * sweeps
        want = int(expected or (float(hz or horizon_s) // eff_cadence))
        # COVERAGE decides completeness, not the row count. A count test is brittle to scheduler
        # jitter -- a perfectly healthy capture measured 159 of 162 observations on 2026-08-26,
        # because a 1s poll granularity against a 5s cadence costs a slot now and then -- and
        # false-stamping healthy captures would destroy the only signal that detects real ones.
        # A worker that STOPPED has a series that stops with it, so its furthest `age_s` falls far
        # short of the horizon; jitter cannot fake reaching the end of the window. The row count is
        # still recorded, so series DENSITY stays a question a consumer can ask separately.
        window = float(hz or horizon_s)
        reached = float(reach_s or 0.0)
        # `complete` must mean OBSERVED THROUGHOUT, not merely "reached the end". The two differ,
        # and the difference is produced by this module's own 429 yield: a capture that yields for
        # 120s mid-series and then runs to the horizon reaches the end with a 120-second hole in
        # it. Under provider pressure -- exactly when the yield exists -- that is the NORMAL shape,
        # so a two-outcome reaper would stamp holed series `complete` precisely when they are least
        # trustworthy, and `outcome` is what a consumer filters on. A third value keeps the
        # distinction in the data instead of collapsing it. (Beta, review, 2026-08-26.)
        #
        # The `n_obs >= 2` clause closes the degenerate case where a window no longer than two
        # cadences is satisfied by a single early row. Not reachable at the shipped 810s horizon,
        # but `horizon_s` is per-capture and MEMECOIN_REAL_MAX_HOLD_S has moved 600 -> 2592000 ->
        # 600 inside one day, so it is guarded rather than argued about.
        holed = gap_s is not None and float(gap_s) > float(max_gap_s)
        if reached < window - eff_cadence or int(n_obs or 0) < 2:
            outcome = "truncated_worker"
        else:
            outcome = "gapped" if holed else "complete"
        stamp_fn(cid, outcome, mint=mint, refused_at=refused_at, horizon_s=hz,
                 expected_obs=want, age_s=reached, n_obs=int(n_obs or 0),
                 max_gap_s=(float(gap_s) if gap_s is not None else None))
        stamped += 1
    return stamped
