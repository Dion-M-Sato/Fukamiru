$ErrorActionPreference = "Stop"

$FukamiruRepository = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$FukamiruRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$FukamiruConfig = Join-Path $FukamiruRoot "config\local.toml"
$FukamiruPython = Join-Path $FukamiruRepository ".venv\Scripts\python.exe"
$FukamiruRunDirectory = Join-Path $FukamiruRoot "runtime\run"
$FukamiruLogDirectory = Join-Path $FukamiruRoot "runtime\logs\service"
$FukamiruPidFile = Join-Path $FukamiruRunDirectory "fukamiru.pid"
$FukamiruSupervisorPidFile = Join-Path $FukamiruRunDirectory "fukamiru-supervisor.pid"
$FukamiruStopFile = Join-Path $FukamiruRunDirectory "fukamiru.stop"
$FukamiruSupervisorScript = Join-Path $PSScriptRoot "supervise.ps1"

if (-not (Test-Path $FukamiruPython)) {
    throw "Fukamiru is not installed. Run scripts\windows\install.ps1 first."
}

New-Item -ItemType Directory -Path $FukamiruRunDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $FukamiruLogDirectory -Force | Out-Null
$env:FUKAMIRU_HEALTH_CONFIG = $FukamiruConfig
$FukamiruHealthProbe = @'
import os
from pathlib import Path
from fukamiru.config import load_settings
settings = load_settings(Path(os.environ['FUKAMIRU_HEALTH_CONFIG']))
print('http://{}:{}/api/health'.format(settings.server.host, settings.server.port))
'@
$FukamiruHealthUri = & $FukamiruPython -c $FukamiruHealthProbe
Remove-Item Env:\FUKAMIRU_HEALTH_CONFIG
if ($LASTEXITCODE -ne 0 -or -not $FukamiruHealthUri) {
    throw "Fukamiru could not resolve the configured health endpoint."
}

if (Test-Path -LiteralPath $FukamiruSupervisorPidFile) {
    $FukamiruExistingSupervisorPid = [int](Get-Content -LiteralPath $FukamiruSupervisorPidFile)
    $FukamiruExistingSupervisor = Get-Process `
        -Id $FukamiruExistingSupervisorPid -ErrorAction SilentlyContinue
    $FukamiruSupervisorPidWrittenAt = (
        Get-Item -LiteralPath $FukamiruSupervisorPidFile
    ).LastWriteTime
    $FukamiruValidSupervisor = (
        $FukamiruExistingSupervisor -and
        $FukamiruExistingSupervisor.ProcessName -match "powershell" -and
        $FukamiruExistingSupervisor.StartTime -le $FukamiruSupervisorPidWrittenAt.AddSeconds(5)
    )
    if ($FukamiruValidSupervisor) {
        $FukamiruExistingAppPid = if (Test-Path -LiteralPath $FukamiruPidFile) {
            [int](Get-Content -LiteralPath $FukamiruPidFile)
        }
        else {
            $null
        }
        Write-Host (
            "Fukamiru supervisor is already running with PID " +
            "$FukamiruExistingSupervisorPid (app PID $FukamiruExistingAppPid)."
        )
        exit 0
    }
    Remove-Item -LiteralPath $FukamiruSupervisorPidFile -Force
}

if (Test-Path -LiteralPath $FukamiruPidFile) {
    $FukamiruOrphanPid = [int](Get-Content -LiteralPath $FukamiruPidFile)
    $FukamiruOrphan = Get-Process -Id $FukamiruOrphanPid -ErrorAction SilentlyContinue
    if ($FukamiruOrphan) {
        $FukamiruAppPidWrittenAt = (Get-Item -LiteralPath $FukamiruPidFile).LastWriteTime
        if ($FukamiruOrphan.Path -ne $FukamiruPython -or
            $FukamiruOrphan.StartTime -gt $FukamiruAppPidWrittenAt.AddSeconds(5)) {
            throw "The app PID file identifies an unexpected process; nothing was stopped."
        }
        Stop-Process -Id $FukamiruOrphanPid
        $FukamiruOrphan.WaitForExit(10000) | Out-Null
    }
    Remove-Item -LiteralPath $FukamiruPidFile -Force
}
Remove-Item -LiteralPath $FukamiruStopFile -Force -ErrorAction SilentlyContinue

$FukamiruFileStamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$FukamiruSupervisorStdout = Join-Path `
    $FukamiruLogDirectory "supervisor-$FukamiruFileStamp.stdout.log"
$FukamiruSupervisorStderr = Join-Path `
    $FukamiruLogDirectory "supervisor-$FukamiruFileStamp.stderr.log"
$FukamiruPowerShell = (Get-Command "powershell.exe").Source
$FukamiruSupervisorParameters = @{
    FilePath = $FukamiruPowerShell
    ArgumentList = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        "`"$FukamiruSupervisorScript`""
    )
    WorkingDirectory = $FukamiruRepository
    WindowStyle = "Hidden"
    RedirectStandardOutput = $FukamiruSupervisorStdout
    RedirectStandardError = $FukamiruSupervisorStderr
    PassThru = $true
}
$FukamiruSupervisor = Start-Process @FukamiruSupervisorParameters
Set-Content -LiteralPath $FukamiruSupervisorPidFile -Value $FukamiruSupervisor.Id

$FukamiruReady = $false
for ($FukamiruProbe = 0; $FukamiruProbe -lt 120; $FukamiruProbe += 1) {
    if ($FukamiruSupervisor.HasExited) {
        throw "Fukamiru supervisor exited during startup. See $FukamiruSupervisorStderr"
    }
    try {
        $FukamiruHealth = Invoke-RestMethod -Uri $FukamiruHealthUri -TimeoutSec 2
        if ($FukamiruHealth.status -eq "ready") {
            $FukamiruReady = $true
            break
        }
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $FukamiruReady) {
    throw "Fukamiru did not become ready within 60 seconds. See $FukamiruLogDirectory"
}
$FukamiruAppPid = [int](Get-Content -LiteralPath $FukamiruPidFile)
Write-Host (
    "Fukamiru is ready (supervisor PID $($FukamiruSupervisor.Id), " +
    "app PID $FukamiruAppPid)."
)
Write-Host "Persistent logs: $FukamiruLogDirectory"
