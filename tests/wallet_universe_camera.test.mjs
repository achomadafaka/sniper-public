import test from 'node:test';
import assert from 'node:assert/strict';
import camera from '../static/wallet_universe.js';
const { worldAt, zoomAt, panBy, fitPoints, pinchCamera } = camera;

const viewport = { width: 800, height: 600 };
const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-8, `${a} differs from ${b}`);

test('zoom preserves the world point under an off-center cursor', () => {
  const camera = { x: 120, y: -70, k: .5 };
  const cursor = { x: 710, y: 110 };
  const before = worldAt(camera, cursor, viewport);
  const after = worldAt(zoomAt(camera, 2, cursor, viewport, { min: .1, max: 3 }), cursor, viewport);
  close(before.x, after.x);
  close(before.y, after.y);
});

test('zoom limits preserve the anchor and never invert the map', () => {
  const camera = { x: 20, y: 30, k: 1 };
  const cursor = { x: 10, y: 590 };
  const original = worldAt(camera, cursor, viewport);
  for (const [factor, expected] of [[100, 3], [.001, .1]]) {
    const next = zoomAt(camera, factor, cursor, viewport, { min: .1, max: 3 });
    assert.equal(next.k, expected);
    const anchored = worldAt(next, cursor, viewport);
    close(original.x, anchored.x);
    close(original.y, anchored.y);
  }
  assert.throws(() => zoomAt(camera, 0, cursor, viewport, { min: .1, max: 3 }));
});

test('drag moves the map by the same screen distance at every zoom', () => {
  for (const k of [.1, 1, 3]) {
    const camera = { x: 50, y: 100, k };
    const world = worldAt(camera, { x: 320, y: 190 }, viewport);
    const next = panBy(camera, 75, -35);
    close((world.x - next.x) * k + viewport.width / 2, 395);
    close((world.y - next.y) * k + viewport.height / 2, 155);
  }
});

test('fit includes the whole planet radius within a narrow phone viewport', () => {
  const phone = { width: 375, height: 470 };
  const points = [{ x: -200, y: 50, r: 80 }, { x: 140, y: -100, r: 40 }];
  const camera = fitPoints(points, phone, 30);
  for (const point of points) {
    const x = (point.x - camera.x) * camera.k + phone.width / 2;
    const y = (point.y - camera.y) * camera.k + phone.height / 2;
    assert.ok(x - point.r * camera.k >= 30 - 1e-8);
    assert.ok(x + point.r * camera.k <= phone.width - 30 + 1e-8);
    assert.ok(y - point.r * camera.k >= 30 - 1e-8);
    assert.ok(y + point.r * camera.k <= phone.height - 30 + 1e-8);
  }
});

test('empty and zero-size data still have a finite reset camera', () => {
  for (const points of [[], [{ x: 0, y: 0, r: 0 }]]) {
    const camera = fitPoints(points, viewport, 30);
    assert.ok(Number.isFinite(camera.x) && Number.isFinite(camera.y));
    assert.ok(Number.isFinite(camera.k) && camera.k > 0);
  }
});

test('a moving pinch midpoint pans and zooms without losing its world anchor', () => {
  const camera = { x: 50, y: 20, k: .5 };
  const start = [{ x: 300, y: 300 }, { x: 500, y: 300 }];
  const current = [{ x: 320, y: 270 }, { x: 720, y: 270 }];
  const next = pinchCamera(camera, start, current, viewport, { min: .1, max: 3 });
  close(next.k, 1);
  const anchor = worldAt(next, { x: 520, y: 270 }, viewport);
  close(anchor.x, 50);
  close(anchor.y, 20);
});

test('coincident pinch points never produce zero, infinite, or inverted zoom', () => {
  const next = pinchCamera({ x: 0, y: 0, k: 1 },
    [{ x: 100, y: 100 }, { x: 400, y: 100 }],
    [{ x: 250, y: 100 }, { x: 250, y: 100 }], viewport, { min: .1, max: 3 });
  assert.equal(next.k, .1);
  assert.ok(Number.isFinite(next.x) && Number.isFinite(next.y));
});
