Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-ZoomKbMachineId {
    return "$env:COMPUTERNAME\$env:USERNAME"
}

function Get-ZoomKbContext {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectDir,
        [Parameter(Mandatory = $true)]
        [string]$SharedDir
    )

    $resolvedProject = (Resolve-Path -LiteralPath $ProjectDir).Path
    New-Item -ItemType Directory -Path $SharedDir -Force | Out-Null
    $resolvedShared = (Resolve-Path -LiteralPath $SharedDir).Path
    $dataDir = Join-Path $resolvedProject "data"
    $pythonPath = Join-Path $resolvedProject ".venv\Scripts\python.exe"
    $startScript = Join-Path $resolvedProject "start.ps1"
    $stopScript = Join-Path $resolvedProject "stop.ps1"
    $databasePath = Join-Path $dataDir "zoom_kb.db"
    $sharedRoot = $resolvedShared
    $snapshotDir = Join-Path $sharedRoot "snapshots"
    $manifestPath = Join-Path $sharedRoot "current.json"
    $leasePath = Join-Path $sharedRoot "lease.json"
    $helperPath = Join-Path $PSScriptRoot "db-sync.py"

    foreach ($required in @($pythonPath, $startScript, $stopScript, $helperPath)) {
        if (-not (Test-Path -LiteralPath $required)) {
            throw "Required file not found: $required"
        }
    }

    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    New-Item -ItemType Directory -Path $snapshotDir -Force | Out-Null

    return [pscustomobject]@{
        ProjectDir = $resolvedProject
        DataDir = $dataDir
        Python = $pythonPath
        StartScript = $startScript
        StopScript = $stopScript
        Database = $databasePath
        SharedRoot = $sharedRoot
        SnapshotDir = $snapshotDir
        Manifest = $manifestPath
        Lease = $leasePath
        Helper = $helperPath
    }
}

function Read-ZoomKbJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Write-ZoomKbJsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $temporary = Join-Path $directory (".{0}.{1}.tmp" -f (Split-Path -Leaf $Path), [guid]::NewGuid().ToString("N"))
    try {
        $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Stop-ZoomKbService {
    param([Parameter(Mandatory = $true)]$Context)
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Context.StopScript
    if ($LASTEXITCODE -ne 0) {
        throw "stop.ps1 failed with exit code $LASTEXITCODE."
    }
}

function Start-ZoomKbService {
    param([Parameter(Mandatory = $true)]$Context)
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Context.StartScript
    if ($LASTEXITCODE -ne 0) {
        throw "start.ps1 failed with exit code $LASTEXITCODE."
    }
}

function Test-ZoomKbDatabase {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)][string]$Path
    )
    & $Context.Python $Context.Helper verify $Path
    if ($LASTEXITCODE -ne 0) {
        throw "SQLite integrity verification failed: $Path"
    }
}

function Get-ZoomKbSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ZoomKbActiveLease {
    param([Parameter(Mandatory = $true)]$Context)
    $lease = Read-ZoomKbJson -Path $Context.Lease
    if ($null -eq $lease) {
        return $null
    }
    try {
        $expiresAt = [DateTimeOffset]::Parse([string]$lease.expires_at)
    }
    catch {
        throw "The shared lease file is invalid: $($Context.Lease)"
    }
    if ($expiresAt -le [DateTimeOffset]::UtcNow) {
        return $null
    }
    return $lease
}

function Assert-ZoomKbLeaseAvailable {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [switch]$Force
    )
    $lease = Get-ZoomKbActiveLease -Context $Context
    if ($null -eq $lease) {
        return
    }
    $machineId = Get-ZoomKbMachineId
    if ([string]$lease.owner -eq $machineId) {
        return
    }
    if (-not $Force) {
        throw "Database is checked out by '$($lease.owner)' until $($lease.expires_at). Run release-control.ps1 there first. Use -Force only after confirming that computer is not running the service."
    }
}
