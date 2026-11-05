@echo off
rem wowam launcher: finds Python for you. No arguments = open the GUI; otherwise passes arguments to wowam.py.
set "HERE=%~dp0"
set "PYCMD="
py -3 -c "import sys" >nul 2>nul && set "PYCMD=py -3" && set "PYWCMD=pyw -3"
if defined PYCMD goto run
python -c "import sys" >nul 2>nul && set "PYCMD=python" && set "PYWCMD=pythonw"
if defined PYCMD goto run

echo Python 3.8 or newer was not found.
where winget >nul 2>nul
if errorlevel 1 goto manual
choice /c YN /m "Install Python 3.12 now with winget (about 25 MB from python.org)"
if errorlevel 2 goto manual
winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto manual
echo.
echo Python is installed. Close this window and run wowam.bat again.
pause
exit /b 0

:manual
echo Install Python from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this again.
pause
exit /b 1

:run
if "%~1"=="" goto gui
%PYCMD% "%HERE%wowam.py" %*
exit /b %errorlevel%

:gui
start "" %PYWCMD% "%HERE%wowam.py" gui
