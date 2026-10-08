// Find jobs: the scanned jobs from data/jobs.db, with scan controls, filters, the AI
// fit check, and an Apply button that opens the form and fills it with the extension.

const SERVER = "http://127.0.0.1:8765";
const PAGE = 150;
const SOURCE_NAMES = {
  simplify: "Simplify", speedyapply: "SpeedyApply", jobright: "Jobright", hiringcafe: "hiring.cafe", linkedin: "LinkedIn", remoteok: "RemoteOK", remotive: "Remotive",
  greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby", smartrecruiters: "SmartRecruiters", themuse: "The Muse", googlejobs: "Google", adzuna: "Adzuna", jobspy: "JobSpy",
};

let jobs = [];
let shown = PAGE;
let source = "";
let sortKey = "fit";
let sortDir = -1;
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
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `server said ${response.status}`);
  return data;
}

function showError(message) {
  $("error").replaceChildren(message ? el("div", { class: "error" }, message) : "");
}

const JOBSPY_NAMES = { indeed: "Indeed", zip_recruiter: "ZipRecruiter", google: "Google Jobs", linkedin: "LinkedIn", glassdoor: "Glassdoor" };
const sourceName = (key) => SOURCE_NAMES[key] || (key.startsWith("jobspy:") ? JOBSPY_NAMES[key.slice(7)] || key.slice(7) : key);
const jobDate = (job) => job.posted || job.first_seen || "";

function ago(day) {
  if (!day) return "";
  const [y, m, d] = day.split("-").map(Number);
  const days = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(y, m - 1, d)) / 864e5);
  return days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
}

function band(score) {
  return score == null ? "" : score >= 70 ? "high" : score >= 45 ? "mid" : "";
}

// ---- loading ----

async function load() {
  try {
    const data = await call(`/jobs?days=${$("days").value}`);
    jobs = data.jobs;
    showError("");
    render();
    showProgress(data.progress);
    $("subtitle").textContent = `${data.counts.total.toLocaleString()} jobs found so far. ${data.progress.last_scan ? `Last scan ${new Date(data.progress.last_scan).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}.` : "No scan yet."}`;
    if (data.progress.running) startPolling();
  } catch {
    showError("The local server is not running, so jobs can't be loaded. Start it in the job-automate folder with: uv run jobautomate-server");
    $("subtitle").textContent = "Not connected";
  }
}

// ---- filtering and the table ----

function filtered() {
  const words = $("search").value.trim().toLowerCase();
  const minFit = Number($("min-fit").value);
  const ai = $("ai").value;
  const hideApplied = $("hide-applied").checked;
  const showHidden = $("show-hidden").checked;
  const list = jobs.filter((j) => {
    if (!showHidden && j.hidden) return false;
    if (hideApplied && j.applied) return false;
    if (source && j.source !== source) return false;
    if (minFit && (j.fit ?? 0) < minFit) return false;
    if (ai === "recommended" && !j.recommend) return false;
    if (ai === "strong" && j.verdict !== "strong") return false;
    if (ai === "noflags" && (j.deep_fit == null || j.red_flags.length)) return false;
    if (ai === "unscored" && j.deep_fit != null) return false;
    if (words && !`${j.title} ${j.company} ${j.location}`.toLowerCase().includes(words)) return false;
    return true;
  });
  const value = (j) => (sortKey === "date" ? jobDate(j) : j[sortKey]);
  return list.sort((a, b) => {
    const x = value(a), y = value(b);
    if (x == null || x === "") return 1;
    if (y == null || y === "") return -1;
    const order = (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * sortDir;
    return order || jobDate(b).localeCompare(jobDate(a));
  });
}

function renderStats() {
  const visible = jobs.filter((j) => !j.hidden);
  const today = new Date().toISOString().slice(0, 10);
  const stats = [
    [visible.length, `jobs in this date range`],
    [visible.filter((j) => j.first_seen === today).length, "found today"],
    [visible.filter((j) => (j.fit ?? 0) >= 70).length, "strong title fit"],
    [visible.filter((j) => j.recommend && !j.red_flags.length).length, "AI says apply, no red flags"],
    [visible.filter((j) => j.applied).length, "applied"],
  ];
  $("stats").replaceChildren(...stats.map(([n, label]) => el("div", { class: "stat" }, el("b", {}, n.toLocaleString()), el("span", {}, label))));
}

function renderChips() {
  const counts = {};
  for (const j of jobs) if (!j.hidden) counts[j.source] = (counts[j.source] || 0) + 1;
  const keys = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  const chip = (key, label, n) => el("button", { class: `chip${source === key ? " on" : ""}`, onclick: () => ((source = key), (shown = PAGE), render()) }, label, el("b", {}, String(n)));
  $("chips").replaceChildren(chip("", "All sources", Object.values(counts).reduce((a, b) => a + b, 0)), ...keys.map((k) => chip(k, sourceName(k), counts[k])));
}

function fitCell(job) {
  const m = job.matched || {};
  const tip = [m.role?.length && `role: ${m.role.join(", ")}`, m.skills?.length && `skills: ${m.skills.join(", ")}`, m.level?.length && `level: ${m.level.join(", ")}`, m.off?.length && `off target: ${m.off.join(", ")}`].filter(Boolean).join(" · ") || "no keyword matches";
  return el("span", { class: `score ${band(job.fit)}`, title: tip }, job.fit == null ? "–" : String(job.fit));
}

function aiCell(job) {
  if (job.deep_fit == null) return el("span", { class: "score", title: "Not checked yet. Open the job and click Check fit with AI." }, "·");
  const tip = [job.verdict && `${job.verdict}${job.recommend ? ", worth applying" : ", skip"}`, ...job.reasons.map((r) => `+ ${r}`), ...job.gaps.map((g) => `gap: ${g}`)].join("\n");
  return el("button", { class: "ai", title: tip, onclick: () => openJob(job) }, el("span", { class: `score ${band(job.deep_fit)}` }, String(job.deep_fit)));
}

function render() {
  renderStats();
  renderChips();
  const list = filtered();
  const rows = list.slice(0, shown).map((job) => {
    const applied = el("input", { type: "checkbox", title: "Applied (adds it to your tracker)", checked: job.applied, onchange: (e) => setApplied(job, e.target.checked) });
    const title = el("div", { class: "title" }, el("a", { href: "#", onclick: (e) => (e.preventDefault(), openJob(job)) }, job.title));
    if (job.red_flags?.length) title.append(el("div", { class: "flags" }, job.red_flags.slice(0, 2).map((f) => el("span", { class: "flag" }, f))));
    return el(
      "tr",
      { class: `${job.applied ? "applied" : ""} ${job.hidden ? "is-hidden" : ""}` },
      el("td", {}, applied),
      el("td", {}, fitCell(job)),
      el("td", {}, aiCell(job)),
      el("td", {}, title),
      el("td", {}, job.company || ""),
      el("td", { class: "small" }, job.location || ""),
      el("td", {}, el("span", { class: "src" }, sourceName(job.source))),
      el("td", { class: "small", title: job.posted ? `Posted ${job.posted}` : `First seen ${job.first_seen}` }, ago(jobDate(job))),
      el("td", {}, el("div", { class: "row-actions" }, el("button", { class: "apply", onclick: () => apply(job) }, "Apply"), queueButton(job))),
      el("td", {}, el("button", { class: "icon-btn", title: job.hidden ? "Show again" : "Hide this job", onclick: () => setHidden(job, !job.hidden) }, job.hidden ? "↺" : "×")),
    );
  });
  $("rows").replaceChildren(...(rows.length ? rows : [el("tr", {}, el("td", { colspan: 10, class: "empty" }, jobs.length ? "No jobs match these filters." : "No jobs yet. Click Scan for new jobs."))]));
  $("more").hidden = list.length <= shown;
  $("more").textContent = `Show more (${(list.length - shown).toLocaleString()} left)`;
  document.querySelectorAll("th[data-sort]").forEach((th) => {
    th.textContent = th.textContent.replace(/ [▲▼]$/, "") + (th.dataset.sort === sortKey ? (sortDir === 1 ? " ▲" : " ▼") : "");
  });
}

// ---- actions ----

// Opens the application form in a new tab; the extension fills it there by itself.
async function apply(job) {
  chrome.runtime.sendMessage({ type: "openApply", url: job.apply_url, boardUrl: job.url });
  call(`/jobs/${job.id}`, { method: "PATCH", body: JSON.stringify({ opened: true }) }).catch(() => {});
}

// Jobs the auto apply queue can send by itself: Greenhouse, Lever, Ashby, Workday and
// Rippling forms, and company career pages that show a Greenhouse form (?gh_jid=).
const QUEUE_HOSTS = /(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|myworkday\.com)$|^ats\.rippling\.com$|\.oraclecloud\.com$|\.icims\.com$|^(workforcenow|myjobs)\.adp\.com$/;
const QUEUE_LABELS = { queued: "Queued", running: "Filling", submitted: "Sent", needs_you: "Needs you", unconfirmed: "Check email", ready: "Test run", skipped: "Skipped", failed: "Failed" };

function queueButton(job) {
  if (job.queue) return el("a", { class: `queued ${job.queue}`, href: "queue.html", title: "See it on the Auto apply page" }, QUEUE_LABELS[job.queue] || job.queue);
  let host = "";
  try {
    host = new URL(job.apply_url).hostname;
  } catch {}
  if (job.applied || !(QUEUE_HOSTS.test(host) || /[?&](gh_jid|ats=successfactors|icims=1)\b/.test(job.apply_url))) return null;
  return el("button", { class: "queue", title: "Add to the auto apply queue", onclick: () => queueJob(job) }, "Queue");
}

async function queueJob(job) {
  try {
    const result = await call("/queue", { method: "POST", body: JSON.stringify({ ids: [job.id] }) });
    if (result.skipped.length) return showError(`Not queued: ${result.skipped[0].reason}.`);
    job.queue = "queued";
    showError("");
    render();
  } catch (error) {
    showError(`Could not queue it: ${error.message}`);
  }
}

async function setApplied(job, on) {
  try {
    await call(`/jobs/${job.id}/applied`, { method: "POST", body: JSON.stringify({ on }) });
    job.applied = on;
    render();
  } catch (error) {
    showError(`Could not save it: ${error.message}`);
  }
}

async function setHidden(job, hidden) {
  try {
    await call(`/jobs/${job.id}`, { method: "PATCH", body: JSON.stringify({ hidden }) });
    job.hidden = hidden ? 1 : 0;
    render();
  } catch (error) {
    showError(`Could not save it: ${error.message}`);
  }
}

// ---- scanning ----

function showProgress(p) {
  const card = $("scan-card");
  if (!p || (!p.running && !p.finished_at)) return card.classList.remove("show");
  card.classList.add("show");
  $("scan").disabled = Boolean(p.running);
  $("scan").textContent = p.running ? "Scanning..." : "Scan for new jobs";
  const scoring = p.phase === "score";
  const found = p.matched ? `, ${p.matched} found on the company's own site` : "";
  $("scan-title").textContent = p.running
    ? scoring ? `Checking fit with AI: ${p.scored} of ${p.to_score}` : p.phase === "match" ? `Finding ${p.to_match} LinkedIn, Indeed and other listings on the companies' own sites...` : "Scanning job sources..."
    : p.error ? `The scan stopped: ${p.error}` : `Scan finished. ${p.added} new job${p.added === 1 ? "" : "s"}${found}${p.to_score ? `, ${p.scored} checked with AI` : ""}.`;
  $("scan-numbers").textContent = p.stale ? `${p.stale} older than your date limit skipped` : "";
  $("scan-sources").replaceChildren(
    ...(p.sources || []).map((s) => el("span", { class: `source ${s.state}`, title: s.state === "done" ? `found ${s.found}, ${s.added} new, in ${s.seconds ?? "?"} seconds` : s.state }, `${sourceName(s.key)}${s.state === "done" ? ` +${s.added}` : ""}`)),
  );
  $("score-bar").hidden = !scoring || !p.to_score;
  $("score-fill").style.width = p.to_score ? `${(100 * p.scored) / p.to_score}%` : "0";
  const notices = p.notices || [];
  $("notices").hidden = !notices.length;
  $("notices-title").textContent = `${notices.length} notice${notices.length === 1 ? "" : "s"} (sources that failed or were skipped)`;
  $("notices-list").replaceChildren(...notices.map((n) => el("div", {}, n)));
}

// While a scan runs, new jobs and AI scores show up as they arrive, not only at the end.
let lastSeen = "";
let lastReload = 0;
async function refreshJobs() {
  try {
    const data = await call(`/jobs?days=${$("days").value}`);
    jobs = data.jobs;
    render();
  } catch {
    /* the next poll tries again */
  }
}

function startPolling() {
  if (polling) return;
  polling = setInterval(async () => {
    try {
      const p = await call("/jobs/progress");
      showProgress(p);
      const seen = `${p.added || 0}/${p.scored || 0}`;
      if (p.running && seen !== lastSeen && Date.now() - lastReload > 3000) {
        lastSeen = seen;
        lastReload = Date.now();
        refreshJobs();
      }
      if (!p.running) {
        clearInterval(polling);
        polling = null;
        load();
      }
    } catch {
      /* keep trying */
    }
  }, 1000);
}

$("scan").addEventListener("click", async () => {
  try {
    const { progress } = await call("/jobs/scan", { method: "POST", body: "{}" });
    showProgress(progress);
    startPolling();
  } catch (error) {
    showError(`Could not start the scan: ${error.message}`);
  }
});

// ---- the job panel ----

async function openJob(job) {
  const full = await call(`/jobs/${job.id}`).catch(() => job);
  $("d-title").textContent = job.title;
  $("d-meta").textContent = [job.company, job.location, sourceName(job.source), job.posted && `posted ${job.posted}`].filter(Boolean).join(" · ");
  const tools = [
    el("button", { class: "primary", onclick: () => apply(job) }, "Apply"),
    queueButton(job),
    el("a", { class: "button", href: job.url, target: "_blank" }, "Open the posting"),
    el("button", { id: "check-ai", onclick: () => checkWithAi(job) }, job.deep_fit == null ? "Check fit with AI" : "Check again"),
    el("button", { onclick: closeJob }, "Close"),
  ];
  $("d-tools").replaceChildren(...tools);
  const body = [];
  if (full.deep_fit != null) {
    body.push(el("h3", {}, `AI fit: ${full.deep_fit} (${full.verdict}${full.recommend ? ", worth applying" : ", probably skip"})`));
    if (full.reasons.length) body.push(el("div", { class: "small muted" }, "Matches"), el("ul", {}, full.reasons.map((r) => el("li", {}, r))));
    if (full.gaps.length) body.push(el("div", { class: "small muted" }, "Gaps"), el("ul", {}, full.gaps.map((g) => el("li", {}, g))));
    if (full.red_flags.length) body.push(el("div", { class: "small muted" }, "Red flags"), el("ul", {}, full.red_flags.map((f) => el("li", {}, f))));
  }
  body.push(el("h3", {}, "Job description"));
  body.push(full.description ? el("div", { class: "jd" }, full.description) : el("p", { class: "muted" }, "Not loaded yet. It is fetched when the AI checks the fit, or open the posting to read it."));
  $("d-body").replaceChildren(...body);
  document.body.classList.add("open");
}

async function checkWithAi(job) {
  try {
    await call("/jobs/score", { method: "POST", body: JSON.stringify({ ids: [job.id] }) });
    $("check-ai").disabled = true;
    $("check-ai").textContent = "Checking...";
    startPolling();
    const wait = setInterval(async () => {
      const p = await call("/jobs/progress").catch(() => null);
      if (p && !p.running) {
        clearInterval(wait);
        await load();
        const fresh = jobs.find((j) => j.id === job.id);
        if (document.body.classList.contains("open")) openJob(fresh || job);
      }
    }, 1500);
  } catch (error) {
    showError(`Could not start the AI check: ${error.message}`);
  }
}

function closeJob() {
  document.body.classList.remove("open");
}

// ---- scan settings ----

async function openSettings() {
  const s = await call("/jobs/settings").catch((error) => (showError(error.message), null));
  if (!s) return;
  $("d-title").textContent = "Scan settings";
  $("d-meta").textContent = "Saved to profile/scanner.yaml";
  const terms = el("textarea", {}, s.search_terms.join("\n"));
  const include = el("textarea", { style: "min-height: 110px" }, s.title_filter.positive.join("\n"));
  const exclude = el("textarea", { style: "min-height: 110px" }, s.title_filter.negative.join("\n"));
  const maxAge = el("select", {}, [1, 3, 7, 14, 30, 0].map((d) => el("option", { value: d, selected: d === s.max_age_days }, d ? `Posted in the last ${d} day${d === 1 ? "" : "s"}` : "Any date")));
  const auto = el("select", {}, [0, 6, 12, 24].map((h) => el("option", { value: h, selected: h === Number(s.auto_scan_hours) }, h ? `Every ${h} hours` : "Off")));
  const deepOn = el("input", { type: "checkbox", checked: s.deep_score.enabled !== false });
  const minFit = el("input", { type: "number", min: 0, max: 100, value: s.deep_score.min_fit ?? 70, style: "width: 70px" });
  const boxes = Object.entries(s.sources).map(([name, on]) => el("label", { class: "check" }, el("input", { type: "checkbox", "data-source": name, checked: on }), sourceName(name)));
  $("d-body").replaceChildren(
    el("label", {}, "Search terms (one per line)"), terms,
    el("label", {}, "Keep titles with any of these words"), include,
    el("label", {}, "Drop titles with any of these words"), exclude,
    el("label", {}, "How recent"), maxAge,
    el("label", {}, "Scan by itself while the server runs"), auto,
    el("label", {}, "Sources"), el("div", { class: "grid2" }, boxes),
    el("label", {}, "AI fit check after each scan"),
    el("div", { class: "check" }, deepOn, "Check new jobs with a fit score of at least", minFit),
  );
  const lines = (area) => area.value.split("\n").map((l) => l.trim()).filter(Boolean);
  $("d-tools").replaceChildren(
    el("button", { class: "primary", onclick: async () => {
      try {
        await call("/jobs/settings", { method: "PUT", body: JSON.stringify({
          search_terms: lines(terms),
          title_filter: { positive: lines(include), negative: lines(exclude) },
          max_age_days: Number(maxAge.value),
          auto_scan_hours: Number(auto.value),
          sources: Object.fromEntries([...document.querySelectorAll("[data-source]")].map((b) => [b.dataset.source, b.checked])),
          deep_score: { enabled: deepOn.checked, min_fit: Number(minFit.value) },
        }) });
        closeJob();
      } catch (error) {
        showError(`Could not save the settings: ${error.message}`);
      }
    } }, "Save"),
    el("button", { onclick: closeJob }, "Cancel"),
  );
  document.body.classList.add("open");
}

// ---- wiring ----

$("settings-button").addEventListener("click", openSettings);
$("drawer-back").addEventListener("click", closeJob);
$("more").addEventListener("click", () => ((shown += PAGE), render()));
for (const id of ["search", "min-fit", "ai", "hide-applied", "show-hidden"]) $(id).addEventListener("input", () => ((shown = PAGE), render()));
$("days").addEventListener("change", () => ((shown = PAGE), load()));
document.querySelectorAll("th[data-sort]").forEach((th) =>
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    sortDir = sortKey === key ? -sortDir : key === "title" || key === "company" || key === "location" || key === "source" ? 1 : -1;
    sortKey = key;
    render();
  }),
);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeJob();
  if (e.key === "/" && document.activeElement !== $("search")) (e.preventDefault(), $("search").focus());
});
// Pick up jobs you applied to in other tabs.
document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && !polling && load());

load();
