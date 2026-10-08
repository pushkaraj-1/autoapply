// LinkedIn: reads the job on the open LinkedIn tab when you click "Queue this job" in
// the extension's popup. It only reads the page, once, at that moment: nothing is
// added to LinkedIn's page and nothing is clicked for you. You then click LinkedIn's
// own Apply button; the background script catches the company page that opens,
// closes it and puts the job in the queue.
//
// liReadJob runs inside the LinkedIn tab (chrome.scripting.executeScript), so it
// must not use anything from outside its own body.
function liReadJob() {
  const clean = (text) => String(text || "").replace(/\s+/g, " ").trim();
  const visible = (e) => Boolean(e && e.offsetParent !== null);
  const fromQuery = new URLSearchParams(location.search).get("currentJobId");
  const fromPath = location.pathname.match(/\/jobs\/view\/(?:[^/]*?-)?(\d{6,})/);
  const id = fromQuery || (fromPath && fromPath[1]) || "";

  // LinkedIn's Apply button ("Apply" goes to the company's site, "Easy Apply" stays).
  const apply = [...document.querySelectorAll("button, a")].find((b) => visible(b) && /^(easy apply|apply)$/i.test(clean(b.innerText)));
  if (!apply) return { found: false };
  const label = clean(apply.getAttribute("aria-label"));
  const easy = /easy apply/i.test(`${clean(apply.innerText)} ${label}`);

  // The job's header: the nearest box around Apply holding a heading and the company link.
  let card = apply;
  for (let i = 0; card && i < 14; i++) {
    card = card.parentElement;
    if (card && card.querySelector('a[href*="/company/"]') && card.querySelector("h1, h2")) break;
  }
  card = card || document.body;
  const heading = card.querySelector("h1") || card.querySelector("h2");
  const companyLink = [...card.querySelectorAll('a[href*="/company/"]')].map((a) => clean(a.innerText)).find((t) => t && t.length < 80);
  // "ML Engineer | Proxima | LinkedIn" on a job's own page.
  const parts = clean(document.title.replace(/^\(\d+\)\s*/, "")).split(" | ");
  const title = clean(heading && heading.innerText) || (label.match(/apply to (.+?)(?: on company website)?$/i) || [])[1] || (parts.length >= 3 ? parts[0] : "");
  const company = companyLink || (parts.length >= 3 ? parts[1] : "");
  // "Greater Boston · 1 hour ago · 82 people clicked apply"
  const line = [...card.querySelectorAll("span, div, p")].map((e) => clean(e.innerText)).find((t) => /^[^·]{2,60} · /.test(t) && t.length < 200);
  const description = [...document.querySelectorAll("#job-details, .jobs-description__content, [class*='jobs-description'], article")].map((e) => clean(e.innerText)).sort((a, b) => b.length - a.length)[0] || "";
  return {
    found: true,
    easy,
    listing_url: id ? `https://www.linkedin.com/jobs/view/${id}/` : location.href.split("?")[0],
    title,
    company,
    location: line ? line.split(/\s*·\s*/)[0] : "",
    description: description.slice(0, 20000),
  };
}
