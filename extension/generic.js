// Any other job site: read the labelled fields of the application form on the page,
// ask the local server for answers, and fill them. Built for Gusto, Wellfound and
// iCIMS, and used as a best effort on every other site when you click Fill (the
// extension then loads itself into that page). The job's company, title and
// description are read from the page too. iCIMS shows its form inside an iframe
// (?in_iframe=1), where this script also runs.

const GN_HOSTS = /(^|\.)jobs\.gusto\.com$|(^|\.)wellfound\.com$|\.icims\.com$|\.oraclecloud\.com$|\.successfactors\.(com|eu)$|\.sapsf\.(com|eu|cn)$|\.ns2cloud\.com$/;
const GN_ICIMS = /\.icims\.com$/.test(location.hostname);
// Oracle fills in answers saved from earlier applications to the same company, and
// from unfinished ones. Its questions are answered again, so an old answer is never sent.
const GN_REANSWER = /\.oraclecloud\.com$/.test(location.hostname);
// Its address lists are answered again too: its resume import can put in the wrong
// city. Each one only depends on the one before (City lists that state's cities, ZIP
// that city's codes), so they go in this order, and the street goes last, typed: its
// suggestions are other cities' streets of the same name.
const GN_ADDRESS_ORDER = [/^country/i, /^state|province/i, /^city/i, /^(zip|postal)/i, /^county/i, /^address line 1/i];
const gnAddressRank = (field) => {
  const rank = GN_ADDRESS_ORDER.findIndex((re) => re.test(clean(field.label)));
  return rank < 0 ? 0 : rank - GN_ADDRESS_ORDER.length;
};
const gnReanswer = (field) => GN_REANSWER && field.filled && (field.kind === "select" || (field.kind === "listbox" && gnAddressRank(field) < 0));
const GN_SKIP_TYPES = /^(hidden|submit|button|reset|password|search|image)$/;
// Page furniture that is never part of an application.
const GN_OUTSIDE = 'header, footer, nav, [role="search"], [role="banner"], [role="navigation"], #onetrust-consent-sdk, [id*="cookie" i], [class*="cookie" i], #job-autofill-panel';
const GN_CONTROLS = 'input, textarea, select, button[aria-haspopup="listbox"], [role="combobox"]:not(input)';
const GN_PLACEHOLDER = /^(select|choose|please select|pick|no selection|--|—|-)\b|^\s*$/i;

// The application is the form with the most fillable fields; pages without a real
// form (common on newer sites) are read as a whole.
function gnForm() {
  const forms = [...document.querySelectorAll("form")].filter((f) => !f.closest(GN_OUTSIDE));
  const count = (root) => gnFields(root).length;
  const best = forms.map((f) => [f, count(f)]).sort((a, b) => b[1] - a[1])[0];
  return best && best[1] >= 2 ? best[0] : document.body;
}

// "applicant_first_name" or "firstName" as "applicant first name" / "first Name".
function gnHumanize(text) {
  return clean(String(text || "").replace(/[_\-\[\].]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/\b\d+\b/g, ""));
}

function gnLabel(element) {
  const byFor = element.id && document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
  const byIds = (element.getAttribute("aria-labelledby") || "").split(" ").map((id) => document.getElementById(id)).filter(Boolean);
  let text =
    (byFor && byFor.innerText) ||
    (element.closest("label") && element.closest("label").innerText) ||
    byIds.map((e) => e.innerText).join(" ") ||
    element.getAttribute("aria-label") ||
    (element.closest("fieldset") && element.closest("fieldset").querySelector("legend") && element.closest("fieldset").querySelector("legend").innerText) ||
    "";
  // Unlabelled boxes (often file uploads): use the nearby text.
  for (let box = element.parentElement, i = 0; !clean(text) && box && i < 4; box = box.parentElement, i++) text = box.innerText;
  return gnCleanLabel(text || element.placeholder || "") || gnHumanize(element.name || element.id);
}

function gnCleanLabel(text) {
  // A required star ends the label; anything after it is help or screen-reader text.
  const firstLine = clean(clean(text).split("\n")[0]).replace(/^\*\s*/, "");
  return (firstLine.indexOf("*") > 2 ? firstLine.slice(0, firstLine.indexOf("*")) : firstLine)
    .replace(/\s*\bSelect\.*$/, "")
    .replace(/\s*\((optional|required)\)\s*$/i, "")
    .replace(/\s*\*\s*$/, "")
    .replace(/\s*:\s*$/, "")
    .slice(0, 300);
}

function gnRequired(element, rawLabel) {
  return element.required || element.getAttribute("aria-required") === "true" || /\*/.test(rawLabel || "");
}

// Radios share a name; checkbox groups often only share an id prefix
// ("...[359453][jobListingQuestionOptionId]--267725").
function gnGroupKey(element) {
  // Radios already share a name per question (Workable's "QA_12563770", "QA_12563778"
  // are different questions), so only checkbox ids lose their numeric suffix.
  if (element.type === "radio" && element.name) return element.name;
  return (element.name || element.id || "").replace(/(--|-|_)\d+$/, "").replace(/\[\]$/, "");
}

function gnCommonAncestor(elements) {
  let node = elements[0].parentElement;
  while (node && !elements.every((e) => node.contains(e))) node = node.parentElement;
  return node;
}

// Text of one option: its own label (Oracle hides the input and labels it apart),
// else climb from the input while the box holds no other option.
function gnOptionText(input, group) {
  const own = [...(input.labels || [])].map((l) => clean(l.innerText)).find(Boolean);
  if (own) return own;
  let box = input;
  while (box.parentElement && group.filter((e) => box.parentElement.contains(e)).length === 1) box = box.parentElement;
  return clean(box.innerText || input.value);
}

// The question is the text around the options. If the options' shared box holds
// nothing else, step out while no other question's inputs come into view.
function gnQuestionText(group, options) {
  const minusOptions = (box) => {
    let text = box ? box.innerText : "";
    for (const option of options) text = text.replace(option, " ");
    return gnCleanLabel(text.replace(/\s+/g, " "));
  };
  let container = gnCommonAncestor(group);
  while (container && !minusOptions(container) && container.parentElement && container.parentElement.querySelectorAll("input, textarea, select").length === group.length) {
    container = container.parentElement;
  }
  return minusOptions(container);
}

function gnListboxFilled(element) {
  return !GN_PLACEHOLDER.test(clean(element.innerText || element.value || ""));
}

function gnFields(form) {
  const fields = [];
  const seen = new Set();
  const all = [...form.querySelectorAll(GN_CONTROLS)].filter((e) => !e.closest(GN_OUTSIDE));
  for (const element of all) {
    const type = (element.type || "").toLowerCase();
    const listbox = element.tagName !== "INPUT" && element.tagName !== "SELECT" && element.tagName !== "TEXTAREA";
    if (listbox) {
      // Custom dropdowns: a button or box that opens a list of [role=option] items.
      if (element.offsetParent === null || element.closest("[role=listbox]")) continue;
      const key = element.id || element.getAttribute("name") || `listbox-${all.indexOf(element)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const label = gnLabel(element);
      fields.push({ key, label, kind: "listbox", options: [], required: gnRequired(element, label), group: [element], filled: gnListboxFilled(element) });
      continue;
    }
    if (GN_SKIP_TYPES.test(type) || element.disabled || element.readOnly && type !== "file") continue;
    // Honeypots: boxes people never see, there to catch bots. Never filled.
    if (/honey.?pot/i.test(`${element.name} ${element.id} ${element.className}`)) continue;
    // Oracle's "Import your profile" box re-reads the resume over the whole form; it
    // is used (or not) by atsOracleImport, never as an upload box.
    if (type === "file" && /profile-import/.test(String(element.className))) continue;
    // Boxes hidden from people (aria-hidden) are mirrors the site fills itself, such as
    // Workable's city and postcode behind its location search. File inputs are often
    // hidden behind an upload button, so those still count.
    if (type !== "file" && type !== "radio" && type !== "checkbox" && element.closest('[aria-hidden="true"]')) continue;
    // iCIMS hides its real dropdowns behind a styled box; they count while that box shows.
    const styledSelect = GN_ICIMS && element.tagName === "SELECT" && element.parentElement && element.parentElement.offsetParent !== null;
    if (type !== "file" && type !== "radio" && type !== "checkbox" && !styledSelect && element.offsetParent === null) continue;
    // Styled radios and checkboxes hide the real input, but their label or box still shows.
    // A box that is hidden as a whole (iCIMS's EU consent for non-EU visitors) is left alone.
    const ownLabel = element.labels && element.labels[0];
    if ((type === "radio" || type === "checkbox") && element.offsetParent === null && !(ownLabel && ownLabel.offsetParent) && !(element.parentElement && element.parentElement.offsetParent)) continue;
    // A combobox input that belongs to a custom dropdown is handled through that dropdown.
    if (element.getAttribute("role") === "combobox" && element.closest('[aria-haspopup="listbox"]')) continue;
    // Pick-from-a-list inputs (role=combobox, no typing to search) behave like dropdowns.
    // Oracle's dropdowns (cx-select) can be typed in to search, but still only take an option.
    const pickOnly = element.getAttribute("aria-autocomplete") !== "list" || element.classList.contains("cx-select-input");
    if (element.tagName === "INPUT" && element.getAttribute("role") === "combobox" && pickOnly && (element.getAttribute("aria-controls") || element.hasAttribute("aria-expanded"))) {
      const key = element.name || element.id || `listbox-${all.indexOf(element)}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const label = gnLabel(element);
      fields.push({ key, label, kind: "listbox", options: [], required: gnRequired(element, label), group: [element], filled: gnListboxFilled(element) });
      continue;
    }
    const choice = type === "radio" || type === "checkbox";
    const key = choice ? gnGroupKey(element) : element.name || element.id || `${type || element.tagName.toLowerCase()}-${all.indexOf(element)}`;
    if (!key || seen.has(key)) continue;
    seen.add(key);
    const group = choice ? all.filter((e) => e.type === type && gnGroupKey(e) === key) : [element];
    let kind = "text";
    let options = [];
    let label = gnLabel(element);
    // Masked boxes start out holding their mask, "(___) ___-____", which is not an answer.
    let filled = /[a-z0-9]/i.test(element.value || "");
    if (element.tagName === "TEXTAREA") kind = "textarea";
    else if (element.tagName === "SELECT") {
      kind = "select";
      const blank = (o) => !o || !o.value || /no.?selection|^(0|-1|null|none)$/i.test(o.value) || GN_PLACEHOLDER.test(clean(o.text));
      options = [...element.options].filter((o) => !blank(o)).map((o) => clean(o.text));
      filled = element.selectedIndex > 0 && !blank(element.selectedOptions[0]);
    } else if (choice && (type === "radio" || group.length > 1)) {
      kind = type === "radio" ? "select" : "checkboxes";
      options = group.map((e) => gnOptionText(e, group));
      label = gnQuestionText(group, options) || label;
      filled = group.some((e) => e.checked);
    } else if (type === "checkbox") {
      kind = "checkbox";
      options = ["Yes", "No"];
      filled = element.checked;
    } else if (type === "file") {
      kind = "file";
      filled = element.files.length > 0;
    } else if (type === "number") kind = "number";
    else if (type === "date" || type === "month") kind = type;
    fields.push({ key, label, kind, options, required: gnRequired(element, label) || /\*/.test(gnCommonAncestor(group)?.innerText.split("\n")[0] || ""), group, filled });
  }
  // Choices drawn as buttons (role="radio" buttons in a radiogroup, such as Oracle's
  // pills for Title and Degree).
  // SuccessFactors draws its radios this way too, naming the question and each option
  // through aria-labelledby.
  for (const box of form.querySelectorAll('[role="radiogroup"]')) {
    const group = [...box.querySelectorAll('[role="radio"]:not(input)')].filter((b) => b.offsetParent !== null);
    if (!group.length || box.closest(GN_OUTSIDE)) continue;
    const row = box.closest(".input-row, fieldset, .form-group, li, .RCMFormField") || box.parentElement;
    const rowLabel = row && row.querySelector("label, legend");
    const raw = box.getAttribute("aria-label") || gnAriaText(box) || (rowLabel && rowLabel.innerText) || "";
    const label = gnCleanLabel(raw);
    if (!label) continue;
    const key = `pills-${label}`;
    const required = /\*/.test(raw) || Boolean(row && row.querySelector('[class*="required"]')) || box.getAttribute("aria-required") === "true";
    fields.push({ key, label, kind: "pills", options: group.map((b) => gnAriaText(b) || clean(b.innerText)), required, group, filled: group.some((b) => b.getAttribute("aria-checked") === "true") });
  }
  return fields;
}

// Text an element is named by through aria-labelledby, if any.
function gnAriaText(element) {
  const ids = (element.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
  return clean(ids.map((id) => document.getElementById(id)?.innerText || "").join(" "));
}

// ---- custom dropdowns ----

function gnVisibleOptions() {
  return [...document.querySelectorAll('[role="option"]')].filter((o) => o.offsetParent !== null && !GN_PLACEHOLDER.test(clean(o.innerText)));
}

// The choices of this dropdown only: the list it points to with aria-controls
// (empty until that list opens), or else whatever list is showing.
function gnOptionsFor(element) {
  const id = element.getAttribute("aria-controls") || element.getAttribute("aria-owns");
  if (!id) return gnVisibleOptions();
  const list = document.getElementById(id);
  // Oracle's lists hold grid cells rather than options.
  return list ? [...list.querySelectorAll('[role="option"], [role="gridcell"].cx-select__list-item')].filter((o) => o.offsetParent !== null && !GN_PLACEHOLDER.test(clean(o.innerText))) : [];
}

function gnListOpen(element) {
  return element.getAttribute("aria-expanded") === "true" || gnOptionsFor(element).length > 0;
}

// Escape first; some lists only close on a click outside them or when focus leaves.
async function gnCloseListbox(element) {
  const escape = { bubbles: true, key: "Escape", code: "Escape", keyCode: 27 };
  for (const target of [element, document.activeElement]) target?.dispatchEvent(new KeyboardEvent("keydown", escape));
  if (await waitFor(() => !gnListOpen(element), 250)) return;
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    document.body.dispatchEvent(new (type.startsWith("pointer") ? PointerEvent : MouseEvent)(type, { bubbles: true, cancelable: true, clientX: 2, clientY: 2 }));
  }
  element.blur();
  await waitFor(() => !gnListOpen(element), 500);
}

async function gnOpenListbox(element) {
  element.scrollIntoView({ block: "center" });
  realClick(element);
  let options = await waitFor(() => gnOptionsFor(element).length && gnOptionsFor(element), 1500);
  if (!options) {
    element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "ArrowDown", keyCode: 40 }));
    options = await waitFor(() => gnOptionsFor(element).length && gnOptionsFor(element), 1500);
  }
  return options || [];
}

// Reads each dropdown's choices so the answers can pick from them.
async function gnReadListOptions(fields) {
  for (const field of fields.filter((f) => f.kind === "listbox" && !f.filled).slice(0, 30)) {
    const [element] = field.group;
    field.options = (await gnOpenListbox(element)).map((o) => clean(o.innerText)).slice(0, 400);
    // Long lists that load a page at a time (SuccessFactors shows 100) can be searched;
    // country lists often hold the United States past the first page.
    if (element.tagName === "INPUT" && field.options.length >= 90 && !field.options.some((o) => /united states/i.test(o))) {
      const found = await gnSearchList(element, "United States");
      field.options.push(...found.map((o) => clean(o.innerText)).filter((o) => !field.options.includes(o)));
      setNativeValue(element, "");
    }
    await gnCloseListbox(element);
  }
}

// Types into a searchable dropdown and returns the options it then shows.
async function gnSearchList(element, text) {
  element.focus();
  setNativeValue(element, text);
  for (const type of ["keydown", "keyup"]) element.dispatchEvent(new KeyboardEvent(type, { bubbles: true, key: text.slice(-1), keyCode: text.toUpperCase().charCodeAt(text.length - 1) }));
  await sleep(300);
  // The list has caught up once an option starts with the text; lists that keep
  // the term mid-option ("Bachelor of Science (BS)") are taken a little later.
  const want = text.toLowerCase().slice(0, 4);
  const starts = () => {
    const options = gnOptionsFor(element);
    return options.some((o) => clean(o.innerText).toLowerCase().startsWith(want)) && options;
  };
  const shown = () => {
    const options = gnOptionsFor(element);
    return options.some((o) => clean(o.innerText).toLowerCase().includes(want)) && options;
  };
  const found = (await waitFor(starts, 2500)) || shown();
  if (found) return found;
  // Some lists (Oracle's ZIP code) only search on real typing.
  setNativeValue(element, "");
  element.focus();
  document.execCommand("insertText", false, text);
  return (await waitFor(starts, 4000)) || shown() || [];
}

// Some lists show states as codes ("CA").
const GN_STATE_CODES = Object.fromEntries(
  "Alabama AL|Alaska AK|Arizona AZ|Arkansas AR|California CA|Colorado CO|Connecticut CT|Delaware DE|District of Columbia DC|Florida FL|Georgia GA|Hawaii HI|Idaho ID|Illinois IL|Indiana IN|Iowa IA|Kansas KS|Kentucky KY|Louisiana LA|Maine ME|Maryland MD|Massachusetts MA|Michigan MI|Minnesota MN|Mississippi MS|Missouri MO|Montana MT|Nebraska NE|Nevada NV|New Hampshire NH|New Jersey NJ|New Mexico NM|New York NY|North Carolina NC|North Dakota ND|Ohio OH|Oklahoma OK|Oregon OR|Pennsylvania PA|Rhode Island RI|South Carolina SC|South Dakota SD|Tennessee TN|Texas TX|Utah UT|Vermont VT|Virginia VA|Washington WA|West Virginia WV|Wisconsin WI|Wyoming WY"
    .split("|")
    .map((pair) => [pair.slice(0, -3).toLowerCase(), pair.slice(-2)]),
);

// "January 2027", "2027-01-25" or "01/25/2027" as the value a date box wants.
function gnDateValue(value, kind) {
  const text = String(value).trim();
  let iso = /^\d{4}-\d{2}(-\d{2})?$/.test(text) ? text : "";
  if (!iso) {
    const parsed = new Date(/^[A-Za-z]+ \d{4}$/.test(text) ? `1 ${text}` : text);
    if (Number.isNaN(parsed.getTime())) throw new Error(`could not read "${value}" as a date`);
    iso = `${parsed.getFullYear()}-${String(parsed.getMonth() + 1).padStart(2, "0")}-${String(parsed.getDate()).padStart(2, "0")}`;
  }
  return kind === "month" ? iso.slice(0, 7) : iso.length === 7 ? `${iso}-01` : iso;
}

async function gnJobInfo() {
  const ats = typeof atsJobInfo === "function" && atsJobInfo();
  if (ats) return ats;
  // Gusto's form lives on /applicants/new and iCIMS's on /login, /candidate and so
  // on; the job text is on the posting page.
  let doc = document;
  const posting = GN_ICIMS
    ? location.href.replace(/(\/jobs\/\d+\/[^/?#]+)\/.*$/, "$1/job?in_iframe=1")
    : location.href.replace(/\/applicants\/new.*$/, "");
  if (posting !== location.href) {
    try {
      doc = new DOMParser().parseFromString(await (await fetch(posting)).text(), "text/html");
    } catch {
      doc = document;
    }
  }
  if (GN_ICIMS) {
    // The page title names the city, not the company, so the company comes from
    // the site address (internal-vtgdefense.icims.com gives "vtgdefense").
    const heading = doc.querySelector(".iCIMS_Header, h1");
    const body = doc.querySelector(".iCIMS_JobContent, .iCIMS_JobsTable, #iCIMS_MainColumn") || doc.body;
    return {
      title: clean(heading ? heading.innerText || heading.textContent : doc.title.split(" in ")[0]),
      company:
        clean((([...doc.querySelectorAll("a")].map((a) => a.textContent).find((t) => /^\s*back to .+ careers\s*$/i.test(t || "")) || "").match(/back to (.+) careers/i) || [])[1] || "") ||
        location.hostname.split(".")[0].replace(/^(careers|internal|external|jobs|uscareers|campus)-/, ""),
      description: clean(body.innerText || body.textContent || "").slice(0, 12000),
    };
  }
  const title = clean(doc.title).replace(/^\*\s*/, "").replace(/^(submit (your |an )?application for|apply (now )?(for|to))\s+/i, "");
  const [jobTitle, company] = title.includes(" at ") ? [title.slice(0, title.lastIndexOf(" at ")), title.slice(title.lastIndexOf(" at ") + 4)] : [title, ""];
  const main = doc.querySelector("main") || doc.body;
  const description = (main.innerText || main.textContent || "").slice(0, 12000);
  const meta = doc.querySelector('meta[property="og:site_name"]');
  return { title: clean(jobTitle.split("|")[0]), company: clean(company.split("|")[0].split("•")[0]) || (meta && meta.content) || "", description };
}

// International phone widgets put a country flag picker right next to the box.
function gnFlagPicker(element) {
  for (let node = element.parentElement, i = 0; node && i < 3; node = node.parentElement, i++) {
    if (node.querySelector('[class*="flag" i], [id*="flag" i], .iti__country-container, .iti__flag-container')) return true;
  }
  return false;
}

// Search-as-you-type boxes (location, years): type, then pick the matching suggestion.
async function gnAutocomplete(element, value) {
  const want = String(value).toLowerCase();
  const [first, second] = want.split(",").map((s) => s.trim());
  const find = () => {
    const options = [...document.querySelectorAll("[role=option], [id*='-option-']")].filter((o) => o.offsetParent);
    const text = (o) => clean(o.innerText).toLowerCase();
    return (
      options.find((o) => text(o) === want) ||
      options.find((o) => text(o).startsWith(first) && (!second || text(o).includes(second) || text(o).includes(", ca"))) ||
      options.find((o) => text(o).startsWith(first)) ||
      null
    );
  };
  // Dropdown-style boxes open on a click of their outer box plus Down Arrow;
  // search boxes need typing first.
  element.focus();
  realClick(element.closest("[class*=control]") || element);
  element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "ArrowDown", keyCode: 40 }));
  let option = await waitFor(find, 1500);
  if (!option) {
    setNativeValue(element, String(value).split(",")[0]);
    for (const type of ["keydown", "keyup"]) element.dispatchEvent(new KeyboardEvent(type, { bubbles: true, key: "a", keyCode: 65 }));
    option = await waitFor(find, 8000);
  }
  if (!option) throw new Error(`no suggestion matched "${value}"`);
  realClick(option);
  await sleep(120);
}

// Like fillText, but gives the page a moment to save the value before focus
// leaves. Some sites (Eightfold, for New York Life) check the box on blur and
// would otherwise still see it as empty.
async function gnFillText(element, value) {
  element.focus();
  setNativeValue(element, value);
  await sleep(150);
  element.dispatchEvent(new Event("blur", { bubbles: true }));
  element.blur();
}

async function gnFill(field, value, files) {
  // Pages that redraw a section (Oracle's address, after Country is set) replace its
  // boxes; the new box with the same name is used.
  if (!field.group[0].isConnected) {
    // Same name, else (ids made up afresh on each redraw, as on SuccessFactors) same question.
    const fresh = await waitFor(() => {
      const now = gnFields(gnForm()).filter((f) => f.group[0].isConnected && f.kind === field.kind);
      return now.find((f) => f.key === field.key) || now.find((f) => f.label === field.label);
    }, 4000);
    if (!fresh) throw new Error("the box went away");
    field.group = fresh.group;
  }
  const [element] = field.group;
  if (field.kind === "listbox" && GN_REANSWER && /^address line 1/i.test(clean(field.label))) {
    await gnFillText(element, String(value));
    element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Tab", keyCode: 9 }));
    await sleep(300);
    return;
  }
  if (field.kind === "listbox") {
    const options = await gnOpenListbox(element);
    const code = GN_STATE_CODES[String(value).toLowerCase()];
    const pick = (list) => matchText(list, String(value), (o) => o.innerText) || (code && matchText(list, code, (o) => o.innerText));
    // An option that is or starts with the answer; "contains" alone is weaker
    // (Oracle's city list reads "Acton, Los Angeles, CA" for Acton).
    const strict = (list) => {
      const want = clean(String(value)).toLowerCase();
      return list.find((o) => clean(o.innerText).toLowerCase() === want) || list.find((o) => clean(o.innerText).toLowerCase().startsWith(want)) || (code && list.find((o) => clean(o.innerText) === code));
    };
    let match = strict(options);
    // Long lists (Oracle, SuccessFactors) only show part of their options until searched.
    if (!match && element.tagName === "INPUT") {
      const found = await gnSearchList(element, String(value).slice(0, 30));
      match = strict(found) || pick(found);
    }
    if (!match) match = pick(options);
    if (!match) {
      await gnCloseListbox(element);
      throw new Error(`no option "${value}"`);
    }
    realClick(match);
    // Most lists close as soon as an option is picked; close the rest firmly.
    if (!(await waitFor(() => !gnListOpen(element), 250))) await gnCloseListbox(element);
    return;
  }
  if (field.kind === "date" || field.kind === "month") return gnFillText(element, gnDateValue(value, field.kind));
  // Many sites never show the file name next to the box, so don't wait long for it.
  if (field.kind === "file") return attachFile(element, fileFor(value, { files }), element.closest("div, label, fieldset") || document.body, 1500);
  if ((field.kind === "text" || field.kind === "number") && element.getAttribute("aria-autocomplete") === "list") return gnAutocomplete(element, value);
  if (field.kind === "text" || field.kind === "textarea" || field.kind === "number") return gnFillText(element, value);
  if (field.kind === "select" && element.tagName === "SELECT") return chooseNativeSelect(element, value);
  const pickIn = (wanted) => {
    const match = matchText(field.group, wanted, (e) => gnOptionText(e, field.group));
    if (!match) throw new Error(`no option "${wanted}"`);
    if (!match.checked) gnClickChoice(match);
  };
  if (field.kind === "select") return pickIn(value);
  if (field.kind === "pills") {
    const match = matchText(field.group, String(value), (b) => gnAriaText(b) || b.innerText);
    if (!match) throw new Error(`no option "${value}"`);
    if (match.getAttribute("aria-checked") !== "true") realClick(match);
    await waitFor(() => match.getAttribute("aria-checked") === "true", 1000);
    return;
  }
  if (field.kind === "checkboxes") return (Array.isArray(value) ? value : [value]).forEach(pickIn);
  if (field.kind === "checkbox") {
    const want = /^(yes|true)$/i.test(String(value));
    if (element.checked !== want) gnClickChoice(element);
  }
}

// Styled choices hide the real radio or checkbox; click what a person would click
// (its label or the visible box around it), then the input itself if that didn't take.
function gnClickChoice(input) {
  const before = input.checked;
  if (input.offsetParent !== null) input.click();
  else realClick(input.labels?.[0] || input.closest("label") || [...gnAncestors(input)].find((e) => e.offsetParent !== null) || input);
  if (input.checked === before) input.click();
}

function* gnAncestors(element) {
  for (let node = element.parentElement, i = 0; node && i < 4; node = node.parentElement, i++) yield node;
}

function gnCheck(field, value, files) {
  const [element] = field.group;
  if (field.kind === "listbox") {
    const shown = clean(element.value || element.innerText || "");
    const wanted = [String(value), GN_STATE_CODES[String(value).toLowerCase()] || ""].filter(Boolean).map((v) => v.toLowerCase().slice(0, 20));
    return wanted.some((w) => shown.toLowerCase().includes(w)) ? null : `page shows "${shown}"`;
  }
  if (field.kind === "date" || field.kind === "month") return element.value ? null : "date did not take";
  if (field.kind === "file") return element.files.length || document.body.innerText.includes(fileFor(value, { files }).name) ? null : "file did not attach";
  if (element.getAttribute("aria-autocomplete") === "list") {
    // Dropdown-style boxes show the choice next to the input; search boxes keep it in the input.
    const control = element.closest("[class*=control]");
    const shown = clean(control ? control.innerText : element.value);
    const first = String(value).split(",")[0].trim().toLowerCase();
    return shown.toLowerCase().includes(first) ? null : `page shows "${shown}"`;
  }
  if (field.kind === "text" || field.kind === "textarea" || field.kind === "number") {
    const digits = (v) => String(v).replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
    // Sites reformat numbers: "213-275-9348" for a phone, "100,000" for a salary.
    const numeric = !/[a-z]/i.test(String(value)) && digits(value).length >= 4;
    const same = clean(element.value) === clean(value) || (numeric && digits(element.value) === digits(value));
    return same ? null : `page shows "${element.value}"`;
  }
  if (field.kind === "select" && element.tagName === "SELECT") {
    const shown = element.selectedOptions[0] ? clean(element.selectedOptions[0].text) : "";
    return shown.toLowerCase().startsWith(String(value).toLowerCase()) ? null : `page shows "${shown}"`;
  }
  if (field.kind === "checkbox") return element.checked === /^(yes|true)$/i.test(String(value)) ? null : "checkbox not set";
  if (field.kind === "pills") return field.group.some((b) => b.getAttribute("aria-checked") === "true") ? null : "nothing selected";
  return field.group.some((e) => e.checked) ? null : "nothing selected";
}

const genericSite = {
  matches: () => gnFields(gnForm()).length > 1 || (typeof atsSfForm === "function" && atsSfForm()),

  async run() {
    if (typeof atsSfExpand === "function") await atsSfExpand();
    const job = await gnJobInfo();
    const problems = [];
    const needYou = [];
    const review = [];
    let filled = 0;
    let kept = 0;
    let warnings = [];
    const done = new Set();
    const known = GN_HOSTS.test(location.hostname);
    // Oracle: its resume import first (it overwrites the form), then the education
    // entries it creates, which usually lack the degree.
    if (typeof atsOracleImport === "function" && ATS_ORACLE && document.querySelector("input[type=file][class*='profile-import']")) {
      const first = await api("/answer", { url: pageJobUrl(), job, fields: [] });
      if (first && first.ok) {
        try {
          await atsOracleImport(first.data.files);
        } catch (error) {
          // Filling now would be undone when the import lands, so stop here.
          const summary = `${error.message}, so nothing else was filled. Fill this application again in a minute.`;
          showPanel(summary, true);
          return { summary, failed: true };
        }
        problems.push(...(await atsOracleFixEntries(first.data.profile || {})));
      }
    } else if (typeof atsOracleFixEntries === "function" && ATS_ORACLE) {
      const first = await api("/answer", { url: pageJobUrl(), job, fields: [] });
      if (first && first.ok) problems.push(...(await atsOracleFixEntries(first.data.profile || {})));
    }
    // Some answers reveal more boxes (iCIMS shows a privacy consent after the
    // California resident box), so look again once after the first pass.
    const given = new Map(); // question -> the answer that went in, for the last check
    let lastFiles = {};
    for (let pass = 0; pass < 2; pass++) {
      const all = gnFields(gnForm()).filter((f) => !done.has(f.key));
      all.forEach((f) => done.add(f.key));
      // Fields you (or the site's resume reader) already filled are left as they are.
      kept += all.filter((f) => f.filled && !gnReanswer(f)).length;
      const fields = all.filter((f) => !f.filled || gnReanswer(f));
      if (GN_REANSWER) fields.sort((a, b) => gnAddressRank(a) - gnAddressRank(b));
      if (!fields.length) break;
      await gnReadListOptions(fields);
      const reply = await api("/answer", { url: pageJobUrl(), job, fields: fields.map(({ group, ...f }) => f) });
      if (!reply || !reply.ok) {
        const summary = `The local server could not prepare this job: ${reply ? reply.data.detail || reply.status : "no reply"}`;
        showPanel(summary, true);
        return { summary, failed: true };
      }
      const answers = Object.fromEntries(reply.data.fields.map((f) => [f.id, f.answer]));
      const { files } = reply.data;
      if (pass === 0) {
        try {
          if ((await atsSfAttachResume(files.resume)) === "attached") filled++;
        } catch (error) {
          problems.push(`Resume: ${error.message}`);
        }
      }
      warnings = reply.data.warnings || [];
      // With a separate country code box, the number box wants the national number only.
      const dialBox = all.some((f) => /country( ?\/ ?region)? code|dial(ing)? code|phone country|phone code/i.test(f.label));
      for (const field of fields) {
        const answer = answers[field.key] || {};
        if (dialBox && field.kind === "text" && /phone|mobile|cell/i.test(field.label) && !/code/i.test(field.label) && answer.value) {
          const digits = String(answer.value).replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
          if (digits.length === 10) answer.value = `${digits.slice(0, 3)}-${digits.slice(3, 6)}-${digits.slice(6)}`;
        } else if (field.kind === "text" && /phone|mobile|cell/i.test(field.label) && gnFlagPicker(field.group[0])) {
          // A phone box with its own country flag picker and no country chosen reads
          // a 10-digit number as an international one; "+1" in front makes it a US one.
          const digits = String(answer.value || "").replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
          if (digits.length === 10) answer.value = `+1${digits}`;
        }
        if (isEmpty(answer.value)) {
          if (field.filled) {
            kept++; // a saved answer with nothing better to put in its place
            continue;
          }
          if (field.required || answer.source === "ask") needYou.push(`${field.label.slice(0, 70)}${answer.note ? ` (${answer.note})` : ""}`);
          continue;
        }
        // A saved answer that already matches is left as it is.
        if (field.filled && !gnCheck(field, answer.value, files)) {
          kept++;
          continue;
        }
        try {
          answer.value = await fillWithRetries((v) => gnFill(field, v, files), field, answer.value, field.options.length ? async () => field.options : null);
          await sleep(60);
          let issue = gnCheck(field, answer.value, files);
          // Number-only salary boxes throw away "Flexible"; use the salary number instead.
          const salary = reply.data.profile?.standing_answers?.salary;
          if (issue && salary && field.kind === "text" && /salary|compensation|pay (range|expectation)/i.test(field.label) && /[a-z]/i.test(String(answer.value))) {
            answer.value = String(salary.number);
            await gnFill(field, answer.value, files);
            await sleep(60);
            issue = gnCheck(field, answer.value, files);
          }
          if (issue) throw new Error(issue);
          filled++;
          given.set(field.label, { kind: field.kind, value: answer.value });
          if (answer.source === "llm") review.push(`${field.label.slice(0, 70)}: "${answer.value}"`);
        } catch (error) {
          // A dropdown whose options depend on an earlier answer (State after Country)
          // gets another try in the second pass, with its options read again.
          if (pass === 0 && /^(select|listbox)$/.test(field.kind) && /no option/.test(error.message)) done.delete(field.key);
          else problems.push(`${field.label.slice(0, 70)}: ${error.message}`);
        }
      }
      lastFiles = files;
      await sleep(250);
    }
    // Some pages redraw a section after a later answer and lose what went in before
    // (SuccessFactors' address after Country). Required boxes that are empty again get
    // their answer once more.
    for (const field of gnFields(gnForm()).filter((f) => f.required && !f.filled && given.has(f.label) && given.get(f.label).kind === f.kind)) {
      const { value } = given.get(field.label);
      try {
        await gnFill(field, value, lastFiles);
        await sleep(60);
        const issue = gnCheck(field, value, lastFiles);
        if (issue) throw new Error(issue);
      } catch (error) {
        problems.push(`${field.label.slice(0, 70)}: ${error.message}`);
      }
    }
    api("/log", { url: pageJobUrl(), company: job.company, title: job.title, event: "filled", problems });
    watchSubmit(job.company, job.title);
    const lines = [...warnings.map((w) => `WARNING: ${w}`), ...(warnings.length ? [""] : []), `Filled ${filled} fields for ${job.title}${job.company ? ` at ${job.company}` : ""}.`];
    if (kept) lines.push(`Left ${kept} field${kept === 1 ? "" : "s"} that already had an answer as they were.`);
    if (!known) lines.push("", "This site's form is new to me, so I filled every field I could recognise. Please check the whole form carefully, including any steps after this one.");
    if (problems.length) lines.push("", "Please fix these by hand:", ...problems.map((p) => `- ${p}`));
    if (needYou.length) lines.push("", "These need your answer:", ...needYou.map((p) => `- ${p}`));
    if (review.length) lines.push("", "Answers the AI wrote, please read them:", ...review.map((p) => `- ${p}`));
    if (!problems.length && !needYou.length) lines.push("", "Everything matched. Please look it over and press Submit yourself.");
    const summary = lines.join("\n");
    showPanel(summary, problems.length || needYou.length || warnings.length);
    // The same report the other sites give, so the queue can judge the fill.
    return { summary, filled, kept, problems, needYou, review, warnings };
  },
};
