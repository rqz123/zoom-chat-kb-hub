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

Write-Host "Stopping the local service before publishing a consistent snapshot..."
Stop-ZoomKbService -Context $context

$temporaryLocal = Join-Path ([IO.Path]::GetTempPath()) ("zoom-kb-{0}.db" -f [guid]::NewGuid().ToString("N"))
$uploadingPath = $null
try {
    & $context.Python $context.Helper backup $context.Database $temporaryLocal
    if ($LASTEXITCODE -ne 0) {
        throw "SQLite backup failed with exit code $LASTEXITCODE."
    }

    $manifest = Read-ZoomKbJson -Path $context.Manifest
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
    Test-ZoomKbDatabase -Context $context -Path $uploadingPath
    $hash = Get-ZoomKbSha256 -Path $uploadingPath
    $size = (Get-Item -LiteralPath $uploadingPath).Length
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
    Remove-Item -LiteralPath $temporaryLocal -Force -ErrorAction SilentlyContinue
    if ($null -ne $uploadingPath) {
        Remove-Item -LiteralPath $uploadingPath -Force -ErrorAction SilentlyContinue
    }
}
