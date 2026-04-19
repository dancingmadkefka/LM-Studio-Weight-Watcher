param(
    [string]$TaskName = "LM Studio Weight Watcher"
)

$ErrorActionPreference = "Stop"
$legacyTaskName = "LM Studio Weight Updater"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'"
} else {
    Write-Host "Scheduled task '$TaskName' was not found"
}

if ($TaskName -ne $legacyTaskName -and (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue)) {
    Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false
    Write-Host "Removed legacy scheduled task '$legacyTaskName'"
}
