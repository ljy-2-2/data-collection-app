export interface JointTargetDefinition {
  name: string;
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
}

export interface JointGroupDefinition {
  id: 'left-arm' | 'body' | 'right-arm';
  label: string;
  joints: readonly JointTargetDefinition[];
}

const joint = (name: string, label: string, value: number, min: number, max: number): JointTargetDefinition => ({
  name,
  label,
  value,
  min,
  max,
  step: name.includes('gripper') ? 0.005 : 0.01,
});

const LEFT_ARM = [
  joint('idx21_arm_l_joint1', '左臂 J1', -0.163557, -3.071796, 3.071796),
  joint('idx22_arm_l_joint2', '左臂 J2', -0.530748, -2.059505, 2.059505),
  joint('idx23_arm_l_joint3', '左臂 J3', 0.934962, -3.071796, 3.071796),
  joint('idx24_arm_l_joint4', '左臂 J4', -0.941735, -2.495838, 1.012308),
  joint('idx25_arm_l_joint5', '左臂 J5', 1.036588, -3.071796, 3.071796),
  joint('idx26_arm_l_joint6', '左臂 J6', -0.45063, -1.012308, 1.012308),
  joint('idx27_arm_l_joint7', '左臂 J7', 1.369374, -1.535907, 1.535907),
  joint('idx31_gripper_l_inner_joint1', '左夹爪', -0.785, -0.785, 0),
] as const;

const BODY = [
  joint('idx01_body_joint1', '升降 J1', 0, -1.082104, 0.000174),
  joint('idx02_body_joint2', '升降 J2', 0, -0.000174, 2.6529),
  joint('idx03_body_joint3', '腰部俯仰', 0, -1.919862, 1.57097),
  joint('idx04_body_joint4', '腰部侧倾', 0, -0.436332, 0.436332),
  joint('idx05_body_joint5', '腰部旋转', 0, -3.045599, 3.045599),
] as const;

const RIGHT_ARM = [
  joint('idx61_arm_r_joint1', '右臂 J1', 2.5383, -3.071796, 3.071796),
  joint('idx62_arm_r_joint2', '右臂 J2', 0.1937, -2.059505, 2.059505),
  joint('idx63_arm_r_joint3', '右臂 J3', -2.9588, -3.071796, 3.071796),
  joint('idx64_arm_r_joint4', '右臂 J4', -1.8983, -2.495838, 1.012308),
  joint('idx65_arm_r_joint5', '右臂 J5', -0.8024, -3.071796, 3.071796),
  joint('idx66_arm_r_joint6', '右臂 J6', -0.7667, -1.012308, 1.012308),
  joint('idx67_arm_r_joint7', '右臂 J7', -0.0627, -1.535907, 1.535907),
  joint('idx71_gripper_r_inner_joint1', '右夹爪', -0.7836, -0.785, 0),
] as const;

export const G2_JOINT_GROUPS: readonly JointGroupDefinition[] = [
  { id: 'left-arm', label: '左臂', joints: LEFT_ARM },
  { id: 'body', label: '身体', joints: BODY },
  { id: 'right-arm', label: '右臂', joints: RIGHT_ARM },
];

export const G2_MANUAL_PRESET = G2_JOINT_GROUPS.flatMap(({ joints }) => [...joints]);
export type JointTargets = Record<string, number>;

export function defaultG2JointTargets(): JointTargets {
  return Object.fromEntries(G2_MANUAL_PRESET.map(({ name, value }) => [name, value]));
}
