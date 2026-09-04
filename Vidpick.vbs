Option Explicit

Dim shell, fso, root, pythonw, starter
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = root & "\.venv\Scripts\pythonw.exe"
starter = root & "\start_vidpick.bat"

If fso.FileExists(pythonw) And fso.FileExists(root & "\.venv\.vidpick_ready") Then
    shell.Run Quote(pythonw) & " " & Quote(root & "\main.py"), 1, False
Else
    shell.Run Quote(starter), 1, False
End If

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
