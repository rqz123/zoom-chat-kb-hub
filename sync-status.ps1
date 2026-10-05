param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$SharedDir = "G:\My Drive\Claude\Project\zoom-chat-kb-hub\shared-state"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "sync-common.ps1")

$context = Get-ZoomKbContext -ProjectDir $ProjectDir -SharedDir $SharedDir
$manifest = Read-ZoomKbJson -Path $context.Manifest
$lease = Read-ZoomKbJson -Path $context.Lease

Write-Host "This computer: $(Get-ZoomKbMachineId)"
if ($null -eq $manifest) {
    Write-Host "Shared snapshot: none"
} else {
    Write-Host "Shared generation: $($manifest.generation)"
    Write-Host "Published by: $($manifest.published_by)"
    Write-Host "Published at: $($manifest.published_at)"
    Write-Host "Snapshot: $($manifest.snapshot)"
}

if ($null -eq $lease) {
    Write-Host "Lease: available"
} else {
    $expiresAt = [DateTimeOffset]::Parse([string]$lease.expires_at)
    $state = "active"
    if ($expiresAt -le [DateTimeOffset]::UtcNow) {
        $state = "expired"
    }
    Write-Host "Lease: $state"
    Write-Host "Owner: $($lease.owner)"
    Write-Host "Expires: $($lease.expires_at)"
}

$serviceStatePath = Join-Path $context.DataDir "zoom-kb-service.json"
$running = $false
if (Test-Path -LiteralPath $serviceStatePath) {
    try {
        $serviceState = Read-ZoomKbJson -Path $serviceStatePath
        $running = $null -ne (Get-Process -Id ([int]$serviceState.pid) -ErrorAction SilentlyContinue)
    } catch {
        $running = $false
    }
}
Write-Host "Local service running: $running"
