const $ = (id) => document.getElementById(id);
const say = (t) => { $('status').textContent = t; };

chrome.storage.sync.get({ backendUrl: '', token: '' }).then((s) => { $('url').value = s.backendUrl; $('token').value = s.token; });
chrome.storage.local.get('vaultQueue').then(({ vaultQueue = [] }) => { if (vaultQueue.length) say(`${vaultQueue.length} capture(s) waiting to be sent.`); });

$('save').onclick = async () => {
  const backendUrl = $('url').value.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//.test(backendUrl)) return say('Address must start with https:// or http://');
  // ask Chrome for permission to talk to the Pi (only that origin)
  const origin = new URL(backendUrl).origin + '/*';
  const granted = await chrome.permissions.request({ origins: [origin] });
  if (!granted) return say('Permission to reach the Pi was not granted.');
  await chrome.storage.sync.set({ backendUrl, token: $('token').value.trim() });
  say('Saved ✓  Now click Test.');
};

$('test').onclick = () => {
  say('Testing…');
  chrome.runtime.sendMessage({ type: 'test' }, (r) => {
    say(r && r.ok ? 'Pi reachable ✓\n' + JSON.stringify(r.body, null, 2) : 'Cannot reach the Pi: ' + (r && r.error));
  });
};

$('flush').onclick = () => {
  chrome.runtime.sendMessage({ type: 'flush' }, (r) => say(`Sent ${r.sent}, still waiting ${r.remaining}.`));
};
