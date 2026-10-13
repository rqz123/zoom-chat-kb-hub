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
$incomingPath = $null

# Running take again while already holding the lease must not roll back the work
# done here since that generation was restored.
$ownLease = Get-ZoomKbActiveLease -Context $context
$alreadyCurrent = $null -ne $manifest -and $null -ne $ownLease -and
    [string]$ownLease.owner -eq $machineId -and
    [int64]$ownLease.generation -ge [int64]$manifest.generation -and
    (Test-Path -LiteralPath $context.Database)

try {
    if ($alreadyCurrent) {
        Write-Host "This computer already holds generation $($ownLease.generation); keeping the local database."
        $restoredGeneration = [int64]$ownLease.generation
    }
    elseif ($null -ne $manifest) {
        $snapshotRelative = [string]$manifest.snapshot
        if ([string]::IsNullOrWhiteSpace($snapshotRelative) -or $snapshotRelative.Contains("..") -or [IO.Path]::IsPathRooted($snapshotRelative)) {
            throw "The shared manifest contains an invalid snapshot path."
        }
        $snapshotPath = Join-Path $context.SharedRoot ($snapshotRelative.Replace('/', [IO.Path]::DirectorySeparatorChar))
        if (-not (Test-Path -LiteralPath $snapshotPath)) {
            throw "The manifest arrived before its snapshot. Wait for Google Drive to finish syncing: $snapshotPath"
        }

        # Copy first and check only the local copy: the hash then covers exactly
        # the bytes that get restored, and SQLite never opens a file on Google Drive.
        $incomingPath = Join-Path $context.DataDir ("zoom_kb.{0}.incoming" -f [guid]::NewGuid().ToString("N"))
        Copy-Item -LiteralPath $snapshotPath -Destination $incomingPath -Force
        if ((Get-Item -LiteralPath $incomingPath).Length -ne [int64]$manifest.size_bytes) {
            throw "Snapshot size mismatch. Google Drive may still be syncing; wait and try again."
        }
        if ((Get-ZoomKbSha256 -Path $incomingPath) -ne ([string]$manifest.sha256).ToLowerInvariant()) {
            throw "Snapshot hash mismatch. Google Drive may still be syncing; wait and try again."
        }
        Test-ZoomKbDatabase -Context $context -Path $incomingPath

        Write-Host "Stopping the local service before restoring generation $($manifest.generation)..."
        Stop-ZoomKbService -Context $context

        if (Test-Path -LiteralPath $context.Database) {
            # A forced stop leaves committed transactions in -wal. The SQLite backup
            # API folds them into the copy; moving only the main file would drop them.
            $localBackupDir = Join-Path $context.DataDir "sync-local-backups"
            $backupName = "zoom_kb-before-generation-{0}-{1}.db" -f $manifest.generation, [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
            Backup-ZoomKbDatabase -Context $context -Source $context.Database -Destination (Join-Path $localBackupDir $backupName)
        }
        # The local database is safely backed up. Its sidecar files must not be
        # paired with the database restored from another computer.
        Remove-ZoomKbDatabaseFiles -Path $context.Database
        Move-Item -LiteralPath $incomingPath -Destination $context.Database
        Test-ZoomKbDatabase -Context $context -Path $context.Database
        $restoredGeneration = [int64]$manifest.generation
    }
    elseif (Test-Path -LiteralPath $context.Database) {
        Write-Warning "No shared snapshot found at $($context.Manifest). Keeping this computer's database. If another computer already released one, wait for Google Drive to sync and run take again."
    }
    else {
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
finally {
    if ($null -ne $incomingPath) {
        Remove-ZoomKbDatabaseFiles -Path $incomingPath
    }
}
