$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Get-Command py -ErrorAction SilentlyContinue
$useLauncher = $false
if ($launcher) {
    & py -3.12 --version *> $null
    $useLauncher = $LASTEXITCODE -eq 0
}
if ($useLauncher) {
    & py -3.12 -m venv (Join-Path $root '.venv')
} else {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe'),
        'C:\Python312\python.exe'
    )
    $python312 = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $python312) {
        throw 'Python 3.12 was not found. Install Python 3.12 and rerun this script.'
    }
    & $python312 -m venv (Join-Path $root '.venv')
}

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
& $venvPython -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Project dependency installation failed.' }
& $venvPython -m pip install -r (Join-Path $root 'requirements-retrieval.txt')
if ($LASTEXITCODE -ne 0) { throw 'Local retrieval dependency installation failed.' }
Write-Host 'Project Python 3.12 environment is ready.'
Write-Host 'Prepare the local embedding model with: .\.venv\Scripts\python.exe scripts\prepare_retrieval_model.py'
