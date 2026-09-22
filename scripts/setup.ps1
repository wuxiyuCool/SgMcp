# 初始化开发环境：在项目根创建单一 .venv，editable 安装共享包与全部三层 server。
# （各层仍保留独立 pyproject.toml，可单独打包部署；开发期共用一个 venv 更省事）
# 用法：powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
$ErrorActionPreference = "Stop"
$ROOT = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $ROOT ".venv"

if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "==> 创建根 .venv" -ForegroundColor Cyan
    python -m venv $venv
}
$pip = Join-Path $venv "Scripts\python.exe"
& $pip -m pip install --upgrade pip

Write-Host "==> editable 安装 shared + 三层 server（一次解析本地包依赖）" -ForegroundColor Cyan
& $pip -m pip install -e (Join-Path $ROOT "shared") `
    -e (Join-Path $ROOT "layers\common") `
    -e (Join-Path $ROOT "layers\business\it_ops") `
    -e (Join-Path $ROOT "layers\business\purchasing") `
    -e (Join-Path $ROOT "layers\business\manufacturing") `
    -e (Join-Path $ROOT "layers\gateway")

Write-Host "完成。使用 $venv\Scripts\python.exe 运行任意 server 或 tests\smoke_test.py" -ForegroundColor Green
