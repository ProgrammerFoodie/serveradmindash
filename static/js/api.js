// Thin fetch wrapper. A 401 anywhere means the session is gone: go back to the login page.

let csrf = "";

export class AuthError extends Error {
  constructor() { super("signed out"); this.name = "AuthError"; }
}

async function request(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", cache: "no-store", ...opts });
  if (res.status === 401) { location.href = "/login"; throw new AuthError(); }
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}

export const api = {
  get: (path) => request(path),
  async session() { const s = await request("/api/session"); csrf = s.csrf; return s; },
  live: (tab) => request(`/api/live?tab=${encodeURIComponent(tab)}`),
  history: (metrics, range) => request(`/api/history?metrics=${metrics.map(encodeURIComponent).join(",")}&range=${encodeURIComponent(range)}`),
  logs: (kind, name, lines = 200) => request(`/api/logs?kind=${kind}&name=${encodeURIComponent(name)}&lines=${lines}`),
  post: (path, body) => request(path, {
    method: "POST", body: JSON.stringify(body || {}),
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
  }),
};
