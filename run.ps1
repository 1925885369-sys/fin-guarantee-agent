$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

