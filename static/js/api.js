/* Thin JSON client over the Covex-Board API. Every write goes through here so
   error messages reach the UI in one place. */

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (cause) {
    throw new ApiError('Cannot reach the server. Is it still running?', 0);
  }
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (response.status === 401 && !path.startsWith('/api/auth/')) {
    /* The session expired, or was ended from another browser. Send them back
       to the sign-in screen rather than raising the same error in every view. */
    window.location.replace('/login');
  }
  if (!response.ok) {
    const message = (payload && payload.error) || `${response.status} ${response.statusText}`;
    throw new ApiError(message, response.status);
  }
  return payload;
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === '' || value === 'all') continue;
    search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

export const api = {
  bootstrap: () => request('GET', '/api/bootstrap'),

  me: () => request('GET', '/api/auth/me'),
  logout: () => request('POST', '/api/auth/logout'),
  changePassword: (payload) => request('POST', '/api/auth/password', payload),

  users: () => request('GET', '/api/users'),
  createUser: (payload) => request('POST', '/api/users', payload),
  updateUser: (id, patch) => request('PATCH', `/api/users/${id}`, patch),
  resetUserPassword: (id, password) => request('POST', `/api/users/${id}/password`,
    password ? { password } : {}),
  deleteUser: (id) => request('DELETE', `/api/users/${id}`),

  project: (key) => request('GET', `/api/projects/${encodeURIComponent(key)}`),
  createProject: (payload) => request('POST', '/api/projects', payload),

  issues: (params) => request('GET', `/api/issues${query(params)}`),
  createIssue: (payload) => request('POST', '/api/issues', payload),
  issue: (id) => request('GET', `/api/issues/${id}`),
  updateIssue: (id, patch) => request('PATCH', `/api/issues/${id}`, patch),
  addComment: (id, payload) => request('POST', `/api/issues/${id}/comments`, payload),
  // One file at a time, base64 in the body. See tracker/api.py upload_attachment.
  uploadAttachment: (id, payload) => request('POST', `/api/issues/${id}/attachments`, payload),

  sprints: (key) => request('GET', `/api/projects/${encodeURIComponent(key)}/sprints`),
  createSprint: (key, payload) => request('POST', `/api/projects/${encodeURIComponent(key)}/sprints`, payload),
  updateSprint: (id, patch) => request('PATCH', `/api/sprints/${id}`, patch),
  sprintAction: (id, action, payload) => request('POST', `/api/sprints/${id}/${action}`, payload || {}),
  deleteSprint: (id) => request('DELETE', `/api/sprints/${id}`),

  stats: (key) => request('GET', `/api/projects/${encodeURIComponent(key)}/stats`),
};
