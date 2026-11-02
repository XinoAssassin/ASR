<#
.SYNOPSIS
  Build the portable release zips of ASR Recorder (Windows x64).

.DESCRIPTION
  Produces in desktop\build\dist\ (or -OutDir):
    ASR-Recorder-<version>-win-x64-portable.zip
        asr-recorder.exe, WebView2Loader.dll (GNU toolchain builds), portable marker, README.txt
    ASR-Recorder-<version>-win-x64-with-backend-portable.zip
        the same + managed-backend\ (backend source, uv.exe, VERSION), the resource folder the
        exe looks for next to itself (Tauri's resource_dir on Windows is the exe's folder)

  Both zips contain one top-level folder; the app keeps all its state inside it (portable
  marker). One exe serves both editions: the edition is the presence of managed-backend\.

  Steps: frontend build (pnpm), release exe (cargo, tauri/custom-protocol), backend staging
  (scripts\stage-backend.py: source copy + pinned, checksum-verified uv.exe), zips.
  -Installer additionally builds the NSIS installers of both editions (not used for releases).

  Needs: rustup / cargo on PATH, pnpm, Python 3.11+ (py or python). Honours CARGO_TARGET_DIR
  and the proxy variables of the calling shell.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File desktop\scripts\package-portable.ps1
#>
param(
  [string]$OutDir = "",
  [switch]$SkipFrontend,
  [switch]$SkipBuild,
  [switch]$Installer
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3

$Desktop = Split-Path -Parent $PSScriptRoot
$Repo = Split-Path -Parent $Desktop
$Tauri = Join-Path $Desktop "src-tauri"
if (-not $OutDir) { $OutDir = Join-Path $Desktop "build\dist" }
$Target = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { Join-Path $Tauri "target" }
$Conf = Get-Content (Join-Path $Tauri "tauri.conf.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$Version = $Conf.version

function Exec([string]$what, [scriptblock]$cmd) {
  Write-Host "== $what"
  # Windows PowerShell turns a native tool's stderr into error records: do not stop on them,
  # judge by the exit code.
  $ErrorActionPreference = "Continue"
  & $cmd 2>&1 | ForEach-Object { "$_" }
  if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" }
}

function Find-Python {
  foreach ($c in @(@("py", "-3"), @("python"), @("python3"))) {
    $exe = Get-Command $c[0] -ErrorAction SilentlyContinue
    if ($exe) { return , $c }
  }
  throw "Python 3.11+ not found (py / python)"
}

if (-not $SkipFrontend) {
  Exec "frontend install" { pnpm --dir (Join-Path $Repo "frontend") install --frozen-lockfile }
  Exec "frontend build" { pnpm --dir (Join-Path $Repo "frontend") build }
}
if (-not $SkipBuild) {
  Push-Location $Tauri
  try { Exec "release exe" { cargo build --release --features tauri/custom-protocol } }
  finally { Pop-Location }
}
$py = Find-Python
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs = $py[1..($py.Count - 1)] }
Exec "stage backend" { & $py[0] @pyArgs -I (Join-Path $PSScriptRoot "stage-backend.py") }

$Exe = Join-Path $Target "release\asr-recorder.exe"
if (-not (Test-Path $Exe)) { throw "missing $Exe" }
$Loader = Join-Path $Target "release\WebView2Loader.dll"
$Staged = Join-Path $Desktop "build\managed-backend"

$Readme = @"
ASR Recorder $Version (portable)
================================

Run asr-recorder.exe. Nothing is installed: the app keeps everything in this folder
(because of the file "portable" next to the exe):

  data\app\      settings, backend connection, API key, upload queue
  data\logs\     logs (app, sidecar, managed backend setup)
  data\webview\  the window's browser profile (web UI settings)
  recordings\    recordings (default folder; another one can be chosen in the app)
{0}
Moving the folder (another drive, a USB stick) is fine: stored paths follow it at the next start
{1}.

To remove the app, quit it (tray icon -> 退出 / Quit) and delete this folder.
WARNING: deleting the folder also deletes everything listed above{2}.
Copy recordings you want to keep first.

Requirements: Windows 10 2004+ or Windows 11, x64. The window uses Microsoft Edge WebView2,
which Windows 11 includes. On Windows 10 without it, install the "Evergreen Runtime" from
https://developer.microsoft.com/microsoft-edge/webview2/ (a zip cannot carry the installer).

The exe is not code-signed: SmartScreen may warn on first start ("More info" -> "Run anyway"),
or right-click the zip -> Properties -> Unblock before unpacking.
"@

$BackendLines = @"
  backend\       local backend: Python environment created on first run (1-5 GB)
  data\asr\      local backend data: database, uploaded audio, transcripts
  models\        speech models (several GB; another folder, e.g. on a bigger drive, can be
                 chosen in the setup wizard)
  managed-backend\  backend source and uv shipped with this zip (do not edit)

First run: the setup wizard asks for CPU / NVIDIA (CUDA) / AMD (ROCm) and downloads the Python
environment once (about 1 GB for CPU, 2.5 GB CUDA, 5 GB ROCm; needs internet). Speech models are
downloaded afterwards in Settings -> local models.
"@

function New-PortableZip([string]$name, [bool]$withBackend) {
  $stage = Join-Path $OutDir "stage"
  $dir = Join-Path $stage $name
  if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  Copy-Item $Exe $dir
  if (Test-Path $Loader) { Copy-Item $Loader $dir }
  Set-Content -Path (Join-Path $dir "portable") -Encoding ASCII -Value "This file makes ASR Recorder keep all its data in this folder. Do not delete it."
  if ($withBackend) {
    Copy-Item -Recurse $Staged (Join-Path $dir "managed-backend")
    $text = $Readme -f $BackendLines, " (the Python environment is rebuilt from its local cache)", ", including the local backend's database, uploads and the downloaded models unless you chose another models folder"
  } else {
    $text = $Readme -f "", "", ""
  }
  Set-Content -Path (Join-Path $dir "README.txt") -Encoding UTF8 -Value $text
  $zip = Join-Path $OutDir "$name.zip"
  if (Test-Path $zip) { Remove-Item -Force $zip }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  [System.IO.Compression.ZipFile]::CreateFromDirectory($dir, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true)
  Remove-Item -Recurse -Force $stage
  $h = (Get-FileHash $zip -Algorithm SHA256).Hash
  "{0}  {1:N1} MB  SHA-256 {2}" -f $zip, ((Get-Item $zip).Length / 1MB), $h
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-PortableZip "ASR-Recorder-$Version-win-x64-portable" $false
New-PortableZip "ASR-Recorder-$Version-win-x64-with-backend-portable" $true

if ($Installer) {
  Push-Location $Desktop
  try {
    Exec "tauri CLI install" { pnpm install --frozen-lockfile }
    # The SPA is already built; a file, because PowerShell 5.1 strips the quotes of a JSON argument.
    $plain = Join-Path $Desktop "build\tauri.no-before-build.json"
    Set-Content -Path $plain -Encoding ASCII -Value '{"build":{"beforeBuildCommand":""}}'
    Exec "NSIS installer (recorder)" { pnpm tauri build --config $plain }
    $nsis = Join-Path $Target "release\bundle\nsis"
    $plainExe = Get-ChildItem $nsis -Filter "*_x64-setup.exe" | Select-Object -First 1
    Copy-Item $plainExe.FullName $OutDir
    Exec "NSIS installer (with backend)" { pnpm tauri build --config src-tauri/tauri.bundled.conf.json --config $plain }
    $withExe = Get-ChildItem $nsis -Filter "*_x64-setup.exe" | Select-Object -First 1
    $dest = Join-Path $OutDir ($withExe.Name -replace "_x64-setup\.exe$", "_x64_with-backend-setup.exe")
    Copy-Item $withExe.FullName $dest -Force
    Get-ChildItem $OutDir -Filter "*setup.exe" | ForEach-Object { "{0}  {1:N1} MB" -f $_.FullName, ($_.Length / 1MB) }
  } finally { Pop-Location }
}
"PACKAGE OK"
