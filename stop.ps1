$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidPath = Join-Path $projectDir "data\zoom-kb-service.json"

if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Host "Zoom Chat Knowledge Hub is not running (no service state file)."
    exit 0
}

try {
    $state = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
    $process = Get-Process -Id ([int]$state.pid) -ErrorAction Stop
}
catch {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    Write-Host "Zoom Chat Knowledge Hub is not running; stale service state was removed."
    exit 0
}

$recordedStart = [DateTimeOffset]::Parse([string]$state.started_at).UtcDateTime
$actualStart = $process.StartTime.ToUniversalTime()
if ([Math]::Abs(($actualStart - $recordedStart).TotalSeconds) -gt 2) {
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    throw "The recorded PID now belongs to a different process. It was not stopped; stale service state was removed."
}

Stop-Process -Id $process.Id -Force
$process.WaitForExit()
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue

Write-Host "Zoom Chat Knowledge Hub stopped (PID $($process.Id))."
