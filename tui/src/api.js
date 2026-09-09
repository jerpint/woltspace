// ONE thin client. Request/response only - no timers, no polling.
// When the event feed ships it plugs in behind this same module as a push
// transport; the UI never learns the difference.

// WOLTSPACE_API is what the platform stamps on everything it spawns — the
// whole address of *this* colony, port and all. WOLTSPACE_URL stays honoured
// behind it for anyone who set it by hand before the stamp existed. Falling
// through to 7777 when a colony runs elsewhere means driving the wrong lodge's
// sessions, so the stamp wins.
export const BASE = (
  process.env.WOLTSPACE_API?.trim() ||
  process.env.WOLTSPACE_URL?.trim() ||
  'http://localhost:7777'
).replace(/\/$/, '');

const unreachable = (e) =>
  `lodge unreachable at ${BASE} (${e.cause?.code || e.message}); run woltspace start, then retry`;

// A GET may be retried once; a POST may not.
//
// The first fetch after an attach has been seen to fail with ECONNRESET against
// a lodge that was demonstrably healthy - same pid, still listening, answering
// /health throughout. Pressing r immediately fixed it every time, so whatever
// the cause, it does not survive one more attempt. (The obvious suspect is a
// pooled socket the lodge closed while the tui sat blocked in spawnSync for the
// length of the session, unable to see the close; that did not reproduce under
// a deliberately blocked event loop, so it stays a suspect, not an answer.)
//
// What is certain is that the old behaviour was wrong: one dropped connection
// printed "run woltspace start" about a lodge that was already running. Reads
// only - a spawn or create the lodge already received would come back as a
// second session.
const idempotent = (opts) => !opts.method || opts.method.toUpperCase() === 'GET';

async function req(path, opts = {}) {
  let res;
  try {
    res = await fetch(BASE + path, opts);
  } catch (e) {
    // Only a connection-level failure is worth a second try, and even then the
    // message has to stay honest: if the retry fails the same way, the lodge
    // really is unreachable and `woltspace start` really is the answer.
    if (!idempotent(opts)) throw new Error(unreachable(e));
    try {
      res = await fetch(BASE + path, opts);
    } catch (again) {
      throw new Error(unreachable(again));
    }
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
export const runtimeCapabilities = () => req('/runtime/capabilities');
export const spawnSession = (wolt, workdir, executionPolicy) =>
  post('/sessions/new/lodge', {
    wolt,
    ...(workdir ? { workdir } : {}),
    ...(executionPolicy ? { execution_policy: executionPolicy } : {}),
  });
export const createWolt = (name, type, workdir, executionPolicy) =>
  post('/sessions/new/create', {
    name,
    type,
    ...(workdir ? { workdir } : {}),
    ...(executionPolicy ? { execution_policy: executionPolicy } : {}),
  });
export const stopSession = (name) => post(`/sessions/${encodeURIComponent(name)}/stop`);
// Harness-aware on the server side: rebuilds tmux if needed and restarts the
// agent with its own resume flavor (claude --resume / codex resume / opencode --session).
export const resumeSession = (name) => post(`/sessions/${encodeURIComponent(name)}/resume`);

// Human-attributed message: from_wolt names the sender, empty from_session
// means "no reply-by-session-id line" (the human isn't a session).
export const sendMessage = (sessionId, text, fromName) =>
  post(`/sessions/${encodeURIComponent(sessionId)}/message`, {
    text,
    from_wolt: fromName,
    from_session: '',
  });
