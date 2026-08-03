<#
.SYNOPSIS
    Switch the dashboard between container mode and native audio mode.

.DESCRIPTION
    Audio-reactive lighting cannot work while the backend runs in a Linux
    container on Docker Desktop for Windows: the container has no access to
    host audio devices, so a session starts, reports itself running, and never
    reacts to sound. The bridge (tools/sbd-audio-bridge.py) solves that for
    everyday use by streaming PCM in over loopback TCP.

    This script is the other path -- the one with zero added capture latency,
    for judgement work against real hardware. Above all tuning the 24 genre
    presets by ear, which needs the shortest possible path from sound to bulb
    to be trustworthy.

    Doing this by hand means: stop the container, remember the exact uvicorn
    invocation, remember --no-proxy-headers, and remember to start the
    container again afterwards. That last step is the one that gets forgotten,
    and forgetting it leaves the dashboard silently down.

    So the container is restored in a `finally` block. Ctrl-C, uvicorn
    crashing, a bad config -- every exit path goes through the same restore.
    A state file survives even a hard kill, so `-Action off` can put things
    back afterwards.

.PARAMETER Action
    on      Stop the container, run the backend natively on the host. Blocks
            until you stop it with Ctrl-C, then restores the container.
    off     Restore the container from a previous run's saved state. Only
            needed if `on` was killed hard enough to skip its own cleanup.
    status  Report which mode is currently serving, and on what.

.PARAMETER Port
    Host port to serve on. Defaults to 8502 -- the port the container
    publishes and the port `tailscale serve` maps, so the existing dashboard
    URL and tailnet URL keep working unchanged.

.EXAMPLE
    tools\sbd-native-audio.ps1
    Switch to native audio mode. Ctrl-C when finished; the container comes back.

.EXAMPLE
    tools\sbd-native-audio.ps1 -Action status
#>
[CmdletBinding()]
param(
    [ValidateSet("on", "off", "status")]
    [string]$Action = "on",

    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8502,
    [string]$Container = "smart-bulb-dashboard"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# tools/ -> repo root
$repo      = Split-Path -Parent $PSScriptRoot
$backend   = Join-Path $repo "backend"
$py        = Join-Path $backend "venv\Scripts\python.exe"
$stateDir  = Join-Path $repo ".state"
$statePath = Join-Path $stateDir "native-audio.json"

# Docker Desktop publishes ports from inside its own VM, so the process
# holding 8502 while the container runs is Docker's, not ours. Anything else
# holding it is a genuine conflict we must not stomp on.
$dockerProcessNames = @("com.docker.backend", "com.docker.proxy", "vpnkit",
                        "wslrelay", "Docker Desktop", "dockerd", "docker")

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Warn([string]$Message) {
    Write-Host "!!  $Message" -ForegroundColor Yellow
}

function Write-Err([string]$Message) {
    Write-Host "!!  $Message" -ForegroundColor Red
}

function Get-ContainerState {
    <# running | stopped | missing -- "missing" matters because a machine
       that never built the image must not be told its container failed. #>
    $out = & docker inspect --format '{{.State.Running}}' $Container 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) { return "missing" }
    if ($out.Trim() -eq "true") { return "running" }
    return "stopped"
}

function Get-PortHolder {
    <# Only a listener on the address we intend to bind can actually conflict.

       This machine has three listeners on 8502: com.docker.backend on
       127.0.0.1 (the container's published port) and tailscaled on both
       tailnet addresses (`tailscale serve` fronting the dashboard). Taking
       "the first listener on the port" picks tailscaled and refuses to start
       in the completely normal case -- found by running -Action status
       against the real machine, not by reading the code.

       tailscaled's bind is on a different address, so it never conflicts, and
       it must keep running: it is what makes the tailnet URL work while
       native mode serves. #>
    param([int]$TcpPort, [string]$Address = $BindHost)
    $conns = Get-NetTCPConnection -LocalPort $TcpPort -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { return $null }

    # A wildcard bind (0.0.0.0 / ::) covers every address, so it conflicts too.
    $conflicting = @($conns | Where-Object {
        $_.LocalAddress -eq $Address -or $_.LocalAddress -eq "0.0.0.0" -or $_.LocalAddress -eq "::"
    })
    if ($conflicting.Count -eq 0) { return $null }

    $owningPid = $conflicting[0].OwningProcess
    $proc = Get-Process -Id $owningPid -ErrorAction SilentlyContinue
    [pscustomobject]@{
        ProcessId = $owningPid
        Address   = $conflicting[0].LocalAddress
        Name      = if ($proc) { $proc.ProcessName } else { "unknown" }
        Path      = if ($proc -and $proc.Path) { $proc.Path } else { "" }
    }
}

function Test-IsDockerHolder($holder) {
    if (-not $holder) { return $false }
    foreach ($n in $dockerProcessNames) {
        if ($holder.Name -like "$n*") { return $true }
    }
    return $false
}

function Wait-PortFree {
    param([int]$TcpPort, [int]$TimeoutSeconds = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-PortHolder -TcpPort $TcpPort)) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Save-NativeState($containerWasRunning) {
    New-Item -ItemType Directory -Force $stateDir | Out-Null
    [pscustomobject]@{
        container_was_running = [bool]$containerWasRunning
        container             = $Container
        port                  = $Port
        started_at            = (Get-Date).ToString("o")
    } | ConvertTo-Json | Set-Content -Path $statePath -Encoding UTF8
}

function Get-NativeState {
    if (-not (Test-Path $statePath)) { return $null }
    try { return Get-Content $statePath -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Clear-NativeState {
    if (Test-Path $statePath) { Remove-Item $statePath -Force -ErrorAction SilentlyContinue }
}

function Restore-Container {
    param([bool]$WasRunning)
    if (-not $WasRunning) {
        # Respect the prior state. Blindly starting would "helpfully" bring up
        # a dashboard the user had deliberately stopped before switching.
        Write-Step "container was not running before native mode -- leaving it stopped"
        Clear-NativeState
        return
    }
    Write-Step "restoring container '$Container'"
    & docker start $Container | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Err "could not restart '$Container'. Start it by hand: docker start $Container"
        return
    }
    # Deliberately not "$Port": that is the port native mode was serving on,
    # which may not be the one the container publishes. Claiming a port the
    # container isn't on would send someone to a dead URL.
    Clear-NativeState
    Write-Step "container is back up -- dashboard restored on its published port"
}

# ----------------------------------------------------------------- status ---
if ($Action -eq "status") {
    $state  = Get-ContainerState
    $holder = Get-PortHolder -TcpPort $Port
    $saved  = Get-NativeState

    Write-Host ""
    Write-Host "  container '$Container' : $state"
    if ($holder) {
        $who = if (Test-IsDockerHolder $holder) { "$($holder.Name) (Docker)" } else { $holder.Name }
        Write-Host "  ${BindHost}:$Port      : held by $who (pid $($holder.ProcessId))"
    }
    else {
        Write-Host "  ${BindHost}:$Port      : free -- nothing is serving the dashboard"
    }

    # Listeners on other addresses cannot conflict, but seeing them explains
    # why the tailnet URL keeps working across a mode switch.
    $others = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Where-Object { $_.LocalAddress -ne $BindHost -and $_.LocalAddress -ne "0.0.0.0" -and $_.LocalAddress -ne "::" })
    foreach ($o in $others) {
        $op = Get-Process -Id $o.OwningProcess -ErrorAction SilentlyContinue
        $on = if ($op) { $op.ProcessName } else { "unknown" }
        Write-Host "  also listening         : $($o.LocalAddress):$Port ($on) -- does not conflict"
    }
    if ($saved) {
        Write-Warn "a native-mode run is recorded as still in progress (started $($saved.started_at))."
        Write-Warn "if nothing is serving, run:  tools\sbd-native-audio.ps1 -Action off"
    }
    $mode = if ($saved) { "native (or interrupted native)" }
            elseif ($state -eq "running") { "container" }
            else { "nothing running" }
    Write-Host "  serving mode           : $mode"
    Write-Host ""
    exit 0
}

# -------------------------------------------------------------------- off ---
if ($Action -eq "off") {
    $saved = Get-NativeState
    if (-not $saved) {
        Write-Step "no interrupted native run recorded"
        $state = Get-ContainerState
        if ($state -eq "stopped") {
            Write-Warn "container '$Container' is stopped. Start it with: docker start $Container"
        }
        exit 0
    }
    $holder = Get-PortHolder -TcpPort $Port
    if ($holder -and -not (Test-IsDockerHolder $holder)) {
        Write-Err "port $Port is still held by $($holder.Name) (pid $($holder.ProcessId))."
        Write-Err "that is probably the native backend still running -- stop it first, then re-run."
        exit 1
    }
    Restore-Container -WasRunning ([bool]$saved.container_was_running)
    exit 0
}

# --------------------------------------------------------------------- on ---
if (-not (Test-Path $py))      { throw "Python venv not found at $py" }
if (-not (Test-Path $backend)) { throw "backend/ not found at $backend" }

$stale = Get-NativeState
if ($stale) {
    Write-Warn "a previous native run did not clean up (started $($stale.started_at))."
    Write-Warn "continuing; its recorded container state will be preserved."
}

$containerState = Get-ContainerState
$containerWasRunning = ($containerState -eq "running")

# Pre-check BEFORE touching the container. If something unrelated already owns
# the port, refusing now leaves everything exactly as it was -- which is the
# whole difference between "refused" and "corrupted state".
$holder = Get-PortHolder -TcpPort $Port
if ($holder -and -not (Test-IsDockerHolder $holder)) {
    Write-Err "port $Port is already held by '$($holder.Name)' (pid $($holder.ProcessId))."
    if ($holder.Path) { Write-Err "  $($holder.Path)" }
    Write-Err "refusing to start. Stop that process, or pass -Port <other>."
    exit 1
}

if ($containerState -eq "missing") {
    Write-Warn "container '$Container' does not exist -- running native anyway, nothing to restore."
}

# Record the prior state BEFORE stopping, so the restore is driven by what was
# true when we arrived rather than what we just did.
if ($stale) { $containerWasRunning = [bool]$stale.container_was_running }
Save-NativeState $containerWasRunning

try {
    if ($containerWasRunning) {
        Write-Step "stopping container '$Container'"
        & docker stop $Container | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "docker stop failed for '$Container'" }
    }
    else {
        Write-Step "container '$Container' already stopped -- nothing to stop"
    }

    if (-not (Wait-PortFree -TcpPort $Port)) {
        $still = Get-PortHolder -TcpPort $Port
        $name  = if ($still) { "$($still.Name) (pid $($still.ProcessId))" } else { "an unidentified process" }
        throw "port $Port did not free up -- still held by $name"
    }

    Write-Host ""
    Write-Step "native audio mode: uvicorn on ${BindHost}:${Port} with direct WASAPI access"
    Write-Host "    dashboard : http://127.0.0.1:$Port"
    Write-Host "    audio     : real capture devices are visible here (no bridge needed)"
    Write-Host "    stop      : Ctrl-C -- the container comes back automatically"
    Write-Host ""

    $env:PYTHONUNBUFFERED = "1"

    # --no-proxy-headers is NOT optional: uvicorn trusts X-Forwarded-For from
    # 127.0.0.1 by default, letting any local process forge its source IP and
    # walk past the PIN gate's per-IP lockout. Same reason as the sibling
    # supervisor in deploy/windows/run-dashboard.ps1.
    Push-Location $backend
    try {
        & $py -m uvicorn main:app --host $BindHost --port $Port --no-proxy-headers
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Err $_.Exception.Message
}
finally {
    # Every exit path lands here: Ctrl-C, uvicorn crashing, the port check
    # failing, a docker error. "Dashboard silently left down" must not be a
    # reachable state.
    Write-Host ""
    Restore-Container -WasRunning $containerWasRunning
}
