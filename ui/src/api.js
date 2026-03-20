/**
 * LeafHub Admin API client.
 *
 * Token stored in localStorage under 'lh_admin_token'.
 * If LEAFHUB_ADMIN_TOKEN is not set server-side, the header is ignored.
 * All errors throw an Error with a human-readable message.
 */

export function getAdminToken() {
  return localStorage.getItem('lh_admin_token') || ''
}

export function setAdminToken(token) {
  localStorage.setItem('lh_admin_token', token)
}

async function req(method, path, body) {
  const headers = { 'Content-Type': 'application/json' }
  const token = getAdminToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (res.status === 204) return null

  const json = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }))
  if (!res.ok) {
    const msg = json.detail || `HTTP ${res.status}`
    throw new Error(msg)
  }
  return json
}

// ── Providers ─────────────────────────────────────────────────────────────────

export const providers = {
  list:   ()       => req('GET',    '/admin/providers'),
  create: (data)   => req('POST',   '/admin/providers', data),
  update: (id, d)  => req('PUT',    `/admin/providers/${id}`, d),
  delete: (id)     => req('DELETE', `/admin/providers/${id}`),
}

// ── Projects ──────────────────────────────────────────────────────────────────

export const projects = {
  list:       ()            => req('GET',    '/admin/projects'),
  create:     (data)        => req('POST',   '/admin/projects', data),
  update:     (id, d)       => req('PUT',    `/admin/projects/${id}`, d),
  delete:     (id)          => req('DELETE', `/admin/projects/${id}`),
  rotate:     (id)          => req('POST',   `/admin/projects/${id}/rotate-token`),
  deactivate: (id)          => req('POST',   `/admin/projects/${id}/deactivate`),
  activate:   (id)          => req('POST',   `/admin/projects/${id}/activate`),
  link:       (id, path, copyProbe = true) => req('POST', `/admin/projects/${id}/link`, { path, copy_probe: copyProbe }),
}

// ── System ────────────────────────────────────────────────────────────────────

export const system = {
  health: () => req('GET', '/health'),
  status: () => req('GET', '/admin/status'),
}
