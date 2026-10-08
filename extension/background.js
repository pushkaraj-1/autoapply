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

// ---- LinkedIn: "Queue this job" in the popup (linkedin.js) ----

// LinkedIn tab id -> the job you asked to queue. After that, the tab LinkedIn's Apply
// button opens (you click it yourself) is read for the company's page, closed, and
// the job is sent to the queue. Nothing is added to LinkedIn's page.
const captures = new Map();
const watching = new Map(); // opened tab id -> LinkedIn tab id
const CAPTURE_MS = 90000;
const ICON = "icon128.png";

// The company's page an opened tab is heading to, or null while it is still on LinkedIn
// (whose /jobs/view/externalApply/<id>?url=... hop carries the address in `url`).
function companyPage(url) {
  try {
    const page = new URL(url);
    if (/(^|\.)linkedin\.com$/.test(page.hostname)) {
      const inner = page.searchParams.get("url");
      return inner && /^https?:\/\//.test(inner) ? inner : null;
    }
    return /^https?:$/.test(page.protocol) ? url : null;
  } catch {
    return null;
  }
}

function tellResult(title, message, ok) {
  chrome.notifications.create({ type: "basic", iconUrl: ICON, title, message, priority: 1 });
  chrome.action.setBadgeBackgroundColor({ color: ok ? "#2ec4b6" : "#f07167" });
  chrome.action.setBadgeText({ text: ok ? "+1" : "!" });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 8000);
  chrome.storage.session.set({ linkedinResult: { title, message, ok, at: Date.now() } });
}

async function finishCapture(linkedinTab, openedTab, url) {
  const capture = captures.get(linkedinTab);
  if (!capture) return;
  captures.delete(linkedinTab);
  watching.delete(openedTab);
  clearTimeout(capture.timer);
  chrome.tabs.remove(openedTab).catch(() => {});
  const name = `${capture.job.title} at ${capture.job.company}`;
  try {
    const response = await fetch(SERVER + "/queue/link", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...capture.job, apply_url: url }) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) tellResult("Not queued", `${name}: ${data.detail || `the server answered ${response.status}`}`, false);
    else if (data.added) tellResult("Queued", `${name} (applies on ${data.site})`, true);
    else tellResult("Saved, not queued", `${name}: ${data.reason || "see Find jobs"}`, false);
  } catch {
    tellResult("Not queued", `${name}: the local server isn't running`, false);
  }
}

chrome.tabs.onCreated.addListener((tab) => {
  if (tab.openerTabId == null || !captures.has(tab.openerTabId)) return;
  watching.set(tab.id, tab.openerTabId);
  const url = companyPage(tab.pendingUrl || tab.url || "");
  if (url) finishCapture(tab.openerTabId, tab.id, url);
});

chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (!watching.has(tabId) || !change.url) return;
  const url = companyPage(change.url);
  if (url) finishCapture(watching.get(tabId), tabId, url);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type !== "linkedinCapture" || message.tabId == null) return false;
  const tabId = message.tabId;
  clearTimeout(captures.get(tabId)?.timer);
  const timer = setTimeout(() => {
    if (captures.delete(tabId)) tellResult("Not queued", `${message.job.title}: Apply wasn't clicked within a minute and a half. Click Queue this job again.`, false);
  }, CAPTURE_MS);
  captures.set(tabId, { job: message.job, timer });
  sendResponse(true);
  return false;
});
