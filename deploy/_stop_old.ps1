foreach ($p in 3003, 8100) {
  Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
}
Start-Sleep 2
$left = netstat -ano | findstr "LISTENING" | findstr "3003 8100"
Write-Host ("remaining listeners: " + @($left).Count)
