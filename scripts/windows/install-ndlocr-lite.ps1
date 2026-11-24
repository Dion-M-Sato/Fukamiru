param(
    [string]$RuntimeRoot = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) "runtime")
)

$ErrorActionPreference = "Stop"
$VirtualEnvironment = Join-Path $RuntimeRoot "ndlocr-venv"
$Python = Join-Path $VirtualEnvironment "Scripts\python.exe"
$Executable = Join-Path $VirtualEnvironment "Scripts\ndlocr-lite.exe"
$UvCache = Join-Path $RuntimeRoot "uv-cache"
# Pin the tested release; do not install a moving upstream branch.
$Package = "ndlocr-lite==2.0.0"

New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null

$Uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -ne $Uv) {
    if (-not (Test-Path $Python)) {
        & $Uv.Source venv --python ">=3.11" $VirtualEnvironment --cache-dir $UvCache
        if ($LASTEXITCODE -ne 0) { throw "NDLOCR-Lite virtual environment creation failed." }
    }
    & $Uv.Source pip install --system-certs --python $Python --upgrade $Package --cache-dir $UvCache
} else {
    if (-not (Test-Path $Python)) {
        python -m venv $VirtualEnvironment
        if ($LASTEXITCODE -ne 0) { throw "NDLOCR-Lite virtual environment creation failed." }
    }
    & $Python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
    & $Python -m pip install --upgrade $Package
}
if ($LASTEXITCODE -ne 0) { throw "NDLOCR-Lite package installation failed." }

if (-not (Test-Path $Executable)) {
    throw "NDLOCR-Lite executable was not installed: $Executable"
}

$JsonOnlyPatch = Join-Path (Split-Path $PSScriptRoot -Parent) "patch_ndlocr_json_only.py"
& $Python $JsonOnlyPatch
if ($LASTEXITCODE -ne 0) {
    throw "NDLOCR-Lite JSON-only compatibility patch failed."
}

# Download and hash-check v2 models before processing user documents,
# including network-disabled passage recognition.
& $Python (Join-Path (Split-Path $PSScriptRoot -Parent) "prepare_ndlocr_models.py")
if ($LASTEXITCODE -ne 0) { throw "NDLOCR-Lite model preparation failed." }

& $Executable --version
if ($LASTEXITCODE -ne 0) { throw "NDLOCR-Lite CLI verification failed." }
Write-Host "NDLOCR-Lite installed: $Executable"
Write-Host "Set ocr.ndlocr_command in config/local.toml to this path."
