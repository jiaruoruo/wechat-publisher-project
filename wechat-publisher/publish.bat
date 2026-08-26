@echo off
REM ============================================================
REM 微信公众号一键发布脚本
REM 前置要求：
REM   1. 配置 API Key（任一即可）：
REM        set OPENAI_API_KEY=sk-xxx     （默认，推荐）
REM        或 set DEEPSEEK_API_KEY=sk-xxx
REM        或 set DASHSCOPE_API_KEY=sk-xxx
REM   2. 首次需要登录：python main.py login （扫码）
REM ============================================================
setlocal

REM 浏览器路径（沙箱/自定义安装位置，可按需修改）
if not defined PLAYWRIGHT_BROWSERS_PATH (
    set "PLAYWRIGHT_BROWSERS_PATH=%~dp0.pw-browsers"
)

echo [1/3] 检查配置...
python main.py check

echo.
echo [2/3] 检查登录状态...
if not exist "%~dp0storagerowser_session.json" (
    echo   未登录，请先运行: python main.py login
    echo   或使用: python main.py run --topic "你的选题"
    goto :end
)

echo.
echo [3/3] 开始发布...
if "%1"=="" (
    python main.py run
) else (
    python main.py run --topic "%*"
)

:end
endlocal
