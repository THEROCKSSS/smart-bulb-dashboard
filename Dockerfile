FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY frontend ./frontend

WORKDIR /app/backend
EXPOSE 8500

# W2-186. Also declared in docker-compose.yml (compose's version wins when
# it's set); this one covers `docker run` without compose. Probes /healthz
# rather than /api/system/health -- see backend/main.py for why they're
# separate. Python, not curl/wget: python:3.11-slim ships neither.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8500/healthz', timeout=5).status == 200 else 1)"

# --no-proxy-headers is deliberate. uvicorn enables its own
# ProxyHeadersMiddleware by default, trusting 127.0.0.1, and it rewrites
# the request's client address from X-Forwarded-For before the app is
# reached -- so anything able to talk to the port from loopback can hand
# itself an arbitrary source IP and get a fresh per-IP lockout bucket.
# The app does this properly instead, gated on an explicit
# SBD_TRUSTED_PROXIES list (see backend/reverse_proxy.py). Leaving both
# layers on would mean the outer, unconfigurable one silently wins.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8500", "--no-proxy-headers"]
