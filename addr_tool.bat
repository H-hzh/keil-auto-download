@echo off
rem md32 地址修改工具启动器（Windows / 双击可用）
rem 用法：addr_tool.bat [可选的校准表.xlsx]
setlocal
set PY=D:\ux-venv\Scripts\python.exe
if not exist "%PY%" (
    echo [错误] 找不到 Python：%PY%
    pause
    exit /b 1
)
"%PY%" "%~dp0addr_tool.py" %*
if errorlevel 1 pause
endlocal
