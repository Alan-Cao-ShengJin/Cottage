<#
.SYNOPSIS
    Expose the local Agent Rooms server on a public HTTPS URL so ChatGPT can reach it.

.DESCRIPTION
    ChatGPT calls your server from OpenAI's infrastructure, so `localhost` is invisible
    to it. This opens a tunnel and prints the public URL, then tells you the two things
    to paste into ChatGPT.

    Uses `npx` so nothing has to be installed. Cloudflare's quick tunnel is the default
    because it needs no account; localtunnel is the fallback.

    SECURITY: the server refuses to start publicly while BOOTSTRAP_OPERATOR is on with the
    published default token (see app/config.py). Set a real secret first:

        $env:OPERATOR_TOKEN = -join ((48..57)+(97..122) | Get-Random -Count 40 | % {[char]$_})

    Treat the tunnel URL as a secret too: anyone holding it plus a token can act in your
    rooms. Quick tunnels are unauthenticated and world-reachable.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\tunnel.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [ValidateSet('cloudflare', 'localtunnel')]
    [string]$Provider = 'cloudflare'
)

$ErrorActionPreference = 'Continue'

if (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
    Write-Host 'npx not found. Install Node.js, or install cloudflared directly.' -ForegroundColor Yellow
    exit 1
}

# Warn about the exact footgun the startup guard blocks, before they hit it.
if (-not $env:OPERATOR_TOKEN -or $env:OPERATOR_TOKEN -eq 'dev-owner-token') {
    Write-Host ''
    Write-Host 'WARNING: OPERATOR_TOKEN is unset or still the published default.' -ForegroundColor Yellow
    Write-Host 'The server will refuse to start once PUBLIC_BASE_URL is public. Set one:' -ForegroundColor Yellow
    Write-Host '  $env:OPERATOR_TOKEN = -join ((48..57)+(97..122) | Get-Random -Count 40 | % {[char]$_})' -ForegroundColor DarkGray
    Write-Host ''
}

Write-Host "Opening a $Provider tunnel to http://localhost:$Port ..." -ForegroundColor Cyan
Write-Host 'Watch for the public https:// URL below, then:' -ForegroundColor DarkGray
Write-Host '  1. stop the backend, set $env:PUBLIC_BASE_URL to that URL, restart it' -ForegroundColor DarkGray
Write-Host '     (the MCP url and the Action schema are both built from it)' -ForegroundColor DarkGray
Write-Host '  2. MCP connector    -> <public-url>/mcp' -ForegroundColor DarkGray
Write-Host '  3. GPT Action schema -> <public-url>/openapi-gpt.json' -ForegroundColor DarkGray
Write-Host ''

if ($Provider -eq 'cloudflare') {
    # `cloudflared` on npm ships the binary; `tunnel --url` is the account-free mode.
    & npx --yes cloudflared tunnel --url "http://localhost:$Port"
}
else {
    & npx --yes localtunnel --port $Port
}
