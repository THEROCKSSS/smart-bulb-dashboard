"""Both application servers must answer for the shared container to be healthy."""
import urllib.request
for url in ('http://127.0.0.1:8500/healthz', 'http://127.0.0.1:8081/status'):
    with urllib.request.urlopen(url, timeout=5) as response:
        if response.status != 200:
            raise SystemExit(1)
