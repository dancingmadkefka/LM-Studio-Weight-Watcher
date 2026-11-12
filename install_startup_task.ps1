param(
    [string]$TaskName = "LM Studio Weight Watcher",
    [int]$DelayMinutes = 1
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Prefer a configured no-console launcher (Windowless.exe plus weights.json)
# when one is installed; otherwise use the bundled hidden VBS launcher.
$windowlessExe = Join-Path $env:LOCALAPPDATA 'Daemon\windowless\Windowless.exe'
$windowlessConfig = Join-Path $env:LOCALAPPDATA 'Daemon\windowless\weights.json'
$vbsLauncher = Join-Path $scriptDir "run_watcher_hidden.vbs"

if ((Test-Path -LiteralPath $windowlessExe) -and (Test-Path -LiteralPath $windowlessConfig)) {
    $launcherPath = $windowlessExe
    $action = New-ScheduledTaskAction -Execute $windowlessExe -Argument ('"' + $windowlessConfig + '"')
} elseif (Test-Path -LiteralPath $vbsLauncher) {
    $launcherPath = $vbsLauncher
    $wscript = Join-Path $env:WINDIR "System32\wscript.exe"
    $action = New-ScheduledTaskAction -Execute $wscript -Argument ('"' + $vbsLauncher + '"')
} else {
    throw "Launcher script not found: $vbsLauncher"
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$legacyTaskName = "LM Studio Weight Updater"

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser

if ($DelayMinutes -gt 0) {
    $trigger.Delay = "PT$($DelayMinutes)M"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
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
