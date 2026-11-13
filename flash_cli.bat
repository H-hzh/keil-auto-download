@echo off
rem  Launcher for flash_md32.py (must stay in the same folder).
rem  Full options:  python flash_md32.py --help
rem  Examples:      flash_cli.bat                    default project: build+flash+reset+verify
rem                 flash_cli.bat --build-only       build only
rem                 flash_cli.bat --chip GD32F303CC   force chip model
rem                 flash_cli.bat "D:\other\foo.uvprojx"   another project
setlocal
set "PY=D:\ux-venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0flash_md32.py" %*
exit /b %ERRORLEVEL%
