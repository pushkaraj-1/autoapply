// Dashboard: statistics and the list of applications, kept by the local server in data/tracker.json.

const SERVER = "http://127.0.0.1:8765";
const STATUSES = [
  ["applied", "Applied"],
  ["heard_back", "Heard back"],
  ["interview", "Interview"],
  ["offer", "Offer"],
  ["rejected", "Rejected"],
];
const STATUS_LABEL = Object.fromEntries(STATUSES);
const DAY = 24 * 3600 * 1000;

let applications = [];
let filter = "all";
let search = "";
let withinDays = 0;
let shown = 50;
const PAGE = 50;

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
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `server said ${response.status}`);
  return data;
}

function dayKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function percent(part, whole) {
  return whole ? `${Math.round((100 * part) / whole)}%` : "0%";
}

// Stages an application ever reached, so a rejection after an interview still counts as an interview.
function reached(application) {
  const seen = new Set([application.status, ...application.history.map((h) => h.status)]);
  return {
    heardBack: ["heard_back", "interview", "offer", "rejected"].some((s) => seen.has(s)),
    interview: seen.has("interview") || seen.has("offer"),
    offer: seen.has("offer"),
    rejected: application.status === "rejected",
  };
}

function firstReplyDays(application) {
  const reply = application.history.find((h) => h.status !== "applied");
  return reply ? (new Date(reply.at) - new Date(application.applied_at)) / DAY : null;
}

// ---- statistics ----

const CHART_DAYS = 30;

function countSince(days) {
  const from = new Date(startOfDay(new Date()) - (days - 1) * DAY);
  return applications.filter((a) => new Date(a.applied_at) >= from).length;
}

function renderStats() {
  const cards = [
    ["Today", countSince(1), "today"],
    ["This week", countSince(7)],
    ["Last 30 days", countSince(30)],
    ["Total", applications.length],
  ];
  $("stats").replaceChildren(
    ...cards.map(([label, value, kind]) => el("div", { class: `stat ${kind || ""}` }, el("div", { class: "value" }, value.toLocaleString()), el("div", { class: "label" }, label))),
  );

  // One quiet line for what happened after applying.
  const stages = applications.map(reached);
  const heard = stages.filter((s) => s.heardBack).length;
  const interviews = stages.filter((s) => s.interview).length;
  const offers = stages.filter((s) => s.offer).length;
  const replies = applications.map(firstReplyDays).filter((d) => d != null);
  const parts = [
    [heard, "heard back"],
    [interviews, interviews === 1 ? "interview" : "interviews"],
    [offers, offers === 1 ? "offer" : "offers"],
    [percent(heard, applications.length), "reply rate"],
  ];
  const line = parts.flatMap(([n, label], i) => [i ? " · " : "", el("b", {}, String(n)), ` ${label}`]);
  if (replies.length) line.push(` · replies come after about ${Math.max(1, Math.round(replies.reduce((s, d) => s + d, 0) / replies.length))} days`);
  $("outcomes").replaceChildren(...line);
}

// A step for the y axis that gives three or four round grid lines.
function niceStep(max) {
  const rough = Math.max(1, max) / 3;
  const power = 10 ** Math.floor(Math.log10(rough));
  return [1, 2, 5, 10].map((m) => m * power).find((s) => s >= rough);
}

function renderChart() {
  const perDay = {};
  for (const a of applications) {
    const key = dayKey(new Date(a.applied_at));
    perDay[key] = (perDay[key] || 0) + 1;
  }
  const today = startOfDay(new Date());
  const days = Array.from({ length: CHART_DAYS }, (_, i) => new Date(today.getFullYear(), today.getMonth(), today.getDate() - (CHART_DAYS - 1 - i)));
  const counts = days.map((d) => perDay[dayKey(d)] || 0);
  const step = niceStep(Math.max(...counts));
  const top = Math.max(step, Math.ceil(Math.max(...counts) / step) * step);

  const lines = [];
  const labels = [];
  for (let v = 0; v <= top; v += step) {
    const bottom = `${(100 * v) / top}%`;
    if (v) lines.push(el("div", { class: "grid-line", style: `bottom: ${bottom}` }));
    labels.push(el("span", { style: `bottom: ${bottom}` }, String(v)));
  }
  const tip = $("tip");
  const bars = days.map((day, i) =>
    el(
      "div",
      {
        class: `col${i === CHART_DAYS - 1 ? " today" : ""}`,
        onmouseenter: (event) => {
          const n = counts[i];
          tip.replaceChildren(el("div", { class: "muted small" }, day.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })), el("b", {}, `${n} application${n === 1 ? "" : "s"}`));
          const col = event.currentTarget.getBoundingClientRect();
          const chart = $("chart").getBoundingClientRect();
          tip.style.left = `${Math.min(col.left - chart.left + col.width / 2 + 8, chart.width - 150)}px`;
          tip.style.top = "8px";
          tip.classList.add("show");
        },
        onmouseleave: () => tip.classList.remove("show"),
      },
      el("div", { style: `height: ${counts[i] ? Math.max(2, (100 * counts[i]) / top) : 0}%` }),
    ),
  );
  $("plot").replaceChildren(...lines, el("div", { class: "bars" }, bars));
  $("y-axis").replaceChildren(...labels);
  // A date under every fifth day keeps the axis readable.
  $("x-axis").replaceChildren(
    ...days.map((d, i) => el("span", {}, (CHART_DAYS - 1 - i) % 5 === 0 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "")),
  );
}

// ---- list ----

function renderFilters() {
  const options = [["all", "All"], ...STATUSES];
  $("filters").replaceChildren(
    ...options.map(([value, label]) => {
      const count = value === "all" ? applications.length : applications.filter((a) => a.status === value).length;
      return el("button", { class: `chip${filter === value ? " on" : ""}`, onclick: () => ((filter = value), (shown = PAGE), renderFilters(), renderRows()) }, label, el("b", {}, String(count)));
    }),
  );
}

function editable(application, field, className) {
  return el("td", {
    class: className,
    contenteditable: "true",
    spellcheck: "false",
    onkeydown: (event) => event.key === "Enter" && (event.preventDefault(), event.target.blur()),
    onblur: async (event) => {
      // The role cell also holds the job link (↗); keep only the text.
      const value = event.target.textContent.replace(/\s*↗$/, "").trim();
      if (value === (application[field] || "")) return;
      await save(application, { [field]: value });
    },
  }, application[field] || "");
}

function renderRows() {
  const words = search.toLowerCase();
  const from = withinDays ? new Date(startOfDay(new Date()) - (withinDays - 1) * DAY) : null;
  const list = applications
    .filter((a) => filter === "all" || a.status === filter)
    .filter((a) => !from || new Date(a.applied_at) >= from)
    .filter((a) => !words || `${a.role} ${a.company} ${a.location}`.toLowerCase().includes(words))
    .sort((a, b) => new Date(b.applied_at) - new Date(a.applied_at));
  $("more").hidden = list.length <= shown;
  $("more").textContent = `Show more (${list.length - shown} left)`;
  if (!list.length) {
    $("rows").replaceChildren(el("tr", {}, el("td", { colspan: 8, class: "empty" }, applications.length ? "Nothing matches." : "No applications yet. Submit one with the extension, or click Add application.")));
    return;
  }
  $("rows").replaceChildren(
    ...list.slice(0, shown).map((a) => {
      const applied = new Date(a.applied_at);
      const days = Math.floor((startOfDay(new Date()) - startOfDay(applied)) / DAY);
      const status = el(
        "select",
        { class: "status", style: `--c: var(--${a.status})`, onchange: (event) => save(a, { status: event.target.value }) },
        STATUSES.map(([value, label]) => el("option", { value, selected: value === a.status }, label)),
      );
      const role = editable(a, "role", "role");
      if (a.url) role.append(" ", el("a", { href: a.url, target: "_blank", contenteditable: "false", title: "Open the job" }, "↗"));
      return el(
        "tr",
        {},
        role,
        editable(a, "company"),
        editable(a, "location"),
        el("td", { title: applied.toLocaleString() }, applied.toLocaleDateString(undefined, { month: "short", day: "numeric" }), el("div", { class: "muted small" }, days === 0 ? "today" : `${days} day${days === 1 ? "" : "s"} ago`)),
        el("td", {}, status),
        el("td", {}, el("button", { class: `jd-button${a.has_description ? "" : " missing"}`, onclick: () => openDescription(a) }, a.has_description ? "View" : "Add")),
        el("td", {}, a.has_letter ? el("a", { href: `${SERVER}/applications/${a.id}/letter`, target: "_blank" }, "PDF") : el("span", { class: "muted" }, "None")),
        el("td", {}, el("button", { class: "del", title: "Remove from tracker", onclick: () => remove(a) }, "×")),
      );
    }),
  );
}

function renderAll() {
  renderStats();
  renderChart();
  renderFilters();
  renderRows();
  $("subtitle").textContent = `${applications.length} applications tracked. Updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.`;
}

async function save(application, changes) {
  try {
    const updated = await call(`/applications/${application.id}`, { method: "PATCH", body: JSON.stringify(changes) });
    applications = applications.map((a) => (a.id === updated.id ? updated : a));
    renderAll();
  } catch (error) {
    showError(`Could not save the change: ${error.message}`);
  }
}

async function remove(application) {
  if (!confirm(`Remove ${application.role || "this application"}${application.company ? ` at ${application.company}` : ""} from the tracker?`)) return;
  try {
    await call(`/applications/${application.id}`, { method: "DELETE" });
    applications = applications.filter((a) => a.id !== application.id);
    renderAll();
  } catch (error) {
    showError(`Could not remove it: ${error.message}`);
  }
}

function showError(message) {
  $("error").replaceChildren(message ? el("div", { class: "error" }, message) : "");
}

async function load() {
  try {
    applications = (await call("/applications")).applications;
    showError("");
    renderAll();
  } catch {
    showError("The local server is not running, so your applications can't be loaded. Start it in the job-automate folder with: uv run jobautomate-server");
    $("subtitle").textContent = "Not connected";
  }
}

// ---- add by hand ----

$("add").addEventListener("click", () => {
  $("form").reset();
  $("f-date").value = dayKey(new Date());
  $("dialog").showModal();
});
$("cancel").addEventListener("click", () => $("dialog").close());
$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await call("/applications", {
      method: "POST",
      body: JSON.stringify({
        role: $("f-role").value.trim(),
        company: $("f-company").value.trim(),
        location: $("f-location").value.trim(),
        url: $("f-url").value.trim(),
        applied_at: `${$("f-date").value}T12:00:00`,
        description: $("f-description").value.trim(),
      }),
    });
    $("dialog").close();
    load();
  } catch (error) {
    showError(`Could not add it: ${error.message}`);
  }
});
// ---- job description panel ----

async function openDescription(application, editing = false) {
  const { text } = await call(`/applications/${application.id}/description`).catch(() => ({ text: "" }));
  $("d-title").textContent = application.role || "Job description";
  $("d-meta").textContent = [application.company, application.location, `applied ${new Date(application.applied_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`].filter(Boolean).join(" · ");
  const tools = [];
  const body = $("d-body");
  if (editing || !text) {
    const area = el("textarea", { placeholder: "Paste the job description here." });
    area.value = text;
    body.replaceChildren(area);
    tools.push(
      el("button", { class: "primary", onclick: async () => {
        try {
          const updated = await call(`/applications/${application.id}/description`, { method: "PUT", body: JSON.stringify({ text: area.value }) });
          applications = applications.map((a) => (a.id === updated.id ? updated : a));
          renderRows();
          openDescription(updated);
        } catch (error) {
          showError(`Could not save the description: ${error.message}`);
        }
      } }, "Save"),
    );
    if (text) tools.push(el("button", { onclick: () => openDescription(application) }, "Cancel"));
    setTimeout(() => area.focus(), 50);
  } else {
    body.textContent = text;
    body.scrollTop = 0;
    tools.push(el("button", { onclick: () => openDescription(application, true) }, "Edit"));
    tools.push(el("button", { onclick: (event) => navigator.clipboard.writeText(text).then(() => (event.target.textContent = "Copied")) }, "Copy"));
  }
  if (application.url) tools.push(el("a", { class: "button", href: application.url, target: "_blank" }, "Open the posting"));
  tools.push(el("button", { onclick: closeDescription }, "Close"));
  $("d-tools").replaceChildren(...tools);
  document.body.classList.add("drawer-open");
}

function closeDescription() {
  document.body.classList.remove("drawer-open");
}
$("drawer-back").addEventListener("click", closeDescription);
document.addEventListener("keydown", (event) => event.key === "Escape" && closeDescription());

$("search").addEventListener("input", (event) => ((search = event.target.value), (shown = PAGE), renderRows()));
$("date-filter").addEventListener("change", (event) => ((withinDays = Number(event.target.value)), (shown = PAGE), renderRows()));
$("more").addEventListener("click", () => ((shown += PAGE), renderRows()));

load();
// Pick up applications submitted in other tabs.
document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && load());
