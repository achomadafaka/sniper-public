# Armed wallet links

Operator request: all currently armed UID/chain pairs; verified funding transfers,
repeat buyers, and which buy before the main Fomo wallet. No full suites.

Read the current roster and proof claims without writing the trading ledger.
Use a separate additive `data/wallet_identity_links.json` artifact; the existing
Solana graph publisher and durable funding receipts remain compatible.

The research collector stores bounded raw provider captures outside the trading
ledger. Reuse current Wallet Universe ±60-second exact-source flow windows,
including known roster peers. Solana actors require venue signer/economic
payment proof. EVM peers require a successful canonical receipt, signed payer,
linked acquisition/payment, and matching mined transaction identity. Compare
block/slot and transaction index; missing order is unknown. Counts use distinct
coins, never a PnL-selected sample. A repeat buyer appears on two distinct coins.

Funding means a confirmed signed directed transfer with exact atomic units.
Native funding and trusted-quote token funding retain different assets/decimals.
Shared exchange/hub funding is labeled; a transfer cannot establish common
ownership or bot identity. Never promote account claims or funded authority.

Every current armed pair gets explicit address, source-window and funding
coverage, even with no mapping/activity/provider. Bounded history, unsupported
routes, malformed/pruned receipts, provider timeouts/rejections/429s and incomplete
pagination cannot become zero. Conflicting/refuted/currently changed mappings
discard evidence that no longer binds. Unknown ordering cannot become before.

Collection uses separate read-only research credentials and shared provider
pacing/caps. No synchronous entry-path work, global mutation, funded restart,
or trading-state write. UI reads saved evidence only. This does not find all
historical alts, decode unsupported launchpads/relay initiators, establish
ownership, or measure predictive edge. Same-block earlier order may have zero
wall-clock seconds; retain both truths.

Rollback removes the additive artifact/reader and returns the web release;
raw captures and old Solana proofs remain. Atomic publication preserves the
previous artifact on interrupted runs. Revalidate claims on each web read.

Validation: focused failing proof/projection/UI checks, bounded live captures,
authenticated all-chain APIs, and unchanged funded pin/process/authority.
