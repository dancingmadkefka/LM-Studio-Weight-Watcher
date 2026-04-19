Option Explicit

Dim shell
Dim fso
Dim scriptDir
Dim pythonExe
Dim command
Dim fallbackToPath
Dim logPath

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
fallbackToPath = False
logPath = fso.BuildPath(scriptDir, "watcher-launch.log")

pythonExe = shell.ExpandEnvironmentStrings("%LMSTUDIO_WATCHER_PYTHON%")
If pythonExe = "%LMSTUDIO_WATCHER_PYTHON%" Or Len(pythonExe) = 0 Then
    pythonExe = shell.ExpandEnvironmentStrings("%USERPROFILE%\miniforge3\envs\weightupdater\pythonw.exe")
End If

If Not fso.FileExists(pythonExe) Then
    pythonExe = shell.ExpandEnvironmentStrings("%USERPROFILE%\miniforge3\envs\weightupdater\python.exe")
End If

If Not fso.FileExists(pythonExe) Then
    pythonExe = shell.ExpandEnvironmentStrings("%USERPROFILE%\miniforge3\pythonw.exe")
End If

If Not fso.FileExists(pythonExe) Then
    pythonExe = shell.ExpandEnvironmentStrings("%USERPROFILE%\miniforge3\python.exe")
End If

If Not fso.FileExists(pythonExe) Then
    pythonExe = "pythonw"
    fallbackToPath = True
End If

command = """" & pythonExe & """ """ & fso.BuildPath(scriptDir, "lmstudio_weight_watcher.py") & """"
shell.CurrentDirectory = scriptDir
If fallbackToPath Then
    WriteLaunchLog logPath, "Falling back to pythonw from PATH at " & Now
End If
shell.Run command, 0, False

Sub WriteLaunchLog(path, message)
    Dim stream
    On Error Resume Next
    Set stream = fso.OpenTextFile(path, 8, True)
    If Err.Number = 0 Then
        stream.WriteLine message
        stream.Close
    End If
    On Error GoTo 0
End Sub
