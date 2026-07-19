// ONE thin client. Request/response only - no timers, no polling.
// When the event feed ships it plugs in behind this same module as a push
// transport; the UI never learns the difference.

export const BASE = (process.env.WOLTSPACE_URL || 'http://localhost:7777').replace(/\/$/, '');

async function req(path, opts = {}) {
  let res;
  try {
    res = await fetch(BASE + path, opts);
  } catch (e) {
    throw new Error(`lodge unreachable at ${BASE} (${e.cause?.code || e.message})`);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON error body */
  }
  if (!res.ok) throw new Error(data?.error || data?.detail || `${res.status} ${res.statusText}`);
  return data;
}

const post = (path, body) =>
  req(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });

export const listSessions = () => req('/sessions');
export const listWolts = () => req('/wolts');
export const spawnSession = (wolt) => post('/sessions/new/lodge', { wolt });
export const stopSession = (name) => post(`/sessions/${encodeURIComponent(name)}/stop`);

// Human-attributed message: from_wolt names the sender, empty from_session
// means "no reply-by-session-id line" (the human isn't a session).
export const sendMessage = (sessionId, text, fromName) =>
  post(`/sessions/${encodeURIComponent(sessionId)}/message`, {
    text,
    from_wolt: fromName,
    from_session: '',
  });
