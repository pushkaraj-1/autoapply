// Shared helpers and the fill loop. Each site file (greenhouse.js, ashby.js)
// defines an adapter with matches(), fill(field, value, ctx) and check(field, value, ctx).
// Sites are React apps, so values are set through the native setters and
// followed by the events React listens for.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clean = (text) => (text || "").replace(/\s+/g, " ").trim();
const isEmpty = (value) => value === null || value === undefined || (Array.isArray(value) && !value.length);

// The address that names this job. SuccessFactors' application page has the same
// address for every job of a company, so its job number is added from the form;
// otherwise two jobs would share answers, a cover letter and a tracker entry.
function pageJobUrl() {
  const req = /successfactors\.(com|eu)$|\.sapsf\./.test(location.hostname) && document.querySelector("#career_job_req_id");
  if (!req || !req.value || location.href.includes("career_job_req_id=")) return location.href;
  return `${location.href}${location.search ? "&" : "?"}career_job_req_id=${encodeURIComponent(req.value)}`;
}

function api(path, body) {
  return new Promise((resolve) => chrome.runtime.sendMessage({ type: "api", path, body }, resolve));
}

async function waitFor(check, timeout = 4000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const result = check();
    if (result) return result;
    await sleep(40);
  }
  return null;
}

function setNativeValue(element, value) {
  const proto = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(element, value);
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

function fillText(element, value) {
  element.focus();
  setNativeValue(element, value);
  element.dispatchEvent(new Event("blur", { bubbles: true }));
  element.blur();
}

// Some dropdowns only react to the whole event sequence a real click makes.
function realClick(element) {
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    const Event = type.startsWith("pointer") ? PointerEvent : MouseEvent;
    element.dispatchEvent(new Event(type, { bubbles: true, cancelable: true, button: 0, view: window }));
  }
}

// Names compared exactly, ignoring case, punctuation and "&" versus "and", so
// "Computer & Information Science" matches "Computer and Information Science".
const nameKey = (text) => clean(text).toLowerCase().replace(/&/g, " and ").replace(/[^a-z0-9]+/g, " ").trim();

function matchText(candidates, wanted, textOf = (c) => c.innerText) {
  const want = clean(wanted).toLowerCase();
  const text = (c) => clean(textOf(c)).toLowerCase();
  return (
    candidates.find((c) => text(c) === want) ||
    candidates.find((c) => text(c).startsWith(want)) ||
    candidates.find((c) => text(c).includes(want))
  );
}

function fileFromBase64(file) {
  const binary = atob(file.base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new File([bytes], file.name, { type: "application/pdf" });
}

async function attachFile(input, file, shownIn = document.body, timeout = 5000) {
  const transfer = new DataTransfer();
  transfer.items.add(fileFromBase64(file));
  input.files = transfer.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  await waitFor(() => shownIn.innerText.includes(file.name), timeout);
}

function fileFor(value, ctx) {
  const file = ctx.files[value];
  if (!file) throw new Error(`no ${value.replace("_", " ")} file was prepared`);
  return file;
}

// ---- the fill loop ----

async function runFill(adapter) {
  const reply = await api("/plan", { url: location.href });
  if (!reply || !reply.ok) {
    const detail = reply ? reply.data.detail || `server error ${reply.status}` : "no reply";
    return { summary: `The local server could not prepare this job: ${detail}`, failed: true };
  }
  const { fields, files, company, title, warnings = [] } = reply.data;
  const ctx = { fields, files };
  const problems = [];
  const label = (field) => clean(field.label).slice(0, 60);
  let filled = 0;

  for (const field of fields) {
    if (isEmpty(field.answer.value)) continue;
    try {
      await adapter.fill(field, field.answer.value, ctx);
      filled++;
    } catch (error) {
      problems.push(`${label(field)}: ${error.message}`);
    }
    await sleep(40);
  }
  if (adapter.after) await adapter.after(ctx);

  // Some sites read the resume and overwrite fields after we fill them, so
  // anything that doesn't match gets one more try before it is reported.
  await sleep(700);
  for (const field of fields) {
    if (isEmpty(field.answer.value) || problems.some((p) => p.startsWith(label(field)))) continue;
    let issue = adapter.check(field, field.answer.value, ctx);
    if (issue && field.kind !== "file") {
      try {
        await adapter.fill(field, field.answer.value, ctx);
        await sleep(200);
        issue = adapter.check(field, field.answer.value, ctx);
      } catch (error) {
        issue = error.message;
      }
    }
    if (issue) problems.push(`${label(field)}: ${issue}`);
  }

  const review = fields
    .filter((f) => f.answer.source === "llm")
    .map((f) => `${label(f)}: "${f.answer.value}" ${f.answer.note || ""}`);
  const needYou = fields
    .filter((f) => isEmpty(f.answer.value) && (f.answer.source === "ask" || (f.required && f.answer.source !== "skip")))
    .map((f) => `${label(f)}${f.answer.note ? ` (${f.answer.note})` : ""}`);

  api("/log", { url: location.href, company, title, event: "filled", problems });
  watchSubmit(company, title);

  const lines = [...warnings.map((w) => `WARNING: ${w}`), ...(warnings.length ? [""] : []), `Filled ${filled} fields for ${title} at ${company}.`];
  if (problems.length) lines.push("", "Please fix these by hand:", ...problems.map((p) => `- ${p}`));
  if (needYou.length) lines.push("", "These need your answer:", ...needYou.map((p) => `- ${p}`));
  if (review.length) lines.push("", "Answers the AI wrote, please read them:", ...review.map((p) => `- ${p}`));
  if (!problems.length && !needYou.length) lines.push("", "Everything matched. Please look it over and press Submit yourself.");
  const summary = lines.join("\n");
  showPanel(summary, problems.length || needYou.length || warnings.length);
  // The lists are read by the auto apply queue to decide whether it may submit.
  return { summary, filled, problems, needYou, review, warnings, company, title };
}

let watchingSubmit = false;
function watchSubmit(company, title) {
  if (watchingSubmit) return;
  watchingSubmit = true;
  document.addEventListener(
    "click",
    (event) => {
      const button = event.target.closest("button, input[type=submit], [role=button]");
      if (button && /submit/i.test(button.innerText || button.value || button.getAttribute("aria-label") || "")) {
        api("/log", { url: pageJobUrl(), company, title, event: "submit_clicked", problems: [] }).then((reply) => {
          if (reply && reply.ok) showPanel("Added this job to your tracker as applied. You can see it on the Dashboard.", false);
        });
      }
    },
    true,
  );
}

// ---- the panel in the corner of the page ----
// Lives in a shadow root so the job site's own styles can't reach it.

const PANEL_CSS = `
  :host { all: initial; }
  .card {
    --bg: rgba(255, 255, 255, 0.92); --fg: #1d1d1f; --muted: #6e6e73; --line: rgba(0, 0, 0, 0.08);
    --chip: rgba(0, 0, 0, 0.04); --accent: #0a84ff;
    position: fixed; top: 16px; right: 16px; z-index: 2147483647; box-sizing: border-box;
    width: min(360px, calc(100vw - 32px)); max-height: min(72vh, 640px); display: flex; flex-direction: column;
    background: var(--bg); color: var(--fg); border: 1px solid var(--line); border-radius: 14px;
    backdrop-filter: saturate(180%) blur(18px); -webkit-backdrop-filter: saturate(180%) blur(18px);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06), 0 12px 32px rgba(0, 0, 0, 0.14);
    font: 13px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
    overflow: hidden; animation: enter 0.22s ease-out;
  }
  @media (prefers-color-scheme: dark) {
    .card { --bg: rgba(30, 30, 32, 0.9); --fg: #f5f5f7; --muted: #a1a1a6; --line: rgba(255, 255, 255, 0.1); --chip: rgba(255, 255, 255, 0.06); }
  }
  .card[data-state="done"] { --accent: #30a46c; }
  .card[data-state="problem"] { --accent: #e5484d; }
  @keyframes enter { from { opacity: 0; transform: translateY(-6px) scale(0.98); } }
  header { display: flex; align-items: center; gap: 8px; padding: 11px 10px 9px 14px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); flex: none; }
  .card[data-state="working"] .dot { animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.35; transform: scale(0.8); } }
  .title { flex: 1; font-size: 12px; font-weight: 600; letter-spacing: 0.01em; color: var(--muted); }
  button {
    all: unset; cursor: pointer; width: 24px; height: 24px; border-radius: 7px; display: grid; place-items: center;
    color: var(--muted); font-size: 16px; line-height: 1;
  }
  button:hover { background: var(--chip); color: var(--fg); }
  .bar { height: 2px; margin: 0 14px; border-radius: 2px; background: var(--line); overflow: hidden; }
  .bar::after { content: ""; display: block; width: 40%; height: 100%; background: var(--accent); animation: slide 1.1s ease-in-out infinite; }
  @keyframes slide { from { transform: translateX(-100%); } to { transform: translateX(250%); } }
  .card:not([data-state="working"]) .bar { display: none; }
  .body { padding: 6px 14px 14px; overflow-y: auto; overscroll-behavior: contain; }
  p { margin: 0 0 8px; }
  p:last-child, ul:last-child { margin-bottom: 0; }
  .label { margin: 12px 0 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
  ul { list-style: none; margin: 0 0 8px; padding: 0; display: grid; gap: 4px; }
  li { padding: 6px 9px; border-radius: 8px; background: var(--chip); overflow-wrap: anywhere; }
  .warning { padding: 8px 10px; border-radius: 8px; background: rgba(229, 72, 77, 0.1); color: #e5484d; }
  .card.small .body { display: none; }
`;

// Turns the plain summary into short paragraphs, labelled lists ("Please fix
// these by hand:" followed by "- " lines) and warnings.
function panelBody(text) {
  const body = document.createElement("div");
  body.className = "body";
  let list = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("- ")) {
      if (!list) list = body.appendChild(document.createElement("ul"));
      list.appendChild(document.createElement("li")).textContent = line.slice(2);
      continue;
    }
    list = null;
    if (!line.trim()) continue;
    const node = body.appendChild(document.createElement(/:$/.test(line) ? "div" : "p"));
    if (/:$/.test(line)) node.className = "label";
    if (line.startsWith("WARNING: ")) node.className = "warning";
    node.textContent = line.replace(/^WARNING: /, "").replace(/:$/, "");
  }
  return body;
}

function showPanel(text, hasProblems) {
  let host = document.getElementById("job-autofill-panel");
  if (!host) {
    host = document.createElement("div");
    host.id = "job-autofill-panel";
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `<style>${PANEL_CSS}</style><div class="card" role="status" aria-live="polite">
      <header><span class="dot"></span><span class="title">Job Autofill</span>
      <button class="fold" title="Minimise" aria-label="Minimise">&#8211;</button>
      <button class="close" title="Close" aria-label="Close">&#215;</button></header>
      <div class="bar"></div><div class="body"></div></div>`;
    root.querySelector(".close").onclick = () => host.remove();
    root.querySelector(".fold").onclick = () => root.querySelector(".card").classList.toggle("small");
    document.documentElement.appendChild(host);
  }
  const card = host.shadowRoot.querySelector(".card");
  // Messages that end in "..." are progress updates; the rest are results.
  card.dataset.state = /\.\.\.$/.test(text.trim()) ? "working" : hasProblems ? "problem" : "done";
  card.querySelector(".title").textContent = card.dataset.state === "working" ? "Filling" : hasProblems ? "Needs a look" : "Done";
  card.querySelector(".body").replaceWith(panelBody(text));
}
