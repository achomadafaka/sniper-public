// Range-bar geometry for a funded position card: stop on the left, entry in the middle, take-profit
// on the right, and a marker at the selected sell-value price. Pure arithmetic, no DOM.
//
// The two halves have different scales on purpose: the left half maps entry -> stop, the right half
// maps entry -> take-profit. An edge means the sell value passed that displayed price level, not
// that the funded runner fired an exit.
(function (root) {
  'use strict';

  function num(v) { const n = Number(v); return v == null || v === '' || !Number.isFinite(n) ? null : n; }

  // Pick the rail that bounds each side. Returns {pct, label} or null when nothing bounds it.
  function rightRail(p) {
    const eff = p.effective_exit || {};
    const band = p.exit_target;
    const supply = supplyOf(p);
    if (band && num(band.mcap_low) > 0 && supply && num(p.entry_price) > 0) {
      const pct = (num(band.mcap_low) / supply / num(p.entry_price) - 1) * 100;
      if (pct > 0) return { pct, label: 'TP band' };
    }
    if (num(eff.tp_target_mcap) > 0 && supply && num(p.entry_price) > 0) {
      const pct = (num(eff.tp_target_mcap) / supply / num(p.entry_price) - 1) * 100;
      if (pct > 0) return { pct, label: 'TP' };
    }
    if (eff.tp_pct != null) {
      if (num(eff.tp_pct) > 0) return { pct: num(eff.tp_pct), label: 'TP' };
    } else if (num(p.default_tp_pct) > 0) {
      return { pct: num(p.default_tp_pct), label: 'TP' };
    }
    if (num(p.derisk_effective_pct) > 0) return { pct: num(p.derisk_effective_pct), label: 'derisk' };
    return { pct: 100, label: 'no TP' };
  }

  function leftRail(p) {
    const eff = p.effective_exit || {};
    const supply = supplyOf(p);
    if (num(eff.sl_target_mcap) > 0 && supply && num(p.entry_price) > 0) {
      const pct = (1 - num(eff.sl_target_mcap) / supply / num(p.entry_price)) * 100;
      if (pct > 0) return { pct, label: 'SL' };
    }
    if (eff.sl_pct != null) {
      if (Math.abs(num(eff.sl_pct)) > 0) return { pct: Math.abs(num(eff.sl_pct)), label: 'SL' };
    } else if (num(p.chain_stop_pct) > 0) {
      return { pct: num(p.chain_stop_pct), label: 'SL' };
    }
    if (num(p.catastrophe_pct) > 0) return { pct: num(p.catastrophe_pct), label: 'floor' };
    return { pct: 100, label: 'no SL' };
  }

  // The rail's own supply first: the TOTAL supply the runner pinned, so every cap on the bar is on
  // the FDV basis the operator's targets are read on. Otherwise market cap belongs to the market
  // mark, even when the sell-value price differs.
  function supplyOf(p) {
    const target = num(p.target_supply);
    if (target > 0) return target;
    const mcap = num(p.market_cap), mid = num(p.market_mid_price);
    if (mcap > 0 && mid > 0) return mcap / mid;
    return null;
  }

  // The supply every CAP on the bar is shown on: total supply, GMGN's basis, when the server knows
  // it (cap_supply). The rails above keep supplyOf: it is the supply the runner reads targets on.
  function capSupplyOf(p) {
    const cap = num(p.cap_supply);
    if (cap > 0) return cap;
    // New payloads distinguish display supply from the legacy entry ratio used by targets.
    // Explicit unknown must stay unknown, including while the background supply read is pending.
    if ('display_supply' in p) {
      const display = num(p.display_supply);
      return display > 0 ? display : null;
    }
    return supplyOf(p);
  }

  // The whole geometry for one position. `sell` is the selected price behind Total PnL.
  //   pos: 0..1 marker position along the bar (0.5 = entry)
  //   side: 'up' | 'down' | 'flat'
  //   hit: 'tp' | 'derisk' | 'sl' | null when the marker sits at an edge (never for 'no TP')
  function geometry(p) {
    const entry = num(p.entry_price);
    const sell = num(p.selected_exit_price);
    const right = rightRail(p), left = leftRail(p);
    const supply = capSupplyOf(p);
    const out = {
      entry, sell, right, left, supply,
      basis: (num(p.cap_supply) > 0 || p.mcap_basis === 'fdv') ? 'FDV' : 'circ',
      price_change_pct: (entry > 0 && sell != null && sell >= 0) ? (sell / entry - 1) * 100 : null,
      entry_mcap: (supply && entry > 0) ? entry * supply : null,
      sell_mcap: (supply && sell != null && sell >= 0) ? sell * supply : null,
      tp_mcap: (supply && entry > 0) ? entry * (1 + right.pct / 100) * supply : null,
      sl_mcap: (supply && entry > 0) ? entry * (1 - left.pct / 100) * supply : null,
      pos: 0.5, side: 'flat', hit: null,
    };
    if (out.price_change_pct == null) return out;
    if (out.price_change_pct >= 0) {
      const frac = Math.min(1, out.price_change_pct / right.pct);
      out.pos = 0.5 + frac / 2; out.side = out.price_change_pct > 0 ? 'up' : 'flat';
      // The right rail keeps its meaning: only a real take-profit level is a TP hit, a de-risk
      // level is its own event, and the +100% "no TP" fallback is a display bound, never a hit.
      if (frac >= 1) out.hit = right.label === 'no TP' ? null : right.label === 'derisk' ? 'derisk' : 'tp';
    } else {
      const frac = Math.min(1, -out.price_change_pct / left.pct);
      out.pos = 0.5 - frac / 2; out.side = 'down';
      if (frac >= 1) out.hit = 'sl';
    }
    return out;
  }

  const api = { geometry, rightRail, leftRail, supplyOf, capSupplyOf };
  root.mcRange = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
