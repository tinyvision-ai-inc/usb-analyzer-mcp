# One-time setup (run in an elevated PowerShell) to expose usb-analyzer-mcp to the
# Tailscale network only:
#   1. Windows Firewall: allow TCP <Port> inbound ONLY from tailnet addresses
#      (100.64.0.0/10 and fd7a:115c:a1e0::/48). No LAN, no internet.
#   2. Scheduled task: start serve-tailnet.ps1 at logon of the current user
#      (the suite is a GUI COM server and needs the interactive session).
# Tailscale Funnel/Serve is NOT used, so nothing is published publicly.
#
# Usage:   powershell -ExecutionPolicy Bypass -File scripts\install-tailnet.ps1 [-User DOMAIN\name] [-Port 8765]
# Remove:  powershell -ExecutionPolicy Bypass -File scripts\install-tailnet.ps1 -Uninstall
param(
    [int]$Port = 8765,
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$ruleName = "usb-analyzer-mcp (tailnet only)"
$taskName = "usb-analyzer-mcp (tailnet)"
$serve = Join-Path $PSScriptRoot "serve-tailnet.ps1"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this from an elevated (Administrator) PowerShell." }

Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
if ($Uninstall) { Write-Host "Removed firewall rule and scheduled task."; return }

New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -RemoteAddress "100.64.0.0/10", "fd7a:115c:a1e0::/48" -Profile Any | Out-Null
Write-Host "Firewall: TCP $Port allowed from tailnet ranges only."

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$serve`" -Port $Port"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Serve usb-analyzer-mcp on the Tailscale IP only" | Out-Null
Write-Host "Scheduled task '$taskName' will start the server at logon of $User."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName '$taskName'"
