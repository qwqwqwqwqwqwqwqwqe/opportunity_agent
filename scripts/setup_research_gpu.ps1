[CmdletBinding()]
param([switch]$CheckOnly)

$ErrorActionPreference = "Stop"
$researchRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$researchVenv = Join-Path $researchRoot ".venv-research"
$researchPython = Join-Path $researchVenv "Scripts\python.exe"
Set-Location $researchRoot

if (-not $CheckOnly) {
    if (-not (Test-Path -LiteralPath $researchPython)) {
        & python -m venv $researchVenv
        if ($LASTEXITCODE -ne 0) { throw "Research venv creation failed" }
    }
    & $researchPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
    # A separate environment keeps the application's existing CPU torch intact.
    & $researchPython -m pip install "torch==2.8.0" --index-url https://download.pytorch.org/whl/cu128
    if ($LASTEXITCODE -ne 0) { throw "CUDA PyTorch installation failed" }
    & $researchPython -m pip install -e ".[dev,v2,resume,rag,observability]"
    if ($LASTEXITCODE -ne 0) { throw "Research dependencies installation failed" }
}
if (-not (Test-Path -LiteralPath $researchPython)) { $researchPython = "python" }
& $researchPython -c "import torch; print({'torch':torch.__version__, 'cuda':torch.cuda.is_available()}); assert torch.cuda.is_available(), 'CUDA unavailable'; x=torch.ones(16,device='cuda'); print({'gpu':torch.cuda.get_device_name(0),'tensor_sum':x.sum().item()})"
if ($LASTEXITCODE -ne 0) { throw "CUDA execution check failed" }
$env:RERANKER_DEVICE = "cuda"
$env:RERANKER_BATCH_SIZE = "4"
$env:RESEARCH_ALLOW_MODEL_DOWNLOAD = if ($CheckOnly) { "0" } else { "1" }
& $researchPython -m opportunity_agent.v2.rag.models
if ($LASTEXITCODE -ne 0) { throw "BGE GPU inference warmup failed" }
Write-Host "Research GPU ready. Python: $researchPython"
