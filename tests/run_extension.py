"""Runs the extension's content scripts inside a real Greenhouse or Ashby form, without installing the extension.

Branded Chrome no longer loads unpacked extensions under automation, so this
stubs chrome.runtime, forwards API calls to the local server, and triggers a
fill. The local server must be running. Nothing is submitted, and /log calls
are dropped so test runs don't count as applications.

    uv run python tests/run_extension.py <application form url> [--show]
"""

import json
import sys
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SERVER = "http://127.0.0.1:8765"

STUB = """
window.chrome = window.chrome || {};
window.__listeners = [];
chrome.runtime = {
  sendMessage: (message, callback) => { if (message.type !== 'api') return callback && callback(null); window.__jaApi(JSON.stringify(message)).then(r => callback && callback(JSON.parse(r))); },
  onMessage: { addListener: (fn) => window.__listeners.push(fn) },
  lastError: undefined,
};
chrome.storage = { local: { get: (defaults, callback) => (callback ? callback(defaults) : Promise.resolve(defaults)), set: () => {} } };
"""

TRIGGER = """
() => new Promise(resolve => {
  for (const fn of window.__listeners) {
    if (fn({ type: "fill" }, {}, resolve) === true) return;
  }
  resolve({ summary: "no listener took the fill message" });
})
"""


def api(message_json: str) -> str:
    message = json.loads(message_json)
    if message.get("path") == "/log":
        return json.dumps({"ok": True, "status": 200, "data": {}})
    response = httpx.post(SERVER + message["path"], json=message["body"], timeout=180) if message.get("body") else httpx.get(SERVER + message["path"])
    return json.dumps({"ok": response.is_success, "status": response.status_code, "data": response.json()})


def main() -> None:
    url = sys.argv[1]
    show = "--show" in sys.argv
    with sync_playwright() as p:
        # Chrome's new headless mode passes most bot checks; --show opens a visible window.
        browser = p.chromium.launch(channel="chrome", headless=not show)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.expose_function("__jaApi", api)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.locator('[id="first_name"], [data-field-path], input[name="email"], [data-testid="input-first_name"], input[type="email"], input[type="file"]').first.wait_for(state="attached", timeout=30000)
        page.wait_for_timeout(3000)
        page.evaluate(STUB)
        manifest = json.loads((ROOT / "extension" / "manifest.json").read_text())
        scripts = manifest["content_scripts"][0]["js"]
        page.evaluate("\n".join((ROOT / "extension" / name).read_text() for name in scripts))
        result = page.evaluate(TRIGGER)
        print(result["summary"])
        page.screenshot(path=str(ROOT / "data" / "extension_test.png"), full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
