import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import ts from 'typescript';
const src = readFileSync(new URL('../lib/tianjiModel.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const { tianjiCameraMatrix } = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
// Pan is in screen pixels and must not rotate/scale the measured model.
test('robot camera pan preserves rotation, zoom and depth at any orbit', () => {
  for (const yaw of [0, 1.2]) for (const zoom of [0.6, 3.4]) {
    const normal = tianjiCameraMatrix(560, 300, { yaw, pitch: 0.4, zoom });
    const panned = tianjiCameraMatrix(560, 300, { yaw, pitch: 0.4, zoom, panX: 28, panY: -15 });
    panned.forEach((value, i) => assert.ok(Math.abs(value - normal[i] - ([3, 7].includes(i) ? 0.1 : 0)) < 1e-10));
  }
});

test('front orbit looks from base positive X with positive Z up', () => {
  const camera=tianjiCameraMatrix(560,300,{yaw:Math.PI,pitch:0,zoom:1});
  assert.ok(camera[8]<0); // +X is closer to the front-facing camera.
  assert.ok(camera[6]>0); // +Z remains screen-up.
});
