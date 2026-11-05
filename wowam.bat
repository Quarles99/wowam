@echo off
rem wowam launcher: finds Python for you. No arguments = open the GUI; otherwise passes arguments to wowam.py.
set "HERE=%~dp0"
set "PYCMD="
py -3 -c "import sys" >nul 2>nul && set "PYCMD=py -3" && set "PYWCMD=pyw -3"
if defined PYCMD goto run
python -c "import sys" >nul 2>nul && set "PYCMD=python" && set "PYWCMD=pythonw"
if defined PYCMD goto run
echo Python 3.8 or newer was not found. Install it from https://www.python.org/downloads/ and run this again.
pause
exit /b 1

:run
if "%~1"=="" goto gui
%PYCMD% "%HERE%wowam.py" %*
exit /b %errorlevel%

:gui
start "" %PYWCMD% "%HERE%wowam.py" gui
