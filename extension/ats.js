// iCIMS, Oracle Recruiting Cloud and SAP SuccessFactors. Their application forms
// are filled by generic.js; this file handles what comes before and around them:
// the posting page (open the application), the email / sign-in screens (fill the
// email and consent, then hand over to you), and job details.
//
// None of the three lets you apply without an email or an account, and their
// Next / Sign In buttons can send you a code, so those are always left to you.

const ATS_ICIMS = /\.icims\.com$/.test(location.hostname);
const ATS_ORACLE = /\.oraclecloud\.com$/.test(location.hostname) && location.pathname.includes("/hcmUI/CandidateExperience/");
// The apply pages live on career*.successfactors.com / .eu, *.sapsf.com or, for its
// U.S. government cloud, career-*.ns2cloud.com (L3Harris); company career sites
// built on SuccessFactors are recognised by their Apply controls.
const ATS_SF_APPLY = /^career\d*\.successfactors\.(com|eu)$|\.sapsf\.(com|eu|cn)$|^career[\w-]*\.ns2cloud\.com$/.test(location.hostname);
const atsSfPosting = () => Boolean(document.querySelector("a.dialogApplyBtn, [id^='applyOption-'], .btn-social-apply"));

function atsPlatform() {
  if (ATS_ICIMS) return "icims";
  if (ATS_ORACLE) return "oracle";
  if (ATS_SF_APPLY || atsSfPosting()) return "successfactors";
  return null;
}

const ATS_NAMES = { icims: "iCIMS", oracle: "Oracle", successfactors: "SuccessFactors" };

// ---- the posting page: open the application ----

// Returns a message once it has opened the application, or null when this isn't a
// posting page it knows.
async function atsOpenApplication() {
  if (ATS_ORACLE && /\/job\/\d+\/?$/.test(location.pathname) && document.querySelector("button.apply-now-button")) {
    location.href = `${location.origin}${location.pathname.replace(/\/$/, "")}/apply/email`;
    return "I opened Oracle's application. Once it loads, click Fill this application again.";
  }
  if (ATS_ICIMS) {
    const apply = document.querySelector("a.iCIMS_ApplyOnlineButton");
    if (apply && !document.querySelector("#enterEmailForm")) {
      location.href = apply.href;
      return "I opened the iCIMS application. Once it loads, click Fill this application again.";
    }
  }
  if (!ATS_SF_APPLY && atsSfPosting() && !document.querySelector("#careerform")) {
    const manual = document.querySelector("#applyOption-top-manual, [id^='applyOption-'][id$='-manual']");
    const toggle = document.querySelector(".btn-social-apply button.dropdown-toggle");
    const plain = [...document.querySelectorAll("a.dialogApplyBtn")].find((a) => a.offsetParent);
    if (manual && toggle) {
      realClick(toggle);
      await sleep(300);
      manual.click();
    } else if (plain) plain.click();
    else if (manual) manual.click();
    else return null;
    return "I clicked Apply. Once the SuccessFactors page loads, click Fill this application again.";
  }
  return null;
}

// ---- email and sign-in screens ----

// Which screen this is, or null for an application form (or anything else).
function atsWall() {
  if (ATS_ICIMS && (document.querySelector("form#enterEmailForm, input[name=css_loginName]") || /\/(login|connect)$/.test(location.pathname))) return "icims-email";
  if (ATS_ORACLE) {
    if (document.querySelector(".pin-code-input__input")) return "oracle-pin";
    if (document.querySelector("quick-email-verification-form, email-verification-form, input[name=primary-email]")) return "oracle-email";
  }
  if (ATS_SF_APPLY) {
    if (document.querySelector("#fbqa_apply")) return null; // quick apply: a real form, account boxes included
    if (document.querySelector("#fbclc_createAccountButton")) return "sf-register";
    if (document.querySelector("input#username") && document.querySelector("input#password")) return "sf-signin";
  }
  return null;
}

function atsTick(box) {
  if (!box || box.checked) return;
  const label = (box.id && document.querySelector(`label[for="${CSS.escape(box.id)}"]`)) || box.closest("label");
  realClick(label || box);
  if (!box.checked) box.click();
}

const wallSite = {
  matches: () => Boolean(atsWall()),

  async run() {
    const wall = atsWall();
    const email = await new Promise((resolve) => chrome.runtime.sendMessage({ type: "api", path: "/profile/email" }, (reply) => resolve(reply && reply.ok ? reply.data.email : "")));
    let summary;
    if (wall === "icims-email") {
      const box = document.querySelector("input[name=css_loginName], form#enterEmailForm input[type=email]");
      if (box && email && !box.value) fillText(box, email);
      // Consent: "Continue" in its dropdown (some sites), then the I agree box.
      const consent = document.querySelector("select#gdpr_consent_type");
      if (consent) {
        const option = [...consent.options].find((o) => /continue|agree|accept|yes/i.test(o.text));
        if (option) {
          consent.value = option.value;
          consent.dispatchEvent(new Event("change", { bubbles: true }));
          await sleep(300);
        }
      }
      atsTick(document.querySelector("input#accept_gdpr"));
      summary = "This is iCIMS's email step. I filled your email and agreed to the privacy notice. Click Next yourself. iCIMS then asks you to sign in or create an account; after that, click Fill this application on the form.";
    } else if (wall === "oracle-email") {
      const box = document.querySelector("input[name=primary-email]");
      if (box && email && !box.value) fillText(box, email);
      const terms = document.querySelector("#legal-disclaimer-checkbox");
      if (terms && !terms.checked) {
        realClick(document.querySelector(".apply-flow-input-checkbox__button") || terms);
        await sleep(200);
        if (!terms.checked) atsTick(terms);
      }
      summary = "This is Oracle's email step. I filled your email" + (terms ? " and agreed to the terms" : "") + ". Click Next yourself. If Oracle emails you a 6-digit code, type it in. Then click Fill this application on the form.";
    } else if (wall === "oracle-pin") {
      summary = "Oracle sent a verification code to your email. Type it in and click Verify, then click Fill this application on the form.";
    } else if (wall === "sf-signin") {
      summary = "This is SuccessFactors' sign-in page. Sign in, or click Create an account if you're new here. Then click Fill this application on the form.";
    } else {
      summary = "This is SuccessFactors' create-account page. Fill in your email and a password, accept the privacy statement, and create the account. Then click Fill this application on the form.";
    }
    showPanel(summary, false);
    // The queue reads `wall` to take the next step itself (Oracle's Next and code).
    return { summary, failed: true, wall };
  },
};

// ---- job details ----

// Title and company for these platforms, or null to use the general reading.
function atsJobInfo() {
  if (ATS_ORACLE) {
    const company = document.querySelector('meta[property="og:site_name"]');
    const heading = document.querySelector("h1.job-details__title, h1");
    return { title: clean(heading ? heading.innerText : document.title.split(" - ")[0]), company: clean(company ? company.content : ""), description: "" };
  }
  if (ATS_SF_APPLY) {
    const heading = document.querySelector("h1, .jobTitle, #jobTitle");
    const title = clean(heading ? heading.innerText : document.title).replace(/^career opportunities:\s*/i, "").replace(/\s*\(\d+\)\s*$/, "");
    return { title, company: "", description: "" };
  }
  return null;
}

// ---- SuccessFactors: the resume ----

// The resume box has no file input until its plus icon is clicked; that opens a
// "Select a source" dialog with the real input.
async function atsSfAttachResume(file) {
  if (!ATS_SF_APPLY || !file) return null;
  const field = [...document.querySelectorAll(".attachmentField")].find((f) => /resume|cv/i.test(f.innerText));
  if (!field) return null;
  if (document.querySelector("#fbja_uploadedResumeId")?.value) return "kept";
  const plus = field.querySelector(".addAttachments, [id$='_attachIcon']");
  if (!plus) return null;
  realClick(plus);
  plus.click();
  const input = await waitFor(() => [...document.querySelectorAll("input[type=file].fileUpload")].find((i) => !i.files.length), 5000);
  if (!input) throw new Error("the resume upload box did not open");
  await attachFile(input, file, document.body, 1);
  const done = await waitFor(() => document.querySelector("#fbja_uploadedResumeId")?.value || [...document.querySelectorAll("[id$='_attachSuccess'], .attachSuccessIcon")].some((e) => e.offsetParent), 10000);
  if (!done) throw new Error("the resume did not finish uploading");
  return "attached";
}

// ---- Oracle: the profile import and education entries ----

const ATS_ORACLE_SAVE = ".app-dialog__footer-button.save-btn";

// Oracle's "Import your profile" reads the resume into the form and overwrites what
// is already there, so it goes first, before anything else is filled. It creates the
// education entries, so it is only used when there are none yet (Oracle keeps them
// from your earlier applications to the same company). It can take over a minute.
// Returns true once the import has finished.
async function atsOracleImport(files) {
  if (!ATS_ORACLE || !files || !files.resume) return false;
  const input = document.querySelector("input[type=file][class*='profile-import']");
  if (!input || document.querySelector(".apply-flow-profile-item-tile")) return false;
  const box = input.closest("[class*='profile-import']") || input.parentElement;
  await attachFile(input, files.resume, box, 1000);
  if (!(await waitFor(() => /successfully imported/i.test(document.body.innerText), 150000))) throw new Error("Oracle's resume import did not finish");
  await sleep(3000); // the form redraws with the imported details
  return true;
}

// Entries Oracle marks "Fields to fix" (imported education usually lacks its
// degree): opens each one, sets the degree from your profile's matching school,
// and saves. Returns the problems left.
async function atsOracleFixEntries(profile) {
  if (!ATS_ORACLE) return [];
  const problems = [];
  const words = (text) => clean(text).toLowerCase().replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter((w) => w.length > 2 && !/^(the|of|and|university|college|school|institute)$/.test(w));
  for (let round = 0; round < 6; round++) {
    const tile = [...document.querySelectorAll(".apply-flow-profile-item-tile--invalid")].find((t) => t.offsetParent && !t.dataset.jaTried);
    if (!tile) break;
    tile.dataset.jaTried = "1";
    const name = clean(tile.querySelector("[class*='summary-title']")?.innerText || "");
    const place = clean(tile.querySelector("[class*='summary-subtitle']")?.innerText || "");
    const edit = tile.querySelector("[class*='edit-item-icon']");
    if (!edit) continue;
    edit.click();
    const save = await waitFor(() => [...document.querySelectorAll(ATS_ORACLE_SAVE)].find((b) => b.offsetParent), 5000);
    if (!save) {
      problems.push(`${name || "An entry"} (${place}): Oracle says it has fields to fix, and its edit form did not open`);
      continue;
    }
    // The edit form is the nearest box around Save that holds its choices.
    let form = save.parentElement;
    while (form && form !== document.body && !form.querySelector("[role=radiogroup], .input-row--invalid")) form = form.parentElement;
    // Save shows before the choices are drawn.
    await waitFor(() => form && form.querySelector("[role=radiogroup] [role=radio]"), 4000);
    await sleep(300);
    const school = (profile.education || []).find((e) => {
      const want = words(place);
      return [e.school, e.school_full, ...(e.names || [])].some((s) => s && words(s).some((w) => want.includes(w)) && words(s).filter((w) => want.includes(w)).length >= Math.min(2, want.length));
    });
    for (const group of form ? form.querySelectorAll("[role=radiogroup]") : []) {
      const pills = [...group.querySelectorAll("[role=radio]:not(input)")];
      if (!pills.length || pills.some((p) => p.getAttribute("aria-checked") === "true")) continue;
      const label = group.getAttribute("aria-label") || "";
      if (!/degree/i.test(label) || !school) {
        problems.push(`${name || "An entry"} (${place}): ${label || "a choice"} is empty`);
        continue;
      }
      let pick = matchText(pills, school.degree_level || school.degree || "");
      if (!pick) {
        const reply = await api("/choose", { label, wanted: school.degree_level || school.degree, options: pills.map((p) => clean(p.innerText)) });
        pick = reply && reply.ok && reply.data.option ? matchText(pills, reply.data.option) : null;
      }
      if (!pick) {
        problems.push(`${name} (${place}): no degree option matches "${school.degree_level || school.degree}"`);
        continue;
      }
      realClick(pick);
      if (!(await waitFor(() => pick.getAttribute("aria-checked") === "true", 1500))) pick.click();
      await waitFor(() => pick.getAttribute("aria-checked") === "true", 1500);
    }
    save.click();
    const closed = await waitFor(() => !save.isConnected || !save.offsetParent, 6000);
    if (!closed) {
      const cancel = [...document.querySelectorAll(".app-dialog__footer-button.cancel-btn")].find((b) => b.offsetParent);
      problems.push(`${name || "An entry"} (${place}): Oracle would not save it${form ? `: ${clean(form.innerText).match(/[^.]*required[^.]*\./i)?.[0] || "a field still needs fixing"}` : ""}`);
      cancel?.click();
      await sleep(500);
    }
    await sleep(800);
  }
  const left = [...document.querySelectorAll(".apply-flow-profile-item-tile--invalid")].filter((t) => t.offsetParent);
  if (left.length && !problems.length) problems.push(`${left.length} entr${left.length === 1 ? "y" : "ies"} still marked "Fields to fix"`);
  return problems;
}

// ---- SuccessFactors: the application's folded sections ----

// The application form (not its sign-in or new-account pages).
function atsSfForm() {
  return ATS_SF_APPLY && !atsWall() && Boolean(document.querySelector("#careerform"));
}

// The sections start folded, which hides their fields; all are opened first.
async function atsSfExpand() {
  if (!atsSfForm()) return;
  const all = [...document.querySelectorAll("a, button")].find((a) => a.offsetParent && /^\s*expand all( sections)?\s*$/i.test(a.innerText));
  if (all) {
    realClick(all);
    await sleep(1200);
  }
  for (const header of document.querySelectorAll('#careerform [aria-expanded="false"]')) {
    if (header.offsetParent && !header.closest("[role=listbox], [role=combobox]") && header.getAttribute("role") !== "combobox") {
      realClick(header);
      await sleep(250);
    }
  }
  await sleep(800);
}
