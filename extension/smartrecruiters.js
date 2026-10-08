// SmartRecruiters: the job ad lives on jobs.smartrecruiters.com/<Company>/<id>, and
// "I'm interested" opens the application at .../oneclick-ui/company/<Company>/publication/<uuid>.
// The application is built from web components whose boxes sit inside shadow roots,
// where the general reader (generic.js) can't see, so this file reads the fields
// itself, asks the server for answers, and fills them with generic.js's helpers.
// It fills one page at a time and never presses Next or Submit.

const SR_HOST = /^(jobs|careers)\.smartrecruiters\.com$|^www\.smartr\.me$/.test(location.hostname);
const srApplyPage = () => SR_HOST && location.pathname.includes("/oneclick-ui/");

// Every element matching selector, in page order, looking inside open shadow roots too.
function srDeepAll(selector, root = document, out = []) {
  for (const element of root.querySelectorAll("*")) {
    if (element.matches(selector)) out.push(element);
    if (element.shadowRoot) srDeepAll(selector, element.shadowRoot, out);
  }
  return out;
}

// The parent, stepping out of a shadow root to its host.
const srUp = (node) => node.parentElement || (node.getRootNode() instanceof ShadowRoot ? node.getRootNode().host : null);
const srShown = (element) => element.getClientRects().length > 0;

// Bot check pages (DataDome) are left to you; they are never answered for you.
const srBlocked = () => /access is temporarily restricted|unusual activity from your device/i.test(document.body.innerText) || Boolean(document.querySelector('iframe[src*="captcha-delivery.com"]'));

function srLabel(element) {
  const root = element.getRootNode();
  const byFor = element.id && root.querySelector && root.querySelector(`label[for="${CSS.escape(element.id)}"]`);
  const byIds = (element.getAttribute("aria-labelledby") || "").split(" ").filter(Boolean).map((id) => root.getElementById && root.getElementById(id)).filter(Boolean);
  let text = (byFor && byFor.innerText) || (element.labels && element.labels[0] && element.labels[0].innerText) || byIds.map((e) => e.innerText).join(" ") || element.getAttribute("aria-label") || "";
  // The web component around the box often carries the label as an attribute.
  for (let host = root.host, i = 0; !clean(text) && host && i < 3; host = host.getRootNode().host, i++) {
    text = host.getAttribute("label") || host.getAttribute("aria-label") || "";
    const hostLabel = host.id && host.getRootNode().querySelector(`label[for="${CSS.escape(host.id)}"]`);
    if (!clean(text) && hostLabel) text = hostLabel.innerText;
  }
  // Otherwise the nearest text around it.
  for (let box = srUp(element), i = 0; !clean(text) && box && i < 5; box = srUp(box), i++) text = box.innerText;
  return gnCleanLabel(text || element.placeholder || "") || gnHumanize(element.name || element.id);
}

function srRequired(element, label) {
  const host = element.getRootNode().host;
  return element.required || element.getAttribute("aria-required") === "true" || Boolean(host && (host.hasAttribute("required") || host.getAttribute("aria-required") === "true")) || /\*/.test(label);
}

// A stable name for a box. Ids inside a component are only unique within it (every
// text component may call its box "input"), so a box in a shadow root is named
// after its component.
function srKey(element, index) {
  const host = element.getRootNode().host;
  if (host) return host.id || host.getAttribute("name") || element.name || `sr-${index}`;
  return element.name || element.id || `sr-${index}`;
}

// The question around a group of choices: step outward until there is text besides
// the options, but never so far that another question's boxes come into view.
function srQuestion(group, options) {
  let box = srUp(group[0]);
  for (let i = 0; box && i < 6; i++, box = srUp(box)) {
    if (srDeepAll("input, textarea, select", box).length > group.length) break;
    const text = options.reduce((rest, option) => rest.replace(option, " "), box.innerText || "");
    if (gnCleanLabel(text)) return { label: gnCleanLabel(text), required: /\*/.test(text) };
  }
  return { label: "", required: false };
}

// Fields in generic.js's shape, so its fill and check functions can be used.
function srFields() {
  const all = srDeepAll("input, textarea, select").filter((e) => !e.closest("#job-autofill-panel"));
  const fields = [];
  const seen = new Set();
  for (const [index, element] of all.entries()) {
    const type = (element.type || "").toLowerCase();
    if (GN_SKIP_TYPES.test(type) || element.disabled) continue;
    if (type !== "file" && type !== "radio" && type !== "checkbox" && !srShown(element)) continue;
    const choice = type === "radio" || type === "checkbox";
    const key = choice && element.name ? element.name : srKey(element, index);
    if (seen.has(key)) continue;
    seen.add(key);
    const group = choice && element.name ? all.filter((e) => e.type === type && e.name === element.name) : [element];
    let label = srLabel(element).trim();
    let required = srRequired(element, label);
    let kind = "text";
    let options = [];
    let filled = Boolean(clean(element.value || ""));
    if (element.tagName === "TEXTAREA") kind = "textarea";
    else if (element.tagName === "SELECT") {
      kind = "select";
      options = [...element.options].filter((o) => o.value && !GN_PLACEHOLDER.test(clean(o.text))).map((o) => clean(o.text));
      filled = element.selectedIndex > 0;
    } else if (choice && (type === "radio" || group.length > 1)) {
      kind = type === "radio" ? "select" : "checkboxes";
      options = group.map((e) => srLabel(e).trim());
      const question = srQuestion(group, options);
      label = question.label || label;
      required = required || question.required;
      filled = group.some((e) => e.checked);
    } else if (type === "checkbox") {
      kind = "checkbox";
      options = ["Yes", "No"];
      filled = element.checked;
    } else if (type === "file") {
      kind = "file";
      filled = element.files.length > 0;
    } else if (type === "number") kind = "number";
    else if (element.getAttribute("role") === "combobox" || element.getAttribute("aria-autocomplete") === "list") {
      // City search: the server answers "City, State" and the first suggestion that fits is picked.
      if (/location|city|where do you live/i.test(label)) kind = "location";
    }
    fields.push({ key, label, kind, options, required, group, filled });
  }
  return fields;
}

// Suggestions can be in a shadow root too, so they are looked for everywhere.
async function srPickSuggestion(element, value) {
  const city = String(value).split(",")[0].trim();
  const search = async (typed, match, ms = 6000) => {
    element.focus();
    element.select();
    if (!document.execCommand("insertText", false, typed) || element.value !== typed) setNativeValue(element, typed);
    return waitFor(() => match(srDeepAll("[role=option]").filter(srShown)), ms);
  };
  const wanted = String(value).toLowerCase();
  const text = (o) => clean(o.innerText).toLowerCase();
  const find = (options) => options.find((o) => text(o) === wanted) || options.find((o) => text(o).startsWith(city.toLowerCase()) && /california|, ca\b/.test(text(o))) || options.find((o) => text(o).startsWith(city.toLowerCase())) || null;
  const option = (await search(city, find)) || (await countryInstead(search));
  if (!option) throw new Error(`no suggestion matched "${value}" or the country`);
  realClick(option);
  await sleep(300);
}

async function srFill(field, value, files) {
  const [element] = field.group;
  if (field.kind === "location") return srPickSuggestion(element, value);
  if (field.kind === "file") {
    const file = fileFor(value, { files });
    const transfer = new DataTransfer();
    transfer.items.add(fileFromBase64(file));
    element.files = transfer.files;
    element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    return;
  }
  return gnFill(field, value, files);
}

function srCheck(field, value, files) {
  const [element] = field.group;
  if (field.kind === "location") return clean(element.value) ? null : "the city did not take";
  if (field.kind === "file") return element.files.length ? null : "file did not attach";
  return gnCheck(field, value, files);
}

// Job ad page: open the application.
function srOpenApplication() {
  if (!SR_HOST || srApplyPage()) return null;
  const apply = [...document.querySelectorAll('a[href*="/oneclick-ui/"]')].find((a) => a.hostname === location.hostname) || document.querySelector('a[href*="/oneclick-ui/"]');
  if (!apply) return null;
  location.href = apply.href;
  return "I opened the SmartRecruiters application. Once it loads, click Fill this application again.";
}

const smartRecruitersSite = {
  matches: () => srApplyPage() && (srBlocked() || srFields().length > 1),

  async run() {
    if (srBlocked()) {
      const summary = "SmartRecruiters is showing its security check instead of the application. Please complete it yourself, then click Fill this application again.";
      showPanel(summary, true);
      return { summary, failed: true };
    }
    const problems = [];
    const needYou = [];
    const review = [];
    let filled = 0;
    let kept = 0;

    // The resume goes first: SmartRecruiters reads it and fills your name, email and
    // so on by itself, and those boxes are then left as they are.
    const base = await api("/answer", { url: location.href, fields: [] });
    if (!base || !base.ok) {
      const summary = `The local server could not prepare this job: ${base ? base.data.detail || base.status : "no reply"}`;
      showPanel(summary, true);
      return { summary };
    }
    const { company, title } = base.data;
    watchSubmit(company, title);
    const resumeBox = srFields().find((f) => f.kind === "file" && !f.filled && !/cover/i.test(f.label));
    if (resumeBox && base.data.files.resume) {
      showPanel("Attaching your resume...", false);
      try {
        await srFill(resumeBox, "resume", base.data.files);
        filled++;
        await sleep(4000); // let SmartRecruiters read it
      } catch (error) {
        problems.push(`Resume: ${error.message}`);
      }
    }

    showPanel("Filling the form...", false);
    const all = srFields().filter((f) => !(f.kind === "file" && f.group[0] === (resumeBox && resumeBox.group[0])));
    kept += all.filter((f) => f.filled).length;
    const fields = all.filter((f) => !f.filled);
    const reply = await api("/answer", { url: location.href, fields: fields.map(({ group, filled: _, ...f }) => f) });
    if (!reply || !reply.ok) {
      const summary = `The local server could not answer: ${reply ? reply.data.detail || reply.status : "no reply"}`;
      showPanel(summary, true);
      return { summary };
    }
    const answers = Object.fromEntries(reply.data.fields.map((f) => [f.id, f.answer]));
    const { files } = reply.data;
    const warnings = reply.data.warnings || [];
    for (const field of fields) {
      const answer = answers[field.key] || {};
      if (isEmpty(answer.value)) {
        if (field.required || answer.source === "ask") needYou.push(`${field.label.slice(0, 70)}${answer.note ? ` (${answer.note})` : ""}`);
        continue;
      }
      try {
        await srFill(field, answer.value, files);
        await sleep(80);
        const issue = srCheck(field, answer.value, files);
        if (issue) throw new Error(issue);
        filled++;
        if (answer.source === "llm") review.push(`${field.label.slice(0, 70)}: "${answer.value}"`);
      } catch (error) {
        problems.push(`${field.label.slice(0, 70)}: ${error.message}`);
      }
    }

    api("/log", { url: location.href, company, title, event: "filled", problems });
    const lines = [...warnings.map((w) => `WARNING: ${w}`), ...(warnings.length ? [""] : []), `Filled ${filled} fields for ${title} at ${company}.`];
    if (kept) lines.push(`Left ${kept} field${kept === 1 ? "" : "s"} that SmartRecruiters (or you) had already filled as they were.`);
    if (problems.length) lines.push("", "Please fix these by hand:", ...problems.map((p) => `- ${p}`));
    if (needYou.length) lines.push("", "These need your answer:", ...needYou.map((p) => `- ${p}`));
    if (review.length) lines.push("", "Answers the AI wrote, please read them:", ...review.map((p) => `- ${p}`));
    lines.push("", "If this application has another page, click Next and then Fill this application again. Press Submit yourself at the end.");
    const summary = lines.join("\n");
    showPanel(summary, problems.length || needYou.length || warnings.length);
    return { summary, filled, problems, needYou, review, warnings, company, title };
  },
};
