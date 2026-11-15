$s = [Environment]::GetFolderPath('Startup')
$launcher = Join-Path $s 'meridian-prod.bat'
$content = "@echo off" + [Environment]::NewLine + "start `"meridian-prod`" /min cmd /c `"D:\meridian\deploy\start-prod.bat`""
Set-Content -Path $launcher -Value $content -Encoding ASCII
Write-Host "INSTALLED: $launcher"
Get-Content $launcher
