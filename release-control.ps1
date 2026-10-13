param(
    [string]$ProjectDir = $PSScriptRoot,
    [string]$SharedDir = "G:\My Drive\Claude\Project\zoom-chat-kb-hub\shared-state",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "sync-common.ps1")

$context = Get-ZoomKbContext -ProjectDir $ProjectDir -SharedDir $SharedDir
$machineId = Get-ZoomKbMachineId
$activeLease = Get-ZoomKbActiveLease -Context $context
if ($null -ne $activeLease -and [string]$activeLease.owner -ne $machineId -and -not $Force) {
    throw "Database is checked out by '$($activeLease.owner)'. Refusing to overwrite its work. Use -Force only after confirming that computer is offline."
}
if (-not (Test-Path -LiteralPath $context.Database)) {
    throw "Local database not found: $($context.Database)"
}

# Refuse to publish over a generation this computer never restored. This catches
# a take-control that ran before Google Drive delivered the newer manifest.
$manifest = Read-ZoomKbJson -Path $context.Manifest
if ($null -ne $manifest -and $null -ne $manifest.generation -and -not $Force) {
    $sharedGeneration = [int64]$manifest.generation
    $lease = Read-ZoomKbJson -Path $context.Lease
    if ($null -eq $lease -or [string]$lease.owner -ne $machineId) {
        throw "Shared generation $sharedGeneration exists but this computer does not hold the lease. Run take-control.ps1 first, or use -Force to overwrite it with this computer's database."
    }
    if ([int64]$lease.generation -lt $sharedGeneration) {
        throw "Shared generation $sharedGeneration was published after this computer took control from generation $($lease.generation). Publishing would discard that work. Use -Force only if this computer's database should win."
    }
}

Write-Host "Stopping the local service before publishing a consistent snapshot..."
Stop-ZoomKbService -Context $context

$temporaryLocal = Join-Path ([IO.Path]::GetTempPath()) ("zoom-kb-{0}.db" -f [guid]::NewGuid().ToString("N"))
$uploadingPath = $null
try {
    # Build, verify and fingerprint the snapshot locally. SQLite never opens the
    # copy on Google Drive, so no -wal/-shm files appear there.
    Backup-ZoomKbDatabase -Context $context -Source $context.Database -Destination $temporaryLocal
    $hash = Get-ZoomKbSha256 -Path $temporaryLocal
    $size = (Get-Item -LiteralPath $temporaryLocal).Length

    $generation = 1
    if ($null -ne $manifest -and $null -ne $manifest.generation) {
        $generation = [int64]$manifest.generation + 1
    }
    $timestamp = [DateTimeOffset]::UtcNow
    $safeMachine = ($env:COMPUTERNAME -replace '[^A-Za-z0-9_.-]', '_')
    $snapshotName = "zoom-kb-{0:D8}-{1}-{2}.db" -f $generation, $timestamp.ToString("yyyyMMddTHHmmssZ"), $safeMachine
    $snapshotPath = Join-Path $context.SnapshotDir $snapshotName
    $uploadingPath = Join-Path $context.SnapshotDir (".{0}.uploading" -f $snapshotName)

    Copy-Item -LiteralPath $temporaryLocal -Destination $uploadingPath -Force
    if ((Get-ZoomKbSha256 -Path $uploadingPath) -ne $hash) {
        throw "The snapshot copied to Google Drive does not match the local snapshot."
    }
    Move-Item -LiteralPath $uploadingPath -Destination $snapshotPath -Force
    $uploadingPath = $null

    $newManifest = [ordered]@{
        format_version = 1
        generation = $generation
        snapshot = "snapshots/$snapshotName"
        sha256 = $hash
        size_bytes = $size
        published_at = $timestamp.ToString("o")
        published_by = $machineId
    }
    Write-ZoomKbJsonAtomic -Path $context.Manifest -Value $newManifest

    if (Test-Path -LiteralPath $context.Lease) {
        $lease = Read-ZoomKbJson -Path $context.Lease
        if ($Force -or $null -eq $lease -or [string]$lease.owner -eq $machineId) {
            Remove-Item -LiteralPath $context.Lease -Force
        }
    }

    Write-Host "Database released to Google Drive."
    Write-Host "Generation: $generation"
    Write-Host "Snapshot: $snapshotPath"
    Write-Host "SHA-256: $hash"
    Write-Host "The local service remains stopped. Wait until Google Drive reports 'Up to date' before taking control on another computer."
}
finally {
    Remove-ZoomKbDatabaseFiles -Path $temporaryLocal
    if ($null -ne $uploadingPath) {
        Remove-Item -LiteralPath $uploadingPath -Force -ErrorAction SilentlyContinue
    }
}
