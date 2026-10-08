// Ashby forms: every field sits in a box with data-field-path set to the field's path.
// Yes/No questions are two buttons, single choices are radio buttons, and
// location is a search box with suggestions.

function fieldBox(id) {
  return document.querySelector(`[data-field-path="${id}"]`);
}

function choiceInputs(box, type) {
  return [...box.querySelectorAll(`input[type=${type}]`)].map((input) => ({
    input,
    label: box.querySelector(`label[for="${input.id}"]`) || input.closest("label") || input.parentElement,
  }));
}

function pickChoice(box, type, wanted) {
  const choices = choiceInputs(box, type);
  const choice = matchText(choices, wanted, (c) => c.label.innerText);
  if (!choice) throw new Error(`no option "${wanted}" (saw: ${choices.map((c) => clean(c.label.innerText)).join(" / ") || "nothing"})`);
  if (!choice.input.checked) choice.input.click();
}

// "Start typing..." boxes (location, and single choices with long lists): type the
// way a keyboard does, wait for the suggestions, and click the one find() picks.
async function ashbyCombobox(box, text, find, what, ms = 8000) {
  const input = box.querySelector("input[role=combobox]") || box.querySelector("input");
  const options = () => {
    const listbox = input.getAttribute("aria-controls") && document.getElementById(input.getAttribute("aria-controls"));
    return [...(listbox || document).querySelectorAll("[role=option]")].filter((o) => o.offsetParent !== null);
  };
  input.focus();
  input.select();
  if (!document.execCommand("insertText", false, text) || input.value !== text) setNativeValue(input, text);
  let option = await waitFor(() => find(options()), ms);
  if (!option) {
    // Some lists only open on a click or Down Arrow.
    realClick(input);
    input.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "ArrowDown", code: "ArrowDown", keyCode: 40 }));
    option = await waitFor(() => find(options()), Math.min(ms, 3000));
  }
  if (!option) {
    const seen = options().slice(0, 6).map((o) => clean(o.innerText));
    throw new Error(`no ${what} (saw: ${seen.join(" / ") || "nothing"})`);
  }
  realClick(option);
  await sleep(200);
}

async function chooseAshbyLocation(box, place) {
  const [city, ...rest] = place.split(",").map((s) => s.trim());
  const state = (rest[rest.length - 1] || "").toLowerCase();
  const states = { ca: "california", ny: "new york", wa: "washington", tx: "texas", ma: "massachusetts" };
  const text = (o) => clean(o.innerText).toLowerCase();
  try {
    await ashbyCombobox(
      box,
      city,
      (options) =>
        options.find((o) => text(o).startsWith(city.toLowerCase()) && (text(o).includes(`, ${state}`) || (states[state] && text(o).includes(states[state])))) ||
        options.find((o) => text(o).startsWith(city.toLowerCase())) ||
        null,
      `"${place}" in the suggestions`,
    );
  } catch (error) {
    // No such city in the list: the country instead.
    let option = null;
    await countryInstead(async (typed, match, ms) => {
      await ashbyCombobox(box, typed, (options) => (option = match(options)), typed, ms).catch(() => {});
      return option;
    });
    if (!option) throw error;
  }
}

// "2026-12-15" or "2026-12" as the date box's MM/DD/YYYY.
function ashbyDate(value) {
  const m = String(value).match(/^(\d{4})-(\d{2})(?:-(\d{2}))?/);
  if (m) return `${m[2]}/${m[3] || "15"}/${m[1]}`;
  const parsed = new Date(String(value));
  return isNaN(parsed) ? "" : `${String(parsed.getMonth() + 1).padStart(2, "0")}/${String(parsed.getDate()).padStart(2, "0")}/${parsed.getFullYear()}`;
}

async function chooseAshbyOption(box, value) {
  await ashbyCombobox(box, String(value), (options) => matchText(options, String(value)) || null, `option "${value}"`);
}

const ashbySite = {
  matches: () => location.hostname === "jobs.ashbyhq.com" && Boolean(document.querySelector("[data-field-path]")),

  async fill(field, value, ctx) {
    const box = fieldBox(field.id);
    if (!box) throw new Error("field not found on the page");
    if (field.kind === "file") {
      await attachFile(box.querySelector("input[type=file]"), fileFor(value, ctx), box);
    } else if (field.kind === "text") {
      fillText(box.querySelector("textarea, input:not([type=file]):not([type=checkbox]):not([type=radio])"), value);
    } else if (field.kind === "boolean") {
      const button = box.querySelector(`button[data-option="${value.toLowerCase()}"]`);
      if (!button) throw new Error(`no "${value}" button`);
      if (button.getAttribute("aria-pressed") !== "true") realClick(button);
    } else if (field.kind === "radio" && !box.querySelector("input[type=radio]") && box.querySelector("input[role=combobox]")) {
      await chooseAshbyOption(box, value); // long lists are a search box instead of radio buttons
    } else if (field.kind === "radio") {
      pickChoice(box, "radio", value);
    } else if (field.kind === "checkboxes") {
      for (const item of [value].flat()) pickChoice(box, "checkbox", item); // one answer or a list; never letter by letter
    } else if (field.kind === "location") {
      await chooseAshbyLocation(box, value);
    } else if (field.kind === "date") {
      const input = box.querySelector("input");
      const text = ashbyDate(value);
      if (!input || !text) throw new Error(`can't read "${value}" as a date`);
      input.focus();
      input.select();
      if (!document.execCommand("insertText", false, text) || input.value !== text) setNativeValue(input, text);
      input.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Enter", keyCode: 13 }));
      input.blur();
      await sleep(150);
    } else {
      throw new Error(`don't know how to fill a "${field.kind}" field`);
    }
  },

  async optionsFor(field) {
    const box = fieldBox(field.id);
    if (!box) return [];
    const radios = choiceInputs(box, "radio").map((c) => clean(c.label.innerText));
    return radios.length ? radios : field.options || [];
  },

  check(field, value, ctx) {
    const box = fieldBox(field.id);
    if (!box) return "field not found on the page";
    if (field.kind === "file") return box.innerText.includes(fileFor(value, ctx).name) ? null : "file did not attach";
    if (field.kind === "text") {
      const shown = box.querySelector("textarea, input:not([type=file]):not([type=checkbox]):not([type=radio])").value;
      return clean(shown) === clean(value) ? null : `page shows "${shown}"`;
    }
    if (field.kind === "boolean") {
      const button = box.querySelector(`button[data-option="${value.toLowerCase()}"]`);
      return button && button.getAttribute("aria-pressed") === "true" ? null : `"${value}" is not selected`;
    }
    if (field.kind === "radio" && !box.querySelector("input[type=radio]") && box.querySelector("input[role=combobox]")) {
      const shown = clean(box.querySelector("input[role=combobox]").value || box.innerText);
      return shown.toLowerCase().includes(String(value).toLowerCase()) ? null : `page shows "${shown.slice(0, 60)}"`;
    }
    if (field.kind === "radio" || field.kind === "checkboxes") {
      const type = field.kind === "radio" ? "radio" : "checkbox";
      const checked = choiceInputs(box, type).filter((c) => c.input.checked).map((c) => clean(c.label.innerText));
      const wanted = Array.isArray(value) ? value : [value];
      const missing = wanted.filter((w) => !checked.some((c) => c.toLowerCase().startsWith(w.toLowerCase())));
      return missing.length ? `not selected: ${missing.join(", ")}` : null;
    }
    if (field.kind === "date") return box.querySelector("input").value === ashbyDate(value) ? null : `page shows "${box.querySelector("input").value}"`;
    if (field.kind === "location") {
      const shown = box.querySelector("input").value;
      return shown.includes(value.split(",")[0]) ? null : `page shows "${shown}"`;
    }
    return null;
  },
};

// On an Ashby job page the form lives under /application.
function ashbyApplicationUrl() {
  const match = location.pathname.match(/^\/([^/]+)\/([0-9a-f-]{36})\/?$/);
  return location.hostname === "jobs.ashbyhq.com" && match ? `${location.origin}/${match[1]}/${match[2]}/application` : null;
}
