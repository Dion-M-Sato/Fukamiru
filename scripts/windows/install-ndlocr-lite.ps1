param(
    [string]$RuntimeRoot = (Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) "runtime")
)

$ErrorActionPreference = "Stop"
$Repository = Join-Path $RuntimeRoot "ndlocr-lite"
$VirtualEnvironment = Join-Path $RuntimeRoot "ndlocr-venv"
$Python = Join-Path $VirtualEnvironment "Scripts\python.exe"
$Executable = Join-Path $VirtualEnvironment "Scripts\ndlocr-lite.exe"
$UvCache = Join-Path $RuntimeRoot "uv-cache"

New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null

if (Test-Path (Join-Path $Repository ".git")) {
    git -C $Repository -c http.sslBackend=schannel pull --ff-only
} elseif (Test-Path $Repository) {
    throw "NDLOCR-Lite destination exists but is not a Git repository: $Repository"
} else {
    git -c http.sslBackend=schannel clone --depth 1 `
        "https://github.com/ndl-lab/ndlocr-lite.git" $Repository
}

$Uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -ne $Uv) {
    if (-not (Test-Path $Python)) {
        & $Uv.Source venv $VirtualEnvironment --cache-dir $UvCache
    }
    & $Uv.Source pip install --system-certs --python $Python $Repository --cache-dir $UvCache
} else {
    if (-not (Test-Path $Python)) {
        python -m venv $VirtualEnvironment
    }
    & $Python -m pip install --upgrade pip
    & $Python -m pip install $Repository
}

if (-not (Test-Path $Executable)) {
    throw "NDLOCR-Lite executable was not installed: $Executable"
}

$JsonOnlyPatch = Join-Path (Split-Path $PSScriptRoot -Parent) "patch_ndlocr_json_only.py"
& $Python $JsonOnlyPatch
if ($LASTEXITCODE -ne 0) {
    throw "NDLOCR-Lite JSON-only compatibility patch failed."
}

Write-Host "NDLOCR-Lite installed: $Executable"
Write-Host "Set ocr.ndlocr_command in config/local.toml to this path."
