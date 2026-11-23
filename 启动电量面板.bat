@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 优先用 Python 官方启动器；没有就退回 PATH 里的 pythonw。
rem 如果你的 Python 不在这两处（例如装在 D:\Anaconda），
rem 把下一行直接改成它的完整路径：set "PY=D:\Anaconda\pythonw.exe"
set "PY=pyw"

where pyw >nul 2>nul
if not errorlevel 1 goto run

set "PY=pythonw"
where pythonw >nul 2>nul
if not errorlevel 1 goto run

echo 没找到 Python。
echo 请装一个 Python 3.9 以上（安装时勾上 tcl/tk），
echo 或者用记事本打开本文件，把 set "PY=pyw" 改成你机器上 pythonw.exe 的完整路径。
pause
exit /b 1

:run
rem 先把托盘拉起来（已经在跑的话它会自己退出），再打开面板
start "" %PY% "%~dp0tray_app.py"
timeout /t 2 /nobreak >nul
start "" %PY% "%~dp0my_devices.py"
