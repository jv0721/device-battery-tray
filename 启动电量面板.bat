@echo off
chcp 65001 >nul
setlocal

rem 找一个能用的 pythonw.exe（不带黑框的那个）。找不到就让用户自己填。
set "PY="
where pyw >nul 2>nul && set "PY=pyw"
if not defined PY where pythonw >nul 2>nul && set "PY=pythonw"
for %%V in (314 313 312 311 310 39 38) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe"
  )
)
if not defined PY (
  echo 没找到 Python。
  echo 请先装 Python 3.9 以上（安装时记得勾上 tcl/tk），
  echo 或者用记事本打开本文件，把 set "PY=..." 那一行改成你自己的路径。
  pause
  exit /b 1
)

rem 先把托盘拉起来（已经在跑的话它自己会退出），再打开面板
start "" "%PY%" "%~dp0tray_app.py"
timeout /t 2 /nobreak >nul
start "" "%PY%" "%~dp0my_devices.py"
