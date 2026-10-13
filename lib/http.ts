export async function requestJson<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (init?.body) headers.set('Content-Type', 'application/json');
  const response = await fetch(`${base.replace(/\/$/, '')}${path}`, {
    cache: 'no-store',
    ...init,
    headers,
  });
  if (!response.ok) {
    let detail = '';
    try {
      const payload = await response.json() as { detail?: unknown };
      detail = typeof payload.detail === 'string' ? payload.detail : '';
    } catch {
      // Keep the HTTP status fallback for non-JSON hardware errors.
    }
    throw new Error(detail || `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}
