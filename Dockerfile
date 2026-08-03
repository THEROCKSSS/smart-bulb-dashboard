FROM python:3.11-slim

WORKDIR /app

# libportaudio2 is not optional. sounddevice loads the PortAudio shared
# library via CFFI at *import* time, and backend/main.py imports
# audio_reactive at module scope (main.py:29), which imports sounddevice at
# module scope (audio_reactive.py:12). python:3.11-slim ships no audio
# libraries, so without this the container dies during startup with an
# OSError before uvicorn ever binds -- it does not fail lazily when someone
# starts an audio session.
#
# Installing it makes the app *import*; it does not make audio capture work.
# A Linux container on Docker Desktop for Windows has no access to the host's
# audio devices, so audio-reactive lighting has no input device to open. See
# deploy/windows-service.md for the deployment that does support it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libportaudio2 \
 && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY frontend ./frontend

# The in-app documentation browser (System -> Docs) reads real markdown off
# disk via backend/docs_library.py, whose DOC_ROOTS are docs/, the project
# root and iterations/. None of that was in the image, so the browser was
# silently EMPTY in every container deployment -- it only ever worked when the
# backend ran from a host checkout. Found by querying docs_library inside the
# running container and getting `{"categories": [], "total": 0}`.
#
# Markdown only, and only the directories that are actually indexed: this is
# documentation for the person using the app, not the repo.
COPY docs ./docs
COPY iterations ./iterations
COPY *.md ./

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
