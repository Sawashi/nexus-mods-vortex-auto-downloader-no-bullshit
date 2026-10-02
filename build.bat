@echo off
rem Builds dist\NexusAutoDownloader.exe: the program, Python and the libraries in one file, so whoever runs it needs
rem no Python.
rem
rem   build.bat             one .exe file (it unpacks itself at every start)
rem   build.bat --onedir    a folder with the .exe in it (starts faster)
rem   build.bat --console   also shows a console window, to see why a build will not start
rem
rem Building needs Python 3.14. If it is not on this computer, it is installed from the python-3.14.x-amd64.exe that
rem lies next to this file, into the .python folder here (for this user only: no admin rights, PATH is not touched).
setlocal
cd /d "%~dp0"

rem Double-clicked? Then keep the window open at the end, so the result can be read.
set "CLICKED="
echo %CMDCMDLINE% | find /i "%~nx0" >nul && set "CLICKED=1"

rem Find Python 3.14: a copy installed here earlier, the py launcher, the usual install folders, then PATH.
set "PY="
if exist ".python\python.exe" set "PY=%CD%\.python\python.exe"
if not defined PY for /f "delims=" %%P in ('py -3.14 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY if exist "%ProgramFiles%\Python314\python.exe" set "PY=%ProgramFiles%\Python314\python.exe"
if not defined PY python -c "import sys; sys.exit(sys.version_info[:2] != (3, 14))" >nul 2>&1 && set "PY=python"
if defined PY goto :build

rem Not found: install it from the installer that came with the project.
set "INSTALLER="
for %%F in (python-3.14.*-amd64.exe) do set "INSTALLER=%%F"
if not defined INSTALLER goto :no_python
echo Python 3.14 was not found. Installing it from %INSTALLER% into the .python folder (this takes a minute) ...
start "" /wait "%CD%\%INSTALLER%" /quiet InstallAllUsers=0 "TargetDir=%CD%\.python" Include_launcher=0 InstallLauncherAllUsers=0 Include_test=0 Include_doc=0 Include_pip=1 Include_tcltk=1 AssociateFiles=0 Shortcuts=0 PrependPath=0
if not exist ".python\python.exe" goto :install_failed
set "PY=%CD%\.python\python.exe"

:build
echo Building with %PY%
"%PY%" "%~dp0tools\build_exe.py" %*
set "RESULT=%ERRORLEVEL%"
goto :end

:no_python
echo.
echo Python 3.14 was not found on this computer, and there is no python-3.14.x-amd64.exe next to build.bat to install it from.
echo Install Python 3.14 from https://www.python.org/downloads/windows/ or put that installer in this folder, then run build.bat again.
set "RESULT=1"
goto :end

:install_failed
echo.
echo Installing Python from %INSTALLER% did not work ^(the installer's own log is in %TEMP%^).
echo Run that file yourself to install Python 3.14, then run build.bat again.
set "RESULT=1"
goto :end

:end
if not "%RESULT%"=="0" echo. & echo The build did not finish.
if defined CLICKED pause
exit /b %RESULT%
