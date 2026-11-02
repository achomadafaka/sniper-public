'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  canonicalIds,
  chooseSchedule,
  collectBoundedPass,
  createDomAwareScroll,
  createDeliveryController,
  createTabLease,
  inspectFeed,
  remainingQuietMs,
  startBrowserCapture,
  waitForReadiness,
} = require('../../scripts/memecoin_x_capture.user.js');

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (key) => data.has(key) ? data.get(key) : null,
    setItem: (key, value) => data.set(key, String(value)),
  };
}

function pass(cards = [{id: '101', html: '<article>one</article>'}]) {
  return {
    status: cards.length ? 'ok' : 'no_new',
    cards,
    prior_sentinel: null,
    newest_sentinel: cards[0]?.id || null,
    sentinel_found: null,
    diagnostics: [],
  };
}

test('failed and non-2xx delivery stays pending until matching committed ack', async () => {
  const sent = [];
  let attempt = 0;
  const controller = createDeliveryController({
    storage: memoryStorage(),
    now: () => 1700000000000,
    random: () => 0.25,
    send: async (payload) => {
      sent.push(payload);
      attempt += 1;
      if (attempt === 1) return {status: 0};
      if (attempt === 2) return {status: 503};
      return {status: 200, body: JSON.stringify({
        run_id: payload.run_id, status: 'ok', cards_seen: 1,
      })};
    },
  });

  assert.equal((await controller.deliver(pass())).acknowledged, false);
  assert.equal((await controller.deliver(pass())).acknowledged, false);
  assert.equal(controller.isDelivered('101', '<article>one</article>'), false);
  assert.equal((await controller.deliver(pass())).acknowledged, true);
  assert.equal(controller.isDelivered('101', '<article>one</article>'), true);
  assert.deepEqual(sent.map((payload) => payload.run_id), [sent[0].run_id, sent[0].run_id, sent[0].run_id]);
});

test('lost success response retry reuses run id and accepts duplicate committed result', async () => {
  const runIds = [];
  let first = true;
  const controller = createDeliveryController({
    storage: memoryStorage(), now: () => 1700000000000, random: () => 0.5,
    send: async (payload) => {
      runIds.push(payload.run_id);
      if (first) {
        first = false;
        return {status: 0};
      }
      return {status: 200, body: JSON.stringify({
        run_id: payload.run_id, status: 'ok', duplicate: true,
      })};
    },
  });
  await controller.deliver(pass());
  const result = await controller.deliver(pass());
  assert.equal(result.acknowledged, true);
  assert.equal(runIds[0], runIds[1]);
});

test('empty pass still sends a heartbeat', async () => {
  const sent = [];
  const controller = createDeliveryController({
    storage: memoryStorage(), now: () => 1700000000000, random: () => 0,
    send: async (payload) => {
      sent.push(payload);
      return {status: 200, body: JSON.stringify({run_id: payload.run_id, status: 'no_new'})};
    },
  });
  const result = await controller.deliver(pass([]));
  assert.equal(result.acknowledged, true);
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].cards, []);
  assert.equal(sent[0].status, 'no_new');
  assert.equal(sent[0].cards_scanned, undefined);
});

test('parser-error response does not acknowledge or drop pending fingerprints', async () => {
  const runIds = [];
  const controller = createDeliveryController({
    storage: memoryStorage(), now: () => 1700000000000,
    random: () => (runIds.length + 1) / 10,
    send: async (payload) => {
      runIds.push(payload.run_id);
      return {status: runIds.length === 1 ? 422 : 200, body: JSON.stringify({
        run_id: payload.run_id, status: runIds.length === 1 ? 'parser_error' : 'ok',
        parse_failures: runIds.length === 1 ? 1 : 0,
      })};
    },
  });
  const result = await controller.deliver(pass());
  assert.equal(result.acknowledged, false);
  assert.equal(controller.isDelivered('101', '<article>one</article>'), false);
  assert.equal(controller.pendingRunId(), null);
  assert.equal((await controller.deliver(pass())).acknowledged, true);
  assert.notEqual(runIds[0], runIds[1]);
});

test('clock-unhealthy response keeps fingerprints and retries with a fresh run id', async () => {
  const runIds = [];
  let attempt = 0;
  const controller = createDeliveryController({
    storage: memoryStorage(), now: () => 1700000000000,
    random: () => (attempt + 1) / 10,
    send: async (payload) => {
      runIds.push(payload.run_id);
      attempt += 1;
      return {status: 200, body: JSON.stringify({
        run_id: payload.run_id, status: attempt === 1 ? 'clock_unhealthy' : 'ok',
      })};
    },
  });
  assert.equal((await controller.deliver(pass())).acknowledged, false);
  assert.equal(controller.isDelivered('101', '<article>one</article>'), false);
  assert.equal(controller.pendingRunId(), null);
  assert.equal((await controller.deliver(pass())).acknowledged, true);
  assert.notEqual(runIds[0], runIds[1]);
  assert.equal(controller.isDelivered('101', '<article>one</article>'), true);
});

test('wrong feed emits diagnostic heartbeat and no cards', async () => {
  const result = await collectBoundedPass({
    feedState: () => ({status: 'wrong_feed', diagnostics: ['wrong_feed']}),
    readArticles: () => [{id: '101', html: '<article>ignored</article>'}],
    scroll: async () => {}, priorSentinel: null,
  });
  assert.equal(result.status, 'wrong_feed');
  assert.deepEqual(result.cards, []);
  assert.deepEqual(result.diagnostics, ['wrong_feed']);
});

test('bounded pass stops at sentinel and marks a miss after three scrolls', async () => {
  let index = 0;
  const batches = [
    [{id: '105', html: 'five'}],
    [{id: '104', html: 'four'}],
    [{id: '103', html: 'three'}, {id: '102', html: 'old'}],
  ];
  const found = await collectBoundedPass({
    feedState: () => ({status: 'ok', diagnostics: []}),
    readArticles: () => batches[index],
    scroll: async () => { index += 1; }, priorSentinel: '102', maxScrolls: 3,
  });
  assert.equal(found.status, 'ok');
  assert.equal(found.sentinel_found, true);
  assert.deepEqual(found.cards.map((card) => card.id), ['105', '104', '103']);
  assert.equal(found.cards_scanned, 4);
  assert.equal(found.stop_reason, 'sentinel_found');

  let scrolls = 0;
  const missed = await collectBoundedPass({
    feedState: () => ({status: 'ok', diagnostics: []}),
    readArticles: () => [{id: String(200 - scrolls), html: `card-${scrolls}`}],
    scroll: async () => { scrolls += 1; }, priorSentinel: '1', maxScrolls: 3,
  });
  assert.equal(missed.status, 'coverage_gap');
  assert.equal(missed.sentinel_found, false);
  assert.equal(scrolls, 3);
  assert.equal(missed.cards_scanned, 4);
  assert.equal(missed.stop_reason, 'scroll_limit');
});

test('bounded pass includes a changed sentinel card before stopping', async () => {
  const changed = {id: '102', html: '<article>complete edited card</article>'};
  const result = await collectBoundedPass({
    feedState: () => ({status: 'ok', diagnostics: []}),
    readArticles: () => [
      {id: '103', html: '<article>new</article>'},
      changed,
      {id: '101', html: '<article>older</article>'},
    ],
    scroll: async () => {}, priorSentinel: '102',
    isDelivered: (id, html) => id === '102' && html !== changed.html,
  });
  assert.equal(result.status, 'ok');
  assert.equal(result.sentinel_found, true);
  assert.deepEqual(result.cards.map((card) => card.id), ['103', '102']);
});

test('browser startup accepts Tampermonkey API as a lexical binding', async () => {
  const scheduled = [];
  const requests = [];
  const root = {
    document: {
      body: {innerText: ''},
      querySelector: () => ({textContent: 'Following'}),
      querySelectorAll: () => [],
    },
    sessionStorage: memoryStorage(),
    console: {log: () => {}, warn: () => {}},
    location: {reload: () => {}},
    scrollBy: () => {},
    setTimeout: (callback) => { scheduled.push(callback); },
  };
  const gmRequest = (options) => {
    requests.push(options);
    const payload = JSON.parse(options.data);
    options.onload({
      status: 200,
      responseText: JSON.stringify({run_id: payload.run_id, status: 'no_new'}),
    });
  };

  startBrowserCapture(root, gmRequest, {
    waitForReadinessFn: async () => ({status: 'ok', diagnostics: [], waitMs: 0}),
    random: () => 0, leaseController: {claim: () => true},
  });
  assert.equal(scheduled.length, 1);
  await scheduled.shift()();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests.length, 1);
  const payload = JSON.parse(requests[0].data);
  assert.equal(payload.status, 'no_new');
  assert.equal(payload.cadence_bucket, 'idle_10_15m');
  assert.equal(payload.scroll_count, 0);
  assert.equal(payload.reload_reason, 'healthy_ack');
});

test('transport retry resends pending payload without another browser pass', async () => {
  const scheduled = [];
  let readinessCalls = 0;
  let articleReads = 0;
  let requestCalls = 0;
  const storage = memoryStorage();
  storage.setItem('mcx_latest_sentinel_v1', '1');
  const root = {
    document: {body: {innerText: ''}, querySelector: () => ({}), querySelectorAll: () => []},
    sessionStorage: storage,
    console: {log: () => {}, warn: () => {}},
    location: {reload: () => {}},
    scrollBy: () => {}, addEventListener: () => {},
    setTimeout: (callback, delay) => { scheduled.push({callback, delay}); },
  };
  const gmRequest = (options) => {
    requestCalls += 1;
    const payload = JSON.parse(options.data);
    if (requestCalls === 1) options.onerror();
    else options.onload({status: 200, responseText: JSON.stringify({
      run_id: payload.run_id, status: payload.status,
    })});
  };
  startBrowserCapture(root, gmRequest, {
    waitForReadinessFn: async () => {
      readinessCalls += 1;
      return {status: 'ok', diagnostics: [], waitMs: 0};
    },
    readArticlesFn: () => {
      articleReads += 1;
      return [{id: '2', html: '<article>two</article>'}];
    },
    scrollFn: async () => ({waitMs: 5000}), random: () => 0,
    leaseController: {claim: () => true},
  });
  await scheduled.shift().callback();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(scheduled.at(-1).delay, 60000);
  await scheduled.pop().callback();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requestCalls, 2);
  assert.equal(readinessCalls, 1);
  assert.equal(articleReads, 4);
});

test('shared tab lease admits one collector and survives same-tab reload', () => {
  const shared = memoryStorage();
  const firstSession = memoryStorage();
  const secondSession = memoryStorage();
  let nowMs = 1000;
  const first = createTabLease({
    sharedStorage: shared, sessionStorage: firstSession,
    now: () => nowMs, random: () => 0.1,
  });
  const second = createTabLease({
    sharedStorage: shared, sessionStorage: secondSession,
    now: () => nowMs, random: () => 0.2,
  });

  assert.equal(first.claim(), true);
  assert.equal(second.claim(), false);
  const reloadedFirst = createTabLease({
    sharedStorage: shared, sessionStorage: firstSession,
    now: () => nowMs + 1000, random: () => 0.9,
  });
  assert.equal(reloadedFirst.claim(), true);
  nowMs += 121000;
  assert.equal(second.claim(), true);
  assert.equal(first.claim(), false);
});

test('passive duplicate tab does not inspect or deliver the feed', async () => {
  const scheduled = [];
  let readinessCalls = 0;
  let requestCalls = 0;
  const root = {
    document: {body: {innerText: ''}, querySelector: () => ({}), querySelectorAll: () => []},
    sessionStorage: memoryStorage(), localStorage: memoryStorage(),
    console: {log: () => {}, warn: () => {}},
    location: {reload: () => {}}, scrollBy: () => {}, addEventListener: () => {},
    setTimeout: (callback, delay) => scheduled.push({callback, delay}),
  };
  startBrowserCapture(root, () => { requestCalls += 1; }, {
    waitForReadinessFn: async () => {
      readinessCalls += 1;
      return {status: 'ok', diagnostics: [], waitMs: 0};
    },
    leaseController: {claim: () => false},
  });

  await scheduled.shift().callback();
  assert.equal(readinessCalls, 0);
  assert.equal(requestCalls, 0);
  assert.equal(scheduled.at(-1).delay, 45000);
});

test('following feed detection is language-independent', () => {
  const forYou = {textContent: 'Para ti'};
  const following = {
    textContent: 'Siguiendo',
    closest: () => ({querySelectorAll: () => [forYou, following]}),
  };
  const document = {
    body: {innerText: ''},
    querySelector: () => following,
  };

  assert.deepEqual(inspectFeed(document), {status: 'ok', diagnostics: []});
});

test('first home tab remains a wrong-feed state', () => {
  const forYou = {
    textContent: 'Para ti',
    closest: () => ({querySelectorAll: () => [forYou, following]}),
  };
  const following = {textContent: 'Siguiendo'};
  const document = {
    body: {innerText: ''},
    querySelector: () => forYou,
  };

  assert.deepEqual(inspectFeed(document), {
    status: 'wrong_feed', diagnostics: ['wrong_feed'],
  });
});

test('adaptive schedule uses exact new-card and idle window boundaries', () => {
  const base = {acknowledged: true, recoveryUsed: true, pageUnreadyCount: 1};
  const freshMin = chooseSchedule({
    ...base, capture: {status: 'ok', cards: [{id: '1'}], sentinel_found: true},
    random: () => 0,
  });
  assert.deepEqual(freshMin, {
    delayMs: 300000, cadenceBucket: 'new_5_8m', recoveryUsed: false,
    pageUnreadyCount: 0, frozen: false,
  });
  assert.equal(chooseSchedule({
    ...base, capture: {status: 'ok', cards: [{id: '1'}], sentinel_found: true},
    random: () => 1,
  }).delayMs, 480000);
  assert.equal(chooseSchedule({
    ...base, capture: {status: 'no_new', cards: [], sentinel_found: true},
    random: () => 0,
  }).delayMs, 600000);
  assert.equal(chooseSchedule({
    ...base, capture: {status: 'no_new', cards: [], sentinel_found: true},
    random: () => 1,
  }).delayMs, 900000);
});

test('adaptive schedule accelerates only the first unresolved coverage gap', () => {
  const first = chooseSchedule({
    capture: {status: 'coverage_gap', cards: [], sentinel_found: false},
    acknowledged: true, recoveryUsed: false, pageUnreadyCount: 0, random: () => 0,
  });
  assert.deepEqual(first, {
    delayMs: 300000, cadenceBucket: 'recovery_5_8m', recoveryUsed: true,
    pageUnreadyCount: 0, frozen: false,
  });
  const repeated = chooseSchedule({
    capture: {status: 'coverage_gap', cards: [], sentinel_found: false},
    acknowledged: true, recoveryUsed: first.recoveryUsed,
    pageUnreadyCount: first.pageUnreadyCount, random: () => 1,
  });
  assert.deepEqual(repeated, {
    delayMs: 900000, cadenceBucket: 'gap_10_15m', recoveryUsed: true,
    pageUnreadyCount: 0, frozen: false,
  });
});

test('page readiness retries once while operator states freeze immediately', () => {
  const first = chooseSchedule({
    capture: {status: 'page_unready', cards: []}, acknowledged: true,
    recoveryUsed: false, pageUnreadyCount: 0, random: () => 0,
  });
  assert.deepEqual(first, {
    delayMs: 900000, cadenceBucket: 'page_unready_15m', recoveryUsed: false,
    pageUnreadyCount: 1, frozen: false,
  });
  assert.deepEqual(chooseSchedule({
    capture: {status: 'page_unready', cards: []}, acknowledged: true,
    recoveryUsed: false, pageUnreadyCount: 1, random: () => 0,
  }), {
    delayMs: null, cadenceBucket: 'frozen', recoveryUsed: false,
    pageUnreadyCount: 2, frozen: true,
  });
  for (const status of ['wrong_feed', 'logged_out', 'challenge', 'rate_limited']) {
    assert.equal(chooseSchedule({
      capture: {status, cards: []}, acknowledged: true,
      recoveryUsed: false, pageUnreadyCount: 0, random: () => 0,
    }).frozen, true);
  }
});

test('unacknowledged delivery retries after sixty seconds without changing state', () => {
  assert.deepEqual(chooseSchedule({
    capture: {status: 'ok', cards: [{id: '1'}]}, acknowledged: false,
    recoveryUsed: true, pageUnreadyCount: 1, random: () => 0,
  }), {
    delayMs: 60000, cadenceBucket: 'delivery_retry_60s', recoveryUsed: true,
    pageUnreadyCount: 1, frozen: false,
  });
});

test('quiet-period math is bounded and canonical ids are unique', () => {
  assert.equal(remainingQuietMs({nowMs: 90000, lastTrustedInputMs: null}), 0);
  assert.equal(remainingQuietMs({nowMs: 90000, lastTrustedInputMs: 0}), 0);
  assert.equal(remainingQuietMs({nowMs: 89999, lastTrustedInputMs: 0}), 1);
  assert.equal(remainingQuietMs({nowMs: 1, lastTrustedInputMs: 0}), 89999);
  const article = (href) => ({
    querySelectorAll: () => [{querySelector: () => ({}), getAttribute: () => href}],
  });
  const document = {
    querySelectorAll: () => [article('/a/status/10'), article('/b/status/10'),
      article('/c/status/11')],
  };
  assert.deepEqual(canonicalIds(document), ['10', '11']);
});

test('readiness waits for Following cards and does not mislabel a blank page', async () => {
  let nowMs = 0;
  let ready = false;
  const following = {
    textContent: 'Siguiendo',
    closest: () => ({querySelectorAll: () => [{textContent: 'Para ti'}, following]}),
  };
  const document = {
    body: {innerText: ''},
    querySelector: () => ready ? following : null,
    querySelectorAll: () => ready ? [{
      querySelectorAll: () => [{querySelector: () => ({}),
        getAttribute: () => '/source/status/42'}],
    }] : [],
  };
  const result = await waitForReadiness({
    document, now: () => nowMs,
    sleep: async (ms) => { nowMs += ms; if (nowMs >= 1000) ready = true; },
  });
  assert.deepEqual(result, {status: 'ok', diagnostics: [], waitMs: 1000});

  ready = false;
  nowMs = 0;
  const blank = await waitForReadiness({
    document, now: () => nowMs, timeoutMs: 30000,
    sleep: async (ms) => { nowMs += ms; },
  });
  assert.deepEqual(blank, {
    status: 'page_unready', diagnostics: ['page_unready'], waitMs: 30000,
  });
});

test('readiness returns authenticated wrong-feed and challenge states immediately', async () => {
  const first = {
    textContent: 'Para ti',
    closest: () => ({querySelectorAll: () => [first, {textContent: 'Siguiendo'}]}),
  };
  assert.deepEqual(await waitForReadiness({
    document: {body: {innerText: ''}, querySelector: () => first, querySelectorAll: () => []},
    now: () => 0, sleep: async () => assert.fail('must not sleep'),
  }), {status: 'wrong_feed', diagnostics: ['wrong_feed'], waitMs: 0});
  assert.deepEqual(await waitForReadiness({
    document: {body: {innerText: 'Verify you are human'}, querySelector: () => null,
      querySelectorAll: () => []},
    now: () => 0, sleep: async () => assert.fail('must not sleep'),
  }), {status: 'challenge', diagnostics: ['challenge'], waitMs: 0});
});

test('DOM-aware scroll waits for canonical progress and stability', async () => {
  let nowMs = 0;
  let ids = ['10'];
  const scrolls = [];
  const scroll = createDomAwareScroll({
    root: {scrollBy: (...args) => scrolls.push(args)}, readIds: () => ids,
    now: () => nowMs, sleep: async (ms) => {
      nowMs += ms;
      if (nowMs === 500) ids = ['10', '9'];
    },
  });
  assert.deepEqual(await scroll(), {waitMs: 2000});
  assert.deepEqual(scrolls, [[0, 450]]);
});

test('DOM-aware scroll stops after the five-second no-progress timeout', async () => {
  let nowMs = 0;
  const scrolls = [];
  const scroll = createDomAwareScroll({
    root: {scrollBy: (...args) => scrolls.push(args)}, readIds: () => ['10'],
    now: () => nowMs, sleep: async (ms) => { nowMs += ms; },
  });
  assert.deepEqual(await scroll(), {waitMs: 5000});
  assert.deepEqual(scrolls, [[0, 450]]);
});

test('bounded pass reports exact scroll and DOM wait audit facts', async () => {
  let calls = 0;
  const result = await collectBoundedPass({
    feedState: () => ({status: 'ok', diagnostics: []}),
    readArticles: () => [{id: String(10 - calls), html: `card-${calls}`}],
    scroll: async () => { calls += 1; return {waitMs: 2000}; },
    priorSentinel: '1', maxScrolls: 3,
  });
  assert.equal(result.scroll_count, 3);
  assert.equal(result.dom_wait_ms, 6000);
});
