@echo off
rem ===========================================================================
rem  TorSOCKS5 一键启动脚本（Windows）
rem
rem  用法：
rem    run.bat                使用默认配置启动
rem    run.bat run --port 1080
rem    run.bat doctor         环境自检
rem    run.bat selftest       离线自检
rem ===========================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "PYTHON="
where py >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON (
    where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
    echo 错误：找不到 Python 3，请先安装 Python 3.8 或更高版本。
    echo 下载地址：https://www.python.org/downloads/
    pause
    exit /b 1
)

%PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"
if errorlevel 1 (
    echo 错误：Python 版本过低，需要 3.8 或更高版本。
    %PYTHON% -V
    pause
    exit /b 1
)

if "%~1"=="" (
    set "CMD=run"
) else (
    set "CMD=%*"
)

%PYTHON% "%~dp0torsocks5_cli.py" !CMD!
if errorlevel 1 pause
endlocal
