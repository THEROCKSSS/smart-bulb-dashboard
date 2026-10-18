"""Exercise the Expo web build against the real isolated API (no bulb hardware).

Build mobile/dist first; SBD_BROWSER_TEST=1 enables Chromium journeys.
Native rendering on a physical phone remains a separate check.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import pytest

pytestmark = pytest.mark.skipif(os.environ.get('SBD_BROWSER_TEST') != '1', reason='opt-in Chromium journey')
ROOT = Path(__file__).resolve().parents[2]


def test_native_controls_share_settings_and_presets_with_dashboard(client, monkeypatch, tmp_path):
    from playwright.sync_api import sync_playwright, expect
    import main
    import bulb_manager
    from types import SimpleNamespace
    monkeypatch.setattr(bulb_manager, 'FAVORITES_PATH', str(tmp_path / 'favorites.json'))
    bridge = {'connected': True, 'listening': True, 'device_index': 4, 'devices': [
        {'index': 4, 'name': 'Microphone', 'hostapi': 'Windows WASAPI'},
        {'index': 86, 'name': 'CABLE Output (VB-Audio Virtual Cable)', 'hostapi': 'Windows WASAPI'},
    ]}
    def switch(index):
        bridge['device_index'] = index
        return True
    monkeypatch.setattr(main.audio_bridge, 'status', lambda: dict(bridge))
    monkeypatch.setattr(main.audio_bridge, 'get_server', lambda: SimpleNamespace(status=lambda: bridge, request_device=switch))
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / 'mobile' / 'dist')))
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    settings_path = '/api/devices/bulb-1/audio-reactive/settings'
    client.post(settings_path, json={'source': 'bridge', 'band_gains': [1.75, 1, 1], 'mode': 'weighted_blend'})
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 390, 'height': 844}, reduced_motion='reduce')
            errors = []
            unavailable = {'value': True}
            page.on('pageerror', lambda error: errors.append(str(error)))
            def forward(route):
                request = route.request
                from urllib.parse import urlsplit
                url = urlsplit(request.url)
                path = url.path + ('?' + url.query if url.query else '')
                headers = {'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'content-type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,DELETE,OPTIONS', 'Content-Type': 'application/json'}
                if request.method == 'OPTIONS':
                    return route.fulfill(status=204, headers=headers)
                if path.startswith('/api/stream'):
                    return route.fulfill(status=204, headers=headers)
                if unavailable['value']:
                    return route.fulfill(status=503, body='{"detail":"Dashboard unavailable"}', headers=headers)
                response = client.request(request.method, path, content=request.post_data, headers={'Content-Type': 'application/json'})
                headers['Content-Type'] = response.headers.get('Content-Type', 'application/json')
                route.fulfill(status=response.status_code, body=response.content, headers=headers)
            page.route('**/api/**', forward)
            page.route('https://studio.test/**', forward)
            page.goto(origin)
            expect(page.get_by_text('A little atmosphere.', exact=True)).to_be_visible()
            # A failed first connection must not disable address recovery.
            page.get_by_role('tab', name='Connect', exact=True).click()
            page.get_by_role('button', name='Dashboard connection', exact=True).click()
            page.get_by_role('textbox', name='Dashboard address', exact=True).fill('https://studio.test')
            expect(page.get_by_role('button', name='Save connection', exact=True)).to_be_enabled()
            unavailable['value'] = False
            page.get_by_role('button', name='Save connection', exact=True).click()
            expect(page.get_by_role('button', name='Windows audio input', exact=True)).to_be_enabled()
            page.get_by_role('button', name='Windows audio input', exact=True).click()
            page.get_by_role('button', name='CABLE Output (VB-Audio Virtual Cable) · Windows WASAPI', exact=True).click()
            expect(page.get_by_text('Selected: CABLE Output (VB-Audio Virtual Cable)', exact=True)).to_be_visible()
            assert bridge['device_index'] == 86
            saved = client.get(settings_path).json()['settings']
            assert saved['source_device_name'] == 'CABLE Output (VB-Audio Virtual Cable)'
            assert saved['device_index'] == 0
            page.get_by_role('tab', name='Room', exact=True).click()
            expect(page.get_by_role('button', name='Ocean', exact=True)).to_be_enabled()
            page.get_by_text('Your color, your room', exact=True).scroll_into_view_if_needed()
            page.screenshot(path=str(ROOT / '.state' / 'mobile-color-wheel.png'), full_page=True)
            page.get_by_role('button', name='Ocean', exact=True).click()
            expect(page.get_by_role('button', name='Ocean', exact=True)).to_contain_text('Ocean \u2713')
            status = client.get('/api/devices/bulb-1/status').json()
            assert abs(status['hue'] - 205) < 2
            assert page.get_by_test_id('swatch-Ocean').evaluate('e => getComputedStyle(e).backgroundColor') == 'rgb(64, 175, 255)'
            wheel = page.get_by_test_id('color-wheel')
            wheel.scroll_into_view_if_needed()
            box = wheel.bounding_box()
            page.mouse.click(box['x'] + box['width'] * .8, box['y'] + box['height'] * .5)
            expect(page.get_by_role('button', name='Ocean', exact=True)).not_to_contain_text('Ocean \u2713')
            assert client.get('/api/devices/bulb-1/status').json()['hue'] < 3
            page.get_by_role('button', name='Colors & favorites', exact=True).click()
            page.get_by_role('textbox', name='Favorite color name', exact=True).fill('Phone red')
            page.get_by_role('button', name='Save current color', exact=True).click()
            expect(page.get_by_role('button', name='Phone red', exact=True)).to_be_visible()
            page.get_by_role('button', name='Phone red', exact=True).click()
            expect(page.get_by_role('button', name='Phone red', exact=True)).to_be_enabled()
            assert not page.get_by_text('Request failed (404)', exact=True).count()
            page.get_by_role('button', name='Light bulb', exact=True).click()
            page.get_by_role('button', name='Bedroom Bulb', exact=True).click()
            expect(page.get_by_role('button', name='Ocean', exact=True)).to_be_enabled()
            page.get_by_role('button', name='Ocean', exact=True).click()
            expect(page.get_by_role('button', name='Ocean', exact=True)).to_contain_text('Ocean \u2713')
            assert abs(client.get('/api/devices/bulb-2/status').json()['hue'] - 205) < 2
            assert client.get('/api/devices/bulb-1/status').json()['hue'] < 3
            page.reload()
            expect(page.get_by_role('button', name='Light bulb', exact=True)).to_contain_text('Bedroom Bulb')
            page.get_by_role('button', name='Light bulb', exact=True).click()
            page.get_by_role('button', name='Living Room Bulb', exact=True).click()
            page.get_by_role('tab', name='Audio', exact=True).click()
            expect(page.get_by_text('Lighting mixer', exact=True)).to_be_visible()
            expect(page.get_by_text('1.75×', exact=True)).to_be_visible()
            page.get_by_role('button', name='Beat response', exact=True).click()
            page.get_by_role('button', name='Aggressive', exact=True).click()
            expect(page.get_by_text('Saved', exact=True)).to_be_visible()
            assert client.get(settings_path).json()['settings']['beat_sensitivity'] == 'aggressive'
            assert page.get_by_role('tab', name='Mixer', exact=True).count() == 0
            assert page.get_by_role('tab', name='Mixes', exact=True).count() == 0
            page.get_by_role('textbox', name='Mix name', exact=True).fill('Phone evening')
            page.get_by_role('button', name='Save mix', exact=True).click()
            expect(page.get_by_role('button', name='Recall Phone evening', exact=True)).to_be_visible()
            mix = client.get('/api/audio/session-presets?device_id=bulb-1').json()[0]
            assert mix['config']['band_gains'] == [1.75, 1, 1]
            assert mix['config']['source'] == 'bridge'
            page.reload()
            page.get_by_role('tab', name='Audio', exact=True).click()
            expect(page.get_by_role('button', name='Recall Phone evening', exact=True)).to_be_visible()
            # A desktop edit must reach an already-open phone without reloading.
            client.post(settings_path, json={'band_gains': [2.25, 1, 1]})
            expect(page.get_by_text('2.25×', exact=True)).to_be_visible(timeout=10000)
            page.get_by_role('button', name='Starting mix', exact=True).click()
            page.get_by_role('button', name='Chill / Ambient', exact=True).click()
            page.get_by_role('button', name='Use Chill / Ambient', exact=True).click()
            expect(page.get_by_text('Saved', exact=True)).to_be_visible()
            assert client.get(settings_path).json()['settings']['mode'] == 'breathing_silence'
            page.get_by_role('button', name='Fine tune', exact=True).click()
            page.get_by_role('button', name='Session & gentle lighting', exact=True).click()
            duration = page.get_by_role('textbox', name='Session duration (seconds)', exact=True)
            duration.fill('180'); duration.press('Tab')
            expect(page.get_by_text('Saved', exact=True)).to_be_visible()
            assert client.get(settings_path).json()['settings']['max_duration_s'] == 180
            duration.fill(''); duration.press('Tab')
            expect(page.get_by_text('Saved', exact=True)).to_be_visible()
            assert client.get(settings_path).json()['settings']['max_duration_s'] is None
            page.screenshot(path=str(ROOT / '.state' / 'mobile-mixer.png'), full_page=True)
            assert errors == []
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('tab', name='Tools', exact=True).click()
            page.get_by_role('button', name='Timers & schedules', exact=True).click()
            page.get_by_role('button', name='Continue to dashboard tools', exact=True).click()
            expect(page.locator('#device-select')).to_have_value('bulb-1')
            expect(page.locator('#main')).to_contain_text('Sleep')
            page.locator('#device-select').select_option('bulb-2')
            page.reload()
            expect(page.locator('#device-select')).to_have_value('bulb-2')
            assert 'device=' not in page.url
            assert errors == []
            browser.close()
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=2)
