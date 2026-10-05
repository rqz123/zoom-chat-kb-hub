$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Virtual environment not found. Run the Setup commands in README.md first."
}

& ".\.venv\Scripts\python.exe" -m uvicorn zoom_kb.app:app --host 127.0.0.1 --port 8765 --no-access-log
