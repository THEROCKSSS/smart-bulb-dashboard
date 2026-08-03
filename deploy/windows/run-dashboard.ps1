<#
.SYNOPSIS
    Supervised launcher for the Smart Bulb Dashboard backend (Windows).

.DESCRIPTION
    Runs uvicorn in a restart loop and writes stdout/stderr to logs/.
    Intended to be driven by the Scheduled Task created by
    install-scheduled-task.ps1, but it is equally fine to run by hand.

    The restart policy deliberately mirrors the NSSM policy documented in
    ../windows-service.md, so both deployment paths behave the same:

      * 5 second delay between restarts   (NSSM AppRestartDelay 5000)
      * a run shorter than 10s is a "fast failure"  (NSSM AppThrottle 10000)
      * 5 consecutive fast failures means a broken config, not a transient
        fault, so we stop rather than spin a crash loop that floods the log

    --no-proxy-headers is NOT optional. uvicorn trusts X-Forwarded-For from
    127.0.0.1 by default, which lets any local process forge its source IP
    and walk past the PIN gate's per-IP lockout.

.PARAMETER BindHost
    Interface to bind. Defaults to 127.0.0.1 (tailscale serve fronts it).

.PARAMETER Port
    Port to bind. Defaults to 8502, which is what `tailscale serve` maps.
#>
[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8502
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# deploy/windows/ -> repo root
$repo    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$backend = Join-Path $repo "backend"
$py      = Join-Path $backend "venv\Scripts\python.exe"
$logDir  = Join-Path $repo "logs"

if (-not (Test-Path $py))      { throw "Python venv not found at $py" }
if (-not (Test-Path $backend)) { throw "backend/ not found at $backend" }

New-Item -ItemType Directory -Force $logDir | Out-Null

$outLog = Join-Path $logDir "service.out.log"
$errLog = Join-Path $logDir "service.err.log"

# Start-Process redirection truncates, so preserve the previous run first --
# a crash's last words are the whole reason to keep a log at all.
foreach ($pair in @(@($outLog, "service.out.prev.log"), @($errLog, "service.err.prev.log"))) {
    if ((Test-Path $pair[0]) -and (Get-Item $pair[0]).Length -gt 0) {
        Move-Item $pair[0] (Join-Path $logDir $pair[1]) -Force
    }
}

function Write-Log([string]$Message) {
    $line = "{0} [supervisor] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Output $line
    Add-Content -Path (Join-Path $logDir "supervisor.log") -Value $line
}

$env:PYTHONUNBUFFERED = "1"

$uvicornArgs = @(
    "-m", "uvicorn", "main:app",
    "--host", $BindHost,
    "--port", $Port,
    "--no-proxy-headers"
)

Write-Log "starting supervisor for ${BindHost}:${Port} (repo: $repo)"

$fastFailures = 0
$maxFastFailures = 5

while ($true) {
    $started = Get-Date
    Write-Log "launching uvicorn"

    $proc = Start-Process -FilePath $py `
                          -ArgumentList $uvicornArgs `
                          -WorkingDirectory $backend `
                          -RedirectStandardOutput $outLog `
                          -RedirectStandardError $errLog `
                          -NoNewWindow -PassThru

    $proc.WaitForExit()
    $elapsed = (Get-Date) - $started
    Write-Log ("uvicorn exited with code {0} after {1:N1}s" -f $proc.ExitCode, $elapsed.TotalSeconds)

    if ($elapsed.TotalSeconds -lt 10) {
        $fastFailures++
        Write-Log "fast failure $fastFailures/$maxFastFailures"
        if ($fastFailures -ge $maxFastFailures) {
            Write-Log "giving up -- $maxFastFailures consecutive fast failures means a broken config, not a transient fault"
            exit 1
        }
    }
    else {
        $fastFailures = 0
    }

    Start-Sleep -Seconds 5
}
