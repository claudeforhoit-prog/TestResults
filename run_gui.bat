@echo off
cd /d %~dp0
if exist perfreport.exe (perfreport.exe gui) else (.venv\Scripts\python.exe -m perfreport gui)
