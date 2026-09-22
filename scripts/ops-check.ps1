# 运维自检：起三层 server → 跑运维测试集 → 停掉 server。
# 用法：powershell -ExecutionPolicy Bypass -File scripts/ops-check.ps1 [-Load 50]
#   退出码 0 = 全部通过（可直接用于巡检 / CI）。
param(
    [int]$Load = 0,
    [int]$BootWaitSeconds = 10,
    [string]$Python = ""
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

Write-Host "==> 启动三层 server（HTTP）" -ForegroundColor Cyan
$procs = @()
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
    $testArgs = @((Join-Path $ROOT "tests\ops\run_ops_test.py"))
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
