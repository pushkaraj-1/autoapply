"""Runs the extension's content scripts in the open window from tests/workday_session.py.

Used for sites behind a login or captcha (Workday, iCIMS). The scripts are wrapped in a function
so they can be injected again after code changes. The local server must be
running. /log calls are dropped so test runs don't count as applications.

    uv run python tests/run_extension_live.py
"""

import json
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
    response = httpx.post(SERVER + message["path"], json=message["body"], timeout=300) if message.get("body") is not None else httpx.get(SERVER + message["path"])
    return json.dumps({"ok": response.is_success, "status": response.status_code, "data": response.json()})


def main() -> None:
    with sync_playwright() as p:
        page = p.chromium.connect_over_cdp("http://127.0.0.1:9222").contexts[0].pages[-1]
        try:
            page.expose_function("__jaApi", api)
        except Exception:
            pass  # already exposed from an earlier run
        manifest = json.loads((ROOT / "extension" / "manifest.json").read_text())
        source = "\n".join((ROOT / "extension" / name).read_text() for name in manifest["content_scripts"][0]["js"])
        # iCIMS shows its form inside an iframe; run there when there is one.
        frame = next((f for f in page.frames if "in_iframe=1" in f.url), page.main_frame)
        frame.evaluate(STUB)
        frame.evaluate(f"(() => {{\n{source}\n}})()")
        result = frame.evaluate(TRIGGER)
        print(result["summary"])
        page.screenshot(path=str(ROOT / "data" / "extension_live.png"), full_page=True)


if __name__ == "__main__":
    main()
