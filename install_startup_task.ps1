param(
    [string]$TaskName = "LM Studio Weight Watcher",
    [int]$DelayMinutes = 1
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcherPath = Join-Path $scriptDir "run_watcher_hidden.vbs"

if (-not (Test-Path -LiteralPath $launcherPath)) {
    throw "Launcher script not found: $launcherPath"
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$wscript = Join-Path $env:WINDIR "System32\wscript.exe"
$legacyTaskName = "LM Studio Weight Updater"

$action = New-ScheduledTaskAction -Execute $wscript -Argument ('"' + $launcherPath + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser

if ($DelayMinutes -gt 0) {
    $trigger.Delay = "PT$($DelayMinutes)M"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

if ($TaskName -ne $legacyTaskName -and (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue)) {
    Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Starts the LM Studio Weight Watcher tray watcher at logon." `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName' for $currentUser"
Write-Host "Launcher: $launcherPath"
