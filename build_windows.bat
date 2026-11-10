@echo off
rem Build dist\perfreport\perfreport.exe on Windows. Requires Python 3.11 or 3.12 (py launcher), internet access.
cd /d %~dp0
set PY=.venv\Scripts\python.exe
rem Retry option for company networks whose proxy re-signs HTTPS (pip then fails the SSL certificate check)
set TRUST=--trusted-host pypi.org --trusted-host files.pythonhosted.org
if not exist %PY% (
  echo [1/6] Creating Python environment .venv
  py -3 -m venv .venv || goto :err
)
echo [2/6] Installing packages (pandas, matplotlib, python-docx, plotly, pyyaml, openpyxl, pyinstaller)
%PY% -m pip install --upgrade pip >nul 2>&1 || %PY% -m pip install --upgrade pip %TRUST% >nul 2>&1
%PY% -m pip install -r requirements.txt pyinstaller && goto :check
echo NOTE: download failed. On company networks this is often the SSL certificate check.
echo       Retrying without the certificate check for pypi.org and files.pythonhosted.org ...
%PY% -m pip install -r requirements.txt pyinstaller %TRUST% || goto :neterr
:check
echo [3/6] Checking packages
%PY% -c "import pandas, matplotlib, docx, plotly, yaml, openpyxl; print('packages OK')" || goto :neterr
echo [4/6] Building executable
%PY% -m PyInstaller --noconfirm --name perfreport --console ^
  --add-data "perfreport\template.html;perfreport" ^
  --collect-data plotly ^
  --collect-submodules pandas ^
  --hidden-import openpyxl ^
  run_perfreport.py || goto :err
echo [5/6] Copying settings and samples
xcopy /E /I /Y profiles dist\perfreport\profiles >nul
xcopy /E /I /Y rules dist\perfreport\rules >nul
xcopy /E /I /Y samples dist\perfreport\samples >nul
copy /Y run_gui.bat dist\perfreport\ >nul
dist\perfreport\perfreport.exe inspect samples\sample_data\esx01_vm_metrics.csv vcf_ops_vm >nul 2>&1 || goto :exeerr
echo [6/6] Testing report generation with 2 worker processes (samples, about 30 seconds)
set PERFREPORT_REQUIRE_POOL=1
dist\perfreport\perfreport.exe run samples\sample_cases.csv samples\sample_data --out "%TEMP%\perfreport_selftest" --workers 2 >"%TEMP%\perfreport_selftest.log" 2>&1
set RC=%ERRORLEVEL%
set PERFREPORT_REQUIRE_POOL=
if not "%RC%"=="0" goto :seqtest
echo Parallel chart drawing works on this PC.
goto :done
:seqtest
echo NOTE: parallel chart drawing did not work on this PC. Charts will be drawn one by one, which is slower.
echo       Details: %TEMP%\perfreport_selftest.log
dist\perfreport\perfreport.exe run samples\sample_cases.csv samples\sample_data --out "%TEMP%\perfreport_selftest" --workers 1 >"%TEMP%\perfreport_selftest_seq.log" 2>&1 || goto :runerr
:done
echo.
echo Done: dist\perfreport\perfreport.exe
echo Copy the whole dist\perfreport folder to the target PC and run run_gui.bat there.
pause
goto :eof
:neterr
echo.
echo FAILED while installing packages. Check the internet connection or proxy, then run this file again.
echo If your company has its own Python package repository, set it once and run this file again:
echo   py -3 -m pip config --user set global.index-url https://YOUR-REPOSITORY/simple
pause
exit /b 1
:exeerr
echo.
echo FAILED: the built perfreport.exe did not start. Send the messages above.
pause
exit /b 1
:runerr
echo.
echo FAILED: the built perfreport.exe could not generate the sample report. See %TEMP%\perfreport_selftest_seq.log
pause
exit /b 1
:err
echo.
echo FAILED. See messages above.
pause
exit /b 1
