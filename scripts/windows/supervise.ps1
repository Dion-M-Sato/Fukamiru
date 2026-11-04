$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$FukamiruRepository = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$FukamiruRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$FukamiruConfig = Join-Path $FukamiruRoot "config\local.toml"
$FukamiruPython = Join-Path $FukamiruRepository ".venv\Scripts\python.exe"
$FukamiruRunDirectory = Join-Path $FukamiruRoot "runtime\run"
$FukamiruLogDirectory = Join-Path $FukamiruRoot "runtime\logs\service"
$FukamiruPidFile = Join-Path $FukamiruRunDirectory "fukamiru.pid"
$FukamiruSupervisorPidFile = Join-Path $FukamiruRunDirectory "fukamiru-supervisor.pid"
$FukamiruStopFile = Join-Path $FukamiruRunDirectory "fukamiru.stop"
$FukamiruSupervisorLog = Join-Path $FukamiruLogDirectory "supervisor.log"

New-Item -ItemType Directory -Path $FukamiruRunDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $FukamiruLogDirectory -Force | Out-Null
$env:FUKAMIRU_HEALTH_CONFIG = $FukamiruConfig
$FukamiruHealthProbe = @'
import os
from pathlib import Path
from fukamiru.config import load_settings
settings = load_settings(Path(os.environ['FUKAMIRU_HEALTH_CONFIG']))
print('http://{}:{}/api/liveness'.format(settings.server.host, settings.server.port))
'@
$FukamiruHealthUri = & $FukamiruPython -c $FukamiruHealthProbe
Remove-Item Env:\FUKAMIRU_HEALTH_CONFIG
if ($LASTEXITCODE -ne 0 -or -not $FukamiruHealthUri) {
    throw "Fukamiru could not resolve the configured health endpoint."
}

function Write-FukamiruSupervisorLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $FukamiruTimestamp = Get-Date -Format "yyyy-MM-ddTHH:mm:ss.fffK"
    Add-Content -LiteralPath $FukamiruSupervisorLog -Value "$FukamiruTimestamp $Message"
}

function Test-FukamiruHealth {
    try {
        $FukamiruHealth = Invoke-RestMethod -Uri $FukamiruHealthUri -TimeoutSec 3
        return $FukamiruHealth.status -eq "alive"
    }
    catch {
        return $false
    }
}

function Stop-FukamiruChild {
    param([System.Diagnostics.Process]$Process)

    if (-not $Process -or $Process.HasExited) {
        return
    }
    Write-FukamiruSupervisorLog "Stopping child PID $($Process.Id)."
    Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue
    $Process.WaitForExit(10000) | Out-Null
}

Set-Content -LiteralPath $FukamiruSupervisorPidFile -Value $PID
Write-FukamiruSupervisorLog "Supervisor started with PID $PID."
$FukamiruRestartCount = 0
$FukamiruChild = $null

try {
    while (-not (Test-Path -LiteralPath $FukamiruStopFile)) {
        $FukamiruStartedAt = Get-Date
        $FukamiruFileStamp = $FukamiruStartedAt.ToString("yyyyMMdd-HHmmss-fff")
        $FukamiruStdoutLog = Join-Path $FukamiruLogDirectory "fukamiru-$FukamiruFileStamp.stdout.log"
        $FukamiruStderrLog = Join-Path $FukamiruLogDirectory "fukamiru-$FukamiruFileStamp.stderr.log"
        $env:PYTHONUNBUFFERED = "1"
        $FukamiruStartParameters = @{
            FilePath = $FukamiruPython
            ArgumentList = @(
                "-m", "fukamiru.cli", "--config", "`"$FukamiruConfig`"",
                "start", "--no-browser"
            )
            WorkingDirectory = $FukamiruRepository
            WindowStyle = "Hidden"
            RedirectStandardOutput = $FukamiruStdoutLog
            RedirectStandardError = $FukamiruStderrLog
            PassThru = $true
        }
        $FukamiruChild = Start-Process @FukamiruStartParameters
        Set-Content -LiteralPath $FukamiruPidFile -Value $FukamiruChild.Id
        Write-FukamiruSupervisorLog (
            "Started child PID $($FukamiruChild.Id); stdout=$FukamiruStdoutLog; " +
            "stderr=$FukamiruStderrLog."
        )

        $FukamiruEverHealthy = $false
        $FukamiruConsecutiveHealthFailures = 0
        $FukamiruRequestedRestart = $false
        while (-not $FukamiruChild.HasExited) {
            if (Test-Path -LiteralPath $FukamiruStopFile) {
                Stop-FukamiruChild -Process $FukamiruChild
                break
            }

            $FukamiruHealthy = Test-FukamiruHealth
            $FukamiruRuntimeSeconds = ((Get-Date) - $FukamiruStartedAt).TotalSeconds
            if ($FukamiruHealthy) {
                if (-not $FukamiruEverHealthy) {
                    Write-FukamiruSupervisorLog "Child PID $($FukamiruChild.Id) is healthy."
                }
                $FukamiruEverHealthy = $true
                $FukamiruConsecutiveHealthFailures = 0
                if ($FukamiruRuntimeSeconds -ge 120) {
                    $FukamiruRestartCount = 0
                }
            }
            elseif ($FukamiruEverHealthy) {
                $FukamiruConsecutiveHealthFailures += 1
                if ($FukamiruConsecutiveHealthFailures -eq 1) {
                    Write-FukamiruSupervisorLog (
                        "Health check failed for child PID $($FukamiruChild.Id)."
                    )
                }
                if ($FukamiruConsecutiveHealthFailures -ge 3) {
                    Write-FukamiruSupervisorLog (
                        "Health failed three consecutive times; restarting child PID " +
                        "$($FukamiruChild.Id)."
                    )
                    $FukamiruRequestedRestart = $true
                    Stop-FukamiruChild -Process $FukamiruChild
                    break
                }
            }
            elseif ($FukamiruRuntimeSeconds -ge 90) {
                Write-FukamiruSupervisorLog (
                    "Child PID $($FukamiruChild.Id) did not become healthy within 90 seconds; " +
                    "restarting."
                )
                $FukamiruRequestedRestart = $true
                Stop-FukamiruChild -Process $FukamiruChild
                break
            }
            Start-Sleep -Seconds 5
        }

        Remove-Item -LiteralPath $FukamiruPidFile -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $FukamiruStopFile) {
            break
        }

        $FukamiruChild.Refresh()
        $FukamiruExitCode = if ($FukamiruChild.HasExited) {
            try {
                $FukamiruChild.ExitCode
            }
            catch {
                $null
            }
        }
        else {
            $null
        }
        $FukamiruExitDescription = if ($null -ne $FukamiruExitCode) {
            "exit code $FukamiruExitCode"
        }
        elseif ($FukamiruChild.HasExited) {
            "exit code unavailable"
        }
        elseif ($FukamiruRequestedRestart) {
            "health-triggered restart"
        }
        else {
            "unknown state"
        }
        Write-FukamiruSupervisorLog (
            "Child PID $($FukamiruChild.Id) stopped ($FukamiruExitDescription)."
        )
        $FukamiruRestartCount += 1
        $FukamiruRestartDelay = [Math]::Min(60, [Math]::Pow(2, [Math]::Min(5, $FukamiruRestartCount)))
        Write-FukamiruSupervisorLog "Restarting in $FukamiruRestartDelay seconds."
        Start-Sleep -Seconds $FukamiruRestartDelay
    }
}
catch {
    Write-FukamiruSupervisorLog "Supervisor error: $($_.Exception.ToString())"
    throw
}
finally {
    Stop-FukamiruChild -Process $FukamiruChild
    Remove-Item -LiteralPath $FukamiruPidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $FukamiruSupervisorPidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $FukamiruStopFile -Force -ErrorAction SilentlyContinue
    Write-FukamiruSupervisorLog "Supervisor stopped."
}
