"""Opens a Chrome window for building support for a site behind a login, and records each application step.

You sign in and click through the steps yourself. Every time the page shows a
new set of fields, the recorder saves the page and a summary of its fields to
data/samples/<site>/. Pages with a password box are never saved. Close the
window when you are done.

The window also listens on port 9222, so other scripts can read the page while
it is open.

    uv run python tests/workday_session.py <job url>
"""

import json
import sys
from urllib.parse import urlparse
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "data" / "browser-profile"

SUMMARY = """() => {
  const visible = (e) => e.offsetParent !== null || e.type === 'file';
  const labelFor = (e) => {
    const box = e.closest('[data-automation-id^="formField-"]') || e.closest('fieldset') || e.parentElement;
    const label = box && box.querySelector('label, legend');
    return label ? label.innerText.replace(/\\s+/g, ' ').trim() : (e.getAttribute('aria-label') || '');
  };
  const fields = [...document.querySelectorAll('input, textarea, select, button[aria-haspopup], [role=combobox], [role=radio], [role=checkbox]')]
    .filter(visible)
    .map((e) => ({
      tag: e.tagName, type: e.type || '', id: e.id, name: e.name || '',
      automation: e.getAttribute('data-automation-id') || '',
      formField: (e.closest('[data-automation-id^="formField-"]') || {}).dataset?.automationId || '',
      role: e.getAttribute('role') || '', label: labelFor(e).slice(0, 160),
      text: (e.innerText || e.value || '').slice(0, 60), required: e.getAttribute('aria-required') || '',
    }));
  const heading = [...document.querySelectorAll('h2, h3, [data-automation-id=progressBarActiveStep]')].map((h) => h.innerText.trim()).filter(Boolean).slice(0, 3);
  return { url: location.href, heading, hasPassword: Boolean(document.querySelector('input[type=password]')), fields };
}"""


def main() -> None:
    host = urlparse(sys.argv[1]).hostname.removeprefix("www.").removeprefix("jobs.")
    OUT = ROOT / "data" / "samples" / ("workday" if "myworkday" in host else "icims" if host.endswith("icims.com") else host.split(".")[0])
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, viewport=None, args=["--remote-debugging-port=9222"]
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(sys.argv[1], wait_until="domcontentloaded", timeout=60000)
        print("Window is open. Sign in and go through the application steps. Close the window when done.", flush=True)
        last, step = None, 0
        while context.pages:
            page = context.pages[-1]
            try:
                page.wait_for_timeout(2000)
                # iCIMS shows its form inside an iframe; record that frame when there is one.
                frame = next((f for f in page.frames if "in_iframe=1" in f.url), page.main_frame)
                summary = frame.evaluate(SUMMARY)
            except Exception:
                continue
            signature = (summary["url"].split("?")[0], tuple(summary["heading"]), tuple(f["label"] for f in summary["fields"]))
            if signature == last or summary["hasPassword"] or not summary["fields"]:
                continue
            last = signature
            step += 1
            name = f"step-{step:02d}"
            (OUT / f"{name}.json").write_text(json.dumps(summary, indent=2))
            (OUT / f"{name}.html").write_text(frame.content())
            print(f"saved {name}: {summary['heading']} ({len(summary['fields'])} fields)", flush=True)


if __name__ == "__main__":
    main()
