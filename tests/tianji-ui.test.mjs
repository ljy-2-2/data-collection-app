import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
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
const { TianjiTeleopWorkspace } = await server.ssrLoadModule(
  '/components/teleop/TianjiTeleopWorkspace.tsx',
);
const { TeleopPanel } = await server.ssrLoadModule(
  '/components/teleop/TeleopPanel.tsx',
);
const { validTianjiParameters, TIANJI_DEFAULTS, TIANJI_CAMERAS, TIANJI_SIDES } =
  await server.ssrLoadModule('/lib/tianji.ts');
const { linkTransforms, tianjiCameraMatrix } = await server.ssrLoadModule(
  '/lib/tianjiModel.ts',
);
const props = {
  task: '天机测试',
  baseUrl: 'http://127.0.0.1:9',
  darkMode: false,
  status: null,
  preview: null,
  action: null,
  error: null,
  onAction: () => {
    throw Error('No hardware during render');
  },
};
test('camera and arm labels match anatomical left and right', () => {
  assert.deepEqual(TIANJI_CAMERAS, [
    { id: 'usb_1', label: '主相机' },
    { id: 'usb_2', label: '右腕部相机' },
    { id: 'usb_4', label: '左腕部相机' },
  ]);
  assert.deepEqual(TIANJI_SIDES, [
    { id: 'left', label: '左臂', letter: 'L' },
    { id: 'right', label: '右臂', letter: 'R' },
  ]);
});
test('robot selection includes Tianji and preserves G2', () => {
  const html = renderToStaticMarkup(createElement(TeleopPanel, props));
  assert.match(html, /value="tianji" selected=""/);
  assert.match(html, /智元 G2/);
});
test('Tianji workspace has three modes, three named cameras, modal parameters and zero reset', () => {
  const html = renderToStaticMarkup(
    createElement(TianjiTeleopWorkspace, props),
  );
  for (const mode of ['dual', 'left', 'right'])
    assert.match(html, new RegExp(`value="${mode}"`));
  for (const camera of ['usb_1', 'usb_2', 'usb_4'])
    assert.match(html, new RegExp(`value="${camera}"`));
  assert.match(html, /<dialog[^>]*class="tianji-parameter-dialog"/);
  assert.doesNotMatch(html, /<dialog[^>]* open/);
  assert.equal((html.match(/type="number"/g) || []).length, 11);
  const dialog = html.match(/<dialog[\s\S]*?<\/dialog>/)[0];
  assert.equal((dialog.match(/type="number"/g) || []).length, 8);
  for (const label of ['左夹爪打开角度', '左夹爪关闭角度', '右夹爪打开角度', '右夹爪关闭角度'])
    assert.ok(dialog.includes(label));
  assert.match(dialog, /其它参数/);
  assert.doesNotMatch(dialog, /关节速度上限|关节加速度上限|MIT 模式/);
  for (const label of ['比例', '关节速度上限', '关节加速度上限']) {
    assert.match(html, new RegExp(`aria-label="增加${label}"`));
    assert.match(html, new RegExp(`aria-label="减少${label}"`));
  }
  assert.doesNotMatch(html, /请先保存参数，再开始遥操作|当前为力位模式/);
  assert.doesNotMatch(html, /恢复默认|保存参数|tianji-primary-actions/);
  const modeSelectEnd = html.indexOf('</select>');
  const fields = html.indexOf('class="tianji-primary-parameters"');
  const otherButton = html.indexOf('其它参数');
  assert.ok(modeSelectEnd < fields && fields < otherButton);
  assert.match(html, /自动保存/);
  assert.doesNotMatch(html, /已自动保存|本阶段不触发采集/);
  assert.match(html, /本次完成 <strong>0<\/strong> Episode/);
  assert.match(html, /未开始遥操作/);
  assert.ok(html.indexOf('tianji-collection-summary') < html.indexOf('检查设备</button>'));
  assert.doesNotMatch(html, /tianji-collection-panel|tianji-collection-stats|<h3>数据采集<\/h3>|结束本段/);
  assert.match(html, /A \/ X 开始采集，B \/ Y 结束并保存/);
  assert.match(html, /零点（默认）/);
  assert.match(html, /厂家角度增加\/减少/);
  assert.match(html, /正向绕本关节轴按右手规则/);
  for (const side of ['左臂', '右臂']) {
    assert.ok(html.includes(`aria-label="增加${side} J3 10°（厂家关节坐标）"`));
    assert.ok(html.includes(`aria-label="减少${side} J3 10°（厂家关节坐标）"`));
  }
  assert.doesNotMatch(
    html,
    /仅遥操作|断开设备|Tianji · 网线|tianji-camera-tabs|teleop-visual-caption/,
  );
  for (const label of ['主相机', '右腕部相机', '左腕部相机'])
    assert.match(html, new RegExp(label));
  assert.doesNotMatch(html, /头部深度|身体|开始遥操作采集/);
  assert.match(html, /disabled=""[^>]*>[^<]*复位/);
});
test('draft parameters reject empty, NaN and excessive values', () => {
  assert.ok(validTianjiParameters(TIANJI_DEFAULTS));
  assert.equal(TIANJI_DEFAULTS.right_gripper_speed, 10);
  for (const scale of [0.1, 1, 1.5, 2]) {
    assert.equal(validTianjiParameters({ ...TIANJI_DEFAULTS, scale }), true);
  }
  for (const scale of [0.099, 2.001, Infinity]) {
    assert.equal(validTianjiParameters({ ...TIANJI_DEFAULTS, scale }), false);
  }
  assert.equal(
    validTianjiParameters({ ...TIANJI_DEFAULTS, scale: NaN }),
    false,
  );
  assert.equal('kp' in TIANJI_DEFAULTS, false);
  assert.equal('kd' in TIANJI_DEFAULTS, false);
  assert.equal(validTianjiParameters({ ...TIANJI_DEFAULTS, left_gripper_accel: 0 }), false);
  assert.equal(validTianjiParameters({ ...TIANJI_DEFAULTS, right_gripper_accel: 201 }), false);
  for (const side of ['left', 'right']) {
    for (const endpoint of ['open', 'closed']) {
      assert.equal(validTianjiParameters({ ...TIANJI_DEFAULTS, [`${side}_gripper_${endpoint}_rad`]: 4 }), false);
    }
  }
  assert.equal(
    validTianjiParameters({ ...TIANJI_DEFAULTS, max_joint_speed_deg_s: 181 }),
    false,
  );
});
test('CAD assembly resolves all links and a left joint leaves the right arm fixed', () => {
  const model = JSON.parse(
    readFileSync(new URL('../public/tianji-model.json', import.meta.url)),
  );
  const zero = linkTransforms(model, {}),
    moved = linkTransforms(model, { Joint2_L: 0.5 });
  assert.equal(Object.keys(zero).length, model.links.length);
  assert.notDeepEqual(zero.Link7_L, moved.Link7_L);
  assert.deepEqual(zero.Link7_R, moved.Link7_R);
  assert.ok(model.links.find((l) => l.name === 'stand').visuals.length);
  const q = linkTransforms(model, { gripper_drive_L: 0.8 });
  assert.notDeepEqual(zero.gripper_L_1, q.gripper_L_1);
});
test('physical right uses B/R and both shoulders turn around local Z without moving the stand', () => {
  const model = JSON.parse(readFileSync(new URL('../public/tianji-model.json', import.meta.url)));
  const installation = JSON.parse(readFileSync(new URL('../public/tianji-installation.json', import.meta.url)));
  const nominal = linkTransforms(model, {});
  model.joints = model.joints.map((joint) => ({ ...joint, ...installation.joints[joint.name] }));
  const swapped = linkTransforms(model, {});
  assert.deepEqual(installation.arm_models, { left: 'left', right: 'right' });
  assert.deepEqual(installation.arm_channels, { left: 'A', right: 'B' });
  assert.deepEqual(installation.arm_base_sides, { left: 'left', right: 'right' });
  for (const index of [3, 7, 11]) {
    assert.ok(Math.abs(swapped.Base_R[index] - nominal.Base_R[index]) < 1e-9);
    assert.ok(Math.abs(swapped.Base_L[index] - nominal.Base_L[index]) < 1e-9);
  }
  for (const side of ['L', 'R']) {
    for (let row = 0; row < 3; row += 1) {
      for (let column = 0; column < 3; column += 1) {
        const index = row * 4 + column;
        const sign = column === 2 ? 1 : -1;
        assert.ok(Math.abs(swapped[`Base_${side}`][index] - sign * nominal[`Base_${side}`][index]) < 1e-9);
      }
    }
  }
  assert.deepEqual(swapped.stand, nominal.stand);
  const moved = linkTransforms(model, { Joint2_L: 0.5 });
  assert.notDeepEqual(moved.Link7_L, swapped.Link7_L);
  assert.deepEqual(moved.Link7_R, swapped.Link7_R);
  const motors = JSON.parse(readFileSync(new URL('../teleop_control/vendor/tianji/teleop/gripper/config.json', import.meta.url))).motors;
  for (const side of ['left', 'right']) {
    for (const endpoint of ['open', 'closed']) {
      assert.equal(TIANJI_DEFAULTS[`${side}_gripper_${endpoint}_rad`], motors[side][`${endpoint}_rad`]);
    }
  }
});
test('opaque orthographic view puts front surfaces ahead of rear surfaces without perspective scaling', () => {
  const camera = tianjiCameraMatrix(560, 300, { yaw: 0, pitch: 0, zoom: 1 });
  const project = ([x, y, z]) =>
    [0, 1, 2, 3].map(
      (row) =>
        camera[row * 4] * x +
        camera[row * 4 + 1] * y +
        camera[row * 4 + 2] * z +
        camera[row * 4 + 3],
    );
  // The upright camera faces world +X; the visible front is towards -X.
  const front = project([-0.4, 0.1, 0.85]);
  const back = project([0.4, 0.1, 0.85]);
  assert.ok(
    front[2] < back[2],
    'LESS depth test must retain the front surface',
  );
  assert.ok(front[2] > -1 && back[2] < 1);
  assert.deepEqual(front.slice(0, 2), back.slice(0, 2));
  assert.equal(front[3], 1);
  assert.equal(back[3], 1);
});
