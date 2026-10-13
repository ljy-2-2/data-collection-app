export interface PlatformUser {
  id: string;
  username: string;
  is_admin: boolean;
  is_active: boolean;
}

export interface PlatformAuthStatus {
  authenticated: boolean;
  user: PlatformUser | null;
  unavailable?: boolean;
  detail?: string;
  warning?: string;
}

export interface LoginPreferences {
  username: string;
  remember_password: boolean;
  auto_login: boolean;
  has_saved_password: boolean;
  password_length: number;
}

export function logoutPendingKey(baseUrl: string) {
  return `collection-logout-pending:${baseUrl}`;
}
export function hasPendingLogout(baseUrl: string) {
  return window.localStorage.getItem(logoutPendingKey(baseUrl)) === '1';
}
