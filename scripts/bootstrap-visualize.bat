@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ABIST Knowledge Base -- kb-visualize rendering environment bootstrap.
rem Creates .venv-visualize (Python 3.11 + Manim) next to the repo root.
rem
rem The main .venv is uv-managed and pinned to Python 3.12 (pyproject.toml);
rem `uv sync` would prune anything pip-installed there, so Manim needs its own
rem interpreter. manim_runner.resolve_python() looks for it at
rem <root>\.venv-visualize\Scripts\python.exe.
rem
rem Usage:
rem   scripts\bootstrap-visualize.bat
rem   scripts\bootstrap-visualize.bat --recreate
rem   scripts\bootstrap-visualize.bat --check-only
rem   scripts\bootstrap-visualize.bat --root C:\path\to\kb

set "ROOT="
set "RECREATE=0"
set "CHECK_ONLY=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--recreate" (
  set "RECREATE=1"
  shift
  goto parse_args
)
if /I "%~1"=="--check-only" (
  set "CHECK_ONLY=1"
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
echo Valid: --root DIR  --recreate  --check-only
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

set "VENV=%ROOT%\.venv-visualize"
set "VPY=%VENV%\Scripts\python.exe"
set "REQ=%ROOT%\requirements-visualize.txt"

echo ==^> root: %ROOT%

if "%CHECK_ONLY%"=="1" goto check

rem `python` on Windows is a Microsoft Store stub; always go through the `py` launcher.
where py >nul 2>&1
if errorlevel 1 (
  echo [ERROR] The `py` launcher was not found.
  echo         Install Python 3.11 from https://www.python.org/downloads/
  echo         Do not use bare `python` -- it is a Microsoft Store stub.
  exit /b 3
)

py -3.11 -V >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python 3.11 not found via the `py` launcher.
  echo         Install it with: winget install Python.Python.3.11
  exit /b 4
)

if "%RECREATE%"=="1" (
  if exist "%VENV%" (
    echo ==^> removing existing venv: %VENV%
    rmdir /s /q "%VENV%"
  )
)

if exist "%VPY%" (
  echo ==^> venv already exists: %VENV%
  echo     pass --recreate to rebuild it
) else (
  echo ==^> creating venv: %VENV%
  py -3.11 -m venv "%VENV%" || (
    echo [ERROR] venv creation failed.
    exit /b 5
  )
)

if not exist "%REQ%" (
  echo [ERROR] Not found: %REQ%
  exit /b 6
)

echo ==^> upgrading pip
"%VPY%" -m pip install --disable-pip-version-check -q -U pip || (
  echo [ERROR] pip upgrade failed.
  exit /b 7
)

echo ==^> installing %REQ%
"%VPY%" -m pip install --disable-pip-version-check -q -r "%REQ%" || (
  echo [ERROR] dependency install failed.
  exit /b 8
)

:check
if not exist "%VPY%" (
  echo [ERROR] venv not found: %VENV%
  echo         Run scripts\bootstrap-visualize.bat without --check-only first.
  exit /b 9
)

rem ffmpeg is required for mp4 output but is not a Python dependency; warn only.
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [WARN] ffmpeg not found on PATH. mp4 output will fail; png still works.
  echo        Install it with: winget install Gyan.FFmpeg.Essentials
)

echo ==^> diagnostics
"%VPY%" "%ROOT%\tools\visualize\check_deps.py"
if errorlevel 1 (
  echo [ERROR] check_deps.py failed.
  exit /b 10
)

echo.
echo Done. kb-visualize can now render.
exit /b 0
