// ==UserScript==
// @name         Memecoin X Source-Graph Capture
// @namespace    sniper.memecoin.x
// @version      1.5
// @description  Read-only bounded Following-feed capture to a loopback receiver.
// @match        https://x.com/home
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==
//
// Run first: python3 scripts/run_memecoin_x_receiver.py
// Open x.com/home on Following in the dedicated burner session. This userscript performs only
// DOM reads, fixed-height scrolls, and timed reloads. It never clicks or takes account actions.
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    const request = typeof GM_xmlhttpRequest === 'function'
      ? GM_xmlhttpRequest
      : root.GM_xmlhttpRequest;
    api.startBrowserCapture(root, request);
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const RECEIVER = 'http://127.0.0.1:8787/x/cards';
  const MAX_CARDS = 50;
  const MAX_SCROLLS = 3;
  const NEW_MIN_MS = 5 * 60 * 1000;
  const NEW_MAX_MS = 8 * 60 * 1000;
  const IDLE_MIN_MS = 10 * 60 * 1000;
  const IDLE_MAX_MS = 15 * 60 * 1000;
  const PAGE_UNREADY_MS = 15 * 60 * 1000;
  const DELIVERY_RETRY_MS = 60 * 1000;
  const QUIET_MS = 90 * 1000;
  const LEASE_MS = 120 * 1000;
  const LEASE_RETRY_MS = 45 * 1000;
  const LEASE_RENEW_MS = 45 * 1000;
  const DELIVERED_KEY = 'mcx_delivered_fingerprints_v1';
  const SENTINEL_KEY = 'mcx_latest_sentinel_v1';
  const SCHEDULE_KEY = 'mcx_schedule_state_v1';
  const LEASE_KEY = 'mcx_capture_lease_v1';
  const TAB_ID_KEY = 'mcx_capture_tab_id_v1';
  const ACK_STATUSES = new Set([
    'ok', 'no_new', 'coverage_gap', 'wrong_feed', 'logged_out', 'challenge', 'rate_limited',
    'page_unready',
  ]);

  function parseArray(raw) {
    try {
      const value = JSON.parse(raw || '[]');
      return Array.isArray(value) ? value : [];
    } catch (_) {
      return [];
    }
  }

  function hashText(value) {
    let hash = 2166136261;
    const text = String(value || '');
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16).padStart(8, '0');
  }

  function fingerprint(id, html) {
    return `${id}:${hashText(html)}`;
  }

  function makeRunId(now, random) {
    return `mcx-${Math.floor(now())}-${Math.floor(random() * 1000000)}`;
  }

  function windowDelay(minMs, maxMs, random) {
    const draw = Math.max(0, Math.min(1, Number(random())));
    return Math.round(minMs + draw * (maxMs - minMs));
  }

  function chooseSchedule({
    capture, acknowledged, recoveryUsed = false, pageUnreadyCount = 0,
    random = Math.random,
  }) {
    if (!acknowledged) {
      return {
        delayMs: DELIVERY_RETRY_MS, cadenceBucket: 'delivery_retry_60s',
        recoveryUsed, pageUnreadyCount, frozen: false,
      };
    }
    const status = capture?.status;
    if (['wrong_feed', 'logged_out', 'challenge', 'rate_limited'].includes(status)) {
      return {
        delayMs: null, cadenceBucket: 'frozen', recoveryUsed,
        pageUnreadyCount, frozen: true,
      };
    }
    if (status === 'page_unready') {
      const failures = pageUnreadyCount + 1;
      return failures >= 2
        ? {delayMs: null, cadenceBucket: 'frozen', recoveryUsed,
          pageUnreadyCount: failures, frozen: true}
        : {delayMs: PAGE_UNREADY_MS, cadenceBucket: 'page_unready_15m', recoveryUsed,
          pageUnreadyCount: failures, frozen: false};
    }
    if (status === 'coverage_gap') {
      return recoveryUsed
        ? {delayMs: windowDelay(IDLE_MIN_MS, IDLE_MAX_MS, random),
          cadenceBucket: 'gap_10_15m', recoveryUsed: true,
          pageUnreadyCount: 0, frozen: false}
        : {delayMs: windowDelay(NEW_MIN_MS, NEW_MAX_MS, random),
          cadenceBucket: 'recovery_5_8m', recoveryUsed: true,
          pageUnreadyCount: 0, frozen: false};
    }
    const resetRecovery = capture?.sentinel_found === true || !capture?.prior_sentinel;
    const nextRecovery = resetRecovery ? false : recoveryUsed;
    if ((capture?.cards || []).length > 0) {
      return {
        delayMs: windowDelay(NEW_MIN_MS, NEW_MAX_MS, random),
        cadenceBucket: 'new_5_8m', recoveryUsed: nextRecovery,
        pageUnreadyCount: 0, frozen: false,
      };
    }
    return {
      delayMs: windowDelay(IDLE_MIN_MS, IDLE_MAX_MS, random),
      cadenceBucket: 'idle_10_15m', recoveryUsed: nextRecovery,
      pageUnreadyCount: 0, frozen: false,
    };
  }

  function remainingQuietMs({nowMs, lastTrustedInputMs, quietMs = QUIET_MS}) {
    if (lastTrustedInputMs === null || lastTrustedInputMs === undefined) return 0;
    return Math.max(0, quietMs - Math.max(0, nowMs - lastTrustedInputMs));
  }

  function createTabLease({
    sharedStorage, sessionStorage, now = Date.now, random = Math.random,
    leaseMs = LEASE_MS,
  }) {
    let tabId = null;
    try {
      tabId = sessionStorage?.getItem(TAB_ID_KEY);
      if (!tabId) {
        tabId = `mcx-tab-${Math.floor(now())}-${Math.floor(random() * 1000000)}`;
        sessionStorage?.setItem(TAB_ID_KEY, tabId);
      }
    } catch (_) {
      tabId = null;
    }

    function claim() {
      if (!sharedStorage || !tabId) return false;
      try {
        const nowMs = Number(now());
        let current = null;
        try {
          current = JSON.parse(sharedStorage.getItem(LEASE_KEY) || 'null');
        } catch (_) {
          current = null;
        }
        const currentExpiry = Number(current?.expiresAt);
        if (current?.owner && current.owner !== tabId &&
            Number.isFinite(currentExpiry) && currentExpiry > nowMs) {
          return false;
        }
        sharedStorage.setItem(LEASE_KEY, JSON.stringify({
          owner: tabId, expiresAt: nowMs + leaseMs,
        }));
        const committed = JSON.parse(sharedStorage.getItem(LEASE_KEY) || 'null');
        return committed?.owner === tabId && Number(committed?.expiresAt) > nowMs;
      } catch (_) {
        return false;
      }
    }

    return {claim, tabId: () => tabId};
  }

  function createDeliveryController({storage, send, now = Date.now, random = Math.random}) {
    const delivered = new Set(parseArray(storage.getItem(DELIVERED_KEY)));
    let pending = null;

    function saveDelivered() {
      storage.setItem(DELIVERED_KEY, JSON.stringify([...delivered].slice(-2000)));
    }

    async function deliver(pass) {
      if (!pending) {
        const cards = [];
        const fingerprints = [];
        for (const card of pass.cards || []) {
          const key = fingerprint(card.id, card.html);
          if (delivered.has(key)) continue;
          cards.push(card.html);
          fingerprints.push(key);
        }
        const payload = {
          run_id: makeRunId(now, random),
          cards,
          status: pass.status || (cards.length ? 'ok' : 'no_new'),
          prior_sentinel: pass.prior_sentinel ?? null,
          newest_sentinel: pass.newest_sentinel ?? null,
          sentinel_found: pass.sentinel_found ?? null,
          diagnostics: [...(pass.diagnostics || [])],
          cards_scanned: pass.cards_scanned,
          stop_reason: pass.stop_reason,
          cadence_bucket: pass.cadence_bucket,
          scroll_count: pass.scroll_count,
          dom_wait_ms: pass.dom_wait_ms,
          reload_reason: pass.reload_reason,
          recovery_used: pass.recovery_used,
          operator_postponed: pass.operator_postponed,
        };
        pending = {payload, fingerprints};
      }

      let response;
      try {
        response = await send(pending.payload);
      } catch (_) {
        response = {status: 0};
      }
      let body = null;
      try {
        body = JSON.parse(response?.body || '{}');
      } catch (_) {
        body = null;
      }
      const acknowledged = Boolean(
        response && response.status >= 200 && response.status < 300 && body &&
        body.run_id === pending.payload.run_id && ACK_STATUSES.has(body.status)
      );
      if (!acknowledged) {
        const runId = pending.payload.run_id;
        if (body?.run_id === runId && ['clock_unhealthy', 'parser_error'].includes(body.status)) {
          pending = null; // keep fingerprints undelivered; retry the card under a fresh run id
        }
        return {acknowledged: false, run_id: runId, status: response?.status || 0};
      }

      for (const key of pending.fingerprints) delivered.add(key);
      saveDelivered();
      if (pending.payload.newest_sentinel) {
        storage.setItem(SENTINEL_KEY, pending.payload.newest_sentinel);
      }
      const result = {acknowledged: true, run_id: pending.payload.run_id, response: body};
      pending = null;
      return result;
    }

    return {
      deliver,
      isDelivered: (id, html) => delivered.has(fingerprint(id, html)),
      priorSentinel: () => storage.getItem(SENTINEL_KEY),
      pendingRunId: () => pending?.payload.run_id || null,
    };
  }

  async function collectBoundedPass({
    feedState, readArticles, scroll, priorSentinel, maxCards = MAX_CARDS,
    maxScrolls = MAX_SCROLLS, isDelivered = () => true,
  }) {
    const state = feedState();
    if (state.status !== 'ok') {
      return {
        status: state.status, cards: [], prior_sentinel: priorSentinel,
        newest_sentinel: null, sentinel_found: null,
        cards_scanned: 0, stop_reason: state.status,
        scroll_count: 0, dom_wait_ms: 0,
        diagnostics: [...(state.diagnostics || [])],
      };
    }

    const cards = [];
    const seenIds = new Set();
    let newest = null;
    let sentinelFound = priorSentinel ? false : null;
    let scrolls = 0;
    let domWaitMs = 0;
    let cutoff = false;
    while (true) {
      const articles = readArticles() || [];
      for (const card of articles) {
        if (!card?.id || seenIds.has(card.id)) continue;
        seenIds.add(card.id);
        if (!newest) newest = card.id;
        if (priorSentinel && card.id === priorSentinel) {
          if (!isDelivered(card.id, card.html)) {
            if (cards.length >= maxCards) {
              cutoff = true;
              break;
            }
            cards.push(card);
          }
          sentinelFound = true;
          break;
        }
        if (cards.length >= maxCards) {
          cutoff = true;
          break;
        }
        cards.push(card);
      }
      if (sentinelFound === true || cutoff || !priorSentinel) break;
      if (scrolls >= maxScrolls) break;
      const progress = await scroll();
      domWaitMs += Number(progress?.waitMs || 0);
      scrolls += 1;
    }

    const gap = cutoff || (priorSentinel && sentinelFound !== true);
    const stopReason = cutoff ? 'card_limit'
      : (sentinelFound === true ? 'sentinel_found'
        : (priorSentinel ? 'scroll_limit' : 'initial_pass'));
    return {
      status: gap ? 'coverage_gap' : (cards.length ? 'ok' : 'no_new'),
      cards,
      prior_sentinel: priorSentinel,
      newest_sentinel: newest,
      sentinel_found: sentinelFound,
      cards_scanned: seenIds.size,
      stop_reason: stopReason,
      scroll_count: scrolls,
      dom_wait_ms: domWaitMs,
      diagnostics: gap ? ['coverage_gap'] : [],
    };
  }

  function canonicalId(article) {
    const anchor = [...article.querySelectorAll('a[href*="/status/"]')]
      .find((candidate) => candidate.querySelector('time'));
    const match = anchor?.getAttribute('href')?.match(/\/status\/(\d+)/);
    return match ? match[1] : null;
  }

  function canonicalIds(document) {
    const ids = [...document.querySelectorAll('article[data-testid="tweet"]')]
      .map(canonicalId).filter(Boolean);
    return [...new Set(ids)];
  }

  async function waitForReadiness({
    document, now = Date.now,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    timeoutMs = 30000, pollMs = 500,
  }) {
    const started = now();
    while (true) {
      const state = inspectFeed(document);
      if (['challenge', 'rate_limited', 'logged_out'].includes(state.status)) {
        return {...state, waitMs: now() - started};
      }
      const selected = document.querySelector('[role="tab"][aria-selected="true"]');
      if (selected && state.status === 'wrong_feed') {
        return {...state, waitMs: now() - started};
      }
      if (state.status === 'ok' && canonicalIds(document).length > 0) {
        return {...state, waitMs: now() - started};
      }
      const elapsed = now() - started;
      if (elapsed >= timeoutMs) {
        return {
          status: 'page_unready', diagnostics: ['page_unready'], waitMs: elapsed,
        };
      }
      await sleep(Math.min(pollMs, timeoutMs - elapsed));
    }
  }

  function createDomAwareScroll({
    root, readIds, now = Date.now,
    sleep = (ms) => new Promise((resolve) => root.setTimeout(resolve, ms)),
    progressTimeoutMs = 5000, stableMs = 1500, pollMs = 250,
  }) {
    return async function domAwareScroll() {
      const started = now();
      const initial = new Set(readIds());
      let lastSignature = [...initial].sort().join(',');
      let stableSince = null;
      root.scrollBy(0, 450);
      while (true) {
        const elapsed = now() - started;
        if (elapsed >= progressTimeoutMs) return {waitMs: elapsed};
        await sleep(Math.min(pollMs, progressTimeoutMs - elapsed));
        const ids = readIds();
        const signature = [...ids].sort().join(',');
        const progressed = ids.some((id) => !initial.has(id));
        if (progressed && signature !== lastSignature) stableSince = now();
        if (progressed && stableSince === null) stableSince = now();
        lastSignature = signature;
        if (stableSince !== null && now() - stableSince >= stableMs) {
          return {waitMs: now() - started};
        }
      }
    };
  }

  function inspectFeed(document) {
    const text = String(document.body?.innerText || '').toLowerCase();
    if (/verify you are human|unusual activity|security challenge/.test(text)) {
      return {status: 'challenge', diagnostics: ['challenge']};
    }
    if (/rate limit exceeded|try again later/.test(text)) {
      return {status: 'rate_limited', diagnostics: ['rate_limited']};
    }
    if (/log in to x|sign in to x/.test(text)) {
      return {status: 'logged_out', diagnostics: ['unauthenticated']};
    }
    const selected = document.querySelector('[role="tab"][aria-selected="true"]');
    const tablist = selected?.closest?.('[role="tablist"]');
    const homeTabs = tablist ? [...tablist.querySelectorAll('[role="tab"]')] : [];
    const isFollowing = Boolean(selected) && (
      /following/i.test(selected.textContent || '') ||
      (homeTabs.length === 2 && homeTabs[1] === selected)
    );
    if (!isFollowing) {
      return {status: 'wrong_feed', diagnostics: ['wrong_feed']};
    }
    return {status: 'ok', diagnostics: []};
  }

  function browserSend(request, payload) {
    return new Promise((resolve) => {
      request({
        method: 'POST', url: RECEIVER,
        headers: {'Content-Type': 'application/json'},
        data: JSON.stringify(payload),
        onload: (response) => resolve({status: response.status, body: response.responseText}),
        onerror: () => resolve({status: 0}),
        ontimeout: () => resolve({status: 0}),
        timeout: 15000,
      });
    });
  }

  function startBrowserCapture(root, request = root.GM_xmlhttpRequest, options = {}) {
    if (!root.document || !root.sessionStorage || typeof request !== 'function') return;
    const now = options.now || Date.now;
    const random = options.random || Math.random;
    const sleep = options.sleep || ((ms) => new Promise((resolve) => root.setTimeout(resolve, ms)));
    const waitForReadinessFn = options.waitForReadinessFn || waitForReadiness;
    let lease = options.leaseController;
    if (!lease) {
      try {
        lease = createTabLease({
          sharedStorage: root.localStorage, sessionStorage: root.sessionStorage, now, random,
        });
      } catch (_) {
        lease = {claim: () => false};
      }
    }
    const controller = createDeliveryController({
      storage: root.sessionStorage,
      send: (payload) => browserSend(request, payload),
      now, random,
    });
    let active = false;
    let pendingCapture = null;
    let pendingSchedule = null;
    let lastTrustedInputMs = null;
    let leaseRenewing = false;
    let scheduleState = {recoveryUsed: false, pageUnreadyCount: 0, operatorPostponed: false};
    try {
      const saved = JSON.parse(root.sessionStorage.getItem(SCHEDULE_KEY) || '{}');
      scheduleState = {
        recoveryUsed: saved.recoveryUsed === true,
        pageUnreadyCount: Number.isInteger(saved.pageUnreadyCount)
          ? Math.max(0, Math.min(2, saved.pageUnreadyCount)) : 0,
        operatorPostponed: saved.operatorPostponed === true,
      };
    } catch (_) {
      // Invalid scheduler state is safely reset; capture evidence remains in receiver storage.
    }

    function saveScheduleState() {
      root.sessionStorage.setItem(SCHEDULE_KEY, JSON.stringify(scheduleState));
    }

    const noteTrustedInput = (event) => {
      if (event?.isTrusted === true) lastTrustedInputMs = now();
    };
    if (typeof root.addEventListener === 'function') {
      for (const type of ['wheel', 'pointerdown', 'keydown']) {
        root.addEventListener(type, noteTrustedInput, {passive: true});
      }
    }

    const readArticles = options.readArticlesFn || (() => [
      ...root.document.querySelectorAll('article[data-testid="tweet"]')]
      .map((article) => ({id: canonicalId(article), html: article.outerHTML}))
      .filter((card) => card.id));
    const scroll = options.scrollFn || createDomAwareScroll({
      root, readIds: () => canonicalIds(root.document), now, sleep,
    });

    function scheduleNext(schedule, {reload}) {
      if (schedule.frozen || schedule.delayMs === null) {
        leaseRenewing = false;
        root.console?.warn('[mcx] capture frozen', schedule.cadenceBucket);
        return;
      }
      const proceed = () => {
        if (!lease.claim()) {
          leaseRenewing = false;
          root.setTimeout(run, LEASE_RETRY_MS);
          return;
        }
        leaseRenewing = true;
        const quiet = remainingQuietMs({nowMs: now(), lastTrustedInputMs});
        if (quiet > 0) {
          scheduleState.operatorPostponed = true;
          saveScheduleState();
          root.setTimeout(proceed, quiet);
          return;
        }
        if (reload) root.location.reload();
        else run();
      };
      root.setTimeout(proceed, schedule.delayMs);
    }

    async function run() {
      if (active) return;
      if (!lease.claim()) {
        leaseRenewing = false;
        root.console?.log('[mcx] passive duplicate tab');
        root.setTimeout(run, LEASE_RETRY_MS);
        return;
      }
      leaseRenewing = true;
      const quiet = remainingQuietMs({nowMs: now(), lastTrustedInputMs});
      if (quiet > 0) {
        scheduleState.operatorPostponed = true;
        saveScheduleState();
        root.setTimeout(run, quiet);
        return;
      }
      active = true;
      let capture = pendingCapture;
      let schedule = pendingSchedule;
      let result = {acknowledged: false};
      try {
        if (!capture) {
          const readiness = await waitForReadinessFn({
            document: root.document, now, sleep, timeoutMs: 30000,
          });
          capture = await collectBoundedPass({
            feedState: () => readiness, readArticles, scroll,
            priorSentinel: controller.priorSentinel(), isDelivered: controller.isDelivered,
          });
          if (capture.status === 'page_unready') capture.dom_wait_ms = readiness.waitMs;
          const scheduleCards = capture.cards.filter(
            (card) => !controller.isDelivered(card.id, card.html));
          schedule = chooseSchedule({
            capture: {...capture, cards: scheduleCards}, acknowledged: true,
            recoveryUsed: scheduleState.recoveryUsed,
            pageUnreadyCount: scheduleState.pageUnreadyCount, random,
          });
          capture = {
            ...capture,
            cadence_bucket: schedule.cadenceBucket,
            reload_reason: ['ok', 'no_new', 'coverage_gap'].includes(capture.status)
              ? 'healthy_ack' : null,
            recovery_used: schedule.recoveryUsed,
            operator_postponed: scheduleState.operatorPostponed,
          };
          pendingCapture = capture;
          pendingSchedule = schedule;
        }
        result = await controller.deliver(capture);
        if (result.acknowledged) {
          scheduleState = {
            recoveryUsed: schedule.recoveryUsed,
            pageUnreadyCount: schedule.pageUnreadyCount,
            operatorPostponed: false,
          };
          saveScheduleState();
          pendingCapture = null;
          pendingSchedule = null;
        } else {
          schedule = chooseSchedule({
            capture, acknowledged: false,
            recoveryUsed: scheduleState.recoveryUsed,
            pageUnreadyCount: scheduleState.pageUnreadyCount, random,
          });
        }
        root.console?.log('[mcx] capture', capture.status, result);
      } catch (error) {
        root.console?.warn('[mcx] capture error', error);
        schedule = chooseSchedule({
          capture: capture || {status: 'error', cards: []}, acknowledged: false,
          recoveryUsed: scheduleState.recoveryUsed,
          pageUnreadyCount: scheduleState.pageUnreadyCount, random,
        });
      } finally {
        active = false;
      }
      const shouldReload = result.acknowledged &&
        ['ok', 'no_new', 'coverage_gap'].includes(capture?.status) && !schedule.frozen;
      scheduleNext(schedule, {reload: shouldReload});
    }

    if (typeof root.setInterval === 'function') {
      root.setInterval(() => {
        if (leaseRenewing && !lease.claim()) leaseRenewing = false;
      }, LEASE_RENEW_MS);
    }
    root.console?.log('[mcx] read-only capture active');
    root.setTimeout(run, 5000);
  }

  return {
    canonicalIds,
    chooseSchedule,
    collectBoundedPass,
    createDomAwareScroll,
    createDeliveryController,
    createTabLease,
    fingerprint,
    inspectFeed,
    remainingQuietMs,
    startBrowserCapture,
    waitForReadiness,
  };
});
