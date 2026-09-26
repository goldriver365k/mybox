# MySSD 자동 실행 (Windows 작업 스케줄러) — 사용자가 직접 실행할 때만 설정을 바꿉니다.
#   설치: autostart_task_install.bat   해제: autostart_task_uninstall.bat
param([string]$Mode = "install")
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$name = "MySSD"
$user = "$env:USERDOMAIN\$env:USERNAME"
$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\MySSD.lnk"

if ($Mode -eq "uninstall") {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "작업 스케줄러의 MySSD 자동 실행을 해제했습니다."
    } else {
        Write-Host "작업 스케줄러에 MySSD 가 등록되어 있지 않습니다."
    }
    Write-Host "실행 중인 서버는 작업 관리자 > 세부 정보 > pythonw.exe 를 끝내서 종료할 수 있습니다."
    exit 0
}

$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) {
    Write-Host "pythonw.exe 를 찾을 수 없습니다. Python 설치 시 'Add python.exe to PATH' 를 체크했는지 확인하세요."
    exit 1
}
try {
    $action = New-ScheduledTaskAction -Execute $pyw -Argument ('"' + (Join-Path $dir "server.py") + '"') -WorkingDirectory $dir
    # Windows 로그인 시 시작 + 이후 5분마다 확인:
    #   - 서버가 실행 중이면 새로 시작하지 않음 (MultipleInstances IgnoreNew → 중복 실행 없음)
    #   - 서버가 꺼져 있으면 다시 시작 (5분에 한 번만 시도 → 무한 재시작 반복 없음)
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "MySSD 개인 파일 서버: 로그인 시 시작, 꺼져 있으면 5분 안에 다시 시작" -Force | Out-Null
} catch {
    Write-Host "등록하지 못했습니다: $($_.Exception.Message)"
    Write-Host "이 파일을 마우스 오른쪽 버튼 > '관리자 권한으로 실행' 으로 다시 실행해 보세요."
    exit 1
}
if (Test-Path $startup) {
    Remove-Item $startup
    Write-Host "예전 시작프로그램 방식(MySSD.lnk)은 두 번 실행되지 않도록 삭제했습니다."
}
Write-Host ""
Write-Host "등록 완료: Windows 로그인 시 MySSD 가 창 없이 시작되고, 꺼지면 5분 안에 다시 시작됩니다."
Write-Host "  실행 파일: $pyw"
Write-Host "  로그: $dir\logs\"
Write-Host "확인: 작업 스케줄러(taskschd.msc) > 작업 스케줄러 라이브러리 > MySSD"
Start-ScheduledTask -TaskName $name
Write-Host "지금 바로 시작했습니다. (이미 실행 중이면 그대로 둡니다)"
