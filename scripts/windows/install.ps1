param([switch]$Full)

$ErrorActionPreference = "Stop"

$FukamiruRepository = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$FukamiruRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$FukamiruConfigDirectory = Join-Path $FukamiruRoot "config"
$FukamiruConfig = Join-Path $FukamiruConfigDirectory "local.toml"
$FukamiruVenv = Join-Path $FukamiruRepository ".venv"
$FukamiruVenvPython = Join-Path $FukamiruVenv "Scripts\python.exe"
$FukamiruTorchVersion = "2.13.0"
$FukamiruPytorchIndex = "https://download.pytorch.org/whl/cu126"
$env:PIP_PROGRESS_BAR = "off"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:PIP_NO_INPUT = "1"

function Find-FukamiruBootstrapPython {
    $FukamiruPythonCandidates = @(
        [PSCustomObject]@{ Command = "py"; PrefixArguments = @("-3.12") },
        [PSCustomObject]@{ Command = "py"; PrefixArguments = @("-3.11") },
        [PSCustomObject]@{ Command = "python"; PrefixArguments = @() },
        [PSCustomObject]@{ Command = "python3"; PrefixArguments = @() },
        [PSCustomObject]@{ Command = "py"; PrefixArguments = @() }
    )

    foreach ($FukamiruCandidate in $FukamiruPythonCandidates) {
        $FukamiruResolvedCommand = Get-Command $FukamiruCandidate.Command -ErrorAction SilentlyContinue
        if (-not $FukamiruResolvedCommand) {
            continue
        }

        $FukamiruCandidateCommand = $FukamiruResolvedCommand.Source
        $FukamiruCandidateArguments = $FukamiruCandidate.PrefixArguments
        $FukamiruVersionProbe = "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
        try {
            & $FukamiruCandidateCommand @FukamiruCandidateArguments -c $FukamiruVersionProbe *> $null
            if ($LASTEXITCODE -eq 0) {
                return [PSCustomObject]@{
                    Command = $FukamiruCandidateCommand
                    PrefixArguments = $FukamiruCandidateArguments
                }
            }
        }
        catch {
            continue
        }
    }

    throw @"
Python 3.11 or later was not found.
Install 64-bit Python for Windows, enable "Add python.exe to PATH", and rerun this script.
Download: https://www.python.org/downloads/windows/
Verification command: python --version
"@
}

Set-Location $FukamiruRepository
if ((Test-Path $FukamiruVenv) -and (-not (Test-Path $FukamiruVenvPython))) {
    throw "The existing .venv is incomplete. Rename or remove it, then rerun this script."
}

if (-not (Test-Path $FukamiruVenvPython)) {
    $FukamiruBootstrapPython = Find-FukamiruBootstrapPython
    $FukamiruBootstrapCommand = $FukamiruBootstrapPython.Command
    $FukamiruBootstrapArguments = $FukamiruBootstrapPython.PrefixArguments
    & $FukamiruBootstrapCommand @FukamiruBootstrapArguments -m venv $FukamiruVenv
    if ($LASTEXITCODE -ne 0) {
        throw "Python was found, but creating the virtual environment failed."
    }
}

$FukamiruPython = $FukamiruVenvPython
& $FukamiruPython -m pip install --upgrade pip --progress-bar off
if ($Full) {
$FukamiruCudaProbe = @"
import torch
valid = (
    torch.__version__.startswith("$FukamiruTorchVersion+cu126")
    and torch.version.cuda == "12.6"
    and torch.cuda.is_available()
)
raise SystemExit(0 if valid else 1)
"@
& $FukamiruPython -c $FukamiruCudaProbe
if ($LASTEXITCODE -ne 0) {
    # The optional full profile preserves the tested CUDA 12.6 stack.
    # For another GPU stack install optional dependencies manually instead.
    & $FukamiruPython -m pip install --force-reinstall "torch==$FukamiruTorchVersion" `
        --index-url $FukamiruPytorchIndex --progress-bar off
    if ($LASTEXITCODE -ne 0) {
        throw "Installing the official CUDA 12.6 PyTorch wheel failed."
    }
}
& $FukamiruPython -m pip install -e ".[dev,rag,japanese,ocr]" --progress-bar off
if ($LASTEXITCODE -ne 0) {
    throw "Installing Fukamiru dependencies failed."
}
& $FukamiruPython -c $FukamiruCudaProbe
if ($LASTEXITCODE -ne 0) {
    throw @"
Fukamiru dependencies did not preserve a usable CUDA PyTorch build.
Expected torch $FukamiruTorchVersion+cu126 with torch.cuda.is_available() == true.
CPU fallback is disabled; the CUDA Toolkit is not required for the prebuilt wheel.
"@
}

} else {
    & $FukamiruPython -m pip install -e ".[dev]" --progress-bar off
    if ($LASTEXITCODE -ne 0) { throw "Installing Fukamiru dependencies failed." }
}



New-Item -ItemType Directory -Path $FukamiruConfigDirectory -Force | Out-Null
if (-not (Test-Path $FukamiruConfig)) {
    Copy-Item (Join-Path $FukamiruRepository "config\local.example.toml") $FukamiruConfig
}

& $FukamiruPython -m fukamiru.cli --config $FukamiruConfig init
if ($LASTEXITCODE -ne 0) { throw "Fukamiru initialization failed." }
Write-Host "Fukamiru installation is ready."
Write-Host "Config: $FukamiruConfig"
