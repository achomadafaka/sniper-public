/**
 * WorldMap — D3 Natural Earth world map for the Intel tab.
 * Matrix-themed geopolitical intelligence overlay.
 * Requires D3 v7 (loaded globally as `d3`) and topojson-client.
 */
const WorldMap = (() => {
  let _svg = null;
  let _g = null;
  let _projection = null;
  let _path = null;
  let _zoom = null;
  let _container = null;
  let _events = [];
  let _openIdeaSlugs = new Set();
  let _tooltip = null;
  let _onSeeIdeas = null;
  let _worldData = null;
  let _initialized = false;
  let _resizeRAF = null;
  let _scanlineTimer = null;
  let _currentTransform = null;
  let _controls = null;
  let _overlay = null;

  const STATUS_COLOR = {
    active:     '#ff3333',
    escalating: '#ff3333',
    developing: '#ffaa00',
    monitoring: '#00ff41',
    resolved:   '#334455',
  };
  const STATUS_GLOW = {
    active:     '#ff333366',
    escalating: '#ff333366',
    developing: '#ffaa0044',
    monitoring: '#00ff4133',
    resolved:   '#00000000',
  };
  const TRAJ_ICON = { escalating: '↑', 'de-escalating': '↓', stable: '→', unknown: '?' };
  const TRAJ_COLOR = { escalating: '#ff3333', 'de-escalating': '#00ff41', stable: '#ffaa00', unknown: '#556677' };

  function _normStatus(s) {
    s = (s || '').toLowerCase();
    if (s.includes('resolv')) return 'resolved';
    if (s.includes('monitor')) return 'monitoring';
    if (s.includes('develop') || s.includes('emerging') || s.includes('spread')) return 'developing';
    if (s.includes('escal')) return 'active';
    if (s.includes('active')) return 'active';
    return 'active';
  }
  function _normTraj(t) {
    t = (t || '').toLowerCase();
    if (t.includes('de-escal') || t.includes('deescal') || t.includes('improving') || t.includes('easing')) return 'de-escalating';
    if (t.includes('escal') || t.includes('worsening') || t.includes('intensif')) return 'escalating';
    if (t.includes('stable') || t.includes('ongoing') || t.includes('unchanged') || t.includes('entrenching')) return 'stable';
    return 'unknown';
  }

  function _esc(str) {
    return String(str || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function _toEpochMs(ts) {
    const ms = Date.parse(ts || '');
    return Number.isFinite(ms) ? ms : 0;
  }

  function _rankEventPriority(ev, options) {
    const openIdeaSlugs = options && options.openIdeaSlugs ? options.openIdeaSlugs : new Set();
    const recencyBoostMs = options && options.nowMs ? options.nowMs : Date.now();
    let score = 0;
    if (openIdeaSlugs.has(ev.slug)) score += 1000;
    if (ev._nt === 'escalating') score += 300;
    else if (ev._nt === 'stable') score += 120;
    else if (ev._nt === 'de-escalating') score += 80;
    if (ev._ns === 'active') score += 180;
    else if (ev._ns === 'developing') score += 90;
    else if (ev._ns === 'monitoring') score += 40;
    score += Math.min((ev.connected_events || []).length * 8, 40);
    const updatedMs = _toEpochMs(ev.last_updated || ev._memory_updated_at);
    if (updatedMs > 0) {
      const ageHours = Math.max(0, (recencyBoostMs - updatedMs) / 3600000);
      score += Math.max(0, 60 - ageHours * 6);
    }
    return score;
  }

  function _computeClusters(pinData, options) {
    const zoomK = Math.max(1, options && options.zoomK ? options.zoomK : 1);
    const isMobile = !!(options && options.isMobile);
    const openIdeaSlugs = options && options.openIdeaSlugs ? options.openIdeaSlugs : new Set();
    const nowMs = options && options.nowMs ? options.nowMs : Date.now();
    const baseRadiusPx = isMobile
      ? (zoomK >= 2.4 ? 18 : zoomK >= 1.5 ? 24 : 30)
      : (zoomK >= 3.4 ? 10 : zoomK >= 2.1 ? 15 : 24);
    const clusterRadius = Math.max(6, baseRadiusPx / zoomK);

    const ranked = pinData
      .map((d) => Object.assign({}, d, {
        _priority: _rankEventPriority(d, { openIdeaSlugs, nowMs }),
      }))
      .sort((a, b) => b._priority - a._priority || a.slug.localeCompare(b.slug));

    const clusters = [];
    ranked.forEach((item) => {
      let bestCluster = null;
      let bestDistance = Number.POSITIVE_INFINITY;
      clusters.forEach((cluster) => {
        const dx = item.px - cluster.px;
        const dy = item.py - cluster.py;
        const distance = Math.sqrt((dx * dx) + (dy * dy));
        if (distance <= clusterRadius && distance < bestDistance) {
          bestDistance = distance;
          bestCluster = cluster;
        }
      });
      if (!bestCluster) {
        clusters.push({
          px: item.px,
          py: item.py,
          events: [item],
          top: item,
          ideaCount: openIdeaSlugs.has(item.slug) ? 1 : 0,
        });
        return;
      }
      bestCluster.events.push(item);
      bestCluster.ideaCount += openIdeaSlugs.has(item.slug) ? 1 : 0;
      const count = bestCluster.events.length;
      bestCluster.px = ((bestCluster.px * (count - 1)) + item.px) / count;
      bestCluster.py = ((bestCluster.py * (count - 1)) + item.py) / count;
      if (
        item._priority > bestCluster.top._priority
        || (item._priority === bestCluster.top._priority && item.slug < bestCluster.top.slug)
      ) {
        bestCluster.top = item;
      }
    });

    return clusters
      .map((cluster) => {
        const sortedEvents = cluster.events
          .slice()
          .sort((a, b) => b._priority - a._priority || a.slug.localeCompare(b.slug));
        const top = cluster.top;
        return Object.assign({}, top, {
          slug: sortedEvents.length === 1
            ? top.slug
            : `cluster:${sortedEvents.map((ev) => ev.slug).sort().join('|')}`,
          title: top.title,
          px: cluster.px,
          py: cluster.py,
          _clusterCount: sortedEvents.length,
          _clusterEvents: sortedEvents,
          _clusterIdeaCount: cluster.ideaCount,
          _priority: top._priority,
        });
      })
      .sort((a, b) => b._priority - a._priority || a.slug.localeCompare(b.slug));
  }

  function _compactLabelText(item, options) {
    const isMobile = !!(options && options.isMobile);
    const maxLen = options && options.maxLen ? options.maxLen : (isMobile ? 14 : 16);
    const fallback = String(item.slug || '').replace(/[-_]+/g, ' ').trim();
    let base = String(item.title || fallback || '').trim();
    if (!base) return fallback || '';
    base = base
      .replace(/\s+[—-]\s+.*/g, '')
      .replace(/:.*/g, '')
      .replace(/\bDay\s+\d+\b/ig, '')
      .replace(/\bWeek\s+\d+\b/ig, '')
      .replace(/\bCONFIRMED\b/ig, '')
      .replace(/\bLargely Holding\b/ig, '')
      .replace(/\bInstitutional Accumulation\b/ig, 'Accumulation')
      .replace(/\bStrategic Competition\b/ig, 'Strategy')
      .replace(/\bNegotiations\b/ig, 'Talks')
      .replace(/\bCeasefire\b/ig, 'Ceasefire')
      .replace(/\bRussia-Ukraine War\b/ig, 'Russia-Ukraine')
      .replace(/\bIsrael-Lebanon War\b/ig, 'Israel-Lebanon')
      .replace(/\bIsrael-Gaza War\b/ig, 'Israel-Gaza')
      .replace(/\bIran-US War\b/ig, 'Iran-US')
      .replace(/\bIran War Energy\b/ig, 'Iran Energy')
      .replace(/\bUkraine Peace Talks\b/ig, 'Ukraine Talks')
      .replace(/\bFree Trade Agreement\b/ig, 'FTA')
      .replace(/\bGlobal Tariff Regime\b/ig, 'Tariffs')
      .replace(/\bPressure Campaign\b/ig, 'Pressure')
      .replace(/\bEnergy Crisis\b/ig, 'Energy')
      .replace(/\s{2,}/g, ' ')
      .trim();

    if (base.length > maxLen) {
      const tokens = base.split(/\s+/).filter(Boolean);
      const preferred = [];
      tokens.forEach((token) => {
        if (preferred.join(' ').length >= maxLen - 2) return;
        if (preferred.length >= (isMobile ? 2 : 3)) return;
        preferred.push(token);
      });
      if (preferred.length > 0) base = preferred.join(' ');
    }

    if (base.length > maxLen) base = `${base.slice(0, maxLen - 1).trimEnd()}…`;
    return base;
  }

  function _computeLabelLayout(pinData, options) {
    const width = options && options.width ? options.width : 800;
    const zoomK = Math.max(1, options && options.zoomK ? options.zoomK : 1);
    const isMobile = !!(options && options.isMobile);
    const openIdeaSlugs = options && options.openIdeaSlugs ? options.openIdeaSlugs : new Set();
    const nowMs = options && options.nowMs ? options.nowMs : Date.now();

    const cellSize = isMobile
      ? Math.max(34, Math.round(54 / Math.min(zoomK, 2.5)))
      : Math.max(18, Math.round(42 / Math.min(zoomK, 3)));
    const perCellCap = isMobile
      ? (zoomK >= 2 ? 2 : 1)
      : (zoomK >= 2.8 ? 3 : zoomK >= 1.6 ? 2 : 1);
    const totalCap = isMobile
      ? (zoomK >= 2.5 ? 8 : 5)
      : (zoomK >= 3 ? 16 : zoomK >= 1.8 ? 12 : 9);
    const labelLen = isMobile
      ? (zoomK >= 2 ? 16 : 12)
      : (zoomK >= 2 ? 20 : 16);

    const candidates = pinData
      .filter(d => d._ns === 'active' || d._nt === 'escalating' || openIdeaSlugs.has(d.slug))
      .map((d) => Object.assign({}, d, {
        _priority: _rankEventPriority(d, { openIdeaSlugs, nowMs }),
      }))
      .sort((a, b) => b._priority - a._priority || a.slug.localeCompare(b.slug));

    const grouped = new Map();
    candidates.forEach((d) => {
      const key = `${Math.round(d.px / cellSize)},${Math.round(d.py / cellSize)}`;
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(d);
    });

    let selected = [];
    grouped.forEach((group, key) => {
      const keep = group.slice(0, perCellCap).map((item, idx) => Object.assign({}, item, {
        _labelIdx: idx,
        _cellKey: key,
      }));
      if (keep.length > 0 && group.length > keep.length) {
        keep[keep.length - 1]._overflowCount = group.length - keep.length;
      }
      selected = selected.concat(keep);
    });

    if (selected.length > totalCap) {
      const selectedByCell = new Map();
      selected
        .sort((a, b) => b._priority - a._priority || a.slug.localeCompare(b.slug))
        .slice(0, totalCap)
        .forEach((item) => {
          if (!selectedByCell.has(item._cellKey)) selectedByCell.set(item._cellKey, []);
          selectedByCell.get(item._cellKey).push(item);
        });

      selected = [];
      grouped.forEach((group, key) => {
        const kept = (selectedByCell.get(key) || [])
          .sort((a, b) => b._priority - a._priority || a.slug.localeCompare(b.slug))
          .map((item, idx) => Object.assign({}, item, {
            _labelIdx: idx,
            _overflowCount: 0,
          }));
        if (kept.length > 0 && group.length > kept.length) {
          kept[kept.length - 1]._overflowCount = group.length - kept.length;
        }
        selected = selected.concat(kept);
      });
    }

    const laidOut = selected
      .sort((a, b) => a.py - b.py || a.px - b.px)
      .map((d) => {
        const raw = _compactLabelText(d, { isMobile, maxLen: labelLen });
        const extraCount = Math.max(0, ((d._clusterCount || 1) - 1) + (d._overflowCount || 0));
        const labelText = extraCount > 0 ? `${raw} +${extraCount}` : raw;
        const approxW = labelText.length * 7 + 18;
        return Object.assign({}, d, {
          _labelText: labelText,
          _labelFlip: (d.px + 8 + approxW) > width - 6,
        });
      });

    const placed = [];
    return laidOut
      .sort((a, b) => b._priority - a._priority || a.py - b.py)
      .map((d) => {
        const labelWidth = Math.min(d._labelText.length * 7 + 20, isMobile ? 128 : 168);
        const baseY = (d.py + 4 + d._labelIdx * 20) - 12;
        const x0 = d._labelFlip ? d.px - 8 - labelWidth : d.px + 8;
        const x1 = x0 + labelWidth;
        const labelHeight = isMobile ? 18 : 20;
        const gap = isMobile ? 3 : 4;
        let offsetY = 0;
        for (let i = 0; i < 8; i += 1) {
          const y0 = baseY + offsetY;
          const y1 = y0 + labelHeight;
          const collides = placed.some((p) => !(x1 < p.x0 || x0 > p.x1 || y1 < p.y0 || y0 > p.y1));
          if (!collides) {
            placed.push({ x0, x1, y0, y1 });
            break;
          }
          offsetY += labelHeight + gap;
        }
        return Object.assign({}, d, { _labelOffsetY: offsetY });
      })
      .sort((a, b) => a.py - b.py || a.px - b.px);
  }

  function _buildFocusTransform(pinData, width, height, options) {
    const padding = options && options.padding != null ? options.padding : 28;
    const maxScale = options && options.maxScale != null ? options.maxScale : 5;
    const minScale = options && options.minScale != null ? options.minScale : 1;
    if (!pinData || pinData.length === 0) {
      return { x: 0, y: 0, k: 1 };
    }
    if (pinData.length === 1) {
      const k = Math.max(minScale, Math.min(maxScale, options && options.singlePointScale ? options.singlePointScale : 2.8));
      return {
        x: width / 2 - pinData[0].px * k,
        y: height / 2 - pinData[0].py * k,
        k,
      };
    }
    const xs = pinData.map(d => d.px);
    const ys = pinData.map(d => d.py);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const innerWidth = Math.max(40, width - padding * 2);
    const innerHeight = Math.max(40, height - padding * 2);
    const dx = Math.max(1, maxX - minX);
    const dy = Math.max(1, maxY - minY);
    const scale = Math.max(minScale, Math.min(maxScale, Math.min(innerWidth / dx, innerHeight / dy)));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    return {
      x: width / 2 - cx * scale,
      y: height / 2 - cy * scale,
      k: scale,
    };
  }

  function _zoomIdentity() {
    return (typeof d3 !== 'undefined' && d3.zoomIdentity) ? d3.zoomIdentity : { x: 0, y: 0, k: 1 };
  }

  function _transformFromState(state) {
    return _zoomIdentity().translate(state.x, state.y).scale(state.k);
  }

  function _scheduleRenderPins() {
    if (_resizeRAF) return;
    _resizeRAF = requestAnimationFrame(() => {
      _resizeRAF = null;
      _renderPins();
    });
  }

  function _resetView(options) {
    if (!_svg || !_zoom) return;
    const transition = options && options.transition;
    const target = _zoomIdentity();
    if (transition) _svg.transition().duration(200).call(_zoom.transform, target);
    else _svg.call(_zoom.transform, target);
  }

  function _focusVisibleEvents(options) {
    if (!_svg || !_zoom || !_container) return;
    const width = _container.clientWidth || 800;
    const isMobile = width < 500;
    const height = isMobile
      ? Math.max(220, Math.round(width * 0.6))
      : Math.max(300, Math.round(width * 0.42));
    const pinData = _events.map(ev => {
      const p = _projection([ev.lon, ev.lat]);
      return p ? Object.assign({}, ev, { px: p[0], py: p[1] }) : null;
    }).filter(Boolean);
    const focus = _buildFocusTransform(pinData, width, height, {
      padding: isMobile ? 18 : 28,
      maxScale: isMobile ? 2.6 : 3.4,
      singlePointScale: isMobile ? 2.2 : 2.8,
    });
    const target = _transformFromState(focus);
    const transition = options && options.transition;
    if (transition) _svg.transition().duration(200).call(_zoom.transform, target);
    else _svg.call(_zoom.transform, target);
  }

  function _focusCluster(clusterEvents, options) {
    if (!_svg || !_zoom || !_container || !clusterEvents || clusterEvents.length === 0) return;
    const width = _container.clientWidth || 800;
    const isMobile = width < 500;
    const height = isMobile
      ? Math.max(220, Math.round(width * 0.6))
      : Math.max(300, Math.round(width * 0.42));
    const focus = _buildFocusTransform(clusterEvents, width, height, {
      padding: isMobile ? 16 : 24,
      maxScale: isMobile ? 3.1 : 4.1,
      singlePointScale: isMobile ? 2.4 : 3.0,
    });
    const target = _transformFromState(focus);
    const transition = options && options.transition;
    if (transition) _svg.transition().duration(220).call(_zoom.transform, target);
    else _svg.call(_zoom.transform, target);
  }

  function _mountControls() {
    if (!_container || _controls) return;
    const controls = document.createElement('div');
    controls.className = 'wm-controls';
    controls.style.cssText = [
      'position:absolute',
      'top:10px',
      'right:10px',
      'display:flex',
      'gap:6px',
      'z-index:4',
      'pointer-events:auto',
    ].join(';');

    const makeButton = (label, title, onClick) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'wm-control-btn';
      btn.title = title;
      btn.setAttribute('aria-label', title);
      btn.textContent = label;
      btn.style.cssText = [
        'height:28px',
        'min-width:28px',
        'padding:0 8px',
        'border-radius:4px',
        'border:1px solid #00ff4133',
        'background:rgba(0,8,0,0.85)',
        'color:#00ff41',
        'font:600 13px/1 "Courier New",monospace',
        'cursor:pointer',
      ].join(';');
      btn.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        onClick();
      });
      return btn;
    };

    controls.appendChild(makeButton('+', 'Zoom in', () => {
      if (_svg && _zoom) _svg.transition().duration(150).call(_zoom.scaleBy, 1.35);
    }));
    controls.appendChild(makeButton('−', 'Zoom out', () => {
      if (_svg && _zoom) _svg.transition().duration(150).call(_zoom.scaleBy, 1 / 1.35);
    }));
    controls.appendChild(makeButton('↺', 'Reset map', () => _resetView({ transition: true })));

    _container.style.position = 'relative';
    _container.appendChild(controls);
    _controls = controls;
  }

  async function init(containerId, options) {
    try {
      options = options || {};
      _container = document.getElementById(containerId);
      if (!_container) return;
      if (typeof d3 === 'undefined' || typeof topojson === 'undefined') {
        _container.innerHTML = '<div style="color:#00ff41;padding:24px;text-align:center;font-family:monospace;font-size:0.8rem">[ MAP OFFLINE — D3 NOT LOADED ]</div>';
        return;
      }

      _onSeeIdeas = options.onSeeIdeas || null;

      const width = _container.clientWidth || 800;
      const isMobile = width < 500;
      const height = isMobile
        ? Math.max(220, Math.round(width * 0.6))
        : Math.max(300, Math.round(width * 0.42));

      _container.innerHTML = '';

      // SVG with defs for filters and gradients
      _svg = d3.select(_container)
        .append('svg')
        .attr('width', '100%')
        .attr('height', height)
        .attr('viewBox', `0 0 ${width} ${height}`)
        .style('display', 'block')
        .style('border-radius', '6px')
        .style('overflow', 'hidden');

      const defs = _svg.append('defs');

      // Matrix green glow filter
      const glowFilter = defs.append('filter').attr('id', 'wm-glow').attr('x', '-50%').attr('y', '-50%').attr('width', '200%').attr('height', '200%');
      glowFilter.append('feGaussianBlur').attr('in', 'SourceGraphic').attr('stdDeviation', '2').attr('result', 'blur');
      glowFilter.append('feMerge').selectAll('feMergeNode').data(['blur', 'SourceGraphic']).enter().append('feMergeNode').attr('in', d => d);

      // Pin glow filter (stronger)
      const pinGlow = defs.append('filter').attr('id', 'wm-pin-glow-f').attr('x', '-100%').attr('y', '-100%').attr('width', '300%').attr('height', '300%');
      pinGlow.append('feGaussianBlur').attr('in', 'SourceGraphic').attr('stdDeviation', '4').attr('result', 'blur');
      pinGlow.append('feMerge').selectAll('feMergeNode').data(['blur', 'SourceGraphic']).enter().append('feMergeNode').attr('in', d => d);

      // Scanline pattern
      const scanPat = defs.append('pattern').attr('id', 'wm-scanlines').attr('width', '1').attr('height', '4').attr('patternUnits', 'userSpaceOnUse');
      scanPat.append('rect').attr('width', '1').attr('height', '2').attr('fill', '#000').attr('opacity', '0.08');

      // Vignette radial gradient
      const vignette = defs.append('radialGradient').attr('id', 'wm-vignette').attr('cx', '50%').attr('cy', '50%').attr('r', '60%');
      vignette.append('stop').attr('offset', '0%').attr('stop-color', 'transparent');
      vignette.append('stop').attr('offset', '100%').attr('stop-color', '#000').attr('stop-opacity', '0.7');

      // Grid pattern for ocean
      const gridPat = defs.append('pattern').attr('id', 'wm-grid').attr('width', '40').attr('height', '40').attr('patternUnits', 'userSpaceOnUse');
      gridPat.append('path').attr('d', 'M 40 0 L 0 0 0 40').attr('fill', 'none').attr('stroke', '#00ff41').attr('stroke-width', '0.15').attr('opacity', '0.3');

      // Tooltip
      _tooltip = document.createElement('div');
      _tooltip.id = 'wm-tooltip';
      _tooltip.style.cssText = [
        'position:fixed', 'display:none', 'z-index:10002',
        'background:rgba(0,8,0,0.95)', 'border:1px solid #00ff4155',
        'border-radius:4px', 'padding:12px 14px',
        'min-width:220px', 'max-width:320px',
        'box-shadow:0 0 20px rgba(0,255,65,0.15), 0 8px 32px rgba(0,0,0,0.8)',
        'pointer-events:none', 'font-family:"Courier New",monospace',
        'font-size:0.78rem', 'color:#00ff41',
        'backdrop-filter:blur(8px)',
      ].join(';');
      document.body.appendChild(_tooltip);

      _projection = d3.geoNaturalEarth1()
        .scale(width / 6.2)
        .translate([width / 2, height / 2]);
      _path = d3.geoPath().projection(_projection);

      _zoom = d3.zoom()
        .scaleExtent([1, isMobile ? 4 : 8])
        .on('zoom', (event) => {
          const t = event.transform;
          _currentTransform = t;
          _g.attr('transform', t);
          _scheduleRenderPins();
        });

      _currentTransform = _zoomIdentity();
      _svg.call(_zoom).style('touch-action', 'none');

      _g = _svg.append('g');
      _overlay = _svg.append('g')
        .attr('class', 'wm-overlay')
        .style('pointer-events', 'none');

      // Ocean background with grid
      _g.append('path')
        .datum({ type: 'Sphere' })
        .attr('d', _path)
        .attr('fill', '#020a02');
      _g.append('path')
        .datum({ type: 'Sphere' })
        .attr('d', _path)
        .attr('fill', 'url(#wm-grid)')
        .attr('opacity', 0.5);

      // Graticule — matrix green grid lines
      const graticule = d3.geoGraticule().step([20, 20])();
      _g.append('path')
        .datum(graticule)
        .attr('d', _path)
        .attr('fill', 'none')
        .attr('stroke', '#00ff41')
        .attr('stroke-width', 0.2)
        .attr('opacity', 0.15);

      // Load world data
      try {
        const topo = await d3.json('/static/world-110m.json');
        _worldData = topojson.feature(topo, topo.objects.countries);

        // Country fills — dark with green edges
        _g.append('g')
          .attr('class', 'countries')
          .selectAll('path')
          .data(_worldData.features)
          .enter()
          .append('path')
          .attr('d', _path)
          .attr('fill', '#0a1a0a')
          .attr('stroke', '#00ff41')
          .attr('stroke-width', 0.3)
          .attr('stroke-opacity', 0.25)
          .on('mouseenter', function() {
            d3.select(this)
              .attr('fill', '#0d2a0d')
              .attr('stroke-opacity', 0.5);
          })
          .on('mouseleave', function() {
            d3.select(this)
              .attr('fill', '#0a1a0a')
              .attr('stroke-opacity', 0.25);
          });

        // Country borders — brighter
        _g.append('path')
          .datum(topojson.mesh(topo, topo.objects.countries, (a, b) => a !== b))
          .attr('d', _path)
          .attr('fill', 'none')
          .attr('stroke', '#00ff41')
          .attr('stroke-width', 0.35)
          .attr('stroke-opacity', 0.2);

      } catch (e) {
        console.warn('WorldMap: failed to load world-110m.json', e);
        _g.append('text').attr('x', width / 2).attr('y', height / 2)
          .attr('text-anchor', 'middle').attr('fill', '#00ff41').attr('font-family', 'monospace')
          .text('[ MAP DATA UNAVAILABLE ]');
      }

      // Scanline overlay
      _svg.append('rect')
        .attr('width', width).attr('height', height)
        .attr('fill', 'url(#wm-scanlines)')
        .attr('pointer-events', 'none');

      // Vignette overlay
      _svg.append('rect')
        .attr('width', width).attr('height', height)
        .attr('fill', 'url(#wm-vignette)')
        .attr('pointer-events', 'none');

      // Globe outline glow
      _g.append('path')
        .datum({ type: 'Sphere' })
        .attr('d', _path)
        .attr('fill', 'none')
        .attr('stroke', '#00ff41')
        .attr('stroke-width', 0.8)
        .attr('stroke-opacity', 0.3)
        .attr('filter', 'url(#wm-glow)');

      // Inject animations CSS
      if (!document.getElementById('wm-pulse-style')) {
        const style = document.createElement('style');
        style.id = 'wm-pulse-style';
        style.textContent = `
          @keyframes wm-pulse {
            0%   { r: 10; opacity: 0.6; }
            50%  { r: 20; opacity: 0; }
            100% { r: 20; opacity: 0; }
          }
          .wm-pin-pulse { animation: wm-pulse 2.5s ease-out infinite; }
          @keyframes wm-ring-breathe {
            0%, 100% { opacity: 0.4; }
            50% { opacity: 0.9; }
          }
          .wm-pin-ring { animation: wm-ring-breathe 2s ease-in-out infinite; }
          @keyframes wm-scan {
            0% { transform: translateY(-100%); }
            100% { transform: translateY(100%); }
          }
        `;
        document.head.appendChild(style);
      }

      // Pins layer
      _g.append('g').attr('class', 'pins');
      _mountControls();
      _initialized = true;

      _svg.on('click', () => _hideTooltip());

      if (window.ResizeObserver) {
        new ResizeObserver(() => {
          if (!_resizeRAF) _resizeRAF = requestAnimationFrame(() => { _resizeRAF = null; _onResize(); });
        }).observe(_container);
      }

      if (_events.length > 0) _renderPins();

    } catch (err) {
      console.warn('WorldMap: init failed', err);
      if (_container) _container.innerHTML = '<div style="color:#00ff41;padding:24px;text-align:center;font-family:monospace">[ INIT FAILED ]</div>';
    }
  }

  function _onResize() {
    if (!_svg || !_container || !_projection || !_path) return;
    const width = _container.clientWidth || 800;
    const isMob = width < 500;
    const height = isMob
      ? Math.max(220, Math.round(width * 0.6))
      : Math.max(300, Math.round(width * 0.42));
    _svg.attr('viewBox', `0 0 ${width} ${height}`).attr('height', height);
    _projection.scale(width / 6.2).translate([width / 2, height / 2]);
    _g.selectAll('path').attr('d', _path);
    // Update overlay rects
    _svg.selectAll('rect').attr('width', width).attr('height', height);
    _renderPins();
  }

  // Coordinate inference (unchanged)
  const REGION_COORDS = {
    'North America':  [38.0, -97.0],  'South America':  [-15.0, -60.0],
    'Europe':         [50.0, 15.0],    'Middle East':    [30.0, 45.0],
    'Africa':         [5.0, 20.0],     'South Asia':     [25.0, 75.0],
    'East Asia':      [35.0, 115.0],   'Southeast Asia': [10.0, 110.0],
    'Central Asia':   [45.0, 65.0],    'Oceania':        [-25.0, 135.0],
    'Global':         [20.0, 0.0],
  };

  const SLUG_COORD_HINTS = [
    [['iran','hormuz','tehran'],           [32.0,  53.0]],
    [['ukraine','kyiv','zelens'],          [49.0,  32.0]],
    [['russia','moscow','kremlin'],        [55.0,  37.0]],
    [['china','beijing','taiwan','hong-kong'], [35.0, 105.0]],
    [['israel','gaza','palestine','lebanon','beirut','litani'], [31.5, 34.8]],
    [['pakistan','islamabad','lahore'],     [30.0,  70.0]],
    [['afghanistan','kabul','kandahar'],    [34.0,  65.0]],
    [['india','delhi','mumbai'],           [20.0,  77.0]],
    [['korea','pyongyang','seoul'],         [37.5, 127.0]],
    [['japan','tokyo','osaka'],             [36.0, 138.0]],
    [['britain','london','england'],        [51.5,  -0.1]],
    [['france','paris','lyon'],             [46.0,   2.0]],
    [['germany','berlin'],                  [51.0,  10.0]],
    [['italy','rome','milan','meloni'],     [42.0,  12.0]],
    [['turkey','ankara','istanbul'],        [39.0,  35.0]],
    [['syria','damascus'],                  [34.8,  38.9]],
    [['saudi','riyadh'],                    [24.0,  45.0]],
    [['cuba','havana'],                     [22.0, -79.0]],
    [['venezuela','caracas'],               [ 8.0, -66.0]],
    [['brazil','brasilia','lula'],          [-15.0,-47.0]],
    [['sudan','darfur','khartoum'],         [15.0,  32.0]],
    [['hungary','budapest','orban'],        [47.5,  19.0]],
    [['ethiopia','kenya','nigeria','africa'], [5.0, 20.0]],
    [['middle-east','gulf','opec','hormuz'], [26.0, 50.0]],
    [['europe','nato','eu-'],               [50.0,  15.0]],
    [['uk-','uk '],                         [51.5,  -0.1]],
    [['diego garcia'],                      [-7.3,  72.4]],
    [['arctic','norway'],                   [70.0,  20.0]],
    [['energy','oil','gas'],                [25.0,  50.0]],
    [['btc','bitcoin','crypto','eth'],      [37.0, -122.0]], // Silicon Valley
    [['dhs','tsa','ice','shutdown'],        [38.9, -77.0]], // Washington DC
    [['fed','powell','treasury'],           [38.9, -77.0]],
    [['weather','storm','hurricane'],       [38.0, -97.0]],
    [['us-','usa','america','washington','pentagon','trump'], [38.9, -77.0]],
    [['global','world','international'],    [20.0,   0.0]],
  ];

  function _inferCoords(ev) {
    if (ev.lat != null && ev.lon != null) return [ev.lat, ev.lon];
    const slug = (ev.slug || '').toLowerCase();
    const title = (ev.title || '').toLowerCase();
    const text = slug + ' ' + title;
    for (const [keywords, coords] of SLUG_COORD_HINTS) {
      if (keywords.some(kw => text.includes(kw))) return coords;
    }
    if (ev.region && REGION_COORDS[ev.region]) return REGION_COORDS[ev.region];
    return null;
  }

  function setEvents(events, options) {
    _events = (events || []).map(ev => {
      const coords = _inferCoords(ev);
      if (!coords) return null;
      return Object.assign({}, ev, { lat: coords[0], lon: coords[1], _ns: _normStatus(ev.status), _nt: _normTraj(ev.trajectory) });
    }).filter(Boolean);
    if (_initialized) {
      const view = options && options.view ? options.view : 'preserve';
      if (view === 'fit' && _events.length > 0) _focusVisibleEvents({ transition: false });
      else if (view === 'reset') _resetView({ transition: false });
      else _renderPins();
    }
  }

  function setIdeasWithOpenIdeas(slugSet) {
    _openIdeaSlugs = slugSet instanceof Set ? slugSet : new Set(slugSet);
    if (_initialized) _renderPins();
  }

  function highlightEvent(slug) {
    if (!_g) return;
    _g.selectAll('.wm-pin-dot')
      .attr('stroke', d => (d.slug === slug || (d._clusterEvents || []).some(ev => ev.slug === slug)) ? '#ffffff' : 'none')
      .attr('stroke-width', d => (d.slug === slug || (d._clusterEvents || []).some(ev => ev.slug === slug)) ? 2 : 0);
    _g.selectAll('.wm-pin-ring')
      .attr('stroke', d => (d.slug === slug || (d._clusterEvents || []).some(ev => ev.slug === slug)) ? '#ffffff' : (STATUS_COLOR[d._ns] || '#00ff41'))
      .attr('stroke-width', d => (d.slug === slug || (d._clusterEvents || []).some(ev => ev.slug === slug)) ? 2 : 1);
    setTimeout(() => {
      _g.selectAll('.wm-pin-dot').attr('stroke', 'none').attr('stroke-width', 0);
      _g.selectAll('.wm-pin-ring').each(function(d) {
        d3.select(this).attr('stroke', STATUS_COLOR[d._ns] || '#00ff41').attr('stroke-width', 1);
      });
    }, 2000);
  }

  function _renderPins() {
    if (!_g || !_projection || !_overlay) return;
    const pinsGroup = _g.select('.pins');
    if (pinsGroup.empty()) return;
    const overlayGroup = _overlay;
    const zoomK = _currentTransform && Number.isFinite(_currentTransform.k) ? _currentTransform.k : 1;
    const radiusScale = 1 / zoomK;
    const transformX = _currentTransform && Number.isFinite(_currentTransform.x) ? _currentTransform.x : 0;
    const transformY = _currentTransform && Number.isFinite(_currentTransform.y) ? _currentTransform.y : 0;
    const mapWidth = _container ? _container.clientWidth : 800;
    const isMobMap = mapWidth < 500;

    const pinData = _events.map(ev => {
      const p = _projection([ev.lon, ev.lat]);
      return p ? Object.assign({}, ev, { px: p[0], py: p[1], _ns: _normStatus(ev.status), _nt: _normTraj(ev.trajectory) }) : null;
    }).filter(Boolean);
    const clusterData = _computeClusters(pinData, {
      zoomK,
      isMobile: isMobMap,
      openIdeaSlugs: _openIdeaSlugs,
    });

    // Glow circles (ambient light behind pins)
    const glows = pinsGroup.selectAll('.wm-pin-glow-circle').data(clusterData, d => d.slug);
    glows.enter().append('circle').attr('class', 'wm-pin-glow-circle')
      .merge(glows)
      .attr('cx', d => d.px).attr('cy', d => d.py)
      .attr('r', d => (10 + Math.min(((d._clusterCount || 1) - 1) * 2, 16)) * radiusScale)
      .attr('fill', d => STATUS_GLOW[d._ns] || '#ff333333')
      .attr('pointer-events', 'none');
    glows.exit().remove();

    // Pulse rings for escalating events
    const pulses = pinsGroup.selectAll('.wm-pin-pulse').data(
      clusterData.filter(d => d._nt === 'escalating'), d => d.slug
    );
    pulses.enter().append('circle').attr('class', 'wm-pin-pulse')
      .merge(pulses)
      .attr('cx', d => d.px).attr('cy', d => d.py)
      .attr('r', d => (12 + Math.min(((d._clusterCount || 1) - 1) * 1.5, 10)) * radiusScale)
      .attr('fill', 'none')
      .attr('stroke', d => STATUS_COLOR[d._ns] || '#ff3333')
      .attr('stroke-width', 1.5 * radiusScale)
      .style('opacity', 0.6);
    pulses.exit().remove();

    // Outer ring (status color, breathing)
    const rings = pinsGroup.selectAll('.wm-pin-ring').data(clusterData, d => d.slug);
    rings.enter().append('circle').attr('class', 'wm-pin-ring')
      .merge(rings)
      .attr('cx', d => d.px).attr('cy', d => d.py)
      .attr('r', d => (7 + Math.min(((d._clusterCount || 1) - 1) * 1.6, 9)) * radiusScale)
      .attr('fill', 'none')
      .attr('stroke', d => STATUS_COLOR[d._ns] || '#00ff41')
      .attr('stroke-width', 1 * radiusScale)
      .style('cursor', 'pointer')
      .on('mouseenter', function(event, d) { _showTooltip(event, d, false); })
      .on('mouseleave', function() { _hideTooltip(); })
      .on('click', function(event, d) {
        event.stopPropagation();
        if ((d._clusterCount || 1) > 1) _focusCluster(d._clusterEvents, { transition: true });
        _showTooltip(event, d, true);
      });
    rings.exit().remove();

    // Inner dot (solid, bright)
    const dots = pinsGroup.selectAll('.wm-pin-dot').data(clusterData, d => d.slug);
    dots.enter().append('circle').attr('class', 'wm-pin-dot')
      .merge(dots)
      .attr('cx', d => d.px).attr('cy', d => d.py)
      .attr('r', d => (4.5 + Math.min(((d._clusterCount || 1) - 1) * 1.1, 5)) * radiusScale)
      .attr('fill', d => STATUS_COLOR[d._ns] || '#ff3333')
      .attr('filter', 'url(#wm-pin-glow-f)')
      .style('cursor', 'pointer')
      .on('mouseenter', function(event, d) { _showTooltip(event, d, false); })
      .on('mouseleave', function() { _hideTooltip(); })
      .on('click', function(event, d) {
        event.stopPropagation();
        if ((d._clusterCount || 1) > 1) _focusCluster(d._clusterEvents, { transition: true });
        _showTooltip(event, d, true);
      });
    dots.exit().remove();

    const overlayData = clusterData.map((item) => Object.assign({}, item, {
      sx: transformX + item.px * zoomK,
      sy: transformY + item.py * zoomK,
    }));

    const clusterCounts = overlayGroup.selectAll('.wm-cluster-count').data(
      overlayData.filter(d => (d._clusterCount || 1) > 1),
      d => d.slug
    );
    const clusterBadgeBg = overlayGroup.selectAll('.wm-cluster-badge-bg').data(
      overlayData.filter(d => (d._clusterCount || 1) > 1),
      d => d.slug
    );
    clusterBadgeBg.enter().append('circle').attr('class', 'wm-cluster-badge-bg')
      .merge(clusterBadgeBg)
      .attr('cx', d => d.sx + 10)
      .attr('cy', d => d.sy - 10)
      .attr('r', d => ((d._clusterCount || 1) >= 10 ? 9 : 8))
      .attr('fill', '#d8ef72')
      .attr('stroke', '#0b1507')
      .attr('stroke-width', 1.25)
      .style('pointer-events', 'none');
    clusterBadgeBg.exit().remove();
    clusterCounts.enter().append('text').attr('class', 'wm-cluster-count')
      .merge(clusterCounts)
      .attr('x', d => d.sx + 10)
      .attr('y', d => d.sy - 7)
      .attr('text-anchor', 'middle')
      .attr('fill', '#15250c')
      .attr('font-size', '10px')
      .attr('font-family', '"Courier New",monospace')
      .attr('font-weight', '700')
      .style('pointer-events', 'none')
      .text(d => String(d._clusterCount || 1));
    clusterCounts.exit().remove();

    // Idea markers (diamond shape for events with open trade ideas)
    const ideaData = overlayData.filter(d => (d._clusterIdeaCount || 0) > 0);
    const ideaMarkers = overlayGroup.selectAll('.wm-idea-marker').data(ideaData, d => d.slug);
    ideaMarkers.enter().append('text').attr('class', 'wm-idea-marker')
      .merge(ideaMarkers)
      .attr('x', d => d.sx).attr('y', d => d.sy - 13)
      .attr('text-anchor', 'middle').attr('fill', '#ccff00').attr('font-size', '11px')
      .attr('filter', 'url(#wm-glow)')
      .text(d => (d._clusterIdeaCount || 0) > 1 ? `◆${d._clusterIdeaCount}` : '◆');
    ideaMarkers.exit().remove();

    // Labels
    const labelData = _computeLabelLayout(overlayData.map((item) => Object.assign({}, item, {
      px: item.sx,
      py: item.sy,
    })), {
      width: mapWidth,
      isMobile: isMobMap,
      zoomK,
      openIdeaSlugs: _openIdeaSlugs,
    });

    const labelAccent = overlayGroup.selectAll('.wm-pin-label-accent').data(labelData, d => d.slug);
    labelAccent.enter().append('rect').attr('class', 'wm-pin-label-accent')
      .attr('rx', 2).attr('pointer-events', 'none')
      .merge(labelAccent)
      .attr('fill', d => STATUS_COLOR[d._ns] || '#8ef7a9')
      .attr('opacity', 0.95)
      .attr('x', d => {
        const w = Math.min(d._labelText.length * 7 + 20, isMobMap ? 128 : 168);
        return d._labelFlip ? d.px - 8 - w : d.px + 8;
      })
      .attr('y', d => (d.py + 4 + d._labelIdx * 20) - 12)
      .attr('transform', d => d._labelOffsetY ? `translate(0, ${d._labelOffsetY})` : null)
      .attr('width', 3)
      .attr('height', isMobMap ? 18 : 20);
    labelAccent.exit().remove();

    // Label backgrounds
    const labelBg = overlayGroup.selectAll('.wm-pin-label-bg').data(labelData, d => d.slug);
    labelBg.enter().append('rect').attr('class', 'wm-pin-label-bg')
      .attr('rx', 6).attr('pointer-events', 'none')
      .merge(labelBg)
      .attr('fill', 'rgba(5,10,9,0.92)')
      .attr('stroke', 'rgba(101,130,113,0.35)')
      .attr('stroke-width', 0.8)
      .attr('x', d => {
        const w = Math.min(d._labelText.length * 7 + 20, isMobMap ? 128 : 168);
        return d._labelFlip ? d.px - 8 - w : d.px + 8;
      })
      .attr('y', d => (d.py + 4 + d._labelIdx * 20) - 12)
      .attr('transform', d => d._labelOffsetY ? `translate(0, ${d._labelOffsetY})` : null)
      .attr('width', d => Math.min(d._labelText.length * 7 + 20, isMobMap ? 128 : 168))
      .attr('height', isMobMap ? 18 : 20);
    labelBg.exit().remove();

    // Label text
    const labels = overlayGroup.selectAll('.wm-pin-label').data(labelData, d => d.slug);
    labels.enter().append('text').attr('class', 'wm-pin-label wm-label').attr('pointer-events', 'none')
      .merge(labels)
      .attr('x', d => d._labelFlip ? d.px - 12 : d.px + 14)
      .attr('y', d => d.py + 4 + d._labelIdx * 20)
      .attr('transform', d => d._labelOffsetY ? `translate(0, ${d._labelOffsetY})` : null)
      .attr('fill', '#edf6ee')
      .attr('font-size', isMobMap ? '11px' : '12px')
      .attr('font-family', '"IBM Plex Mono","SFMono-Regular","Courier New",monospace')
      .attr('font-weight', '600')
      .attr('letter-spacing', '0.1px')
      .attr('text-anchor', d => d._labelFlip ? 'end' : 'start')
      .text(d => d._labelText);
    labels.exit().remove();
  }

  function _showTooltip(event, d, sticky) {
    const sc = STATUS_COLOR[d._ns] || '#ff3333';
    const ti = TRAJ_ICON[d._nt] || '?';
    const tc = TRAJ_COLOR[d._nt] || '#ffaa00';
    const clusterEvents = Array.isArray(d._clusterEvents) ? d._clusterEvents : [d];
    const clusterCount = d._clusterCount || 1;
    const clusterIdeas = d._clusterIdeaCount || 0;
    const hasIdea = clusterIdeas > 0 || _openIdeaSlugs.has(d.slug);
    const isMobTT = window.innerWidth < 600;
    const sumLen = isMobTT ? 90 : 140;
    const sumText = (d.summary || '').slice(0, sumLen);
    const ellip = (d.summary || '').length > sumLen ? '...' : '';
    const clusterList = clusterEvents
      .slice(0, 4)
      .map((ev) => {
        const evStatus = STATUS_COLOR[ev._ns] || '#00ff41';
        return `<li style="margin:2px 0;color:#9fe39f"><span style="color:${evStatus}">●</span> ${_esc(ev.title || ev.slug)}</li>`;
      })
      .join('');
    const extraClusterCount = Math.max(0, clusterCount - 4);
    const primaryIdeaSlug = clusterEvents.find((ev) => _openIdeaSlugs.has(ev.slug))?.slug || d.slug;

    _tooltip.innerHTML = [
      `<div style="font-weight:700;font-size:0.85rem;margin-bottom:6px;color:#e0ffe0;line-height:1.3;font-family:'Courier New',monospace">${clusterCount > 1 ? `${clusterCount} linked events` : _esc(d.title || d.slug)}</div>`,
      `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">`,
        `<span style="color:${sc};border:1px solid ${sc}55;background:${sc}15;border-radius:3px;padding:1px 8px;font-size:0.7rem;font-weight:700;font-family:monospace;letter-spacing:0.5px">${(d._ns || 'active').toUpperCase()}</span>`,
        `<span style="color:${tc};font-size:0.78rem;font-family:monospace">${ti} ${(d._nt || 'unknown').replace(/-/g,' ')}</span>`,
        hasIdea ? '<span style="color:#ccff00;font-size:0.7rem;font-weight:700;font-family:monospace">◆ TRADE IDEA</span>' : '',
        clusterCount > 1 ? `<span style="color:#9fe39f;font-size:0.72rem;font-family:monospace">${clusterIdeas > 0 ? `${clusterIdeas} idea-linked` : 'clustered hotspot'}</span>` : '',
      `</div>`,
      clusterCount > 1 ? `<div style="color:#7db57d;font-size:0.72rem;line-height:1.45;margin-bottom:8px;font-family:monospace">${_esc(d.title || d.slug)}</div>` : '',
      d.status_note ? `<div style="color:#00ff4188;font-size:0.72rem;font-style:italic;margin-bottom:4px;font-family:monospace">// ${_esc(d.status_note)}</div>` : '',
      clusterCount > 1 ? `<ul style="margin:0 0 8px 16px;padding:0;font-size:0.72rem;line-height:1.4;font-family:monospace">${clusterList}${extraClusterCount > 0 ? `<li style="margin:2px 0;color:#6e996e">+${extraClusterCount} more in this cluster</li>` : ''}</ul>` : '',
      d.summary ? `<div style="color:#88bb88;font-size:0.75rem;line-height:1.4;margin-bottom:6px;font-family:monospace">${_esc(sumText)}${ellip}</div>` : '',
      clusterCount > 1 ? `<button id="wm-zoom-cluster-btn" style="background:#0f2a0f;color:#9fe39f;border:1px solid #2c8f4a;border-radius:3px;padding:3px 12px;font-size:0.7rem;cursor:pointer;font-weight:700;font-family:monospace;letter-spacing:0.5px;margin-right:6px">ZOOM CLUSTER</button>` : '',
      (hasIdea && _onSeeIdeas) ? `<button id="wm-see-ideas-btn" style="background:#00ff4115;color:#00ff41;border:1px solid #00ff4133;border-radius:3px;padding:3px 12px;font-size:0.7rem;cursor:pointer;font-weight:700;font-family:monospace;letter-spacing:0.5px">VIEW IDEAS ↓</button>` : '',
    ].join('');

    _tooltip.style.display = 'block';
    _tooltip.style.pointerEvents = sticky ? 'auto' : 'none';

    if (isMobTT) {
      const ttW = Math.min(window.innerWidth - 24, 400);
      _tooltip.style.minWidth = ttW + 'px';
      _tooltip.style.maxWidth = ttW + 'px';
      const mapRect = _container ? _container.getBoundingClientRect() : { top: 0, bottom: 200 };
      const tx = Math.max(12, Math.round((window.innerWidth - ttW) / 2));
      let ty = mapRect.bottom + 8;
      if (ty + 260 > window.innerHeight) ty = Math.max(8, mapRect.top - 268);
      _tooltip.style.left = tx + 'px';
      _tooltip.style.top = ty + 'px';
    } else {
      _tooltip.style.minWidth = '240px';
      _tooltip.style.maxWidth = '320px';
      let tx = event.clientX + 16;
      let ty = event.clientY - 10;
      if (tx + 330 > window.innerWidth) tx = event.clientX - 330;
      if (ty + 240 > window.innerHeight) ty = event.clientY - 220;
      _tooltip.style.left = Math.max(8, tx) + 'px';
      _tooltip.style.top = Math.max(8, ty) + 'px';
    }

    const seeBtn = document.getElementById('wm-see-ideas-btn');
    if (seeBtn && _onSeeIdeas) {
      seeBtn.addEventListener('click', function(e) { e.stopPropagation(); _hideTooltip(); _onSeeIdeas(primaryIdeaSlug); });
    }
    const zoomBtn = document.getElementById('wm-zoom-cluster-btn');
    if (zoomBtn && clusterCount > 1) {
      zoomBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        _focusCluster(clusterEvents, { transition: true });
      });
    }
  }

  function _hideTooltip() { if (_tooltip) _tooltip.style.display = 'none'; }

  return {
    init,
    setEvents,
    setIdeasWithOpenIdeas,
    highlightEvent,
    __test__: {
      rankEventPriority: _rankEventPriority,
      computeClusters: _computeClusters,
      computeLabelLayout: _computeLabelLayout,
      buildFocusTransform: _buildFocusTransform,
    },
  };
})();
