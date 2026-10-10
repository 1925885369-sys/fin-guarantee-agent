$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Project environment is missing. Run .\setup.ps1 first.'
}
& $python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

