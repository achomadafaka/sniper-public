"""negRisk CONVERSION accounting — pure parsing of on-chain conversion receipts
into per-sub-market legs.

Mechanics (Polymarket NegRiskAdapter, verified 2026-06-15 against contract source
+ a real receipt — see docs/audits/2026-06-15-negrisk-conversion-feasibility.md):
`convertPositions(indexSet, amount)` over an n-question event burns NO on the m
set-bit outcomes (`amount` each) and mints YES on the n-m unset outcomes (`amount`
each), returning `(m-1)*amount` collateral. The Data-API row lacks indexSet/amount,
so we read the on-chain receipt: the NET ERC-1155 transfers to/from the analyzed
wallet ARE the per-leg truth (adapter internals — complete-set mints to the adapter
— are ignored). Token id -> (sub-market, outcome) comes from Gamma `clobTokenIds`.

This module is pure (no network); fetchers live separately.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

# ERC-1155 / ERC-20 event topic0 signatures
_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
_TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
_ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_SHARE_DECIMALS = 1_000_000.0  # Polymarket shares + USDC are 6-decimal


@dataclass
class ConversionLeg:
    condition_id: str
    outcome_index: int
    outcome: str            # "Yes" | "No"
    outcome_label: str      # negRisk sub-market groupItemTitle
    share_delta: float      # + minted to wallet, - burned from wallet


@dataclass
class ConversionEffect:
    tx: str
    wallet: str
    yes_minted: list[ConversionLeg] = field(default_factory=list)   # received (delta > 0)
    no_burned: list[ConversionLeg] = field(default_factory=list)    # sent     (delta < 0)
    collateral_usdc: float = 0.0       # net USDC to wallet == (m-1)*amount
    amount: float = 0.0                # convert amount (max |share delta|)
    m_no_burned: int = 0
    n_outcomes: int = 0
    event_slug: str = ""               # negRisk event slug (for episode categorization)


def build_token_map(submarkets: list[dict]) -> dict[int, tuple[str, int, str]]:
    """clobToken id -> (condition_id, outcome_index, sub-market label).

    `submarkets` is the Gamma event `markets[]` (conditionId, groupItemTitle,
    clobTokenIds [yes_id, no_id], outcomes [Yes, No])."""
    tm: dict[int, tuple[str, int, str]] = {}
    for m in submarkets:
        cid = m.get("conditionId")
        clob = m.get("clobTokenIds")
        if isinstance(clob, str):
            clob = json.loads(clob)
        label = m.get("groupItemTitle") or m.get("question") or ""
        for idx, tid in enumerate(clob or []):
            tm[int(tid)] = (cid, idx, label)
    return tm


def _addr_eq(topic: str, wallet: str) -> bool:
    return topic[-40:].lower() == wallet[-40:].lower()


def _words(data_hex: str) -> list[str]:
    h = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def _erc1155_moves(log: dict) -> list[tuple[int, int]]:
    """Return [(token_id, value)] for a TransferSingle/Batch log (raw integer units)."""
    t0 = log["topics"][0]
    w = _words(log["data"])
    if t0 == _TRANSFER_SINGLE:
        return [(int(w[0], 16), int(w[1], 16))]
    if t0 == _TRANSFER_BATCH:
        off_ids = int(w[0], 16) // 32
        off_vals = int(w[1], 16) // 32
        n_ids = int(w[off_ids], 16)
        ids = [int(w[off_ids + 1 + i], 16) for i in range(n_ids)]
        n_vals = int(w[off_vals], 16)
        vals = [int(w[off_vals + 1 + i], 16) for i in range(n_vals)]
        return list(zip(ids, vals))
    return []


def parse_conversion_effect(receipt_logs: list[dict], wallet: str,
                            token_map: dict[int, tuple[str, int, str]]) -> ConversionEffect:
    """Net the wallet's ERC-1155 position transfers + USDC delta in a conversion tx.

    Positive share delta = minted to the wallet (YES); negative = burned/sent (NO).
    Adapter-internal transfers (wallet not party) are ignored."""
    tx = receipt_logs[0].get("transactionHash", "") if receipt_logs else ""
    share_delta: dict[int, float] = {}
    usdc_delta = 0.0
    for log in receipt_logs:
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0]
        if t0 in (_TRANSFER_SINGLE, _TRANSFER_BATCH):
            frm, to = topics[2], topics[3]
            to_w, from_w = _addr_eq(to, wallet), _addr_eq(frm, wallet)
            if not (to_w or from_w):
                continue
            sign = 1.0 if to_w else -1.0
            for tid, val in _erc1155_moves(log):
                share_delta[tid] = share_delta.get(tid, 0.0) + sign * val / _SHARE_DECIMALS
        elif t0 == _ERC20_TRANSFER:
            frm, to = topics[1], topics[2]
            val = int(log["data"], 16) / _SHARE_DECIMALS
            if _addr_eq(to, wallet):
                usdc_delta += val
            elif _addr_eq(frm, wallet):
                usdc_delta -= val

    yes_minted: list[ConversionLeg] = []
    no_burned: list[ConversionLeg] = []
    for tid, delta in share_delta.items():
        if abs(delta) < 1e-9:
            continue
        cid, oidx, label = token_map.get(tid, ("", -1, "?unmapped"))
        outcome = "Yes" if oidx == 0 else ("No" if oidx == 1 else "?")
        leg = ConversionLeg(condition_id=cid, outcome_index=oidx, outcome=outcome,
                            outcome_label=label, share_delta=delta)
        (yes_minted if delta > 0 else no_burned).append(leg)

    amount = max((abs(l.share_delta) for l in yes_minted + no_burned), default=0.0)
    return ConversionEffect(
        tx=tx, wallet=wallet, yes_minted=yes_minted, no_burned=no_burned,
        collateral_usdc=usdc_delta, amount=amount,
        m_no_burned=len(no_burned),
        n_outcomes=len({cid for cid, _, _ in token_map.values()}),
    )


# --- fetchers (network; cached; injectable for CI) -------------------------------------

def _polygon_rpc_url() -> str | None:
    url = os.getenv("POLYGON_RPC_URL")
    if url:
        return url
    try:  # fallback: read repo .env without importing web_settings
        from sniper.analysis.winning_wallet_thesis.ledger import CACHE_DIR
        root = os.path.dirname(os.path.dirname(CACHE_DIR))   # .../<repo>/runs/wallet_anatomy -> <repo>
        for line in open(os.path.join(root, ".env")):
            if line.startswith("POLYGON_RPC_URL="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _rpc_receipt(client, rpc_url: str, tx: str) -> dict:
    # Reuse a pooled client across all receipts — a fresh client per tx triggers a DNS
    # lookup each time and overloads the local resolver on a 300+ tx wallet.
    r = client.post(rpc_url, json={"jsonrpc": "2.0", "id": 1,
                                   "method": "eth_getTransactionReceipt", "params": [tx]})
    r.raise_for_status()
    return r.json()["result"]


def _gamma_event_submarkets(slug: str, session=None) -> list[dict]:
    from sniper.markets import polymarket_api as P
    s = session if session is not None else P.build_session()
    data = P._request_json(s, f"{P.GAMMA_BASE_URL}/events",
                           params={"slug": slug, "limit": 100, "closed": "true"})
    ev = next((e for e in (data or []) if str(e.get("slug") or "") == slug),
              (data or [None])[0])
    return (ev or {}).get("markets") or []


def load_conversions(raw_activity: list[dict], *, wallet: str, refresh: bool = False,
                     rpc_url: str | None = None, receipt_fetch=None, submarket_fetch=None,
                     cache_dir: str | None = None) -> list[tuple[int, ConversionEffect]]:
    """Parse the wallet's negRisk CONVERSION activity into time-stamped ConversionEffects.

    For each CONVERSION row: fetch the tx receipt (per-leg truth) + the Gamma event sub-markets
    (token->sub-market map), parse the net wallet effect. Receipts + sub-markets are cached per
    wallet under runs/wallet_anatomy/. Transient fetch failures skip that conversion (retried
    next run), never crash enrichment. Returns [] with no network if the wallet has no
    conversions. Network calls are injectable (receipt_fetch / submarket_fetch) for CI."""
    rows = [a for a in raw_activity if a.get("type") == "CONVERSION" and a.get("transactionHash")]
    if not rows:
        return []
    from sniper.analysis.winning_wallet_thesis.ledger import CACHE_DIR
    cache_dir = cache_dir or CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    rcpt_path = os.path.join(cache_dir, f"{wallet.lower()}_conversion_receipts.json")
    subm_path = os.path.join(cache_dir, f"{wallet.lower()}_conversion_submarkets.json")
    receipts: dict[str, dict] = {}
    submarkets: dict[str, list] = {}
    if not refresh:
        for path, into in ((rcpt_path, receipts), (subm_path, submarkets)):
            if os.path.exists(path):
                with open(path) as f:
                    into.update(json.load(f))

    import time
    _client = _session = None
    if receipt_fetch is None:
        url = rpc_url or _polygon_rpc_url()
        if url is None:
            print("  conversions: no POLYGON_RPC_URL — skipping (unreconciled)", file=sys.stderr)
            return []
        import httpx
        _client = httpx.Client(timeout=45)                          # one pooled client for all receipts
        receipt_fetch = lambda tx: _rpc_receipt(_client, url, tx)   # noqa: E731
    if submarket_fetch is None:
        from sniper.markets import polymarket_api as P
        _session = P.build_session()
        submarket_fetch = lambda slug: _gamma_event_submarkets(slug, _session)  # noqa: E731

    def _flush(paths_data):
        for path, data in paths_data:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)

    effects: list[tuple[int, ConversionEffect]] = []
    dirty_r = dirty_s = False
    n_fetched = 0
    try:
        for row in rows:
            slug = row.get("eventSlug") or ""
            tx = row.get("transactionHash")
            if slug not in submarkets:
                try:
                    submarkets[slug] = submarket_fetch(slug); dirty_s = True
                except Exception as e:  # noqa: BLE001 — transient; skip, retry next run
                    print(f"  conversions: submarket fetch failed {slug} {type(e).__name__}", file=sys.stderr)
                    continue
            if tx not in receipts:
                try:
                    receipts[tx] = receipt_fetch(tx); dirty_r = True
                    n_fetched += 1
                    if _client is not None:
                        time.sleep(0.05)                            # gentle pacing; pooled conn, low DNS pressure
                    if n_fetched % 50 == 0:                         # checkpoint so progress survives flaky egress
                        _flush([(rcpt_path, receipts), (subm_path, submarkets)])
                except Exception as e:  # noqa: BLE001
                    print(f"  conversions: receipt fetch failed {tx[:12]} {type(e).__name__}", file=sys.stderr)
                    continue
            rec = receipts.get(tx) or {}
            logs = rec.get("logs") or []
            tm = build_token_map(submarkets.get(slug) or [])
            eff = parse_conversion_effect(logs, wallet, tm)
            eff.event_slug = slug
            if eff.yes_minted or eff.no_burned:
                effects.append((int(row.get("timestamp") or 0), eff))
    finally:
        if dirty_r or dirty_s:
            _flush([(rcpt_path, receipts), (subm_path, submarkets)])
        if _client is not None:
            _client.close()
    return effects
