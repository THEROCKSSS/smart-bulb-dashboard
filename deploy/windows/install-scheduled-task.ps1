<#
.SYNOPSIS
    Install the Smart Bulb Dashboard as a Scheduled Task that starts at logon.

.DESCRIPTION
    Why a Scheduled Task and not an NSSM service (see ../windows-service.md):

    NSSM's default LocalSystem account has no desktop audio session, so
    audio-reactive lighting would start, report itself running, and never
    react to sound -- the project's flagship feature, silently dead. Running
    the NSSM service as a real user fixes that but requires storing a Windows
    password in the service configuration.

    A logon task runs as you, in your session, with your audio devices, and
    stores no password. The trade is that it starts at logon rather than at
    boot with nobody logged in -- which was never achievable for desktop
    audio capture anyway.

    Crash recovery lives in run-dashboard.ps1 (a supervised restart loop),
    not in Task Scheduler, because Task Scheduler's minimum restart interval
    is one minute. The wrapper restarts in 5s, matching the documented NSSM
    policy.

    Requires no administrator rights: the task runs as the current user at
    the Limited run level.

.PARAMETER TaskName
    Scheduled Task name. Default "SmartBulbDashboard".

.PARAMETER Port
    Port to bind. Default 8502 (what `tailscale serve` maps).

.EXAMPLE
    pwsh -File deploy\windows\install-scheduled-task.ps1

.EXAMPLE
    # Remove it again
    Unregister-ScheduledTask -TaskName SmartBulbDashboard -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$TaskName = "SmartBulbDashboard",
    [int]$Port = 8502
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repo    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$runner  = Join-Path $PSScriptRoot "run-dashboard.ps1"
$user    = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path $runner)) { throw "Runner not found at $runner" }

# powershell.exe (Windows PowerShell 5.1) is present on every Windows box;
# pwsh may not be. The runner script is compatible with both.
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Port {1}' -f $runner, $Port) `
    -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user

$principal = New-ScheduledTaskPrincipal `
    -UserId $user `
    -LogonType Interactive `
    -RunLevel Limited

# ExecutionTimeLimit 0 == run forever. Without it Windows kills the task
# after 3 days, which would look exactly like a random unexplained outage.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Write-Host "Registering scheduled task '$TaskName' for $user (port $Port)..."

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Smart Bulb Dashboard - local REST API and web UI for Tuya smart bulbs. Starts at logon; run-dashboard.ps1 supervises restarts." `
    -Force | Out-Null

Write-Host "Registered. Starting it now..."
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Task state:" (Get-ScheduledTask -TaskName $TaskName).State
Write-Host "Logs:       $repo\logs\"
Write-Host "Verify:     curl http://127.0.0.1:$Port/api/system/health"
Write-Host "Stop:       Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Remove:     Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
