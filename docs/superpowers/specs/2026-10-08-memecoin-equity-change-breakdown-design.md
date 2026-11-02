# Equity change breakdown

Operator order 2026-10-08 ~17:40Z, via Alpha. In the morning the operator saw total equity at
$1,555, and later ~$1,535 while "Today" PnL rose. Nothing on screen explained the gap. Alpha's
reconciliation from ~14Z: +$25.17 trade (one real close at 16:19Z), native drift -$43..-45 (ETH,
BNB and SOL down 5-6%), net about -$19, against equity $1,539.14 (cash 1,046.52 + gas_usd 492.62,
unrealized 0), with a $1-2 residual. Equity is never stored today, so no morning number can be
checked afterwards.

Display only. Not trading advice and not a performance claim.

## What the card shows

Under the Real tab hero, one card with a window switch: **Today** (the UTC day, as the hero's
Today: `day = now[:10]` in `sniper/routes/api_memecoin.py`) and **24h**. The operator removed "since
baseline" (2026-10-08), so this card has no baseline window. For the chosen window:

| Row | Meaning |
|---|---|
| Equity at start -> now | the recorded total at the window's first sample, its time, and the newest sample |
| Trading | realized PnL of real closes in the window + (unrealized now - unrealized at start) |
| Native price drift | memecoin_native_float `drift()` over the same two sample times, all assets |
| Other | change in equity - Trading - Native price drift |

Other is ALWAYS shown, with its sign, never hidden, rounded to zero or clamped. It holds gas paid
outside a trade's PnL, deposits and withdrawals (not detected in v1), bridge transfers in flight,
rounding and read timing. A line under the card says the figures are recorded by the runner once
a minute, while the hero reads live balances, so the two equity figures can differ by a few
dollars (SOL price source, open-position marks, read time).

States, never a $0 for an unknown:
- "not recorded yet": the window starts before the recorder's first sample (Today until the next
  UTC midnight after deploy; 24h for a day).
- "unknown at start" / "unknown now": the sample at that end is missing a network, a price or the
  open book; the card names what is missing.
- "no sample at the window start": the recorder has samples but none in the first 150 s of the
  window (the runner was down).
- stale: the newest sample is over 3 min old, or the page could not refresh (the drift panel's SF09
  rule: the page ages the snapshot itself, and no receipt time counts as stale).

## Source: runner-composed (option a)

The runner's native-float sampler thread composes equity once a minute from reads that already
happen. It makes no new custody or funded call, and it never calls a funded seam from a display
thread. Option (b), the web storing each accounting result, records nothing while no dashboard is
open; it is not used.

Per network, the same formula as the hero (`real_accounting_route`): equity = cash + native USD +
(deployed - reserved) + unrealized. Networks are the hero's, `["solana"] + MEMECOIN_EVM_ALLOWLIST`
(today solana, 1, 4663, 5042, 56, 8453).

| Network | Cash | Native USD | Read by |
|---|---|---|---|
| solana | USDC | SOL x SOLUSDT | slot-lane prefetch: SOL is already recorded; USDC is added to the recorder as ("solana", "quote"), one dict assignment |
| 4663, 56 | WETH / WBNB x price | ETH / BNB x price | the gas top-up pass (recorder, every 15 s) |
| 8453, 1 | WETH x ETHUSDT | ETH x ETHUSDT | the panel's public read, every 5 min |
| 5042 (Arc) | USDC (ERC-20 0x3600..., 6 decimals) | 0 (native is the quote) | NEW: one batched eth_chainId + balanceOf POST every 5 min to the chain-owner endpoint in the free-endpoint registry (rpc.mainnet.arc.io), on the sampler thread; never the custody RPC |

- Prices: the sampler's own Binance sample for that minute (ETHUSDT, BNBUSDT, SOLUSDT). USDC is $1.
  The drift row uses the same prices and units, so in a window without trades or transfers Other is
  0 up to rounding.
- Units: the sampler's chain units with their observation time (SF07). A network is known when every
  part was read within 15 min (`UNITS_MAX_AGE_S`, by observation).
- Open book: `m.real_accounting` on a `get_readonly_connection()` handle to the ledger, with the
  newest stored `memecoin_position_marks.mid` per open position, no older than 15 min. Every open
  position needs its own fresh mark, paused ones too. One without a fresh mark is NOT counted at
  cost (which is where the hero puts it): its network's equity and the sample's unrealized are
  unknown, and the card names it ("open position without a fresh mark"), so a stuck bag keeps the
  card unknown until it is marked or written off (Gamma REFUSE on d514d4f51). An open position on
  a chain outside the list is named as missing, as the hero lists it.
  Measured on the live ledger: p50 18.2 ms, p90 21.4 ms per call (7 calls).
- Reserved entries: as the hero, deployed includes reserved and reserved is subtracted, because that
  USDC is still in cash.

## Storage

`data/memecoin_equity.db`, its own SQLite file, never the ledger, retention 30 days:
- `equity_samples(ts, equity_usd, unrealized_usd, missing)`: `equity_usd` is NULL unless every
  network, price and the open book are known; `missing` is a JSON list of what was not.
- `equity_by_network(ts, network, equity_usd, cash_usd, native_usd, open_value_usd, unrealized_usd,
  observed_at)`: NULL parts stay NULL; `observed_at` is the oldest read behind that network's
  balances.

`data/equity_change.json`: the sampler's snapshot each minute (tmp + rename). It holds the newest
sample, per-network parts and both windows already computed. `GET /api/memecoin/real/equity-change`
serves the file read-only (no RPC, ledger or Binance from the web) and marks it stale after 3 min.

## Window math

- Start sample: the first sample with ts in [window start, window start + 150 s]. End sample: the
  newest sample. Both must have a known `equity_usd`.
- Trading = SUM(realized_pnl) over `memecoin_positions` WHERE execution_mode='real' AND is_open=0
  AND closed_at in (start ts, end ts], plus unrealized(end) - unrealized(start). The bounds are the
  samples' own times, not the calendar bounds: a close before the start sample is already in the
  start equity. All 1,980 real closes share one ISO format (microseconds, "+00:00"), so the bounds
  compare as ISO strings. Today's Trading therefore leaves out closes in the first <= 150 s of the
  day that the hero's Today counts; the card shows its start time.
- Native price drift = the sum over ETH, BNB and SOL of `drift(prices, units_since(asset), start ts,
  end ts)` from `data/native_float.db`. Unknown when any asset is unknown.
- Other = (end equity - start equity) - Trading - drift. Unknown when any of the three is unknown,
  with the reason.

## Edge cases (CLAUDE.md rule 5)

1. Money path: no. Nothing reads these files for entry, exit, sizing or signing. The one hook is in
   the slot-lane prefetch: it copies the USDC value the prefetch already holds into the recorder.
2. Fill path: the hook is one `remember()` call, measured p50 432 ns, p90 455 ns (20k calls). It is
   not on the fill path: the prefetch refresh is background, and the slot lane only reads its
   snapshot. Everything else runs on the sampler's daemon thread: the ledger read (p50 18.2 / p90
   21.4 ms a minute) on a read-only autocommit handle (no WAL pin, no write lock), and the Arc read
   every 5 min. No synchronous call, lock or retry is added to an entry or exit.
3. Concurrency: the recorder replaces each key as one tuple; the sampler thread owns its SQLite
   handles; the web reads JSON written by rename. No module attribute, env var or shared connection
   is mutated. GUARD (Alpha 2026-10-08): the recorder never takes a lock the fill path takes (no
   exec_lock or wallet lock; a WAL reader blocks no writer), and its ledger handle is always
   `get_readonly_connection()`. Both are pinned by tests.
4. Absent, zero, unknown: a failed read is None, never 0, and makes that network unknown, so the
   sample's equity is NULL with the reason; a measured 0 balance is 0; a missing price makes native
   USD unknown; an unknown end makes every row of that window unknown, Other included. Other is never
   hidden and never clamped.
5. Dependencies: Binance down: no prices that minute, so the sample is incomplete. A public endpoint
   (8453, 1) down: the last read carries 15 min by observation, then unknown. THE ARC READ (Alpha
   approved 2026-10-08): one eth_chainId + balanceOf POST every 5 min to the chain-owner endpoint
   (rpc.mainnet.arc.io), on the sampler thread, never the custody RPC; failed, wrong chain or older
   than 15 min, Arc is unknown, so the sample's equity is unknown, never $0. Ledger busy:
   busy_timeout 5 s on the sampler's handle, then the book is unknown. Slot lane off: Solana unknown.
   `real_accounting` raising: book unknown, logged at WARNING (the runner logs WARNING and up only).
   Any sampler fault is logged and the next minute runs.
6. Units: USDC 6 decimals at $1; WETH, WBNB, ETH and BNB 18 (CHAIN_QUOTE); Arc USDC read as ERC-20 at
   6 decimals; SOL 9; times are epoch seconds in the store and ISO in the ledger. Network ids are the
   hero's `by_network` keys.
7. Sample: our own custody wallets only. Trading counts every real close (robot and manual), the same
   all-money number as the hero's Today. A deposit or a bridge moves Other, not Trading.
8. Rollback: `MEMECOIN_EQUITY_RECORDER=0` (default on; it rides the native-float sampler and is off
   when that is) and a restart; the card then says the recorder is off. Both files can be deleted;
   nothing else reads them. Reverting the code needs no other step: its tables are new.

Not handled in v1: drift counts every chain the native float panel reads; if the allowlist drops one
of them (8453 or 1), its ETH moves into Other (Gamma LOW); deposit and withdrawal detection; a partial exit straddling the window start (its
realized PnL is booked at the close and was already in cash at the start, so Trading overstates and
Other offsets it; positions mostly last under a minute).

## Checks

- A sample missing any network, price or the open book stores `equity_usd` NULL and is never summed
  as 0 (unit test on the composer).
- Other equals change - Trading - drift with its sign, also when large and negative, and the card
  always renders the Other row, including as unknown (unit test plus browser render test). Each test
  is proved red on its parent.
