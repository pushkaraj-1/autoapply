// Rippling forms: standard fields use data-testid="input-<oid>", custom questions
// sit in a box whose data-testid ends with ".<uniqueKey>". Dropdowns are
// role=combobox divs whose options have ids starting with "<combobox id>-list-option-".

function ripplingBox(id) {
  return document.querySelector(`[data-testid$=".${id}"]`);
}

function ripplingInput(field) {
  if (field.kind === "location") return document.querySelector('[data-testid="location"] input');
  if (field.kind === "file") return document.querySelector(`input[data-testid="input-${field.id}"]`);
  return (
    document.querySelector(`[data-testid="input-${field.id}"]`) ||
    (ripplingBox(field.id) && ripplingBox(field.id).querySelector("textarea, input"))
  );
}

async function chooseRipplingOption(combobox, wanted) {
  const options = () => [...document.querySelectorAll(`[id^="${combobox.id}-list-option-"]`)];
  if (!options().length) realClick(combobox);
  await waitFor(() => options().length, 3000);
  const option = matchText(options(), wanted);
  if (!option) throw new Error(`no option "${wanted}" (saw: ${options().map((o) => clean(o.innerText)).join(" / ") || "nothing"})`);
  realClick(option);
  await sleep(250);
}

async function chooseRipplingLocation(input, place) {
  const [city, state] = place.split(",").map((s) => s.trim().toLowerCase());
  input.focus();
  setNativeValue(input, place.split(",")[0]);
  for (const type of ["keydown", "keyup"]) input.dispatchEvent(new KeyboardEvent(type, { bubbles: true, key: "s", keyCode: 83 }));
  const option = await waitFor(() => {
    const options = [...document.querySelectorAll("[role=option]")];
    const text = (o) => clean(o.innerText).toLowerCase();
    return options.find((o) => text(o).startsWith(city) && (text(o).includes(state) || text(o).includes(", ca"))) || null;
  }, 10000);
  if (!option) throw new Error("no matching city in the suggestions");
  realClick(option);
  await sleep(300);
}

const ripplingSite = {
  matches: () => location.hostname === "ats.rippling.com" && Boolean(document.querySelector('[data-testid="input-first_name"]')),

  async fill(field, value, ctx) {
    if (field.kind === "file") {
      const input = ripplingInput(field);
      if (!input) throw new Error("upload field not found on the page");
      await attachFile(input, fileFor(value, ctx), input.closest('[data-testid="field"]') || document.body);
      // Rippling reads the resume and fills fields itself; let that finish first.
      if (field.id === "resume") await sleep(3000);
    } else if (field.kind === "select") {
      const box = ripplingBox(field.id);
      const combobox = box && box.querySelector("[role=combobox]");
      if (!combobox) throw new Error("field not found on the page");
      await chooseRipplingOption(combobox, value);
    } else if (field.kind === "enum") {
      // Radio buttons (labels you click) or a dropdown, depending on the question.
      const box = ripplingBox(field.id);
      if (!box) throw new Error("field not found on the page");
      const radios = [...box.querySelectorAll("[role=radio]")];
      if (radios.length) {
        for (const want of [value].flat()) {
          const choice = matchText(radios, want);
          if (!choice) throw new Error(`no option "${want}" (saw: ${radios.map((r) => clean(r.innerText)).join(" / ")})`);
          if (choice.getAttribute("aria-checked") !== "true") realClick(choice);
        }
        await sleep(150);
      } else {
        const combobox = box.querySelector("[role=combobox]");
        if (!combobox) throw new Error("field not found on the page");
        for (const want of [value].flat()) await chooseRipplingOption(combobox, want);
      }
    } else if (field.kind === "date") {
      const box = ripplingBox(field.id);
      const m = String(value).match(/^(\d{4})-(\d{2})(?:-(\d{2}))?/);
      if (!box || !m) throw new Error(box ? `can't read "${value}" as a date` : "field not found on the page");
      for (const [part, text] of [["month", m[2]], ["day", m[3] || "15"], ["year", m[1]]]) {
        const input = box.querySelector(`[data-testid="input-${part}"]`);
        if (!input) throw new Error(`no ${part} box`);
        input.focus();
        input.select();
        if (!document.execCommand("insertText", false, text) || input.value.replace(/\D/g, "") !== text.replace(/^0/, "") && input.value !== text) setNativeValue(input, text);
        input.blur();
      }
      await sleep(150);
    } else if (field.kind === "location") {
      await chooseRipplingLocation(ripplingInput(field), value);
    } else if (field.kind === "text") {
      const input = ripplingInput(field);
      if (!input) throw new Error("field not found on the page");
      fillText(input, field.id === "phone_number" ? value.replace(/^\+1\s*/, "") : value);
    } else {
      throw new Error(`don't know how to fill a "${field.kind}" field`);
    }
  },

  async optionsFor(field) {
    const box = ripplingBox(field.id);
    const radios = box ? [...box.querySelectorAll("[role=radio]")].map((r) => clean(r.innerText)) : [];
    return radios.length ? radios : field.options || [];
  },

  check(field, value, ctx) {
    if (field.kind === "file") {
      const box = ripplingInput(field) && ripplingInput(field).closest('[data-testid="field"]');
      return box && box.innerText.includes(fileFor(value, ctx).name) ? null : "file did not attach";
    }
    if (field.kind === "enum") {
      const box = ripplingBox(field.id);
      if (!box) return "field not found on the page";
      const radios = [...box.querySelectorAll("[role=radio]")];
      const shown = radios.length
        ? radios.filter((r) => r.getAttribute("aria-checked") === "true" || (r.querySelector("input") || {}).checked || (box.querySelector(`input[type=radio][value="${CSS.escape(clean(r.innerText))}"]`) || {}).checked).map((r) => clean(r.innerText))
        : [clean((box.querySelector("[role=combobox]") || {}).innerText || "")];
      const missing = [value].flat().filter((v) => !shown.some((s) => s.toLowerCase() === String(v).toLowerCase()));
      return missing.length ? `page shows "${shown.join(", ")}"` : null;
    }
    if (field.kind === "date") {
      const box = ripplingBox(field.id);
      const parts = box ? ["month", "day", "year"].map((p) => (box.querySelector(`[data-testid="input-${p}"]`) || {}).value || "") : [];
      return parts.every(Boolean) ? null : `page shows "${parts.join("/")}"`;
    }
    if (field.kind === "select") {
      const combobox = ripplingBox(field.id) && ripplingBox(field.id).querySelector("[role=combobox]");
      const shown = combobox ? clean(combobox.innerText) : "";
      return shown.toLowerCase() === value.toLowerCase() ? null : `page shows "${shown}"`;
    }
    const input = ripplingInput(field);
    if (!input) return "field not found on the page";
    if (field.kind === "location") return input.value.toLowerCase().startsWith(value.split(",")[0].toLowerCase()) ? null : `page shows "${input.value}"`;
    if (field.id === "phone_number") return input.value.replace(/\D/g, "").endsWith(value.replace(/\D/g, "").slice(-10)) ? null : `page shows "${input.value}"`;
    return clean(input.value) === clean(value) ? null : `page shows "${input.value}"`;
  },
};
