@echo off
setlocal EnableExtensions

set "ROOT_DIR=%~dp0"
set "SCRIPT_PATH=%ROOT_DIR%lmstudio_weight_watcher.py"
set "LOG_PATH=%ROOT_DIR%watcher-launch.log"
set "HIDDEN_LAUNCH=0"

if /I "%~1"=="--hidden-launch" (
    set "HIDDEN_LAUNCH=1"
    shift
)

if not exist "%SCRIPT_PATH%" (
    call :fail "Watcher script not found: %SCRIPT_PATH%"
    exit /b 1
)

set "PYTHON_EXE="
set "USE_PY_LAUNCHER=0"

if defined LMSTUDIO_WATCHER_PYTHON (
    if exist "%LMSTUDIO_WATCHER_PYTHON%" (
        set "PYTHON_EXE=%LMSTUDIO_WATCHER_PYTHON%"
    ) else (
        call :fail "LMSTUDIO_WATCHER_PYTHON is set but does not exist: %LMSTUDIO_WATCHER_PYTHON%"
        exit /b 1
    )
)

if not defined PYTHON_EXE if exist "%ROOT_DIR%.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
)

if not defined PYTHON_EXE if exist "%ROOT_DIR%venv\Scripts\python.exe" (
    set "PYTHON_EXE=%ROOT_DIR%venv\Scripts\python.exe"
)

if not defined PYTHON_EXE (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=python"
    )
)

if not defined PYTHON_EXE (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "USE_PY_LAUNCHER=1"
    )
)

if not defined PYTHON_EXE if "%USE_PY_LAUNCHER%"=="0" (
    call :fail "Could not find Python. Install Python and make sure `py` or `python` works, or set LMSTUDIO_WATCHER_PYTHON to a full python.exe path."
    exit /b 1
)

pushd "%ROOT_DIR%" >nul
if "%USE_PY_LAUNCHER%"=="1" (
    py -3 "%SCRIPT_PATH%" %*
) else (
    "%PYTHON_EXE%" "%SCRIPT_PATH%" %*
)
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul

if not "%EXIT_CODE%"=="0" (
    call :fail "LM Studio Weight Watcher exited with code %EXIT_CODE%."
)

exit /b %EXIT_CODE%

:fail
if "%HIDDEN_LAUNCH%"=="1" (
    >>"%LOG_PATH%" echo [%date% %time%] %~1
) else (
    echo %~1
)
exit /b 0
