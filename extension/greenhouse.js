// Greenhouse forms: plain inputs found by id, react-select dropdowns whose
// options have ids starting with react-select-<field id>-option-.

function phoneDigits(phone, country) {
  let digits = phone.replace(/\D/g, "");
  if (country === "United States" && digits.length === 11 && digits.startsWith("1")) digits = digits.slice(1);
  return digits;
}

function menuOptions(id) {
  return [...document.querySelectorAll(`[id^="react-select-${id}-option-"]`)];
}

async function openMenu(input) {
  if (menuOptions(input.id).length) return;
  realClick(input.closest(".select__control"));
  await waitFor(() => menuOptions(input.id).length, 2000);
}

// Country options read like "United States +1", so a dialing code after the name also counts.
function findOption(options, wanted) {
  const want = wanted.toLowerCase();
  const escaped = want.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return (
    options.find((o) => clean(o.innerText).toLowerCase() === want) ||
    options.find((o) => new RegExp(`^${escaped}\\s*\\+\\d`).test(clean(o.innerText).toLowerCase())) ||
    matchText(options, wanted)
  );
}

async function chooseOption(id, wanted) {
  const input = document.getElementById(id);
  if (!input) throw new Error("field not found on the page");
  await openMenu(input);
  let option = findOption(menuOptions(id), wanted);
  if (!option) {
    // Type to filter, for long lists.
    setNativeValue(input, wanted.split(",")[0]);
    option = await waitFor(() => findOption(menuOptions(id), wanted), 8000);
  }
  if (!option) {
    const seen = menuOptions(id).slice(0, 6).map((o) => clean(o.innerText));
    input.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape", keyCode: 27 }));
    throw new Error(`no option "${wanted}" (saw: ${seen.join(" / ") || "nothing"})`);
  }
  realClick(option);
  await sleep(80);
}

async function chooseLocation(id, place) {
  const input = document.getElementById(id);
  if (!input) throw new Error("field not found on the page");
  realClick(input.closest(".select__control"));
  await sleep(80);
  setNativeValue(input, place.split(",")[0]);
  const state = place.split(",").slice(-1)[0].trim().toLowerCase();
  const option = await waitFor(() => menuOptions(id).find((o) => clean(o.innerText).toLowerCase().includes(state)) || null, 10000);
  if (!option) throw new Error("no matching city in the suggestions");
  realClick(option);
  await sleep(80);
}

// Newer forms draw some choice questions as a group of checkboxes or radio buttons
// (a fieldset with the question's id) instead of a dropdown.
function choiceGroup(id) {
  const element = document.getElementById(id);
  if (!element || element.tagName !== "FIELDSET") return null;
  return [...element.querySelectorAll("input[type=checkbox], input[type=radio]")].map((input) => ({
    input,
    text: clean((element.querySelector(`label[for="${CSS.escape(input.id)}"]`) || input.closest("label") || input.parentElement.parentElement).innerText),
  }));
}

function tickChoice(id, wanted) {
  const choices = choiceGroup(id);
  const choice = matchText(choices, wanted, (c) => c.text);
  if (!choice) throw new Error(`no option "${wanted}" (saw: ${choices.map((c) => c.text).join(" / ") || "nothing"})`);
  if (!choice.input.checked) choice.input.click();
}

function shownValue(field) {
  const element = document.getElementById(field.id);
  if (field.kind === "text") return element ? element.value : null;
  const group = choiceGroup(field.id);
  if (group) {
    const ticked = group.filter((c) => c.input.checked).map((c) => c.text);
    return field.kind === "multiselect" ? ticked : ticked[0] || "";
  }
  const control = element && element.closest(".select__control");
  if (!control) return null;
  if (field.kind === "multiselect") {
    return [...control.querySelectorAll(".select__multi-value__label")].map((e) => clean(e.innerText));
  }
  const single = control.querySelector(".select__single-value");
  return single ? clean(single.innerText) : "";
}

// ---- education: one block of School / Degree (and sometimes Discipline) per school ----

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

function selectedText(id) {
  const element = document.getElementById(id);
  const single = element && element.closest(".select__control") && element.closest(".select__control").querySelector(".select__single-value");
  return single ? clean(single.innerText) : "";
}

async function addEducationBlock(index) {
  let box = document.getElementById("school--0");
  while (box && !/Add another/i.test(box.innerText)) box = box.parentElement;
  const add = box && [...box.querySelectorAll("button, a")].find((e) => /add another/i.test(e.innerText));
  if (!add) throw new Error("could not find the Add another button for education");
  realClick(add);
  if (!(await waitFor(() => document.getElementById(`school--${index}`), 3000))) throw new Error("a new education block did not appear");
}

async function fillDatePart(id, value) {
  const element = document.getElementById(id);
  if (!element || !value) return;
  if (element.getAttribute("role") === "combobox") await chooseOption(id, value);
  else fillText(element, value);
}

// Picks the first of these names the list has, matched exactly (see nameKey), trying
// each in order. Long lists are searched with `search` (or each name) first.
// A nearby but different name is never picked; that is reported instead.
async function chooseExact(id, names, search) {
  if (names.some((n) => nameKey(n) === nameKey(selectedText(id)))) return;
  const input = document.getElementById(id);
  if (!input) throw new Error("field not found on the page");
  await openMenu(input);
  const find = (name) => menuOptions(id).find((o) => nameKey(o.innerText) === nameKey(name)) || null;
  // Short lists show every option at once.
  for (const name of names) {
    const hit = !search && find(name);
    if (hit) return realClick(hit), sleep(80);
  }
  for (const name of names) {
    setNativeValue(input, search || name);
    const hit = await waitFor(() => find(name), search ? 6000 : 1500);
    if (hit) return realClick(hit), sleep(80);
    if (search) {
      const other = names.map(find).find(Boolean);
      if (other) return realClick(other), sleep(80);
      break;
    }
  }
  const seen = menuOptions(id).slice(0, 5).map((o) => clean(o.innerText));
  input.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape", keyCode: 27 }));
  throw new Error(`none of "${names.join('", "')}" is in the list${search ? ` (searched "${search}")` : ""} (saw: ${seen.join(" / ") || "nothing"})`);
}

async function fillEducation(schools) {
  for (let i = 0; i < schools.length; i++) {
    const school = schools[i];
    if (!document.getElementById(`school--${i}`)) await addEducationBlock(i);
    await chooseExact(`school--${i}`, school.names || [school.school], school.search || school.school);
    if (document.getElementById(`degree--${i}`) && selectedText(`degree--${i}`) !== school.degree) await chooseOption(`degree--${i}`, school.degree);
    // Major lists differ per company; the profile's majors are tried in order, exactly.
    if (document.getElementById(`discipline--${i}`)) await chooseExact(`discipline--${i}`, school.disciplines || [school.discipline]);
    for (const [which, date] of [["start", school.start], ["end", school.end]]) {
      const [year, month] = date.split("-");
      await fillDatePart(`${which}-month--${i}`, MONTHS[Number(month) - 1]);
      await fillDatePart(`${which}-year--${i}`, year);
    }
  }
}

function checkEducation(schools) {
  const missing = [];
  schools.forEach((school, i) => {
    if (!(school.names || [school.school]).some((n) => nameKey(n) === nameKey(selectedText(`school--${i}`)))) missing.push(`school ${i + 1} shows "${selectedText(`school--${i}`)}"`);
    if (document.getElementById(`degree--${i}`) && selectedText(`degree--${i}`) !== school.degree) missing.push(`degree ${i + 1} not set`);
  });
  return missing.length ? missing.join(", ") : null;
}

const countryOf = (ctx) => (ctx.fields.find((f) => f.id === "country") || { answer: {} }).answer.value || "";

const greenhouseSite = {
  matches: () => location.hostname.endsWith("greenhouse.io") && Boolean(document.getElementById("first_name")),

  async fill(field, value, ctx) {
    if (field.kind === "file") {
      const input = document.getElementById(field.id);
      if (!input) throw new Error("upload field not found on the page");
      await attachFile(input, fileFor(value, ctx));
    } else if (field.kind === "text") {
      const element = document.getElementById(field.id);
      if (!element) throw new Error("field not found on the page");
      fillText(element, field.id === "phone" ? phoneDigits(value, countryOf(ctx)) : value);
    } else if (field.kind === "select") {
      const shown = shownValue(field);
      if (!(shown && shown.startsWith(value))) await (choiceGroup(field.id) ? tickChoice(field.id, value) : chooseOption(field.id, value));
    } else if (field.kind === "multiselect") {
      for (const item of [value].flat()) {
        if (!(shownValue(field) || []).includes(item)) await (choiceGroup(field.id) ? tickChoice(field.id, item) : chooseOption(field.id, item));
      }
    } else if (field.kind === "location") {
      await chooseLocation(field.id, value);
    } else if (field.kind === "education") {
      if (document.getElementById("school--0")) await fillEducation(value);
    } else {
      throw new Error(`don't know how to fill a "${field.kind}" field`);
    }
  },

  // Every option the page offers for a choice question.
  async optionsFor(field) {
    const group = choiceGroup(field.id);
    if (group) return group.map((c) => c.text);
    const input = document.getElementById(field.id);
    if (!input || !input.closest(".select__control")) return field.options || [];
    await openMenu(input);
    const options = menuOptions(field.id).map((o) => clean(o.innerText));
    input.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Escape", keyCode: 27 }));
    return options.length ? options : field.options || [];
  },

  async after(ctx) {
    const answeredDemographics = ctx.fields.some((f) => /^\d+$/.test(f.id) && !isEmpty(f.answer.value));
    const consent = document.querySelector('input[name="gdpr_demographic_data_consent_given"]');
    if (consent && answeredDemographics && !consent.checked) consent.click();
    // Privacy-law consent to store and process the application (EU boards). You agreed
    // to accept such terms (profile.yaml, standing_answers.agree_to_certifications).
    if (ctx.agreeToTerms) {
      for (const box of document.querySelectorAll('input[name="gdpr_processing_consent_given"], input[name="gdpr_retention_consent_given"]')) {
        if (!box.checked) box.click();
      }
    }
  },

  check(field, value, ctx) {
    if (field.kind === "file") return document.body.innerText.includes(fileFor(value, ctx).name) ? null : "file did not attach";
    if (field.kind === "education") return document.getElementById("school--0") ? checkEducation(value) : null;
    const shown = shownValue(field);
    if (shown === null) return "field not found on the page";
    if (field.kind === "multiselect") {
      const missing = [value].flat().filter((v) => !shown.includes(v));
      return missing.length ? `missing ${missing.join(", ")}` : null;
    }
    const country = countryOf(ctx);
    if (field.id === "phone") return phoneDigits(shown, country) === phoneDigits(value, country) ? null : `page shows "${shown}"`;
    if (field.id === "country") return shown ? null : "nothing selected";
    if (field.kind === "location") return shown.includes(value.split(",")[0]) ? null : `page shows "${shown}"`;
    return clean(shown) === clean(value) ? null : `page shows "${shown}"`;
  },
};
