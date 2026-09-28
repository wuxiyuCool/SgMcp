# 运维自检：起三层 server → 跑运维测试集 → 停掉 server。
# 用法：powershell -ExecutionPolicy Bypass -File scripts/ops-check.ps1 [-Load 50]
#   退出码 0 = 全部通过（可直接用于巡检 / CI）。
param(
    [int]$Load = 0,
    [switch]$Rebuild,   # 强制重建 Go 二进制（默认：缺失或 Go 源码更新时自动重建）
    [switch]$Secure,    # 全链路鉴权自检：临时生成随机令牌并启用网关/下游 Bearer + 审批令牌
    [int]$BootWaitSeconds = 10,
    [string]$Python = "",
    [string]$GodhUrl = ""   # 远端 go_datahub 地址（如 http://192.168.1.20:9300/mcp）；留空取环境变量 MCP_GODATAHUB_URL，再留空则本机起停
)
$ErrorActionPreference = "Stop"
$ROOT = Split-Path $PSScriptRoot -Parent

# 解释器：优先用 -Python 指定，其次项目根 .venv，最后退回 PATH 上的 python
$py = $Python
if (-not $py) {
    $candidate = Join-Path $ROOT ".venv\Scripts\python.exe"
    if (Test-Path $candidate) { $py = $candidate }
}
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py -or -not (Test-Path $py)) {
    Write-Host "未找到可用的 Python 解释器。请先运行 scripts/setup.ps1，或用 -Python <路径> 指定。" -ForegroundColor Red
    exit 1
}
Write-Host "使用解释器：$py" -ForegroundColor DarkGray

$servers = @(
    @{ Entry = "mcp_common_server.main"; Port = 9100 },
    @{ Entry = "mcp_itops.main";         Port = 9200 },
    @{ Entry = "mcp_gateway.main";       Port = 9000 }
)

# 未 pip install 时也能直接跑：把各层 src 挂到 PYTHONPATH（editable 安装后此设置无害）
$srcPaths = @(
    (Join-Path $ROOT "shared\src"),
    (Join-Path $ROOT "layers\common\src"),
    (Join-Path $ROOT "layers\business\it_ops\src"),
    (Join-Path $ROOT "layers\business\purchasing\src"),
    (Join-Path $ROOT "layers\business\manufacturing\src"),
    (Join-Path $ROOT "layers\gateway\src")
)
$env:PYTHONPATH = ($srcPaths -join ";")
$env:NO_PROXY = "*"
$env:PYTHONIOENCODING = "utf-8"

# 数据集路由注册表（业务名 → 租户/兜底 dsn_ref，Go 与 Python 两侧共用同一约定）：
# dsn_ref 指向 DSN_<名称> 注册表；本机无真实库，测试走 simulate 模式验证路由与任务流。
# _DOMAIN=域归属（领域查询工具按域限定枚举）、_DESC=中文说明（工具枚举描述用）
$env:DATASET_ORDERS_DBTYPE = "pg"
$env:DATASET_ORDERS_DSN_T1 = "order_t1_pg"
$env:DATASET_ORDERS_DSN_DEFAULT = "order_pg"
$env:DATASET_ORDERS_DOMAIN = "order"
$env:DATASET_ORDERS_DESC = "订单数据（按月分表）"
$env:DATASET_INCIDENTS_DBTYPE = "pg"
$env:DATASET_INCIDENTS_DSN_DEFAULT = "itsm_pg"
$env:DATASET_INCIDENTS_DOMAIN = "itops"
$env:DATASET_INCIDENTS_DESC = "IT 运维事件单（工单流水）"
$env:DATASET_ASSETS_DBTYPE = "pg"
$env:DATASET_ASSETS_DSN_DEFAULT = "itsm_pg"
$env:DATASET_ASSETS_DOMAIN = "itops"
$env:DATASET_ASSETS_DESC = "CMDB 资产台账"

# go_datahub 地址解析：-GodhUrl > OS 环境变量 > config/platform.env > 本机默认；写回环境供网关与测试集共用
function Get-CfgValue([string]$file, [string]$key) {
    if (-not (Test-Path $file)) { return "" }
    foreach ($line in Get-Content $file -Encoding UTF8) {
        if ($line -match "^\s*(export\s+)?$key\s*=\s*(.+)$") {
            return $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}
$godh = if ($GodhUrl) { $GodhUrl } `
        elseif ($env:MCP_GODATAHUB_URL) { $env:MCP_GODATAHUB_URL } `
        elseif (Get-CfgValue (Join-Path $ROOT "config\platform.env") "MCP_GODATAHUB_URL") { Get-CfgValue (Join-Path $ROOT "config\platform.env") "MCP_GODATAHUB_URL" } `
        else { "http://127.0.0.1:9300/mcp" }
$env:MCP_GODATAHUB_URL = $godh
$godhIsLocal = $godh -match "^https?://(127\.0\.0\.1|localhost|\[?::1\]?)(:\d+)?/"

if ($Secure) {
    # 临时随机令牌：只存在于本次子进程环境里，不落盘、不进 git
    function New-Token { -join ((48..57)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_}) }
    if (-not $env:MCP_GATEWAY_TOKEN)  { $env:MCP_GATEWAY_TOKEN  = New-Token }
    if (-not $env:MCP_SERVER_TOKEN)   { $env:MCP_SERVER_TOKEN   = New-Token }   # Python 下游三层共用
    if (-not $env:GO_DATAHUB_TOKEN)   { $env:GO_DATAHUB_TOKEN  = New-Token }
    if (-not $env:MCP_APPROVAL_TOKEN) { $env:MCP_APPROVAL_TOKEN = New-Token }
    $env:MCP_GODATAHUB_TOKEN  = $env:GO_DATAHUB_TOKEN           # 网关→Go 同值
    $env:MCP_DOWNSTREAM_TOKEN_IT_OPS       = $env:MCP_SERVER_TOKEN
    $env:MCP_DOWNSTREAM_TOKEN_COMMON_TOOLS = $env:MCP_SERVER_TOKEN
    Write-Host "==> -Secure：已启用全链路 Bearer + 审批令牌（临时随机值，仅本次进程有效）" -ForegroundColor Green
}

Write-Host "==> 启动三层 server（HTTP）" -ForegroundColor Cyan
$procs = @()

# 中层 Go 重活 server 必须先于网关处理：网关启动时聚合器要拉它的 tools/list。
# - 远端地址（-GodhUrl / MCP_GODATAHUB_URL 非本机）→ 不起停本地，只把地址交给测试集直测
# - 本机默认 → 二进制不存在时自动 go build，无 Go 环境则跳过并让测试集 --skip-go
$godhExe = Join-Path $ROOT "layers\business\go_datahub\bin\datahub-server.exe"
# -Secure 时 Go 也以 Bearer 启动（token 来自上面的随机值或外部环境）
if ($Secure) { $godhArgs = @("-port", "9300", "-token", $env:GO_DATAHUB_TOKEN) } else { $godhArgs = @("-port", "9300") }

# 二进制过期检测：Go 源码比 exe 新还跑旧二进制，会出现"改了 Go 代码但自检全绿"的假象
function Test-GoStale([string]$exe) {
    if ($Rebuild) { return $true }
    if (-not (Test-Path $exe)) { return $true }
    $t = (Get-Item $exe).LastWriteTime
    $src = Join-Path $ROOT "layers\business\go_datahub"
    return [bool](Get-ChildItem -Path $src -Recurse -Include *.go, go.mod |
        Where-Object { $_.LastWriteTime -gt $t } | Select-Object -First 1)
}
$testArgs = @((Join-Path $ROOT "tests\ops\run_ops_test.py"), "--godh-url", $godh)
if (-not $godhIsLocal) {
    Write-Host "==> go_datahub 使用远端地址：$godh（跳过本机构建/起停）" -ForegroundColor Cyan
} elseif (Test-GoStale $godhExe) {
    if (Get-Command go -ErrorAction SilentlyContinue) {
        Write-Host "==> 构建 go_datahub（二进制缺失或 Go 源码已更新）" -ForegroundColor Cyan
        Push-Location (Join-Path $ROOT "layers\business\go_datahub")
        go build -o bin/datahub-server.exe ./cmd/datahub-server
        $buildOk = ($LASTEXITCODE -eq 0)
        Pop-Location
        if (-not $buildOk) { Write-Warning "go build 失败，跳过 Go 层" }
    } else {
        Write-Warning "未找到 go_datahub 二进制且无 Go 环境，跳过 Go 层"
    }
    if (Test-Path $godhExe) {
        $procs += Start-Process -FilePath $godhExe -ArgumentList $godhArgs -PassThru -WindowStyle Hidden
        Write-Host "    go_datahub -> :9300 (PID $($procs[-1].Id))"
    } else {
        Write-Warning "Go 二进制过期但无 Go 环境重建，测试将跑旧行为（或手动构建后重试）"
        $testArgs += "--skip-go"
    }
} else {
    $procs += Start-Process -FilePath $godhExe -ArgumentList $godhArgs -PassThru -WindowStyle Hidden
    Write-Host "    go_datahub -> :9300 (PID $($procs[-1].Id))$(' ' + $(if ($Secure) {'[Bearer 已启用]'} else {''}))"
}

# Python 三层：下游在前、网关最后（网关聚合器启动时连下游拉 tools/list）
foreach ($s in $servers) {
    $procs += Start-Process -FilePath $py `
        -ArgumentList @("-m", $s.Entry, "--transport", "http", "--port", "$($s.Port)") `
        -PassThru -WindowStyle Hidden
    Write-Host "    $($s.Entry) -> :$($s.Port) (PID $($procs[-1].Id))"
}

try {
    Start-Sleep -Seconds $BootWaitSeconds
    Write-Host "`n==> 运行运维测试集" -ForegroundColor Cyan
    # 注意：不要用 $args（PowerShell 自动变量，赋值会被忽略）
    if ($Load -gt 0) { $testArgs += @("--load", "$Load") }
    & $py @testArgs
    $code = $LASTEXITCODE
}
finally {
    Write-Host "`n==> 停止 server" -ForegroundColor Cyan
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
}

exit $code
