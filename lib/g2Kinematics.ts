import type { JointTargets } from './teleop';

export interface Vec3 { x: number; y: number; z: number }
export interface SkeletonSegment { from: string; to: string; group: 'base' | 'body' | 'head' | 'left' | 'right' }
export interface G2Skeleton { points: Record<string, Vec3>; segments: SkeletonSegment[] }

type Matrix = number[];
type JointSpec = { name: string; origin: [number, number, number]; rpy: [number, number, number]; axis: 'x' | 'y' | 'z' };

const BODY: JointSpec[] = [
  { name: 'idx01_body_joint1', origin: [0.102, 0, 0.144], rpy: [0, 0, 0], axis: 'y' },
  { name: 'idx02_body_joint2', origin: [0, 0, 0.36], rpy: [0, 0, 0], axis: 'y' },
  { name: 'idx03_body_joint3', origin: [0, 0, 0.38], rpy: [0, 0, 0], axis: 'y' },
  { name: 'idx04_body_joint4', origin: [0, 0, 0], rpy: [0, 0, 0], axis: 'x' },
  { name: 'idx05_body_joint5', origin: [0, 0, 0.149], rpy: [0, 0, 0], axis: 'z' },
];
const HEAD: JointSpec[] = [
  { name: 'idx11_head_joint1', origin: [-0.013, 0, 0.496], rpy: [0, 0, 0], axis: 'z' },
  { name: 'idx12_head_joint2', origin: [0.03335, 0, 0.07], rpy: [0, 0, 0], axis: 'x' },
  { name: 'idx13_head_joint3', origin: [-0.03035, 0, 0], rpy: [0, 0, 0], axis: 'y' },
];
const ARM_ORIGINS: Array<[[number, number, number], [number, number, number]]> = [
  [[0, 0, 0.069], [0, 0, 0]],
  [[0, 0, 0.1745], [Math.PI / 2, 0, 0]],
  [[0, 0, 0], [-Math.PI / 2, 0, 0]],
  [[0.018, 0, 0.287], [Math.PI / 2, 0, 0]],
  [[-0.018, 0, 0], [-Math.PI / 2, 0, 0]],
  [[0, 0, 0.314], [Math.PI / 2, 0, 0]],
  [[0, 0, 0], [Math.PI / 2, 0, Math.PI / 2]],
];

const identity = (): Matrix => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
function multiply(a: Matrix, b: Matrix): Matrix {
  const out = Array<number>(16).fill(0);
  for (let row = 0; row < 4; row += 1) for (let column = 0; column < 4; column += 1) {
    for (let index = 0; index < 4; index += 1) out[row * 4 + column] += a[row * 4 + index] * b[index * 4 + column];
  }
  return out;
}
function translation([x, y, z]: [number, number, number]): Matrix {
  const matrix = identity(); matrix[3] = x; matrix[7] = y; matrix[11] = z; return matrix;
}
function rotation(axis: 'x' | 'y' | 'z', angle: number): Matrix {
  const cosine = Math.cos(angle); const sine = Math.sin(angle);
  if (axis === 'x') return [1, 0, 0, 0, 0, cosine, -sine, 0, 0, sine, cosine, 0, 0, 0, 0, 1];
  if (axis === 'y') return [cosine, 0, sine, 0, 0, 1, 0, 0, -sine, 0, cosine, 0, 0, 0, 0, 1];
  return [cosine, -sine, 0, 0, sine, cosine, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
}
function rpy([roll, pitch, yaw]: [number, number, number]): Matrix {
  return multiply(multiply(rotation('z', yaw), rotation('y', pitch)), rotation('x', roll));
}
function origin(matrix: Matrix): Vec3 { return { x: matrix[3], y: matrix[7], z: matrix[11] }; }
function value(values: JointTargets, name: string) { return Number.isFinite(values[name]) ? values[name] : 0; }
function jointTransform(parent: Matrix, spec: JointSpec, values: JointTargets): Matrix {
  return multiply(multiply(multiply(parent, translation(spec.origin)), rpy(spec.rpy)), rotation(spec.axis, value(values, spec.name)));
}

export function buildG2Skeleton(values: JointTargets): G2Skeleton {
  const points: Record<string, Vec3> = {};
  const segments: SkeletonSegment[] = [];
  const put = (name: string, point: Vec3) => { points[name] = point; };
  const link = (from: string, to: string, group: SkeletonSegment['group']) => segments.push({ from, to, group });
  const basePoints: Record<string, Vec3> = {
    base_center: { x: 0, y: 0, z: 0.12 }, base_fl: { x: 0.27, y: 0.22, z: 0.12 },
    base_fr: { x: 0.27, y: -0.22, z: 0.12 }, base_bl: { x: -0.27, y: 0.22, z: 0.12 },
    base_br: { x: -0.27, y: -0.22, z: 0.12 }, wheel_fl: { x: 0.27, y: 0.27, z: 0.05 },
    wheel_fr: { x: 0.27, y: -0.27, z: 0.05 }, wheel_bl: { x: -0.27, y: 0.27, z: 0.05 },
    wheel_br: { x: -0.27, y: -0.27, z: 0.05 }, body_root: { x: 0, y: 0, z: 0.12 },
  };
  Object.entries(basePoints).forEach(([name, point]) => put(name, point));
  [['base_fl', 'base_fr'], ['base_fr', 'base_br'], ['base_br', 'base_bl'], ['base_bl', 'base_fl'],
    ['base_fl', 'wheel_fl'], ['base_fr', 'wheel_fr'], ['base_bl', 'wheel_bl'], ['base_br', 'wheel_br'],
    ['base_center', 'base_fl'], ['base_center', 'base_fr'], ['base_center', 'base_bl'], ['base_center', 'base_br']]
    .forEach(([from, to]) => link(from, to, 'base'));

  let bodyMatrix = translation([0, 0, 0.12]);
  let prior = 'body_root';
  BODY.forEach((spec) => {
    bodyMatrix = jointTransform(bodyMatrix, spec, values); put(spec.name, origin(bodyMatrix)); link(prior, spec.name, 'body'); prior = spec.name;
  });
  let headMatrix = bodyMatrix; prior = 'idx05_body_joint5';
  HEAD.forEach((spec) => {
    headMatrix = jointTransform(headMatrix, spec, values); put(spec.name, origin(headMatrix)); link(prior, spec.name, 'head'); prior = spec.name;
  });
  put('head_tip', origin(multiply(headMatrix, translation([0, 0, 0.15])))); link(prior, 'head_tip', 'head');

  const armBase = multiply(multiply(bodyMatrix, translation([0, 0, 0.3085])), rpy([-Math.PI / 2, 0, 0]));
  (['left', 'right'] as const).forEach((side) => {
    let current = armBase; prior = 'idx05_body_joint5';
    ARM_ORIGINS.forEach(([jointOrigin, jointRpy], index) => {
      const number = index + 1;
      const name = side === 'left' ? `idx2${number}_arm_l_joint${number}` : `idx6${number}_arm_r_joint${number}`;
      const adjustedRpy: [number, number, number] = side === 'right' && index === 0 ? [Math.PI, 0, 0] : jointRpy;
      const adjustedOrigin: [number, number, number] = side === 'right' && index === 0 ? [0, 0, -0.069] : jointOrigin;
      current = jointTransform(current, { name, origin: adjustedOrigin, rpy: adjustedRpy, axis: 'z' }, values);
      put(name, origin(current)); link(prior, name, side); prior = name;
    });
    const endName = `${side}_end_effector`;
    put(endName, origin(multiply(current, translation([0.088, 0, 0])))); link(prior, endName, side);
  });
  return { points, segments };
}
