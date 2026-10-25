[CmdletBinding()]
param(
    [string]$DatabasePath = (Join-Path $PSScriptRoot "..\data\applyops.db"),
    [int]$Port = 8000,
    [switch]$Reload,
    [switch]$Worker
)
$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($env:DATABASE_URL)) {
    if (-not [System.IO.Path]::IsPathRooted($DatabasePath)) { $DatabasePath = Join-Path $projectRoot $DatabasePath }
    $databaseFullPath = [System.IO.Path]::GetFullPath($DatabasePath)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $databaseFullPath) | Out-Null
    $env:DATABASE_URL = "sqlite+aiosqlite:///" + ($databaseFullPath -replace "\\", "/")
}
$env:APP_MODE = "applyops"
$env:AUTO_CREATE_SCHEMA = "0"
$env:MEMORY_VECTOR_ENABLED = "0"
if ([string]::IsNullOrWhiteSpace($env:JWT_SECRET)) {
    $env:JWT_SECRET = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    Write-Warning "Using a temporary JWT_SECRET; set JWT_SECRET to preserve sessions across restarts."
}
Set-Location $projectRoot
if ($Worker) { & python -m opportunity_agent.v3.worker; exit $LASTEXITCODE }
& python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "ApplyOps database migration failed" }
$uvicornArgs = @("-m", "uvicorn", "opportunity_agent.v3.app:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Reload) { $uvicornArgs += "--reload" }
& python @uvicornArgs
