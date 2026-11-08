# Настройки задачи планировщика «TaskTreker\Обновление портфеля» (А4, разбор недели 24–31.10):
# запуск и от батареи, не останавливаться при переходе на батарею, будить компьютер к запуску,
# пропущенный запуск — при первой возможности. Повторный запуск скрипта ничего не ломает.
#   powershell -NoProfile -ExecutionPolicy Bypass -File ops\schedule.ps1
param([string]$Path = '\TaskTreker\', [string]$Name = 'Обновление портфеля')
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskPath $Path -TaskName $Name
$s = $task.Settings
$s.DisallowStartIfOnBatteries = $false
$s.StopIfGoingOnBatteries = $false
$s.WakeToRun = $true
$s.StartWhenAvailable = $true
Set-ScheduledTask -TaskPath $Path -TaskName $Name -Settings $s | Out-Null
$t = (Get-ScheduledTask -TaskPath $Path -TaskName $Name).Settings
'От батареи: {0}; будить: {1}; пропущенный — при возможности: {2}' -f (-not $t.DisallowStartIfOnBatteries), $t.WakeToRun, $t.StartWhenAvailable
