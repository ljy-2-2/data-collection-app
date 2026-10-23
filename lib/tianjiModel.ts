export type Matrix = number[];
export const identity = (): Matrix => [
  1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1,
];
export function multiply(a: Matrix, b: Matrix): Matrix {
  return Array.from({ length: 16 }, (_, i) =>
    [0, 1, 2, 3].reduce(
      (s, k) => s + a[Math.floor(i / 4) * 4 + k] * b[k * 4 + (i % 4)],
      0,
    ),
  );
}
export function axisRotation(axis: number[], angle: number): Matrix {
  const n = Math.hypot(...axis) || 1,
    [x, y, z] = axis.map((v) => v / n),
    c = Math.cos(angle),
    s = Math.sin(angle),
    t = 1 - c;
  return [
    t * x * x + c,
    t * x * y - s * z,
    t * x * z + s * y,
    0,
    t * x * y + s * z,
    t * y * y + c,
    t * y * z - s * x,
    0,
    t * x * z - s * y,
    t * y * z + s * x,
    t * z * z + c,
    0,
    0,
    0,
    0,
    1,
  ];
}
export function transform(xyz: number[], rpy: number[]): Matrix {
  const m = multiply(
    multiply(axisRotation([0, 0, 1], rpy[2]), axisRotation([0, 1, 0], rpy[1])),
    axisRotation([1, 0, 0], rpy[0]),
  );
  m[3] = xyz[0];
  m[7] = xyz[1];
  m[11] = xyz[2];
  return m;
}
export const columnMajor = (m: Matrix) =>
  new Float32Array(
    Array.from({ length: 16 }, (_, i) => m[(i % 4) * 4 + Math.floor(i / 4)]),
  );
export function tianjiCameraMatrix(
  width: number,
  height: number,
  view: { yaw: number; pitch: number; zoom: number },
): Matrix {
  const scale = 1.02 * view.zoom;
  // Orthographic projection. Camera +Z points towards the viewer, so nearer
  // surfaces must map to a smaller depth for WebGL's LESS depth comparison.
  const projection = identity();
  projection[0] = (scale * height) / width;
  projection[5] = scale;
  projection[10] = -0.25;
  const upright = [0, -1, 0, 0, 0, 0, 1, 0, -1, 0, 0, 0, 0, 0, 0, 1];
  return multiply(
    projection,
    multiply(
      axisRotation([1, 0, 0], view.pitch),
      multiply(
        upright,
        multiply(
          axisRotation([0, 0, 1], view.yaw),
          transform([0, 0, -0.85], [0, 0, 0]),
        ),
      ),
    ),
  );
}
export interface TianjiModel {
  root_link?: string;
  source: string;
  meshes: Record<string, { vertices: number[][]; faces: number[][] }>;
  links: {
    name: string;
    visuals: {
      mesh: string;
      xyz: number[];
      rpy: number[];
      scale: number[];
      color: number[];
    }[];
  }[];
  joints: {
    name: string;
    type: string;
    parent: string;
    child: string;
    xyz: number[];
    rpy: number[];
    axis: number[];
    mimic: { joint: string; multiplier: string; offset: string } | null;
    limit: { lower: string; upper: string } | null;
  }[];
}
export function linkTransforms(
  model: TianjiModel,
  angles: Record<string, number>,
): Record<string, Matrix> {
  const result: Record<string, Matrix> = { [model.root_link ?? "stand"]: identity() };
  let pending = [...model.joints];
  while (pending.length) {
    const next = pending.filter((j) => {
      if (!result[j.parent]) return true;
      const a = j.mimic
        ? (angles[j.mimic.joint] ?? 0) * Number(j.mimic.multiplier) +
          Number(j.mimic.offset)
        : (angles[j.name] ?? 0);
      result[j.child] = multiply(
        result[j.parent],
        multiply(
          transform(j.xyz, j.rpy),
          (j.type === 'revolute' || j.type === 'continuous') ? axisRotation(j.axis, a) : identity(),
        ),
      );
      return false;
    });
    if (next.length === pending.length) throw new Error('模型关节结构不完整');
    pending = next;
  }
  return result;
}
