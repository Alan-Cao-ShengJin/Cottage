<#
.SYNOPSIS
    Start the Agent Rooms backend and console together.

.DESCRIPTION
    Exists because the obvious commands are subtly wrong on Windows PowerShell 5.1:

      * `&&` is a parser error, not a chaining operator;
      * `uvicorn` resolves to the *global* Python, which has none of this project's
        dependencies, so it fails with ModuleNotFoundError rather than doing nothing
        obvious.

    So this script always invokes the venv interpreter explicitly and never chains with
    `&&`. Backend runs in the background; the frontend runs in the foreground so Ctrl+C
    stops both.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\dev.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -BackendOnly
#>
[CmdletBinding()]
param(
    [int]$ApiPort = 8000,
    [int]$WebPort = 3000,
    [switch]$BackendOnly,
    [switch]$NoReload
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

# --- locate the interpreter -------------------------------------------------
$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    $python = Join-Path $root '.venv\Scripts\python.exe'
}
if (-not (Test-Path $python)) {
    Write-Host 'No virtualenv found. Create one first:' -ForegroundColor Yellow
    Write-Host '  python -m venv backend\.venv'
    Write-Host '  backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt'
    exit 1
}

# Fail early with a clear message rather than a traceback from uvicorn's importer.
& $python -c "import fastapi, aiosqlite, mcp" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Dependencies are missing from the virtualenv. Install them:' -ForegroundColor Yellow
    Write-Host "  $python -m pip install -r backend\requirements.txt"
    exit 1
}

$backend = $null
try {
    $args = @('-m', 'uvicorn', 'app.main:app', '--port', $ApiPort, '--app-dir', 'backend')
    if (-not $NoReload) { $args += '--reload' }

    Write-Host "backend  -> http://127.0.0.1:$ApiPort  (MCP at /mcp)" -ForegroundColor Cyan
    $backend = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $root -PassThru -NoNewWindow

    if ($BackendOnly) {
        Write-Host 'Ctrl+C to stop.' -ForegroundColor DarkGray
        Wait-Process -Id $backend.Id
        return
    }

    $web = Join-Path $root 'frontend'
    if (-not (Test-Path (Join-Path $web 'node_modules'))) {
        Write-Host 'Installing frontend dependencies (first run only)...' -ForegroundColor DarkGray
        Push-Location $web
        try { & npm install } finally { Pop-Location }
    }

    Write-Host "console  -> http://localhost:$WebPort" -ForegroundColor Cyan
    Write-Host 'Ctrl+C to stop both.' -ForegroundColor DarkGray

    Push-Location $web
    try {
        $env:NEXT_PUBLIC_API_BASE = "http://localhost:$ApiPort"
        & npm run dev -- --port $WebPort
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($backend -and -not $backend.HasExited) {
        Write-Host 'stopping backend...' -ForegroundColor DarkGray
        Stop-Process -Id $backend.Id -Force -ErrorAction SilentlyContinue
    }
}
