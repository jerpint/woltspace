import * as matrix from 'matrix-js-sdk';

const q = (selector) => document.querySelector(selector);
const shell = q('.shell');
const list = q('#wolt-list');
const input = q('#input');
const messages = q('#messages');
const timeline = q('#timeline');
const commands = q('#commands');
const dialog = q('#matrix-dialog');
const glyph = { raccoon: '🦝', beaver: '🦫', otter: '🦦', dog: '🐶', wolf: '🐺' };
const AUTH_KEY = 'woltspace.matrix.mvp.auth.v1';

let wolts = [];
let active;
let client;
let roomId = '';
let timelineListener;
let decryptedListener;

const escapeText = (value) => {
  const node = document.createElement('span');
  node.textContent = String(value ?? '');
  return node.innerHTML;
};

function renderWolts() {
  const needle = q('#search').value.toLowerCase();
  const shown = wolts.filter((wolt) => wolt.name.toLowerCase().includes(needle));
  list.innerHTML = shown.map((wolt) => `<button class="wolt-row${active?.name === wolt.name ? ' active' : ''}" data-name="${escapeText(wolt.name)}"><span class="wolt-avatar">${glyph[wolt.type] || '🐾'}</span><span class="wolt-meta"><strong>${escapeText(wolt.name)}</strong><small>${active?.name === wolt.name ? (client ? 'Encrypted Matrix room' : 'Room preview') : 'No room connected yet'}</small></span></button>`).join('') || 'No wolts found.';
  list.querySelectorAll('button').forEach((button) => { button.onclick = () => selectWolt(button.dataset.name); });
}

function selectWolt(name) {
  active = wolts.find((wolt) => wolt.name === name) || active;
  if (!active) return;
  const icon = glyph[active.type] || '🐾';
  q('#wolt-name').textContent = q('#detail-name').textContent = active.name;
  q('#avatar').textContent = q('#detail-avatar').textContent = icon;
  input.placeholder = `Message ${active.name}`;
  q('#terminal').href = active.session ? `/tui?session=${encodeURIComponent(active.session)}` : '/tui';
  renderWolts();
  shell.classList.remove('sidebar-open');
}

function addMessage({ who, body, time = 'now', eventId = '' }) {
  if (eventId && messages.querySelector(`[data-event-id="${CSS.escape(eventId)}"]`)) return;
  const human = who === 'You';
  const working = who === 'working';
  const article = document.createElement('article');
  article.className = `message ${human ? 'human' : working ? 'working' : ''}`;
  if (eventId) article.dataset.eventId = eventId;
  article.innerHTML = `<div class="message-avatar">${human ? 'YO' : glyph[active?.type] || '🦝'}</div><div><div class="message-head"><strong>${escapeText(working ? active.name : who)}</strong><time>${escapeText(time)}</time></div><div class="bubble">${escapeText(body)}</div></div>`;
  messages.append(article);
  timeline.scrollTop = timeline.scrollHeight;
  return article;
}

function showPreview() {
  messages.innerHTML = '';
  [
    { who: 'You', body: 'What should we build first?', time: '9:42' },
    { who: 'n00b', body: 'A tiny Matrix proof: one encrypted room, this clean chat surface, and Element as the independent client.', time: '9:42' },
    { who: 'You', body: 'And the terminal?', time: '9:43' },
    { who: 'n00b', body: 'Still here. Advanced opens the same live session in the workshop view.', time: '9:43' },
  ].forEach(addMessage);
}

function eventBody(event) {
  const content = event.getContent();
  return event.getType() === 'm.room.message' && content.msgtype === 'm.text' ? content.body : '';
}

function addMatrixEvent(event) {
  const body = eventBody(event);
  if (!body) return;
  const mine = event.getSender() === client.getUserId();
  const date = new Date(event.getTs());
  addMessage({
    who: mine ? 'You' : active.name,
    body,
    time: date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }),
    eventId: event.getId(),
  });
}

function renderMatrixTimeline() {
  const room = client?.getRoom(roomId);
  if (!room) return;
  messages.innerHTML = '';
  room.getLiveTimeline().getEvents().forEach(addMatrixEvent);
}

async function startMatrix(auth) {
  client = matrix.createClient({
    baseUrl: auth.homeserver,
    accessToken: auth.accessToken,
    userId: auth.userId,
    deviceId: auth.deviceId,
  });
  await client.initRustCrypto();
  roomId = auth.roomId;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Matrix sync timed out')), 20000);
    client.once(matrix.ClientEvent.Sync, (state) => {
      if (state === 'PREPARED') { clearTimeout(timeout); resolve(); }
    });
    client.startClient({ initialSyncLimit: 30 });
  });
  const room = client.getRoom(roomId);
  if (!room) throw new Error('That room is not joined on this account. Join it in Element first.');
  if (!client.isRoomEncrypted(roomId)) throw new Error('That Matrix room is not encrypted.');
  renderMatrixTimeline();
  timelineListener = (event, eventRoom, toStart) => {
    if (!toStart && eventRoom?.roomId === roomId) addMatrixEvent(event);
  };
  decryptedListener = (event) => {
    if (event.getRoomId() === roomId) renderMatrixTimeline();
  };
  client.on(matrix.RoomEvent.Timeline, timelineListener);
  client.on(matrix.MatrixEventEvent.Decrypted, decryptedListener);
  q('#connection').textContent = 'Encrypted Matrix room';
  q('#room-detail').textContent = roomId;
  q('#device-detail').textContent = client.getDeviceId();
  q('#matrix-connect').textContent = 'Connected';
  q('#matrix-disconnect').hidden = false;
  q('.preview').hidden = true;
  renderWolts();
}

async function disconnectMatrix() {
  const error = q('#matrix-error');
  error.textContent = '';
  if (!window.confirm('Disconnect this Woltspace Matrix device and remove its local encryption store?')) return;
  const current = client;
  if (current) {
    try {
      if (timelineListener) current.off(matrix.RoomEvent.Timeline, timelineListener);
      if (decryptedListener) current.off(matrix.MatrixEventEvent.Decrypted, decryptedListener);
      await current.logout(true);
      await current.clearStores();
    } catch (failure) {
      error.textContent = `Could not revoke this Matrix device: ${failure?.message || failure}`;
      return;
    }
  }
  localStorage.removeItem(AUTH_KEY);
  client = undefined;
  roomId = '';
  timelineListener = undefined;
  decryptedListener = undefined;
  q('#connection').textContent = 'Matrix preview';
  q('#room-detail').textContent = 'Preview only';
  q('#device-detail').textContent = 'Not connected';
  q('#matrix-connect').textContent = 'Connect Matrix';
  q('#matrix-disconnect').hidden = true;
  q('.preview').hidden = false;
  showPreview();
  renderWolts();
  dialog.close();
}

async function login(event) {
  event.preventDefault();
  const error = q('#matrix-error');
  error.textContent = '';
  const homeserver = q('#matrix-homeserver').value.replace(/\/$/, '');
  const userId = q('#matrix-user').value.trim();
  const password = q('#matrix-password').value;
  const requestedRoom = q('#matrix-room').value.trim();
  let loginClient;
  try {
    loginClient = matrix.createClient({ baseUrl: homeserver });
    const response = await loginClient.loginRequest({
      type: 'm.login.password',
      identifier: { type: 'm.id.user', user: userId },
      password,
      initial_device_display_name: 'Woltspace PWA',
    });
    const auth = { homeserver, userId: response.user_id, deviceId: response.device_id, accessToken: response.access_token, roomId: requestedRoom };
    localStorage.setItem(AUTH_KEY, JSON.stringify(auth));
    q('#matrix-password').value = '';
    await startMatrix(auth);
    dialog.close();
  } catch (failure) {
    localStorage.removeItem(AUTH_KEY);
    error.textContent = failure?.message || String(failure);
    if (client) { client.stopClient(); client = undefined; }
  }
}

async function boot() {
  try {
    const response = await fetch('/wolts');
    const payload = await response.json();
    wolts = Array.isArray(payload) ? payload : payload.wolts || [];
  } catch { wolts = []; }
  if (!wolts.some((wolt) => wolt.name === 'n00b')) wolts.unshift({ name: 'n00b', type: 'raccoon' });
  selectWolt('n00b');
  showPreview();
  const stored = localStorage.getItem(AUTH_KEY);
  if (stored) {
    q('#matrix-disconnect').hidden = false;
    try { await startMatrix(JSON.parse(stored)); }
    catch (failure) {
      q('#matrix-error').textContent = `Saved device could not reconnect: ${failure?.message || failure}`;
      dialog.showModal();
    }
  }
}

q('#composer').onsubmit = async (event) => {
  event.preventDefault();
  const body = input.value.trim();
  if (!body) return;
  if (body === '/terminal') return location.assign(q('#terminal').href);
  if (body === '/help') addMessage({ who: active.name, body: 'For this slice: send a message, type /terminal, or open Advanced.' });
  else if (client) {
    input.disabled = true;
    try { await client.sendTextMessage(roomId, body); }
    catch (failure) { addMessage({ who: active.name, body: `Message was not sent: ${failure?.message || failure}` }); }
    finally { input.disabled = false; input.focus(); }
  } else {
    addMessage({ who: 'You', body });
    const wait = addMessage({ who: 'working', body: 'Connect Matrix to send this message.' });
    setTimeout(() => wait.remove(), 2500);
  }
  input.value = '';
  commands.hidden = true;
};
input.oninput = () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 150)}px`; commands.hidden = !input.value.trimStart().startsWith('/'); };
input.onkeydown = (event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); q('#composer').requestSubmit(); } };
commands.querySelectorAll('button').forEach((button) => { button.onclick = () => { input.value = button.dataset.command; commands.hidden = true; input.focus(); }; });
q('#search').oninput = renderWolts;
q('#details-open').onclick = () => shell.classList.toggle('details-open');
q('#details-close').onclick = () => shell.classList.remove('details-open');
q('#nav-open').onclick = () => shell.classList.add('sidebar-open');
q('#nav-close').onclick = () => shell.classList.remove('sidebar-open');
q('#matrix-connect').onclick = () => dialog.showModal();
q('#matrix-close').onclick = q('#preview-mode').onclick = () => dialog.close();
q('#matrix-disconnect').onclick = disconnectMatrix;
q('#matrix-form').onsubmit = login;

boot();
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
