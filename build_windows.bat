@echo off
rem Build dist\perfreport\perfreport.exe on Windows. Requires Python 3.11+ (py launcher).
cd /d %~dp0
py -3 -m venv .venv || goto :err
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller || goto :err
pyinstaller --noconfirm --name perfreport --console ^
  --add-data "perfreport\template.html;perfreport" ^
  --collect-data plotly ^
  --hidden-import openpyxl ^
  run_perfreport.py || goto :err
xcopy /E /I /Y profiles dist\perfreport\profiles
xcopy /E /I /Y rules dist\perfreport\rules
xcopy /E /I /Y samples dist\perfreport\samples
copy /Y run_gui.bat dist\perfreport\
echo.
echo Done: dist\perfreport\perfreport.exe
goto :eof
:err
echo FAILED. See messages above.
exit /b 1
