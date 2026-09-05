# 微信公众号一键发布脚本（PowerShell）
# 用法: .\publish.ps1 [选题主题]
# 前置: 配置 API Key + 首次 python main.py login 扫码

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

if (-not $env:PLAYWRIGHT_BROWSERS_PATH) {
    $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $Root ".pw-browsers"
}

Write-Host "[1/3] 检查配置..." -ForegroundColor Cyan
python main.py check

Write-Host ""
Write-Host "[2/3] 检查登录..." -ForegroundColor Cyan
$sessionFile = Join-Path $Root "storage/browser_session.json"
if (-not (Test-Path $sessionFile)) {
    Write-Host "  未登录！请先运行: python main.py login" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "[3/3] 开始发布..." -ForegroundColor Cyan
if ($args.Count -eq 0) {
    python main.py run
} else {
    python main.py run --topic ($args -join " ")
}
