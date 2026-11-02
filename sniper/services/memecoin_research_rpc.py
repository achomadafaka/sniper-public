"""Research Solana RPC keys: rotation, per-key usage, and the credits-exhausted page.

Research lanes read on MEMECOIN_RESEARCH_HELIUS_RPC_URLS (comma list) when it is set, else on the
single MEMECOIN_RESEARCH_HELIUS_RPC_URL. Either way the keys sit behind RotatingSolanaRpcHttp, so a
spent key rotates to the next one and the lane can report which key is in what state.

Helius credits belong to the ACCOUNT, not the key. A second key on a spent account is spent too,
which is why the page says "another account".

Nothing here asks the provider for usage. `unknown` means this process has not called the key yet.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

from sniper.execution.solana_rpc_pool import RotatingSolanaRpcHttp, key_id

logger = logging.getLogger("memecoin")

PAGE_KIND = "HELIUS-CREDITS-EXHAUSTED"
# The header the single-key path (`memecoin_solana_blocktime._rpc_call`) always sent.
_HEADERS = {"user-agent": "Mozilla/5.0 Sniper/1.0"}
_PAGES_FILE = ("data", "helius_credit_pages.json")
_pages_lock = threading.Lock()


def research_urls(env=None):
    from sniper.services.memecoin_solana_provider import provider_urls
    return list(provider_urls("research", env))


def funded_overlap(urls, env=None):
    """Key ids that are BOTH research and funded. Research load on a funded key spends the credit
    the funded reads need, so any overlap refuses the lane."""
    env = os.environ if env is None else env
    funded = [env.get("MEMECOIN_SOLANA_RPC_URL") or ""]
    funded += (env.get("MEMECOIN_SOLANA_RPC_POOL_URLS") or "").split(",")
    funded_ids = {key_id(u) for u in funded if u.strip()}
    return sorted({key_id(u) for u in urls} & funded_ids)


def page_text(lane, key):
    return (f"lane={lane} key={key} is out of credit; reads on it stop until the plan resets. "
            "Rotate or add a key on another account (credits are per account, not per key).")


def page_once(lane, key, *, send_fn=None, today=None, path=None):
    """Send the page unless this lane already paged this key today (UTC). Returns True if sent.

    The dedupe is a file because the prewarm lane is a fresh cron process every 15 minutes; a
    per-process memory would page a spent key 96 times a day."""
    from sniper.services.memecoin_alerts import send_alert
    from sniper.util.atomic import atomic_write_json
    from sniper.util.state_paths import state_path
    path = path or state_path(*_PAGES_FILE)
    today = today or datetime.now(timezone.utc).date().isoformat()
    tag = f"{lane}:{key}"
    with _pages_lock:
        try:
            with open(path) as f:
                sent = json.load(f)
        except (OSError, ValueError):
            sent = {}
        if not isinstance(sent, dict):
            sent = {}
        if sent.get(tag) == today:
            return False
        sent[tag] = today
        try:
            atomic_write_json(path, sent)
        except Exception:
            logger.exception("could not record the %s page for %s", PAGE_KIND, tag)
    send_alert(PAGE_KIND, page_text(lane, key), send_fn=send_fn)
    return True


def funded_credit_page(send_fn=None):
    """Hook for the FUNDED pool. The read that found the exhaustion may be on the entry path, so
    the file and the queue work run on their own thread and the read returns at once."""
    def on_exhausted(_idx, key):
        from sniper.services.memecoin_telegram import notification_lifecycle_active
        if send_fn is None and not notification_lifecycle_active():
            # The web builds funded contexts too. A default send there would START a second
            # notification worker and duplicate the runner's messages; the runner pages instead.
            logger.warning("%s funded key=%s (no dispatcher in this process; not paged)",
                           PAGE_KIND, key)
            return
        threading.Thread(target=page_once, args=("funded", key), kwargs={"send_fn": send_fn},
                         name="helius-credit-page", daemon=True).start()
    return on_exhausted


def build_research_call(lane, *, min_call_s=1.2, send_fn=None, http=None, env=None):
    """(call, pool) for a research lane, or (None, None) when no key is set or a key is funded.

    `call(method, params)` has the build_read_rpc_call contract: paced, one 429 retry, result or
    raise. The pool raises the last HTTPError once every key is out, so that retry still sees it.
    """
    from sniper.services.memecoin_solana_blocktime import build_read_rpc_call
    urls = research_urls(env)
    if not urls:
        return None, None
    overlap = funded_overlap(urls, env)
    if overlap:
        logger.error("%s: research key(s) %s are also FUNDED keys; refusing to start",
                     lane, ",".join(overlap))
        return None, None
    if http is None:
        from sniper.execution.solana_wallet import _http as http
    pool = RotatingSolanaRpcHttp(
        http, urls, on_credit_exhausted=lambda _idx, key: page_once(lane, key, send_fn=send_fn),
        name="research:" + lane)
    anchor = urls[0]

    def raw_call(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        response = pool("POST", anchor, headers=_HEADERS, body=body, timeout=15)
        payload = response[1] if isinstance(response, tuple) and len(response) > 1 else None
        return payload.get("result") if isinstance(payload, dict) else None

    return build_read_rpc_call(min_call_s=min_call_s, raw_call=raw_call), pool

# Operator's funded-only profile: these are actual funded dependencies, never research.
_FUNDED_DEPENDENCIES = frozenset({'slot_signal', 'pool_first_sight', 'pool_discovery',
    'signal_pool_resolver', 'coin_age', 'rh_deposit', 'tape', 'reconcile'})


def funded_dependency_url(purpose, env=None):
    """Validated neutral read for explicit funded-only Shyft dependencies; no role fallback."""
    from sniper.services.memecoin_solana_provider import infra_provider, provider_urls
    env = os.environ if env is None else env
    if purpose not in _FUNDED_DEPENDENCIES:
        raise ValueError('funded_purpose_invalid')
    if infra_provider(env) != 'shyft' or str(env.get('MEMECOIN_FUNDED_ONLY', '0')).lower() not in {
            '1', 'true', 'yes', 'on'}:
        return None
    return provider_urls('read', env)[0]


def build_dependency_read_call(purpose, *, min_call_s=1.2, send_fn=None, http=None, env=None):
    """Paced off-entry funded dependency; legacy retains the separate research guard."""
    from sniper.services.memecoin_solana_blocktime import build_read_rpc_call
    url = funded_dependency_url(purpose, env)
    if url is None:
        return build_research_call(purpose, min_call_s=min_call_s, send_fn=send_fn, http=http, env=env)
    if http is None:
        from sniper.execution.solana_wallet import _http as http
    pool = RotatingSolanaRpcHttp(http, [url], name='funded-read:' + purpose,
                               on_credit_exhausted=funded_credit_page(send_fn))
    def raw_call(method, params):
        response = pool('POST', url, headers=_HEADERS,
            body=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}), timeout=15)
        payload = response[1] if isinstance(response, tuple) and len(response) > 1 else None
        if (not isinstance(payload, dict) or 'error' in payload or 'result' not in payload
                or response[0] != 200):
            raise RuntimeError('rpc_read_failed')
        return payload['result']
    return build_read_rpc_call(min_call_s=min_call_s, raw_call=raw_call), pool
