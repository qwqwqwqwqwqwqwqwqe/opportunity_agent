[CmdletBinding()]
param(
    [string]$DatabasePath = (Join-Path $PSScriptRoot "..\data\v2.db"),
    [int]$Port = 8000,
    [switch]$Reload
)

# This starts the implemented V2 foundation without Docker.  SQLite is suitable
# for local verification only; use PostgreSQL + pgvector before deploying RAG.
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
# The two-argument GetFullPath overload is unavailable in Windows PowerShell
# 5/.NET Framework, so join relative paths before normalising them.
if ([System.IO.Path]::IsPathRooted($DatabasePath)) {
    $databaseFullPath = [System.IO.Path]::GetFullPath($DatabasePath)
} else {
    $databaseFullPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $DatabasePath))
}
$databaseDirectory = Split-Path -Parent $databaseFullPath
New-Item -ItemType Directory -Force -Path $databaseDirectory | Out-Null

$env:DATABASE_URL = "sqlite+aiosqlite:///" + ($databaseFullPath -replace "\\", "/")
$env:AUTO_CREATE_SCHEMA = "0"
if ([string]::IsNullOrWhiteSpace($env:JWT_SECRET)) {
    # A fresh process-local secret keeps a local demo safe enough to start. Set
    # JWT_SECRET yourself if cookies must survive a server restart.
    $env:JWT_SECRET = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
    Write-Warning "Generated a temporary JWT_SECRET. Set JWT_SECRET for persistent local sessions."
}

Set-Location $projectRoot
& python -m alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "V2 database migration failed" }
$arguments = @("-m", "uvicorn", "opportunity_agent.v2.api.app:app", "--host", "127.0.0.1", "--port", "$Port")
if ($Reload) { $arguments += "--reload" }
& python @arguments
