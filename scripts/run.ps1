# Set up .venv (if needed), install requirements, then run the pipeline.
#
# Usage (Windows PowerShell):
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1               # full pipeline
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1 --skip-wind   # without wind data
#   powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Test         # run the test suite
#
# Picks the first installed Python that is 3.12 or newer, preferring 3.13.

param(
    [switch]$Test,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RunArgs
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Venv = Join-Path $RepoRoot '.venv'

function Test-Python {
    param([string]$Exe, [string[]]$Prefix)
    try {
        & $Exe @Prefix -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Test-CanCreateVenv {
    param([string]$Exe, [string[]]$Prefix)
    $probe = Join-Path ([IO.Path]::GetTempPath()) ("venv-probe-" + [guid]::NewGuid().ToString())
    try {
        & $Exe @Prefix -m venv $probe *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        Remove-Item -Recurse -Force $probe -ErrorAction SilentlyContinue
    }
}

function Get-VenvPython {
    foreach ($p in @((Join-Path $Venv 'Scripts\python.exe'), (Join-Path $Venv 'bin/python'))) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Find-BasePython {
    # $env:PYTHON_BIN=C:\path\to\python.exe overrides the search (it must still pass the checks).
    if ($env:PYTHON_BIN -and (Test-Python -Exe $env:PYTHON_BIN -Prefix @()) -and (Test-CanCreateVenv -Exe $env:PYTHON_BIN -Prefix @())) {
        return @{ Exe = $env:PYTHON_BIN; Prefix = @() }
    }
    $candidates = @(
        @{ Exe = 'py';        Prefix = @('-3.13') },
        @{ Exe = 'py';        Prefix = @('-3.12') },
        @{ Exe = 'py';        Prefix = @('-3.14') },
        @{ Exe = 'py';        Prefix = @('-3.15') },
        @{ Exe = 'python3.13'; Prefix = @() },
        @{ Exe = 'python3.12'; Prefix = @() },
        @{ Exe = 'python';    Prefix = @() },
        @{ Exe = 'python3';   Prefix = @() },
        @{ Exe = 'py';        Prefix = @() }
    )
    foreach ($c in $candidates) {
        if ((Test-Python -Exe $c.Exe -Prefix $c.Prefix) -and (Test-CanCreateVenv -Exe $c.Exe -Prefix $c.Prefix)) { return $c }
    }
    return $null
}

$VenvPy = Get-VenvPython
if ($VenvPy -and -not (Test-Python -Exe $VenvPy -Prefix @())) {
    Write-Host "Existing .venv was built with Python older than 3.12. Recreating it."
    Remove-Item -Recurse -Force $Venv
    $VenvPy = $null
}

if (-not $VenvPy) {
    $base = Find-BasePython
    if (-not $base) {
        Write-Error "Python 3.12 or newer is required, but none was found. Install Python 3.13 from https://www.python.org/downloads/ and retry."
    }
    Write-Host "Creating .venv with $($base.Exe) $($base.Prefix -join ' ')"
    & $base.Exe @($base.Prefix) -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Creating the virtual environment failed." }
    $VenvPy = Get-VenvPython
}

Write-Host "Installing requirements"
& $VenvPy -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Installing requirements failed." }

if ($Test) {
    & $VenvPy -m pytest @RunArgs
} else {
    & $VenvPy src\run_all.py @RunArgs
}
exit $LASTEXITCODE
