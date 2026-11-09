@echo off
rem Build dist\tcgen\tcgen.exe on Windows. Requires Python 3.9 or later (py launcher) and internet access for PyInstaller.
cd /d %~dp0
set PY=.venv\Scripts\python.exe
if not exist %PY% (
  echo [1/4] Creating Python environment .venv
  py -3 -m venv .venv || goto :err
)
echo [2/4] Installing PyInstaller
%PY% -m pip install --upgrade pip
%PY% -m pip install pyinstaller || goto :neterr
echo [3/4] Running self tests
%PY% -m unittest discover -s tests || goto :testerr
echo [4/4] Building executable
%PY% -m PyInstaller --noconfirm --windowed --name tcgen tcgen_app.py || goto :err
copy /Y README.md dist\tcgen\ >nul
copy /Y run_gui.bat dist\tcgen\ >nul
echo.
echo Done: dist\tcgen\tcgen.exe
echo Copy the whole dist\tcgen folder to the PC where you make test commands, then run run_gui.bat there.
echo Settings and command lists are saved in that folder.
pause
goto :eof
:neterr
echo.
echo FAILED while installing PyInstaller. Check the internet connection or proxy, then run this file again.
pause
exit /b 1
:testerr
echo.
echo FAILED: self tests did not pass. Send the messages above.
pause
exit /b 1
:err
echo.
echo FAILED. See messages above.
pause
exit /b 1
