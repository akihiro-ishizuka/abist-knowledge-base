@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ABIST Knowledge Base bootstrap (init + register + index)
rem Usage:
rem   scripts\bootstrap.bat
rem   scripts\bootstrap.bat --skip-embed
rem   scripts\bootstrap.bat --dry-run-register
rem   scripts\bootstrap.bat --root C:\path\to\kb

set "ROOT="
set "SKIP_EMBED=0"
set "DRY_RUN_REGISTER=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--skip-embed" (
  set "SKIP_EMBED=1"
  shift
  goto parse_args
)
if /I "%~1"=="--dry-run-register" (
  set "DRY_RUN_REGISTER=1"
  shift
  goto parse_args
)
if /I "%~1"=="--root" (
  if "%~2"=="" (
    echo [ERROR] --root requires a path.
    exit /b 2
  )
  set "ROOT=%~f2"
  shift
  shift
  goto parse_args
)
echo [ERROR] Unknown argument: %~1
echo Valid: --root DIR  --skip-embed  --dry-run-register
exit /b 2

:args_done
if not defined ROOT (
  rem This .bat lives under scripts\; move to repo root.
  pushd "%~dp0.." || exit /b 1
  set "ROOT=%CD%"
  popd
)

cd /d "%ROOT%" || (
  echo [ERROR] Cannot cd to root: %ROOT%
  exit /b 1
)

where uv >nul 2>&1
if errorlevel 1 (
  echo [ERROR] uv not found. Install from https://docs.astral.sh/uv/
  exit /b 3
)

echo ==> root: %ROOT%
echo ==> uv sync
uv sync
if errorlevel 1 exit /b 1

echo ==> abist-kb init
uv run abist-kb --root "%ROOT%" init
if errorlevel 1 exit /b 1

if not exist "%ROOT%\docs\" (
  echo [ERROR] docs\ is missing. init may have failed.
  exit /b 1
)

if "%DRY_RUN_REGISTER%"=="1" (
  echo ==> document register-disk ^(dry-run^)
  uv run abist-kb --root "%ROOT%" document register-disk
  if errorlevel 1 exit /b 1
  echo.
  echo Dry-run only. Re-run without --dry-run-register to apply.
  echo Put .md files under docs\ first.
  exit /b 0
)

echo ==> document register-disk --apply
uv run abist-kb --root "%ROOT%" document register-disk --apply
if errorlevel 1 exit /b 1

echo ==> index build --corpus work
uv run abist-kb --root "%ROOT%" index build --corpus work
if errorlevel 1 exit /b 1

if "%SKIP_EMBED%"=="1" (
  echo ==> skipped index embed
) else (
  echo ==> index embed --corpus work
  uv run abist-kb --root "%ROOT%" index embed --corpus work
  if errorlevel 1 exit /b 1
)

echo.
echo Done. Example search:
echo   uv run abist-kb --root "%ROOT%" search "keyword"
exit /b 0
