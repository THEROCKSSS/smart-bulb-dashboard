"""Browser regression checks. Fixture API never sends commands to real bulbs.

Run with a Python environment containing playwright: python -B tools/verify-frontend.py
"""
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?")[0]
        return str(ROOT / "frontend" / (path.removeprefix("/static/") if path.startswith("/static/") else "index.html"))

    def log_message(self, *args):
        pass


def run():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = os.environ.get("SBD_VERIFY_URL", f"http://127.0.0.1:{server.server_port}").rstrip("/")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        status = {"online": True, "power": False, "mode": "colour", "hue": 32,
                  "saturation_pct": 70, "value_pct": 48, "brightness_pct": 48, "color_temp_pct": 40}
        held_status = []
        held_power = []
        commands = []
        hold = True
        offline = False
        no_devices = False
        def api(route):
            path = route.request.url.split(base)[-1]
            if route.request.method == "POST":
                data = route.request.post_data_json
                commands.append((path, data))
                if path.endswith("/power"):
                    held_power.append((route, data))
                    return
                if path.endswith("/brightness"):
                    status["value_pct"] = data["value"]
                if path.endswith("/white"):
                    status.update(mode="white", color_temp_pct=data["color_temp"])
                body = {"result": {"dps": {}}}
            elif path.endswith("/status") and "/devices/" in path and "/audio-reactive/" not in path:
                if hold:
                    held_status.append(route)
                    return
                body = {"online": False} if offline else status
            elif path == "/api/devices":
                body = [] if no_devices else [{"id": "bulb-1", "name": "Studio bulb"}, {"id": "bulb-2", "name": "Desk bulb"}]
            elif path == "/api/auth/status":
                body = {"enabled": False, "authenticated": True}
            elif path == "/api/system/remote-access/status":
                body = {"warnings": []}
            elif path == "/api/stream":
                route.fulfill(status=200, content_type="text/event-stream", body="")
                return
            elif path == "/api/scenes":
                body = [{"id":"reading","name":"Reading","description":"Soft white light for a quiet chapter."}, {"id":"evening","name":"Evening","description":"Warm light for winding down."}]
            elif path == "/api/effects":
                body = [{"id":"candle","name":"Candle","description":"A soft, flickering glow."}]
            elif path == "/api/presets":
                body = [{"id":"amber","name":"Amber","rgb":[255,180,90]}]
            elif path == "/api/docs":
                body = {"categories": [], "total": 0}
            elif path == "/api/audio/devices":
                body = {"devices": []}
            elif path == "/api/audio/presets":
                body = {"presets": []}
            elif path == "/api/audio/bridge":
                body = {"listening": False, "connected": False}
            elif "/audio-reactive/status" in path or "/timers/" in path:
                body = {"active": False}
            else:
                body = []
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        page.route("**/api/**", api)
        page.goto(base)
        # Physical I/O may take seconds. The control surface must render first.
        expect(page.locator("#brightness-slider")).to_be_visible(timeout=1500)
        print("PASS: controls render before hardware status returns")
        hold = False
        for route in held_status:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(status))
        expect(page.locator("#power-toggle")).to_be_enabled()
        page.locator("#power-toggle").click()
        expect(page.locator("#power-toggle")).to_have_text("Confirming…")
        expect(page.locator("#light-stage")).to_have_class("light-stage is-on is-pending")
        assert len(held_power) == 1
        route, data = held_power.pop()
        status["power"] = data["on"]
        route.fulfill(status=200, content_type="application/json", body='{"result": {"dps": {"20": true}}}')
        expect(page.locator("#power-toggle")).to_have_text("Turn off")
        print("PASS: immediate feedback followed by hardware confirmation")
        page.locator("#power-toggle").click()
        route, _ = held_power.pop()
        route.fulfill(status=200, content_type="application/json", body='{"result": {"Error": "fixture rejection"}}')
        expect(page.locator("#power-toggle")).to_have_text("Turn off")
        expect(page.locator(".toast.error")).to_contain_text("rejected")
        print("PASS: failed physical command rolls back; no false success")
        page.locator("#brightness-slider").focus()
        page.keyboard.press("ArrowRight")
        expect(page.locator("#brightness-val")).to_have_text("49%")
        assert commands[-1][1] == {"value":49}
        page.locator("#temp-slider").focus()
        page.keyboard.press("ArrowRight")
        page.locator("#apply-white").focus()
        # Allow the regular status poll to arrive while an unapplied draft exists.
        page.wait_for_timeout(4200)
        expect(page.locator("#temp-slider")).to_have_value("41")
        page.locator("#apply-white").click()
        assert commands[-1][1]["color_temp"] == 41
        print("PASS: native keyboard sliders and white-temperature draft survive polling")
        # Screenshots explicitly identify synthetic data; fixtures never reach the real bulb.
        evidence = ROOT / "docs" / "screenshots"
        page.evaluate("document.querySelector('.topbar-note').textContent='NON-LIVE DATA · Browser verification fixture'")
        page.locator("#main").evaluate("e => e.scrollTop = 0")
        page.screenshot(path=str(evidence / "control.png"), full_page=True)
        print("PERF:", json.dumps(page.evaluate("({fcp: performance.getEntriesByName('first-contentful-paint')[0]?.startTime, dom: performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd})")))
        for route_name, heading in [("light/looks","Scenes & Effects"),("light/presets","Presets"),("audio/session","Audio Reactive"),("automation","Automation"),("rooms","Rooms"),("system/history","History"),("system/docs","Documentation")]:
            page.goto(base + "/#/" + route_name)
            expect(page.locator("#main")).to_contain_text(heading)
            expect(page.locator("#main")).not_to_contain_text("Failed to load")
            if route_name == "light/looks":
                page.get_by_role("button", name="Reading Soft white").click()
                assert commands[-1][1] == {"scene_id":"reading"}
            if route_name == "light/presets":
                page.get_by_role("button", name="Amber", exact=True).click()
                assert commands[-1][1] == {"preset_id":"amber"}
            print("PASS: route", route_name)
        # Quick power works and stays current even with keyboard focus on another page.
        expect(page.locator("#qc-power")).to_have_text("Turn off")
        page.locator("#qc-power").click()
        expect(page.locator("#qc-power")).to_have_text("Confirming…")
        route, data = held_power.pop()
        status["power"] = False
        route.fulfill(status=200, content_type="application/json", body='{"result": {"dps": {"20": false}}}')
        expect(page.locator("#qc-power")).to_have_text("Turn on")
        expect(page.locator("#qc-power")).to_be_focused()
        page.locator("#qc-scene-link").focus()
        page.wait_for_timeout(4200)
        expect(page.locator("#qc-scene-link")).to_be_focused()
        page.locator(".skip-link").focus()
        page.keyboard.press("Enter")
        assert page.url.endswith("#/system/docs")
        print("PASS: persistent quick power and skip navigation")
        for width, height in [(1440,1000),(1024,900),(768,1024),(390,844),(320,780)]:
            page.set_viewport_size({"width":width,"height":height})
            page.goto(base + "/#/light/control")
            expect(page.locator("#power-toggle")).to_be_enabled()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert page.locator("#main").evaluate("e => e.scrollWidth <= e.clientWidth"), width
            for label in ["Light","Audio","Automation","Rooms","System"]:
                expect(page.get_by_role("navigation",name="Main navigation").get_by_role("link",name=label,exact=True)).to_be_visible()
            if width == 390:
                page.screenshot(path=str(evidence / "control-mobile.png"), full_page=True)
            print("PASS: layout", width, height)
        page.emulate_media(reduced_motion="reduce")
        assert page.locator("#subtab-panel").evaluate("e=>getComputedStyle(e).animationName") == "none"
        offline = True
        page.reload()
        expect(page.locator("#control-source")).to_have_text("OFFLINE")
        expect(page.locator("#power-toggle")).to_be_disabled()
        offline = False
        page.locator("#control-retry").click()
        expect(page.locator("#power-toggle")).to_be_enabled()
        page.locator("#device-select").select_option("bulb-2")
        page.reload()
        expect(page.locator("#device-select")).to_have_value("bulb-2")
        no_devices = True
        page.goto(base + "/#/light/control")
        page.reload()
        expect(page.locator("#main")).to_contain_text("No lights connected yet")
        page.goto(base + "/#/system/docs")
        expect(page.locator("#main")).to_contain_text("Documentation")
        print("PASS: reduced motion, offline retry, device persistence, empty-device recovery")
        assert not errors, errors
        print("PASS: zero browser runtime errors; all control requests used fixtures")
        if os.environ.get("SBD_VERIFY_URL"):
            live_page = browser.new_page(viewport={"width":1440,"height":1000})
            live_errors = []
            external = []
            live_page.on("pageerror", lambda error: live_errors.append(str(error)))
            live_page.on("request", lambda request: external.append(request.url) if not request.url.startswith(base + "/") else None)
            live_page.goto(base)
            # Read-only real API check. Never submit real control or login mutations.
            auth = live_page.request.get(base + "/api/auth/status").json()
            if not auth.get("enabled"):
                routes = [("light/control","Set the mood"),("light/looks","Scenes & Effects"),
                          ("light/presets","Presets & Favorites"),("audio/session","Audio Reactive"),
                          ("audio/presets","Session Presets"),("automation","Automation"),("rooms","Rooms"),
                          ("system/history","History"),("system/health","System Health"),
                          ("system/diagnostics","Diagnostics"),("system/security","Security Log"),
                          ("system/backup","Backup & Restore"),("system/settings","Settings"),("system/docs","Documentation")]
                for route_name, title in routes:
                    live_page.goto(base + "/#/" + route_name)
                    expect(live_page.locator("#main .panel-title").first).to_contain_text(title,timeout=15000)
                    expect(live_page.locator("#main .empty-state.loading")).to_have_count(0,timeout=15000)
                    expect(live_page.locator("#main")).not_to_contain_text("Failed to load")
                    print("PASS: real API read-only view", route_name)
            live_page.route("**/api/auth/status", lambda r:r.fulfill(status=200, content_type="application/json", body='{"enabled":true,"authenticated":false}'))
            live_page.route("**/api/auth/login", lambda r:r.fulfill(status=401, content_type="application/json", body='{"detail":"Test PIN rejected"}'))
            live_page.reload()
            expect(live_page.get_by_role("heading",name="Welcome home.")).to_be_visible()
            expect(live_page.locator("#pin-input")).to_be_focused()
            assert live_page.locator(".app-shell").evaluate("e=>e.inert")
            live_page.locator("#pin-input").fill("fixture-only")
            live_page.get_by_role("button",name="Unlock your lights").click()
            expect(live_page.locator("#pin-error")).to_have_text("Test PIN rejected")
            expect(live_page.locator("#pin-input")).to_have_value("")
            expect(live_page.locator("#pin-input")).to_be_focused()
            print("PASS: PIN dialog focus, inert background and rejected-login fixture")
            assert not live_errors, live_errors
            assert not external, external
            print("PASS: live browser has no runtime errors or external requests")
            live_page.close()
        browser.close()
    server.shutdown()


if __name__ == "__main__":
    run()
