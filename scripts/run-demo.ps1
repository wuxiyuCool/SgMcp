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

# 中层 Go 重活 server（必须在网关之前处理：网关进程启动时读取配置）
# - 地址来源：OS 环境变量 > config/platform.env（网关自己也会加载该文件，这里只为决定是否本机起停）
# - 指向远端 → 本机不起；指向本机 → 使用/构建本地二进制（无 Go 环境则跳过，仅 Python 三层）
function Get-CfgValue([string]$file, [string]$key) {
    if (-not (Test-Path $file)) { return "" }
    foreach ($line in Get-Content $file -Encoding UTF8) {
        if ($line -match "^\s*(export\s+)?$key\s*=\s*(.+)$") {
            return $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}
$godh = $env:MCP_GODATAHUB_URL
if (-not $godh) { $godh = Get-CfgValue (Join-Path $ROOT "config\platform.env") "MCP_GODATAHUB_URL" }
if (-not $godh) { $godh = "http://127.0.0.1:9300/mcp" }
$env:MCP_GODATAHUB_URL = $godh
$godhIsLocal = $godh -match "^https?://(127\.0\.0\.1|localhost|\[?::1\]?)(:\d+)?/"
if (-not $godhIsLocal) {
    Write-Host "go_datahub 走远端：$godh（本机不启动）" -ForegroundColor Cyan
} else {
    $godhExe = Join-Path $ROOT "layers\business\go_datahub\bin\datahub-server.exe"
    if (-not (Test-Path $godhExe) -and (Get-Command go -ErrorAction SilentlyContinue)) {
        Push-Location (Join-Path $ROOT "layers\business\go_datahub")
        go build -o bin/datahub-server.exe ./cmd/datahub-server
        Pop-Location
    }
    if (Test-Path $godhExe) {
        $p = Start-Process -FilePath $godhExe -ArgumentList @("-port", "9300") -PassThru -WindowStyle Hidden
        Write-Host "启动了 go_datahub 于端口 9300 (PID $($p.Id))" -ForegroundColor Green
    } else {
        Write-Host "提示：未部署 go_datahub（Go 重活中层），已跳过" -ForegroundColor Yellow
    }
}

Start-Server "mcp_gateway.main" 9000         # 上层 审批网关（最后起，确保继承上面的 go_datahub 环境变量）

Write-Host ""
Write-Host "服务已启动：网关 http://127.0.0.1:9000/mcp" -ForegroundColor Cyan
Write-Host "客户端只需连接网关。"
Write-Host "关闭：Get-Process python | Where-Object {$_.Path -like '*.venv*'} | Stop-Process"
