$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectDir ".venv\Scripts\python.exe"
$dataDir = Join-Path $projectDir "data"
$pidPath = Join-Path $dataDir "zoom-kb-service.json"
$stdoutPath = Join-Path $dataDir "zoom-kb-service.stdout.log"
$stderrPath = Join-Path $dataDir "zoom-kb-service.stderr.log"
$healthUrl = "http://127.0.0.1:8765/api/health"

function Get-RecordedProcess {
    if (-not (Test-Path -LiteralPath $pidPath)) {
        return $null
    }

    try {
        $state = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
        $process = Get-Process -Id ([int]$state.pid) -ErrorAction Stop
        $recordedStart = [DateTimeOffset]::Parse([string]$state.started_at).UtcDateTime
        $actualStart = $process.StartTime.ToUniversalTime()
        if ([Math]::Abs(($actualStart - $recordedStart).TotalSeconds) -le 2) {
            return $process
        }
    }
    catch {
        # A missing process or invalid state file is treated as stale state.
    }

    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    return $null
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Virtual environment not found. Run the Setup commands in README.md first."
}

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null

$existingProcess = Get-RecordedProcess
if ($null -ne $existingProcess) {
    Write-Host "Zoom Chat Knowledge Hub is already running (PID $($existingProcess.Id))."
    Write-Host "Open http://127.0.0.1:8765"
    exit 0
}

$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -ne $listener) {
    throw "Port 8765 is already in use by PID $($listener.OwningProcess). Stop that process or change the service port."
}

$process = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList @("-m", "uvicorn", "zoom_kb.app:app", "--host", "127.0.0.1", "--port", "8765", "--no-access-log") `
    -WorkingDirectory $projectDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

$state = [ordered]@{
    pid = $process.Id
    started_at = $process.StartTime.ToUniversalTime().ToString("o")
    project_dir = $projectDir
}
$state | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($process.HasExited) {
        break
    }
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 1
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}

if (-not $ready) {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    $details = if (Test-Path -LiteralPath $stderrPath) {
        (Get-Content -LiteralPath $stderrPath -Tail 20) -join [Environment]::NewLine
    } else {
        "No error log was created."
    }
    throw "Service did not become ready.`n$details"
}

Write-Host "Zoom Chat Knowledge Hub started (PID $($process.Id))."
Write-Host "Open http://127.0.0.1:8765"
Write-Host "Logs: $stdoutPath and $stderrPath"
