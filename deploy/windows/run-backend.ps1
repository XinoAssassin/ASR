<#
.SYNOPSIS
  Run the ASR backend (`asr serve`) on Windows without Docker.

.DESCRIPTION
  One-time setup (PowerShell, in the repository's backend folder):
    winget install --id=astral-sh.uv -e        # or: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    cd D:\ASR\app\backend                 # repository checked out to D:\ASR\app; state goes to D:\ASR\data etc.
    uv sync --frozen --no-dev --extra cpu --extra local    # NVIDIA: --extra cuda instead of --extra cpu

  Models (explicit, once; same cache dir as below):
    $env:ASR_MODEL_CACHE_DIR = 'D:\ASR\models'
    .\.venv\Scripts\asr.exe models download fsmn-vad campplus qwen3-forced-aligner qwen3-asr-0.6b --mirror

  Then run this script, e.g.:
    powershell -ExecutionPolicy Bypass -File deploy\windows\run-backend.ps1 -BindHost 0.0.0.0 -ApiKeyFile D:\ASR\api-key.txt

  The key is read from a file (or from an ASR_API_KEY already set in the environment or in .env),
  never passed on the command line, where other local users could see it.

  Listening on 0.0.0.0 requires an API key and a Windows Firewall rule for the port, e.g. (admin):
    New-NetFirewallRule -DisplayName "ASR backend" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private

  To start it at logon without a console window, register a scheduled task that runs
  `powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File <this script> ...`.
  Guide: docs/DEPLOYMENT.md.
#>
param(
    # Repository root (the folder that contains backend\). Defaults to two levels above this script.
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    # Writable state; keep it outside the repository.
    [string]$DataDir = 'D:\ASR\data',
    # Allowed upload roots, ';'-separated. Must not contain $DataDir.
    [string]$UploadRoots = 'D:\ASR\uploads',
    [string]$ModelCacheDir = 'D:\ASR\models',
    [string]$BindHost = '127.0.0.1',
    [int]$Port = 8000,
    # File holding the shared X-API-Key (one line). Without it, ASR_API_KEY from the environment
    # or .env is used; no key at all = auth off: loopback clients only, and -BindHost must be
    # loopback (asr serve refuses anything else).
    [string]$ApiKeyFile = '',
    # Browser origins of the SPA, comma-separated. The desktop app needs http://tauri.localhost.
    [string]$CorsOrigins = 'http://localhost:8080,http://127.0.0.1:8080,http://tauri.localhost',
    [ValidateSet('cpu', 'cuda')][string]$Device = 'cpu',
    [ValidateSet('text', 'json')][string]$LogFormat = 'text',
    [string]$LogFile = 'D:\ASR\logs\asr.log'
)

$ErrorActionPreference = 'Stop'
$backend = Join-Path $Root 'backend'
$asr = Join-Path $backend '.venv\Scripts\asr.exe'
if (-not (Test-Path $asr)) {
    throw "missing $asr; run 'uv sync --frozen --no-dev --extra cpu --extra local' in $backend first"
}
if ($ApiKeyFile) {
    $env:ASR_API_KEY = (Get-Content -Raw -Encoding utf8 $ApiKeyFile).Trim()
}
if ($BindHost -ne '127.0.0.1' -and $BindHost -ne 'localhost' -and -not $env:ASR_API_KEY) {
    # A key only in .env is fine too; with no key at all, asr serve refuses a non-loopback
    # -BindHost and exits (an unauthenticated API must not be reachable from the network).
    Write-Warning 'Listening beyond loopback needs ASR_API_KEY (-ApiKeyFile or .env); asr serve refuses to start without one (docs/DEPLOYMENT.md).'
}

$logDir = if ($LogFile) { Split-Path $LogFile -Parent } else { '' }
foreach ($dir in @($DataDir, $ModelCacheDir, $logDir) + ($UploadRoots -split ';')) {
    if ($dir) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
}

# Process-scoped variables: nothing is written to the user's or the machine's environment.
$env:ASR_DATA_DIR = $DataDir
$env:ASR_UPLOAD_ROOTS = $UploadRoots
$env:ASR_MODEL_CACHE_DIR = $ModelCacheDir
$env:MODELSCOPE_CACHE = Join-Path $ModelCacheDir '.cache\modelscope'
$env:HF_HOME = Join-Path $ModelCacheDir '.cache\huggingface'
$env:ASR_CORS_ORIGINS = $CorsOrigins
$env:ASR_DEVICE = $Device
$env:ASR_LOG_FORMAT = $LogFormat
if ($LogFile) { $env:ASR_LOG_FILE = $LogFile }
$env:PYTHONUTF8 = '1'
# DASHSCOPE_API_KEY (cloud provider) is read from the environment or from backend\.env / .env;
# it can also be stored from the Settings page. Never pass it on the command line.

Set-Location $backend
& $asr serve --host $BindHost --port $Port
exit $LASTEXITCODE
