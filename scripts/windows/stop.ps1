$ErrorActionPreference = "Stop"

$FukamiruRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$FukamiruRunDirectory = Join-Path $FukamiruRoot "runtime\run"
$FukamiruPidFile = Join-Path $FukamiruRunDirectory "fukamiru.pid"
$FukamiruSupervisorPidFile = Join-Path $FukamiruRunDirectory "fukamiru-supervisor.pid"
$FukamiruStopFile = Join-Path $FukamiruRunDirectory "fukamiru.stop"

if (-not (Test-Path -LiteralPath $FukamiruPidFile) -and `
    -not (Test-Path -LiteralPath $FukamiruSupervisorPidFile)) {
    Write-Host "Fukamiru is not running."
    exit 0
}

New-Item -ItemType File -Path $FukamiruStopFile -Force | Out-Null
$FukamiruSupervisorPid = if (Test-Path -LiteralPath $FukamiruSupervisorPidFile) {
    [int](Get-Content -LiteralPath $FukamiruSupervisorPidFile)
} else {
    $null
}
$FukamiruSupervisorPidWrittenAt = if (Test-Path -LiteralPath $FukamiruSupervisorPidFile) {
    (Get-Item -LiteralPath $FukamiruSupervisorPidFile).LastWriteTime
} else {
    $null
}

for ($FukamiruWait = 0; $FukamiruWait -lt 100; $FukamiruWait += 1) {
    $FukamiruSupervisor = if ($FukamiruSupervisorPid) {
        Get-Process -Id $FukamiruSupervisorPid -ErrorAction SilentlyContinue
    } else {
        $null
    }
    $FukamiruApp = if (Test-Path -LiteralPath $FukamiruPidFile) {
        $FukamiruAppPid = [int](Get-Content -LiteralPath $FukamiruPidFile)
        Get-Process -Id $FukamiruAppPid -ErrorAction SilentlyContinue
    } else {
        $null
    }
    if (-not $FukamiruSupervisor -and -not $FukamiruApp) {
        break
    }
    Start-Sleep -Milliseconds 100
}

$FukamiruRepository = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$FukamiruPython = Join-Path $FukamiruRepository ".venv\Scripts\python.exe"
if ($FukamiruApp -and -not $FukamiruApp.HasExited) {
    $FukamiruAppPidWrittenAt = (Get-Item -LiteralPath $FukamiruPidFile).LastWriteTime
    if ($FukamiruApp.Path -ne $FukamiruPython -or
        $FukamiruApp.StartTime -gt $FukamiruAppPidWrittenAt.AddSeconds(5)) {
        throw "The app PID file identifies an unexpected process; nothing was stopped."
    }
    Stop-Process -Id $FukamiruApp.Id
    $FukamiruApp.WaitForExit(5000) | Out-Null
}
if ($FukamiruSupervisor -and -not $FukamiruSupervisor.HasExited) {
    if ($FukamiruSupervisor.ProcessName -notmatch "powershell" -or
        $FukamiruSupervisor.StartTime -gt $FukamiruSupervisorPidWrittenAt.AddSeconds(5)) {
        throw "The supervisor PID file identifies an unexpected process; nothing was stopped."
    }
    Stop-Process -Id $FukamiruSupervisor.Id
    $FukamiruSupervisor.WaitForExit(5000) | Out-Null
}
Remove-Item -LiteralPath $FukamiruPidFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $FukamiruSupervisorPidFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $FukamiruStopFile -Force -ErrorAction SilentlyContinue
Write-Host "Fukamiru stopped."
