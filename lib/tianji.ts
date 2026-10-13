export type TianjiMode = 'dual' | 'left' | 'right';
export type TianjiSide = 'left' | 'right';
export const TIANJI_JOG_STEP_DEG = 10;
export interface TianjiParameters {
  scale: number;
  max_joint_speed_deg_s: number;
  max_joint_accel_deg_s2: number;
  left_gripper_speed: number;
  right_gripper_speed: number;
  kp: number;
  kd: number;
}
export const TIANJI_DEFAULTS: TianjiParameters = {
  scale: 1,
  max_joint_speed_deg_s: 180,
  max_joint_accel_deg_s2: 450,
  left_gripper_speed: 10,
  right_gripper_speed: 10,
  kp: 80,
  kd: 2,
};
export const TIANJI_FIELDS: {
  key: keyof TianjiParameters;
  label: string;
  unit: string;
  min: number;
  max: number;
  step: number;
}[] = [
  { key: 'scale', label: '比例', unit: '×', min: 0.01, max: 1, step: 0.01 },
  {
    key: 'max_joint_speed_deg_s',
    label: '关节速度上限',
    unit: '°/s',
    min: 0.1,
    max: 180,
    step: 0.1,
  },
  {
    key: 'max_joint_accel_deg_s2',
    label: '关节加速度上限',
    unit: '°/s²',
    min: 1,
    max: 450,
    step: 1,
  },
  {
    key: 'left_gripper_speed',
    label: '左夹爪速度',
    unit: 'rad/s',
    min: 0.01,
    max: 100,
    step: 0.1,
  },
  {
    key: 'right_gripper_speed',
    label: '右夹爪速度',
    unit: 'rad/s',
    min: 0.01,
    max: 100,
    step: 0.1,
  },
  { key: 'kp', label: '电机 Kp', unit: '', min: 0, max: 500, step: 1 },
  { key: 'kd', label: '电机 Kd', unit: '', min: 0.01, max: 5, step: 0.1 },
];
export function validTianjiParameters(p: TianjiParameters) {
  return TIANJI_FIELDS.every(
    ({ key, min, max }) =>
      typeof p[key] === 'number' &&
      Number.isFinite(p[key]) &&
      p[key] >= min &&
      p[key] <= max,
  );
}
export const TIANJI_CAMERAS = [
  { id: 'usb_1', label: '主相机' },
  { id: 'usb_2', label: '右腕部相机' },
  { id: 'usb_4', label: '左腕部相机' },
];
export const TIANJI_SIDES = [
  { id: 'left' as const, label: '左臂', letter: 'L' },
  { id: 'right' as const, label: '右臂', letter: 'R' },
];
export const TIANJI_LIMITS = [
  [-170, 170],
  [-120, 120],
  [-170, 170],
  [-144.99, 60],
  [-170, 170],
  [-60, 60],
  [-90, 90],
];
export interface TianjiStatus {
  recording?: boolean;
  collection?: {
    state:
      | 'idle'
      | 'ready'
      | 'starting'
      | 'recording'
      | 'finalizing'
      | 'saved'
      | 'incomplete';
    armed: boolean;
    busy: boolean;
    recording: boolean;
    error: string;
    completed: number;
    episode: string | null;
    elapsed_s: number;
    frames: Record<string, number>;
    last_episode: string | null;
    upload: {
      pending: number;
      uploaded: number;
      failed: number;
      uploading?: number;
      error?: string;
    };
  };
  state: 'idle' | 'checking' | 'running' | 'moving' | 'error' | 'unknown';
  mode?: TianjiMode;
  upload_mode?: TianjiMode;
  devices: { id: string; name: string; status: string; detail?: string }[];
  joints: Partial<Record<TianjiSide, number[]>>;
  cameras: { id: string; available: boolean; updated_at: number | null }[];
  grippers?: {
    fault: string;
    motors: Partial<
      Record<
        TianjiSide,
        {
          feedback?: { position_rad?: number; age_s?: number };
          target_rad?: number;
        }
      >
    >;
  } | null;
  error?: string;
  camera_error?: string;
  camera_phase?: string;
  reason?: string;
}
