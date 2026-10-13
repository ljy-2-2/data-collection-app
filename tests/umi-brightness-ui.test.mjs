import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const server = await createServer({
  configFile: false,
  server: { middlewareMode: true, watch: null, hmr: false, ws: false },
  appType: 'custom',
});
after(() => server.close());
const { CameraBrightnessControls, CameraBrightness, brightnessValue } = await server.ssrLoadModule('/components/CameraBrightness.tsx');
const control = { supported: true, value: 22, min: 10, max: 50, step: 4, default: 30 };
const properties = {
  label: '左相机', control, draft: '22', busy: null, disabled: false,
  message: '', error: '', onChange: () => {}, onCommit: () => {}, onRetry: () => {},
};

function elements(element, type) {
  if (Array.isArray(element)) return element.flatMap((child) => elements(child, type));
  if (!element || typeof element !== 'object') return [];
  return element.type === type ? [element] : elements(element.props?.children, type);
}
const inputs = (element) => elements(element, 'input');

test('brightness renders independent slider and native numeric field using hardware range and step', () => {
  const html = renderToStaticMarkup(createElement(CameraBrightnessControls, properties));
  assert.match(html, /<section class="umi-camera-brightness"/);
  assert.match(html, /aria-label="左相机亮度滑块" type="range" min="10" max="50" step="4" value="22"/);
  assert.match(html, /aria-label="左相机亮度数值" type="number" min="10" max="50" step="4"/);
  assert.doesNotMatch(html, /disabled=""|role="alert"/);
  assert.doesNotMatch(html, /<img/); // Adjustments cannot own or replace the video stream.
});

test('native slider change, release and keyboard handlers submit the current control value', () => {
  const changes = [];
  const commits = [];
  const [slider] = inputs(CameraBrightnessControls({
    ...properties, onChange: (value) => changes.push(value), onCommit: (value) => commits.push(value),
  }));
  slider.props.onChange({ currentTarget: { value: '26' } });
  slider.props.onPointerUp({ currentTarget: { value: '26' } });
  slider.props.onKeyUp({ key: 'ArrowRight', currentTarget: { value: '30' } });
  slider.props.onKeyUp({ key: 'Home', currentTarget: { value: '10' } });
  slider.props.onKeyUp({ key: 'Shift', currentTarget: { value: '10' } });
  assert.deepEqual(changes, ['26']);
  assert.deepEqual(commits, ['26', '30', '10']);
});

test('native number field commits on Enter and blur without submitting a surrounding form', () => {
  const changes = [];
  const commits = [];
  let prevented = false;
  const [, number] = inputs(CameraBrightnessControls({
    ...properties, onChange: (value) => changes.push(value), onCommit: (value) => commits.push(value),
  }));
  number.props.onChange({ currentTarget: { value: '34' } });
  number.props.onKeyDown({ key: 'Enter', currentTarget: { value: '34' }, preventDefault: () => { prevented = true; } });
  number.props.onBlur({ currentTarget: { value: '38' } });
  assert.deepEqual(changes, ['34']);
  assert.deepEqual(commits, ['34', '38']);
  assert.equal(prevented, true);
});

test('unsupported and failed controls are local disabled states with an actionable error', () => {
  const unsupported = renderToStaticMarkup(createElement(CameraBrightnessControls, {
    ...properties, control: { supported: false }, draft: '', disabled: true,
    message: '此相机不支持亮度调节',
  }));
  assert.equal((unsupported.match(/disabled=""/g) ?? []).length, 3);
  assert.match(unsupported, /此相机不支持亮度调节/);
  assert.doesNotMatch(unsupported, /NaN|undefined/);
  const failed = renderToStaticMarkup(createElement(CameraBrightnessControls, {
    ...properties, control: null, draft: '', disabled: true, error: '相机已断开',
  }));
  assert.match(failed, /role="alert"/);
  assert.match(failed, /相机已断开/);
  assert.match(failed, /aria-label="重新读取左相机亮度"/);
});

test('pending brightness reads and writes disable only their own controls', () => {
  const html = renderToStaticMarkup(createElement('div', null,
    createElement(CameraBrightnessControls, { ...properties, busy: 'saving', disabled: true }),
    createElement(CameraBrightnessControls, { ...properties, label: '右相机' }),
  ));
  assert.equal((html.match(/disabled=""/g) ?? []).length, 3);
  assert.match(html, /aria-label="正在设置亮度"/);
  const idle = renderToStaticMarkup(createElement(CameraBrightness, {
    baseUrl: 'http://127.0.0.1:9', cameraKey: '', label: '中心相机', active: false,
  }));
  assert.match(idle, /等待相机连接/);
  assert.equal((idle.match(/disabled=""/g) ?? []).length, 3);
});

test('reset submits the advertised default for just the selected camera', () => {
  const commits = [];
  const [button] = elements(CameraBrightnessControls({
    ...properties, onCommit: (value) => commits.push(value),
  }), 'button');
  assert.equal(button.props['aria-label'], '左相机恢复默认亮度');
  button.props.onClick();
  assert.deepEqual(commits, ['30']);
});

test('numeric commits honor hardware bounds and step without converting unfinished input to zero', () => {
  assert.equal(brightnessValue('', control), null);
  assert.equal(brightnessValue('-', control), null);
  assert.equal(brightnessValue('NaN', control), null);
  assert.equal(brightnessValue('18', control), 18);
  assert.equal(brightnessValue('19', control), 18);
  assert.equal(brightnessValue('-100', control), 10);
  assert.equal(brightnessValue('100', control), 50);
  assert.equal(brightnessValue('20', { ...control, max: 20 }), 18);
  assert.equal(brightnessValue('-9', { ...control, min: -16, max: 16 }), -8);
});
