export type AsyncAction =
  | 'check-teleop'
  | 'start-teleop'
  | 'stop-teleop'
  | 'move-teleop'
  | 'check-umi'
  | 'start-umi'
  | 'stop-umi'
  | 'record-start'
  | 'record-stop'
  | 'record-cancel'
  | null;

export interface TeleopDevice {
  id: 'robot' | 'xr' | 'headset';
  name: string;
  status: 'checking' | 'connected' | 'offline' | 'warning';
  detail?: string;
}

export interface TeleopStatus {
  checked_at: string;
  devices: TeleopDevice[];
  session: {
    state: 'idle' | 'running' | 'unknown';
    session_id?: string;
    started_at?: string;
  };
  motion?: {
    state: 'idle' | 'moving';
    command_id?: string;
    completed_command_id?: string;
    failed_command_id?: string;
  };
}

export interface PreviewStatus {
  available: boolean;
  recording?: boolean;
  updated_at: string | null;
  joint_positions?: Record<string, number>;
  cameras: Array<{ id: string; available: boolean; updated_at: string | null }>;
}
