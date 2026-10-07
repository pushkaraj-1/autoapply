// Workday: a multi-step application behind a login. Every field sits in a box with
// data-automation-id="formField-<name>"; repeating sections (work experience,
// education, languages, websites) are role=group panels with Delete buttons and
// an "Add Another" button. This adapter fills the current step, presses
// Save and Continue, and repeats until the Review step, where it stops so you
// can submit yourself.

const wdId = (id, root = document) => root.querySelector(`[data-automation-id="${id}"]`);
const wdVisible = (e) => e && e.offsetParent !== null;
const wdKey = (box) => box.dataset.fkitId || box.dataset.automationId;

function wdStep() {
  const step = wdId("progressBarActiveStep");
  return step ? clean(step.innerText).replace(/^current step \d+ of \d+\s*/i, "") : "";
}

function wdLabel(box) {
  const label = box.querySelector("legend, label");
  return label ? clean(label.innerText).replace(/\*$/, "").trim() : "";
}

function wdRequired(box) {
  return Boolean(box.querySelector("abbr")) || Boolean(box.querySelector('[aria-required="true"]'));
}

function wdKind(box) {
  if (box.querySelector('[data-automation-id="dateInputWrapper"], [data-automation-id="dateSectionYear-input"]')) return "date";
  if (box.querySelector("button[aria-haspopup=listbox]")) return "select";
  if (box.querySelector('[data-automation-id="multiSelectContainer"]')) return "prompt";
  if (box.querySelector("input[type=radio]")) return "radio";
  if (box.querySelector("input[type=file]")) return "file";
  if (box.querySelector("input[type=checkbox]")) return "checkbox";
  if (box.querySelector("textarea")) return "textarea";
  if (box.querySelector("input")) return "text";
  return "unknown";
}

function wdBoxes(root = document) {
  return [...root.querySelectorAll('[data-automation-id^="formField-"]')].filter(wdVisible);
}

function wdErrors() {
  return wdBoxes()
    .filter((box) => /Error:/.test(box.innerText))
    .map((box) => `${wdLabel(box)}: ${clean(box.innerText.split("Error:")[1]).slice(0, 100)}`);
}

// ---- widgets ----

function wdSetText(box, value) {
  const input = box.querySelector("textarea, input:not([type=checkbox]):not([type=radio]):not([type=file])");
  if (!input) throw new Error("text box not found");
  fillText(input, value);
}

function wdPopupOptions() {
  return [...document.querySelectorAll("[role=option]")].filter(
    (o) => wdVisible(o) && !o.closest('[data-automation-id="selectedItemList"]') && clean(o.innerText) !== "Select One",
  );
}

async function wdOpenSelect(box) {
  const button = box.querySelector("button[aria-haspopup=listbox]");
  realClick(button);
  await waitFor(() => wdPopupOptions().length, 3000);
  return button;
}

function wdCloseSelect(button) {
  button.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape", keyCode: 27 }));
  document.body.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape", keyCode: 27 }));
}

async function wdSelectOptions(box) {
  const button = await wdOpenSelect(box);
  const options = wdPopupOptions().map((o) => clean(o.innerText));
  wdCloseSelect(button);
  await sleep(300);
  return options;
}

async function wdChoose(box, wanted) {
  const button = box.querySelector("button[aria-haspopup=listbox]");
  const choices = Array.isArray(wanted) ? wanted : [wanted];
  if (choices.some((w) => clean(button.innerText).toLowerCase() === w.toLowerCase())) return;
  await wdOpenSelect(box);
  const option = choices.map((w) => matchText(wdPopupOptions(), w)).find(Boolean);
  if (!option) {
    const seen = wdPopupOptions().map((o) => clean(o.innerText)).slice(0, 8);
    wdCloseSelect(button);
    throw new Error(`no option "${choices[0]}" (saw: ${seen.join(" / ") || "nothing"})`);
  }
  realClick(option);
  await sleep(500);
}

// Search-style pickers ("How Did You Hear About Us?", phone code). Options can be
// grouped in categories, so look at the top level first, then inside each category.
function wdPromptOptions() {
  return [...document.querySelectorAll('[data-automation-id="promptOption"]')].filter(
    (o) => wdVisible(o) && !o.closest('[data-automation-id="selectedItemList"]'),
  );
}

function wdPromptSelected(box) {
  return [...box.querySelectorAll('[data-automation-id="selectedItemList"] [role=option], [data-automation-id="selectedItem"]')].map((e) => clean(e.innerText));
}

async function wdPrompt(box, wanted) {
  const choices = (Array.isArray(wanted) ? wanted : [wanted]).filter(Boolean);
  const selected = wdPromptSelected(box);
  if (choices.some((w) => selected.some((s) => s.toLowerCase().startsWith(w.toLowerCase())))) return;
  const input = box.querySelector("input");
  const open = async () => {
    if (wdPromptOptions().length) return; // clicking again would close it
    input.focus();
    realClick(input);
    await waitFor(() => wdPromptOptions().length, 3000);
  };
  await open();
  const isCategory = (o) => Boolean(o.closest('[data-uxi-multiselectlistitem-hassidecharm="true"]'));
  for (const want of choices) {
    const direct = matchText(wdPromptOptions().filter((o) => !isCategory(o)), want);
    if (direct) {
      realClick(direct);
      await sleep(800);
      return;
    }
  }
  // Look inside likely categories first ("Job Boards" before "Agency").
  const likely = (name) => (/job board|career|website|online|social|internet|web/i.test(name) ? 0 : /other/i.test(name) ? 2 : 1);
  const categories = wdPromptOptions()
    .filter(isCategory)
    .map((o) => clean(o.innerText))
    .sort((a, b) => likely(a) - likely(b));
  for (const category of categories) {
    if (!wdPromptOptions().length) await open();
    const node = wdPromptOptions().find((o) => clean(o.innerText) === category);
    if (!node) continue;
    realClick(node);
    await sleep(1200);
    for (const want of choices) {
      const inside = matchText(wdPromptOptions().filter((o) => !isCategory(o)), want);
      if (inside) {
        realClick(inside);
        await sleep(800);
        return;
      }
    }
    const back = [...document.querySelectorAll('[data-automation-id="backButton"]')].find(wdVisible);
    if (back) {
      realClick(back);
      await sleep(800);
    } else {
      input.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape", keyCode: 27 }));
      await sleep(600);
      await open();
    }
  }
  input.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape", keyCode: 27 }));
  throw new Error(`could not find "${choices[0]}" in the list`);
}

// Types into a search box the way a keyboard does, then presses Enter to search.
// Workday only searches on input it sees as typed; a value set from script finds nothing.
function wdType(input, text) {
  input.focus();
  input.select();
  if (!document.execCommand("insertText", false, text) || input.value !== text) setNativeValue(input, text);
  const enter = { bubbles: true, cancelable: true, key: "Enter", code: "Enter", keyCode: 13, which: 13 };
  for (const type of ["keydown", "keypress", "keyup"]) input.dispatchEvent(new KeyboardEvent(type, enter));
}

// Searches and waits until the new results stop changing, then returns the option
// find() picks from them, or null. When a search has a single result, Workday picks
// it by itself on Enter and closes the list; stop() sees that and ends the wait.
async function wdSearch(input, term, find, stop = () => false) {
  const shown = () => wdPromptOptions().map((o) => o.innerText).join("|");
  const before = shown();
  wdType(input, term);
  let last = null;
  let steady = 0;
  for (let i = 0; i < 24 && steady < 3; i++) {
    await sleep(200);
    const hit = find();
    if (hit || stop()) return hit || null;
    const now = shown();
    // Count only once the list shows this search's results (or after 2.5 s).
    steady = now && now === last && (now !== before || i > 12) ? steady + 1 : 0;
    last = now;
  }
  return find();
}

// Removes an item Workday picked by itself that isn't the one we wanted.
async function wdUnpick(box, text) {
  const item = [...box.querySelectorAll('[data-automation-id="selectedItem"]')].find((e) => clean(e.innerText) === text);
  const remove = item && (item.querySelector('[data-automation-id="DELETE_charm"], [aria-label*="delete" i], [aria-label*="remove" i]'));
  if (!remove) return false;
  realClick(remove);
  return Boolean(await waitFor(() => !wdPromptSelected(box).includes(text), 1500));
}

function wdClearSearch(input) {
  input.focus();
  input.select();
  if (!document.execCommand("delete") || input.value) setNativeValue(input, "");
}

// Search boxes with long lists (schools, majors): type the search words, wait for
// the results, and pick the first of the names they show, matched exactly (see
// nameKey). With no search words, each name is searched in turn. A nearby but
// different name is never picked; that is reported instead.
async function wdSearchPick(box, search, names) {
  if (wdPromptSelected(box).some((s) => names.some((n) => nameKey(n) === nameKey(s)))) return;
  const input = box.querySelector("input");
  if (!input) throw new Error("no search box");
  const find = () => {
    for (const name of names) {
      const hit = wdPromptOptions().find((o) => nameKey(o.innerText) === nameKey(name));
      if (hit) return hit;
    }
    return null;
  };
  const wanted = () => wdPromptSelected(box).some((s) => names.some((n) => nameKey(n) === nameKey(s)));
  for (const term of search ? [search] : names) {
    const before = wdPromptSelected(box);
    const option = await wdSearch(input, term, find, () => wdPromptSelected(box).length !== before.length);
    if (option) {
      realClick(option);
      await waitFor(wanted, 1500);
    }
    if (wanted()) {
      await wdLeave(input); // Workday keeps the pick once focus leaves the box
      return;
    }
    // Workday picked a single, different result by itself: take it out again.
    for (const other of wdPromptSelected(box).filter((s) => !before.includes(s))) {
      if (!(await wdUnpick(box, other))) throw new Error(`Workday picked "${other}" by itself, which isn't one of "${names.join('", "')}"`);
    }
  }
  const seen = wdPromptOptions().slice(0, 5).map((o) => clean(o.innerText));
  wdClearSearch(input);
  await wdLeave(input);
  throw new Error(`none of "${names.join('", "')}" is in the list${search ? ` (searched "${search}")` : ""} (saw: ${seen.join(" / ") || "nothing"})`);
}

// Leaves a search box the way you would by clicking elsewhere on the page. Workday
// only keeps a picked item once focus has left the box.
async function wdLeave(input) {
  input.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape", keyCode: 27 }));
  input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
  input.blur();
  const away = wdId("pageHeaderTitleText") || document.querySelector("h2, h3") || document.body;
  realClick(away);
  await sleep(300);
}

// Workday's Skills box: search each skill and add it when the list has it under the
// same name ("Python" also matches "Python (Programming Language)"), then click away
// so it stays.
async function wdFillSkills(box, skills) {
  const input = () => box.querySelector("input");
  if (!input()) return [];
  const norm = (text) => text.toLowerCase().replace(/\s*\(.*\)$/, "").replace(/[^a-z0-9+#]/g, "");
  const chosen = () => wdPromptSelected(box).map(norm);
  const missing = [];
  for (const skill of skills) {
    if (chosen().includes(norm(skill))) continue;
    const box_ = input();
    const before = wdPromptSelected(box);
    const option = await wdSearch(box_, skill, () => wdPromptOptions().find((o) => norm(clean(o.innerText)) === norm(skill)) || null, () => wdPromptSelected(box).length !== before.length);
    if (option) {
      realClick(option);
      await waitFor(() => chosen().includes(norm(skill)), 1500);
    }
    // A different skill Workday picked by itself on Enter is taken out again.
    for (const other of wdPromptSelected(box).filter((s) => !before.includes(s) && norm(s) !== norm(skill))) await wdUnpick(box, other);
    if (!option && !chosen().includes(norm(skill))) wdClearSearch(box_);
    await wdLeave(box_);
    if (!chosen().includes(norm(skill))) missing.push(skill);
  }
  return missing;
}

function wdChooseRadio(box, wanted) {
  const choices = [...box.querySelectorAll("input[type=radio]")].map((input) => ({
    input,
    label: box.querySelector(`label[for="${input.id}"]`) || input.parentElement,
  }));
  const choice = matchText(choices, wanted, (c) => c.label.innerText);
  if (!choice) throw new Error(`no option "${wanted}"`);
  if (!choice.input.checked) choice.input.click();
}

function wdSetCheckbox(box, checked) {
  const input = box.querySelector("input[type=checkbox]");
  if (input && input.checked !== checked) input.click();
}

const WD_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

// Month/year dates must be chosen in Workday's own picker, or Workday keeps the
// old value behind the scenes. Picking a neighbouring month first and then the
// right one makes it register even when the right month already shows.
async function wdPickMonth(box, year, month) {
  for (const target of [month === 1 ? 2 : month - 1, month]) {
    if (!wdId("monthPickerSpinner")) {
      realClick(wdId("dateIcon", box));
      if (!(await waitFor(() => wdId("monthPickerSpinner"), 3000))) throw new Error("date picker did not open");
    }
    for (let i = 0; i < 60; i++) {
      const shown = Number(wdId("monthPickerSpinnerLabel").innerText);
      if (shown === year) break;
      realClick(wdId(shown > year ? "monthPickerLeftSpinner" : "monthPickerRightSpinner"));
      await sleep(200);
    }
    const tile = [...document.querySelectorAll('[data-automation-id="monthPickerTileLabel"]')].find((t) =>
      (t.getAttribute("aria-label") || "").endsWith(`${WD_MONTHS[target - 1]} ${year}`),
    );
    if (!tile) throw new Error(`no ${WD_MONTHS[target - 1]} ${year} in the date picker`);
    realClick(tile);
    await sleep(600);
  }
}

// "2026-05" fills month and year; a year-only box gets the year.
async function wdSetDate(box, value) {
  const [year, month, day] = String(value).split("-");
  const part = (name) => box.querySelector(`[data-automation-id="dateSection${name}-input"]`);
  if (part("Month") && month && !part("Day") && wdId("dateIcon", box)) {
    await wdPickMonth(box, Number(year), Number(month));
    return;
  }
  if (part("Month") && month) fillText(part("Month"), String(Number(month)));
  if (part("Day") && day) fillText(part("Day"), String(Number(day)));
  if (part("Year")) fillText(part("Year"), year);
}

async function wdFill(box, kind, value) {
  if (kind === "text" || kind === "textarea") wdSetText(box, value);
  else if (kind === "select") await wdChoose(box, value);
  else if (kind === "prompt") await wdPrompt(box, value);
  else if (kind === "radio") wdChooseRadio(box, value);
  else if (kind === "checkbox") wdSetCheckbox(box, /^(yes|true)$/i.test(String(value)));
  else if (kind === "date") await wdSetDate(box, value);
  else throw new Error(`don't know how to fill a "${kind}" field`);
  await sleep(150);
}

// ---- repeating sections ----

// Section groups are named after their headings, which differ between companies:
// "Work-Experience-section" on one site, "Work-History-(Optional)-section" on another.
// Their entries are "<heading>-<n>-panel" groups inside them.
const WD_SECTIONS = {
  work: /^work-(experience|history)\b/i,
  education: /^education\b/i,
  languages: /^languages\b/i,
  websites: /^websites\b/i,
  skills: /skills/i,
  resume: /^resume/i,
};

// The section's group, or null when this page doesn't have it.
function wdSection(name) {
  return [...document.querySelectorAll('[role=group][aria-labelledby$="-section"]')].find((g) => WD_SECTIONS[name].test(g.getAttribute("aria-labelledby"))) || null;
}

function wdPanels(section) {
  const prefix = section.getAttribute("aria-labelledby").replace(/-section$/, "");
  return [...section.querySelectorAll('[role=group][aria-labelledby$="-panel"]')].filter((p) => p.getAttribute("aria-labelledby").startsWith(`${prefix}-`));
}

async function wdSyncPanels(name, count) {
  const section = wdSection(name);
  if (!section) throw new Error("the section is no longer on the page (Workday may have reloaded it)");
  for (let guard = 0; guard < 12 && wdPanels(section).length > count; guard++) {
    const panels = wdPanels(section);
    const remove = [...panels[panels.length - 1].querySelectorAll("button")].find((b) => clean(b.innerText) === "Delete");
    if (!remove) break;
    realClick(remove);
    await waitFor(() => wdPanels(section).length < panels.length, 3000);
  }
  for (let guard = 0; guard < 12 && wdPanels(section).length < count; guard++) {
    const before = wdPanels(section).length;
    // "Add" for the first entry, "Add Another" after that.
    const add = [...section.querySelectorAll('[data-automation-id="add-button"]')].filter(wdVisible).pop();
    if (!add) throw new Error("no Add button");
    add.click();
    if (!(await waitFor(() => wdPanels(section).length > before, 4000))) throw new Error("a new entry did not appear after Add");
    await sleep(300);
  }
  return wdPanels(section);
}

const inPanel = (panel, name) => wdId(`formField-${name}`, panel);

async function wdFillExperience(jobs) {
  const panels = await wdSyncPanels("work", jobs.length);
  for (const [i, job] of jobs.entries()) {
    const panel = panels[i];
    wdSetText(inPanel(panel, "jobTitle"), job.title);
    wdSetText(inPanel(panel, "companyName"), job.company);
    if (inPanel(panel, "location")) wdSetText(inPanel(panel, "location"), job.location);
    if (inPanel(panel, "currentlyWorkHere")) wdSetCheckbox(inPanel(panel, "currentlyWorkHere"), !job.end);
    await sleep(300);
    await wdSetDate(inPanel(panel, "startDate"), job.start);
    if (job.end && inPanel(panel, "endDate")) await wdSetDate(inPanel(panel, "endDate"), job.end);
    if (inPanel(panel, "roleDescription")) wdSetText(inPanel(panel, "roleDescription"), job.description);
  }
}

// Newer Workday pages (Salesforce's, for one) have no Work-Experience-section group.
// Their history boxes are named "workExperience-<n>--<field>", so panels are counted
// by those names and the server fills each panel from the profile in order.
function wdPanelNumbers(prefix) {
  const numbers = [];
  for (const box of document.querySelectorAll(`[data-fkit-id^="${prefix}-"]`)) {
    const match = box.dataset.fkitId.match(new RegExp(`^${prefix}-(\\d+)--`));
    if (match && !numbers.includes(match[1])) numbers.push(match[1]);
  }
  return numbers;
}

// The section's Add button: the nearest one around its panels, or around its
// heading when there are no panels yet.
function wdSectionAddButton(prefix, heading) {
  const anchor =
    document.querySelector(`[data-fkit-id^="${prefix}-"]`) ||
    [...document.querySelectorAll("h2, h3, h4, legend, [role=heading]")].find((h) => heading.test(clean(h.innerText)));
  for (let node = anchor; node && node !== document.body; node = node.parentElement) {
    const button = [...node.querySelectorAll("button")].find((b) => /^add( another)?$/i.test(clean(b.innerText || b.getAttribute("aria-label") || "")));
    if (button) return button;
  }
  return null;
}

async function wdEnsurePanels(prefix, heading, count) {
  for (let guard = 0; guard < 12 && wdPanelNumbers(prefix).length < count; guard++) {
    const before = wdPanelNumbers(prefix).length;
    const add = wdSectionAddButton(prefix, heading);
    if (!add) throw new Error("could not find its Add button");
    realClick(add);
    if (!(await waitFor(() => wdPanelNumbers(prefix).length > before, 3000))) throw new Error("a new entry did not appear after Add");
  }
}

// Ways degree lists name a degree, best first. Some lists spell it out
// ("Master's Degree"), others only abbreviate ("MS").
const WD_DEGREE_NAMES = {
  master: ["Master's Degree", "Masters Degree", "Master's", "Masters", "Master", "Master of Science", "Master of Science (MS)", "MS", "M.S.", "MSc", "M.Sc."],
  bachelor: ["Bachelor's Degree", "Bachelors Degree", "Bachelor's", "Bachelors", "Bachelor", "Bachelor of Engineering", "BE", "B.E.", "BEng", "B.Tech", "Bachelor of Science", "Bachelor of Science (BS)", "BS", "B.S.", "BSc", "B.Sc."],
};

// Picks the degree only by an exact name, so "Master" never lands on an MBA.
async function wdChooseDegree(box, school) {
  const level = /bachelor/i.test(school.degree_level) ? "bachelor" : /master/i.test(school.degree_level) ? "master" : null;
  const names = [school.degree_level, school.degree, ...(WD_DEGREE_NAMES[level] || [])].filter(Boolean);
  const options = await wdSelectOptions(box);
  const pick = names.map((n) => options.find((o) => nameKey(o) === nameKey(n))).find(Boolean);
  if (!pick) throw new Error(`no option for "${school.degree_level}" (saw: ${options.slice(0, 10).join(" / ")})`);
  await wdChoose(box, pick);
}

async function wdFillEducation(schools) {
  const panels = await wdSyncPanels("education", schools.length);
  const problems = [];
  for (const [i, school] of schools.entries()) {
    const panel = panels[i];
    // One field that fails doesn't stop the others.
    const step = async (name, fill) => {
      try {
        await fill();
      } catch (error) {
        problems.push(`${school.school}, ${name}: ${error.message}`);
      }
    };
    const schoolBox = inPanel(panel, "schoolName") || inPanel(panel, "school");
    if (schoolBox && wdKind(schoolBox) === "prompt") await step("school", () => wdSearchPick(schoolBox, school.search || school.school, school.names || [school.school]));
    else if (schoolBox) await step("school", () => wdFill(schoolBox, wdKind(schoolBox), school.school));
    if (inPanel(panel, "degree")) await step("degree", () => wdChooseDegree(inPanel(panel, "degree"), school));
    if (inPanel(panel, "fieldOfStudy")) await step("field of study", () => wdSearchPick(inPanel(panel, "fieldOfStudy"), null, school.majors || [school.discipline]));
    if (inPanel(panel, "gradeAverage")) await step("GPA", () => wdSetText(inPanel(panel, "gradeAverage"), String(school.gpa).split("/")[0].trim()));
    if (inPanel(panel, "firstYearAttended")) await step("start year", () => wdSetDate(inPanel(panel, "firstYearAttended"), String(school.start).slice(0, 4)));
    if (inPanel(panel, "lastYearAttended")) await step("end year", () => wdSetDate(inPanel(panel, "lastYearAttended"), String(school.end).slice(0, 4)));
  }
  if (problems.length) throw new Error(problems.join("; "));
}

async function wdFillLanguages(languages) {
  const panels = await wdSyncPanels("languages", languages.length);
  for (const [i, language] of languages.entries()) {
    const panel = panels[i];
    if (inPanel(panel, "language")) await wdChoose(inPanel(panel, "language"), language.name);
    if (inPanel(panel, "native")) wdSetCheckbox(inPanel(panel, "native"), Boolean(language.fluent));
    // Reading / Speaking / Writing levels: pick the most fluent option.
    for (const box of wdBoxes(panel).filter((b) => wdKind(b) === "select" && b !== inPanel(panel, "language"))) {
      const options = await wdSelectOptions(box);
      const best = options.find((o) => /fluent|native|advanced|expert/i.test(o)) || options[options.length - 1];
      if (best) await wdChoose(box, best);
    }
  }
}

async function wdFillWebsites(links) {
  const urls = [links.linkedin, links.github, links.website].filter(Boolean);
  const panels = await wdSyncPanels("websites", urls.length);
  urls.forEach((url, i) => inPanel(panels[i], "url") && wdSetText(inPanel(panels[i], "url"), url));
}

async function wdUploadResume(root, files) {
  if (wdId("delete-file", root)) return; // already attached
  const input = wdId("file-upload-input-ref", root);
  if (!input) return;
  await attachFile(input, files.resume, root);
  await waitFor(() => /success/i.test(root.innerText) || wdId("delete-file", root), 10000);
}

// ---- one step ----

// Boxes inside these sections are filled by the section's own code, not asked about.
function wdInHandledSection(box) {
  const group = box.closest("[role=group][aria-labelledby]");
  return Boolean(group) && Object.values(WD_SECTIONS).some((s) => s.test(group.getAttribute("aria-labelledby")));
}

function wdKnownAnswer(box, profile) {
  const id = box.dataset.automationId;
  const digits = profile.contact.phone.replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
  const known = {
    "formField-legalName--firstName": profile.name.first,
    "formField-legalName--lastName": profile.name.last,
    "formField-addressLine1": profile.contact.address_line1,
    "formField-postalCode": profile.contact.postal_code,
    "formField-city": profile.contact.city,
    "formField-countryRegion": profile.contact.state,
    "formField-country": ["United States of America", "United States"],
    "formField-phoneType": "Mobile",
    "formField-countryPhoneCode": ["United States of America (+1)", "United States"],
    "formField-phoneNumber": digits,
    "formField-email": profile.contact.email,
    "formField-linkedInAccount": profile.links.linkedin,
    "formField-name": `${profile.name.first} ${profile.name.last}`,
    "formField-dateSignedOn": new Date().toISOString().slice(0, 10),
  };
  if (id in known) return known[id];
  if (wdKind(box) === "checkbox" && /consent|agree|acknowledge|certify|terms/i.test(wdLabel(box))) return "Yes";
  return undefined;
}

// Optional fields we have no answer for are left alone.
const WD_SKIP = /^formField-(preferredCheck|addressLine2|regionSubdivision\d|extension|legalName--middleName|skills)$/;

// Reads the boxes, asks the server about the ones the page can't answer itself,
// and fills them. Returns the keys it asked about.
async function wdAskAndFill(boxes, files, profile, problems, needYou, review) {
  const fields = [];
  for (const box of boxes) {
    const kind = wdKind(box);
    const known = wdKnownAnswer(box, profile);
    if (known !== undefined) {
      fields.push({ key: wdKey(box), label: wdLabel(box), kind, known, required: wdRequired(box), box });
      continue;
    }
    const options = kind === "select" ? await wdSelectOptions(box) : kind === "radio" ? [...box.querySelectorAll("label[for]")].map((l) => clean(l.innerText)) : kind === "checkbox" ? ["Yes", "No"] : [];
    fields.push({ key: wdKey(box), label: wdLabel(box), kind: kind === "radio" || kind === "select" ? "select" : kind, options, required: wdRequired(box), box });
  }

  const unknown = fields.filter((f) => f.known === undefined).map(({ box, known, ...f }) => f);
  const reply = await api("/answer", { url: location.href, fields: unknown });
  if (!reply || !reply.ok) throw new Error(`the local server could not answer: ${reply ? reply.data.detail || reply.status : "no reply"}`);
  Object.assign(files, reply.data.files);
  const answers = Object.fromEntries(reply.data.fields.map((f) => [f.id, f.answer]));

  for (const field of fields) {
    const answer = field.known !== undefined ? { value: field.known, source: "profile" } : answers[field.key] || {};
    const value = answer.value;
    if (isEmpty(value)) {
      if (field.required || answer.source === "ask") needYou.push(`${field.label.slice(0, 70)}${answer.note ? ` (${answer.note})` : ""}`);
      continue;
    }
    try {
      const kind = wdKind(field.box);
      // A school box (newer layout) is searched with the school's own search words.
      const school = kind === "prompt" && /^education-\d+--school/i.test(field.key) && (profile.education || []).find((s) => (s.names || [s.school]).includes(value));
      // A major box is searched for each of the school's majors in turn.
      const major = kind === "prompt" && /^education-\d+--fieldOfStudy/i.test(field.key) && (profile.education || []).find((s) => s.discipline === value);
      if (school) await wdSearchPick(field.box, school.search || school.school, school.names || [school.school]);
      else if (major) await wdSearchPick(field.box, null, major.majors || [major.discipline]);
      else await wdFill(field.box, kind, kind === "prompt" && /how did you hear/i.test(field.label) ? [value, ...profile.standing_answers.job_source, "Other"] : value);
      if (answer.source === "llm") review.push(`${field.label.slice(0, 70)}: "${value}"`);
    } catch (error) {
      problems.push(`${field.label.slice(0, 70)}: ${error.message}`);
    }
  }
  return fields.map((f) => f.key);
}

async function wdFillStep(files, profile) {
  const step = wdStep();
  const problems = [];
  const needYou = [];
  const review = [];

  if (/autofill with resume|quick apply|upload/i.test(step)) {
    await wdUploadResume(document.body, files);
    return { step, problems, needYou, review };
  }

  const isSkills = (b) => /(^|-)skills$/i.test(wdKey(b) || "");
  const usable = (b) => !WD_SKIP.test(b.dataset.automationId) && !wdInHandledSection(b) && !isSkills(b) && wdKind(b) !== "unknown";

  // My Experience: each section in the order the page shows them, top to bottom,
  // then whatever other boxes the step has.
  if (/experience/i.test(step) && wdSection("work")) {
    const fills = {
      work: ["work experience", () => wdFillExperience(profile.experience)],
      education: ["education", () => wdFillEducation(profile.education)],
      languages: ["languages", () => wdFillLanguages(profile.languages)],
      websites: ["websites", () => wdFillWebsites(profile.links)],
      skills: ["skills", () => wdFillSkillsSection(profile, problems)],
      resume: ["resume", () => wdSection("resume") && wdUploadResume(wdSection("resume"), files)],
    };
    const present = Object.keys(fills)
      .filter((name) => wdSection(name))
      .sort((a, b) => (wdSection(a).compareDocumentPosition(wdSection(b)) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
    for (const name of present) {
      const [label, fill] = fills[name];
      showPanel(`Filling "${step}": ${label}...`, false);
      try {
        await fill();
      } catch (error) {
        problems.push(`${label[0].toUpperCase()}${label.slice(1)}: ${error.message}`);
      }
    }
    await wdAskAndFill(wdBoxes().filter(usable), files, profile, problems, needYou, review);
    return { step, problems, needYou, review };
  }

  // Newer layout, with no section groups: one panel per job and school in the
  // profile, before the boxes are read.
  const newLayout = /experience/i.test(step);
  if (newLayout) {
    for (const [prefix, heading, count, name] of [
      ["workExperience", /^work experience$/i, profile.experience.length, "work experience"],
      ["education", /^education$/i, profile.education.length, "education"],
    ]) {
      try {
        await wdEnsurePanels(prefix, heading, count);
      } catch (error) {
        problems.push(`${name}: ${error.message}`);
      }
    }
  }

  const asked = new Set(await wdAskAndFill(wdBoxes().filter(usable), files, profile, problems, needYou, review));
  if (newLayout) {
    // Ticking or unticking "I currently work here" shows or hides the end date, so
    // history boxes that appeared while filling get a second pass.
    const later = wdBoxes().filter((b) => usable(b) && /^(workExperience|education)-\d+--/.test(wdKey(b) || "") && !asked.has(wdKey(b)));
    if (later.length) await wdAskAndFill(later, files, profile, problems, needYou, review);
    try {
      await wdFillSkillsSection(profile, problems);
    } catch (error) {
      problems.push(`Skills: ${error.message}`);
    }
    const resumeSection = wdSection("resume");
    if (resumeSection) await wdUploadResume(resumeSection, files);
  }
  return { step, problems, needYou, review };
}

// Skills from the resume, one search at a time.
async function wdFillSkillsSection(profile, problems) {
  const skillsBox = wdBoxes().find((b) => /(^|-)skills$/i.test(wdKey(b) || "") && wdKind(b) === "prompt");
  if (!skillsBox || !(profile.skills || []).length) return;
  const missing = await wdFillSkills(skillsBox, profile.skills);
  if (missing.length === profile.skills.length) problems.push("Skills: none of your skills were found in this site's list");
}

// A new step renders in stages (autofill data arrives a moment later), so wait
// until its fields exist and their count has held steady for a second.
async function wdWaitForForm() {
  let last = -1;
  let steady = 0;
  for (let i = 0; i < 30 && steady < 2; i++) {
    const count = wdBoxes().length + (wdId("file-upload-input-ref") ? 1 : 0);
    steady = count > 0 && count === last ? steady + 1 : 0;
    last = count;
    await sleep(500);
  }
}

async function wdNext(step) {
  const next = wdId("pageFooterNextButton");
  if (!next) throw new Error("no Save and Continue button");
  next.click();
  const moved = await waitFor(() => (wdStep() && wdStep() !== step) || wdErrors().length || wdId("errorBanner"), 20000);
  await sleep(1500);
  return Boolean(moved) && wdStep() !== step;
}

// ---- sign in / create account ----
// The email and password come from WORKDAY_EMAIL and WORKDAY_PASSWORD in .env via
// the local server; they are never stored in the extension. The hidden
// "beecatcher" box is a bot trap and is left alone.

const WD_HOSTS = /myworkdayjobs\.com$|myworkday\.com$/;

function wdLoginForm() {
  const password = wdId("password");
  if (!wdVisible(password)) return null;
  const verify = wdId("verifyPassword");
  return { email: wdId("email"), password, verify: wdVisible(verify) ? verify : null };
}

function wdLoginErrors() {
  return [...document.querySelectorAll('[role=alert], [data-automation-id*="rror"]')]
    .filter(wdVisible)
    .map((e) => clean(e.innerText))
    .filter(Boolean);
}

async function wdFillLogin(form) {
  const reply = await api("/login", { url: location.href });
  if (!reply || !reply.ok) throw new Error(reply ? reply.data.detail || `server said ${reply.status}` : "the local server did not reply");
  const { email, password } = reply.data;
  if (form.email) fillText(form.email, email);
  fillText(form.password, password);
  if (form.verify) fillText(form.verify, password);
  const agree = wdId("createAccountCheckbox");
  if (wdVisible(agree) && !agree.checked) agree.click();
  await sleep(300);
}

// Fills the boxes as soon as a sign-in or create-account form shows up, without
// pressing anything. Each form is filled once, so your own typing is kept.
const wdAutoFilled = new WeakSet();
if (WD_HOSTS.test(location.hostname)) {
  setInterval(() => {
    const form = wdLoginForm();
    if (!form || wdAutoFilled.has(form.password) || form.password.value) return;
    wdAutoFilled.add(form.password);
    wdFillLogin(form).catch((error) => showPanel(`Could not fill the Workday sign-in: ${error.message}`, true));
  }, 1000);
}

// Fills the open sign-in or create-account form and presses its button. Returns
// null once Workday lets us in, or the errors it shows.
async function wdSubmitLogin(creating) {
  const form = wdLoginForm();
  showPanel(creating ? "Creating your Workday account..." : "Signing in to Workday...", false);
  await wdFillLogin(form);
  const submit = wdId(creating ? "createAccountSubmitButton" : "signInSubmitButton");
  // Workday lays a click_filter box over its submit buttons; clicking that is what works.
  const target = (submit && submit.parentElement && wdId("click_filter", submit.parentElement)) || submit;
  if (!target) throw new Error(`no ${creating ? "Create Account" : "Sign In"} button found`);
  realClick(target);
  await sleep(1000); // errors from an earlier try can still be showing
  const done = await waitFor(() => !wdLoginForm() || wdLoginErrors().length, 20000);
  return done && !wdLoginForm() ? null : wdLoginErrors();
}

// Fill button on a sign-in page: open the email form if needed, fill it, press
// Sign In (or Create Account), and wait until Workday lets us in.
async function wdSignIn() {
  const emailButton = wdId("SignInWithEmailButton");
  const onSignInPage = wdVisible(emailButton) || /sign in|create account/i.test(wdStep());
  // The step bar loads before the sign-in buttons do.
  if (onSignInPage) await waitFor(() => wdLoginForm() || wdVisible(wdId("SignInWithEmailButton")), 15000);
  for (let tries = 0; tries < 3 && !wdLoginForm() && wdVisible(wdId("SignInWithEmailButton")); tries++) {
    wdId("SignInWithEmailButton").click();
    await waitFor(wdLoginForm, 3000);
  }
  if (!wdLoginForm()) {
    if (onSignInPage) throw new Error('The Workday sign-in form did not open. Click "Sign in with email", then click Fill this application again.');
    return;
  }
  await sleep(500);
  // Some companies (Salesforce) open on Create Account with a small Sign In link.
  // Signing in comes first; an account is only created when there isn't one yet.
  // The Sign In link shows up a moment after the Create Account form does.
  if (wdLoginForm().verify) await waitFor(() => wdVisible(wdId("signInLink")), 3000);
  // Like "Sign in with email", this link ignores synthetic pointer events; a plain click works.
  for (let tries = 0; tries < 3 && wdLoginForm() && wdLoginForm().verify && wdVisible(wdId("signInLink")); tries++) {
    wdId("signInLink").click();
    if (await waitFor(() => wdLoginForm() && !wdLoginForm().verify, 3000)) await sleep(1500); // let the new form settle
  }
  let errors = await wdSubmitLogin(Boolean(wdLoginForm().verify));
  if (errors && !wdLoginForm().verify) {
    const createLink = wdId("createAccountLink");
    if (wdVisible(createLink)) {
      const signInErrors = errors;
      createLink.click();
      if (await waitFor(() => wdLoginForm() && wdLoginForm().verify, 4000)) {
        errors = await wdSubmitLogin(true);
        if (errors) errors = [...signInErrors, ...errors];
      }
    }
  }
  if (errors) {
    const lines = ["Workday did not sign you in."];
    if (errors.length) lines.push("", ...errors.map((e) => `- ${e}`));
    lines.push("", "Check WORKDAY_EMAIL and WORKDAY_PASSWORD (or EMAIL and PASSWORD) in the .env file, or sign in yourself and click Fill this application again.");
    throw new Error(lines.join("\n"));
  }
  // Some jobs ask how to start after signing in; take the plain form.
  const manual = await waitFor(() => wdVisible(wdId("applyManually")) && wdId("applyManually"), 4000);
  if (manual) realClick(manual);
  await waitFor(() => (wdStep() && !/sign in|create account/i.test(wdStep())) || wdErrorPage(), 15000);
}

// Right after signing in, Workday sometimes shows "Something went wrong. Please
// refresh the page and then try again." A reload fixes it.
// The step bar can still show above the message, but the form and its buttons don't.
const wdErrorPage = () => !wdId("pageFooterNextButton") && /something went wrong/i.test(document.body.innerText) && /refresh the page/i.test(document.body.innerText);

// Reloads the page and lets content.js carry on filling once it loads (see
// continueAfterReload). Returns false when it already reloaded a moment ago.
function wdReloadAndContinue() {
  try {
    if (Date.now() - Number(sessionStorage.getItem("ja-wd-reloaded") || 0) < 60000) return false;
    sessionStorage.setItem("ja-wd-reloaded", String(Date.now()));
    sessionStorage.setItem("ja-continue", String(Date.now()));
  } catch {
    return false;
  }
  location.reload();
  return true;
}

const workdaySite = {
  matches: () =>
    WD_HOSTS.test(location.hostname) &&
    Boolean(wdId("progressBarActiveStep") || wdLoginForm() || wdVisible(wdId("SignInWithEmailButton")) || wdErrorPage()),

  async run() {
    try {
      if (!wdErrorPage()) await wdSignIn();
    } catch (error) {
      showPanel(error.message, true);
      return { summary: error.message };
    }
    if (wdId("progressBarActiveStep")) await waitFor(() => wdErrorPage() || wdId("pageFooterNextButton"), 15000);
    if (wdErrorPage()) {
      const summary = wdReloadAndContinue()
        ? "Workday showed an error page after signing in, so I reloaded it. Filling carries on by itself once the page loads."
        : "Workday still shows an error page after a reload. Please refresh the page yourself, then click Fill this application again.";
      showPanel(summary, true);
      return { summary };
    }
    if (!wdId("progressBarActiveStep")) {
      const summary = /already applied/i.test(document.body.innerText)
        ? "You're signed in. Workday says you have already applied for this job."
        : "You're signed in. Open the job and click Apply, then click Fill this application again.";
      showPanel(summary, false);
      return { summary };
    }
    const base = await api("/answer", { url: location.href, fields: [] });
    if (!base || !base.ok) {
      const summary = `The local server could not prepare this job: ${base ? base.data.detail || base.status : "no reply"}`;
      showPanel(summary, true);
      return { summary };
    }
    const { files, profile } = base.data;
    // Watch from the start: if filling stops early, you may still submit by hand later.
    watchSubmit(base.data.company, base.data.title);
    const done = [];
    const review = [];
    for (let guard = 0; guard < 10; guard++) {
      const step = wdStep();
      if (/review/i.test(step)) {
        const summary = [`Reached the Review step after: ${done.join(", ") || "nothing"}.`, ...(review.length ? ["", "Answers the AI wrote, please read them:", ...review.map((r) => `- ${r}`)] : []), "", "Please look it over and press Submit yourself."].join("\n");
        showPanel(summary, false);
        api("/log", { url: location.href, company: document.title, title: clean((wdId("jobTitleHeading") || {}).innerText), event: "filled", problems: [] });
        watchSubmit(document.title, clean((wdId("jobTitleHeading") || {}).innerText));
        return { summary };
      }
      showPanel(`Filling "${step}"...`, false);
      await wdWaitForForm();
      const result = await wdFillStep(files, profile);
      review.push(...result.review);
      await sleep(800);
      // Errors shown before saving can be stale (Workday re-checks on save), so
      // only our own problems and missing answers stop us here.
      if (result.problems.length || result.needYou.length) {
        const lines = [`Stopped on "${step}" so you can check it.`];
        if (result.problems.length) lines.push("", "Please fix these by hand:", ...result.problems.map((p) => `- ${p}`));
        if (result.needYou.length) lines.push("", "These need your answer:", ...result.needYou.map((p) => `- ${p}`));
        lines.push("", "When this step is right, click Fill this application again to continue.");
        const summary = lines.join("\n");
        showPanel(summary, true);
        return { summary };
      }
      done.push(step);
      const moved = await wdNext(step);
      if (!moved) {
        const errs = wdErrors();
        const summary = [`Workday did not move past "${step}".`, ...(errs.length ? ["", ...errs.map((e) => `- ${e}`)] : []), "", "Fix the fields above, then click Fill again."].join("\n");
        showPanel(summary, true);
        return { summary };
      }
    }
    return { summary: "Stopped after 10 steps." };
  },
};
