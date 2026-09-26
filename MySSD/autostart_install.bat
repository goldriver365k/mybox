@echo off
chcp 65001 >nul
rem MySSD 자동 실행 등록: 현재 사용자의 "시작프로그램" 폴더에 바로가기를 만든다 (관리자 권한 불필요)
setlocal
set "DIR=%~dp0"
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
  echo pythonw.exe 를 찾을 수 없습니다. Python 설치 시 "Add python.exe to PATH" 를 체크했는지 확인하세요.
  pause
  exit /b 1
)
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\MySSD.lnk"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $s.TargetPath=$env:PYW; $s.Arguments=[char]34+$env:DIR+'server.py'+[char]34; $s.WorkingDirectory=$env:DIR; $s.Save()"
if not exist "%LNK%" (
  echo 등록에 실패했습니다.
  pause
  exit /b 1
)
echo.
echo 등록 완료: Windows 로그인 시 MySSD 서버가 창 없이 자동으로 시작됩니다.
echo   바로가기: %LNK%
echo   실행 파일: %PYW%
echo   로그: %DIR%logs\
echo.
echo 지금 바로 시작하려면 위 바로가기를 더블클릭하거나 PC를 다시 로그인하세요.
echo (python server.py 창이 이미 열려 있다면 먼저 Ctrl+C로 종료하세요)
pause
