# Wallet forward recorder (notdecu size-tier test)

**Date:** 2026-10-02 · **Author:** Lambda · **Status:** operator order (via Alpha, 2026-10-02), design
sent to Alpha before the build and approved: option (a) runs outside the flat-book rule; no schedule
until Alpha installs one.

**For:** Gamma's pre-registered forward test (gamma episode ee10f5f6).
- **Unit:** notdecu's FIRST Solana buy of $300 or more on a coin after T0.
- **Outcome:** exact-route quotes; TP +25% / SL -35% / the +300 s mark.
- **Rug:** r_min <= -50% within 300 s.
- **Why a recorder:** a trial arm cannot do this. The dry run refuses with 0 Solana buy signals in
  7 days, because J7 does not deliver him.

## Design

`scripts/research/wallet_forward_recorder.py --owner <address> --label <name>`, a standalone process.

1. **Detection.**
   - One websocket on the research Helius key (`slot_lane_send_monitor.rpc_url()`, which refuses a
     funded key).
   - Two `programSubscribe` subscriptions, Token and Token-2022, filtered `memcmp` owner@32 on his
     address. These are his token accounts. His owner address is not watched: it carries about
     19.5k frames an hour of other signers' failures (trap ff51c53c). His token accounts gave
     16 notifications an hour on 2026-09-27.
   - Builder and parser: `memecoin_slot_signal._routed_subscribe` and `parse_token_notification`.
2. **Resolution.** A rise on one of his coin accounts is queued. A first sight is queued too: its
   previous amount is unknown. On the research key, through the shared pacer:
   - `getSignaturesForAddress` on that token account;
   - `getTransaction` for the signature in the notified slot only.
   His own coin delta must be positive, or the rise is dropped as `not_his_buy`. He must also have
   SIGNED the transaction, or it is dropped as `not_his_signature`. The first live smoke
   (2026-10-02 09:07-09:22Z) had 5 notifications. Three were other wallets sending coins into his
   accounts (dust or airdrops; his SOL never moved). Two were new accounts created at 0, which
   are no longer queued. A cross-chain fill delivered by a solver would also be dropped, visibly.
3. **Size.** `memecoin_source_valuation.value_source_buy`, the runner's own source valuation. When
   it cannot read the receipt (the short pre-upgrade curve event, brain 9241badf), the size comes
   from `dump_forensics.trade_of`. `basis` names which one was used.
   - SOL/USD comes from one capture-role Jupiter quote, at most once a minute, as the on-chain
     watcher does.
4. **Unit.** The first buy at or over `--min-usd` (300) on a coin, with block time at or after T0,
   starts one capture.
   - T0 is the recorder's first start; it is stored in the DB and kept across restarts.
   - The capture is `memecoin_refusal_capture.start(..., gate="forward_<label>")`, on the refusal
     capture's own code. It takes a buy quote at once, then a sell quote of that bag every
     `MEMECOIN_CAPTURE_CADENCE_S` (25 s).
   - Its horizon is 330 s, and `refused_at` is HIS buy time, so the first `age_s` is the detection
     lag.
   - Rows go to the recorder's own DB, `data/wallet_forward_<label>.db`, through
     `db_memecoin_captures`.
5. **Output.** The heartbeat (`<db>.heartbeat`) and the log carry COUNTS only: units, pending,
   active, calls this hour, subscription acks and notifications. They carry no returns, per the
   prereg's no-peeking rule. Gamma grades from the DB.

## Rule 5

1. **Money path:** none.
   - The process imports no runner code, writes no ledger table, and wakes no lane. A test pins that
     its source names none of `memecoin_real_runner`, `onchain_wallet_watch`,
     `memecoin_onchain_wake`, `make_real_buy_fn` or `record_onchain_detection`.
   - Jupiter spends the MEASUREMENT key (`declare_measurement_process`) and yields on a 429, as the
     refusal capture does.
   - Decimals come from the capture RPC pool, which `assert_pool_disjoint` checks is disjoint from
     every funded role.
2. **Fill path:** nothing on it. A separate process; the funded runner's read lanes are untouched.
3. **Concurrency:**
   - one socket thread feeding a queue;
   - one main thread that resolves, pumps and writes;
   - one sqlite writer;
   - the pacer is cross-process.
4. **Absent, zero or unknown:**
   - a rise whose signature never appears is `dropped/unresolved` after 5 passes, never "no buy";
   - a size the valuation and `trade_of` both cannot read stays NULL (`basis` unknown) and is never
     a unit;
   - a disconnected interval is a `gaps` row, so a buy inside it is censored, not absent. So is
     process downtime: at start, the time since the last heartbeat is a `process_down` gap (Gamma
     LOW 1);
   - the buy's signature is searched up to 4 pages of 25 back on his token account, because dust
     sends can bury it under newer ones (LOW 2). `dropped` keeps one row per signature and reason
     (LOW 3), and the heartbeat carries the dropped counts.
5. **Dependencies:**
   - **Hourly call cap** (default 200): a hit leaves the queue intact and is logged once an hour
     (`cap_hits`).
   - **Research-key stop** (quota or HTTP): logged, and the rise retries.
   - **Socket:** reconnects with backoff up to 60 s. Idle, it pings every 30 s: the first smoke
     (2026-10-02 09:07Z) saw Helius close an idle socket after 5 minutes with ConnectionClosedOK,
     and websockets 12's sync client sends no keepalive. A gap row spans the outage, from the drop
     to the next subscription.
   - **Jupiter 429:** the capture yields (`MEMECOIN_CAPTURE_JUPITER_YIELD_S`).
6. **Units:**
   - slots for matching a rise to its signature;
   - atomic token amounts;
   - USD sizes;
   - epoch seconds for T0 and block time.
7. **The sample:** one wallet's buys, counted only from T0. Buys before T0, and buys inside a gap,
   are not units. Gamma's read reports both.
8. **Rollback:** stop the process; there is no cron line until Alpha adds one. The DB is
   research-only.

## Not in scope

- Grading. Gamma's read computes TP/SL/+300 s and r_min from the capture rows.
- Any other wallet. `--owner` and `--label` make a second recorder possible; none is ordered.
