let csrf = null;
export const setCsrf = (t) => { csrf = t; };

export async function api(method, path, body) {
  const headers = {};
  const opts = { method, headers, credentials: 'same-origin' };
  if (body !== undefined) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  if (csrf && method !== 'GET') headers['X-CSRF-Token'] = csrf;
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) throw apiError(r, data, path);
  return data;
}
export const get = (p) => api('GET', p);
export const post = (p, b = {}) => api('POST', p, b);
export const put = (p, b = {}) => api('PUT', p, b);
export const patch = (p, b = {}) => api('PATCH', p, b);
export const del = (p) => api('DELETE', p);

function apiError(r, data, path) {
  const e = new Error((data && data.error && data.error.message) || r.statusText || 'Request failed');
  e.status = r.status; e.code = data && data.error && data.error.code;
  if (r.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/me') window.dispatchEvent(new Event('apigate:logout'));
  return e;
}

// POST that answers with server-sent events; yields parsed JSON objects.
export async function* stream(path, body, signal) {
  const headers = { 'Content-Type': 'application/json' };
  if (csrf) headers['X-CSRF-Token'] = csrf;
  const r = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body), credentials: 'same-origin', signal });
  if (!r.ok) { let d = null; try { d = await r.json(); } catch { /* */ } throw apiError(r, d, path); }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      for (const line of raw.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (data && data !== '[DONE]') { try { yield JSON.parse(data); } catch { /* ignore */ } }
      }
    }
  }
}
