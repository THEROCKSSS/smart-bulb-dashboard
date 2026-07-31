# Running as a Windows service (W2-177)

Goal: the dashboard starts with the machine, restarts if it crashes, and
runs with no console window and nobody logged in. Windows has no systemd,
so this uses [NSSM](https://nssm.cc/) (the Non-Sucking Service Manager) to
wrap the uvicorn process as a real service.

Not verified end to end by this project — the Windows testing here has all
been "run it in a terminal". The NSSM commands are the standard ones and
the flags match what `deploy/systemd/smart-bulb-dashboard.service` does on
Linux, but treat the first install as something to check rather than
assume, and run the smoke test afterwards.

## Install

1. Download NSSM from <https://nssm.cc/download>, unzip, and use the
   `win64\nssm.exe` binary. Nothing to install.

2. From an **Administrator** PowerShell:

```powershell
$repo = "C:\smart-bulb-dashboard"
$py   = "$repo\backend\venv\Scripts\python.exe"

nssm install SmartBulbDashboard $py
nssm set SmartBulbDashboard AppParameters "-m uvicorn main:app --host 127.0.0.1 --port 8500 --no-proxy-headers"
nssm set SmartBulbDashboard AppDirectory "$repo\backend"
nssm set SmartBulbDashboard DisplayName "Smart Bulb Dashboard"
nssm set SmartBulbDashboard Description "Local REST API and web UI for Tuya smart bulbs"
nssm set SmartBulbDashboard Start SERVICE_AUTO_START
```

`AppDirectory` must be `backend\`, not the repo root — `main:app` is
imported relative to the working directory.

`--no-proxy-headers` is not optional if you put a reverse proxy in front.
uvicorn otherwise rewrites the client address from `X-Forwarded-For` before
the app sees it, which takes the decision out of `SBD_TRUSTED_PROXIES`'s
hands. See §3.2 of [`../docs/deployment.md`](../docs/deployment.md).

3. Restart on crash, matching the Linux unit's `Restart=always` /
   `RestartSec=5`:

```powershell
nssm set SmartBulbDashboard AppExit Default Restart
nssm set SmartBulbDashboard AppRestartDelay 5000
# Give up if it dies within 10s of starting, 5 times running -- that's a
# broken config, not a transient fault, and a crash loop makes the logs
# unreadable.
nssm set SmartBulbDashboard AppThrottle 10000
```

4. Logs. There's no journalctl, so send stdout/stderr to files and let
   NSSM rotate them:

```powershell
New-Item -ItemType Directory -Force "$repo\logs" | Out-Null
nssm set SmartBulbDashboard AppStdout "$repo\logs\service.out.log"
nssm set SmartBulbDashboard AppStderr "$repo\logs\service.err.log"
nssm set SmartBulbDashboard AppRotateFiles 1
nssm set SmartBulbDashboard AppRotateBytes 10485760
```

`logs/` sits under the repo, and `.gitignore` already excludes `*.log`.

5. Environment (the reverse-proxy and TLS settings). NSSM takes
   `NAME=VALUE` pairs separated by newlines:

```powershell
nssm set SmartBulbDashboard AppEnvironmentExtra "PYTHONUNBUFFERED=1`nSBD_TRUSTED_PROXIES=127.0.0.1,::1"
```

Set `SBD_TRUSTED_PROXIES` **only** if a reverse proxy really is in front.
On a direct LAN bind it would let anything on the network forge
`X-Forwarded-For` and walk past the PIN gate's per-IP lockout.

6. Start it and confirm:

```powershell
nssm start SmartBulbDashboard
Get-Service SmartBulbDashboard
python "$repo\deploy\smoke-test.py" --base-url http://127.0.0.1:8500
```

## Which account it runs as

By default the service runs as `LocalSystem`, which is fine for the API and
for controlling bulbs.

**Audio-reactive lighting is the exception.** `LocalSystem` has no desktop
audio session, so capturing "whatever is playing on the speakers" (a
loopback/stereo-mix device) will not work — the session starts, reports
itself running, and never reacts to sound. If that's your use case, run it
as your own user:

```powershell
nssm set SmartBulbDashboard ObjectName ".\YourUsername" "YourPassword"
```

That stores a password in the service configuration. If you'd rather not,
the honest alternative is a Startup-folder shortcut or a Scheduled Task
running at logon as you — you lose "runs with nobody logged in", which was
never true for desktop audio capture anyway.

## Managing it

```powershell
nssm restart SmartBulbDashboard
nssm stop SmartBulbDashboard
nssm edit SmartBulbDashboard      # GUI for everything above
nssm remove SmartBulbDashboard confirm
```

## Firewall

Binding `127.0.0.1` (as above) needs no firewall rule and is correct when a
reverse proxy fronts it. For direct LAN access, bind `0.0.0.0` and open the
port to the local subnet **only**:

```powershell
New-NetFirewallRule -DisplayName "Smart Bulb Dashboard" -Direction Inbound `
  -Protocol TCP -LocalPort 8500 -RemoteAddress LocalSubnet -Action Allow
```

`-RemoteAddress LocalSubnet` is the part that matters. Without it the rule
also applies to any public network the machine later joins.

## Alternatives

- **Docker Desktop** with `docker-compose.yml` and `restart: unless-stopped`
  handles restart-on-boot without NSSM. Note that `network_mode: host`
  doesn't work usefully on Docker Desktop for Windows, so Tuya
  auto-discovery (`/rescan`) won't find devices — direct control still
  works fine.
- **Task Scheduler**, "At startup", "Run whether user is logged on or not".
  Workable, but it has no crash-restart equivalent, which is the main
  reason to bother with a service at all.
