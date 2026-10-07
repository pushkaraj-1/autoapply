const SERVER = "http://127.0.0.1:8765";
const RADIUS = 42;
const DAY = 24 * 3600 * 1000;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;
const $ = (id) => document.getElementById(id);
const statusLine = $("status");

let goal = 20;
let today = 0;

// ---- server and counts ----

fetch(`${SERVER}/health`)
  .then((response) => response.json())
  .then(() => {
    $("dot").className = "dot ok";
    $("server").textContent = "Server running";
    $("fill").disabled = false;
    $("applied").disabled = false;
    showCounts();
    warmActiveTab();
  })
  .catch(() => {
    $("dot").className = "dot bad";
    $("server").textContent = "Server is off";
    statusLine.innerHTML = "Start it in the job-automate folder with <code>uv run jobautomate-server</code>.";
  });

// Opening the popup on a job form starts the answers, so Fill is quicker.
async function warmActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) return;
  fetch(`${SERVER}/plan/warm`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: tab.url }) }).catch(() => {});
}

function dayKey(date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

async function showCounts() {
  const { applications } = await (await fetch(`${SERVER}/applications`)).json();
  const perDay = {};
  for (const a of applications) perDay[dayKey(new Date(a.applied_at))] = (perDay[dayKey(new Date(a.applied_at))] || 0) + 1;
  const now = new Date();
  today = perDay[dayKey(now)] || 0;

  // Days in a row with at least one application, ending today (or yesterday).
  let streak = 0;
  let cursor = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (today ? 0 : 1));
  while (perDay[dayKey(cursor)]) {
    streak++;
    cursor = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate() - 1);
  }
  $("streak").textContent = streak ? `${streak} day streak` : "No streak yet";
  const week = applications.filter((a) => Date.now() - new Date(a.applied_at) < 7 * DAY).length;
  $("total").textContent = `${applications.length} in total, ${week} this week`;

  // The last 7 days, today on the right.
  const days = [...Array(7)].map((_, i) => new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6 + i));
  const most = Math.max(1, ...days.map((d) => perDay[dayKey(d)] || 0));
  $("week").replaceChildren(
    ...days.map((d, i) => {
      const count = perDay[dayKey(d)] || 0;
      const column = document.createElement("div");
      column.className = `day${count ? " has" : ""}${i === 6 ? " now" : ""}`;
      column.title = `${count} on ${d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}`;
      const bar = document.createElement("div");
      bar.className = "bar";
      bar.style.height = `${(count / most) * 100}%`;
      column.append(count ? String(count) : "", bar, d.toLocaleDateString(undefined, { weekday: "narrow" }));
      return column;
    }),
  );
  drawRing();
}

function drawRing() {
  $("today").textContent = today;
  $("goal").textContent = goal;
  $("headline").textContent = today >= goal ? "Goal reached today" : "Applied today";
  const progress = $("progress");
  progress.classList.toggle("done", today >= goal);
  progress.style.strokeDasharray = CIRCUMFERENCE;
  progress.style.strokeDashoffset = CIRCUMFERENCE * (1 - Math.min(today / goal, 1));
}

// ---- settings kept in Chrome (goal and the autofill switch) ----

chrome.storage.local.get({ goal: 20, autoFill: false }, (saved) => {
  goal = saved.goal;
  $("auto").checked = saved.autoFill;
  drawRing();
});

$("auto").addEventListener("change", (event) => {
  const on = event.target.checked;
  chrome.storage.local.set({ autoFill: on });
  statusLine.textContent = on
    ? "Application forms will now fill by themselves when you open them. You still press Submit yourself."
    : "Forms will only fill when you click Fill this application.";
});

function editGoal() {
  const input = document.createElement("input");
  input.className = "goal-input";
  input.type = "number";
  input.min = 1;
  input.value = goal;
  const button = $("goal");
  button.replaceWith(input);
  input.focus();
  input.select();
  const done = () => {
    const value = Math.max(1, parseInt(input.value, 10) || goal);
    goal = value;
    chrome.storage.local.set({ goal: value });
    input.replaceWith(button);
    drawRing();
  };
  input.addEventListener("blur", done);
  input.addEventListener("keydown", (event) => event.key === "Enter" && input.blur());
}
$("goal").addEventListener("click", editGoal);

// ---- actions ----

$("fill").addEventListener("click", async () => {
  $("fill").disabled = true;
  statusLine.textContent = "Filling the form. The first time for a job this can take a few seconds while answers are written.";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  // On sites the extension isn't set up for, load it into the page first.
  await new Promise((resolve) => chrome.runtime.sendMessage({ type: "inject", tabId: tab.id }, resolve));
  chrome.tabs.sendMessage(tab.id, { type: "fill" }, (reply) => {
    $("fill").disabled = false;
    if (chrome.runtime.lastError || !reply) {
      statusLine.textContent =
        "I couldn't find an application form on this page. Open the job's application form (often behind an Apply button) and try again.";
      return;
    }
    statusLine.textContent = reply.summary;
  });
});

// For jobs where the Submit click wasn't caught, or that you applied to without the extension.
$("applied").addEventListener("click", async () => {
  $("applied").disabled = true;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/.test(tab.url || "")) {
    statusLine.textContent = "Open the job's page first, then click Mark applied. Or add it by hand on the Dashboard.";
    $("applied").disabled = false;
    return;
  }
  statusLine.textContent = "Saving...";
  // The page's text is kept as the job description when the job board can't be read.
  let pageText = "";
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => (document.querySelector("main") || document.body).innerText.slice(0, 30000),
    });
    pageText = result.result || "";
  } catch {
    pageText = "";
  }
  try {
    const response = await fetch(`${SERVER}/applications`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: tab.url, page_title: tab.title || "", page_text: pageText }),
    });
    const application = await response.json();
    if (!response.ok) throw new Error(application.detail || response.status);
    const name = [application.role, application.company].filter(Boolean).join(" at ") || "this job";
    statusLine.textContent = application.already
      ? `${name} is already in your tracker.`
      : `Saved ${name} as applied. You can change its details on the Dashboard.`;
    showCounts();
  } catch (error) {
    statusLine.textContent = `Could not save it: ${error.message}`;
  }
  $("applied").disabled = false;
});

$("find").addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("jobs.html") });
});

fetch(`${SERVER}/jobs/progress`)
  .then((response) => response.json())
  .then(({ counts, running }) => {
    const badge = $("new-jobs");
    if (running) badge.textContent = "scanning";
    else if (counts.today) badge.textContent = `${counts.today} new today`;
    badge.hidden = !running && !counts.today;
  })
  .catch(() => {});

$("dashboard").addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("dashboard.html") });
});
