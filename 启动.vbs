' =====================================================
' 视频 AI 重命名助手 — 启动脚本（VBS 无控制台版）
' 双击运行，不显示黑窗口；启动失败会弹出日志尾部，便于排错
'
' 注意：本文件必须以 ANSI(GBK) 保存。Windows 脚本宿主(WSH)只认
'       ANSI 或 UTF-16LE，存成 UTF-8 会因多字节序列被按 GBK 误读
'       而报「无效字符 / 缺少语句」—— 之前的启动报错就是这么来的。
' =====================================================
Option Explicit

Dim shell, fso, appRoot, pyCmd, cmd, logFile, port
Dim ok, i, body, tail

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appRoot = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = appRoot
port = "8000"

' ---------- 日志文件（把程序输出落盘，静默启动也能排错） ----------
Dim logDir
logDir = fso.BuildPath(appRoot, "logs")
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)
logFile = fso.BuildPath(logDir, "start.log")

' ---------- 按优先级找 Python ----------
' 先用 python.exe（可重定向输出）；只有 pythonw 时退而求其次（无日志）
pyCmd = ""
If fso.FileExists(fso.BuildPath(appRoot, "python\python.exe")) Then
    pyCmd = fso.BuildPath(appRoot, "python\python.exe")
ElseIf fso.FileExists(fso.BuildPath(appRoot, "venv\Scripts\python.exe")) Then
    pyCmd = fso.BuildPath(appRoot, "venv\Scripts\python.exe")
ElseIf fso.FileExists(fso.BuildPath(appRoot, "python\pythonw.exe")) Then
    pyCmd = fso.BuildPath(appRoot, "python\pythonw.exe")
ElseIf fso.FileExists(fso.BuildPath(appRoot, "venv\Scripts\pythonw.exe")) Then
    pyCmd = fso.BuildPath(appRoot, "venv\Scripts\pythonw.exe")
Else
    pyCmd = "pythonw"
End If

Function QQ(s)
    QQ = """" & s & """"
End Function

' ---------- 启动服务（隐藏窗口 + 输出重定向到日志） ----------
If InStr(LCase(pyCmd), "pythonw") > 0 Then
    cmd = QQ(pyCmd) & " " & QQ(fso.BuildPath(appRoot, "run.py")) & " --port " & port
Else
    cmd = "cmd /c " & QQ(pyCmd) & " " & QQ(fso.BuildPath(appRoot, "run.py")) & _
          " --port " & port & " > " & QQ(logFile) & " 2>&1"
End If

On Error Resume Next
shell.Run cmd, 0, False
If Err.Number <> 0 Then
    MsgBox "启动失败：" & Err.Description & vbCrLf & vbCrLf & _
           "请确认已安装 Python 3.11+，或把便携版 Python 放到 python\ 目录。", _
           16, "视频 AI 重命名助手"
    WScript.Quit 1
End If
On Error GoTo 0

' ---------- 等待服务就绪（最多 60 秒） ----------
ok = False
For i = 1 To 60
    WScript.Sleep 1000
    If HttpOk("http://127.0.0.1:" & port & "/api/health") Then
        ok = True
        Exit For
    End If
Next

If Not ok Then
    tail = ""
    If fso.FileExists(logFile) Then
        On Error Resume Next
        body = fso.OpenTextFile(logFile, 1, False).ReadAll
        On Error GoTo 0
        If Len(body) > 1500 Then body = Right(body, 1500)
        tail = body
    End If
    MsgBox "服务启动失败（60 秒未就绪）。" & vbCrLf & _
           "完整日志：" & logFile & vbCrLf & vbCrLf & _
           "日志尾部：" & vbCrLf & tail, _
           16, "视频 AI 重命名助手"
    WScript.Quit 1
End If

' ---------- 打开浏览器 ----------
shell.Run "http://127.0.0.1:" & port & "/", 1, False

Function HttpOk(url)
    On Error Resume Next
    Dim http
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    If Err.Number <> 0 Then
        HttpOk = False
        Exit Function
    End If
    http.SetTimeouts 800, 800, 800, 800
    http.Open "GET", url, False
    http.Send
    HttpOk = (http.Status = 200)
    On Error GoTo 0
End Function
