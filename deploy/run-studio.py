"""Supervise the API and Expo Go bundle server in a single app container."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    stopping = False
    def shutdown(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    env = {**os.environ, "CI": "1", "EXPO_NO_TELEMETRY": "1", "EXPO_NO_DEPENDENCY_VALIDATION": "1"}
    services = [
        {"name": "API", "cwd": ROOT / "backend", "cmd": [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8500", "--no-proxy-headers"]},
        {"name": "Expo", "cwd": ROOT / "mobile", "cmd": ["node", "node_modules/expo/bin/cli", "start", "--go", "--host", "lan", "--port", "8081"]},
    ]
    try:
        while not stopping:
            for service in services:
                process = service.get("process")
                if process and process.poll() is None:
                    continue
                now = time.monotonic()
                if process:
                    print(f"{service['name']} exited ({process.returncode}); restarting", flush=True)
                    service["process"] = None
                    service["retry"] = now + 3
                if now < service.get("retry", 0):
                    continue
                service["process"] = subprocess.Popen(service["cmd"], cwd=service["cwd"], env=env, start_new_session=True)
                print(f"{service['name']} started", flush=True)
            time.sleep(.5)
    finally:
        for service in services:
            process = service.get("process")
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for service in services:
            process = service.get("process")
            if process:
                try: process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


if __name__ == "__main__":
    main()
