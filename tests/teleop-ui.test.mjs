import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

// Render components offline without launching the App, logging in, or running
// any effects that could talk to hardware. Vite loads the project's TSX.
const server = await createServer({
  configFile: false,
  server: { middlewareMode: true, watch: null, hmr: false, ws: false },
  appType: 'custom',
});
after(() => server.close());
const { TeleopPanel } = await server.ssrLoadModule('/components/teleop/TeleopPanel.tsx');
const { G2TeleopWorkspace } = await server.ssrLoadModule('/components/teleop/G2TeleopWorkspace.tsx');
const { LoginScreen } = await server.ssrLoadModule('/components/LoginScreen.tsx');
const props = {
  baseUrl: 'http://127.0.0.1:9', status: null, preview: null, darkMode: false,
  action: null, error: null,
  onAction: () => { throw new Error('Rendering must not start hardware'); },
};

void test('login exposes both opt-in options and explicit logout leaves them unchecked', () => {
  const html = renderToStaticMarkup(createElement(LoginScreen, {
    baseUrl: props.baseUrl, onLogin: () => {}, forgetSaved: true,
    logoutBusy: false, logoutMessage: '',
  }));
  assert.match(html, /记住账号密码/);
  assert.match(html, /自动登录/);
  assert.equal((html.match(/type="checkbox"/g) ?? []).length, 2);
  assert.doesNotMatch(html, /checked=""/);
  assert.match(html, /type="password"[^>]*required=""[^>]*value=""/);
});

void test('fresh teleop entry shows only task and robot configuration', () => {
  const html = renderToStaticMarkup(createElement(TeleopPanel, props));
  assert.match(html, /任务名称/);
  assert.match(html, /机器人型号/);
  assert.match(html, /智元 G2/);
  assert.match(html, /value="遥操作数据采集"/);
  assert.match(html, /class="collection-task-name" type="text"/);
  assert.doesNotMatch(html, /type="submit"[^>]*disabled/);
  assert.doesNotMatch(html, /机器人姿态|手动控制|检查设备|开始遥操作采集/);
});

void test('return action stays in the stepper and UMI uses its requested default task', () => {
  const teleop = readFileSync(new URL('../components/teleop/TeleopPanel.tsx', import.meta.url), 'utf8');
  const stepper = teleop.slice(teleop.indexOf('<section className="surface umi-stepper'), teleop.indexOf('</section>'));
  assert.match(stepper, /step === 2/);
  assert.match(stepper, /返回配置/);
  assert.match(stepper, /disabled={!canReturn}/);
  assert.doesNotMatch(teleop, /teleop-task-summary/);
  const page = readFileSync(new URL('../app/page.tsx', import.meta.url), 'utf8');
  assert.match(page, /const \[task, setTask\] = useState\('Umi数据采集'\)/);
  assert.match(page, /className="collection-task-name" type="text" value={task}/);
});

void test('speed and scale retain editable native number controls and bounded increments', () => {
  const html = renderToStaticMarkup(createElement(G2TeleopWorkspace, { ...props, task: '测试' }));
  for (const label of ['速度', '幅度']) {
    assert.ok(html.includes(`aria-label="${label}" type="number" min="0.1" max="1" step="0.1"`));
  }
  const css = readFileSync(new URL('../app/globals.css', import.meta.url), 'utf8');
  assert.match(css, /\.teleop-mode-parameters input\[type='number'\] \{[^}]*border: 1px solid[^}]*appearance: auto;/);
  assert.match(css, /\.teleop-mode-parameters input\[type='number'\]::-webkit-inner-spin-button \{[^}]*opacity: 1;/);
});

for (const darkMode of [false, true]) {
  void test(`G2 workspace retains its controls and previews (dark=${darkMode})`, () => {
    const html = renderToStaticMarkup(createElement(G2TeleopWorkspace, { ...props, task: '抓取测试', darkMode }));
    for (const label of ['遥操作模式', '所需设备', '机器人姿态', '相机画面', '切换相机画面', '姿态配置', '手动控制', '额外信息保存', '检查设备', '开始遥操作采集']) {
      assert.ok(html.includes(label), `Missing ${label}`);
    }
    assert.doesNotMatch(html, /机器人型号/);
    assert.match(html, /value="0.5"/);
    assert.match(html, /disabled[^>]*><[^>]+>.*开始遥操作采集/);
  });
}

void test('running G2 session keeps its stop action and disables manual motion', () => {
  const status = { session: { state: 'running', session_id: 'test' }, motion: { state: 'idle' } };
  const html = renderToStaticMarkup(createElement(G2TeleopWorkspace, { ...props, status, task: '抓取测试' }));
  assert.match(html, /结束遥操作/);
  assert.doesNotMatch(html, /开始遥操作采集/);
  assert.match(html, /aria-label="提高[^"]+" disabled/);
});
