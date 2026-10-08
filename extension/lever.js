// Lever forms: every field is found by its name attribute. Dropdowns are native
// selects, choices are labelled radio buttons or checkboxes, and location is a
// search box that fills a hidden selectedLocation field when a suggestion is clicked.

function named(name) {
  return document.querySelector(`[name="${name}"]`);
}

function leverChoices(name) {
  return [...document.querySelectorAll(`input[name="${name}"], input[name="${name}[]"]`)].map((input) => ({
    input,
    label: input.closest("label") || input.parentElement,
  }));
}

function chooseLeverChoice(name, wanted) {
  const choices = leverChoices(name);
  const choice = matchText(choices, wanted, (c) => c.label.innerText);
  if (!choice) throw new Error(`no option "${wanted}" (saw: ${choices.map((c) => clean(c.label.innerText)).join(" / ") || "nothing"})`);
  if (!choice.input.checked) choice.input.click();
}

function chooseNativeSelect(select, wanted) {
  const option = matchText([...select.options].filter((o) => o.value), wanted, (o) => o.text);
  if (!option) throw new Error(`no option "${wanted}"`);
  select.value = option.value;
  select.dispatchEvent(new Event("input", { bubbles: true }));
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

async function chooseLeverLocation(place) {
  const input = named("location");
  const [city, state] = place.split(",").map((s) => s.trim().toLowerCase());
  const search = async (typed, match, ms = 10000) => {
    input.focus();
    setNativeValue(input, typed);
    // The search runs on key events, not on input events.
    for (const type of ["keydown", "keypress", "keyup"]) {
      input.dispatchEvent(new KeyboardEvent(type, { bubbles: true, key: "s", code: "KeyS", keyCode: 83, which: 83 }));
    }
    return waitFor(() => match([...document.querySelectorAll(".dropdown-location")]), ms);
  };
  const text = (o) => clean(o.innerText).toLowerCase();
  const option =
    (await search(place.split(",")[0], (options) => options.find((o) => text(o).startsWith(city) && (text(o).includes(state) || /\busa?\b/.test(text(o)))) || null)) ||
    (await countryInstead(search));
  if (!option) throw new Error("no matching city or country in the suggestions");
  realClick(option);
  await sleep(120);
}

const leverSite = {
  matches: () => location.hostname === "jobs.lever.co" && Boolean(named("email")),

  async fill(field, value, ctx) {
    const element = named(field.id);
    if (field.kind === "file") {
      const input = named("resume");
      const box = input.closest("li, .application-question") || document.body;
      await attachFile(input, fileFor(value, ctx), box);
      // Lever reads the resume and fills some fields itself; let that finish
      // so our answers are written after it.
      await waitFor(() => /success/i.test(box.innerText), 8000);
      await sleep(1500);
    } else if (field.kind === "text") {
      if (!element) throw new Error("field not found on the page");
      fillText(element, value);
    } else if (field.kind === "select") {
      if (!element) throw new Error("field not found on the page");
      chooseNativeSelect(element, value);
    } else if (field.kind === "radio") {
      chooseLeverChoice(field.id, value);
    } else if (field.kind === "checkboxes") {
      for (const item of [value].flat()) chooseLeverChoice(field.id, item); // one answer or a list; never letter by letter
    } else if (field.kind === "location") {
      await chooseLeverLocation(value);
    } else {
      throw new Error(`don't know how to fill a "${field.kind}" field`);
    }
  },

  check(field, value, ctx) {
    const element = named(field.id);
    if (field.kind === "file") {
      const box = named("resume").closest("li, .application-question") || document.body;
      return /success/i.test(box.innerText) ? null : "resume did not upload";
    }
    if (field.kind === "radio" || field.kind === "checkboxes") {
      const checked = leverChoices(field.id).filter((c) => c.input.checked).map((c) => clean(c.label.innerText).toLowerCase());
      const wanted = Array.isArray(value) ? value : [value];
      const missing = wanted.filter((w) => !checked.some((c) => c.startsWith(w.toLowerCase())));
      return missing.length ? `not selected: ${missing.join(", ")}` : null;
    }
    if (!element) return "field not found on the page";
    if (field.kind === "select") {
      const shown = element.selectedOptions[0] ? clean(element.selectedOptions[0].text) : "";
      return shown.toLowerCase().startsWith(value.toLowerCase()) ? null : `page shows "${shown}"`;
    }
    if (field.kind === "location") {
      const chosen = named("selectedLocation");
      return element.value.toLowerCase().startsWith(value.split(",")[0].toLowerCase()) && chosen && chosen.value
        ? null
        : `page shows "${element.value}" and no suggestion was picked`;
    }
    return clean(element.value) === clean(value) ? null : `page shows "${element.value}"`;
  },
};
