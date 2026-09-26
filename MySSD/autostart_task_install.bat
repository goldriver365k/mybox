@echo off
chcp 65001 >nul
rem MySSD 자동 실행 등록 (작업 스케줄러, 꺼지면 자동 재시작)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart_task.ps1" install
pause
