param([ValidateSet('Start','Test','Stop')][string]$Action='Test', [ValidateRange(1,100)][int]$Rounds=10)
$ErrorActionPreference='Stop'
$workspace=Split-Path -Parent $PSScriptRoot
Push-Location $workspace
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        $dockerBin=Join-Path $env:LOCALAPPDATA 'Programs/DockerDesktop/resources/bin'
        if (Test-Path -LiteralPath (Join-Path $dockerBin 'docker.exe')) { $env:PATH=$dockerBin+';'+$env:PATH }
        else { throw 'Docker CLI unavailable in this shell.' }
    }
    $composeArgs=@('compose','-p','applyops-concurrency','-f','compose.concurrency.yml')
    if ($Action -eq 'Start') {
        New-Item -ItemType Directory -Path 'data/concurrency-control' -Force | Out-Null
        & docker @composeArgs up --build -d --wait
        if ($LASTEXITCODE -ne 0) { throw 'Test environment startup failed; inspect Compose logs.' }
    } elseif ($Action -eq 'Test') {
        & python tests/concurrency/run.py --rounds $Rounds
        if ($LASTEXITCODE -ne 0) { throw 'Concurrency tests failed; retain data/concurrency-reports evidence.' }
    } else {
        # Stop only this named project; intentionally retain its database volume.
        & docker @composeArgs down
        if ($LASTEXITCODE -ne 0) { throw 'Test environment shutdown failed.' }
    }
} finally { Pop-Location }
