# 运维自检：起三层 server → 跑运维测试集 → 停掉 server。
# 用法：powershell -ExecutionPolicy Bypass -File scripts/ops-check.ps1 [-Load 50]
#   退出码 0 = 全部通过（可直接用于巡检 / CI）。
param(
    [int]$Load = 0,
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

Write-Host "==> 启动三层 server（HTTP）" -ForegroundColor Cyan
$procs = @()
foreach ($s in $servers) {
    $procs += Start-Process -FilePath $py `
        -ArgumentList @("-m", $s.Entry, "--transport", "http", "--port", "$($s.Port)") `
        -PassThru -WindowStyle Hidden
    Write-Host "    $($s.Entry) -> :$($s.Port) (PID $($procs[-1].Id))"
}

# 中层 Go 重活 server：
# - 远端地址（-GodhUrl / MCP_GODATAHUB_URL 非本机）→ 不起停本地，只把地址交给测试集直测
# - 本机默认 → 二进制不存在时自动 go build，无 Go 环境则跳过并让测试集 --skip-go
$godhExe = Join-Path $ROOT "layers\business\go_datahub\bin\datahub-server.exe"
$testArgs = @((Join-Path $ROOT "tests\ops\run_ops_test.py"), "--godh-url", $godh)
if (-not $godhIsLocal) {
    Write-Host "==> go_datahub 使用远端地址：$godh（跳过本机构建/起停）" -ForegroundColor Cyan
} elseif (-not (Test-Path $godhExe)) {
    if (Get-Command go -ErrorAction SilentlyContinue) {
        Write-Host "==> 构建 go_datahub" -ForegroundColor Cyan
        Push-Location (Join-Path $ROOT "layers\business\go_datahub")
        go build -o bin/datahub-server.exe ./cmd/datahub-server
        $buildOk = ($LASTEXITCODE -eq 0)
        Pop-Location
        if (-not $buildOk) { Write-Warning "go build 失败，跳过 Go 层" }
    } else {
        Write-Warning "未找到 go_datahub 二进制且无 Go 环境，跳过 Go 层"
    }
    if (Test-Path $godhExe) {
        $procs += Start-Process -FilePath $godhExe -ArgumentList @("-port", "9300") -PassThru -WindowStyle Hidden
        Write-Host "    go_datahub -> :9300 (PID $($procs[-1].Id))"
    } else {
        $testArgs += "--skip-go"
    }
} else {
    $procs += Start-Process -FilePath $godhExe -ArgumentList @("-port", "9300") -PassThru -WindowStyle Hidden
    Write-Host "    go_datahub -> :9300 (PID $($procs[-1].Id))"
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
