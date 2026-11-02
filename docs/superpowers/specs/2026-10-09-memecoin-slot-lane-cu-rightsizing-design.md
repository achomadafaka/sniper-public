# Right-sizing the slot lane's compute-unit limit, per measured build class

Status: SPEC, for Gamma. Author: Beta, 2026-10-09. Order: Alpha.

**SCOPE DECISION (Alpha, 2026-10-09, on section 2's saving line):** no right-sizing and no new classes.
The build is only:
- **(A) persistent demotion** (section 4). It is a live defect fix that only ever moves a class back to
  the default, safer limit. Alpha informs the operator rather than asking.
- **(B) the class key recorded on default builds** (section 3, step 1). This is evidence only.
- **(C) the third matcher form** (section 4).

The headroom rule and step 3 are not built. Sections 3-6 describe the full design. Section 9 is what is
built.

## 1. The problem, measured

The slot lane signs a compute-unit (CU) limit on every buy. The priority fee is the CU price (µL/CU) ×
that SIGNED limit. The fee does not depend on what the transaction consumes, and a landed failed buy
pays it too.

Today:
- A build signs its class limit only when its class has ≥ 10 successful sends on record. That is the
  existing rule (`SLOT_LANE_CU_CLASSES`, `sniper/settings/memecoin.py`): the limit is the recorded max ×
  1.15, rounded up to 1k, and only for the coin token programs in that class's sample.
- 5 classes exist, all leg-funded pump builds: 182k-215k.
- Every other build signs the lane default, `MEMECOIN_SLOT_LANE_CU_LIMIT`: 400,000 in `.env`. Before
  2026-10-07 ~16-20 UTC it was 260,000. Exotic legs are raised from the default by `slot_lane_exotic_cu`
  and never lowered.
- **A default build already ran out once.** 4oFrhiTQ (probablyinsider, 2026-10-07 15:37 UTC) was an
  unclassed pump_curve leg build at the then 260k default, recorded as
  `chain_error:Computational budget exceeded`. The default went to 400k within hours. The same venue
  spans 87k to over 260k depending on composition.

Measured from the ledger alone: `memecoin_entry_attempts.trace_json`, the slot_build `cu_limit` and the
receipt_cu `consumed`. The window is 2026-10-02 15:19 to 2026-10-09 05:29 UTC, 168 slot-lane sends with a
tx_sig.

| build (venue, what it signed) | sends | filled with a consumed reading | consumed p50 | consumed max |
|---|---|---|---|---|
| pump_amm, class leg/BisonFi, 215k | 33 | 19 | 179,430 | 188,488 |
| pump_curve, class leg/BisonFi, 207k | 15 | 10 | 165,248 | 172,764 |
| pump_amm, default 400k | 20 | 13 | 120,087 | 130,235 |
| pump_amm, default 260k | 23 | 15 | 130,278 | 212,228 |
| pump_curve, default 260k | 15 | 6 | 137,469 | 156,737 |
| pump_curve, default 400k | 5 | 3 | 87,393 | 105,130 |
| meteora_damm_v2, default 400k | 3 | 3 | 107,330 | 110,017 |

- **Most builds now sign the default.** Since 2026-10-07 12:00 UTC: 34 default builds against 7 class
  builds (21 pump_amm, 10 pump_curve, 3 DAMM v2), about 20 per day.
- **The default builds are not one class.** pump_amm at the 260k default reached 212k while pump_amm at
  400k stayed under 131k. The trace does not record why. Funding (native SOL or a USDC leg), the leg's
  dex and the coin's token program are not in the trace for an unclassed build. So the ledger cannot
  name these classes yet; section 3 step 1 fixes that.
- Gamma's receipt read of 2026-10-02 (`~/.cache/gamma/cuvenue`) put natively funded pump_amm at
  114-119k and the curve at 71-94k, both with fewer than 10 successful sends then.

## 2. What it is worth (measured, today's price)

Our CU price is 3.8M µL/CU (`MEMECOIN_SLOT_LANE_CU_PRICE_MICROLAMPORTS`), that is 3.8 lamports per CU.
SOL is at $110 here.
- **A 400k default build pays 0.00152 SOL ($0.17) in priority.**
- **pump_amm at the default, at a class limit of about 150k** (130,235 × 1.15, if one class holds):
  saves 250k CU × 3.8 = 0.00095 SOL ($0.105) per buy. At about 12 such buys per day: **about $1.27 per
  day**.
- **pump_curve at the default**, if a native class reaches n ≥ 10 near today's 105k max: about $0.12 per
  buy and about $0.68 per day. That holds only for the native class: a leg curve build ran past 260k.
- **Ceiling at today's price: about $2 per day.**
- **Under a higher CU price the saving scales with it.** At the S-landing spec's arm B price (55.9M µL/CU,
  `docs/superpowers/specs/2026-10-09-memecoin-slot-lane-s-landing-design.md`), 250k CU is 0.014 SOL ($1.54)
  per buy.
- **The cost side.** One CU-exhaustion failure loses a fill and its expected value, and still pays the
  fee. At today's price, one lost fill a month is about the whole month's saving. Section 4 limits each
  class to one such failure per change in program cost.
- **Possible secondary effect (unmeasured).** A smaller requested limit may fit more easily into a
  leader's per-account CU budget on a hot pool. No claim until measured.

## 3. The change, in order

**Step 1: record the class key on every slot-lane build.** It is pure and in memory; the build already
holds every field. The slot_build substage gains:
- `cu_venue`, `cu_funding` (native, leg or exotic), `cu_leg_dex`;
- `cu_token_program`, `cu_ata_creates` (the build's CreateIdempotent count);
- `cu_hook` (true, false or null; see section 4).

Today only `cu_class` is recorded, and only for a classed build. A research script then derives, per key,
the successful-send count, the max consumed and the token programs sampled, from the ledger alone with no
RPC. It is read-only, one job, reading by index.

**Step 2: persistent demotion and the headroom rule (section 4).** Ships before or with any new class.
It also fixes a flaw in today's 5 classes: a demotion is in memory only, so every restart (watchdog or
deploy) re-arms a class that already ran out.

**Step 3: new class entries in `SLOT_LANE_CU_CLASSES`.** A key gets an entry only when step 1's script
shows ≥ 10 successful sends for it. The entry carries (n, max consumed, token programs sampled, ATA-create
counts sampled, evidence date), and its limit is max × 1.15, rounded up to 1k (the existing rule).
- **Candidates:** native pump_amm, native pump_curve, raydium_cpmm, meteora_damm_v2, raydium_launchlab,
  and further single-dex legs, per venue and route class.
- **Entries are reviewed constants**, as today. No limit is learned at runtime: a runtime-learned limit
  would be a money-path value that moves with no review.

**These keep today's limit (the default, or the exotic raise), whatever the samples say:**
- **an exotic (on-demand) leg:** `slot_lane_exotic_cu` only raises;
- **a multi-hop leg:** a routePlan of more than one step, where `leg_dex` is None;
- **an ATA-create count the class never sampled.** An extra quote ATA costs about 22-28k CU;
- **a Token-2022 coin whose transfer-hook state is not known to be absent.** A hook runs its own program,
  so its cost is unbounded by our samples;
- **a token program the class never sampled;**
- **any key under n = 10;**
- **a demoted class.**

## 4. Failure: a too-low limit lands, fails and pays

- **Detection, per class.** Exists today: `pump_curve_buy.cu_exhausted` reads `ComputationalBudgetExceeded`
  in the receipt's chain error or `exceeded CUs meter` in its logs. It runs on every failed classed send,
  from the receipt the lane already read.
  - The one exhaustion on record is stored as `Computational budget exceeded`, a third form.
  - The build adds that form to the matcher, with a test on that recorded text, so detection never
    depends on which of the three forms a receipt carries.
- **An exhaustion on a default build** has no class to demote. It pages once, with step 1's class key,
  as evidence for the default or the exotic budgets. Nothing is lowered.
- **Headroom rule (new).** A FILLED classed send whose consumed exceeds its class's recorded max also
  demotes the class. Its 15% margin is being eaten, so the class goes back before a fill is lost. The
  consumed reading is the one receipt_cu already takes.
- **Auto-revert, no operator step.**
  - A demotion takes effect at once in memory: `CU_DEMOTED`, add-only, lane thread only, the existing
    membership test.
  - It is persisted as a ledger row: class key, the entry's evidence date, signature, signed limit,
    consumed, reason, time. The write runs on its own daemon thread (INSERT OR IGNORE through
    `db_memecoin`), never on the lane thread.
  - At startup the runner loads every row for the current entries into `CU_DEMOTED` before the lane
    thread starts. If that load fails, every class is treated as demoted, so every build signs the
    default, and the runner pages once. Never "no demotions".
  - A demoted class pages once, with the class, the signature and the consumed reading.
  - **It stays demoted until a reviewed table change replaces its entry** with a new evidence date. A row
    binds to the evidence date, so a new entry starts clean and an old row never blocks it.
- **Bounded loss.** One failed fill per class per change in program cost, and not one per restart.

## 5. No added call on the send path

- **Class lookup:** pure, in memory (as today).
- **The hook state:** read from mint bytes the warm path already holds.
  - The pumpswap pool warm reads the coin's mint account (`getMultipleAccounts`, base64) off the send
    path, and `_store` already parses supply and decimals from it. It also parses the Token-2022
    TransferHook extension there.
  - SPL mints are hook-free by program.
  - A build whose facts came from a path without those bytes (the miss learner, graduation state) has
    hook state null and keeps the default.
- **The trace fields:** ride the existing slot_build substage.
- **The demotion write:** on its own thread.
- **The startup load:** before the lane starts.
- No RPC, simulateTransaction, lock or retry is added between the signal and the send.

## 6. The one check

`tests/test_slot_lane_cu_classes.py` gains one invariant over the build surface, `build_cu_limit` and
`slot_lane_cu_class`. Every key the builders can produce signs `MEMECOIN_SLOT_LANE_CU_LIMIT` (or the exotic
raise, which is ≥ the default) when ANY of these holds:
- n < `SLOT_LANE_CU_MIN_SENDS`;
- token program unsampled;
- ATA-create count unsampled;
- hook state not false;
- multi-hop;
- demoted.

It enumerates the table's own entries with n < 10 plus synthetic unsampled keys. It is proved to fail on
a mutant that drops the n ≥ 10 filter, and on one that ignores the hook state.

The armed config check (`sniper/settings/memecoin.py`) refuses any per-class override below the class's
recorded max, as today. It also refuses any wired entry whose n < 10.

## 7. Edge cases (CLAUDE.md rule 5)

1. **Money path: yes.** It changes the signed limit only. Entry, sizing, caps and route are unchanged.
2. **Fill path speed.** Nothing is added (section 5). The class key costs a few attribute reads in a build
   that is already pure.
3. **Concurrency.**
   - `CU_DEMOTED`: written by the lane thread only, add-only, read by builders on that thread.
   - Persistence: on a daemon thread, INSERT OR IGNORE, so a runner overlap during a deploy cannot
     double-write.
   - Startup load: completes before the lane thread starts.
   - No module attribute is captured and restored.
4. **Absent, zero, unknown.**
   - A receipt without `computeUnitsConsumed` records None (exists) and is never a sample.
   - A failed send is never a sample: a reverted swap stops early and consumes less.
   - Hook state null is never "no hook".
   - A failed demotion load is "all demoted", never "none".
5. **Dependency failures.**
   - The receipt read fails, or the send is `submitted_unknown`: no evidence, no demotion, no sample.
   - The pool warm's mint read fails: hook state null, default limit.
   - A pump program upgrade that raises CU: the first failure (or headroom breach) in each class demotes
     it.
6. **Units.** µL/CU × CU / 1e6 = lamports. The fee is on the SIGNED limit. The Sender tip transfer (arm
   B, about 150 CU) is inside the 15% margin, and the samples mix both arms.
7. **Sample.**
   - The samples are our own successful sends, which is what we are sizing, so here they describing us is
     correct.
   - The tail beyond the observed max is unknown. That is why there is a margin, demotion and the
     headroom rule.
   - Builds that never ran out at the default are uncensored.
   - Since 10-02: 1 exhaustion, 4oFrhiTQ, at the old 260k default on an unclassed leg curve build. None
     at 400k, and none on a classed build.
8. **Rollback.**
   - **Per class:** set `MEMECOIN_SLOT_LANE_CU_LIMIT_<VENUE>_<FUNDING>_<DEX>` to the default (allowed: it
     is ≥ the recorded max) and restart, or revert the entry.
   - **Half-applied states:** step 1 alone changes no limit. Step 2 alone only adds persistence and the
     headroom rule to today's 5 classes. Step 3 requires step 2.

## 8. Not handled

- **The hook state on venues whose warm path holds no mint bytes.** Those builds keep the default.
- **Any change to the CU price.** That is the S-landing spec, and an operator decision.
- **The leader-side effect of a smaller requested limit.** Unmeasured; no claim.
- **The ordinary (Jupiter/Raydium) entry path.** Jupiter sizes its own limit (`dynamicComputeUnitLimit`).

## 9. What is built (Alpha's scope)

**(A) Persistent demotion.**
- **Ledger table** `memecoin_slot_cu_demotions`, created in `db_memecoin`. One row per demotion of a class
  entry:
  - the class key (venue/funding/dex);
  - the entry key, from the class's own `SLOT_LANE_CU_CLASSES` tuple: n, recorded max, sorted token
    programs. A changed entry has a new key, so an old row stops applying only when a reviewed table
    change replaces the entry;
  - demoted_at, signature, signed limit, reason.
  - Written with INSERT OR IGNORE.
- **Write: off the send thread.** `_demote_cu_class_if_exhausted` demotes in memory at once, as today. It
  then starts a daemon thread that opens its own writable connection, writes the row, and closes. A
  failed write logs the exception and pages once. The in-memory demotion still holds for this process.
- **Load: in `start_slot_lane`, before the lane thread starts,** on its own short-lived read-only
  connection. Every row whose entry key matches the current table goes into `CU_DEMOTED`.
  - The table is created in `ensure_memecoin_tables`, which the runner runs at startup before the lane.
    A ledger without the table is a KNOWN ZERO: no demotions and no page (Gamma).
  - Any other load failure (corrupt, locked, unreadable) counts every class as demoted, so every build
    signs the default, and the runner pages once. Never "no demotions".
  - The startup log line names the demoted classes.
- **The page on demotion:** memecoin_alerts, kind `slot_cu_demoted`, with the class, signature and
  limit. It is sent on the daemon thread.

**(B) The class key on default builds.**
- `build_cu_limit` (pump_curve_buy) also returns the key it looked up: venue, funding, leg dex, coin
  token program.
- The two callers (the curve and pumpswap builders) put it in the build detail as `cu_key`.
- `record_slot_build` pops it into the slot_build substage, beside `cu_limit` and `cu_class`. It is
  never a validity field.
- Scope: the pump builders, which are the only ones with classes (31 of the 34 default builds since
  10-07). The CPMM, LaunchLab, DAMM v2 and DBC builders sign a fixed limit or the exotic raise and are
  unchanged.

**(C) The third form.** `cu_exhausted` also matches `Computational budget exceeded`, the text stored on
4oFrhiTQ. A test covers that text.

**The check (Alpha):** a test that fails if a restart forgets a demotion.
- A classed send runs out through the lane path, so the row is written.
- `CU_DEMOTED` is cleared, as a new process would see it.
- The startup loader runs against the same ledger.
- The class then builds at the lane default.
- It is shown to fail when the loader is a no-op, and when the write is skipped.

**Residual losses (Gamma), each bounded to one more failed fill per event:**
- A daemon thread dies at interpreter exit. A demotion in the last moments before a watchdog restart or
  deploy can lose its row, so the class re-arms once.
- During a deploy overlap, a demotion by the OLD runner after the NEW runner's load is not seen until the
  next restart.

**Seen while measuring, not acted on:** the pump_amm/leg/BisonFi class's highest consumed since 10-02 is
188,488, above its recorded max of 186,857. Its limit is 215k, so 12% headroom is left. There is no
exhaustion and no change; it is evidence for whoever revisits the table.
