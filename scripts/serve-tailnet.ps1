# Serve usb-analyzer-mcp over streamable HTTP on this machine's Tailscale IPv4 only.
# Reachable from devices on the tailnet; never bound to LAN/public interfaces.
param(
    [int]$Port = 8765,
    [int]$WaitForTailscaleSec = 120
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$ts = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $ts) { $ts = "C:\Program Files\Tailscale\tailscale.exe" }

# Wait for Tailscale to come up after logon.
$ip = $null
$deadline = (Get-Date).AddSeconds($WaitForTailscaleSec)
while (-not $ip -and (Get-Date) -lt $deadline) {
    $ip = (& $ts ip -4 2>$null | Select-Object -First 1)
    if ($ip) { $ip = $ip.Trim() }
    if (-not ($ip -match '^100\.')) { $ip = $null; Start-Sleep 5 }
}
if (-not $ip) { throw "Tailscale IPv4 not available" }

# Use the venv interpreter directly: `uv run` would try to re-sync and fail while
# another copy of the server (e.g. a stdio instance) holds the entry-point exe.
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run 'uv sync' in $repo first" }

$log = Join-Path $env:LOCALAPPDATA "usb-analyzer-mcp"
New-Item -ItemType Directory -Force $log | Out-Null
Write-Host "usb-analyzer-mcp on http://${ip}:${Port}/mcp"
# Start-Process (not &): PowerShell 5.1 turns a native command's stderr logging into errors.
$p = Start-Process -FilePath $python -WorkingDirectory $repo -NoNewWindow -PassThru `
    -ArgumentList "-m", "usb_analyzer_mcp", "--transport", "streamable-http", "--host", $ip, "--port", "$Port" `
    -RedirectStandardOutput (Join-Path $log "server.out.log") -RedirectStandardError (Join-Path $log "server.log")
$p.WaitForExit()
exit $p.ExitCode
