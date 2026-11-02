# Professional day-trading techniques — sourced, in-depth research (5-stream)

Date: 2026-06-22. Method: 5 parallel deep-research agents, web-mandatory, ~76 total web calls,
academic PDFs read from primary source, unverified claims flagged. Replaces the earlier shallow
pass (the discretionary-techniques agent there made 0 tool calls — priors only).

Framing constant: **~9bps round-trip TAKER fee on Hyperliquid perps, paper, no maker rebate, no
latency edge.** Every technique scored against that floor.

## The one cross-cutting conclusion (now SOURCED, not asserted)

**Almost every classic day-trading technique with rigorous evidence is taker-dead at 9bps.** The
edge lives at maker/HFT/latency level or sub-fee horizons. Specifically:

- **Order-flow imbalance** — the *only* peer-reviewed microstructure signal (Cont-Kukanov-Stoikov).
  Honest replication: ~3% OOS R², 53% hit, **Sharpe 0.12, "trading costs eat you alive"**
  (Markwick). Edge ≪ 9bps, decays in seconds. Maker/HFT only.
- **BTC intraday time-series momentum** — best-documented intraday effect (Gao et al. JFE; global
  confirm Baltussen et al.). BTC version (Shen/Urquhart/Wang, **verified from primary PDF**):
  breakeven **3–10bps unlevered** → does NOT clear a 9bps round-trip as a taker. Maker/leverage only.
- **VWAP** — Bonferroni-corrected sweep (~5.8M configs): crossover edge = **0 of 74,800 configs**;
  trend/breakout negative; only short-side ±2σ band reversion survives (~89bps, equities,
  unvalidated on crypto). VWAP is an *execution benchmark*, not alpha.
- **Market/Auction Profile** (TPO, IB, 80% rule, POC-magnet, value-area fade) — its own proponents
  call it "a weather vane, not a crystal ball." **No peer-reviewed OOS net-of-cost backtest exists**
  for any branded rule. Session-broken in 24/7 crypto (no open/close auction).
- **CVD / tape / DOM** — built on aggressor classification wrong **21–31%** of the time; DOM is
  spoofable + iceberg-hidden (>44% depth hidden). Sub-30bps micro-reversals = taker-dead.
- **ORB** — real equity evidence (Zarattini-Aziz, 675% QQQ) BUT the edge is a relative-volume
  filter + leverage + idealized fills; the "open" doesn't exist 24/7; untested on perps.
- **Overnight/gap anomaly** — strong academically, dies at **~1bp round-trip** (Elm Wealth "Night
  Moves": −5%/yr at 1bp, −100%/yr at 10bps). No crypto analog.
- **Cross-sectional / relative-strength momentum** — works at months; **REVERSES at days/weeks**
  (Jegadeesh 1990, Lehmann 1990; crypto: daily reversal, Liu-Tsyvinski momentum is 1–4 *weeks*).
  Wrong horizon for day-trading; "buy today's leaders" intraday has weak backing.

Base rate (sourced): **~97% of day traders lose, <1% profit after fees; retail flow PREDICTS
returns yet retail LOSES money** (Barber & Odean) — the costs-erase-the-signal paradox is exactly
our situation.

## What ACTUALLY clears 9bps for a taker (the sourced winners)

1. **Liquidation cascades + whale-liquidation targeting.** HL's public `clearinghouseState` (every
   account's position + liq price) is a genuinely unique data edge — Glassnode validated HL liq
   clusters align with realized liquidations. Cascade moves = 100s–1000s of bps. CAVEAT: model
   SLIPPAGE not just fee (cascade fills brutal, thin book); copy-lag is the killer for following.
2. **Funding/OI-divergence squeeze setups.** Price↑+OI↑ stalling + extreme funding = long-squeeze
   fuel; large squeeze moves clear the fee, but timing is loose → use as BIAS, not a trigger.
3. **Catalyst volatility** (unlocks, CPI/FOMC, listings) — large moves, direction is the hard part;
   pre-position via funding/OI, trade the unwind.

## The biggest finding for the engine: the WORKFLOW transfers ~80%

The professional process — not the indicators — is what's encodable and where an engine BEATS a
human (no fear/greed):
- **Selection ("trade what's In Play")** — RVOL ≥2–3× baseline, volatility/ATR, liquidity floor,
  abnormal-flow score; trade only top-K. Highly encodable; the single highest-leverage idea.
- **Prep = levels + if-then scenarios** — pure math; "if price reaches X and behaves like Y, enter"
  IS a rule engine. Encodable.
- **Entry = confluence + confirmation, never anticipation** — N-of-M gating + candle-close /
  retest-holds; anti-chase is enforced *by the 9bps cost itself*. Mostly encodable; the order-flow
  "tape feel" is the one genuinely discretionary residue (proxy with depth/OFI, admit the gap).
- **Management** — ATR/structure stops, fixed-fractional sizing, R-multiples, scale-out, breakeven
  ratchet, pyramid-UP (never average down). Fully mechanical; engine > human here.
- **Expectancy/A-setup gating** — per-setup expectancy + SQN, only arm setups clearing the bar
  after cost (Van Tharp 0.2–0.8R benchmark). Engine *can't be talked out of its bar*.
- **Review/retire loop** — auto-tag, rolling expectancy, auto-retire on decay. Engine outperforms.

## Implications — how this changes the plan

- **VALIDATES the pivot we already made.** The two genuinely-sourced-best taker edges are
  liquidation cascades (the cluster-map) and whale/smart-money following (the analyzer + copy algo).
  The research independently lands on exactly what we built. Keep going there.
- **REVISES the engine-algorithm spec** (`2026-06-22-hl-engine-algorithm-design.md`):
  - Regime gate → KEEP, but as a risk-reducer/gate, NOT a return generator (sourced: ~0 OOS lift).
  - OI-divergence → KEEP as a BIAS filter (sourced as the most promising positioning signal), not a
    standalone taker trigger.
  - Relative-strength → **DROP / demote.** Sourced refutation: cross-sectional momentum reverses
    intraday; wrong horizon. Do not build it as a swing setup as specced.
  - ADD the discipline layer (expectancy/SQN gating, fixed-R sizing, in-play selection score,
    auto-retire) — this is the highest-value, best-sourced, most-encodable work and the engine
    currently lacks real sizing (flat $25) and an expectancy gate.
- **Everything still forward-tested** the way we test the basis/cascade edges (EV-after-fee, t>2,
  n≥40, OOS-is-the-real-test); crypto backtests run 5–10× optimistic OOS (McLean-Pontiff).

Full sourced detail (inline URLs, per-technique evidence, failure modes) is in the 5 agent
reports in this session's transcript. Load-bearing primary sources: Markwick OFI replication;
Shen-Urquhart-Wang BTC intraday momentum (breakeven 3–10bps); Cont-Kukanov-Stoikov; Gao et al.
JFE; Zarattini-Aziz ORB; Osler NY Fed stop-clustering; Glassnode HL liquidation validation;
Barber-Odean day-trader loss studies; Bellafiore *Playbook* / SMB workflow; Van Tharp expectancy/SQN.
