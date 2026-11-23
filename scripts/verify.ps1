# scripts/verify.ps1 — 一键自动验证门禁（单元测试 + 静态检查）
#
# 用法（仓库根目录）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1          # 标准门禁：ruff + pytest + tsc
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1 -Full    # 追加前端生产构建
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1 -SkipFrontend   # 只跑后端
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify.ps1 -SkipBackend    # 只跑前端
#
# 任一步失败 → 退出码 1（适合提交前自检 / CI / make verify）。
# Linux/macOS/CI 请用等价的 scripts/verify.sh。
param(
    [switch]$Full,
    [switch]$SkipBackend,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$script:Failed = 0
$script:Results = @()

function Assert-Tool($Name, $Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Host "  [×] 未找到 $Name —— $Hint" -ForegroundColor Red
        $script:Failed++
        return $false
    }
    return $true
}

function Step($Name, $Dir, $Exe, $StepArgs) {
    Write-Host ""
    Write-Host ("==> [{0}] {1} {2}  (cwd={3})" -f $Name, $Exe, ($StepArgs -join ' '), $Dir) -ForegroundColor Cyan
    if (-not (Assert-Tool $Exe "请先安装（见 docs/如何运行.md 环境要求）")) {
        $script:Results += [PSCustomObject]@{ Step = $Name; Result = "SKIP(缺工具)"; Seconds = 0 }
        return
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    Push-Location (Join-Path $Root $Dir)
    try {
        & $Exe @StepArgs
        $code = $LASTEXITCODE
    }
    finally { Pop-Location }
    $sw.Stop()
    $ok = ($code -eq 0)
    if (-not $ok) { $script:Failed++ }
    $mark = if ($ok) { "PASS" } else { "FAIL" }
    $color = if ($ok) { "Green" } else { "Red" }
    Write-Host ("<== [{0}] {1}  ({2}s)" -f $Name, $mark, [math]::Round($sw.Elapsed.TotalSeconds, 1)) -ForegroundColor $color
    $script:Results += [PSCustomObject]@{ Step = $Name; Result = $mark; Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1) }
}

$total = [System.Diagnostics.Stopwatch]::StartNew()
Write-Host "==== Meridian 一键验证门禁 ====" -ForegroundColor Yellow
Write-Host ("仓库根: {0}" -f $Root)

if (-not $SkipBackend) {
    Step "api/ruff 静态检查"   "api" "uv" @("run", "ruff", "check", "app/", "tests/")
    Step "api/pytest 单元测试" "api" "uv" @("run", "pytest", "-q")
}
if (-not $SkipFrontend) {
    Step "web/tsc 类型检查"    "web" "npx" @("tsc", "--noEmit")
    if ($Full) { Step "web/build 生产构建" "web" "pnpm" @("build") }
}

$total.Stop()
Write-Host ""
Write-Host "==== 验证结果汇总 ====" -ForegroundColor Yellow
$script:Results | ForEach-Object {
    $color = if ($_.Result -like "PASS*") { "Green" } else { "Red" }
    Write-Host ("  {0,-24} {1,-12} {2}s" -f $_.Step, $_.Result, $_.Seconds) -ForegroundColor $color
}
Write-Host ("总耗时 {0}s" -f [math]::Round($total.Elapsed.TotalSeconds, 1))

if ($script:Failed -gt 0) {
    Write-Host ("验证未通过：{0} 步失败" -f $script:Failed) -ForegroundColor Red
    exit 1
}
Write-Host "ALL GREEN ✔ 全部门禁通过" -ForegroundColor Green
exit 0
