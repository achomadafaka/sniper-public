/**
 * TickerBar — compact market strip.
 *
 * Receives data via:
 *   1. WebSocket push: msg.ticker from the existing /stream WebSocket
 *   2. REST fallback: GET /api/ticker on first load
 *
 * Call TickerBar.init(containerId) once.
 * Call TickerBar.update(snapshot) when new data arrives.
 */
const TickerBar = (() => {
  let _container = null;
  let _track = null;
  let _items = {};
  let _stale = false;
  let _lastUpdate = null;
  let _wrapper = null;
  let _initialized = false;

  const ITEM_ORDER = ['S&P 500', 'NASDAQ', 'DOW', 'VIX', '10Y', 'GOLD', 'OIL', 'BTC', 'ETH', 'SOL'];

  function init(containerId) {
    _container = document.getElementById(containerId);
    if (!_container) return;

    _container.classList.add('intel-ticker-shell');
    _container.innerHTML = '';

    const label = document.createElement('div');
    label.className = 'intel-ticker-label';
    label.textContent = 'MARKETS';
    _container.appendChild(label);

    _wrapper = document.createElement('div');
    _wrapper.className = 'intel-ticker-viewport';

    _track = document.createElement('div');
    _track.className = 'intel-ticker-track';
    _wrapper.appendChild(_track);
    _container.appendChild(_wrapper);

    _wrapper.addEventListener('wheel', (event) => {
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
      _wrapper.scrollLeft += event.deltaY;
      event.preventDefault();
    }, { passive: false });

    const staleEl = document.createElement('div');
    staleEl.id = 'ticker-stale';
    staleEl.className = 'intel-ticker-stale';
    _container.appendChild(staleEl);

    _initialized = true;

    _fetchInitial();
  }

  async function _fetchInitial() {
    try {
      const resp = await fetch('/api/ticker');
      if (resp.ok) {
        const data = await resp.json();
        if (data.symbols) update(data);
      }
    } catch(e) {
      // Silently degrade — ticker will populate when WS pushes
    }
  }

  function update(snapshot) {
    if (!snapshot || !snapshot.symbols) return;
    _items = snapshot.symbols;
    _stale = !!snapshot.stale;
    _lastUpdate = new Date();
    if (_initialized) _render();
  }

  function _render() {
    if (!_track) return;

    const orderedKeys = [
      ...ITEM_ORDER.filter(k => _items[k]),
      ...Object.keys(_items).filter(k => !ITEM_ORDER.includes(k)),
    ];

    if (orderedKeys.length === 0) {
      _track.innerHTML = '<span class="intel-ticker-empty">Fetching market data…</span>';
      return;
    }

    _track.innerHTML = orderedKeys.map(key => {
      const item = _items[key];
      const color = item.direction === 'up' ? '#22c55e' : (item.direction === 'down' ? '#ef4444' : '#94a3b8');
      const arrow = item.direction === 'up' ? '\u25b2' : (item.direction === 'down' ? '\u25bc' : '\u2014');
      const changeStr = item.change_pct != null
        ? `${item.change_pct > 0 ? '+' : ''}${item.change_pct.toFixed(2)}%`
        : '';
      const priceStr = _formatPrice(key, item.price);
      return `<div class="intel-ticker-pill">
        <span class="intel-ticker-symbol">${_esc(key)}</span>
        <span class="intel-ticker-price">${priceStr}</span>
        <span class="intel-ticker-change" style="color:${color}">${arrow} ${changeStr || 'flat'}</span>
      </div>`;
    }).join('');

    const staleEl = document.getElementById('ticker-stale');
    if (staleEl) {
      if (_stale) {
        staleEl.textContent = 'stale';
        staleEl.dataset.state = 'stale';
      } else if (_lastUpdate) {
        const mins = Math.floor((new Date() - _lastUpdate) / 60000);
        staleEl.textContent = mins < 1 ? 'live' : `${mins}m ago`;
        staleEl.dataset.state = mins < 2 ? 'live' : 'aged';
      }
    }
  }

  function _formatPrice(symbol, price) {
    if (price == null) return '\u2014';
    // Large numbers: compact format
    if (price >= 10000) return price.toLocaleString('en-US', {maximumFractionDigits: 0});
    if (price >= 1000) return price.toLocaleString('en-US', {maximumFractionDigits: 1});
    if (price >= 100) return price.toFixed(2);
    if (price >= 10) return price.toFixed(2);
    return price.toFixed(3);
  }

  function _esc(str) {
    return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function destroy() {
    if (_container) _container.innerHTML = '';
    _wrapper = null;
    _initialized = false;
  }

  return { init, update, destroy };
})();
