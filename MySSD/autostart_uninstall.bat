@echo off
chcp 65001 >nul
rem MySSD 자동 실행 해제: 시작프로그램 바로가기만 삭제한다 (SSD 파일, .env, 로그는 그대로)
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\MySSD.lnk"
if exist "%LNK%" (
  del "%LNK%"
  echo 자동 실행을 해제했습니다.
) else (
  echo 자동 실행이 등록되어 있지 않습니다.
)
echo 지금 실행 중인 서버는 작업 관리자 - 세부 정보 - pythonw.exe 를 끝내서 종료할 수 있습니다.
pause
