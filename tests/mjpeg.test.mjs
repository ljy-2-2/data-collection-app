import assert from 'node:assert/strict';
import test from 'node:test';
import { MjpegDecoder, readMjpeg } from '../lib/mjpeg.ts';

void test('JPEG markers split across network chunks retain one complete frame', () => {
  const decoder = new MjpegDecoder();
  assert.equal(decoder.push(Uint8Array.of(45, 45, 13, 10, 255)), null);
  assert.equal(decoder.push(Uint8Array.of(216, 1, 255, 0, 2, 255)), null);
  assert.deepEqual(decoder.push(Uint8Array.of(217, 13, 10)), Uint8Array.of(255, 216, 1, 255, 0, 2, 255, 217));
});

void test('batched frames show the latest frame and retain a partial next frame', () => {
  const decoder = new MjpegDecoder();
  assert.deepEqual(decoder.push(Uint8Array.of(255, 216, 1, 255, 217, 255, 216, 2, 255, 217, 255, 216, 3)), Uint8Array.of(255, 216, 2, 255, 217));
  assert.deepEqual(decoder.push(Uint8Array.of(255, 217)), Uint8Array.of(255, 216, 3, 255, 217));
});

void test('video uses the raw transport and releases its reader on cancellation', async (context) => {
  const controller = new AbortController();
  let cancelled = false;
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(url, '/umi/cameras/left?raw=1');
    assert.equal(options.signal, controller.signal);
    return new Response(new ReadableStream({
      start(stream) { stream.enqueue(Uint8Array.of(255, 216, 1, 255, 217)); },
      cancel() { cancelled = true; },
    }));
  });
  await readMjpeg('/umi/cameras/left', controller.signal, () => controller.abort());
  assert.equal(cancelled, true);
});
