@echo off
setlocal EnableExtensions
REM ============================================================
REM  MiniMax H3 Creative Workbench - Windows packaging script
REM
REM  Usage:
REM    scripts\build_plugin.bat                build frontend + pack zip
REM    scripts\build_plugin.bat --no-build     skip frontend build (reuse web/dist)
REM
REM  Output: dist_package\ComfyUI-MiniMaxH3-Studio_v{version}_{stamp}.zip
REM  Contents (runtime only): __init__.py, nodes/, studio/, requirements.txt, web/dist
REM  Deps: node + npm (for frontend build). zipping uses built-in PowerShell.
REM ============================================================

set "ROOT=%~dp0.."
cd /d "%ROOT%"

set "NO_BUILD="
if /I "%1"=="--no-build" set "NO_BUILD=1"

REM ---------- read version ----------
for /f "delims=" %%v in ('node -e "const p=require('./web/package.json');process.stdout.write(String(p.version||'0.0.0'))" 2^>nul') do set "VERSION=%%v"
if not defined VERSION set "VERSION=0.0.0"

REM ---------- timestamp ----------
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%t"

set "OUT_DIR=%ROOT%\dist_package"
set "ZIP=%OUT_DIR%\ComfyUI-MiniMaxH3-Studio_v%VERSION%_%STAMP%.zip"

REM ---------- build frontend ----------
if not defined NO_BUILD (
    echo [1/2] Building frontend ^(npm run build^) ...
    if not exist "%ROOT%\web\node_modules" (
        echo   web\node_modules missing, running npm install ...
        pushd "%ROOT%\web"
        call npm install
        if errorlevel 1 goto :fail
        popd
    )
    pushd "%ROOT%\web"
    call npm run build
    if errorlevel 1 goto :fail
    popd
    if not exist "%ROOT%\web\dist\minimax-h3-studio.js" (
        echo   Frontend build failed: web\dist\minimax-h3-studio.js missing
        goto :fail
    )
) else (
    if not exist "%ROOT%\web\dist\minimax-h3-studio.js" (
        echo   Warning: web\dist\minimax-h3-studio.js missing; --no-build gives empty frontend
    )
)

REM ---------- assemble runtime snapshot + pack (PowerShell) ----------
echo [2/2] Collecting runtime files and packing ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$root=(Resolve-Path '%ROOT%').Path;" ^
  "$stage=Join-Path $env:TEMP ('mh3pkg_' + [guid]::NewGuid().ToString('N'));" ^
  "$pkg=Join-Path $stage 'ComfyUI-MiniMaxH3-Studio';" ^
  "New-Item -ItemType Directory -Force -Path $pkg,$pkg\web,$pkg\nodes,$pkg\studio,$pkg\web\dist | Out-Null;" ^
  "Copy-Item (Join-Path $root '__init__.py'),(Join-Path $root 'requirements.txt') $pkg;" ^
  "$nodes=Get-ChildItem (Join-Path $root 'nodes') -Recurse -File | Where-Object { $_.FullName -notmatch '__pycache__' };" ^
  "foreach($f in $nodes){ $rel=$f.FullName.Substring($root.Length).TrimStart('\'); $dst=Join-Path $pkg $rel; New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null; Copy-Item $f.FullName $dst };" ^
  "$studio=Get-ChildItem (Join-Path $root 'studio') -Recurse -File | Where-Object { $_.FullName -notmatch '__pycache__' };" ^
  "foreach($f in $studio){ $rel=$f.FullName.Substring($root.Length).TrimStart('\'); $dst=Join-Path $pkg $rel; New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null; Copy-Item $f.FullName $dst };" ^
  "Get-ChildItem (Join-Path $root 'web\dist') -File | ForEach-Object { Copy-Item $_.FullName (Join-Path $pkg 'web\dist') };" ^
  "New-Item -ItemType Directory -Force -Path (Join-Path $root 'dist_package') | Out-Null;" ^
  "Compress-Archive -Path (Join-Path $stage 'ComfyUI-MiniMaxH3-Studio') -DestinationPath '%ZIP%' -CompressionLevel Optimal;" ^
  "Remove-Item -Recurse -Force $stage"
if errorlevel 1 goto :fail

echo   Packed: %ZIP%
echo.
echo Publish notes:
echo   - Upload the zip above to a GitHub Release; users unzip into ComfyUI\custom_nodes\ to run (frontend included).
echo   - Deps: pip install -r custom_nodes\ComfyUI-MiniMaxH3-Studio\requirements.txt
echo   - Do NOT release after source changes without rebuilding (frontend/backend version mismatch).
exit /b 0

:fail
echo Packaging failed (errorlevel %ERRORLEVEL%)
exit /b 1
