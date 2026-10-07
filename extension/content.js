// Entry point: picks the adapter for this page and runs the fill when the popup asks,
// or by itself when the popup's Autofill switch is on.

// wallSite: the email / sign-in screens of iCIMS, Oracle and SuccessFactors (ats.js).
// smartRecruitersSite needs generic.js's helpers, so it is loaded after it.
const SITES = [greenhouseSite, ashbySite, leverSite, ripplingSite, workdaySite, smartRecruitersSite, wallSite, genericSite];
// Lets the popup and background see the scripts are already here, so they are loaded once.
window.__jaLoaded = true;
let filling = null;

function startFill(site) {
  if (!filling) {
    showPanel("Filling the form...", false);
    filling = (site.run ? site.run() : runFill(site))
      .catch((error) => {
        const summary = `Something went wrong while filling: ${error.message}`;
        showPanel(summary, true);
        return { summary };
      })
      .finally(() => (filling = null));
  }
  return filling;
}

// On a job's posting page, opens its application instead (Ashby's form page, or the
// Apply step of iCIMS, Oracle and SuccessFactors). Returns a message, or null.
async function openApplication() {
  const ashby = typeof ashbyApplicationUrl === "function" && !SITES.slice(0, -1).some((s) => s.matches()) && ashbyApplicationUrl();
  if (ashby) {
    location.href = ashby;
    return "I opened the application form. Once it loads, click Fill this application again.";
  }
  return srOpenApplication() || (typeof atsOpenApplication === "function" ? atsOpenApplication() : null);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type !== "fill") return false;
  (async () => {
    const opened = await openApplication();
    if (opened) return { summary: opened };
    const site = SITES.find((s) => s.matches());
    return site ? startFill(site) : null;
  })().then((reply) => sendResponse(reply || undefined));
  return true;
});

// Autofill: wait for an application form (not a search box) to appear, then fill it once.
// A resume upload, a Workday step, or an email box among several other fields;
// a lone newsletter or search box doesn't count.
function looksLikeApplication() {
  if (document.querySelector('input[type=file], [data-automation-id="progressBarActiveStep"]')) return true;
  if (srApplyPage()) return srFields().length > 1; // its boxes are inside shadow roots
  // iCIMS / Oracle email steps and SuccessFactors / Oracle forms, which may have no file box yet.
  if (document.querySelector("form#enterEmailForm, input[name=primary-email], .apply-flow-section, #careerform #fbqa_apply, #careerform [role=combobox]")) return true;
  return Boolean(document.querySelector("input[type=email]")) && gnFields(gnForm()).length >= 3;
}

// A fill that reloaded the page to get past an error (Workday) carries on after the
// reload, even with the Autofill switch off.
function continueAfterReload() {
  try {
    const at = Number(sessionStorage.getItem("ja-continue") || 0);
    sessionStorage.removeItem("ja-continue");
    return Date.now() - at < 60000;
  } catch {
    return false;
  }
}

// Fills by itself when the popup's switch is on, when this tab was opened from the
// Find jobs page's Apply button, or right after a fill reloaded the page.
async function shouldAutofill() {
  if (continueAfterReload()) return true;
  const { autoFill } = await chrome.storage.local.get({ autoFill: false });
  if (autoFill) return true;
  const tab = await new Promise((resolve) => chrome.runtime.sendMessage({ type: "applyTab" }, (reply) => resolve(chrome.runtime.lastError ? null : reply)));
  return Boolean(tab);
}

// iCIMS reloads its page after a resume upload, which used to start the fill over
// and over; there a page fills by itself only once (click Fill to fill it again).
// Other sites (Workday reloads after you sign in) always fill by themselves.
function firstAutofill() {
  if (typeof ATS_ICIMS === "undefined" || !ATS_ICIMS) return true;
  const key = `ja-autofilled:${location.pathname}`;
  try {
    const at = Number(sessionStorage.getItem(key) || 0);
    if (Date.now() - at < 10 * 60 * 1000) return false;
    sessionStorage.setItem(key, String(Date.now()));
  } catch {
    // no session storage: fill anyway
  }
  return true;
}

shouldAutofill().then(async (yes) => {
  if (!yes) return;
  // Posting pages: open the application (Ashby's form page, or the Apply step of
  // iCIMS, Oracle and SuccessFactors) once the page has drawn its Apply button.
  await sleep(1200);
  if (await openApplication()) return;
  const site = await waitFor(() => looksLikeApplication() && SITES.find((s) => s.matches()), 20000);
  if (site && firstAutofill()) {
    await sleep(700); // let the page finish drawing its fields
    startFill(site);
  }
});
