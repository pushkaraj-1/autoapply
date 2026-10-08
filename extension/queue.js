// Auto apply: the queue the server works through in a headless Chrome. Jobs where
// every field matched are submitted; the rest wait under Needs you, and Open and
// fill opens them in a tab here so you can finish and submit them yourself.

const SERVER = "http://127.0.0.1:8765";
const GROUPS = [
  ["needs", "Needs you"],
  ["queued", "Queued"],
  ["sent", "Sent"],
  ["other", "Other"],
];
const SITE_NAMES = { greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby", workday: "Workday", rippling: "Rippling", oracle: "Oracle Cloud", successfactors: "SuccessFactors", icims: "iCIMS (hands to you)", adp: "ADP (hands to you)" };
const siteList = () => data.settings.sites.map((s) => SITE_NAMES[s] || s).join(", ").replace(/, ([^,]*)$/, " and $1");

let data = null;
let group = "needs";
let polling = null;

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "style") node.style.cssText = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}

async function call(path, options = {}) {
  const response = await fetch(SERVER + path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `server said ${response.status}`);
  return body;
}

function showError(message) {
  $("error").replaceChildren(message ? el("div", { class: "error" }, message) : "");
}

function showNotice(message) {
  $("notice").replaceChildren(message ? el("div", { class: "note" }, message) : "");
}

function groupOf(entry) {
  if (entry.status === "submitted" || entry.applied) return "sent";
  if (entry.status === "queued" || entry.status === "running") return "queued";
  if (entry.status === "needs_you" || entry.status === "unconfirmed") return "needs";
  return "other";
}

const when = (stamp) => (stamp ? new Date(stamp).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "");

// ---- loading ----

async function load() {
  try {
    data = await call("/queue");
    showError("");
    render();
    if (data.running) startPolling();
  } catch {
    showError("The local server is not running. Start it in the job-automate folder with: uv run jobautomate-server");
    $("status-title").textContent = "Not connected";
  }
}

function startPolling() {
  if (polling) return;
  polling = setInterval(async () => {
    try {
      data = await call("/queue");
      render();
      if (!data.running) {
        clearInterval(polling);
        polling = null;
      }
    } catch {
      /* keep trying */
    }
  }, 2000);
}

// ---- rendering ----

function renderStatus() {
  const s = data.settings;
  const cap = s.daily_cap;
  $("ring").style.setProperty("--p", Math.min(100, (100 * data.submitted_today) / Math.max(cap, 1)));
  $("ring-text").textContent = `${data.submitted_today}/${cap}`;
  const queued = data.entries.filter((e) => e.status === "queued").length;
  const title = $("status-title");
  if (data.running) {
    title.replaceChildren(el("span", { class: "dot" }), data.stopping ? "Stopping..." : data.skipping ? "Skipping this job..." : data.current ? `Working on ${data.current.title} at ${data.current.company}` : data.message);
  } else {
    title.textContent = data.message || (queued ? `${queued} job${queued === 1 ? "" : "s"} waiting. Click Start to send them.` : "Nothing queued yet.");
  }
  const detail = [`${data.submitted_today} sent today, limit ${cap} a day.`];
  if (data.running && data.current) detail.push(data.message);
  if (!s.submit) detail.push("Test mode is on: forms are filled and checked, but nothing is sent.");
  $("status-detail").textContent = detail.join(" ");
  const start = $("start");
  start.textContent = data.running ? (data.stopping ? "Stopping..." : "Stop") : "Start";
  start.title = data.running ? "Stop the queue now. The job being filled goes back in line." : "";
  const skip = $("skip");
  skip.hidden = !data.running || !data.current;
  skip.disabled = Boolean(data.stopping || data.skipping);
  skip.textContent = data.skipping ? "Skipping..." : "Skip this job";
  start.className = data.running ? "stop" : "primary";
  start.disabled = data.stopping || (!data.running && !queued);
}

let codeShownFor = null;

function renderCode() {
  const request = data.code_request;
  document.title = request ? "Code needed · Auto apply" : "Auto apply";
  if (!request) {
    codeShownFor = null;
    $("code").replaceChildren();
    return;
  }
  if (codeShownFor === request.job_id) return; // keep what you are typing
  codeShownFor = request.job_id;
  const input = el("input", { type: "text", autocomplete: "one-time-code", placeholder: "Code", maxlength: 12, required: true });
  const form = el(
    "form",
    {
      onsubmit: (event) => {
        event.preventDefault();
        act("/queue/code", { method: "POST", body: JSON.stringify({ job_id: request.job_id, code: input.value.trim() }) }, () => showNotice("Code sent. The queue is entering it and submitting."));
      },
    },
    input,
    el("button", { class: "primary", type: "submit" }, "Send code"),
  );
  $("code").replaceChildren(
    el(
      "div",
      { class: "code-box" },
      el("b", {}, `${request.company} emailed you a security code`),
      el(
        "span",
        { class: "small" },
        request.reading_email
          ? `For ${request.title}. The queue is reading it from your inbox and will enter it by itself. If you see the email first, you can type the code here.`
          : `For ${request.title}. Copy it from your email and type it here; the queue enters it and submits. It waits up to ${request.minutes} minutes.`,
      ),
      form,
    ),
  );
  input.focus();
}

function tagFor(entry) {
  if (entry.applied && entry.status !== "submitted") return el("span", { class: "tag good" }, "Applied by you");
  const tags = {
    queued: ["", "Queued"],
    running: ["warn", "Filling now"],
    submitted: ["good", "Submitted"],
    needs_you: ["warn", "Needs you"],
    unconfirmed: ["warn", "Check your email"],
    ready: ["", "Filled, not sent (test mode)"],
    skipped: ["", "Skipped"],
    failed: ["bad", "Failed"],
  };
  const [kind, text] = tags[entry.status] || ["", entry.status];
  return el("span", { class: `tag ${kind}` }, text);
}

function item(entry) {
  const meta = [entry.company, entry.location, entry.site, entry.deep_fit != null && `AI fit ${entry.deep_fit}`, entry.finished_at && when(entry.finished_at)].filter(Boolean).join(" · ");
  const parts = [
    el("div", { class: "item-head" }, el("div", {}, el("h3", {}, entry.title), el("div", { class: "muted small" }, meta)), tagFor(entry)),
  ];
  if (entry.reasons.length) parts.push(el("ul", {}, entry.reasons.map((r) => el("li", {}, r))));
  if (entry.ai_answers.length) parts.push(el("details", {}, el("summary", {}, `Answers the AI wrote (${entry.ai_answers.length})`), el("ul", {}, entry.ai_answers.map((a) => el("li", {}, a)))));

  const actions = [];
  if (entry.status !== "running" && !entry.applied && entry.status !== "submitted") {
    actions.push(el("button", { class: "primary", onclick: () => openAndFill(entry) }, "Open and fill"));
  }
  if (entry.has_screenshot) {
    actions.push(el("a", { class: "button", href: `${SERVER}/queue/${entry.job_id}/screenshot`, target: "_blank" }, entry.status === "submitted" ? "Confirmation" : "Screenshot"));
    if (entry.status === "submitted") actions.push(el("a", { class: "button", href: `${SERVER}/queue/${entry.job_id}/screenshot?which=form`, target: "_blank" }, "Filled form"));
  }
  if (["needs_you", "failed", "ready", "skipped"].includes(entry.status) && !entry.applied) actions.push(el("button", { onclick: () => retry(entry) }, "Try again"));
  if (entry.status === "unconfirmed" && !entry.applied) actions.push(el("button", { onclick: () => markApplied(entry) }, "It went through, mark applied"));
  actions.push(el("a", { class: "button", href: entry.url, target: "_blank" }, "Posting"));
  if (entry.status !== "running") actions.push(el("button", { onclick: () => remove(entry) }, entry.status === "queued" ? "Remove" : "Clear"));
  parts.push(el("div", { class: "actions" }, actions));
  return el("div", { class: "item" }, parts);
}

function render() {
  renderStatus();
  renderCode();
  const counts = Object.fromEntries(GROUPS.map(([key]) => [key, 0]));
  for (const entry of data.entries) counts[groupOf(entry)]++;
  $("chips").replaceChildren(
    ...GROUPS.map(([key, label]) => el("button", { class: `chip${group === key ? " on" : ""}`, onclick: () => ((group = key), render()) }, label, el("b", {}, String(counts[key])))),
  );
  const list = data.entries.filter((e) => groupOf(e) === group);
  if (group === "sent") list.reverse();
  const empty = {
    needs: "Nothing needs you right now.",
    queued: "The queue is empty. Click Add best matches, or use Queue on the Find jobs page.",
    sent: "Nothing sent yet.",
    other: "Nothing here.",
  };
  const items = list.length ? list.map(item) : [el("div", { class: "empty" }, empty[group])];
  const open = list.filter((e) => !e.applied && ["needs_you", "unconfirmed"].includes(e.status));
  if (group === "needs" && open.length > 1) {
    const batch = open.slice(0, 5);
    items.unshift(
      el("div", { class: "list-tools" }, el("button", { class: "primary", onclick: () => batch.forEach(openAndFill) }, `Open and fill ${batch.length === open.length ? "all" : `the next ${batch.length}`} in my browser`)),
    );
  }
  $("list").replaceChildren(...items);
}

// ---- actions ----

function openAndFill(entry) {
  chrome.runtime.sendMessage({ type: "openApply", url: entry.apply_url, boardUrl: entry.url });
}

async function act(path, options, after) {
  try {
    const result = await call(path, options);
    if (after) after(result);
    await load();
  } catch (error) {
    showError(error.message);
  }
}

const retry = (entry) => act("/queue", { method: "POST", body: JSON.stringify({ ids: [entry.job_id] }) });
const remove = (entry) => act(`/queue/${entry.job_id}`, { method: "DELETE" });
const markApplied = (entry) => act(`/jobs/${entry.job_id}/applied`, { method: "POST", body: JSON.stringify({ on: true }) });

$("best").addEventListener("click", () => {
  showNotice("Looking for matches. Jobs the AI hasn't checked yet are being scored now, which can take a minute...");
  $("best").disabled = true;
  act("/queue/best", { method: "POST", body: JSON.stringify({ limit: 50 }) }, (result) => {
    group = "queued";
    const scored = result.scored_now ? ` Scored ${result.scored_now} new job${result.scored_now === 1 ? "" : "s"} with the AI first.` : "";
    showNotice(
      result.added.length
        ? `Added ${result.added.length} job${result.added.length === 1 ? "" : "s"} the AI recommends with a fit of ${data.settings.min_fit} or more.${scored}`
        : `No new jobs to add out of ${result.considered} the queue can send.${scored} The queue sends ${siteList()} forms the AI recommends with a fit of ${data.settings.min_fit} or more; scan for more jobs on the Find jobs page.`,
    );
  }).finally(() => ($("best").disabled = false));
});

$("skip").addEventListener("click", () => act("/queue/skip", { method: "POST" }));

$("start").addEventListener("click", () => {
  if (data.running) return act("/queue/stop", { method: "POST" });
  act("/queue/start", { method: "POST" }, () => {
    group = "queued";
    startPolling();
  });
});

// ---- settings ----

function openSettings() {
  const s = data.settings;
  const cap = el("input", { type: "number", min: 1, max: 1000, value: s.daily_cap });
  const lookups = el("input", { type: "number", min: 0, max: 2000, value: s.web_lookups_per_day ?? 40 });
  const minFit = el("input", { type: "number", min: 0, max: 100, value: s.min_fit });
  const gap = el("input", { type: "number", min: 10, max: 600, value: s.gap_seconds });
  const testMode = el("input", { type: "checkbox", checked: !s.submit });
  const sites = Object.entries(SITE_NAMES).map(([key, name]) => el("label", { class: "check" }, el("input", { type: "checkbox", "data-site": key, checked: s.sites.includes(key) }), name));
  $("d-body").replaceChildren(
    el("label", {}, "Most applications to send in a day"), cap,
    el("label", {}, "Add best matches takes jobs with an AI fit of at least"), minFit,
    el("label", {}, "Seconds to wait between applications"), gap,
    el("label", {}, "Sites to send by itself"), ...sites,
    el("label", {}, "Web searches a day for finding companies' own sites (for LinkedIn, Indeed and other listings)"), lookups,
    el("div", { class: "hint" }, `Search service: ${data.search || "unknown"}`),
    el("label", {}, "Test mode"),
    el("label", { class: "check" }, testMode, "Fill and check forms, but don't press Submit"),
  );
  $("d-tools").replaceChildren(
    el("button", { class: "primary", onclick: () => act("/queue/settings", { method: "PUT", body: JSON.stringify({
      daily_cap: Number(cap.value), min_fit: Number(minFit.value), gap_seconds: Number(gap.value), submit: !testMode.checked, web_lookups_per_day: Number(lookups.value),
      sites: [...document.querySelectorAll("[data-site]")].filter((b) => b.checked).map((b) => b.dataset.site),
    }) }, closeSettings) }, "Save"),
    el("button", { onclick: closeSettings }, "Cancel"),
  );
  document.body.classList.add("open");
}

function closeSettings() {
  document.body.classList.remove("open");
}

$("settings-button").addEventListener("click", openSettings);
$("drawer-back").addEventListener("click", closeSettings);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeSettings());
// Pick up jobs you finished in other tabs.
document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && !polling && load());

load();
