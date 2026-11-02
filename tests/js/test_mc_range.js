'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { geometry } = require('../../static/mc-range.js');

// $175 clip at entry price 1e-6 with a $100K market cap -> supply 1e11.
function pos(over = {}) {
  return Object.assign({
    entry_price: 1e-6, market_mid_price: 1e-6,
    selected_exit_price: over.market_mid_price === undefined ? 1e-6 : over.market_mid_price,
    market_cap: 100_000,
    default_tp_pct: 25, chain_stop_pct: 35, catastrophe_pct: 80, derisk_effective_pct: 15,
    effective_exit: null, exit_target: null,
  }, over);
}

test('range marker and displayed cap follow the PnL sell price, not a higher market mark', () => {
  const g = geometry(pos({
    market_mid_price: 1.052e-6, market_cap: 240_000,
    selected_exit_price: 1.008e-6,
  }));
  assert.ok(Math.abs(g.price_change_pct - 0.8) < 1e-9);
  assert.ok(Math.abs(g.pos - 0.516) < 1e-9);
  assert.ok(Math.abs(g.sell_mcap - 240_000 * 1.008 / 1.052) < 1e-6);
  assert.equal(g.sell, 1.008e-6);
});

test('missing sell price does not substitute the market mark for a PnL marker', () => {
  const g = geometry(pos({ market_mid_price: 1.052e-6, selected_exit_price: null }));
  assert.equal(g.price_change_pct, null);
  assert.equal(g.sell_mcap, null);
  assert.equal(g.pos, 0.5);
});

test('a known zero sell value reaches the loss rail instead of looking unknown', () => {
  const g = geometry(pos({ selected_exit_price: 0 }));
  assert.equal(g.price_change_pct, -100);
  assert.equal(g.sell_mcap, 0);
  assert.equal(g.pos, 0);
});

test('flat at entry sits in the middle with both rails from the defaults', () => {
  const g = geometry(pos());
  assert.equal(g.pos, 0.5);
  assert.equal(g.side, 'flat');
  assert.equal(g.right.pct, 25);
  assert.equal(g.left.pct, 35);
  assert.equal(Math.round(g.entry_mcap), 100_000);
  assert.equal(Math.round(g.tp_mcap), 125_000);
  assert.equal(Math.round(g.sl_mcap), 65_000);
});

test('half way to take-profit lands three quarters along, green side', () => {
  const g = geometry(pos({ market_mid_price: 1.125e-6 }));
  assert.equal(g.side, 'up');
  assert.ok(Math.abs(g.pos - 0.75) < 1e-9);
  assert.equal(g.hit, null);
});

test('through the stop pins the marker to the left edge and names the hit', () => {
  const g = geometry(pos({ market_mid_price: 0.6e-6 }));
  assert.equal(g.side, 'down');
  assert.equal(g.pos, 0);
  assert.equal(g.hit, 'sl');
});

test('an operator mcap take-profit outranks the percent default', () => {
  const g = geometry(pos({ effective_exit: { tp_target_mcap: 200_000, tp_pct: 0 } }));
  assert.ok(Math.abs(g.right.pct - 100) < 1e-9);
  assert.equal(Math.round(g.tp_mcap), 200_000);
});

test('tp off falls back to derisk, then to +100%; stop off falls back to the floor', () => {
  const a = geometry(pos({ effective_exit: { tp_pct: 0 } }));
  assert.equal(a.right.label, 'derisk');
  assert.equal(a.right.pct, 15);
  const b = geometry(pos({ effective_exit: { tp_pct: 0 }, derisk_effective_pct: 0 }));
  assert.equal(b.right.pct, 100);
  const c = geometry(pos({ chain_stop_pct: 0 }));
  assert.equal(c.left.label, 'floor');
  assert.equal(c.left.pct, 80);
});

test('no live price leaves the marker centred and mcaps unknown, never zero', () => {
  const g = geometry(pos({ market_mid_price: null, market_cap: null }));
  assert.equal(g.price_change_pct, null);
  assert.equal(g.pos, 0.5);
  assert.equal(g.entry_mcap, null);
});

test('the "no TP" rail is a display bound, never a hit', () => {
  // Codex review 2026-09-23 U1: a marker past the +100% fallback rail read as "TP reached".
  const g = geometry(pos({ market_mid_price: 2.1e-6, default_tp_pct: 0, derisk_effective_pct: 0 }));
  assert.equal(g.right.label, 'no TP');
  assert.equal(g.hit, null);
});

test('a de-risk-only rail reports a de-risk hit, not a take-profit', () => {
  const g = geometry(pos({ market_mid_price: 1.2e-6, default_tp_pct: 0, derisk_effective_pct: 15 }));
  assert.equal(g.right.label, 'derisk');
  assert.equal(g.hit, 'derisk');
});

test('a pinned total supply puts every cap on the bar on the FDV basis and says so', () => {
  // Hogdog: circulating 35.3K at 4.4111e-5 (800M), FDV 44.1K (1B total).
  const px = 4.4111e-5;
  const g = geometry(pos({
    entry_price: px, market_mid_price: px, selected_exit_price: px, market_cap: 35_300,
    target_supply: 1e9, mcap_basis: 'fdv', effective_exit: { sl_target_mcap: 40_000 },
  }));
  assert.equal(g.basis, 'FDV');
  assert.ok(Math.abs(g.entry_mcap - px * 1e9) < 1e-6);
  assert.ok(Math.abs(g.sl_mcap - 40_000) < 1e-6);
  const circ = geometry(pos({ entry_price: px, market_mid_price: px, selected_exit_price: px,
    market_cap: 35_300 }));
  assert.equal(circ.basis, 'circ');
  assert.ok(Math.abs(circ.entry_mcap - 35_300) < 1e-6);
});

test('68538: caps read on the total supply while an mcap target keeps the runner basis', () => {
  // 56, 2026-10-02: entry ratio 796M (what the runner reads targets on), on-chain total 1B (GMGN).
  const px = 2.0634e-4, circ = 164190.5 / px;
  const g = geometry(pos({
    entry_price: px, market_mid_price: 2.3e-4, selected_exit_price: 2.3e-4, market_cap: 230_000,
    target_supply: circ, mcap_basis: 'circulating', cap_supply: 1e9, cap_basis: 'total',
    effective_exit: { tp_target_mcap: 300_000, tp_pct: 0 },
  }));
  assert.equal(g.basis, 'FDV');
  assert.ok(Math.abs(g.entry_mcap - px * 1e9) < 1e-6);
  assert.ok(Math.abs(g.sell_mcap - 230_000) < 1e-6);
  // The rail fires where the runner fires (300K on 796M); the bar names that price on 1B.
  assert.ok(Math.abs(g.right.pct - (300_000 / circ / px - 1) * 100) < 1e-9);
  assert.ok(Math.abs(g.tp_mcap - 300_000 * 1e9 / circ) < 1e-3);
  const s = geometry(pos({
    entry_price: px, market_mid_price: 2.3e-4, selected_exit_price: 2.3e-4,
    target_supply: circ, cap_supply: 1e9, cap_basis: 'total',
    effective_exit: { sl_target_mcap: 150_000 },
  }));
  assert.ok(Math.abs(s.left.pct - (1 - 150_000 / circ / px) * 100) < 1e-9);
  assert.ok(Math.abs(s.sl_mcap - 150_000 * 1e9 / circ) < 1e-3);
});

test('an explicit unknown display supply never reuses the funded entry ratio', () => {
  const p = pos({cap_supply: null, display_supply: null, market_cap: null,
    target_supply: 831749939, effective_exit: {tp_target_mcap: 200000}});
  const g = geometry(p);
  assert.equal(g.supply, null);
  assert.equal(g.entry_mcap, null);
  assert.equal(g.sell_mcap, null);
  // The operator rail still uses its frozen supply.
  assert.equal(g.right.pct, (200000 / p.target_supply / p.entry_price - 1) * 100);
  const known = geometry({...p, display_supply: 1e9});
  assert.equal(known.supply, 1e9);
  assert.equal(known.entry_mcap, 1000);
});
