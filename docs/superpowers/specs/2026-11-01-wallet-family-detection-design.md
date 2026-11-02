# Wallet family detection using existing research tools

Operator request: improve wallet universe and Office Forensics to follow creators, their funded wallets, launches and exits, and test earlier profitable entries. Reuse wallet_network.db, wallet_watch, receipt proofs and current tape. Office seats stay stopped. No parallel identity graph, funded callbacks or provider changes.

## Deliverable
An independent research collector extends the existing wallet watch list and graph with verified native-SOL funding paths (including temporary WSOL), timestamped observed activity and durable paging/coverage. Existing graph readers provide wallet-family evidence as known at a decision cutoff. A pilot comparison records same-transaction buyer coordination separately from independent arrivals. Strategy readiness requires executable entry/exit evidence, costs and honest censored outcomes; a working tracker does not itself establish an edge.

## Edge cases and boundaries
No calls, waits or locks are added to signing/entry/exit paths. Private research readers retain existing global pace, quota stops and credential separation. Missing or failed reads are unknown, never zero movement. Failed chain transactions do not prove transfers. Fee payer is not necessarily transfer source. Native and WSOL atomic units stay integers serialized as text; decimals are never guessed. Duplicate pages/restarts must not duplicate movements or skip an unfinished interval. Balance reconciliation and ordered source/init/close instructions constrain WSOL proof. Provider rejection, timeout, malformed pages, pagination backlog and disk/wallet caps remain explicit coverage states. Exchange/high-fanout funding is evidence, not common ownership. Read time and event time are separate; future discoveries cannot enter historical decisions. Expansion is bounded and infrastructure stops it. No blanket scam blacklist, arbitrary-program value tracing, complete control attribution, unseen-chain guarantee or profitable-strategy guarantee.

## Rollback
Stop only the independent collector. Existing graph/watch data remains readable; never restart Office or funded services to install it. New graph evidence cannot arm wallets or fund entries.
