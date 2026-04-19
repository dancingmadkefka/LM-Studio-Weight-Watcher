Option Explicit

Dim shell
Dim fso
Dim scriptDir
Dim launcherPath
Dim command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcherPath = fso.BuildPath(scriptDir, "run_watcher.bat")

If Not fso.FileExists(launcherPath) Then
    WScript.Quit 1
End If

command = "cmd.exe /c """"" & launcherPath & """ --hidden-launch"""
shell.CurrentDirectory = scriptDir
shell.Run command, 0, False
