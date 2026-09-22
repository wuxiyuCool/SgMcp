# 一键启动三层 server（各自 HTTP 独立部署，客户端只连网关）
# 用法：powershell -ExecutionPolicy Bypass -File scripts/run-demo.ps1
$ErrorActionPreference = "Stop"
$ROOT = Split-Path $PSScriptRoot -Parent
$py = Join-Path $ROOT ".venv\Scripts\python.exe"

function Start-Server([string]$entry, [string]$port) {
    $p = Start-Process -FilePath $py -ArgumentList @("-m", $entry, "--transport", "http", "--port", $port) -PassThru -WindowStyle Hidden
    Write-Host "启动了 $entry 于端口 $port (PID $($p.Id))" -ForegroundColor Green
}

Start-Server "mcp_common_server.main" 9100   # 下层 通用工具
Start-Server "mcp_itops.main" 9200           # 中层 IT 运维（试点）
Start-Server "mcp_gateway.main" 9000         # 上层 审批网关

Write-Host ""
Write-Host "服务已启动：网关 http://127.0.0.1:9000/mcp" -ForegroundColor Cyan
Write-Host "客户端只需连接网关。"
Write-Host "关闭：Get-Process python | Where-Object {$_.Path -like '*.venv*'} | Stop-Process"
