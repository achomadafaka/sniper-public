"""Construct the shared submission backends from validated config and own their lifecycle.

``auto_free`` (the default) and ``pooled_rpc`` both route each chain through one long-lived pooled
client and the typed same-bytes transport over the already-configured custody RPC; ``auto_free``
adds local Solana TPU and, in the legacy profile, chain-owner public endpoints. Shyft Solana never
constructs public or Sender providers; explicitly enabled Frankfurt Nozomi remains available.
Explicit ``rpc`` is the no-backend rollback seam.
No backend here ever builds or signs
a transaction — the executor signs once and the transport delivers those exact bytes. ``close_all``
(also wired to ``atexit``) closes every pooled client and stops warming; warming threads are daemons
and never keep the process alive.

Endpoints are never logged here (providers carry URL-free labels).
"""
from __future__ import annotations

import atexit
import json
import time
import logging
import threading

logger = logging.getLogger(__name__)

_LOG = logging.getLogger(__name__)

# process-wide registry so a single close_all() (atexit + explicit runner shutdown) tears everything
# down. Guarded by a lock because backends for several chains build on the runner's main thread.
_LOCK = threading.Lock()
_CLIENTS = []
_TRANSPORTS = []
_WARM_STOPS = []
_WARM_THREADS = []
_HEALTH = []          # (registry_key, HealthRegistry) for the sanitized status surface (Task 8)
_SIDECARS = []        # owned TpuSidecarProcess handles (close() on shutdown)


def _register_sidecar(proc):
    with _LOCK:
        _SIDECARS.append(proc)


def submission_sidecars():
    """The owned TPU sidecar process handles (for the sanitized status surface)."""
    with _LOCK:
        return list(_SIDECARS)


def _register_client(client):
    with _LOCK:
        _CLIENTS.append(client)


def register_client(client):
    """Register an externally constructed HTTP client for process-lifecycle cleanup."""
    _register_client(client)
    return client


def _register_health(key, health):
    with _LOCK:
        _HEALTH.append((key, health))


def submission_health_snapshots():
    """Merge every registered endpoint HealthRegistry into one ``{provider_id: snapshot}`` map for the
    sanitized status/cockpit surface. URL-free, credential-free — the registry snapshots already are."""
    with _LOCK:
        registries = list(_HEALTH)
    out = {}
    for _key, health in registries:
        out.update(health.sanitized_snapshot())
    return out


def _register_transport(transport):
    with _LOCK:
        _TRANSPORTS.append(transport)


def _register_warmer(stop_event, thread):
    with _LOCK:
        _WARM_STOPS.append(stop_event)
        _WARM_THREADS.append(thread)


def close_all():
    """Stop warming and close every pooled client + transport. Idempotent; bounded."""
    with _LOCK:
        stops, threads, transports, clients, sidecars = (
            list(_WARM_STOPS), list(_WARM_THREADS), list(_TRANSPORTS), list(_CLIENTS),
            list(_SIDECARS))
        _WARM_STOPS.clear()
        _WARM_THREADS.clear()
        _TRANSPORTS.clear()
        _CLIENTS.clear()
        _HEALTH.clear()
        _SIDECARS.clear()
    for stop in stops:
        try:
            stop.set()
        except Exception:
            pass
    for thread in threads:
        try:
            thread.join(timeout=1.0)
        except Exception:
            pass
    for sidecar in sidecars:
        try:
            sidecar.close()                          # owned TPU sidecar: bounded, idempotent
        except Exception:
            pass
    for transport in transports:
        try:
            transport.close()
        except Exception:
            pass
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


atexit.register(close_all)


def _default_client_factory(*, read_timeout_s, warm_interval_s=5.0, warm_calls=1):
    from sniper.execution.fast_http import PooledHttpClient, warm_keepalive_expiry
    return PooledHttpClient(read_timeout_s=read_timeout_s,
                            keepalive_expiry_s=warm_keepalive_expiry(
                                warm_interval_s, read_timeout_s, serial_calls=warm_calls))


def _http_seam(client, timeout_s):
    def http(method, url, headers=None, body=None, timeout=None):
        return client.request(method, url, headers=headers, body=body,
                              timeout=(timeout if timeout is not None else timeout_s))
    return http


def build_solana_backend(*, keypair, custody_rpc_url, env=None, client_factory=None, http=None,
                         tpu_sidecar=None, warm_external_http=False, allow_unavailable_custody=False):
    """Build the configured Solana backend and its ``(transport, builder)`` pair.

    ``rpc`` returns the legacy no-backend pair. ``pooled_rpc`` builds the shared same-bytes transport
    over warm custody RPC only. ``auto_free`` adds the supervised local TPU sidecar (``solana:tpu``)
    and, in the legacy profile, health-gated chain-owner public RPC. Shyft excludes public RPC;
    explicitly enabled Frankfurt Nozomi joins the same-byte fanout, and so does the Sender A/B
    unless MEMECOIN_SOLANA_SENDER_WITH_SHYFT is off (2026-10-15). All modes return ``builder=None``:
    the executor signs the swap once and the transport delivers those exact bytes. A missing or
    unhealthy sidecar is degraded RPC-only ``auto_free``, never a failure. ``keypair`` is retained for
    call-site compatibility; no transaction is signed here."""
    from sniper.settings import memecoin_submission as C
    cfg = C.solana_submit_config(env)
    C._validate_solana(cfg, env)
    from sniper.services.memecoin_solana_provider import infra_provider, provider_urls
    shyft = infra_provider(env) == "shyft"
    if shyft:
        try:
            allowed_custody = provider_urls("funded", env)
        except ValueError:
            raise C.SubmissionConfigError("Shyft custody configuration is invalid") from None
        if custody_rpc_url not in allowed_custody:
            raise C.SubmissionConfigError("Shyft custody endpoint is not configured")
    if cfg.mode == "rpc":
        return None, None
    from sniper.execution.solana_fast_submit import SolanaRpcProvider
    from sniper.execution.submission_transport import SubmissionTransport
    from sniper.execution import solana_sender_ab as sender_ab

    timeout_s = cfg.timeout_ms / 1000.0
    owns_client = http is None
    if owns_client:
        make_client = client_factory or (
            lambda **kw: _default_client_factory(
                **kw, warm_interval_s=cfg.warm_interval_s,
                warm_calls=(1 + int(sender_ab.nozomi_knob_on(env))) if shyft else 3))
        client = make_client(read_timeout_s=timeout_s)
        http = _http_seam(client, timeout_s)

    custody_verdict = ('ok', 'validated')
    try:
        _validate_solana_custody(http, custody_rpc_url, timeout_s)
    except Exception as exc:
        custody_verdict = getattr(exc, 'verdict', None)
        if shyft and allow_unavailable_custody and custody_verdict and custody_verdict[0] == 'fail':
            logger.warning('Solana custody unavailable; identity-gated funded context constructed')
        else:
            if owns_client:
                try:
                    client.close()
                except Exception:
                    pass
            raise
    if owns_client:
        _register_client(client)

    providers = [SolanaRpcProvider(
        http, custody_rpc_url, provider_id="solana:custody_rpc",
        skip_preflight=False, max_retries=3, timeout_s=timeout_s)]
    warm_specs = [("solana:custody_rpc", _warm_call(http, custody_rpc_url, "getHealth"))]
    health = None
    if cfg.mode == "auto_free" and not shyft:
        from sniper.execution.free_endpoint_registry import dedupe_endpoints, free_endpoints
        from sniper.execution.submission_health import (
            HealthGatedProvider, HealthRegistry, apply_probe, probe_solana_identity,
        )
        officials = dedupe_endpoints(custody_rpc_url, free_endpoints("solana"))
        # Submission-only endpoints have no read identity method. Keep them absent from the health
        # registry so HealthGatedProvider treats them as intentionally ungated, not "unverified".
        health = HealthRegistry(
            [e.provider_id for e in officials if e.health_method != "none"])
        # Startup identity probe (getHealth + mainnet-beta genesis): a wrong-genesis endpoint is
        # hard-excluded before any send; a transient failure is re-checked by the runtime loop.
        _startup_probe(officials, health,
                       lambda e: apply_probe(health, e, probe_solana_identity(http, e, timeout_s=timeout_s)))
        _start_health_checks(
            officials, health,
            lambda e: apply_probe(
                health, e, probe_solana_identity(http, e, timeout_s=timeout_s)))
        for e in officials:
            providers.append(HealthGatedProvider(
                SolanaRpcProvider(http, e.url, provider_id=e.provider_id, skip_preflight=True,
                                  timeout_s=timeout_s), health))
        _register_health("solana", health)
    # Keep supervised TPU in the graph while cold: NOT_SENT is neutral and the monitor can restore
    # it without rebuilding transport. Shyft metadata does not transfer provider stake to QUIC.
    if cfg.mode == "auto_free" and tpu_sidecar is not None:
        _register_sidecar(tpu_sidecar)
        providers.append(tpu_sidecar.provider(health=health))
    if sender_ab.knob_on(env) and (not shyft or sender_ab.with_shyft_on(env)):
        # LAST: existing custody/public/TPU workers dispatch before the tipped auxiliary.
        providers.append(sender_ab.SenderProvider(http, timeout_s=timeout_s))
        warm_specs.append((sender_ab.PROVIDER_ID,
                           lambda: http("GET", sender_ab.SENDER_PING_URL)))
        logger.warning("solana submit: Sender A/B provider on (slot-lane tipped entries only)")
    nozomi = sender_ab.nozomi_provider(http, env=env, timeout_s=timeout_s)
    if nozomi is not None:
        providers.append(nozomi)
        warm_specs.append((sender_ab.NOZOMI_PROVIDER_ID,
                           lambda: http("GET", sender_ab.NOZOMI_PING_URL)))
        logger.warning("solana submit: Nozomi A/B provider on (Nozomi-tipped entries: %s)",
                       "slot lane + every funded entry lane (MEMECOIN_NOZOMI_ALL_LANES)"
                       if sender_ab.nozomi_all_lanes_on(env) else "slot lane only")
    custody_identity = None
    if shyft and allow_unavailable_custody:
        from dataclasses import replace
        from sniper.execution.free_endpoint_registry import FreeEndpoint, SOLANA_MAINNET_GENESIS
        from sniper.execution.submission_health import HealthRegistry, HealthGatedProvider, apply_probe, probe_solana_identity
        custody = FreeEndpoint('solana', 'solana:custody_rpc', custody_rpc_url, 'custody_rpc',
                               'getHealth+getGenesisHash', SOLANA_MAINNET_GENESIS)
        identities = tuple(replace(custody, provider_id=p.provider_id) for p in providers)
        custody_identity = HealthRegistry([p.provider_id for p in providers])
        def record_identity(verdict):
            # A proved identity survives ordinary outages; a mismatch remains excluded until restart.
            if verdict[0] == 'fail' and custody_identity.should_attempt(custody.provider_id):
                return
            with custody_identity._lock:
                for identity in identities:
                    apply_probe(custody_identity, identity, verdict)
        record_identity(custody_verdict)
        def probe_identity(endpoint):
            record_identity(probe_solana_identity(http, endpoint,
                timeout_s=max(timeout_s, _CUSTODY_PROBE_MIN_TIMEOUT_S)))
        _register_health('solana', custody_identity)
        _start_health_checks((custody,), custody_identity, probe_identity)
        providers = [HealthGatedProvider(p, custody_identity) for p in providers]
    transport = SubmissionTransport(providers, deadline_s=cfg.deadline_ms / 1000.0)
    if custody_identity is not None:
        transport.custody_ready_fn = lambda: custody_identity.should_attempt('solana:custody_rpc')
        transport.custody_http = http
    _register_transport(transport)
    if owns_client or warm_external_http:
        _start_warming(warm_specs, cfg.warm_interval_s)
    return transport, None


def build_evm_backend(chain, *, custody_rpc_url=None, env=None, client_factory=None, http=None):
    """Build the EVM submission transport. ``rpc`` returns the legacy no-backend seam. ``pooled_rpc``
    builds the shared same-bytes transport over the warm custody RPC only. ``auto_free`` additionally
    layers the chain-owner official endpoints (Base: standard + Flashblocks; Robinhood: public RPC +
    sequencer), each health-gated so a startup wrong-chain identity excludes it before any send. Only
    ``eth_sendRawTransaction`` ever runs on a provider — no signing, nonce, approval, or receipt logic
    is added here."""
    from sniper.settings import memecoin_submission as C
    cfg = C.evm_submit_config(env)
    C._validate_evm(cfg, [str(chain)], env)
    from sniper.execution.free_endpoint_registry import private_submit_endpoint
    private = private_submit_endpoint(chain)
    if private is not None:
        return _build_private_evm_backend(chain, private, cfg, custody_rpc_url=custody_rpc_url,
                                          client_factory=client_factory, http=http)
    if cfg.mode == "rpc":
        return None
    if not custody_rpc_url:
        raise C.SubmissionConfigError("pooled EVM submission requires the custody RPC URL")
    from sniper.execution.evm_fast_submit import EvmRpcProvider
    from sniper.execution.submission_transport import SubmissionTransport

    timeout_s = cfg.timeout_ms / 1000.0
    owns_client = http is None
    if owns_client:
        make_client = client_factory or (lambda **kw: _default_client_factory(**kw))
        client = make_client(read_timeout_s=timeout_s)
        http = _http_seam(client, timeout_s)

    chain = str(chain)
    try:
        _validate_evm_custody(http, custody_rpc_url, chain, timeout_s)
    except Exception:
        if owns_client:
            try:
                client.close()
            except Exception:
                pass
        raise
    if owns_client:
        _register_client(client)
    providers = [EvmRpcProvider(http, custody_rpc_url, provider_id="%s:custody_rpc" % chain,
                                timeout_s=timeout_s)]
    warm_specs = [("%s:custody_rpc" % chain, _warm_call(http, custody_rpc_url, "eth_chainId"))]
    if cfg.mode == "auto_free":
        from sniper.execution.free_endpoint_registry import dedupe_endpoints, free_endpoints
        from sniper.execution.submission_health import (
            HealthGatedProvider, HealthRegistry, apply_probe, probe_evm_identity,
        )
        officials = dedupe_endpoints(custody_rpc_url, free_endpoints(chain))
        # A submission-only endpoint intentionally has no pre-send read method. Leaving it out of
        # the registry makes it attemptable; pre-registering it would gate it forever as unverified.
        health = HealthRegistry(
            [e.provider_id for e in officials if e.health_method != "none"])
        # Startup identity probe (concurrent, bounded): a wrong-chain endpoint is hard-excluded BEFORE
        # any send; a transient failure is a soft failure the runtime loop re-checks. Never signs.
        _startup_probe(officials, health,
                       lambda e: apply_probe(health, e, probe_evm_identity(http, e, timeout_s=timeout_s)))
        _start_health_checks(
            officials, health,
            lambda e: apply_probe(
                health, e, probe_evm_identity(http, e, timeout_s=timeout_s)))
        for e in officials:
            providers.append(HealthGatedProvider(
                EvmRpcProvider(http, e.url, provider_id=e.provider_id, timeout_s=timeout_s), health))
            if e.health_method == "none":
                # A refused read warms the sending socket without granting provider identity.
                warm_specs.append((e.provider_id, _warm_call(http, e.url, "eth_chainId")))
        _register_health("%s:evm" % chain, health)
    if chain == "56":
        # The 56 builder A/B (MEMECOIN_BSC_BUILDER_AB, 2026-10-09): last, send_after_accept, and
        # posting only marked arm-B entry swaps as bundles that expire in 30 s.
        from sniper.execution import bsc_builder_ab
        builders = bsc_builder_ab.providers(http, env=env, timeout_s=timeout_s)
        if builders:
            providers.extend(builders)
            logger.warning("evm submit 56: builder A/B providers on: %s",
                           ",".join(p.provider_id for p in builders))

    transport = SubmissionTransport(providers, deadline_s=cfg.deadline_ms / 1000.0)
    _register_transport(transport)
    if owns_client:
        _start_warming(warm_specs, 5.0)
    return transport


def _build_private_evm_backend(chain, private, cfg, *, custody_rpc_url, client_factory, http):
    """A private-only chain (1): ONE provider, its pinned private endpoint, in EVERY submit mode. The
    custody RPC is identity-checked for its reads but is never a sender, and there is no fallback:
    a relay failure is ambiguous or not-sent, never a public broadcast (spec v2 §3.6)."""
    from sniper.settings import memecoin_submission as C
    if not custody_rpc_url:
        raise C.SubmissionConfigError("chain %s requires its custody RPC URL for reads" % chain)
    from sniper.execution.evm_fast_submit import EvmRpcProvider
    from sniper.execution.submission_transport import SubmissionTransport
    # Floor AND ceiling: `rpc` mode skips `_validate_evm`, so nothing else bounds these here.
    timeout_s = min(max(cfg.timeout_ms, 1000), C._MAX_DEADLINE_MS) / 1000.0
    deadline_s = min(max(cfg.deadline_ms, 1500), C._MAX_DEADLINE_MS) / 1000.0
    owns_client = http is None
    if owns_client:
        make_client = client_factory or (lambda **kw: _default_client_factory(**kw))
        client = make_client(read_timeout_s=timeout_s)
        http = _http_seam(client, timeout_s)
    try:
        _validate_evm_custody(http, custody_rpc_url, str(chain), timeout_s)
    except Exception:
        if owns_client:
            try:
                client.close()
            except Exception:
                pass
        raise
    if owns_client:
        _register_client(client)
    provider_id, url = private
    transport = SubmissionTransport(
        [EvmRpcProvider(http, url, provider_id=provider_id, timeout_s=timeout_s)],
        deadline_s=deadline_s)
    _register_transport(transport)
    return transport


def _startup_probe(endpoints, health, probe_one, *, max_workers=4):
    """Run each endpoint's read-only identity probe concurrently and feed the health registry. Bounded
    by a small pool; each ``probe_one`` is itself timeout-bounded by the http seam. Best-effort — a
    probe that raises is swallowed (the endpoint simply stays unverified, never wrongly excluded)."""
    endpoints = list(endpoints)
    if not endpoints:
        return
    from concurrent.futures import ThreadPoolExecutor, wait
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(endpoints))),
                            thread_name_prefix="memecoin-health") as ex:
        futs = [ex.submit(_safe_probe, probe_one, e) for e in endpoints]
        wait(futs)


def _safe_probe(probe_one, endpoint):
    try:
        verdict = probe_one(endpoint)
        if verdict == "wrong_chain":
            _LOG.critical("submission endpoint wrong-chain excluded provider=%s",
                          endpoint.provider_id)
        return verdict
    except Exception:
        return None


def _start_health_checks(endpoints, health, probe_one, *, interval_s=None, register=True):
    """Run identity probes off the action thread for the backend lifetime.

    The checker waits before its first pass because construction already ran the startup probe. Each
    pass is concurrent and every network call is bounded by the provider HTTP timeout. Actions only
    read the last completed HealthRegistry snapshot; they never wait on this thread.
    """
    endpoints = tuple(endpoints)
    stop = threading.Event()
    interval = float(health.health_interval_s if interval_s is None else interval_s)

    def loop():
        while not stop.wait(interval):
            _startup_probe(endpoints, health, probe_one)

    thread = threading.Thread(
        target=loop, name="memecoin-submission-health", daemon=True)
    thread.start()
    if register:
        _register_warmer(stop, thread)
    return stop, thread


# Startup identity validation: a WRONG answer and NO answer are different failures.
#
# `wrong_chain` fails instantly and unconditionally — signing against a mis-pointed RPC is the
# catastrophe this gate exists for, and no number of retries makes a wrong genesis right. But a
# transport flake is not an identity mismatch, and one probe with zero retries turned every blip
# into a full startup failure. Measured 2026-08-14: 839 failed watchdog launches, a 96-minute
# funded blackout after boot, and two deploy rollbacks (13:36Z, 13:38Z) — each from a single
# seconds-long RPC flake that answered normally when probed moments later.
_CUSTODY_PROBE_ATTEMPTS = 3
_CUSTODY_PROBE_RETRY_SLEEP_S = 2.0
# The startup identity probe is not on the submit hot path, so it must not inherit the submit
# budget. MEMECOIN_EVM_SUBMIT_TIMEOUT_MS is 1000 ms BY DESIGN — that is a broadcast deadline, not
# a read allowance. Chain 4663's custody RPC answers eth_chainId correctly in 1.9-3.7 s (8/8
# samples over 1 s, 2026-08-25), so all three attempts timed out and the runner refused to start:
# the same outage shape as 2026-08-14, from latency instead of a flake. A SLOW endpoint is not a
# WRONG endpoint. The gate keeps its teeth: `wrong_chain` still rejects instantly and
# unconditionally below, and an endpoint that never answers still fails closed.
_CUSTODY_PROBE_MIN_TIMEOUT_S = 5.0


def _endpoint_host(endpoint):
    """Host only, for error strings that are logged and paged. A custody URL may carry an API key in
    its path or query (Alchemy does), so the url itself must never reach a log line. Distinct from
    `free_endpoint_registry._norm_url`, which deliberately KEEPS path and query for de-duplication
    and is therefore not safe to print."""
    from urllib.parse import urlsplit
    try:
        return urlsplit(str(getattr(endpoint, "url", "") or "")).hostname or "?"
    except Exception:
        return "?"


def _probe_custody_with_retry(probe, http, endpoint, timeout_s, label):
    # The verdict travels into the exception. Both exits used to raise the SAME string, so an
    # exhausted `http_429` was reported in the exact words of a genuine `wrong_chain` -- the probe
    # KNEW which it was and discarded it at the boundary. On 2026-09-01 that cost a funded deploy:
    # a transient 429 on the 4663 custody RPC read as an identity failure, and it was only ruled out
    # by noticing the SAME error on a release already running healthily. A second observation should
    # not be needed to tell a rate limit from a wrong chain. Diagnostic only: every raise/return
    # decision below is unchanged.
    from sniper.settings.memecoin_submission import SubmissionConfigError
    probe_timeout_s = max(float(timeout_s), _CUSTODY_PROBE_MIN_TIMEOUT_S)
    verdict = ("fail", "unprobed")
    for attempt in range(_CUSTODY_PROBE_ATTEMPTS):
        verdict = probe(http, endpoint, timeout_s=probe_timeout_s)
        if verdict[0] == "ok":
            return
        if verdict[0] == "skip":
            # `health_method='none'` -- a submission-only endpoint with no read identity. Custody
            # endpoints always set a health method, so this is unreachable today; without the branch
            # a future one would fall through and spend every retry before refusing startup, which
            # reads as an outage rather than as "no identity check is configured".
            raise SubmissionConfigError(
                "%s validation failed: endpoint declares no identity check (health_method='none')"
                % label)
        if verdict[0] == "wrong_chain":
            exc = SubmissionConfigError(
                "%s validation failed: wrong_chain, received %s" % (label, verdict[1]))
            exc.verdict = verdict
            raise exc
        if attempt + 1 < _CUSTODY_PROBE_ATTEMPTS:
            time.sleep(_CUSTODY_PROBE_RETRY_SLEEP_S)
    # NEVER the url: a custody endpoint may carry an API key in its path or query, and this string
    # is logged and paged. The host alone identifies which endpoint refused.
    exc = SubmissionConfigError(
        "%s validation failed: %s after %d attempts (host %s). A transient rate limit or timeout "
        "here is NOT an identity mismatch -- re-probe the endpoint before suspecting the release."
        % (label, verdict[1] or verdict[0], _CUSTODY_PROBE_ATTEMPTS, _endpoint_host(endpoint)))
    exc.verdict = verdict
    raise exc


def _validate_solana_custody(http, url, timeout_s):
    from sniper.execution.free_endpoint_registry import FreeEndpoint, SOLANA_MAINNET_GENESIS
    from sniper.execution.submission_health import probe_solana_identity
    endpoint = FreeEndpoint(
        "solana", "solana:custody_rpc", url, "custody_rpc",
        "getHealth+getGenesisHash", SOLANA_MAINNET_GENESIS)
    _probe_custody_with_retry(
        probe_solana_identity, http, endpoint, timeout_s, "custody Solana identity")


def _validate_evm_custody(http, url, chain, timeout_s):
    from sniper.execution.free_endpoint_registry import FreeEndpoint
    from sniper.execution.submission_health import probe_evm_identity
    from sniper.settings.memecoin_submission import SubmissionConfigError
    try:
        expected = hex(int(str(chain), 10))
    except ValueError:
        raise SubmissionConfigError("custody EVM identity validation failed") from None
    endpoint = FreeEndpoint(
        str(chain), "%s:custody_rpc" % chain, url, "custody_rpc", "eth_chainId", expected)
    try:
        _probe_custody_with_retry(
            probe_evm_identity, http, endpoint, timeout_s, "custody EVM identity")
        return
    except SubmissionConfigError as exc:
        verdict = getattr(exc, "verdict", ("fail", ""))
        if verdict[0] != "fail":
            raise                          # wrong_chain / no identity check: never masked
        custody_exc = exc
    # A DARK CUSTODY ENDPOINT MUST NOT KEEP THE RUNNER DOWN WHEN THE POOL CAN PROVE THE CHAIN
    # (2026-09-03). The Alchemy custody endpoint died (SSL handshake timeout) with an open funded
    # position; this gate then refused every watchdog relaunch for the whole outage while two
    # healthy, identity-checked pool endpoints answered eth_chainId in 60-190ms. Reads already
    # rotate through the pool (`_rpc`) and auto_free broadcast already fans out to the chain-owner
    # public endpoints, so the runner could have run the entire time. The proof this gate exists
    # for is the CHAIN ID, and a pool member proving it is the same proof from a different host.
    #
    # Fail-closed stays fail-closed: a wrong_chain from custody still raises above and is never
    # masked by a healthy alternate; a pool member answering the wrong chain is marked disproven
    # and skipped; and if no member proves the chain, the ORIGINAL transport error is re-raised
    # unchanged. The dark custody endpoint is quarantined so `choose`/`_rpc` route around it until
    # it is re-proven, and the event is logged loudly by host, never by url.
    from sniper.execution import evm_rpc_pool as _pool
    for cand in _pool.pool_for(url):
        if cand == url:
            continue
        alt = FreeEndpoint(
            str(chain), "%s:pool_rpc" % chain, cand, "pool_rpc", "eth_chainId", expected)
        v = probe_evm_identity(http, alt, timeout_s=max(float(timeout_s), _CUSTODY_PROBE_MIN_TIMEOUT_S))
        if v[0] == "ok":
            _pool.mark_identity(cand, True)
            _pool.penalize(url, reason="custody_dark_at_startup")
            logger.warning(
                "custody EVM endpoint unreachable at startup (host %s): chain %s identity proven by "
                "pool endpoint (host %s); starting with custody quarantined until it answers",
                _endpoint_host(endpoint), chain, _endpoint_host(alt))
            return
        if v[0] == "wrong_chain":
            _pool.mark_identity(cand, False)
    raise custody_exc


def _warm_call(http, url, method):
    """A bounded, sign-free keep-alive probe. Never submits; a failure only marks the provider down."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method})

    def warm():
        http("POST", url, body=body)
    return warm


def _start_warming(warm_specs, interval_s):
    if not warm_specs:
        return
    from sniper.execution.fast_http import start_warming
    stop = threading.Event()
    thread = start_warming(warm_specs, interval_s=interval_s, stop_event=stop)
    _register_warmer(stop, thread)
