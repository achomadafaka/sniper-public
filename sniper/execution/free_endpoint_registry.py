"""Immutable registry of official, chain-owner free submission endpoints.

One code-pinned, chain-keyed table of the ONLY free public endpoints ``auto_free`` may use. Each entry
is a URL-free-labelled front door for delivering already-signed bytes — never a signer, never a
confirmation authority. Endpoints are the chain owners' own documented public / preconfirm / sequencer
services. This table is changed only by code review + a pinned deploy — never from a remote document
or an operator-supplied list. Provider labels (``8453:base_standard``), never URLs, are what gets
persisted or logged.

Identity, verified live 2026-07-25/26:
- EVM public + preconfirm endpoints are identity-checked by ``eth_chainId`` (Base ``0x2105``,
  Robinhood ``0x1237``);
- Solana's public RPC by ``getHealth`` + ``getGenesisHash`` (mainnet-beta genesis);
- the Robinhood **sequencer** answers NO read method (eth_chainId / net_version / eth_blockNumber all
  return -32601) while accepting ``eth_sendRawTransaction``. It therefore carries
  ``health_method="none"``: it cannot be identity-checked before a send and is validated only by the
  normal chain receipt + balance reconciliation (design §9). It is an additional delivery target that
  can never alone confirm a transaction — a 4663-signed tx would not land on any other chain, and
  confirmation is always read from the custody 4663 RPC.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

# Solana mainnet-beta genesis hash — verified live 2026-07-25 via getGenesisHash.
SOLANA_MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
BASE_CHAIN_IDENTITY = "0x2105"          # 8453
BSC_CHAIN_IDENTITY = "0x38"             # 56
ROBINHOOD_CHAIN_IDENTITY = "0x1237"     # 4663
ARC_CHAIN_IDENTITY = "0x13b2"           # 5042
INK_CHAIN_IDENTITY = "0xdef1"            # 57073


# PRIVATE-ONLY SUBMISSION (operator, 2026-09-30; eth-mainnet spec v2 §3.6). Chain-1 signed bytes go
# to Flashbots Protect fast (all builders) and NOWHERE else: never custody, never a public endpoint, in
# every MEMECOIN_EVM_SUBMIT_MODE, failing closed. An approved deviation from the chain-owner-only rule
# above, for chain 1 alone: public-mempool buys on L1 memecoins get sandwiched. Free and keyless;
# answered eth_chainId 0x1 on 2026-09-30.
PRIVATE_ONLY_SUBMIT = {
    "1": ("1:flashbots_protect_fast", "https://rpc.flashbots.net/fast"),
}


def private_submit_endpoint(chain_id):
    """(provider_id, url) of a private-only chain's single submission endpoint, else None."""
    return PRIVATE_ONLY_SUBMIT.get(str(chain_id))


def private_only_refusal(raw_hex, url):
    """A reason when these signed bytes belong to a private-only chain and `url` is not its private
    endpoint (nothing is sent), else None."""
    from sniper.execution.submission_wiring import evm_raw_chain_id
    chain = evm_raw_chain_id(raw_hex)
    endpoint = PRIVATE_ONLY_SUBMIT.get(str(chain)) if chain is not None else None
    if endpoint is None or str(url or "").rstrip("/") == endpoint[1].rstrip("/"):
        return None
    return "chain-%s bytes are private-only (%s); refused a send to another endpoint" % (
        chain, endpoint[0])


@dataclass(frozen=True)
class FreeEndpoint:
    chain_id: str
    provider_id: str          # URL-free label, e.g. "8453:base_standard"
    url: str
    role: str                 # public_rpc | preconfirm_rpc | sequencer
    health_method: str        # eth_chainId | getHealth+getGenesisHash | none
    expected_identity: str    # identity the health check must confirm (informational when 'none')


_REGISTRY = {
    "solana": (
        FreeEndpoint("solana", "solana:public_rpc", "https://api.mainnet-beta.solana.com",
                     "public_rpc", "getHealth+getGenesisHash", SOLANA_MAINNET_GENESIS),
    ),
    "8453": (
        FreeEndpoint("8453", "8453:base_standard", "https://mainnet.base.org",
                     "public_rpc", "eth_chainId", BASE_CHAIN_IDENTITY),
        FreeEndpoint("8453", "8453:base_flashblocks", "https://mainnet-preconf.base.org",
                     "preconfirm_rpc", "eth_chainId", BASE_CHAIN_IDENTITY),
    ),
    # 56 (2026-09-06 census). Chain-owner dataseed plus publicnode, both proved 0x38. publicnode is
    # ~80ms against the dataseed's 234-370ms but 403s without the runner's user agent, the same trap
    # as Base. `bsc.drpc.org` rate-limits on the first request and `binance.llamarpc.com` does not
    # resolve; neither belongs in a list a funded path reads.
    "56": (
        FreeEndpoint("56", "56:bsc_dataseed", "https://bsc-dataseed.bnbchain.org",
                     "public_rpc", "eth_chainId", BSC_CHAIN_IDENTITY),
        FreeEndpoint("56", "56:publicnode", "https://bsc-rpc.publicnode.com",
                     "public_rpc", "eth_chainId", BSC_CHAIN_IDENTITY),
    ),
    "4663": (
        FreeEndpoint("4663", "4663:public_rpc", "https://rpc.mainnet.chain.robinhood.com",
                     "public_rpc", "eth_chainId", ROBINHOOD_CHAIN_IDENTITY),
        FreeEndpoint("4663", "4663:sequencer", "https://sequencer.mainnet.chain.robinhood.com",
                     "sequencer", "none", ROBINHOOD_CHAIN_IDENTITY),
    ),
    # 5042 (2026-09-16). Arc's own public RPC, eth_chainId 0x13b2 verified live. Without it the only
    # delivery target was custody publicnode, which answered 200 to the BANK entry and never admitted
    # it (neither node knew the hash; nonce stayed 0).
    "5042": (
        FreeEndpoint("5042", "5042:public_rpc", "https://rpc.mainnet.arc.io",
                     "public_rpc", "eth_chainId", ARC_CHAIN_IDENTITY),
    ),
    "57073": (
        FreeEndpoint("57073", "57073:gelato", "https://rpc-gel.inkonchain.com",
                     "public_rpc", "eth_chainId", INK_CHAIN_IDENTITY),
        FreeEndpoint("57073", "57073:quicknode", "https://rpc-qnd.inkonchain.com",
                     "public_rpc", "eth_chainId", INK_CHAIN_IDENTITY),
    ),
}


def free_endpoints(chain_id) -> tuple:
    """The official free endpoints for a chain id (``"solana"`` / ``"8453"`` / ``"4663"``). Unknown
    chains have none — the free backend never invents an endpoint."""
    return _REGISTRY.get(str(chain_id), ())


def _norm_url(url):
    """Normalize for de-duplication: lowercase scheme + host, drop a trailing path slash. Keeps path
    and query so two genuinely different endpoints on one host are not collapsed."""
    p = urlsplit(str(url or "").strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, ""))


def dedupe_endpoints(custody_url, entries) -> tuple:
    """Drop any official endpoint whose normalized URL equals the custody URL or an earlier entry, so
    the warm custody provider is never double-dialed and the official list carries no repeats."""
    seen = {_norm_url(custody_url)} if custody_url else set()
    out = []
    for e in entries:
        key = _norm_url(e.url)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return tuple(out)
