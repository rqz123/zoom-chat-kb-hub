param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$SharedDir = "G:\My Drive\Claude\Project\zoom-chat-kb-hub\shared-state",
    [int]$LeaseHours = 168,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "sync-common.ps1")

if ($LeaseHours -lt 1) {
    throw "LeaseHours must be at least 1."
}

$context = Get-ZoomKbContext -ProjectDir $ProjectDir -SharedDir $SharedDir
$machineId = Get-ZoomKbMachineId
Assert-ZoomKbLeaseAvailable -Context $context -Force:$Force
$manifest = Read-ZoomKbJson -Path $context.Manifest
$restoredGeneration = 0
$leaseWritten = $false

try {
    if ($null -ne $manifest) {
        $snapshotRelative = [string]$manifest.snapshot
        if ([string]::IsNullOrWhiteSpace($snapshotRelative) -or $snapshotRelative.Contains("..") -or [IO.Path]::IsPathRooted($snapshotRelative)) {
            throw "The shared manifest contains an invalid snapshot path."
        }
        $snapshotPath = Join-Path $context.SharedRoot ($snapshotRelative.Replace('/', [IO.Path]::DirectorySeparatorChar))
        if (-not (Test-Path -LiteralPath $snapshotPath)) {
            throw "The manifest arrived before its snapshot. Wait for Google Drive to finish syncing: $snapshotPath"
        }
        $actualHash = Get-ZoomKbSha256 -Path $snapshotPath
        if ($actualHash -ne ([string]$manifest.sha256).ToLowerInvariant()) {
            throw "Snapshot hash mismatch. Google Drive may still be syncing; wait and try again."
        }
        if ((Get-Item -LiteralPath $snapshotPath).Length -ne [int64]$manifest.size_bytes) {
            throw "Snapshot size mismatch. Google Drive may still be syncing; wait and try again."
        }
        Test-ZoomKbDatabase -Context $context -Path $snapshotPath

        Write-Host "Stopping the local service before restoring generation $($manifest.generation)..."
        Stop-ZoomKbService -Context $context

        $incomingPath = Join-Path $context.DataDir ("zoom_kb.{0}.incoming" -f [guid]::NewGuid().ToString("N"))
        $localBackupDir = Join-Path $context.DataDir "sync-local-backups"
        New-Item -ItemType Directory -Path $localBackupDir -Force | Out-Null
        try {
            Copy-Item -LiteralPath $snapshotPath -Destination $incomingPath -Force
            Test-ZoomKbDatabase -Context $context -Path $incomingPath
            if (Test-Path -LiteralPath $context.Database) {
                $backupName = "zoom_kb-before-generation-{0}-{1}.db" -f $manifest.generation, [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
                Move-Item -LiteralPath $context.Database -Destination (Join-Path $localBackupDir $backupName)
            }
            # The service has stopped, so any remaining SQLite sidecar files are stale.
            # They must not be paired with the database restored from another computer.
            Remove-Item -LiteralPath "$($context.Database)-wal" -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath "$($context.Database)-shm" -Force -ErrorAction SilentlyContinue
            Move-Item -LiteralPath $incomingPath -Destination $context.Database
            Test-ZoomKbDatabase -Context $context -Path $context.Database
        }
        finally {
            Remove-Item -LiteralPath $incomingPath -Force -ErrorAction SilentlyContinue
        }
        $restoredGeneration = [int64]$manifest.generation
    }
    elseif (-not (Test-Path -LiteralPath $context.Database)) {
        throw "No shared snapshot or local database exists. Initialize the project on one computer first."
    }

    $now = [DateTimeOffset]::UtcNow
    $lease = [ordered]@{
        format_version = 1
        owner = $machineId
        acquired_at = $now.ToString("o")
        expires_at = $now.AddHours($LeaseHours).ToString("o")
        generation = $restoredGeneration
        project_dir = $context.ProjectDir
    }
    Write-ZoomKbJsonAtomic -Path $context.Lease -Value $lease
    $leaseWritten = $true

    Start-ZoomKbService -Context $context
    Write-Host "Control acquired by $machineId."
    Write-Host "Restored generation: $restoredGeneration"
    Write-Host "Lease expires: $($lease.expires_at)"
    Write-Host "Open http://127.0.0.1:8765"
}
catch {
    if ($leaseWritten -and (Test-Path -LiteralPath $context.Lease)) {
        $writtenLease = Read-ZoomKbJson -Path $context.Lease
        if ($null -ne $writtenLease -and [string]$writtenLease.owner -eq $machineId) {
            Remove-Item -LiteralPath $context.Lease -Force -ErrorAction SilentlyContinue
        }
    }
    throw
}
