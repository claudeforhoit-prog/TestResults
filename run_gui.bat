@echo off
cd /d %~dp0
if exist perfreport.exe (
  perfreport.exe gui
  goto :eof
)
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import pandas" 2>nul || goto :nopkg
  .venv\Scripts\python.exe -m perfreport gui
  goto :eof
)
:nopkg
echo This folder is the source package, not a built program.
echo Run build_windows.bat first. After it finishes, use dist\perfreport\run_gui.bat
pause
