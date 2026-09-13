param([switch]$RegisterLoginTask)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $projectRoot 'backend\venv\Scripts\python.exe'
$bridgePath = Join-Path $PSScriptRoot 'sbd-audio-bridge.py'
$statePath = Join-Path $projectRoot '.state'
New-Item -ItemType Directory -Force -Path $statePath | Out-Null
if ($RegisterLoginTask) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -File `"$PSCommandPath`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName 'Smart Bulb Audio Bridge' -Action $action -Trigger $trigger -Principal $principal -Force | Out-Null
}
$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -like '*sbd-audio-bridge.py*'
}
if ($existing) { Write-Output 'Audio helper is already running.'; exit 0 }
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-u', "`"$bridgePath`"", '--quiet') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $statePath 'bridge.log') -RedirectStandardError (Join-Path $statePath 'bridge-error.log')
Write-Output "Audio helper started (PID $($process.Id)). Input: saved choice or CABLE Output."
