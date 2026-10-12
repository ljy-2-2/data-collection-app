import { G2_MANUAL_PRESET, type JointTargets } from './teleop';

export interface TeleopPosePreset {
  id: string;
  name: string;
  targets: Partial<JointTargets>;
}

export interface TeleopPosePresetState {
  presets: TeleopPosePreset[];
  selectedId: string;
}

const STORAGE_KEY = 'teleop-pose-presets-v1';
const KNOWN_JOINTS = new Map(G2_MANUAL_PRESET.map((definition) => [definition.name, definition]));
const DEFAULT_PRESET: TeleopPosePreset = {
  id: 'g2-recorded-pose',
  name: 'G2 双臂记录姿态',
  targets: Object.fromEntries(G2_MANUAL_PRESET.map(({ name, value }) => [name, value])),
};

function validPreset(value: unknown): value is TeleopPosePreset {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as TeleopPosePreset;
  if (!candidate.id || !candidate.name?.trim() || !candidate.targets) return false;
  return Object.entries(candidate.targets).every(([name, target]) => {
    const definition = KNOWN_JOINTS.get(name);
    return Boolean(definition && typeof target === 'number' && Number.isFinite(target) && target >= definition.min && target <= definition.max);
  });
}

export function loadTeleopPosePresetState(): TeleopPosePresetState {
  if (typeof window === 'undefined') return { presets: [DEFAULT_PRESET], selectedId: DEFAULT_PRESET.id };
  const raw = window.localStorage.getItem(STORAGE_KEY);
  if (!raw) return { presets: [DEFAULT_PRESET], selectedId: DEFAULT_PRESET.id };
  try {
    const parsed = JSON.parse(raw) as TeleopPosePresetState;
    if (!Array.isArray(parsed.presets) || !parsed.presets.every(validPreset)) throw new Error('invalid presets');
    const selectedId = parsed.presets.some(({ id }) => id === parsed.selectedId) ? parsed.selectedId : (parsed.presets[0]?.id ?? '');
    return { presets: parsed.presets, selectedId };
  } catch {
    return { presets: [DEFAULT_PRESET], selectedId: DEFAULT_PRESET.id };
  }
}

export function saveTeleopPosePresetState(state: TeleopPosePresetState) {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}
