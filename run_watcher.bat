@echo off
setlocal EnableExtensions

set "ROOT_DIR=%~dp0"
set "SCRIPT_PATH=%ROOT_DIR%lmstudio_weight_watcher.py"
set "LOG_PATH=%ROOT_DIR%watcher-launch.log"
set "ERR_LOG_PATH=%ROOT_DIR%watcher-error.log"
set "HIDDEN_LAUNCH=0"
set "PYTHON_EXE="
set "USE_PY_LAUNCHER=0"
set "LAST_CANDIDATE="
set "LAST_IS_PY=0"

if /I "%~1"=="--hidden-launch" (
    set "HIDDEN_LAUNCH=1"
    shift
)

if not exist "%SCRIPT_PATH%" (
    call :fail "Watcher script not found: %SCRIPT_PATH%"
    exit /b 1
)

pushd "%ROOT_DIR%" >nul
if defined LMSTUDIO_WATCHER_PYTHON (
    if not exist "%LMSTUDIO_WATCHER_PYTHON%" (
        call :fail "LMSTUDIO_WATCHER_PYTHON does not exist: %LMSTUDIO_WATCHER_PYTHON%"
        popd >nul
        exit /b 1
    )
    call :probe_python "%LMSTUDIO_WATCHER_PYTHON%" "LMSTUDIO_WATCHER_PYTHON"
    if errorlevel 1 goto failed
    goto launch
)

if exist "%ROOT_DIR%.venv\Scripts\python.exe" (
    call :probe_python "%ROOT_DIR%.venv\Scripts\python.exe" ".venv"
    if not errorlevel 1 goto launch
)
if exist "%ROOT_DIR%venv\Scripts\python.exe" (
    call :probe_python "%ROOT_DIR%venv\Scripts\python.exe" "venv"
    if not errorlevel 1 goto launch
)
where python >nul 2>nul
if not errorlevel 1 (
    call :probe_python "python" "PATH python"
    if not errorlevel 1 goto launch
)
where py >nul 2>nul
if not errorlevel 1 (
    call :probe_py
    if not errorlevel 1 goto launch
)
if not defined LAST_CANDIDATE (
    call :fail "Could not find Python. Set LMSTUDIO_WATCHER_PYTHON to a python.exe path."
    popd >nul
    exit /b 1
)

:failed
call :fail "No usable Python interpreter. Tried candidates are recorded in watcher-error.log."
rem Re-run the last failed candidate normally so a hidden tray launch shows its
rem dependency error in a Windows message box. --once remains non-modal.
if "%LAST_IS_PY%"=="1" (
    if "%HIDDEN_LAUNCH%"=="1" (py -3 "%SCRIPT_PATH%" %* >>"%LOG_PATH%" 2>>"%ERR_LOG_PATH%") else (py -3 "%SCRIPT_PATH%" %*)
) else (
    if "%HIDDEN_LAUNCH%"=="1" ("%LAST_CANDIDATE%" "%SCRIPT_PATH%" %* >>"%LOG_PATH%" 2>>"%ERR_LOG_PATH%") else ("%LAST_CANDIDATE%" "%SCRIPT_PATH%" %*)
)
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
exit /b %EXIT_CODE%

:launch
if "%HIDDEN_LAUNCH%"=="1" (
    if "%USE_PY_LAUNCHER%"=="1" (py -3 "%SCRIPT_PATH%" %* >>"%LOG_PATH%" 2>>"%ERR_LOG_PATH%") else ("%PYTHON_EXE%" "%SCRIPT_PATH%" %* >>"%LOG_PATH%" 2>>"%ERR_LOG_PATH%")
) else (
    if "%USE_PY_LAUNCHER%"=="1" (py -3 "%SCRIPT_PATH%" %*) else ("%PYTHON_EXE%" "%SCRIPT_PATH%" %*)
)
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul
if not "%EXIT_CODE%"=="0" call :fail "LM Studio Weight Watcher exited with code %EXIT_CODE%."
exit /b %EXIT_CODE%

:probe_python
set "LAST_CANDIDATE=%~1"
set "LAST_IS_PY=0"
call "%~1" "%SCRIPT_PATH%" --preflight-only 2>>"%ERR_LOG_PATH%"
if errorlevel 1 (
    call :candidate_failed "%~2" "%~1"
    exit /b 1
)
set "PYTHON_EXE=%~1"
exit /b 0

:probe_py
set "LAST_CANDIDATE=py -3"
set "LAST_IS_PY=1"
py -3 "%SCRIPT_PATH%" --preflight-only 2>>"%ERR_LOG_PATH%"
if errorlevel 1 (
    call :candidate_failed "py -3" "py -3"
    exit /b 1
)
set "USE_PY_LAUNCHER=1"
exit /b 0

:candidate_failed
>>"%ERR_LOG_PATH%" echo Preflight rejected %~1: %~2
call :fail "Preflight rejected %~1: %~2"
exit /b 0

:fail
if "%HIDDEN_LAUNCH%"=="1" (
    >>"%LOG_PATH%" echo [%date% %time%] %~1
) else (
    echo %~1
)
exit /b 0
