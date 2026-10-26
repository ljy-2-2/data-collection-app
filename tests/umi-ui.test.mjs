import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

// Expose the page-local component only in this offline renderer. No effects,
// hardware connections, or changes to production page exports are needed.
const server = await createServer({
  configFile: false,
  server: { middlewareMode: true, watch: null, hmr: false, ws: false },
  appType: 'custom',
  plugins: [{
    name: 'umi-sensor-test-export',
    transform(source, id) {
      if (id.endsWith('/app/page.tsx')) return `${source}\nexport { UmiSensorCurve };`;
    },
  }],
});
after(() => server.close());
const { UmiSensorCurve } = await server.ssrLoadModule('/app/page.tsx');

void test('UMI preview renders degrees for readings, calibration and graph without mutating radians', () => {
  const reading = Object.freeze({ online: true, value: Math.PI });
  const points = Object.freeze([
    Object.freeze({ t: 1, value: 0 }),
    Object.freeze({ t: 2, value: Math.PI / 2 }),
  ]);
  const html = renderToStaticMarkup(createElement(UmiSensorCurve, {
    side: 'left', reading, points, minimum: Math.PI / 2,
    onMinimumChange: () => {},
  }));
  assert.match(html, /<strong>180\.00°<\/strong>/);
  assert.match(html, /原始角度/);
  assert.match(html, /原始零点/);
  assert.match(html, /<strong>90\.00°<\/strong>/);
  assert.match(html, /text-anchor="end">45\.00°<\/text>/);
  assert.match(html, /text-anchor="end">103\.50°<\/text>/);
  assert.match(html, /text-anchor="end">-13\.50°<\/text>/);
  assert.equal(reading.value, Math.PI);
  assert.equal(points[1].value, Math.PI / 2);
});

void test('monitor graph padding cannot introduce negative axis labels below calibrated zero', () => {
  const points = Object.freeze([
    Object.freeze({ t: 1, value: 0 }),
    Object.freeze({ t: 2, value: Math.PI / 180 }),
  ]);
  const html = renderToStaticMarkup(createElement(UmiSensorCurve, {
    side: 'left', reading: { online: true, value: Math.PI / 180 }, points,
  }));
  assert.match(html, /y="120" text-anchor="end">0\.00°<\/text>/);
  assert.doesNotMatch(html, /text-anchor="end">-/);
  assert.match(html, /<polyline points="56\.0,116\.0 /);
  assert.match(html, /<strong>2\.00°<\/strong>/);
  assert.equal(points[0].value, 0);
  assert.equal(points[1].value, Math.PI / 180);
});

void test('UMI monitor displays the already calibrated position with no extra zero subtraction', () => {
  for (const [value, expected] of [[0, '0.00°'], [-0, '0.00°'], [-1e-8, '0.00°'], [Math.PI / 6, '60.00°']]) {
    const html = renderToStaticMarkup(createElement(UmiSensorCurve, {
      side: 'right', reading: { online: true, value }, points: [{ t: 1, value }],
    }));
    assert.ok(html.includes(`<strong>${expected}</strong>`));
    assert.ok(html.includes(`text-anchor="end">${expected}</text>`));
    assert.match(html, /总开合角/);
    assert.doesNotMatch(html, /-0\.00°/);
    assert.doesNotMatch(html, /type="checkbox"/);
  }
});

void test('negative raw readings and calibration references remain negative during preview', () => {
  const value = -Math.PI / 3;
  const minimum = -Math.PI / 2;
  const html = renderToStaticMarkup(createElement(UmiSensorCurve, {
    side: 'left', reading: { online: true, value }, points: [{ t: 1, value }],
    minimum, onMinimumChange: () => {},
  }));
  assert.match(html, /<strong>-60\.00°<\/strong>/);
  assert.match(html, /<strong>-90\.00°<\/strong>/);
  assert.match(html, /text-anchor="end">-60\.00°<\/text>/);
});

void test('invalid encoder readings retain the no-data placeholder', () => {
  for (const value of [null, undefined, NaN, Infinity]) {
    const html = renderToStaticMarkup(createElement(UmiSensorCurve, {
      side: 'left', reading: { online: false, value }, points: [],
    }));
    assert.match(html, /<strong>--<\/strong>/);
    assert.doesNotMatch(html, /NaN|Infinity/);
  }
});
