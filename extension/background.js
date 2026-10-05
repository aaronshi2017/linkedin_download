// Sends captures to the Pi. If the Pi can't be reached, keeps them in a local queue and retries every 5 min.
const QUEUE_KEY = 'vaultQueue';

async function settings() {
  const s = await chrome.storage.sync.get({ backendUrl: '', token: '' });
  return { url: s.backendUrl.replace(/\/+$/, ''), token: s.token };
}

async function post(payload) {
  const { url, token } = await settings();
  if (!url || !token) throw new Error('Set the Pi URL and token in the extension options first.');
  const r = await fetch(`${url}/capture`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Vault-Token': token },
    body: JSON.stringify(payload)
  });
  if (r.status === 401) throw Object.assign(new Error('Wrong token'), { fatal: true });
  if (!r.ok) throw new Error(`Pi returned HTTP ${r.status}`);
  return r.json();
}

async function enqueue(payload) {
  const { [QUEUE_KEY]: q = [] } = await chrome.storage.local.get(QUEUE_KEY);
  q.push({ payload, at: Date.now() });
  await chrome.storage.local.set({ [QUEUE_KEY]: q });
  updateBadge(q.length);
}

async function flush() {
  const { [QUEUE_KEY]: q = [] } = await chrome.storage.local.get(QUEUE_KEY);
  const remaining = [];
  for (const item of q) {
    try { await post(item.payload); } catch (e) { remaining.push(item); }
  }
  await chrome.storage.local.set({ [QUEUE_KEY]: remaining });
  updateBadge(remaining.length);
  return { sent: q.length - remaining.length, remaining: remaining.length };
}

function updateBadge(n) {
  chrome.action.setBadgeText({ text: n ? String(n) : '' });
  chrome.action.setBadgeBackgroundColor({ color: '#d97706' });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'capture') {
    post(msg.payload)
      .then((res) => sendResponse({ ok: true, ...res }))
      .catch(async (e) => {
        if (e.fatal || /Set the Pi URL/.test(e.message)) return sendResponse({ ok: false, error: e.message });
        await enqueue(msg.payload);
        sendResponse({ ok: false, queued: true, error: e.message });
      });
    return true; // async response
  }
  if (msg.type === 'flush') { flush().then(sendResponse); return true; }
  if (msg.type === 'test') {
    settings().then(async ({ url }) => {
      try { const r = await fetch(`${url}/health`); sendResponse({ ok: r.ok, body: await r.json() }); }
      catch (e) { sendResponse({ ok: false, error: e.message }); }
    });
    return true;
  }
});

chrome.alarms.create('vault-retry', { periodInMinutes: 5 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === 'vault-retry') flush(); });
