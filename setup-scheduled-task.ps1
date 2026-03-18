# conversation-warehouse 매시간 동기화 예약 작업 설정
# 관리자 권한으로 실행: powershell -ExecutionPolicy Bypass -File setup-scheduled-task.ps1

$taskName = "ConversationWarehouseSync"
$scriptPath = "C:\Users\user\scripts\conversation-warehouse\sync-conversations.js"
$nodePath = (Get-Command node).Source

# 기존 작업 제거
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# 매시간 실행 트리거
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 365)

# 액션: node sync-conversations.js
$action = New-ScheduledTaskAction -Execute $nodePath -Argument $scriptPath -WorkingDirectory "C:\Users\user\scripts\conversation-warehouse"

# 설정: 로그온하지 않아도 실행, 네트워크 필요
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

# 등록
Register-ScheduledTask -TaskName $taskName -Trigger $trigger -Action $action -Settings $settings -Description "Claude Code 대화 기록을 MongoDB Atlas에 매시간 동기화" -RunLevel Limited

Write-Host "✅ 예약 작업 '$taskName' 등록 완료 (매시간 실행)"
Write-Host "확인: Get-ScheduledTaskInfo -TaskName '$taskName'"
