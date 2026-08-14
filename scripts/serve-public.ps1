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

# --- the port must be free BEFORE we burn a tunnel URL -----------------------
# Learned the hard way: with the port already taken, the server died at startup, the
# tunnel happily forwarded to whatever *else* was listening, and verification reported a
# confusing `307` from a stale build instead of "the port was in use". Fail here, loudly,
# before anything is exposed.
function Get-PortHolder {
    param([int]$PortNumber)
    try {
        $conn = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            return [pscustomobject]@{
                Pid  = $conn.OwningProcess
                Name = if ($proc) { $proc.ProcessName } else { 'unknown' }
            }
        }
    }
    catch {
        # Get-NetTCPConnection throws when nothing matches; fall through to the bind probe.
    }
    # Fallback for environments without the cmdlet: try to bind it ourselves.
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $PortNumber)
        $listener.Start()
        $listener.Stop()
        return $null
    }
    catch {
        return [pscustomobject]@{ Pid = 0; Name = 'unknown' }
    }
}

$holder = Get-PortHolder -PortNumber $Port
if ($holder) {
    Write-Host ''
    Write-Host "Port $Port is already in use by PID $($holder.Pid) ($($holder.Name))." -ForegroundColor Yellow
    Write-Host 'That is usually a server left over from an earlier run. Either stop it:' -ForegroundColor Yellow
    Write-Host "    Stop-Process -Id $($holder.Pid) -Force" -ForegroundColor DarkGray
    Write-Host '  or use a different port:' -ForegroundColor Yellow
    Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\serve-public.ps1 -Port 8001" -ForegroundColor DarkGray
    exit 1
}

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

    # Confirm *our* server is the one answering before trusting any verification result.
    # Without this the script once verified against a stale process on the same port and
    # reported its unrelated failure as ours.
    $ready = $false
    $deadline = (Get-Date).AddSeconds(30)
    while (-not $ready -and (Get-Date) -lt $deadline) {
        if ($server.HasExited) { break }
        Start-Sleep -Milliseconds 700
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
            if ($health.protocol -eq 'arp/1') { $ready = $true }
        }
        catch {
            # not up yet
        }
    }

    if ($server.HasExited) {
        Write-Host ''
        Write-Host 'The server exited during startup - see its output above.' -ForegroundColor Yellow
        Write-Host 'A startup guard may have refused to run; the message says which.' -ForegroundColor Yellow
        exit 1
    }
    if (-not $ready) {
        Write-Host ''
        Write-Host "The server did not answer on http://127.0.0.1:$Port within 30s." -ForegroundColor Yellow
        exit 1
    }

    if (-not $SkipVerify) {
        # Walk the whole OAuth + MCP flow. This catches a mismatched PUBLIC_BASE_URL, a
        # Host-allowlist rejection, or a failed discovery document -- all of which
        # otherwise surface as an opaque failure inside ChatGPT.
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
    # Guarded: a server that has already exited would make Wait-Process throw a red error
    # on top of whatever actually went wrong.
    if (-not $server.HasExited) {
        Wait-Process -Id $server.Id -ErrorAction SilentlyContinue
    }
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
