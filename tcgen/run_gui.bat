@echo off
rem Start the test command generator. Uses tcgen.exe if present, otherwise the installed Python.
cd /d %~dp0
if exist tcgen.exe (
  start "" tcgen.exe
  goto :eof
)
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 tcgen_app.py
  goto :eof
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw tcgen_app.py
  goto :eof
)
echo Python 3 is not installed on this PC, and tcgen.exe is not in this folder.
echo Run build_windows.bat on a PC that has Python, then copy the dist\tcgen folder to this PC.
pause
