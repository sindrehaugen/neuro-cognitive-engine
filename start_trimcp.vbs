Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Dynamically resolve root path instead of hardcoding absolute path
strAppPath = fso.GetParentFolderName(WScript.ScriptFullName)
strGoLauncher = strAppPath & "\go\trimcp-launch.exe"
strPython = strAppPath & "\.venv\Scripts\python.exe"
strBootstrap = strAppPath & "\scripts\bootstrap-compose-secrets.py"

' Run the secrets bootstrapping script synchronously first to mirror Makefile behavior
If fso.FileExists(strPython) And fso.FileExists(strBootstrap) Then
    WshShell.Run chr(34) & strPython & Chr(34) & " " & chr(34) & strBootstrap & Chr(34), 0, True
End If

If fso.FileExists(strGoLauncher) Then
    ' Use the robust Go-based launcher for v1.0
    WshShell.Run chr(34) & strGoLauncher & Chr(34), 0
Else
    ' Fallback path: Ensure local database containers are started synchronously before starting host worker
    WshShell.Run "docker compose -f " & chr(34) & strAppPath & "\docker-compose.local.yml" & chr(34) & " up -d", 0, True
    
    ' Fallback to pythonw if launcher is missing
    WshShell.Run chr(34) & strAppPath & "\.venv\Scripts\pythonw.exe" & Chr(34) & " " & chr(34) & strAppPath & "\start_worker.py" & Chr(34), 0
End If

Set WshShell = Nothing
Set fso = Nothing

