"""Opt-in Chromium journey through the actual API and isolated saved state.

SBD_BROWSER_TEST=1 python -m pytest backend/tests/test_mixer_browser.py
"""
import json
import os
from pathlib import Path
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("SBD_BROWSER_TEST") != "1", reason="opt-in Chromium journey")
ROOT = Path(__file__).resolve().parents[2]


def test_lighting_mix_and_named_preset_survive_browser_reload(client):
    from playwright.sync_api import sync_playwright, expect

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def translate_path(self, path):
            path = path.split("?")[0]
            return str(ROOT / "frontend" / (path.removeprefix("/static/") if path.startswith("/static/") else "index.html"))

        def do_GET(self):
            if self.path.startswith("/api/"):
                return self.forward()
            return super().do_GET()

        def do_POST(self):
            self.forward()

        do_PATCH = do_POST
        do_DELETE = do_POST

        def forward(self):
            # Streaming telemetry is covered separately; this journey edits a
            # stopped mixer and must not start real background/hardware workers.
            if self.path.startswith("/api/stream"):
                self.send_response(204); self.end_headers(); return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            response = client.request(self.command, self.path, content=body,
                                      headers={"Content-Type": "application/json"})
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers(); self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/#/audio/session")
            expect(page.get_by_role("heading", name="Lighting mixer", exact=True)).to_be_visible()
            page.screenshot(path=str(ROOT / '.state' / 'web-audio-studio.png'), full_page=True, animations='disabled')
            bass = page.get_by_role("slider", name="Bass gain")
            bass.evaluate("e => { e.value = '2.25'; e.dispatchEvent(new Event('input', {bubbles:true})); e.dispatchEvent(new Event('change', {bubbles:true})); }")
            expect(page.locator(".mix-save")).to_have_text("Saved")
            assert client.get("/api/devices/bulb-1/audio-reactive/settings").json()["settings"]["band_gains"] == [2.25, 1, 1]
            page.reload()
            expect(page.get_by_role("slider", name="Bass gain")).to_have_value("2.25")
            page.get_by_role("textbox", name="Mix name", exact=True).fill("Evening mix")
            page.get_by_role("button", name="Save mix", exact=True).click()
            expect(page.get_by_role("button", name="Evening mix", exact=True)).to_be_visible()
            page.get_by_role("button", name="Rename Evening mix", exact=True).click()
            name = page.get_by_role("textbox", name="Rename mix", exact=True)
            name.fill("Quiet evening"); name.press("Enter")
            expect(page.get_by_role("button", name="Quiet evening", exact=True)).to_be_visible()
            page.reload()
            expect(page.get_by_role("button", name="Quiet evening", exact=True)).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert errors == []
            browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
