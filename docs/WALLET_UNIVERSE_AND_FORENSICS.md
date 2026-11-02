# Wallet universe and launch forensics

The goal is to move from “a wallet bought a coin” to **who acted, what they could have known, how they were funded, how the launch was prepared, and where value subsequently moved**. That supports better research questions than copying a trade after its cheapest inventory has already been accumulated.

There are three related systems: wallet-universe observations, a relationship graph, and campaign-level forensics. Their records preserve the evidence and time boundaries needed to keep retrospective explanations from becoming imaginary early signals.

## 1. Wallet-universe observations

The wallet-universe layer builds descriptive observations around exact transactions across supported chains and venues. It is separate from funded execution and signing.

| Mechanism | Why it exists | Code |
| --- | --- | --- |
| Chain-aware wallet identity and observation metadata | Avoid merging similar addresses, chain identities, or unrelated source events | [`wallet_universe_metrics.py`](../sniper/services/wallet_universe_metrics.py) |
| Capital reads with age and provider-budget accounting | Distinguish a real zero from a failed read; record when a balance observation was actually known | [`wallet_universe_capital_reads.py`](../sniper/services/wallet_universe_capital_reads.py) |
| Source recovery from canonical blocks/logs | Some providers expose valid logs and blocks without a receipt index; retain that provenance instead of pretending a receipt was fetched | [`wallet_universe_source.py`](../sniper/services/wallet_universe_source.py), [`wallet_universe_recovery.py`](../sniper/services/wallet_universe_recovery.py) |
| Quote flow around the exact source transaction | Measure buying and selling around an event using signed flow and raw atomic units | [`wallet_universe_flow.py`](../sniper/services/wallet_universe_flow.py) |
| Venue-specific decoding | Interpret actual pool events, transfers, vault identities, and token units | [`wallet_universe_sol_venues.py`](../sniper/services/wallet_universe_sol_venues.py), [`wallet_universe_venues.py`](../sniper/services/wallet_universe_venues.py) |
| A separately pinned observation worker | Keep research collection and its mutable state away from the funded signer | [`wallet_universe_metrics.py` worker](../scripts/research/wallet_universe_metrics.py) |

“Universe” means the retained, observable cohort under its coverage rules—not every wallet on a blockchain. Quote flow is not automatically profit, and a wallet balance is not proof of who controls it.

## 2. From addresses to evidence-backed relationships

The research graph represents funding, mint creation, trading relationships, shared funders, holdings, co-buying, and other observations. It distinguishes verified proof, a claimed relationship, and missing evidence.

See [`wallet_network.py`](../sniper/office/wallet_network.py), [`wallet_watch.py`](../sniper/office/wallet_watch.py), and [`wallet_family.py`](../sniper/office/wallet_family.py).

Important distinctions:

- A common exchange withdrawal or shared service does not prove common control.
- A fee beneficiary is not necessarily the signing developer.
- A later transfer to a launch hub does not prove that hub funded an earlier purchase.
- No observed edge is not the same as having looked comprehensively and found no edge.
- A relationship learned after launch cannot be backdated into a pre-launch decision.

Those distinctions are stored and tested, not left solely to an analyst's prose.

## 3. Reconstructing launch preparation

Campaign records join funding and account preparation to later creation and trading events. Separate event time, receipt time, and knowledge time make it possible to ask whether a setup was recognizable before the move.

The relevant modules include:

- [`wallet_campaign.py`](../sniper/office/wallet_campaign.py): persisted preparations, campaign versions, launch records, and source cursors.
- [`wallet_launch_proof.py`](../sniper/office/wallet_launch_proof.py): receipt-bound signing developer and creator/beneficiary roles kept separate.
- [`wallet_stream_proof.py`](../sniper/office/wallet_stream_proof.py): bounded decoding and lossless evidence storage for retained confirmed stream payloads.
- [`wallet_receipt_jobs.py`](../sniper/office/wallet_receipt_jobs.py): bounded retrospective verification jobs without rewriting forward collection history.
- [`wallet_selector_identity.py`](../sniper/office/wallet_selector_identity.py): exact watched/submitted set identity rather than relying only on equal counts.

The purpose is to recognize recurring funding and setup sequences through wallet rotation. A recognizable sequence is a research lead; whether it predicts a profitable executable opportunity is a separate test.

## 4. Following supply and money

[`wallet_token_flow.py`](../sniper/office/wallet_token_flow.py) records token-account observations and explicit transfers with atomic quantities, owners, instruction identities, and known times. Campaign history and report modules assemble the evidence for review.

This enables questions such as:

1. Was most inventory acquired on a curve before migration or public attention?
2. Did later linked wallets pay a much higher average price than the initial buyers?
3. Was apparent outside demand genuinely independently funded, or did value later settle back to the same hub?
4. Were proceeds from selling inventory, creator fees, retained holdings, or a service/settlement business?
5. Which relationships could have been recognized before launch, and which were only reconstructed afterward?

Token-specific trading cash flows, cross-token fee collections, account-rent refunds, transfers, and remaining inventory must be reconciled separately. A positive observed cash-flow subtotal is not automatically whole-network net profit.

## 5. From forensics to forward research

The same machinery can work backward from successful runners and compare them with failures: recurring launch sources, early buyers, funding routes, initial supply concentration, and later outside demand.

[`wallet_campaign_quotes.py`](../sniper/office/wallet_campaign_quotes.py) records quote-based launch studies, exact quantities, and request/response timing without giving those quotes signing or fill authority. [`wallet_campaign_report.py`](../sniper/office/wallet_campaign_report.py) exposes persisted campaign evidence for review.

Research must include failed launches, dust pools, missing coverage, realistic sizing, fees, latency, and failed or unavailable exits. A recurring buyer found retrospectively is not yet a validated entry rule. Historical data, current observation health, and the ability to sell are independent requirements.

## What a reviewer can verify

The public repository includes the implementations, schema ownership, test fixtures, and tests for wallet-universe flow/capital/source handling, relationship graphs, launch proofs, campaign history, quotes, and reports. It does not publish the operator's live graph, private wallet dossiers, full retained chain corpus, or production account state.

Start with [`tests/test_wallet_universe_flow.py`](../tests/test_wallet_universe_flow.py), [`tests/test_wallet_universe_source.py`](../tests/test_wallet_universe_source.py), and [`tests/office/`](../tests/office/).
