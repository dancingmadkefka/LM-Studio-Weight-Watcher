@echo off
setlocal

set "ROOT_DIR=%~dp0"
set "PYTHON_EXE="

if defined LMSTUDIO_WATCHER_PYTHON (
    set "PYTHON_EXE=%LMSTUDIO_WATCHER_PYTHON%"
)

if not defined PYTHON_EXE if exist "%USERPROFILE%\miniforge3\envs\weightupdater\python.exe" (
    set "PYTHON_EXE=%USERPROFILE%\miniforge3\envs\weightupdater\python.exe"
)

if not defined PYTHON_EXE if exist "%USERPROFILE%\miniforge3\python.exe" (
    set "PYTHON_EXE=%USERPROFILE%\miniforge3\python.exe"
)

if not defined PYTHON_EXE (
    set "PYTHON_EXE=python"
)

pushd "%ROOT_DIR%"
"%PYTHON_EXE%" "%ROOT_DIR%lmstudio_weight_watcher.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd

exit /b %EXIT_CODE%
