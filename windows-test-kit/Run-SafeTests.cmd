@echo off
setlocal DisableDelayedExpansion
title ConfigBackup - safe local workstation tests
echo ConfigBackup 1.1.0 - safe local workstation tests
echo Optional prerequisite setup asks first. Tests do not change policy or upload data.
echo.
if not exist "%~dp0Run-WorkstationTests.py" goto missing
:discover
if defined CONFIGBACKUP_TEST_PYTHON goto custom
if exist "%~dp0.venv\Scripts\python.exe" goto dotvenv
if exist "%~dp0venv\Scripts\python.exe" goto venv
set "CB_TEST_PYTHON=py.exe"
set "CB_TEST_ARGS=-3"
"%CB_TEST_PYTHON%" %CB_TEST_ARGS% -I -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
if not errorlevel 1 goto run
set "CB_TEST_PYTHON=python.exe"
set "CB_TEST_ARGS="
"%CB_TEST_PYTHON%" -I -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
if not errorlevel 1 goto run
goto offerpython
:custom
set "CB_TEST_PYTHON=%CONFIGBACKUP_TEST_PYTHON%"
set "CB_TEST_ARGS="
goto check
:dotvenv
set "CB_TEST_PYTHON=%~dp0.venv\Scripts\python.exe"
set "CB_TEST_ARGS="
goto check
:venv
set "CB_TEST_PYTHON=%~dp0venv\Scripts\python.exe"
set "CB_TEST_ARGS="
:check
"%CB_TEST_PYTHON%" -I -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
if errorlevel 1 goto missing
:run
"%CB_TEST_PYTHON%" %CB_TEST_ARGS% -I -B "%~dp0Run-WorkstationTests.py" --offer-install --offer-components
set "CB_TEST_EXIT=%ERRORLEVEL%"
echo.
echo Launcher exit: %CB_TEST_EXIT% ^(0 PASS, 1 FAIL, 2 REVIEW, 3 INCOMPLETE^)
goto done

:offerpython
if defined CB_TEST_PYTHON_INSTALL_TRIED goto missing
where winget.exe >nul 2>&1
if errorlevel 1 goto missing
echo Python 3.10+ was not found. Optional Python 3.14 current-user installation:
echo winget install --id Python.Python.3.14 --exact --source winget --scope user --interactive --no-upgrade
echo This downloads Python. Review the installer and source/license prompts.
choice /C NY /N /M "Install Python now? [N/Y] (N skips): "
if errorlevel 3 goto missing
if not errorlevel 2 goto missing
set "CB_TEST_PYTHON_INSTALL_TRIED=1"
winget install --id Python.Python.3.14 --exact --source winget --scope user --interactive --no-upgrade
set "CONFIGBACKUP_TEST_PYTHON_INSTALL_EXIT=%ERRORLEVEL%"
echo Python installer exit: %CONFIGBACKUP_TEST_PYTHON_INSTALL_EXIT%
rem Retry known current-user locations; the existing shell PATH may be stale.
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" goto python314
if exist "%LOCALAPPDATA%\Programs\Python\Python314-32\python.exe" goto python314x86
if exist "%LOCALAPPDATA%\Programs\Python\Python314-arm64\python.exe" goto python314arm
goto discover
:python314
set "CB_TEST_PYTHON=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
set "CB_TEST_ARGS="
goto check
:python314x86
set "CB_TEST_PYTHON=%LOCALAPPDATA%\Programs\Python\Python314-32\python.exe"
set "CB_TEST_ARGS="
goto check
:python314arm
set "CB_TEST_PYTHON=%LOCALAPPDATA%\Programs\Python\Python314-arm64\python.exe"
set "CB_TEST_ARGS="
goto check
:missing
echo A required launcher file or usable Python 3.10+ was not found.
echo If you selected an interpreter, check CONFIGBACKUP_TEST_PYTHON.
echo If Python was installed to a custom path, restart this launcher or set its exact path.
echo No tests ran. Read LAUNCHER-HELP.html or LAUNCHER-GUIDE.txt in this folder.
if exist "%~dp0LAUNCHER-HELP.html" start "" "%~dp0LAUNCHER-HELP.html"
set "CB_TEST_EXIT=3"
:done
if not defined CONFIGBACKUP_TEST_NO_PAUSE pause
exit /b %CB_TEST_EXIT%
