@echo off
rem Build dist\tcgen\tcgen.exe on Windows. Requires Python 3.9 or later (py launcher) and internet access for PyInstaller.
rem tcgen keeps settings, commands and test cases in the data folder (default C:\Users\<user>\tcgen_data), not in dist\tcgen.
rem PyInstaller deletes dist\tcgen on every build. Step 4 keeps data files that an older tcgen saved in dist\tcgen.
cd /d %~dp0
set PY=.venv\Scripts\python.exe
if not exist %PY% (
  echo [1/5] Creating Python environment .venv
  py -3 -m venv .venv || goto :err
)
echo [2/5] Installing PyInstaller
%PY% -m pip install --upgrade pip
%PY% -m pip install pyinstaller || goto :neterr
echo [3/5] Running self tests
%PY% -m unittest discover -s tests || goto :testerr
echo [4/5] Checking dist\tcgen for data files of an older version
set KEEP=build_olddata
set OLD=
for %%F in (settings.json run_commands.csv monitor_commands.csv test_cases.json) do if exist dist\tcgen\%%F set OLD=1
if defined OLD (
  if exist %KEEP% rmdir /s /q %KEEP%
  mkdir %KEEP%
  for %%F in (settings.json run_commands.csv monitor_commands.csv test_cases.json) do if exist dist\tcgen\%%F copy /Y dist\tcgen\%%F %KEEP%\ >nul
  if exist dist\tcgen\testcases xcopy /E /I /Y /Q dist\tcgen\testcases %KEEP%\testcases >nul
  if exist dist\tcgen\backup xcopy /E /I /Y /Q dist\tcgen\backup %KEEP%\backup >nul
  echo       Kept them in %KEEP%. The new tcgen.exe copies them to the data folder when it starts.
)
echo [5/5] Building executable
%PY% -m PyInstaller --noconfirm --windowed --name tcgen tcgen_app.py || goto :err
if defined OLD xcopy /E /I /Y /Q %KEEP% dist\tcgen >nul
copy /Y README.md dist\tcgen\ >nul
copy /Y run_gui.bat dist\tcgen\ >nul
echo.
echo Done: dist\tcgen\tcgen.exe
echo Copy the whole dist\tcgen folder to the PC where you make test commands, then run run_gui.bat there.
echo Settings, commands and test cases stay in the data folder, so rebuilding does not remove them.
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
