// Relays calls from the content script to the local server (content scripts can't
// reach localhost themselves because of the page's CORS rules), and remembers the
// tabs opened from the Find jobs page so they fill by themselves.
const SERVER = "http://127.0.0.1:8765";
const APPLY_TABS = "applyTabs"; // tab id -> { boardUrl, at }, in session storage
const APPLY_TAB_HOURS = 6;

async function applyTabs() {
  const { [APPLY_TABS]: tabs = {} } = await chrome.storage.session.get(APPLY_TABS);
  const cutoff = Date.now() - APPLY_TAB_HOURS * 3600 * 1000;
  return Object.fromEntries(Object.entries(tabs).filter(([, t]) => t.at > cutoff));
}

async function rememberTab(tabId, boardUrl) {
  const tabs = await applyTabs();
  tabs[tabId] = { boardUrl, at: Date.now() };
  await chrome.storage.session.set({ [APPLY_TABS]: tabs });
}

// A job page that opens the real form in a new tab (LinkedIn, Indeed, company sites)
// passes the board job on to that tab.
chrome.tabs.onCreated.addListener(async (tab) => {
  if (tab.openerTabId == null) return;
  const opener = (await applyTabs())[tab.openerTabId];
  if (opener) rememberTab(tab.id, opener.boardUrl);
});

// Loads the content scripts into every frame of a tab that doesn't have them yet
// (job sites the extension isn't set up for). Used by Fill and by Apply tabs.
async function injectInto(tabId) {
  const files = chrome.runtime.getManifest().content_scripts[0].js;
  const checks = await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, func: () => Boolean(window.__jaLoaded) }).catch(() => []);
  const frameIds = checks.filter((c) => !c.result).map((c) => c.frameId);
  if (frameIds.length) await chrome.scripting.executeScript({ target: { tabId, frameIds }, files }).catch(() => {});
}

// Apply tabs on sites the extension isn't set up for still get filled.
chrome.tabs.onUpdated.addListener(async (tabId, change) => {
  if (change.status === "complete" && (await applyTabs())[tabId]) injectInto(tabId);
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const tabs = await applyTabs();
  if (tabs[tabId]) {
    delete tabs[tabId];
    chrome.storage.session.set({ [APPLY_TABS]: tabs });
  }
});

// Asks the server to start on the answers while the page loads.
function warm(url) {
  fetch(SERVER + "/plan/warm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) }).catch(() => {});
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "openApply") {
    chrome.tabs.create({ url: message.url }).then((tab) => rememberTab(tab.id, message.boardUrl));
    warm(message.url);
    return false;
  }
  if (message.type === "inject") {
    injectInto(message.tabId).then(() => sendResponse(true));
    return true;
  }
  if (message.type === "applyTab") {
    applyTabs().then((tabs) => sendResponse(sender.tab ? tabs[sender.tab.id] || null : null));
    return true;
  }
  if (message.type !== "api") return false;
  (async () => {
    let body = message.body;
    // Tell the server which Find jobs listing this application came from.
    if (message.path === "/log" && body && sender.tab) {
      const tab = (await applyTabs())[sender.tab.id];
      if (tab) body = { ...body, board_url: tab.boardUrl };
    }
    try {
      const response = await fetch(SERVER + message.path, {
        method: body ? "POST" : "GET",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await response.json().catch(() => ({}));
      sendResponse({ ok: response.ok, status: response.status, data });
    } catch (error) {
      sendResponse({ ok: false, status: 0, data: { detail: String(error) } });
    }
  })();
  return true; // keeps the channel open for the async reply
});
