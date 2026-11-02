"""Channel E — on-chain funding graph + synchronized-entry linkage.

Identifies the wallet's USDC funder (earliest inbound transfer), the funder's age (an
ex-ante signal — freshly-funded wallets making large concentrated bets are the shape we
watch), and coordination: other tracked wallets sharing the same funder AND entering the
same markets within a short window. Strong coordination needs BOTH shared funding and
synchronized entries.

Reuses the Polygon JSON-RPC plumbing from `conversions.py`. The pure linkage logic takes
an injectable `log_fetch` so it is testable without a node; the real default issues
`eth_getLogs` for USDC Transfers to the wallet. Degrades LOUDLY: no RPC -> dark, funder
None, explicit data_gap — never a phantom coordination claim.
"""
from __future__ import annotations

from typing import Callable

# USDC (PoS) on Polygon; ERC-20 Transfer(address,address,uint256) topic0.
USDC_POLYGON = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _norm(a: str | None) -> str:
    return (a or "").strip().lower()


def _default_log_fetch(rpc_url: str, *, from_block: str = "earliest", to_block: str = "latest",
                       max_blocks_ts: int = 50) -> Callable[[str], list[dict]]:
    """Real inbound-USDC-Transfer fetch via eth_getLogs. Returns normalized [{from, ts, value}].

    IMPORTANT: standard JSON-RPC `eth_getLogs` does NOT include a timestamp on log objects (that is
    an Etherscan extension), so we order by blockNumber and resolve real timestamps via
    `eth_getBlockByNumber` for the EARLIEST blocks only (funding happens early) — bounded to
    `max_blocks_ts` calls. Later blocks get ts=0 and are dropped by the funder selection (which
    filters truthy ts). Block range is configurable; on any RPC error the caller surfaces a
    data_gap (never a silent 'no funder')."""
    def _fetch(wallet: str) -> list[dict]:
        import httpx
        topic_to = "0x" + "0" * 24 + _norm(wallet)[2:]
        params = [{
            "address": USDC_POLYGON,
            "topics": [TRANSFER_TOPIC, None, topic_to],
            "fromBlock": from_block, "toBlock": to_block,
        }]
        try:
            with httpx.Client(timeout=20.0) as client:
                r = client.post(rpc_url, json={"jsonrpc": "2.0", "id": 1,
                                               "method": "eth_getLogs", "params": params})
                logs = (r.json() or {}).get("result") or []
                recs = []
                for lg in logs:
                    topics = lg.get("topics") or []
                    if len(topics) < 3:
                        continue
                    frm = "0x" + topics[1][-40:]
                    try:
                        value = int(lg.get("data", "0x0"), 16) / 1_000_000.0  # USDC 6-decimals
                        block = int(lg.get("blockNumber", "0x0"), 16)
                    except (TypeError, ValueError):
                        value, block = 0.0, 0
                    recs.append({"from": _norm(frm), "block": block, "value": value})
                # resolve timestamps for the earliest unique blocks only (funding is early)
                block_ts: dict[int, int] = {}
                for blk in sorted({rc["block"] for rc in recs if rc["block"]})[:max_blocks_ts]:
                    rb = client.post(rpc_url, json={"jsonrpc": "2.0", "id": 1,
                                                    "method": "eth_getBlockByNumber",
                                                    "params": [hex(blk), False]})
                    ts_hex = ((rb.json() or {}).get("result") or {}).get("timestamp")
                    if ts_hex:
                        block_ts[blk] = int(ts_hex, 16)
        except Exception as e:  # noqa: BLE001 — caller surfaces a data_gap, never silently "no funder"
            return [{"_error": f"funding graph RPC unavailable: {e}"}]
        return [{"from": rc["from"], "ts": block_ts.get(rc["block"], 0), "value": rc["value"]}
                for rc in recs]
    return _fetch


def build_funding(wallet: str, *, log_fetch: Callable[[str], list[dict]] | None = None,
                  rpc_url: str | None = None, peer_funders: dict | None = None,
                  my_entries: list | None = None, peer_entries: dict | None = None,
                  sync_window_s: int = 3600, now_ts: float | None = None,
                  exchange_addrs: set | None = None, from_block: str = "earliest") -> dict:
    """Funder + age + linkage + synchronized entries for `wallet`. Pure given injected feeds.
    `from_block` bounds the log scan (default 'earliest'; pass a hex block to limit range)."""
    data_gaps: list[str] = []
    if log_fetch is None:
        if rpc_url:
            log_fetch = _default_log_fetch(rpc_url, from_block=from_block)
        else:
            return {"funder": None, "funded_ts": None, "funder_age_days": None,
                    "linked_wallets": [], "synchronized_entries": 0,
                    "data_gaps": ["funding graph unavailable (no RPC)"]}

    raw_transfers = log_fetch(wallet) or []
    for t in raw_transfers:
        if isinstance(t, dict) and t.get("_error"):
            data_gaps.append(str(t.get("_error")))
    transfers = sorted((t for t in raw_transfers if t.get("ts")),
                       key=lambda t: t["ts"])
    excl = {_norm(a) for a in (exchange_addrs or set())}
    funder, funded_ts = None, None
    for t in transfers:
        frm = _norm(t.get("from"))
        if frm and frm not in excl:
            funder, funded_ts = frm, int(t["ts"])
            break
    if not excl:
        data_gaps.append("exchange-deposit filtering not applied")

    funder_age_days = None
    if funded_ts is not None and now_ts is not None:
        funder_age_days = (float(now_ts) - funded_ts) / 86400.0

    linked = [w for w, f in (peer_funders or {}).items()
              if _norm(f) == funder and _norm(w) != _norm(wallet)] if funder else []

    sync = 0
    if linked and my_entries and peer_entries:
        mine = [(str(c), int(ts)) for c, ts in my_entries]
        for w in linked:
            for (pc, pts) in (peer_entries.get(w) or []):
                for (mc, mts) in mine:
                    if str(pc) == mc and abs(int(pts) - mts) <= sync_window_s:
                        sync += 1
                        break

    return {"funder": funder, "funded_ts": funded_ts, "funder_age_days": funder_age_days,
            "linked_wallets": linked, "synchronized_entries": sync, "data_gaps": data_gaps}
