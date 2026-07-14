' Launch FetchForge with no visible console window.
' Runs launch.bat with a hidden window (style 0). Python still gets a console,
' so ffmpeg/yt-dlp inherit it and never flash their own windows — it's just
' invisible. The server self-exits ~30s after the browser tab is closed
' (heartbeat watchdog), so nothing lingers.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "cmd /c launch.bat", 0, False
