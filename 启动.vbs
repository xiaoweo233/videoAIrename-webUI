' =====================================================
' 视频 AI 重命名助手 — 启动脚本（VBS 无控制台版）
' 双击运行，不显示黑窗口
' =====================================================
Option Explicit

Dim shell, fso, appRoot, pyCmd, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appRoot = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = appRoot

' 按优先级找 Python
pyCmd = ""
If fso.FileExists(appRoot & "\python\pythonw.exe") Then
    pyCmd = appRoot & "\python\pythonw.exe"
ElseIf fso.FileExists(appRoot & "\python\python.exe") Then
    pyCmd = appRoot & "\python\python.exe"
ElseIf fso.FileExists(appRoot & "\venv\Scripts\pythonw.exe") Then
    pyCmd = appRoot & "\venv\Scripts\pythonw.exe"
Else
    pyCmd = "pythonw"
End If

' 3 秒后打开浏览器
shell.Run "cmd /c timeout /t 3 >nul & start http://127.0.0.1:8000/", 0, False

' 0 = 隐藏窗口
cmd = """" & pyCmd & """ """ & appRoot & "\run.py"" --port 8000"
On Error Resume Next
shell.Run cmd, 0, False
If Err.Number <> 0 Then
    MsgBox "启动失败：" & Err.Description & vbCrLf & _
           "请确认已安装 Python 3.11+，或把便携版 Python 放到 python\ 目录。", 16, "视频 AI 重命名助手"
End If
