// Background service worker: proxies requests to the local karaoke server.
// Content scripts on youtube.com cannot reach 127.0.0.1 directly (Local Network Access rules),
// but the extension itself can once the user has granted local-network access to this extension
// (Chrome asks for that on setup.html, which opens on install and from the panel).
const bufToB64 = buf => new Promise(res => { const fr = new FileReader(); fr.onload = () => res(fr.result.split(',')[1]); fr.readAsDataURL(new Blob([buf])); });
const openSetup = () => chrome.tabs.create({ url: chrome.runtime.getURL('setup.html') });

chrome.runtime.onInstalled.addListener(() => openSetup());
chrome.action?.onClicked.addListener(() => openSetup());

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.open === 'setup') { openSetup(); sendResponse({ ok: true }); return; }
  (async () => {
    try {
      const r = await fetch(msg.url, msg.init);
      if (!r.ok) return sendResponse({ error: `${msg.url} ${r.status}`, status: r.status });
      if (msg.binary) sendResponse({ b64: await bufToB64(await r.arrayBuffer()) });
      else sendResponse({ json: await r.json() });
    } catch (e) { sendResponse({ error: e.message }); }
  })();
  return true; // async response
});
