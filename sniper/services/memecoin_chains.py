"""Canonical memecoin chain vocabulary (spec 2026-07-30).

One resolver so `robin`, `4663` and `Robinhood` cannot address different row sets on
different endpoints — before this, `paper-report` spoke CHAIN_IDS aliases while
`wallet_recommendations` matched raw `network_id`, and passing one vocabulary to the
other returned the wrong chain's rows with no error.

There is deliberately NO "all" key: chains are never pooled. `unresolved` is a
data-quality bucket for positions whose chain could not be identified — it is listed
so those rows stay visible, and is never merged into a real chain's numbers.
"""

CHAINS = (
    {"key": "solana", "label": "Solana", "network_ids": ("solana",), "funded": True},
    {"key": "base", "label": "Base", "network_ids": ("8453", "base"), "funded": True},
    {"key": "robin", "label": "Robinhood", "network_ids": ("4663", "robin"), "funded": True},
    # `funded` is CAPABILITY — the funded implementation exists — not "enabled". BSC's landed
    # 2026-09-06 (PancakeSwap V2/V3 rungs, proxy-aware token safety); it becomes armable only when
    # 56 is in MEMECOIN_EVM_ALLOWLIST. See funded_chain_keys().
    {"key": "bsc", "label": "BSC", "network_ids": ("56", "bsc"), "funded": True},
    # Ethereum (2026-09-30, eth-mainnet spec). CAPABILITY only, like Arc and Ink: nothing trades
    # until "1" is in MEMECOIN_EVM_ALLOWLIST AND the pinned chain-1 coverage manifest is ready.
    {"key": "eth", "label": "Ethereum", "network_ids": ("1", "eth", "ethereum"), "funded": True},
    # Arc (Circle, chain_id 5042, mainnet 2026-09-16). `funded: True` is CAPABILITY only, per the
    # docstring above — it does not arm anything. Arc opens by ONE edit, adding "5042" to
    # MEMECOIN_EVM_ALLOWLIST; the default allowlist excludes it, so funded_chain_keys() omits
    # "arc" until an operator makes that edit.
    {"key": "arc", "label": "Arc", "network_ids": ("5042", "arc"), "funded": True},
    {"key": "ink", "label": "Ink", "network_ids": ("57073", "ink"), "funded": True},
    {"key": "unresolved", "label": "Unresolved", "network_ids": ("evm-unknown",), "funded": False},
)

# Extra spellings that are not themselves stored network ids.
_ALIASES = {"sol": "solana", "robinhood": "robin", "ethereum": "eth", "unknown": "unresolved"}
_BY_NETWORK_ID = {nid: c["key"] for c in CHAINS for nid in c["network_ids"]}
_BY_KEY = {c["key"]: c for c in CHAINS}


def resolve_chain(value):
    """Any alias, label or stored network id -> canonical key. Unknown or 'all' -> None."""
    v = str(value or "").strip().lower()
    if not v:
        return None
    if v in _BY_KEY:
        return v
    if v in _ALIASES:
        return _ALIASES[v]
    return _BY_NETWORK_ID.get(v)


def canonical_chain_id(value):
    """Any alias -> the network id the FUNDED runner uses ('solana', '8453', '4663', ...).

    This is the identity stored in `memecoin_wallets.real_chains` and compared against
    `identity.network_id` at the entry gate. The UI speaks 'robin'/'base'; funded events
    carry '4663'/'8453'. Storing the UI spelling meant a wallet rendered as armed while
    real_approved_user_ids('4663') returned nobody, so it could never execute.
    Returns None when the value does not resolve.
    """
    key = resolve_chain(value)
    return _BY_KEY[key]["network_ids"][0] if key else None


def network_ids_for(key):
    """Network ids as stored in memecoin_positions.network_id for a canonical key."""
    return _BY_KEY[key]["network_ids"]


def chain_label(key):
    """Human label for a canonical key, or the key itself when unrecognised."""
    entry = _BY_KEY.get(key)
    return entry["label"] if entry else str(key)


def funded_chain_keys():
    """Canonical keys the funded runner can execute on RIGHT NOW: Solana, plus every chain that
    has a funded implementation AND is enabled in `MEMECOIN_EVM_ALLOWLIST`.

    THE ALLOWLIST IS AUTHORITATIVE, THE FLAG IS ONLY CAPABILITY (2026-09-06). These were two
    hand-synced sources before: `CHAINS[...]["funded"]` said whether the code exists, the allowlist
    said whether the venue is on, and nothing kept them equal. Chain 56 shipped its full funded
    implementation on 2026-09-06 with the flag still False, so `set_wallet_chain_armed` refused
    every BSC arm with `chain_not_funded` while every other layer was ready — a chain that looked
    fundable and could not be armed. Deriving the answer means opening a chain is ONE edit
    (`MEMECOIN_EVM_ALLOWLIST`) and the two can no longer disagree.

    The flag still gates: a chain with no funded implementation (eth, and the `unresolved`
    data-quality bucket) can never be armed by putting its id in the allowlist. Solana has no EVM
    allowlist entry by construction and is always funded.
    """
    from sniper.settings import memecoin as _cfg
    allow = {str(x).strip() for x in getattr(_cfg, "MEMECOIN_EVM_ALLOWLIST", ()) if str(x).strip()}
    return tuple(c["key"] for c in CHAINS if c["funded"]
                 and (c["key"] == "solana" or allow.intersection(c["network_ids"])))
