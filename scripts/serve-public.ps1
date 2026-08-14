<#
.SYNOPSIS
    Open a tunnel and serve Agent Rooms on it, ready for ChatGPT. One command.

.DESCRIPTION
    Replaces a four-step dance that is easy to get wrong:

      1. set a token in one shell,
      2. start a tunnel in another and copy its URL,
      3. set PUBLIC_BASE_URL to that URL and restart the server -- in a shell that also
         has the token, which the second shell did not,
      4. remember the token to paste at the consent screen.

    This does all of it: starts the tunnel, waits for the URL, exports the environment the
    server needs, starts the server, and prints the two values you paste into ChatGPT.

    Ctrl+C stops both.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\serve-public.ps1

.EXAMPLE
    # Reuse a token you already have, so existing OAuth grants keep working.
    powershell -ExecutionPolicy Bypass -File scripts\serve-public.ps1 -Token $env:DEV_BOOTSTRAP_TOKEN
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$Token = "",
    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

# --- interpreter -------------------------------------------------------------
$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host 'No virtualenv at backend\.venv. Create one first:' -ForegroundColor Yellow
    Write-Host '  python -m venv backend\.venv'
    Write-Host '  backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt'
    exit 1
}
if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
    Write-Host 'npx not found (needed for the tunnel). Install Node.js.' -ForegroundColor Yellow
    exit 1
}

# --- credential --------------------------------------------------------------
# Generated here rather than left to the caller, because the published default refuses to
# start on a public URL and a hand-typed value tends to be weak.
if (-not $Token) {
    $Token = -join ((48..57) + (97..122) | Get-Random -Count 40 | ForEach-Object { [char]$_ })
}
$env:DEV_BOOTSTRAP_TOKEN = $Token
$env:MCP_REQUIRE_AUTH = 'true'

$tunnelLog = Join-Path $env:TEMP "agent-rooms-tunnel-$PID.log"
$tunnel = $null
$server = $null

try {
    Write-Host "Opening a tunnel to http://localhost:$Port ..." -ForegroundColor Cyan
    $tunnel = Start-Process -FilePath 'npx.cmd' `
        -ArgumentList '--yes', 'cloudflared', 'tunnel', '--url', "http://localhost:$Port" `
        -PassThru -NoNewWindow -RedirectStandardOutput $tunnelLog -RedirectStandardError "$tunnelLog.err"

    # cloudflared prints the URL once the tunnel is up; the first run also downloads the
    # binary, so allow generous time before giving up.
    $publicUrl = $null
    $deadline = (Get-Date).AddSeconds(120)
    while (-not $publicUrl -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 800
        if ($tunnel.HasExited) { break }
        foreach ($file in @($tunnelLog, "$tunnelLog.err")) {
            if (-not (Test-Path $file)) { continue }
            $match = Select-String -Path $file -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' `
                -AllMatches -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($match) { $publicUrl = $match.Matches[0].Value; break }
        }
    }

    if (-not $publicUrl) {
        Write-Host 'Could not obtain a tunnel URL. Tunnel output:' -ForegroundColor Yellow
        foreach ($file in @($tunnelLog, "$tunnelLog.err")) {
            if (Test-Path $file) { Get-Content $file -Tail 20 }
        }
        exit 1
    }

    $env:PUBLIC_BASE_URL = $publicUrl
    Write-Host ''
    Write-Host '  Tunnel is up.' -ForegroundColor Green
    Write-Host ''
    Write-Host '  Paste into ChatGPT -> New Plugin:' -ForegroundColor Cyan
    Write-Host "    Connection (Server URL) : $publicUrl/mcp"
    Write-Host '    Authentication          : OAuth   (leave discovery alone)'
    Write-Host ''
    Write-Host '  Paste at the consent screen ("prove it is you"):' -ForegroundColor Cyan
    Write-Host "    $Token"
    Write-Host ''
    Write-Host '  Then name the identity, e.g. "ChatGPT (Alan)". It cannot rename itself.' -ForegroundColor DarkGray
    Write-Host ''

    Write-Host "Starting the server on $publicUrl ..." -ForegroundColor Cyan
    $server = Start-Process -FilePath $python `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--port', $Port, '--app-dir', 'backend' `
        -PassThru -NoNewWindow

    if (-not $SkipVerify) {
        # Wait for readiness, then walk the whole OAuth + MCP flow. This is what catches a
        # mismatched PUBLIC_BASE_URL, a Host-allowlist rejection, or a failed discovery
        # document -- all of which otherwise surface as an opaque failure inside ChatGPT.
        Start-Sleep -Seconds 6
        Write-Host 'Verifying the flow end to end...' -ForegroundColor Cyan
        & $python (Join-Path $root 'scripts\verify_oauth_flow.py') $publicUrl $Token
        if ($LASTEXITCODE -ne 0) {
            Write-Host ''
            Write-Host 'Verification failed - do not bother trying ChatGPT yet.' -ForegroundColor Yellow
            Write-Host 'See docs\CONNECT_CHATGPT.md section 8 for what each failure means.' -ForegroundColor Yellow
        }
        else {
            Write-Host ''
            Write-Host '  Verified. ChatGPT will be able to connect.' -ForegroundColor Green
        }
    }

    Write-Host ''
    Write-Host 'Running. Ctrl+C to stop both.' -ForegroundColor DarkGray
    Wait-Process -Id $server.Id
}
finally {
    foreach ($proc in @($server, $tunnel)) {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
    # The tunnel URL is a secret while it is up; do not leave it lying in TEMP.
    foreach ($file in @($tunnelLog, "$tunnelLog.err")) {
        if (Test-Path $file) { Remove-Item -Force -LiteralPath $file -ErrorAction SilentlyContinue }
    }
    Write-Host 'Stopped. The tunnel URL is now dead; re-run to get a new one.' -ForegroundColor DarkGray
}
