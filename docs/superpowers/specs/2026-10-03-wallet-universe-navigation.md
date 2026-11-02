# Wallet universe navigation

2026-10-04 measurement amendment: the [measured-size contract](2026-10-04-wallet-universe-measurements.md)
adds liquid capital and descriptive post-buy quote flow as selectable size modes. Current proved
addresses, complete balance components and fresh reads are required. Flow comparison requires ten
complete distinct coins and 75% eligible coverage; individual completed windows remain inspectable
before that threshold. These observations never create funded authority or causal impact claims.
The navigation, following and radial timing contracts below are preserved. Capital measurement is
now owned by a separate pinned observer rather than being an unsolved navigation edge.

Approved by the operator on 2026-10-03: ship the navigable orbital preview into the live Wallet Map.

The per-chain map uses the live roster from `/api/memecoin/wallet-map/universe` and the existing read-only Solana herders endpoint. No snapshot from the design preview is shipped. Chain tabs come from the API; graph data remains Solana-only. There are no new RPC requests, funded-runner calls, or writes in the map read path.

Planets represent roster wallets. Size measures named repeat buyers under the existing k/n rules; unknown following stays unknown. Satellites are observed repeat buyers, not claimed owners or bots. A selected shared satellite can reveal every measured wallet system it connects.

Distance from the center describes median share of unique decoded peers buying earlier in the same sampled first-buy slot, with at least ten usable coin samples. It does not measure global coin entry timing. Each peer contributes its first indexed buy once. Missing source order and slots without other indexed buyers are excluded rather than treated as early-zero. The outer belt is unranked. Capital, funding ownership, and causal coin impact are not synthesized into size or timing.

Click planets, their labels, or satellites to inspect them. Search and Browse cover every wallet. Drag pans; cursor-anchored wheel zoom, zoom buttons, keyboard navigation, reset, focus, pinch handling, and an expanded viewport use the same camera. Dragging suppresses the trailing selection click. Same-chain refresh retains selection and camera; chain switching clears old satellites and ignores late responses from other chains. Missing graph data and stale observations have visible status.

Existing arm/trial/retire controls remain in the wallet inspector, with the existing confirmation, CSRF, authority, and server validation. Detailed policy and origin evidence is collapsed beneath them. The map cannot create authority from missing measurements. On phones, explicit system exploration and connection inspection bring the map into view; background refreshes do not scroll the page.

Edges not solved: cross-chain relationship collection, wallet capital measurement, proven funding/bot ownership, global early-entry rank, or causal coin impact. Physical two-finger device behavior needs a device check; pinch camera math has regression coverage.

Rollback uses the previous pinned web release. Versioned helper assets and their script order ship with the template; the funded runner is not restarted.

Verification:

```bash
node --test tests/wallet_universe_camera.test.mjs
python3 -m pytest tests/test_wallet_universe_navigation.py tests/test_memecoin_wallet_map.py tests/test_memecoin_wallet_map_ui.py tests/test_wallet_universe.py tests/test_wallet_map_actions.py tests/test_web_server_asset_paths.py tests/test_memecoin_ui_render_runtime.py -q
```
