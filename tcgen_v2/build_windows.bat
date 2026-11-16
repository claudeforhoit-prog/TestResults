@echo off
rem Build dist\tcgen2\tcgen2.exe on Windows. Requires Python 3.9 or later (py launcher) and internet access for PyInstaller.
rem tcgen2 keeps settings, commands, profiles and test cases in the data folder (default C:\Users\<user>\tcgen2_data), not in dist\tcgen2.
rem tcgen v1.x (dist\tcgen, tcgen_data) is a separate program. Building tcgen2 does not change it.
rem PyInstaller deletes dist\tcgen2 on every build. Step 4 keeps data files that an older build saved in dist\tcgen2.
cd /d %~dp0
set PY=.venv\Scripts\python.exe
rem Retry option for company networks whose proxy re-signs HTTPS (pip then fails the SSL certificate check)
set TRUST=--trusted-host pypi.org --trusted-host files.pythonhosted.org
if not exist %PY% (
  echo [1/5] Creating Python environment .venv
  py -3 -m venv .venv || goto :err
)
echo [2/5] Installing PyInstaller
%PY% -m pip install --upgrade pip >nul 2>&1 || %PY% -m pip install --upgrade pip %TRUST% >nul 2>&1
%PY% -m pip install pyinstaller && goto :tests
echo NOTE: download failed. On company networks this is often the SSL certificate check.
echo       Retrying without the certificate check for pypi.org and files.pythonhosted.org ...
%PY% -m pip install pyinstaller %TRUST% || goto :neterr
:tests
echo [3/5] Running self tests
%PY% -m unittest discover -s tests || goto :testerr
echo [4/5] Checking dist\tcgen2 for data files of an older version
set KEEP=build_olddata
set OLD=
for %%F in (settings.json run_commands.csv monitor_commands.csv test_cases.json) do if exist dist\tcgen2\%%F set OLD=1
if defined OLD (
  if exist %KEEP% rmdir /s /q %KEEP%
  mkdir %KEEP%
  for %%F in (settings.json run_commands.csv monitor_commands.csv test_cases.json) do if exist dist\tcgen2\%%F copy /Y dist\tcgen2\%%F %KEEP%\ >nul
  if exist dist\tcgen2\testcases xcopy /E /I /Y /Q dist\tcgen2\testcases %KEEP%\testcases >nul
  if exist dist\tcgen2\backup xcopy /E /I /Y /Q dist\tcgen2\backup %KEEP%\backup >nul
  echo       Kept them in %KEEP%. The new tcgen2.exe copies them to the data folder when it starts.
)
echo [5/5] Building executable
%PY% -m PyInstaller --noconfirm --windowed --name tcgen2 tcgen_app.py || goto :err
if defined OLD xcopy /E /I /Y /Q %KEEP% dist\tcgen2 >nul
copy /Y README.md dist\tcgen2\ >nul
copy /Y run_gui.bat dist\tcgen2\ >nul
echo.
echo Done: dist\tcgen2\tcgen2.exe
echo Copy the whole dist\tcgen2 folder to the PC where you make test commands, then run run_gui.bat there.
echo Settings, commands, profiles and test cases stay in the data folder, so rebuilding does not remove them.
pause
goto :eof
:neterr
echo.
echo FAILED while installing PyInstaller. Check the internet connection or proxy, then run this file again.
echo If your company has its own Python package repository, set it once and run this file again:
echo   py -3 -m pip config --user set global.index-url https://YOUR-REPOSITORY/simple
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
