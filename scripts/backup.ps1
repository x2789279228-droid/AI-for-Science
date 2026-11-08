# 定时备份（评分表"部署运维"项）：data/（DB+DocIR+上传件）+ checkpoints 断点库
# 计划任务注册（管理员 PowerShell，每天 03:00）：
#   schtasks /Create /TN "MeridianBackup" /SC DAILY /ST 03:00 ^
#     /TR "powershell -ExecutionPolicy Bypass -File D:\传智杯\scholar-agent\scripts\backup.ps1"
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # scholar-agent/
$dest = Join-Path $root ("backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $dest | Out-Null

# 只备 SQLite 主库（WAL 模式下用 VACUUM INTO 保证一致性快照）
$sqlite = Join-Path $root "api\data\scholar.db"
if (Test-Path $sqlite) {
    $tmp = Join-Path $dest "scholar.db"
    & python -c "import sqlite3, sys; sqlite3.connect(sys.argv[1]).execute('VACUUM INTO ?', (sys.argv[2],))" $sqlite $tmp
}
# DocIR / 上传件 / checkpoints
foreach ($rel in @("api\data\docir", "api\data\uploads", "api\data\checkpoints.db")) {
    $src = Join-Path $root $rel
    if (Test-Path $src) { Copy-Item -Recurse -Force $src (Join-Path $dest (Split-Path -Leaf $rel)) }
}

# 保留最近 7 份
Get-ChildItem -Path $root -Directory -Filter "backup-*" |
    Sort-Object Name -Descending |
    Select-Object -Skip 7 |
    Remove-Item -Recurse -Force
Write-Host "✓ 备份完成 → $dest"
