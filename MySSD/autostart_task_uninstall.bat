@echo off
chcp 65001 >nul
rem MySSD 자동 실행 해제 (작업 스케줄러)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart_task.ps1" uninstall
pause
