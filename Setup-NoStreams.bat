@echo off
setlocal EnableExtensions
title NoStreams setup
cd /d "%~dp0"

echo ==================================================
echo   NoStreams one-click setup
echo   Installs Python, ffmpeg and the audio helpers,
echo   makes a launcher, then deletes this installer.
echo ==================================================
echo.

if not exist "NoStream-Client.py" goto :noclient
where winget >nul 2>nul
if errorlevel 1 goto :nowinget

rem ---------- 1. Python ----------
set "PY="
python --version >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :havepy

echo [1/4] Installing Python ...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if defined PY goto :havepy
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY goto :pyfail
goto :havepy

:havepy
echo [1/4] Python is ready.

rem ---------- 2. ffmpeg ----------
where ffmpeg >nul 2>nul
if not errorlevel 1 goto :haveff
echo [2/4] Installing ffmpeg ...
winget install -e --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements
goto :ffdone
:haveff
echo [2/4] ffmpeg is already installed.
:ffdone

rem ---------- 3. Python packages ----------
echo [3/4] Installing audio packages ...
%PY% -m pip install --disable-pip-version-check pyaudiowpatch numpy
if errorlevel 1 goto :pipfail

rem ---------- 4. Launcher ----------
echo [4/4] Creating the launcher ...
> "Start NoStreams.bat" echo @echo off
>> "Start NoStreams.bat" echo cd /d "%%~dp0"
>> "Start NoStreams.bat" echo python NoStream-Client.py
>> "Start NoStreams.bat" echo if errorlevel 9009 py -3 NoStream-Client.py
>> "Start NoStreams.bat" echo pause

echo.
echo All done! Use "Start NoStreams.bat" in this folder whenever you want to go live.
echo (Have your stream key ready - you get it by signing up on the website.)
echo.
choice /m "Launch NoStreams now"
if errorlevel 2 goto :cleanup
start "" "Start NoStreams.bat"

:cleanup
rem Delete this installer. The "goto" trick lets a batch file remove itself.
(goto) 2>nul & del "%~f0"
exit /b 0

:noclient
echo Could not find NoStream-Client.py next to this installer.
echo Put both files in the same folder and run this again.
pause
exit /b 1

:nowinget
echo "winget" was not found. It comes with Windows 10 (1809+) and Windows 11.
echo Update Windows or install "App Installer" from the Microsoft Store, then run this again.
pause
exit /b 1

:pyfail
echo Python was installed but could not be found yet.
echo Close this window and run this installer once more.
pause
exit /b 1

:pipfail
echo Installing the audio packages failed. Check your internet connection and run this again.
pause
exit /b 1
